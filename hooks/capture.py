#!/usr/bin/env python3
"""
Claude Code hook script — captures session data to SQLite.
Uses ONLY stdlib so it works with any Python 3.8+ without a venv.

Called by Claude Code hooks as:
  python capture.py stop       (Stop hook)
  python capture.py post_tool  (PostToolUse hook)
"""

import ast
import sys
import json
import re
import sqlite3
from pathlib import Path
from datetime import datetime, timezone

DB_PATH   = Path(__file__).parent.parent / "data" / "observability.db"
ERROR_LOG = Path(__file__).parent.parent / "data" / "errors.log"

# ── Pricing ───────────────────────────────────────────────────────────────────

PRICING = {
    "claude-opus-4-7":          {"input": 15.0/1e6,  "output": 75.0/1e6,  "cache_write": 18.75/1e6, "cache_read": 1.50/1e6},
    "claude-opus-4-6":          {"input": 15.0/1e6,  "output": 75.0/1e6,  "cache_write": 18.75/1e6, "cache_read": 1.50/1e6},
    "claude-sonnet-4-6":        {"input": 3.0/1e6,   "output": 15.0/1e6,  "cache_write": 3.75/1e6,  "cache_read": 0.30/1e6},
    "claude-sonnet-4-5":        {"input": 3.0/1e6,   "output": 15.0/1e6,  "cache_write": 3.75/1e6,  "cache_read": 0.30/1e6},
    "claude-haiku-4-5":         {"input": 0.80/1e6,  "output": 4.0/1e6,   "cache_write": 1.00/1e6,  "cache_read": 0.08/1e6},
    "claude-haiku-4-5-20251001":{"input": 0.80/1e6,  "output": 4.0/1e6,   "cache_write": 1.00/1e6,  "cache_read": 0.08/1e6},
}
DEFAULT_PRICING = {"input": 3.0/1e6, "output": 15.0/1e6, "cache_write": 3.75/1e6, "cache_read": 0.30/1e6}


# ── Failure detection ─────────────────────────────────────────────────────────

# Patterns safe to run against stderr (errors are unambiguous there)
_FAIL_PATTERNS_STDERR = [
    (re.compile(r'command not found',                    re.I), 'command_not_found'),
    (re.compile(r'No such file or directory',            re.I), 'path_not_found'),
    (re.compile(r'Permission denied',                    re.I), 'permission_denied'),
    (re.compile(r'ModuleNotFoundError|No module named',  re.I), 'import_error'),
    (re.compile(r'SyntaxError|IndentationError',         re.I), 'syntax_error'),
    (re.compile(r'Traceback \(most recent call',         re.I), 'exception'),
    (re.compile(r'[1-9]\d* failed',                      re.I), 'test_failure'),
    (re.compile(r'\bFAILED\s+\S+\.py',                  re.I), 'test_failure'),
    (re.compile(r'npm ERR!',                             re.I), 'npm_error'),
    (re.compile(r'exit code [1-9]',                      re.I), 'nonzero_exit'),
    (re.compile(r'\bError:',                             re.I), 'generic_error'),
]

# Strict patterns safe to match in stdout (truly unambiguous signals)
_FAIL_PATTERNS_STDOUT = [
    (re.compile(r'Traceback \(most recent call',         re.I), 'exception'),
    (re.compile(r'ModuleNotFoundError|No module named',  re.I), 'import_error'),
    (re.compile(r'SyntaxError|IndentationError',         re.I), 'syntax_error'),
    (re.compile(r'npm ERR!',                             re.I), 'npm_error'),
    (re.compile(r'[1-9]\d* failed',                      re.I), 'test_failure'),
    (re.compile(r'\bFAILED\s+\S+\.py',                  re.I), 'test_failure'),
]

# Only classify tools that produce shell/runtime output
_TOOL_CLASSIFY = {'Bash', 'bash', 'computer', 'mcp__', 'execute'}

# Sentinel returned when response is a structured Bash dict with no extractable stderr
_NO_STDERR = '__NO_STDERR__'


def _split_bash_response(response: str) -> tuple:
    """Return (stdout, stderr) from a Bash tool response.

    Claude Code uses Python-dict format: {'stdout': '...', 'stderr': '...', ...}
    Responses are stored truncated at 1000 chars so ast.literal_eval often fails.
    We fall back to regex extraction, using _NO_STDERR sentinel when we can confirm
    stderr is empty so callers know not to use broad patterns on stdout content.
    """
    # Fast path: full parseable dict
    try:
        d = ast.literal_eval(response)
        if isinstance(d, dict):
            return (d.get('stdout') or '', d.get('stderr') or '')
    except Exception:
        pass

    # Structured but truncated: detect stderr field
    r = response.strip()
    if r.startswith("{'stdout'") or r.startswith('{"stdout"'):
        # Try to extract non-empty stderr
        m = re.search(r"['\"]stderr['\"]\s*:\s*['\"](.+?)['\"]", r, re.DOTALL)
        if m and m.group(1).strip():
            return ('', m.group(1))
        # stderr key is present and empty (common case)
        return (_NO_STDERR, '')

    # Unstructured plain-text response
    return ('', response)


