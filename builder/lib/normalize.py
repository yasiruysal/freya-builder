"""Answer normalisation for pre_process_code hooks. The weak agent model often passes the customer's words unchanged
("June third nineteen eighty seven", "four four one zero", "four hundred and fifty five"), so the hook must convert.

Copy the body of SNIPPET into a pre_process_code (it only uses re and datetime, has no {{ or }}, never raises), then call:
    to_iso_date(text, day_first=True) -> 'YYYY-MM-DD' or ''      to_digits(text) -> '4410'      to_number(text) -> 455.0 or None
"""

SNIPPET = r'''
import re, datetime
_UNITS = {'zero': 0, 'oh': 0, 'o': 0, 'one': 1, 'two': 2, 'three': 3, 'four': 4, 'five': 5, 'six': 6, 'seven': 7, 'eight': 8, 'nine': 9,
          'ten': 10, 'eleven': 11, 'twelve': 12, 'thirteen': 13, 'fourteen': 14, 'fifteen': 15, 'sixteen': 16, 'seventeen': 17,
          'eighteen': 18, 'nineteen': 19}
_TENS = {'twenty': 20, 'thirty': 30, 'forty': 40, 'fifty': 50, 'sixty': 60, 'seventy': 70, 'eighty': 80, 'ninety': 90}
_ORD = {'first': 1, 'second': 2, 'third': 3, 'fourth': 4, 'fifth': 5, 'sixth': 6, 'seventh': 7, 'eighth': 8, 'ninth': 9, 'tenth': 10,
        'eleventh': 11, 'twelfth': 12, 'thirteenth': 13, 'fourteenth': 14, 'fifteenth': 15, 'sixteenth': 16, 'seventeenth': 17,
        'eighteenth': 18, 'nineteenth': 19, 'twentieth': 20, 'thirtieth': 30}
_MON = ['jan', 'feb', 'mar', 'apr', 'may', 'jun', 'jul', 'aug', 'sep', 'oct', 'nov', 'dec']

def _tokens(text):
    t = str(text or '').lower().replace('-', ' ').replace(',', ' ')
    t = re.sub(r'(\d)(st|nd|rd|th)\b', r'\1', t)
    return [w for w in re.split(r'[\s/.]+', t) if w and w not in ('the', 'of', 'and', 'a')]

def _small(ws, i):
    """A number 0-99 written as words or ordinals starting at ws[i]. Returns (value, next index) or (None, i)."""
    w = ws[i] if i < len(ws) else ''
    if w in ('oh', 'o', 'zero') and i + 1 < len(ws) and _UNITS.get(ws[i + 1], 99) < 10:   # "oh five" = 05
        return _UNITS[ws[i + 1]], i + 2
    if w in _TENS:
        v = _TENS[w]; j = i + 1
        if j < len(ws) and (ws[j] in _UNITS or ws[j] in _ORD) and (_UNITS.get(ws[j], _ORD.get(ws[j], 99)) < 10):
            v += _UNITS.get(ws[j], _ORD.get(ws[j])); j += 1
        return v, j
    for tbl in (_UNITS, _ORD):
        if w in tbl: return tbl[w], i + 1
    return None, i

def to_number(text):
    """'four hundred and fifty five' -> 455.0, '1,250.50' -> 1250.5, 'two thousand one hundred' -> 2100.0. None if unreadable."""
    s = str(text or '').lower()
    m = re.search(r'\d[\d,]*(\.\d+)?', s)
    if m: return float(m.group(0).replace(',', ''))
    ws = _tokens(s); total = 0; cur = 0; seen = False; i = 0
    while i < len(ws):
        w = ws[i]
        if w in ('hundred',): cur = max(cur, 1) * 100; i += 1; seen = True; continue
        if w in ('thousand',): total += max(cur, 1) * 1000; cur = 0; i += 1; seen = True; continue
        v, j = _small(ws, i)
        if v is None: i += 1; continue
        cur += v; i = j; seen = True
    return float(total + cur) if seen else None

def to_digits(text):
    """'four four one zero' / '44 10' / 'forty four ten' / 'twenty two' -> digits; 'double four' -> 44."""
    out = ''; ws = _tokens(text); i = 0
    while i < len(ws):
        w = ws[i]
        if w.isdigit(): out += w; i += 1; continue
        if w == 'double' and i + 1 < len(ws):
            v, j = _small(ws, i + 1)
            if v is not None and v < 10: out += str(v) * 2; i = j; continue
        if w in ('oh', 'o', 'zero') : out += '0'; i += 1; continue
        if w in _UNITS and _UNITS[w] < 10: out += str(_UNITS[w]); i += 1; continue
        v, j = _small(ws, i)
        if v is not None: out += str(v); i = j; continue
        i += 1
    return out

def _year(ws, i):
    """'1987', 'nineteen eighty seven', 'two thousand and three', 'nineteen oh five'."""
    if i < len(ws) and re.fullmatch(r'\d{4}', ws[i]): return int(ws[i]), i + 1
    a, j = _small(ws, i)
    if a is None: return None, i
    if i < len(ws) and ws[i] == 'two' and j < len(ws) and ws[j] == 'thousand':
        b, k = _small(ws, j + 1)
        return 2000 + (b or 0), (k if b is not None else j + 1)
    b, k = _small(ws, j)
    if a in (19, 20) and b is not None: return a * 100 + b, k
    return None, i

def to_iso_date(text, day_first=True):
    """Any spoken or written date -> 'YYYY-MM-DD', or '' when it cannot be read."""
    s = str(text or '').strip()
    if re.fullmatch(r'\d{4}-\d{2}-\d{2}', s): return s
    m = re.fullmatch(r'(\d{1,2})[./\- ](\d{1,2})[./\- ](\d{4})', s)
    if m:
        d, mo = (int(m.group(1)), int(m.group(2))) if day_first else (int(m.group(2)), int(m.group(1)))
        try: return datetime.date(int(m.group(3)), mo, d).isoformat()
        except Exception: return ''
    ws = _tokens(s); mon = 0; mi = -1
    for i, w in enumerate(ws):
        for k, n in enumerate(_MON):
            if w.startswith(n): mon, mi = k + 1, i; break
        if mon: break
    nums = []                      # (value, kind) for each number group outside the month word
    i = 0
    while i < len(ws):
        if i == mi: i += 1; continue
        y, j = _year(ws, i)
        if y and 1900 <= y <= 2100: nums.append((y, 'year')); i = j; continue
        if ws[i].isdigit(): nums.append((int(ws[i]), 'num')); i += 1; continue
        v, j = _small(ws, i)
        if v is not None: nums.append((v, 'num')); i = j; continue
        i += 1
    years = [v for v, k in nums if k == 'year']; small = [v for v, k in nums if k == 'num']
    if not years: return ''
    if mon and small: d = small[0]
    elif not mon and len(small) >= 2: d, mon = (small[0], small[1]) if day_first else (small[1], small[0])
    else: return ''
    try: return datetime.date(years[0], mon, d).isoformat()
    except Exception: return ''
_WD = ['monday', 'tuesday', 'wednesday', 'thursday', 'friday', 'saturday', 'sunday']

def resolve_day(text, now_iso):
    """Resolve what the customer said about a day against the call's `now` (never the real clock).
    'today', 'tomorrow', 'day after tomorrow', 'this friday', 'next friday', 'friday', 'monday the 26th', 'the 16th',
    '16 October', 'October sixteenth', '2026-10-16' -> 'YYYY-MM-DD', or '' when no day is named.
    'next <weekday>' = that weekday in the following calendar week; 'this <weekday>' / a bare weekday = the current week
    (or the next one when that day has passed). Change these two lines if a brief defines them differently."""
    try: today = datetime.date.fromisoformat(str(now_iso)[:10])
    except Exception: return ''
    t = ' '.join(_tokens(text))
    if 'day after tomorrow' in t: return (today + datetime.timedelta(days=2)).isoformat()
    if 'tomorrow' in t: return (today + datetime.timedelta(days=1)).isoformat()
    if 'today' in t or 'tonight' in t or 'this evening' in t or 'this afternoon' in t: return today.isoformat()
    iso = to_iso_date(text)
    if iso: return iso
    ws = t.split()
    MONTHS = ['january', 'february', 'march', 'april', 'may', 'june', 'july', 'august', 'september', 'october', 'november', 'december']
    mon = next((k + 1 for w in ws for k, n in enumerate(MONTHS) if w == n or (len(w) >= 3 and w == n[:3] and w != 'may')), 0)
    day = None
    for i in range(len(ws)):
        if i > 0 and ws[i - 1] in ('at', 'around', 'about', 'after', 'before', 'by'): continue      # a time, not a day
        if i + 1 < len(ws) and ws[i + 1] in ('am', 'pm', "o'clock", 'oclock', 'thirty', 'fifteen', 'hundred'): continue
        v, j = _small(ws, i)
        if v is None and ws[i].isdigit() and len(ws[i]) <= 2: v = int(ws[i])
        if v is not None and 1 <= v <= 31: day = v; break
    for k, n in enumerate(_WD):
        if n in ws:
            monday = today - datetime.timedelta(days=today.weekday())
            d = monday + datetime.timedelta(days=k)
            if 'next' in ws: d = d + datetime.timedelta(days=7)
            elif d < today: d = d + datetime.timedelta(days=7)
            if day and d.day != day:            # 'monday the 26th': trust the date number
                for add in range(0, 62):
                    c = today + datetime.timedelta(days=add)
                    if c.day == day and (not mon or c.month == mon): return c.isoformat()
            return d.isoformat()
    if day:
        for add in range(0, 62):
            c = today + datetime.timedelta(days=add)
            if c.day == day and (not mon or c.month == mon): return c.isoformat()
    return ''
# ---- Turkish ----------------------------------------------------------------------------------------------------
_TR_N = {'sıfır': 0, 'bir': 1, 'iki': 2, 'üç': 3, 'dört': 4, 'dörd': 4, 'beş': 5, 'altı': 6, 'yedi': 7, 'sekiz': 8, 'dokuz': 9,
         'on': 10, 'yirmi': 20, 'otuz': 30, 'kırk': 40, 'elli': 50, 'altmış': 60, 'yetmiş': 70, 'seksen': 80, 'doksan': 90}
_TR_SUF = ['', 'u', 'ü', 'i', 'ı', 'nci', 'ncı', 'ncü', 'ncu', 'inci', 'ıncı', 'üncü', 'uncu', 'si', 'sı', 'sü', 'su', 'sinde', 'sında',
           'sünde', 'sunda', 'inde', 'ında', 'ünde', 'unda', 'nde', 'nda', 'de', 'da', 'te', 'ta', 'n', 'e', 'a', 'ye', 'ya', 'ine', 'ına', 'üne', 'una', 'sine', 'sına']
_TR_MON = ['ocak', 'şubat', 'mart', 'nisan', 'mayıs', 'haziran', 'temmuz', 'ağustos', 'eylül', 'ekim', 'kasım', 'aralık']
_TR_WD = [('pazartesi', 0), ('salı', 1), ('çarşamba', 2), ('perşembe', 3), ('cumartesi', 5), ('cuma', 4), ('pazar', 6)]

def _tr_lower(text):
    return str(text or '').replace('İ', 'i').replace('I', 'ı').lower()

def _tr_tokens(text):
    t = _tr_lower(text)
    t = re.sub(r"(\d)[’'`]\w+", r'\1', t)          # 14'ünde -> 14
    t = re.sub(r"([a-zçğıöşü])[’'`]\w+", r'\1', t)  # mart'ta -> mart
    t = t.replace('-', ' ').replace(',', ' , ')
    return [w for w in re.split(r'[\s/.]+', t) if w]

def _tr_num(w):
    """Value of one Turkish number word, with its suffix ('dördünde', 'yirmisi', 'onuncu'), or None."""
    if w in ('yüz', 'bin'): return None
    for stem in sorted(_TR_N, key=len, reverse=True):
        if w.startswith(stem) and w[len(stem):] in _TR_SUF: return _TR_N[stem]
    return None

def _is_tr(text):
    """Turkish if it has Turkish letters or a word only Turkish uses (not 'on', which is also English)."""
    t = _tr_lower(text)
    if re.search(r'[çğışöü]', t): return True
    words = (set(_TR_N) - {'on'}) | {'yüz', 'bin', 'lira', 'kuruş', 'tl', 'yarın', 'bugün', 'haftaya', 'gelecek', 'önümüzdeki', 'ayın'}
    stems = _TR_MON + [n for n, _ in _TR_WD]
    return any(w in words or any(w.startswith(m) for m in stems) for w in _tr_tokens(t))

def _tr_value(ws):
    """Turkish number words -> integer: 'bin dokuz yüz seksen sekiz' -> 1988, 'dört bin iki yüz elli' -> 4250."""
    total, cur, seen = 0, 0, False
    for w in ws:
        if w == 'yüz' or (w.startswith('yüz') and w[3:] in _TR_SUF): cur = (cur or 1) * 100; seen = True
        elif w == 'bin' or (w.startswith('bin') and w[3:] in _TR_SUF): total += (cur or 1) * 1000; cur = 0; seen = True
        else:
            v = _tr_num(w)
            if v is None: continue
            cur += v; seen = True
    return total + cur if seen else None

def tr_number(text):
    s = _tr_lower(text)
    m = re.search(r'\d{1,3}(?:\.\d{3})+(?:,\d+)?|\d+,\d+|\d+(?:\.\d+)?', s)
    if m:
        x = m.group(0)
        if ',' in x or re.fullmatch(r'\d{1,3}(?:\.\d{3})+', x): x = x.replace('.', '').replace(',', '.')
        return float(x)
    ws = _tr_tokens(s)
    if 'lira' in ws:
        i = ws.index('lira'); lira = _tr_value(ws[:i]) or 0
        rest = ws[i + 1:]; kurus = _tr_value(rest[:rest.index('kuruş')] if 'kuruş' in rest else rest) or 0
        return float(lira) + kurus / 100.0
    v = _tr_value(ws)
    return float(v) if v is not None else None

def tr_digits(text):
    """'dört dört yedi bir' -> 4471; 'kırk dört on' -> 4410 (a tens word joins the unit after it)."""
    vals = []
    for w in _tr_tokens(text):
        if w.isdigit(): vals.append(w); continue
        v = _tr_num(w)
        if v is None: continue
        if vals and isinstance(vals[-1], int) and vals[-1] >= 20 and vals[-1] % 10 == 0 and v < 10: vals[-1] += v
        else: vals.append(v)
    return ''.join(str(v) for v in vals)

def tr_date(text):
    s = str(text or '').strip()
    m = re.fullmatch(r'(\d{1,2})[./\- ](\d{1,2})[./\- ](\d{4})', s)
    if m:
        try: return datetime.date(int(m.group(3)), int(m.group(2)), int(m.group(1))).isoformat()
        except Exception: return ''
    ws = _tr_tokens(s); mon, mi = 0, -1
    for i, w in enumerate(ws):
        for k, n in enumerate(_TR_MON):
            if w.startswith(n): mon, mi = k + 1, i; break
        if mon: break
    if not mon: return ''
    before, after = ws[:mi], ws[mi + 1:]
    day = None
    for w in before:
        if w.isdigit() and 1 <= int(w) <= 31: day = int(w)
    if day is None:
        d = _tr_value(before); day = d if d and 1 <= d <= 31 else None
    yr = None
    for w in after:
        if re.fullmatch(r'\d{4}', w): yr = int(w)
    if yr is None:
        yv = _tr_value(after); yr = yv if yv and 1900 <= yv <= 2100 else None
    if not (day and yr): return ''
    try: return datetime.date(yr, mon, day).isoformat()
    except Exception: return ''

def tr_resolve_day(text, now_iso):
    try: today = datetime.date.fromisoformat(str(now_iso)[:10])
    except Exception: return ''
    t = ' '.join(_tr_tokens(text))
    if 'öbür gün' in t or 'yarından sonra' in t: return (today + datetime.timedelta(days=2)).isoformat()
    if re.search(r'\byarın', t): return (today + datetime.timedelta(days=1)).isoformat()
    if re.search(r'\bbugün|\bbu akşam|\bbu öğle', t): return today.isoformat()
    d = tr_date(text)
    if d: return d
    ws = t.split()
    for i, w in enumerate(ws):
        for name, k in _TR_WD:
            if w.startswith(name):
                monday = today - datetime.timedelta(days=today.weekday()); day = monday + datetime.timedelta(days=k)
                prev = ws[i - 1] if i else ''
                if prev in ('gelecek', 'haftaya', 'önümüzdeki', 'sonraki') or 'haftaya' in ws or 'gelecek hafta' in t: day += datetime.timedelta(days=7)
                elif day < today: day += datetime.timedelta(days=7)
                return day.isoformat()
    mon = 0
    for w in ws:
        for k, n in enumerate(_TR_MON):
            if w.startswith(n): mon = k + 1
    for i, w in enumerate(ws):
        v = int(w) if w.isdigit() else _tr_num(w)
        if w.isdigit() is False and v is not None and i + 1 < len(ws) and _tr_num(ws[i + 1]) is not None and _tr_num(ws[i + 1]) < 10 and v >= 10:
            v += _tr_num(ws[i + 1])
        if v is not None and 1 <= v <= 31:
            for add in range(0, 62):
                c = today + datetime.timedelta(days=add)
                if c.day == v and (not mon or c.month == mon): return c.isoformat()
    return ''

_en_number, _en_digits, _en_date, _en_resolve = to_number, to_digits, to_iso_date, resolve_day

def to_number(text):
    if _is_tr(text) or re.search(r'\d{1,3}(?:\.\d{3})+(?:,\d+)?|\b\d+,\d{1,2}\b', str(text or '')): return tr_number(text)   # 1.250,00 is Turkish format
    return _en_number(text)

def to_digits(text):
    return tr_digits(text) if _is_tr(text) and not re.fullmatch(r'[\d\s]+', str(text or '')) else _en_digits(text)

def to_iso_date(text, day_first=True):
    if _is_tr(text):
        d = tr_date(text)
        if d: return d
    return _en_date(text, day_first)

def resolve_day(text, now_iso):
    if _is_tr(text):
        d = tr_resolve_day(text, now_iso)
        if d: return d
    return _en_resolve(text, now_iso)


def to_time(text):
    """Spoken or written clock time -> 'HH:MM' or ''. English and Turkish: '17:15', '5:15 pm', 'quarter past five in the
    afternoon', 'half past twelve', 'ten am', 'sabah on', 'öğleden sonra üçte', 'on iki buçuk', 'beşi çeyrek geçe', 'akşam sekiz'."""
    t = _tr_lower(text)
    m = re.search(r'\b(\d{1,2})[:.](\d{2})\s*(am|pm|a\.m\.|p\.m\.)?', t)
    h = mi = None; pm = None
    if m:
        h, mi = int(m.group(1)), int(m.group(2)); pm = (m.group(3) or '')[:1] or None
    else:
        ws = _tr_tokens(t)
        trk = _is_tr(t) or bool(re.search(r'\b(sabah|saat|akşam|öğle|gece|buçuk|çeyrek)', t))
        nums = []
        for w in ws:
            if w.isdigit(): nums.append(int(w))
            else:
                v = _tr_num(w) if trk else None
                if v is None and w in _UNITS: v = _UNITS[w]
                if v is None and w in _TENS: v = _TENS[w]
                if v is not None:
                    if nums and nums[-1] in (10, 20) and v < 10 and not (trk and nums[-1] == 10 and v == 0): nums[-1] += v
                    else: nums.append(v)
        if not nums: return ''
        mi = 0
        if 'buçuk' in t or 'half past' in t: mi = 30
        if 'çeyrek' in t:
            mi = 45 if ('kala' in t or 'var' in ws) else 15
        if 'quarter past' in t: mi = 15
        if 'quarter to' in t: mi = 45
        h = nums[0] if not ('quarter to' in t or (('çeyrek' in t) and ('kala' in t or 'var' in ws))) else nums[0] - 1
        if len(nums) > 1 and mi == 0 and nums[1] < 60: mi = nums[1]
        if re.search(r'\b(pm|evening|afternoon|night|tonight)\b', t) or re.search(r'akşam|öğleden sonra|gece|öğlen', t): pm = 'p'
        if re.search(r'\b(am|morning)\b', t) or 'sabah' in t: pm = 'a'
    if h is None or not (0 <= h <= 24) or not (0 <= (mi or 0) < 60): return ''
    if pm == 'p' and h < 12: h += 12
    if pm == 'a' and h == 12: h = 0
    return '%02d:%02d' % (h % 24, mi or 0)

'''

