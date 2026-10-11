// Mock of Freya Bank's voice statement/debt services (final case), on Cloudflare Workers.
// Source template: mockbank-final/build.py replaces __CUSTOMERS__ with test-environment/test-customers.json
// and writes mockbank-final/worker.js. Do not edit worker.js by hand.
//
// Contract: cases/FreyaBank-Final/sources/documents/2026-10-01-service-contract.md
//   POST /oauth2/token                        -> {access_token, token_type: "Bearer", expires_in: 1800}
//   POST /voice/v1/<service>                  -> the seven services (cards.list ... statement.pdf.send)
// Our own test routes (extra, not in the contract):
//   POST /v1/practice/sessions {"case": "<customerNo or registered case id>", "now"?}
//        -> {ok, variables: {customerNumber, now, customer_id}}
//   GET  /v1/practice/ledger/<customer_id>    -> everything that session recorded
//   POST /v1/practice/end {"customer_id"}      -> stop routing the customer's calls to that session
//   POST /v1/practice/register {"cases": [...]} -> add test customers at run time
//   GET  /v1/practice/cases, GET /health

const DATA = __CUSTOMERS__;

const SERVICES = ["cards.list", "cards.balance", "statement.week.get", "statement.week.decide",
                  "statement.week.change", "contact.email.get", "statement.pdf.send"];
const ORIGIN = "ivr-gateway-test:ivr-channel-v0";
const NOT_FOUND_EXC = "com.freyabank.ivrgateway.ivrchannel.rest.exception.NotFoundException";
const DEFAULT_NOW = "2026-10-12T10:30:00+03:00";
const SESSION_TTL_MS = 30 * 60 * 1000;   // a practice session catches calls for its customer for 30 minutes
const LIVE_CALLS_CAP = 300;

// Built-in customers: customerNo -> services; masked card number -> customerNo.
const CUSTOMERS = {};
const CARD_OWNER = {};
for (const c of DATA.customers) {
  CUSTOMERS[c.customerNo] = c;
  for (const sv of ["cards.balance", "statement.week.get", "statement.week.decide", "statement.week.change"]) {
    for (const card of Object.keys(c.services[sv] || {})) CARD_OWNER[card] = c.customerNo;
  }
}

const CORS = { "Access-Control-Allow-Origin": "*" };
const json = (obj, status = 200, extra = {}) =>
  new Response(JSON.stringify(obj), { status, headers: { "content-type": "application/json; charset=utf-8", ...CORS, ...extra } });

const guid = () => crypto.randomUUID().replace(/-/g, "");

function errorBody(code, message, type, rootCause) {
  const g = guid();
  return { result: { guid: g, returnCode: code, messageText: message, code, message, type,
                     details: { guid: g, rootCause, errorOrigin: ORIGIN } } };
}
const ERR = {
  invalid: (msg) => ({ http: 400, response: errorBody("INVALID_ARGUMENTS_ERROR", msg, "ERROR", "InvalidArgumentsException") }),
  technical: () => ({ http: 500, response: errorBody("FBE00099", "Teknik hata: ABORTED: ", "ERROR", "TechnicalError") }),
  noCard: () => ({ http: 404, response: errorBody("FB_IVR_CHANNEL_102", "Müşteriye ait geçerli kart bilgisi bulunamadı.", "WARNING", NOT_FOUND_EXC) }),
  balanceNotFound: () => ({ http: 404, response: errorBody("FB_IVR_CHANNEL_120", "Kart bakiye servisi hatası: kart bulunamadı", "WARNING", NOT_FOUND_EXC) }),
  weekNotFound: () => ({ http: 404, response: errorBody("FB_IVR_CHANNEL_121", "Hesap kesim haftası servisi hatası: kart bulunamadı", "WARNING", NOT_FOUND_EXC) }),
  noPdf: () => ({ http: 404, response: errorBody("FB_IVR_CHANNEL_107", "Pdf dokümanı bulunamadı", "WARNING", NOT_FOUND_EXC) }),
};

