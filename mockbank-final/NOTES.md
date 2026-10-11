# Final mock: Freya Bank statement/debt services

URL: https://freya-final-mock.yasir-freya.workers.dev (Cloudflare Worker `freya-final-mock`, Durable Object `Bank` bound as `BANK`).

Files: `worker.src.js` (source), `build.py` (embeds `cases/FreyaBank-Final/test-environment/test-customers.json` as `const DATA` and writes `worker.js`; nothing is typed by hand), `worker.js` (generated, deployed), `check.py` (checks against the deployed mock).

Rebuild and deploy: `python3 mockbank-final/build.py && source /Users/yasir/Documents/Claude/Freya/env.sh && python3 mockbank/deploy.py --js mockbank-final/worker.js freya-final-mock`, then `python3 mockbank-final/check.py`.

## Endpoints (contract 2026-10-01, sections 1, 2 and 5)

All POST, JSON, envelope `{"data": {...}, "call": {...}}`. The answer is `{"data", "result"}`; an error is `{"result"}` only (section 3 and 4).

| Path | Looked up by | Not found |
|---|---|---|
| `/voice/v1/cards.list` | `data.customerNo` (string or number) | 404 `FB_IVR_CHANNEL_102` WARNING |
| `/voice/v1/cards.balance` | `data.cardNumber` (masked, as cards.list gave it) | 404 `FB_IVR_CHANNEL_120` WARNING |
| `/voice/v1/statement.week.get` | `data.cardNumber` | 404 `FB_IVR_CHANNEL_121` WARNING |
| `/voice/v1/statement.week.decide` | `cardNumber`, then `newStatementWeek` (3 or "3") | 400 `INVALID_ARGUMENTS_ERROR` (unknown card, current week, week outside 1-4) |
| `/voice/v1/statement.week.change` | same as decide | same as decide |
| `/voice/v1/contact.email.get` | `data.customerNo`; `intAddressInfoType` does not change the answer | 400 `INVALID_ARGUMENTS_ERROR` |
| `/voice/v1/statement.pdf.send` | `data.customerNo` | 404 `FB_IVR_CHANNEL_107` "Pdf dokümanı bulunamadı" |
| `/oauth2/token` | form or anything | always 200 `{access_token, token_type: "Bearer", expires_in: 1800}` |

- For test customers the mock returns the listed body and HTTP status exactly, with the listed `guid`s. This includes the listed 500s (97073972 cards.list, 92335586 contact.email.get, 94835408 statement.week.get, 94339861 decide), the 404s (93821776 cards.list, 93635512 pdf.send) and the 12 s `delaySeconds` on 94907600's cards.balance. The delay runs in the Worker, not in the Durable Object, and the field is not put in the body.
- Required fields that are missing or empty give 400 `INVALID_ARGUMENTS_ERROR` (type ERROR) and a message that names the field, e.g. `card number cannot be empty`, `intAddressInfoType cannot be empty`, `newStatementWeek cannot be empty`, `statementDay cannot be empty`, `emailAddress cannot be empty`. The required fields are customerNo (cards.list, contact.email.get, pdf.send); cardNumber (card services, pdf.send); newStatementWeek and statementDay (decide, change); intAddressInfoType; and currencyType, statementDate and emailAddress (pdf.send). Contract section 4 and 5.
- Error bodies are built like the contract examples: `returnCode`=`code`, `messageText`=`message`, a new 32-hex `guid` (also in `details.guid`), and `errorOrigin` `ivr-gateway-test:ivr-channel-v0`. If a handler crashes, the mock answers 500 `FBE00099` "Teknik hata: ABORTED: ".
- Invalid JSON gives a 400. GET on a service gives a 405.

## Auth

The token endpoint issues a token. The services do **not** check the token or the headers (contract section 2: "token and headers are not checked there", section 6, test-customers.md). A request without a token or with a bogus one gets the same answer.

## How customers are found

