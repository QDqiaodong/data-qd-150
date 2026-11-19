"""Offline perishable daily-demand training panel (schema_version=8).

This mode is a one-shot, read-only data-set builder.  It organizes historical
per-SKU daily POS observations and in-store shelf-witness coverage into
reproducible sliding lookback/horizon windows split into train and test
samples.  It never predicts demand, never optimizes promotion prices, stock
allocation or fulfillment plans, never mutates the v1-v6 entry points, and
never writes any business ledger.

Two evidence streams stay separate and are never added together or
cross-inferred:

  * the POS observation of a business day (``sold_units`` / ``promoted`` /
    ``covered_minutes``), which only becomes queryable at the day's UTC
    midnight boundary;
  * shelf in-stock minutes derived from half-open UTC witness intervals that
    each reference, by report identity and ``(package, order, lot)``, an
    exact line verdict of an immutable v6 report, and that carry their own
    ``visible_at`` instant at which both the interval and the linked verdict
    become usable.

Only visible, currently-arriving (``received > 0``) and ``release`` verdicts
contribute qualifying in-stock minutes.  Witnesses linked to
``quarantine`` / ``unknown`` / ``no_receipt`` lines are still evidence that a
witness was seen: they can force a day's ``in_stock_minutes`` to 0 instead of
leaving it null, but they never add coverage.  Overlapping minutes on the
same day are unioned once; witnesses crossing midnight are clipped per UTC
day and never carry leftover minutes into another day.

Point-in-time semantics
-----------------------
Every day view is evaluated at a query cutoff.  The calendar view and every
target day use the run's ``evidence_as_of`` cutoff.  A feature day of a
sample anchored at day A may only use proof available by the earlier of
A's day-end (next-day 00:00Z) and ``evidence_as_of``: late proof can
qualify a past *target* day but can never backfill a feature that was not
knowable at its anchor.  Facts whose visibility instant equals the cutoff
are included.

A target label is complete only when the POS row is visible at the cutoff
and both measurements equal 1440 (full POS monitoring and full qualifying
in-stock coverage); the label is then the original ``sold_units`` (an
explicit zero stays 0).  Otherwise the label is null.  Two censored-target
policies are supported:

  * ``drop_window`` excludes any whole window containing an invalid target;
  * ``mask_days`` keeps the window and marks invalid targets with
    ``label=null`` / ``valid=false``.

Only the Python standard library is used.
"""

from __future__ import annotations

import bisect
import os
import re
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from .common import ident, require, rows, read, save

SCHEMA_VERSION = 8
MAX_SKUS = 64
MAX_CALENDAR_DAYS = 366
MAX_REPORTS = 64
MAX_WITNESSES = 20000
MAX_LOOKBACK = 90
MAX_HORIZON = 28
MAX_SOLD = 10 ** 9
MAX_RECEIVED = 10 ** 9
DAY_MINUTES = 1440

DROP_WINDOW = 'drop_window'
MASK_DAYS = 'mask_days'
POLICIES = (DROP_WINDOW, MASK_DAYS)

DISPOSITIONS = ('release', 'quarantine', 'unknown', 'no_receipt')
UTC = timezone.utc

# Whole-minute UTC timestamps only, canonical Z spelling.
_MINUTE_STAMP = re.compile(r'^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:00Z$')
_DAY_STAMP = re.compile(r'^\d{4}-\d{2}-\d{2}$')


# --------------------------------------------------------------------------
# Strict scalar validation
# --------------------------------------------------------------------------

def _integer(value, minimum, maximum, label):
    # type(...) is int deliberately: bool is an int subclass and is rejected,
    # as are floats and numeric strings.
    if type(value) is not int or not minimum <= value <= maximum:
        raise ValueError('%s must be an integer in [%d, %d]'
                         % (label, minimum, maximum))
    return value


def _canonical_day(value, label):
    if not isinstance(value, str) or not _DAY_STAMP.match(value):
        raise ValueError('%s must use canonical YYYY-MM-DD format' % label)
    try:
        parsed = date.fromisoformat(value)
    except ValueError:
        raise ValueError('%s is not a valid ISO date' % label)
    if parsed.isoformat() != value:
        raise ValueError('%s must use canonical YYYY-MM-DD format' % label)
    return parsed


