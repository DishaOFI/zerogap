"""Shared caseworker review e-mail renderer + the small JS-semantics helpers the ported nodes need.

Python port of `renderReviewEmail(b, d)` as it was injected into the n8n Code nodes
"Guardrails: Validate AI Draft" and "Prepare Letter & Review Email" (both copies are identical).
Note: that injected copy is newer than v3_nodes/_renderer.js — it shows `b.care_level`, writes
`b.review_facts` and has the "Confirm – release for assessment" button for route 'none'. The injected
copy is what ran, so that is what is ported here.

The output HTML must be byte-identical to the JS template literals, so whitespace and escaping are
reproduced exactly.
"""
from __future__ import annotations

import math
import re
from decimal import Decimal


# ---------------------------------------------------------------------------------------------------------
# JS semantics helpers
# ---------------------------------------------------------------------------------------------------------
class _Undefined:
    """JS `undefined` (missing property). Distinct from None (= JSON null)."""
    _inst = None

    def __new__(cls):
        if cls._inst is None:
            cls._inst = super().__new__(cls)
        return cls._inst

    def __bool__(self):
        return False

    def __repr__(self):
        return 'undefined'


UNDEF = _Undefined()

# ECMAScript WhiteSpace + LineTerminator (what `\s` and String.prototype.trim use)
JS_WS_CHARS = ('\t\n\v\f\r \u00a0\u1680\u2000\u2001\u2002\u2003\u2004\u2005\u2006\u2007\u2008\u2009\u200a'
               '\u2028\u2029\u202f\u205f\u3000\ufeff')
JS_WS = '[' + re.escape(JS_WS_CHARS) + ']'      # JS `\s`
JS_NWS = '[^' + re.escape(JS_WS_CHARS) + ']'    # JS `\S`


def g(obj, *keys):
    """Optional-chained property read `obj?.k1?.k2`; missing -> UNDEF, nullish base -> UNDEF."""
    for k in keys:
        if isinstance(obj, dict):
            obj = obj.get(k, UNDEF)
        elif isinstance(obj, list) and k == 'length':
            obj = len(obj)
        elif isinstance(obj, str) and k == 'length':
            obj = utf16_len(obj)
        else:
            return UNDEF
    return obj


def nullish(v) -> bool:
    return v is None or v is UNDEF


def truthy(v) -> bool:
    if v is None or v is UNDEF or v is False:
        return False
    if isinstance(v, bool):
        return True
    if isinstance(v, (int, float)):
        return not (v == 0 or (isinstance(v, float) and math.isnan(v)))
    if isinstance(v, str):
        return v != ''
    return True  # objects and arrays (also empty ones) are truthy in JS


def or_(*vals):
    """a || b || c"""
    for v in vals[:-1]:
        if truthy(v):
            return v
    return vals[-1]


def coalesce(*vals):
    """a ?? b ?? c"""
    for v in vals[:-1]:
        if not nullish(v):
            return v
    return vals[-1]


def utf16_len(s: str) -> int:
    return len(s.encode('utf-16-le', 'surrogatepass')) // 2


def js_num(x) -> str:
    """Number.prototype.toString() for a float/int."""
    if isinstance(x, bool):
        return 'true' if x else 'false'
    if isinstance(x, int):
        return str(x)
    if math.isnan(x):
        return 'NaN'
    if math.isinf(x):
        return 'Infinity' if x > 0 else '-Infinity'
    if x == 0:
        return '0'
    sign = '-' if x < 0 else ''
    t = Decimal(repr(abs(x))).as_tuple()
    digits = ''.join(map(str, t.digits)).rstrip('0') or '0'
    # exponent such that value = 0.d1d2... * 10^n
    n = t.exponent + len(t.digits)
    k = len(digits)
    if k <= n <= 21:
        s = digits + '0' * (n - k)
    elif 0 < n <= 21:
        s = digits[:n] + '.' + digits[n:]
    elif -6 < n <= 0:
        s = '0.' + '0' * (-n) + digits
    else:
        e = n - 1
        s = digits[0] + ('.' + digits[1:] if k > 1 else '') + 'e' + ('+' if e >= 0 else '-') + str(abs(e))
    return sign + s


