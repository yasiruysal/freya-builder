"""The builder: one command, a brief in, a Freya phone agent out.

    python3 builder/build.py <brief.md> [<more brief files>] <tool_api.json> [--strategy H5] [--slot NAME] [--no-save] [--minutes 30]

It prepares a clean work folder (the brief, the tool API, the platform docs, our skills, and for some strategies a
reference generator and the self-test tools), runs Claude (Opus) headless in it, and takes the workflow it writes.
Then it saves the workflow to Freya under the slot (updated in place), exports it to submission/<slot>.workflow.json,
and writes the trace: traces/<slot>_<time>.log (summary) and .jsonl (every message).

Strategies (which skills the builder gets):
    H1 core | H2 core+library | H3 core+spec-first | H4 core+library+spec-first | H5 core+library+spec-first+redteam
    H6 best of 3: three H3 candidates built in parallel while a fourth session writes test customers from the brief;
       every candidate is tested on them in the local simulator and the best one is kept.
"""
import argparse, json, os, pathlib, re, shutil, subprocess, sys, time

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
MIX = [["core", "spec-first"], ["core", "library"], ["core", "spec-first", "library", "primitives"]]   # H6mix: one strategy per candidate
STRATEGIES = {"H6mix": ["core", "spec-first"], "H6": ["core", "spec-first"], "H1": ["core"], "H2": ["core", "library"], "H3": ["core", "spec-first"],
              "H4": ["core", "library", "spec-first"], "H5": ["core", "library", "spec-first", "redteam"]}


def prepare(work, briefs, api, skills):
    if work.exists(): shutil.rmtree(work)
    (work / "brief").mkdir(parents=True); (work / "tools").mkdir(); (work / "out").mkdir()
    for b in briefs: shutil.copy(b, work / "brief" / pathlib.Path(b).name)
    shutil.copy(api, work / "tools" / pathlib.Path(api).name)
    shutil.copytree(ROOT / "docs", work / "docs")
    shutil.copy(ROOT / "freya.py", work / "freya.py")
    (work / "lib").mkdir(exist_ok=True); shutil.copy(ROOT / "builder" / "lib" / "normalize.py", work / "lib" / "normalize.py")
    shutil.copy(ROOT / "builder" / "contract.py", work / "tools" / "contract.py")
    for s in skills:
        shutil.copytree(ROOT / "builder" / "skills" / s, work / ".claude" / "skills" / s)
    if "library" in skills:
        shutil.copy(ROOT / "builder" / "lib" / "reference_build.py", work / "lib" / "reference_build.py")
    if "redteam" in skills:
        shutil.copy(ROOT / "sim" / "engine.py", work / "tools" / "engine.py"); shutil.copy(ROOT / "sim" / "selftest.py", work / "tools" / "selftest.py")


def prompt(briefs, api, skills, minutes):
    names = ", ".join("brief/" + pathlib.Path(b).name for b in briefs)
    return (f"Build the phone agent for this bank. Brief: {names}. Tool API: tools/{pathlib.Path(api).name}.\n"
            f"Use these skills, in this order: {', '.join(skills)}. Read each SKILL.md first.\n"
            "Write the finished workflow to out/agent.workflow.json (the workflow object itself, with initial_node, nodes, ...).\n"
            "Validate it with the dry run in the core skill. If Freya does not answer (503/504), check the JSON rules yourself and go on.\n"
            "Do not save it to Freya and do not create workflows: the harness does that. Never print environment variables or keys.\n"
            f"You have {minutes} minutes in total. Check the time with `date`. Stop testing and fixing {max(3, minutes // 6)} minutes before the end, "
            "so that out/agent.workflow.json is complete and valid when you stop.\n"
            "Finish with one line: DONE <number of nodes> nodes, <number of tools> tools.")


def summarise(stream_path, log, t0, t1):
    calls, model = [], None
    for line in open(stream_path):
        try: m = json.loads(line)
        except Exception: continue
        if m.get("type") == "system" and m.get("model"): model = m["model"]
        if m.get("type") == "assistant":
            msg = m.get("message", {}); purpose = ""
            for b in msg.get("content", []):
                if b.get("type") == "text" and b.get("text", "").strip(): purpose = b["text"].strip().splitlines()[0][:140]; break
                if b.get("type") == "tool_use": purpose = f"{b['name']}: " + (b["input"].get("description") or b["input"].get("command") or b["input"].get("file_path") or "")[:120].replace("\n", " "); break
            calls.append((msg.get("model") or model, purpose))
        if m.get("type") == "result": log.append(f"builder result: {m.get('num_turns')} turns, {m.get('duration_ms', 0) / 1000:.0f} s, cost ${m.get('total_cost_usd') or 0:.2f}")
    log.append(f"model calls: {len(calls)}")
    log += [f"  {i + 1:>3}. {mdl} | {p}" for i, (mdl, p) in enumerate(calls)]


