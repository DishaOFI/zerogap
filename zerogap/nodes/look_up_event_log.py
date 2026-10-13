"""Look Up Applicant in Event Log — ZERO-CONTEXT agent (port of Look_Up_Applicant_in_Event_Log.js).

Without ARIS there is no data model that says which column identifies a person, so the agent does what a
context-free agent typically does: it searches the event log for the applicant's NAME and takes every matching
row with every column. (Deliberately naive – this is what the demo shows.)
Input: the rows returned by "Search Event Log by Name" (Google Sheets, filtered on first + last name) –
nothing is downloaded. For a local CSV extract use rows_from_csv() to produce those rows (streamed, never loaded whole).
"""
from __future__ import annotations
import math
import re
import unicodedata

CONFIG = {
    'SAMPLE_ROWS': 20,        # how many matched rows are kept in the output (the count is always complete)
    'LOG_ROWS': 18229,        # rows in the event-log sheet (shown as "rows scanned"); set to your sheet's row count
}


# ---- minimal JS semantics helpers ---------------------------------------------------------------------------
class _Undefined:
    __slots__ = ()

    def __repr__(self):
        return 'undefined'

    def __bool__(self):
        return False


UNDEF = _Undefined()
_WS = '\t\n\x0b\x0c\r \xa0  -     　﻿'
_TRIM = re.compile(f'^[{_WS}]+|[{_WS}]+\\Z')


def _truthy(v) -> bool:
    if v is None or v is False or v is UNDEF:
        return False
    if isinstance(v, (int, float)):
        return not (v == 0 or (isinstance(v, float) and math.isnan(v)))
    if isinstance(v, str):
        return v != ''
    return True


def _num_str(x) -> str:
    if isinstance(x, int):
        return str(x)
    if math.isnan(x):
        return 'NaN'
    if math.isinf(x):
        return 'Infinity' if x > 0 else '-Infinity'
    if x == 0:
        return '0'
    sign = '-' if x < 0 else ''
    m = re.fullmatch(r'(\d+)(?:\.(\d+))?(?:e([+-]\d+))?', repr(abs(x)))
    ip, fp, ex = m.group(1), m.group(2) or '', int(m.group(3) or 0)
    ds, n = ip + fp, len(ip) + ex
    stripped = ds.lstrip('0'); n -= len(ds) - len(stripped); ds = stripped.rstrip('0')
    k = len(ds)
    if k <= n <= 21:
        return sign + ds + '0' * (n - k)
    if 0 < n <= 21:
        return sign + ds[:n] + '.' + ds[n:]
    if -6 < n <= 0:
        return sign + '0.' + '0' * (-n) + ds
    e = n - 1
    return sign + (ds if k == 1 else ds[0] + '.' + ds[1:]) + 'e' + ('+' if e > 0 else '-') + str(abs(e))


def _s(v) -> str:
    """JS String(v) / template-literal conversion."""
    if v is UNDEF:
        return 'undefined'
    if v is None:
        return 'null'
    if v is True or v is False:
        return 'true' if v else 'false'
    if isinstance(v, (int, float)):
        return _num_str(v)
    if isinstance(v, str):
        return v
    if isinstance(v, list):
        return ','.join('' if (x is None or x is UNDEF) else _s(x) for x in v)
    if isinstance(v, dict):
        return '[object Object]'
    return str(v)


def _or(a, b):
    return a if _truthy(a) else b


def _rget(r: dict, k):
    """r[k] where k may be undefined (JS then reads the property named 'undefined')."""
    return r.get('undefined' if k is None else k, UNDEF)


def _strip(v):
    if isinstance(v, dict):
        return {k: _strip(x) for k, x in v.items() if x is not UNDEF}
    if isinstance(v, list):
        return [None if x is UNDEF else _strip(x) for x in v]
    if isinstance(v, float) and (math.isnan(v) or math.isinf(v)):
        return None
    return v