def js_str(v) -> str:
    """String(v)"""
    if v is UNDEF:
        return 'undefined'
    if v is None:
        return 'null'
    if v is True:
        return 'true'
    if v is False:
        return 'false'
    if isinstance(v, (int, float)):
        return js_num(v)
    if isinstance(v, str):
        return v
    if isinstance(v, list):
        return join(v, ',')
    return '[object Object]'


def join(arr, sep=',') -> str:
    """Array.prototype.join — null/undefined elements become ''."""
    return sep.join('' if nullish(x) else js_str(x) for x in arr)


def to_number(v) -> float:
    """Number(v)"""
    if v is UNDEF:
        return math.nan
    if v is None or v is False:
        return 0
    if v is True:
        return 1
    if isinstance(v, (int, float)):
        return v
    if isinstance(v, str):
        s = js_trim(v)
        if s == '':
            return 0
        try:
            if re.fullmatch(r'[+-]?(\d+\.?\d*|\.\d+)([eE][+-]?\d+)?', s, re.ASCII):
                f = float(s)
                return int(f) if f.is_integer() and abs(f) < 2 ** 53 and 'e' not in s.lower() and '.' not in s else f
            if re.fullmatch(r'0[xX][0-9a-fA-F]+', s):
                return int(s, 16)
            if re.fullmatch(r'0[oO][0-7]+', s):
                return int(s, 8)
            if re.fullmatch(r'0[bB][01]+', s):
                return int(s, 2)
            if re.fullmatch(r'[+-]?Infinity', s):
                return -math.inf if s[0] == '-' else math.inf
        except ValueError:
            pass
        return math.nan
    if isinstance(v, list):
        return to_number(join(v)) if len(v) <= 1 else math.nan
    return math.nan


def neg(v):
    """unary minus"""
    n = to_number(v)
    return -n if n != 0 else (0 if isinstance(n, int) else -0.0)


def js_trim(s: str) -> str:
    return s.strip(JS_WS_CHARS)


def strict_eq(a, b) -> bool:
    """a === b"""
    if a is UNDEF or b is UNDEF:
        return a is b
    if a is None or b is None:
        return a is b
    if isinstance(a, bool) or isinstance(b, bool):
        return isinstance(a, bool) and isinstance(b, bool) and a == b
    if isinstance(a, (int, float)) and isinstance(b, (int, float)):
        return a == b
    if isinstance(a, str) and isinstance(b, str):
        return a == b
    return a is b


def js_slice(s: str, start: int, end: int | None = None) -> str:
    n = len(s)
    start = max(n + start, 0) if start < 0 else min(start, n)
    end = n if end is None else (max(n + end, 0) if end < 0 else min(end, n))
    return s[start:end] if end > start else ''


def index_of(s: str, sub: str) -> int:
    return s.find(sub)


def last_index_of(s: str, sub: str) -> int:
    return s.rfind(sub)


def clean(v):
    """What JSON serialisation of the n8n output does: undefined properties vanish, undefined/NaN in arrays -> null."""
    if isinstance(v, dict):
        return {k: clean(x) for k, x in v.items() if x is not UNDEF}
    if isinstance(v, list):
        return [None if x is UNDEF else clean(x) for x in v]
    if isinstance(v, float) and (math.isnan(v) or math.isinf(v)):
        return None
    return v


def json_parse(s: str):
    """JSON.parse — rejects NaN/Infinity which Python's json accepts."""
    import json

    def _bad(c):
        raise ValueError(f'invalid constant {c}')
    return json.loads(s, parse_constant=_bad)


