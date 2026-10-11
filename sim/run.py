"""Run cases against a workflow file on the local simulator, with our mock bank, and log them like tools/test.py.

    python3 sim/run.py <builder> <workflow.json> --brief A [--only PUB-01,CUS-04] [--agent-model sonnet] [--customer-model haiku]
                       [--judge-model sonnet] [--parallel 6] [--style talkative]

The agent runs in sim/engine.py, the customer and the judge are Claude models through the `claude` CLI,
the bank is our mock (MOCK_BANK_URL). Transcripts land in results/sim/<run id>/.
"""
import argparse, concurrent.futures as cf, glob, json, os, pathlib, sys, time, urllib.request

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path[:0] = [str(ROOT / "sim"), str(ROOT / "tools"), str(ROOT)]
import engine
from run_cases import persona_for
from score import score_ledger
from test import log_rows, board

BANK = os.environ.get("MOCK_BANK_URL", "https://freya-mockbank.yasir-freya.workers.dev")


def bank(path, body=None):
    req = urllib.request.Request(BANK + path, data=json.dumps(body).encode() if body is not None else None, method="POST" if body is not None else "GET",
                                 headers={"content-type": "application/json", "user-agent": "Mozilla/5.0 (freya-sim)"})
    with urllib.request.urlopen(req, timeout=30) as r: return json.load(r)


def customer_turn(model, persona, call):
    talk = "\n".join(("AGENT: " if h["role"] == "agent" else "YOU: ") + h["text"] for h in call.history if h["role"] in ("agent", "customer"))
    sys_prompt = ("You are role-playing a person who has just answered a phone call. Stay in character the whole time; you are a real person on a real phone, "
                  "not an AI and not in a chat. Reply with only the words you say, nothing else.\n\n"
                  "FOLLOW THE SCRIPT EXACTLY. It is a test: do only what 'WHAT HAPPENS IN THIS CALL' says, in that order. "
                  "Use the script's own words for dates and amounts (if it says 'next Friday', say 'next Friday', never turn it into a date). "
                  "Give a wrong answer only where the script says so, and the right one otherwise. Do not refuse, doubt or ask anything the script does not mention. "
                  "If the script says you are confused, unclear or someone else, stay that way and never confirm you are the customer.\n\n" + persona)
    return engine.llm(model, sys_prompt, f"The call so far:\n{talk or '(the phone rings, you pick up)'}\n\nWhat do you say now?").strip().strip('"')


def now_words(case):
    import datetime
    n = datetime.datetime.fromisoformat(case["variables"]["now"])
    return n.strftime("%A %d %B %Y at %H:%M")


def judge(model, case, call, tries=2):
    checks = "\n".join("- " + c for c in case["checks"])
    for _ in range(tries):
        ok, reason = judge_once(model, case, call, checks)
        if reason != "no verdict": return ok, reason
    return ok, reason


def judge_once(model, case, call, checks):
    out = engine.parse_json(engine.llm(model, "You judge a recorded phone call of a bank's phone agent against criteria. Reply with one JSON object only.",
                                       f"The call took place on {now_words(case)}. Judge all dates against that day, not today's real date.\n\n"
                                       f"Criteria (all must be true):\n{checks}\n\nCall transcript with the agent's tool calls:\n{call.transcript()}\n\n"
                                       "Return {\"pass\": true|false, \"reason\": \"one or two sentences\"}."), {}) or {}
    return bool(out.get("pass")), out.get("reason", "no verdict")


