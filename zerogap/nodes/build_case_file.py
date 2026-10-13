"""Build Case File — Python port of the n8n Code node ``Build_Case_File.js``.

Classifies the case documents and reconstructs the case timeline. Deterministic (no LLM): every value
comes from the documents themselves.

Two paths, exactly as in the JS:
  A) the trigger e-mail carries an application form (text of 'Extract Application PDF'): the form is parsed
     line by line (two-column layout, sections 1-14) and IS the application; stored documents only count
     if they belong to the same applicant.
  B) otherwise the case is rebuilt from the stored case documents ('Read Case Document Text').

JS regex semantics are reproduced with ``_jre`` (a small translator from JS regex source to Python ``re``):
``\\s`` = JS whitespace set (incl. NBSP / Unicode spaces), ``\\d``/``\\w``/``\\b`` ASCII-only, ``.`` excludes
JS line terminators, ``$`` only matches at the very end.
"""
from __future__ import annotations

import datetime as _dt
import email.utils as _eu
import functools
import math
import re
import unicodedata

# ----------------------------------------------------------------------------- JS compatibility helpers

# JS \s (WhiteSpace + LineTerminator), written as Python-re class content
_WS = r'\t\n\x0b\x0c\r \xa0  -     　﻿'
_WS_CHARS = '\t\n\x0b\x0c\r \xa0 ' + ''.join(chr(c) for c in range(0x2000, 0x200b)) + \
    '    　﻿'
_AW = 'A-Za-z0-9_'  # JS \w
_B = rf'(?:(?<=[{_AW}])(?![{_AW}])|(?<![{_AW}])(?=[{_AW}]))'  # JS \b (ASCII word chars)
_ALNUM_U = r'[^\W_]'  # [\p{L}\p{N}]


@functools.lru_cache(maxsize=None)
def _jre(src: str, flags: str = '') -> re.Pattern:
    """Compile a JS regex source with JS semantics for \\s \\d \\w \\b . $ and \\p{L}."""
    out, i, in_class = [], 0, False
    while i < len(src):
        c = src[i]
        if c == '\\':
            n = src[i + 1]
            if n == 'p' and src[i + 2] == '{':
                j = src.index('}', i)
                name = src[i + 3:j]
                if in_class or name != 'L':
                    raise ValueError(f'unsupported \\p in {src!r}')
                out.append(r'[^\W\d_]')
                i = j + 1
                continue
            if in_class:
                out.append({'s': _WS, 'd': '0-9', 'w': _AW}.get(n, '\\' + n))
            else:
                out.append({'s': f'[{_WS}]', 'S': f'[^{_WS}]', 'd': '[0-9]', 'D': '[^0-9]',
                            'w': f'[{_AW}]', 'W': f'[^{_AW}]', 'b': _B}.get(n, '\\' + n))
            i += 2
            continue
        if in_class:
            if c == ']':
                in_class = False
            out.append(c)
        elif c == '[':
            in_class = True
            out.append(c)
        elif c == '.':
            out.append('[^\n\r  ]')
        elif c == '$':
            out.append(r'\Z')
        else:
            out.append(c)
        i += 1
    return re.compile(''.join(out), re.IGNORECASE if 'i' in flags else 0)


def _truthy(v) -> bool:
    if isinstance(v, float) and math.isnan(v):
        return False
    if isinstance(v, (list, dict)):
        return True
    return bool(v)


def _js_str(v) -> str:
    """String(v || '')"""
    if not _truthy(v):
        return ''
    if v is True:
        return 'true'
    if isinstance(v, float) and v.is_integer():
        return str(int(v))
    return str(v)


def _trim(s: str) -> str:
    return s.strip(_WS_CHARS)


def _flat(t) -> str:
    # remove everything that is not a letter, number or whitespace; collapse whitespace to one line
    s = ''.join(ch for ch in _js_str(t) if unicodedata.category(ch)[0] in 'LN' or ch in _WS_CHARS)
    return _trim(_jre(r'\s+').sub(' ', s))


def _search(src, s, flags=''):
    return _jre(src, flags).search(s)


