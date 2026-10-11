"""Generate out/example-v3_brief-a.workflow.json for Brief A."""
import json, sys

BASE = "https://freya-bank-mock.freyavoice.workers.dev/v1/"
BLOCK_URL = "http://127.0.0.1:1/blocked"
OUT = sys.argv[1] if len(sys.argv) > 1 else "out/example-v3_brief-a.workflow.json"

HDR = {"type": "object", "properties": {"Content-Type": {"type": "string", "default": "application/json"}}}


def api_fn(name, endpoint, body_props, required=None, description="", retries=0):
    props = {"customer_id": {"type": "string", "default": "{{customer_id}}"}}
    props.update(body_props)
    return {
        "type": "function",
        "config": {"timeoutMs": 12000, "maxRetries": retries},
        "function": {
            "name": name,
            "handler": "__function__:api_call",
            "description": description,
            "parameters": {
                "type": "object",
                "properties": {
                    "url": {"type": "string", "default": BASE + endpoint},
                    "method": {"type": "string", "default": "POST"},
                    "connect_retries": {"type": "number", "default": 0},
                    "headers": HDR,
                    "body": {"type": "object", "properties": props, "required": required or []},
                },
            },
        },
    }


def cond_any(*conds):
    return {"logic": "or", "conditions": list(conds)}


def c(var, op, operand=None):
    d = {"variable": var, "operator": op}
    if operand is not None:
        d["operand"] = operand
    return d


def is_true(var):
    return [c(var, "equals", "true"), c(var, "equals", "True")]


def and_(*conds):
    return {"logic": "and", "conditions": list(conds)}


def tr(target, desc, condition):
    return {"target_node": target, "description": desc, "condition": condition}


ALWAYS = {"operator": "always"}


def tool_tr(target, desc):
    return {"target_node": target, "description": desc, "is_tool_call": True,
            "condition": {"operator": "matches_intent", "description": desc}}


def code_node(name, code, nxt):
    return {
        "name": name, "node_type": "code", "passthrough": True, "passthrough_default_transition": nxt,
        "on_enter": {"type": "silent"}, "disable_inactivity_monitor": True,
        "pre_actions": [{
            "type": "function",
            "function": {"name": "code_exec", "handler": "__function__:code_exec",
                         "parameters": {"type": "object", "properties": {
                             "code": {"type": "string", "default": code},
                             "timeout_ms": {"type": "number", "default": 10000}}}}}],
        "transitions": [tr(nxt, "always", ALWAYS)],
    }


CLOSE_PRE = "try:\n    vars['closed'] = 'yes'\nexcept Exception:\n    pass\n"


def close_fn(outcome, reason):
    props = {"outcome": {"type": "string", "default": outcome}}
    if reason:
        props["reason"] = {"type": "string", "default": reason}
    f = api_fn("close_call", "close_call", props, description="Close the call.")
    f["pre_process_code"] = CLOSE_PRE
    return f


def transfer_fn(queue):
    return api_fn("transfer_to_agent", "transfer_to_agent", {"queue": {"type": "string", "default": queue}},
                  description="Transfer.")


def close_node(name, text, outcome, reason, before=None):
    acts = ([before] if before else []) + [close_fn(outcome, reason)]
    return {
        "name": name, "node_type": "api_request", "passthrough": True, "passthrough_default_transition": "hang_up",
        "on_enter": {"type": "tts", "text": text}, "disable_inactivity_monitor": True,
        "pre_actions": acts,
        "transitions": [tr("hang_up", "always", ALWAYS)],
    }


# ---------------------------------------------------------------- code snippets
DATES_LIB = r'''
import datetime, re
HOL = set(['2026-10-26', '2026-11-11', '2026-11-26', '2026-12-25', '2027-01-01'])
WD = ['Monday', 'Tuesday', 'Wednesday', 'Thursday', 'Friday', 'Saturday', 'Sunday']
MO = ['January', 'February', 'March', 'April', 'May', 'June', 'July', 'August', 'September', 'October', 'November', 'December']
def isbd(d):
    return d.weekday() < 5 and d.isoformat() not in HOL
def nextbd(d):
    d = d + datetime.timedelta(days=1)
    while not isbd(d):
        d = d + datetime.timedelta(days=1)
    return d
def addbd(d, n):
    for i in range(n):
        d = nextbd(d)
    return d
def say(d):
    return WD[d.weekday()] + ' ' + str(d.day) + ' ' + MO[d.month - 1] + ' ' + str(d.year)
def lastbd_of_week(d):
    f = d + datetime.timedelta(days=4 - d.weekday())
    while not isbd(f):
        f = f - datetime.timedelta(days=1)
    return f
nowraw = str(vars.get('now') or '')
try:
    today = datetime.date.fromisoformat(nowraw[:10])
except Exception:
    today = datetime.date.today()
nowhm = nowraw[11:16] if len(nowraw) >= 16 else '09:00'
'''