def _minute_timestamp(value, label):
    """Parse a canonical whole-minute UTC timestamp ``YYYY-MM-DDTHH:MM:00Z``."""
    if not isinstance(value, str) or not _MINUTE_STAMP.match(value):
        raise ValueError('%s must be a UTC whole-minute timestamp '
                         'YYYY-MM-DDTHH:MM:00Z' % label)
    try:
        return datetime.strptime(value, '%Y-%m-%dT%H:%M:%SZ') \
            .replace(tzinfo=UTC)
    except ValueError:
        raise ValueError('%s is not a valid UTC timestamp' % label)


def _optional_integer(value, minimum, maximum, label):
    if value is None:
        return None
    return _integer(value, minimum, maximum, label)


def _optional_bool(value, label):
    if value is None:
        return None
    if not isinstance(value, bool):
        raise ValueError('%s must be a boolean or null' % label)
    return value


# --------------------------------------------------------------------------
# Input parsing and structural validation
# --------------------------------------------------------------------------

def parse_input(data):
    """Validate the whole v8 source and return a normalized fact view."""
    require(data, 'schema_version calendar_start cutoff_date split_date '
                  'evidence_as_of lookback_days horizon_days '
                  'censored_target_policy skus observations v6_reports '
                  'shelf_witnesses')
    if type(data['schema_version']) is not int \
            or data['schema_version'] != SCHEMA_VERSION:
        raise ValueError('schema_version must be the integer 8')

    calendar_start = _canonical_day(data['calendar_start'], 'calendar_start')
    cutoff = _canonical_day(data['cutoff_date'], 'cutoff_date')
    split_day = _canonical_day(data['split_date'], 'split_date')
    if not calendar_start <= cutoff:
        raise ValueError('calendar_start must be on or before cutoff_date')
    if not calendar_start <= split_day < cutoff:
        raise ValueError('split_date must be inside the calendar and before '
                         'cutoff_date')
    day_count = (cutoff - calendar_start).days + 1
    if day_count > MAX_CALENDAR_DAYS:
        raise ValueError('calendar may span at most %d days'
                         % MAX_CALENDAR_DAYS)
    # The last calendar day must have a representable next day.
    try:
        datetime.combine(cutoff + timedelta(days=1),
                         datetime.min.time(), UTC)
    except OverflowError:
        raise ValueError('calendar end has no representable next day')

    evidence_as_of = _minute_timestamp(data['evidence_as_of'],
                                       'evidence_as_of')

    lookback = _integer(data['lookback_days'], 1, MAX_LOOKBACK,
                        'lookback_days')
    horizon = _integer(data['horizon_days'], 1, MAX_HORIZON,
                       'horizon_days')
    policy = data['censored_target_policy']
    if policy not in POLICIES:
        raise ValueError('censored_target_policy must be one of: '
                         + ', '.join(POLICIES))

    skus = _parse_skus(data['skus'])
    observations = _parse_observations(data['observations'], skus,
                                       calendar_start, cutoff)
    verdict_index = _parse_reports(data['v6_reports'])
    witnesses = _parse_witnesses(data['shelf_witnesses'], skus,
                                 verdict_index, calendar_start, cutoff)
    return {
        'calendar_start': calendar_start,
        'cutoff': cutoff,
        'split_day': split_day,
        'evidence_as_of': evidence_as_of,
        'lookback': lookback,
        'horizon': horizon,
        'policy': policy,
        'skus': skus,
        'observations': observations,
        'witnesses': witnesses,
    }


def _parse_skus(raw):
    sku_list = rows(raw, MAX_SKUS)
    if not sku_list:
        raise ValueError('at least one sku is required')
    skus = []
    seen = set()
    for raw_sku in sku_list:
        sku = ident(raw_sku)
        if sku in seen:
            raise ValueError('duplicate sku: ' + sku)
        seen.add(sku)
        skus.append(sku)
    return skus


