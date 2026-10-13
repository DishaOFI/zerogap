"""ARIS Case History (port of ARIS_Case_History.js).

Reads the ARIS answer and attaches the applicant's case history to the case file.
"""
from __future__ import annotations
import json
import math
import re


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


class JSError(Exception):
    pass


def _truthy(v) -> bool:
    if v is None or v is False or v is UNDEF:
        return False
    if isinstance(v, (int, float)):
        return not (v == 0 or (isinstance(v, float) and math.isnan(v)))
    if isinstance(v, str):
        return v != ''
    return True


def _nullish(v) -> bool:
    return v is None or v is UNDEF


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


def _js_str(v) -> str:
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
        return ','.join('' if _nullish(x) else _js_str(x) for x in v)
    if isinstance(v, dict):
        return '[object Object]'
    return str(v)


_NUM_RE = re.compile(r'[+-]?(?:[0-9]+\.?[0-9]*|\.[0-9]+)(?:[eE][+-]?[0-9]+)?')


def _js_number(v):
    """JS Number(v)."""
    if v is UNDEF:
        return math.nan
    if v is None:
        return 0
    if v is True or v is False:
        return int(v)
    if isinstance(v, (int, float)):
        return v
    if isinstance(v, list):
        return 0 if not v else (_js_number(_js_str(v)) if len(v) == 1 else math.nan)
    if isinstance(v, dict):
        return math.nan
    s = _TRIM.sub('', str(v))
    if s == '':
        return 0
    if _NUM_RE.fullmatch(s):
        f = float(s)
        return int(f) if f.is_integer() and abs(f) < 2 ** 53 else f
    m = re.fullmatch(r'0([xXoObB])([0-9a-fA-F]+)', s)
    if m:
        try:
            return int(m.group(2), {'x': 16, 'o': 8, 'b': 2}[m.group(1).lower()])
        except ValueError:
            return math.nan
    if s in ('Infinity', '+Infinity'):
        return math.inf
    if s == '-Infinity':
        return -math.inf
    return math.nan


def _u16_slice(s: str, a: int, b: int) -> str:
    """String.prototype.slice on UTF-16 code units."""
    if all(ord(ch) < 0x10000 for ch in s):
        return s[a:b]
    u = s.encode('utf-16-le', 'surrogatepass')
    return u[2 * a:2 * b].decode('utf-16-le', 'surrogatepass')


