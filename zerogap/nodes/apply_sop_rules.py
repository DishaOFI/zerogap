"""Apply SOP Rules — reads the rulebook the agent was given and switches each control on or off
based on what the SOP text actually says. Identical code in both flows:
  WITH ARIS  -> SOPs come from the governed ARIS repository (MCP)
  WITHOUT    -> SOPs come from a document folder (Google Drive)
so any difference in behaviour comes only from the rulebook, never from the code.

Python port of the n8n Code node "Apply SOP Rules" (behavioural parity with the JS original).
"""
from __future__ import annotations

import re
import unicodedata

# JS `\s` (ECMAScript WhiteSpace + LineTerminator); Python's str.isspace() differs (e.g. \x1c-\x1f, \x85, ﻿).
_JS_WS = set('\t\n\v\f\r       　﻿') | {chr(c) for c in range(0x2000, 0x200B)}
_JS_WS_RUN = re.compile('[' + ''.join(re.escape(c) for c in sorted(_JS_WS)) + ']+')


def _keep(ch: str) -> bool:
    # /[^\p{L}\p{N}\s]/u — keep letters, numbers and whitespace
    return ch in _JS_WS or unicodedata.category(ch)[0] in ('L', 'N')


def _js_trim(s: str) -> str:
    i, j = 0, len(s)
    while i < j and s[i] in _JS_WS:
        i += 1
    while j > i and s[j - 1] in _JS_WS:
        j -= 1
    return s[i:j]


def _js_str(t) -> str:
    # String(t || '')
    if t is None or t is False or t == '' or t == 0:
        return ''
    if t is True:
        return 'true'
    return str(t)


def _slice16(s: str, end: int) -> str:
    """String.prototype.slice(0, end) — counts UTF-16 code units, not code points."""
    n = 0
    for i, ch in enumerate(s):
        w = 2 if ord(ch) > 0xFFFF else 1
        if n + w > end:
            # a cut through a surrogate pair keeps a lone high surrogate in JS; it never matches anything ASCII
            return s[:i] + ('\ud800' if n < end else '')
        n += w
    return s


# ARIS returns SOP text with punctuation stripped ("3workingday", "Deadline Escalation Procedure", "70 of the …"),
# Drive keeps it. Both are normalised the same way (punctuation removed, no space inserted) before matching.
def norm(t) -> str:
    s = _js_str(t).lower()
    s = ''.join(ch for ch in s if _keep(ch))
    s = _JS_WS_RUN.sub(' ', s)
    return ' ' + _js_trim(s) + ' '


def has(text: str, *phrases: str) -> bool:
    return any(_js_trim(norm(p)) in text for p in phrases)


CATALOG = [
    {'key': 'agent', 'p': ['zero gap agent completeness check operating procedure', 'zero gap agent operating procedure'], 'title': 'Zero Gap Agent Operating Procedure'},
    {'key': 'completeness', 'p': ['completeness check & missing-information work instruction', 'completeness check work instruction'], 'title': 'Completeness Check & Missing-Information Work Instruction'},
    {'key': 'deadline', 'p': ['deadline & escalation procedure', 'deadline escalation procedure'], 'title': 'Deadline & Escalation Procedure'},
    {'key': 'governance', 'p': ['operational governance & control policy', 'operational governance control policy'], 'title': 'Operational Governance & Control Policy'},
    {'key': 'handover', 'p': ['case handover & ownership protocol', 'case handover ownership protocol'], 'title': 'Case Handover & Ownership Protocol'},
]

# ---- Policy amendments (PA-…): published in the repository, they take precedence over the base SOPs.
# The agent is never reconfigured: whatever amendments the rulebook contains on this run are applied.
AMENDMENTS = [
    {'id': 'PA-2026-01', 'name': 'GP contact becomes mandatory', 'key': 'gp_mandatory', 'p': ['gp contact becomes mandatory'], 'effect': 'Missing GP (Hausarzt) is a hard failure and is requested in the letter.'},
    {'id': 'PA-2026-02', 'name': 'Identity proof for authorized representatives', 'key': 'rep_identity_proof', 'p': ['identity proof for authorized representatives', 'identity proof for authorised representatives'], 'effect': 'A named representative must also prove their own identity; the letter goes to the representative.'},
    {'id': 'PA-2026-03', 'name': 'Escalate instead of writing', 'key': 'escalation_routing', 'p': ['when to escalate instead of writing'], 'effect': 'Escalate to the Case Manager (phone) on a 5/10-day track, with 10 or fewer working days left, or when a request was already sent – never a second letter.'},
    {'id': 'PA-2026-04', 'name': 'Internal 20-day service target', 'key': 'internal_target', 'p': ['internal 20day service target', 'internal service target'], 'effect': 'Countdown against an internal 20-working-day target; "At risk" at 5 or fewer days; statutory deadline still shown.'},
]