def _parse_observations(raw, skus, calendar_start, cutoff):
    """Normalize POS rows into a (sku, day-index) keyed view."""
    observation_list = rows(raw, MAX_CALENDAR_DAYS * MAX_SKUS)
    sku_set = set(skus)
    by_key = {}
    for raw_row in observation_list:
        require(raw_row, 'date sku sold_units covered_minutes promoted')
        day = _canonical_day(raw_row['date'], 'observation.date')
        if not calendar_start <= day <= cutoff:
            raise ValueError('observation date outside the historical '
                             'calendar: ' + day.isoformat())
        sku = ident(raw_row['sku'])
        if sku not in sku_set:
            raise ValueError('observation references an unknown sku: ' + sku)
        key = (sku, day)
        if key in by_key:
            raise ValueError('duplicate observation for %s on %s'
                             % (sku, day.isoformat()))
        sold = _optional_integer(raw_row['sold_units'], 0, MAX_SOLD,
                                 'sold_units')
        covered = _optional_integer(raw_row['covered_minutes'], 0,
                                    DAY_MINUTES, 'covered_minutes')
        promoted = _optional_bool(raw_row['promoted'], 'promoted')
        by_key[key] = {
            'day': day,
            'sku': sku,
            'sold_units': sold,
            'covered_minutes': covered,
            'promoted': promoted,
        }
    return by_key


def _parse_reports(raw):
    """Consume only schema_version and line_verdicts of immutable v6 reports."""
    report_list = rows(raw, MAX_REPORTS)
    verdict_index = {}
    report_ids = set()
    for entry in report_list:
        require(entry, 'report_id report')
        report_id = ident(entry['report_id'])
        if report_id in report_ids:
            raise ValueError('duplicate v6 report id: ' + report_id)
        report_ids.add(report_id)
        report = entry['report']
        if not isinstance(report, dict):
            raise ValueError('v6 report must be an object: ' + report_id)
        if type(report.get('schema_version')) is not int \
                or report['schema_version'] != 6:
            raise ValueError('v6 report %s must carry integer '
                             'schema_version=6' % report_id)
        raw_verdicts = report.get('line_verdicts')
        if not isinstance(raw_verdicts, list):
            raise ValueError('v6 report %s requires a line_verdicts array'
                             % report_id)
        seen_keys = set()
        for raw_verdict in raw_verdicts:
            if not isinstance(raw_verdict, dict):
                raise ValueError('line verdict must be an object in report: '
                                 + report_id)
            package = ident(raw_verdict.get('package'))
            order = ident(raw_verdict.get('order'))
            lot = ident(raw_verdict.get('lot'))
            sku = ident(raw_verdict.get('sku'))
            received = _integer(raw_verdict.get('received'), 0,
                                MAX_RECEIVED, 'verdict.received')
            disposition = raw_verdict.get('disposition')
            if disposition not in DISPOSITIONS:
                raise ValueError('unknown disposition in report %s: %r'
                                 % (report_id, disposition))
            if disposition == 'no_receipt':
                if received != 0:
                    raise ValueError('no_receipt verdict requires received=0 '
                                     'in report %s' % report_id)
            elif received <= 0:
                raise ValueError('positive-arrival verdicts require '
                                 'received > 0 in report %s' % report_id)
            key = (report_id, package, order, lot)
            if key in seen_keys:
                raise ValueError('duplicate verdict key %s/%s/%s in report %s'
                                 % (package, order, lot, report_id))
            seen_keys.add(key)
            verdict_index[key] = {
                'report_id': report_id,
                'package': package,
                'order': order,
                'lot': lot,
                'sku': sku,
                'received': received,
                'disposition': disposition,
            }
    return verdict_index