def detect_failure(response: str, tool_name: str = '') -> str | None:
    """Return error_class string if response signals a failure, else None."""
    if not response:
        return None
    if tool_name and not any(tool_name.startswith(t) for t in _TOOL_CLASSIFY):
        return None

    stdout, stderr = _split_bash_response(response)

    # Non-empty stderr: match all patterns
    if stderr.strip():
        for pat, cls in _FAIL_PATTERNS_STDERR:
            if pat.search(stderr):
                return cls
        return None

    if stdout == _NO_STDERR:
        # Structured response confirmed to have no stderr — no error
        return None

    # Plain-text or stdout-only: strict patterns only to avoid false positives
    check = stdout if stdout.strip() else response
    for pat, cls in _FAIL_PATTERNS_STDOUT:
        if pat.search(check):
            return cls

    return None


def get_pricing(model: str) -> dict:
    if not model:
        return DEFAULT_PRICING
    for key, p in PRICING.items():
        if key in model:
            return p
    return DEFAULT_PRICING


def calc_cost(input_tokens, output_tokens, cache_creation, cache_read, model) -> float:
    p = get_pricing(model)
    return (
        input_tokens   * p["input"] +
        output_tokens  * p["output"] +
        cache_creation * p["cache_write"] +
        cache_read     * p["cache_read"]
    )


# ── Schema ────────────────────────────────────────────────────────────────────

SCHEMA = """
CREATE TABLE IF NOT EXISTS sessions (
    id                          TEXT PRIMARY KEY,
    project_path                TEXT,
    started_at                  TEXT,
    ended_at                    TEXT,
    total_input_tokens          INTEGER DEFAULT 0,
    total_output_tokens         INTEGER DEFAULT 0,
    total_cache_creation_tokens INTEGER DEFAULT 0,
    total_cache_read_tokens     INTEGER DEFAULT 0,
    total_api_calls             INTEGER DEFAULT 0,
    total_tool_calls            INTEGER DEFAULT 0,
    model                       TEXT,
    estimated_cost_usd          REAL DEFAULT 0.0,
    transcript_path             TEXT
);

CREATE TABLE IF NOT EXISTS api_calls (
    id                    INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id            TEXT,
    call_index            INTEGER,
    timestamp             TEXT,
    input_tokens          INTEGER DEFAULT 0,
    output_tokens         INTEGER DEFAULT 0,
    cache_creation_tokens INTEGER DEFAULT 0,
    cache_read_tokens     INTEGER DEFAULT 0,
    model                 TEXT,
    stop_reason           TEXT,
    estimated_cost_usd    REAL DEFAULT 0.0,
    FOREIGN KEY (session_id) REFERENCES sessions(id)
);

CREATE TABLE IF NOT EXISTS user_messages (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id        TEXT,
    msg_index         INTEGER,
    api_call_index    INTEGER,
    timestamp         TEXT,
    prompt_preview    TEXT,
    prompt_length     INTEGER DEFAULT 0,
    has_tool_results  INTEGER DEFAULT 0,
    tool_result_count INTEGER DEFAULT 0,
    token_delta       INTEGER DEFAULT 0,
    cumulative_input  INTEGER DEFAULT 0,
    is_first          INTEGER DEFAULT 0,
    FOREIGN KEY (session_id) REFERENCES sessions(id)
);

CREATE TABLE IF NOT EXISTS tool_calls (
    id                    INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id            TEXT,
    timestamp             TEXT,
    tool_name             TEXT,
    tool_input_json       TEXT,
    tool_response_preview TEXT,
    input_size            INTEGER DEFAULT 0,
    response_size         INTEGER DEFAULT 0,
    is_failure            INTEGER DEFAULT 0,
    error_class           TEXT,
    FOREIGN KEY (session_id) REFERENCES sessions(id)
);

CREATE TABLE IF NOT EXISTS raw_events (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    captured_at TEXT DEFAULT (datetime('now')),
    event_type  TEXT,
    session_id  TEXT,
    raw_data    TEXT
);
"""


