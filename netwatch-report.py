import argparse
import os
import sqlite3
import time
from contextlib import closing
from datetime import datetime

from netwatch_analysis import GAP_MS, OutageAnalysis, consecutive, probe_cycles, probe_statuses
from netwatch_config import EXTERNAL_TARGETS, network_settings
from netwatch_summary import ZONE, maintain_reports, tables_in, write_report
from netwatch_telegram import deliver_report, deliver_summaries

DB = 'file:/var/lib/netwatch/netwatch.sqlite3?mode=ro'
_, GATEWAY = network_settings()
TARGETS = (GATEWAY, *EXTERNAL_TARGETS)


def stamp(ms):
    return datetime.fromtimestamp(ms / 1000, ZONE).isoformat(timespec='seconds')


def report(db, start, end):
    counts = {target: dict.fromkeys(('ok', 'no_reply', 'error'), 0)
              for target in TARGETS}
    tables = tables_in(db)
    cycles = probe_cycles(db, start, end) if 'probes' in tables else ()
    analysis = OutageAnalysis()

    previous = None
    active = None
    events = []
    for sample, statuses in cycles:
        ts = sample[2]
        gateway_target, gateway, cf, google = probe_statuses(statuses, GATEWAY)
        analysis.observe(sample, gateway, cf, google)
        for target in (gateway_target, *EXTERNAL_TARGETS):
            totals = counts.setdefault(target, dict.fromkeys(('ok', 'no_reply', 'error'), 0))
            totals[statuses.get(target, 'error')] += 1

        if previous is None:
            if ts - start > GAP_MS:
                events.append(f'{stamp(start)} .. {stamp(ts)}  no_samples')
        else:
            prev_ts = previous[2]
            if not consecutive(previous, sample):
                if active is not None:
                    label, first = active
                    events.append(f'{stamp(first)} .. {stamp(prev_ts)}  {label}  last_seen')
                    active = None
                events.append(f'{stamp(prev_ts)} .. {stamp(ts)}  sampling_break')

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
        previous = sample

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
    analysis.print_summary()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--hours', type=int, default=24)
    parser.add_argument('--save', action='store_true',
                        help='Save a dated report to /var/lib/netwatch/reports')
    parser.add_argument('--maintenance', action='store_true',
                        help='Create calendar summaries and remove expired reports')
    args = parser.parse_args()
    if args.hours < 1:
        parser.error('--hours must be at least 1')
    end = time.time_ns() // 1_000_000
    saved = None
    if args.save or not args.maintenance:
        with closing(sqlite3.connect(DB, uri=True)) as db, db:
            db.execute('BEGIN')
            start = end - args.hours * 3_600_000
            if args.save:
                saved = write_report(lambda: report(db, start, end))
            else:
                report(db, start, end)
    if saved is not None:
        deliver_report(saved)
    if args.maintenance:
        with closing(sqlite3.connect(DB, uri=True)) as db, db:
            db.execute('BEGIN')
            summaries = maintain_reports(db, now=datetime.fromtimestamp(end / 1000, ZONE))
        deliver_summaries(summaries)


if __name__ == '__main__':
    main()
