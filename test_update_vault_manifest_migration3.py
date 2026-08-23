#!/usr/bin/env python3
"""
test_update_vault_manifest_migration3.py — fixture tests for
update_vault_manifest_migration3.py (PX-20260822-05).

Uses synthetic stand-ins for both the retired Desktop folder and the
vault (PARCELYTICS_ARCHIVE_ROOT + PARCELYTICS_RETIRED_DESKTOP_ROOT both
pointed at tempdirs) -- never touches the real drive or the real
Desktop. See the script's own "SANDBOX DISCLOSURE" for what this suite
does and does not prove.

Run: python3 test_update_vault_manifest_migration3.py
"""
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

FAILURES = []


def check(name, condition, detail=""):
    status = "PASS" if condition else "FAIL"
    print(f"[{status}] {name}" + (f" — {detail}" if detail and not condition else ""))
    if not condition:
        FAILURES.append(name)
    return condition


def _reload(archive_root, retired_root, manifest_dir=None):
    os.environ["PARCELYTICS_ARCHIVE_ROOT"] = archive_root
    os.environ["PARCELYTICS_RETIRED_DESKTOP_ROOT"] = retired_root
    # CRITICAL: always point the manifest path at a tempdir, never the real
    # repo's vault_manifest.md -- this suite must not be able to touch the
    # real chain-of-custody record.
    manifest_dir = manifest_dir or tempfile.mkdtemp()
    os.environ["PARCELYTICS_VAULT_MANIFEST_PATH"] = os.path.join(manifest_dir, "vault_manifest.md")
    for mod in ("config", "migrate_archive", "archive_source_collateral",
                "update_vault_manifest_migration3"):
        sys.modules.pop(mod, None)
    import config  # noqa: F401
    import update_vault_manifest_migration3 as m3
    assert m3.MANIFEST_PATH == os.environ["PARCELYTICS_VAULT_MANIFEST_PATH"], (
        "MANIFEST_PATH env override did not take effect -- refusing to risk "
        "the real vault_manifest.md"
    )
    return m3


def _write(path, content=b"x"):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as f:
        f.write(content)


def _build_full_fixture(retired, vault, include_dupe=True, include_rates=True):
    """Builds a representative (not exhaustive) stand-in for the real 63-file
    run: one ordinary 2022 AJR collateral file, the two 2021 roll PDFs, a
    2025-09-04 file (with an optional ' 2' duplicate-folder sibling to
    exercise dedup), and the adopted_tax_rates xlsx. Vault already has the
    matching real destination files (simulating archive_source_
    collateral.py --execute having already run for real)."""
    # Ordinary 2022 AJR collateral (high-confidence, MIGRATION_MAP-derived date).
    _write(os.path.join(retired, "227EARS092822 (2) 2", "MIF_227.pdf"), b"2022 mif content")
    _write(os.path.join(vault, "travis", "certified_roll", "2022-09-28", "MIF_227.pdf"), b"2022 mif content")

    # The two 2021 roll PDFs -- real destination is 2022-01-25 (post-relocation).
    for fn in ("2021 CERTIFIED APPRAISAL ROLL as of Supp 0_Alpha.pdf",
               "2021 CERTIFIED APPRAISAL ROLL as of Supp 0_GEO.pdf"):
        _write(os.path.join(retired, fn), f"{fn} content".encode())
        _write(os.path.join(vault, "travis", "certified_roll", "2022-01-25", fn),
               f"{fn} content".encode())

    # 2025-09-04 vintage, plus an optional duplicate-suffixed sibling folder.
    _write(os.path.join(retired, "227EARS090425", "227EARS090425.csv"), b"2025 real content")
    _write(os.path.join(vault, "travis", "certified_roll", "2025-09-04", "227EARS090425.csv"), b"2025 real content")
    if include_dupe:
        _write(os.path.join(retired, "227EARS090425 2", "227EARS090425.csv"), b"2025 real content")

    if include_rates:
        _write(os.path.join(retired, "2025RatesHistory1990-2025.xlsx"), b"rates content")
        _write(os.path.join(vault, "travis", "rates", "2025RatesHistory1990-2025.xlsx"), b"rates content")


