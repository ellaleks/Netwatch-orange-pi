import argparse
import os
import sqlite3
import tempfile
import time
from contextlib import redirect_stdout
from datetime import datetime
from itertools import groupby
from pathlib import Path
from zoneinfo import ZoneInfo

from netwatch_config import EXTERNAL_TARGETS, network_settings

DB = 'file:/var/lib/netwatch/netwatch.sqlite3?mode=ro'
REPORT_DIR = Path('/var/lib/netwatch/reports')
_, GATEWAY = network_settings()
TARGETS = (GATEWAY, *EXTERNAL_TARGETS)
ZONE = ZoneInfo('Europe/Brussels')
GAP_MS = 8_000


def stamp(ms):
    return datetime.fromtimestamp(ms / 1000, ZONE).isoformat(timespec='seconds')


def tables_in(db):
    return {name for name, in db.execute(
        "SELECT name FROM sqlite_master WHERE type = 'table'"
    )}


def report(db, start, end):
    counts = {target: dict.fromkeys(('ok', 'no_reply', 'error'), 0)
              for target in TARGETS}
    tables = tables_in(db)
    rows = ()
    if 'probes' in tables:
        rows = db.execute('''
            SELECT run_id, cycle, ts_ms, target, status
            FROM probes WHERE ts_ms BETWEEN ? AND ? ORDER BY rowid
        ''', (start, end))

    previous = None
    active = None
    events = []
    for (run, cycle, ts), probes in groupby(rows, key=lambda row: row[:3]):
        statuses = {target: status for _, _, _, target, status in probes}
        gateway_targets = statuses.keys() - set(EXTERNAL_TARGETS)
        gateway_target = next(iter(gateway_targets)) if len(gateway_targets) == 1 else GATEWAY
        for target in (gateway_target, *EXTERNAL_TARGETS):
            totals = counts.setdefault(target, dict.fromkeys(('ok', 'no_reply', 'error'), 0))
            totals[statuses.get(target, 'error')] += 1

        if previous is None:
            if ts - start > GAP_MS:
                events.append(f'{stamp(start)} .. {stamp(ts)}  no_samples')
        else:
            prev_run, prev_cycle, prev_ts = previous
            if (run != prev_run or cycle != prev_cycle + 1
                    or not 0 < ts - prev_ts <= GAP_MS):
                if active is not None:
                    label, first = active
                    events.append(f'{stamp(first)} .. {stamp(prev_ts)}  {label}  last_seen')
                    active = None
                events.append(f'{stamp(prev_ts)} .. {stamp(ts)}  sampling_break')

        gateway, cf, google = (statuses.get(target, 'error')
                               for target in (gateway_target, *EXTERNAL_TARGETS))
        label = None
        if 'error' in (gateway, cf, google):
            label = 'probe_error'
        elif cf == google == 'no_reply':
            label = 'external_down' if gateway == 'ok' else 'all_down'

        if active is not None and label != active[0]:
            old_label, first = active
            seconds = (ts - first) / 1000
            events.append(f'{stamp(first)} .. {stamp(ts)}  {old_label}  ~{seconds:.0f}s')
            active = None
        if label is not None and active is None:
            active = (label, ts)
        previous = (run, cycle, ts)

    print(f'Period: {stamp(start)} .. {stamp(end)}')
    print('Target          OK  No reply  Errors   ICMP loss')
    for target, totals in counts.items():
        ok, lost, errors = (totals[key] for key in ('ok', 'no_reply', 'error'))
        loss = f'{100 * lost / (ok + lost):.2f}%' if ok + lost else 'n/a'
        print(f'{target:13} {ok:5} {lost:9} {errors:7} {loss:>11}')

    if previous is None:
        events.append(f'{stamp(start)} .. {stamp(end)}  no_samples')
    else:
        last = previous[2]
        if active is not None:
            label, first = active
            events.append(f'{stamp(first)} .. {stamp(last)}  {label}  last_seen')
        if end - last > GAP_MS:
            events.append(f'{stamp(last)} .. {stamp(end)}  no_recent_samples')
    print('\nEvents:')
    print('\n'.join(events) if events else 'none')

    print('\nSpeed tests (HTTPS, Cloudflare):')
    service_result = os.environ.get('SERVICE_RESULT')
    if service_result and service_result != 'success':
        print(f'Latest speed service result: {service_result}')
    speed_rows = ()
    if 'speed_tests' in tables:
        speed_rows = db.execute('''
            SELECT started_ms, finished_ms, direction, bytes, mbps, detail
            FROM speed_tests
            WHERE finished_ms BETWEEN ? AND ?
            ORDER BY finished_ms, rowid
        ''', (start, end)).fetchall()
    if not speed_rows:
        print('No speed measurements in this period.')
    for started, finished, direction, size, mbps, detail in speed_rows:
        result = f'{mbps:.2f} Mbit/s ({size} bytes)' if mbps is not None else (
            f'error: {detail or "no result"}'
        )
        print(f'{stamp(started)} .. {stamp(finished)}  {direction}: {result}')


def save_report(db, start, end):
    REPORT_DIR.mkdir(mode=0o750, exist_ok=True)
    temporary = tempfile.NamedTemporaryFile(
        mode='w', encoding='utf-8', dir=REPORT_DIR, prefix='.netwatch-', delete=False
    )
    temporary_path = Path(temporary.name)
    try:
        with temporary, redirect_stdout(temporary):
            report(db, start, end)
        created = datetime.now(ZONE).strftime('%Y-%m-%d_%H-%M-%S%z')
        final_path = REPORT_DIR / f'netwatch-{created}.txt'
        temporary_path.replace(final_path)
    finally:
        temporary_path.unlink(missing_ok=True)
    print(f'Report saved: {final_path}')


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--hours', type=int, default=24)
    parser.add_argument('--save', action='store_true',
                        help='Save a dated report to /var/lib/netwatch/reports')
    args = parser.parse_args()
    if args.hours < 1:
        parser.error('--hours must be at least 1')
    end = time.time_ns() // 1_000_000
    with sqlite3.connect(DB, uri=True) as db:
        db.execute('BEGIN')
        start = end - args.hours * 3_600_000
        if args.save:
            save_report(db, start, end)
        else:
            report(db, start, end)


if __name__ == '__main__':
    main()
