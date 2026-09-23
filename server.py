"""
server.py
An MCP server that exposes read-only tools over a small GL-style
financial records database (finance.db, created by generate_data.py).

Run directly:
    python server.py
Or with the MCP Inspector for interactive testing:
    npx @modelcontextprotocol/inspector .venv/bin/python server.py
"""

import json
import os
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import numpy as np
import pandas as pd
from mcp.server.auth.provider import AccessToken, TokenVerifier
from mcp.server.auth.settings import AuthSettings
from mcp.server.fastmcp import FastMCP
from pydantic import AnyHttpUrl

DB_PATH = Path(__file__).parent / "finance.db"
REAL_DB_PATH = Path(__file__).parent / "real_finance.db"

# --- Transport / auth configuration -----------------------------------
# Default: stdio, no auth — this is what Claude Desktop uses, spawning this
# script as a local subprocess. There's no network exposure in that mode,
# so token auth wouldn't protect anything real.
#
# Setting MCP_TRANSPORT=http switches to Streamable HTTP (a real network
# listener), which DOES need auth. In that mode MCP_AUTH_TOKEN must also be
# set — a single shared secret checked on every request. This is a
# deliberately minimal stand-in for real auth: no external identity
# provider, no token issuance/rotation/expiry. It demonstrates the
# mechanism (reject unauthenticated requests) rather than being a
# production-ready auth system.
MCP_TRANSPORT = os.environ.get("MCP_TRANSPORT", "stdio")
MCP_AUTH_TOKEN = os.environ.get("MCP_AUTH_TOKEN")
HTTP_HOST = os.environ.get("MCP_HTTP_HOST", "127.0.0.1")
HTTP_PORT = int(os.environ.get("MCP_HTTP_PORT", "8000"))


class StaticTokenVerifier(TokenVerifier):
    """Checks a bearer token against a single expected value. See note above."""

    def __init__(self, valid_token: str):
        self._valid_token = valid_token

    async def verify_token(self, token: str) -> Optional[AccessToken]:
        if token == self._valid_token:
            return AccessToken(
                token=token,
                client_id="local-dev-client",
                scopes=["read", "write"],
                expires_at=None,
            )
        return None


if MCP_TRANSPORT == "http":
    if not MCP_AUTH_TOKEN:
        raise RuntimeError(
            "MCP_TRANSPORT=http requires MCP_AUTH_TOKEN to also be set, e.g.:\n"
            "  PowerShell: $env:MCP_AUTH_TOKEN = 'some-long-random-string'\n"
            "  bash:       export MCP_AUTH_TOKEN='some-long-random-string'"
        )
    server_url = AnyHttpUrl(f"http://{HTTP_HOST}:{HTTP_PORT}")
    mcp = FastMCP(
        "Financial Records",
        host=HTTP_HOST,
        port=HTTP_PORT,
        token_verifier=StaticTokenVerifier(MCP_AUTH_TOKEN),
        auth=AuthSettings(
            issuer_url=server_url,
            resource_server_url=server_url,
            required_scopes=["read"],
        ),
    )
else:
    mcp = FastMCP("Financial Records")

# Accounts that increase with a credit (Liability, Revenue) vs a debit
# (Asset, Expense) — used only to present a signed "balance" that behaves
# the way an accountant would expect.
CREDIT_NORMAL_TYPES = {"Liability", "Revenue", "Equity"}


@contextmanager
def get_connection():
    """Read-only connection to the SQLite database."""
    conn = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
    finally:
        conn.close()