def _set_key(v):
    """SameValueZero key for a JS Set (1 and '1' differ, 1 and 1.0 are the same)."""
    if isinstance(v, bool):
        return ('b', v)
    if isinstance(v, (int, float)):
        return ('n', float(v))
    if isinstance(v, str):
        return ('s', v)
    if v is None or v is UNDEF:
        return ('u', repr(v))
    return ('o', id(v))


# ---- name / date normalisation (as in the JS node) -----------------------------------------------------------
def low(s) -> str:
    t = unicodedata.normalize('NFD', _s(_or(s, '')).lower())
    t = re.sub('[̀-ͯ]', '', t)
    return re.sub('[^a-z0-9]', '', t)


def digits(s) -> str:
    return re.sub('[^0-9]', '', _s(_or(s, '')))


def ymd(v) -> str:
    s = _s(_or(v, ''))
    m = re.search(r'([0-9]{4})-([0-9]{2})-([0-9]{2})', s)
    if m:
        return m.group(1) + m.group(2) + m.group(3)
    m = re.search(r'([0-9]{1,2})[./ ]([0-9]{1,2})[./ ]([0-9]{4})', s)
    if m:
        return m.group(3) + m.group(2).zfill(2) + m.group(1).zfill(2)
    m = re.search(r'^([0-9]{2})([0-9]{2})([0-9]{4})\Z', s)
    return m.group(3) + m.group(2) + m.group(1) if m else digits(s)


def _norm(h: str) -> str:
    return re.sub('[^a-zA-Z]', '', h).lower()


P = '(applicant|insured|insuredperson|antragsteller|versicherter|customer)?'
RE_FIRST = re.compile(f'^{P}(firstname|vorname|givenname)\\Z')
RE_LAST = re.compile(f'^{P}(lastname|nachname|surname|familyname)\\Z')
RE_FULL = re.compile(f'^{P}(name|fullname)\\Z')


def _find(head, rx):
    return next((h for h in head if rx.search(_norm(h))), None)


# ---- JS Date helpers for the "most recent first" ordering ------------------------------------------------------
def _days_from_civil(y: int, m: int, d: int) -> int:
    y -= m <= 2
    era = (y if y >= 0 else y - 399) // 400
    yoe = y - era * 400
    doy = (153 * (m + (-3 if m > 2 else 9)) + 2) // 5 + d - 1
    doe = yoe * 365 + yoe // 4 - yoe // 100 + doy
    return era * 146097 + doe - 719468


def _date_utc(y: int, mon: int, d: int, h: int = 0, mi: int = 0) -> float:
    """Date.UTC(y, mon, d, h, mi) – month is 0-based and overflows like JS; years 0–99 mean 1900–1999."""
    if 0 <= y <= 99:
        y += 1900
    y += mon // 12
    mon %= 12
    t = (_days_from_civil(y, mon + 1, 1) + d - 1) * 86400000 + h * 3600000 + mi * 60000
    return float(t) if abs(t) <= 8.64e15 else math.nan


_ISO = re.compile(r'^([+-][0-9]{6}|[0-9]{4})(?:-([0-9]{2})(?:-([0-9]{2}))?)?'
                  r'(?:[T ]([0-9]{2}):([0-9]{2})(?::([0-9]{2})(?:\.([0-9]{1,9}))?)?(Z|[+-][0-9]{2}:?[0-9]{2})?)?\Z', re.I)


def _date_parse(v: str) -> float:
    """Subset of Date.parse: ISO 8601 date / date-time. Date-time without an offset is read as UTC
    (V8 uses the server's local time zone there; n8n normally runs in UTC). Anything else -> NaN."""
    m = _ISO.match(_TRIM.sub('', v))
    if not m:
        return math.nan
    y = int(m.group(1)); mo = int(m.group(2) or 1); d = int(m.group(3) or 1)
    hh = int(m.group(4) or 0); mi = int(m.group(5) or 0); ss = int(m.group(6) or 0)
    ms = int((m.group(7) or '0')[:3].ljust(3, '0'))
    if not (1 <= mo <= 12 and 1 <= d <= 31 and hh <= 24 and mi < 60 and ss < 60):
        return math.nan
    t = (_days_from_civil(y, mo, d) * 86400 + hh * 3600 + mi * 60 + ss) * 1000 + ms
    tz = m.group(8)
    if tz and tz.upper() != 'Z':
        sign = 1 if tz[0] == '+' else -1
        tzd = tz[1:].replace(':', '')
        t -= sign * (int(tzd[:2]) * 60 + int(tzd[2:])) * 60000
    return float(t)