def test_2021_pdfs_corrected_to_2022_01_25_with_path_history():
    with tempfile.TemporaryDirectory() as tmp:
        retired = os.path.join(tmp, "retired")
        vault = os.path.join(tmp, "vault")
        _build_full_fixture(retired, vault)
        m3 = _reload(vault, retired)

        rows = m3.build_migration3_rows()
        pdf_rows = [r for r in rows if "GEO.pdf" in r["dest"]]
        ok = check("exactly one row found for the GEO PDF", len(pdf_rows) == 1,
                    f"found {len(pdf_rows)}")
        if pdf_rows:
            row = pdf_rows[0]
            ok = check("GEO PDF destination is 2022-01-25, not the stale 2021-09-25",
                        row["dest"].endswith(os.path.join("certified_roll", "2022-01-25",
                                                            "2021 CERTIFIED APPRAISAL ROLL as of Supp 0_GEO.pdf")),
                        row["dest"]) and ok
            ok = check("2021-09-25 does not appear anywhere in the corrected destination",
                        "2021-09-25" not in row["dest"], row["dest"]) and ok
            ok = check("note records the full path history (staged -> relocated)",
                        "2021-09-25" in row["note"] and "2022-01-25" in row["note"]
                        and "relocated" in row["note"].lower(), row["note"]) and ok
        return ok


def test_2025_vintage_marked_confirmed_not_inferred():
    with tempfile.TemporaryDirectory() as tmp:
        retired = os.path.join(tmp, "retired")
        vault = os.path.join(tmp, "vault")
        _build_full_fixture(retired, vault, include_dupe=False)
        m3 = _reload(vault, retired)

        rows = m3.build_migration3_rows()
        row_2025 = [r for r in rows if "227EARS090425.csv" in r["dest"]]
        ok = check("2025 vintage file found", len(row_2025) == 1, f"found {len(row_2025)}")
        if row_2025:
            note = row_2025[0]["note"]
            ok = check("note says CONFIRMED, not the stale INFERRED-DATE flag text",
                        "CONFIRMED" in note and "INFERRED-DATE" not in note, note) and ok
        return ok


def test_duplicate_suffixed_folder_dedups_to_one_row():
    with tempfile.TemporaryDirectory() as tmp:
        retired = os.path.join(tmp, "retired")
        vault = os.path.join(tmp, "vault")
        _build_full_fixture(retired, vault, include_dupe=True)
        m3 = _reload(vault, retired)

        rows = m3.build_migration3_rows()
        matches = [r for r in rows if r["dest"].endswith("227EARS090425.csv")]
        return check("the ' 2'-suffixed duplicate folder collapses to exactly one row, "
                     "not two", len(matches) == 1, f"found {len(matches)}")


def test_rates_file_uses_registered_rates_slug():
    """PX-20260822-05-rev1 ruling: archive_source_collateral.py now uses
    the registered 'rates' slug (config.py's TAX_RATES_XL), not the
    unregistered 'adopted_tax_rates' invention -- this script's row must
    reflect that fixed destination, with no vintage subfolder."""
    with tempfile.TemporaryDirectory() as tmp:
        retired = os.path.join(tmp, "retired")
        vault = os.path.join(tmp, "vault")
        _build_full_fixture(retired, vault)
        m3 = _reload(vault, retired)

        rows = m3.build_migration3_rows()
        rate_rows = [r for r in rows if "2025RatesHistory1990-2025.xlsx" in r["dest"]]
        ok = check("rates file found", len(rate_rows) == 1, f"found {len(rate_rows)}")
        if rate_rows:
            dest = rate_rows[0]["dest"]
            ok = check("destination uses the registered 'rates' slug",
                        os.path.join("travis", "rates", "2025RatesHistory1990-2025.xlsx") in dest,
                        dest) and ok
            ok = check("destination does NOT use the retired unregistered "
                        "adopted_tax_rates slug", "adopted_tax_rates" not in dest, dest) and ok
        return ok


def test_regression_guard_fires_if_2021_pdf_destination_reverts():
    """If archive_source_collateral.py's fix (a) is ever reverted, this
    script must refuse loudly, not silently re-apply a stale correction."""
    with tempfile.TemporaryDirectory() as tmp:
        retired = os.path.join(tmp, "retired")
        vault = os.path.join(tmp, "vault")
        _build_full_fixture(retired, vault)
        m3 = _reload(vault, retired)

        import archive_source_collateral as asc
        real_build_plan = asc.build_plan

        def reverted_build_plan():
            plan = real_build_plan()
            reverted = []
            for src, dest_dir, flag in plan:
                if "GEO.pdf" in src:
                    dest_dir = m3.config._travis_archive("certified_roll", "2021-09-25")
                    flag = "ASSUMED-VINTAGE: placed under 2021-09-25"
                reverted.append((src, dest_dir, flag))
            return reverted

        asc.build_plan = reverted_build_plan
        try:
            raised = False
            detail = ""
            try:
                m3.build_migration3_rows()
            except m3.ArchiveSourceCollateralRegressionError as e:
                raised = True
                detail = str(e)
        finally:
            asc.build_plan = real_build_plan
        return check("regression guard fires if the 2021 PDF destination "
                     "reverts to the stale 2021-09-25 path", raised, detail)


