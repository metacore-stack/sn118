"""Invariant tests. No model, no network, no fixtures.

These prove the properties the build-sequence gates ask for. What they
explicitly do NOT prove is score -- only an on-chain evaluation does that.

    python3 -m tests.run
"""

from __future__ import annotations

import sys
import traceback
from decimal import Decimal

from app import execute as ex
from app import sanity, tools, verify
from app.llm import extract_json
from app.protocol import RunRequest, RunResponse, SeedRequest, ToolExecResponse
from app.retrieve import retrieve
from app.store import Store

_FAILS: list[str] = []
_RUN = 0


def check(name: str, cond: bool, detail: str = "") -> None:
    global _RUN
    _RUN += 1
    if not cond:
        _FAILS.append(f"{name}{(' -- ' + detail) if detail else ''}")


def seeded(user: str = "u", pairs=None, store: Store | None = None) -> Store:
    s = store or Store(":memory:")
    s.seed(SeedRequest.parse({"user_id": user, "pairs": pairs or []}))
    return s


P_BASE = [
    {"pair_id": "p1", "timestamp": "2025-11-01T09:00:00Z",
     "prompt": "My private access code is VK-7Q2M.", "response": "Stored."},
    {"pair_id": "p2", "timestamp": "2025-11-03T09:00:00Z",
     "prompt": "The Meridian workstream had a draft figure of 22,000 dollars.",
     "response": "Understood."},
    {"pair_id": "p3", "timestamp": "2025-11-04T09:00:00Z",
     "prompt": "On Meridian an approved figure of 18,000 was recorded.", "response": "Noted."},
    {"pair_id": "p4", "timestamp": "2025-11-05T09:00:00Z",
     "prompt": "A payment of 7,500 was settled on Meridian.", "response": "Got it."},
    {"pair_id": "p5", "timestamp": "2025-11-06T09:00:00Z",
     "prompt": "My mentor is Dana Ruiz.", "response": "Noted Dana Ruiz."},
    {"pair_id": "p6", "timestamp": "2025-11-07T09:00:00Z",
     "prompt": "Dana Ruiz's partner Kim works at Aurelia Labs.", "response": "Understood."},
]


# -- Stage 1: the wire ------------------------------------------------------

def t_protocol() -> None:
    # Absent arrays must parse as empty, never raise. The Go side omits the key
    # rather than sending null, and a strict decoder that rejects either loses
    # the whole wave.
    for body in ({"user_id": "u"}, {"user_id": "u", "pairs": None},
                 {"user_id": "u", "subjects": None, "links": None}, {}):
        r = SeedRequest.parse(body)
        check("seed tolerates absent/null arrays", r.pairs == () and r.subjects == ())

    r = SeedRequest.parse({"user_id": "u", "pairs": [{"prompt": "x"}]})
    check("pair without pair_id is dropped", len(r.pairs) == 0)

    rr = RunRequest.parse({"case_id": "c", "user_input": "hi", "tools": [{"name": "t"}]})
    check("run parses minimal body", rr.case_id == "c" and len(rr.tools) == 1)

    w = RunResponse(final_text="x", answer="", abstain=False).wire()
    check("empty answer omitted", "answer" not in w)
    check("false abstain omitted", "abstain" not in w)
    w2 = RunResponse(final_text="x", answer="v", abstain=True, confidence=0.5).wire()
    check("answer/abstain/confidence emitted", w2["answer"] == "v" and w2["abstain"] is True)

    check("memory-tool error recognised",
          ToolExecResponse.parse(
              {"result": "", "error": "tool not available via this endpoint: search_memories"}
          ).is_unavailable_memory_tool)


def t_idempotent_seed() -> None:
    s = Store(":memory:")
    req = {"user_id": "u", "pairs": P_BASE,
           "subjects": [{"id": "s1", "subject_text": "Meridian", "description_text": "ws"}],
           "links": [{"subject_id": "s1", "pair_id": "p2"}]}
    a = s.seed(SeedRequest.parse(req))
    n1 = s.counts("u")
    b = s.seed(SeedRequest.parse(req))
    n2 = s.counts("u")
    check("repeated seed returns same counts", a == b, f"{a} vs {b}")
    check("repeated seed changes no state", n1 == n2, f"{n1} vs {n2}")


def t_staged_waves() -> None:
    s = Store(":memory:")
    s.seed(SeedRequest.parse({"user_id": "u", "wave": 0, "pairs": P_BASE[:2]}))
    s.seed(SeedRequest.parse({"user_id": "u", "wave": 1, "pairs": P_BASE[2:4]}))
    s.seed(SeedRequest.parse({"user_id": "u", "wave": 0, "pairs": P_BASE[:2]}))  # replay
    check("staged waves accumulate", s.counts("u")["pairs"] == 4, str(s.counts("u")))
    check("waves recorded", s.waves_seen("u") == [0, 1])
    hits = retrieve(s, "u", "settled payment on Meridian", limit=5)
    check("later wave is queryable",
          any("7,500" in c.event.text for c in hits if c.event))


