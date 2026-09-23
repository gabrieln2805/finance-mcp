"""
generate_data.py
Builds finance.db — a synthetic, general-ledger-style SQLite database used
by the synthetic-data tools in server.py (list_accounts, monthly_summary,
detect_spending_anomalies, ...).

What's in it:
  - accounts:     a 14-row chart of accounts, loosely modeled on SAP
                  (numeric GL codes, Asset/Liability/Revenue/Expense types).
  - transactions: 24 months of activity (2024-07-01 .. 2026-06-30) with
                  categories, vendors and cost centers.

It's not pure noise, so the analytics tools have something to find:
  - Seasonality:  conference season (Sep/Oct) drives Events, Sponsorships,
                  Airfare and Hotel; year-end (Dec) drives Bonus and travel.
  - Recurring:    five vendor/category pairs bill monthly at a near-fixed
                  price, the way real SaaS/rent bills do.
  - Anomalies:    16 transactions get their amount multiplied 5-12x. Their
                  IDs are written to anomalies_ground_truth.csv, which the
                  server never reads — it's the answer key for scoring
                  detect_spending_anomalies honestly.

Output is deterministic for a given --seed, so re-running it is safe.

Run:
    python generate_data.py
    python generate_data.py --db-path other.db --seed 7
"""

import argparse
import csv
import random
import sqlite3
from datetime import date, timedelta
from pathlib import Path

HERE = Path(__file__).parent
DB_PATH = HERE / "finance.db"
GROUND_TRUTH_PATH = HERE / "anomalies_ground_truth.csv"

START = date(2024, 7, 1)
MONTHS = 24
NUM_ANOMALIES = 16

ACCOUNTS = [
    ("A1000", "Cash and Equivalents", "Asset", "1000"),
    ("A1200", "Accounts Receivable", "Asset", "1200"),
    ("A1500", "Inventory", "Asset", "1500"),
    ("L2000", "Accounts Payable", "Liability", "2000"),
    ("L2100", "Accrued Liabilities", "Liability", "2100"),
    ("R4000", "Product Revenue", "Revenue", "4000"),
    ("R4100", "Service Revenue", "Revenue", "4100"),
    ("E5000", "Cost of Goods Sold", "Expense", "5000"),
    ("E6000", "Salaries and Wages", "Expense", "6000"),
    ("E6100", "Marketing and Advertising", "Expense", "6100"),
    ("E6200", "Software and Subscriptions", "Expense", "6200"),
    ("E6300", "Travel and Entertainment", "Expense", "6300"),
    ("E6400", "Office and Facilities", "Expense", "6400"),
    ("E6500", "Professional Services", "Expense", "6500"),
]

# Categories per revenue/expense account. Asset and liability rows carry no category.
CATEGORIES = {
    "R4000": ["Product Sales - SMB", "Product Sales - Enterprise", "Product Sales - Renewal"],
    "R4100": ["Implementation", "Support Contracts", "Consulting"],
    "E5000": ["Materials", "Freight", "Manufacturing Labor"],
    "E6000": ["Base Salary", "Benefits", "Bonus", "Payroll Tax"],
    "E6100": ["Digital Ads", "Events", "Sponsorships", "Content"],
    "E6200": ["SaaS Tools", "Cloud Infrastructure", "Data Licenses"],
    "E6300": ["Airfare", "Hotel", "Meals", "Ground Transport"],
    "E6400": ["Rent", "Utilities", "Supplies", "Maintenance"],
    "E6500": ["Legal", "Accounting", "Consulting Fees"],
}

VENDORS = [
    "Acme Corp", "Globex Ltd", "Initech", "Umbrella Supplies", "Stark Industries",
    "Wayne Enterprises", "Wonka Manufacturing", "Cyberdyne Systems", "Soylent Foods",
    "Hooli Cloud", "Pied Piper Systems", "Aperture Labs", "Massive Dynamic",
]

COST_CENTERS = ["CC-100-SALES", "CC-200-MKTG", "CC-300-ENG", "CC-400-OPS", "CC-500-GA"]

