---
name: spec-first
description: Turn the brief into an explicit rule list and coverage table before writing the workflow.
---

# Spec first

Before writing any workflow JSON:

1. Read the brief files in full, then the tool API. If the brief is raw material (mail, policy extract, memo, code
   list), resolve it first: later and more specific documents override earlier ones; the code list gives the exact
   strings. Write down every override you applied.
2. Write `out/spec.json` with these sections, for whatever kind of call this is (inbound or outbound, collections,
   inquiry, service request...). Use the documents' own words and values, nothing invented; leave a section empty when
   the documents have nothing for it, and never add a step (identity check, negotiation...) the documents do not ask for.
   - `flow`: the call's steps in the documents' order, each with its sources; mark steps you had to assume.
   - `inputs`: the call variables (names, formats, what is already known or verified before the call starts).
   - `reads`: every service the agent calls to learn something, when, with which fields, and what it does with the answer.
   - `selection`: how the agent picks the object the call is about (a card, an account, a product, a week...) when there
     are several, and what it says when there is none or the choice is unclear.
   - `actions`: every service that changes something; for each: preconditions (what must be true or checked first, from
     the documents), the confirmation the customer must give, the exact values sent, success and failure handling.
   - `say`: every sentence or piece of information the agent must say, in the documents' wording and language, with when;
     formats (currencies, dates, amounts) as the documents want them.
   - `never`: what must not be said or done (before what).
   - `counts`: every "once", "twice", "N attempts", "at most" rule.
   - `situations`: every special situation → trigger (with counter-examples) → reply → tool calls → exit.
   - `exits`: every way the call ends (hand-over to a person, closing, ...) with the service call or code it needs.
   - `unresolved`: conflicts or gaps from RECONCILED.md (if any) and the safe behaviour chosen.
3. Write `out/coverage.md`: one row per rule of the brief (by number if it has numbers, otherwise by quote), with the
   workflow element that implements it (node / route / code / hook / fixed text). Every rule needs a row.
4. Build the workflow from `out/spec.json`, then walk `out/coverage.md` row by row against the finished JSON and fix
   every rule that is not implemented where the row says.
