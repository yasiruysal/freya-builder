"""Check the deployed final mock against the contract and every test customer's listed answers.

    MOCK_URL=https://freya-final-mock.<sub>.workers.dev python3 mockbank-final/check.py
"""
import concurrent.futures as cf, json, os, pathlib, sys, time, urllib.error, urllib.parse, urllib.request

URL = os.environ.get("MOCK_URL", "https://freya-final-mock.yasir-freya.workers.dev").rstrip("/")
DATA = json.loads((pathlib.Path(__file__).resolve().parent.parent / "cases/FreyaBank-Final/test-environment/test-customers.json").read_text())
CALL = {"ani": "05000000000", "lang": "tr"}
UA = "Mozilla/5.0 (mock-check)"
FAILS = []


def http(path, body=None, method=None, headers=None, raw=None):
    data = raw if raw is not None else (json.dumps(body).encode() if body is not None else None)
    h = {"content-type": "application/json", "accept": "application/json", "user-agent": UA, **(headers or {})}
    req = urllib.request.Request(URL + path, data=data, method=method or ("POST" if data is not None else "GET"), headers=h)
    try:
        with urllib.request.urlopen(req, timeout=40) as r: return r.status, json.loads(r.read() or b"null")
    except urllib.error.HTTPError as e:
        txt = e.read()
        try: return e.code, json.loads(txt or b"null")
        except Exception: return e.code, txt.decode(errors="replace")


TOKEN = None
def svc(name, data, token=True, extra_headers=None):
    h = {"authorization": "Bearer " + TOKEN} if token and TOKEN else {}
    return http("/voice/v1/" + name, {"data": data, "call": CALL}, headers={**h, **(extra_headers or {})})


def expect(label, got, status, body=None):
    s, b = got
    if s != status: FAILS.append(f"{label}: HTTP {s}, expected {status}: {str(b)[:200]}"); return False
    if body is not None and b != body: FAILS.append(f"{label}: body differs\n   got {json.dumps(b, ensure_ascii=False)[:300]}\n   exp {json.dumps(body, ensure_ascii=False)[:300]}"); return False
    return True


def expect_error(label, got, status, code, type_=None, msg=None):
    s, b = got
    ok = s == status and isinstance(b, dict) and "data" not in b and isinstance(b.get("result"), dict)
    if ok:
        r = b["result"]
        ok = (r.get("code") == code and r.get("returnCode") == code and r.get("message") == r.get("messageText")
              and (type_ is None or r.get("type") == type_) and (msg is None or r.get("message") == msg)
              and r.get("details", {}).get("guid") == r.get("guid") and len(r.get("guid") or "") == 32)
    if not ok: FAILS.append(f"{label}: expected HTTP {status} {code}, got {s} {str(b)[:250]}")
    return ok


def customer_requests(c):
    """Every (label, service, data, expected entry) listed for a test customer."""
    no, sv, out = c["customerNo"], c["services"], []
    out.append((f"{no} cards.list", "cards.list", {"customerNo": no, "aiFlow": "CardOps"}, sv["cards.list"]))
    out.append((f"{no} cards.list (number)", "cards.list", {"customerNo": int(no), "aiFlow": "CardOps"}, sv["cards.list"]))
    for card, e in sv.get("cards.balance", {}).items(): out.append((f"{no} cards.balance {card}", "cards.balance", {"cardNumber": card}, e))
    for card, e in sv.get("statement.week.get", {}).items(): out.append((f"{no} statement.week.get {card}", "statement.week.get", {"cardNumber": card}, e))
    for s in ("statement.week.decide", "statement.week.change"):
        for card, weeks in sv.get(s, {}).items():
            day = sv["statement.week.get"][card]["response"].get("data", {}).get("statementDay", 1)
            for w, e in weeks.items():
                out.append((f"{no} {s} {card} w{w}", s, {"cardNumber": card, "newStatementWeek": int(w), "statementDay": day}, e))
                out.append((f"{no} {s} {card} w'{w}'", s, {"cardNumber": card, "newStatementWeek": w, "statementDay": str(day)}, e))
    for t in ("E", "P", "B"):
        out.append((f"{no} contact.email.get {t}", "contact.email.get", {"customerNo": int(no), "intAddressInfoType": t}, sv["contact.email.get"]))
    cards = list(sv.get("cards.balance", {})) or ["540000******0000"]
    pdf = sv.get("statement.pdf.send") or {"http": 404, "code": "FB_IVR_CHANNEL_107"}
    out.append((f"{no} statement.pdf.send", "statement.pdf.send", {"customerNo": no, "cardNumber": cards[0], "currencyType": "TL", "statementDate": "05.10.2026", "emailAddress": "x@freyabank.example"}, pdf))
    return out


