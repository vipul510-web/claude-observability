"""FastAPI server for the Claude Code Observability dashboard."""

import ast
import re
import sys
from collections import Counter, defaultdict
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


_FAIL_PATTERNS_STDERR = [
    (re.compile(r'command not found',                    re.I), 'command_not_found',  'Command not found'),
    (re.compile(r'No such file or directory',            re.I), 'path_not_found',     'Path not found'),
    (re.compile(r'Permission denied',                    re.I), 'permission_denied',  'Permission denied'),
    (re.compile(r'ModuleNotFoundError|No module named',  re.I), 'import_error',       'Import error'),
    (re.compile(r'SyntaxError|IndentationError',         re.I), 'syntax_error',       'Syntax error'),
    (re.compile(r'Traceback \(most recent call',         re.I), 'exception',          'Python exception'),
    (re.compile(r'[1-9]\d* failed',                      re.I), 'test_failure',       'Test failure'),
    (re.compile(r'\bFAILED\s+\S+\.py',                  re.I), 'test_failure',       'Test failure'),
    (re.compile(r'npm ERR!',                             re.I), 'npm_error',          'npm error'),
    (re.compile(r'exit code [1-9]',                      re.I), 'nonzero_exit',       'Non-zero exit'),
    (re.compile(r'\bError:',                             re.I), 'generic_error',      'Error'),
]

_FAIL_PATTERNS_STDOUT = [
    (re.compile(r'Traceback \(most recent call',         re.I), 'exception',          'Python exception'),
    (re.compile(r'ModuleNotFoundError|No module named',  re.I), 'import_error',       'Import error'),
    (re.compile(r'SyntaxError|IndentationError',         re.I), 'syntax_error',       'Syntax error'),
    (re.compile(r'npm ERR!',                             re.I), 'npm_error',          'npm error'),
    (re.compile(r'[1-9]\d* failed',                      re.I), 'test_failure',       'Test failure'),
    (re.compile(r'\bFAILED\s+\S+\.py',                  re.I), 'test_failure',       'Test failure'),
]

_ERROR_LABELS = {r[1]: r[2] for r in _FAIL_PATTERNS_STDERR}

_TOOL_CLASSIFY = {'Bash', 'bash', 'computer', 'mcp__', 'execute'}

_NO_STDERR = '__NO_STDERR__'


def _split_bash_response(text: str) -> tuple:
    try:
        d = ast.literal_eval(text)
        if isinstance(d, dict):
            return (d.get('stdout') or '', d.get('stderr') or '')
    except Exception:
        pass
    r = text.strip()
    if r.startswith("{'stdout'") or r.startswith('{"stdout"'):
        m = re.search(r"['\"]stderr['\"]\s*:\s*['\"](.+?)['\"]", r, re.DOTALL)
        if m and m.group(1).strip():
            return ('', m.group(1))
        return (_NO_STDERR, '')
    return ('', text)


def _classify(text: str, tool_name: str = '') -> Optional[str]:
    if not text:
        return None
    if tool_name and not any(tool_name.startswith(t) for t in _TOOL_CLASSIFY):
        return None
    stdout, stderr = _split_bash_response(text)
    if stderr.strip():
        for pat, cls, _ in _FAIL_PATTERNS_STDERR:
            if pat.search(stderr):
                return cls
        return None
    if stdout == _NO_STDERR:
        return None
    check = stdout if stdout.strip() else text
    for pat, cls, _ in _FAIL_PATTERNS_STDOUT:
        if pat.search(check):
            return cls
    return None


