import json
import sqlite3
import subprocess
import time

from netwatch_config import network_settings

DB = "/var/lib/netwatch/netwatch.sqlite3"
BASE = "https://speed.cloudflare.com"
INTERFACE, _ = network_settings()
CURL = [
    "/usr/bin/curl", "-q", "-4", "--interface", f"if!{INTERFACE}", "--noproxy", "*",
    "--silent", "--show-error", "--fail", "--connect-timeout", "5",
    "--max-time", "45", "--output", "/dev/null", "--write-out", "%{json}",
]


def measure(direction, size, url, options, payload=None):
    started = time.time_ns() // 1_000_000
    received = None
    mbps = None
    detail = None
    try:
        result = subprocess.run(
            CURL + options + [BASE + url], input=payload,
            capture_output=True, timeout=50,
        )
        if result.returncode:
            detail = f"curl {result.returncode}: {result.stderr.decode(errors='replace').strip()}"
        else:
            info = json.loads(result.stdout)
            received = int(info[f"size_{direction}"])
            if info["http_code"] != 200 or received != size:
                detail = f"HTTP {info['http_code']}, bytes {received}/{size}"
            else:
                mbps = info[f"speed_{direction}"] * 8 / 1_000_000
    except (OSError, subprocess.TimeoutExpired, ValueError, KeyError, TypeError) as exc:
        detail = str(exc)
    finished = time.time_ns() // 1_000_000
    return started, finished, direction, received, mbps, detail


def main():
    tests = [
        ("download", 10_000_000, "/__down?bytes=10000000",
         ["--max-filesize", "10000000"]),
        ("upload", 2_000_000, "/__up",
         ["--header", "Content-Type: application/octet-stream", "--data-binary", "@-"],
         bytes(2_000_000)),
    ]
    failed = False
    with sqlite3.connect(DB, timeout=10) as db:
        db.execute("""
            CREATE TABLE IF NOT EXISTS speed_tests (
                started_ms INTEGER NOT NULL,
                finished_ms INTEGER NOT NULL,
                direction TEXT NOT NULL,
                bytes INTEGER,
                mbps REAL,
                detail TEXT
            )
        """)
        for test in tests:
            row = measure(*test)
            with db:
                db.execute("INSERT INTO speed_tests VALUES (?, ?, ?, ?, ?, ?)", row)
            _, _, direction, _, mbps, detail = row
            if mbps is None:
                failed = True
                print(f"{direction}: error: {detail}", flush=True)
            else:
                print(f"{direction}: {mbps:.2f} Mbit/s", flush=True)
    return int(failed)


if __name__ == "__main__":
    raise SystemExit(main())