# ---------------------------------------------------------------------------------------------------------
# Review e-mail renderer
# ---------------------------------------------------------------------------------------------------------
MON = ['January', 'February', 'March', 'April', 'May', 'June', 'July', 'August', 'September', 'October',
       'November', 'December']


def _mon(m: str) -> str:
    i = int(m) - 1
    return MON[i] if 0 <= i < 12 else 'undefined'


def esc(s) -> str:
    # String(s ?? '') with &, <, > escaped (quotes are NOT escaped)
    return js_str('' if nullish(s) else s).replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;')


_ISO_START = re.compile(r'^(\d{4})-(\d{2})-(\d{2})', re.ASCII)
_DE_DATE = re.compile(r'^(\d{1,2})\.(\d{1,2})\.(\d{4})\Z', re.ASCII)
_ISO_ANY = re.compile(r'(\d{4})-(\d{2})-(\d{2})', re.ASCII)


def fmt_date(v) -> str:
    if not truthy(v):
        return '–'
    sv = js_str(v)
    m = _ISO_START.match(sv)
    if m:
        return f'{int(m[3])} {_mon(m[2])} {m[1]}'
    m = _DE_DATE.match(sv)
    if m:
        return f'{int(m[1])} {_mon(m[2])} {m[3]}'
    return sv


def iso_to_text(t) -> str:
    return _ISO_ANY.sub(lambda m: f'{int(m[3])} {_mon(m[2])} {m[1]}', js_str(t))


F = 'font-family:Arial,Helvetica,sans-serif;'
INK, MUTED, LINE = '#1f2937', '#6b7280', '#e5e7eb'


def P(html: str, extra: str = '') -> str:
    return f'<p style="margin:0 0 14px 0;{F}font-size:14px;line-height:1.6;color:{INK};{extra}">{html}</p>'


def H(t) -> str:
    return f'<p style="margin:22px 0 8px 0;{F}font-size:14px;font-weight:bold;color:{INK};">{esc(t)}</p>'


def _list(tag: str, items: list[str]) -> str:
    if not items:
        return ''
    lis = ''.join(f'<li style="margin-bottom:4px;">{i}</li>' for i in items)
    return f'<{tag} style="margin:0 0 14px 0;padding-left:22px;{F}font-size:14px;line-height:1.6;color:{INK};">{lis}</{tag}>'


def OL(items):
    return _list('ol', items)


def UL(items):
    return _list('ul', items)


def _btn(label: str, href, primary: bool) -> str:
    style = ('background:#1f4e79;color:#ffffff;border:1px solid #1f4e79;' if primary
             else 'background:#ffffff;color:#1f2937;border:1px solid #9ca3af;')
    return (f'<td style="padding:0 10px 0 0;"><a href="{js_str(href)}" style="display:inline-block;padding:11px 30px;'
            f'border-radius:4px;{F}font-size:14px;font-weight:bold;text-decoration:none;{style}">{label}</a></td>')


def _pair(x):
    # const [k, v] = x
    k = x[0] if isinstance(x, list) and len(x) > 0 else UNDEF
    v = x[1] if isinstance(x, list) and len(x) > 1 else UNDEF
    return k, v


