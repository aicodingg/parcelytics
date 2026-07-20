"""
search_logic.py — pure, DB-free address-matching helpers.

Cowork brief "Search overhaul — Phase 2 go-ahead (decisions on your Phase 1
findings)", July 2026, decision D2. This module holds only the parts of the
matching algorithm that don't need a database connection, so they can be
unit-tested directly (see verify_property_html_render.py's search_logic
checks) without a live Postgres instance -- this sandbox has none.

Background (Phase 1 finding): the `parcel` table has no separate city
column and `zip_code` is never populated by any loader (0% coverage,
confirmed by an exhaustive grep across loaders/). City/zip, when present at
all, are free text embedded inside the single `situs_address` TEXT column,
and only ~38% of a real, independently-inspected AJR year's rows carry a
city string at all (with real spelling drift in the source data itself --
e.g. "PFLUGERVILLE" also appears as "PLUGERVILLE", "PFLGUERVILLE"). This is
why the algorithm below treats city/zip as a soft ranking signal ("boost
tokens") rather than a hard filter anywhere -- a hard filter would silently
hide real parcels (e.g. 3411 Bridle Path's own situs_address has no city
token in it at all, despite Austin being the correct, real city).

The actual database query (the ILIKE substring match itself) lives in
app.py's search_parcels_by_address(), which calls address_match_attempts()
below to get the ordered sequence of patterns to try.
"""

_DROP_TOKENS = {"TX", "TEXAS"}


def normalize_query_tokens(q):
    """
    D2 step 1: uppercase, strip commas/periods, collapse whitespace, drop
    standalone "TX"/"TEXAS" tokens. Returns a list of tokens (never a
    string) so the rest of the algorithm can add/remove tokens from the end
    without re-parsing.
    """
    if not q:
        return []
    q = q.upper().replace(",", " ").replace(".", " ")
    return [t for t in q.split() if t not in _DROP_TOKENS]


def address_match_attempts(tokens):
    """
    D2 steps 2-3, as a generator of (pattern_tokens, boost_tokens) pairs to
    try IN ORDER against the database (caller stops at the first attempt
    that yields any real match):

      1. the full token list, no boost tokens (this alone preserves every
         search that already works today, since it's the same "whole
         string as one substring" behavior as before)
      2. on failure, drop the trailing token (it becomes a boost token) and
         retry -- repeated until either a match is found or only two
         tokens remain (D2: "street-number + one token" floor)

    Pure and side-effect-free: does not touch the database, does not know
    what "a match" means -- the caller (app.py) runs each pattern against
    situs_address and only advances to the next attempt on zero rows.
    """
    if not tokens:
        return
    remaining = list(tokens)
    boost = []
    while True:
        yield list(remaining), list(boost)
        if len(remaining) <= 2:
            return
        boost.append(remaining[-1])
        remaining = remaining[:-1]


def rank_candidates(rows, boost_tokens, pattern_tokens):
    """
    D2 step 4: sort matched rows by
      1. how many boost_tokens appear in situs_address (more first) --
         this is the mechanism that ranks "123 Cameron Rd, Pflugerville"
         results in Pflugerville above the ones in Austin/Manor/Round Rock,
         WITHOUT ever excluding the Austin ones (a wrong/typo'd boost token
         just means zero boost matches, not zero results -- the fallback
         the brief requires).
      2. situs_address starting with the matched pattern (prefix matches
         before ones that merely contain it elsewhere)
      3. alphabetical, as a stable tie-breaker

    `rows` — iterable of dicts with a "situs_address" key (case-insensitive
    matching is done here; callers don't need to pre-uppercase anything).
    Returns a new list; does not mutate the input.
    """
    prefix = " ".join(pattern_tokens)

    def sort_key(row):
        addr = (row.get("situs_address") or "").upper()
        boost_count = sum(1 for t in boost_tokens if t in addr)
        is_not_prefix = 0 if addr.startswith(prefix) else 1
        return (-boost_count, is_not_prefix, addr)

    return sorted(rows, key=sort_key)


def is_numeric_account_query(q):
    """
    True if `q` looks like it's meant to be resolved as an exact account
    number / prop_id rather than an address-text search -- i.e. once
    whitespace/dashes are stripped, what's left is all digits. Mirrors the
    character class app.py's normalize_parcel_id()/the "/" route's
    numeric-first branch already accept; kept here (rather than duplicated)
    so api_address_search() and the "/" route agree on what counts as
    "numeric" without re-deriving it independently.
    """
    if not q:
        return False
    stripped = q.strip().replace("-", "").replace(" ", "")
    return stripped.isdigit()