def redact(*paths):
    """Remove API keys from the traces before they are committed."""
    secrets = [os.environ.get(k) for k in ("FREYA_API_KEY", "FREYA_BANK_TEAM_KEY", "CLOUDFLARE_API_TOKEN", "ANTHROPIC_API_KEY")]
    for p in paths:
        t = pathlib.Path(p).read_text()
        for s in secrets:
            if s and len(s) > 8: t = t.replace(s, "REDACTED")
        pathlib.Path(p).write_text(t)


SIM_PARALLEL = int(os.environ.get("SIM_PARALLEL", "8"))     # all simulator conversations at once, over every candidate
SELECT_TESTS = int(os.environ.get("SELECT_TESTS", "8"))     # customers per candidate when choosing the best one
ENV = {**os.environ, "BASH_DEFAULT_TIMEOUT_MS": "900000", "BASH_MAX_TIMEOUT_MS": "1200000", "CLAUDE_CODE_DISABLE_BACKGROUND_TASKS": "1"}


def run_claude(work, text, model, out):
    cmd = ["claude", "-p", text, "--model", model, "--setting-sources", "project",
           "--allowedTools", "Read", "Write", "Edit", "Glob", "Grep", "Bash", "--permission-mode", "dontAsk", "--max-turns", "200",
           "--output-format", "stream-json", "--verbose"]
    return subprocess.Popen(cmd, cwd=work, env=ENV, stdout=out, stderr=subprocess.STDOUT)


AUDIT_PROMPT = ("You did not build this phone agent; another builder did. Audit it. Read the brief ({briefs}), the tool API (tools/{api}) and "
                "the core skill (.claude/skills/core/SKILL.md), then out/agent.workflow.json (and out/build.py if it exists, the script that "
                "generated it). Go through the brief rule by rule (every numbered rule, every table row, every required sentence, every memo "
                "change) and find where the workflow implements it. Write out/audit.md: one line per rule, OK or the gap. Then fix every gap "
                "and every violation of the core skill's platform facts, in out/build.py (and regenerate) or directly in the JSON, keeping "
                "everything that already works. Run `python3 tools/contract.py out/agent.workflow.json tools/{api}` and fix every ERROR. "
                "You have {minutes} minutes; stop {stop} minutes before the end with a complete, valid out/agent.workflow.json. "
                "Finish with: DONE <number of gaps fixed>.")


TESTS_PROMPT = ("Read the brief ({briefs}) and the tool API (tools/{api}), then the redteam skill (.claude/skills/redteam/SKILL.md) and "
                "the format at the top of tools/selftest.py. Do steps 1 and 2 of the redteam skill only: write out/tests.json with 14 to 18 "
                "customers that cover every rule family of THIS brief at its edges, with expected results computed by the brief (work the "
                "calendar out with Python from `now`). Do not build an agent. You have {minutes} minutes. Finish with: DONE <number> tests.")


