"""Settings, read from environment variables or a .env file next to app.py (see .env.example)."""
from __future__ import annotations
import os, re
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _load_dotenv(path: Path) -> None:
    if not path.exists():
        return
    for line in path.read_text(encoding='utf-8').splitlines():
        line = line.strip()
        if not line or line.startswith('#') or '=' not in line:
            continue
        k, v = line.split('=', 1)
        v = v.strip()
        if len(v) >= 2 and v[0] in '"\'' and v[0] in v[1:]:
            v = v[1:v.index(v[0], 1)]   # quoted value – anything after the closing quote (e.g. a comment) is dropped
        else:
            v = '' if v.startswith('#') else re.split(r'\s#', v, maxsplit=1)[0].strip()   # unquoted: "VALUE   # comment" -> "VALUE"
        os.environ.setdefault(k.strip(), v)


_load_dotenv(ROOT / '.env')


def _env(name: str, default: str = '') -> str:
    return os.environ.get(name, default).strip()


def _bool(name: str, default: bool) -> bool:
    v = _env(name, '')
    return default if v == '' else v.lower() in ('1', 'true', 'yes', 'on')


@dataclass
class Settings:
    # --- app
    base_url: str = field(default_factory=lambda: _env('BASE_URL', 'http://localhost:8000').rstrip('/'))
    host: str = field(default_factory=lambda: _env('HOST', '0.0.0.0'))
    port: int = field(default_factory=lambda: int(_env('PORT', '8000')))
    data_dir: Path = field(default_factory=lambda: Path(_env('DATA_DIR', str(ROOT / 'data'))))
    # login for the portal (HTTP basic auth) – set both when the app is reachable from the internet; empty = no login
    basic_auth_user: str = field(default_factory=lambda: _env('BASIC_AUTH_USER'))
    basic_auth_password: str = field(default_factory=lambda: _env('BASIC_AUTH_PASSWORD'))
    agents: list[str] = field(default_factory=lambda: [a.strip() for a in _env('AGENTS', 'aris,zero').split(',') if a.strip()])
    # 'local' = read documents, SOPs, ARIS history and the event log from data/ (demo, no credentials needed)
    # 'live'  = ARIS MCP + Google Drive / Docs / Sheets
    aris_source: str = field(default_factory=lambda: _env('ARIS_SOURCE', 'local'))
    drive_source: str = field(default_factory=lambda: _env('DRIVE_SOURCE', 'local'))
    # --- intake
    gmail_intake: bool = field(default_factory=lambda: _bool('GMAIL_INTAKE', False))
    gmail_query: str = field(default_factory=lambda: _env('GMAIL_QUERY', 'is:unread has:attachment filename:pdf -label:zerogap-processed'))
    poll_seconds: int = field(default_factory=lambda: int(_env('POLL_SECONDS', '60')))
    # --- e-mail out ('gmail', 'smtp' or 'outbox' = write .eml files to data/outbox, nothing is sent)
    mail_mode: str = field(default_factory=lambda: _env('MAIL_MODE', 'outbox'))
    review_email_to: str = field(default_factory=lambda: _env('REVIEW_EMAIL_TO', 'caseworker@your-domain.example'))
    case_manager_email: str = field(default_factory=lambda: _env('CASE_MANAGER_EMAIL', 'case.manager@your-domain.example'))
    test_recipient: str = field(default_factory=lambda: _env('TEST_RECIPIENT', ''))
    mail_from: str = field(default_factory=lambda: _env('MAIL_FROM', ''))
    smtp_host: str = field(default_factory=lambda: _env('SMTP_HOST'))
    smtp_port: int = field(default_factory=lambda: int(_env('SMTP_PORT', '587')))
    smtp_user: str = field(default_factory=lambda: _env('SMTP_USER'))
    smtp_password: str = field(default_factory=lambda: _env('SMTP_PASSWORD'))
    # --- Google (Gmail, Drive, Docs, Sheets) – OAuth desktop client, token created by `python app.py google-login`
    google_client_file: Path = field(default_factory=lambda: Path(_env('GOOGLE_CLIENT_FILE', str(ROOT / 'secrets' / 'google_client.json'))))
    google_token_file: Path = field(default_factory=lambda: Path(_env('GOOGLE_TOKEN_FILE', str(ROOT / 'secrets' / 'google_token.json'))))
    drive_folder_id: str = field(default_factory=lambda: _env('DRIVE_FOLDER_ID'))
    event_log_name: str = field(default_factory=lambda: _env('EVENT_LOG_NAME', 'Bitmarck_Care_Application_obj'))
    event_log_rows: int = field(default_factory=lambda: int(_env('EVENT_LOG_ROWS', '18229')))
    # --- ARIS MCP – OAuth login created by `python app.py aris-login`, or a bearer token
    aris_mcp_url: str = field(default_factory=lambda: _env('ARIS_MCP_URL', 'https://myfirst.ariscloud.com/aris-mcp/mcp'))
    aris_mcp_token: str = field(default_factory=lambda: _env('ARIS_MCP_TOKEN'))
    aris_token_file: Path = field(default_factory=lambda: Path(_env('ARIS_TOKEN_FILE', str(ROOT / 'secrets' / 'aris_oauth.json'))))
    # the OAuth application registered in ARIS (UMC → Applications), e.g. "zero-gap-agent"
    aris_client_id: str = field(default_factory=lambda: _env('ARIS_CLIENT_ID'))
    aris_client_secret: str = field(default_factory=lambda: _env('ARIS_CLIENT_SECRET'))
    aris_scope: str = field(default_factory=lambda: _env('ARIS_SCOPE', 'UserProfile'))
    aris_redirect_url: str = field(default_factory=lambda: _env('ARIS_REDIRECT_URL', 'http://localhost:3030/callback'))
    
    aris_tool_search: str = field(default_factory=lambda: _env('ARIS_TOOL_SEARCH', 'aris_ads_document_search'))
    aris_tool_get_text: str = field(default_factory=lambda: _env('ARIS_TOOL_GET_TEXT', 'aris_ads_document_getAsText'))
    aris_get_text_arg: str = field(default_factory=lambda: _env('ARIS_GET_TEXT_ARG', 'guid'))
    aris_tool_query: str = field(default_factory=lambda: _env('ARIS_TOOL_QUERY', 'aris_miningserver_query'))
    # --- LLM (optional – without a key the deterministic, guardrail-checked template letter is used)
    openai_api_key: str = field(default_factory=lambda: _env('OPENAI_API_KEY'))
    openai_model: str = field(default_factory=lambda: _env('OPENAI_MODEL', 'gpt-4o-mini'))
    # --- rules engine
    check_date: str = field(default_factory=lambda: _env('CHECK_DATE', 'auto'))
    # demo only: pause after each step so the portal shows the progress live (0 = run at full speed)
    step_delay_ms: int = field(default_factory=lambda: int(_env('STEP_DELAY_MS', '0')))

    @property
    def db_path(self) -> Path:
        return self.data_dir / 'zerogap.sqlite'


settings = Settings()