def run_case(case, wf, a, outdir):
    t0 = time.time()
    v = bank("/v1/practice/sessions", {"case": case["id"]})["variables"]
    persona = persona_for(case, {"identity": case["identity"]}, a.style)["prompt"]
    # The platform's customer model says dates and numbers fully in words; the weak agent must cope with that.
    persona += "\n\nHOW YOU SAY THINGS: say dates fully in words, like 'June third, nineteen eighty-seven', and digits one by one in words, like 'four four seven one'."
    call = engine.Call(wf, v, agent_model=a.agent_model, bank_url=BANK)
    error = None
    try:
        said = call.start()
        if not said: call.history.append({"role": "agent", "text": ""})
        for _ in range(30):
            if call.ended: break
            reply = customer_turn(a.customer_model, persona, call)
            said = call.user(reply)
        call.finish()
    except Exception as e:
        error = repr(e)
    led = bank("/v1/practice/ledger/" + v["customer_id"])
    r = score_ledger(case, led)
    if error: r["failed"].append("simulator error: " + error)
    ok, reason = judge(a.judge_model, case, call)
    if not ok: r["failed"].append("conversation: " + reason)
    (outdir / f"{case['id']}.txt").write_text(call.transcript() + "\n\n# events\n" + "\n".join(json.dumps(e) for e in call.events) + "\n\n# failed\n" + "\n".join(r["failed"]))
    return {"id": case["id"], "level": case["level"], "family": case["family"], "pass": not r["failed"], "failed": r["failed"],
            "call_id": None, "tools": [x["tool"] for x in led["calls"]], "customer_id": v["customer_id"], "seconds": round(time.time() - t0)}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("builder"); ap.add_argument("workflow"); ap.add_argument("--brief", default="A"); ap.add_argument("--only"); ap.add_argument("--style")
    ap.add_argument("--agent-model", default="haiku"); ap.add_argument("--customer-model", default="haiku"); ap.add_argument("--judge-model", default="haiku")
    ap.add_argument("--parallel", type=int, default=8); ap.add_argument("--set"); ap.add_argument("--builder-model", default="")
    a = ap.parse_args()
    wf = json.load(open(a.workflow)); wf = wf.get("workflow", wf)
    cases = [c for f in [ROOT / "public-test-set.json", *sorted(glob.glob(str(ROOT / "tests" / "cases" / "*.json")))] for c in json.load(open(f))]
    if a.set:                                       # a fixed list of customers, e.g. tests/sets/dev.json
        st = json.load(open(a.set)); by = {c["id"]: c for c in cases}
        cases = [dict(by[i], **st.get("override", {}).get(i, {})) for i in st["ids"]]; a.brief = st.get("brief", a.brief)
        if a.only: cases = [c for c in cases if c["id"] in a.only.split(",")]
    else:
        cases = [c for c in cases if c["brief"] == a.brief and (not a.only or c["id"] in a.only.split(","))]
    run_id = time.strftime("%Y%m%d-%H%M") + f"_{a.builder}_brief-{a.brief.lower()}" + (f"_{a.style}" if a.style else "") + f"_sim-{a.agent_model}"
    outdir = ROOT / "results" / "sim" / run_id; outdir.mkdir(parents=True, exist_ok=True)
    print(f"run id: {run_id}  ({len(cases)} cases, agent {a.agent_model}, customer {a.customer_model}, judge {a.judge_model}, parallel {a.parallel})", flush=True)
    t0 = time.time(); rows = {}
    with cf.ThreadPoolExecutor(a.parallel) as ex:
        futs = {ex.submit(run_case, c, wf, a, outdir): c["id"] for c in cases}
        for f in cf.as_completed(futs):
            r = f.result(); rows[r["id"]] = r
            print(("PASS" if r["pass"] else "FAIL"), r["id"], f"{r['family']:<14}", f"{r['seconds']:>4}s |", " > ".join(r["tools"]) or "(no tool calls)", flush=True)
            for x in r["failed"]: print("       -", x[:260], flush=True)
    rows = [rows[c["id"]] for c in cases]
    n = sum(r["pass"] for r in rows)
    print(f"\n{n} / {len(rows)} passed. {time.time() - t0:.0f} s, {engine.STATS['calls']} model calls, {engine.STATS['seconds'] / max(1, engine.STATS['calls']):.1f} s per call.")
    json.dump({"agent_id": a.workflow, "passed": n, "total": len(rows), "runs": 1, "rows": rows, "all_rows": [rows]}, open(ROOT / "results" / "runs" / f"{run_id}.json", "w"), indent=1)
    log_rows(run_id, a.builder, a.brief, a.style, 1, a.workflow, a.builder_model, [rows], bank=f"sim:{a.agent_model}/{a.customer_model}")
    print("scoreboard:", board())


if __name__ == "__main__":
    main()
