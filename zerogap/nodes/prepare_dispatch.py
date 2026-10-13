"""Prepare Dispatch (port of Prepare_Dispatch.js).

After approval: the caseworker chose the channel with the button (email / print); an escalation goes to the Case Manager.
The PDF binary of 'Create Letter PDF' is not part of the JSON output (binaries are handled outside the nodes).
"""
from __future__ import annotations
import math
import re


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
        return ','.join('' if (x is None or x is UNDEF) else _js_str(x) for x in v)
    if isinstance(v, dict):
        return '[object Object]'
    return str(v)


def _get(o, *keys):
    """Optional chaining o?.k1?.k2 …"""
    for k in keys:
        o = o.get(k, UNDEF) if isinstance(o, dict) else UNDEF
    return o


def _strip(v):
    if isinstance(v, dict):
        return {k: _strip(x) for k, x in v.items() if x is not UNDEF}
    if isinstance(v, list):
        return [None if x is UNDEF else _strip(x) for x in v]
    if isinstance(v, float) and (math.isnan(v) or math.isinf(v)):
        return None
    return v


def _or(a, b):
    return a if _truthy(a) else b


def _esc(s) -> str:
    return _js_str(_or(s, '')).replace('&', '&amp;').replace('<', '&lt;')


def run(ctx, items: list[dict]) -> list[dict]:
    c = ctx.node('Create Letter PDF').first()
    q = _or(_get(ctx.node('Wait for Caseworker Decision').first(), 'query'), {})
    disp = _get(c, 'dispatch')
    d = {**(disp if isinstance(disp, dict) and _truthy(disp) else {})}
    route = _get(c, 'route')
    if route == 'none':
        d['channel'] = 'none'
    elif route == 'escalation':
        d['channel'] = 'case_manager'
    elif _get(q, 'channel') == 'print' and _truthy(_get(d, 'options', 'print')):
        d['channel'] = 'print'
    elif _get(q, 'channel') == 'email' and _truthy(_get(d, 'options', 'email')):
        d['channel'] = 'email'
    is_esc = d.get('channel', UNDEF) == 'case_manager'
    app_id = _js_str(_or(_get(c, 'application_id'), ''))
    if is_esc:
        subject = f"Escalation – phone outreach today – {_js_str(_get(c, 'applicant_name'))} (case {app_id})"
    else:
        subject = _TRIM.sub('', f'Your application for long-term care insurance benefits – case {app_id}')
    body = _esc(_get(c, 'drafts', 'escalation_summary') if is_esc else _get(c, 'drafts', 'outreach_letter'))
    json_in = items[0] if items else {}   # $json = the current (first) input item
    return [_strip({**json_in, 'dispatch': d,
                    'letter_subject': subject,
                    'letter_html': '<div style="font-family:Arial,Helvetica,sans-serif;font-size:14px;line-height:1.6;color:#1f2937;'
                                   f'white-space:pre-wrap">{body}</div>',
                    'applicant_name': _get(c, 'applicant_name'), 'application_id': _get(c, 'application_id'), 'route': route})]
