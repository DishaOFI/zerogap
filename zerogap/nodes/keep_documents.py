"""Keep Case Documents Only / Keep SOP Documents Only (port of Keep_Case_Documents.js and Keep_SOP_Documents.js).

Keeps only the Google Docs this step needs from the shared Drive folder (list = file names only, nothing downloaded).
Their text is then read through the Google Docs API by the next node. Word (.docx), CSV, PDF and other files are skipped –
convert Word files to Google Docs in Drive (open the file → File → Save as Google Docs).
Policy amendments (PA-2026-xx) are skipped as well – the agent without ARIS context does not know about them.
"""
from __future__ import annotations
import math
import re

# re.A: ASCII \b / case folding, like a JS regex without the u flag; \Z = JS end-of-string ($ without m)
SOP_NAME = re.compile(r'operating[ _-]?procedure|work[ _-]?instruction|escalation[ _-]?procedure|governance|control[ _-]?policy'
                      r'|handover|ownership[ _-]?protocol|\bSOP\b', re.I | re.A)
AMENDMENT = re.compile(r'policy[ _-]?amendment|(^|[^a-z])PA[ _-]?[0-9]{4}[ _-]?[0-9]{2}', re.I | re.A)
_GDOC = re.compile(r'google-apps\.document')
_EXT = re.compile(r'\.[a-z0-9]{2,5}\Z', re.I | re.A)


def _truthy(v) -> bool:
    if v is None or v is False:
        return False
    if isinstance(v, (int, float)):
        return not (v == 0 or (isinstance(v, float) and math.isnan(v)))
    if isinstance(v, str):
        return v != ''
    return True


def _str(v) -> str:
    # String(v || '') – only strings occur in a Drive file list; other values are converted plainly
    if not _truthy(v):
        return ''
    if v is True:
        return 'true'
    if isinstance(v, float) and v.is_integer():
        return str(int(v))
    return v if isinstance(v, str) else str(v)


def _is_google_doc(f: dict) -> bool:
    return bool(_GDOC.search(_str(f.get('mimeType')))) or (
        not _truthy(f.get('mimeType')) and not _EXT.search(_str(f.get('name'))))


def _keep(items: list[dict], want: str) -> list[dict]:
    docs = [f for f in items if isinstance(f, dict) and _truthy(f.get('id')) and _is_google_doc(f)
            and not AMENDMENT.search(_str(f.get('name')))]
    keep = [f for f in docs if (want == 'SOP') == bool(SOP_NAME.search(_str(f.get('name'))))]
    if not keep:
        raise RuntimeError(f"No Google Docs {'SOPs' if want == 'SOP' else 'case documents'} found in the folder. "
                           'Convert the Word files to Google Docs (File → Save as Google Docs).')
    return keep


def run_case(ctx, items: list[dict]) -> list[dict]:
    """Keep Case Documents Only (WANT = 'CASE')."""
    return _keep(items, 'CASE')


def run_sop(ctx, items: list[dict]) -> list[dict]:
    """Keep SOP Documents Only (WANT = 'SOP')."""
    return _keep(items, 'SOP')