SETUP_CODE = DATES_LIB + r'''
try:
    nb = nextbd(today)
    vars['today_iso'] = today.isoformat()
    vars['today_say'] = say(today)
    vars['now_hm'] = nowhm
    vars['cb_next_bd'] = nb.isoformat()
    lines = []
    lines.append('Now: ' + say(today) + ', ' + nowhm + '.')
    if isbd(today) and nowhm < '19:00':
        start = '10:00' if nowhm < '10:00' else 'later than ' + nowhm
        lines.append('Allowed today (' + today.isoformat() + '): from ' + start + ' until 19:00.')
        vars['cb_today_ok'] = 'yes'
    else:
        lines.append('Today is not possible.')
        vars['cb_today_ok'] = 'no'
    lines.append('Allowed on the next business day, ' + say(nb) + ' (' + nb.isoformat() + '): 10:00 to 19:00, both included.')
    lines.append('Any other day, or a time outside these hours, is NOT allowed.')
    lines.append('Tomorrow is ' + say(today + datetime.timedelta(days=1)) + '.')
    vars['callback_facts'] = '\n'.join(lines)
except Exception as e:
    vars['callback_facts'] = 'Callback window: today or the next business day, 10:00 to 19:00.'
vars['topic'] = 'none'
'''

ACCOUNT_CODE = DATES_LIB + r'''
import json
def num(x):
    try:
        return float(x)
    except Exception:
        return 0.0
def fmt(x):
    x = round(num(x), 2)
    return str(int(x)) if x == int(x) else ('%.2f' % x)
prods = []
raw = vars.get('products')
try:
    if isinstance(raw, str):
        raw = json.loads(raw)
    if isinstance(raw, dict):
        raw = [raw]
    if isinstance(raw, list):
        prods = [p for p in raw if isinstance(p, dict)]
except Exception:
    prods = []
if not prods:
    found = dict()
    for k in list(vars.keys()):
        m = re.search(r'products[.\[](\d+)\]?\.(\w+)$', str(k))
        if m:
            found.setdefault(int(m.group(1)), dict())[m.group(2)] = vars.get(k)
    prods = [found[i] for i in sorted(found)]
p = prods[0] if prods else dict()
try:
    ptype = str(p.get('type') or '')
    if ptype == 'credit_card':
        words = 'credit card ending ' + str(p.get('card_last4') or '')
        floor = num(p.get('minimum_payment'))
        floor_name = 'minimum payment'
    elif ptype == 'car_loan':
        words = 'car loan'
        floor = num(p.get('instalment_amount'))
        floor_name = 'instalment amount'
    else:
        words = 'personal loan'
        floor = num(p.get('instalment_amount'))
        floor_name = 'instalment amount'
    due = num(p.get('past_due'))
    late = int(num(p.get('days_late')))
    band = 'early' if late <= 7 else 'late'
    deadline = addbd(today, 12 if band == 'early' else 7)
    a1 = lastbd_of_week(today)
    if a1 <= today:
        a1 = lastbd_of_week(today + datetime.timedelta(days=7))
    vars['product_id'] = str(p.get('product_id') or 'p1')
    vars['product_words'] = words
    vars['past_due'] = round(due, 2)
    vars['floor'] = round(floor, 2)
    vars['past_due_text'] = fmt(due)
    vars['floor_text'] = fmt(floor)
    vars['days_late'] = late
    vars['band'] = band
    vars['deadline_iso'] = deadline.isoformat()
    vars['deadline_say'] = say(deadline)
    valid = []
    d = today
    while d <= deadline:
        if isbd(d):
            valid.append(d.isoformat())
        d = d + datetime.timedelta(days=1)
    vars['valid_dates'] = ','.join(valid)
    cal = []
    d = today
    end = deadline + datetime.timedelta(days=10)
    while d <= end:
        tag = d.isoformat() + ' ' + say(d) + ': '
        if d > deadline:
            tag += 'AFTER THE DEADLINE - not allowed, offer the deadline ' + say(deadline)
        elif d.weekday() >= 5:
            tag += 'weekend - not allowed, offer ' + say(nextbd(d))
        elif d.isoformat() in HOL:
            tag += 'bank holiday - not allowed, offer ' + say(nextbd(d))
        else:
            tag += 'allowed'
        if d == today:
            tag += ' (TODAY)'
        if d == deadline:
            tag += ' (DEADLINE, the latest allowed date)'
        cal.append(tag)
        d = d + datetime.timedelta(days=1)
    monday = today - datetime.timedelta(days=today.weekday())
    wk = []
    for i in range(7):
        t = monday + datetime.timedelta(days=i)
        wk.append('"this ' + WD[i] + '" = ' + say(t) + '; "next ' + WD[i] + '" = ' + say(t + datetime.timedelta(days=7)))
    f = []
    f.append('Today: ' + say(today) + ' (' + today.isoformat() + ').')
    f.append('Product: ' + words + '.')
    f.append('Full past-due amount: ' + fmt(due) + '.')
    f.append('Floor (the ' + floor_name + '): ' + fmt(floor) + '. Amounts from ' + fmt(floor) + ' up to ' + fmt(due) + ' are allowed. Below ' + fmt(floor) + ' is not allowed.')
    f.append('Days late: ' + str(late) + ' (' + band + ' band).')
    f.append('DEADLINE (latest date you may record): ' + say(deadline) + ' (' + deadline.isoformat() + ').')
    f.append('Persuasion attempt 1 date: ' + say(a1) + ' (' + a1.isoformat() + ').')
    if band == 'late':
        f.append('Persuasion attempts allowed: 2. Attempt 2 = the floor ' + fmt(floor) + ' on the deadline ' + say(deadline) + '.')
    else:
        f.append('Persuasion attempts allowed: 1. There is NO attempt 2.')
    f.append('')
    f.append('Weekday words:')
    f.extend(wk)
    f.append('')
    f.append('Calendar (look every date up here):')
    f.extend(cal)
    vars['account_facts'] = '\n'.join(f)
    vars['account_state'] = 'ok' if prods else 'missing'
except Exception as e:
    vars['account_facts'] = 'Account data could not be read.'
    vars['account_state'] = 'missing'
    vars['product_words'] = 'account'
    vars['past_due_text'] = 'unknown'
    vars['days_late'] = 'some'
'''

