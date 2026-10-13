"""Create Letter PDF — renders the outreach letter, escalation note or release note as an A4 PDF.
Dependency-free PDF writer (Helvetica, WinAnsi), ported 1:1 from the n8n node. The PDF bytes are returned under
item['__binary__']['letter'] (kept out of the stored JSON)."""
from __future__ import annotations
import re

_W = [278, 278, 355, 556, 556, 889, 667, 191, 333, 333, 389, 584, 278, 333, 278, 278, 556, 556, 556, 556, 556, 556, 556, 556, 556, 556,
      278, 278, 584, 584, 584, 556, 1015, 667, 667, 722, 722, 667, 611, 778, 722, 278, 500, 667, 556, 833, 722, 778, 667, 778, 722, 667,
      611, 722, 667, 944, 667, 667, 611, 278, 278, 278, 469, 556, 333, 556, 556, 500, 556, 556, 278, 556, 556, 222, 222, 500, 222, 833,
      556, 556, 556, 556, 333, 500, 278, 556, 500, 722, 500, 500, 500, 334, 260, 334, 584]
_MAP = {'–': 0x96, '—': 0x97, '‘': 0x91, '’': 0x92, '“': 0x93, '”': 0x94, '€': 0x80, '•': 0x95, '…': 0x85, ' ': 0x20}
_MON = ['January', 'February', 'March', 'April', 'May', 'June', 'July', 'August', 'September', 'October', 'November', 'December']


def _enc(s) -> str:
    out = []
    for ch in str('' if s is None else s):
        c = ord(ch)
        b = _MAP.get(ch, c if (c < 256 and not (0x80 <= c < 0xA0)) else 63)
        x = chr(b)
        out.append('\\' + x if x in '()\\' else x)
    return ''.join(out)


def _width(s, size, bold=False) -> float:
    t = 0
    for ch in str(s):
        c = ord(ch)
        t += _W[c - 32] if 32 <= c <= 126 else (556 if ch == '–' else 1000 if ch == '—' else 556)
    return t * size / 1000 * (1.06 if bold else 1)


def _wrap(text, size, max_w, bold=False):
    out, line = [], ''
    for word in [w for w in re.split(r'\s+', str(text)) if w]:
        t = f'{line} {word}' if line else word
        if _width(t, size, bold) > max_w and line:
            out.append(line); line = word
        else:
            line = t
    out.append(line)
    return out


def _fx(v: float) -> str:
    return f'{v:.2f}'


