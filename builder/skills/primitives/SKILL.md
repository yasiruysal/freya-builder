---
name: primitives
description: Map every rule of any brief to one of eight rule types, each with a standard implementation. Domain-free.
---

# Rule primitives

Whatever the business (bank, insurer, gym, utility), every rule in a brief is one of these eight types. Classify each
rule, then implement it with the matching mechanism. A rule that seems new is still one of these, often two combined.

| Type | Looks like | Implementation |
|---|---|---|
| **SAY** | "say X", "tell them Y", a quoted sentence, a disclosure | Fixed text: `tts`, or a `what_to_do_next` that quotes it, at the exact moment the brief names. Never only a prompt line. |
| **NEVER** | "do not say/mention/offer X (before Y)" | The words or offers never appear in a step where they are forbidden: the step's `task_message` has no access to them (no `{{amount}}` before identity), plus one explicit line. |
| **ORDER** | "first X, then Y", "only after X" | Separate steps; the route to Y needs X's result variable (`equals`), never a model judgement. |
| **COUNT** | "once", "twice", "N attempts", "at most", "the second time" | A counter written by a code node on each counted event; the route reads the counter (see the counted-rule recipes). |
| **COMPUTE** | dates, deadlines, business days, floors, percentages, rounding, bands, thresholds | Python in a code node from `now` and the account data; the model only reads the finished facts. Recompute after every change. |
| **VALIDATE** | "only accept X if...", "must be between", formats | Check in code before the write; the write tool's pre-hook re-checks; a rejection tells the model exactly what to offer instead. |
| **TRIGGER** | "if the customer says/asks X, do Y" (situations) | An extracted enum value with the brief's definition and counter-examples → a route → its step (reply, tool, outcome). Reachable from every step where it can happen. |
| **CLOSE** | outcome tables, codes, "close as", priorities between outcomes | Code decides the outcome and reason from the call's state (what was recorded, noted, transferred, failed) using the brief's table and priority rules; one close step per outcome/reason, values baked from the tool API's allowed list. |

Steps:
1. List every rule of the brief (numbered rules, table rows, quoted sentences, memo changes, code-list entries) with its type(s).
2. For each, write the workflow element that implements it (node, route, code variable, hook, fixed text).
3. After building, walk the list against the JSON. A rule with no element is a bug.
4. Prefer the mechanism in the table over a prompt instruction, always: the agent runs on a weak model.