RESET_TOPIC = "try:\n    vars['topic'] = 'none'\nexcept Exception:\n    pass\n"

# ---------------------------------------------------------------- hooks
POST_COMMON = r'''
def _v():
    try:
        if isinstance(vars, dict):
            return vars
    except Exception:
        pass
    return dict()
'''

VERIFY_PRE = r'''
import re, datetime
try:
    b = config.get('body')
    if isinstance(b, dict) and b.get('field') == 'date_of_birth':
        a = str(b.get('answer') or '').strip()
        MO = ['jan', 'feb', 'mar', 'apr', 'may', 'jun', 'jul', 'aug', 'sep', 'oct', 'nov', 'dec']
        out = ''
        if re.match(r'^\d{4}-\d{2}-\d{2}$', a):
            out = a
        else:
            m = re.match(r'^(\d{1,2})[./\- ](\d{1,2})[./\- ](\d{4})$', a)
            if m:
                out = '%s-%02d-%02d' % (m.group(3), int(m.group(2)), int(m.group(1)))
            else:
                low = a.lower()
                mon = 0
                for i, n in enumerate(MO):
                    if re.search(r'\b' + n, low):
                        mon = i + 1
                        break
                nums = re.findall(r'\d+', low)
                yr = [x for x in nums if len(x) == 4]
                dy = [x for x in nums if len(x) <= 2]
                if mon and yr and dy:
                    out = '%s-%02d-%02d' % (yr[0], mon, int(dy[0]))
        if out:
            b['answer'] = out
    if isinstance(b, dict) and b.get('field') == 'phone_last4':
        digits = re.sub(r'\D', '', str(b.get('answer') or ''))
        if len(digits) >= 4:
            b['answer'] = digits[-4:]
except Exception:
    pass
'''

VERIFY_POST = r'''
try:
    if isinstance(result, dict):
        result.pop('header', None)
        d = result.get('data')
        if isinstance(d, dict):
            if d.get('ok') is False:
                d['what_to_do_next'] = 'The answer could not be read as a date. This does not count as a wrong answer. Ask the customer to say the date of birth again with the month as a word.'
            elif d.get('all_verified'):
                d['what_to_do_next'] = 'Both answers are correct.'
            elif d.get('hint') == 'day_month_swapped':
                d['what_to_do_next'] = 'Day and month seem swapped. This does not count as a wrong answer. Ask the customer to say the date of birth again with the month as a word.'
            elif d.get('verified'):
                d['what_to_do_next'] = 'Correct. Now ask for the last four digits of the phone number on file.'
            elif d.get('wrong_answers_left') == 0:
                d['what_to_do_next'] = 'Wrong answer. No attempts left.'
            else:
                d['what_to_do_next'] = 'That answer does not match our records. Tell the customer it does not match and ask the same question again. Do not give any hint.'
except Exception:
    pass
'''

PROMISE_PRE = r'''
try:
    vars['promise_block_msg'] = ''
    b = config.get('body')
    if isinstance(b, dict):
        d = str(b.get('date') or '')[:10]
        valid = [x for x in str(vars.get('valid_dates') or '').split(',') if x]
        msg = ''
        try:
            amt = float(str(b.get('amount')).replace(',', ''))
        except Exception:
            amt = -1.0
        full = float(vars.get('past_due') or 0)
        fl = float(vars.get('floor') or 0)
        already = str(vars.get('create_promise_response.data.ok') or '').lower() == 'true'
        if already:
            msg = 'A promise is already recorded for this product. It cannot be changed in this call; the customer can change it in the mobile app.'
        elif valid and d not in valid:
            if d > valid[-1]:
                msg = 'NOT recorded. ' + d + ' is after the deadline. Tell the customer the latest date you can record is ' + valid[-1] + ' and offer that date.'
            elif d < valid[0]:
                msg = 'NOT recorded. ' + d + ' is in the past. Offer today or a later business day.'
            else:
                nxt = [x for x in valid if x > d][0]
                msg = 'NOT recorded. ' + d + ' is not a business day. Offer the next business day ' + nxt + ' and read back again.'
        elif full and (amt < fl - 0.005 or amt > full + 0.005):
            if amt > full + 0.005:
                msg = 'NOT recorded. The amount is more than the past-due amount ' + str(full) + '. Use an amount up to ' + str(full) + '.'
            else:
                msg = 'NOT recorded. The amount is below the floor ' + str(fl) + ' and cannot be recorded. Offer the floor ' + str(fl) + '.'
        if msg:
            vars['promise_block_msg'] = msg
            config['url'] = 'BLOCK_URL'
        else:
            b['amount'] = round(amt, 2)
            b['date'] = d
except Exception:
    pass
'''.replace("BLOCK_URL", BLOCK_URL)

