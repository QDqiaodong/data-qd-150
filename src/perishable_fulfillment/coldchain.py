"""Offline cold-chain arrival release analysis (schema_version=6).

Warehouse staff must decide, after dispatch, whether an arrived lot batch may
enter sellable stock.  The unchanged v1 allocator (``model.analyze``) only
fixes the *shippable upper bound* per ``(order, lot)``; it says nothing about
what was physically packed, signed for, or the transport temperature.  This
mode is a one-shot, read-only offline analysis: it joins packing lines,
received quantities and temperature observations to the fixed v1 orders and
lots, and emits a traceable release report.  It never writes stock, ledgers,
orders or prices, never re-allocates on temperature outcomes, and never reads
the v2 SQLite ledger, the v3 road network, v4 promotions or the v5 recall
graph.

Temperature semantics
----------------------
Each reading is a measurement at its timestamp, normalized to UTC.  When two
adjacent readings are no more than the SKU's ``max_gap_minutes`` apart,
temperature is interpreted as changing **linearly** between them; a longer
separation is an unobserved gap and no interpolation is performed across it.
The one-sided edge regions (transport start to first reading, last reading to
transport end) are never linearly extrapolated: when the edge length is within
the gap allowance the boundary reading's temperature holds constantly over
the edge, otherwise the whole edge is an unobserved gap.  A package carrying a
single reading applies those two edge rules independently.  Temperature
exactly equal to ``min_c``/``max_c`` is inside the acceptable band.

Computation structure (explicit choice)
---------------------------------------
Two exact structures satisfy the contract:

  A. clip every adjacent-sample (or constant-edge) interval against the SKU
     band and accumulate rational out-of-range exposure, tracking over-long
     separations as standalone uncovered intervals;
  B. first normalize the package readings into non-overlapping UTC segments
     (covered vs. unobserved), then scan those segments per SKU policy.

This module adopts **structure A**.  Its correctness obligations are:

  * segments meet only at reading instants, which have zero duration, so a
    boundary instant can never be timed twice -- exposure is accumulated per
    disjoint closed-open interval and the edges are clipped to the transport
    window, never extended beyond it;
  * an over-long separation produces a gap interval and is excluded from
    clipping entirely, so unobserved time is never silently interpolated or
    treated as compliant;
  * every crossing instant of ``min_c``/``max_c`` is solved as an exact
    rational fraction of the segment (``Fraction`` throughout -- no float
    duration accumulation, no rounding before the verdict), and exposure is
    only ever compared exactly against ``max_excursion_minutes``;
  * the same package timeline is re-evaluated independently for each line's
    own SKU policy, so SKUs sharing a box never mix temperature bands or gap
    allowances.

Structure B would have to obey the identical interpolation, gap and band
semantics; the test suite contains an independent structure-B oracle
implemented from scratch (it imports none of the production temperature code)
and hand-computed rational cases that both views must agree with.

Only the Python standard library is used.
"""

from __future__ import annotations

import math
import os
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from fractions import Fraction
from pathlib import Path

from .common import ident, require, rows, read, save
from .model import analyze

SCHEMA_VERSION = 6

# Bounded input sizes, documented in docs/contract-v6.md.  The 8 MB JSON byte
# limit is inherited unchanged from common.read.
MAX_POLICIES = 128
MAX_PACKAGES = 256
MAX_READINGS_PER_PACKAGE = 4096
MAX_LINES_PER_PACKAGE = 512
MAX_QUANTITY = 10 ** 9
MAX_GAP_MINUTES = 10 ** 9

# Disposition vocabulary used in the report.
RELEASE = 'release'
QUARANTINE = 'quarantine'
UNKNOWN = 'unknown'
NO_RECEIPT = 'no_receipt'


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


def _finite_number(value, label):
    # A genuine JSON number: int or float, never bool, always finite.  The
    # restricted parser already rejects NaN/Infinity at the JSON layer.
    if type(value) not in (int, float) or isinstance(value, bool) \
            or not math.isfinite(value):
        raise ValueError('%s must be a finite JSON number' % label)
    return value


def _timestamp(value, label):
    """Parse a timezone-aware ISO-8601 timestamp and normalize it to UTC."""
    if not isinstance(value, str):
        raise ValueError('%s must be an ISO-8601 timestamp string' % label)
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        raise ValueError('%s is not a valid ISO-8601 timestamp' % label)
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError('%s must carry an explicit timezone offset' % label)
    return parsed.astimezone(timezone.utc)


