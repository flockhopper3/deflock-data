#!/usr/bin/env python3
"""Junk entry filter for ALPR organization names.

Flags obvious non-organization entries that should be excluded from the map:
  - "DNU", "do not use", "test", "demo", "delete" keywords
  - Numeric-only names (e.g., "265", "1234")
  - Very short names (1-2 characters)
  - Common placeholder patterns
"""

import re


# Exact matches (case-insensitive) that are always junk
_JUNK_EXACT = {
    "dnu",
    "test",
    "demo",
    "delete",
    "deleted",
    "remove",
    "removed",
    "n/a",
    "na",
    "none",
    "null",
    "unknown",
    "tbd",
    "temp",
    "temporary",
    "duplicate",
    "dup",
    "old",
    "blank",
    "dupe",
    "inactive",
    # Known placeholder strings Flock portal operators use internally.
    "gsoc",
    "fxgsoc",
}

# Substrings (case-insensitive) that indicate junk
_JUNK_SUBSTRINGS = [
    "do not use",
    "don't use",
    "dont use",
    "do not activate",
    "test account",
    "test agency",
    "testing only",
    "please delete",
    "to be deleted",
    "deactivated",
    "decommissioned",
    "duplicate",
    "dupe ",  # trailing space: catches "DUPE DO NOT USE ..." as a word, not substrings in longer words
]


def is_junk(raw_name: str) -> bool:
    """Return True if the raw organization name is a junk/non-org entry.

    Args:
        raw_name: The raw organization name string.

    Returns:
        True if the entry should be excluded from geocoding/mapping.
    """
    if not raw_name or not raw_name.strip():
        return True

    cleaned = raw_name.strip()

    # Very short names (1-2 chars) are junk
    if len(cleaned) <= 2:
        return True

    # Numeric-only names
    if re.fullmatch(r"\d+", cleaned):
        return True

    # Exact match against known junk words
    lower = cleaned.lower()
    if lower in _JUNK_EXACT:
        return True

    # Substring match. We check the full name — if any "duplicate"/"do not use"
    # token appears, the entry is a placeholder. Real agencies whose Flock
    # portals happen to carry such a tag are still recoverable: Step 3 of
    # 01_parse_orgs un-flags is_junk when a matching portal's synthesized
    # name is clean.
    for substr in _JUNK_SUBSTRINGS:
        if substr in lower:
            return True

    return False
