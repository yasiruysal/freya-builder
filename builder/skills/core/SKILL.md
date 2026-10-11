---
name: core
description: How to build a robust Freya phone agent from a bank's brief. Read first, always.
---

# Build a Freya phone agent from a brief

You are the builder. You get a bank's **brief** (one or more Markdown files) and its **tool API** (`openapi.json`).
You produce one Freya workflow JSON that is the phone agent. The agent runs on a **weak model** (`experimental-5`).
It forgets rules buried in long prompts, goes silent after bare tool results, miscounts, and misreads intents.
So the design rule is: **the model only talks and recognises; code and routes decide.**

Everything about the bank comes from the brief and the tool API. Never reuse a rule, value, name, date, phrase or
outcome from another brief or from an example. If the brief is raw material (mails, policy extracts, memos, code
lists), the later or more specific document overrides the earlier one (a memo that changes a policy wins), and the
code list fixes the exact outcome / reason / queue strings.

Read `docs/workflow-format.md` and `docs/tools-and-code.md` once. The facts below are the ones that cost points.

## Platform facts (verified)

- Each user turn runs three separate model calls: extraction (node's `extraction_schema`), routing (only transition
  descriptions / intents), dialogue (`system_prompt` + `task_message` + history + node `tools`). Routing never reads
  `task_message`.
- `tools` only on `conversation` nodes; `pre_actions` only on `api_request` / `code` nodes. Anything else is silently dropped.
- Tool parameter values: a `default` (literal, or `{{var}}` that is set when the node is entered) is baked and hidden
  from the model. A `{{var}}` default whose variable is unset is filled by the model. Never use `const`.
- **Every `api_call` body field needs a `default`** (verified on Freya: a field without one is never sent; the agent
  looped forever because `field` and `answer` never reached the bank). A model-filled field gets
  `"default": "{{<unique name>}}"` whose variable nothing else sets (e.g. `{{vi_answer}}`).
- Each `api_call` body field is a leaf under `body.properties.<field>` with its own `default`, `description`, `enum`.
- Results: the envelope is `{status, data, message, status_code, header}`; the API body is `data`. Map with `data.` paths.
  Flattened keys appear by themselves. For a tool in a conversation node's `tools`: `<tool>_response.data.<field>`,
  `<tool>_success_count`, `<tool>_failure_count`. **For a call in `pre_actions` (api_request / code node) every automatic
  key is named after the NODE, not the tool**: a node `record` whose pre-action is `create_promise` gives
  `record_response.data.ok`, `record_success_count`, `record_failure_count` (verified on Freya). Routes after such a node
  must test the node-named keys; a route on `create_promise_response...` never fires there. Safest: in the tool's
  `post_process_code`, write your own string flag (`vars['promise_state'] = 'ok'` / `'failed'`) and route on it.
- **Lists in results are never mapped to a variable** (verified on Freya: `response_mapping` of `data.products` leaves
  `products` unset). Lists only exist as flattened keys, by index: `<prefix>_response.data.products.0.past_due`,
  `...products.1.type`. The prefix is the **node name** for a `pre_actions` call (`load_account_response....`) and the
  tool name for a tool in `tools`. In code, read products by scanning keys:
  `for k, v in vars.items(): m = re.search(r'_response\.data\.products\.(\d+)\.(\w+)$', str(k))` (take the prefix of the most recent account node) and group by index. Never rely on a
  mapped list, and never hard-code one prefix.
- **Extraction field types:** only `string`, `integer`, `float`, `boolean`, `date`, `datetime`, `enum`, `name`, `phone_number`, `object`. Anything else (`number`, `int`, `text`) is rejected when the workflow is saved. Amounts are `float`.
- **A variable declared in any `extraction_schema` can only be written by code with a value that fits that schema**
  (verified on Freya: writing `''` or a value outside `enum_values` is rejected with "rejected by schema validation"
  and the old value stays). Give code-set flags and counters their own names, never the name of an extracted variable.
