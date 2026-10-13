"""The two agents as Python pipelines (replaces the two n8n flows).

Every step is recorded under its former n8n node name with duration and JSON output, so the portal, the review
links and the ARIS event-log export work from the same store. A run stops at "Wait for Caseworker Decision"
(status 'waiting') and continues when the caseworker approves or rejects (resume())."""
from __future__ import annotations
import datetime as dt, json, secrets, threading, time, traceback
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from .config import Settings
from .ctx import Ctx
from .db import DB
from .nodes import (apply_sop_rules, aris_case_history, build_aris_history_query, build_case_file, completeness_rules_engine,
                    compliance_controls_aris, create_letter_pdf, guardrails, keep_documents, look_up_event_log, prepare_dispatch,
                    prepare_letter_review_email, read_document_text, write_case_record, zero_context_agent)
from .prompts import aris_prompt, zero_prompt
from .sources import local
from .sources.llm_mail import llm_draft, send_mail

AGENT_LABEL = {'aris': 'Agent with ARIS context', 'zero': 'Agent with zero context'}
CASE_SEARCH_TEXTS = ['Antrag', 'Vollmacht', 'Fragebogen', 'Nachreichung', 'Gutachten', 'Bescheid', 'Widerspruch', 'Confirmation']
SOP_SEARCH_TEXTS = ['Zero_Gap', 'Policy Amendment']


def _iso(t: dt.datetime) -> str:
    return t.astimezone(dt.timezone.utc).strftime('%Y-%m-%dT%H:%M:%S.') + f'{t.microsecond // 1000:03d}Z'