# JS regexes without the u flag: \d is ASCII-only
_MISSING = object()  # stands in for JS `undefined` (key omitted from the JSON output)
_PA_HEAD = re.compile(r'policy amendment pa ?(\d{4}) ?(\d{2})', re.ASCII)
_PA_TEST = re.compile(r'policy amendment pa ?\d{4} ?\d{2}', re.ASCII)


def _truthy(v) -> bool:
    return not (v is None or v is False or v == '' or (isinstance(v, (int, float)) and not isinstance(v, bool) and (v == 0 or v != v)))


def run(ctx, items: list[dict]) -> list[dict]:
    sop_items = ctx.node('Read SOP Text').all()
    sops = [d for d in sop_items if _truthy(d.get('readable'))]

    docs = []
    for d in sops:
        n = norm(d.get('text'))
        pa = _PA_HEAD.search(_slice16(n, 200))
        if pa:
            docs.append({'key': 'amendment', 'title': f'PA-{pa.group(1)}-{pa.group(2)}', 'source': d.get('name', _MISSING), 'n': n})
            continue
        head = _slice16(n, 250)
        c = next((x for x in CATALOG if has(head, *x['p'])), None)
        docs.append({'key': c['key'] if c else 'other', 'title': c['title'] if c else d.get('name', _MISSING), 'source': d.get('name', _MISSING), 'n': n})

    def T(k):
        return ' '.join(d['n'] for d in docs if d['key'] == k)

    ALL = ' '.join(d['n'] for d in docs)

    def where(*phrases):
        # undefined inside an array serialises as null in JSON
        return [None if d['title'] is _MISSING else d['title'] for d in docs if has(d['n'], *phrases)]

    # Each control is ON when its governing sentence is present in the rulebook (phrases taken from the original SOPs).
    R = [
        {'id': 1, 'key': 'vollmacht_required', 'name': 'Representative needs a matching Vollmacht', 'in': ALL,
         'p': ['proof of authority (Vollmacht)', 'a matching power of attorney or court decree']},
        {'id': 2, 'key': 'benefit_type_required', 'name': 'Benefit type must be selected (care-provider details if applicable)', 'in': T('agent'),
         'p': ['No selection: soft warning for the 3-day MD commissioning deadline', 'without a selected benefit type']},
        {'id': 3, 'key': 'civil_servant_check', 'name': 'Civil servant → Beihilfestelle check', 'in': ALL, 'p': ['Beihilfestelle']},
        {'id': 4, 'key': 'quarantine_assumed_link', 'name': 'Medical documents on a probabilistic link are quarantined', 'in': ALL,
         'p': ['strictly quarantined', 'quarantined for human verification']},
        {'id': 5, 'key': 'expedited_deadlines', 'name': 'Expedited 5 / 10-working-day deadlines', 'in': ALL,
         'p': ['in hospital or an inpatient rehabilitation facility', 'Urgent SLA: 5-Day/10-Day', 'Deadline-type indicator']},
        {'id': 6, 'key': 'clock_from_first_contact', 'name': 'Decision clock starts at first contact (receipt), regardless of completeness', 'in': ALL,
         'p': ['regardless of completeness', 'clock starts at first contact', 'starting at first applicant contact']},
        {'id': 7, 'key': 'pause_from_applicant_receipt', 'name': 'Clock pauses only when the applicant receives the request', 'in': ALL,
         'p': ['paused only from the day the applicant receives', 'paused from the day this request is received by the applicant']},
        {'id': 8, 'key': 'md_within_3_days', 'name': 'Commission the Medical Service within 3 working days of receipt', 'in': ALL,
         'p': ['commission the Medical Service within 3 working days', '3-working-day Medical Service commissioning', '3-working-day commissioning']},
        {'id': 9, 'key': 'human_signoff', 'name': 'No letter or escalation leaves without human sign-off', 'in': ALL,
         'p': ['without human review and sign-off', 'does not send it unattended', 'never transmitted to the applicant unattended']},
        {'id': 10, 'key': 'itemized_request', 'name': 'Request lists every missing item; repeat request → Team Lead', 'in': ALL,
         'p': ['listing precisely which mandatory fields or documents are outstanding']},
        {'id': 11, 'key': 'early_warning', 'name': 'Early warning: Level 1 at 70 %, Level 2 on 3-day / non-response risk', 'in': T('deadline'),
         'p': ['approaching 70% of the applicable decision deadline', 'commissioning window is at risk']},
    ]
    rules = []
    for r in R:
        on = has(r['in'], *r['p'])
        rules.append({'id': r['id'], 'key': r['key'], 'name': r['name'], 'in_rulebook': on, 'found_in': where(*r['p']) if on else []})
    P: dict = {r['key']: r['in_rulebook'] for r in rules}
    P['repeat_request_team_lead'] = has(ALL, 'a repeat request is required for the same case')
    P['assumed_link_flag'] = has(ALL, 'assumed link')
    m = re.search(r'average time to issue missinginformation request.{0,300}?illustrative (\d+) working day', T('governance'), re.ASCII)
    P['dispatch_kpi_days'] = int(m.group(1)) if m else None

    deadline_text = T('deadline')

    def num(rx, d):
        mm = re.search(rx, deadline_text, re.ASCII)
        return int(mm.group(1)) if mm else d

    P['decision_days'] = num(r' (\d+)workingday decision deadline', 25)
    P['level1_pct'] = num(r'approaching (\d+) of the applicable', 70) if P['early_warning'] else None
    P['penalty_eur_per_week'] = num(r'(\d+) for every started week', 70)

    amend_docs = [d for d in docs if _PA_TEST.search(_slice16(d['n'], 200))]
    amendments = [{'id': a['id'], 'title': f"{a['id']} – {a['name']}", 'effect': a['effect'], 'key': a['key']}
                  for a in AMENDMENTS if any(has(_slice16(d['n'], 300), *a['p']) for d in amend_docs)]
    for a in AMENDMENTS:
        P[a['key']] = any(x['id'] == a['id'] for x in amendments)
    A = ' '.join(d['n'] for d in amend_docs)

    def anum(rx, d):
        mm = re.search(rx, A, re.ASCII)
        return int(mm.group(1)) if mm else d

    P['internal_target_days'] = anum(r'internal service target of (\d+) working days', 20) if P['internal_target'] else None
    P['at_risk_days'] = anum(r'when (\d+) or fewer working days remain to the internal target', 5) if P['internal_target'] else None
    P['escalate_when_days_left'] = anum(r'(\d+) or fewer working days remain on the decision clock', 10) if P['escalation_routing'] else None
    # which document governs each rule (shown as a citation next to every finding)
    P['cite'] = {
        'gp': 'PA-2026-01' if P['gp_mandatory'] else 'Operating Procedure §3 (Medical Contacts)',
        'representative': 'Operating Procedure §3 (Legal Representation)',
        'rep_identity': 'PA-2026-02' if P['rep_identity_proof'] else None,
        'benefit': 'Operating Procedure §3 (Type of Benefit)',
        'routing': 'PA-2026-03' if P['escalation_routing'] else 'Work Instruction §5/§7',
        'countdown': 'PA-2026-04' if P['internal_target'] else 'Deadline & Escalation Procedure §3',
        'signature': 'Operating Procedure §3 (Execution)',
    }

    missing = [c['title'] for c in CATALOG if not any(d['key'] == c['key'] for d in docs)]
    # two versions of the same SOP in one folder would mix rulebooks — report it so it can be cleaned up
    duplicates = [c['title'] for c in CATALOG if sum(1 for d in docs if d['key'] == c['key']) > 1]

    def _doc_out(d):
        o = {'title': d['title'], 'source': d['source']}
        # a missing `name` is `undefined` in JS and vanishes from the JSON
        return {k: v for k, v in o.items() if v is not _MISSING}

    first = items[0] if items else {}
    return [{
        **first,  # case file (+ ARIS case history in the ARIS flow)
        'policy': {
            'source': 'ARIS repository (MCP)' if any(isinstance(i, dict) and i.get('origin') == 'aris' for i in sop_items) else 'document folder (Google Drive)',
            'documents': [_doc_out(d) for d in docs],
            'missing_documents': missing, 'duplicate_documents': duplicates,
            'rules': rules, 'switches': P, 'amendments': amendments,
        },
    }]