def open_db() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(DB_PATH), timeout=10)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=5000")
    conn.executescript(SCHEMA)
    # Migrate existing DBs that predate the failure columns
    for ddl in [
        "ALTER TABLE tool_calls ADD COLUMN is_failure INTEGER DEFAULT 0",
        "ALTER TABLE tool_calls ADD COLUMN error_class TEXT",
    ]:
        try:
            conn.execute(ddl)
        except sqlite3.OperationalError:
            pass
    conn.commit()
    return conn


# ── Transcript parser ─────────────────────────────────────────────────────────

def parse_transcript(path: str):
    """
    Parse a JSONL transcript. Returns (api_calls, user_messages).

    api_calls:     one record per assistant turn with token usage
    user_messages: one record per user turn with token delta = how much this
                   turn added to the context window
    """
    entries = []
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    entries.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
    except Exception:
        return [], []

    api_calls     = []
    user_messages = []

    prev_input    = 0
    api_call_idx  = 0
    msg_idx       = 0
    pending_user  = None  # last user message waiting for its paired assistant turn

    for entry in entries:
        etype = entry.get("type")

        # ── User turn ────────────────────────────────────────────────────────
        if etype == "user":
            msg     = entry.get("message", {})
            content = msg.get("content", [])
            if isinstance(content, str):
                content = [{"type": "text", "text": content}]

            text_parts        = []
            has_tool_results  = False
            tool_result_count = 0

            for block in (content if isinstance(content, list) else []):
                if not isinstance(block, dict):
                    continue
                btype = block.get("type", "")

                if btype == "text":
                    text_parts.append(block.get("text", ""))

                elif btype == "tool_result":
                    has_tool_results = True
                    tool_result_count += 1
                    inner = block.get("content", [])
                    if isinstance(inner, str):
                        text_parts.append(f"[tool_result: {inner[:200]}]")
                    elif isinstance(inner, list):
                        for ib in inner:
                            if isinstance(ib, dict) and ib.get("type") == "text":
                                text_parts.append(f"[tool_result: {ib.get('text','')[:200]}]")

            full_text = "\n".join(text_parts)

            pending_user = {
                "msg_index":        msg_idx,
                "api_call_index":   None,
                "timestamp":        entry.get("timestamp", ""),
                "prompt_preview":   full_text[:600],
                "prompt_length":    len(full_text),
                "has_tool_results": has_tool_results,
                "tool_result_count":tool_result_count,
                "token_delta":      0,
                "cumulative_input": 0,
                "is_first":         msg_idx == 0,
            }
            user_messages.append(pending_user)
            msg_idx += 1

        # ── Assistant turn ────────────────────────────────────────────────────
        elif etype == "assistant":
            msg   = entry.get("message", {})
            usage = msg.get("usage", {})
            if not usage:
                continue

            input_tokens  = int(usage.get("input_tokens", 0))
            output_tokens = int(usage.get("output_tokens", 0))
            cache_create  = int(usage.get("cache_creation_input_tokens", 0))
            cache_read    = int(usage.get("cache_read_input_tokens", 0))
            token_delta   = input_tokens - prev_input
            prev_input    = input_tokens

            if pending_user is not None:
                pending_user["api_call_index"]  = api_call_idx
                pending_user["token_delta"]     = token_delta
                pending_user["cumulative_input"]= input_tokens
                pending_user = None

            api_calls.append({
                "timestamp":    entry.get("timestamp", ""),
                "model":        msg.get("model", ""),
                "stop_reason":  msg.get("stop_reason", ""),
                "input_tokens": input_tokens,
                "output_tokens":output_tokens,
                "cache_creation": cache_create,
                "cache_read":   cache_read,
            })
            api_call_idx += 1

    return api_calls, user_messages


# ── Event handlers ────────────────────────────────────────────────────────────