PROMISE_POST = POST_COMMON + r'''
try:
    v = _v()
    if isinstance(result, dict):
        result.pop('header', None)
        d = result.get('data')
        if not isinstance(d, dict):
            d = dict()
            result['data'] = d
        blocked = str(v.get('promise_block_msg') or '')
        sc = result.get('status_code')
        if not blocked and d.get('ok') is not True and not d.get('error') and sc not in (500, 502, 503, 504, 200):
            blocked = 'NOT recorded: the date or the amount is not allowed. Check the calendar and the allowed amounts, propose an allowed value and read back again.'
        if d.get('ok') is True:
            d['what_to_do_next'] = 'Recorded.'
        elif blocked:
            d['ok'] = False
            d['recorded'] = False
            d['what_to_do_next'] = blocked
        else:
            n = int(float(v.get('promise_service_fails') or 0)) + 1
            v['promise_service_fails'] = n
            d['ok'] = False
            d['recorded'] = False
            if n < 2:
                d['what_to_do_next'] = 'The service failed and NOTHING was recorded. Do not say it is recorded. Call create_promise again right now with the same amount and date.'
            else:
                d['what_to_do_next'] = 'The service failed again and NOTHING was recorded. Tell the customer you could not record the promise, then call transition_to_close_service_error.'
except Exception:
    pass
'''

CB_PRE = r'''
try:
    vars['cb_block_msg'] = ''
    b = config.get('body')
    if isinstance(b, dict):
        d = str(b.get('date') or '')[:10]
        t = str(b.get('time') or '').strip()
        if len(t) == 4 and t[1] == ':':
            t = '0' + t
        t = t[:5]
        b['time'] = t
        b['date'] = d
        ok = '10:00' <= t <= '19:00'
        if d == str(vars.get('today_iso')):
            ok = ok and str(vars.get('cb_today_ok')) == 'yes' and t > str(vars.get('now_hm') or '00:00')
        elif d != str(vars.get('cb_next_bd')):
            ok = False
        if not ok:
            vars['cb_block_msg'] = 'NOT booked: ' + d + ' ' + t + ' is outside the allowed callback window. If this was the first time outside the window, ask once for another time today or on the next business day between 10:00 and 19:00. If it is the second time, call transition_to_close_no_callback.'
            config['url'] = 'BLOCK_URL'
except Exception:
    pass
'''.replace("BLOCK_URL", BLOCK_URL)

CB_POST = POST_COMMON + r'''
try:
    v = _v()
    if isinstance(result, dict):
        result.pop('header', None)
        d = result.get('data')
        if not isinstance(d, dict):
            d = dict()
            result['data'] = d
        blocked = str(v.get('cb_block_msg') or '')
        if blocked:
            d['ok'] = False
            d['what_to_do_next'] = blocked
        elif d.get('ok') is not True:
            d['what_to_do_next'] = 'The callback could not be booked. Say so honestly.'
except Exception:
    pass
'''

FALLBACK_PRE = r'''
try:
    done = str(vars.get('closed') or '') == 'yes'
    for k in list(vars.keys()):
        ks = str(k)
        if ks.startswith('close_') and (ks.endswith('_success_count') or ks.endswith('_failure_count')):
            done = True
    if done:
        config['url'] = 'BLOCK_URL'
    b = config.get('body')
    if isinstance(b, dict) and b.get('outcome') in ('promise', 'paid'):
        b.pop('reason', None)
except Exception:
    pass
'''.replace("BLOCK_URL", BLOCK_URL)

# ---------------------------------------------------------------- prompts
SYSTEM = """You are the digital assistant of Freya Bank, on an outbound phone call. Speak English. Keep every turn to one or two short sentences. Ask one question at a time.
The customer you are calling is {{title}} {{last_name}} ({{first_name}} {{last_name}}). Address them as {{title}} {{last_name}}.
Current date and time: {{now}}. Use only this date, never the real clock.

Rules that always hold:
- If asked whether you are a robot or an AI: say you are Freya Bank's digital assistant, then continue.
- If asked whether this is a scam: say they can verify the call by phoning Freya Bank's customer service number, then continue.
- If asked something you cannot answer from what you were given: say you do not have that information. Never invent figures, dates or reasons.
- Never offer a discount, a waiver, an instalment plan or any other term.
- Never say that something is recorded, booked or done unless a tool result says ok true.
- After every tool result, speak to the customer or call the next tool. Never end a turn with nothing.
- When the customer gives a number or date in pieces, wait for the whole of it."""

GREETING_TASK = """You have just said the opening line and asked whether you are speaking with {{title}} {{last_name}}.
Your only job: find out who is on the line. Do NOT say why you are calling. Do NOT mention collections, debt, payment, an amount or any product.
- Only "yes", "that's me", "speaking" or the customer's own name confirms it. "Hello", "go ahead" or "who is this" do not.
- If the answer is unclear, ask once more: "Am I speaking with {{title}} {{last_name}}?" If they asked a question, answer it briefly first, then ask.
- If they ask why you are calling, say you can only discuss it with {{title}} {{last_name}} and ask again whether that is who you are speaking with.
No tool call is allowed in this step."""