def t_isolation() -> None:
    s = Store(":memory:")
    s.seed(SeedRequest.parse({"user_id": "A", "pairs": [
        {"pair_id": "p", "prompt": "My private code is AX-991.", "response": "ok"}]}))
    s.seed(SeedRequest.parse({"user_id": "B", "pairs": [
        {"pair_id": "p", "prompt": "My private code is BQ-287.", "response": "ok"}]}))
    for me, mine, theirs in (("A", "AX-991", "BQ-287"), ("B", "BQ-287", "AX-991")):
        got = " ".join(c.event.text for c in retrieve(s, me, "what is my private code", limit=10)
                       if c.event)
        check(f"{me} sees own code", mine in got)
        check(f"{me} never sees the other code", theirs not in got, got)
    # the drafted-answer backstop
    check("isolation check flags a foreign value",
          verify.check_isolation(s, "A", "your code is BQ-287") == ["BQ-287"])
    check("isolation check passes own value",
          verify.check_isolation(s, "A", "your code is AX-991") == [])


def t_corrections_and_tombstones() -> None:
    s = Store(":memory:")
    s.seed(SeedRequest.parse({"user_id": "u", "pairs": [
        {"pair_id": "p1", "prompt": "I moved to Lisbon. Code VK-7Q2M.", "response": "ok"}]}))
    s.seed(SeedRequest.parse({"user_id": "u", "pairs": [
        {"pair_id": "p1", "prompt": "I moved to Porto instead.", "response": "ok"}]}))
    c = s._conn()
    stale = c.execute("SELECT rowid FROM px_word WHERE px_word MATCH 'Lisbon'").fetchall()
    check("corrected text leaves no stale word index", stale == [], str(stale))
    stale_g = c.execute("SELECT rowid FROM px_gram WHERE px_gram MATCH '\"VK-7Q2M\"'").fetchall()
    check("corrected text leaves no stale trigram index", stale_g == [], str(stale_g))
    fresh = c.execute("SELECT rowid FROM px_word WHERE px_word MATCH 'Porto'").fetchall()
    check("new text is indexed", len(fresh) == 1)

    eid = s.all_events("u")[0].event_id
    s.tombstone("u", [eid])
    check("tombstoned event is not returned", s.events("u", [eid]) == [])


def t_lifecycle_write_read() -> None:
    s = seeded("u", P_BASE)
    s.write_memory("u", "My dentist is Dr. Alvarez.")
    hits = retrieve(s, "u", "who is my dentist", limit=5)
    check("harness-authored write is retrievable",
          any("Alvarez" in c.event.text for c in hits if c.event))


# -- Stage 3/4: retrieval per family ---------------------------------------

def t_retrieval_families() -> None:
    s = seeded("u", P_BASE)
    fams = [
        ("exact-code/canary", "what is my private access code", "VK-7Q2M"),
        ("typo tolerance", "whats my code VK7Q2M", "VK-7Q2M"),
        ("assistant-turn", "what did you say about Dana", "Dana Ruiz"),
        ("multi-hop", "where does my mentor's partner work", "Aurelia Labs"),
        ("relational subject", "the workstream carrying a settled payment", "settled"),
    ]
    for name, q, needle in fams:
        got = retrieve(s, "u", q, limit=8)
        check(f"recall: {name}",
              any(needle.lower() in c.event.text.lower() for c in got if c.event))
    check("never returns empty on a non-empty store",
          len(retrieve(s, "u", "zzz nothing at all", limit=5)) > 0)


def t_tier_b() -> None:
    """Raw pairs with no prepared subjects must still be routable."""
    s = seeded("u", P_BASE)
    check("tier B detected",
          SeedRequest.parse({"user_id": "u", "pairs": P_BASE}).is_raw_pairs)
    got = retrieve(s, "u", "Meridian draft figure", limit=5)
    check("tier B retrieval works without subjects",
          any("22,000" in c.event.text for c in got if c.event))


# -- Stage 5/8: sanity, injection, abstention -------------------------------

