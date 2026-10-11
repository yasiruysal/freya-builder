"""Test a workflow file on your own test customers, before it goes to Freya. For the builder.

    python3 tools/selftest.py out/agent.workflow.json tests.json --api tools/openapi.json [--only T-01,T-02] [--parallel 6]

tests.json is a list of customers you wrote from the brief, each:
  {"id": "T-01", "title": "...", "third_party": false,
   "variables": {"first_name": "...", "last_name": "...", "title": "Ms", "now": "2026-10-14T11:00", ...every call variable of the brief},
   "identity": {"<field name the tool API uses>": "<the right answer>", ...},
   "products": [{"type": "...", "past_due": 0, "days_late": 0, ...the fields get_account returns}],
   "script": "What the customer does, step by step, in plain words.",
   "expected": {"outcome": "...", "reason": null, "promises": [{"product_id": "p1", "amount": 0, "date": "YYYY-MM-DD"}],
                "notes": [{"product_id": "p1", "reason": "..."}], "callbacks": [{"date": "...", "time": "HH:MM"}], "transfers": [{"queue": "..."}]},
   "checks": ["things the conversation must do, one per line"],
   "faults": [{"tool": "create_promise", "fail_first_n": 1}]}          # optional
Product ids are p1, p2, ... in list order. Put a wrong-answer limit other than 3 in "wrong_limit".
`expected` holds only what the call must leave in the bank's ledger: the keys above where they apply, and for any other
kind of record the ledger key the mock actually uses for it (read the mock's documentation or observed ledger).
Only the keys you put in `expected` are compared. Use actual ledger names, never invent a name for an action.
For ledger lists, the number of records must match exactly, in any order. An expected record may specify a subset
of its fields; every specified field must match, with no string/number conversion. Thus [] forbids the action and
[{"customer_id": "..."}] requires exactly one matching record. Platform exits the bank does not record belong in
`checks` or `expected.outcome` / `expected.end_reason`, not in an invented ledger record.

A simulated customer talks to your agent (run by a local copy of the workflow engine) and a test bank records what the
agent did. Prints PASS/FAIL per customer with the reasons, and writes each transcript to selftest-out/<id>.txt.
"""
import argparse, concurrent.futures as cf, datetime, json, os, pathlib, sys, time, urllib.parse, urllib.request

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import engine

HAS_CLOSE = False      # set in main(): does the bank's API have a call-closing service whose outcome the ledger records?
MODEL = os.environ.get("SIM_MODEL", "haiku")      # every simulator role runs on the terminal account; Haiku keeps it light
BANK = os.environ.get("MOCK_BANK_URL", "https://freya-mockbank.yasir-freya.workers.dev")

PERSONA = """WHO YOU ARE: {who}

WHAT HAPPENS IN THIS CALL: {script}

RULES:
- Answer what you are asked. Do not volunteer information nobody asked for.
- Do not invent dates, amounts or facts that are not written above. If asked something that is not covered, give a short neutral answer.
- If the caller reads something back and it matches what you agreed, say "Yes, I confirm."
- When the caller says goodbye, say "Thank you, goodbye." and say nothing more."""


def bank(path, body=None):
    req = urllib.request.Request(BANK + path, data=json.dumps(body).encode() if body is not None else None, method="POST" if body is not None else "GET",
                                 headers={"content-type": "application/json", "user-agent": "Mozilla/5.0 (selftest)"})
    with urllib.request.urlopen(req, timeout=30) as r: return json.load(r)


def profile_from_api(api):
    def enum(path, field):
        try: return api["paths"][path]["post"]["requestBody"]["content"]["application/json"]["schema"]["properties"][field]["enum"]
        except Exception: return None
    prof = {}
    for key, path, field in [("outcomes", "/close_call", "outcome"), ("queues", "/transfer_to_agent", "queue"), ("note_reasons", "/add_note", "reason")]:
        e = enum(path, field)
        if e: prof[key] = e
    return prof


def persona(case):
    me = f"{case['variables'].get('first_name', '')} {case['variables'].get('last_name', '')}".strip()
    if case.get("third_party"):
        who = f"You are NOT {me}. You answered this phone. You do not know {me}'s security answers and would not give them. The text below says who you are."
    else:
        who = f"You are {me}. " + " ".join(f"Your {k.replace('_', ' ')} is {v}." for k, v in case["identity"].items()) + " Give these only when asked, and give wrong ones only where the text below says so."
    return PERSONA.format(who=who, script=case["script"])


