"""Calendar summaries and retention for Netwatch text reports."""

import re
import tempfile
from contextlib import redirect_stdout
from datetime import date, datetime, time, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from netwatch_telegram import delivery_marker

ZONE = ZoneInfo('Europe/Brussels')
REPORT_DIR = Path('/var/lib/netwatch/reports')
STAMP = '%Y-%m-%d_%H-%M-%S%z'
RETENTION_SECONDS = 365 * 24 * 60 * 60
REPORT_NAME = re.compile(
    r'netwatch-(?:(?P<period>monthly-\d{4}-(?:0[1-9]|1[0-2])|'
    r'quarterly-\d{4}-Q[1-4]|yearly-\d{4})_created-)?'
    r'(?P<created>\d{4}-\d{2}-\d{2}_\d{2}-\d{2}-\d{2}[+-]\d{4})\.txt'
)


def tables_in(db):
    return {name for name, in db.execute(
        "SELECT name FROM sqlite_master WHERE type = 'table'"
    )}


def write_report(render, period=None, directory=REPORT_DIR):
    directory.mkdir(mode=0o750, exist_ok=True)
    temporary = tempfile.NamedTemporaryFile(
        mode='w', encoding='utf-8', dir=directory, prefix='.netwatch-', delete=False
    )
    path = Path(temporary.name)
    try:
        with temporary, redirect_stdout(temporary):
            render()
        created = datetime.now(ZONE).strftime(STAMP)
        prefix = f'{period}_created-' if period else ''
        final_path = directory / f'netwatch-{prefix}{created}.txt'
        path.replace(final_path)
    finally:
        path.unlink(missing_ok=True)
    print(f'Report saved: {final_path}')
    return final_path


def prune_reports(directory, now):
    cutoff = now.timestamp() - RETENTION_SECONDS
    existing_periods = {}
    if not directory.exists():
        return existing_periods
    for path in sorted(directory.iterdir()):
        match = REPORT_NAME.fullmatch(path.name)
        if match is None or path.is_symlink() or not path.is_file():
            continue
        try:
            created = datetime.strptime(match['created'], STAMP)
        except ValueError:
            continue
        if created.timestamp() < cutoff:
            path.unlink(missing_ok=True)
            if match['period']:
                delivery_marker(path).unlink(missing_ok=True)
            print(f'Expired report removed: {path.name}')
        elif match['period']:
            existing_periods[match['period']] = path
    return existing_periods


def shift_month(day, months):
    year, month = divmod(day.year * 12 + day.month - 1 + months, 12)
    return date(year, month + 1, 1)


def completed_periods(today):
    month_end = today.replace(day=1)
    month = shift_month(month_end, -1)
    days = (month_end - month).days
    yield f'monthly-{month:%Y-%m}', [
        (str(start), start, start + timedelta(days=1))
        for start in (month + timedelta(days=offset) for offset in range(days))
    ]

    quarter_end = date(today.year, 3 * ((today.month - 1) // 3) + 1, 1)
    quarter = shift_month(quarter_end, -3)
    yield f'quarterly-{quarter.year}-Q{(quarter.month - 1) // 3 + 1}', [
        (f'{start:%Y-%m}', start, shift_month(start, 1))
        for start in (shift_month(quarter, offset) for offset in range(3))
    ]

    year = today.year - 1
    yield f'yearly-{year}', [
        (f'{year}-Q{index + 1}', start, shift_month(start, 3))
        for index, start in enumerate(date(year, month, 1) for month in (1, 4, 7, 10))
    ]


def midnight_ms(day):
    return int(datetime.combine(day, time.min, tzinfo=ZONE).timestamp() * 1000)


def has_measurements(db, tables, targets, start, end):
    bounds = (midnight_ms(start), midnight_ms(end))
    for target in targets:
        if db.execute('''
            SELECT 1 FROM probes WHERE target = ? AND ts_ms >= ? AND ts_ms < ? LIMIT 1
        ''', (target, *bounds)).fetchone():
            return True
    return 'speed_tests' in tables and db.execute(
        'SELECT 1 FROM speed_tests WHERE finished_ms >= ? AND finished_ms < ? LIMIT 1', bounds
    ).fetchone() is not None


def render_summary(db, period, buckets, tables, targets):
    print(f'Netwatch calendar summary: {period}')
    print('Calendar timezone: Europe/Brussels')
    print('Loss = no reply / (OK + no reply). Errors are counted separately.')
    print('Speed averages use successful HTTPS transfers only.')
    for label, start, end in buckets:
        print(f'\n[{label}]')
        bounds = (midnight_ms(start), midnight_ms(end))
        ping_present = False
        for target in targets:
            total, ok, missed, errors, rtt = db.execute("""
                SELECT COUNT(*), SUM(status = 'ok'), SUM(status = 'no_reply'),
                       SUM(status = 'error'), AVG(CASE WHEN status = 'ok' THEN rtt_ms END)
                FROM probes
                WHERE target = ? AND ts_ms >= ? AND ts_ms < ?
            """, (target, *bounds)).fetchone()
            if not total:
                continue
            ping_present = True
            loss = f'{100 * missed / (ok + missed):.2f}%' if ok + missed else 'n/a'
            latency = f'{rtt:.2f} ms' if rtt is not None else 'n/a'
            print(f'  {target}: OK {ok}, no reply {missed}, errors {errors}, '
                  f'loss {loss}, mean RTT {latency}')
        if not ping_present:
            print('  Ping: no measurements')

        speed = []
        if 'speed_tests' in tables:
            speed = db.execute("""
                SELECT direction, COUNT(mbps), COUNT(*) - COUNT(mbps), AVG(mbps)
                FROM speed_tests
                WHERE finished_ms >= ? AND finished_ms < ?
                GROUP BY direction ORDER BY direction
            """, bounds).fetchall()
        for direction, ok, errors, mbps in speed:
            average = f'{mbps:.2f} Mbit/s' if mbps is not None else 'n/a'
            print(f'  {direction}: OK {ok}, errors {errors}, mean {average}')
        if not speed:
            print('  Speed: no measurements')


def maintain_reports(db, directory=REPORT_DIR, now=None):
    now = now if now is not None else datetime.now(ZONE)
    existing_periods = prune_reports(directory, now)
    tables = tables_in(db)
    targets = [target for target, in db.execute(
        'SELECT DISTINCT target FROM probes ORDER BY target'
    )] if 'probes' in tables else []
    for period, buckets in completed_periods(now.astimezone(ZONE).date()):
        if period in existing_periods:
            continue
        if midnight_ms(buckets[-1][2]) <= (now.timestamp() - RETENTION_SECONDS) * 1000:
            continue
        if has_measurements(db, tables, targets, buckets[0][1], buckets[-1][2]):
            existing_periods[period] = write_report(
                lambda: render_summary(db, period, buckets, tables, targets),
                period, directory
            )
    return list(existing_periods.values())