def _minutes(delta):
    """Exact duration in minutes as a rational number (never a float)."""
    micros = (delta.days * 86400 + delta.seconds) * 1_000_000 \
        + delta.microseconds
    return Fraction(micros, 60_000_000)


def _frac_json(value):
    return {'numerator': value.numerator,
            'denominator': value.denominator}


def _temp(value):
    """Exact rational temperature; JSON floats keep their exact binary value."""
    return Fraction(value)


# --------------------------------------------------------------------------
# Input parsing and structural validation
# --------------------------------------------------------------------------

def parse_input(data):
    """Validate the whole v6 source and return a normalized fact view."""
    require(data, 'schema_version fulfillment policies packages')
    if type(data['schema_version']) is not int \
            or data['schema_version'] != SCHEMA_VERSION:
        raise ValueError('schema_version must be the integer 6')

    fulfillment = data['fulfillment']
    # model.analyze remains the single authority for the v1 object contract,
    # its FEFO priority, held, expiry and shortage semantics.
    v1_result = analyze(fulfillment)
    raw_lots = fulfillment['lots']
    raw_orders = fulfillment['orders']
    lot_sku = {lot['id']: lot['sku'] for lot in raw_lots}
    order_sku = {order['id']: order['sku'] for order in raw_orders}

    policies = _parse_policies(data['policies'], lot_sku, order_sku)
    packages = _parse_packages(data['packages'])

    # Fixed positive v1 allocations keyed by (order, lot).
    allocation = {}
    for order_row in v1_result['orders']:
        for piece in order_row['allocations']:
            if piece['quantity'] > 0:
                allocation[(order_row['order'], piece['lot'])] = \
                    piece['quantity']

    _validate_lines(packages, allocation, lot_sku, order_sku)

    return {
        'v1_result': v1_result,
        'raw_lots': raw_lots,
        'raw_orders': raw_orders,
        'lot_sku': lot_sku,
        'order_sku': order_sku,
        'policies': policies,
        'packages': packages,
        'allocation': allocation,
    }


def _parse_policies(raw, lot_sku, order_sku):
    policy_list = rows(raw, MAX_POLICIES)
    policies = {}
    for raw_policy in policy_list:
        require(raw_policy,
                'sku min_c max_c max_gap_minutes max_excursion_minutes')
        sku = ident(raw_policy['sku'])
        if sku in policies:
            raise ValueError('duplicate policy for sku: ' + sku)
        min_c = _finite_number(raw_policy['min_c'], 'min_c')
        max_c = _finite_number(raw_policy['max_c'], 'max_c')
        if not min_c < max_c:
            raise ValueError('policy requires min_c < max_c for sku: ' + sku)
        max_gap = _integer(raw_policy['max_gap_minutes'], 0,
                           MAX_GAP_MINUTES, 'max_gap_minutes')
        max_excursion = _integer(raw_policy['max_excursion_minutes'], 0,
                                 MAX_GAP_MINUTES, 'max_excursion_minutes')
        policies[sku] = {
            'sku': sku,
            'min_c': min_c,
            'max_c': max_c,
            'max_gap_minutes': max_gap,
            'max_excursion_minutes': max_excursion,
            'min_c_exact': _temp(min_c),
            'max_c_exact': _temp(max_c),
        }
    required_skus = set(lot_sku.values()) | set(order_sku.values())
    missing = sorted(required_skus - set(policies))
    if missing:
        raise ValueError('missing temperature policy for sku: '
                         + ', '.join(missing))
    extra = sorted(set(policies) - required_skus)
    if extra:
        raise ValueError('policy covers an sku absent from fulfillment: '
                         + ', '.join(extra))
    return policies


