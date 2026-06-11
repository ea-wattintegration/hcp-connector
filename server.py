#!/usr/bin/env python3
"""
Housecall Pro -> Claude custom connector (remote MCP server).

A small, READ-ONLY server that lets Claude look up your Housecall Pro data
live. It exposes a handful of safe "lookup" tools (recent jobs, find a
customer, open invoices, job details, a revenue snapshot). It never creates,
edits, or deletes anything in Housecall Pro.

It speaks MCP over Streamable HTTP so it can be added to Claude as a custom
connector (Settings -> Connectors -> Add custom connector).

Required environment variables (set these in your hosting dashboard):
  HCP_API_KEY      Your Housecall Pro API key.
  MCP_PATH_SECRET  A long random string. It becomes part of the connector URL
                   so strangers who don't know it can't reach your data.

Optional:
  AMOUNTS_IN_CENTS  "true" (default) or "false" if dollar values look 100x off.

Run locally for testing:
  pip install -r requirements.txt
  HCP_API_KEY=xxx MCP_PATH_SECRET=test123 uvicorn server:app --port 8000
  -> MCP endpoint: http://localhost:8000/test123
"""
import os
import time
import requests
from mcp.server.fastmcp import FastMCP

BASE = "https://api.housecallpro.com"
API_KEY = os.environ.get("HCP_API_KEY", "")
SECRET = os.environ.get("MCP_PATH_SECRET", "mcp")
AMOUNTS_IN_CENTS = os.environ.get("AMOUNTS_IN_CENTS", "true").lower() != "false"

HEADERS = {"Authorization": f"Token {API_KEY}", "Accept": "application/json"}

# The secret becomes the URL path, e.g. https://your-app.onrender.com/<SECRET>
mcp = FastMCP("Housecall Pro", streamable_http_path=f"/{SECRET}")


# ----------------------------------------------------------------- helpers
def _get(path, params=None):
    r = requests.get(f"{BASE}{path}", headers=HEADERS, params=params or {}, timeout=30)
    r.raise_for_status()
    return r.json()


def _rows(payload):
    if isinstance(payload, list):
        return payload
    if isinstance(payload, dict):
        return next((v for v in payload.values() if isinstance(v, list)), [])
    return []


def _pages(path, params=None, max_pages=3, page_size=100):
    """Fetch up to max_pages of a list endpoint (keeps responses fast)."""
    out, page = [], 1
    while page <= max_pages:
        payload = _get(path, {**(params or {}), "page": page, "page_size": page_size})
        rows = _rows(payload)
        out.extend(rows)
        if len(rows) < page_size:
            break
        page += 1
        time.sleep(0.15)
    return out


def _money(x):
    try:
        v = float(x or 0)
    except (TypeError, ValueError):
        v = 0.0
    return v / 100.0 if AMOUNTS_IN_CENTS else v


def _usd(v):
    return "${:,.2f}".format(v)


def _job_date(j):
    wt = j.get("work_timestamps") or {}
    return (wt.get("completed_at") or wt.get("on_my_way_at")
            or (j.get("schedule") or {}).get("scheduled_start")
            or j.get("created_at") or "")


def _customer_name(j):
    c = j.get("customer") or {}
    return (c.get("name")
            or " ".join(filter(None, [c.get("first_name"), c.get("last_name")]))
            or c.get("company") or "Unknown").strip()


# ------------------------------------------------------------------- tools
@mcp.tool()
def recent_jobs(limit: int = 10) -> str:
    """List the most recent jobs (customer, date, status, amount). limit max 25."""
    limit = max(1, min(int(limit), 25))
    jobs = _pages("/jobs", max_pages=1, page_size=100)
    jobs.sort(key=_job_date, reverse=True)
    if not jobs:
        return "No jobs found."
    lines = []
    for j in jobs[:limit]:
        lines.append(
            f"- {(_job_date(j) or '')[:10]} | {_customer_name(j)} | "
            f"{j.get('work_status', '?')} | {_usd(_money(j.get('total_amount')))} | "
            f"{(j.get('description') or '').strip()[:80]}"
        )
    return "\n".join(lines)


@mcp.tool()
def find_customer(name: str) -> str:
    """Find customers whose name, email, or company matches the query text."""
    q = (name or "").strip().lower()
    if not q:
        return "Provide a name to search for."
    customers = _pages("/customers", params={"q": name}, max_pages=3, page_size=100)
    hits = []
    for c in customers:
        blob = " ".join(str(c.get(k, "")) for k in
                        ("first_name", "last_name", "company", "email", "mobile_number")).lower()
        if q in blob:
            full = " ".join(filter(None, [c.get("first_name"), c.get("last_name")])) or c.get("company") or "—"
            hits.append(f"- {full} | {c.get('email','')} | {c.get('mobile_number','')} | id={c.get('id')}")
    return "\n".join(hits[:15]) if hits else f"No customers matched '{name}'."


@mcp.tool()
def open_invoices(limit: int = 20) -> str:
    """List unpaid / outstanding invoices (amount due, due date, customer)."""
    limit = max(1, min(int(limit), 50))
    invoices = _pages("/invoices", max_pages=3, page_size=100)
    open_inv = [i for i in invoices
                if (i.get("status") or "").lower() not in ("paid", "void", "refunded")
                and _money(i.get("due_amount") or i.get("amount")) > 0]
    open_inv.sort(key=lambda i: i.get("due_at") or i.get("invoice_date") or "")
    if not open_inv:
        return "No open invoices found in the recent set."
    total = sum(_money(i.get("due_amount") or i.get("amount")) for i in open_inv)
    lines = [f"Open invoices (showing {min(limit, len(open_inv))}), total due {_usd(total)}:"]
    for i in open_inv[:limit]:
        lines.append(
            f"- #{i.get('invoice_number','?')} | due {(i.get('due_at') or i.get('invoice_date') or '')[:10]} "
            f"| {_usd(_money(i.get('due_amount') or i.get('amount')))} | status={i.get('status','?')}"
        )
    return "\n".join(lines)


@mcp.tool()
def revenue_snapshot() -> str:
    """A quick snapshot across recent jobs: count, total value, outstanding, avg ticket."""
    jobs = _pages("/jobs", max_pages=5, page_size=100)  # recent ~500 jobs
    if not jobs:
        return "No jobs found."
    total = sum(_money(j.get("total_amount")) for j in jobs)
    outstanding = sum(_money(j.get("outstanding_balance")) for j in jobs)
    n = len(jobs)
    return (f"Based on the {n} most recent jobs:\n"
            f"- Total job value: {_usd(total)}\n"
            f"- Outstanding balance: {_usd(outstanding)}\n"
            f"- Average ticket: {_usd(total / n)}\n"
            f"(For a full-history breakdown, use the dashboard script.)")


# Starlette ASGI app for hosting (uvicorn server:app)
app = mcp.streamable_http_app()


if __name__ == "__main__":
    mcp.run(transport="streamable-http")
