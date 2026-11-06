"""Estimate outage counts and durations from consecutive ping checks."""

from itertools import groupby

from netwatch_config import EXTERNAL_TARGETS

GAP_MS = 8_000


def probe_cycles(db, start, end):
    rows = db.execute("""
        SELECT run_id, cycle, ts_ms, target, status
        FROM probes
        WHERE ts_ms >= ? AND ts_ms < ?
        ORDER BY ts_ms, rowid
    """, (start, end))
    for sample, probes in groupby(rows, key=lambda row: row[:3]):
        yield sample, {target: status for _, _, _, target, status in probes}


def consecutive(previous, current):
    prev_run, prev_cycle, prev_time = previous
    run, cycle, timestamp = current
    return run == prev_run and cycle == prev_cycle + 1 and 0 < timestamp - prev_time <= GAP_MS


def probe_statuses(statuses, default_gateway):
    gateways = [target for target in statuses if target not in EXTERNAL_TARGETS]
    gateway = gateways[0] if len(gateways) == 1 else default_gateway
    return gateway, statuses.get(gateway, 'error'), *(
        statuses.get(target, 'error') for target in EXTERNAL_TARGETS
    )


def format_duration(ms):
    seconds = (ms + 500) // 1000
    days, seconds = divmod(seconds, 86_400)
    hours, seconds = divmod(seconds, 3_600)
    minutes, seconds = divmod(seconds, 60)
    prefix = f'{days}d ' if days else ''
    return f'{prefix}{hours:02d}h {minutes:02d}m {seconds:02d}s'


class OutageAnalysis:
    def __init__(self):
        self.previous = None
        self.outages = 0
        self.router_misses = 0
        self.usable_external_checks = 0
        self.downtime = dict.fromkeys(('ok', 'no_reply', 'error'), 0)

    def observe(self, sample, router, first_external, second_external):
        down = first_external == second_external == 'no_reply'
        connected = False
        previous_down = False
        if self.previous is not None:
            previous_sample, previous_router, previous_down = self.previous
            connected = consecutive(previous_sample, sample)
            if connected and previous_down:
                self.downtime[previous_router] += sample[2] - previous_sample[2]
        if down and not (connected and previous_down):
            self.outages += 1
        self.router_misses += router == 'no_reply'
        self.usable_external_checks += down or 'ok' in (first_external, second_external)
        self.previous = sample, router, down

    def print_summary(self):
        print('\nAnalysis:')
        print(f'  Internet outage episodes observed: {self.outages}')
        print(f'  Router checks without a reply: {self.router_misses}')
        if self.usable_external_checks:
            print(f'  Estimated time without internet: {format_duration(sum(self.downtime.values()))}')
            print(f'    While the router responded: {format_duration(self.downtime["ok"])}')
            print(f'    While the router did not answer: {format_duration(self.downtime["no_reply"])}')
            print(f'    With router status unknown: {format_duration(self.downtime["error"])}')
        else:
            print('  Estimated time without internet: unavailable (no conclusive external checks)')
        print('  Both external probes must record no reply to count an outage. '
              'Durations use consecutive checks only. Missing checks, external probe errors, '
              'and time after the last check do not add to downtime.')
