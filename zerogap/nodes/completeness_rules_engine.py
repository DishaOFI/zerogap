"""Completeness Rules Engine — deterministic completeness check, statutory clocks, routing and escalation,
driven ONLY by the rulebook read in "Apply SOP Rules" (base SOPs + any policy amendments found on this run).
Same code in both flows. Every finding carries the document that governs it (e.g. "per PA-2026-02").

Python port of the n8n Code node "Completeness Rules Engine" (behavioural parity with the JS original).
"""
from __future__ import annotations

import datetime as _dt
import math
import re

from ._dates import add_wd, fmt, next_wd, wd_between

CONFIG = {
    'LANG': 'en',
    'CHECK_DATE': 'auto',    # 'auto' = re-check on the latest resubmission (or receipt if none), or a date like '2026-09-22'
    'TEST_RECIPIENT': None,  # while testing, applicant emails go here instead of the real address
    'CASE_MANAGER_EMAIL': 'case.manager@your-domain.example',  # receives escalations (PA-2026-03)
}


# ---------- JS value semantics -------------------------------------------------------------------
class _Undefined:
    """JS `undefined`: falsy, prints as 'undefined', and its keys vanish from the JSON output."""
    __slots__ = ()

    def __bool__(self):
        return False

    def __repr__(self):
        return 'undefined'


UNDEF = _Undefined()
NAN = float('nan')


def truthy(v) -> bool:
    if v is None or v is UNDEF or v is False or isinstance(v, str) and v == '':
        return False
    if isinstance(v, (int, float)) and not isinstance(v, bool):
        return not (v == 0 or v != v)
    return True  # objects and arrays (even empty) are truthy in JS


def or_(*vals):
    """a || b || c"""
    for v in vals[:-1]:
        if truthy(v):
            return v
    return vals[-1]


def nn(v, d):
    """a ?? d"""
    return d if v is None or v is UNDEF else v


def g(o, k):
    """o?.k — UNDEF for a missing key or a null/undefined object."""
    if isinstance(o, dict):
        return o.get(k, UNDEF)
    return UNDEF


def num(v):
    """ToNumber for arithmetic."""
    if v is UNDEF:
        return NAN
    if v is None or v is False:
        return 0
    if v is True:
        return 1
    if isinstance(v, (int, float)):
        return v
    try:
        return float(v) if str(v).strip() else 0
    except ValueError:
        return NAN


def plus(a, b):
    """JS `+` (string concatenation if either side is a string)."""
    if isinstance(a, str) or isinstance(b, str):
        return s(a) + s(b)
    return num(a) + num(b)


def s(v) -> str:
    """String(v) / template-literal interpolation."""
    if v is UNDEF:
        return 'undefined'
    if v is None:
        return 'null'
    if v is True:
        return 'true'
    if v is False:
        return 'false'
    if isinstance(v, float):
        if v != v:
            return 'NaN'
        if math.isinf(v):
            return 'Infinity' if v > 0 else '-Infinity'
        if v.is_integer() and abs(v) < 1e21:
            return str(int(v))
        return repr(v)
    if isinstance(v, list):
        return join(v, ',')
    if isinstance(v, dict):
        return '[object Object]'
    return str(v)


def join(arr, sep=',') -> str:
    """Array.prototype.join: null/undefined become ''."""
    return sep.join('' if x is None or x is UNDEF else s(x) for x in arr)


def lt(a, b) -> bool:
    """JS `<` for the values used here (ISO date strings / numbers)."""
    if isinstance(a, str) and isinstance(b, str):
        return a < b
    x, y = num(a), num(b)
    return x < y  # NaN compares false, as in JS


def le(a, b) -> bool:
    if isinstance(a, str) and isinstance(b, str):
        return a <= b
    return num(a) <= num(b)


def js_max0(x):
    """Math.max(0, x) — NaN propagates."""
    return x if x != x else max(0, x)


def _clean(v):
    """JSON.stringify semantics: drop undefined keys, undefined in arrays and NaN become null."""
    if isinstance(v, dict):
        return {k: _clean(x) for k, x in v.items() if x is not UNDEF}
    if isinstance(v, list):
        return [None if x is UNDEF else _clean(x) for x in v]
    if isinstance(v, float) and (v != v or math.isinf(v)):
        return None
    return v


