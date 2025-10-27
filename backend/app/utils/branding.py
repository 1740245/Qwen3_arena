from __future__ import annotations

import re

_VENDOR_PATTERN = re.compile(r"bitget", re.IGNORECASE)


def sanitize_vendor_terms(text: str | None) -> str | None:
    if not text:
        return text
    return _VENDOR_PATTERN.sub("Professor Oak", text)