def test_regression_guard_fires_if_2025_flag_reverts_to_inferred():
    with tempfile.TemporaryDirectory() as tmp:
        retired = os.path.join(tmp, "retired")
        vault = os.path.join(tmp, "vault")
        _build_full_fixture(retired, vault, include_dupe=False)
        m3 = _reload(vault, retired)

        import archive_source_collateral as asc
        real_build_plan = asc.build_plan

        def reverted_build_plan():
            plan = real_build_plan()
            reverted = []
            for src, dest_dir, flag in plan:
                if "227EARS090425" in src:
                    flag = "INFERRED-DATE 2025-09-04 (from folder name MM/DD/YY, not independently confirmed)"
                reverted.append((src, dest_dir, flag))
            return reverted

        asc.build_plan = reverted_build_plan
        try:
            raised = False
            detail = ""
            try:
                m3.build_migration3_rows()
            except m3.ArchiveSourceCollateralRegressionError as e:
                raised = True
                detail = str(e)
        finally:
            asc.build_plan = real_build_plan
        return check("regression guard fires if the 2025 vintage flag "
                     "reverts to the stale INFERRED-DATE text", raised, detail)


def test_regression_guard_fires_if_rates_slug_reverts():
    with tempfile.TemporaryDirectory() as tmp:
        retired = os.path.join(tmp, "retired")
        vault = os.path.join(tmp, "vault")
        _build_full_fixture(retired, vault)
        m3 = _reload(vault, retired)

        import archive_source_collateral as asc
        real_build_plan = asc.build_plan

        def reverted_build_plan():
            plan = real_build_plan()
            reverted = []
            for src, dest_dir, flag in plan:
                if "2025RatesHistory1990-2025.xlsx" in src:
                    dest_dir = m3.config._travis_archive("adopted_tax_rates")
                reverted.append((src, dest_dir, flag))
            return reverted

        asc.build_plan = reverted_build_plan
        try:
            raised = False
            detail = ""
            try:
                m3.build_migration3_rows()
            except m3.ArchiveSourceCollateralRegressionError as e:
                raised = True
                detail = str(e)
        finally:
            asc.build_plan = real_build_plan
        return check("regression guard fires if the rates destination "
                     "reverts to the unregistered adopted_tax_rates slug",
                     raised, detail)


def test_dry_run_writes_nothing():
    with tempfile.TemporaryDirectory() as tmp:
        retired = os.path.join(tmp, "retired")
        vault = os.path.join(tmp, "vault")
        _build_full_fixture(retired, vault)
        m3 = _reload(vault, retired)

        manifest_dir = os.path.dirname(m3.MANIFEST_PATH)
        before = set(os.listdir(manifest_dir)) if os.path.isdir(manifest_dir) else set()

        rc = m3.main([])
        ok = check("dry-run exits 0", rc == 0, f"rc={rc}")
        after = set(os.listdir(manifest_dir)) if os.path.isdir(manifest_dir) else set()
        ok = check("dry-run creates no new files (no backup, no write)", before == after,
                    f"before={before - after}, after={after - before}") and ok
        return ok


def test_missing_vault_file_reported_not_silently_dropped():
    with tempfile.TemporaryDirectory() as tmp:
        retired = os.path.join(tmp, "retired")
        vault = os.path.join(tmp, "vault")
        _build_full_fixture(retired, vault, include_rates=False)
        # Rates file exists in the retired plan (source present) but was
        # never actually copied to the vault -- simulates a partial run.
        _write(os.path.join(retired, "2025RatesHistory1990-2025.xlsx"), b"rates content")
        m3 = _reload(vault, retired)

        rows = m3.build_migration3_rows()
        verified, missing, mismatches = m3.verify_and_hash(rows)
        ok = check("the un-copied rates file is reported as missing",
                    any("2025RatesHistory1990-2025.xlsx" in r["dest"] for r in missing))
        ok = check("everything else still verifies fine",
                    len(verified) == len(rows) - 1,
                    f"verified={len(verified)}, rows={len(rows)}") and ok
        return ok


