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


def is_account_number_query(q):
    """
    True if `q` looks like it's meant to be resolved as an exact account
    number / prop_id rather than an address-text search.

    PX-20260827-04: renamed from is_numeric_account_query() and extended
    beyond all-digit strings. That original all-digit-only check was
    written before Dallas onboarding and silently excluded Dallas's real
    ACCOUNT_NUM shape: per derive_prop_id_geo_id()'s own validation logic
    (loaders/dcad_format.py, PX-20260826-04 -- the authoritative source
    read for this fix rather than guessed at) a Dallas ACCOUNT_NUM is any
    non-blank string, and DCAD's own real export data confirms 205,049 of
    806,563 real ACCOUNT_NUMs are alphanumeric, with the letters serving as
    STRUCTURAL block/unit designators (e.g. "381077000C0250000") rather
    than being some kind of corruption -- so an all-digit-only gate here
    made every one of those accounts unfindable through any of the four
    shared typeahead boxes (api_address_search() below is the one real
    call site), even though direct-URL / full-page-search resolution
    (resolve_exact_parcel(), which has no digit gate at all) already
    handled them correctly. Confirmed live pre-fix:
    GET /dallas-tx/api/address_search?q=381077000C0250000 -> {"results": []}
    while the same account resolves fine at
    GET /dallas-tx/parcel/381077000C0250000 and via full-page search
    (GET /dallas-tx/?q=381077000C0250000).

    Rather than simply accepting "any non-blank string" (derive_prop_id_
    geo_id()'s own rule) -- which would swallow genuine address text too,
    defeating the whole point of this function -- this applies a tighter,
    still-real-shape-grounded rule so address queries keep falling through
    to search_parcels_by_address() as before:
      1. once whitespace/dashes are stripped, what remains must be
         non-empty and alphanumeric only (letters + digits, no other
         punctuation) -- real account numbers never contain punctuation
         beyond the dashes/spaces already stripped; address text routinely
         does ("123 Main St, Apt #4").
      2. it must contain at least one digit -- excludes pure-word address
         tokens ("AUSTIN", "MAIN") that would otherwise pass the
         alphanumeric check once their spaces are stripped.
      3. digits must be the majority of the stripped characters (>=50%) --
         real ACCOUNT_NUMs are "digits with occasionally embedded letters"
         per DCAD's own confirmed finding above (in the 17-char real-world
         example, 16 of 17 characters are digits); address text with an
         embedded number ("100 Highway", "5C Main") is letter-majority
         once its spaces are stripped and is correctly rejected here.
      4. length must fit schema.sql's real geo_id column width
         (VARCHAR(20)) -- anything longer literally cannot be a geo_id.
    All-digit strings (the original, unextended behavior) always satisfy
    every one of these unchanged, so no existing all-digit account number
    query is affected by this change.

    Kept here (rather than duplicated) so api_address_search() and the "/"
    route agree on what counts as an account number without re-deriving it
    independently.
    """
    if not q:
        return False
    stripped = q.strip().replace("-", "").replace(" ", "")
    if not stripped or not stripped.isalnum():
        return False
    if len(stripped) > 20:
        return False
    digit_count = sum(1 for c in stripped if c.isdigit())
    if digit_count == 0:
        return False
    return digit_count >= len(stripped) / 2.0


# Backward-compatible alias -- PX-20260827-04 renamed this function (see
# is_account_number_query()'s own docstring for why); kept in case any
# future call site still imports the old name.
is_numeric_account_query = is_account_number_query