def build_letter_pdf(o: dict) -> bytes:
    PW, PH, L = 595.28, 841.89, 72
    R = PW - 64; TW = R - L
    pages = [[]]
    st = {'y': PH - 64}

    def op(s): pages[-1].append(s)

    def new_page():
        pages.append([]); st['y'] = PH - 72

    def text(x, yy, s, size=10.5, bold=False, gray=0):
        op(f"BT {gray:g} g /{'F2' if bold else 'F1'} {size:g} Tf {_fx(x)} {_fx(yy)} Td ({_enc(s)}) Tj ET")

    def need(h):
        if st['y'] - h < 72:
            new_page()

    y = st['y']
    text(L, y, o.get('sender') or 'Care Insurance Fund', 15, True, 0.12)
    text(L, y - 16, o.get('senderLine') or 'Long-term care insurance · Application intake', 9, False, 0.45)
    op(f'0.85 G 0.8 w {L} {_fx(y - 26)} m {R:g} {_fx(y - 26)} l S')

    def right(s, yy, size=9.5, bold=False):
        text(R - _width(s, size, bold), yy, s, size, bold, 0.3)

    y -= 58
    right(o.get('dateText', ''), y)
    refs = o.get('refs') or []
    for i, r in enumerate(refs):
        right(r, y - 13 * (i + 1))
    rec = o.get('recipient') or []
    for i, line in enumerate(rec):
        text(L, y - 14 * i, line, 10.5, i == 0, 0.1)
    y -= max(14 * len(rec), 13 * (len(refs) + 1)) + 30
    st['y'] = y
    for line in _wrap(o.get('subject', ''), 11.5, TW, True):
        need(16); text(L, st['y'], line, 11.5, True, 0.1); st['y'] -= 16
    st['y'] -= 12
    size, lh = 10.5, 15
    for raw in str(o.get('body') or '').replace('\r', '').split('\n'):
        line_txt = raw.strip()
        if not line_txt:
            st['y'] -= lh * 0.6; continue
        m = re.match(r'^(\d+[.)]|[-•])\s+(.*)$', line_txt)
        indent = 18 if m else 0
        parts = _wrap(m.group(2) if m else line_txt, size, TW - indent, False)
        for i, p in enumerate(parts):
            need(lh)
            if m and i == 0:
                text(L, st['y'], m.group(1), size, False, 0.1)
            text(L + indent, st['y'], p, size, False, 0.1)
            st['y'] -= lh

    objs: list[str] = []

    def add(s):
        objs.append(s); return len(objs)

    catalog, pages_obj = add(''), add('')
    f1 = add('<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica /Encoding /WinAnsiEncoding >>')
    f2 = add('<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica-Bold /Encoding /WinAnsiEncoding >>')
    kids = []
    for i, ops in enumerate(pages):
        footer_txt = (o['footer'] + '   ·   ' if o.get('footer') else '') + f'Page {i + 1} of {len(pages)}'
        content = '\n'.join(ops + [f'BT 0.55 g /F1 8 Tf {L} 40 Td ({_enc(footer_txt)}) Tj ET'])
        c = add(f'<< /Length {len(content.encode("latin-1", "replace"))} >>\nstream\n{content}\nendstream')
        kids.append(add(f'<< /Type /Page /Parent {pages_obj} 0 R /MediaBox [0 0 {PW} {PH}] /Resources << /Font << /F1 {f1} 0 R /F2 {f2} 0 R >> >> /Contents {c} 0 R >>'))
    objs[catalog - 1] = f'<< /Type /Catalog /Pages {pages_obj} 0 R >>'
    objs[pages_obj - 1] = f"<< /Type /Pages /Kids [{' '.join(f'{k} 0 R' for k in kids)}] /Count {len(kids)} >>"
    pdf = b'%PDF-1.4\n%\xe2\xe3\xcf\xd3\n'
    offs = []
    for i, body in enumerate(objs):
        offs.append(len(pdf))
        pdf += f'{i + 1} 0 obj\n{body}\nendobj\n'.encode('latin-1', 'replace')
    xref = len(pdf)
    pdf += f'xref\n0 {len(objs) + 1}\n0000000000 65535 f \n'.encode() + ''.join(f'{x:010d} 00000 n \n' for x in offs).encode()
    pdf += f'trailer\n<< /Size {len(objs) + 1} /Root {catalog} 0 R >>\nstartxref\n{xref}\n%%EOF'.encode()
    return pdf


def run(ctx, items: list[dict]) -> list[dict]:
    b = dict(items[0]) if items else {}
    d = b.get('drafts') or {}
    is_esc, is_ready = b.get('route') == 'escalation', b.get('route') == 'none'
    if not d.get('outreach_letter') and not is_esc and not is_ready:
        return [b]
    f = b.get('form') or {}
    a, r = f.get('applicant') or {}, f.get('representation') or {}
    via_rep = bool(r.get('name') and d.get('addressee') and d.get('addressee') == r.get('name'))
    address = [x.strip() for x in str((r.get('address') if via_rep else a.get('address')) or '').split(',') if x.strip()]
    now = ctx.now
    name = b.get('applicant_name') or ''
    insurer = b.get('insurer') or 'Care Insurance Fund'
    pdf = build_letter_pdf({
        'sender': insurer,
        'senderLine': 'Long-term care insurance · Application intake',
        'dateText': f'{now.day} {_MON[now.month - 1]} {now.year}',
        'refs': ['Member number: ' + (b.get('insurance_number') or '–'), 'Case: ' + (b.get('application_id') or 'new application')],
        'recipient': (['Case file', b.get('application_id') or b.get('insurance_number') or ''] if is_ready
                      else ['Case Manager', 'Long-Term Care Claims Operations'] if is_esc
                      else [d.get('addressee') or name, *address]),
        'subject': (f'Release for assessment – {name}' if is_ready
                    else f"Escalation – phone outreach required – {name} (case {b.get('application_id') or ''})" if is_esc
                    else 'Your application for long-term care insurance benefits' + (f' for {name}' if via_rep else '') + ' – information required'),
        'body': ((d.get('escalation_summary') or f'{name}: all mandatory information is present. The application can be released to the Medical Service for assessment.') if is_ready
                 else d.get('escalation_summary') if is_esc else d.get('outreach_letter')),
        'footer': f'{insurer} · Long-term care insurance',
    })
    file_name = d.get('letter_file_name') or f"Letter_{b.get('application_id') or 'case'}.pdf"
    b['__binary__'] = {'letter': {'data': pdf, 'fileName': file_name, 'mimeType': 'application/pdf'}}
    return [b]