def test_source_vault_hash_mismatch_fails_loudly():
    with tempfile.TemporaryDirectory() as tmp:
        retired = os.path.join(tmp, "retired")
        vault = os.path.join(tmp, "vault")
        _build_full_fixture(retired, vault)
        m3 = _reload(vault, retired)

        # Corrupt the vault copy of the ordinary 2022 collateral file so it
        # no longer matches its (still-reachable) retired source.
        _write(os.path.join(vault, "travis", "certified_roll", "2022-09-28", "MIF_227.pdf"),
               b"CORRUPTED, does not match source")

        rows = m3.build_migration3_rows()
        verified, missing, mismatches = m3.verify_and_hash(rows)
        ok = check("source-vs-vault hash mismatch is caught", len(mismatches) == 1,
                    f"mismatches={len(mismatches)}")

        rc = m3.main([])
        ok = check("a run against a real mismatch exits non-zero even in dry-run mode "
                    "(it must not silently proceed to write later)", rc == 1, f"rc={rc}") and ok
        return ok


def test_real_execute_updates_header_and_appends_section():
    with tempfile.TemporaryDirectory() as tmp:
        retired = os.path.join(tmp, "retired")
        vault = os.path.join(tmp, "vault")
        _build_full_fixture(retired, vault)
        m3 = _reload(vault, retired)

        os.makedirs(os.path.dirname(m3.MANIFEST_PATH), exist_ok=True)
        original_manifest = (
            "# Raw Vault manifest — Travis County\n"
            "Generated: 2026-08-05T00:24:42.083576\n\n"
            "> **Two migrations are recorded in this manifest.** Migration 1... "
            "Migration 2... Each new-path cell was independently re-hashed and "
            "verified against migration 1's recorded hash before being written; "
            "a mismatch anywhere would have blocked this update entirely.\n\n"
            "| Year | Source | Original path | Size | Vault date | SHA-256 (original) | Status | Current path |\n"
            "|---|---|---|---|---|---|---|---|\n"
            "| 2025 | certified | `/x/y.TXT` | 1 B | 2025-07-20 | `abc…` | COPIED_VERIFIED | `/z/y.TXT` (sha256 abc…) |\n"
        )
        with open(m3.MANIFEST_PATH, "w") as f:
            f.write(original_manifest)

        rc = m3.main(["--execute"])
        ok = check("--execute exits 0", rc == 0, f"rc={rc}")

        with open(m3.MANIFEST_PATH, "r") as f:
            new_content = f.read()

        ok = check("header now says 'Three migrations'", "Three migrations are recorded" in new_content) and ok
        ok = check("original migration-1 row is byte-for-byte untouched",
                    "| 2025 | certified | `/x/y.TXT` | 1 B | 2025-07-20 | `abc…` | COPIED_VERIFIED | `/z/y.TXT` (sha256 abc…) |" in new_content) and ok
        ok = check("Migration 3 section header present", "## Migration 3" in new_content) and ok
        ok = check("Migration 3 table contains the corrected 2022-01-25 PDF row",
                    "certified_roll/2022-01-25" in new_content and "GEO.pdf" in new_content) and ok
        ok = check("Migration 3 table contains the CONFIRMED (not inferred) 2025 note",
                    "CONFIRMED 2025-09-04" in new_content) and ok

        backups = [f for f in os.listdir(os.path.dirname(m3.MANIFEST_PATH))
                   if f.startswith("vault_manifest.md.backup_")]
        ok = check("exactly one timestamped backup was written", len(backups) == 1,
                    f"backups={backups}") and ok
        if backups:
            with open(os.path.join(os.path.dirname(m3.MANIFEST_PATH), backups[0])) as f:
                backup_content = f.read()
            ok = check("backup contains the ORIGINAL (pre-migration-3) content, unmodified",
                        backup_content == original_manifest) and ok
        return ok


