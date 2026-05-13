#!/usr/bin/env python3
"""
Claude Code hook script — captures session data to SQLite.
Uses ONLY stdlib so it works with any Python 3.8+ without a venv.

Called by Claude Code hooks as:
  python capture.py stop       (Stop hook)
  python capture.py post_tool  (PostToolUse hook)
  python capture.py pre_tool   (PreToolUse hook)
"""

import sys
import json
import sqlite3
import os
from pathlib import Path
from datetime import datetime, timezone

DB_PATH = Path(__file__).parent.parent / "data" / "observability.db"
ERROR_LOG = Path(__file__).parent.parent / "data" / "errors.log"

# ── Pricing table (per token) ────────────────────────────────────────────────

PRICING = {
    "claude-opus-4-7":          {"input": 15.0/1e6,  "output": 75.0/1e6,  "cache_write": 18.75/1e6, "cache_read": 1.50/1e6},
    "claude-opus-4-6":          {"input": 15.0/1e6,  "output": 75.0/1e6,  "cache_write": 18.75/1e6, "cache_read": 1.50/1e6},
    "claude-sonnet-4-6":        {"input": 3.0/1e6,   "output": 15.0/1e6,  "cache_write": 3.75/1e6,  "cache_read": 0.30/1e6},
    "claude-sonnet-4-5":        {"input": 3.0/1e6,   "output": 15.0/1e6,  "cache_write": 3.75/1e6,  "cache_read": 0.30/1e6},
    "claude-haiku-4-5":         {"input": 0.80/1e6,  "output": 4.0/1e6,   "cache_write": 1.00/1e6,  "cache_read": 0.08/1e6},
    "claude-haiku-4-5-20251001":{"input": 0.80/1e6,  "output": 4.0/1e6,   "cache_write": 1.00/1e6,  "cache_read": 0.08/1e6},
}
DEFAULT_PRICING = {"input": 3.0/1e6, "output": 15.0/1e6, "cache_write": 3.75/1e6, "cache_read": 0.30/1e6}


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
        input_tokens     * p["input"] +
        output_tokens    * p["output"] +
        cache_creation   * p["cache_write"] +
        cache_read       * p["cache_read"]
    )


# ── Database setup ────────────────────────────────────────────────────────────

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

