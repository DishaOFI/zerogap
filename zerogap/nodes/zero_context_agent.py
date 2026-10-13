"""Zero-Context Agent (port of Zero_Context_Agent.js).

What an agent without ARIS context does with this case (deterministic, so the demo is repeatable).
It follows the OLD rulebook snapshot it was given (no policy amendments) and, lacking a governed data model,
it fills gaps on its own: name-matched history, copied values, assumptions, a generic checklist, an
auto-sent letter and a direct write to the case record. Every risk below is recorded with its evidence.
"""
from __future__ import annotations
import math
import re

# While testing, a letter the agent wants to email to an address it copied from another case goes here instead.
# None = such a letter is NOT emailed (the review mail still shows where it would have gone).
CONFIG = {'TEST_RECIPIENT': None}


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
        return ','.join('' if _nullish(x) else _s(x) for x in v)
    if isinstance(v, dict):
        return '[object Object]'
    return str(v)


def _get(o, *keys):
    """Optional chaining o?.k1?.k2 …"""
    for k in keys:
        o = o.get(k, UNDEF) if isinstance(o, dict) else UNDEF
    return o


def _or(a, b):
    return a if _truthy(a) else b


def _qq(a, b):
    return b if _nullish(a) else a


def _len(v) -> int:
    if isinstance(v, (list, str)):
        return len(v.encode('utf-16-le', 'surrogatepass')) // 2 if isinstance(v, str) else len(v)
    if _nullish(v):
        raise TypeError(f"Cannot read properties of {_s(v)} (reading 'length')")
    return 0   # .length of a number/object is undefined -> treated as falsy


def _gt0(v) -> bool:
    if isinstance(v, bool):
        return v
    if isinstance(v, (int, float)):
        return v > 0
    if isinstance(v, str):
        try:
            return float(_TRIM.sub('', v) or 0) > 0
        except ValueError:
            return False
    return False


def _strip(v):
    if isinstance(v, dict):
        return {k: _strip(x) for k, x in v.items() if x is not UNDEF}
    if isinstance(v, list):
        return [None if x is UNDEF else _strip(x) for x in v]
    if isinstance(v, float) and (math.isnan(v) or math.isinf(v)):
        return None
    return v


def _spread(v) -> dict:
    return dict(v) if isinstance(v, dict) else {}


# ---- node ------------------------------------------------------------------------------------------------------
# skip process columns (timestamps, flags, counters) – only take value columns
SKIP = re.compile(r'timestamp|_flag\Z|flag\Z|_count\Z|business_days|^days|demo_', re.I)
UNNECESSARY = [
    {'item': 'Medical reports and diagnosis from all treating doctors', 'sensitive': True},
    {'item': 'Hospital discharge report (Entlassungsbericht)', 'sensitive': True},
    {'item': 'Copy of the applicant’s identity card', 'sensitive': True},
    {'item': 'Bank details and last three bank statements', 'sensitive': True},
    {'item': 'Care provider contract', 'sensitive': False},
]
FIELD_OF = {'BENEFIT_TYPE_MISSING': 'benefit_type', 'GP_MISSING': 'gp', 'ADDRESS_MISSING': 'address', 'DOB_MISSING': 'date_of_birth'}
_CORE = re.compile(r'benefit_type|care_level|benefit_amount|statutory_track')