def _parse_packages(raw):
    package_list = rows(raw, MAX_PACKAGES)
    packages = []
    seen_ids = set()
    for raw_package in package_list:
        require(raw_package, 'id transit_start transit_end readings lines')
        package_id = ident(raw_package['id'])
        if package_id in seen_ids:
            raise ValueError('duplicate package id: ' + package_id)
        seen_ids.add(package_id)
        start = _timestamp(raw_package['transit_start'], 'transit_start')
        end = _timestamp(raw_package['transit_end'], 'transit_end')
        if not start < end:
            raise ValueError('transit_start must be before transit_end for '
                             'package: ' + package_id)

        reading_list = rows(raw_package['readings'],
                            MAX_READINGS_PER_PACKAGE)
        if not reading_list:
            raise ValueError('package must carry at least one reading: '
                             + package_id)
        readings = []
        previous = None
        for raw_reading in reading_list:
            require(raw_reading, 'at temp_c')
            at = _timestamp(raw_reading['at'], 'reading.at')
            if not start <= at <= end:
                raise ValueError('reading outside the transit window in '
                                 'package: ' + package_id)
            if previous is not None and not previous < at:
                raise ValueError('readings must be strictly increasing in '
                                 'package: ' + package_id)
            previous = at
            temp_c = _finite_number(raw_reading['temp_c'], 'reading.temp_c')
            readings.append({'at': at, 'temp_c': temp_c,
                             'temp_exact': _temp(temp_c)})

        line_list = rows(raw_package['lines'], MAX_LINES_PER_PACKAGE)
        lines = []
        seen_pairs = set()
        for raw_line in line_list:
            require(raw_line, 'order lot packed received')
            order_id = ident(raw_line['order'])
            lot_id = ident(raw_line['lot'])
            packed = _integer(raw_line['packed'], 0, MAX_QUANTITY, 'packed')
            received = _integer(raw_line['received'], 0, packed, 'received')
            pair = (order_id, lot_id)
            if pair in seen_pairs:
                raise ValueError('duplicate order-lot line in package %s: '
                                 '%s/%s' % (package_id, order_id, lot_id))
            seen_pairs.add(pair)
            lines.append({'order': order_id, 'lot': lot_id,
                          'packed': packed, 'received': received})

        packages.append({'id': package_id, 'transit_start': start,
                         'transit_end': end, 'readings': readings,
                         'lines': lines})
    return packages


def _validate_lines(packages, allocation, lot_sku, order_sku):
    """Reference, SKU-match and over-packing checks against fixed allocation."""
    packed_total = defaultdict(int)
    for package in packages:
        for line in package['lines']:
            order_id, lot_id = line['order'], line['lot']
            if order_id not in order_sku:
                raise ValueError('line references an unknown order: '
                                 + order_id)
            if lot_id not in lot_sku:
                raise ValueError('line references an unknown lot: ' + lot_id)
            if lot_sku[lot_id] != order_sku[order_id]:
                raise ValueError(
                    'line lot sku does not match order sku: %s/%s'
                    % (order_id, lot_id))
            key = (order_id, lot_id)
            if key not in allocation:
                raise ValueError(
                    'packing line has no positive v1 allocation: %s/%s'
                    % (order_id, lot_id))
            packed_total[key] += line['packed']
    for (order_id, lot_id), packed in sorted(packed_total.items()):
        if packed > allocation[(order_id, lot_id)]:
            raise ValueError(
                'packed quantity exceeds fixed v1 allocation for %s/%s'
                % (order_id, lot_id))


# --------------------------------------------------------------------------
# Exact rational temperature geometry (structure A)
# --------------------------------------------------------------------------

def _outside_fraction(t0, t1, min_c, max_c):
    """Fraction of parameter u in [0,1] where T(u)=t0+(t1-t0)u is out of band.

    Temperatures and the returned fraction are exact :class:`Fraction`
    values.  T(u) == min_c or T(u) == max_c stays compliant, so crossing
    points themselves are excluded; they are single instants with zero
    duration anyway.
    """
    delta = t1 - t0
    if delta == 0:
        return Fraction(1) if (t0 < min_c or t0 > max_c) else Fraction(0)
    cold = Fraction(0)
    hot = Fraction(0)
    if delta > 0:  # warming
        cold_at = (min_c - t0) / delta
        if t0 < min_c:
            # T < min_c for u < cold_at, clamped to the unit segment.
            cold += min(cold_at, Fraction(1))
        hot_at = (max_c - t0) / delta
        if t1 > max_c:
            # T > max_c for u > hot_at, clamped to the unit segment.
            hot += Fraction(1) - max(hot_at, Fraction(0))
    else:  # cooling, delta < 0
        cold_at = (min_c - t0) / delta
        if t1 < min_c:
            cold += Fraction(1) - max(cold_at, Fraction(0))
        hot_at = (max_c - t0) / delta
        if t0 > max_c:
            hot += min(hot_at, Fraction(1))
    return cold + hot


