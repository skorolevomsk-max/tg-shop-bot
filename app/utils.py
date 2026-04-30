"""Small helpers."""

from __future__ import annotations

import html as _html

# Currencies whose minor unit is the same as major (no decimals).
ZERO_DECIMAL_CURRENCIES = {"JPY", "KRW", "VND", "CLP", "ISK", "UGX", "RWF"}


def to_minor_units(amount_major: float, currency: str) -> int:
    """Convert a human price ('199.99' RUB) to minor units (kopecks/cents)."""
    if currency.upper() in ZERO_DECIMAL_CURRENCIES:
        return int(round(amount_major))
    return int(round(amount_major * 100))


def to_major_units(amount_minor: int, currency: str) -> float:
    if currency.upper() in ZERO_DECIMAL_CURRENCIES:
        return float(amount_minor)
    return amount_minor / 100.0


def format_price(amount_minor: int, currency: str) -> str:
    """Pretty price like '199.99 RUB' or '500 JPY'."""
    major = to_major_units(amount_minor, currency)
    if currency.upper() in ZERO_DECIMAL_CURRENCIES:
        body = f"{int(major)}"
    else:
        body = f"{major:.2f}".rstrip("0").rstrip(".")
        if "." not in body:
            body = f"{major:.2f}"
    return f"{body} {currency.upper()}"


def safe_html(text: str | None) -> str:
    if not text:
        return ""
    return _html.escape(text, quote=False)


def chunked(seq, n):
    buf = []
    for item in seq:
        buf.append(item)
        if len(buf) == n:
            yield buf
            buf = []
    if buf:
        yield buf