// The Freya panel's "Test API Request" sends variables unfilled, e.g. "{{customerNumber}}".
const isTemplate = (v) => typeof v === "string" && (v.trim().startsWith("{{") || v.startsWith("%7B%7B"));
const isEmpty = (v) => v === undefined || v === null || (typeof v === "string" && v.trim() === "");
const str = (v) => (v === undefined || v === null ? "" : String(v).trim());

export default {
  async fetch(request, env) {
    try {
      const stub = env.BANK.get(env.BANK.idFromName("bank"));   // one instance: every read sees every write
      const res = await stub.fetch(request);
      const delay = Number(res.headers.get("x-mock-delay") || 0);
      if (delay > 0) {                                           // the slow answer in the test data: wait outside the DO
        await new Promise((r) => setTimeout(r, delay * 1000));
        const h = new Headers(res.headers); h.delete("x-mock-delay");
        return new Response(res.body, { status: res.status, headers: h });
      }
      return res;
    } catch (e) {
      return json(ERR.technical().response, 500);
    }
  },
};

export class Bank {
  constructor(ctx) { this.storage = ctx.storage; }

  async fetch(request) {
    try {
      return await this.route(request);
    } catch (e) {
      return json(ERR.technical().response, 500);
    }
  }

  async route(request) {
    const url = new URL(request.url);
    const path = url.pathname.replace(/\/+$/, "") || "/";
    const method = request.method;
    if (method === "OPTIONS") {
      return new Response(null, { headers: { ...CORS, "Access-Control-Allow-Methods": "GET, POST, OPTIONS", "Access-Control-Allow-Headers": "*" } });
    }
    if (path === "/health" || path === "/") return json({ ok: true, services: SERVICES.map((s) => "POST /voice/v1/" + s), token: "POST /oauth2/token" });

    // ---- access token (contract section 2) ----
    if (path.endsWith("/oauth2/token")) {
      if (method !== "POST") return json({ error: "invalid_request", error_description: "POST required" }, 405);
      return json({ access_token: "mock-" + guid(), token_type: "Bearer", expires_in: 1800 });
    }

    // ---- our practice routes ----
    if (path === "/v1/practice/sessions" && method === "POST") return this.session(await this.readJson(request) || {});
    if (path.startsWith("/v1/practice/ledger/")) {
      const id = decodeURIComponent(path.split("/").pop());
      const led = await this.storage.get("led:" + id);
      return led ? json({ ok: true, customer_id: id, ...led }) : json({ ok: false, error: "unknown customer_id" }, 404);
    }
    if (path === "/v1/practice/cases") {
      const reg = await this.storage.list({ prefix: "case:" });
      return json({ ok: true, cases: [...Object.keys(CUSTOMERS), ...[...reg.keys()].map((k) => k.slice(5))] });
    }
    if (path === "/v1/practice/end" && method === "POST") {         // stop routing calls to a session (it keeps its ledger)
      const b = await this.readJson(request) || {};
      const led = await this.storage.get("led:" + b.customer_id);
      if (!led) return json({ ok: false, error: "unknown customer_id" }, 404);
      const act = await this.storage.get("active:" + led.customerNo);
      if (act && act.id === b.customer_id) await this.storage.delete("active:" + led.customerNo);
      return json({ ok: true });
    }
    if (path === "/v1/practice/register" && method === "POST") return this.register(await this.readJson(request) || {});

    // ---- the seven services (contract section 5), matched on the last path segment ----
    const service = path.split("/").pop();
    if (SERVICES.includes(service)) {
      if (method !== "POST") return json(errorBody("INVALID_ARGUMENTS_ERROR", "method not allowed, use POST", "ERROR", "MethodNotAllowed"), 405);
      return this.service(service, request);
    }
    return json({ ok: false, error: "not found", routes: ["POST /oauth2/token", ...SERVICES.map((s) => "POST /voice/v1/" + s)] }, 404);
  }

  async readJson(request) {
    const text = await request.text();
    if (!text.trim()) return {};
    try { return JSON.parse(text); } catch (e) { return null; }
  }

  // ---------- practice: sessions, register ----------

  async getCase(id) {
    id = str(id);
    const reg = await this.storage.get("case:" + id);
    if (reg) return reg;
    if (CUSTOMERS[id]) return { id, variables: { customerNumber: id, now: DEFAULT_NOW } };
    return null;
  }