def _parse_witnesses(raw, skus, verdict_index, calendar_start, cutoff):
    witness_list = rows(raw, MAX_WITNESSES)
    sku_set = set(skus)
    witnesses = []
    seen_ids = set()
    cal_lo = datetime.combine(calendar_start, datetime.min.time(), UTC)
    cal_hi = datetime.combine(cutoff + timedelta(days=1),
                              datetime.min.time(), UTC)
    for raw_witness in witness_list:
        require(raw_witness, 'id sku start end report_id package order lot '
                             'visible_at')
        witness_id = ident(raw_witness['id'])
        if witness_id in seen_ids:
            raise ValueError('duplicate shelf witness id: ' + witness_id)
        seen_ids.add(witness_id)
        sku = ident(raw_witness['sku'])
        if sku not in sku_set:
            raise ValueError('witness %s references an unknown sku: %s'
                             % (witness_id, sku))
        start = _minute_timestamp(raw_witness['start'], 'witness.start')
        end = _minute_timestamp(raw_witness['end'], 'witness.end')
        visible_at = _minute_timestamp(raw_witness['visible_at'],
                                       'witness.visible_at')
        if not start < end:
            raise ValueError('witness %s requires start < end' % witness_id)
        if end > visible_at:
            raise ValueError('witness %s requires end <= visible_at'
                             % witness_id)
        # Half-open interval must intersect the historical calendar span.
        if not (start < cal_hi and end > cal_lo):
            raise ValueError('witness %s does not intersect the historical '
                             'calendar' % witness_id)
        report_id = ident(raw_witness['report_id'])
        package = ident(raw_witness['package'])
        order = ident(raw_witness['order'])
        lot = ident(raw_witness['lot'])
        key = (report_id, package, order, lot)
        verdict = verdict_index.get(key)
        if verdict is None:
            # Dangling references are whole-input errors; missing evidence
            # must instead be expressed by omitting the witness.
            raise ValueError('witness %s has no exact verdict for '
                             '%s/%s/%s/%s'
                             % (witness_id, report_id, package, order, lot))
        if verdict['sku'] != sku:
            raise ValueError('witness %s sku does not match the linked '
                             'verdict sku' % witness_id)
        qualifying = (verdict['disposition'] == 'release'
                      and verdict['received'] > 0)
        witnesses.append({
            'id': witness_id,
            'sku': sku,
            'start': start,
            'end': end,
            'visible_at': visible_at,
            'qualifying': qualifying,
        })
    return witnesses


# --------------------------------------------------------------------------
# In-stock coverage geometry
# --------------------------------------------------------------------------

class CoverageIndex:
    """Point-in-time in-stock coverage lookup per (sku, calendar day).

    For every (sku, day) that at least one witness intersects, two sorted
    timelines are retained:

      * ``first_seen``: distinct ``visible_at`` instants at which any witness
        becomes visible, so any query cutoff can tell whether a witness (of
        any kind) is known yet;
      * ``release_states``: ``(visible_at, 1440-bit mask)`` states covering
        the union of qualifying release intervals visible by that instant.

    A query at cutoff Q therefore costs O(log n) with no per-query interval
    scan; overlapping minutes are merged through bitwise OR so same-day
    overlaps count once, and cross-midnight intervals are clipped into each
    UTC day independently with no minute carry-over.
    """

    def __init__(self, witnesses, calendar_start, cutoff):
        grouped = {}
        for witness in witnesses:
            start_day = (witness['start'].date() - calendar_start).days
            end_day = (witness['end'].date() - calendar_start).days
            first = max(start_day, 0)
            last = min(end_day, (cutoff - calendar_start).days)
            for offset in range(first, last + 1):
                day = calendar_start + timedelta(days=offset)
                day_start = datetime.combine(day, datetime.min.time(), UTC)
                day_end = day_start + timedelta(days=1)
                lo = max(witness['start'], day_start)
                hi = min(witness['end'], day_end)
                if lo >= hi:
                    continue
                lo_bit = int((lo - day_start).total_seconds()) // 60
                hi_bit = int((hi - day_start).total_seconds()) // 60
                mask = ((1 << (hi_bit - lo_bit)) - 1) << lo_bit
                bucket = grouped.setdefault((witness['sku'], day),
                                            {'seen': [], 'release': []})
                bucket['seen'].append(witness['visible_at'])
                if witness['qualifying']:
                    bucket['release'].append(
                        (witness['visible_at'], mask))
        self._views = {}
        for key, bucket in grouped.items():
            seen_times = sorted(set(bucket['seen']))
            states = {}
            for visible_at, mask in bucket['release']:
                states[visible_at] = states.get(visible_at, 0) | mask
            release_times = sorted(states)
            cumulative = []
            union = 0
            for visible_at in release_times:
                union |= states[visible_at]
                cumulative.append((visible_at, union))
            self._views[key] = {'seen': seen_times,
                                'release': cumulative}

    def in_stock_minutes(self, sku, day, cutoff):
        """Return null (no visible witness), 0 (seen, no coverage) or minutes."""
        view = self._views.get((sku, day))
        if view is None:
            return None
        seen_at = bisect.bisect_right(view['seen'], cutoff)
        if seen_at == 0:
            return None
        states = view['release']
        idx = bisect.bisect_right(states, cutoff, key=lambda pair: pair[0])
        if idx == 0:
            return 0
        return states[idx - 1][1].bit_count()