def evaluate_package(package, policy):
    """Exact exposure/coverage evaluation of one package for one SKU policy.

    Returns disjoint covered segments and unobserved gaps over the closed
    transport window, plus rational minute totals.  Covered segments meet
    only at zero-duration reading instants, so no time is counted twice.
    """
    start, end = package['transit_start'], package['transit_end']
    readings = package['readings']
    gap_limit = timedelta(minutes=policy['max_gap_minutes'])
    min_c = policy['min_c_exact']
    max_c = policy['max_c_exact']

    segments = []
    gaps = []
    excursion = Fraction(0)
    covered = Fraction(0)

    def add_covered(seg_start, seg_end, interpolation, temp_a, temp_b,
                    raw_a, raw_b):
        nonlocal excursion, covered
        if seg_start == seg_end:
            return
        duration = _minutes(seg_end - seg_start)
        outside = _outside_fraction(temp_a, temp_b, min_c, max_c)
        segment_excursion = duration * outside
        excursion += segment_excursion
        covered += duration
        segments.append({
            'start': seg_start,
            'end': seg_end,
            'interpolation': interpolation,
            'start_temp_c': raw_a,
            'end_temp_c': raw_b,
            'excursion_minutes': segment_excursion,
        })

    def add_gap(gap_start, gap_end, reason):
        if gap_start == gap_end:
            return
        gaps.append({'start': gap_start, 'end': gap_end, 'reason': reason})

    first = readings[0]
    # Leading one-sided edge: constant boundary reading, never extrapolated.
    leading = first['at'] - start
    if leading > gap_limit:
        add_gap(start, first['at'], 'leading_edge_gap')
    else:
        add_covered(start, first['at'], 'constant_edge',
                    first['temp_exact'], first['temp_exact'],
                    first['temp_c'], first['temp_c'])

    # Adjacent reading intervals: linear within the gap allowance, otherwise
    # the whole separation stays unobserved and is never interpolated.
    for earlier, later in zip(readings, readings[1:]):
        separation = later['at'] - earlier['at']
        if separation > gap_limit:
            add_gap(earlier['at'], later['at'], 'inter_reading_gap')
        else:
            add_covered(earlier['at'], later['at'], 'linear',
                        earlier['temp_exact'], later['temp_exact'],
                        earlier['temp_c'], later['temp_c'])

    last = readings[-1]
    trailing = end - last['at']
    if trailing > gap_limit:
        add_gap(last['at'], end, 'trailing_edge_gap')
    else:
        add_covered(last['at'], end, 'constant_edge',
                    last['temp_exact'], last['temp_exact'],
                    last['temp_c'], last['temp_c'])

    window = _minutes(end - start)
    gap_minutes = window - covered
    if gap_minutes < 0:
        raise ValueError('internal: covered time exceeds transit window')
    return {
        'segments': segments,
        'gaps': gaps,
        'excursion_minutes': excursion,
        'covered_minutes': covered,
        'gap_minutes': gap_minutes,
        'window_minutes': window,
    }


def _disposition(line, evidence, policy):
    """Decide the fate of a line's received units from exact evidence."""
    if line['received'] == 0:
        return NO_RECEIPT, ['no_units_received']
    threshold = policy['max_excursion_minutes']
    # Provable over-temperature meeting the threshold dominates everything,
    # even when unobserved gaps remain elsewhere in the window.
    if evidence['excursion_minutes'] >= threshold:
        reasons = ['provable_excursion_meets_or_exceeds_threshold']
        if evidence['gaps']:
            reasons.append('unobserved_gap_present')
        return QUARANTINE, reasons
    if evidence['gaps']:
        return UNKNOWN, ['unobserved_gap_exceeds_max_gap_minutes',
                         'provable_excursion_below_threshold']
    return RELEASE, ['coverage_complete',
                     'provable_excursion_below_threshold']


# --------------------------------------------------------------------------
# Report construction
# --------------------------------------------------------------------------

def _iso(moment):
    return moment.isoformat()