# Average number of random transactions per account per month.
MONTHLY_VOLUME = {
    "A1000": 3, "A1200": 3, "A1500": 3,
    "L2000": 3, "L2100": 2,
    "R4000": 6, "R4100": 5,
    "E5000": 4, "E6000": 5, "E6100": 4, "E6200": 4,
    "E6300": 5, "E6400": 3, "E6500": 4,
}

# Amount range (min, max) by account type.
AMOUNT_RANGE = {
    "Asset": (150, 15000),
    "Liability": (150, 15000),
    "Revenue": (1500, 25000),
    "Expense": (100, 8000),
}

# category -> {month: multiplier}. Multiplies both how often a category shows
# up that month and how large its amounts are.
SEASONALITY = {
    "Events": {9: 2.5, 10: 2.5},
    "Sponsorships": {9: 3.0, 10: 2.5},
    "Airfare": {9: 2.0, 10: 2.0, 12: 1.8},
    "Hotel": {9: 2.0, 10: 2.0, 12: 1.5},
    "Bonus": {12: 4.0},
}

# (account_id, category, vendor, monthly price, day of month)
RECURRING = [
    ("E6200", "SaaS Tools", "Initech", 1200.00, 3),
    ("E6200", "Cloud Infrastructure", "Hooli Cloud", 4800.00, 5),
    ("E6200", "Data Licenses", "Aperture Labs", 2500.00, 10),
    ("E6400", "Rent", "Wayne Enterprises", 7500.00, 1),
    ("E6400", "Utilities", "Globex Ltd", 900.00, 15),
]


def month_starts():
    y, m = START.year, START.month
    for _ in range(MONTHS):
        yield date(y, m, 1)
        m += 1
        if m == 13:
            y, m = y + 1, 1


def days_in_month(d: date) -> int:
    nxt = date(d.year + (d.month == 12), d.month % 12 + 1, 1)
    return (nxt - timedelta(days=1)).day


def random_txn(rng: random.Random, account_id: str, account_type: str, month: date) -> dict | None:
    category = rng.choice(CATEGORIES[account_id]) if account_id in CATEGORIES else None
    season = SEASONALITY.get(category, {}).get(month.month, 1.0)

    # Off-season categories get thinned out so peaks stand out in counts too.
    if season == 1.0 and category in SEASONALITY and rng.random() < 0.4:
        return None

    lo, hi = AMOUNT_RANGE[account_type]
    amount = rng.uniform(lo, hi) * (1 + (season - 1) * 0.5)

    vendor = None
    if account_type in ("Liability", "Expense"):
        vendor = rng.choice(VENDORS)

    if account_type == "Asset":
        description = "Asset activity"
    elif account_type == "Liability":
        description = f"Liability activity - {vendor}"
    elif account_type == "Revenue":
        description = f"{category} activity"
    else:
        description = f"{category} activity - {vendor}"

    return {
        "account_id": account_id,
        "txn_date": month.replace(day=rng.randint(1, days_in_month(month))).isoformat(),
        "amount": round(amount, 2),
        "category": category,
        "vendor": vendor,
        "cost_center": rng.choice(COST_CENTERS),
        "description": description,
        "recurring": False,
    }


def build_transactions(rng: random.Random) -> list[dict]:
    types = {a[0]: a[2] for a in ACCOUNTS}
    txns = []

    for month in month_starts():
        for account_id, volume in MONTHLY_VOLUME.items():
            # Seasonal categories get extra draws in their peak months.
            peak = max(
                (SEASONALITY.get(c, {}).get(month.month, 1.0) for c in CATEGORIES.get(account_id, [])),
                default=1.0,
            )
            n = rng.randint(max(0, volume - 1), volume + 1) + round((peak - 1) * 2)
            for _ in range(n):
                t = random_txn(rng, account_id, types[account_id], month)
                if t:
                    txns.append(t)

        for account_id, category, vendor, price, day in RECURRING:
            txns.append({
                "account_id": account_id,
                "txn_date": month.replace(day=day).isoformat(),
                "amount": round(price * rng.uniform(0.98, 1.02), 2),
                "category": category,
                "vendor": vendor,
                "cost_center": "CC-500-GA",
                "description": f"{category} monthly - {vendor}",
                "recurring": True,
            })

    txns.sort(key=lambda t: t["txn_date"])
    for i, t in enumerate(txns, start=1):
        t["transaction_id"] = i
    return txns