# ---- CSV source (replaces the n8n binary fallback) -------------------------------------------------------------
class CsvRows(list):
    """The matching rows of a CSV file plus its header (`columns`), so the lookup still reports the event-log
    columns when nobody matches – as the JS CSV branch did (it read the header from the file)."""
    columns: list[str] = []

def _split_line(line: str, sep: str) -> list[str]:
    out, cell, q, i = [], '', False, 0
    while i < len(line):
        ch = line[i]
        if q:
            if ch == '"':
                if i + 1 < len(line) and line[i + 1] == '"':
                    cell += '"'; i += 1
                else:
                    q = False
            else:
                cell += ch
        elif ch == '"':
            q = True
        elif ch == sep:
            out.append(_TRIM.sub('', cell)); cell = ''
        elif ch != '\r':
            cell += ch
        i += 1
    out.append(_TRIM.sub('', cell))
    return out


def rows_from_csv(path, first_name, last_name) -> list[dict]:
    """Streams an event-log CSV and returns only the rows whose first + last name columns match the applicant
    (last name only when the first name is empty) – the same pre-filter the Google Sheets search applies.
    Lines are split on '\\n' like the JS CSV fallback; the separator (';' or ',') is taken from the header line."""
    want_first, want_last = low(first_name), low(last_name)
    raw_last = _TRIM.sub('', _s(_or(last_name, '')))
    rows = CsvRows()
    head = sep = None
    i_first = i_last = i_full = None
    with open(path, 'rb') as fh:
        for raw in fh:
            line = raw.decode('utf-8', 'replace')
            if line.endswith('\n'):
                line = line[:-1]
            if head is None:
                line = line.lstrip('﻿') if line.startswith('﻿') else line
            if not _TRIM.sub('', line):
                continue
            if head is None:
                sep = ';' if line.count(';') > line.count(',') else ','
                head = _split_line(line, sep)
                f, la, fu = _find(head, RE_FIRST), _find(head, RE_LAST), _find(head, RE_FULL)
                i_first = head.index(f) if f is not None else None
                i_last = head.index(la) if la is not None else None
                i_full = head.index(fu) if fu is not None else None
                rows.columns = list(head)
                if not want_last:
                    break
                continue
            if raw_last and raw_last not in line and want_last not in low(line):
                continue   # cheap pre-check before splitting the line
            c = _split_line(line, sep)
            cell = lambda k: c[k] if k is not None and k < len(c) else ''  # noqa: E731
            if i_last is not None:
                ok = low(cell(i_last)) == want_last and (not want_first or i_first is None or low(cell(i_first)) == want_first)
            elif i_full is not None:
                n = low(cell(i_full))
                ok = (n in (want_first + want_last, want_last + want_first)) if want_first else n.endswith(want_last)
            else:
                ok = True   # no name columns recognised – keep every line that contains the last name
            if ok:
                rows.append({h: (c[k] if k < len(c) else '') for k, h in enumerate(head)})
    return rows


# ---- node ------------------------------------------------------------------------------------------------------
_RECEIV = re.compile(r'receiv', re.I)
_TSCOL = re.compile(r'created|timestamp|antragsdatum', re.I)
_ID = re.compile(r'insured_?person_?id|member|versicherten|insurance_?n|kv_?nr', re.I)
_DOB = re.compile(r'birth|geburt|dob', re.I)
_TS_DMY = re.compile(r'([0-9]{1,2})[./]([0-9]{1,2})[./]([0-9]{4})(?:[^0-9]+([0-9]{1,2}):([0-9]{2}))?')


