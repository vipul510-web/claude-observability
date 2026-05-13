#!/bin/bash
set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

# Create venv if not present
if [ ! -d ".venv" ]; then
  echo "Creating virtual environment..."
  python3 -m venv .venv
fi

source .venv/bin/activate

echo "Installing / updating dependencies..."
pip install -q -r requirements.txt

mkdir -p data

PORT="${PORT:-7842}"

echo ""
echo "  Claude Code Observability Dashboard"
echo "  ─────────────────────────────────────"
echo "  http://localhost:${PORT}"
echo "  Press Ctrl+C to stop"
echo ""

uvicorn api.server:app --host 0.0.0.0 --port "$PORT" --reload