def _js_date(v):
    """new Date(v) -> aware UTC datetime, or None when JS would give an Invalid Date."""
    try:
        if isinstance(v, bool):
            v = int(v)
        if isinstance(v, (int, float)):
            if isinstance(v, float) and not math.isfinite(v):
                return None
            return _dt.datetime(1970, 1, 1, tzinfo=_dt.timezone.utc) + _dt.timedelta(milliseconds=int(v))
        s = str(v).strip()
        m = re.fullmatch(r'([+-]\d{6}|\d{4})(?:-(\d{2})(?:-(\d{2}))?)?'
                         r'(?:T(\d{2}):(\d{2})(?::(\d{2})(?:\.(\d+))?)?)?(Z|[+-]\d{2}:\d{2})?', s)
        if m:
            y, mo, d, hh, mi, ss, frac, tz = m.groups()
            dt = _dt.datetime(int(y), int(mo or 1), int(d or 1), int(hh or 0), int(mi or 0), int(ss or 0),
                              int(((frac or '0') + '000')[:3]) * 1000, tzinfo=_dt.timezone.utc)
            if tz and tz != 'Z':  # date-time without offset is local time in JS; n8n runs in UTC
                sign = 1 if tz[0] == '+' else -1
                dt -= sign * _dt.timedelta(hours=int(tz[1:3]), minutes=int(tz[4:6]))
            return dt
        try:  # RFC 2822 ("Wed, 08 Oct 2026 08:30:00 +0000"), as Gmail headers use
            dt = _eu.parsedate_to_datetime(s)
        except (TypeError, ValueError):
            dt = _dt.datetime.fromisoformat(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=_dt.timezone.utc)
        return dt.astimezone(_dt.timezone.utc)
    except (ValueError, OverflowError, TypeError):
        return None


def _js_number(v):
    if isinstance(v, (int, float)) and not isinstance(v, bool):
        return v
    s = _trim(str(v))
    if s == '':
        return 0
    try:
        return float(s)
    except ValueError:
        return float('nan')


# ----------------------------------------------------------------------------- document classification

# SOPs / work instructions that a broad document search may also return are not case documents
SOP_TITLES = ['operating procedure', 'work instruction', 'escalation procedure', 'control policy', 'ownership protocol']
TYPES = [  # order matters: first match wins (checked against the start of the document)
    {'key': 'objection', 'label': 'Widerspruch (objection letter)', 'p': ['objection letter']},
    {'key': 'decision', 'label': 'Pflegebescheid (care-level decision)',
     'p': ['bescheid über die feststellung', 'carelevel decision notice', 'care level decision notice']},
    {'key': 'gutachten', 'label': 'Gutachten record (MD assessment report)', 'p': ['assessment report', 'gutachten received']},
    {'key': 'resubmission', 'label': 'Nachreichung (resubmission cover)', 'p': ['nachreichung', 'resubmission of requested information']},
    {'key': 'confirmation', 'label': 'Letter of confirmation / next steps', 'p': ['what happens next', 'letter of confirmation']},
    {'key': 'vollmacht', 'label': 'Vollmacht (power of attorney)', 'p': ['power of attorney', ' vollmacht ']},
    {'key': 'questionnaire', 'label': 'Fragebogen (applicant questionnaire)', 'p': ['fragebogen', 'applicant questionnaire']},
    {'key': 'application', 'label': 'Antrag (care application)',
     'p': ['antrag auf feststellung', 'antrag auf leistungen', 'application for determination', 'application for care insurance']},
]


# ----------------------------------------------------------------------------- A) application form parser

_CHECKED = r'[☒☑✔✓⊠]|\[\s*[xX]\s*\]'
_BOX = r'^\s*([☒☑✔✓⊠☐□]|\[\s*[xX ]?\s*\])\s*'


