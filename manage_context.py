#!/usr/bin/env python3
"""Manage weekly_context entries in training.db.

Usage:
  python3 manage_context.py add "note text" "YYYY-MM-DD"   # add entry
  python3 manage_context.py list                            # list active entries
  python3 manage_context.py clear ID                        # mark entry as promoted
"""

import json
import os
import sqlite3
import sys
import time

BASE_DIR = "/opt/claude-butler"
STATE_DIR = os.path.join(BASE_DIR, "state")
TRAINING_DB = os.path.join(STATE_DIR, "training.db")


def get_conn() -> sqlite3.Connection:
    os.makedirs(STATE_DIR, exist_ok=True)
    conn = sqlite3.connect(TRAINING_DB)
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS activities (
            activity_id TEXT PRIMARY KEY,
            date TEXT,
            name TEXT,
            activity_type TEXT,
            duration_seconds REAL,
            distance_meters REAL,
            avg_heart_rate INTEGER,
            max_heart_rate INTEGER,
            calories INTEGER,
            training_load REAL,
            training_effect_aerobic REAL,
            training_effect_label TEXT,
            rpe INTEGER,
            splits_json TEXT,
            hr_zones_json TEXT,
            analysis TEXT,
            analyzed_at TEXT,
            raw_json TEXT
        );

        CREATE TABLE IF NOT EXISTS weekly_context (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            note TEXT,
            created_at TEXT,
            expires_at TEXT,
            promoted INTEGER DEFAULT 0
        );
    """)
    conn.commit()
    return conn


def cmd_add(note: str, expires_at: str) -> None:
    conn = get_conn()
    created_at = time.strftime("%Y-%m-%d")
    conn.execute(
        "INSERT INTO weekly_context (note, created_at, expires_at, promoted) VALUES (?, ?, ?, 0)",
        (note, created_at, expires_at),
    )
    conn.commit()
    row_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
    conn.close()
    print(f"Added weekly_context id={row_id}: '{note}' (expires {expires_at})")


def cmd_list() -> None:
    conn = get_conn()
    today = time.strftime("%Y-%m-%d")
    rows = conn.execute(
        "SELECT id, note, created_at, expires_at FROM weekly_context "
        "WHERE expires_at >= ? AND promoted = 0 ORDER BY created_at",
        (today,),
    ).fetchall()
    conn.close()

    if not rows:
        print("No active weekly context entries.")
        return

    print(f"Active weekly context entries (as of {today}):")
    for row in rows:
        row_id, note, created, expires = row
        print(f"  [{row_id}] {note}  (created: {created}, expires: {expires})")


def cmd_clear(entry_id: int) -> None:
    conn = get_conn()
    result = conn.execute(
        "UPDATE weekly_context SET promoted = 1 WHERE id = ?", (entry_id,)
    )
    conn.commit()
    conn.close()
    if result.rowcount:
        print(f"Marked weekly_context id={entry_id} as promoted (cleared).")
    else:
        print(f"No entry found with id={entry_id}.")


def main() -> None:
    args = sys.argv[1:]
    if not args:
        print(__doc__)
        sys.exit(1)

    subcmd = args[0].lower()

    if subcmd == "add":
        if len(args) < 3:
            print("Usage: manage_context.py add \"note\" \"YYYY-MM-DD\"")
            sys.exit(1)
        cmd_add(args[1], args[2])

    elif subcmd == "list":
        cmd_list()

    elif subcmd == "clear":
        if len(args) < 2:
            print("Usage: manage_context.py clear ID")
            sys.exit(1)
        try:
            cmd_clear(int(args[1]))
        except ValueError:
            print(f"Invalid ID: {args[1]}")
            sys.exit(1)

    else:
        print(f"Unknown command: {subcmd}")
        print(__doc__)
        sys.exit(1)


if __name__ == "__main__":
    main()