# ---------- node ---------------------------------------------------------------------------------
def run(ctx, items: list[dict]) -> list[dict]:
    cfg = {**CONFIG, **(ctx.config.get('engine', {}) or {})}
    c = items[0] if items else {}
    P = c['policy']['switches']
    CITE = or_(g(P, 'cite'), {})
    tl = or_(g(c, 'timeline'), {})
    a = or_(g(c, 'applicant'), {})
    rep = g(c, 'representative')
    vm = g(c, 'vollmacht')
    hist = or_(g(c, 'aris_history'), None)
    Pg = lambda k: g(P, k)  # noqa: E731

    # ---------- check date: the agent re-checks the case as it stood on this day
    today = ctx.now.astimezone(_dt.timezone.utc).date().isoformat()
    # an OPEN case for exactly this insurance number in ARIS = the same application: its first contact (formless Antrag),
    # statutory track and earlier missing-information requests apply to this check
    open_case = g(hist, 'case') if truthy(g(hist, 'found')) and truthy(g(g(hist, 'case'), 'open')) else None
    tl_received = g(tl, 'received')
    oc_received = g(open_case, 'received')
    received = oc_received if truthy(oc_received) and (not truthy(tl_received) or lt(oc_received, tl_received)) else tl_received
    if received is not tl_received and received != tl_received:
        first_contact_source = f"first contact recorded in ARIS (case {s(g(open_case, 'application_id'))})"
    else:
        first_contact_source = 'date the application form was received' if g(c, 'source') == 'email_form' else 'date of the application'
    if cfg['CHECK_DATE'] == 'auto':
        check_date = today if g(c, 'source') == 'email_form' else or_(g(tl, 'resubmission_received'), received, today)
    else:
        check_date = cfg['CHECK_DATE']

    def on_file(d):
        return truthy(d) and truthy(check_date) and le(d, check_date)

    recheck = bool(truthy(received) and lt(received, check_date))

    # ---------- findings (each with the governing document)
    hard, soft, info = [], [], []
    buckets = {'hard': hard, 'soft': soft, 'info': info}

    def add(lvl, code, field, message, basis, req=False, keywords=None, letter=None):
        buckets[lvl].append({
            'code': code, 'field': field, 'message': f'{message} (per {s(basis)})' if truthy(basis) else message,
            'message_en': message, 'basis': basis, 'requestable': req,
            'letter_keywords': [] if keywords is None else keywords, 'letter_text': letter,
        })

    applicant_name = or_(' '.join(s(x) for x in (g(a, 'first_name'), g(a, 'last_name')) if truthy(x)), 'the applicant')
    OP = 'Operating Procedure §3 (Applicant Data)'
    if not truthy(g(a, 'first_name')) or not truthy(g(a, 'last_name')):
        add('hard', 'NAME_MISSING', 'applicant.name', 'Full name of the applicant is missing.', OP, True, ['name'], 'the applicant’s full name')
    if not truthy(g(a, 'date_of_birth')):
        add('hard', 'DOB_MISSING', 'applicant.date_of_birth', 'Date of birth is missing.', OP, True, ['date of birth'], 'the applicant’s date of birth')
    if not truthy(g(a, 'insurance_number')):
        add('hard', 'INSURANCE_NO_MISSING', 'applicant.insurance_number', 'Insurance / member number is missing.', OP, True, ['member number', 'insurance number'], 'the insurance (member) number')
    if not truthy(g(a, 'address')):
        add('hard', 'ADDRESS_MISSING', 'applicant.address', 'Current address is missing.', OP, True, ['address'], 'the current address')
    sig = g(c, 'signature')
    if not truthy(sig) or re.search(r'not signed|unsigned|nicht unterschrieben', s(sig), re.I):
        add('hard', 'SIGNATURE_MISSING', 'execution.signature', 'Signature missing – unsigned forms cannot initiate processing.', g(CITE, 'signature'), True, ['signature'], 'a signed application form')

    # representative: Vollmacht (base SOP) + identity proof of the representative (PA-2026-02)
    vollmacht_ok = False
    if truthy(rep):
        rep_name = s(g(rep, 'name'))
        if truthy(Pg('vollmacht_required')):
            if truthy(vm) and on_file(g(vm, 'received_on')):
                if truthy(g(vm, 'matches')):
                    vollmacht_ok = True
                    add('info', 'VOLLMACHT_VERIFIED', 'representation.document', f"Vollmacht for {rep_name} on file since {fmt(g(vm, 'received_on'))} ({s(g(vm, 'received_via'))}) and matches the application.", g(CITE, 'representative'))
                else:
                    add('hard', 'VOLLMACHT_MISMATCH', 'representation.document', 'The Vollmacht on file does not match the named representative or applicant.', g(CITE, 'representative'), True, ['power of attorney'], 'a power of attorney that matches the named representative')
            else:
                rel = g(rep, 'relationship')
                add('hard', 'VOLLMACHT_MISSING', 'representation.document', f"{rep_name}{' (' + s(rel) + ')' if truthy(rel) else ''} is named as representative, but no power of attorney (Vollmacht) is on file.", g(CITE, 'representative'), True, ['power of attorney', 'Vollmacht'], f'a signed power of attorney (Vollmacht) authorising {rep_name} to act on the applicant’s behalf')
        else:
            add('info', 'REPRESENTATIVE_ACCEPTED', 'representation', f'Representative {rep_name} accepted as named on the application.', None)
        if truthy(Pg('rep_identity_proof')) and not truthy(g(g(c, 'rep_identity'), 'verified')):
            note = g(g(c, 'rep_identity'), 'applicant_only_note')
            add('hard', 'REP_IDENTITY_PROOF_MISSING', 'representation.identity', f"Identity proof of authorized representative {rep_name} is missing{' – ' + s(note) if truthy(note) else ''}.", g(CITE, 'rep_identity'), True, ['identity'], 'proof of your identity as authorised representative (a copy of your identity document)')
    benefits = or_(g(c, 'benefits_selected'), [])
    if truthy(Pg('benefit_type_required')) and not len(benefits):
        add('hard', 'BENEFIT_TYPE_MISSING', 'benefit_type', 'No type of benefit selected – required before the decision can be issued (the Medical Service can still be commissioned).', g(CITE, 'benefit'), True, ['benefit'], 'the type of benefit applied for (care allowance, in-kind care, combination benefit or full inpatient care)')
    if not truthy(g(c, 'gp')):
        if truthy(Pg('gp_mandatory')):
            add('hard', 'GP_MISSING', 'medical.gp', 'GP (Hausarzt) name and address missing.', g(CITE, 'gp'), True, ['general practitioner'], 'the name and address of the general practitioner (Hausarzt)')
        else:
            add('soft', 'GP_MISSING', 'medical.gp', 'GP (Hausarzt) name and address missing – needed to request the medical history before the assessment.', g(CITE, 'gp'))
    if truthy(Pg('civil_servant_check')) and truthy(g(c, 'civil_servant')):
        add('hard', 'BEIHILFE_NOT_VERIFIED', 'civil_service.beihilfestelle', 'Civil servant application without verified Beihilfestelle.', 'Operating Procedure §3 (Civil Servant)', True, ['Beihilfe'], 'confirmation of the civil-service subsidy office (Beihilfestelle)')
    if not truthy(g(g(c, 'link'), 'exact')) and len(or_(g(c, 'medical_docs'), [])):
        docs = join(c['medical_docs'], ', ')
        if truthy(Pg('quarantine_assumed_link')):
            add('soft', 'MEDICAL_DOCS_QUARANTINED', 'attachments', f'Medical documents ({docs}) quarantined – assumed link, no primary key.', 'Operating Procedure §4')
        else:
            add('info', 'MEDICAL_DOCS_ATTACHED', 'attachments', f'Medical documents ({docs}) attached to the case.', None)
    final_status = 'Missing Information' if hard else 'Ready for Assessment'

    # ---------- statutory clock, internal target (PA-2026-04)
    hist_track = None
    oc_track = g(open_case, 'track')
    if truthy(Pg('expedited_deadlines')) and truthy(oc_track):
        hist_track = 5 if re.search(r'(^|\D)5-day', s(oc_track), re.ASCII) else 10 if re.search(r'(^|\D)10-day', s(oc_track), re.ASCII) else None
    expedited = truthy(Pg('expedited_deadlines')) and (truthy(len(or_(g(c, 'sla_triggers'), []))) or truthy(hist_track))
    if truthy(hist_track):
        track = hist_track
    elif expedited:
        track = 5 if any(re.search(r'hospital|krankenhaus', s(t), re.I) for t in c['sla_triggers']) else 10
    else:
        track = or_(Pg('decision_days'), 25)
    complete_on = or_(g(tl, 'resubmission_received'), None if hard else received)
    clock_start = received if truthy(Pg('clock_from_first_contact')) else or_(complete_on, received)
    clock_basis = first_contact_source if truthy(Pg('clock_from_first_contact')) else 'date the application was confirmed complete'
    tl_disp = g(tl, 'request_dispatched')
    # requests sent before this check
    prior_requests = plus(
        1 if truthy(tl_disp) and truthy(check_date) and lt(tl_disp, check_date) else 0,
        or_(g(open_case, 'missing_info_count'), g(open_case, 'missing_info_flag'), 0) if truthy(open_case) else 0)
    pause = {'start': None, 'end': None, 'basis': None, 'working_days': 0}
    if truthy(prior_requests):
        if truthy(Pg('pause_from_applicant_receipt')):
            pause['basis'] = 'only from the day the applicant receives the request – receipt is not evidenced, so no pause is applied'
        else:
            # no 'working_days' key here: it stays undefined unless a dispatch date is known (as in the JS original)
            pause = {'start': tl_disp, 'end': or_(g(tl, 'resubmission_received'), check_date), 'basis': 'from the day the gap was identified'}
        if truthy(pause['start']):
            frm = pause['start'] if (isinstance(pause['start'], str) and isinstance(clock_start, str) and pause['start'] > clock_start) else clock_start
            to = pause['end'] if lt(pause['end'], check_date) else check_date
            pause['working_days'] = wd_between(frm, to)
    pause_wd = pause.get('working_days', UNDEF)
    elapsed = js_max0(wd_between(clock_start, check_date) - num(pause_wd))
    statutory_left = num(track) - elapsed
    decision_due = add_wd(clock_start, num(track) + num(pause_wd))
    internal = None
    if truthy(Pg('internal_target')) and not expedited:
        target = Pg('internal_target_days')
        internal = {'target': target, 'left': num(target) - elapsed, 'due': add_wd(clock_start, num(target) + num(pause_wd))}
        internal['at_risk'] = le(internal['left'], nn(Pg('at_risk_days'), 5))
    md_ordered = or_(g(tl, 'md_order'), None)
    if truthy(Pg('md_within_3_days')):
        md = {'rule': 'within 3 working days of receipt', 'due': add_wd(received, 3), 'ordered': md_ordered}
    else:
        md = {'rule': 'after the application is confirmed complete', 'due': next_wd(complete_on) if truthy(complete_on) else None, 'ordered': md_ordered}
    if truthy(md['ordered']):
        md['status'] = 'on time' if truthy(md['due']) and le(md['ordered'], md['due']) else 'late' if truthy(Pg('md_within_3_days')) else 'ordered'
    else:
        md['status'] = 'open'

    # ---------- routing: letter or escalation to the Case Manager (PA-2026-03)
    requestable = [f for f in hard + soft if truthy(f['requestable'])]
    route = 'letter' if requestable else 'none'
    route_reasons: list[str] = []
    if requestable and truthy(Pg('escalation_routing')):
        if expedited:
            route_reasons.append(f'case is on the {s(track)}-working-day track')
        if le(statutory_left, nn(Pg('escalate_when_days_left'), 10)):
            route_reasons.append(f'{s(statutory_left)} working days remain on the decision clock')
        if truthy(prior_requests):
            route_reasons.append(f'a missing-information request was already sent on {fmt(tl_disp)}' if truthy(tl_disp)
                                 else f'{s(prior_requests)} missing-information request(s) already sent for this case (ARIS)')
        if route_reasons:
            route = 'escalation'

    # ---------- escalation levels (Deadline & Escalation Procedure)
    escalation = {'level': 0, 'owner': None, 'reason': None}

    def bump(level, owner, reason):
        nonlocal escalation
        if level > escalation['level']:
            escalation = {'level': level, 'owner': owner, 'reason': reason}

    if truthy(Pg('early_warning')):
        pct = or_(Pg('level1_pct'), 70)
        if elapsed >= math.ceil(num(track) * num(pct) / 100) and not truthy(prior_requests) and hard:
            bump(1, 'Case Handler', f'{s(pct)} % of the decision deadline elapsed without a missing-information request.')
        if truthy(md['due']) and (lt(md['due'], md['ordered']) if truthy(md['ordered']) else lt(md['due'], check_date)):
            bump(2, 'Team Lead', f"3-working-day commissioning window ({fmt(md['due'])}) missed.")
    if truthy(prior_requests) and hard and truthy(Pg('repeat_request_team_lead')) and route != 'escalation':
        bump(2, 'Team Lead', f'Repeat request: {s(prior_requests)} earlier request(s) for this case (Work Instruction §7).')
    if statutory_left < 0:
        bump(3 if truthy(Pg('early_warning')) else 1, 'Process Owner', f"Decision deadline {fmt(decision_due)} missed – €{s(or_(Pg('penalty_eur_per_week'), 70))} per started week.")
    if not truthy(Pg('early_warning')) and escalation['level']:
        escalation['level'] = 0

    # ---------- outreach content
    itemized = truthy(Pg('itemized_request'))
    letter_items = ([f['letter_text'] for f in requestable] if itemized else ['the outstanding documents']) if route == 'letter' else []
    # PA-2026-02: write to the representative
    addressee = g(rep, 'name') if truthy(rep) and (truthy(Pg('rep_identity_proof')) or vollmacht_ok) else applicant_name

    # ---------- suggestions from the ARIS case history (exact insurance-number match only)
    SUG = {
        'BENEFIT_TYPE_MISSING': ('benefit type', re.compile(r'benefit|leistung', re.I)),
        'GP_MISSING': ('general practitioner', re.compile(r'(^|_)gp(_|$)|hausarzt|practitioner', re.I)),
        'VOLLMACHT_MISSING': ('power of attorney', re.compile(r'vollmacht|attorney|representative', re.I)),
        'REP_IDENTITY_PROOF_MISSING': ('representative identity proof', re.compile(r'identity|ausweis', re.I)),
        'ADDRESS_MISSING': ('address', re.compile(r'address|anschrift', re.I)),
        'DOB_MISSING': ('date of birth', re.compile(r'birth|geburt', re.I)),
    }
    suggestions = []
    if truthy(g(hist, 'error')):
        add('soft', 'ARIS_HISTORY_UNAVAILABLE', 'case_link', f"ARIS case history could not be read ({s(g(hist, 'error'))}).", None)
    elif truthy(hist) and not truthy(g(hist, 'found')):
        add('info', 'NO_ARIS_HISTORY', 'case_link', f"No earlier case for member number {s(g(hist, 'searched_for'))} in ARIS (exact match on the insurance number).", None)
    elif truthy(g(hist, 'found')):
        app_id = s(g(g(hist, 'case'), 'application_id'))
        add('info', 'ARIS_HISTORY_FOUND', 'case_link', f"Member is known in ARIS: {s(g(hist, 'case_count'))} earlier case(s) for exactly this insurance number, latest {app_id}.", None)
        attrs = or_(g(hist, 'attributes'), {})
        for f in [x for x in hard + soft if x['code'] in SUG]:
            label, rx = SUG[f['code']]
            hits = [(k, v) for k, v in attrs.items() if rx.search(k)]
            if hits:
                val = join([v for _, v in hits], ', ')
                suggestions.append({'item': label, 'value': val, 'source': f'ARIS case {app_id}',
                                    'text': f'{label}: “{val}” is on file in ARIS case {app_id} – suggestion only, confirm with the applicant.'})
            else:
                suggestions.append({'item': label, 'value': None, 'source': f'ARIS case {app_id}',
                                    'text': f'Check earlier ARIS case {app_id} – the {label} may already be on file there (suggestion only).'})
    namesakes = g(hist, 'namesakes')
    if num(namesakes) > 0 if not isinstance(namesakes, str) else lt(0, namesakes):
        add('info', 'NAMESAKES_IGNORED', 'case_link', f'{s(namesakes)} other person(s) with the same name exist in ARIS – ignored, because the case is linked by insurance number.', None)

    # ---------- dispatch after the caseworker's approval
    hist_email = next((v for k, v in or_(g(hist, 'attributes'), {}).items() if re.search('mail', k, re.I) and '@' in s(v)), None)
    hist_email = or_(hist_email, None)
    email = or_(g(a, 'email'), hist_email, None)
    rep_name_raw = g(rep, 'name')
    same = (addressee is UNDEF and rep_name_raw is UNDEF) or (addressee is not UNDEF and rep_name_raw is not UNDEF and addressee == rep_name_raw
                                                              and type(addressee) is type(rep_name_raw))
    postal = g(rep, 'address') if same and truthy(g(rep, 'address')) else g(a, 'address')
    if route == 'escalation':
        cm = cfg['CASE_MANAGER_EMAIL']
        dispatch = {'channel': 'case_manager', 'to': cm,
                    'text': f"After approval the escalation is sent to the Case Manager ({s(cm)}) for phone outreach today{' (per PA-2026-03)' if truthy(Pg('escalation_routing')) else ''}."}
    else:
        if truthy(email) and truthy(postal):
            text = f'After approval the letter can be emailed to {s(email)} or printed and posted to {s(addressee)}, {s(postal)}.'
        elif truthy(email):
            text = f'After approval the letter is emailed to {s(email)}.'
        elif truthy(postal):
            text = f'No email address on file – after approval the letter is printed and posted to {s(addressee)}, {s(postal)}.'
        else:
            text = 'No email or postal address on file – send manually.'
        dispatch = {'channel': 'email' if truthy(email) else 'print' if truthy(postal) else 'none',
                    'options': {'email': truthy(email), 'print': truthy(postal)},
                    'to': or_(cfg['TEST_RECIPIENT'], email) if truthy(email) else None, 'real_email': email,
                    'test_mode': bool(truthy(email) and truthy(cfg['TEST_RECIPIENT'])),
                    'postal_name': addressee, 'postal_address': or_(postal, None), 'text': text}

    # ---------- countdown shown in the queue
    if internal:
        queue_tag = f"⏱ {s(internal['left'])} working days to internal target · {s(statutory_left)} to statutory deadline{' · At risk — internal target' if internal['at_risk'] else ''}"
    else:
        queue_tag = f'⚠ SLA breached {s(-statutory_left)} working days ago' if statutory_left < 0 else f'⏱ {s(statutory_left)} working days to SLA breach'

    # ---------- the rules this agent applied, with their source (shown side by side in the portals)
    policy_view = [
        {'topic': 'GP (Hausarzt) missing', 'rule': 'Hard failure – requested in the letter' if truthy(Pg('gp_mandatory')) else 'Soft warning – not requested', 'basis': g(CITE, 'gp')},
        {'topic': 'Representative', 'rule': 'Vollmacht + identity proof of the representative; letter to the representative' if truthy(Pg('rep_identity_proof')) else 'Vollmacht only',
         'basis': 'PA-2026-02' if truthy(Pg('rep_identity_proof')) else g(CITE, 'representative')},
        {'topic': 'Letter or escalation', 'rule': 'Escalate to the Case Manager on a short track, ≤ 10 days left or a repeat request – never a second letter' if truthy(Pg('escalation_routing')) else 'Outreach letter (a repeat request is flagged to the Team Lead)',
         'basis': g(CITE, 'routing')},
        {'topic': 'Countdown', 'rule': f"Internal target {s(internal['target'])} working days + statutory {s(track)}" if internal else f'Statutory {s(track)} working days only',
         'basis': g(CITE, 'countdown')},
    ]

    missing_list = [f['message_en'] for f in hard]
    if route == 'escalation':
        template_escalation = (f"{queue_tag} — Escalation to the Case Manager for phone outreach today. Condition: {'; '.join(route_reasons)} (per PA-2026-03). "
                               f"Missing: {join([f['letter_text'] for f in requestable], '; ')}. {s(statutory_left)} working days remain on the decision clock.")
    else:
        template_escalation = (f"{queue_tag} — {final_status}: {s(applicant_name)} ({s(or_(g(a, 'insurance_number'), 'no member number'))}, case {s(or_(g(c, 'case_ref'), 'new'))}). "
                               + (f"Missing: {join(missing_list, ' ')} " if hard else 'No blocking items. ')
                               + (f"Escalation Level {s(escalation['level'])} – {s(escalation['owner'])}: {s(escalation['reason'])} " if escalation['level'] else '')
                               + ('Recommended action: review and approve the drafted request.' if letter_items else ''))

    policy_in = c['policy']
    sop_titles = [g(d, 'title') for d in or_(g(policy_in, 'documents'), [])]
    days_to_internal = nn(internal['left'], None) if internal else None
    at_risk_internal = nn(internal.get('at_risk', UNDEF), False) if internal else False
    out = {
        **c,
        'form': {'applicant': a,
                 'representation': {'authorized_person': True, 'name': g(rep, 'name'), 'address': or_(g(rep, 'address'), g(a, 'address'))} if truthy(rep) else {},
                 'benefits_selected': benefits},
        'applicant_name': applicant_name, 'insurance_number': or_(g(a, 'insurance_number'), None), 'application_id': or_(g(c, 'case_ref'), None),
        'gp': or_(g(c, 'gp'), None), 'link_status': 'exact_match' if truthy(g(g(c, 'link'), 'exact')) else 'unlinked',
        'check': {'date': check_date, 'recheck': recheck, 'prior_requests': prior_requests},
        'final_status': final_status, 'hard_failures': hard, 'soft_warnings': soft, 'info': info, 'md_commissioning_allowed': True,
        'route': route, 'route_reasons': route_reasons,
        'sla': {
            'sla_start_date': clock_start, 'sla_start_source': clock_basis, 'received_date': received, 'check_date': check_date, 'elapsed_working_days': elapsed,
            'target_business_days': track, 'paused_working_days': pause_wd, 'pause': pause,
            'sla_deadline_date': decision_due, 'days_to_sla_breach': statutory_left, 'internal_target': internal,
            'decided_on': or_(g(tl, 'decision'), None), 'md_commissioning_deadline': md['due'], 'md_rule': md['rule'], 'md_ordered': md['ordered'], 'md_status': md['status'],
            'urgent_tag': f'Urgent SLA: {s(track)}-Day' if expedited else None, 'queue_tag': queue_tag,
        },
        'suggestions': suggestions, 'dispatch': dispatch, 'policy_view': policy_view,
        'policy': {**policy_in, 'available': len(sop_titles) > 0, 'used': sop_titles, 'escalation': escalation, 'guidance': []},
        'review_context': {
            'llm_input': {
                'output_language': 'German' if cfg['LANG'] == 'de' else 'English', 'insurer': g(c, 'insurer'),
                'addressee': addressee, 'applicant_name': applicant_name, 'representative': or_(g(rep, 'name'), None),
                'insurance_number': g(a, 'insurance_number'), 'case_id': g(c, 'case_ref'),
                'application_status': final_status, 'queue_tag': queue_tag, 'check_date': fmt(check_date), 'recheck': recheck,
                'ROUTE': route, 'ROUTE_REASONS': route_reasons, 'CASE_MANAGER': 'Case Manager' if route == 'escalation' else None,
                'days_to_sla_breach': statutory_left, 'days_to_internal_target': days_to_internal, 'at_risk_internal_target': at_risk_internal,
                'escalation_level': escalation['level'], 'escalation_owner': escalation['owner'],
                'REQUEST_STYLE': 'itemized' if itemized else 'general', 'LETTER_ITEMS': letter_items,
                'MISSING_ITEMS': [f['letter_text'] for f in requestable],
                'findings_with_basis': [f['message'] for f in hard + soft],
                'amendments_in_force': [g(x, 'id') for x in or_(g(policy_in, 'amendments'), [])],
            },
            'lang': cfg['LANG'], 'insurer': g(c, 'insurer'), 'addressee': addressee, 'applicant_name': applicant_name, 'insurance_number': g(a, 'insurance_number'),
            'sla_start_fmt': fmt(clock_start), 'clock_basis': clock_basis, 'days_to_sla_breach': statutory_left, 'escalation_level': escalation['level'],
            'letter_items': letter_items, 'itemized': itemized, 'requestable': requestable if itemized and route == 'letter' else [], 'template_escalation': template_escalation,
        },
    }
    return [_clean(out)]