def _parse_form(raw_text) -> dict:
    # strip synthetic-test banner/footer lines (they mention "Pflegegeld", "Signature" etc.)
    lines = [_trim(l) for l in _js_str(raw_text).replace('\r', '').split('\n')]
    lines = [l for l in lines if l and not _search(r'^SYNTHETIC TEST DATA', l, 'i') and not _search(r'^Test coverage:', l, 'i')]
    text = '\n'.join(lines)

    def is_blank(v):
        return (not v or bool(_search(r'^[_\s.\-–—]*$', v))
                or bool(_search(r'^\[?\s*(not signed|nicht unterschrieben|unsigned)\s*\]?$', v, 'i')))

    def looks_like_label(l):
        return bool(_search(_BOX, l) or _search(r'^\d{1,2}\.\s+[A-Z]', l) or _search(r'^[^:]{2,70}:\s*', l)
                    or _search(r'^\((Date and|no box|Datum und)', l, 'i'))

    # index of section headings "N. Title" so repeated labels can be scoped
    heading_idx: dict[str, int] = {}
    for i, l in enumerate(lines):
        m = _search(r'^(\d{1,2})\.\s+[A-Z]', l)
        if m and m.group(1) not in heading_idx:
            heading_idx[m.group(1)] = i

    def rng(sec):
        start = heading_idx.get(sec)
        if start is None:
            return 0, len(lines)
        # two-column forms: the text of the other column can sit between headings, so a section ends at the
        # next section of the SAME column (N+1); the last section of a column ends at the end of the text
        next_same = heading_idx.get(str(int(sec) + 1))
        if next_same is not None and next_same > start and int(sec) != 7:
            return start + 1, next_same
        nexts = [i for i in heading_idx.values() if i > start]
        return start + 1, (len(lines) if int(sec) in (7, 14) else (min(nexts) if nexts else len(lines)))

    # value of "Label: value" (value may wrap onto following line(s) or sit on the next line)
    def field(label_src, sec=None):
        a, b = rng(sec) if sec else (0, len(lines))
        rx = _jre('^(?:' + label_src + r')[^:]*:\s*(.*)$', 'i')
        for i in range(a, b):
            m = rx.search(lines[i])
            if not m:
                continue
            v = _trim(m.group(1))
            j = i + 1
            # value on next line (e.g. "Signature:\nIngrid Hoffmann") or wrapped continuation
            while (j < len(lines) and j < i + 3 and not looks_like_label(lines[j]) and
                   (v == '' or _search(r'[,&\/(]$', v) or _search(r'^[a-zäöüß(]', lines[j])
                    or _search(r'^\d{5}\s', lines[j])
                    or (len(_jre(r'\s+').split(lines[j])) <= 3 and not _search(r'[.:]$', lines[j])))):
                v = _trim(v + ' ' + lines[j])
                j += 1
            return None if is_blank(v) else _trim(_jre(r'\s+').sub(' ', v))
        return None

    # checkbox state for a unique label
    def box(label_src, flags=''):
        rx = _jre(label_src, flags)
        l = next((x for x in lines if _search(_BOX, x) and rx.search(x)), None)
        if l is None:
            return None  # label not found on form
        return bool(_search(_CHECKED, _search(_BOX, l).group(1)))

    # ---- Insurance number (label is unique; may appear anywhere after PDF edits)
    ins_line = _search(r'Versichertennummer[^\n:]*:\s*([^\n]*)', text, 'i')
    insurance_number = None
    if ins_line:
        mm = _search(r'^[A-Z0-9][A-Z0-9\-\/]*', _trim(ins_line.group(1)), 'i')
        insurance_number = mm.group(0) if mm else None
    if insurance_number:
        insurance_number = insurance_number.upper()

    # ---- representation: remove the template label text before looking for the enclosure statement
    r3a, r3b = rng('3')
    rep_text = ' '.join(lines[r3a:r3b])
    rep_text = _jre(r'guardianship certificate \(Betreuungsausweis\) enclosed', 'i').sub('', rep_text)
    rep_text = _jre(r'copy of power of attorney \(Vorsorgevollmacht\) enclosed', 'i').sub('', rep_text)

    benefits_map = [
        ('Pflegegeld', (r'\(Pflegegeld\)', ''), True),
        ('Pflegesachleistung', (r'\(Pflegesachleistung\)', ''), True),
        ('Kombinationsleistung', (r'\(Kombinationsleistung\)', ''), True),
        ('Teilstationäre Pflege (Tages-/Nachtpflege)', (r'Day or Night Care|Tages- oder Nachtpflege', 'i'), False),
        ('Vollstationäre Pflege', (r'\(Vollstationäre Pflege\)', ''), True),
        ('Vollstationäre Pflege in Einrichtungen der Eingliederungshilfe', (r'Disability Assistance Facilities|Eingliederungshilfe', 'i'), False),
    ]

    form = {
        'application_type': 'initial' if box(r'Initial Application|Erstantrag', 'i')
        else 're-assessment' if box(r'Re-assessment|Höherstufung|Wiederholung', 'i')
        else 'change_of_benefit' if box(r'Change of Care Benefit|Änderung', 'i') else None,
        'applicant': {
            'last_name': field(r'Last Name|Name(?! of)', '2'),
            'first_name': field(r'First Name|Vorname', '2'),
            'date_of_birth': field(r'Date of Birth|Geburtsdatum', '2'),
            'address': field(r'Address|Anschrift', '2'),
            'telephone': field(r'Telephone|Telefon', '2'),
            'insurance_number': insurance_number,
        },
        'representation': {
            'legal_guardian': box(r'Legal Guardian|Betreuer\b'),
            'authorized_person': box(r'Authorized Person|Bevollmächtigte'),
            'name': field(r'Name, First Name', '3'),
            'address': field(r'Address', '3'),
            'telephone': field(r'Telephone', '3'),
            'poa_declared_enclosed': bool(_search(
                r'(Vorsorgevollmacht|power of attorney)\)?.{0,60}?(enclosed|beigefügt|anbei|Anlage|Annex)', rep_text, 'i')),
            'guardianship_cert_declared_enclosed': bool(_search(
                r'(Betreuungsausweis|guardianship certificate)\)?.{0,60}?(enclosed|beigefügt|anbei|Anlage|Annex)', rep_text, 'i')),
        },
        'benefits_selected': [n for n, rx, _ in benefits_map if box(*rx)],
        'benefits_core_selected': [n for n, rx, core in benefits_map if core and box(*rx)],
        'medical': {
            'gp_name': field(r'General Practitioner \/ Hausarzt — Name|Hausarzt.*Name', '5'),
            'gp_address': field(r'General Practitioner \/ Hausarzt — Address|Hausarzt.*Anschrift', '5'),
            'gp_telephone': field(r'General Practitioner \/ Hausarzt — Telephone|Hausarzt.*Telefon', '5'),
            'specialists': field(r'Specialist|Fachärzt', '5'),
        },
        'care_provider': {
            'relative': box(r'Spouse \/ Relative|Angehörige', 'i'),
            'employed_caregiver': box(r'Employed Caregiver', 'i'),
            'care_service': box(r'Care Service \/ Care Home|Pflegedienst', 'i'),
            'name': field(r'Name of Caregiver', '6'),
            'address': field(r'Address', '6'),
            'telephone': field(r'Telephone Number', '6'),
            'since': field(r'Since When', '6'),
        },
        'cause_accident_or_damage': any(box(k, 'i') for k in ['Occupational Accident', 'War Service Damage', 'Other Damage']),
        'prior_applications_elsewhere': any(box(k, 'i') for k in ['foreign benefit providers', 'social welfare providers', 'other benefit providers']),
        'civil_service': {
            'is_civil_servant': box(r'employed in the public service \(Beamter|Beamter\/-in', 'i'),
            'survivor_pension': box(r"survivor's pension", 'i'),
            'spouse_or_parent_civil_servant': box(r'spouse \/ parent was \/ is employed', 'i'),
            'beihilfestelle_verified': box(r'Beihilfestelle', 'i'),
            'beihilfestelle_reference': field(r'Beihilfestelle — Name|Beihilfestelle.*Reference'),
        },
        'confidentiality_release_accepted': box(r'Declaration accepted|Erklärung', 'i'),
        'execution': {
            'date': field(r'Date(?! of Birth)|Datum', '14'),
            'location': field(r'Location|Ort', '14'),
            'signature': field(r'Signature|Unterschrift', '14'),
        },
    }
    cs = form['civil_service']
    cs['any'] = bool(cs['is_civil_servant'] or cs['survivor_pension'] or cs['spouse_or_parent_civil_servant'])

    # ---- accelerated statutory deadline triggers (Req #12) – keywords chosen so they never occur in the blank template
    trigger_defs = [
        (r'krankenhaus|hospitali[sz]|hospital stay|im Krankenhaus|stationäre[rn]? Aufenthalt', 'Krankenhausaufenthalt', 5),
        (r'\breha\b|rehabilitation', 'Stationäre Rehabilitation', 5),
        (r'hospiz|hospice|palliativ', 'Hospiz / Palliativversorgung', 5),
        (r'familienpflegezeit|pflegezeit|family care leave|care leave', 'Pflegezeit / Familienpflegezeit angekündigt', 10),
    ]
    hits = [t for t in trigger_defs if _search(t[0], text, 'i')]
    form['sla_triggers'] = [h[1] for h in hits]
    form['urgent_sla_days'] = min(h[2] for h in hits) if hits else None

    # ---- parser self-check: how many template anchors did we recognise? (guardrail for unknown layouts / scans)
    anchors = [r'Versichertennummer', r'Type of Benefit|Leistungsart', r'Hausarzt', r'Who Will Provide Care|Pflegeperson',
               r'Execution|Unterschrift|Signature']
    form['parser_confidence'] = sum(1 for a in anchors if _search(a, text, 'i')) / len(anchors)
    form['text_length'] = len(text)
    return form


