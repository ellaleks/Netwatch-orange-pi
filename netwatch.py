import argparse
import os
import re
import sqlite3
import subprocess
import time
from concurrent.futures import ThreadPoolExecutor
from uuid import uuid4

from netwatch_config import EXTERNAL_TARGETS, network_settings

DB = "/var/lib/netwatch/netwatch.sqlite3"
INTERFACE, GATEWAY = network_settings()
TARGETS = (GATEWAY, *EXTERNAL_TARGETS)
INTERVAL = 5
RTT = re.compile(r"rtt min/avg/max/mdev = [\d.]+/([\d.]+)/")


def probe(target):
    try:
        result = subprocess.run(
            ["/usr/bin/ping", "-4", "-n", "-q", "-I", INTERFACE,
             "-c", "1", "-W", "2", "-w", "3", target],
            capture_output=True, text=True, timeout=4,
            env={**os.environ, "LC_ALL": "C"},
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return target, "error", None, str(exc)[:300]

    if result.returncode == 1:
        return target, "no_reply", None, None
    if result.returncode != 0:
        detail = result.stderr.strip() or result.stdout.strip()
        return target, "error", None, f"exit={result.returncode}: {detail}"[:300]

    match = RTT.search(result.stdout)
    if match is None:
        return target, "error", None, "Cannot read RTT from ping output"
    return target, "ok", float(match.group(1)), None


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--once", action="store_true")
    args = parser.parse_args()
    os.umask(0o027)
    run_id = uuid4().hex
    cycle = 0

    with sqlite3.connect(DB, timeout=10) as db, ThreadPoolExecutor(3) as pool:
        db.execute("PRAGMA journal_mode=WAL").fetchone()
        db.execute("PRAGMA synchronous=FULL")
        db.execute("""
            CREATE TABLE IF NOT EXISTS probes (
                run_id TEXT NOT NULL,
                cycle INTEGER NOT NULL,
                ts_ms INTEGER NOT NULL,
                target TEXT NOT NULL,
                status TEXT NOT NULL CHECK (status IN ('ok', 'no_reply', 'error')),
                rtt_ms REAL,
                detail TEXT,
                PRIMARY KEY (run_id, cycle, target)
            )
        """)
        db.execute("CREATE INDEX IF NOT EXISTS probes_target_time ON probes(target, ts_ms)")
        db.execute("CREATE INDEX IF NOT EXISTS probes_time ON probes(ts_ms)")

        while True:
            started = time.monotonic()
            ts_ms = time.time_ns() // 1_000_000
            rows = [(run_id, cycle, ts_ms, *result)
                    for result in pool.map(probe, TARGETS)]
            with db:
                db.executemany(
                    "INSERT INTO probes VALUES (?, ?, ?, ?, ?, ?, ?)", rows
                )
            if args.once:
                print(f"Saved {len(rows)} probes to {DB}")
                return
            cycle += 1
            time.sleep(max(0, INTERVAL - (time.monotonic() - started)))


if __name__ == "__main__":
    main()