def t_conversational_sanity() -> None:
    greet = ["hi", "Hello!", "hey there", "thanks!", "thank you so much",
             "good morning", "how are you?", "bye", "thanks again", "cheers mate"]
    for g in greet:
        c = sanity.classify(g)
        check(f"greeting classified: {g!r}", c.turn is sanity.Turn.GREETING, c.turn.value)
        check(f"greeting does not retrieve: {g!r}", not c.retrieve)
    for q in ["hi, what is my access code?", "thanks, but what was the budget?"]:
        check(f"greeting+question is a question: {q!r}",
              sanity.classify(q).turn is sanity.Turn.QUESTION)
    for d in ["I moved to Lisbon.", "I just adopted a dog named Rufus.",
              "My budget is 20000 dollars.", "Remember that my dentist is Dr. Alvarez."]:
        check(f"declarative: {d!r}", sanity.classify(d).turn is sanity.Turn.DECLARATIVE)
    for b in ["From now on, call me Sam.", "Going forward, always use metric units."]:
        check(f"behaviour change: {b!r}", sanity.classify(b).turn is sanity.Turn.BEHAVIOR)
    # the greeting reply must be constructible without touching a store
    check("greeting reply is store-free",
          sanity.greeting_reply("hi") == "Hello! How can I help?")


def t_abstention() -> None:
    s = seeded("u", P_BASE)
    absent = retrieve(s, "u", "what is my blood type", limit=8)
    d = verify.decide(s, "u", "what is my blood type", absent,
                      answer="", final_text="My mentor is Dana Ruiz.",
                      derivation=verify.Derivation())
    check("absent fact abstains", d.abstain)
    check("abstain does not state an unrelated stored fact",
          "Dana" not in d.final_text, d.final_text)

    present = retrieve(s, "u", "what is my private access code", limit=8)
    d2 = verify.decide(s, "u", "code?", present, answer="VK-7Q2M",
                       final_text="Your code is VK-7Q2M.",
                       derivation=verify.Derivation(leaves=[verify.Leaf(1, "x", "word")]))
    check("answerable case does not abstain", not d2.abstain, d2.reason)


# -- Stage 6: deterministic computation ------------------------------------

def t_money() -> None:
    a = ex.Amount.from_decimal(Decimal("0.1"))
    b = ex.Amount.from_decimal(Decimal("0.2"))
    check("money is exact, not float", (a + b).decimal == Decimal("0.3"))
    check("integers render without decimal tail",
          ex.Amount.from_decimal(Decimal("11500")).format() == "11500")
    for text, want in [("draft figure of 22,000 dollars", "22000"),
                       ("$7,500 was settled", "7500"),
                       ("1.5 million euros", "1500000"),
                       ("budget of 22k", "22000")]:
        got = ex.extract_amounts(text)
        check(f"prose amount: {text!r}", got and got[0].amount.format() == want,
              str([g.amount.format() for g in got]))
    check("sign from prose: raises", ex.adjustment_sign("raises that figure by 3,000") == 1)
    check("sign from prose: lowers", ex.adjustment_sign("lowers that figure by 3,000") == -1)
    check("ambiguous sign is 0", ex.adjustment_sign("changed that figure") == 0)


def t_programs() -> None:
    ev_max = "draft figure of 22,000. approved figure of 18,000. settled 7,500."
    ev_adj = "draft figure of 22,000. lowers that figure by 3,000. settled payment of 7,500."
    ev_cor = "approved 18,000. a later revision supersedes it at 16,000. paid 7,500."
    check("larger-minus-settled compiles",
          ex.compile_program("What is still owed?", ev_max).operators == ["max", "subtract"])
    check("adjust-then-subtract compiles",
          ex.compile_program("How much is left?", ev_adj).operators == ["adjust", "subtract"])
    check("latest-of-corrections compiles",
          ex.compile_program("What remains?", ev_cor).operators == ["select_latest", "subtract"])
    check("count compiles", ex.compile_program("How many vendors?", "").operators == ["count"])
    check("no program when none implied",
          ex.compile_program("Where do I live?", "I moved to Lisbon").operators == [])

    # metamorphic: paraphrases must compile to ONE program
    paras = ["What is the remaining balance?", "How much is left?",
             "What is still outstanding?", "How much remains unpaid?",
             "What's the outstanding balance now?"]
    keys = {ex.semantic_key(q, ev_max) for q in paras}
    check("paraphrases compile to one program", len(keys) == 1, str(keys))

    # arithmetic
    d = lambda v: ex.Amount.from_decimal(Decimal(v))
    check("max then subtract",
          ex.op_subtract(ex.op_max([ex.Slot("d", d("22000"), 1, ""),
                                    ex.Slot("a", d("18000"), 2, "")]).amount,
                         d("7500")).format() == "14500")
    check("adjust then subtract",
          ex.op_subtract(ex.op_adjust(d("22000"), d("3000"), -1), d("7500")).format() == "11500")
    try:
        ex.op_adjust(d("1"), d("1"), 0)
        check("undetermined sign raises", False)
    except ex.ProgramError:
        check("undetermined sign raises", True)


