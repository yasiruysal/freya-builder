# Trace of the builder run that made `submission/final.workflow.json`

One builder run (slot `final-b`, started 21:28 Istanbul). It was resumed three times after builder bug fixes; each
resume continues the same work folder and writes its own log, so the four logs are one run in order.

- `final.trace.jsonl`: the trace named in submission.json: every Claude Code (Opus) session of the run, in order.
- `final.log`: the builder's console output.
- `final-b_*.agentic.log`: the builder's own step log, one per start/resume (21:28, 21:56, 22:12, 22:18).
- `sessions/`: the same Claude Code sessions as separate files (reconcile, tests, cand-1..3, fix-1..3, test repairs).
