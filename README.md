# Freya "Build the Builder": agent builder

Run (case folder + the URL of our mock of the bank's services):

    python3 builder-agentic/build.py {case_folder} {mock_url} --runner cli --minutes 60

- `builder-agentic/build.py`: the control loop (case adapter, reconcile, three Opus candidates with different skills,
  a test writer, simulator-based selection, a fix round with a regression veto, lint, contract check, save + export).
- `builder/skills/`: the knowledge the Opus sessions use; `builder/lib/normalize.py`, `builder/lint.py`, `builder/contract.py`.
- `sim/`: the local stand-in for Freya's workflow engine used for the builder's own tests.
- `mockbank-final/`: our mock of the bank's services (Cloudflare Worker).
- `submission/final.workflow.json`: the exported phone agent; `traces/`: the trace of the run that built it.
Requires `env.sh` with FREYA_API_KEY (see env.example) and the `claude` CLI logged in.