def test_second_execute_refuses_cleanly_no_duplicate_section():
    with tempfile.TemporaryDirectory() as tmp:
        retired = os.path.join(tmp, "retired")
        vault = os.path.join(tmp, "vault")
        _build_full_fixture(retired, vault)
        m3 = _reload(vault, retired)

        os.makedirs(os.path.dirname(m3.MANIFEST_PATH), exist_ok=True)
        original_manifest = (
            "# Raw Vault manifest — Travis County\n"
            "Generated: 2026-08-05T00:24:42.083576\n\n"
            "> **Two migrations are recorded in this manifest.** ... "
            "a mismatch anywhere would have blocked this update entirely.\n\n"
            "| Year | Source | Original path | Size | Vault date | SHA-256 (original) | Status | Current path |\n"
            "|---|---|---|---|---|---|---|---|\n"
        )
        with open(m3.MANIFEST_PATH, "w") as f:
            f.write(original_manifest)

        rc1 = m3.main(["--execute"])
        ok = check("first --execute exits 0", rc1 == 0, f"rc={rc1}")

        with open(m3.MANIFEST_PATH) as f:
            after_first = f.read()

        rc2 = m3.main(["--execute"])
        ok = check("second --execute against an already-updated manifest exits "
                    "non-zero (refuses, does not silently duplicate)", rc2 == 1, f"rc={rc2}") and ok

        with open(m3.MANIFEST_PATH) as f:
            after_second = f.read()
        ok = check("manifest content is unchanged by the refused second run",
                    after_first == after_second) and ok
        ok = check("only one 'Migration 3' section header exists",
                    after_second.count("## Migration 3") == 1,
                    f"count={after_second.count('## Migration 3')}") and ok
        return ok


def test_prior_migration_overlap_excluded_noted_count_correct():
    """PX-20260822-05-rev2 MUST-FIX. The real overlap this round caught:
    archive_source_collateral.py's plan for the '2021EARS092521 2'
    duplicate-suffixed folder includes 20210925_000416_PTD.csv, planned
    for travis/certified_roll/2021-09-25/20210925_000416_PTD.csv -- but
    migration 1 already put a file at that exact destination (recorded in
    vault_manifest.md's Current-path column). That plan entry must be
    excluded from the Migration-3 rows (not double-recorded as new), with
    a printed PRIOR-MIGRATION SKIP note, and the resulting count must be
    correct (one fewer than the pre-fix count for the same fixture)."""
    with tempfile.TemporaryDirectory() as tmp:
        retired = os.path.join(tmp, "retired")
        vault = os.path.join(tmp, "vault")
        _build_full_fixture(retired, vault)

        # The real overlap case: a duplicate-suffixed '2021EARS092521 2'
        # folder plans to copy the same file migration 1 already migrated.
        overlap_dest = os.path.join(vault, "travis", "certified_roll", "2021-09-25",
                                     "20210925_000416_PTD.csv")
        _write(os.path.join(retired, "2021EARS092521 2", "20210925_000416_PTD.csv"),
               b"already-migrated PTD content")
        _write(overlap_dest, b"already-migrated PTD content")

        manifest_dir = tempfile.mkdtemp()
        manifest_path = os.path.join(manifest_dir, "vault_manifest.md")
        with open(manifest_path, "w") as f:
            f.write(
                "# Raw Vault manifest — Travis County\n"
                "Generated: 2026-08-05T00:24:42.083576\n\n"
                "> **Two migrations are recorded in this manifest.**\n\n"
                "| Year | Source | Original path | Size | Vault date | SHA-256 (original) | Status | Current path |\n"
                "|---|---|---|---|---|---|---|---|\n"
                "| 2021 | ajr | `/Users/diegog/Desktop/Claude Files/2021EARS092521/"
                "20210925_000416_PTD.csv` | 0.67 GB | 2021-09-25 | `6f377ce41be80328…` "
                "| COPIED_VERIFIED | `" + overlap_dest + "` (sha256 6f377ce41be80328…, "
                "verified 2026-08-22) |\n"
            )

        m3 = _reload(vault, retired, manifest_dir=manifest_dir)

        rows_without_overlap_check = list(m3.build_migration3_rows())
        # (rows already reflect the exclusion since build_migration3_rows()
        # applies it internally -- this is the real, post-fix row set.)
        matches = [r for r in rows_without_overlap_check if r["dest"] == overlap_dest]
        ok = check("the prior-migration-overlap destination is excluded from "
                    "Migration-3 rows", len(matches) == 0, f"matches={matches}")

        # Count check: the fixture normally yields 5 rows (see
        # _build_full_fixture's 5 non-overlapping files); adding the one
        # real overlap file must NOT increase that count to 6 -- it must
        # still be excluded, holding at 5.
        ok = check("row count is unaffected by the excluded overlap file "
                    "(stays at the non-overlap count, not +1)",
                    len(rows_without_overlap_check) == 5,
                    f"count={len(rows_without_overlap_check)}") and ok
        return ok