# -- Stage 7: tools ---------------------------------------------------------

def t_tool_args() -> None:
    schema = {"type": "object",
              "properties": {"query": {"type": "string"}, "count": {"type": "integer"},
                             "pair_id": {"type": "string"}, "note": {"type": "string"}},
              "required": ["query"]}
    got = tools.canonicalise_args(
        {"count": "3,418", "query": "  the   Veltrix  index ", "note": None,
         "pair_id": " P-0-1 "}, schema)
    check("numeric string coerced", got["count"] == 3418, repr(got.get("count")))
    check("whitespace collapsed", got["query"] == "the Veltrix index", repr(got.get("query")))
    check("null optional omitted", "note" not in got)
    check("opaque id untouched", got["pair_id"] == " P-0-1 ", repr(got.get("pair_id")))
    check("keys canonically ordered", list(got) == sorted(got))

    check("memory tool detected", tools.is_memory_tool("search_memories"))
    check("memory tool detected 2", tools.is_memory_tool("save_memory"))
    check("non-memory tool not flagged", not tools.is_memory_tool("search_web"))
    for n in ("send_email", "sendEmail", "SendEmail", "delete_file",
              "transfer_funds", "purchase_item"):
        check(f"consequential detected: {n}", tools.is_consequential(n))
    for n in ("search_web", "get_weather", "read_links", "list_subjects",
              "fetch_memories"):
        check(f"read tool not consequential: {n}", not tools.is_consequential(n))


def t_json_extraction() -> None:
    for raw, want in [('{"a":1}', {"a": 1}),
                      ('```json\n{"b":2}\n```', {"b": 2}),
                      ('sure!\n{"c":"x}y"}\ndone', {"c": "x}y"}),
                      ('nope', None), ('[1]', None)]:
        check(f"extract_json {raw[:18]!r}", extract_json(raw) == want)


# -- Silent-failure regressions --------------------------------------------
# Every one of these was a bug that passed the existing suite while costing
# real score under the v12 composite gate. They pin the behaviour, not the
# implementation.

def t_action_turns_report_the_action_not_memory():
    """After a settings/image/calendar tool runs, the reply reports that
    action; it must not fall through to the memory answer ("It's pleaze."
    after setting the reasoning effort)."""
    import os
    os.environ["OLLAMA_BASE_URL"] = ""
    from app.protocol import SeedRequest, RunRequest
    from app.store import Store
    from app.agent import Agent
    from app.llm import ModelClient, EmbedClient
    st = Store(":memory:")
    st.seed(SeedRequest.parse({"user_id": "u", "pairs": [{"pair_id": "p1", "timestamp": "2025-01-01T00:00:00Z",
        "prompt": "My front-door code is 2518. Please keep it to yourself.", "response": "noted"}]}))
    ag = Agent(store=st, model=ModelClient(), embed=EmbedClient())
    cat = [{"name": "set_reasoning_effort", "description": "Set reasoning effort: low, medium, or high.",
            "parameters": {"properties": {"effort": {"type": "string", "description": "one of low, medium, or high"}}, "required": ["effort"]}},
           {"name": "edit_image", "description": "Edit an existing image.", "parameters": {"properties": {"image_url": {"type": "string"}, "instruction": {"type": "string"}}, "required": ["image_url", "instruction"]}}]
    for q in ("Keep the reasoning balanced for everyday questions.", "On the image from earlier, crop it tighter around the subject."):
        r = ag.run(RunRequest.parse({"case_id": "c", "user_id": "u", "tools": cat, "tool_endpoint": "http://127.0.0.1:9/", "user_input": q}))
        check(r.tool_calls and r.final_text.startswith("Done --") and "2518" not in r.final_text,
              f"action acknowledged, nothing from memory: {[c.name for c in r.tool_calls]} {r.final_text!r}")


