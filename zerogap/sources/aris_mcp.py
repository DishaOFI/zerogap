"""ARIS MCP client (replaces n8n's MCP Client nodes).

Auth: either ARIS_MCP_TOKEN (bearer token) or the OAuth login stored by `python app.py aris-login`
(same OAuth flow n8n's "MCP OAuth2" credential uses: dynamic client registration + browser login, refreshed automatically).
Tool names are configurable because ARIS renamed them (searchDocuments -> aris_ads_document_search, ...)."""
from __future__ import annotations
import asyncio, json, threading, urllib.parse, webbrowser
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

from mcp import ClientSession
from mcp.client.auth import OAuthClientProvider, TokenStorage
from mcp.client.streamable_http import streamablehttp_client
from mcp.shared.auth import OAuthClientInformationFull, OAuthClientMetadata, OAuthToken

# CALLBACK_PORT = 3030


class FileTokenStorage(TokenStorage):
    def __init__(self, path: Path):
        self.path = Path(path)

    def _load(self) -> dict:
        return json.loads(self.path.read_text()) if self.path.exists() else {}

    def _save(self, d: dict) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(d, indent=1))

    async def get_tokens(self):
        t = self._load().get('tokens')
        return OAuthToken(**t) if t else None

    async def set_tokens(self, tokens) -> None:
        d = self._load(); d['tokens'] = tokens.model_dump(mode='json', exclude_none=True); self._save(d)

    async def get_client_info(self):
        c = self._load().get('client')
        return OAuthClientInformationFull(**c) if c else None

    async def set_client_info(self, info) -> None:
        d = self._load(); d['client'] = info.model_dump(mode='json', exclude_none=True); self._save(d)


# def _oauth(url: str, token_file: Path, interactive: bool) -> OAuthClientProvider:
#     async def redirect_handler(auth_url: str) -> None:
#         if not interactive:
#             raise RuntimeError('ARIS login expired – run: python app.py aris-login')
#         print('Open this URL to sign in to ARIS:\n', auth_url)
#         try:
#             webbrowser.open(auth_url)
#         except Exception:  # noqa: BLE001
#             pass

#     async def callback_handler() -> tuple[str, str | None]:
#         got = {}

#         class H(BaseHTTPRequestHandler):
#             def do_GET(self):  # noqa: N802
#                 q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
#                 got['code'], got['state'] = (q.get('code') or [None])[0], (q.get('state') or [None])[0]
#                 self.send_response(200); self.end_headers(); self.wfile.write(b'ARIS login complete - you can close this tab.')

#             def log_message(self, *a):
#                 pass

#         srv = HTTPServer(('127.0.0.1', CALLBACK_PORT), H)
#         await asyncio.get_running_loop().run_in_executor(None, lambda: [srv.handle_request() for _ in iter(lambda: 'code' in got, True)])
#         return got['code'], got.get('state')

#     meta = OAuthClientMetadata(client_name='Zero Gap Agent', redirect_uris=[f'http://127.0.0.1:{CALLBACK_PORT}/callback'],
#                                grant_types=['authorization_code', 'refresh_token'], response_types=['code'], token_endpoint_auth_method='none')
#     return OAuthClientProvider(server_url=url, client_metadata=meta, storage=FileTokenStorage(token_file),
#                                redirect_handler=redirect_handler, callback_handler=callback_handler)
def _oauth(settings, interactive: bool) -> OAuthClientProvider:
    redirect = settings.aris_redirect_url
    cb = urllib.parse.urlparse(redirect)
    storage = FileTokenStorage(settings.aris_token_file)
    if settings.aris_client_id:   # pre-registered ARIS application – skip dynamic registration
        d = storage._load()
        if (d.get('client') or {}).get('client_id') != settings.aris_client_id:
            d['client'] = {'client_id': settings.aris_client_id, 'client_secret': settings.aris_client_secret or None,
                           'redirect_uris': [redirect], 'grant_types': ['authorization_code', 'refresh_token'], 'response_types': ['code'],
                           'token_endpoint_auth_method': 'client_secret_post' if settings.aris_client_secret else 'none',
                           'scope': settings.aris_scope or None}
            d['client'] = {k: v for k, v in d['client'].items() if v is not None}
            d.pop('tokens', None)
            storage._save(d)

    async def redirect_handler(auth_url: str) -> None:
        if not interactive:
            raise RuntimeError('ARIS login expired – run: python app.py aris-login')
        print('Open this URL to sign in to ARIS:\n', auth_url)
        try:
            webbrowser.open(auth_url)
        except Exception:  # noqa: BLE001
            pass

    async def callback_handler() -> tuple[str, str | None]:
        got = {}

        class H(BaseHTTPRequestHandler):
            def do_GET(self):  # noqa: N802
                q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
                if 'code' in q or 'error' in q:
                    got['code'], got['state'] = (q.get('code') or [None])[0], (q.get('state') or [None])[0]
                    got['error'] = (q.get('error_description') or q.get('error') or [None])[0]
                self.send_response(200); self.end_headers()
                self.wfile.write(b'ARIS login complete - you can close this tab.' if got.get('code') else b'Waiting for the ARIS login ...')

            def log_message(self, *a):
                pass

        srv = HTTPServer(('0.0.0.0', cb.port or 80), H)

        def wait():
            while 'code' not in got:
                srv.handle_request()
            srv.server_close()
        await asyncio.get_running_loop().run_in_executor(None, wait)
        if not got.get('code'):
            raise RuntimeError(f"ARIS login failed: {got.get('error')}")
        return got['code'], got.get('state')

    meta = OAuthClientMetadata(client_name='zero-gap-agent', redirect_uris=[redirect], grant_types=['authorization_code', 'refresh_token'],
                               response_types=['code'], scope=settings.aris_scope or None,
                               token_endpoint_auth_method='client_secret_post' if settings.aris_client_secret else 'none')
    return OAuthClientProvider(server_url=settings.aris_mcp_url, client_metadata=meta, storage=storage,
                               redirect_handler=redirect_handler, callback_handler=callback_handler)