def _from_form(ctx, form_text: str, docs: list[dict], skipped: list[dict]) -> list[dict]:
    f = _parse_form(form_text)
    A = f.get('applicant') or {}
    full_name = ' '.join(x for x in [A.get('first_name'), A.get('last_name')] if x)
    r = f.get('representation') or {}
    rep = None
    if (r.get('legal_guardian') or r.get('authorized_person')) and r.get('name'):
        m = _search(r'^([^,(]+),\s*([^,(]+)', r['name'])
        rel = _search(r'\(([^,)]+)', r['name'])
        rep = {'name': f"{_trim(m.group(2))} {_trim(m.group(1))}" if m else _trim(r['name'].split('(')[0]),
               'relationship': _trim(rel.group(1) if rel else '') or None,
               'address': r.get('address') or None,
               'kind': 'legal guardian' if r.get('legal_guardian') else 'authorized person'}
    try:
        mail = ctx.node('Gmail Trigger').first() or {}
    except KeyError:
        mail = {}
    if _truthy(mail.get('date')):
        md = _js_date(mail['date'])
    elif _truthy(mail.get('internalDate')):
        md = _js_date(_js_number(mail['internalDate']))
    else:
        md = ctx.now
    recv = (md or ctx.now).astimezone(_dt.timezone.utc).strftime('%Y-%m-%d')
    declared = r.get('guardianship_cert_declared_enclosed') if r.get('legal_guardian') else r.get('poa_declared_enclosed')
    flat_form = _flat(form_text).lower()
    rep_id_ok = bool(_search(
        r'(identity (document|card|proof)|id card|personalausweis)[\s\w]{0,40}(of the )?(representative|guardian|authori[sz]ed person|bevollmächtigt)'
        r'|(representatives?|guardians?) (identity|id card|personalausweis)', flat_form))
    email_m = _search(r'E-?Mail[^:\n]*:\s*([\w.+-]+@[\w-]+(\.[\w-]+)+)', form_text, 'i')
    f_num = _flat(A.get('insurance_number') or '').lower()
    own = [d for d in docs if (full_name and full_name in d['text']) or (f_num and f_num in d['text'].lower())]
    medical = f.get('medical') or {}
    execution = f.get('execution') or {}
    return [{
        'source': 'email_form', 'case_ref': None, 'insurer': 'Care Insurance Fund',
        'applicant': {'first_name': A.get('first_name') or None, 'last_name': A.get('last_name') or None,
                      'date_of_birth': A.get('date_of_birth') or None, 'address': A.get('address') or None,
                      'insurance_number': A.get('insurance_number') or None,
                      'email': email_m.group(1) if email_m else None, 'contact': A.get('telephone') or None},
        'representative': rep,
        'vollmacht': {
            'representative': rep['name'], 'relationship': rep['relationship'], 'signed': True, 'names_applicant': True,
            'matches': bool(declared), 'received_on': recv if declared else None,
            'received_via': ('declared as enclosed with the application ('
                             + ('Betreuungsausweis' if rep['kind'] == 'legal guardian' else 'Vollmacht') + ')') if declared else None,
        } if rep else None,
        'rep_identity': {'verified': rep_id_ok,
                         'evidence': 'Identity document of the representative enclosed with the application' if rep_id_ok else None,
                         'applicant_only_note': None} if rep else {'verified': False},
        'application_type': f.get('application_type'), 'signature': execution.get('signature') or None,
        'benefits_selected': f.get('benefits_selected') or [],
        'gp': ', '.join(x for x in [medical.get('gp_name'), medical.get('gp_address')] if x) if medical.get('gp_name') else None,
        'sla_triggers': f.get('sla_triggers') or [],
        'civil_servant': bool((f.get('civil_service') or {}).get('is_civil_servant')), 'medical_docs': [],
        'timeline': {'received': recv}, 'form_date': execution.get('date') or None,
        'link': {'method': 'insurance number', 'exact': bool(A.get('insurance_number')), 'documents_on_case': 1 + len(own)},
        'documents': [{'name': 'Application form (email attachment)', 'type': 'application', 'label': 'Antrag (application form)'}]
        + [_doc_ref(d) for d in own],
        'other_documents': [], 'skipped_documents': skipped,
    }]