# --------------------------------------------------------------------------
# Day-view and sample construction
# --------------------------------------------------------------------------

def _pos_available(day, cutoff):
    """A POS row becomes queryable exactly at its business day's end."""
    boundary = datetime.combine(day + timedelta(days=1),
                                datetime.min.time(), UTC)
    return boundary <= cutoff


def build_day_view(parsed, coverage, sku, day, cutoff):
    """Materialize one SKU/day record as visible at the given cutoff."""
    observation = parsed['observations'].get((sku, day))
    observed = observation is not None and _pos_available(day, cutoff)
    if observed:
        sold = observation['sold_units']
        covered = observation['covered_minutes']
        promoted = observation['promoted']
    else:
        sold = covered = promoted = None
    in_stock = coverage.in_stock_minutes(sku, day, cutoff)
    uncensored = (observed and covered == DAY_MINUTES
                  and in_stock == DAY_MINUTES)
    return {
        'date': day.isoformat(),
        'sold_units': sold,
        'covered_minutes': covered,
        'promoted': promoted,
        'in_stock_minutes': in_stock,
        'observed': observed,
        'uncensored': uncensored,
    }


def _target_view(parsed, coverage, sku, day):
    """Target days always use the run evidence cutoff (no anchor cap)."""
    view = build_day_view(parsed, coverage, sku, day,
                          parsed['evidence_as_of'])
    if view['uncensored']:
        view['label'] = view['sold_units']
        view['valid'] = True
    else:
        view['label'] = None
        view['valid'] = False
    return view


def _feature_view(parsed, coverage, sku, day, anchor_day):
    """Feature days are capped at the earlier of anchor day-end and cutoff."""
    anchor_end = datetime.combine(anchor_day + timedelta(days=1),
                                  datetime.min.time(), UTC)
    cutoff = min(anchor_end, parsed['evidence_as_of'])
    return build_day_view(parsed, coverage, sku, day, cutoff)


