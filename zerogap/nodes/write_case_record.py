"""Write Case Record (CRM) (port of Write_Case_Record.js).

ZERO-CONTEXT agent writes its result straight into the case record.
SIMULATED: nothing is written to a real system. The record shows what would have been stored.
"""
from __future__ import annotations


def run(ctx, items: list[dict]) -> list[dict]:
    c = ctx.node('Zero-Context Agent').first()
    zc = c.get('zero_context')
    if not isinstance(zc, dict):
        raise TypeError(f"Cannot read properties of {'null' if zc is None else 'undefined'} (reading 'record_write')")
    w = zc.get('record_write')
    out = {}
    if 'case_ref' in c:
        out['case_ref'] = c['case_ref']
    if 'applicant_name' in c:
        out['applicant_name'] = c['applicant_name']
    out['crm_write'] = dict(w) if isinstance(w, dict) else {}
    return [out]
