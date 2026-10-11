---
name: redteam
description: Write your own tricky test customers from the brief and test the agent on them in the local simulator.
---

# Red-team your agent

There are no practice customers for a new brief. Make your own and try to break the agent.

1. Write `out/tests.json`: 14 to 18 customers in the format at the top of `tools/selftest.py`. Use the brief's call
   variables, identity fields (as the tool API names them), product types and fields (as `get_account` returns them).
   Cover each rule family at its edge: the happy path; a third party with a callback; a wrong number; unclear who
   answers; identity wrong answers up to the limit; a declined identity; a swapped day/month if the brief has a hint;
   a date after the deadline; a weekend or holiday; a vague date; "next <weekday>"; an amount between floor and full;
   below the floor; a flat refusal through all attempts; hardship; dispute; already paid; restructuring; a person
   after a recorded promise; changing a recorded promise; a tool failure (`faults`); and, if the brief has several
   products, mixed results across products. Write the script in the customer's words.
2. Compute each `expected` result by the brief, carefully. Work the calendar out with Python from `now`, never by
   head. A wrong expectation wastes a fix round. Put 2 to 4 checks per customer for the conversation rules.
3. Run: `python3 tools/selftest.py out/agent.workflow.json out/tests.json --api <tool API file>` (about 5 minutes).
   Run it in the foreground and wait.
4. For each FAIL, read `selftest-out/<id>.txt`. Decide: agent bug, or test bug (your expectation or script was wrong).
   Fix agent bugs in the generator / workflow; fix test bugs in `out/tests.json`. Re-run only the failed ones with
   `--only`. At most two fix rounds. Prefer fixes in routes and code over longer prompts.
5. The simulator's agent model is stronger than the real one. A case that passes only because the model was clever
   (a rule that lives only in a prompt) is still a risk: move that rule into a route, counter or fixed text.