@contextmanager
def get_real_connection():
    """Read-only connection to real_finance.db (San Francisco vendor payments)."""
    conn = sqlite3.connect(f"file:{REAL_DB_PATH}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
    finally:
        conn.close()


@contextmanager
def get_writable_real_connection():
    """
    Read-write connection to real_finance.db, used ONLY for the review_flags
    table. The payments/departments tables themselves are never written to
    through this server — they're re-derived from the source data by
    load_real_data.py, so this server treats them as read-only.
    """
    conn = sqlite3.connect(REAL_DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
    finally:
        conn.close()


def ensure_review_flags_schema() -> None:
    """Create the review_flags table if it doesn't exist yet. Safe to call every startup."""
    if not REAL_DB_PATH.exists():
        return  # real_finance.db hasn't been loaded yet; nothing to attach to
    with get_writable_real_connection() as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS review_flags (
                flag_id         INTEGER PRIMARY KEY AUTOINCREMENT,
                payment_id      INTEGER NOT NULL REFERENCES payments(payment_id),
                reason          TEXT NOT NULL,
                flagged_at      TEXT NOT NULL,
                status          TEXT NOT NULL DEFAULT 'open' CHECK (status IN ('open', 'resolved')),
                resolution_note TEXT,
                resolved_at     TEXT
            )
            """
        )
        conn.commit()


ensure_review_flags_schema()


def _row_to_dict(row: sqlite3.Row) -> dict:
    return dict(row)


@mcp.tool()
def list_accounts() -> list[dict]:
    """List every account in the chart of accounts, with its type and GL code."""
    with get_connection() as conn:
        rows = conn.execute(
            "SELECT account_id, account_name, account_type, gl_code, currency "
            "FROM accounts ORDER BY gl_code"
        ).fetchall()
        return [_row_to_dict(r) for r in rows]


@mcp.tool()
def get_account_balance(account_id: str) -> dict:
    """
    Get the current balance for a single account.

    Args:
        account_id: The account ID, e.g. "E6100" for Marketing and Advertising.
                    Use list_accounts to see valid IDs.
    """
    with get_connection() as conn:
        account = conn.execute(
            "SELECT * FROM accounts WHERE account_id = ?", (account_id,)
        ).fetchone()
        if account is None:
            return {"error": f"No account found with account_id '{account_id}'"}

        total = conn.execute(
            "SELECT COALESCE(SUM(amount), 0) FROM transactions WHERE account_id = ?",
            (account_id,),
        ).fetchone()[0]

        # Purely illustrative sign convention — flip if your own chart of
        # accounts uses the opposite normal balance.
        signed_balance = total if account["account_type"] in CREDIT_NORMAL_TYPES else -total

        return {
            "account_id": account_id,
            "account_name": account["account_name"],
            "account_type": account["account_type"],
            "currency": account["currency"],
            "gross_activity": round(total, 2),
            "signed_balance": round(signed_balance, 2),
        }


@mcp.tool()
def get_transactions(
    account_id: Optional[str] = None,
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
    category: Optional[str] = None,
    limit: int = 50,
) -> list[dict]:
    """
    Retrieve transactions, optionally filtered by account, date range, and category.

    Args:
        account_id: Restrict to one account ID (e.g. "R4000"). Omit for all accounts.
        start_date: ISO date "YYYY-MM-DD", inclusive lower bound.
        end_date: ISO date "YYYY-MM-DD", inclusive upper bound.
        category: Exact category match (e.g. "SaaS Tools").
        limit: Max rows to return (default 50, capped at 500).
    """
    limit = max(1, min(limit, 500))

    clauses = []
    params: list = []

    if account_id:
        clauses.append("t.account_id = ?")
        params.append(account_id)
    if start_date:
        clauses.append("t.txn_date >= ?")
        params.append(start_date)
    if end_date:
        clauses.append("t.txn_date <= ?")
        params.append(end_date)
    if category:
        clauses.append("t.category = ?")
        params.append(category)

    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    query = f"""
        SELECT t.transaction_id, t.txn_date, t.account_id, a.account_name,
               t.amount, t.currency, t.category, t.vendor, t.cost_center, t.description
        FROM transactions t
        JOIN accounts a ON a.account_id = t.account_id
        {where}
        ORDER BY t.txn_date DESC
        LIMIT ?
    """
    params.append(limit)

    with get_connection() as conn:
        rows = conn.execute(query, params).fetchall()
        return [_row_to_dict(r) for r in rows]


@mcp.tool()
def search_transactions(keyword: str, limit: int = 20) -> list[dict]:
    """
    Free-text search across transaction description and vendor fields.

    Args:
        keyword: Text to search for (case-insensitive, partial match).
        limit: Max rows to return (default 20, capped at 200).
    """
    limit = max(1, min(limit, 200))
    like_pattern = f"%{keyword}%"

    with get_connection() as conn:
        rows = conn.execute(
            """
            SELECT t.transaction_id, t.txn_date, t.account_id, a.account_name,
                   t.amount, t.currency, t.category, t.vendor, t.cost_center, t.description
            FROM transactions t
            JOIN accounts a ON a.account_id = t.account_id
            WHERE t.description LIKE ? COLLATE NOCASE
               OR t.vendor LIKE ? COLLATE NOCASE
            ORDER BY t.txn_date DESC
            LIMIT ?
            """,
            (like_pattern, like_pattern, limit),
        ).fetchall()
        return [_row_to_dict(r) for r in rows]


@mcp.tool()
def spending_by_category(
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
    account_type: Optional[str] = "Expense",
) -> list[dict]:
    """
    Aggregate transaction totals by category, largest first.

    Args:
        start_date: ISO date "YYYY-MM-DD", inclusive lower bound.
        end_date: ISO date "YYYY-MM-DD", inclusive upper bound.
        account_type: Filter to one account type (default "Expense").
                      Pass null/None to include all types.
    """
    clauses = []
    params: list = []

    if start_date:
        clauses.append("t.txn_date >= ?")
        params.append(start_date)
    if end_date:
        clauses.append("t.txn_date <= ?")
        params.append(end_date)
    if account_type:
        clauses.append("a.account_type = ?")
        params.append(account_type)

    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    query = f"""
        SELECT t.category, COUNT(*) AS transaction_count, ROUND(SUM(t.amount), 2) AS total_amount
        FROM transactions t
        JOIN accounts a ON a.account_id = t.account_id
        {where}
        GROUP BY t.category
        ORDER BY total_amount DESC
    """

    with get_connection() as conn:
        rows = conn.execute(query, params).fetchall()
        return [_row_to_dict(r) for r in rows]


@mcp.tool()
def monthly_summary(year: int, month: int) -> dict:
    """
    Revenue, expense, and net total for a given calendar month.

    Args:
        year: Four-digit year, e.g. 2026.
        month: Month number 1-12.
    """
    if not (1 <= month <= 12):
        return {"error": "month must be between 1 and 12"}

    start = f"{year:04d}-{month:02d}-01"
    end_month = month + 1 if month < 12 else 1
    end_year = year if month < 12 else year + 1
    end = f"{end_year:04d}-{end_month:02d}-01"

    with get_connection() as conn:
        revenue = conn.execute(
            """
            SELECT COALESCE(SUM(t.amount), 0) FROM transactions t
            JOIN accounts a ON a.account_id = t.account_id
            WHERE a.account_type = 'Revenue' AND t.txn_date >= ? AND t.txn_date < ?
            """,
            (start, end),
        ).fetchone()[0]

        expense = conn.execute(
            """
            SELECT COALESCE(SUM(t.amount), 0) FROM transactions t
            JOIN accounts a ON a.account_id = t.account_id
            WHERE a.account_type = 'Expense' AND t.txn_date >= ? AND t.txn_date < ?
            """,
            (start, end),
        ).fetchone()[0]

        top_categories = conn.execute(
            """
            SELECT t.category, ROUND(SUM(t.amount), 2) AS total_amount
            FROM transactions t
            JOIN accounts a ON a.account_id = t.account_id
            WHERE a.account_type = 'Expense' AND t.txn_date >= ? AND t.txn_date < ?
            GROUP BY t.category
            ORDER BY total_amount DESC
            LIMIT 5
            """,
            (start, end),
        ).fetchall()

    return {
        "period": f"{year:04d}-{month:02d}",
        "total_revenue": round(revenue, 2),
        "total_expense": round(expense, 2),
        "net": round(revenue - expense, 2),
        "top_expense_categories": [_row_to_dict(r) for r in top_categories],
    }


@mcp.tool()
def detect_spending_anomalies(
    account_type: Optional[str] = "Expense",
    category: Optional[str] = None,
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
    z_threshold: float = 2.5,
    limit: int = 20,
) -> dict:
    """
    Flag transactions that are statistical outliers within their own category,
    using a z-score (how many standard deviations from that category's mean).
    Categories with fewer than 5 transactions are skipped — not enough
    history to say what "normal" looks like for them.

    Args:
        account_type: Restrict to one account type (default "Expense").
                      Pass null/None to include all types.
        category: Restrict to a single category. Omit to scan all categories.
        start_date: ISO date "YYYY-MM-DD", inclusive lower bound.
        end_date: ISO date "YYYY-MM-DD", inclusive upper bound.
        z_threshold: Flag transactions with |z-score| above this (default 2.5).
        limit: Max flagged transactions to return (default 20, capped at 100).
    """
    limit = max(1, min(limit, 100))

    clauses = []
    params: list = []
    if account_type:
        clauses.append("a.account_type = ?")
        params.append(account_type)
    if category:
        clauses.append("t.category = ?")
        params.append(category)
    if start_date:
        clauses.append("t.txn_date >= ?")
        params.append(start_date)
    if end_date:
        clauses.append("t.txn_date <= ?")
        params.append(end_date)
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""

    query = f"""
        SELECT t.transaction_id, t.txn_date, t.account_id, a.account_name,
               t.amount, t.category, t.vendor, t.description
        FROM transactions t
        JOIN accounts a ON a.account_id = t.account_id
        {where}
    """

    with get_connection() as conn:
        df = pd.read_sql_query(query, conn, params=params)

    if df.empty:
        return {"flagged": [], "note": "No transactions matched the given filters."}

    # Compute z-score within each category (population of that category only).
    stats = df.groupby("category")["amount"].agg(["mean", "std", "count"])
    df = df.join(stats, on="category")
    df = df[df["count"] >= 5].copy()  # not enough history otherwise
    df["z_score"] = (df["amount"] - df["mean"]) / df["std"].replace(0, np.nan)

    flagged = df[df["z_score"].abs() >= z_threshold].copy()
    flagged = flagged.sort_values("z_score", key=abs, ascending=False).head(limit)

    results = [
        {
            "transaction_id": int(r.transaction_id),
            "txn_date": r.txn_date,
            "account_name": r.account_name,
            "category": r.category,
            "amount": round(r.amount, 2),
            "category_mean": round(r.mean, 2),
            "z_score": round(r.z_score, 2),
            "vendor": r.vendor,
            "description": r.description,
        }
        for r in flagged.itertuples()
    ]

    return {
        "flagged": results,
        "categories_scanned": int(stats.shape[0]),
        "categories_skipped_insufficient_data": int((stats["count"] < 5).sum()),
        "note": (
            "Z-score flags statistical outliers relative to a category's own "
            "history, not necessarily errors — always sanity-check flagged items."
        ),
    }


@mcp.tool()
def forecast_category_spend(category: str, periods_ahead: int = 1) -> dict:
    """
    Forecast future monthly spend for a category using a simple linear trend
    fit over its historical monthly totals. This is intentionally basic
    (ordinary least squares on month index vs. total) — good for a quick
    directional read, not a substitute for a real forecasting model.

    Args:
        category: The category to forecast, e.g. "Cloud Infrastructure".
        periods_ahead: How many future months to forecast (default 1, max 6).
    """
    periods_ahead = max(1, min(periods_ahead, 6))

    query = """
        SELECT strftime('%Y-%m', txn_date) AS month, SUM(amount) AS total
        FROM transactions
        WHERE category = ?
        GROUP BY month
        ORDER BY month
    """
    with get_connection() as conn:
        df = pd.read_sql_query(query, conn, params=[category])

    if len(df) < 3:
        return {
            "category": category,
            "error": f"Only {len(df)} month(s) of history — need at least 3 to fit a trend.",
        }

    x = np.arange(len(df))
    y = df["total"].to_numpy()
    slope, intercept = np.polyfit(x, y, deg=1)

    # R^2 as a rough goodness-of-fit signal so the caller knows how much to trust this.
    y_pred = slope * x + intercept
    ss_res = np.sum((y - y_pred) ** 2)
    ss_tot = np.sum((y - y.mean()) ** 2)
    r_squared = 1 - ss_res / ss_tot if ss_tot > 0 else 0.0

    last_month = pd.Period(df["month"].iloc[-1], freq="M")
    forecasts = []
    for i in range(1, periods_ahead + 1):
        future_x = len(df) - 1 + i
        forecast_value = slope * future_x + intercept
        forecast_month = (last_month + i).strftime("%Y-%m")
        forecasts.append({"month": forecast_month, "forecast": round(max(forecast_value, 0), 2)})

    return {
        "category": category,
        "history": [
            {"month": m, "total": round(t, 2)} for m, t in zip(df["month"], df["total"])
        ],
        "trend_slope_per_month": round(slope, 2),
        "r_squared": round(r_squared, 3),
        "forecast": forecasts,
        "note": (
            "Linear trend only — doesn't account for seasonality. "
            "Low r_squared means the trend line is a poor fit; treat the forecast with caution."
        ),
    }


@mcp.tool()
def list_departments() -> list[dict]:
    """List every San Francisco city department present in the real payments data."""
    with get_real_connection() as conn:
        rows = conn.execute(
            "SELECT department_code, department_name FROM departments ORDER BY department_name"
        ).fetchall()
        return [_row_to_dict(r) for r in rows]


@mcp.tool()
def get_department_spend(department_code: str, fiscal_year: Optional[int] = None) -> dict:
    """
    Total real spend for one SF department, optionally restricted to a fiscal year.

    Args:
        department_code: Department code, e.g. "DPH" for Public Health. Use list_departments.
        fiscal_year: Restrict to one fiscal year, e.g. 2026. Omit for all years loaded.
    """
    clauses = ["department_code = ?"]
    params: list = [department_code]
    if fiscal_year:
        clauses.append("fiscal_year = ?")
        params.append(fiscal_year)
    where = " AND ".join(clauses)

    with get_real_connection() as conn:
        dept = conn.execute(
            "SELECT department_name FROM departments WHERE department_code = ?",
            (department_code,),
        ).fetchone()
        if dept is None:
            return {"error": f"No department found with code '{department_code}'"}

        result = conn.execute(
            f"SELECT COUNT(*) AS n, COALESCE(SUM(amount), 0) AS total FROM payments WHERE {where}",
            params,
        ).fetchone()

    return {
        "department_code": department_code,
        "department_name": dept["department_name"],
        "fiscal_year": fiscal_year,
        "payment_count": result["n"],
        "total_amount": round(result["total"], 2),
    }


@mcp.tool()
def get_payments(
    department_code: Optional[str] = None,
    category: Optional[str] = None,
    fiscal_year: Optional[int] = None,
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
    limit: int = 50,
) -> list[dict]:
    """
    Retrieve real SF vendor payments, optionally filtered.

    Args:
        department_code: Restrict to one department, e.g. "DPH".
        category: Restrict to one category ("object" in source data), e.g. "Travel".
        fiscal_year: Restrict to one fiscal year, e.g. 2026.
        start_date: ISO date "YYYY-MM-DD", inclusive lower bound on payment_date.
                    Note: many source rows have no payment_date and will be excluded
                    if this filter is set.
        end_date: ISO date "YYYY-MM-DD", inclusive upper bound on payment_date.
        limit: Max rows to return (default 50, capped at 500).
    """
    limit = max(1, min(limit, 500))
    clauses = []
    params: list = []

    if department_code:
        clauses.append("p.department_code = ?")
        params.append(department_code)
    if category:
        clauses.append("p.category = ?")
        params.append(category)
    if fiscal_year:
        clauses.append("p.fiscal_year = ?")
        params.append(fiscal_year)
    if start_date:
        clauses.append("p.payment_date >= ?")
        params.append(start_date)
    if end_date:
        clauses.append("p.payment_date <= ?")
        params.append(end_date)

    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    query = f"""
        SELECT p.payment_id, p.fiscal_year, p.department_code, d.department_name,
               p.character, p.category, p.vendor, p.amount, p.payment_date,
               p.contract_number, p.purchase_order
        FROM payments p
        JOIN departments d ON d.department_code = p.department_code
        {where}
        ORDER BY p.payment_date DESC NULLS LAST
        LIMIT ?
    """
    params.append(limit)

    with get_real_connection() as conn:
        rows = conn.execute(query, params).fetchall()
        return [_row_to_dict(r) for r in rows]


@mcp.tool()
def search_payments(keyword: str, limit: int = 20) -> list[dict]:
    """
    Free-text search over vendor names in the real SF payments data.

    Args:
        keyword: Text to search for (case-insensitive, partial match) in vendor name.
        limit: Max rows to return (default 20, capped at 200).
    """
    limit = max(1, min(limit, 200))
    like_pattern = f"%{keyword}%"

    with get_real_connection() as conn:
        rows = conn.execute(
            """
            SELECT p.payment_id, p.fiscal_year, p.department_code, d.department_name,
                   p.category, p.vendor, p.amount, p.payment_date
            FROM payments p
            JOIN departments d ON d.department_code = p.department_code
            WHERE p.vendor LIKE ? COLLATE NOCASE
            ORDER BY p.payment_date DESC NULLS LAST
            LIMIT ?
            """,
            (like_pattern, limit),
        ).fetchall()
        return [_row_to_dict(r) for r in rows]


@mcp.tool()
def real_spending_by_category(
    fiscal_year: Optional[int] = None, department_code: Optional[str] = None
) -> list[dict]:
    """
    Aggregate real payment totals by category ("object" in source data), largest first.

    Args:
        fiscal_year: Restrict to one fiscal year, e.g. 2026. Omit for all years loaded.
        department_code: Restrict to one department, e.g. "DPH". Omit for all departments.
    """
    clauses = []
    params: list = []
    if fiscal_year:
        clauses.append("fiscal_year = ?")
        params.append(fiscal_year)
    if department_code:
        clauses.append("department_code = ?")
        params.append(department_code)
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""

    query = f"""
        SELECT category, COUNT(*) AS payment_count, ROUND(SUM(amount), 2) AS total_amount
        FROM payments
        {where}
        GROUP BY category
        ORDER BY total_amount DESC
    """
    with get_real_connection() as conn:
        rows = conn.execute(query, params).fetchall()
        return [_row_to_dict(r) for r in rows]


@mcp.tool()
def fiscal_year_summary(fiscal_year: int) -> dict:
    """
    Total spend, top departments, and top categories for one fiscal year of real SF data.

    Args:
        fiscal_year: Four-digit fiscal year, e.g. 2026.
    """
    with get_real_connection() as conn:
        total = conn.execute(
            "SELECT COALESCE(SUM(amount), 0) FROM payments WHERE fiscal_year = ?",
            (fiscal_year,),
        ).fetchone()[0]

        top_departments = conn.execute(
            """
            SELECT d.department_name, ROUND(SUM(p.amount), 2) AS total_amount
            FROM payments p JOIN departments d ON d.department_code = p.department_code
            WHERE p.fiscal_year = ?
            GROUP BY d.department_name
            ORDER BY total_amount DESC
            LIMIT 5
            """,
            (fiscal_year,),
        ).fetchall()

        top_categories = conn.execute(
            """
            SELECT category, ROUND(SUM(amount), 2) AS total_amount
            FROM payments
            WHERE fiscal_year = ?
            GROUP BY category
            ORDER BY total_amount DESC
            LIMIT 5
            """,
            (fiscal_year,),
        ).fetchall()

    return {
        "fiscal_year": fiscal_year,
        "total_amount": round(total, 2),
        "top_departments": [_row_to_dict(r) for r in top_departments],
        "top_categories": [_row_to_dict(r) for r in top_categories],
        "note": (
            "Figures reflect only the fiscal years and row cap loaded by load_real_data.py, "
            "not the full historical dataset."
        ),
    }


@mcp.tool()
def flag_payment_for_review(payment_id: int, reason: str) -> dict:
    """
    Flag a real payment for human review. This writes to a local annotation
    table only — it does NOT modify San Francisco's actual records, which
    this server has no write access to.

    Args:
        payment_id: The payment_id to flag (from get_payments or search_payments).
        reason: Why this payment is being flagged, e.g. "Amount looks unusually
                large for this vendor" or "Missing contract number on a payment
                over $50k".
    """
    if not reason or not reason.strip():
        return {"error": "reason cannot be empty — flags need a explanation to be useful."}

    with get_real_connection() as conn:
        payment = conn.execute(
            """
            SELECT p.payment_id, p.vendor, p.amount, p.category, d.department_name
            FROM payments p JOIN departments d ON d.department_code = p.department_code
            WHERE p.payment_id = ?
            """,
            (payment_id,),
        ).fetchone()

    if payment is None:
        return {"error": f"No payment found with payment_id {payment_id}"}

    flagged_at = datetime.now(timezone.utc).isoformat()
    with get_writable_real_connection() as conn:
        cursor = conn.execute(
            "INSERT INTO review_flags (payment_id, reason, flagged_at, status) "
            "VALUES (?, ?, ?, 'open')",
            (payment_id, reason.strip(), flagged_at),
        )
        conn.commit()
        flag_id = cursor.lastrowid

    return {
        "flag_id": flag_id,
        "status": "open",
        "flagged_at": flagged_at,
        "payment": {
            "payment_id": payment["payment_id"],
            "vendor": payment["vendor"],
            "amount": round(payment["amount"], 2),
            "category": payment["category"],
            "department_name": payment["department_name"],
        },
        "reason": reason.strip(),
    }


@mcp.tool()
def list_flagged_payments(status: Optional[str] = "open") -> list[dict]:
    """
    List review flags, joined with the underlying payment details.

    Args:
        status: Filter by "open" (default), "resolved", or null/None for both.
    """
    clauses = []
    params: list = []
    if status:
        if status not in ("open", "resolved"):
            return [{"error": "status must be 'open', 'resolved', or omitted"}]
        clauses.append("f.status = ?")
        params.append(status)
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""

    with get_writable_real_connection() as conn:
        rows = conn.execute(
            f"""
            SELECT f.flag_id, f.status, f.reason, f.flagged_at,
                   f.resolution_note, f.resolved_at,
                   p.payment_id, p.vendor, p.amount, p.category, d.department_name
            FROM review_flags f
            JOIN payments p ON p.payment_id = f.payment_id
            JOIN departments d ON d.department_code = p.department_code
            {where}
            ORDER BY f.flagged_at DESC
            """,
            params,
        ).fetchall()
        return [_row_to_dict(r) for r in rows]


@mcp.tool()
def resolve_flag(flag_id: int, resolution_note: str) -> dict:
    """
    Mark a review flag as resolved.

    Args:
        flag_id: The flag_id to resolve (from list_flagged_payments).
        resolution_note: What was found / what action was taken.
    """
    if not resolution_note or not resolution_note.strip():
        return {"error": "resolution_note cannot be empty."}

    with get_writable_real_connection() as conn:
        flag = conn.execute(
            "SELECT flag_id, status FROM review_flags WHERE flag_id = ?", (flag_id,)
        ).fetchone()
        if flag is None:
            return {"error": f"No flag found with flag_id {flag_id}"}
        if flag["status"] == "resolved":
            return {"error": f"Flag {flag_id} is already resolved."}

        resolved_at = datetime.now(timezone.utc).isoformat()
        conn.execute(
            "UPDATE review_flags SET status = 'resolved', resolution_note = ?, resolved_at = ? "
            "WHERE flag_id = ?",
            (resolution_note.strip(), resolved_at, flag_id),
        )
        conn.commit()

    return {
        "flag_id": flag_id,
        "status": "resolved",
        "resolution_note": resolution_note.strip(),
        "resolved_at": resolved_at,
    }


@mcp.resource("finance://schema")
def get_schema() -> str:
    """Expose the database schema so a client can inspect table structure."""
    with get_connection() as conn:
        rows = conn.execute(
            "SELECT sql FROM sqlite_master WHERE type = 'table'"
        ).fetchall()
        return "\n\n".join(r["sql"] for r in rows if r["sql"])


@mcp.resource("finance://data-freshness")
def get_data_freshness() -> str:
    """
    Report what real data is actually loaded right now: which fiscal years,
    how many rows, and when it was last pulled from DataSF. Unlike
    finance://schema (fixed structure), this changes every time
    load_real_data.py is re-run — a client can check this before assuming
    a given fiscal year is available.
    """
    if not REAL_DB_PATH.exists():
        return json.dumps({"status": "not_loaded", "note": "real_finance.db does not exist yet — run load_real_data.py"})

    with get_real_connection() as conn:
        fiscal_years = conn.execute(
            "SELECT fiscal_year, COUNT(*) AS payment_count, ROUND(SUM(amount), 2) AS total_amount "
            "FROM payments GROUP BY fiscal_year ORDER BY fiscal_year"
        ).fetchall()
        total_rows = conn.execute("SELECT COUNT(*) FROM payments").fetchone()[0]
        metadata = dict(
            conn.execute("SELECT key, value FROM load_metadata").fetchall()
        )

    return json.dumps(
        {
            "status": "loaded",
            "total_payments": total_rows,
            "fiscal_years_loaded": [dict(r) for r in fiscal_years],
            "source": metadata.get("source"),
            "loaded_at_utc": metadata.get("loaded_at_utc"),
            "note": (
                "Only the fiscal years listed above are available. Queries for "
                "years not listed here will return empty results, not errors."
            ),
        },
        indent=2,
    )


@mcp.prompt()
def monthly_close_review(year: int, month: int) -> str:
    """Prompt template: ask the model to walk through a monthly close review."""
    return (
        f"Use the finance tools to review {year:04d}-{month:02d}. "
        f"Pull the monthly summary, then the top expense categories, "
        f"and flag anything that looks unusual (large one-off transactions, "
        f"a category spiking versus what you'd expect). Summarize like a "
        f"controller writing a one-paragraph close note."
    )


if __name__ == "__main__":
    if MCP_TRANSPORT == "http":
        print(f"Starting Streamable HTTP server on http://{HTTP_HOST}:{HTTP_PORT}/mcp")
        print("Requests must include: Authorization: Bearer <MCP_AUTH_TOKEN>")
        mcp.run(transport="streamable-http")
    else:
        mcp.run()
