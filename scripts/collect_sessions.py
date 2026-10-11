"""Copy the Claude Code (Opus) session logs of one builder run into traces/<slot>-sessions/, with keys removed.

    python3 scripts/collect_sessions.py <slot>

Each LLM step of the builder (reconcile, candidates, test writer, fix rounds) is a `claude -p` session whose stream is
saved as <work folder>/<step>/session.jsonl. These are the builder's session logs the organisers ask for.
"""
import os, pathlib, sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
slot = sys.argv[1]
work = ROOT / "builder-agentic" / "work" / slot
out = ROOT / "traces" / f"{slot}-sessions"; out.mkdir(parents=True, exist_ok=True)
secrets = [os.environ.get(k) for k in ("FREYA_API_KEY", "FREYA_BANK_TEAM_KEY", "CLOUDFLARE_API_TOKEN", "ANTHROPIC_API_KEY")]
n = 0
for f in sorted(work.glob("*/session.jsonl")):
    t = f.read_text(errors="ignore")
    for s in secrets:
        if s and len(s) > 8: t = t.replace(s, "REDACTED")
    (out / f"{f.parent.name}.session.jsonl").write_text(t); n += 1
print(f"{n} session logs -> {out}")
