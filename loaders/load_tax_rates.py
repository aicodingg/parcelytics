"""
Load 2025RatesHistory1990-2025.xlsx into county_tax_rate.

Sheet layout: one row per taxing entity.
Columns: TDC (entity_code), JURISNAME, RATE25, RATE24, … RATE90
"""
import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import config
from loaders.db import get_conn, execute_schema, batch_upsert

import openpyxl


def load(conn):
    wb = openpyxl.load_workbook(config.TAX_RATES_XL, data_only=True)
    ws = wb.active

    # Find header row (first row with 'TDC' in col A)
    header_row = None
    for row in ws.iter_rows(values_only=True):
        if row[0] == "TDC":
            header_row = row
            break
    if not header_row:
        raise ValueError("Could not find TDC header row in tax rates XLSX")

    # Build year→col mapping from column names like RATE25, RATE24 …
    year_cols = {}
    for idx, cell in enumerate(header_row):
        if cell and str(cell).startswith("RATE") and len(str(cell)) == 6:
            try:
                yr_suffix = int(str(cell)[4:])
                year = 2000 + yr_suffix if yr_suffix <= 30 else 1900 + yr_suffix
                year_cols[idx] = year
            except ValueError:
                pass

    rows = []
    upsert_sql = """
        INSERT INTO county_tax_rate (entity_code, entity_name, tax_year, rate)
        VALUES (%s, %s, %s, %s)
        ON CONFLICT (entity_code, tax_year) DO UPDATE
            SET entity_name = EXCLUDED.entity_name,
                rate        = EXCLUDED.rate
    """

    for row in ws.iter_rows(values_only=True):
        entity_code = row[0]
        entity_name = row[1]
        if not entity_code or entity_code == "TDC":
            continue
        for col_idx, year in year_cols.items():
            val = row[col_idx]
            if val is None or val == "-" or val == "":
                continue
            try:
                rate = float(val)   # already decimal per $100 (e.g. 0.375845)
            except (ValueError, TypeError):
                continue
            rows.append((str(entity_code), str(entity_name), year, rate))

    n = batch_upsert(conn, upsert_sql, rows)
    print(f"  county_tax_rate: {n:,} rows loaded")
    return n


if __name__ == "__main__":
    conn = get_conn()
    execute_schema(conn)
    load(conn)
    conn.close()
