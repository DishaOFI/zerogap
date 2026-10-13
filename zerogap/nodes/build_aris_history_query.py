"""Build ARIS History Query (port of Build_ARIS_History_Query.js).

Builds ONE ARIS AQL query that returns the case history for the applicant's member / insurance number.
ARIS filters all rows server-side (every column is an aggregation -> one result row, works on 600k+ rows).
"""
from __future__ import annotations
import copy
import math
import re

CONFIG = {
    'TENANT': 'demobitmarck', 'DATASET': 'ocr_ocpm_bitmarck', 'TABLE': 'o_Care_Application', 'ID_COLUMN': 'Insured_Person_ID',
    'CASE_COLUMNS': ['Application_ID', 'Case_Status', 'Application_Type', 'Application_Channel', 'Priority', 'Statutory_Track',
                     'Existing_Case_Flag', 'Existing_Case_ID', 'Application_Received_Timestamp', 'Decision_Sent_Timestamp', 'MD_Assessment_Required',
                     'Missing_Information_Flag', 'Missing_Information_Count'],
    # Add applicant attributes here once they exist in the ARIS table (they are used to pre-fill missing items), e.g.
    # 'Benefit_Type', 'GP_Name', 'GP_Address', 'Representative_Name', 'Vollmacht_On_File', 'Email', 'Address'
    'ATTRIBUTE_COLUMNS': [],
    # Name columns of the event log in ARIS (if it has them) – used ONLY to count other people with the same name,
    # to show that the exact insurance-number match avoided them. e.g. { 'first': 'First_Name', 'last': 'Last_Name' }
    'NAME_COLUMNS': None,
}

# ---- minimal JS semantics helpers ---------------------------------------------------------------------------
_WS = '\t\n\x0b\x0c\r \xa0  -     　﻿'   # JS String.prototype.trim set
_TRIM = re.compile(f'^[{_WS}]+|[{_WS}]+\\Z')


def _truthy(v) -> bool:
    if v is None or v is False:
        return False
    if isinstance(v, (int, float)):
        return not (v == 0 or (isinstance(v, float) and math.isnan(v)))
    if isinstance(v, str):
        return v != ''
    return True


def _num_str(x) -> str:
    """JS Number.prototype.toString()."""
    if isinstance(x, int):
        return str(x)
    if math.isnan(x):
        return 'NaN'
    if math.isinf(x):
        return 'Infinity' if x > 0 else '-Infinity'
    if x == 0:
        return '0'
    sign = '-' if x < 0 else ''
    m = re.fullmatch(r'(\d+)(?:\.(\d+))?(?:e([+-]\d+))?', repr(abs(x)))
    ip, fp, ex = m.group(1), m.group(2) or '', int(m.group(3) or 0)
    ds, n = ip + fp, len(ip) + ex
    stripped = ds.lstrip('0'); n -= len(ds) - len(stripped); ds = stripped.rstrip('0')
    k = len(ds)
    if k <= n <= 21:
        return sign + ds + '0' * (n - k)
    if 0 < n <= 21:
        return sign + ds[:n] + '.' + ds[n:]
    if -6 < n <= 0:
        return sign + '0.' + '0' * (-n) + ds
    e = n - 1
    es = ('+' if e > 0 else '-') + str(abs(e))
    return sign + (ds if k == 1 else ds[0] + '.' + ds[1:]) + 'e' + es


def _js_str(v) -> str:
    if v is None:
        return 'null'
    if v is True or v is False:
        return 'true' if v else 'false'
    if isinstance(v, (int, float)):
        return _num_str(v)
    if isinstance(v, str):
        return v
    if isinstance(v, list):
        return ','.join('' if x is None else _js_str(x) for x in v)
    if isinstance(v, dict):
        return '[object Object]'
    return str(v)


def _cfg(ctx) -> dict:
    return {**CONFIG, **((getattr(ctx, 'config', None) or {}).get('build_aris_history_query', {}) or {})}


def run(ctx, items: list[dict]) -> list[dict]:
    C = _cfg(ctx)
    cf = items[0] if items else {}
    ap = cf.get('applicant') if isinstance(cf.get('applicant'), dict) else {}
    ins = ap.get('insurance_number')
    id_ = _TRIM.sub('', _js_str(ins if _truthy(ins) else 'NONE')).replace("'", '')
    T = f'"{C["TABLE"]}"'

    def col(f):
        return f'{T}."{f}"'
    hit = f"{col(C['ID_COLUMN'])} = '{id_}'"
    field_scripts = [
        {'script': f"COUNT(CASE WHEN {hit} THEN {col('Application_ID')} END)", 'alias': 'H_Case_Count'},
        {'script': f"SUM(CASE WHEN {hit} THEN {col('Missing_Information_Count')} ELSE 0 END)", 'alias': 'H_Missing_Info_Requests'},
    ]
    NC = C['NAME_COLUMNS']
    if _truthy(NC) and _truthy(ap.get('last_name')):
        script = (f"COUNT(DISTINCT CASE WHEN {col(NC.get('last'))} = '{_js_str(ap['last_name']).replace(chr(39), '')}'"
                  + (f" AND {col(NC.get('first'))} = '{_js_str(ap['first_name']).replace(chr(39), '')}'"
                     if _truthy(NC.get('first')) and _truthy(ap.get('first_name')) else '')
                  + f" AND {col(C['ID_COLUMN'])} <> '{id_}' THEN {col(C['ID_COLUMN'])} END)")
        field_scripts.append({'alias': 'H_Namesakes', 'script': script})
    for f in [*C['CASE_COLUMNS'], *C['ATTRIBUTE_COLUMNS']]:
        field_scripts.append({'script': f'MAX(CASE WHEN {hit} THEN {col(f)} END)', 'alias': f})
    return [{'searched_for': id_, 'attribute_columns': copy.deepcopy(C['ATTRIBUTE_COLUMNS']),
             'query_body': {'tenantId': C['TENANT'], 'dataSetId': C['DATASET'],
                            'query': {'table': {'fieldScripts': field_scripts}, 'limit': 10}}}]
