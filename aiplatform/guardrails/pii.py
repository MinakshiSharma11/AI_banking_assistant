"""Free-text PII masking before egress to a hosted LLM (LLM gateway rule).

Masks civil IDs (12 digits), Kuwaiti mobile numbers, IBANs, card numbers and e-mail
addresses that a user might type into a question. Local providers keep the text as is.
"""

from __future__ import annotations

import re

_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"\b[A-Z]{2}\d{2}[A-Z0-9]{11,30}\b"), "[IBAN]"),
    (re.compile(r"\b(?:\d[ -]?){15,18}\d\b"), "[CARD]"),
    (re.compile(r"(?<!\d)[23]\d{11}(?!\d)"), "[CIVIL_ID]"),
    (re.compile(r"(?:\+?965[ -]?)?(?<!\d)[569]\d{7}(?!\d)"), "[MOBILE]"),
    (re.compile(r"\b[\w.+-]+@[\w-]+\.[\w.-]+\b"), "[EMAIL]"),
]


def mask_pii(text: str) -> str:
    for pattern, token in _PATTERNS:
        text = pattern.sub(token, text)
    return text