def plant_anomalies(rng: random.Random, txns: list[dict]) -> list[dict]:
    # Only categorized, non-recurring rows — detect_spending_anomalies groups by
    # category, so an anomaly on an uncategorized row could never be found.
    candidates = [t for t in txns if t["category"] and not t["recurring"]]
    truth = []
    for t in rng.sample(candidates, NUM_ANOMALIES):
        multiplier = round(rng.uniform(5, 12), 2)
        original = t["amount"]
        t["amount"] = round(original * multiplier, 2)
        truth.append({
            "transaction_id": t["transaction_id"],
            "account_id": t["account_id"],
            "category": t["category"],
            "txn_date": t["txn_date"],
            "original_amount": original,
            "amount": t["amount"],
            "multiplier": multiplier,
        })
    return sorted(truth, key=lambda r: r["transaction_id"])


def write_db(db_path: Path, txns: list[dict]) -> None:
    db_path.unlink(missing_ok=True)
    conn = sqlite3.connect(db_path)
    conn.executescript("""
        CREATE TABLE accounts (
            account_id   TEXT PRIMARY KEY,
            account_name TEXT NOT NULL,
            account_type TEXT NOT NULL,   -- Asset / Liability / Revenue / Expense
            gl_code      TEXT NOT NULL,
            currency     TEXT NOT NULL
        );
        CREATE TABLE transactions (
            transaction_id INTEGER PRIMARY KEY AUTOINCREMENT,
            account_id      TEXT NOT NULL REFERENCES accounts(account_id),
            txn_date        TEXT NOT NULL,   -- ISO YYYY-MM-DD
            amount          REAL NOT NULL,   -- positive number; sign handled by account_type
            currency        TEXT NOT NULL,
            category        TEXT,
            vendor          TEXT,
            cost_center     TEXT,
            description     TEXT
        );
        CREATE INDEX idx_txn_account ON transactions(account_id);
        CREATE INDEX idx_txn_date ON transactions(txn_date);
        CREATE INDEX idx_txn_category ON transactions(category);
    """)
    conn.executemany(
        "INSERT INTO accounts VALUES (?, ?, ?, ?, 'USD')",
        ACCOUNTS,
    )
    conn.executemany(
        """INSERT INTO transactions
           (transaction_id, account_id, txn_date, amount, currency,
            category, vendor, cost_center, description)
           VALUES (:transaction_id, :account_id, :txn_date, :amount, 'USD',
                   :category, :vendor, :cost_center, :description)""",
        txns,
    )
    conn.commit()
    conn.close()


def write_ground_truth(path: Path, truth: list[dict]) -> None:
    with open(path, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=list(truth[0].keys()))
        writer.writeheader()
        writer.writerows(truth)


def main():
    parser = argparse.ArgumentParser(description="Generate the synthetic finance.db.")
    parser.add_argument("--db-path", type=Path, default=DB_PATH)
    parser.add_argument("--ground-truth-path", type=Path, default=GROUND_TRUTH_PATH)
    parser.add_argument("--seed", type=int, default=42)
    args = parser.parse_args()

    rng = random.Random(args.seed)
    txns = build_transactions(rng)
    truth = plant_anomalies(rng, txns)

    write_db(args.db_path, txns)
    write_ground_truth(args.ground_truth_path, truth)

    print(
        f"Created {args.db_path.name} with {len(ACCOUNTS)} accounts and "
        f"{len(txns)} transactions ({len(truth)} planted anomalies -> "
        f"{args.ground_truth_path.name})."
    )


if __name__ == "__main__":
    main()