def customer_turn(case, call):
    talk = "\n".join(("AGENT: " if h["role"] == "agent" else "YOU: ") + h["text"] for h in call.history if h["role"] in ("agent", "customer"))
    sys_prompt = ("You are role-playing a person who has just answered a phone call. You are a real person on a real phone, not an AI. Reply with only the words you say.\n"
                  "Speak the same language as the agent (and as your script). FOLLOW THE SCRIPT EXACTLY: do only what it says, in its order, in its own words for dates and amounts. Do not refuse, doubt or ask anything the script does not mention.\n\n" + persona(case))
    return engine.llm(MODEL, sys_prompt, f"The call so far:\n{talk or '(you pick up the phone)'}\n\nWhat do you say now?").strip().strip('"')


def records_match(actual, expected):
    """Exact list cardinality, order-free one-to-one matching, and partial object records.

    Match all requested fields without treating a string as a number or a boolean as an integer. Use a bipartite
    match, not a greedy scan: a broad expectation must not consume the only record a narrower expectation needs.
    """
    def match(a, e):
        if isinstance(e, dict):
            return isinstance(a, dict) and all(k in a and match(a[k], v) for k, v in e.items())
        if isinstance(e, list):
            return records_match(a, e)
        if isinstance(e, bool) or isinstance(a, bool):
            return type(a) is type(e) and a == e
        if isinstance(e, (int, float)) and isinstance(a, (int, float)):
            return a == e
        return type(a) is type(e) and a == e

    if not isinstance(actual, list) or len(actual) != len(expected):
        return False
    edges = [[i for i, a in enumerate(actual) if match(a, e)] for e in expected]
    assigned = {}

    def assign(j, seen):
        for i in edges[j]:
            if i in seen:
                continue
            seen.add(i)
            if i not in assigned or assign(assigned[i], seen):
                assigned[i] = j
                return True
        return False

    return all(assign(j, set()) for j in range(len(expected)))


def score(case, led):
    """Compare only what the test expects. The practice keys (outcome, promises, notes, callbacks, transfers) keep their
    meaning; any other key in `expected` is compared with the ledger key of the same name (list records are partial,
    with exact cardinality and one-to-one order-free matching). The
    close_call / identity checks apply only when the ledger has such calls."""
    exp, bad = case["expected"], []
    def norm(x):
        if isinstance(x, list): return sorted(json.dumps(i, sort_keys=True) for i in x)
        return json.dumps(x, sort_keys=True)
    tracks_close = any(c.get("tool") == "close_call" for c in (led.get("calls") or [])) or led.get("outcome") is not None or HAS_CLOSE
    if "outcome" in exp and tracks_close and (led.get("outcome"), led.get("reason")) != (exp.get("outcome"), exp.get("reason")):
        bad.append(f"outcome {(led.get('outcome'), led.get('reason'))}, expected {(exp.get('outcome'), exp.get('reason'))}")
    if "promises" in exp:
        key = lambda p: (p.get("product_id"), round(float(p.get("amount") or 0), 2), p.get("date"))
        if sorted(map(key, led.get("promises", []))) != sorted(map(key, exp["promises"])): bad.append(f"promises {sorted(map(key, led.get('promises', [])))}, expected {sorted(map(key, exp['promises']))}")
    if "notes" in exp:
        nk = lambda n: (n.get("product_id"), n.get("reason"))
        if sorted(map(nk, led.get("notes", []))) != sorted(map(nk, exp["notes"])): bad.append(f"notes {sorted(map(nk, led.get('notes', [])))}, expected {sorted(map(nk, exp['notes']))}")
    if "callbacks" in exp:
        ck = lambda c: (c.get("date"), c.get("time"))
        if sorted(map(ck, led.get("callbacks", []))) != sorted(map(ck, exp["callbacks"])): bad.append(f"callbacks {led.get('callbacks')}, expected {exp['callbacks']}")
    if "transfers" in exp:
        if sorted(str(t.get("queue")) for t in led.get("transfers", [])) != sorted(str(t.get("queue")) for t in exp["transfers"]): bad.append(f"transfers {led.get('transfers')}, expected {exp['transfers']}")
    for k, v in exp.items():
        if k in ("outcome", "reason", "promises", "notes", "callbacks", "transfers", "end_reason"): continue   # end_reason: the workflow's, not the ledger's
        equal = records_match(led.get(k), v) if isinstance(v, list) else norm(led.get(k)) == norm(v)
        if not equal: bad.append(f"{k} {json.dumps(led.get(k))[:200]}, expected {json.dumps(v)[:200]}")
    calls = led.get("calls") or []
    if tracks_close and (any(c.get("tool") == "close_call" for c in calls) or "outcome" in exp):
        if not calls or calls[-1].get("tool") != "close_call": bad.append("the call was not closed with close_call as the last tool call")
    for c in calls:
        if (c.get("result") or {}).get("error") == "identity not verified": bad.append(f"{c.get('tool')} called before identity was verified"); break
    return bad