def _evidence_json(evidence):
    return {
        'excursion_minutes': _frac_json(evidence['excursion_minutes']),
        'covered_minutes': _frac_json(evidence['covered_minutes']),
        'gap_minutes': _frac_json(evidence['gap_minutes']),
        'window_minutes': _frac_json(evidence['window_minutes']),
        'covered_segments': [
            {'start': _iso(seg['start']),
             'end': _iso(seg['end']),
             'interpolation': seg['interpolation'],
             'start_temp_c': seg['start_temp_c'],
             'end_temp_c': seg['end_temp_c'],
             'excursion_minutes': _frac_json(seg['excursion_minutes'])}
            for seg in evidence['segments']
        ],
        'gaps': [
            {'start': _iso(gap['start']),
             'end': _iso(gap['end']),
             'reason': gap['reason']}
            for gap in evidence['gaps']
        ],
    }


def compile_report(data, source_sha256):
    """Validate, analyze and construct the full v6 cold-chain report."""
    parsed = parse_input(data)
    policies = parsed['policies']
    packages = parsed['packages']
    allocation = parsed['allocation']

    package_by_id = {package['id']: package for package in packages}

    # Evidence depends only on (package, sku); lines of the same SKU sharing a
    # box reuse the identical rational evaluation.
    evidence_cache = {}

    def evidence_for(package, sku):
        key = (package['id'], sku)
        if key not in evidence_cache:
            evidence_cache[key] = evaluate_package(package, policies[sku])
        return evidence_cache[key]

    line_verdicts = []
    packed_by_pair = defaultdict(int)
    short_by_pair = defaultdict(int)
    release_by_pair = defaultdict(int)
    quarantine_by_pair = defaultdict(int)
    unknown_by_pair = defaultdict(int)

    for package in packages:
        for line in package['lines']:
            order_id, lot_id = line['order'], line['lot']
            sku = parsed['lot_sku'][lot_id]
            policy = policies[sku]
            evidence = evidence_for(package, sku)
            disposition, reasons = _disposition(line, evidence, policy)
            short = line['packed'] - line['received']
            packed_by_pair[(order_id, lot_id)] += line['packed']
            short_by_pair[(order_id, lot_id)] += short
            if line['received'] > 0:
                if disposition == RELEASE:
                    release_by_pair[(order_id, lot_id)] += line['received']
                elif disposition == QUARANTINE:
                    quarantine_by_pair[(order_id, lot_id)] += \
                        line['received']
                else:
                    unknown_by_pair[(order_id, lot_id)] += line['received']
            line_verdicts.append({
                'order': order_id,
                'lot': lot_id,
                'package': package['id'],
                'sku': sku,
                'packed': line['packed'],
                'received': line['received'],
                'short_shipped': short,
                'disposition': disposition,
                'reasons': reasons,
                'policy': {
                    'min_c': policy['min_c'],
                    'max_c': policy['max_c'],
                    'max_gap_minutes': policy['max_gap_minutes'],
                    'max_excursion_minutes':
                        policy['max_excursion_minutes'],
                },
                'temperature_evidence': _evidence_json(evidence),
            })

    line_verdicts.sort(key=lambda row: (row['order'], row['lot'],
                                        row['package']))

    # Every positive allocation reconciles exactly, including never-packed
    # remainders; zero allocations and v1 shortages stay visible via the
    # embedded v1 result and the order summary.
    reconciliation = []
    for (order_id, lot_id), quantity in sorted(allocation.items()):
        packed = packed_by_pair.get((order_id, lot_id), 0)
        unshipped = quantity - packed
        row = {
            'order': order_id,
            'lot': lot_id,
            'sku': parsed['lot_sku'][lot_id],
            'allocation': quantity,
            'packed': packed,
            'unshipped': unshipped,
            'short_shipped': short_by_pair.get((order_id, lot_id), 0),
            'received_release': release_by_pair.get((order_id, lot_id), 0),
            'received_quarantine':
                quarantine_by_pair.get((order_id, lot_id), 0),
            'received_unknown': unknown_by_pair.get((order_id, lot_id), 0),
        }
        reconciled = (row['unshipped'] + row['short_shipped']
                      + row['received_release'] + row['received_quarantine']
                      + row['received_unknown'])
        if reconciled != quantity:
            raise ValueError(
                'internal: allocation conservation broken for %s/%s'
                % (order_id, lot_id))
        reconciliation.append(row)

    order_summary = _build_order_summary(parsed, packed_by_pair,
                                         short_by_pair, release_by_pair,
                                         quarantine_by_pair,
                                         unknown_by_pair)

    totals = _build_totals(parsed, reconciliation, packages)

    report = {
        'schema_version': SCHEMA_VERSION,
        'source_sha256': source_sha256,
        'fulfillment': parsed['v1_result'],
        'allocation': [
            {'order': order_id, 'lot': lot_id, 'quantity': quantity}
            for (order_id, lot_id), quantity in sorted(allocation.items())
        ],
        'policies': [
            policies[sku]
            for sku in sorted(policies)
        ],
        'packages': [_package_json(package)
                     for package in sorted(packages, key=lambda p: p['id'])],
        'line_verdicts': line_verdicts,
        'reconciliation': reconciliation,
        'order_summary': order_summary,
        'totals': totals,
    }
    _strip_internal_policy_fields(report)
    return report


