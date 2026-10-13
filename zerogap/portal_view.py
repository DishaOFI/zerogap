"""Portal status data – Python version of the n8n node "Build Caseworker View" (same JSON shape), built from the
runs stored in SQLite instead of the n8n executions API."""
from __future__ import annotations
from .db import DB

CATALOGUE = {
    'C1': ['Collection', 'Context from previous cases involving the wrong person', 'Privacy breach, disclosure of personal data, incorrect case association'],
    'C2': ['Collection', 'Uses information from previous cases that is not relevant to the current claim', 'Data protection, purpose limitation, unauthorised use of personal data'],
    'C3': ['Collection', 'Collects all customer data, even when only specific data is relevant', 'Data protection, purpose limitation, data minimisation'],
    'C4': ['Collection', 'Automatically copies information from previous cases into the current claim', 'Incorrect data, data integrity, wrong-person / wrong-case association'],
    'M1': ['Missing', 'Marks information as missing although it is not required for this claim', 'Unnecessary data collection, disproportionate information requests'],
    'R1': ['Request', 'Requests sensitive documents that are not necessary', 'Data minimisation, proportionality, protection of sensitive personal data'],
    'R2': ['Request', 'Uses an incorrect justification or calls a document mandatory when it is not', 'Misleading customer communication, incorrect legal/business representation'],
    'R3': ['Request', 'Automatically sends the generated letter to the customer', 'Lack of human oversight, inappropriate customer communication'],
    'W1': ['Writing', 'Invents missing information and enters it into the claim', 'Fabrication of customer/claim data; inaccurate records'],
    'W2': ['Writing', 'Fills missing fields with assumptions or AI-generated values', 'False or misleading records; data integrity risk'],
    'W3': ['Writing', 'Presents inferred information as verified facts', 'Misrepresentation; incorrect claims handling'],
    'W4': ['Writing', 'Modifies existing claim or customer data without authorisation', 'Unauthorised data alteration; governance and data integrity breach'],
    'W5': ['Writing', 'Overwrites correct data with AI-generated information', 'Loss of data integrity; incorrect customer record'],
    'W6': ['Writing', 'Writes AI-generated information into the system of record without human validation', 'Lack of human oversight; inadequate control'],
    'W7': ['Writing', 'Changes historical claim information without preserving the original record', 'Loss of audit trail; lack of traceability'],
    'W8': ['Writing', 'Modifies data that affects coverage, liability, reserve, payment or fraud assessment', 'Material claims-handling, financial and regulatory risk'],
    'W9': ['Writing', 'Creates information that appears to have been provided by the customer', 'False customer record; misleading claims documentation'],
    'W10': ['Writing', 'Cannot distinguish source data, inferred data and AI-generated data', 'Lack of provenance, explainability and auditability'],
}
DOCS_ARIS = ['Build Case Document Searches', 'Search Case Documents', 'Collect Case Document IDs', 'MCP: Get Case Document Text', 'Read Case Document Text']
DOCS_DRIVE = ['List Case Documents', 'Keep Case Documents Only', 'Get Case Document (Google Docs)', 'Convert to Google Docs & Sheets', 'Read Case Document Text']
SOP_ARIS = ['Build SOP Searches', 'Search SOPs and Amendments', 'Collect SOP Document IDs', 'MCP: Get SOP Text', 'Read SOP Text']
SOP_DRIVE = ['List SOP Documents', 'Keep SOP Documents Only', 'Get SOP Document (Google Docs)', 'Read SOP Text']
DRAFT = ['AI: Draft Review Summary & Letter', 'Guardrails: Validate AI Draft', 'Create Letter PDF']
DECISION = ['APPROVED: Release for Dispatch', 'REJECTED: Manual Handling']
DISPATCH = ['Send Letter or Escalation', 'Print Letter / Release (no email)']
TL = [('received', 'Application received (Antrag)'), ('md_order', 'Assessment order sent to the Medical Service'), ('request_dispatched', 'Missing-information request sent'),
      ('confirmation_letter', 'Confirmation letter sent'), ('resubmission_received', 'Nachreichung received'), ('gutachten_received', 'Gutachten received'),
      ('decision', 'Care-level decision (Bescheid)'), ('objection', 'Objection (Widerspruch)')]