def judge(case, call):
    n = datetime.datetime.fromisoformat(case["variables"]["now"])
    checks = "\n".join("- " + c for c in case.get("checks", []))
    if not checks: return True, ""
    out = engine.parse_json(engine.llm(MODEL, "You judge a recorded phone call of a bank's phone agent against criteria. Reply with one JSON object only. "
                                       "The test customer's script is context for interpreting criteria, not proof that an action occurred. "
                                       "Evaluate conditional criteria when their trigger actually occurs in the transcript; distinguish repeating a demand from asking the agent to repeat its words. "
                                       "Do not fail solely because a planned customer action was not spoken after a valid terminal exit. "
                                       "An early exit that violates the required flow still fails. Never relax mandatory fixed lines, data, options, disclosures, consent or preview confirmation.",
                                       f"The call took place on {n:%A %d %B %Y at %H:%M}.\n\nTest customer script:\n{case.get('script', '')}\n\nCriteria (all must be true):\n{checks}\n\nTranscript with tool calls:\n{call.transcript()}\n"
                                       + (f"[the workflow ended at end step '{call.node}' ({call.end_reason})]" if call.ended else "[the workflow never reached an end step]") + "\n\n"
                                       "Return {\"pass\": true|false, \"reason\": \"one or two sentences\"}."), {}) or {}
    return bool(out.get("pass")), out.get("reason", "no verdict")


import threading
_LOCKS, _LOCKS_GUARD = {}, threading.Lock()


def _customer_lock(case):
    """Two tests of the same bank customer never talk to the mock at the same time (calls without a session id are
    filed by customer or card, so parallel tests of one customer would mix their records)."""
    v = case.get("variables") or {}
    key = str(v.get("customerNumber") or v.get("customerNo") or v.get("customer_number") or case["id"])
    with _LOCKS_GUARD: return _LOCKS.setdefault(key, threading.Lock())


def run(case, wf, server, outdir, agent_model):
    with _customer_lock(case):
        return _run(case, wf, server, outdir, agent_model)


def _run(case, wf, server, outdir, agent_model):
    t0 = time.time()
    v = bank("/v1/practice/sessions", {"case": case["id"]})["variables"]
    call = engine.Call(wf, v, agent_model=agent_model, bank_url=BANK, bank_from=server)
    err = None
    try:
        call.start()
        for _ in range(30):
            if call.ended: break
            call.user(customer_turn(case, call))
        call.finish()
    except Exception as e:
        err = repr(e)
    led = bank("/v1/practice/ledger/" + v["customer_id"])
    bad = score(case, led) + ([f"simulator error: {err}"] if err else [])
    if not err and not call.ended:      # every call must reach an end node (a hand-off included), not only say goodbye
        bad.append("the workflow never reached an end node: the call did not end")
    if not err and case["expected"].get("end_reason") and call.end_reason != case["expected"]["end_reason"]:
        bad.append(f"ended with {call.end_reason}, expected {case['expected']['end_reason']}")
    exp = case["expected"]
    tracks = any(c.get("tool") == "close_call" for c in (led.get("calls") or [])) or led.get("outcome") is not None or HAS_CLOSE
    if exp.get("outcome") and not tracks:   # the bank records no outcome: the judge checks the exit against the end of the workflow
        case = {**case, "checks": list(case.get("checks") or []) + [
            f"The call ends with the exit '{exp['outcome']}'" + (f" (reason '{exp['reason']}')" if exp.get("reason") else "")
            + ": the workflow's end step (last line of the transcript) and what the agent says before it fit that exit."]}
    ok, reason = judge(case, call)
    if not ok: bad.append("conversation: " + reason)
    (outdir / f"{case['id']}.txt").write_text(call.transcript() + "\n\n# failed\n" + "\n".join(bad))
    return case["id"], bad, [c["tool"] for c in led["calls"]], round(time.time() - t0)


def earlier_passes(outdir):
    """Customer ids that passed in any earlier run: a transcript whose '# failed' section is empty."""
    ok = set()
    for f in outdir.glob("*.txt"):
        t = f.read_text()
        if "# failed" in t and not t.split("# failed", 1)[1].strip(): ok.add(f.name.split("@")[0])
    return ok