def _strip_internal_policy_fields(report):
    for policy in report['policies']:
        policy.pop('min_c_exact', None)
        policy.pop('max_c_exact', None)


def _package_json(package):
    return {
        'id': package['id'],
        'transit_start': _iso(package['transit_start']),
        'transit_end': _iso(package['transit_end']),
        'readings': [
            {'at': _iso(reading['at']), 'temp_c': reading['temp_c']}
            for reading in package['readings']
        ],
        'lines': [
            {'order': line['order'], 'lot': line['lot'],
             'packed': line['packed'], 'received': line['received']}
            for line in sorted(package['lines'],
                               key=lambda row: (row['order'], row['lot']))
        ],
    }


def _build_order_summary(parsed, packed_by_pair, short_by_pair,
                         release_by_pair, quarantine_by_pair,
                         unknown_by_pair):
    v1_by_order = {row['order']: row for row in parsed['v1_result']['orders']}
    summary = []
    for raw_order in sorted(parsed['raw_orders'], key=lambda row: row['id']):
        order_id = raw_order['id']
        v1_row = v1_by_order[order_id]
        packed = sum(qty for (oid, _lot), qty in packed_by_pair.items()
                     if oid == order_id)
        unshipped = sum(
            quantity - packed_by_pair.get((order_id, lot_id), 0)
            for (oid, lot_id), quantity in parsed['allocation'].items()
            if oid == order_id)
        short = sum(qty for (oid, _lot), qty in short_by_pair.items()
                    if oid == order_id)
        released = sum(qty for (oid, _lot), qty in release_by_pair.items()
                       if oid == order_id)
        quarantined = sum(qty for (oid, _lot), qty
                          in quarantine_by_pair.items() if oid == order_id)
        unknown = sum(qty for (oid, _lot), qty in unknown_by_pair.items()
                      if oid == order_id)
        summary.append({
            'order': order_id,
            'sku': raw_order['sku'],
            'requested': v1_row['requested'],
            'fulfilled': v1_row['fulfilled'],
            'shortage': v1_row['shortage'],
            'packed': packed,
            'unshipped': unshipped,
            'short_shipped': short,
            'received_release': released,
            'received_quarantine': quarantined,
            'received_unknown': unknown,
        })
    return summary


def _build_totals(parsed, reconciliation, packages):
    totals = {
        'allocation': sum(row['allocation'] for row in reconciliation),
        'packed': sum(row['packed'] for row in reconciliation),
        'unshipped': sum(row['unshipped'] for row in reconciliation),
        'short_shipped': sum(row['short_shipped']
                             for row in reconciliation),
        'received_release': sum(row['received_release']
                                for row in reconciliation),
        'received_quarantine': sum(row['received_quarantine']
                                   for row in reconciliation),
        'received_unknown': sum(row['received_unknown']
                                for row in reconciliation),
        'package_count': len(packages),
        'line_count': sum(len(package['lines']) for package in packages),
        'reading_count': sum(len(package['readings'])
                             for package in packages),
    }
    conserved = (totals['unshipped'] + totals['short_shipped']
                 + totals['received_release']
                 + totals['received_quarantine']
                 + totals['received_unknown'])
    if conserved != totals['allocation']:
        raise ValueError('internal: global allocation conservation broken')
    received_total = (totals['received_release']
                      + totals['received_quarantine']
                      + totals['received_unknown'])
    if totals['packed'] != totals['short_shipped'] + received_total:
        raise ValueError('internal: packed quantity conservation broken')
    return totals


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------

DESCRIPTION = ('Offline cold-chain arrival release analysis over fixed v1 '
               'fulfillment allocations')


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

    parser = argparse.ArgumentParser(prog='perishable-coldchain',
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
