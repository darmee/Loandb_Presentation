"""Turning free-text account officer names into one name per person.

`account_officer` is `character varying(120)` with no foreign key, so the same
person appears under many spellings. One real officer in the live database is
recorded fifteen different ways:

    Chinyere · CHINYERE · chinyere · Chinyere Utomi · Utomi chinyere
    Utomi Chinyere · Chinyere utomi · chinyere Utomi · chinyere utomi
    CHINYERE UTOMI · UTOMI CHINYERE · utomi chinyere · Chinyere U.
    Chinyere Elizabeth Utomi · Chinyere/Victor

Any report that groups by the raw column shows her as fifteen officers, and a
query using `= 'Chinyere'` finds ~68% of her book and looks entirely plausible
while doing it.

Two stages, and the split matters:

`normalise()` handles what a machine can safely decide - case, punctuation,
extra whitespace, word order. It maps "UTOMI CHINYERE" and "Chinyere Utomi" to
the same key because reordering a person's own names cannot change who they
are.

ALIASES handles what a machine cannot. Deciding that bare "Chinyere" is the
same human as "Chinyere Utomi" requires knowing there is only one Chinyere -
that is a fact about the bank, not about the string. Guessing it would silently
merge two people's loan books, so it is stated explicitly here instead.

Run `manage.py list_officers` against the live database to see every raw value
with its volume, then extend ALIASES.
"""

import re
from collections import defaultdict

# Canonical display name -> the normalised keys that mean that person.
# Extend this from `manage.py list_officers`.
ALIASES = {
    "Chinyere Elizabeth Utomi": [
        "chinyere",
        "chinyere utomi",
        "chinyere u",
        "chinyere elizabeth utomi",
    ],
}

# Names recording work shared between officers. Kept apart rather than credited
# to either one, because splitting or assigning them is a business decision.
SHARED_MARKERS = ("/", " and ", " & ", ",")

_PUNCT = re.compile(r"[^\w\s]")
_SPACE = re.compile(r"\s+")


def normalise(raw):
    """A comparison key for an officer name.

    Lowercases, drops punctuation, collapses whitespace and sorts the words, so
    "UTOMI CHINYERE" and "Chinyere Utomi" produce the same key.
    """
    if not raw:
        return ""
    text = _PUNCT.sub(" ", raw.lower())
    words = _SPACE.sub(" ", text).strip().split()
    return " ".join(sorted(words))


def is_shared(raw):
    """True if the value names more than one officer, e.g. "Chinyere/Victor"."""
    if not raw:
        return False
    return any(marker in raw.lower() for marker in SHARED_MARKERS)


def _alias_index():
    """Normalised key -> canonical display name."""
    index = {}
    for canonical, keys in ALIASES.items():
        index[normalise(canonical)] = canonical
        for key in keys:
            index[normalise(key)] = canonical
    return index


_INDEX = _alias_index()


def canonical(raw):
    """The display name to group `raw` under.

    Falls back to a tidied version of the original, so an officer who is not in
    ALIASES still appears - under their own name, merged across case and word
    order, just not merged with any short form.
    """
    if not raw or not raw.strip():
        return "(unassigned)"
    if is_shared(raw):
        return f"(shared) {raw.strip()}"
    key = normalise(raw)
    if key in _INDEX:
        return _INDEX[key]
    return " ".join(word.capitalize() for word in raw.split())


def group_by_officer(rows, name_key="account_officer"):
    """Collapse rows onto canonical officers.

    `rows` is any iterable of dicts carrying the officer name plus whatever
    aggregates were computed per raw spelling. Returns
    {canonical: {"count": n, "value": Decimal, "variants": {...}}}.
    """
    grouped = defaultdict(lambda: {"count": 0, "value": 0, "variants": {}})
    for row in rows:
        raw = row.get(name_key)
        name = canonical(raw)
        bucket = grouped[name]
        bucket["count"] += row.get("count", 0)
        bucket["value"] += row.get("value", 0) or 0
        if raw:
            bucket["variants"][raw] = row.get("count", 0)
    return dict(grouped)