class Pipeline:
    def __init__(self, settings: Settings, db: DB, google=None, aris=None):
        self.s, self.db, self.google, self.aris = settings, db, google, aris
        self.pool = ThreadPoolExecutor(max_workers=4)

    # ------------------------------------------------------------------ helpers
    def _ctx(self, outputs: dict | None = None) -> Ctx:
        test = self.s.test_recipient or None
        return Ctx(now=dt.datetime.now(dt.timezone.utc), outputs=outputs or {}, config={
            'engine': {'CHECK_DATE': self.s.check_date, 'TEST_RECIPIENT': test, 'CASE_MANAGER_EMAIL': self.s.case_manager_email},
            'zero_context_agent': {'TEST_RECIPIENT': test},
            'look_up_event_log': {'LOG_ROWS': self.s.event_log_rows},
        })

    def _step(self, run_id: int, ctx: Ctx, node: str, fn, *args):
        t0 = time.time()
        try:
            out = fn(*args)
        except Exception as e:  # noqa: BLE001
            self.db.add_step(run_id, node, t0 * 1000, int((time.time() - t0) * 1000), None, f'{type(e).__name__}: {e}')
            raise
        out = out if isinstance(out, list) else [out]
        clean = []
        for it in out:
            it = dict(it) if isinstance(it, dict) else {'value': it}
            for key, b in (it.pop('__binary__', None) or {}).items():
                self.db.put_file(run_id, b['fileName'], b.get('mimeType', 'application/octet-stream'), b['data'])
                it.setdefault('_files', []).append(b['fileName'])
            clean.append(it)
        ctx.set(node, clean)
        self.db.add_step(run_id, node, t0 * 1000, int((time.time() - t0) * 1000), clean)
        if self.s.step_delay_ms and node not in ('Gmail Trigger', 'Wait for Caseworker Decision'):
            time.sleep(self.s.step_delay_ms / 1000)
        return clean

    # ------------------------------------------------------------------ start
    def start(self, agent: str, intake: dict, background: bool = True) -> int:
        """intake = {date, from, subject, form_text, form_name, source}"""
        token = secrets.token_urlsafe(16)
        run_id = self.db.new_run(agent, token, intake.get('source', 'upload'), {k: v for k, v in intake.items() if k != 'form_text'})
        if background:
            self.pool.submit(self._run_safe, run_id, agent, intake)
        else:
            self._run_safe(run_id, agent, intake)
        return run_id

    def _run_safe(self, run_id: int, agent: str, intake: dict) -> None:
        try:
            (self._run_aris if agent == 'aris' else self._run_zero)(run_id, intake)
        except Exception as e:  # noqa: BLE001
            traceback.print_exc()
            self.db.update_run(run_id, status='error', error=f'{type(e).__name__}: {e}')

    def _intake_steps(self, run_id, ctx, intake):
        self._step(run_id, ctx, 'Gmail Trigger', lambda: [{'date': intake.get('date'), 'from': intake.get('from', ''), 'subject': intake.get('subject', ''), 'source': intake.get('source', 'upload')}])
        if intake.get('form_text'):
            self._step(run_id, ctx, 'Extract Application PDF', lambda: [{'text': intake['form_text']}])

    def _after_case_file(self, run_id, ctx):
        cf = ctx.node('Build Case File').first()
        a = cf.get('applicant') or {}
        self.db.update_run(run_id, applicant_name=' '.join(x for x in [a.get('first_name'), a.get('last_name')] if x) or None,
                           insurance_number=a.get('insurance_number'))

    def _finish_review(self, run_id, ctx, case_node: str):
        """Letter PDF -> review e-mail with approve/reject links -> wait."""
        pdf_items = self._step(run_id, ctx, 'Create Letter PDF', create_letter_pdf.run, ctx, ctx.node(case_node).all())
        j = pdf_items[0]
        run = self.db.run(run_id)
        resume = f"{self.s.base_url}/decide/{run_id}/{run['token']}"
        subject = f"{'Approve escalation' if j.get('route') == 'escalation' else 'Review required'}: {j.get('final_status')} – {j.get('applicant_name')} (case {j.get('application_id') or j.get('insurance_number') or 'new'}) – {(j.get('sla') or {}).get('queue_tag', '')}"
        html = str(j.get('approval_html') or '').replace('__RESUME_URL__', resume)
        att = [(f['name'], f['data'], f['mime']) for f in self.db.files(run_id) if f['mime'] == 'application/pdf']
        res = send_mail(self.s, self.google, self.s.review_email_to, subject, html, att)
        self._step(run_id, ctx, 'Send Review Email (letter attached)', lambda: [{'to': self.s.review_email_to, 'subject': subject, 'result': res}])
        self.db.update_run(run_id, status='waiting', application_id=j.get('application_id'), final_status=j.get('final_status'), route=j.get('route'))

    # ------------------------------------------------------------------ agent WITH ARIS context
    def _run_aris(self, run_id: int, intake: dict) -> None:
        ctx = self._ctx(); s = self.s
        self._intake_steps(run_id, ctx, intake)
        live = s.aris_source == 'live'
        # documents + SOPs/amendments from the ARIS repository
        if live:
            self._step(run_id, ctx, 'Build Case Document Searches', lambda: [{'search_body': {'searchText': t}} for t in CASE_SEARCH_TEXTS])
            res = self._step(run_id, ctx, 'Search Case Documents', self.aris.search_documents, CASE_SEARCH_TEXTS)
            ids = self._step(run_id, ctx, 'Collect Case Document IDs', lambda: [{'documentId': i} for i in __import__('zerogap.sources.aris_mcp', fromlist=['x']).collect_ids(res)])
            docs = self._step(run_id, ctx, 'MCP: Get Case Document Text', self.aris.documents_text, [i['documentId'] for i in ids])
        else:
            docs = self._step(run_id, ctx, 'Search Case Documents', lambda: [{'content': [{'type': 'text', 'text': Path(d['path']).read_text(encoding='utf-8')}]}
                                                                          for d in local.doc_items(s.data_dir / 'aris' / 'case_documents') if d['path'].endswith('.txt')]
                                                                         + [{'name': d['name'], 'path': d['path']} for d in local.doc_items(s.data_dir / 'aris' / 'case_documents') if not d['path'].endswith('.txt')])
        self._step(run_id, ctx, 'Read Case Document Text', read_document_text.run, ctx, docs)
        if live:
            self._step(run_id, ctx, 'Build SOP Searches', lambda: [{'search_body': {'searchText': t}} for t in SOP_SEARCH_TEXTS])
            res = self._step(run_id, ctx, 'Search SOPs and Amendments', self.aris.search_documents, SOP_SEARCH_TEXTS)
            ids = self._step(run_id, ctx, 'Collect SOP Document IDs', lambda: [{'documentId': i} for i in __import__('zerogap.sources.aris_mcp', fromlist=['x']).collect_ids(res)])
            sops = self._step(run_id, ctx, 'MCP: Get SOP Text', self.aris.documents_text, [i['documentId'] for i in ids])
        else:
            sops = self._step(run_id, ctx, 'Search SOPs and Amendments', lambda: [{'content': [{'type': 'text', 'text': Path(d['path']).read_text(encoding='utf-8')}]}
                                                                               for d in local.doc_items(s.data_dir / 'aris' / 'sops') if d['path'].endswith('.txt')]
                                                                              + [{'name': d['name'], 'path': d['path']} for d in local.doc_items(s.data_dir / 'aris' / 'sops') if not d['path'].endswith('.txt')])
        sop_text = self._step(run_id, ctx, 'Read SOP Text', read_document_text.run, ctx, sops)
        cf = self._step(run_id, ctx, 'Build Case File', build_case_file.run, ctx, sop_text)
        self._after_case_file(run_id, ctx)
        # case history from ARIS process data – exact insurance number
        hq = self._step(run_id, ctx, 'Build ARIS History Query', build_aris_history_query.run, ctx, cf)
        body = hq[0]['query_body']
        if live:
            ans = self._step(run_id, ctx, 'MCP: Query Case History', lambda: [self.aris.query(body)])
        else:
            ans = self._step(run_id, ctx, 'MCP: Query Case History', lambda: [{'content': [{'type': 'text', 'text': json.dumps(local.aris_query(s.data_dir / 'aris' / 'o_Care_Application.csv', body))}]}])
        hist = self._step(run_id, ctx, 'ARIS Case History', aris_case_history.run, ctx, ans)
        rules = self._step(run_id, ctx, 'Apply SOP Rules', apply_sop_rules.run, ctx, hist)
        eng = self._step(run_id, ctx, 'Completeness Rules Engine', completeness_rules_engine.run, ctx, rules)
        cc = self._step(run_id, ctx, 'Compliance Controls (ARIS)', compliance_controls_aris.run, ctx, eng)
        ai = self._step(run_id, ctx, 'AI: Draft Review Summary & Letter', lambda: [{'text': llm_draft(s, aris_prompt(cc[0]))}])
        self._step(run_id, ctx, 'Guardrails: Validate AI Draft', guardrails.run, ctx, ai)
        self._finish_review(run_id, ctx, 'Guardrails: Validate AI Draft')

    # ------------------------------------------------------------------ agent with ZERO context
    def _run_zero(self, run_id: int, intake: dict) -> None:
        ctx = self._ctx(); s = self.s
        self._intake_steps(run_id, ctx, intake)
        live = s.drive_source == 'live'
        if live:
            g = self.google
            self._step(run_id, ctx, 'Convert to Google Docs & Sheets', lambda: [{'converted': [c.get('name') for c in g.ensure_native_copies(s.drive_folder_id, s.event_log_name)]}])
            listing = self._step(run_id, ctx, 'List Case Documents', g.list_folder, s.drive_folder_id)
            keep = self._step(run_id, ctx, 'Keep Case Documents Only', keep_documents.run_case, ctx, listing)
            docs = self._step(run_id, ctx, 'Get Case Document (Google Docs)', lambda: [{'documentId': f['id'], 'content': g.doc_text(f['id'])} for f in keep])
        else:
            docs = self._step(run_id, ctx, 'List Case Documents', local.doc_items, s.data_dir / 'drive' / 'case_documents')
        self._step(run_id, ctx, 'Read Case Document Text', read_document_text.run, ctx, docs)
        if live:
            listing = self._step(run_id, ctx, 'List SOP Documents', g.list_folder, s.drive_folder_id)
            keep = self._step(run_id, ctx, 'Keep SOP Documents Only', keep_documents.run_sop, ctx, listing)
            sops = self._step(run_id, ctx, 'Get SOP Document (Google Docs)', lambda: [{'documentId': f['id'], 'content': g.doc_text(f['id'])} for f in keep])
        else:
            sops = self._step(run_id, ctx, 'List SOP Documents', local.doc_items, s.data_dir / 'drive' / 'sops')
        sop_text = self._step(run_id, ctx, 'Read SOP Text', read_document_text.run, ctx, sops)
        self._step(run_id, ctx, 'Build Case File', build_case_file.run, ctx, sop_text)
        self._after_case_file(run_id, ctx)
        a = ctx.node('Build Case File').first().get('applicant') or {}
        if live:
            sheet = next((f for f in ctx.node('List SOP Documents').all() if f.get('name') == s.event_log_name and 'spreadsheet' in (f.get('mimeType') or '')), None)
            if not sheet:
                raise RuntimeError(f'No Google Sheet named {s.event_log_name} in the Drive folder')
            rows = self.google.sheet_rows_by_name(sheet['id'], 'Applicant_First_Name', 'Applicant_Last_Name', a.get('first_name'), a.get('last_name'))[0]
        else:
            rows = look_up_event_log.rows_from_csv(s.data_dir / 'drive' / 'event_log.csv', a.get('first_name'), a.get('last_name'))
        self._step(run_id, ctx, 'Search Event Log by Name', lambda: [{'searched_for': ' '.join(x for x in [a.get('first_name'), a.get('last_name')] if x), 'rows_returned': len(rows)}])
        lk = self._step(run_id, ctx, 'Look Up Applicant in Event Log', look_up_event_log.run, ctx, rows)
        rules = self._step(run_id, ctx, 'Apply SOP Rules', apply_sop_rules.run, ctx, lk)
        eng = self._step(run_id, ctx, 'Completeness Rules Engine', completeness_rules_engine.run, ctx, rules)
        za = self._step(run_id, ctx, 'Zero-Context Agent', zero_context_agent.run, ctx, eng)
        self._step(run_id, ctx, 'Write Case Record (CRM)', write_case_record.run, ctx, za)
        ai = self._step(run_id, ctx, 'AI: Draft Letter', lambda: [{'text': llm_draft(s, zero_prompt(za[0]))}])
        self._step(run_id, ctx, 'Prepare Letter & Review Email', prepare_letter_review_email.run, ctx, ai)
        self._finish_review(run_id, ctx, 'Prepare Letter & Review Email')

    # ------------------------------------------------------------------ caseworker decision
    def resume(self, run_id: int, decision: str, channel: str | None) -> dict:
        run = self.db.run(run_id)
        if not run or run['status'] != 'waiting':
            raise ValueError('This case is not waiting for a decision (already decided or not found).')
        ctx = self._ctx(self.db.outputs(run_id))
        self.db.update_run(run_id, status='running')
        try:
            self._step(run_id, ctx, 'Wait for Caseworker Decision', lambda: [{'query': {'decision': decision, **({'channel': channel} if channel else {})}}])
            case = ctx.node('Create Letter PDF').first()
            now = _iso(ctx.now)
            if decision != 'approve':
                self._step(run_id, ctx, 'REJECTED: Manual Handling', lambda: [{'review_decision': 'rejected – caseworker handles manually', 'reviewed_at': now, 'case': case}])
                self.db.update_run(run_id, status='success', decision='rejected')
                return {'decision': 'rejected', 'case': case}
            ap = self._step(run_id, ctx, 'APPROVED: Release for Dispatch', lambda: [{'review_decision': 'approved – letter/escalation released by caseworker', 'reviewed_at': now, 'case': case}])
            pd = self._step(run_id, ctx, 'Prepare Dispatch', prepare_dispatch.run, ctx, ap)[0]
            d = pd.get('dispatch') or {}
            if d.get('channel') in ('email', 'case_manager') and d.get('to'):
                att = [(f['name'], f['data'], f['mime']) for f in self.db.files(run_id) if f['mime'] == 'application/pdf']
                res = send_mail(self.s, self.google, d['to'], pd.get('letter_subject', ''), pd.get('letter_html', ''), att)
                self._step(run_id, ctx, 'Send Letter or Escalation', lambda: [{'to': d['to'], 'result': res}])
            else:
                self._step(run_id, ctx, 'Print Letter / Release (no email)', lambda: [{'channel': d.get('channel'), 'note': 'print the letter PDF from the portal' if d.get('channel') == 'print' else 'released for assessment'}])
            self.db.update_run(run_id, status='success', decision='approved', channel=d.get('channel'))
            return {'decision': 'approved', 'dispatch': d, 'case': case}
        except Exception as e:  # noqa: BLE001
            traceback.print_exc()
            self.db.update_run(run_id, status='error', error=f'{type(e).__name__}: {e}')
            raise
