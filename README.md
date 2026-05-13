# Claude Code Observability

A self-hosted observability layer for Claude Code sessions — tracks every API call, token count, tool use, and cost across all your sessions, then surfaces optimization opportunities.

## What it does

- **Captures** every Claude Code session via hooks: input/output/cache tokens per API call, tool calls, timing
- **Stores** everything in a local SQLite database
- **Visualizes** token usage trends, cost breakdown, cache efficiency, tool usage patterns
- **Analyzes** sessions for inefficiencies and gives concrete improvement suggestions with estimated savings

## Setup

```bash
cd ~/claude-observability

# 1. Create venv and install dependencies
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

# 2. Install hooks into Claude Code (~/.claude/settings.json)
python install.py

# 3. Start the dashboard
./run.sh
```

Dashboard opens at: **http://localhost:7842**

## How it works

`install.py` adds two hooks to `~/.claude/settings.json`:

- **Stop hook** — fires when any Claude Code session ends, parses the session transcript to extract per-call token usage, stores in DB
- **PostToolUse hook** — fires after every tool call, logs tool name and response size

The hook script (`hooks/capture.py`) uses only Python stdlib — no venv required for data capture.

## Running the dashboard

```bash
./run.sh
```

Or manually:

```bash
source .venv/bin/activate
uvicorn api.server:app --host 0.0.0.0 --port 7842 --reload
```

## CLI flags (uvicorn)

| Flag | Default | Description |
|------|---------|-------------|
| `--port` | 7842 | Dashboard port |
| `--host` | 0.0.0.0 | Bind address |
| `--reload` | off | Auto-reload on code change |

## API Cost reference (as of May 2026)

| Model | Input | Output | Cache Write | Cache Read |
|-------|-------|--------|-------------|------------|
| claude-opus-4-7 | $15/MTok | $75/MTok | $18.75/MTok | $1.50/MTok |
| claude-sonnet-4-6 | $3/MTok | $15/MTok | $3.75/MTok | $0.30/MTok |
| claude-haiku-4-5 | $0.80/MTok | $4/MTok | $1.00/MTok | $0.08/MTok |

## File structure

```
claude-observability/
├── README.md
├── requirements.txt
├── install.py               # Hook installer
├── run.sh                   # Start dashboard
├── hooks/
│   └── capture.py           # Hook script (stdlib only, no venv needed)
├── api/
│   └── server.py            # FastAPI server
├── templates/
│   └── index.html           # Dashboard UI (Chart.js)
├── analysis/
│   └── analyzer.py          # Pattern detection + suggestions
└── data/
    └── observability.db     # SQLite database (auto-created)
```

## Data captured

- Session ID, project path, start/end time
- Per-API-call: input tokens, output tokens, cache creation tokens, cache read tokens, model, stop reason
- Per-tool-call: tool name, input size, response size, timestamp
- Raw hook events (for debugging)

## Uninstall hooks

Edit `~/.claude/settings.json` and remove entries containing `capture.py`.
