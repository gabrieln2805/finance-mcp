"""
load_real_data.py
Pulls real municipal spending data from San Francisco's open data portal
(DataSF, dataset p5r5-fd7g: "Vendor Payments - Purchase Order Summary")
and loads it into real_finance.db.

Source: https://data.sfgov.org/City-Management-and-Ethics/Vendor-Payments-Purchase-Order-Summary-/p5r5-fd7g
No authentication required for this read volume. Data updates weekly.

Usage:
    python load_real_data.py                     # default: last 2 fiscal years, up to 8000 rows
    python load_real_data.py --fiscal-years 2024 2025 2026
    python load_real_data.py --max-rows 20000
    python load_real_data.py --offline-csv fixtures/sample_real_data.csv   # dev/offline mode, no network
"""

import argparse
import csv
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

import requests

DB_PATH = "real_finance.db"
API_ENDPOINT = "https://data.sfgov.org/resource/p5r5-fd7g.json"
PAGE_SIZE = 2000


def build_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        DROP TABLE IF EXISTS payments;
        DROP TABLE IF EXISTS departments;
        DROP TABLE IF EXISTS load_metadata;

        CREATE TABLE departments (
            department_code TEXT PRIMARY KEY,
            department_name TEXT NOT NULL
        );

        CREATE TABLE payments (
            payment_id       INTEGER PRIMARY KEY AUTOINCREMENT,
            fiscal_year      INTEGER NOT NULL,
            department_code  TEXT NOT NULL REFERENCES departments(department_code),
            character        TEXT,       -- broad grouping, e.g. "Non-Personnel Services"
            category         TEXT,       -- "object" in the source data, e.g. "Travel"
            vendor           TEXT,
            amount           REAL NOT NULL,  -- vouchers_paid; can be negative (credits/refunds)
            payment_date     TEXT,       -- ISO date, nullable (source data is often missing this)
            contract_number  TEXT,
            purchase_order   TEXT
        );

        CREATE TABLE load_metadata (
            key   TEXT PRIMARY KEY,
            value TEXT
        );

        CREATE INDEX idx_payments_department ON payments(department_code);
        CREATE INDEX idx_payments_date ON payments(payment_date);
        CREATE INDEX idx_payments_category ON payments(category);
        CREATE INDEX idx_payments_fiscal_year ON payments(fiscal_year);
        """
    )


def parse_date(raw: str) -> str | None:
    """Source dates look like '2018-07-05T00:00:00.000' or are empty. Return ISO date or None."""
    if not raw:
        return None
    return raw.split("T")[0]


def parse_amount(raw: str) -> float:
    try:
        return float(raw)
    except (TypeError, ValueError):
        return 0.0


def transform_row(row: dict) -> dict:
    """Normalize one raw API/CSV row into the shape we insert."""
    return {
        "fiscal_year": int(row["fiscal_year"]),
        "department_code": row["department_code"],
        "department_name": row["department"],
        "character": row.get("character"),
        "category": row.get("object"),
        "vendor": row.get("vendor"),
        "amount": parse_amount(row.get("vouchers_paid")),
        "payment_date": parse_date(row.get("purchase_order_date")),
        "contract_number": row.get("contract_number") or None,
        "purchase_order": row.get("purchase_order") or None,
    }


def fetch_from_api(fiscal_years: list[int], max_rows: int):
    """Paginate through the Socrata API for the given fiscal years."""
    year_filter = " OR ".join(f"fiscal_year='{y}'" for y in fiscal_years)
    rows = []
    offset = 0
    while len(rows) < max_rows:
        limit = min(PAGE_SIZE, max_rows - len(rows))
        resp = requests.get(
            API_ENDPOINT,
            params={
                "$where": year_filter,
                "$limit": limit,
                "$offset": offset,
                "$order": "purchase_order_date DESC",
            },
            timeout=60,
        )
        resp.raise_for_status()
        page = resp.json()
        if not page:
            break
        rows.extend(page)
        offset += len(page)
        print(f"  fetched {len(rows)} rows so far...")
        if len(page) < limit:
            break  # no more data available
    return rows


def fetch_from_csv(path: str):
    with open(path, newline="") as f:
        return list(csv.DictReader(f))


def load(conn: sqlite3.Connection, raw_rows: list[dict], source: str) -> int:
    departments = {}
    payment_rows = []

    for raw in raw_rows:
        try:
            r = transform_row(raw)
        except (KeyError, ValueError, TypeError):
            continue  # skip malformed rows rather than aborting the whole load
        departments[r["department_code"]] = r["department_name"]
        payment_rows.append(
            (
                r["fiscal_year"], r["department_code"], r["character"], r["category"],
                r["vendor"], r["amount"], r["payment_date"], r["contract_number"],
                r["purchase_order"],
            )
        )

    conn.executemany(
        "INSERT OR IGNORE INTO departments (department_code, department_name) VALUES (?, ?)",
        list(departments.items()),
    )
    conn.executemany(
        "INSERT INTO payments (fiscal_year, department_code, character, category, "
        "vendor, amount, payment_date, contract_number, purchase_order) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        payment_rows,
    )
    conn.executemany(
        "INSERT INTO load_metadata (key, value) VALUES (?, ?)",
        [
            ("source", source),
            ("loaded_at_utc", datetime.now(timezone.utc).isoformat()),
            ("row_count", str(len(payment_rows))),
        ],
    )
    conn.commit()
    return len(payment_rows)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--fiscal-years", nargs="+", type=int, default=[2025, 2026])
    parser.add_argument("--max-rows", type=int, default=8000)
    parser.add_argument("--db-path", default=DB_PATH)
    parser.add_argument(
        "--offline-csv",
        help="Load from a local CSV instead of the live API (for offline dev/testing).",
    )
    args = parser.parse_args()

    if args.offline_csv:
        print(f"Loading offline from {args.offline_csv} ...")
        raw_rows = fetch_from_csv(args.offline_csv)
        source = f"offline-csv:{args.offline_csv}"
    else:
        print(f"Fetching fiscal years {args.fiscal_years} from DataSF (max {args.max_rows} rows)...")
        raw_rows = fetch_from_api(args.fiscal_years, args.max_rows)
        source = f"data.sfgov.org/resource/p5r5-fd7g.json (fiscal_years={args.fiscal_years})"

    conn = sqlite3.connect(args.db_path)
    try:
        build_schema(conn)
        n = load(conn, raw_rows, source)
        n_depts = conn.execute("SELECT COUNT(*) FROM departments").fetchone()[0]
        print(f"Loaded {n} payments across {n_depts} departments into {args.db_path}")
    finally:
        conn.close()


if __name__ == "__main__":
    main()