# ----------------------------------------------------------------------------- B) case documents from storage

_INS_LABEL = ('re', r'(?:\p{L}+ )?(?:member number|insurance number|versichertennummer)')  # JS RegExp label (flags iu)
# labels per document type (the next label ends the previous value)
L = {
    'application': ['Applicant name', 'Name of applicant', 'Date of birth', 'Geburtsdatum', 'Address', 'Anschrift', _INS_LABEL,
                    'Case reference assigned on receipt', 'Case reference', 'Type of application', 'Authorized representative if any',
                    'Authorized representative', 'Preferred contact', 'Telephone', 'General Practitioner', 'Hausarzt', 'Date submitted',
                    'Signature', 'Reason for Application', 'Declaration', 'Illustrative'],
    'vollmacht': ['Authorized representative', 'Relationship to applicant', 'Address', 'Valid from', 'Scope', 'Signed', 'Countersigned',
                  'Date', 'Identity Verification Note', 'Illustrative'],
    'questionnaire': ['Case reference', 'Completed by', 'Living situation', 'Main areas of difficulty', 'Current support in place',
                      'Assessment location preference', 'Availability for assessment', 'Existing care aids in use',
                      'General Practitioner', 'Hausarzt', 'Additional remarks', 'This questionnaire', 'Illustrative'],
    'resubmission': ['Case reference', 'Applicant', 'In response to request dated', 'Documents enclosed', 'Submitted by',
                     'Date submitted', 'Please find enclosed', 'Illustrative'],
    'gutachten': ['Case reference', 'Applicant', 'Assessment order transmitted to MD on', 'Assessment report Gutachten received on',
                  'Assigned to case by', 'Report status', 'Attachment', 'Illustrative'],
    'decision': ['Case reference', 'Applicant', 'Application received', 'Decision date', 'Decision', 'Begründung',
                 'Your Right to Object', 'Illustrative'],
    'objection': ['Case reference', 'Decision being contested', 'Submitted by', 'Date submitted', 'On behalf', 'Signed', 'Illustrative'],
}