def run_customer(c):
    fails_before, n = len(FAILS), 0
    for label, s, data, e in customer_requests(c):
        n += 1
        if "response" in e:
            t0 = time.time(); got = svc(s, data)
            if expect(label, got, e["http"], e["response"]) and e.get("delaySeconds") and time.time() - t0 < e["delaySeconds"] - 0.5:
                FAILS.append(f"{label}: answered in {time.time() - t0:.1f}s, listed delay {e['delaySeconds']}s")
        else:
            expect_error(label, svc(s, data), e["http"], e["code"], "WARNING")
    return c["customerNo"], n, len(FAILS) == fails_before


def main():
    global TOKEN
    # ---- token ----
    s, b = http("/oauth2/token", raw=b"grant_type=client_credentials&client_id=test&client_secret=test&scope=voice.statement",
                headers={"content-type": "application/x-www-form-urlencoded"})
    if s == 200 and b.get("token_type") == "Bearer" and b.get("expires_in") == 1800 and b.get("access_token"): TOKEN = b["access_token"]
    else: FAILS.append(f"token: {s} {b}")

    # ---- every customer, every listed answer ----
    with cf.ThreadPoolExecutor(12) as ex:
        results = list(ex.map(run_customer, DATA["customers"]))
    ok_customers = sum(1 for _, _, ok in results if ok)
    n_requests = sum(n for _, n, _ in results)

    # ---- token not checked in test env (contract 2 and 6): no token / bogus token -> same answer ----
    ada = DATA["customers"][0]["services"]
    expect("no token", svc("cards.list", {"customerNo": "90000001"}, token=False), 200, ada["cards.list"]["response"])
    expect("bogus token", svc("cards.list", {"customerNo": "90000001"}, token=False, extra_headers={"authorization": "Bearer nope"}), 200, ada["cards.list"]["response"])
    # ---- unknown customer / card / week ----
    expect_error("unknown customer cards.list", svc("cards.list", {"customerNo": "99999999"}), 404, "FB_IVR_CHANNEL_102", "WARNING", "Müşteriye ait geçerli kart bilgisi bulunamadı.")
    expect_error("unknown card balance", svc("cards.balance", {"cardNumber": "540000******9999"}), 404, "FB_IVR_CHANNEL_120", "WARNING")
    expect_error("balance-format card number", svc("cards.balance", {"cardNumber": "5400********1234"}), 404, "FB_IVR_CHANNEL_120", "WARNING")
    expect_error("unknown card week.get", svc("statement.week.get", {"cardNumber": "540000******9999"}), 404, "FB_IVR_CHANNEL_121", "WARNING")
    expect_error("unknown card decide", svc("statement.week.decide", {"cardNumber": "540000******9999", "newStatementWeek": 2, "statementDay": 5}), 400, "INVALID_ARGUMENTS_ERROR", "ERROR")
    expect_error("current week decide", svc("statement.week.decide", {"cardNumber": "540000******1234", "newStatementWeek": 1, "statementDay": 5}), 400, "INVALID_ARGUMENTS_ERROR", "ERROR")
    expect_error("current week change", svc("statement.week.change", {"cardNumber": "540000******1234", "newStatementWeek": "1", "statementDay": 5}), 400, "INVALID_ARGUMENTS_ERROR", "ERROR")
    expect_error("week 5 decide", svc("statement.week.decide", {"cardNumber": "540000******1234", "newStatementWeek": 5, "statementDay": 5}), 400, "INVALID_ARGUMENTS_ERROR", "ERROR")
    expect_error("unknown customer email", svc("contact.email.get", {"customerNo": "99999999", "intAddressInfoType": "E"}), 400, "INVALID_ARGUMENTS_ERROR", "ERROR")
    expect_error("unknown customer pdf", svc("statement.pdf.send", {"customerNo": "99999999", "cardNumber": "540000******1234", "currencyType": "TL", "statementDate": "05.10.2026", "emailAddress": "a@b.example"}), 404, "FB_IVR_CHANNEL_107", "WARNING", "Pdf dokümanı bulunamadı")
    # ---- missing / empty fields (400, message names the field) ----
    expect_error("empty card balance", svc("cards.balance", {"cardNumber": ""}), 400, "INVALID_ARGUMENTS_ERROR", "ERROR", "card number cannot be empty")
    expect_error("missing card week.get", svc("statement.week.get", {}), 400, "INVALID_ARGUMENTS_ERROR", "ERROR", "card number cannot be empty")
    expect_error("missing customerNo cards.list", svc("cards.list", {"aiFlow": "CardOps"}), 400, "INVALID_ARGUMENTS_ERROR", "ERROR", "customer number cannot be empty")
    expect_error("empty intAddressInfoType", svc("contact.email.get", {"customerNo": 90000001, "intAddressInfoType": ""}), 400, "INVALID_ARGUMENTS_ERROR", "ERROR", "intAddressInfoType cannot be empty")
    expect_error("missing newStatementWeek", svc("statement.week.decide", {"cardNumber": "540000******1234", "statementDay": 5}), 400, "INVALID_ARGUMENTS_ERROR", "ERROR", "newStatementWeek cannot be empty")
    expect_error("missing statementDay", svc("statement.week.change", {"cardNumber": "540000******1234", "newStatementWeek": 2}), 400, "INVALID_ARGUMENTS_ERROR", "ERROR", "statementDay cannot be empty")
    for f in ("customerNo", "cardNumber", "currencyType", "statementDate", "emailAddress"):
        d = {"customerNo": "90000001", "cardNumber": "540000******1234", "currencyType": "TL", "statementDate": "05.10.2026", "emailAddress": "ornek@freyabank.example"}
        d[f] = ""
        expect_error(f"pdf empty {f}", svc("statement.pdf.send", d), 400, "INVALID_ARGUMENTS_ERROR", "ERROR")
    expect_error("invalid json", http("/voice/v1/cards.list", raw=b"{not json"), 400, "INVALID_ARGUMENTS_ERROR", "ERROR")
    s, _ = http("/voice/v1/cards.list", method="GET"); s == 405 or FAILS.append(f"GET on service: {s}, expected 405")
    expect("panel template variable", svc("cards.list", {"customerNo": "{{customerNumber}}"}), 200, ada["cards.list"]["response"])

    # ---- sessions: two runs of the same customer do not see each other's changes ----
    s1 = http("/v1/practice/sessions", {"case": "92220795"})[1]
    v1 = s1["variables"]
    if v1.get("customerNumber") != "92220795" or not v1.get("now") or not v1.get("customer_id"): FAILS.append(f"session variables: {v1}")
    sv = next(c for c in DATA["customers"] if c["customerNo"] == "92220795")["services"]
    card = "540000******3201"
    expect("s1 week.get", svc("statement.week.get", {"cardNumber": card}), 200, sv["statement.week.get"][card]["response"])
    expect("s1 decide", svc("statement.week.decide", {"cardNumber": card, "newStatementWeek": 2, "statementDay": 16}), 200, sv["statement.week.decide"][card]["2"]["response"])
    expect("s1 change", svc("statement.week.change", {"cardNumber": card, "newStatementWeek": 2, "statementDay": 16}), 200, sv["statement.week.change"][card]["2"]["response"])
    expect("s1 week.get after change (fixed answer)", svc("statement.week.get", {"cardNumber": card}), 200, sv["statement.week.get"][card]["response"])
    s2 = http("/v1/practice/sessions", {"case": "92220795", "now": "2026-10-13T09:00:00+03:00"})[1]["variables"]
    if s2["customer_id"] == v1["customer_id"] or s2["now"] != "2026-10-13T09:00:00+03:00": FAILS.append(f"second session: {s2}")
    expect("s2 week.get (no change seen)", svc("statement.week.get", {"cardNumber": card}), 200, sv["statement.week.get"][card]["response"])
    expect("s2 email", svc("contact.email.get", {"customerNo": "92220795", "intAddressInfoType": "E"}), 200, sv["contact.email.get"]["response"])
    expect("s2 pdf", svc("statement.pdf.send", {"customerNo": "92220795", "cardNumber": card, "currencyType": "TL", "statementDate": "16.09.2026", "emailAddress": "sevda.karaca@freyabank.example"}), 200, sv["statement.pdf.send"]["response"])
    l1 = http("/v1/practice/ledger/" + v1["customer_id"])[1]
    l2 = http("/v1/practice/ledger/" + s2["customer_id"])[1]
    want1 = {"statement_week_changes": [{"cardNumber": card, "newStatementWeek": 2, "statementDay": 16}],
             "statement_week_previews": [{"cardNumber": card, "newStatementWeek": 2, "statementDay": 16}], "statement_pdf_sends": []}
    want2 = {"statement_week_changes": [], "statement_week_previews": [],
             "statement_pdf_sends": [{"customerNo": "92220795", "cardNumber": card, "currencyType": "TL", "statementDate": "16.09.2026", "emailAddress": "sevda.karaca@freyabank.example"}]}
    for lab, led, want, ntools in (("ledger s1", l1, want1, ["statement.week.get", "statement.week.decide", "statement.week.change", "statement.week.get"]),
                                   ("ledger s2", l2, want2, ["statement.week.get", "contact.email.get", "statement.pdf.send"])):
        for k, v in want.items():
            if led.get(k) != v: FAILS.append(f"{lab} {k}: {led.get(k)} != {v}")
        if [c["tool"] for c in led.get("calls", [])] != ntools: FAILS.append(f"{lab} calls: {[c['tool'] for c in led.get('calls', [])]}")
        for k in ("outcome", "reason", "promises", "notes", "callbacks", "transfers", "calls", "identity"):
            if k not in led: FAILS.append(f"{lab}: missing key {k}")
    # ---- register a test customer at run time, with a fault ----
    r = http("/v1/practice/register", {"cases": [{"id": "CHK-1", "variables": {"customerNumber": "90000001", "now": "2026-10-12T10:30:00+03:00"},
                                                   "faults": [{"tool": "cards.balance", "fail_first_n": 1}]}]})[1]
    if not r.get("ok"): FAILS.append(f"register: {r}")
    v3 = http("/v1/practice/sessions", {"case": "CHK-1"})[1]["variables"]
    expect_error("fault first balance", svc("cards.balance", {"cardNumber": "540000******1234"}), 500, "FBE00099", "ERROR")
    expect("fault second balance", svc("cards.balance", {"cardNumber": "540000******1234"}), 200, ada["cards.balance"]["540000******1234"]["response"])
    l3 = http("/v1/practice/ledger/" + v3["customer_id"])[1]
    if [c["status"] for c in l3.get("calls", [])] != [500, 200]: FAILS.append(f"fault ledger: {l3.get('calls')}")
    for v in (v1, s2, v3): http("/v1/practice/end", {"customer_id": v["customer_id"]})
    s, _ = http("/v1/practice/ledger/s_nope"); s == 404 or FAILS.append("unknown ledger should 404")

    print(f"{ok_customers}/{len(DATA['customers'])} test customers: every listed answer matches ({n_requests} requests)")
    for no, _, ok in results:
        if not ok: print("  customer FAIL", no)
    print("FAILURES:" if FAILS else "ALL CHECKS PASSED")
    for f in FAILS: print(" -", f)
    sys.exit(1 if FAILS else 0)


if __name__ == "__main__":
    main()