def best_of_n(work, briefs, api, skills, a, stream, log, n=3):
    """Build n candidates and a test set in parallel, test every candidate in the simulator, keep the best."""
    t0 = time.time()
    build_min, test_min = max(8, a.minutes // 3), max(6, a.minutes // 4)
    names = ", ".join("brief/" + pathlib.Path(b).name for b in briefs)
    tw = work / "tests"
    if a.stage in ("all", "prepare"):
        if work.exists(): shutil.rmtree(work)
        work.mkdir(parents=True)
        for i in range(n):
            sk = MIX[i % len(MIX)] if a.strategy == "H6mix" else skills
            cw = work / f"cand-{i + 1}"; prepare(cw, briefs, api, sk)
            (cw / "PROMPT.txt").write_text(prompt(briefs, api, sk, build_min)); (cw / "SKILLS.txt").write_text(", ".join(sk))
        prepare(tw, briefs, api, ["core", "redteam"])
        (tw / "PROMPT.txt").write_text(TESTS_PROMPT.format(briefs=names, api=pathlib.Path(api).name, minutes=test_min))
        if a.stage == "prepare":
            log.append(f"prepared {n} candidate folders and a test-writer folder in {work}; each has PROMPT.txt"); return
        procs, outs = [], []
        for d in [work / f"cand-{i + 1}" for i in range(n)] + [tw]:
            f = open(str(stream).replace(".jsonl", f".{d.name}.jsonl"), "w"); outs.append(f)
            procs.append(run_claude(d, (d / "PROMPT.txt").read_text(), a.model, f))
        for p in procs:
            try: p.wait(timeout=max(1, build_min * 60 + 120 - (time.time() - t0)))
            except subprocess.TimeoutExpired: p.kill(); log.append("a session was stopped at its time limit")
        for f in outs: f.close()
        for d in [f"cand-{i + 1}" for i in range(n)] + ["tests"]:
            log.append(f"{d}:"); summarise(str(stream).replace(".jsonl", f".{d}.jsonl"), log, t0, time.time())
    else:
        log.append(f"select stage: candidates and tests were built by separate sessions in {work}")
    tests = tw / "out" / "tests.json"
    cands = [work / f"cand-{i + 1}" / "out" / "agent.workflow.json" for i in range(n)]
    cands = [c for c in cands if c.exists()]
    log.append(f"phase 1 (build + tests) took {(time.time() - t0) / 60:.1f} min; {len(cands)} candidates; tests: {tests.exists()}")
    (work / "out").mkdir(exist_ok=True)
    if not cands: return
    scores = {}
    if tests.exists() and len(cands) > 1:
        runs = []
        per = max(2, SIM_PARALLEL // len(cands))           # the terminal account and the machine slow down above ~8 parallel calls
        for c in cands:
            o = open(c.parent / "selftest.txt", "a")
            runs.append((c, o, subprocess.Popen([sys.executable, str(ROOT / "sim" / "selftest.py"), str(c), str(tests), "--api", str(work / "cand-1" / "tools" / pathlib.Path(api).name),
                                                 "--parallel", str(per)] + (["--resume"] if a.stage == "select" else ["--pick", str(SELECT_TESTS)]),
                                                cwd=c.parent, stdout=o, stderr=subprocess.STDOUT, env=ENV)))
        for c, o, p in runs:
            try: p.wait(timeout=900)
            except subprocess.TimeoutExpired: p.kill()
            o.close()
            txt = (c.parent / "selftest.txt").read_text()
            m = ([None] + list(re.finditer(r"(\d+) / (\d+) passed", txt)))[-1]
            scores[c] = int(m.group(1)) if m else -1
            log.append(f"self-test {c.parent.parent.name}: {m.group(0) if m else 'no result'}")
            log += ["    " + l for l in txt.splitlines() if l.startswith(("PASS", "FAIL"))]
    sys.path.insert(0, str(ROOT / "builder")); import contract as ct
    api_json = json.load(open(api))
    for c in cands:
        errs, _ = ct.check(ct.load(c), api_json)
        if errs:
            log.append(f"contract {c.parent.parent.name}: {len(errs)} errors, candidate ranked last"); scores[c] = scores.get(c, 0) - 1000
    best = max(cands, key=lambda c: scores.get(c, 0))
    log.append(f"kept {best.parent.parent.name}" + (f" ({scores[best]} passed)" if best in scores else ""))
    if getattr(a, "audit", False) and tests.exists() and a.stage == "all":
        best = audit(best, briefs, api, a, stream, log, tests, scores.get(best, 0), t0)
    shutil.copy(best, work / "out" / "agent.workflow.json")
    if tests.exists(): shutil.copy(tests, work / "out" / "tests.json")
    for f in ("spec.json", "coverage.md"):
        if (best.parent / "out" / f).exists(): shutil.copy(best.parent / "out" / f, work / "out" / f)


def audit(best, briefs, api, a, stream, log, tests, score, t0):
    """An independent session audits the kept candidate against the brief and fixes gaps; keep the fix only if it does not lose tests."""
    left = a.minutes - (time.time() - t0) / 60
    if left < 12: log.append(f"audit skipped: {left:.0f} min left"); return best
    cw = best.parent.parent
    shutil.copy(best, cw / "out" / "agent.before-audit.json")
    for s in ("core",):
        if not (cw / ".claude" / "skills" / s).exists(): shutil.copytree(ROOT / "builder" / "skills" / s, cw / ".claude" / "skills" / s)
    names = ", ".join("brief/" + pathlib.Path(b).name for b in briefs)
    mins = int(min(8, left - 8))
    with open(str(stream).replace(".jsonl", ".audit.jsonl"), "w") as f:
        p = run_claude(cw, AUDIT_PROMPT.format(briefs=names, api=pathlib.Path(api).name, minutes=mins, stop=2), a.model, f)
        try: p.wait(timeout=mins * 60 + 60)
        except subprocess.TimeoutExpired: p.kill()
    log.append("auditor:"); summarise(str(stream).replace(".jsonl", ".audit.jsonl"), log, t0, time.time())
    try:
        o = subprocess.run([sys.executable, str(ROOT / "sim" / "selftest.py"), str(best), str(tests), "--api", str(cw / "tools" / pathlib.Path(api).name), "--parallel", str(SIM_PARALLEL), "--pick", str(SELECT_TESTS)],
                           cwd=cw, capture_output=True, text=True, timeout=600, env=ENV).stdout
        m = re.search(r"(\d+) / (\d+) passed", o); after = int(m.group(1)) if m else -1
    except Exception as e:
        after = -1; log.append(f"audit self-test failed: {e}")
    log.append(f"audit self-test: {after} passed (before: {score})")
    if after < score:
        shutil.copy(cw / "out" / "agent.before-audit.json", best); log.append("audit reverted: the audited agent passed fewer tests")
    return best


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("files", nargs="+"); ap.add_argument("--strategy", default="H5"); ap.add_argument("--slot"); ap.add_argument("--no-save", action="store_true")
    ap.add_argument("--minutes", type=int, default=28); ap.add_argument("--model", default="claude-opus-5-5"); ap.add_argument("--audit", action="store_true")
    ap.add_argument("--stage", choices=["all", "prepare", "select"], default="all", help="prepare: only the folders and prompts (the sessions run elsewhere); select: test and pick from existing folders")
    a = ap.parse_args()
    briefs, api = [f for f in a.files if f.endswith(".md")], [f for f in a.files if f.endswith(".json")][0]
    skills = STRATEGIES[a.strategy]
    slot = a.slot or f"ours-{a.strategy.lower()}_{pathlib.Path(briefs[-1]).stem}"
    stamp = time.strftime("%Y%m%d-%H%M%S")
    work = ROOT / "builder" / "work" / slot
    (ROOT / "traces").mkdir(exist_ok=True)
    stream, logf = ROOT / "traces" / f"{slot}_{stamp}.jsonl", ROOT / "traces" / f"{slot}_{stamp}.log"
    t0 = time.time()
    log = [f"builder run {slot}", f"start: {time.strftime('%Y-%m-%d %H:%M:%S %Z')}", f"briefs: {briefs}", f"tool API: {api}",
           f"strategy: {a.strategy} (skills: {', '.join(skills)})", f"builder model: {a.model}"]
    print(f"building {slot} with {a.strategy} in {work} (trace {logf.name})", flush=True)
    if a.strategy in ("H6", "H6mix"):
        best_of_n(work, briefs, api, skills, a, stream, log, n=3)
        if a.stage == "prepare": print("\n".join(log)); return
    else:
        prepare(work, briefs, api, skills)
        with open(stream, "w") as out:
            p = run_claude(work, prompt(briefs, api, skills, a.minutes), a.model, out)
            try: p.wait(timeout=a.minutes * 60 + 120)
            except subprocess.TimeoutExpired: p.kill(); log.append("builder stopped: time limit")
        summarise(stream, log, t0, time.time())
    wf_path = work / "out" / "agent.workflow.json"
    if not wf_path.exists():
        log.append("RESULT: no workflow was written"); print("\n".join(log[-3:])); logf.write_text("\n".join(log) + "\n"); sys.exit(1)
    wf = json.load(open(wf_path)); wf = wf.get("workflow", wf); wf.pop("agent_config", None)
    sys.path.insert(0, str(ROOT / "builder")); import lint, contract
    for c in lint.fix(wf): log.append("lint: " + c)
    cerr, cwarn = contract.check(wf, json.load(open(api)))
    log.append(f"contract check: {len(cerr)} errors, {len(cwarn)} warnings"); log += ["  " + x for x in cerr + cwarn]
    json.dump(wf, open(wf_path, "w"), indent=1)
    (ROOT / "out").mkdir(exist_ok=True); shutil.copy(wf_path, ROOT / "out" / f"{slot}.workflow.json")
    tools = sorted({t["function"]["name"] for n in wf["nodes"].values() for t in (n.get("tools") or []) + (n.get("pre_actions") or [])})
    log.append(f"workflow: {len(wf['nodes'])} nodes, tools used: {', '.join(tools)}")
    for f in ("tests.json", "spec.json", "coverage.md"):
        if (work / "out" / f).exists(): shutil.copy(work / "out" / f, ROOT / "out" / f"{slot}.{f}")
    if not a.no_save:
        try:
            os.chdir(ROOT)
            import freya
            ids = freya.save_workflow(slot, wf)
            (ROOT / "submission").mkdir(exist_ok=True)
            freya.export_workflow(ids["workflow_id"], str(ROOT / "submission" / f"{slot}.workflow.json"))
            log.append(f"created on Freya: workflow_id {ids['workflow_id']}, agent_id {ids['agent_id']}, exported to submission/{slot}.workflow.json")
        except Exception as e:
            log.append(f"Freya save failed: {str(e)[:300]}")
    log.append(f"end: {time.strftime('%Y-%m-%d %H:%M:%S %Z')}  build time: {(time.time() - t0) / 60:.1f} min")
    logf.write_text("\n".join(log) + "\n")
    redact(*[x for x in logf.parent.glob(f"{slot}_{stamp}*")])
    print("\n".join(l for l in log if not l.startswith("  ")), flush=True)


if __name__ == "__main__":
    main()
