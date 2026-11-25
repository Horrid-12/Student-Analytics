"""Canonical Division handling (BUG-126).

Roster exports and older accounts can store the same cohort as either
``"1"`` or ``"Division 1"`` (and occasionally ``"Div. 1"`` / ``"1.0"``).
Filtering, grouping and faculty-scope checks must treat those as one cohort
even though the stored strings differ.

This module deliberately depends on the standard library only so every layer
(auth, views, ui_helpers) can use it without import cycles.
"""

import re

#: ``Division 1`` / ``Div. 1`` / ``Division1`` -> the numeric or letter part.
_PREFIX = re.compile(r"^(?:div(?:ision)?\.?\s*)", re.IGNORECASE)
_TRAILING_ZERO_DECIMAL = re.compile(r"\.0+$")
_MISSING = frozenset({"", "nan", "none", "n/a", "na", "<na>", "unknown", "unassigned"})


def normalize_division(value) -> str:
    """Return the canonical division label (``"Division 1"`` -> ``"1"``).

    Missing/placeholder values become ``""``. Case is preserved so letter
    divisions (``"A"``) keep their display form; use :func:`division_key` for
    case-insensitive comparisons.
    """
    if value is None:
        return ""
    try:
        # NaN is not equal to itself. pd.NA raises on bool(), which the
        # ``str()`` fallback below turns into "<NA>" and filters out.
        if value != value:
            return ""
    except (TypeError, ValueError):
        pass
    text = str(value).strip()
    text = _PREFIX.sub("", text).strip()
    text = _TRAILING_ZERO_DECIMAL.sub("", text).strip()
    if text.casefold() in _MISSING:
        return ""
    return text


def division_key(value) -> str:
    """Comparison key for a division: canonical, case-insensitive."""
    return normalize_division(value).casefold()