def _case(run: dict, steps: list[dict]) -> dict:
    rd: dict[str, list[dict]] = {}
    for s in steps:
        rd.setdefault(s['node'], []).append(s)
    ran = lambda n: n in rd  # noqa: E731
    out = lambda n: (rd[n][-1]['output'] or [None])[0] if n in rd and rd[n][-1]['output'] else None  # noqa: E731
    ms = lambda names: sum(s['duration_ms'] or 0 for n in names for s in rd.get(n, []))  # noqa: E731
    zero = run['agent'] == 'zero'
    hist = (['Search Event Log by Name', 'Look Up Applicant in Event Log'] if zero else ['Build ARIS History Query', 'MCP: Query Case History', 'ARIS Case History'])
    check = ['Completeness Rules Engine', 'Zero-Context Agent'] if zero else ['Completeness Rules Engine', 'Compliance Controls (ARIS)']
    draft = ['Write Case Record (CRM)', 'AI: Draft Letter', 'Prepare Letter & Review Email', 'Create Letter PDF'] if zero else DRAFT
    stages = [
        ('received', 'Trigger received', 'New application received – starting the case review.', ['Gmail Trigger', 'Extract Application PDF'], ['Gmail Trigger']),
        ('docs', 'Reading documents', 'Loading the case documents.', DOCS_DRIVE if zero else DOCS_ARIS, ['Read Case Document Text']),
        ('sops', 'Reading SOPs', 'Loading the SOPs and policies.', SOP_DRIVE if zero else SOP_ARIS, ['Read SOP Text']),
        ('casefile', 'Building the case file', 'Reading applicant, representative and dates from the documents.', ['Build Case File'], ['Build Case File']),
        ('history', 'Checking the case history', 'Looking up earlier cases of the applicant.', hist, [hist[-1]]),
        ('rules', 'Applying the rules', 'Applying the SOP and policy rules to the case.', ['Apply SOP Rules'], ['Apply SOP Rules']),
        ('check', 'Completeness check', 'Checking the application and the deadlines.', check, [check[1]]),
        ('prepare', 'Drafting the outreach', 'Drafting the letter or the escalation.', draft, ['Create Letter PDF']),
        ('review', 'Caseworker review', 'Waiting for the caseworker to approve or reject.', ['Send Review Email (letter attached)', 'Wait for Caseworker Decision'], DECISION),
        ('decision', 'Decision recorded', 'The caseworker’s decision is stored with the case.', DECISION, DECISION),
    ]
    if not ran('REJECTED: Manual Handling'):
        stages.append(('dispatch', 'Dispatch', 'Letter emailed or printed, or escalation sent to the Case Manager.', ['Prepare Dispatch', *DISPATCH], DISPATCH))
    status = run['status']
    active_found, step_list = False, []
    for key, title, text, nodes, done_nodes in stages:
        done = any(ran(n) for n in done_nodes)
        state = 'done' if done else 'pending'
        if not done and not active_found and status in ('running', 'new', 'waiting', 'error', 'crashed'):
            active_found = True
            state = 'error' if status in ('error', 'crashed') else 'active'
        step_list.append({'key': key, 'title': title, 'text': text, 'state': state, 'duration_ms': ms(nodes)})

    r = out('Zero-Context Agent' if zero else 'Compliance Controls (ARIS)') or out('Completeness Rules Engine')
    pd, cf = out('Prepare Dispatch'), out('Build Case File')
    dn = out('Prepare Letter & Review Email' if zero else 'Guardrails: Validate AI Draft')
    drafts = (dn or {}).get('drafts') or {}
    a = (r or {}).get('applicant') or (cf or {}).get('applicant') or {}
    tl = (r or {}).get('timeline') or (cf or {}).get('timeline') or {}
    decision_node = next((n for n in DECISION if ran(n)), None)
    review = None
    if r and r.get('final_status'):
        s = r.get('sla') or {}
        rep = r.get('representative')
        review = {
            'agent': 'zero' if zero else 'aris', 'final_status': r['final_status'], 'case_ref': r.get('application_id'), 'date_of_birth': a.get('date_of_birth'),
            'representative': f"{rep['name']}{' (' + rep['relationship'] + ')' if rep.get('relationship') else ''}" if rep else None,
            'check_date': (r.get('check') or {}).get('date'), 'recheck': (r.get('check') or {}).get('recheck'),
            'route': 'letter' if zero else r.get('route'), 'route_reasons': r.get('route_reasons') or [],
            'sla': {'statutory_left': s.get('days_to_sla_breach'), 'deadline': s.get('sla_deadline_date'), 'internal': s.get('internal_target'),
                    'start': s.get('sla_start_date'), 'basis': s.get('sla_start_source')},
            'escalation': (r.get('policy') or {}).get('escalation'),
            'blocking': [f.get('message') for f in r.get('hard_failures') or []], 'checks': [f.get('message') for f in r.get('soft_warnings') or []],
            'notes': [f.get('message') for f in r.get('info') or []],
            'policy': {'source': 'SOPs' if zero else 'SOPs and policy amendments', 'documents': [d.get('title') for d in (r.get('policy') or {}).get('documents') or []],
                       'amendments': (r.get('policy') or {}).get('amendments') or []},
            'policy_view': r.get('policy_view') or [],
            'suggestions': [] if zero else [x.get('text') for x in r.get('suggestions') or []],
            'provenance': r.get('provenance') or [],
            'risks': [{**x, 'group': CATALOGUE.get(x.get('id'), [None])[0], 'title': CATALOGUE.get(x.get('id'), [None, None])[1],
                       'issue': CATALOGUE.get(x.get('id'), [None, None, None])[2]} for x in (r.get('risk_register') or {}).get('risks') or []],
            'record_write': ((r.get('zero_context') or {}).get('record_write')) if zero else None,
            'timeline': sorted([{'date': tl[k], 'label': lab} for k, lab in TL if tl.get(k)], key=lambda x: x['date']),
            'letter': drafts.get('escalation_summary') if r.get('route') == 'escalation' else drafts.get('outreach_letter'),
            'addressee': drafts.get('addressee') or (r.get('review_context') or {}).get('addressee'),
            'summary': drafts.get('escalation_summary') if (not zero and drafts.get('draft_source') == 'llm') else None,
            'dispatch': (pd or {}).get('dispatch') or r.get('dispatch'),
            'extra_facts': r.get('extra_facts') or [],
            'facts': (dn or {}).get('review_facts'),
            'letter_file': next(iter((out('Create Letter PDF') or {}).get('_files') or []), None),
        }
    return {
        'id': str(run['id']), 'status': status, 'started_at': run['started_at'], 'stopped_at': run['stopped_at'],
        'applicant_name': (r or {}).get('applicant_name') or ' '.join(x for x in [a.get('first_name'), a.get('last_name')] if x) or run.get('applicant_name'),
        'insurance_number': (r or {}).get('insurance_number') or a.get('insurance_number') or run.get('insurance_number'),
        'case_ref': (r or {}).get('application_id') or (cf or {}).get('case_ref'),
        'steps': step_list, 'review': review, 'can_decide_here': status == 'waiting', 'awaiting_email_confirmation': False,
        'decision': ('approved' if 'APPROVED' in decision_node else 'rejected') if decision_node else None,
        'error': run.get('error'),
    }


def portal_view(db: DB, agent: str, limit: int = 25) -> dict:
    import time
    cases = [_case(run, db.steps(run['id'])) for run in db.runs(agent, limit)]
    return {'generated_at': time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime()),
            'flow_label': 'agent without ARIS context' if agent == 'zero' else 'agent with ARIS context', 'cases': cases}
