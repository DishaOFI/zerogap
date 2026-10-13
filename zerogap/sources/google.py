"""Google APIs over plain HTTPS (Gmail, Drive, Docs, Sheets) with an OAuth desktop-client token.

Nothing large is downloaded: documents are read as text through the Docs API, the event log is queried with the
Sheets query language (only the rows with the applicant's name come back), Word / CSV files are converted to
Google Docs / Sheets inside Drive (copy with a Google mime type)."""
from __future__ import annotations
import base64, csv, io, json, threading, time, urllib.parse, webbrowser
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import httpx

SCOPES = ['https://www.googleapis.com/auth/gmail.modify', 'https://www.googleapis.com/auth/drive',
          'https://www.googleapis.com/auth/documents.readonly', 'https://www.googleapis.com/auth/spreadsheets.readonly']
TOKEN_URL = 'https://oauth2.googleapis.com/token'
AUTH_URL = 'https://accounts.google.com/o/oauth2/v2/auth'
DOC = 'application/vnd.google-apps.document'
SHEET = 'application/vnd.google-apps.spreadsheet'


def _client(client_file: Path) -> dict:
    c = json.loads(Path(client_file).read_text())
    return c.get('installed') or c.get('web') or c


def login(client_file: Path, token_file: Path, port: int = 8765) -> None:
    """One-time browser login (desktop OAuth client). Stores a refresh token in token_file."""
    c = _client(client_file)
    redirect = f'http://127.0.0.1:{port}/'
    url = AUTH_URL + '?' + urllib.parse.urlencode({'client_id': c['client_id'], 'redirect_uri': redirect, 'response_type': 'code',
                                                   'scope': ' '.join(SCOPES), 'access_type': 'offline', 'prompt': 'consent'})
    got = {}

    class H(BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            got['code'] = (q.get('code') or [None])[0]
            self.send_response(200); self.end_headers()
            self.wfile.write(b'Google login complete - you can close this tab.')

        def log_message(self, *a):
            pass

    srv = HTTPServer(('127.0.0.1', port), H)
    print('Open this URL to sign in with the Google account that owns the Drive folder and the intake mailbox:\n', url)
    try:
        webbrowser.open(url)
    except Exception:  # noqa: BLE001
        pass
    while 'code' not in got:
        srv.handle_request()
    r = httpx.post(TOKEN_URL, data={'code': got['code'], 'client_id': c['client_id'], 'client_secret': c['client_secret'],
                                    'redirect_uri': redirect, 'grant_type': 'authorization_code'}, timeout=30)
    r.raise_for_status()
    tok = r.json(); tok['obtained_at'] = time.time()
    token_file.parent.mkdir(parents=True, exist_ok=True)
    token_file.write_text(json.dumps(tok, indent=1))
    print('Saved', token_file)


class Google:
    def __init__(self, client_file: Path, token_file: Path):
        self.client_file, self.token_file = Path(client_file), Path(token_file)
        self._lock = threading.Lock()
        self._tok = json.loads(self.token_file.read_text()) if self.token_file.exists() else None

    # ---------------------------------------------------------------- auth
    def _access_token(self) -> str:
        with self._lock:
            if not self._tok:
                raise RuntimeError('Google is not connected – run: python app.py google-login')
            if time.time() > self._tok.get('obtained_at', 0) + self._tok.get('expires_in', 0) - 120:
                c = _client(self.client_file)
                r = httpx.post(TOKEN_URL, data={'client_id': c['client_id'], 'client_secret': c['client_secret'],
                                                'refresh_token': self._tok['refresh_token'], 'grant_type': 'refresh_token'}, timeout=30)
                r.raise_for_status()
                self._tok.update(r.json()); self._tok['obtained_at'] = time.time()
                self.token_file.write_text(json.dumps(self._tok, indent=1))
            return self._tok['access_token']

    def _req(self, method: str, url: str, **kw) -> httpx.Response:
        h = kw.pop('headers', {}) | {'Authorization': f'Bearer {self._access_token()}'}
        r = httpx.request(method, url, headers=h, timeout=kw.pop('timeout', 60), **kw)
        if r.status_code >= 400:
            raise RuntimeError(f'Google API {r.status_code}: {r.text[:300]}')
        return r

    # ---------------------------------------------------------------- Drive
    def list_folder(self, folder_id: str) -> list[dict]:
        files, token = [], None
        while True:
            params = {'q': f"'{folder_id}' in parents and trashed = false", 'fields': 'nextPageToken, files(id,name,mimeType,size)',
                      'pageSize': 1000, 'supportsAllDrives': 'true', 'includeItemsFromAllDrives': 'true'}
            if token:
                params['pageToken'] = token
            j = self._req('GET', 'https://www.googleapis.com/drive/v3/files', params=params).json()
            files += j.get('files', [])
            token = j.get('nextPageToken')
            if not token:
                return files

    def convert_in_drive(self, file_id: str, name: str, mime: str, folder_id: str) -> dict:
        """Copies a Word/CSV file as a native Google Doc/Sheet – the conversion happens inside Google Drive."""
        return self._req('POST', f'https://www.googleapis.com/drive/v3/files/{file_id}/copy', params={'supportsAllDrives': 'true'},
                         json={'name': name, 'mimeType': mime, 'parents': [folder_id]}).json()

    def ensure_native_copies(self, folder_id: str, event_log_name: str) -> list[dict]:
        """Word files -> Google Docs, the event-log CSV -> Google Sheet (only when no native copy exists yet)."""
        files = self.list_folder(folder_id)
        native = {f['name'].lower() for f in files if f.get('mimeType') in (DOC, SHEET)}
        made = []
        for f in files:
            n = f['name']
            base = n.rsplit('.', 1)[0]
            if base.lower() in native:
                continue
            if n.lower().endswith('.docx'):
                made.append(self.convert_in_drive(f['id'], base, DOC, folder_id))
            elif n.lower().endswith('.csv') and base.lower() == event_log_name.lower():
                made.append(self.convert_in_drive(f['id'], base, SHEET, folder_id))
        return made

    # ---------------------------------------------------------------- Docs
    def doc_text(self, doc_id: str) -> str:
        j = self._req('GET', f'https://docs.googleapis.com/v1/documents/{doc_id}').json()
        out = []

        def walk(content):
            for el in content or []:
                if 'paragraph' in el:
                    out.append(''.join(e.get('textRun', {}).get('content', '') for e in el['paragraph'].get('elements', [])))
                elif 'table' in el:
                    for row in el['table'].get('tableRows', []):
                        for cell in row.get('tableCells', []):
                            walk(cell.get('content'))
                elif 'tableOfContents' in el:
                    walk(el['tableOfContents'].get('content'))
        walk(j.get('body', {}).get('content'))
        return ''.join(out)

    # ---------------------------------------------------------------- Sheets
    def sheet_rows_by_name(self, sheet_id: str, first_col: str, last_col: str, first: str, last: str) -> tuple[list[dict], list[str]]:
        """Only the rows whose name columns match – Sheets query language, nothing else is transferred."""
        head = self._req('GET', f'https://sheets.googleapis.com/v4/spreadsheets/{sheet_id}/values/1:1').json().get('values', [[]])[0]
        letter = lambda i: (letter(i // 26 - 1) if i >= 26 else '') + chr(65 + i % 26)  # noqa: E731
        col = {h: letter(i) for i, h in enumerate(head)}
        q = lambda v: '"' + str(v).replace('"', '') + '"'  # noqa: E731
        conds = []
        if last and last_col in col:
            conds.append(f'{col[last_col]} = {q(last)}')
        if first and first_col in col:
            conds.append(f'{col[first_col]} = {q(first)}')
        if not conds:
            return [], head
        tq = 'select * where ' + ' and '.join(conds)
        r = self._req('GET', f'https://docs.google.com/spreadsheets/d/{sheet_id}/gviz/tq', params={'tqx': 'out:csv', 'tq': tq, 'headers': 1})
        rows = list(csv.DictReader(io.StringIO(r.text)))
        return rows, head

    # ---------------------------------------------------------------- Gmail
    def gmail_new_messages(self, query: str, max_results: int = 10) -> list[str]:
        j = self._req('GET', 'https://gmail.googleapis.com/gmail/v1/users/me/messages', params={'q': query, 'maxResults': max_results}).json()
        return [m['id'] for m in j.get('messages', [])]

    def gmail_message(self, msg_id: str) -> dict:
        """Returns {id, date, from, subject, pdfs:[(filename, bytes)]}."""
        m = self._req('GET', f'https://gmail.googleapis.com/gmail/v1/users/me/messages/{msg_id}', params={'format': 'full'}).json()
        hdr = {h['name'].lower(): h['value'] for h in m.get('payload', {}).get('headers', [])}
        pdfs = []

        def walk(p):
            for part in p.get('parts', []) or []:
                walk(part)
            fn = p.get('filename') or ''
            if fn.lower().endswith('.pdf') and p.get('body', {}).get('attachmentId'):
                a = self._req('GET', f"https://gmail.googleapis.com/gmail/v1/users/me/messages/{msg_id}/attachments/{p['body']['attachmentId']}").json()
                pdfs.append((fn, base64.urlsafe_b64decode(a['data'])))
        walk(m.get('payload', {}))
        date = time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime(int(m.get('internalDate', '0')) / 1000))
        return {'id': msg_id, 'date': date, 'from': hdr.get('from', ''), 'subject': hdr.get('subject', ''), 'pdfs': pdfs}

    def gmail_mark_processed(self, msg_id: str, label_name: str = 'zerogap-processed') -> None:
        labels = self._req('GET', 'https://gmail.googleapis.com/gmail/v1/users/me/labels').json().get('labels', [])
        lab = next((l['id'] for l in labels if l['name'] == label_name), None)
        if not lab:
            lab = self._req('POST', 'https://gmail.googleapis.com/gmail/v1/users/me/labels', json={'name': label_name}).json()['id']
        self._req('POST', f'https://gmail.googleapis.com/gmail/v1/users/me/messages/{msg_id}/modify',
                  json={'addLabelIds': [lab], 'removeLabelIds': ['UNREAD']})

    def gmail_send(self, raw_mime: bytes) -> None:
        self._req('POST', 'https://gmail.googleapis.com/gmail/v1/users/me/messages/send',
                  json={'raw': base64.urlsafe_b64encode(raw_mime).decode()})