def build_facts(b: dict) -> list:
    """The "Application details" key facts of the review e-mail (also stored as `review_facts` for the portal)."""
    f = or_(g(b, 'form'), {})
    a = or_(g(f, 'applicant'), {})
    s = or_(g(b, 'sla'), {})
    pol = g(b, 'policy')
    name = applicant_name(a)
    lvl = or_(g(pol, 'escalation', 'level'), 0)
    route = g(b, 'route')
    rep = g(b, 'representative')
    if truthy(rep):
        rel = g(rep, 'relationship')
        rep_txt = js_str(g(rep, 'name')) + (' (' + js_str(rel) + ')' if truthy(rel) else '')
    else:
        rep_txt = '–'
    days = g(s, 'days_to_sla_breach')
    days_txt = (f'{js_num(neg(days))} working days overdue' if to_number(days) < 0
                else f'{js_str(days)} working days remaining')
    if route == 'escalation':
        next_step = f"Escalation to the Case Manager – {join(or_(g(b, 'route_reasons'), []), '; ')} (per PA-2026-03)"
    elif route == 'letter':
        next_step = f"Letter to {js_str(or_(g(b, 'review_context', 'addressee'), name))} – after your approval"
    else:
        next_step = 'None'
    facts = [
        ['Applicant', f"{name} (born {fmt_date(g(a, 'date_of_birth'))})"],
        ['Address', or_(g(a, 'address'), '–')],
        ['Member number', or_(g(a, 'insurance_number'), '–')],
        ['Case reference', or_(g(b, 'application_id'), 'New application')],
        ['Representative', rep_txt],
        ['Benefit applied for', join(or_(g(f, 'benefits_selected'), []), ', ') or 'Not specified'],
        ['GP (Hausarzt)', or_(g(b, 'gp'), 'Not provided')],
        ['Care level', or_(g(b, 'care_level'), 'Not yet assessed')],
        ['Statutory decision deadline', f"{fmt_date(g(s, 'sla_deadline_date'))} ({days_txt})"],
    ]
    it = g(s, 'internal_target')
    if truthy(it):
        facts.append(['Internal service target',
                      f"{js_str(g(it, 'left'))} working days remaining{' – At risk' if truthy(g(it, 'at_risk')) else ''} (per PA-2026-04)"])
    facts.append(['Next step', next_step])
    amends = or_(g(pol, 'amendments'), [])
    if len(amends):
        facts.append(['Policy amendments applied', join([g(x, 'id') for x in amends], ', ')])
    facts.extend(_pair(x) for x in or_(g(b, 'extra_facts'), []))
    if truthy(lvl):
        facts.append(['Escalation', f"Level {js_str(lvl)} – {js_str(g(pol, 'escalation', 'owner'))}"])
    return facts


def applicant_name(a) -> str:
    return join([x for x in (g(a, 'first_name'), g(a, 'last_name')) if truthy(x)], ' ') or 'the applicant'


_SUMMARY_ICON = re.compile('^[\u23f1\u26a0]' + JS_NWS + '*' + JS_WS + '*')


def review_facts(b: dict) -> list:
    return [[k, js_str(v)] for k, v in build_facts(b)]