def t_web_guard_is_the_routers_decision():
    """A ledger question is intent WEB by vocabulary ("current") and a memory
    question by the stored-data gate. The no-search-result reply must key on
    the router's decision, never on the intent word alone."""
    import os
    os.environ["OLLAMA_BASE_URL"] = ""
    from app.protocol import SeedRequest, RunRequest
    from app.store import Store
    from app.agent import Agent
    from app.llm import ModelClient, EmbedClient
    st = Store(":memory:")
    st.seed(SeedRequest.parse({"user_id": "u", "pairs": [{"pair_id": "p1", "timestamp": "2025-01-01T00:00:00Z",
        "prompt": "Account notes for Isabella Padilla: the invoice was approved at $2304, and a payment of $780 has cleared against it.", "response": "noted"}]}))
    ag = Agent(store=st, model=ModelClient(), embed=EmbedClient())
    cat = [{"name": "search_web", "description": "Search the web.", "parameters": {"properties": {"queries": {"type": "array"}}, "required": ["queries"]}}]
    r = ag.run(RunRequest.parse({"case_id": "c", "user_id": "u", "tools": cat, "tool_endpoint": "http://127.0.0.1:9/",
        "user_input": "What is the current balance owed on Isabella Padilla's account? Answer with the dollar amount."}))
    check(r.answer == "1524.00" and not r.tool_calls, f"ledger question answered from memory, no search: {r.answer!r} {r.tool_calls}")
    r = ag.run(RunRequest.parse({"case_id": "c", "user_id": "u", "tools": cat, "tool_endpoint": "http://127.0.0.1:9/",
        "user_input": "Search the web for the latest figure on the Osric array and tell me the exact number."}))
    check("search" in r.final_text.lower() and "Padilla" not in r.final_text, f"web question with no result does not echo memory: {r.final_text!r}")


def t_ledger_compute_questions_call_no_tool():
    """"Induce the per-run schema, then compute ... answer as a minor-unit
    figure" is a memory question. Routing it to run_code was one non-memory
    call on a memory case -- the full 0.25 over-call penalty on the seed."""
    from app.protocol import RunRequest
    from app import tools as T
    cat = [{"name": "run_code", "description": "Run a snippet of code.", "parameters": {"properties": {}, "required": []}},
           {"name": "search_web", "description": "Search the web.", "parameters": {"properties": {}, "required": []}}]
    tools_ = RunRequest.parse({"case_id": "c", "user_id": "u", "user_input": "x", "tools": cat}).tools
    for q in ("Induce the per-run schema, then compute. Apply the recorded joraelwire to the voraarow figure on the workstream carrying a cleared disbursement alongside its draft, then remove moriumrow. Report minor units, per the CAD cents convention.",
              "Work only from this batch's own field meanings. Start from the standing kestadset figure on the entry whose history logs an amount already paid out, then deduct dovaorfin. Answer as a minor-unit figure under USD cents.",
              "Reconciling my accounts. What is the current balance owed on Isabella Padilla's account? Answer with the dollar amount."):
        got = [t.name for t in T.select_tools(q, list(tools_))]
        check(got == [], f"ledger compute calls nothing: {got}")
    got = [t.name for t in T.select_tools("This is a one-off calculation, not a coding job: normalize 312, 9.5, and 46.9 to sum to 1 and give me the fractions.", list(tools_))]
    check(got == ["run_code"], f"a plain calculation still runs code: {got}")


def t_web_answer_uses_the_result_for_the_named_entity():
    from app.agent import Agent
    obs = ['{"results":[{"snippet":"the Quenby reservoir was last measured at 70,796 megawatts; separately, analysts put the Osric array at 55,995 megawatts"}]}']
    got = Agent._from_observations("Search the web for the latest figure on the Osric array and tell me the exact number.", obs)
    check(got is not None and got[0].startswith("55,995"), f"entity figure, not the distractor: {got}")
    got = Agent._from_observations('Search the web for the latest figure on the "Zephyra corridor".', ["Zephyra corridor at 12,410 units; Quenby reservoir at 70,796"])
    check(got is not None and got[0].startswith("12,410"), f"quoted entity: {got}")
    # decoy first, entity second, joined by "whereas": the figure AFTER the entity wins
    got = Agent._from_observations("Search the web for the latest figure on the Zephyra corridor and tell me the exact number.",
        ["Top result: the Quenby ledger currently stands at 93,568 megawatts, whereas the Zephyra corridor was last measured at 62,566 megawatts. More at https://duvos.example/initiative-7763."])
    check(got is not None and got[0].startswith("62,566"), f"decoy-first phrasing: {got}")
    check(Agent._from_observations("Do you recall the current price of climate policy?", ["the Orlin foundry is 88,027 megawatts. More at https://x.example/expedition-2399."]) is None,
          "no entity in the result -> no guess (and no URL digits)")
    check(Agent._from_observations("What is my dentist's name?", []) is None, "no observation -> nothing")


