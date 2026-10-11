---
name: library
description: A working reference generator for a different bank's brief, to reuse its helpers and patterns.
---

# Reference generator

`lib/reference_build.py` is a Python script that generated a working phone agent for a **different** bank's brief
(one product per customer). Its agent passed that bank's practice customers. Reuse it as a starting point for your own
generator script, `out/build.py`, that writes `out/agent.workflow.json`:

- Keep the helpers: tool/function builders, condition helpers, code-node builder, closing nodes, the date library,
  the pre/post hook patterns (answer normalisation, `what_to_do_next`, promise validation, failure counting).
- **Every value in it belongs to the other bank**: holidays, bands, deadlines, attempts, floors, product words, callback
  window, identity questions, outcome / reason / queue strings, sentences, URLs. Replace each one with what your brief
  and your tool API say. Search the script for literals and decide each one again. Delete what your brief does not have,
  add what it has and the script lacks (more products, other identity fields, other situations, notes...).
- Its `BLOCK_URL` trick (redirecting a vetoed call to 127.0.0.1) is **forbidden**: a call outside the bank's tool API
  fails the case. Use the veto described in the core skill instead.
- Its weaknesses, seen on new customers: it closes right after the promise (a later request for a person or to change
  the promise is ignored), it leaves "once" rules and the scam answer to the prompt, and its situation enum confused
  "too much / below the floor" with restructuring and "go ahead" with a confirmation. Fix these in yours.
