"""Web server: caseworker portal, approve/reject links, PDF upload, letter download and ARIS exports."""
from __future__ import annotations
import base64, csv, datetime as dt, html, io, secrets, threading, time, traceback
from pathlib import Path

from fastapi import FastAPI, File, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse, Response, StreamingResponse

from ..config import settings
from ..db import DB
from ..pdftext import pdf_to_text
from ..pipeline import AGENT_LABEL, Pipeline
from ..portal_view import portal_view

HERE = Path(__file__).parent
db = DB(settings.db_path)
_google = _aris = None
if 'live' in (settings.drive_source, settings.aris_source) or settings.gmail_intake or settings.mail_mode == 'gmail':
    from ..sources.google import Google
    _google = Google(settings.google_client_file, settings.google_token_file)
if settings.aris_source == 'live':
    from ..sources.aris_mcp import ArisMCP
    _aris = ArisMCP(settings)
pipeline = Pipeline(settings, db, google=_google, aris=_aris)
app = FastAPI(title='Zero Gap Agent')


@app.middleware('http')
async def _basic_auth(request: Request, call_next):
    """Portal login (BASIC_AUTH_USER / BASIC_AUTH_PASSWORD). The approve/reject links in the review e-mail carry their own
    secret token and the health check must answer without a login, so both stay open."""
    if settings.basic_auth_password and not request.url.path.startswith(('/decide/', '/healthz')):
        try:
            user, _, pw = base64.b64decode(request.headers.get('authorization', '').removeprefix('Basic ')).decode().partition(':')
        except ValueError:
            user = pw = ''
        ok_user = secrets.compare_digest(user.encode(), settings.basic_auth_user.encode())
        ok_pw = secrets.compare_digest(pw.encode(), settings.basic_auth_password.encode())
        if not (ok_user and ok_pw):
            return Response('Login required', status_code=401, headers={'WWW-Authenticate': 'Basic realm="Zero Gap Agent"'})
    return await call_next(request)


@app.get('/healthz')
def healthz():
    return {'ok': True}


def _page(title: str, body: str) -> HTMLResponse:
    return HTMLResponse(f"""<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>{html.escape(title)}</title><style>
body{{font-family:Inter,Segoe UI,Arial,sans-serif;background:#f6f7f9;margin:0;padding:48px 16px;color:#1f2937}}
.c{{max-width:620px;margin:auto;background:#fff;border:1px solid #e5e7eb;border-radius:10px;padding:28px}}
.k{{font-size:11px;color:#6b7280;text-transform:uppercase;letter-spacing:1px}} h1,h2{{margin:8px 0 8px}} a{{color:#1f4e79}}
.b{{display:inline-block;padding:10px 14px;border-radius:8px;border:1px solid #1f4e79;background:#1f4e79;color:#fff;text-decoration:none;margin:6px 8px 0 0}}
.g{{background:#fff;color:#1f4e79}} p,li{{font-size:14px;line-height:1.55}} small{{color:#6b7280}}
</style></head><body><div class="c">{body}</div></body></html>""")


# ------------------------------------------------------------------ pages
@app.get('/', response_class=HTMLResponse)
def home():
    rows = ''.join(f"<li>#{r['id']} · {AGENT_LABEL[r['agent']]} · {html.escape(r['applicant_name'] or 'new application')} · <b>{r['status']}</b>"
                   f"{' · ' + r['decision'] if r['decision'] else ''}</li>" for r in db.runs(None, 8))
    return _page('Zero Gap Agent', f"""<div class="k">Care application intake</div><h1>Zero Gap Agent</h1>
<p>Two agents check every incoming care application. The caseworker reviews and approves before anything is sent.</p>
<a class="b" href="/portal/aris">Portal – agent with ARIS context</a><a class="b g" href="/portal/zero">Portal – agent with zero context</a>
<h2 style="margin-top:26px;font-size:16px">Exports for ARIS Process Mining</h2>
<p><a href="/api/export/event-log.csv">Event log of all runs (CSV)</a> · <a href="/api/export/cases.csv">Case results / KPIs (CSV)</a></p>
<h2 style="font-size:16px">Latest runs</h2><ul>{rows or '<li>No runs yet – upload an application form in a portal.</li>'}</ul>
<small>Sources: ARIS {settings.aris_source} · Drive {settings.drive_source} · mail {settings.mail_mode} · Gmail intake {'on' if settings.gmail_intake else 'off'}</small>""")