def render_review_email(b: dict, d: dict | None) -> str:
    """Review email in a plain, professional business-mail layout (email-client safe, inline styles).
    Mutates `b` like the JS original: sets b['review_facts'] (same details for the portal)."""
    f = or_(g(b, 'form'), {})
    a = or_(g(f, 'applicant'), {})
    ready = g(b, 'final_status') == 'Ready for Assessment'
    name = applicant_name(a)
    case_ref = f"case {js_str(g(b, 'application_id'))}" if truthy(g(b, 'application_id')) else 'a new application'
    hard = or_(g(b, 'hard_failures'), [])
    route = g(b, 'route')

    # key facts
    facts = build_facts(b)
    b['review_facts'] = [[k, js_str(v)] for k, v in facts]  # same details for the portal
    rows = ''.join(
        f'<tr><td style="padding:7px 12px 7px 0;{F}font-size:13px;color:{MUTED};width:38%;vertical-align:top;'
        f'border-bottom:1px solid {LINE};">{esc(k)}</td><td style="padding:7px 0;{F}font-size:13px;color:{INK};'
        f'vertical-align:top;border-bottom:1px solid {LINE};">{esc(v)}</td></tr>' for k, v in facts)
    facts_table = (f'<table role="presentation" cellpadding="0" cellspacing="0" style="border-collapse:collapse;'
                   f'width:100%;margin:0 0 6px 0;border-top:1px solid {LINE};">{rows}</table>')

    # opening
    head = (f"The completeness check for the long-term care application of <b>{esc(name)}</b> (insurance number "
            f"{esc(or_(g(a, 'insurance_number'), '–'))}, {esc(case_ref)}) has been completed.")
    if ready:
        opening = head + (' All mandatory information is present and the application is ready for assessment by the '
                          'Medical Service (MD). Please review the details below and confirm the release.')
    elif route == 'escalation':
        opening = head + (' The application is still incomplete and, per PA-2026-03, it is escalated to the Case '
                          'Manager for phone outreach instead of a further letter. Please approve the escalation.')
    else:
        opening = head + ' The application is incomplete and requires your review before we contact the applicant.'

    # outstanding items
    outstanding = [esc(or_(g(x, 'message'), g(x, 'message_en'))) for x in hard]
    # (The JS also computes notes / steps / sopSource / ARIS insight lines, but never puts them into the HTML;
    #  that dead code is not ported.)

    letter = ''
    if d and truthy(g(d, 'letter_file_name')):
        if route == 'escalation':
            letter = P(f"The escalation note for the Case Manager is attached for your approval "
                       f"(<i>{esc(d['letter_file_name'])}</i>).")
        else:
            letter = P(f"The draft letter to {esc(or_(g(d, 'addressee'), name))} is attached for your approval "
                       f"(<i>{esc(d['letter_file_name'])}</i>).")
    # AI-written case summary is shown only when it passed the guardrails (the template fallback would just repeat
    # the sections below)
    summary = ''
    if d and g(d, 'draft_source') == 'llm' and truthy(g(d, 'escalation_summary')) and not ready:
        summary = P(_SUMMARY_ICON.sub('', esc(iso_to_text(d['escalation_summary'])), count=1))

    # buttons: escalation -> approve escalation; letter -> email (if an e-mail address is on file) and/or print
    # (if a postal address is on file)
    o = or_(g(b, 'dispatch', 'options'), {})
    if route == 'none':
        approve_btns = [['Confirm – release for assessment', 'none']]
    elif route == 'escalation':
        approve_btns = [['Approve escalation', 'case_manager']]
    else:
        approve_btns = ([['Approve & email to applicant', 'email']] if truthy(g(o, 'email')) else []) + \
                       ([['Approve & print letter', 'print']] if truthy(g(o, 'print')) else [])
    if not approve_btns:
        approve_btns.append(['Approve', 'none'])
    actions = ''
    acts = g(d, 'actions') if d else UNDEF
    if truthy(acts):
        approve_url = js_str(g(acts, 'approveUrl'))
        btns = ''.join(_btn(esc(label), approve_url + '&channel=' + ch, i == len(approve_btns) - 1)
                       for i, (label, ch) in enumerate(approve_btns))
        actions = (f'<table role="presentation" cellpadding="0" cellspacing="0" style="margin:6px 0 18px 0;"><tr>'
                   f"{_btn('Reject', g(acts, 'rejectUrl'), False)}{btns}</tr></table>")
    closing = ('Please confirm or reject using the buttons below.' if ready
               else 'Please approve or reject using the buttons below. No correspondence will be sent to the '
                    'applicant without your approval.')

    sugg = or_(g(b, 'suggestions'), [])
    dtext = g(b, 'dispatch', 'text')
    return '\n'.join([
        '<div style="text-align:left;max-width:600px;">',
        P('Dear colleague,'),
        P(opening),
        summary,
        H('Application details'),
        facts_table,
        H('Outstanding items') + OL(outstanding) if outstanding else '',
        H('Suggested from the ARIS case history') + UL([esc(g(x, 'text')) for x in sugg]) if len(sugg) else '',
        letter,
        P(esc(dtext)) if truthy(dtext) else '',
        P(closing, 'margin-top:18px;'),
        actions,
        P(f'Kind regards,<br><b>Zero Gap Agent</b><br><span style="color:{MUTED};">Long-Term Care Claims Operations</span>'),
        '</div>',
    ])
