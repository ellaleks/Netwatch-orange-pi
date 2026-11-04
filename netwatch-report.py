import argparse
import sqlite3
import time
from datetime import datetime
from itertools import groupby
from zoneinfo import ZoneInfo

DB = 'file:/var/lib/netwatch/netwatch.sqlite3?mode=ro'
TARGETS = ('192.168.0.1', '1.1.1.1', '8.8.8.8')
ZONE = ZoneInfo('Europe/Brussels')
GAP_MS = 8_000


def stamp(ms):
    return datetime.fromtimestamp(ms / 1000, ZONE).isoformat(timespec='seconds')


def report(db, start, end):
    counts = {target: dict.fromkeys(('ok', 'no_reply', 'error'), 0)
              for target in TARGETS}
    rows = db.execute('''
        SELECT run_id, cycle, ts_ms, target, status
        FROM probes WHERE ts_ms BETWEEN ? AND ? ORDER BY rowid
    ''', (start, end))

    previous = None
    active = None
    events = []
    for (run, cycle, ts), probes in groupby(rows, key=lambda row: row[:3]):
        statuses = {target: status for _, _, _, target, status in probes}
        for target in TARGETS:
            counts[target][statuses.get(target, 'error')] += 1

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

        gateway, cf, google = (statuses.get(target, 'error') for target in TARGETS)
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


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--hours', type=int, default=24)
    args = parser.parse_args()
    if args.hours < 1:
        parser.error('--hours must be at least 1')
    end = time.time_ns() // 1_000_000
    with sqlite3.connect(DB, uri=True) as db:
        report(db, end - args.hours * 3_600_000, end)


if __name__ == '__main__':
    main()