  async session(body) {
    const c = await this.getCase(body.case ?? body.customerNumber ?? body.customerNo);
    if (!c) return json({ ok: false, error: "unknown test customer / case id" }, 404);
    const customerNo = str(c.variables.customerNumber ?? c.variables.customerNo);
    const now = body.now || c.variables.now || DEFAULT_NOW;
    const id = "s_" + crypto.randomUUID().slice(0, 13);
    const led = newLedger({ case: c.id, customerNo, now });
    await this.storage.put("led:" + id, led);
    await this.storage.put("active:" + customerNo, { id, at: Date.now(), caseId: c.id });
    return json({ ok: true, variables: { ...c.variables, customerNumber: customerNo, now, customer_id: id } });
  }

  async register(body) {
    const cases = Array.isArray(body.cases) ? body.cases : [];
    for (const c of cases) {
      const no = c && c.variables && str(c.variables.customerNumber ?? c.variables.customerNo);
      if (!c || !c.id || !no) return json({ ok: false, error: "each case needs id and variables.customerNumber" }, 400);
      if (!c.services && !CUSTOMERS[no]) return json({ ok: false, error: `case ${c.id}: customerNumber ${no} is not a test customer; give "services" in test-customers.json shape` }, 400);
      if (c.services) {
        for (const sv of ["cards.balance", "statement.week.get", "statement.week.decide", "statement.week.change"]) {
          for (const card of Object.keys(c.services[sv] || {})) {
            if (CARD_OWNER[card] && CARD_OWNER[card] !== no) return json({ ok: false, error: `case ${c.id}: card ${card} belongs to built-in customer ${CARD_OWNER[card]}` }, 400);
            await this.storage.put("card:" + card, no);
          }
        }
      }
      await this.storage.put("case:" + c.id, { id: c.id, variables: c.variables, services: c.services || null, faults: c.faults || [] });
    }
    return json({ ok: true, registered: cases.map((c) => c.id) });
  }

  // ---------- services ----------

  async service(service, request) {
    const body = await this.readJson(request);
    // The envelope is {data, call}; a flat body (fields at top level) is accepted too.
    const data = body && typeof body.data === "object" && body.data !== null ? body.data
               : (body && typeof body === "object" && !Array.isArray(body) ? body : null);

    // Which customer is this about? (to file the call in the right ledger)
    let customerNo = data ? str(data.customerNo) : "";
    if (isTemplate(customerNo)) customerNo = "90000001";
    let cardNumber = data ? str(data.cardNumber) : "";
    if (isTemplate(cardNumber)) cardNumber = "540000******1234";
    const reqCustomerNo = customerNo;                              // what the request carried (decides the answer)
    if (!customerNo && cardNumber) customerNo = CARD_OWNER[cardNumber] || (await this.storage.get("card:" + cardNumber)) || "";

    // Route to the practice session that is active for this customer (if any), else the customer's live ledger.
    let ledKey = null, led = null, caseObj = null;
    const active = customerNo ? await this.storage.get("active:" + customerNo) : null;
    if (active && Date.now() - active.at < SESSION_TTL_MS) {
      ledKey = "led:" + active.id; led = await this.storage.get(ledKey);
      caseObj = await this.storage.get("case:" + active.caseId);
    }
    if (!led && customerNo) {
      ledKey = "led:live:" + customerNo;
      led = (await this.storage.get(ledKey)) || newLedger({ case: customerNo, customerNo, now: null });
    }
    const services = (caseObj && caseObj.services) || (CUSTOMERS[customerNo] && CUSTOMERS[customerNo].services) || null;

    let out;
    led && (led.attempts[service] = (led.attempts[service] || 0) + 1);
    const fault = caseObj && (caseObj.faults || []).find((f) => f.tool === service);
    if (data === null) {
      out = ERR.invalid("request body must be a JSON object with a data object");
    } else if (fault && led.attempts[service] <= (fault.fail_first_n || 0)) {
      out = fault.http === 401 ? { http: 401, response: { error: "invalid_token" } } : ERR.technical();
    } else {
      out = this.answer(service, data, services, reqCustomerNo, cardNumber);
    }

    if (led) {
      const args = data ? { ...data } : {};
      led.calls.push({ n: led.calls.length + 1, tool: service, args, status: out.http, result: out.response });
      if (out.http === 200) record(led, service, data, customerNo, cardNumber);
      if (ledKey.startsWith("led:live:") && led.calls.length > LIVE_CALLS_CAP) led.calls = led.calls.slice(-LIVE_CALLS_CAP);
      await this.storage.put(ledKey, led);
    }
    return json(out.response, out.http, out.delaySeconds ? { "x-mock-delay": String(out.delaySeconds) } : {});
  }

