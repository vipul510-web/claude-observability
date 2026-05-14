"""FastAPI server for the Claude Code Observability dashboard."""

import sys
from pathlib import Path
from datetime import datetime, timedelta
from typing import Optional

import sqlite3

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse

# Ensure project root is on path for analyzer import
ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

DB_PATH = ROOT / "data" / "observability.db"
INDEX_HTML = ROOT / "templates" / "index.html"

app = FastAPI(title="Claude Code Observability", docs_url=None, redoc_url=None)


# ── DB helpers ────────────────────────────────────────────────────────────────

def get_db() -> sqlite3.Connection:
    if not DB_PATH.exists():
        return None
    conn = sqlite3.connect(str(DB_PATH), timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def rows_to_list(rows) -> list:
    if rows is None:
        return []
    return [dict(r) for r in rows]


# ── Routes ────────────────────────────────────────────────────────────────────

@app.get("/", response_class=HTMLResponse)
async def dashboard():
    return HTMLResponse(INDEX_HTML.read_text(encoding="utf-8"))


@app.get("/api/overview")
async def overview():
    conn = get_db()
    if conn is None:
        return {
            "total_sessions": 0, "total_input_tokens": 0, "total_output_tokens": 0,
            "total_cache_creation_tokens": 0, "total_cache_read_tokens": 0,
            "total_estimated_cost_usd": 0.0, "total_api_calls": 0,
            "total_tool_calls": 0, "cache_efficiency_pct": 0.0,
            "sessions_today": 0, "cost_today": 0.0, "no_data": True,
        }
    try:
        row = conn.execute("""
            SELECT
                COUNT(*)                           AS total_sessions,
                COALESCE(SUM(total_input_tokens),0)          AS total_input,
                COALESCE(SUM(total_output_tokens),0)         AS total_output,
                COALESCE(SUM(total_cache_creation_tokens),0) AS total_cache_create,
                COALESCE(SUM(total_cache_read_tokens),0)     AS total_cache_read,
                COALESCE(SUM(estimated_cost_usd),0)          AS total_cost,
                COALESCE(SUM(total_api_calls),0)             AS total_api_calls,
                COALESCE(SUM(total_tool_calls),0)            AS total_tool_calls
            FROM sessions
        """).fetchone()

        today = datetime.now().strftime("%Y-%m-%d")
        today_row = conn.execute("""
            SELECT COUNT(*) AS s, COALESCE(SUM(estimated_cost_usd),0) AS c
            FROM sessions WHERE DATE(ended_at) = ?
        """, (today,)).fetchone()

        eff_input = (row["total_input"] or 0) + (row["total_cache_read"] or 0)
        cache_read = row["total_cache_read"] or 0
        cache_eff = (cache_read / eff_input * 100) if eff_input > 0 else 0.0

        return {
            "total_sessions":              row["total_sessions"] or 0,
            "total_input_tokens":          row["total_input"],
            "total_output_tokens":         row["total_output"],
            "total_cache_creation_tokens": row["total_cache_create"],
            "total_cache_read_tokens":     row["total_cache_read"],
            "total_estimated_cost_usd":    round(row["total_cost"], 4),
            "total_api_calls":             row["total_api_calls"],
            "total_tool_calls":            row["total_tool_calls"],
            "cache_efficiency_pct":        round(cache_eff, 1),
            "sessions_today":              today_row["s"] or 0,
            "cost_today":                  round(today_row["c"] or 0, 4),
            "no_data":                     (row["total_sessions"] or 0) == 0,
        }
    finally:
        conn.close()


@app.get("/api/sessions")
async def sessions(page: int = 1, limit: int = 25, search: Optional[str] = None):
    conn = get_db()
    if conn is None:
        return {"sessions": [], "total": 0, "page": 1, "pages": 0}
    try:
        where = ""
        params: list = []
        if search:
            where = "WHERE id LIKE ? OR project_path LIKE ? OR model LIKE ?"
            params = [f"%{search}%", f"%{search}%", f"%{search}%"]

        total = conn.execute(f"SELECT COUNT(*) FROM sessions {where}", params).fetchone()[0]
        offset = (page - 1) * limit
        rows = conn.execute(
            f"SELECT * FROM sessions {where} ORDER BY ended_at DESC LIMIT ? OFFSET ?",
            params + [limit, offset]
        ).fetchall()

        return {
            "sessions": rows_to_list(rows),
            "total": total,
            "page": page,
            "pages": max(1, (total + limit - 1) // limit),
        }
    finally:
        conn.close()


@app.get("/api/sessions/{session_id}")
async def session_detail(session_id: str):
    conn = get_db()
    if conn is None:
        raise HTTPException(status_code=404, detail="No data yet")
    try:
        s = conn.execute("SELECT * FROM sessions WHERE id = ?", (session_id,)).fetchone()
        if not s:
            raise HTTPException(status_code=404, detail="Session not found")

        api_calls = conn.execute(
            "SELECT * FROM api_calls WHERE session_id = ? ORDER BY call_index",
            (session_id,)
        ).fetchall()

        tool_calls = conn.execute(
            "SELECT * FROM tool_calls WHERE session_id = ? ORDER BY id",
            (session_id,)
        ).fetchall()

        user_messages = conn.execute(
            "SELECT * FROM user_messages WHERE session_id = ? ORDER BY msg_index",
            (session_id,)
        ).fetchall()

        from analysis.analyzer import analyze_session
        issues = analyze_session(dict(s), rows_to_list(api_calls), rows_to_list(tool_calls))

        return {
            "session":       dict(s),
            "api_calls":     rows_to_list(api_calls),
            "tool_calls":    rows_to_list(tool_calls),
            "user_messages": rows_to_list(user_messages),
            "issues":        issues,
        }
    finally:
        conn.close()


@app.get("/api/monthly")
async def monthly():
    """Current-month usage stats for Pro/Max plan users."""
    import calendar as cal_mod
    conn = get_db()
    empty = {
        "sessions_this_month": 0, "tokens_consumed": 0,
        "input_tokens": 0, "output_tokens": 0,
        "cache_creation": 0, "cache_read": 0,
        "cost_this_month": 0.0, "days_elapsed": 1,
        "days_in_month": 31, "calendar_days_left": 30,
        "avg_tokens_per_day": 0, "month_label": "",
    }
    if conn is None:
        return empty
    try:
        today = datetime.now().date()
        month_start = today.replace(day=1).isoformat()
        days_in_month = cal_mod.monthrange(today.year, today.month)[1]
        days_elapsed = today.day
        calendar_days_left = days_in_month - days_elapsed

        row = conn.execute("""
            SELECT
                COUNT(*) AS sessions,
                COALESCE(SUM(total_input_tokens), 0)          AS inp,
                COALESCE(SUM(total_output_tokens), 0)         AS out,
                COALESCE(SUM(total_cache_creation_tokens), 0) AS cc,
                COALESCE(SUM(total_cache_read_tokens), 0)     AS cr,
                COALESCE(SUM(estimated_cost_usd), 0)          AS cost
            FROM sessions
            WHERE DATE(ended_at) >= ?
        """, (month_start,)).fetchone()

        # consumed = tokens that draw from your monthly plan allocation
        consumed = (row["inp"] or 0) + (row["out"] or 0) + (row["cc"] or 0)
        avg_per_day = consumed / max(days_elapsed, 1)

        return {
            "sessions_this_month": row["sessions"] or 0,
            "tokens_consumed":     consumed,
            "input_tokens":        row["inp"] or 0,
            "output_tokens":       row["out"] or 0,
            "cache_creation":      row["cc"] or 0,
            "cache_read":          row["cr"] or 0,
            "cost_this_month":     round(row["cost"] or 0, 4),
            "days_elapsed":        days_elapsed,
            "days_in_month":       days_in_month,
            "calendar_days_left":  calendar_days_left,
            "avg_tokens_per_day":  int(avg_per_day),
            "month_label":         today.strftime("%B %Y"),
        }
    finally:
        conn.close()


@app.get("/api/tools")
async def tools(limit: int = 15):
    conn = get_db()
    if conn is None:
        return {"tools": []}
    try:
        rows = conn.execute("""
            SELECT
                tool_name,
                COUNT(*)                        AS total_calls,
                COALESCE(AVG(response_size), 0) AS avg_response_size,
                COALESCE(SUM(response_size), 0) AS total_response_size,
                COALESCE(MAX(response_size), 0) AS max_response_size,
                COUNT(DISTINCT session_id)      AS sessions_used_in
            FROM tool_calls
            GROUP BY tool_name
            ORDER BY total_calls DESC
            LIMIT ?
        """, (limit,)).fetchall()
        return {"tools": rows_to_list(rows)}
    finally:
        conn.close()


@app.get("/api/trends")
async def trends(days: int = 30):
    conn = get_db()
    if conn is None:
        return {"trends": []}
    try:
        start = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d")
        rows = conn.execute("""
            SELECT
                DATE(ended_at)                               AS date,
                COUNT(*)                                     AS sessions,
                COALESCE(SUM(total_input_tokens), 0)         AS input_tokens,
                COALESCE(SUM(total_output_tokens), 0)        AS output_tokens,
                COALESCE(SUM(total_cache_read_tokens), 0)    AS cache_read_tokens,
                COALESCE(SUM(total_cache_creation_tokens),0) AS cache_creation_tokens,
                COALESCE(SUM(estimated_cost_usd), 0)         AS cost
            FROM sessions
            WHERE DATE(ended_at) >= ?
            GROUP BY DATE(ended_at)
            ORDER BY date
        """, (start,)).fetchall()
        return {"trends": rows_to_list(rows)}
    finally:
        conn.close()


@app.get("/api/analysis")
async def analysis():
    conn = get_db()
    if conn is None:
        return {
            "issues": [], "suggestions": [],
            "score": {"overall": 0, "caching": 0, "context": 0, "tool_efficiency": 0},
            "summary": {"total_sessions": 0, "total_tokens": 0, "total_cost": 0.0,
                        "estimated_waste_tokens": 0, "estimated_waste_cost": 0.0,
                        "potential_savings_pct": 0, "cache_hit_rate_pct": 0.0},
            "most_verbose_tools": [],
        }
    try:
        from analysis.analyzer import analyze_all

        sessions_rows  = conn.execute("SELECT * FROM sessions ORDER BY ended_at DESC").fetchall()
        api_calls_rows = conn.execute("SELECT * FROM api_calls").fetchall()
        tool_calls_rows= conn.execute("SELECT * FROM tool_calls").fetchall()

        return analyze_all(
            rows_to_list(sessions_rows),
            rows_to_list(api_calls_rows),
            rows_to_list(tool_calls_rows),
        )
    finally:
        conn.close()


@app.get("/api/models")
async def models():
    """Breakdown of token usage and cost by model."""
    conn = get_db()
    if conn is None:
        return {"models": []}
    try:
        rows = conn.execute("""
            SELECT
                model,
                COUNT(*)                             AS sessions,
                COALESCE(SUM(total_input_tokens),0)  AS input_tokens,
                COALESCE(SUM(total_output_tokens),0) AS output_tokens,
                COALESCE(SUM(estimated_cost_usd),0)  AS cost
            FROM sessions
            WHERE model IS NOT NULL AND model != ''
            GROUP BY model
            ORDER BY cost DESC
        """).fetchall()
        return {"models": rows_to_list(rows)}
    finally:
        conn.close()


@app.get("/api/health")
async def health():
    return {"status": "ok", "db_exists": DB_PATH.exists(), "db_path": str(DB_PATH)}