def run(ctx, items: list[dict]) -> list[dict]:
    C = {**CONFIG, **((getattr(ctx, 'config', None) or {}).get('zero_context_agent', {}) or {})}
    c = items[0] if items else {}
    a = _or(_get(c, 'applicant'), {})
    nl = _or(_get(c, 'name_lookup'), {'matches': [], 'chosen': None})
    prev = _or(_get(nl, 'chosen'), None)
    prev_keys = list(prev.keys()) if isinstance(prev, dict) else []

    def col(rx):
        return next((k for k in prev_keys if rx.search(k)), UNDEF) if _truthy(prev) else None

    def pick(rx):
        k = next((h for h in prev_keys if rx.search(h) and not SKIP.search(h)), UNDEF) if _truthy(prev) else None
        return {'column': k, 'value': prev[k]} if _truthy(k) and prev[k] != '' else None

    # ---------- 1) collection: name-matched history, all columns, copied into the claim
    copied = []

    def take(field, label, rx):
        v = pick(rx)
        if v:
            copied.append({'field': field, 'label': label, 'value': v['value'],
                           'from': f"{_s(_or(_get(nl, 'chosen_id'), 'previous case'))} ({_s(v['column'])})"})
        return v
    prev_benefit = take('benefit_type', 'Benefit type', re.compile(r'benefit|leistung', re.I))
    prev_gp = take('gp', 'GP (Hausarzt)', re.compile(r'(^|_)gp(_|\Z)|hausarzt|practitioner', re.I))
    prev_level = take('care_level', 'Care level (Pflegegrad)', re.compile(r'care_?level|pflegegrad|grade', re.I))
    take('benefit_amount', 'Monthly benefit amount', re.compile(r'amount|betrag|eur|payment', re.I))
    take('address', 'Address', re.compile(r'address|anschrift|street', re.I))
    take('date_of_birth', 'Date of birth', re.compile(r'birth|geburt|dob', re.I))
    take('email', 'E-mail address', re.compile(r'mail', re.I))
    take('statutory_track', 'Statutory track', re.compile(r'statutory_?track|deadline_?type', re.I))
    prev_missing = pick(re.compile(r'missing_?items|fehlende', re.I))   # the previous case's outstanding items – reused as this claim's request
    # (JS calls .trim()/.split() on the value; a non-string cell is converted with String() here instead of throwing)
    prev_missing_items = []
    if prev_missing and not re.search(r'^(-|none|keine)?\Z', _TRIM.sub('', _s(prev_missing['value'])), re.I):
        prev_missing_items = [x for x in (_TRIM.sub('', p) for p in re.split(r'[,;|]', _s(prev_missing['value']))) if x]

    # ---------- 2) gaps the agent fills with assumptions (AI-generated, stored as if verified)
    invented = []
    bs = _get(c, 'benefits_selected')
    benefits = bs if _truthy(_len(_or(bs, []))) else None
    if not _truthy(benefits) and not prev_benefit:
        invented.append({'field': 'benefit_type', 'label': 'Benefit type', 'value': 'Pflegegeld', 'reason': 'assumed – most common benefit type'})
    if not _truthy(_get(c, 'gp')) and not prev_gp:
        invented.append({'field': 'gp', 'label': 'GP (Hausarzt)', 'value': 'family doctor at the applicant’s address (assumed)', 'reason': 'inferred from the address'})
    rep = _get(c, 'representative')
    invented.append({'field': 'care_provider', 'label': 'Caregiver',
                     'value': f"{_s(_get(rep, 'name'))} (family caregiver, assumed)" if _truthy(rep) else 'family member (assumed)',
                     'reason': 'assumed from the questionnaire'})
    if not prev_level:
        invented.append({'field': 'care_level', 'label': 'Care level (Pflegegrad)', 'value': 'Pflegegrad 2', 'reason': 'estimated from the questionnaire'})

    # ---------- 3) generic checklist (no claim-specific rules) -> items marked missing although not required
    unnecessary = [dict(u) for u in UNNECESSARY]
    # items it "filled" itself are no longer requested – it believes it already has them
    filled_fields = {x['field'] for x in [*copied, *invented]}
    def _field_of(f):
        code = _get(f, 'code')
        return FIELD_OF.get(code) if isinstance(code, str) else None   # FIELD_OF[f.code] (undefined -> never "filled")
    still_missing = [f for f in _or(_get(c, 'hard_failures'), []) if _field_of(f) is None or _field_of(f) not in filled_fields]
    required = [_get(f, 'letter_text') for f in still_missing if _truthy(_get(f, 'requestable'))]
    from_prev = [f'{x} (please resubmit)' for x in prev_missing_items]
    letter_items = [*required, *from_prev, *[u['item'] for u in unnecessary]]

    # ---------- 4) the case record the agent writes (simulated CRM write – nothing real is changed)
    before = {'applicant': ' '.join(_s(x) for x in [_get(a, 'first_name'), _get(a, 'last_name')] if _truthy(x)),
              'member_number': _get(a, 'insurance_number'), 'date_of_birth': _get(a, 'date_of_birth'),
              'address': _get(a, 'address'), 'email': _or(_get(a, 'email'), None), 'statutory_track': None,
              'benefit_type': ', '.join('' if _nullish(x) else _s(x) for x in benefits) if _truthy(benefits) else None,
              'gp': _or(_get(c, 'gp'), None), 'care_provider': None, 'care_level': None, 'benefit_amount': None}
    after = dict(before)
    changes = []

    def set_(field, value, how):
        if _nullish(value) or _s(after.get(field, UNDEF)) == _s(value):
            return
        changes.append({'field': field, 'from': _qq(after.get(field, UNDEF), '(empty)'), 'to': value, 'how': how})
        after[field] = value
    for x in copied:
        set_(x['field'], x['value'], f"copied from {x['from']}")
    for x in invented:
        set_(x['field'], x['value'], x['reason'])
    overwritten = [x for x in changes if x['from'] != '(empty)']
    record_write = {
        'simulated': True, 'written_at': ctx.now_iso(), 'human_validation': False,
        'before': before, 'after': after, 'changes': changes,
        'provenance': 'none – every field stored as "provided by applicant", status "verified"',
        'history': 'none – previous values not kept',
        'previous_case_updated': {'case': _get(nl, 'chosen_id'), 'change': f"linked to {_s(_get(c, 'case_ref'))} and marked \"superseded\""}
        if _truthy(prev) else None,
    }

    # ---------- risk register (same rows as the ARIS agent's register)
    def R(id_, occurred, evidence):
        return {'id': id_, 'occurred': occurred, 'evidence': evidence}
    g = lambda k: _get(nl, k)  # noqa: E731
    unn_items = [u['item'] for u in unnecessary]
    if _truthy(prev):
        c1_occ = _or(g('id_differs'), g('dob_differs'))
        c1_ev = (f"Name search found {_s(g('matches_count'))} row(s) for \"{_s(g('searched_for'))}\" ({_s(g('distinct_people'))} different people"
                 + (f", {_s(g('same_name_and_dob'))} of them with the same date of birth" if _truthy(g('same_name_and_dob')) else '')
                 + f"). It used {_s(_or(g('chosen_id'), 'the latest'))}"
                 + (' (born ' + _s(g('chosen_dob')) + ')' if _truthy(g('chosen_dob')) else '')
                 + f" – applicant is {_s(_get(a, 'insurance_number'))}, born {_s(_get(a, 'date_of_birth'))}.")
        sk = col(re.compile(r'status', re.I))
        status = _get(prev, 'undefined' if sk is UNDEF else sk)
        c2_ev = (f"Previous case {_s(g('chosen_id'))} ({_s(_or(status, 'status unknown'))}) used as context for this claim"
                 + (f" – its outstanding items ({', '.join(prev_missing_items)}) are requested again" if prev_missing_items else '') + '.')
    else:
        c1_occ, c1_ev, c2_ev = False, 'No name match in the event log.', '–'
    columns = g('columns')
    if _nullish(columns):
        raise TypeError("Cannot read properties of undefined (reading 'length')")
    core = [x for x in changes if _CORE.search(x['field'])]
    risks = [
        R('C1', c1_occ, c1_ev),
        R('C2', _truthy(prev), c2_ev),
        R('C3', _gt0(g('matches_count')),
          f"Loaded {_s(g('matches_count'))} row(s) × {_s(_len(columns))} columns for everyone with this name ({_s(g('rows_scanned'))} rows scanned)."),
        R('C4', len(copied) > 0, ', '.join(f"{x['label']} “{_s(x['value'])}”" for x in copied) + f" copied from {_s(g('chosen_id'))}." if copied else '–'),
        R('M1', True, f"Marked missing although not required: {'; '.join([*prev_missing_items, *unn_items])}."),
        R('R1', True, f"Requested: {'; '.join(u['item'] for u in unnecessary if u['sensitive'])}."),
        R('R2', True, 'Letter states that every listed document is “mandatory” for the application.'),
        R('R3', False, 'Letter goes to caseworker review.'),
        R('W1', len(invented) > 0, ', '.join(f"{x['label']} = “{_s(x['value'])}”" for x in invented) + ' entered into the claim.' if invented else '–'),
        R('W2', len(invented) + len(copied) > 0, f'{len(invented) + len(copied)} empty field(s) filled with assumptions or values from another case.'),
        R('W3', len(changes) > 0, 'Filled values stored with status “verified”.'),
        R('W4', len(changes) > 0, f'{len(changes)} field(s) of the case record changed without authorisation.'),
        R('W5', len(overwritten) > 0, '; '.join(f"{x['field']}: “{_s(x['from'])}” → “{_s(x['to'])}”" for x in overwritten)
          if overwritten else 'No existing value overwritten in this run.'),
        R('W6', True, 'Written straight to the case record – no human validation step.'),
        R('W7', _truthy(record_write['previous_case_updated']),
          f"Previous case {_s(g('chosen_id'))} changed (“superseded”) – original not preserved." if _truthy(record_write['previous_case_updated']) else '–'),
        R('W8', len(core) > 0, _or(', '.join(f"{x['field']} = “{_s(x['to'])}”" for x in core), '–')),
        R('W9', len(changes) > 0, 'Copied and assumed values are recorded as “provided by applicant”.'),
        R('W10', True, 'No provenance kept – source, inferred and AI-generated values look the same.'),
    ]

    # what this agent presents as the case (its own record after copying / assuming)
    A2 = record_write['after']
    parts = _s(_or(A2['applicant'], '')).split(' ')
    fn, ln = parts[0], parts[1:]
    A = lambda k: A2.get(k, UNDEF)  # noqa: E731

    # dispatch
    cd = _get(c, 'dispatch')
    pa = A('address') if _get(cd, 'postal_name') == _get(c, 'applicant_name') else _get(cd, 'postal_address')
    em0 = _get(cd, 'options', 'email')
    if _truthy(em0) and _truthy(pa):
        text = f"After approval the letter can be emailed to {_s(_get(cd, 'real_email'))} or printed and posted to {_s(_get(cd, 'postal_name'))}, {_s(pa)}."
    elif _truthy(em0):
        text = f"After approval the letter is emailed to {_s(_get(cd, 'real_email'))}."
    elif _truthy(pa):
        text = f"No email address on file – after approval the letter is printed and posted to {_s(_get(cd, 'postal_name'))}, {_s(pa)}."
    else:
        text = _get(cd, 'text')
    d = {**_spread(cd), 'postal_address': pa, 'text': text}
    em = A('email')
    if _truthy(em) and em != _get(cd, 'real_email'):   # e-mail address taken over from the other person's case
        # the copied address itself is never mailed: the letter goes to TEST_RECIPIENT, or to the address on the form
        to = _or(C['TEST_RECIPIENT'], _or(_get(cd, 'to'), None))
        d.update({'real_email': em, 'to': to, 'test_mode': _truthy(to) and to != em,
                  'channel': 'email' if _truthy(to) else ('print' if _truthy(d['postal_address']) else 'none'),
                  'options': {**_spread(_or(d.get('options', UNDEF), {})), 'email': _truthy(to)}})
        if _truthy(to):
            d['text'] = (f"After approval the letter is emailed to {_s(em)}"
                         + (f" or printed and posted to {_s(d.get('postal_name', UNDEF))}, {_s(d['postal_address'])}" if _truthy(d['postal_address']) else '')
                         + '.')
        else:
            d['text'] = (f"After approval the letter is printed and posted to {_s(d.get('postal_name', UNDEF))}, {_s(d['postal_address'])}. "
                         f"The agent recorded {_s(em)} as the applicant's e-mail address.")

    extra_facts = [['E-mail', _or(A('email'), '–')], ['Statutory track', _or(A('statutory_track'), '–')],
                   ['Monthly benefit', _s(A('benefit_amount')) + ' EUR' if _truthy(A('benefit_amount')) else '–'],
                   ['Caregiver', _or(A('care_provider'), '–')]]
    extra_facts = [[k, v] for k, v in extra_facts if v != '–' or k == 'Caregiver']

    rc = _get(c, 'review_context')
    if _nullish(rc):
        raise TypeError("Cannot read properties of undefined (reading 'llm_input')")
    benefit_list = [A('benefit_type')] if _truthy(A('benefit_type')) else []
    out = {**c,
           'form': {**_spread(_get(c, 'form')),
                    'applicant': {**_spread(a), 'first_name': fn, 'last_name': ' '.join(ln), 'date_of_birth': A('date_of_birth'),
                                  'address': A('address'), 'email': A('email')},
                    'benefits_selected': list(benefit_list)},
           'applicant': {**_spread(a), 'date_of_birth': A('date_of_birth'), 'address': A('address'), 'email': A('email')},
           'benefits_selected': list(benefit_list), 'gp': A('gp'),
           'hard_failures': [*still_missing, *({'code': 'DOCUMENT_REQUIRED', 'field': 'documents', 'message': f"{u['item']} – mandatory.",
                                                'message_en': f"{u['item']} – mandatory.", 'requestable': True, 'letter_keywords': [],
                                                'letter_text': u['item']} for u in unnecessary)],
           'soft_warnings': [], 'info': [],
           'final_status': 'Missing Information', 'route': 'letter', 'route_reasons': [],
           'care_level': _or(A('care_level'), None),
           'dispatch': d,
           'extra_facts': extra_facts,
           'zero_context': {'copied': copied, 'invented': invented, 'unnecessary': unnecessary, 'record_write': record_write},
           'risk_register': {'agent': 'zero-context', 'risks': risks},
           'review_context': {**_spread(rc),
                              'llm_input': {**_spread(_get(rc, 'llm_input')), 'ROUTE': 'letter', 'REQUEST_STYLE': 'itemized',
                                            'LETTER_ITEMS': letter_items, 'JUSTIFICATION': 'mandatory'},
                              'letter_items': list(letter_items)},
           }
    return [_strip(out)]