if __name__ == "__main__":
    ns = {}; exec(SNIPPET, ns)
    D, G, N, R = ns["to_iso_date"], ns["to_digits"], ns["to_number"], ns["resolve_day"]; T = ns["to_time"]
    cases = [(D, "June third nineteen eighty seven", "1987-06-03"), (D, "The twenty-eighth of April nineteen ninety-three", "1993-04-28"),
             (D, "14 March 1988", "1988-03-14"), (D, "1988-03-14", "1988-03-14"), (D, "05/12/1975", "1975-12-05"),
             (D, "twelve, oh five, nineteen seventy-five", "1975-05-12"), (D, "5 December 1975", "1975-12-05"),
             (D, "the 3rd of June 1987", "1987-06-03"), (D, "March 14th, 1988", "1988-03-14"), (D, "second of January two thousand and three", "2003-01-02"),
             (D, "nineteenth of November nineteen seventy-nine", "1979-11-19"), (D, "September ninth nineteen ninety", "1990-09-09"),
             (D, "the twenty first of July nineteen ninety two", "1992-07-21"), (D, "nonsense", ""),
             (G, "four four one zero", "4410"), (G, "44 10", "4410"), (G, "it's 4471", "4471"), (G, "double four one zero", "4410"), (G, "oh three five eight", "0358"),
             (lambda x: R(x, "2026-10-14T11:00"), "tomorrow morning around eleven", "2026-10-15"),
             (lambda x: R(x, "2026-10-14T11:00"), "this Friday", "2026-10-16"), (lambda x: R(x, "2026-10-14T11:00"), "next Friday", "2026-10-23"),
             (lambda x: R(x, "2026-10-14T11:00"), "Monday the 26th", "2026-10-26"), (lambda x: R(x, "2026-10-14T11:00"), "the thirtieth of October", "2026-10-30"),
             (lambda x: R(x, "2026-10-14T11:00"), "on the 16th", "2026-10-16"), (lambda x: R(x, "2026-10-14T11:00"), "Saturday", "2026-10-17"),
             (lambda x: R(x, "2026-10-14T11:00"), "today", "2026-10-14"), (lambda x: R(x, "2026-10-15T10:30"), "Monday at eleven", "2026-10-19"),
             (lambda x: R(x, "2026-10-14T11:00"), "the week after next", ""),
             (N, "four hundred and fifty five", 455.0),
             (G, "twenty two zero one", "2201"), (G, "forty four ten", "4410"),
             (T, "17:15", "17:15"), (T, "5:15 pm", "17:15"), (T, "quarter past five in the afternoon", "17:15"), (T, "half past twelve", "12:30"),
             (T, "ten am", "10:00"), (T, "at eight in the evening", "20:00"), (T, "sabah on", "10:00"), (T, "öğleden sonra üçte", "15:00"),
             (T, "on iki buçuk", "12:30"), (T, "akşam sekiz", "20:00"), (T, "saat on dört otuz", "14:30"), (T, "beşi çeyrek geçe akşam", "17:15"),
             (lambda x: ns["to_number"](x), "dört bin iki yüz elli lira", 4250.0), (lambda x: ns["to_number"](x), "1.250,00 TL", 1250.0),
             (lambda x: ns["to_number"](x), "iki yüz elli lira elli kuruş", 250.5), (lambda x: ns["to_number"](x), "yüz yirmi altı", 126.0),
             (lambda x: ns["to_digits"](x), "dört dört yedi bir", "4471"), (lambda x: ns["to_digits"](x), "kırk dört on", "4410"),
             (lambda x: ns["to_iso_date"](x), "on dört Mart bin dokuz yüz seksen sekiz", "1988-03-14"),
             (lambda x: ns["to_iso_date"](x), "3 Haziran 1987", "1987-06-03"), (lambda x: ns["to_iso_date"](x), "yirmi sekiz Nisan iki bin üç", "2003-04-28"),
             (lambda x: ns["resolve_day"](x, "2026-10-14T11:00"), "yarın sabah on birde", "2026-10-15"),
             (lambda x: ns["resolve_day"](x, "2026-10-14T11:00"), "bu cuma", "2026-10-16"), (lambda x: ns["resolve_day"](x, "2026-10-14T11:00"), "haftaya cuma", "2026-10-23"),
             (lambda x: ns["resolve_day"](x, "2026-10-14T11:00"), "ayın yirmisinde", "2026-10-20"), (lambda x: ns["resolve_day"](x, "2026-10-14T11:00"), "Cumartesi", "2026-10-17"),
             (lambda x: ns["resolve_day"](x, "2026-10-14T11:00"), "30 Ekim'de", "2026-10-30"), (lambda x: ns["resolve_day"](x, "2026-10-14T11:00"), "pazartesi", "2026-10-19"), (N, "1,250.50", 1250.5), (N, "two thousand one hundred", 2100.0), (N, "six hundred thirty seven fifty", None)]
    bad = 0
    for f, x, want in cases:
        got = f(x)
        if got != want and not (want is None and got is not None): print("MISMATCH", getattr(f, "__name__", "?"), repr(x), got, "want", want); bad += 1
    print(f"{len(cases) - bad}/{len(cases)} ok")
