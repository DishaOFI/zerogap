"""Compliance Controls (ARIS) — records, for every risk in the risk catalogue, how the ARIS-context agent avoided it
on this run, with evidence from the run itself. Same rows as the zero-context agent's register.

Python port of the n8n Code node "Compliance Controls (ARIS)" (behavioural parity with the JS original).
"""
from __future__ import annotations

import re

from ._renderer import clean, g, join, js_str, or_, truthy

_H_PREFIX = re.compile(r'^H_')


def _unique(values: list) -> list:
    """[...new Set(values)] — SameValueZero for primitives, identity for objects."""
    out: list = []
    for v in values:
        if isinstance(v, (dict, list)):
            if not any(v is x for x in out):
                out.append(v)
        elif not any(type(v) is type(x) and v == x for x in out):
            out.append(v)
    return out


def run(ctx, items: list[dict]) -> list[dict]:
    c = items[0] if items else {}
    a = or_(g(c, 'applicant'), {})
    h = or_(g(c, 'aris_history'), {})
    try:
        q = ctx.node('Build ARIS History Query').first()
    except KeyError:
        q = None
    field_scripts = or_(g(q, 'query_body', 'query', 'table', 'fieldScripts'), [])
    aliases = [g(f, 'alias') for f in field_scripts]
    cols = [x for x in aliases if not _H_PREFIX.search(js_str(x))]
    findings = list(or_(g(c, 'hard_failures'), [])) + list(or_(g(c, 'soft_warnings'), []))
    req = [f for f in findings if truthy(g(f, 'requestable'))]
    cites = _unique([b for b in (g(f, 'basis') for f in findings) if truthy(b)])

    def R(id_, evidence):
        return {'id': id_, 'occurred': False, 'evidence': evidence}

    ins = js_str(g(a, 'insurance_number'))
    namesakes = g(h, 'namesakes')
    suggestions = or_(g(c, 'suggestions'), [])
    found = truthy(g(h, 'found'))
    case_id = js_str(g(h, 'case', 'application_id'))
    cols_txt = (' (' + join(cols[:6], ', ') + (', …' if len(cols) > 6 else '') + ')') if cols else ''
    risks = [
        R('C1', f"Case linked by exact insurance number {ins}"
                f"{f' – {js_str(namesakes)} other person(s) with the same name in ARIS were ignored' if truthy(namesakes) else ''}."),
        R('C2', f'Only history of insurance number {ins} used (case {case_id}).' if found
          else f'No earlier case for {ins} in ARIS – no history used.'),
        R('C3', f'ARIS query returns one aggregated row with {len(cols)} named process fields for this insurance '
                f'number only{cols_txt}.'),
        R('C4', f"{len(suggestions)} value(s) from ARIS shown as suggestions only – nothing copied into the claim."
          if len(suggestions) else 'Nothing copied from previous cases.'),
        R('M1', f"Only items required by the rulebook are missing: {join([g(f, 'code') for f in req], ', ') or 'none'} "
                f"({join(cites, '; ')})."),
        R('R1', f"Requests only the required items: "
                f"{join(or_(g(c, 'review_context', 'llm_input', 'MISSING_ITEMS'), []), '; ') or 'none'}."),
        R('R2', 'Every finding cites its governing document (SOP section or policy amendment).'),
        R('R3', 'Escalation waits for caseworker approval before it goes to the Case Manager.'
          if g(c, 'route') == 'escalation' else 'Letter waits for caseworker approval (Approve / Reject).'),
        R('W1', 'No missing value is invented – missing items are requested or escalated.'),
        R('W2', 'Empty fields stay empty until the applicant provides them.'),
        R('W3', 'Suggestions are labelled “suggestion – confirm with the applicant”.'),
        R('W4', 'The agent writes nothing to the case record.'),
        R('W5', 'No existing value is changed.'),
        R('W6', 'The AI draft is checked by guardrails and approved by a caseworker before use.'),
        R('W7', 'Historical case data in ARIS is read-only for the agent.'),
        R('W8', 'Benefit, care level and payment data are not touched.'),
        R('W9', 'Every value keeps its source (see provenance).'),
        R('W10', 'Provenance recorded for every value: application form, ARIS document, ARIS process data or AI draft.'),
    ]

    # [first last].join(' ') is never empty (' ' when both are missing) and therefore always kept
    who = [join([g(a, 'first_name'), g(a, 'last_name')], ' '), g(a, 'date_of_birth'), g(a, 'address'),
           g(a, 'insurance_number')]
    provenance = [{'field': 'Applicant, date of birth, address, member number',
                   'value': join([x for x in who if truthy(x)], ' · '),
                   'source': 'Application form (ARIS document)'}]
    rep = g(c, 'representative')
    if truthy(rep):
        provenance.append({'field': 'Representative', 'value': g(rep, 'name'), 'source': 'Application form (ARIS document)'})
    vm = g(c, 'vollmacht')
    if truthy(vm):
        rec = g(vm, 'received_on')
        provenance.append({'field': 'Vollmacht', 'value': 'received ' + js_str(rec) if truthy(rec) else 'on file',
                           'source': 'ARIS document'})
    provenance.append({'field': 'Case history',
                       'value': f"{js_str(g(h, 'case_count'))} case(s), latest {case_id}" if found else 'none',
                       'source': 'ARIS process data (exact insurance-number match)'})
    for s in suggestions:
        provenance.append({'field': g(s, 'item'), 'value': or_(g(s, 'value'), 'see earlier case'),
                           'source': f"{js_str(g(s, 'source'))} – suggestion, not written"})
    provenance.append({'field': 'Escalation note' if g(c, 'route') == 'escalation' else 'Outreach letter',
                       'value': 'drafted', 'source': 'AI draft – guardrails + caseworker approval'})
    out = {**c, 'risk_register': {'agent': 'aris', 'risks': risks}, 'provenance': provenance}
    return [clean(out)]

