"""Local demo sources – the same data the n8n flows read, kept as files under data/ (no credentials needed).

data/aris/case_documents/   case documents in the ARIS repository (.txt / .docx)
data/aris/sops/             SOPs + policy amendments PA-2026-xx in ARIS
data/aris/o_Care_Application.csv   ARIS process data (one row per application)
data/drive/case_documents/  copies of the case documents in Google Drive
data/drive/sops/            the old SOP copies in Google Drive (no amendments)
data/drive/event_log.csv    event log in Google Drive (name search by the zero-context agent)
"""
from __future__ import annotations
import csv, re
from pathlib import Path

DOC_EXT = {'.txt', '.md', '.docx'}


def doc_items(folder: Path) -> list[dict]:
    if not folder.exists():
        return []
    return [{'name': p.name, 'path': str(p)} for p in sorted(folder.iterdir()) if p.suffix.lower() in DOC_EXT and not p.name.startswith('~$')]


def _cmp_max(a, b):
    if a in (None, ''):
        return b
    if b in (None, ''):
        return a
    try:
        return a if float(a) >= float(b) else b
    except ValueError:
        return a if str(a) >= str(b) else b


def aris_query(csv_path: Path, query_body: dict) -> dict:
    """Answers the AQL query built by `build_aris_history_query` from the local CSV, in the shape ARIS returns
    ({headers:[{value}], rows:[[...]]}). Only the aggregations that query uses are supported."""
    scripts = (((query_body or {}).get('query') or {}).get('table') or {}).get('fieldScripts') or []
    id_m = re.search(r'"(\w+)" = \'([^\']*)\'', scripts[0]['script']) if scripts else None
    id_col, id_val = (id_m.group(1), id_m.group(2)) if id_m else (None, None)
    hits, namesakes = [], set()
    name_cond = None
    for s in scripts:
        if s['alias'] == 'H_Namesakes':
            name_cond = dict(re.findall(r'"\w+"\."(\w+)" = \'([^\']*)\'', s['script']))
            name_cond.pop(id_col, None)
    if csv_path.exists() and id_col:
        with open(csv_path, newline='', encoding='utf-8-sig') as f:
            for r in csv.DictReader(f):
                if r.get(id_col) == id_val:
                    hits.append(r)
                elif name_cond and all(r.get(k) == v for k, v in name_cond.items()):
                    namesakes.add(r.get(id_col))
    headers, row = [], []
    for s in scripts:
        alias = s['alias']
        headers.append({'value': alias})
        if alias == 'H_Case_Count':
            row.append(len(hits))
        elif alias == 'H_Missing_Info_Requests':
            row.append(sum(int(float(h.get('Missing_Information_Count') or 0)) for h in hits))
        elif alias == 'H_Namesakes':
            row.append(len(namesakes))
        else:
            v = None
            for h in hits:
                v = _cmp_max(v, h.get(alias))
            row.append(v if v != '' else None)
    return {'headers': headers, 'rows': [row]}