IDENTITY_TASK = """The customer confirmed they are {{title}} {{last_name}}. Verify identity with two security questions, one at a time:
1. Date of birth. Call verify_identity with field "date_of_birth" and the answer as YYYY-MM-DD. A date said only as numbers is day, then month, then year (05/12/1975 = 1975-12-05).
2. Then the last four digits of the phone number on file. Call verify_identity with field "phone_last4" and the four digits.
Call verify_identity every time the customer gives an answer. Each result has a what_to_do_next line: do exactly what it says.
Do NOT say why you are calling and do NOT mention collections, debt, payment, an amount or any product before both answers pass. If asked what the call is about, the questions protect their account.
If the customer does not want to answer, or asks what the call is about instead of answering: the first time, explain once that the questions protect their account and ask again. The second time, call transition_to_close_declined.
If the customer says twice that they do not want to talk, call transition_to_close_declined.
If the customer asks for a human or a real person, call transition_to_human_check_pre."""

NEGOTIATE_TASK = """The customer is verified. You have told them the debt and asked "Can you make this payment today?".
Your job: get a payment promise that follows the rules, read it back, and record it with create_promise.

FACTS (computed for you, trust them over your own arithmetic):
{{account_facts}}

AMOUNTS
- Never ask how much the customer can pay. You propose every amount.
- Start with the full past-due amount.
- If the customer says the full amount is too much: offer the floor ONCE and say that the rest stays overdue.
- If the customer proposes an amount from the floor up to the full amount: accept it. Below the floor: say it cannot be recorded and offer the floor once.

DATES
- A promise date must be today or later, a business day, and not after the deadline. Look every date up in the calendar above.
- A weekend or a bank holiday not after the deadline: offer the next business day shown in the calendar.
- A date after the deadline: say the latest date you can record is the deadline, and offer the deadline.
- A vague period ("next week", "the week after next", "end of the month", "when my salary comes"): ask once for the exact day. If they still give no exact day, propose the deadline.
- "This Friday" / "next Friday": use the weekday words above.

REFUSALS AND PERSUASION
- A date or amount the customer proposes is never a refusal. Handle it with the rules above.
- A flat no or a non-answer (for example "no, I can't pay today" with no date and no amount) uses one persuasion attempt:
  - Attempt 1: the full amount (or the floor if they already said the full amount is too much) on the attempt 1 date.
  - Attempt 2, only if the facts allow 2 attempts: the floor on the deadline.
- Refusing the floor offer, or refusing the deadline offered for a too-late date, also counts as a flat refusal.
- When the allowed attempts are used up and the customer still refuses, do not repeat any offer: call transition_to_close_refused.
- If the customer says twice that they do not want to talk, call transition_to_close_refused.

CONFIRMING AND RECORDING
- When a valid amount and date are agreed, read back the product, the amount and the date (weekday, day and month) and ask "Do you confirm?".
- Only after a clear yes, call create_promise with amount (a number) and date (YYYY-MM-DD). In that turn do not say it is recorded.
- If the customer changes the date or the amount, check it again and read back again.
- If create_promise does not return ok true, follow its what_to_do_next line exactly. Never say it is recorded when it is not."""

THIRD_TASK = """The person on the line is not {{title}} {{last_name}}.
Do NOT say why you are calling. Say nothing about the account, any debt, payment, amount or product, even if they ask or offer to pay. Do not ask any security question.
Your only job: find out when {{title}} {{last_name}} can be reached, and book a callback.

CALLBACK WINDOW:
{{callback_facts}}

- When the person names a day and time inside the window, call schedule_callback with date (YYYY-MM-DD) and time (HH:MM, 24 hour). "Around eleven in the morning" is 11:00. "Tomorrow" is the date shown above.
- If they offer a day or time outside the window, ask once for another time inside it. If the second answer is also outside it, or they cannot give any time, call transition_to_close_no_callback.
- Follow the what_to_do_next line of every tool result exactly."""

HUMAN_TASK = """The customer asked for a human. Ask once, briefly, whether you can help them first. Do not transfer yet.
No tool call is allowed in this step."""

HARDSHIP_TASK = """The customer mentioned a hardship (illness, a death in the family or job loss). You already said one sentence of sympathy and offered to connect them with the customer support team.
Do not try to persuade them to pay. Do not offer a discount, waiver or plan; if they ask for one, say you cannot offer that.
Do not repeat the offer of the support team more than once. Wait for their answer.
No tool call is allowed in this step."""

# ---------------------------------------------------------------- nodes
nodes = {}
nodes["setup"] = code_node("setup", SETUP_CODE, "greeting")

