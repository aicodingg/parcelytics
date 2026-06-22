"""
Migration: add classi_cd column to parcel table.

classi_cd = TCAD internal improvement use code (2-3 digit numeric string).
Source: IMP_INFO.TXT, field [28:38] (10 chars, left-justified).
Loaded by backfill_classi_cd.py after this migration runs.

Run: python3 loaders/migrate_add_classi_cd.py
"""
import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import config
import psycopg2

conn = psycopg2.connect(
    host=config.DB_HOST, port=config.DB_PORT,
    dbname=config.DB_NAME, user=config.DB_USER, password=config.DB_PASS
)
cur = conn.cursor()

# Check if column already exists
cur.execute("""
    SELECT column_name FROM information_schema.columns
    WHERE table_name = 'parcel' AND column_name = 'classi_cd'
""")
if cur.fetchone():
    print("classi_cd column already exists — nothing to do.")
else:
    cur.execute("ALTER TABLE parcel ADD COLUMN classi_cd VARCHAR(6)")
    conn.commit()
    print("✓ Added classi_cd VARCHAR(6) to parcel table.")

conn.close()
