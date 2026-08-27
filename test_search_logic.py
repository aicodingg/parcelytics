"""
test_search_logic.py — fixture tests for search_logic.py's pure functions.

PX-20260827-04 Task 1: is_account_number_query() (renamed/extended from
is_numeric_account_query()) is the one confirmed gate that prevented
Dallas's 205,049 real alphanumeric ACCOUNT_NUMs from being found via any
of the four shared typeahead boxes (api_address_search(), app.py). These
fixtures cover both required cases from the brief: real account-number
shapes (all-digit AND alphanumeric) that must be ACCEPTED, and genuine
address text that must still be correctly REJECTED so it keeps falling
through to search_parcels_by_address() as address-text search.

No DB, no Flask -- pure function calls against real/representative input
strings (the alphanumeric example is the exact live-confirmed Dallas
account from the brief: 381077000C0250000).

Run: python3 test_search_logic.py
"""
import sys

import search_logic

ALL_OK = True


def fail(msg):
    global ALL_OK
    ALL_OK = False
    print(f"FAIL: {msg}")


def ok(msg):
    print(f"PASS: {msg}")


def check_accept(q, label):
    if search_logic.is_account_number_query(q):
        ok(f"accepted as account number: {label} ({q!r})")
    else:
        fail(f"should have been ACCEPTED as account number: {label} ({q!r})")


def check_reject(q, label):
    if not search_logic.is_account_number_query(q):
        ok(f"correctly rejected (not an account number): {label} ({q!r})")
    else:
        fail(f"should have been REJECTED as an account number: {label} ({q!r})")


def main():
    print("=" * 70)
    print("ACCEPT cases -- real account-number shapes")
    print("=" * 70)

    # All-digit Dallas account (original, already-working behavior --
    # must not regress).
    check_accept("00000600499000000", "all-digit Dallas account")

    # The exact live-confirmed alphanumeric Dallas account from the brief
    # -- the real bug case this fix targets. 17 chars, one embedded
    # uppercase letter ('C') as a structural block/unit designator.
    check_accept("381077000C0250000", "alphanumeric Dallas account (brief's live example)")

    # Travis 10-char all-digit geo_id -- existing behavior, must not
    # regress.
    check_accept("0100030105", "Travis 10-char all-digit geo_id")

    # 14-char Tax Office format (dash/space variants) -- existing
    # behavior via normalize_parcel_id() downstream, must still be
    # detected as an account-number query here.
    check_accept("01-0003-0105-0000", "dashed 14-char Tax Office account")
    check_accept("0100 0301 0500 00", "spaced 14-char Tax Office account")

    # Short prop_id-shaped integer -- existing numeric-fallback behavior.
    check_accept("123456", "short numeric prop_id")

    # Alphanumeric account with the letter at a different position, and
    # with multiple embedded letters, to confirm this isn't accidentally
    # position- or single-letter-specific.
    check_accept("38A077000C025B000", "alphanumeric account, multiple embedded letters")

    print()
    print("=" * 70)
    print("REJECT cases -- genuine address text, must NOT be treated as")
    print("an account number (would otherwise skip resolve_exact_parcel()")
    print("and go straight to a doomed geo_id-equality lookup)")
    print("=" * 70)

    check_reject("123 Main St", "street address with number")
    check_reject("Bridle Path", "pure-word street name")
    check_reject("AUSTIN", "pure-word city name")
    check_reject("100 Highway 290", "address with two embedded numbers, letter-majority")
    check_reject("5C Main", "unit-prefixed address, letter-majority")
    check_reject("123 Main St, Apt #4", "address with punctuation")
    check_reject("", "empty string")
    check_reject("   ", "whitespace only")
    check_reject("Spanish Oaks Dr", "multi-word street name")

    # A too-long alphanumeric string (past schema.sql's real geo_id
    # VARCHAR(20) width) can't possibly be a real geo_id -- must be
    # rejected even though it's otherwise digit-majority alphanumeric.
    check_reject("1" * 21, "digit string exceeding VARCHAR(20) width")

    print()
    print("=" * 70)
    if ALL_OK:
        print("ALL CHECKS PASSED")
        return 0
    else:
        print("SOME CHECKS FAILED")
        return 1


if __name__ == "__main__":
    sys.exit(main())