def _doc_ref(d: dict) -> dict:
    """{name, type, label}; a document without a name has name undefined in JS, i.e. no key in JSON."""
    out = {'name': d['name']} if 'name' in d else {}
    out.update(type=d['type'], label=d['label'])
    return out


def _lab_key(lab) -> str:
    return lab[1] if isinstance(lab, tuple) else lab


def _occurrences(doc: dict, cache: dict) -> list[dict]:
    if id(doc) in cache:
        return cache[id(doc)]
    occ = []
    for lab in L.get(doc['type']) or []:
        if isinstance(lab, tuple):
            rx = _jre(lab[1], 'i')
        else:
            rx = re.compile(f'(?<!{_ALNUM_U}){re.escape(lab)}(?!{_ALNUM_U})', re.IGNORECASE)
        for m in rx.finditer(doc['text']):
            occ.append({'lab': _lab_key(lab), 's': m.start(), 'e': m.end()})
    # drop matches inside a longer label match ("Date" inside "Date submitted", "Applicant" inside "Relationship to applicant")
    kept = [o for k, o in enumerate(occ)
            if not any(j != k and x['s'] <= o['s'] and x['e'] >= o['e'] and (x['e'] - x['s']) > (o['e'] - o['s'])
                       for j, x in enumerate(occ))]
    kept.sort(key=lambda o: o['s'])
    cache[id(doc)] = kept
    return kept


def _make_field(cache):
    def field(doc, labels, frm=0):
        if not doc:
            return None
        occ = _occurrences(doc, cache)
        for lab in labels:
            key = _lab_key(lab).lower()
            i = next((k for k, o in enumerate(occ) if o['lab'].lower() == key and o['s'] >= frm), -1)
            if i < 0:
                continue
            end = occ[i + 1]['s'] if i + 1 < len(occ) else len(doc['text'])
            v = _trim(doc['text'][occ[i]['e']:min(end, occ[i]['e'] + 200)])
            if v:
                return v
        return None
    return field


MON = {'january': 1, 'february': 2, 'march': 3, 'april': 4, 'may': 5, 'june': 6, 'july': 7, 'august': 8, 'september': 9,
       'october': 10, 'november': 11, 'december': 12,
       'januar': 1, 'februar': 2, 'märz': 3, 'maerz': 3, 'juni': 6, 'juli': 7, 'oktober': 10, 'dezember': 12}


def _pad(n) -> str:
    return str(n).rjust(2, '0')


def _date(v):
    if not _truthy(v):
        return None
    s = _js_str(v)
    m = _search(r'\b(\d{1,2})\s*(januar[y]?|februar[y]?|march|märz|maerz|april|may|mai|june|juni|july|juli|august|september|october|oktober'
                r'|november|december|dezember)\s+(\d{4})', s, 'i')
    if m:
        k = m.group(2).lower()
        mo = MON.get(k) or MON.get(re.sub(r'y\Z', '', k)) or (5 if k == 'mai' else None)
        if mo:
            return f'{m.group(3)}-{_pad(mo)}-{_pad(m.group(1))}'
    m = _search(r'\b(\d{2})(\d{2})(\d{4})\b', s)  # 04031958 (ARIS strips the dots)
    if m:
        return f'{m.group(3)}-{m.group(2)}-{m.group(1)}'
    m = _search(r'\b(\d{1,2})\s(\d{1,2})\s(\d{4})\b', s)
    if m:
        return f'{m.group(3)}-{_pad(m.group(2))}-{_pad(m.group(1))}'
    m = _search(r'\b(\d{4})(\d{2})(\d{2})\b', s)
    if m and int(m.group(2)) <= 12:
        return f'{m.group(1)}-{m.group(2)}-{m.group(3)}'
    return None


def _de(iso):
    return f'{iso[8:10]}.{iso[5:7]}.{iso[0:4]}' if iso else None