def _days_to_ymd(z: int):
    # civil_from_days (Howard Hinnant) – works for the full JS Date range
    z += 719468
    era = (z if z >= 0 else z - 146096) // 146097
    doe = z - era * 146097
    yoe = (doe - doe // 1460 + doe // 36524 - doe // 146096) // 365
    y = yoe + era * 400
    doy = doe - (365 * yoe + yoe // 4 - yoe // 100)
    mp = (5 * doy + 2) // 153
    d = doy - (153 * mp + 2) // 5 + 1
    m = mp + 3 if mp < 10 else mp - 9
    return y + (m <= 2), m, d


def _to_iso(ms) -> str:
    """new Date(ms).toISOString()."""
    if isinstance(ms, float):
        if math.isnan(ms) or math.isinf(ms):
            raise JSError('Invalid time value')
        ms = math.trunc(ms)
    if abs(ms) > 8.64e15:
        raise JSError('Invalid time value')
    days, rem = divmod(ms, 86400000)
    y, mo, d = _days_to_ymd(days)
    hh, rem = divmod(rem, 3600000)
    mi, rem = divmod(rem, 60000)
    ss, mss = divmod(rem, 1000)
    ys = f'{y:04d}' if 0 <= y <= 9999 else ('+' if y > 0 else '-') + f'{abs(y):06d}'
    return f'{ys}-{mo:02d}-{d:02d}T{hh:02d}:{mi:02d}:{ss:02d}.{mss:03d}Z'


def _strip(v):
    """JSON.stringify view: undefined keys vanish, undefined/NaN/Infinity in values become null."""
    if isinstance(v, dict):
        return {k: _strip(x) for k, x in v.items() if x is not UNDEF}
    if isinstance(v, list):
        return [None if x is UNDEF else _strip(x) for x in v]
    if isinstance(v, float) and (math.isnan(v) or math.isinf(v)):
        return None
    return v


def _get(o, k):
    if isinstance(o, dict):
        return o.get(k, UNDEF)
    if isinstance(o, list) and isinstance(k, int):
        return o[k] if 0 <= k < len(o) else UNDEF
    return UNDEF


def _read_prop(o, k):
    """o.k (non-optional) – throws like JS on null/undefined."""
    if _nullish(o):
        raise JSError(f"Cannot read properties of {_js_str(o)} (reading '{k}')")
    return _get(o, k)


# ---- node ------------------------------------------------------------------------------------------------------
_TS_MS = re.compile(r'^[0-9]{10,}\Z')
_TS_DMY = re.compile(r'^([0-9]{1,2})[./]([0-9]{1,2})[./]([0-9]{4})')
_CLOSED = re.compile(r'complet|closed|decided', re.I)


def _cell(v):
    if _truthy(v) and isinstance(v, (dict, list)):
        if isinstance(v, list):
            return _cell(v[0] if v else UNDEF)
        for k in ('value', 'v', 'text'):
            x = v.get(k, UNDEF)
            if not _nullish(x):
                return x
        return None
    return v


def _ts(v):
    if _nullish(v) or v == '':
        return None
    s = _js_str(v)
    if _TS_MS.search(s):
        return _to_iso(_js_number(s))[:10]
    m = _TS_DMY.search(s)
    return f'{m.group(3)}-{m.group(2).zfill(2)}-{m.group(1).zfill(2)}' if m else _u16_slice(s, 0, 10)


def _or0(v):
    return _js_number(v if _truthy(v) else 0)


def run(ctx, items: list[dict]) -> list[dict]:
    cf = ctx.node('Build Case File').first()
    q = ctx.node('Build ARIS History Query').first()
    history = {'checked': True, 'searched_for': q.get('searched_for', UNDEF), 'found': False, 'case_count': 0, 'error': None,
               'case': None, 'attributes': {}}
    try:
        if not items:
            raise JSError("Cannot read properties of undefined (reading 'json')")
        first = items[0]
        err = first.get('error', UNDEF)
        if _truthy(err):
            raise JSError(err if isinstance(err, str) else json.dumps(err, ensure_ascii=False, separators=(',', ':')))
        raw = _get(_get(_get(first, 'content'), 0), 'text')
        if isinstance(raw, str):
            try:
                raw = json.loads(raw)
            except ValueError as e:
                raise JSError(f'Unexpected token in JSON: {e}') from e
        hs = _read_prop(raw, 'headers')
        headers = [(h.get('value', UNDEF) if isinstance(h, dict) else UNDEF) for h in (hs if _truthy(hs) else [])]
        headers = [x if not _nullish(x) else h for x, h in zip(headers, hs if _truthy(hs) else [])]
        rows = _read_prop(raw, 'rows')
        rows = rows if _truthy(rows) else []
        row = _get(rows, 0)
        row = row if _truthy(row) else []
        o: dict = {}
        for i, h in enumerate(headers):
            o[h if isinstance(h, str) else _js_str(h)] = _cell(_get(row, i))
        g = lambda k: o.get(k, UNDEF)  # noqa: E731
        history['case_count'] = _or0(g('H_Case_Count'))
        history['found'] = isinstance(history['case_count'], (int, float)) and history['case_count'] > 0
        history['prior_missing_info_requests'] = _or0(g('H_Missing_Info_Requests'))
        history['namesakes'] = _or0(g('H_Namesakes'))
        if history['found']:
            history['case'] = {
                'application_id': g('Application_ID'), 'status': g('Case_Status'), 'type': g('Application_Type'),
                'channel': g('Application_Channel'),
                'received': _ts(g('Application_Received_Timestamp')), 'decision_sent': _ts(g('Decision_Sent_Timestamp')),
                'missing_info_flag': _or0(g('Missing_Information_Flag')), 'missing_info_count': _or0(g('Missing_Information_Count')),
                'existing_case_id': g('Existing_Case_ID') if _truthy(g('Existing_Case_ID')) else None,
                'track': g('Statutory_Track') if _truthy(g('Statutory_Track')) else None,
                'open': (not _truthy(g('Decision_Sent_Timestamp'))) and not _CLOSED.search(
                    _js_str(g('Case_Status') if _truthy(g('Case_Status')) else '')),
            }
            ac = q.get('attribute_columns')
            for c in (ac if _truthy(ac) else []):
                v = o.get(c if isinstance(c, str) else _js_str(c), UNDEF)
                if not _nullish(v) and v != '':
                    history['attributes'][c] = v
    except Exception as e:  # noqa: BLE001 – JS catch-all: every failure becomes history.error
        history['error'] = str(e)
    return [_strip({**cf, 'aris_history': history})]
