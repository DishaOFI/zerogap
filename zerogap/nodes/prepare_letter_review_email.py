"""Prepare Letter & Review Email — takes the AI text as it is (no guardrails) and builds the review email for the
caseworker. Zero-context agent flow.

Python port of the n8n Code node "Prepare Letter & Review Email" (behavioural parity with the JS original).
"""
from __future__ import annotations

import re

from ._renderer import (clean, coalesce, g, index_of, js_slice, js_str, js_trim, json_parse, last_index_of, or_,
                        render_review_email)

_FENCE_START = re.compile(r'^```(json)?', re.I | re.A)
_FENCE_END = re.compile(r'```\Z')            # JS `$` without the m flag = very end of the string
_NON_ID = re.compile(r'[^A-Za-z0-9-]')
_NON_ALPHA = re.compile(r'[^A-Za-z]+')


def _parse_letter(raw):
    try:
        s = js_str(raw)
        s = js_trim(_FENCE_END.sub('', _FENCE_START.sub('', s, count=1), count=1))
        out = json_parse(js_slice(s, index_of(s, '{'), last_index_of(s, '}') + 1))
        if out is None:
            raise ValueError('null has no properties')  # JS: TypeError reading .outreach_letter of null
        return or_(g(out, 'outreach_letter'), '')
    except ValueError:
        return ''


def run(ctx, items: list[dict]) -> list[dict]:
    # Like n8n, `c` is the very object stored as the output of "Zero-Context Agent"; the renderer writes
    # `review_facts` onto it.
    c = ctx.node('Zero-Context Agent').first()
    j = items[0] if items else {}
    raw = coalesce(g(j, 'text'), g(j, 'output'), g(j, 'response'), '')
    letter = _parse_letter(raw)
    if not letter:  # '' (a non-string truthy value is kept as is, like the JS)
        lines = '\n'.join(f'{i + 1}. {js_str(x)}' for i, x in enumerate(g(c, 'review_context', 'letter_items')))
        letter = f"""Dear {js_str(g(c, 'review_context', 'addressee'))},

Regarding the application for long-term care benefits for {js_str(g(c, 'applicant_name'))}: to process the application we require the following documents. Each of them is mandatory:

{lines}

Please send all documents within 7 days.

Kind regards
Your Care Insurance Fund"""

    drafts = {
        'outreach_letter': letter, 'escalation_summary': None, 'draft_source': 'llm',
        'addressee': g(c, 'review_context', 'addressee'),
        'letter_file_name': (f"Draft_Letter_{_NON_ID.sub('', js_str(or_(g(c, 'insurance_number'), 'case')))}_"
                             f"{_NON_ALPHA.sub('_', js_str(or_(g(c, 'applicant_name'), '')))}.pdf"),
    }
    approval_html = render_review_email(c, {**drafts, 'actions': {'approveUrl': '__RESUME_URL__?decision=approve',
                                                                  'rejectUrl': '__RESUME_URL__?decision=reject'}})
    return [clean({**c, 'drafts': drafts, 'approval_html': approval_html})]
