"""Shared database connection helper."""
import psycopg2
import psycopg2.extras
import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import config


def get_conn():
    return psycopg2.connect(
        host=config.DB_HOST,
        port=config.DB_PORT,
        dbname=config.DB_NAME,
        user=config.DB_USER,
        password=config.DB_PASS,
    )


def execute_schema(conn):
    schema_path = os.path.join(os.path.dirname(__file__), "..", "schema.sql")
    with open(schema_path) as f:
        sql = f.read()
    with conn.cursor() as cur:
        cur.execute(sql)
    conn.commit()
    print("Schema applied.")


def batch_upsert(conn, sql, rows, batch=2000):
    """Execute an INSERT … ON CONFLICT upsert in batches."""
    total = 0
    with conn.cursor() as cur:
        for i in range(0, len(rows), batch):
            chunk = rows[i : i + batch]
            psycopg2.extras.execute_batch(cur, sql, chunk, page_size=batch)
            total += len(chunk)
    conn.commit()
    return total
