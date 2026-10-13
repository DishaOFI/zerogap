"""Guardrails: Validate AI Draft.

Output guardrails: valid JSON, output language (LANG, currently English), every required item requested,
no decisions/amounts/IBAN/links/foreign IDs, escalation carries the SLA countdown.
Any failure -> deterministic template letter (the run never stalls on the LLM).

Python port of the n8n Code node "Guardrails: Validate AI Draft" (behavioural parity with the JS original).
"""
from __future__ import annotations

import re

from ._renderer import (JS_WS, UNDEF, clean, coalesce, g, index_of, join, js_slice, js_str, js_trim, json_parse,
                        last_index_of, or_, render_review_email, strict_eq, to_number, truthy, utf16_len)

# JS regexes without the `u` flag: \b / \w are ASCII-only and /i folds ASCII only -> re.ASCII.
_FENCE_START = re.compile(r'^```(json)?', re.I | re.A)
_FENCE_END = re.compile(r'```\Z')            # JS `$` without the m flag = very end of the string
_DE_WORDS = re.compile(r'\b(und|der|die|das|Ihr|Ihre|bitte|wir|Sie|Antrag|Pflege)\b', re.A)
_EN_WORDS = re.compile(r'\b(the|and|your|please|we|application|care|you|of|to)\b', re.I | re.A)
_DECISION = re.compile(
    r'abgelehnt|ablehnung|bewilligt|genehmigt|pflegegrad' + JS_WS + r'*[1-5]|\b(rejected|denied|declined|approved|granted)\b'
    r'|care grade' + JS_WS + r'*[1-5]|€|\beuro?s?\b|\bEUR\b', re.I | re.A)
_IBAN = re.compile(r'\bDE\d{2}(?:' + JS_WS + r'|\d){12,}', re.A)
_LINKS = re.compile(r'https?://|www\.', re.I | re.A)
_IDS = re.compile(r'\b(IP-\d{6}|[A-Z]\d{9})\b', re.A)
_FIRST_CONTACT = re.compile(r'first contact')
_NON_ID = re.compile(r'[^A-Za-z0-9-]')
_NON_ALPHA = re.compile(r'[^A-Za-z]+')


def template_letter(c: dict) -> str:
    items = '\n'.join(f'{i + 1}. {js_str(t)}' for i, t in enumerate(g(c, 'letter_items')))
    applicant_name = js_str(g(c, 'applicant_name'))
    sla_start_fmt = js_str(g(c, 'sla_start_fmt'))
    addressee = g(c, 'addressee')
    if g(c, 'lang') == 'de':
        return f"""Sehr geehrte/r {js_str(or_(addressee, 'Damen und Herren'))},

vielen Dank für Ihren Antrag auf Leistungen der Pflegeversicherung für {applicant_name} (Versichertennummer {js_str(or_(g(c, 'insurance_number'), 'unbekannt'))}). Ihr Antrag gilt ab dem Tag Ihrer ersten Kontaktaufnahme ({sla_start_fmt}) als gestellt.

Damit wir Ihren Antrag zügig bearbeiten und die Begutachtung durch den Medizinischen Dienst veranlassen können, benötigen wir noch folgende Angaben bzw. Unterlagen:

{items}

Bitte senden Sie uns die fehlenden Angaben möglichst umgehend zu. Bei Fragen erreichen Sie uns gerne telefonisch.

Mit freundlichen Grüßen
Ihre Pflegekasse"""
    filed = (f' Your application is considered filed as of {sla_start_fmt}.'
             if _FIRST_CONTACT.search(js_str(or_(g(c, 'clock_basis'), 'first contact'))) else '')
    if g(c, 'itemized') is False:
        ask = 'So that we can continue processing the application, please send us the outstanding documents as soon as possible.'
    else:
        ask = ('So that we can process the application promptly and arrange the assessment by the Medical Service, '
               f'we still need the following information or documents:\n\n{items}\n\n'
               'Please send us the missing information as soon as possible.')
    thanks = ('your application for long-term care insurance benefits' if strict_eq(addressee, g(c, 'applicant_name'))
              else 'the application for long-term care insurance benefits for ' + applicant_name)
    return f"""Dear {js_str(or_(addressee, 'Sir or Madam'))},

Thank you for {thanks} (member number {js_str(or_(g(c, 'insurance_number'), 'unknown'))}).{filed}

{ask} If you have any questions, please do not hesitate to call us.

Kind regards
{js_str(or_(g(c, 'insurer'), 'Your Care Insurance Fund'))}"""