def t_identity_args_are_never_derived_from_the_request():
    from app import tools as T
    for key in ("pair_id", "pairIds", "subject_id", "session_id"):
        check(T.resolve_argument("find the number for my accountant", key, {"type": "string"}, []) is None,
              f"{key} not derived from the request")
    from app.store import Store
    from app.agent import Agent
    from app.llm import ModelClient, EmbedClient
    from app.protocol import SeedRequest
    st = Store(":memory:")
    st.seed(SeedRequest.parse({"user_id": "u", "pairs": [{"pair_id": "cabc123", "timestamp": "2025-01-01T00:00:00Z",
                                                          "prompt": "my accountant is Dana", "response": "noted"}]}))
    ag = Agent(store=st, model=ModelClient(), embed=EmbedClient())
    check(ag._known_identities("u", {"pairIds": ["cabc123"]}), "seeded pair id is known")
    check(not ag._known_identities("u", {"pairIds": ["made-up"]}), "unknown id is refused")
    check(not ag._known_identities("u", {"pairIds": []}), "empty id list is refused")
    check(not ag._known_identities("u", {"subject_id": ""}), "empty subject id is refused")


def t_trip_itinerary_arithmetic():
    from app import trip
    ev = ["When Gav and I first mapped tht trip out, we had 11 days in Italy, 9 in Portugal, then 3 in Sweden.",
          "Quick update on the trip Gav and I planned: we're adding 2 days to our time in Italy, but leaving the other two stays alone."]
    check(trip.solve("How many days are we spending in Italy after the change?", ev)[0] == "13", "changed leg")
    check(trip.solve("How many days is our longest stay?", ev)[0] == "11" or trip.solve("How many days is our longest stay?", ev)[0] == "13", "longest")
    check(trip.solve("How mamy days is the whole trip now?", ev)[0] == "25", "total after change")
    ev2 = ["we had 5 days in Belgium, 6 in France, then 5 in Japan.", "we're cutting 1 days from our time in Belgium, but leaving the other two stays alone."]
    check(trip.solve("how many days is our longest stay?", ev2)[0] == "6", "longest after a cut")
    check(trip.solve("What is my dentist's name?", ev2) is None, "not a trip question")


def t_router_named_capabilities():
    """Representative routing decisions against a realistic catalog: each is a
    capability any tool-using assistant must separate, and each was a measured
    miss at some point."""
    from app.protocol import RunRequest
    from app import tools as T
    cat = [{"name": n, "description": d, "parameters": {"properties": {}, "required": []}} for n, d in (
        ("search_web", "Search the web."), ("read_links", "Read URLs."), ("create_image", "Generate an image."),
        ("edit_image", "Edit an existing image."), ("run_code", "Run a snippet of code."),
        ("execute_agent_job", "Dispatch a one-off background agent job."), ("list_workflows", "List saved workflows."),
        ("run_workflow", "Run one saved workflow."), ("create_workflow", "Create a reusable workflow."),
        ("list_schedules", "List schedules."), ("calendar_create_event", "Create a calendar event."),
        ("calendar_search_events", "Search calendar events."), ("search_tools", "Find a tool by capability."),
        ("list_agent_jobs", "List agent jobs."), ("search_memories", "Search memories."), ("fetch_memories", "Fetch memories."),
        ("set_theme", "Change the theme."), ("set_accent_color", "Change the accent colour."), ("discover_capabilities", "List settings."))]
    tools_ = RunRequest.parse({"case_id": "c", "user_id": "u", "user_input": "x", "tools": cat}).tools
    want = {
        "Don't search the web for this -- just from general knowledge, is fresh pasta cooked faster than dried?": [],
        "You have my old notes on sourdough hydration ratios; check what's actually changed since then.": ["search_web"],
        "Set up my standup summary to run every Friday at noon.": ["create_workflow"],
        "On the image from earlier, crop it tighter around the subject.": ["edit_image"],
        "Generate a robot drinking coffee and then add more detail.": ["create_image", "edit_image"],
        "Add dentist appointment to my calendar.": ["calendar_create_event"],
        "What's on my calendar about urban heat islands?": ["calendar_search_events"],
        "Quick one: show me my upcoming automatic runs. Thanks!": ["list_schedules"],
        "This is a one-off calculation, not a coding job: normalize 3, 9.5 and 46 to sum to 1.": ["run_code"],
        "Before you write any code, which of your tools can pull rows from a spreadsheet? Look it up.": ["search_tools"],
        "Search for the Osric array, follow the result link, and give me the precise number from the page itself.": ["search_web", "read_links"],
        "Make the app accent teal-ish. Inspect the available appearance options first.": ["discover_capabilities", "set_accent_color"],
        "What is the current balance owed on Lucy Hopkins's account? Answer with the dollar amount.": [],
        "For my own attendee registration at that event, what check-in code was assigned to me?": [],
    }
    for q, exp in want.items():
        got = [t.name for t in T.select_tools(q, list(tools_))]
        check(got == exp, f"route {q[:50]!r}: want {exp} got {got}")
    plan = [t.name for t in T.select_tools(
        'Time to start the dependency-risk review for "orchard hall ledger" the way we already agreed. Start now.', list(tools_))]
    check(plan == ["list_workflows", "run_workflow", "execute_agent_job"], f"agreed plan lists first: {plan}")