def run(ctx, items: list[dict]) -> list[dict]:
    C = {**CONFIG, **((getattr(ctx, 'config', None) or {}).get('look_up_event_log', {}) or {})}
    cf = ctx.node('Build Case File').first()
    a = _or(cf.get('applicant', UNDEF), {})
    ag = lambda k: a.get(k, UNDEF) if isinstance(a, dict) else UNDEF  # noqa: E731

    # ---- collect the rows: Google Sheets rows (JSON items)
    source, rows_scanned = 'Google Sheet', C['LOG_ROWS']
    rows = []
    for it in items:
        r = dict(it or {})
        r.pop('row_number', None)
        if r:
            rows.append(r)

    # ---- name match (first + last name, no identifier check)
    # no rows: a Google-Sheets result gives no columns; a CsvRows result still knows the CSV header
    head = list(rows[0].keys()) if rows else list(getattr(items, 'columns', []) or [])
    first = _find(head, RE_FIRST)
    last = _find(head, RE_LAST)
    full = _find(head, RE_FULL)
    if full and not last:
        want = [low(f"{_s(ag('first_name'))}{_s(ag('last_name'))}"), low(f"{_s(ag('last_name'))}{_s(ag('first_name'))}")]
    else:
        want = [low(f"{_s(ag('first_name')) if first else ''}{_s(ag('last_name'))}")]

    def name_of(r):
        if full and not last:
            return low(_rget(r, full))
        return low(f"{_s(_rget(r, first)) if first else ''}{_s(_rget(r, last))}")
    matches = [r for r in rows if (lambda n: n and n in want)(name_of(r))]

    # ---- most recent case first – the agent simply takes the top hit
    ts_col = next((h for h in head if _RECEIV.search(h)), None) or next((h for h in head if _TSCOL.search(h)), None)

    def ts_of(r) -> float:
        v = _s(_or(r.get(ts_col, UNDEF), '')) if ts_col else ''
        if re.search(r'^[0-9]{10,}\Z', v):
            return float(v)
        m = _TS_DMY.search(v)
        if m:
            t = _date_utc(int(m.group(3)), int(m.group(2)) - 1, int(m.group(1)), int(m.group(4) or 0), int(m.group(5) or 0))
        else:
            t = _date_parse(v)
        return 0.0 if math.isnan(t) else t      # Date.parse(v) || 0  (Date.UTC never yields NaN here)
    matches.sort(key=lambda r: -ts_of(r))       # stable, like V8's Array.prototype.sort
    chosen = matches[0] if matches else None
    id_col = next((h for h in head if _ID.search(h)), None)
    dob_col = next((h for h in head if _DOB.search(h)), None)
    people = {(_s(_rget(r, id_col)) if id_col else '') + '|' + (ymd(_rget(r, dob_col)) if dob_col else '') for r in matches}
    same_dob = {_set_key(_rget(r, id_col)) for r in matches
                if dob_col and _truthy(ag('date_of_birth')) and ymd(_rget(r, dob_col)) == ymd(ag('date_of_birth'))
                and id_col and digits(_rget(r, id_col)) != digits(ag('insurance_number'))}

    name_lookup = {
        'method': 'name match in the event log (no identifier check)',
        'searched_for': ' '.join(_s(x) for x in [ag('first_name'), ag('last_name')] if _truthy(x)),
        'files': [source], 'rows_scanned': rows_scanned, 'columns': head,
        'matches_count': len(matches), 'distinct_people': len(people),
        'same_name_and_dob': len(same_dob),
        'matches': matches[:C['SAMPLE_ROWS']],      # most recent matching rows, every column
        'chosen': chosen,
        'chosen_id': _rget(chosen, id_col) if chosen and id_col else None,
        'chosen_dob': _rget(chosen, dob_col) if chosen and dob_col else None,
        'id_differs': bool(chosen and id_col and digits(_rget(chosen, id_col)) != digits(ag('insurance_number'))),
        'dob_differs': bool(chosen and dob_col and _truthy(ag('date_of_birth')) and ymd(_rget(chosen, dob_col)) != ymd(ag('date_of_birth'))),
    }
    return [_strip({**cf, 'name_lookup': name_lookup})]

