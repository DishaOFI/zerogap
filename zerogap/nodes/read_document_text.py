"""Read Document Text (port of Read_Document_Text.js) — turns each document into { name, text }.

Works for: Google Docs read with the Google Docs node (Get) -> {name, content: "<text>"}, ARIS MCP getDocumentAsText results
({content: [{type: 'text', text}]}), already extracted text ({name, text}), and local files: Word (.docx) as
{name, docx_bytes: bytes} or {name, path}, .txt/.md via {name, path} (or {name, bytes, mimeType}). PDFs are skipped with a note.
"""
from __future__ import annotations
import io
import math
import os
import re
import zipfile

DOCX_MIME = 'application/vnd.openxmlformats-officedocument.wordprocessingml.document'
_MIME_BY_EXT = {'.docx': DOCX_MIME, '.txt': 'text/plain', '.md': 'text/markdown', '.csv': 'text/csv', '.pdf': 'application/pdf',
                '.json': 'application/json', '.html': 'text/html', '.htm': 'text/html', '.xml': 'application/xml',
                '.doc': 'application/msword', '.zip': 'application/zip'}
_WS = '\t\n\x0b\x0c\r \xa0  -     　﻿'   # JS \s / trim() set
_TRIM = re.compile(f'^[{_WS}]+|[{_WS}]+\\Z')
_ERRTEXT = re.compile(f'^[{_WS}]*(exception|error)', re.I | re.A)
_IS_DOCX = re.compile(r'wordprocessingml', re.I)
_DOCX_NAME = re.compile(r'\.docx\Z', re.I)
_NOT_TEXT = re.compile(r'pdf|officedocument|msword|zip|octet-stream', re.I)


def _truthy(v) -> bool:
    if v is None or v is False:
        return False
    if isinstance(v, (int, float)):
        return not (v == 0 or (isinstance(v, float) and math.isnan(v)))
    if isinstance(v, (str, bytes)):
        return len(v) > 0 if isinstance(v, str) else True
    return True


def _u16len(s: str) -> int:
    return len(s.encode('utf-16-le', 'surrogatepass')) // 2


# ---- .docx → text (zipfile instead of the hand-written unzip/inflate of the JS version)
def docx_text(buf: bytes) -> str:
    try:
        with zipfile.ZipFile(io.BytesIO(buf)) as z:
            xml = z.read('word/document.xml')
    except KeyError:
        return ''
    s = xml.decode('utf-8', 'replace')
    s = s.replace('<w:tab/>', '\t')
    s = re.sub(r'<w:br[^>]*/>', '\n', s)
    s = s.replace('</w:p>', '\n')
    s = re.sub(r'<[^>]+>', '', s)
    s = s.replace('&lt;', '<').replace('&gt;', '>').replace('&quot;', '"').replace('&apos;', "'")
    s = re.sub(r'&#([0-9]+);', lambda m: chr(int(m.group(1)) % 65536), s)   # String.fromCharCode semantics
    s = s.replace('&amp;', '&')
    s = re.sub(r'[ \t]+\n', '\n', s)
    s = re.sub(r'\n{3,}', '\n\n', s)
    return _TRIM.sub('', s)


def _binary(j: dict):
    """The local-file equivalent of the n8n binary property: {fileName, mimeType, load()} or None."""
    if not isinstance(j, dict):
        return None
    if isinstance(j.get('docx_bytes'), (bytes, bytearray)):
        data = bytes(j['docx_bytes'])
        return {'fileName': j.get('name'), 'mimeType': j.get('mimeType') or DOCX_MIME, 'load': lambda: data}
    if isinstance(j.get('bytes'), (bytes, bytearray)):
        data = bytes(j['bytes'])
        ext = os.path.splitext(str(j.get('name') or ''))[1].lower()
        return {'fileName': j.get('name'), 'mimeType': j.get('mimeType') or _MIME_BY_EXT.get(ext, 'application/octet-stream'), 'load': lambda: data}
    if isinstance(j.get('path'), (str, os.PathLike)):
        p = os.fspath(j['path'])
        ext = os.path.splitext(p)[1].lower()

        def load():
            with open(p, 'rb') as fh:
                return fh.read()
        return {'fileName': os.path.basename(p), 'mimeType': j.get('mimeType') or _MIME_BY_EXT.get(ext, 'application/octet-stream'), 'load': load}
    return None


def run(ctx, items: list[dict]) -> list[dict]:
    out = []
    for i, j in enumerate(items):
        j = j if isinstance(j, dict) else {}
        b = _binary(j)
        # name: from the file list (Keep … Only) – the Google Docs API result only carries the document id
        listed = None
        # SOP list first: on the case branch it has not run yet, so the lookup fails there and the case list is used
        for k in ('Keep SOP Documents Only', 'Keep Case Documents Only'):
            if listed:
                break
            try:
                listed = ctx.node(k).item_matching(i) or None
            except KeyError:
                pass   # not on this branch
        name = next((v for v in (j.get('name'), (listed or {}).get('name'), (b or {}).get('fileName'), j.get('documentId'))
                     if _truthy(v)), f'document {i + 1}')
        mime = next((v for v in ((b or {}).get('mimeType'), j.get('mimeType')) if _truthy(v)), '')
        text = ''
        # 1) ARIS MCP result
        content = j.get('content')
        mcp = content[0].get('text') if isinstance(content, list) and content and isinstance(content[0], dict) else None
        if isinstance(mcp, str):
            text = mcp
        # 1b) Google Docs node (Get, simplified): { documentId, content: "<text>" } – read through the API, nothing downloaded
        elif isinstance(content, str):
            text = content
        # 2) local file
        elif b and (_IS_DOCX.search(str(mime)) or _DOCX_NAME.search(str(name))):
            try:
                text = docx_text(b['load']())
            except Exception:  # noqa: BLE001 – unreadable file -> empty text, like the JS version
                text = ''
        elif b and not _NOT_TEXT.search(str(mime)):
            try:
                text = b['load']().decode('utf-8', 'replace')
            except Exception:  # noqa: BLE001
                text = ''
        elif isinstance(j.get('text'), str):
            text = j['text']   # e.g. output of an Extract from File node
        text = re.sub(r'^﻿', '', str(text)).replace('\r', '')
        readable = _u16len(_TRIM.sub('', text)) > 40 and not _ERRTEXT.search(text)
        out.append({'name': name, 'origin': 'aris' if isinstance(mcp, str) else 'file', 'mimeType': mime, 'readable': readable,
                    'text': text if readable else '',
                    'note': None if readable else f"Not readable as text ({mime or 'no file'}) – use .docx, a Google Doc or a .txt file."})
    return out if out else [{'name': None, 'readable': False, 'text': '', 'note': 'No documents found in the folder'}]