def _from_documents(raw: list[dict], docs: list[dict], skipped: list[dict]) -> list[dict]:
    field = _make_field({})

    # ---- the application decides which case is reviewed; other documents must belong to the same case
    app = next((d for d in docs if d['type'] == 'application'), None)
    cr = field(app, ['Case reference assigned on receipt', 'Case reference'])
    case_ref = (cr.split(' ')[0] if cr is not None else None) or None
    full_name = field(app, ['Applicant name', 'Name of applicant']) or ''

    def same_case(d):
        return d is app or not case_ref or case_ref in d['text'] or bool(full_name and full_name in d['text'])

    def by_type(k):
        return next((d for d in docs if d['type'] == k and same_case(d)), None)

    q, vm, rs = by_type('questionnaire'), by_type('vollmacht'), by_type('resubmission')
    gut, dec, conf, obj = by_type('gutachten'), by_type('decision'), by_type('confirmation'), by_type('objection')

    name_parts = [w for w in full_name.split(' ') if w]
    rep_raw = field(app, ['Authorized representative if any', 'Authorized representative'])
    REL = (r'^(.*?)\s+(son|daughter|spouse|husband|wife|partner|mother|father|brother|sister|child|guardian|friend|neighbou?r|carer'
           r'|sohn|tochter|ehefrau|ehemann|ehepartner|betreuerin?)\b\s*(.*)$')
    representative = None
    if rep_raw and not _search(r'^(none|n a|na|keine?|nein|no)$', rep_raw, 'i'):
        m = _search(REL, rep_raw, 'i')
        name = m.group(1) if m else ' '.join([w for w in rep_raw.split(' ') if not _search(r'\d', w)][:2])
        representative = {'name': name, 'relationship': m.group(2).lower() if m else None,
                          'address': m.group(3) if m and m.group(3) else None}
    intake = ' '.join(d['text'] for d in [app, q] if d).lower()
    raw_app = next((d for d in raw if _truthy(d.get('readable')) and app is not None and _flat(d.get('text')) == app['text']), None)
    raw_app_text = _js_str(raw_app.get('text')) if raw_app else ''

    dob = _date(field(app, ['Date of birth', 'Geburtsdatum']))
    ins = field(app, [_INS_LABEL])
    email_m = _search(r'[\w.+-]+@[\w-]+(\.[\w-]+)+', raw_app_text)
    applicant = {
        'first_name': name_parts[0] if name_parts else None, 'last_name': ' '.join(name_parts[1:]) or None,
        'date_of_birth': _de(dob),
        'address': field(app, ['Address', 'Anschrift']),
        'insurance_number': (ins.split(' ')[0] if ins is not None else None) or None,
        # e-mail addresses only survive in sources that keep punctuation (Google Docs); ARIS text strips "@" and "."
        'email': email_m.group(0) if email_m else None,
        'contact': _jre(r'\s*\bE ?mail\b.*$', 'i').sub('', field(app, ['Preferred contact', 'Telephone']) or '', count=1) or None,
    }

    BENEFITS = [('Pflegegeld', r'pflegegeld|care allowance'), ('Pflegesachleistung', r'pflegesachleistung|noncash care|in ?kind care'),
                ('Kombinationsleistung', r'kombinationsleistung|combination benefit'),
                ('Vollstationäre Pflege', r'vollstationär|fully inpatient care|fulltime inpatient')]
    benefits_selected = [b for b, rx in BENEFITS if _search(rx, intake)]
    gp = field(app, ['General Practitioner', 'Hausarzt']) or field(q, ['General Practitioner', 'Hausarzt'])
    triggers = []
    if _search(r'\b(currently|is|am) (in hospital|hospitali[sz]ed|in an inpatient rehab)|krankenhaus|stationäre reha', intake):
        triggers.append('hospital / inpatient rehabilitation')
    if _search(r'pflegezeit|familienpflegezeit|caregiver leave|family care leave', intake):
        triggers.append('caregiver leave (Pflegezeit)')
    civil_servant = bool(_search(r'\bbeamte|civil servant|beihilfe', intake))
    medical_docs = [d.get('name') for d in docs if same_case(d) and
                    _search(r'pflegetagebuch|care diary|entlassungsbericht|discharge report', d['text'][:400].lower())]

    # ---- Vollmacht: content + WHEN it reached the insurer (a resubmission listing it means it was not on file before)
    enclosed = (field(rs, ['Documents enclosed']) or '').lower()
    vollmacht = None
    if vm:
        vt = vm['text']
        poa_late = bool(_search(r'vollmacht|power of attorney', enclosed))
        names_applicant = bool(full_name) and full_name in vt
        dob_matches = (bool(dob) and (dob[8:10] + dob[5:7] + dob[0:4]) in _jre(r'\D').sub(' ', vt)) or \
            (bool(dob) and _date(vt[vt.lower().find('born'):]) == dob)
        vollmacht = {
            'representative': field(vm, ['Authorized representative']),
            'relationship': field(vm, ['Relationship to applicant']),
            'scope': field(vm, ['Scope']),
            'valid_from': _date(field(vm, ['Valid from'])),
            'signed': bool(field(vm, ['Signed'])),
            'names_applicant': names_applicant,
            'applicant_dob_matches': dob_matches,
            'received_on': _date(field(rs, ['Date submitted'])) if poa_late else _date(field(vm, ['Date'])),
            'received_via': 'Nachreichung (resubmission)' if poa_late else 'with the application',
        }
        vollmacht['matches'] = bool(vollmacht['representative'] and representative and
                                    representative['name'].lower().split(' ')[-1] in vollmacht['representative'].lower()
                                    and vollmacht['names_applicant'] and vollmacht['signed'])

    # ---- identity proof of the REPRESENTATIVE (an identity note that only covers the applicant does not count)
    rep_identity = {'verified': False, 'evidence': None, 'applicant_only_note': None}
    if representative:
        rep_low = representative['name'].lower()
        for d in [d for d in docs if same_case(d)]:
            t = d['text'].lower()
            i = -1
            while True:
                i = t.find('identity', i + 1)
                if i < 0:
                    break
                win = t[max(0, i - 40):i + 220]
                about_rep = bool(_search(r'representatives? (identity|id)|identity (document|card|proof) of (the )?(authori[sz]ed )?representative', win)) \
                    or (rep_low in win and not _search(r'applicants? identity', win))
                if about_rep:
                    rep_identity = {'verified': True, 'evidence': f"{d['label']}: {d['text'][i:i + 120]}…", 'applicant_only_note': None}
                    break
                if _search(r'applicants? identity', win) and not rep_identity['applicant_only_note']:
                    rep_identity['applicant_only_note'] = f"{d['label']}: identity check covers the applicant only"
            if rep_identity['verified']:
                break

    # ---- timeline (every date taken from a document)
    timeline = {
        'received': _date(field(app, ['Date submitted'])) or _date(field(dec, ['Application received'])),
        'md_order': _date(field(gut, ['Assessment order transmitted to MD on'])),
        'request_dispatched': _date(field(rs, ['In response to request dated'])),
        'confirmation_letter': _date(conf['text']) if conf else None,
        'resubmission_received': _date(field(rs, ['Date submitted'])),
        'gutachten_received': _date(field(gut, ['Assessment report Gutachten received on'])),
        'decision': _date(field(dec, ['Decision date'])),
        'objection': _date(field(obj, ['Date submitted'])),
    }
    head_words = (app['text'] if app else '').split(' ')
    i_ins = next((k for k, w in enumerate(head_words) if _search(r'kasse|versicherung|insurance|fund', w, 'i')), -1)
    insurer = ' '.join(head_words[:i_ins + 1]) if 0 <= i_ins < 6 else 'Care Insurance Fund'
    decision_text = None
    if dec:
        m = _search(r'Decision date \d{1,2} \p{L}+ \d{4} Decision (.+?) (Begründung|Reasoning|Your Right)', dec['text'], 'i')
        decision_text = (m.group(1) if m else None) or None

    on_case = [d for d in docs if same_case(d)]
    return [{
        'case_ref': case_ref, 'insurer': insurer,
        'applicant': applicant, 'representative': representative, 'vollmacht': vollmacht, 'rep_identity': rep_identity,
        'application_type': field(app, ['Type of application']),
        'signature': field(app, ['Signature']),
        'benefits_selected': benefits_selected, 'gp': gp or None, 'sla_triggers': triggers, 'civil_servant': civil_servant,
        'medical_docs': medical_docs,
        'decision_text': decision_text,
        'timeline': timeline,
        'link': {'method': 'case reference + member number' if case_ref else 'none',
                 'exact': bool(case_ref and applicant['insurance_number']), 'documents_on_case': len(on_case)},
        'documents': [_doc_ref(d) for d in on_case],
        'other_documents': [d.get('name') for d in docs if not same_case(d)],
        'skipped_documents': skipped,
    }]