These are the lookup keys from test-customers.md. Card numbers are unique across customers, so a card call finds its owner without a customerNo. The balance-format number (`5400********1234`) does not match any card and gives a 404, because the contract says to send the number exactly as cards.list returned it. A literal `{{customerNumber}}` or `{{...}}` card number, which the Freya panel's "Test API Request" can send, is answered as customer 90000001 or card 540000******1234 (cloudflare-mock/README).

## State, sessions and ledger keys (our practice routes, not in the contract)

- `POST /v1/practice/sessions {"case": "<customerNo or registered case id>", "now"?}` returns `{ok, variables: {customerNumber, now, customer_id}}`. The default `now` is `2026-10-12T10:30:00+03:00` (example-scenarios.md).
- `GET /v1/practice/ledger/<customer_id>` returns the ledger. `POST /v1/practice/end {"customer_id"}` stops routing calls to that session.
- `POST /v1/practice/register {"cases": [{id, variables: {customerNumber, now}, services?, faults?}]}`. `services` uses the test-customers.json shape and is required when customerNumber is not a built-in test customer. `faults` is `[{"tool": "cards.balance", "fail_first_n": 1, "http"?: 401}]` and gives a 500 `FBE00099` (or a 401) on the first n calls in that session.
- The services carry no call id, only customerNo or cardNumber. A call therefore goes to the **latest practice session opened for that customer in the last 30 minutes**. Otherwise it goes to `live:<customerNo>` (the organisers' runs; the last 300 calls are kept). A new session starts with an empty ledger, so two runs one after the other do not see each other's records. Two parallel self-test runs of the *same* customer would share one ledger (limitation); different customers in parallel are fine.
- **Changes never alter the answers.** The test data has fixed answers per card and week (contract section 6, test-customers.md). After a `statement.week.change`, `statement.week.get` still returns the listed body. The change is only recorded.
- Ledger keys:
  - `calls`: `[{n, tool, args (the data object), status, result}]` for every call, errors included.
  - `statement_week_changes`: `[{cardNumber, newStatementWeek, statementDay}]` for each successful (200) change. `cardNumber` is a string; `newStatementWeek` and `statementDay` are JSON numbers (the recorder converts them with `Number`). `statementDay` is the submitted current day, not the new day returned in the preview.
  - `statement_week_previews`: the same shape, for each successful decide.
  - `statement_pdf_sends`: `[{customerNo, cardNumber, currencyType, statementDate, emailAddress}]` for each successful send; all five fields are strings. The address is recorded as sent and is not checked (contract 5.7), so tests can assert that only the registered address was used.
  - `contact_lookups`: `[{customerNo, intAddressInfoType}]`.
  - Practice keys kept for selftest: `outcome`/`reason`: null, `promises`, `notes`, `callbacks` and `transfers`: [] (the contract has no closing, transfer or promise service; the hand-over is on the platform side), and `identity`: {} (the IVR has already identified the caller). Also `case`, `customerNo`, `now`, `attempts`.

## Assumptions

1. Token not enforced, and `call`/`aiFlow` are ignored (contract sections 2 and 6).
2. Enum values are not validated: `currencyType`, `intAddressInfoType` and the value of `statementDay`. test-customers.md says pdf.send has "one answer per customer" and that `intAddressInfoType` doesn't change the answer. Whatever was sent is recorded, so tests can check it.
3. pdf.send does not check that the card or statementDate belongs to the customer (lookup by customerNo only, test-customers.md).
4. A flat body (fields at the top level, no `data`) is accepted as `data`. This is leniency only, so a slightly wrong tool still works.
5. The yearly limit on week changes is not enforced: the contract has no such flag (open point 3), and Kerem says the rule lives in the card system. The decisions recap (9 Oct) changes only agent behaviour: the card number never read beyond the last 4, and the last-six or full-number steps. The mock follows the test environment (masked numbers).
6. The 404 texts for 120 and 121 are our own wording; the contract says only the code matters.