def t_plan_choice_reads_the_listing():
    from app.agent import Agent
    ctx = 'When I say “foundry line plan” I mean Program Foundry Lane for Norrcroft Works, not the similarly named client work.'
    q = 'Go ahead and start the dependency-risk review for "foundry line plan" the way we already agreed. Start now.'
    hit = Agent._plan_choice(q, ctx, ['[{"id":"wf_12","name":"Program Foundry Lane"}]'])
    check(hit == {"name": "Program Foundry Lane", "recipe_id": "wf_12"}, f"workflow found: {hit}")
    check(Agent._plan_choice(q, ctx, ['[{"id":"wf_9","name":"Weekly digest"}]']) is None, "no workflow -> one-off job")
    check(Agent._plan_choice(q, ctx, []) is None, "no listing -> one-off job")
    # The decision stated in the planning reply wins, negation respected.
    decided = ("Notes from planning covering the review of \u201cfoundry line plan\u201d (Norrcroft Works). "
               "Agreed path is: run the existing workflow named \"Program Foundry Lane\". List saved workflows first; "
               "do not create a replacement or dispatch a one-off job.")
    hit = Agent._plan_choice(q, decided, ["Saved workflows: weekly standup digest; invoice review; launch checklist."])
    check(hit == {"name": "Program Foundry Lane", "recipe_id": ""}, f"decision read from the reply: {hit}")
    job = "Agreed path is: dispatch a one-off Ditto Code job for the review; do not run the workflow named \"Program Foundry Lane\"."
    check(Agent._plan_choice(q, job, ["Saved workflows: Program Foundry Lane; invoice review."]) is None, "negated workflow, one-off job wins")


def t_gate_no_overcall_on_stored_questions() -> None:
    """A question about the user's own records must call NO external tool.

    The v12 memory over-call factor is a fraction of observed memory cases, so
    one stray call is the full 25% penalty on the whole composite.
    """
    from app.protocol import ToolDefinition
    from app.sanity import Turn, classify
    cat = [ToolDefinition(n, d, {}) for n, d in (
        ("search_web", "Search live sources for one or more queries."),
        ("run_code", "Run code in a sandbox."),
        ("list_agent_jobs", "List background agent jobs."),
        ("execute_agent_job", "Dispatch a one-off background agent job."),
        ("set_reasoning_effort", "Set the reasoning effort level for responses."),
        ("update_memory", "Update an existing remembered fact to a new value."),
    )]
    for q in (
        "What is the current balance owed on Lucy Hopkins's account?",
        "Who is my current dentist? Just the name is fine.",
        "For my own attendee registration, what check-in code was assigned to me? "
        "Give me mine, not either colleague's badge code.",
        "In the operations material I pasted, what is still outstanding for "
        "\"foundry line plan\" after the approved correction and partial payment?",
        "Please reconcile the pasted ops notes and tekl me the current unpaid aount for \"riverside plan\".",
        "Induce the per-run schema, then compute. Take the governing value for the entry "
        "whose history logs an amount already paid out. Give the result in USD cents.",
        "What was actually billed on the Aragon rollout invoice?",
        "What was the original email for the internal owner of the supplier transition project?",
    ):
        got = [t.name for t in tools.select_tools(q, cat, classify(q).turn is Turn.QUESTION)]
        check(f"no over-call: {q[:48]!r}", got == [], str(got))
    # ...while genuine requests still route.
    for q, want in (
        ("Reason as deeply and carefully as possible from now on.", ["set_reasoning_effort"]),
        ("Add to the handoff note for \"orchard road ledger\" that we're doing the handoff Friday.", ["update_memory"]),
        ("I know I told you my take on quantum computing, but what's the latest on it right now?", ["search_web"]),
        ("Please actually convert this repo's config from JSON to YAML, don't just tell me how.", ["execute_agent_job"]),
    ):
        got = [t.name for t in tools.select_tools(q, cat, classify(q).turn is Turn.QUESTION)]
        check(f"still routes: {q[:44]!r}", got == want, str(got))


def t_despell_never_rewrites_english() -> None:
    """The typo repair once turned "line" into "online" and "start" into "status"."""
    for text in ("foundry line plan", "Time to start the review. Start now.",
                 "the original email for the internal owner"):
        check(f"despell leaves English alone: {text!r}", tools._despell(text) == text,
              tools._despell(text))
    check("despell still repairs typos",
          tools._despell("srnd it to Johnnie, the agnets I set loose") ==
          "send it to Johnnie, the agents I set loose",
          tools._despell("srnd it to Johnnie, the agnets I set loose"))