def test_existing_manifest_destinations_parses_current_path_column():
    """Unit-level check on the parser itself, independent of the full
    build_migration3_rows() flow above."""
    with tempfile.TemporaryDirectory() as tmp:
        retired = os.path.join(tmp, "retired")
        vault = os.path.join(tmp, "vault")
        _build_full_fixture(retired, vault, include_dupe=False, include_rates=False)

        manifest_dir = tempfile.mkdtemp()
        manifest_path = os.path.join(manifest_dir, "vault_manifest.md")
        with open(manifest_path, "w") as f:
            f.write(
                "| 2025 | certified | `/orig/path/LAND_DET.TXT` | 0.08 GB | 2025-07-20 "
                "| `4056b89697f62fd1…` | COPIED_VERIFIED | `/vault/travis/certified_roll/"
                "2025-07-20/LAND_DET.TXT` (sha256 4056b89697f62fd1…, verified 2026-08-22) |\n"
            )
        m3 = _reload(vault, retired, manifest_dir=manifest_dir)

        dests = m3.existing_manifest_destinations()
        ok = check("parser extracts exactly one Current-path destination",
                    len(dests) == 1, f"dests={dests}")
        ok = check("parsed destination is the Current-path value, not the Original path "
                    "or the hash-prefix backtick span",
                    "/vault/travis/certified_roll/2025-07-20/LAND_DET.TXT" in dests,
                    dests) and ok
        ok = check("the Original-path backtick span was NOT captured",
                    "/orig/path/LAND_DET.TXT" not in dests, dests) and ok
        return ok


def test_execute_refuses_if_migration2_tail_anchor_missing():
    """PX-20260822-05-rev1 MUST-FIX. Before this fix, a mangled/missing
    MIGRATION2_TAIL_ANCHOR would silently drop the Migration-3 header
    sentence (the 'Two'->'Three' word swap alone would still succeed, so
    the old updated_header == content guard couldn't catch it) while the
    section still got appended and the run reported success. Proves the
    new explicit check refuses loudly instead, before any write."""
    with tempfile.TemporaryDirectory() as tmp:
        retired = os.path.join(tmp, "retired")
        vault = os.path.join(tmp, "vault")
        _build_full_fixture(retired, vault)
        m3 = _reload(vault, retired)

        os.makedirs(os.path.dirname(m3.MANIFEST_PATH), exist_ok=True)
        # HEADER_ANCHOR is present and intact, but the tail sentence that
        # normally follows it has been mangled/reworded -- simulates the
        # manifest's header having drifted since this script was written.
        mangled_manifest = (
            "# Raw Vault manifest — Travis County\n"
            "Generated: 2026-08-05T00:24:42.083576\n\n"
            "> **Two migrations are recorded in this manifest.** Migration 1... "
            "Migration 2... Each new-path cell was independently re-hashed and "
            "verified against migration 1's recorded hash before being written; "
            "a mismatch anywhere would have blocked this OTHER, REWORDED update "
            "entirely (mangled on purpose for this test).\n\n"
            "| Year | Source | Original path | Size | Vault date | SHA-256 (original) | Status | Current path |\n"
            "|---|---|---|---|---|---|---|---|\n"
        )
        with open(m3.MANIFEST_PATH, "w") as f:
            f.write(mangled_manifest)

        rc = m3.main(["--execute"])
        ok = check("refuses (non-zero exit) when the tail anchor doesn't match verbatim",
                    rc == 1, f"rc={rc}")

        with open(m3.MANIFEST_PATH) as f:
            after = f.read()
        ok = check("manifest content is completely unchanged", after == mangled_manifest) and ok

        manifest_dir = os.path.dirname(m3.MANIFEST_PATH)
        backups = [f for f in os.listdir(manifest_dir) if f.startswith("vault_manifest.md.backup_")]
        ok = check("no backup file was written (refusal happens BEFORE the backup, per the brief)",
                    len(backups) == 0, f"backups={backups}") and ok
        ok = check("the Migration-3 header sentence was NOT silently dropped into a written file "
                    "(nothing was written at all)", "## Migration 3" not in after) and ok
        return ok


def main():
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
    print()
    if FAILURES:
        print(f"{len(FAILURES)} FAILURE(S): {FAILURES}")
        return 1
    n = sum(1 for name in globals() if name.startswith("test_"))
    print(f"ALL {n} MIGRATION-3 MANIFEST FIXTURE TESTS PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