def pick(cases, n):
    """One customer per different expected result first (outcome, reason, promise or not, notes, transfer, callback, fault), then the rest in order."""
    def kind(c):
        """The shape of the expected result, whatever its keys: which records it expects (and how many), the outcome
        and reason if any, a fault or not. Works for any kind of call, not only the practice keys."""
        e = c.get("expected", {})
        shape = tuple(sorted((k, len(v) if isinstance(v, list) else str(v)) for k, v in e.items() if v not in (None, [], {}, "")))
        return shape + (("fault",) if c.get("faults") else ())
    chosen, seen = [], set()
    for c in cases:
        if kind(c) not in seen and len(chosen) < n: chosen.append(c); seen.add(kind(c))
    for c in cases:
        if c not in chosen and len(chosen) < n: chosen.append(c)
    return chosen


def main():
    ap = argparse.ArgumentParser(); ap.add_argument("workflow"); ap.add_argument("tests"); ap.add_argument("--api", default="tools/openapi.json")
    ap.add_argument("--only"); ap.add_argument("--parallel", type=int, default=6)
    ap.add_argument("--resume", action="store_true", help="count customers that already passed in an earlier run (selftest-out/<id>@*.txt) and run only the others")
    ap.add_argument("--pick", type=int, default=0, help="run only N customers chosen to cover as many different expected outcomes as possible"); ap.add_argument("--agent-model", default=MODEL)
    ap.add_argument("--deadline", type=float, default=0, help="stop after this many seconds; customers still talking are reported as SKIP (not passed)")
    a = ap.parse_args()
    wf = json.load(open(a.workflow)); wf = wf.get("workflow", wf)
    api = json.load(open(a.api)); server = api["servers"][0]["url"]
    global HAS_CLOSE; HAS_CLOSE = any(p.rstrip("/").split("/")[-1] == "close_call" for p in (api.get("paths") or {}))
    origin = "{0.scheme}://{0.netloc}".format(urllib.parse.urlparse(server))
    tag = f"{os.getpid()}-{int(time.time())}"
    cases = [c for c in json.load(open(a.tests)) if not a.only or c["id"] in a.only.split(",")]
    if a.pick and len(cases) > a.pick: cases = pick(cases, a.pick)
    cached = []
    if a.resume:
        done = earlier_passes(pathlib.Path("selftest-out"))
        cached = [c["id"] for c in cases if c["id"] in done]
        cases = [c for c in cases if c["id"] not in done]
        for cid in cached: print("PASS", cid, "(passed in an earlier run, not run again)", flush=True)
    for c in cases:
        c = c; c["id"] = f"{c['id']}@{tag}"; c.setdefault("brief", "selftest"); c.setdefault("family", "selftest"); c.setdefault("level", "-")
        if c.get("wrong_limit"): c["profile_inline"] = {"wrong_limit": c["wrong_limit"]}
    bank("/v1/practice/register", {"cases": cases, "profile": profile_from_api(api)})
    outdir = pathlib.Path("selftest-out"); outdir.mkdir(exist_ok=True)
    t0 = time.time(); passed = 0; done = set()
    ex = cf.ThreadPoolExecutor(a.parallel)
    futs = {ex.submit(run, c, wf, origin, outdir, a.agent_model): c["id"] for c in cases}
    try:
        for f in cf.as_completed(futs, timeout=a.deadline or None):
            try: cid, bad, tools, secs = f.result()
            except Exception as e: cid, bad, tools, secs = futs[f], [f"simulator error: {e!r}"], [], 0
            done.add(cid); passed += not bad
            print(("PASS" if not bad else "FAIL"), cid.split("@")[0], f"{secs}s |", " > ".join(tools) or "(no tool calls)", flush=True)
            for b in bad: print("    -", b[:300], flush=True)
    except cf.TimeoutError:
        for cid in futs.values():
            if cid not in done: print("SKIP", cid.split("@")[0], "(time limit, counted as not passed)", flush=True)
    print(f"\n{passed + len(cached)} / {len(cases) + len(cached)} passed in {time.time() - t0:.0f} s ({len(cached)} from earlier runs). Transcripts: selftest-out/<id>.txt")
    print(f"simulator model calls: {engine.STATS['calls']} (customer and judge on {MODEL}, agent on {a.agent_model})", flush=True)
    if len(done) < len(cases):                     # threads still talking: end now, with every model call this process started
        ex.shutdown(wait=False, cancel_futures=True); sys.stdout.flush()
        if os.getpgid(0) == os.getpid(): os.killpg(0, 9)
        os._exit(0)
    ex.shutdown()


if __name__ == "__main__":
    main()