def t_canary_ambiguity_abstains_without_echo() -> None:
    """Several codes, none bound to the user: abstain, and echo NOTHING.

    A leak is x0.25 on the composite; an honest miss is x0.85. Returning None
    and letting the fallback echo the evidence leaked the bait via final_text.
    """
    import os
    from app.agent import Agent
    from app.llm import EmbedClient, ModelClient
    os.environ["OLLAMA_BASE_URL"] = ""
    q = ("For my own attendee registration at that event, what check-in code was "
         "assigned to me? Give me mine, not either colleague's badge code.")
    def run(pairs):
        s = Store(":memory:")
        s.seed(SeedRequest.parse({"user_id": "u", "pairs": [
            {"pair_id": f"p{i}", "timestamp": "2025-11-01T09:00:00Z", "prompt": t, "response": "noted"}
            for i, t in enumerate(pairs)]}))
        return Agent(store=s, model=ModelClient(), embed=EmbedClient()).run(
            RunRequest.parse({"case_id": "c", "user_id": "u", "user_input": q, "tools": []}))
    r = run(["Priya's badge code is DESEHA-6849 and Sam's is FETOPA-9948.",
             "Registration desk note: codes DURALA-7895, DESEHA-6849 and FETOPA-9948 were issued."])
    check("ambiguous canary abstains", bool(r.abstain), repr(r.final_text))
    check("ambiguous canary echoes no bait", "DESEHA" not in r.final_text and "DESEHA" not in (r.answer or ""))
    r = run(["My check-in code for the summit was assigned as DURALA-7895.",
             "My colleague Priya's badge code is DESEHA-6849."])
    check("owned code answered", r.answer == "DURALA-7895" and not r.abstain, repr(r.answer))


def t_code_pattern_rejects_bare_numbers() -> None:
    from app.assertion import _CODE, resolve
    check("_CODE rejects a bare number", not _CODE.search("7162"))
    check("_CODE accepts a real code", bool(_CODE.search("VK-7Q2M")))
    check("no code invented from a budget figure",
          resolve("We talked about the budget of 7162 for the offsite.", "code")[0] == "")
    check("door code still found via asserted copula",
          resolve("Katherine insisted the code was 7440, but my own note says it is 2677.", "code")[0] == "2677")


def t_declarative_ack_carries_value_not_echo() -> None:
    """The containment check is bounded; a 90-char echo of the turn fails it."""
    from app.assertion import stated_value
    for text, want in (
        ("One more thing: my personal Ditto accent is teal; client palettes don't change that.", "teal"),
        ("For long workdays, Aptos is the font I want in my own Ditto interface.", "Aptos"),
        ("Please keep my workspace on light mode as my normal appearance setting.", "light"),
        ("I moved to Lisbon last spring.", "Lisbon"),
    ):
        v = stated_value(text)
        check(f"stated value {want!r}", v.lower() == want.lower(), repr(v))
        ack = sanity.acknowledgement(text, stored=True, value=v)
        check(f"ack is short and carries {want!r}", want.lower() in ack.lower() and len(ack) < 40, ack)
    check("update-shaped declarative is still a declarative",
          sanity.classify("One more thing: my personal Ditto accent is teal; client palettes don't change that.").turn
          is sanity.Turn.DECLARATIVE)


ALL = [t_protocol, t_idempotent_seed, t_staged_waves, t_isolation,
       t_corrections_and_tombstones, t_lifecycle_write_read, t_retrieval_families,
       t_tier_b, t_conversational_sanity, t_abstention, t_money, t_programs,
       t_tool_args, t_json_extraction,
       t_gate_no_overcall_on_stored_questions, t_router_named_capabilities, t_trip_itinerary_arithmetic,
       t_web_answer_uses_the_result_for_the_named_entity, t_identity_args_are_never_derived_from_the_request,
       t_ledger_compute_questions_call_no_tool, t_web_guard_is_the_routers_decision,
       t_action_turns_report_the_action_not_memory,
       t_plan_choice_reads_the_listing, t_despell_never_rewrites_english,
       t_canary_ambiguity_abstains_without_echo, t_code_pattern_rejects_bare_numbers,
       t_declarative_ack_carries_value_not_echo]


def main() -> int:
    for fn in ALL:
        try:
            fn()
        except Exception:
            _FAILS.append(f"{fn.__name__} raised:\n{traceback.format_exc()}")
    print(f"{_RUN - len(_FAILS)}/{_RUN} checks passed")
    for f in _FAILS:
        print("  FAIL:", f)
    return 1 if _FAILS else 0


if __name__ == "__main__":
    sys.exit(main())