class ArisMCP:
    def __init__(self, settings):
        self.s = settings
        self._lock = threading.Lock()

    async def _call_many(self, calls: list[tuple[str, dict]], interactive: bool = False) -> list[dict]:
        headers = {'Authorization': f'Bearer {self.s.aris_mcp_token}'} if self.s.aris_mcp_token else None
        # auth = None if headers else _oauth(self.s.aris_mcp_url, self.s.aris_token_file, interactive)
        auth = None if headers else _oauth(self.s, interactive)
        out = []
        async with streamablehttp_client(self.s.aris_mcp_url, headers=headers, auth=auth, timeout=60) as (r, w, _):
            async with ClientSession(r, w) as session:
                await session.initialize()
                for tool, args in calls:
                    res = await session.call_tool(tool, args)
                    text = '\n'.join(getattr(c, 'text', '') for c in res.content if getattr(c, 'type', '') == 'text')
                    item = {'content': [{'type': 'text', 'text': text}]}
                    if res.isError:
                        item = {'error': {'message': text or 'MCP tool error'}}
                    out.append(item)
        return out

    def call_many(self, calls: list[tuple[str, dict]]) -> list[dict]:
        """Runs several tool calls in one MCP session; each result looks like n8n's MCP node output."""
        with self._lock:
            return asyncio.run(self._call_many(calls))

    def list_tools(self, interactive: bool = False) -> list[str]:
        async def go():
            headers = {'Authorization': f'Bearer {self.s.aris_mcp_token}'} if self.s.aris_mcp_token else None
            # auth = None if headers else _oauth(self.s.aris_mcp_url, self.s.aris_token_file, interactive)
            auth = None if headers else _oauth(self.s, interactive)
            async with streamablehttp_client(self.s.aris_mcp_url, headers=headers, auth=auth) as (r, w, _):
                async with ClientSession(r, w) as session:
                    await session.initialize()
                    return [t.name for t in (await session.list_tools()).tools]
        return asyncio.run(go())

    # -------- the three calls the agent uses
    def search_documents(self, texts: list[str], limit: int = 50) -> list[dict]:
        return self.call_many([(self.s.aris_tool_search, {'searchText': t, 'offset': 0, 'limit': limit, 'headRevisionOnly': True}) for t in texts])

    def documents_text(self, ids: list[str]) -> list[dict]:
        return self.call_many([(self.s.aris_tool_get_text, {self.s.aris_get_text_arg: i}) for i in ids]) if ids else []

    def query(self, body: dict) -> dict:
        return self.call_many([(self.s.aris_tool_query, body)])[0]


def collect_ids(results: list[dict]) -> list[str]:
    """Same as the n8n node "Collect … Document IDs": merges the IDs of all search results."""
    ids: dict[str, None] = {}
    for it in results:
        v = ((it.get('content') or [{}])[0] or {}).get('text')
        if isinstance(v, str):
            try:
                v = json.loads(v)
            except ValueError:
                v = [x for x in __import__('re').split(r'[\s,]+', v) if x]
        for x in (v if isinstance(v, list) else [v] if v else []):
            i = (x.get('guid') or x.get('id') or x.get('documentId')) if isinstance(x, dict) else x
            if i:
                ids[str(i)] = None
    return list(ids)
