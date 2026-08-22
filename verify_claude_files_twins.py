#!/usr/bin/env python3
"""verify_claude_files_twins.py — PX-20260822-02 (read-only)

For each source-data candidate left in ~/Desktop/Claude Files/, find any
same-named file under PARCELYTICS_DATA_ROOT or the vault's travis/ tree,
hash both sides, and report TWIN_VERIFIED / TWIN_DIFFERS / NO_TWIN.
Nothing is moved, written, or deleted. Gate for the final folder rename.
"""
import hashlib, os, sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import config

CLAUDE = os.path.expanduser("~/Desktop/Claude Files")
ROOTS = [config.PARCELYTICS_DATA_ROOT,
         os.path.join(config.PARCELYTICS_ARCHIVE_ROOT, "travis")]

CANDIDATES = [
    "2021 CERTIFIED APPRAISAL ROLL as of Supp 0_Alpha.pdf",
    "2021 CERTIFIED APPRAISAL ROLL as of Supp 0_GEO.pdf",
    "2025RatesHistory1990-2025.xlsx",
    "DiegoPIR2021 Revised.xlsx",
    "DiegoPIR2022.xlsx",
    "DiegoPIR2023.xlsx",
    "DiegoPIR2024.xlsx",
    "TaxCurOpenData (1).csv",
    "TaxDelqOpenData.csv",
]
CANDIDATE_DIRS = [
    "2021EARS092521 2", "227EARS082824 (2) 2", "227EARS082923 (2) 2",
    "227EARS090425", "227EARS090425 2", "227EARS092822 (2) 2",
]

def sha256_of(p):
    h = hashlib.sha256()
    with open(p, "rb") as f:
        for c in iter(lambda: f.read(4*1024*1024), b""):
            h.update(c)
    return h.hexdigest()

def find_by_name(name):
    hits = []
    for root in ROOTS:
        if not os.path.isdir(root):
            continue
        for r, _d, files in os.walk(root):
            if name in files:
                hits.append(os.path.join(r, name))
    return hits

def check_file(src, name):
    hits = find_by_name(name)
    if not hits:
        print(f"NO_TWIN        {name}  (sole copy — must archive before rename)")
        return
    src_h = sha256_of(src)
    for hit in hits:
        verdict = "TWIN_VERIFIED" if sha256_of(hit) == src_h else "TWIN_DIFFERS "
        print(f"{verdict}  {name}  <->  {hit}")

for name in CANDIDATES:
    p = os.path.join(CLAUDE, name)
    if os.path.isfile(p):
        check_file(p, name)
    else:
        print(f"MISSING_SRC    {name}  (not found in Claude Files)")

print("\n-- directories (per contained data file) --")
for d in CANDIDATE_DIRS:
    dp = os.path.join(CLAUDE, d)
    if not os.path.isdir(dp):
        print(f"MISSING_SRC    {d}/")
        continue
    for r, _dd, files in os.walk(dp):
        for fn in files:
            if fn.startswith("."):
                continue
            check_file(os.path.join(r, fn), fn)