def compile_report(data, source_sha256):
    """Validate, build the calendar views and window samples (schema 8)."""
    parsed = parse_input(data)
    coverage = CoverageIndex(parsed['witnesses'], parsed['calendar_start'],
                             parsed['cutoff'])

    start = parsed['calendar_start']
    cutoff = parsed['cutoff']
    split_day = parsed['split_day']
    lookback = parsed['lookback']
    horizon = parsed['horizon']
    all_days = [start + timedelta(days=i)
                for i in range((cutoff - start).days + 1)]

    # Calendar block: one view per SKU/day at the run evidence cutoff.  SKU
    # order is sorted rather than input order, so legal array permutations
    # cannot change the business output.
    calendar = []
    calendar_day_stats = {}
    for sku in sorted(parsed['skus']):
        stats = {'uncensored_days': 0, 'observed_not_complete_days': 0,
                 'no_pos_days': 0}
        for day in all_days:
            view = build_day_view(parsed, coverage, sku, day,
                                  parsed['evidence_as_of'])
            calendar.append(view)
            if view['uncensored']:
                stats['uncensored_days'] += 1
            elif view['observed']:
                stats['observed_not_complete_days'] += 1
            else:
                stats['no_pos_days'] += 1
        calendar_day_stats[sku] = stats

    samples = []
    summary = []
    n_days = len(all_days)
    for sku in sorted(parsed['skus']):
        boundary_excluded = 0
        in_bounds = 0
        cross_split = 0
        excluded_missing_pos = 0
        excluded_incomplete = 0
        train_count = 0
        test_count = 0

        for anchor_index, anchor_day in enumerate(all_days):
            feature_indices = range(anchor_index - lookback + 1,
                                    anchor_index + 1)
            target_indices = range(anchor_index + 1,
                                   anchor_index + 1 + horizon)
            if anchor_index - lookback + 1 < 0 \
                    or anchor_index + horizon >= n_days:
                # Either window would leave the historical calendar.
                boundary_excluded += 1
                continue
            in_bounds += 1
            target_days = [all_days[i] for i in target_indices]
            first_target = target_days[0]
            last_target = target_days[-1]
            if not (last_target <= split_day or first_target > split_day):
                cross_split += 1
                continue

            target_views = [_target_view(parsed, coverage, sku, day)
                            for day in target_days]
            invalid_targets = [view for view in target_views
                               if not view['valid']]
            if invalid_targets and parsed['policy'] == DROP_WINDOW:
                if any(not view['observed'] for view in invalid_targets):
                    excluded_missing_pos += 1
                else:
                    excluded_incomplete += 1
                continue

            feature_views = [
                _feature_view(parsed, coverage, sku,
                              all_days[i], anchor_day)
                for i in feature_indices
            ]
            split = 'train' if last_target <= split_day else 'test'
            if split == 'train':
                train_count += 1
            else:
                test_count += 1
            samples.append({
                'split': split,
                'sku': sku,
                'anchor_date': anchor_day.isoformat(),
                'feature_days': feature_views,
                'target_days': target_views,
            })

        # Every in-bounds candidate lands in exactly one of these four bins.
        if in_bounds != (cross_split + excluded_missing_pos
                         + excluded_incomplete + train_count + test_count):
            raise ValueError('internal: candidate accounting mismatch')
        summary.append({
            'sku': sku,
            'boundary_excluded': boundary_excluded,
            'in_bounds_candidates': in_bounds,
            'cross_split_excluded': cross_split,
            'dropped_missing_pos': excluded_missing_pos,
            'dropped_observed_incomplete': excluded_incomplete,
            'train_samples': train_count,
            'test_samples': test_count,
            'uncensored_days': calendar_day_stats[sku]['uncensored_days'],
            'observed_not_complete_days':
                calendar_day_stats[sku]['observed_not_complete_days'],
            'no_pos_days': calendar_day_stats[sku]['no_pos_days'],
        })

    # train before test, then SKU and anchor date ascending.
    samples.sort(key=lambda sample: (
        sample['split'], sample['sku'], sample['anchor_date']))

    return {
        'schema_version': SCHEMA_VERSION,
        'source_sha256': source_sha256,
        'policy': {
            'calendar_start': parsed['calendar_start'].isoformat(),
            'cutoff_date': parsed['cutoff'].isoformat(),
            'split_date': parsed['split_day'].isoformat(),
            'lookback_days': parsed['lookback'],
            'horizon_days': parsed['horizon'],
            'evidence_as_of':
                parsed['evidence_as_of'].strftime('%Y-%m-%dT%H:%M:%SZ'),
            'censored_target_policy': parsed['policy'],
        },
        'calendar': calendar,
        'summary': summary,
        'samples': samples,
    }


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------

DESCRIPTION = ('Offline perishable daily-demand training panel: reproducible '
               'sliding-window train/test data sets without prediction')


def _check_paths(source, output):
    if output.is_symlink():
        raise ValueError('output cannot be a symlink')
    if source.resolve() == output.resolve():
        raise ValueError('input and output must differ')
    # resolve() cannot reveal a hard link to the source's inode.
    if output.exists() and os.path.samefile(source, output):
        raise ValueError('input and output must not be the same file')


def run(source):
    data, digest = read(source)
    return compile_report(data, digest)


def main(argv=None):
    import argparse
    import json
    import sys

    parser = argparse.ArgumentParser(prog='perishable-demand-panel',
                                     description=DESCRIPTION)
    parser.add_argument('--source', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        _check_paths(args.source, args.output)
        report = run(args.source)
        save(args.output, report)
    except (ValueError, TypeError, KeyError, OSError, OverflowError,
            RecursionError) as error:
        print(str(error), file=sys.stderr)
        return 2
    print(json.dumps(report, sort_keys=True, ensure_ascii=False,
                     allow_nan=False))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
