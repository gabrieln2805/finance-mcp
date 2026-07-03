# Financial Records MCP Server

A from-scratch MCP (Model Context Protocol) server exposing tools over a
synthetic general-ledger-style financial database. Built on the official
`mcp` Python SDK's `FastMCP` interface.

## What's in the box

```
finance-mcp/
├── generate_data.py   # builds finance.db (14 GL accounts, ~780 transactions)
├── server.py           # the MCP server: tools, one resource, one prompt
├── requirements.txt
└── finance.db           # generated (not committed — regenerate any time)
```

Schema: `accounts` (account_id, account_name, account_type, gl_code, currency)
and `transactions` (transaction_id, account_id, txn_date, amount, currency,
category, vendor, cost_center, description). Loosely modeled on a SAP-style
chart of accounts (numeric GL codes, Asset/Liability/Revenue/Expense types)
so it feels like real FP&A data rather than a toy example.

## 1. Setup

```bash
cd finance-mcp
python3 -m venv .venv
source .venv/bin/activate        # Windows: .venv\Scripts\activate

pip install -r requirements.txt
python generate_data.py           # creates finance.db
```

You should see: `Created finance.db with 14 accounts and 779 transactions.`

## 2. Test it standalone with the MCP Inspector

The Inspector is a browser UI that lets you call tools directly without
needing Claude at all — the fastest way to check your server works before
wiring it into anything.

```bash
mcp dev server.py
```

This opens the Inspector in your browser. Click **Tools**, pick e.g.
`monthly_summary`, fill in `year=2026, month=3`, hit **Run Tool**, and you
should see JSON back immediately.

## 3. Connect it to Claude Desktop

Edit (or create) your Claude Desktop config file:

- macOS: `~/Library/Application Support/Claude/claude_desktop_config.json`
- Windows: `%APPDATA%\Claude\claude_desktop_config.json`

Add an entry (use the **absolute path** to your project folder and the
Python interpreter inside your venv):

```json
{
  "mcpServers": {
    "finance-records": {
      "command": "/absolute/path/to/finance-mcp/.venv/bin/python",
      "args": ["/absolute/path/to/finance-mcp/server.py"]
    }
  }
}
```

On Windows, `command` would be something like
`C:\\path\\to\\finance-mcp\\.venv\\Scripts\\python.exe`.

Restart Claude Desktop. You should see a small hammer/tools icon indicating
the server connected, and you can ask things like *"What was our net income
in March 2026?"* or *"Show me the top spending categories in Q1"* and Claude
will call the tools directly.

## 4. What each tool does

| Tool | Purpose |
|---|---|
| `list_accounts` | Full chart of accounts |
| `get_account_balance(account_id)` | Signed balance for one account |
| `get_transactions(...)` | Filtered transaction listing (account, date range, category) |
| `search_transactions(keyword)` | Free-text search over description/vendor |
| `spending_by_category(...)` | Aggregated totals by category |
| `monthly_summary(year, month)` | Revenue, expense, net, top expense categories for a month |

Plus one **resource** (`finance://schema`, exposes the raw SQL schema) and
one **prompt** (`monthly_close_review`, a template that chains several tool
calls into a close-review workflow).

## 5. Ideas for extending it (intermediate → advanced)

- **Write tools**: add `add_transaction(...)` / `void_transaction(id)` —
  good exercise in validating inputs and returning clear confirmations,
  since MCP tools with side effects should be unambiguous about what changed.
- **Budgets & variance**: add a `budgets` table and a `budget_vs_actual`
  tool that flags categories over threshold.
- **Anomaly detection**: use your existing ML stack (e.g. an XGBoost or
  simple z-score model) inside a tool to flag outlier transactions.
- **Real data**: swap SQLite for a read-only connection into your actual
  SAP/Snowflake tables (start read-only, and be deliberate about what
  columns you expose — MCP tools become part of your data's attack surface).
- **Auth**: if you ever expose this over HTTP instead of stdio, look at the
  SDK's OAuth support before putting it anywhere reachable off your machine.

## Troubleshooting

- `ModuleNotFoundError: No module named 'mcp'` → you're not inside the
  venv, or `pip install -r requirements.txt` didn't complete — re-run it.
- Inspector opens but tools list is empty → check the terminal running
  `mcp dev server.py` for a stack trace; usually a typo in a decorator.
- Claude Desktop doesn't show the server → double-check the config path is
  absolute (not `~` or relative), valid JSON (no trailing commas), and that
  you fully restarted the app (not just closed the window).
