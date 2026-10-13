"""German working-day calendar (national public holidays only) on ISO 'YYYY-MM-DD' strings.
Ported from the n8n Code nodes; all arithmetic is in UTC calendar days, like the JS Date.UTC version."""
from __future__ import annotations

import datetime as _dt

_ONE = _dt.timedelta(days=1)
MON = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec']


def easter(y: int) -> _dt.date:
    """Gregorian Easter Sunday (anonymous Gregorian algorithm, identical to the JS version)."""
    A = y % 19; b = y // 100; cc = y % 100; d = b // 4; e = b % 4; f = (b + 8) // 25; g = (b - f + 1) // 3
    h = (19 * A + b - d - g + 15) % 30; i = cc // 4; k = cc % 4; l = (32 + 2 * e + 2 * i - h - k) % 7
    m = (A + 11 * h + 22 * l) // 451; mo = (h + l - 7 * m + 114) // 31; da = ((h + l - 7 * m + 114) % 31) + 1
    return _dt.date(y, mo, da)


def D(s: str) -> _dt.date:
    # new Date(s + 'T00:00:00Z'): only a plain calendar date is valid (anything else throws later in JS)
    return _dt.date.fromisoformat(s)


def iso(d: _dt.date) -> str:
    return d.isoformat()


_hol: dict[int, set[str]] = {}


def holidays(y: int) -> set[str]:
    # New Year, Good Friday (-2), Easter Monday (+1), Ascension (+39), Whit Monday (+50),
    # Labour Day, German Unity Day, Christmas Day, Boxing Day
    if y not in _hol:
        e = easter(y)
        _hol[y] = {f'{y:04d}-01-01', *(iso(e + n * _ONE) for n in (-2, 1, 39, 50)),
                   f'{y:04d}-05-01', f'{y:04d}-10-03', f'{y:04d}-12-25', f'{y:04d}-12-26'}
    return _hol[y]


def is_wd(d: _dt.date) -> bool:
    return d.weekday() < 5 and iso(d) not in holidays(d.year)


def _lt(a, b) -> bool:
    """JS `<`: NaN-ish loop bounds (e.g. NaN working days) never satisfy it."""
    try:
        return a < b
    except TypeError:
        return False


def wd_between(frm, to) -> int:
    """Working days after `frm` up to and including `to`."""
    if not frm or not to or not isinstance(frm, str) or not isinstance(to, str) or to <= frm:
        return 0
    n, d, e = 0, D(frm), D(to)
    while d < e:
        d += _ONE
        if is_wd(d):
            n += 1
    return n


def add_wd(frm: str, n) -> str:
    """The n-th working day after `frm`."""
    d, k = D(frm), 0
    while _lt(k, n):
        d += _ONE
        if is_wd(d):
            k += 1
    return iso(d)


def next_wd(s: str) -> str:
    """`s` itself if it is a working day, else the next one."""
    d = D(s)
    while not is_wd(d):
        d += _ONE
    return iso(d)


def fmt(s) -> str:
    """'2026-10-08' -> '8 Oct 2026'; empty -> '–'."""
    return f'{int(s[8:10])} {MON[int(s[5:7]) - 1]} {s[0:4]}' if s else '–'
