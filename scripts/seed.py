#!/usr/bin/env python3
"""
Backfill existing Claude Code session transcripts into the observability DB.
Run once after install to see historical data immediately.
"""

import sys
from pathlib import Path

# Add hooks dir to path
sys.path.insert(0, str(Path(__file__).parent.parent / "hooks"))

from capture import open_db, handle_stop

def seed():
    projects_dir = Path.home() / ".claude" / "projects"
    if not projects_dir.exists():
        print(f"No projects directory found at {projects_dir}")
        print("Run at least one Claude Code session first.")
        return

    conn = open_db()
    imported = 0
    skipped  = 0

    force = "--force" in sys.argv

    for transcript in sorted(projects_dir.rglob("*.jsonl")):
        session_id = transcript.stem
        has_msgs = conn.execute(
            "SELECT COUNT(*) FROM user_messages WHERE session_id=?", (session_id,)
        ).fetchone()[0]
        existing = conn.execute(
            "SELECT id FROM sessions WHERE id=?", (session_id,)
        ).fetchone()
        if existing and has_msgs and not force:
            skipped += 1
            continue

        event = {"session_id": session_id, "transcript_path": str(transcript)}
        try:
            handle_stop(event, conn)
            print(f"  ✓ {transcript.parent.name[:20]}/{transcript.name}")
            imported += 1
        except Exception as e:
            print(f"  ✗ {transcript.name}: {e}")

    conn.close()
    print(f"\nDone. Imported {imported} sessions, skipped {skipped} already in DB.")

if __name__ == "__main__":
    seed()