CREATE TABLE IF NOT EXISTS tool_calls (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id          TEXT,
    timestamp           TEXT,
    tool_name           TEXT,
    tool_input_json     TEXT,
    tool_response_preview TEXT,
    input_size          INTEGER DEFAULT 0,
    response_size       INTEGER DEFAULT 0,
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
    conn.commit()
    return conn


# ── Transcript parser ─────────────────────────────────────────────────────────

def parse_transcript(path: str) -> list:
    """Parse a .jsonl transcript file and return a list of API call records."""
    calls = []
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    entry = json.loads(line)
                except json.JSONDecodeError:
                    continue

                if entry.get("type") != "assistant":
                    continue

                msg = entry.get("message", {})
                usage = msg.get("usage", {})
                if not usage:
                    continue

                calls.append({
                    "timestamp":          entry.get("timestamp", ""),
                    "model":              msg.get("model", ""),
                    "stop_reason":        msg.get("stop_reason", ""),
                    "input_tokens":       int(usage.get("input_tokens", 0)),
                    "output_tokens":      int(usage.get("output_tokens", 0)),
                    "cache_creation":     int(usage.get("cache_creation_input_tokens", 0)),
                    "cache_read":         int(usage.get("cache_read_input_tokens", 0)),
                })
    except Exception:
        pass
    return calls


# ── Event handlers ────────────────────────────────────────────────────────────

def handle_stop(event: dict, conn: sqlite3.Connection):
    session_id      = event.get("session_id", "unknown")
    transcript_path = event.get("transcript_path", "")

    api_calls = parse_transcript(transcript_path) if transcript_path else []

    total_input         = sum(c["input_tokens"]   for c in api_calls)
    total_output        = sum(c["output_tokens"]  for c in api_calls)
    total_cache_create  = sum(c["cache_creation"] for c in api_calls)
    total_cache_read    = sum(c["cache_read"]     for c in api_calls)

    model = next((c["model"] for c in reversed(api_calls) if c.get("model")), "")

    total_cost = sum(
        calc_cost(c["input_tokens"], c["output_tokens"],
                  c["cache_creation"], c["cache_read"], c["model"])
        for c in api_calls
    )

    # Derive project path from transcript path
    project_path = ""
    if transcript_path:
        try:
            parts = Path(transcript_path).parts
            if "projects" in parts:
                idx = list(parts).index("projects")
                project_path = parts[idx + 1] if idx + 1 < len(parts) else ""
        except Exception:
            pass

    now_iso = datetime.now(timezone.utc).isoformat()
    started_at = api_calls[0]["timestamp"]  if api_calls else now_iso
    ended_at   = api_calls[-1]["timestamp"] if api_calls else now_iso

    # Existing tool call count (captured by PostToolUse hooks)
    tool_count = conn.execute(
        "SELECT COUNT(*) FROM tool_calls WHERE session_id = ?", (session_id,)
    ).fetchone()[0]

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

    # Insert per-call records (IGNORE duplicates on re-run)
    for i, c in enumerate(api_calls):
        cost = calc_cost(c["input_tokens"], c["output_tokens"],
                         c["cache_creation"], c["cache_read"], c["model"])
        conn.execute("""
            INSERT OR IGNORE INTO api_calls
            (session_id, call_index, timestamp,
             input_tokens, output_tokens, cache_creation_tokens, cache_read_tokens,
             model, stop_reason, estimated_cost_usd)
            VALUES (?,?,?,?,?,?,?,?,?,?)
        """, (session_id, i, c["timestamp"],
              c["input_tokens"], c["output_tokens"], c["cache_creation"], c["cache_read"],
              c["model"], c["stop_reason"], cost))

    conn.commit()


def handle_post_tool(event: dict, conn: sqlite3.Connection):
    session_id    = event.get("session_id", "unknown")
    tool_name     = event.get("tool_name", "")
    tool_input    = event.get("tool_input", {})
    tool_response = event.get("tool_response", "")

    input_json   = json.dumps(tool_input) if isinstance(tool_input, dict) else str(tool_input)
    response_str = str(tool_response) if tool_response else ""

    conn.execute("""
        INSERT INTO tool_calls
        (session_id, timestamp, tool_name, tool_input_json, tool_response_preview,
         input_size, response_size)
        VALUES (?,?,?,?,?,?,?)
    """, (session_id,
          datetime.now(timezone.utc).isoformat(),
          tool_name,
          input_json[:2000],
          response_str[:1000],
          len(input_json),
          len(response_str)))
    conn.commit()


def handle_pre_tool(event: dict, conn: sqlite3.Connection):
    # Store start timestamp keyed by session for timing (best-effort)
    # We just log the raw event; timing is approximated via response size
    pass


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
        sys.exit(0)  # Never block Claude Code

    try:
        event = json.loads(raw) if raw.strip() else {}
        session_id = event.get("session_id", "")

        # Always log raw event
        conn.execute(
            "INSERT INTO raw_events (event_type, session_id, raw_data) VALUES (?,?,?)",
            (event_type, session_id, raw[:10000])
        )
        conn.commit()

        if event_type == "stop":
            handle_stop(event, conn)
        elif event_type == "post_tool":
            handle_post_tool(event, conn)
        elif event_type == "pre_tool":
            handle_pre_tool(event, conn)

    except Exception as e:
        log_error(f"[{event_type}] {e} | raw={raw[:200]}")
    finally:
        conn.close()

    sys.exit(0)  # Always exit 0 so Claude Code continues normally


if __name__ == "__main__":
    main()