@app.get("/api/failures")
async def failures_endpoint():
    conn = get_db()
    if conn is None:
        return {"stats": {}, "recent_failures": [], "loops": []}
    try:
        # Loop detection: tool called 3+ times in a session — pure SQL
        loop_rows = conn.execute("""
            SELECT
                tc.session_id,
                tc.tool_name,
                COUNT(*)        AS call_count,
                s.ended_at,
                s.project_path
            FROM tool_calls tc
            LEFT JOIN sessions s ON tc.session_id = s.id
            GROUP BY tc.session_id, tc.tool_name
            HAVING COUNT(*) >= 3
            ORDER BY call_count DESC
            LIMIT 100
        """).fetchall()
        loops_raw = rows_to_list(loop_rows)

        # Failure detection: use stored column when available, else classify preview
        pragma_cols = {r[1] for r in conn.execute("PRAGMA table_info(tool_calls)").fetchall()}
        has_stored  = 'is_failure' in pragma_cols

        if has_stored:
            # Fetch stored failures first; also fetch un-classified rows for backfill
            tc_rows = conn.execute("""
                SELECT tc.id, tc.session_id, tc.tool_name,
                       tc.tool_response_preview, tc.response_size, tc.timestamp,
                       tc.is_failure, tc.error_class,
                       s.ended_at, s.project_path
                FROM tool_calls tc
                LEFT JOIN sessions s ON tc.session_id = s.id
                ORDER BY tc.id DESC
            """).fetchall()
        else:
            tc_rows = conn.execute("""
                SELECT tc.id, tc.session_id, tc.tool_name,
                       tc.tool_response_preview, tc.response_size, tc.timestamp,
                       0 AS is_failure, NULL AS error_class,
                       s.ended_at, s.project_path
                FROM tool_calls tc
                LEFT JOIN sessions s ON tc.session_id = s.id
                ORDER BY tc.id DESC
            """).fetchall()

        all_calls = rows_to_list(tc_rows)

        # Classify each call; stored value wins, else derive from preview
        failures = []
        session_fail_counts: dict = defaultdict(Counter)

        for call in all_calls:
            tool = call.get('tool_name') or ''
            # Skip tools whose output is file content — too many false positives
            if tool and not any(tool.startswith(t) for t in _TOOL_CLASSIFY):
                continue
            preview = call.get('tool_response_preview') or ''
            # Always re-classify from preview (stored error_class may be from old buggy logic).
            # Only fall back to stored value if preview is empty and is_failure=1 explicitly.
            ec = _classify(preview, tool)
            if ec is None and not preview.strip() and call.get('is_failure') == 1:
                ec = call.get('error_class')
            if ec:
                failures.append({
                    'id':           call['id'],
                    'session_id':   call['session_id'],
                    'tool_name':    call['tool_name'] or '—',
                    'error_class':  ec,
                    'error_label':  _ERROR_LABELS.get(ec, ec),
                    'snippet':      (call.get('tool_response_preview') or '')[:200].strip(),
                    'timestamp':    call.get('timestamp'),
                    'response_size':call.get('response_size', 0),
                    'project_path': call.get('project_path') or '',
                    'ended_at':     call.get('ended_at'),
                })
                session_fail_counts[call['session_id']][call['tool_name'] or ''] += 1

        # Attach failure counts to loops
        for loop in loops_raw:
            loop['failure_count'] = session_fail_counts.get(
                loop['session_id'], {}
            ).get(loop['tool_name'], 0)

        # Stats
        tool_fail_ctr  = Counter(f['tool_name']   for f in failures)
        error_cls_ctr  = Counter(f['error_class'] for f in failures)
        top_tool       = tool_fail_ctr.most_common(1)
        top_error      = error_cls_ctr.most_common(1)

        # ── Token waste: API calls consumed after first failure in each session ──
        # Build first-failure timestamp per session from our classified list
        first_fail_ts: dict = {}
        for f in failures:
            sid = f['session_id']
            ts  = f['timestamp'] or ''
            if sid and sid != 'unknown' and ts:
                if sid not in first_fail_ts or ts < first_fail_ts[sid]:
                    first_fail_ts[sid] = ts

        waste_by_session = []
        total_waste_tokens = 0
        total_waste_cost   = 0.0

        for sid, first_ts in first_fail_ts.items():
            row = conn.execute("""
                SELECT
                    COUNT(*)                                         AS api_calls_after,
                    COALESCE(SUM(input_tokens + output_tokens), 0)  AS tokens_after,
                    COALESCE(SUM(estimated_cost_usd), 0)            AS cost_after
                FROM api_calls
                WHERE session_id = ? AND timestamp > ?
            """, (sid, first_ts)).fetchone()

            if row and (row['api_calls_after'] or 0) > 0:
                tokens = row['tokens_after'] or 0
                cost   = row['cost_after']   or 0.0
                total_waste_tokens += tokens
                total_waste_cost   += cost
                # find first error for this session
                first_err = next(
                    (f['error_label'] for f in failures if f['session_id'] == sid),
                    '—'
                )
                waste_by_session.append({
                    'session_id':        sid,
                    'first_failure_at':  first_ts,
                    'first_error':       first_err,
                    'api_calls_after':   row['api_calls_after'],
                    'tokens_after':      tokens,
                    'cost_after':        round(cost, 4),
                    'project_path':      first_fail_ts.get(sid + '_proj', ''),
                })

        waste_by_session.sort(key=lambda x: x['tokens_after'], reverse=True)

        # Enrich waste rows with project_path from failures list
        proj_by_session = {f['session_id']: f.get('project_path', '') for f in failures}
        for w in waste_by_session:
            w['project_path'] = proj_by_session.get(w['session_id'], '')

        stats = {
            'total_failures':          len(failures),
            'sessions_with_failures':  len({f['session_id'] for f in failures}),
            'sessions_with_loops':     len({l['session_id'] for l in loops_raw}),
            'most_failing_tool':       top_tool[0][0]  if top_tool  else None,
            'most_common_error':       top_error[0][0] if top_error else None,
            'most_common_error_label': _ERROR_LABELS.get(top_error[0][0], top_error[0][0]) if top_error else None,
            'total_waste_tokens':      total_waste_tokens,
            'total_waste_cost':        round(total_waste_cost, 4),
        }

        return {
            'stats':           stats,
            'recent_failures': failures[:50],
            'loops':           loops_raw[:50],
            'waste':           waste_by_session[:20],
        }
    finally:
        conn.close()


@app.get("/api/health")
async def health():
    return {"status": "ok", "db_exists": DB_PATH.exists(), "db_path": str(DB_PATH)}