nodes["greeting"] = {
    "name": "greeting", "node_type": "conversation",
    "on_enter": {"type": "tts", "text": "Hello, this is the digital assistant of Freya Bank calling, and this call is recorded. Am I speaking with {{title}} {{last_name}}?"},
    "on_enter_first_entry_only": True,
    "task_message": GREETING_TASK,
    "extraction_schema": [{
        "name": "who_answered", "type": "string",
        "enum_values": ["confirmed", "someone_else", "wrong_number", "deceased", "not_clear"],
        "description": "Who is on the line, judged from the person's LAST message only. confirmed: they clearly say yes, that's me, speaking, or say the customer's own name as theirs. someone_else: they say they are a different person (a relative, partner, colleague) or that the customer is not there or not available. wrong_number: they say the number does not belong to the customer or they do not know the customer. deceased: they say the customer has died. not_clear: anything else (hello, who is this, go ahead, a question).",
        "min_confidence": 0.6}],
    "max_node_turns": 4, "max_turn_transition": "close_unclear",
    "transitions": [
        tr("identity", "The customer confirmed", cond_any(c("who_answered", "equals", "confirmed"))),
        tr("close_deceased", "Customer died", cond_any(c("who_answered", "equals", "deceased"))),
        tr("close_wrong_number", "Wrong number", cond_any(c("who_answered", "equals", "wrong_number"))),
        tr("third_party", "Someone else answered", cond_any(c("who_answered", "equals", "someone_else"))),
        tr("close_unclear", "Still unclear after the second ask", cond_any(c("greeting.current_turns", "greater_than", 1))),
        tr("greeting", "Ask again", ALWAYS),
    ],
}

verify_tool = api_fn("verify_identity", "verify_identity", {
    "field": {"type": "string", "enum": ["date_of_birth", "phone_last4"], "default": "{{field}}",
              "description": "Which answer this is: date_of_birth or phone_last4."},
    "answer": {"type": "string", "default": "{{answer}}",
               "description": "date_of_birth as YYYY-MM-DD (numbers only = day, month, year). phone_last4 as four digits."},
}, ["field", "answer"], "Check one security answer. CALL it every time the customer gives a date of birth or phone digits.")
verify_tool["pre_process_code"] = VERIFY_PRE
verify_tool["post_process_code"] = VERIFY_POST

nodes["identity"] = {
    "name": "identity", "node_type": "conversation",
    "on_enter": {"type": "tts", "text": "Thank you. Before we continue, I need to ask two security questions. What is your date of birth?"},
    "on_enter_first_entry_only": True,
    "task_message": IDENTITY_TASK,
    "tools": [verify_tool],
    "max_node_turns": 14, "max_turn_transition": "close_declined",
    "transitions": [
        tr("load_account", "Both answers verified", cond_any(*is_true("verify_identity_response.data.all_verified"))),
        tr("close_id_failed", "Three wrong answers", cond_any(c("verify_identity_response.data.wrong_answers_left", "equals", "0"))),
        tool_tr("close_declined", "The customer refused the security questions a second time, or said twice they do not want to talk"),
        tool_tr("human_check_pre", "The customer explicitly asks to speak with a human person (asking whether you are a robot is NOT this)"),
    ],
}

nodes["human_check_pre"] = {
    "name": "human_check_pre", "node_type": "conversation",
    "on_enter": {"type": "llm", "prompt": "The customer asked for a human. If they also asked whether you are a robot or an AI, say you are Freya Bank's digital assistant. Then ask once, briefly, whether you can help them yourself first. Do not mention why you are calling. One or two short sentences. No tool call."},
    "task_message": HUMAN_TASK,
    "extraction_schema": [{"name": "human_insist_pre", "type": "string", "enum_values": ["insist", "continue"],
                           "description": "From the customer's LAST message: insist if they still want a human or a person; continue if they agree to continue with you.",
                           "min_confidence": 0.6}],
    "max_node_turns": 3, "max_turn_transition": "transfer_human",
    "transitions": [
        tr("transfer_human", "Insists", cond_any(c("human_insist_pre", "equals", "insist"))),
        tr("identity", "Continue", cond_any(c("human_insist_pre", "equals", "continue"))),
        tr("human_check_pre", "stay", ALWAYS),
    ],
}

nodes["load_account"] = {
    "name": "load_account", "node_type": "api_request", "passthrough": True,
    "passthrough_default_transition": "compute_account", "on_enter": {"type": "silent"},
    "disable_inactivity_monitor": True, "suppress_tool_results": True,
    "pre_actions": [api_fn("get_account", "get_account", {}, description="Get the late products.", retries=1)],
    "response_mapping": [{"path": "data.products", "variable": "products"}],
    "transitions": [tr("compute_account", "always", ALWAYS)],
}
nodes["compute_account"] = code_node("compute_account", ACCOUNT_CODE, "negotiate")

promise_tool = api_fn("create_promise", "create_promise", {
    "product_id": {"type": "string", "default": "{{product_id}}"},
    "amount": {"type": "number", "default": "{{amount}}", "description": "The agreed amount, a number like 420 or 637.5."},
    "date": {"type": "string", "default": "{{date}}", "description": "The agreed payment date as YYYY-MM-DD."},
}, ["amount", "date"], "Record the payment promise. CALL it only after the customer said yes to your read-back of product, amount and date.")
promise_tool["pre_process_code"] = PROMISE_PRE
promise_tool["post_process_code"] = PROMISE_POST