# ----------------------------------------------------------------------------- entry point

def run(ctx, items) -> list[dict]:
    # ARIS returns document text without punctuation or line breaks, Google Docs keep them. To treat both sources
    # identically every document is flattened the same way and fields are read as "text between this label and the next".
    raw = list(ctx.node('Read Case Document Text').all())
    skipped = []
    for d in raw:
        if not _truthy(d.get('readable')) and _truthy(d.get('name')):
            s = {'name': d['name']}
            if 'note' in d:  # undefined -> key absent in JSON
                s['note'] = d['note']
            skipped.append(s)

    docs = []
    for d in raw:
        if not _truthy(d.get('readable')):
            continue
        f = _flat(d.get('text'))
        head = ' ' + f.lower()[:260] + ' '
        is_sop = any(t in head for t in SOP_TITLES)
        t = None if is_sop else next((x for x in TYPES if any(p in head for p in x['p'])), None)
        if is_sop:
            continue
        docs.append({**({'name': d['name']} if 'name' in d else {}), 'type': t['key'] if t else 'other', 'label': t['label'] if t else 'Other document',
                     'text': f, 'rawText': d.get('text')})

    # A) application form received by email (PDF attachment): when present, that form IS the application
    form_text = None
    try:
        j = ctx.node('Extract Application PDF').first()
        if isinstance(j.get('text'), str) and _search(r'Application for Care Insurance Benefits|Antrag auf Leistungen', j['text'], 'i'):
            form_text = j['text']
    except (KeyError, AttributeError):
        form_text = None
    if form_text:
        return _from_form(ctx, form_text, docs, skipped)
    # B) case documents from storage
    return _from_documents(raw, docs, skipped)
