"""Text from an application-form PDF (replaces n8n's "Extract from File").
Two-column forms are read column by column (left column, then right), like a person reads them."""
from __future__ import annotations
import io
import pdfplumber


def _two_columns(page) -> bool:
    words = page.extract_words()
    if len(words) < 20:
        return False
    mid = page.width / 2
    straddle = sum(1 for w in words if w['x0'] < mid - 4 and w['x1'] > mid + 4)
    right = sum(1 for w in words if w['x0'] >= mid)
    return straddle <= max(2, len(words) * 0.03) and right >= len(words) * 0.2


def pdf_to_text(data: bytes) -> str:
    pages = []
    with pdfplumber.open(io.BytesIO(data)) as pdf:
        for pg in pdf.pages:
            if _two_columns(pg):
                w, h = pg.width, pg.height
                left = pg.crop((0, 0, w / 2, h)).extract_text() or ''
                right = pg.crop((w / 2, 0, w, h)).extract_text() or ''
                pages.append(left + '\n' + right)
            else:
                pages.append(pg.extract_text() or '')
    return '\n\n'.join(pages).strip()