- **A transition to the node itself is a re-entry** (verified on Freya). `on_enter` runs again (with `on_enter_first_entry_only` the model writes a reply from the `task_message` instead), `<node>.current_turns` resets to 0, and `max_node_turns` restarts. An `always` self-transition plus a `current_turns` or `max_node_turns` limit loops forever: one agent asked the same question 80 times. Never write an `always` self-transition. When no transition matches, the agent stays in the node without re-entry, which is what you want.
- `<node>.current_turns` (user turns since the last entry, already 1 when the first user message is routed) and `<node>.total_turns` (whole call, never reset) exist for every node. For "ask at most N times" across re-entries, use `total_turns` or a code counter. `current_turns` only works in a node that is never re-entered.
- **Resetting an extracted variable:** code can only write a value that fits the schema. Give every extracted enum a neutral value (`none`), describe it as "the value for the customer's LAST message; `none` if nothing applies", and reset with that value. Never write `''`, never write a value outside `enum_values`, never use `is_set` on an extracted enum. Use one enum per node (`who_greeting`, `who_retry`) so that a value from another node is never read.
- Tool parameters are baked when the node is **entered**. A variable that is `""` at entry counts as a value (it is baked empty and hidden from the model). Give model-filled parameters unique names (`cp_amount`, not `amount`) that no code ever writes.
- The `tts` of a passthrough `api_request` node is logged **after** its `pre_actions`. If the brief says "goodbye before close_call", say the farewell in a separate passthrough `code` node (`tts` farewell, a one-line snippet) placed before the `close_*` node, and make the `close_*` node `silent`.
- Code: import only `json`, `re` and `datetime`. Write only strings and numbers to `vars`. Store lists and dicts as `json.dumps(...)` strings, never as Python lists, dicts, `None` or booleans.
- The scoring judge reads the system events (every `function_call_start`, including vetoed or redirected calls, with timestamps). A tool call that should not count must not be made: prefer validating in code before an engine-run call over vetoing inside the call.
- Booleans arrive as strings: test `"true"` and `"True"`. Prefer string flags written by code (`ok`, `failed`, ...).
- **A failed call (any non-2xx) skips `post_process_code`.** The pre-hook has already run. The model sees only `API request failed: {...}` and no `what_to_do_next`. Freya writes `<t>_response.status = "error"`, `<t>_response.error = "HTTP 500"`, `<t>_response.message`, `<t>_response.status_code`, `<t>_status_code` and increments `<t>_failure_count`. Earlier `<t>_response.data.*` keys are not cleared. So never count failures or write "failed" flags in a post-hook. Route on `<t>_failure_count` (`greater_than 1` → the service-error close) or on `<t>_response.status equals error`, and derive per-item failures in code from what the pre-hook recorded (attempted, and not confirmed by a success post-hook). Put the retry rule in the `task_message` of every node with a write tool: "If a tool result is an error, call the same tool once more with exactly the same values. Never say it worked."
- Routing runs again right after a tool result, in the same turn: a transition on a result variable fires at once.
- Transitions are checked in order, first match wins; variable conditions need no model call. Top-level condition
  groups use `"logic": "or"`; AND is a nested group. Every passthrough node ends with an `always` transition.
- Code (`code` node, `pre_process_code`, `post_process_code`): Python with `vars` (and `config` / `result` in hooks).
  `json`, `re`, `datetime` work. Never write `{{` or `}}` in code. Wrap everything in `try/except`, write a fallback
  for every variable a prompt or route reads. A code node that raises leaves the placeholders unresolved.
- `post_process_code` may add `result['data']['what_to_do_next'] = '...'`: the model reads it and acts on it. This is
  the fix for "the agent says nothing after a tool call" and "the agent ignores a negative result". Do it on every tool.
  Also `result.pop('header', None)`.
- `on_enter: {"type": "tts", "text": ...}` says a fixed line, with `{{var}}`. A passthrough `api_request` node can say a
  fixed line and run its calls. `end_call` farewell comes from `terminal` (`message_mode` custom/none), not from `on_enter`.