TOPIC_DESC = ("What the customer's LAST message is about. "
              "already_paid: they say they already paid this past-due amount (for example today or in the app). "
              "dispute: they say the debt is not theirs, they never had this product, or the product was closed or paid off in the past. "
              "hardship: they mention illness, a death in the family, or losing their job. "
              "restructuring: they explicitly ask for a new payment plan, lower or smaller instalments, or to restructure or spread the debt. Saying the amount is too much is NOT restructuring. "
              "wants_human: they explicitly ask to be transferred to, or to speak with, a human person or agent. Asking whether you are a robot, a machine or an AI is NOT wants_human, it is none. "
              "none: anything else, including dates, amounts, yes, no, or saying it is too much.")

nodes["negotiate"] = {
    "name": "negotiate", "node_type": "conversation",
    "on_enter": {"type": "tts", "text": "Thank you, {{title}} {{last_name}}, you are verified. I am calling from the collections team about a late payment on your {{product_words}}. The past-due amount is {{past_due_text}} and it is {{days_late}} days late. Can you make this payment today?"},
    "on_enter_first_entry_only": True,
    "task_message": NEGOTIATE_TASK,
    "tools": [promise_tool],
    "extraction_schema": [{"name": "topic", "type": "string",
                           "enum_values": ["none", "already_paid", "dispute", "hardship", "restructuring", "wants_human"],
                           "description": TOPIC_DESC, "min_confidence": 0.7}],
    "max_node_turns": 30, "max_turn_transition": "close_refused",
    "transitions": [
        tr("close_promise", "Promise recorded", cond_any(*is_true("create_promise_response.data.ok"))),
        tr("close_service_error", "Promise failed twice", cond_any(c("promise_service_fails", "greater_than", 1))),
        tr("close_paid", "Already paid", cond_any(c("topic", "equals", "already_paid"))),
        tr("close_dispute", "Dispute", cond_any(c("topic", "equals", "dispute"))),
        tr("hardship", "Hardship", cond_any(c("topic", "equals", "hardship"))),
        tr("transfer_restructuring", "Restructuring", cond_any(c("topic", "equals", "restructuring"))),
        tr("human_check", "Wants a human", cond_any(c("topic", "equals", "wants_human"))),
        tool_tr("close_refused", "The allowed persuasion attempts are used up and the customer still refuses, or the customer said twice they do not want to talk"),
        tool_tr("close_service_error", "create_promise failed twice and nothing was recorded"),
    ],
}

nodes["hardship"] = {
    "name": "hardship", "node_type": "conversation",
    "on_enter": {"type": "tts", "text": "I am sorry to hear that. I can connect you with our customer support team. Would you like that?"},
    "on_enter_first_entry_only": True,
    "task_message": HARDSHIP_TASK,
    "extraction_schema": [{"name": "support_choice", "type": "string", "enum_values": ["accept", "decline", "offers_payment"],
                           "description": "From the customer's LAST message: accept if they want to be connected to the support team; decline if they do not want to be connected or transferred; offers_payment if they name a payment date or amount themselves. Empty if none of these.",
                           "min_confidence": 0.6}],
    "max_node_turns": 4, "max_turn_transition": "close_hardship",
    "transitions": [
        tr("transfer_support", "Accepts support", cond_any(c("support_choice", "equals", "accept"))),
        tr("close_hardship", "Declines support", cond_any(c("support_choice", "equals", "decline"))),
        tr("reset_topic", "Offers a payment", cond_any(c("support_choice", "equals", "offers_payment"))),
        tr("hardship", "stay", ALWAYS),
    ],
}

nodes["human_check"] = {
    "name": "human_check", "node_type": "conversation",
    "on_enter": {"type": "llm", "prompt": "The customer asked for a human. If they also asked whether you are a robot or an AI, say you are Freya Bank's digital assistant. Then ask once, briefly, whether you can help them with this payment yourself first. One or two short sentences. No tool call."},
    "task_message": HUMAN_TASK,
    "extraction_schema": [{"name": "human_insist", "type": "string", "enum_values": ["insist", "continue"],
                           "description": "From the customer's LAST message: insist if they still want a human or a person; continue if they agree to continue with you.",
                           "min_confidence": 0.6}],
    "max_node_turns": 3, "max_turn_transition": "transfer_human",
    "transitions": [
        tr("transfer_human", "Insists", cond_any(c("human_insist", "equals", "insist"))),
        tr("reset_topic", "Continue", cond_any(c("human_insist", "equals", "continue"))),
        tr("human_check", "stay", ALWAYS),
    ],
}
nodes["reset_topic"] = code_node("reset_topic", RESET_TOPIC, "negotiate")

cb_tool = api_fn("schedule_callback", "schedule_callback", {
    "date": {"type": "string", "default": "{{date}}", "description": "Callback date as YYYY-MM-DD."},
    "time": {"type": "string", "default": "{{time}}", "description": "Callback time as HH:MM, 24 hour."},
}, ["date", "time"], "Book a callback. CALL it as soon as the person names a day and time inside the allowed window.")
cb_tool["pre_process_code"] = CB_PRE
cb_tool["post_process_code"] = CB_POST