def handle_stop(event: dict, conn: sqlite3.Connection):
    session_id      = event.get("session_id", "unknown")
    transcript_path = event.get("transcript_path", "")

    api_calls, user_messages = parse_transcript(transcript_path) if transcript_path else ([], [])

    total_input        = sum(c["input_tokens"]   for c in api_calls)
    total_output       = sum(c["output_tokens"]  for c in api_calls)
    total_cache_create = sum(c["cache_creation"] for c in api_calls)
    total_cache_read   = sum(c["cache_read"]     for c in api_calls)
    model              = next((c["model"] for c in reversed(api_calls) if c.get("model")), "")
    total_cost         = sum(
        calc_cost(c["input_tokens"], c["output_tokens"],
                  c["cache_creation"], c["cache_read"], c["model"])
        for c in api_calls
    )

    project_path = ""
    if transcript_path:
        try:
            parts = Path(transcript_path).parts
            if "projects" in parts:
                idx = list(parts).index("projects")
                project_path = parts[idx + 1] if idx + 1 < len(parts) else ""
        except Exception:
            pass

    now_iso    = datetime.now(timezone.utc).isoformat()
    started_at = api_calls[0]["timestamp"]  if api_calls else now_iso
    ended_at   = api_calls[-1]["timestamp"] if api_calls else now_iso

    tool_count = conn.execute(
        "SELECT COUNT(*) FROM tool_calls WHERE session_id = ?", (session_id,)
    ).fetchone()[0]

    # Clear stale per-session rows so re-processing is idempotent
    conn.execute("DELETE FROM api_calls     WHERE session_id = ?", (session_id,))
    conn.execute("DELETE FROM user_messages WHERE session_id = ?", (session_id,))

    conn.execute("""
        INSERT OR REPLACE INTO sessions
        (id, project_path, started_at, ended_at,
         total_input_tokens, total_output_tokens,
         total_cache_creation_tokens, total_cache_read_tokens,
         total_api_calls, total_tool_calls,
         model, estimated_cost_usd, transcript_path)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
    """, (session_id, project_path, started_at, ended_at,
          total_input, total_output, total_cache_create, total_cache_read,
          len(api_calls), tool_count, model, total_cost, transcript_path))

    for i, c in enumerate(api_calls):
        cost = calc_cost(c["input_tokens"], c["output_tokens"],
                         c["cache_creation"], c["cache_read"], c["model"])
        conn.execute("""
            INSERT INTO api_calls
            (session_id, call_index, timestamp,
             input_tokens, output_tokens, cache_creation_tokens, cache_read_tokens,
             model, stop_reason, estimated_cost_usd)
            VALUES (?,?,?,?,?,?,?,?,?,?)
        """, (session_id, i, c["timestamp"],
              c["input_tokens"], c["output_tokens"], c["cache_creation"], c["cache_read"],
              c["model"], c["stop_reason"], cost))

    for um in user_messages:
        conn.execute("""
            INSERT INTO user_messages
            (session_id, msg_index, api_call_index, timestamp,
             prompt_preview, prompt_length,
             has_tool_results, tool_result_count,
             token_delta, cumulative_input, is_first)
            VALUES (?,?,?,?,?,?,?,?,?,?,?)
        """, (session_id, um["msg_index"], um["api_call_index"], um["timestamp"],
              um["prompt_preview"], um["prompt_length"],
              int(um["has_tool_results"]), um["tool_result_count"],
              um["token_delta"], um["cumulative_input"], int(um["is_first"])))

    conn.commit()


def handle_post_tool(event: dict, conn: sqlite3.Connection):
    session_id    = event.get("session_id", "unknown")
    tool_name     = event.get("tool_name", "")
    tool_input    = event.get("tool_input", {})
    tool_response = event.get("tool_response", "")

    input_json   = json.dumps(tool_input) if isinstance(tool_input, dict) else str(tool_input)
    response_str = str(tool_response) if tool_response else ""

    error_class = detect_failure(response_str[:2000], tool_name)
    is_failure  = 1 if error_class else 0

    conn.execute("""
        INSERT INTO tool_calls
        (session_id, timestamp, tool_name, tool_input_json, tool_response_preview,
         input_size, response_size, is_failure, error_class)
        VALUES (?,?,?,?,?,?,?,?,?)
    """, (session_id,
          datetime.now(timezone.utc).isoformat(),
          tool_name, input_json[:2000], response_str[:1000],
          len(input_json), len(response_str), is_failure, error_class))
    conn.commit()


# ── Main ──────────────────────────────────────────────────────────────────────

def log_error(msg: str):
    try:
        ERROR_LOG.parent.mkdir(parents=True, exist_ok=True)
        with open(ERROR_LOG, "a") as f:
            f.write(f"{datetime.now().isoformat()} {msg}\n")
    except Exception:
        pass


def main():
    event_type = sys.argv[1] if len(sys.argv) > 1 else "unknown"
    raw = sys.stdin.read()

    try:
        conn = open_db()
    except Exception as e:
        log_error(f"DB open failed: {e}")
        sys.exit(0)

    try:
        event      = json.loads(raw) if raw.strip() else {}
        session_id = event.get("session_id", "")

        conn.execute(
            "INSERT INTO raw_events (event_type, session_id, raw_data) VALUES (?,?,?)",
            (event_type, session_id, raw[:10000])
        )
        conn.commit()

        if event_type == "stop":
            handle_stop(event, conn)
        elif event_type == "post_tool":
            handle_post_tool(event, conn)

    except Exception as e:
        log_error(f"[{event_type}] {e} | raw={raw[:200]}")
    finally:
        conn.close()

    sys.exit(0)


if __name__ == "__main__":
    main()