- `max_node_turns` needs a `max_turn_transition`, otherwise it does nothing.
- `context_variables` is dropped on save: put state into prompts with `{{var}}`.
- Leave `agent_config` out. No `null` values anywhere in the JSON.
- The agent may call **only the bank's tool API host**. Localhost and any other URL fail the case. To veto a write
  in a pre-hook (date not allowed, amount below the floor, already recorded, already closed...), turn it into a
  harmless read on the same host: `config['url'] = <server>/get_account`, `config['body'] = {'customer_id': ...}`, and
  store the reason in `vars['<tool>_veto']`. The call then returns 200 and records nothing; the post-hook sees the
  veto variable, sets `data['ok'] = False` and a `what_to_do_next` that says what to offer instead, and clears the
  variable. Never redirect to another URL, never send a value the bank would store. Use this veto only after identity
  is verified (`get_account` before that is logged as a policy breach). Before identity (third party, callbacks), do not
  let the model call a write it might get wrong: validate in code first and let an engine `api_request` node make the
  call with baked values, or ask again.

## Lessons from failures (each one cost a case)

1. **Required sentences** (answers to "is this a scam?" / "are you a robot?", SMS confirmation, "the rest stays overdue", "can take one business day", "use the app or a
   branch", sympathy line, "I am sorry for your loss.") go into fixed text: `tts` lines or a `what_to_do_next` that
   quotes the exact sentence. Not only into a long prompt. Questions that can come at any moment (scam, robot, a
   question you cannot answer) need an answer in every conversation step: put the exact sentence into each node's `task_message` and into the
   `what_to_do_next` lines. A global node with `__previous__` has never been verified on real Freya calls (it fires only
   when no transition of the current node matched, and `__previous__` re-enters the node): do not rely on it.
2. **Actions that must happen** (callback, transfer, note, close) are tool calls or engine `pre_actions`, reached by a
   route. Saying "I will transfer you" without the call fails.
3. **Outcome and reason of `close_call`** are never a `{{variable}}` a model extracts (`tools/contract.py` rejects it), and each close node sends or omits the reason exactly as the tool API's description requires for its outcome. They are set by the route that reaches the closing node: one closing node per
   outcome/reason pair, with baked `outcome`/`reason`. Never let a model read the conversation to choose them.
4. **Dates and amounts are computed in code** from `now` and `get_account`: business days, holidays, deadline per
   band, "this/next <weekday>", the persuasion dates, floors. Give the model a ready calendar table and validate the
   date and amount again in the `create_promise` pre-hook (veto + `what_to_do_next` telling what to offer instead).
5. **Tool failure**: retry exactly as the brief says; count failures in code; "recorded" may only be said after
   `ok: true` (the success `what_to_do_next` or a success-route `tts` says it, nothing earlier). Tell the model in
   the read-back step: do not say "recorded" or "noted" before the tool result.
6. **The weak model passes the customer's words unchanged** (verified on Freya: it sent `"June third nineteen eighty
   seven"` as the date of birth, the bank refused it, and the agent asked 14 times). Every tool field that needs a
   format (dates, digits, amounts) gets a `pre_process_code` that converts it, using the tested functions in
   `lib/normalize.py`: paste its `SNIPPET` string at the top of the hook, then
   `b['answer'] = to_iso_date(b['answer']) or b['answer']` (dates), `to_digits(...)` (codes, phone digits),
   `to_number(...)` (amounts), `to_time(...)` (clock times, 'HH:MM'). The functions read English and Turkish ("on dört Mart bin dokuz yüz seksen sekiz", "dört bin iki yüz elli lira", "1.250,00", "haftaya cuma"). Do the same in code nodes that read extracted dates or amounts.
7. **Never trust a `date`-typed extraction or the model for a day the customer names** (verified on Freya: the
   extractor resolved "tomorrow" against the real clock, 2026-10-11, instead of the call's `now`, 2026-10-14, and a
   valid callback was refused; the dialogue model called Saturday 17 October "this Friday" and looped). Extract what
   the customer said about the day as a **string** (`cb_day_words`, `pay_day_words`), then resolve it in a code node
   with `resolve_day(words, vars['now'])` from `lib/normalize.py`, and give the model the resolved date with its
   weekday as text. Check the brief's own meaning of "this/next <weekday>" against the function's docstring.
8. **Identity**: one question at a time, in the brief's order, using the tool's own counters (`wrong_answers_left`,
   `all_verified`, `hint`) for routes. Normalise answers in a pre-hook (dates to `YYYY-MM-DD`, day first when only
   numbers; digits only for number answers). A hint retry is not a wrong answer.
9. **Every special situation of the brief** (third party, deceased, wrong number, unclear, declined, dispute, already
   paid, hardship, restructuring, human, scam, robot, questions you cannot answer...) becomes: trigger → exact reply →
   tool → outcome, with its own route.
10. **"Once" / "twice" / "N attempts" rules are counters in code** (or node turn counts), and routes read them.
   The model must never be the one counting.
11. **The call is not over when the promise is recorded.** After the last required sentence, the customer may still ask
   for a person, to change the promise, or something else. Keep a short post-promise conversation step whose routes
   reach the right closing node (e.g. `transferred` after a recorded promise if the brief says so).
12. **Intents are read from a fixed list with the brief's own definitions and counter-examples**, extracted as an enum
    (`extraction_schema` with `enum_values`, `min_confidence` ~0.6) and routed with `equals`. Write the counter-examples
    that the brief implies: e.g. an amount or date proposed by the customer is never a refusal; "too much" is not a
    restructuring request; "go ahead" / "who is this" is not a confirmation of identity and not a refusal to answer.

13. **The weak model misclassifies, with confidence 1.0.** On Freya, "That is too much. I can pay three hundred this Friday" was extracted as `restructuring` and the call was transferred. Extract the facts separately from the situation: `named_amount` (type `float`), `named_date` (type `date`) and `too_much` (`yes`/`none`). Route those before the situation enum, because the brief makes a proposal "never a refusal". Send irreversible exits (transfer, close) only on an unambiguous value. Where the brief allows it, confirm once ("Shall I transfer you to …?").
14. **One weak-model node must not orchestrate N writes.** For several items (products, accounts), let the model collect and confirm, write the agreements into variables, then let a code + `api_request` loop record each item with baked values.

15. **One customer turn can carry two things** (verified on Freya: "I can pay on 6 November. Am I talking to a
    robot?" was handled as a date proposal and the robot question was never answered; the judge failed the call).
    Extract side questions (robot/AI, scam, who is calling, a question the brief answers) in their **own** variable,
    separate from the main act, in every conversation step, with a neutral value. When it is set, the step's reply
    starts with the brief's answer (a fixed line or a `what_to_do_next`), then continues with the main act. Reset it
    with its neutral value afterwards.
16. **A proposal the agent already rejected, repeated, is a refusal** (verified on Freya: "three hundred or nothing"
    was extracted as a new proposal each time and the agent offered the floor 80 times; the call never closed).
    In code, remember the values the agent rejected; the same value again counts as the brief's refusal (it uses an
    attempt), never as a new proposal. Every conversation step also needs a hard cap (`max_node_turns` with a
    `max_turn_transition` to the right close) so that no loop can run until the simulator hangs up.
17. **What the customer says about a single item before the per-item loop counts** (verified on Freya: "I never had
    that credit card" said while all products were listed; the loop then offered a payment on that card). The step
    that states all items extracts per-item situations (dispute, paid, hardship...) with the item they concern; code
    stores them as that item's decision before the loop, and the loop skips items that already have one.
18. **Avoid words the brief forbids in one context everywhere else too.** The judge reads literally: an opening line
    "this call is recorded" was read as "said 'recorded' before the promise was recorded". Phrase required content
    so that it does not contain a word the brief restricts elsewhere (e.g. "we are recording this call").

19. **The model never closes the call or makes the final writes** (verified on Freya: in a 'finish' step where the
    model had to call create_promise and close_call, it said nothing and called nothing; 8 of 16 calls hung until the
    customer hung up). After the customer's confirmation, route to `api_request` nodes that record with baked values and
    to close nodes that call `close_call` in `pre_actions`. `tools/contract.py` reports a model-called `close_call` as an
    ERROR and other model-called writes as warnings.

20. **A mandated date format applies to every spoken date.** If the brief requires day, month and year, compute the
    complete spoken date in code and use fixed text at every reading, preview, confirmation and closing step. Keep
    the year in due dates too; a model may shorten a date even when a nearby date was complete. Apply relative words
    such as "today" only where the brief explicitly requires or permits them.
21. **An early answer does not skip a mandatory step.** Store a customer's volunteered choice, but still say the
    required availability or options sentence, ask any required consent question, read the preview, and obtain the
    confirmation required for that preview before executing the action. Track facts received separately from required
    steps completed. Build routes around the brief's order; having a value in a variable is not evidence that the
    corresponding disclosure, options or confirmation occurred.

## The call's flow comes from the brief

First write down the steps of the call **as this brief describes them**, in its order: who speaks first and what is
said, how the right person is found, what is checked and how, what is disclosed, what is negotiated, what is recorded,
how the call ends, and which situations can interrupt which steps. Build the graph from that list. A brief that orders
things differently (for example a disclosure before a check, an extra consent step, a survey at the end, a step per
account) gets that order. Only where the brief says nothing, fall back to the default shape below.

## Default shape (only where the brief is silent)

```
setup (code: date facts, counters, flags)          ── passthrough
greeting (conversation: who is on the line; enum)  ── routes: confirmed / third party / wrong number / deceased / unclear
identity (conversation: one question at a time, verify tool + hooks) ── routes on all_verified / no attempts / declined counter
load_account (api_request get_account) → compute (code: products, bands, deadlines, calendar, floors, facts text)
negotiate (conversation: states the debt, asks, persuades by the brief's attempts; create_promise tool with pre/post hooks;
           situation enum → routes)
after_promise (conversation: required sentence, then listen; routes to close / transfer / app answer)
close_<outcome>_<reason> (api_request: optional extra tool such as transfer/note, then close_call baked) → hang_up (end_call)
```

Use more steps where the brief has a strict sub-procedure (e.g. per-product loops for several products, a callback step
for third parties). Keep each `task_message` short: the job of this step, its tools by name, what not to say yet.

## Recipes for counted rules (the most common remaining failure)

Agents built so far still ask a third time, explain twice, or re-offer a refused amount 4-5 times, because the count
lived in a prompt. Build counts into the graph:

- **Counter loop.** In a conversation node, extract an enum for what the customer just did (e.g. `answered`,
  `refused`, `asked_purpose`, `unclear`). Route the counted kinds to a passthrough `code` node that increments a
  counter (`vars['declines'] = int(vars.get('declines') or 0) + 1`) and writes the next instruction as text
  (`vars['decline_step'] = 'Explain once that ...'`). From the code node: counter at the limit → the closing node;
  otherwise back to the conversation node, which has `on_enter_first_entry_only: true` and a `task_message` that
  shows `{{decline_step}}`. The model never decides whether this is the first or the second time.
- **One node per offer.** Persuasion is a ladder of states, not a prompt: `offer_today` → `offer_attempt_1` →
  `offer_attempt_2` (only where the band allows it) → close as refused. A code node computes each offer (amount and
  date as text and as values) before the ladder starts. Each offer node says its one offer (`tts` or a one-line task)
  and has three exits: accepted → read-back; customer proposes a date/amount → the validation step; refused → the
  next rung. An offer equal to one already refused (same amount and date) is skipped by the code. When the ladder
  ends, the route goes to the refusal close: no further offers are possible because no node makes them.
- **Re-asks.** A question that may be asked "once more" gets its own node (`ask_again`) reached by a route; the node
  after it closes on the next unclear answer.
- The same goes for callbacks outside the window (one re-ask node), identity re-tries (the tool's counters), and
  "explain once" rules.

## Recipe for several products (accounts, facilities, cards...)

A single conversation node that negotiates, reads back and records N products fails on the weak model (promises not
recorded, the same note on every product, attempts skipped). Make the graph do the bookkeeping:

1. `compute` (code): read every product from the flattened keys, write per-product facts by index (`p1_words`,
   `p1_due`, `p1_floor`, `p1_deadline`, `p1_offer1_date`, ..., `p1_status = 'open'`) and `n_products`. Write one text block
   that states every product (said once, before any ask).
2. `ask_all_today` (conversation): states all products, asks whether all can be paid today. Yes → code marks every
   product `promise` today → read-back. Otherwise → the loop.
3. `next_product` (code): picks the first product whose status is `open`, copies its facts into `cur_*` variables
   (`cur_id`, `cur_words`, `cur_due`, `cur_floor`, `cur_deadline`, offer dates and the attempt budget of its band), resets
   the per-product counters, and routes to `negotiate_product`; when none is open → `readback_all`.
4. `negotiate_product` (conversation, one product only, the offer ladder of the counted-rule recipe): its exits are
   decisions, extracted as an enum with its own neutral value (`pending`): `promise` (with `cur_amount`, `cur_date`
   validated by code), `refused`, `dispute`, `paid`, `hardship`, and the call-level exits (restructuring, human...).
   Each decision goes to `store_decision` (code): writes `p<i>_status`, `p<i>_amount`, `p<i>_date`, `p<i>_reason`, resets
   the decision enum to `pending`, routes back to `next_product`.
5. `readback_all` (conversation): one sentence built by code with every agreed promise (and any date moved to its own
   deadline), one "Do you confirm?". A change → back to the product concerned.
6. `record_next` (code) → `record_one` (api_request, `create_promise` with baked `{{rec_id}}`, `{{rec_amount}}`,
   `{{rec_date}}`) → `record_ok` (code: mark recorded) → `record_next`; on `record_one_failure_count` (node-named) retry
   once, then mark that product `service_error`. Then the same loop for `add_note` over every product without a
   promise, with its stored reason.
7. `close_decide` (code): applies the brief's closing rule (promise wins, all paid, first non-paid reason, transfers...)
   and routes to the matching close node.
Single-product briefs use the same loop with n = 1; it costs nothing and keeps one design.

## Workflow file rules

- `test_variables`: plausible values for every call variable of the brief (so a dashboard chat starts), never real data.
- Tool URLs: the server URL from the tool API file + the path. Body fields and enums exactly as in the tool API.
- `customer_id` (and any other call variable the tool needs) baked with `{{var}}`.
- No two transitions of one node may have the same `target_node`. The docs say they are merged and an unconditional copy makes the merged one unconditional. Combine the conditions into one OR group.
- Every endpoint of the tool API may return 500. For every engine-run call (`api_request` node), add a route on `<node>_failure_count greater_than 0` (after `maxRetries: 2` for reads) to the brief's service-error close. Never let a failed read fall through to a step that assumes data.
- Adapt to the tool API, not to this bank: GET or path parameters go into `url` as `{{var}}`; auth headers go into `headers` as baked `default`s; a top-level list response flattens as `<prefix>_response.data.0.<field>`.
- Run the simulator with Haiku and 3 runs per case, and treat a case as passed only when all 3 pass. Sonnet hides most weak-model failures.
- **Before you finish, run the contract check** and fix every ERROR:
  `python3 tools/contract.py out/agent.workflow.json tools/<tool api file>`. It compares every call with the tool API:
  URLs, body fields, required fields, fixed values against the allowed values, and allowed values no call can send.
- Validate with a dry run before saving: `python3 -c "import freya,json,sys; print(freya.validate_workflow('check', json.load(open(sys.argv[1]))))" <file>`.