  // The fixed answer for a request, from the test data (status + body). Never invents data.
  answer(service, d, services, customerNo, cardNumber) {
    const entry = (e) => (e ? { http: e.http, response: e.response, delaySeconds: e.delaySeconds || 0 } : null);
    const sv = services ? services[service] : null;
    switch (service) {
      case "cards.list": {
        if (!customerNo) return ERR.invalid("customer number cannot be empty");
        return entry(sv) || ERR.noCard();
      }
      case "cards.balance": {
        if (!cardNumber) return ERR.invalid("card number cannot be empty");
        return entry(sv && sv[cardNumber]) || ERR.balanceNotFound();
      }
      case "statement.week.get": {
        if (!cardNumber) return ERR.invalid("card number cannot be empty");
        return entry(sv && sv[cardNumber]) || ERR.weekNotFound();
      }
      case "statement.week.decide":
      case "statement.week.change": {
        if (!cardNumber) return ERR.invalid("card number cannot be empty");
        if (isEmpty(d.newStatementWeek)) return ERR.invalid("newStatementWeek cannot be empty");
        if (isEmpty(d.statementDay)) return ERR.invalid("statementDay cannot be empty");
        const week = str(d.newStatementWeek);
        const byCard = sv && sv[cardNumber];
        if (!byCard) return ERR.invalid("card number is not valid");
        if (!/^[1-4]$/.test(week)) return ERR.invalid("newStatementWeek must be between 1 and 4");
        return entry(byCard[week]) || ERR.invalid("newStatementWeek is the card's current statement week");
      }
      case "contact.email.get": {
        if (!customerNo) return ERR.invalid("customer number cannot be empty");
        if (isEmpty(d.intAddressInfoType)) return ERR.invalid("intAddressInfoType cannot be empty");
        return entry(sv) || ERR.invalid("customer number is not valid");
      }
      case "statement.pdf.send": {
        if (!customerNo) return ERR.invalid("customer number cannot be empty");
        if (!cardNumber) return ERR.invalid("card number cannot be empty");
        for (const f of ["currencyType", "statementDate", "emailAddress"]) if (isEmpty(d[f])) return ERR.invalid(f + " cannot be empty");
        return entry(sv) || ERR.noPdf();
      }
    }
    return ERR.technical();
  }
}

function newLedger({ case: caseId, customerNo, now }) {
  return {
    case: caseId, customerNo, now, identity: {},
    outcome: null, reason: null, promises: [], notes: [], callbacks: [], transfers: [],
    statement_week_previews: [], statement_week_changes: [], statement_pdf_sends: [], contact_lookups: [],
    attempts: {}, calls: [],
  };
}

// What a successful call recorded (only on HTTP 200). Changes are recorded, never applied to the fixed answers.
function record(led, service, d, customerNo, cardNumber) {
  if (service === "statement.week.decide" || service === "statement.week.change") {
    const rec = { cardNumber, newStatementWeek: Number(str(d.newStatementWeek)), statementDay: Number(str(d.statementDay)) };
    (service === "statement.week.change" ? led.statement_week_changes : led.statement_week_previews).push(rec);
  } else if (service === "statement.pdf.send") {
    led.statement_pdf_sends.push({ customerNo, cardNumber, currencyType: str(d.currencyType),
                                   statementDate: str(d.statementDate), emailAddress: str(d.emailAddress) });
  } else if (service === "contact.email.get") {
    led.contact_lookups.push({ customerNo, intAddressInfoType: str(d.intAddressInfoType).toUpperCase() });
  }
}