@app.get('/portal/{agent}', response_class=HTMLResponse)
def portal(agent: str):
    if agent not in ('aris', 'zero'):
        raise HTTPException(404)
    return HTMLResponse((HERE / 'portal.html').read_text(encoding='utf-8'))


# ------------------------------------------------------------------ API
@app.get('/api/portal/{agent}')
def api_portal(agent: str):
    return JSONResponse(portal_view(db, agent), headers={'Cache-Control': 'no-store'})


@app.post('/api/upload')
async def upload(file: UploadFile = File(...)):
    data = await file.read()
    if not data[:5] == b'%PDF-':
        raise HTTPException(400, 'Please upload the application form as a PDF.')
    text = pdf_to_text(data)
    if len(text.strip()) < 40:
        raise HTTPException(400, 'No readable text in this PDF (scanned image?).')
    return {'runs': start_all({'date': _now(), 'from': 'portal upload', 'subject': file.filename, 'form_name': file.filename, 'form_text': text, 'source': 'upload'})}


@app.post('/api/runs/{run_id}/decision')
def api_decide(run_id: int, decision: str, channel: str | None = None):
    try:
        res = pipeline.resume(run_id, 'approve' if decision == 'approve' else 'reject', channel)
    except ValueError as e:
        raise HTTPException(409, str(e))
    return {'decision': res['decision'], 'channel': (res.get('dispatch') or {}).get('channel')}


@app.get('/decide/{run_id}/{token}', response_class=HTMLResponse)
def decide_link(run_id: int, token: str, decision: str = 'reject', channel: str | None = None):
    """The approve / reject buttons in the review e-mail (replaces n8n's Wait-node resume URL)."""
    run = db.run(run_id)
    if not run or run['token'] != token:
        raise HTTPException(404)
    try:
        res = pipeline.resume(run_id, 'approve' if decision == 'approve' else 'reject', channel)
    except ValueError as e:
        return _page('Already decided', f'<div class="k">Care application review</div><h2>Already decided</h2><p>{html.escape(str(e))}</p>')
    case = res.get('case') or {}
    who = f"{html.escape(str(case.get('applicant_name') or ''))} ({html.escape(str(case.get('insurance_number') or ''))})"
    if res['decision'] == 'rejected':
        return _page('Rejected', f'<div class="k">Care application review</div><h2>Rejected</h2><p>The case has been returned for manual handling.<br>{who}</p><small>You can close this window.</small>')
    d = res.get('dispatch') or {}
    ch = d.get('channel')
    what = ('The application is released to the Medical Service for assessment.' if case.get('route') == 'none'
            else f"The escalation is sent to the Case Manager ({html.escape(str(d.get('to')))}) for phone outreach today." if ch == 'case_manager'
            else f"The letter is emailed to {html.escape(str(d.get('to')))}." if ch == 'email'
            else 'The letter is ready to print in the caseworker portal.')
    return _page('Approved', f'<div class="k">Care application review</div><h2>Approved</h2><p>{what}<br>{who}</p><small>You can close this window.</small>')


@app.get('/files/{run_id}/{name}')
def get_file(run_id: int, name: str):
    for f in db.files(run_id):
        if f['name'] == name:
            return Response(f['data'], media_type=f['mime'], headers={'Content-Disposition': f'inline; filename="{name}"'})
    raise HTTPException(404)


# ------------------------------------------------------------------ exports for ARIS
@app.get('/api/export/event-log.csv')
def export_event_log():
    """One row per executed step of every run – load into ARIS as an event table (case = run)."""
    buf = io.StringIO(); w = csv.writer(buf)
    w.writerow(['Case_ID', 'Agent', 'Application_ID', 'Insured_Person_ID', 'Applicant', 'Activity', 'Start_Timestamp', 'End_Timestamp', 'Duration_ms', 'Status'])
    for r in reversed(db.runs(None, 100000)):
        cid = f"{r['agent'].upper()}-{r['id']}"
        for s in db.steps(r['id'], with_output=False):
            st = dt.datetime.fromtimestamp(s['started_ms'] / 1000, dt.timezone.utc)
            en = st + dt.timedelta(milliseconds=s['duration_ms'] or 0)
            w.writerow([cid, AGENT_LABEL[r['agent']], r['application_id'] or '', r['insurance_number'] or '', r['applicant_name'] or '', s['node'],
                        st.strftime('%Y-%m-%d %H:%M:%S.%f')[:-3], en.strftime('%Y-%m-%d %H:%M:%S.%f')[:-3], s['duration_ms'], 'error' if s['error'] else 'ok'])
    return Response(buf.getvalue(), media_type='text/csv', headers={'Content-Disposition': 'attachment; filename="zero_gap_event_log.csv"'})