def guardrail_check(llm_raw, c: dict) -> dict:
    issues: list[str] = []
    de = g(c, 'lang') == 'de'
    out = UNDEF
    try:
        s = js_str(or_(llm_raw, ''))
        s = _FENCE_START.sub('', s, count=1)
        s = js_trim(_FENCE_END.sub('', s, count=1))
        out = json_parse(js_slice(s, index_of(s, '{'), last_index_of(s, '}') + 1))
    except ValueError:
        issues.append('LLM output is not valid JSON')
    parsed = out is not UNDEF

    need_letter = len(g(c, 'letter_items')) > 0
    letter = or_(g(out, 'outreach_letter'), '') if need_letter else ''
    esc = or_(g(out, 'escalation_summary'), '')
    if parsed and need_letter:
        n = utf16_len(letter)
        if n < 200 or n > 4000:
            issues.append('Letter length outside 200–4000 characters')
        # Output language (Req #5 – currently English, switch LANG to 'de' for German)
        de_hits = len(_DE_WORDS.findall(letter))
        en_hits = len(_EN_WORDS.findall(letter))
        if (de_hits < 8 or en_hits > de_hits / 4) if de else (en_hits < 8 or de_hits > en_hits / 4):
            issues.append(f"Letter is not (mainly) in {'German' if de else 'English'}")
        # Coverage: every item that must be requested is mentioned
        low = letter.lower()
        for f in g(c, 'requestable'):
            kws = g(f, 'letter_keywords')
            if len(kws) and not any(js_str(k).lower() in low for k in kws):
                issues.append(f"Letter does not request: {js_str(g(f, 'code'))}")
        # No eligibility decisions, no amounts, no sensitive data leakage
        if _DECISION.search(letter):
            issues.append('Letter contains a decision or money amount (not allowed)')
        if _IBAN.search(letter):
            issues.append('Letter contains an IBAN')
        if _LINKS.search(letter):
            issues.append('Letter contains links')
        foreign = [m.group(0) for m in _IDS.finditer(letter) if not strict_eq(m.group(0), g(c, 'insurance_number'))]
        if foreign:
            issues.append("Letter contains another person's insurance number")
    days = to_number(g(c, 'days_to_sla_breach'))
    if parsed and (not truthy(esc) or js_str(abs(days)) not in esc):
        issues.append('Escalation summary is missing the SLA countdown')
    lvl = g(c, 'escalation_level')
    if parsed and truthy(lvl) and ('Level ' + js_str(lvl)) not in js_str(or_(esc, '')):
        issues.append(f'Escalation summary does not state escalation Level {js_str(lvl)} (ARIS Deadline Escalation Procedure)')
    ok = len(issues) == 0
    return {
        'guardrail_passed': ok,
        'guardrail_issues': issues,
        'draft_source': 'llm' if ok else 'template',
        'outreach_letter': '' if not need_letter else letter if ok else template_letter(c),
        'escalation_summary': esc if ok else g(c, 'template_escalation'),
    }


def _file_part(v) -> str:
    return _NON_ID.sub('', js_str(v))


def run(ctx, items: list[dict]) -> list[dict]:
    # Like n8n, `base` is the very object stored as the output of "Compliance Controls (ARIS)"; the renderer
    # writes `review_facts` onto it.
    base = ctx.node('Compliance Controls (ARIS)').first()
    j = items[0] if items else {}
    llm_raw = coalesce(g(j, 'text'), g(j, 'output'), g(j, 'response'), '')
    d = guardrail_check(llm_raw, g(base, 'review_context'))

    route = g(base, 'route')
    case_part = or_(g(base, 'application_id'), g(base, 'insurance_number'), 'case')
    if route == 'none':
        letter_file_name = f'Release_for_Assessment_{_file_part(case_part)}.pdf'
    elif route == 'escalation':
        letter_file_name = f'Escalation_Note_{_file_part(case_part)}.pdf'
    elif truthy(d['outreach_letter']):
        letter_file_name = (f"Draft_Letter_{_file_part(or_(g(base, 'insurance_number'), 'unknown'))}_"
                            f"{_NON_ALPHA.sub('_', js_str(or_(g(base, 'applicant_name'), '')))}.pdf")
    else:
        letter_file_name = None
    d.update({'letter_file_name': letter_file_name, 'addressee': g(base, 'review_context', 'addressee')})
    approval_html = render_review_email(base, {
        **d,
        'actions': {'approveLabel': 'Approve', 'rejectLabel': 'Reject',
                    'approveUrl': '__RESUME_URL__?decision=approve', 'rejectUrl': '__RESUME_URL__?decision=reject'},
    })
    return [clean({**base, 'drafts': d, 'approval_html': approval_html})]