nodes["third_party"] = {
    "name": "third_party", "node_type": "conversation",
    "on_enter": {"type": "tts", "text": "Thank you. I can only discuss this with {{title}} {{last_name}} personally. When would be a good time to reach them?"},
    "on_enter_first_entry_only": True,
    "task_message": THIRD_TASK,
    "tools": [cb_tool],
    "extraction_schema": [{"name": "tp_status", "type": "string", "enum_values": ["wrong_number", "deceased", "none"],
                           "description": "From the person's LAST message: wrong_number if they say this number does not belong to the customer or they do not know the customer; deceased if they say the customer has died; none otherwise.",
                           "min_confidence": 0.7}],
    "max_node_turns": 8, "max_turn_transition": "close_no_callback",
    "transitions": [
        tr("close_callback", "Callback booked", cond_any(*is_true("schedule_callback_response.data.ok"))),
        tr("close_deceased", "Deceased", cond_any(c("tp_status", "equals", "deceased"))),
        tr("close_wrong_number", "Wrong number", cond_any(c("tp_status", "equals", "wrong_number"))),
        tool_tr("close_no_callback", "Two offered times were outside the window, or no time can be given"),
    ],
}

BYE = " Thank you for your time. Goodbye."
nodes["close_unclear"] = close_node("close_unclear", "I am sorry, I could not confirm who I am speaking with. I will try again another time." + BYE, "not_reached", "unclear")
nodes["close_wrong_number"] = close_node("close_wrong_number", "I am sorry for the trouble." + BYE, "not_reached", "wrong_number")
nodes["close_deceased"] = close_node("close_deceased", "I am sorry for your loss. Thank you, goodbye.", "not_reached", "deceased")
nodes["close_callback"] = close_node("close_callback", "Thank you, we will call back then." + BYE, "not_reached", "callback")
nodes["close_no_callback"] = close_node("close_no_callback", "I understand, we will try again another time." + BYE, "not_reached", "no_callback")
nodes["close_id_failed"] = close_node("close_id_failed", "I am sorry, I could not verify your identity, so I cannot continue. Please use the Freya Bank mobile app or visit a branch." + BYE, "not_verified", "failed")
nodes["close_declined"] = close_node("close_declined", "I understand. Without the security questions I cannot continue." + BYE, "not_verified", "declined")
nodes["close_promise"] = close_node("close_promise", "Thank you, your payment promise is recorded. You will receive a text message confirming this promise." + BYE, "promise", "")
nodes["close_service_error"] = close_node("close_service_error", "I am sorry, I could not record your promise because of a technical problem." + BYE, "no_promise", "service_error")
nodes["close_paid"] = close_node("close_paid", "Thank you for letting me know. Payments can take one business day to appear." + BYE, "paid", "")
nodes["close_dispute"] = close_node("close_dispute", "I understand, thank you for telling me." + BYE, "no_promise", "dispute")
nodes["close_refused"] = close_node("close_refused", "I understand." + BYE, "no_promise", "refused")
nodes["close_hardship"] = close_node("close_hardship", "I understand. Take care." + BYE, "no_promise", "hardship")
nodes["transfer_restructuring"] = close_node("transfer_restructuring", "I will transfer you to our restructuring team, who can discuss this with you." + BYE, "transferred", "restructuring", transfer_fn("restructuring"))
nodes["transfer_support"] = close_node("transfer_support", "I will connect you with our customer support team now." + BYE, "transferred", "support", transfer_fn("support"))
nodes["transfer_human"] = close_node("transfer_human", "I will transfer you to a colleague now." + BYE, "transferred", "human", transfer_fn("general"))

nodes["hang_up"] = {
    "name": "hang_up", "node_type": "end_call", "on_enter": {"type": "silent"},
    "disable_inactivity_monitor": True, "transitions": [],
    "terminal": {"type": "function", "function": {"name": "call_closed", "handler": "__function__:end_call",
                 "parameters": {"type": "object", "properties": {"message_mode": {"type": "string", "default": "none"}}}}},
}

for k, n in nodes.items():
    assert n["name"] == k

fallback = api_fn("close_call_fallback", "close_call", {
    "outcome": {"type": "string", "enum": ["promise", "paid", "transferred", "no_promise", "not_verified", "not_reached"],
                "default": "{{fallback_outcome}}",
                "description": "promise: a promise was recorded. paid: customer said they already paid. transferred: handed to a person. no_promise: verified customer, no promise. not_verified: identity check not passed. not_reached: the customer was not on the line."},
    "reason": {"type": "string", "default": "{{fallback_reason}}",
               "description": "transferred: restructuring, human or support. no_promise: refused, hardship, dispute or service_error. not_verified: failed or declined. not_reached: callback, no_callback, wrong_number, deceased or unclear. Empty for promise and paid."},
}, ["outcome"], "Record how the call ended, from the conversation.")
fallback["config"]["alwaysRunsAt"] = ["end_of_call"]
fallback["pre_process_code"] = FALLBACK_PRE

wf = {
    "initial_node": "setup",
    "system_prompt": SYSTEM,
    "nodes": nodes,
    "global_nodes": {},
    "tools": [fallback],
    "test_variables": {"customer_id": "c_test", "first_name": "Daniel", "last_name": "Reyes", "title": "Mr", "now": "2026-10-14T11:00"},
}
assert "{{" not in SETUP_CODE + ACCOUNT_CODE + RESET_TOPIC
json.dump(wf, open(OUT, "w"), indent=1)
print("wrote", OUT, len(nodes), "nodes")