@app.get('/api/export/cases.csv')
def export_cases():
    """One row per run with the KPI fields (same column names as the simulated KPI table)."""
    buf = io.StringIO(); w = csv.writer(buf)
    cols = ['Agent_Run_ID', 'Agent', 'Application_ID', 'Insured_Person_ID', 'Applicant', 'Status', 'Started', 'Finished', 'Final_Status', 'Agent_Next_Step',
            'Person_Match_Result', 'Matched_Insured_Person_ID', 'Namesakes_Found', 'Items_Requested_Count', 'Unnecessary_Items_Requested',
            'Values_Copied_From_Other_Person', 'Values_Invented', 'Record_Fields_Overwritten', 'Days_Left_Shown', 'Policy_Amendments_Applied',
            'Risks_Occurred', 'Caseworker_Decision', 'Dispatch_Channel']
    w.writerow(cols)
    step_map = {'aris': 'Compliance Controls (ARIS)', 'zero': 'Zero-Context Agent'}
    for r in reversed(db.runs(None, 100000)):
        o = db.outputs(r['id']).get(step_map[r['agent']], [{}])[0]
        nl, zc = o.get('name_lookup') or {}, o.get('zero_context') or {}
        risks = (o.get('risk_register') or {}).get('risks') or []
        rw = zc.get('record_write') or {}
        nxt = {'escalation': 'Escalation to Case Manager', 'letter': 'Missing-information letter', 'none': 'Ready for assessment'}.get(o.get('route'), '')
        match = ('Correct person' if r['agent'] == 'aris' else 'No match' if not nl.get('chosen_id') else 'Wrong person' if nl.get('id_differs') else 'Correct person') if o else ''
        w.writerow([f"{r['agent'].upper()}-{r['id']}", AGENT_LABEL[r['agent']], o.get('application_id') or '', r['insurance_number'] or '', r['applicant_name'] or '',
                    r['status'], r['started_at'], r['stopped_at'] or '', o.get('final_status') or '', nxt, match,
                    nl.get('chosen_id') or (r['insurance_number'] if r['agent'] == 'aris' else ''), max(0, (nl.get('matches_count') or 0) - 1) if nl else 0,
                    len(o.get('hard_failures') or []), len(zc.get('unnecessary') or []), len(zc.get('copied') or []), len(zc.get('invented') or []),
                    sum(1 for c in rw.get('changes') or [] if c.get('from') != '(empty)'), (o.get('sla') or {}).get('days_to_sla_breach', ''),
                    ', '.join(a.get('id', '') for a in ((o.get('policy') or {}).get('amendments') or [])),
                    sum(1 for x in risks if x.get('occurred')), r['decision'] or '', r['channel'] or ''])
    return Response(buf.getvalue(), media_type='text/csv', headers={'Content-Disposition': 'attachment; filename="zero_gap_case_results.csv"'})


# ------------------------------------------------------------------ intake
def _now() -> str:
    return time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())


def start_all(intake: dict) -> list[dict]:
    return [{'agent': a, 'run_id': pipeline.start(a, intake)} for a in settings.agents]


def _gmail_loop():
    while True:
        try:
            for mid in _google.gmail_new_messages(settings.gmail_query):
                m = _google.gmail_message(mid)
                for name, data in m['pdfs'][:1]:
                    text = pdf_to_text(data)
                    if len(text.strip()) >= 40:
                        start_all({'date': m['date'], 'from': m['from'], 'subject': m['subject'], 'form_name': name, 'form_text': text, 'source': 'gmail', 'message_id': mid})
                _google.gmail_mark_processed(mid)
        except Exception:  # noqa: BLE001
            traceback.print_exc()
        time.sleep(settings.poll_seconds)


@app.on_event('startup')
def _startup():
    if settings.gmail_intake and _google:
        threading.Thread(target=_gmail_loop, daemon=True).start()
