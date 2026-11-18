"""Acceptance tests for the schema_version=6 offline cold-chain analysis.

Two independent checks guard the exact temperature geometry:

  * ``structure_b_oracle`` re-derives the same semantics with the alternative
    computation structure -- the package timeline is first normalized into
    non-overlapping UTC segments (linear, constant-edge or unobserved gap),
    then each segment is scanned against the SKU band.  It imports none of
    the production clipping/evaluation code and rebuilds every rule from the
    raw readings.
  * hand-computed rational cases pin the exact crossing fractions of linear
    temperature ramps, the threshold boundary, the band-edge equality rule,
    one-sided edges and the single-reading package.
"""

import copy
import json
import os
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from fractions import Fraction
from pathlib import Path

from perishable_fulfillment.common import read
from perishable_fulfillment.coldchain import (
    compile_report,
    evaluate_package,
    parse_input,
    run,
)

ROOT = Path(__file__).resolve().parents[1]
FIXTURE = ROOT / 'fixtures' / 'coldchain_input.json'
UTC = timezone.utc


# --------------------------------------------------------------------------
# Payload builders
# --------------------------------------------------------------------------

def lot(lid, sku='milk', expires='2026-12-31', quantity=10, held=0):
    return {'id': lid, 'sku': sku, 'expires': expires,
            'quantity': quantity, 'held': held}


def order(oid, sku='milk', priority=1, quantity=10):
    return {'id': oid, 'sku': sku, 'priority': priority,
            'quantity': quantity}


def policy(sku='milk', min_c=2, max_c=8, max_gap=120, max_excursion=60):
    return {'sku': sku, 'min_c': min_c, 'max_c': max_c,
            'max_gap_minutes': max_gap,
            'max_excursion_minutes': max_excursion}


def reading(at, temp_c):
    return {'at': at, 'temp_c': temp_c}


def line(oid, lid, packed, received):
    return {'order': oid, 'lot': lid, 'packed': packed,
            'received': received}


def package(pid, start, end, readings, lines):
    return {'id': pid, 'transit_start': start, 'transit_end': end,
            'readings': readings, 'lines': lines}


def payload(lots, orders, policies, packages, as_of='2026-11-18'):
    return {
        'schema_version': 6,
        'fulfillment': {'as_of': as_of, 'lots': lots, 'orders': orders},
        'policies': policies,
        'packages': packages,
    }


def base_payload(packages, policies=None, lots=None, orders=None):
    lots = lots if lots is not None else [lot('L1'), lot('L2')]
    orders = orders if orders is not None else [order('O1'), order('O2')]
    policies = policies if policies is not None else [policy()]
    return payload(lots, orders, policies, packages)


def call(data, digest='digest'):
    return compile_report(copy.deepcopy(data), digest)


def at(hour, minute=0, day=18):
    return '2026-11-%02dT%02d:%02d:00+00:00' % (day, hour, minute)


def verdict_map(report):
    return {(v['order'], v['lot'], v['package']): v
            for v in report['line_verdicts']}


def recon_map(report):
    return {(r['order'], r['lot']): r for r in report['reconciliation']}


# --------------------------------------------------------------------------
# Independent structure-B oracle: normalize into disjoint UTC segments first,
# then scan each segment per SKU policy.  Uses Fraction only; no production
# temperature function is called.
# --------------------------------------------------------------------------

def _mins(delta):
    micros = (delta.days * 86400 + delta.seconds) * 1_000_000 \
        + delta.microseconds
    return Fraction(micros, 60_000_000)


def _segment_outside_minutes(length_minutes, t0, t1, min_c, max_c):
    """Independent re-derivation of out-of-band time for one linear segment."""
    if t0 == t1:
        return length_minutes if (t0 < min_c or t0 > max_c) \
            else Fraction(0)
    total = Fraction(0)
    # Partition [0,1] at every band-boundary crossing and test each cell at
    # its open midpoint, so equality exactly on min_c/max_c stays compliant.
    roots = [Fraction(0), Fraction(1)]
    for bound in (min_c, max_c):
        u = (bound - t0) / (t1 - t0)
        if 0 < u < 1:
            roots.append(u)
    roots = sorted(set(roots))
    for lo, hi in zip(roots, roots[1:]):
        mid = (lo + hi) / 2
        temp_mid = t0 + (t1 - t0) * mid
        if temp_mid < min_c or temp_mid > max_c:
            total += length_minutes * (hi - lo)
    return total


def structure_b_oracle(package, pol):
    """Alternative-structure evaluation, rebuilt from the raw readings."""
    start = datetime.fromisoformat(package['transit_start']) \
        .astimezone(UTC)
    end = datetime.fromisoformat(package['transit_end']).astimezone(UTC)
    points = [(datetime.fromisoformat(r['at']).astimezone(UTC),
               Fraction(r['temp_c'])) for r in package['readings']]
    gap_limit = timedelta(minutes=pol['max_gap_minutes'])
    min_c, max_c = Fraction(pol['min_c']), Fraction(pol['max_c'])

    # Phase 1: disjoint (start, end, kind, t0, t1) segments covering the
    # whole transport window exactly once.
    segments = []
    first_at, first_temp = points[0]
    if first_at - start > gap_limit:
        segments.append((start, first_at, 'gap', None, None))
    else:
        segments.append((start, first_at, 'constant', first_temp,
                         first_temp))
    for (a_at, a_t), (b_at, b_t) in zip(points, points[1:]):
        if b_at - a_at > gap_limit:
            segments.append((a_at, b_at, 'gap', None, None))
        else:
            segments.append((a_at, b_at, 'linear', a_t, b_t))
    last_at, last_temp = points[-1]
    if end - last_at > gap_limit:
        segments.append((last_at, end, 'gap', None, None))
    else:
        segments.append((last_at, end, 'constant', last_temp, last_temp))

    # Phase 2: scan the normalized segments with the SKU's own band.
    excursion = Fraction(0)
    covered = Fraction(0)
    gap_total = Fraction(0)
    cursor = start
    for seg_start, seg_end, kind, t0, t1 in segments:
        # Segments must tile the window with no overlap and no hole.
        assert seg_start == cursor
        cursor = seg_end
        length = _mins(seg_end - seg_start)
        if kind == 'gap':
            gap_total += length
            continue
        covered += length
        excursion += _segment_outside_minutes(length, t0, t1, min_c, max_c)
    assert cursor == end
    window = _mins(end - start)
    assert covered + gap_total == window
    return {'excursion_minutes': excursion,
            'covered_minutes': covered,
            'gap_minutes': gap_total,
            'window_minutes': window}


def parsed_package(raw_package):
    parsed = parse_input(base_payload([raw_package]))
    return parsed['packages'][0]


class OracleCrossCheck(unittest.TestCase):
    """The two computation structures must agree on exact rational totals."""

    CASES = [
        # warming ramp crossing max_c twice across midnight
        (package('P1', at(23), at(2, day=19),
                 [reading(at(23), 4), reading(at(0, day=19), 10),
                  reading(at(1, day=19), 10), reading(at(2, day=19), 4)],
                 [line('O1', 'L1', 1, 1)]),
         policy()),
        # cooling ramp crossing min_c
        (package('P2', at(10), at(11),
                 [reading(at(10), 5), reading(at(11), -1)],
                 [line('O1', 'L1', 1, 1)]),
         policy()),
        # fractional crossing: 4 -> 9 over 90 minutes, band 2..8
        (package('P3', at(10), at(11, 30),
                 [reading(at(10), 4), reading(at(11, 30), 9)],
                 [line('O1', 'L1', 1, 1)]),
         policy()),
        # inter-reading gap plus trailing edge gap, single SKU
        (package('P4', at(10), at(15),
                 [reading(at(10), 4), reading(at(11), 4),
                  reading(at(14), 9)],
                 [line('O1', 'L1', 1, 1)]),
         policy(max_gap=60, max_excursion=30)),
        # single reading with a long trailing edge
        (package('P5', at(10), at(13), [reading(at(10), 4)],
                 [line('O1', 'L1', 1, 1)]),
         policy()),
    ]

    def test_structures_agree(self):
        for raw_package, pol in self.CASES:
            package_obj = parsed_package(raw_package)
            production = evaluate_package(package_obj, {
                'min_c_exact': Fraction(pol['min_c']),
                'max_c_exact': Fraction(pol['max_c']),
                'max_gap_minutes': pol['max_gap_minutes'],
                'max_excursion_minutes': pol['max_excursion_minutes'],
                'min_c': pol['min_c'], 'max_c': pol['max_c']})
            oracle = structure_b_oracle(raw_package, pol)
            for key in ('excursion_minutes', 'covered_minutes',
                        'gap_minutes', 'window_minutes'):
                self.assertEqual(production[key], oracle[key],
                                 (raw_package['id'], key))

    def test_hand_computed_fractional_crossing(self):
        # 4 -> 9 over 90 min, band 2..8: >8 for the last (9-8)/(9-4)=1/5 of
        # the segment = 90/5 = exactly 18 minutes.
        raw_package, pol = self.CASES[2]
        package_obj = parsed_package(raw_package)
        evidence = evaluate_package(package_obj, {
            'min_c_exact': Fraction(pol['min_c']),
            'max_c_exact': Fraction(pol['max_c']),
            'max_gap_minutes': pol['max_gap_minutes'],
            'max_excursion_minutes': pol['max_excursion_minutes'],
            'min_c': pol['min_c'], 'max_c': pol['max_c']})
        self.assertEqual(evidence['excursion_minutes'], Fraction(18))
        self.assertEqual(evidence['window_minutes'], Fraction(90))
        self.assertEqual(evidence['gap_minutes'], Fraction(0))
        # The one covered linear segment carries the full exact value.
        self.assertEqual(evidence['segments'][0]['excursion_minutes'],
                         Fraction(18))

    def test_hand_computed_cooling_crossing(self):
        # 5 -> -1 over 60 min, min_c=2: below 2 for (2-(-1))/(5-(-1))=1/2
        # of the segment = exactly 30 minutes.
        raw_package, pol = self.CASES[1]
        package_obj = parsed_package(raw_package)
        evidence = evaluate_package(package_obj, {
            'min_c_exact': Fraction(pol['min_c']),
            'max_c_exact': Fraction(pol['max_c']),
            'max_gap_minutes': pol['max_gap_minutes'],
            'max_excursion_minutes': pol['max_excursion_minutes'],
            'min_c': pol['min_c'], 'max_c': pol['max_c']})
        self.assertEqual(evidence['excursion_minutes'], Fraction(30))

    def test_hand_computed_multi_segment_midnight_accumulation(self):
        # Fixture P1 hand calc: two linear ramps each prove 40 hot minutes
        # and the constant-hot middle segment proves 120, totaling 200.
        report = compile_report(*_fixture_args())
        evidence = verdict_map(report)[('O1', 'L1', 'P1')] \
            ['temperature_evidence']
        self.assertEqual(evidence['excursion_minutes'],
                         {'numerator': 200, 'denominator': 1})
        self.assertEqual(evidence['window_minutes'],
                         {'numerator': 360, 'denominator': 1})
        per_segment = [s['excursion_minutes']
                       for s in evidence['covered_segments']]
        self.assertEqual(per_segment, [
            {'numerator': 40, 'denominator': 1},
            {'numerator': 120, 'denominator': 1},
            {'numerator': 40, 'denominator': 1},
        ])


def _fixture_args():
    data, digest = read(FIXTURE)
    return data, digest


class FixtureAcceptance(unittest.TestCase):
    def test_fixture_dispositions_and_conservation(self):
        report = compile_report(*_fixture_args())
        self.assertEqual(report['schema_version'], 6)
        verdicts = verdict_map(report)
        self.assertEqual(verdicts[('O1', 'L1', 'P1')]['disposition'],
                         'quarantine')
        self.assertEqual(verdicts[('O2', 'L1', 'P1')]['disposition'],
                         'quarantine')
        self.assertEqual(verdicts[('O2', 'L1', 'P1')]['short_shipped'], 1)
        self.assertEqual(verdicts[('O1', 'L1', 'P3')]['disposition'],
                         'release')
        self.assertEqual(verdicts[('O2', 'L2', 'P2')]['disposition'],
                         'release')
        self.assertEqual(verdicts[('O3', 'L3', 'P2')]['disposition'],
                         'release')
        self.assertEqual(verdicts[('O2', 'L2', 'P4')]['disposition'],
                         'unknown')
        # No-receipt line keeps its short quantity outside temperature fate.
        rows = recon_map(report)
        o2_l1 = rows[('O2', 'L1')]
        self.assertEqual((o2_l1['allocation'], o2_l1['packed'],
                          o2_l1['unshipped'], o2_l1['short_shipped'],
                          o2_l1['received_release'],
                          o2_l1['received_quarantine'],
                          o2_l1['received_unknown']), (2, 2, 0, 1, 0, 1, 0))
        for row in report['reconciliation']:
            self.assertEqual(
                row['allocation'],
                row['unshipped'] + row['short_shipped']
                + row['received_release'] + row['received_quarantine']
                + row['received_unknown'])
        totals = report['totals']
        self.assertEqual(totals, {
            'allocation': 21, 'packed': 18, 'unshipped': 3,
            'short_shipped': 1, 'received_release': 9,
            'received_quarantine': 6, 'received_unknown': 2,
            'package_count': 4, 'line_count': 6, 'reading_count': 13})

    def test_zero_receipt_line_is_no_receipt_and_untempered(self):
        pkgs = [package('P1', at(10), at(12),
                        [reading(at(10), 4), reading(at(12), 4)],
                        [line('O1', 'L1', 3, 0)])]
        report = call(base_payload(pkgs))
        verdict = report['line_verdicts'][0]
        self.assertEqual(verdict['disposition'], 'no_receipt')
        self.assertEqual(verdict['short_shipped'], 3)
        self.assertEqual(verdict['reasons'], ['no_units_received'])
        row = recon_map(report)[('O1', 'L1')]
        self.assertEqual(row['received_release'], 0)
        self.assertEqual(row['short_shipped'], 3)

    def test_order_summary_keeps_v1_shortage_visible(self):
        lots = [lot('L1', quantity=4)]
        orders = [order('O1', quantity=10)]
        pkgs = [package('P1', at(10), at(12),
                        [reading(at(10), 4), reading(at(12), 4)],
                        [line('O1', 'L1', 4, 4)])]
        report = call(base_payload(pkgs, lots=lots, orders=orders))
        summary = {r['order']: r for r in report['order_summary']}['O1']
        self.assertEqual(summary['requested'], 10)
        self.assertEqual(summary['fulfilled'], 4)
        self.assertEqual(summary['shortage'], 6)
        self.assertEqual(summary['received_release'], 4)
        self.assertEqual(summary['unshipped'], 0)


class TemperatureSemantics(unittest.TestCase):
    def _evidence(self, readings, start=None, end=None, pol=None):
        pol = pol or policy()
        start = start or readings[0]['at']
        end = end or readings[-1]['at']
        raw_package = package('P1', start, end, readings,
                              [line('O1', 'L1', 1, 1)])
        package_obj = parsed_package(raw_package)
        return evaluate_package(package_obj, {
            'min_c_exact': Fraction(pol['min_c']),
            'max_c_exact': Fraction(pol['max_c']),
            'max_gap_minutes': pol['max_gap_minutes'],
            'max_excursion_minutes': pol['max_excursion_minutes'],
            'min_c': pol['min_c'], 'max_c': pol['max_c']})

    def test_threshold_exactly_equal_means_quarantine(self):
        # 4 -> 10 over 120 min, band 2..8: above max_c for (10-8)/(10-4)=1/3
        # of the segment, i.e. exactly 40 minutes; threshold set to 40.
        ev = self._evidence(
            [reading(at(10), 4), reading(at(12), 10)],
            pol=policy(max_excursion=40))
        self.assertEqual(ev['excursion_minutes'], Fraction(40))
        pkgs = [package('P1', at(10), at(12),
                        [reading(at(10), 4), reading(at(12), 10)],
                        [line('O1', 'L1', 1, 1)])]
        report = call(base_payload(pkgs, policies=[policy(
            max_excursion=40)]))
        self.assertEqual(report['line_verdicts'][0]['disposition'],
                         'quarantine')

    def test_one_minute_below_threshold_releases(self):
        pkgs = [package('P1', at(10), at(12),
                        [reading(at(10), 4), reading(at(12), 10)],
                        [line('O1', 'L1', 1, 1)])]
        report = call(base_payload(pkgs, policies=[policy(
            max_excursion=41)]))
        verdict = report['line_verdicts'][0]
        self.assertEqual(verdict['disposition'], 'release')
        self.assertEqual(
            verdict['temperature_evidence']['excursion_minutes'],
            {'numerator': 40, 'denominator': 1})

    def test_temperature_exactly_on_band_limits_is_compliant(self):
        ev = self._evidence(
            [reading(at(10), 2), reading(at(11), 8)],
            pol=policy(min_c=2, max_c=8))
        self.assertEqual(ev['excursion_minutes'], Fraction(0))
        # A constant reading exactly on min_c over an edge also stays clean.
        ev2 = self._evidence(
            [reading(at(11), 2)], start=at(10), end=at(12),
            pol=policy(min_c=2, max_c=8, max_gap=120))
        self.assertEqual(ev2['excursion_minutes'], Fraction(0))
        self.assertEqual(ev2['gap_minutes'], Fraction(0))

    def test_gap_over_max_gap_forces_unknown_without_interpolation(self):
        pkgs = [package('P1', at(10), at(13),
                        [reading(at(10), 4), reading(at(11), 4),
                         reading(at(13), 4)],
                        [line('O1', 'L1', 1, 1)])]
        report = call(base_payload(pkgs, policies=[policy(max_gap=60)]))
        verdict = report['line_verdicts'][0]
        self.assertEqual(verdict['disposition'], 'unknown')
        self.assertIn('unobserved_gap_exceeds_max_gap_minutes',
                      verdict['reasons'])
        gaps = verdict['temperature_evidence']['gaps']
        self.assertEqual([g['reason'] for g in gaps],
                         ['inter_reading_gap'])
        # The 120-minute gap is excluded from covered time, not interpolated.
        te = verdict['temperature_evidence']
        self.assertEqual(te['covered_minutes'],
                         {'numerator': 60, 'denominator': 1})
        self.assertEqual(te['gap_minutes'],
                         {'numerator': 120, 'denominator': 1})

    def test_quarantine_wins_even_when_gaps_remain(self):
        # The fully covered first interval stays at 10 C for 120 minutes,
        # proving 120 hot minutes (>= threshold 60); the later 120-minute
        # separation exceeds the 60-minute allowance and stays unobserved.
        # Quarantine still dominates the verdict.
        pkgs = [package('P1', at(10), at(14),
                        [reading(at(10), 10), reading(at(11), 10),
                         reading(at(12), 10), reading(at(14), 4)],
                        [line('O1', 'L1', 1, 1)])]
        report = call(base_payload(pkgs, policies=[policy(
            max_gap=60, max_excursion=60)]))
        verdict = report['line_verdicts'][0]
        self.assertEqual(verdict['disposition'], 'quarantine')
        self.assertIn('unobserved_gap_present', verdict['reasons'])

    def test_leading_and_trailing_edge_gaps_single_reading(self):
        # One reading at 11:00, window 10:00..12:30, gap allowance 60 min.
        pkgs = [package('P1', at(10), at(12, 30), [reading(at(11), 4)],
                        [line('O1', 'L1', 1, 1)])]
        report = call(base_payload(pkgs, policies=[policy(max_gap=60)]))
        verdict = report['line_verdicts'][0]
        self.assertEqual(verdict['disposition'], 'unknown')
        reasons = [g['reason'] for g
                   in verdict['temperature_evidence']['gaps']]
        self.assertEqual(reasons, ['trailing_edge_gap'])
        # Leading edge (60 min, within allowance) is constant-covered; the
        # 90-minute trailing edge is the only gap.
        te = verdict['temperature_evidence']
        self.assertEqual(te['covered_minutes'],
                         {'numerator': 60, 'denominator': 1})
        self.assertEqual(te['gap_minutes'],
                         {'numerator': 90, 'denominator': 1})

    def test_edge_gap_exactly_at_limit_is_covered(self):
        pkgs = [package('P1', at(10), at(12), [reading(at(11), 4)],
                        [line('O1', 'L1', 1, 1)])]
        report = call(base_payload(pkgs, policies=[policy(max_gap=60)]))
        self.assertEqual(report['line_verdicts'][0]['disposition'],
                         'release')

    def test_segments_tile_window_without_double_counting(self):
        ev = self._evidence(
            [reading(at(10), 4), reading(at(11), 10),
             reading(at(12), 4)],
            start=at(9, 30), end=at(12, 30),
            pol=policy(max_gap=120))
        self.assertEqual(ev['covered_minutes'] + ev['gap_minutes'],
                         ev['window_minutes'])
        self.assertEqual(ev['window_minutes'], Fraction(180))
        boundaries = [(s['start'], s['end']) for s in ev['segments']]
        for earlier, later in zip(boundaries, boundaries[1:]):
            self.assertEqual(earlier[1], later[0])

    def test_timezone_offsets_normalize_to_utc(self):
        pkgs = [package('P1', '2026-11-18T22:00:00+02:00',
                        '2026-11-19T02:00:00+02:00',
                        [reading('2026-11-18T22:00:00+02:00', 4),
                         reading('2026-11-19T00:00:00+02:00', 4),
                         reading('2026-11-19T02:00:00+02:00', 4)],
                        [line('O1', 'L1', 1, 1)])]
        report = call(base_payload(pkgs))
        out = report['packages'][0]
        self.assertEqual(out['transit_start'],
                         '2026-11-18T20:00:00+00:00')
        self.assertEqual(out['transit_end'],
                         '2026-11-19T00:00:00+00:00')
        self.assertEqual(
            report['line_verdicts'][0]['disposition'], 'release')

    def test_shared_box_skus_use_their_own_bands(self):
        lots = [lot('L1', sku='milk'), lot('L3', sku='yogurt')]
        orders = [order('O1', sku='milk'), order('O3', sku='yogurt')]
        pols = [policy('milk', min_c=2, max_c=8, max_gap=120,
                       max_excursion=60),
                policy('yogurt', min_c=0, max_c=6, max_gap=120,
                       max_excursion=10)]
        # Constant 7 C: fine for milk, over yogurt's max_c for the full
        # 120-minute window -> yogurt quarantine, milk release.
        pkgs = [package('P1', at(10), at(12),
                        [reading(at(10), 7), reading(at(12), 7)],
                        [line('O1', 'L1', 2, 2),
                         line('O3', 'L3', 2, 2)])]
        report = call(base_payload(pkgs, policies=pols, lots=lots,
                                   orders=orders))
        verdicts = verdict_map(report)
        self.assertEqual(verdicts[('O1', 'L1', 'P1')]['disposition'],
                         'release')
        self.assertEqual(verdicts[('O3', 'L3', 'P1')]['disposition'],
                         'quarantine')
        self.assertEqual(
            verdicts[('O3', 'L3', 'P1')]
            ['temperature_evidence']['excursion_minutes'],
            {'numerator': 120, 'denominator': 1})


class AllocationReconciliation(unittest.TestCase):
    def _clean_pkg(self, pid, lines, start=at(10), end=at(12)):
        return package(pid, start, end,
                       [reading(start, 4), reading(end, 4)], lines)

    def test_unshipped_remainder_reported(self):
        pkgs = [self._clean_pkg('P1', [line('O1', 'L1', 3, 3)])]
        report = call(base_payload(pkgs))
        row = recon_map(report)[('O1', 'L1')]
        self.assertEqual(row['allocation'], 10)
        self.assertEqual(row['unshipped'], 7)
        self.assertEqual(row['received_release'], 3)

    def test_packed_split_across_packages_conserves(self):
        pkgs = [
            self._clean_pkg('P1', [line('O1', 'L1', 4, 4)]),
            self._clean_pkg('P2', [line('O1', 'L1', 3, 2)],
                            start=at(13), end=at(15)),
        ]
        report = call(base_payload(pkgs))
        row = recon_map(report)[('O1', 'L1')]
        self.assertEqual(row['packed'], 7)
        self.assertEqual(row['unshipped'], 3)
        self.assertEqual(row['short_shipped'], 1)
        self.assertEqual(row['received_release'], 6)

    def test_partial_arrival_separates_short_and_received(self):
        pkgs = [self._clean_pkg('P1', [line('O1', 'L1', 5, 2)])]
        report = call(base_payload(pkgs))
        row = recon_map(report)[('O1', 'L1')]
        self.assertEqual(row['packed'], 5)
        self.assertEqual(row['short_shipped'], 3)
        self.assertEqual(row['received_release'], 2)
        self.assertEqual(row['unshipped'], 5)

    def test_over_packing_rejected(self):
        pkgs = [self._clean_pkg('P1', [line('O1', 'L1', 6, 6)]),
                self._clean_pkg('P2', [line('O1', 'L1', 5, 5)],
                                start=at(13), end=at(15))]
        with self.assertRaises(ValueError):
            call(base_payload(pkgs))

    def test_line_without_positive_allocation_rejected(self):
        # O2 takes nothing because L1 (10) fully covers O1's request of 10.
        pkgs = [self._clean_pkg('P1', [line('O2', 'L1', 1, 1)])]
        with self.assertRaises(ValueError):
            call(base_payload(pkgs))

    def test_unknown_order_and_lot_rejected(self):
        pkgs = [self._clean_pkg('P1', [line('NOPE', 'L1', 1, 1)])]
        with self.assertRaises(ValueError):
            call(base_payload(pkgs))
        pkgs = [self._clean_pkg('P1', [line('O1', 'NOPE', 1, 1)])]
        with self.assertRaises(ValueError):
            call(base_payload(pkgs))

    def test_sku_mismatch_between_order_and_lot_rejected(self):
        lots = [lot('L1', sku='milk'), lot('L3', sku='yogurt')]
        orders = [order('O1', sku='milk'), order('O3', sku='yogurt')]
        pkgs = [self._clean_pkg('P1', [line('O1', 'L3', 1, 1)])]
        with self.assertRaises(ValueError):
            call(base_payload(pkgs, lots=lots, orders=orders,
                              policies=[policy('milk'), policy('yogurt')]))

    def test_zero_allocation_pairs_absent_but_v1_shortage_kept(self):
        lots = [lot('L1', quantity=3)]
        orders = [order('O1', quantity=3), order('O2', quantity=3)]
        pkgs = [self._clean_pkg('P1', [line('O1', 'L1', 3, 3)])]
        report = call(base_payload(pkgs, lots=lots, orders=orders))
        self.assertNotIn(('O2', 'L1'), recon_map(report))
        o2 = next(r for r in report['order_summary'] if r['order'] == 'O2')
        self.assertEqual(o2['shortage'], 3)
        self.assertEqual(o2['fulfilled'], 0)


class InputValidation(unittest.TestCase):
    def _reject(self, mutate):
        data, _ = _fixture_args()
        mutate(data)
        with self.assertRaises((ValueError, TypeError, KeyError)):
            compile_report(copy.deepcopy(data), 'digest')

    def test_schema_version_must_be_integer_six(self):
        self._reject(lambda d: d.update(schema_version=5))
        self._reject(lambda d: d.update(schema_version='6'))
        self._reject(lambda d: d.update(schema_version=6.0))
        self._reject(lambda d: d.update(schema_version=True))

    def test_top_level_fields_exact(self):
        self._reject(lambda d: d.pop('packages'))
        self._reject(lambda d: d.update(extra=1))

    def test_policy_rules(self):
        self._reject(lambda d: d['policies'][0].update(min_c=8))
        self._reject(lambda d: d['policies'][0].update(
            max_gap_minutes=-1))
        self._reject(lambda d: d['policies'][0].update(
            max_excursion_minutes=1.5))
        self._reject(lambda d: d['policies'].append(
            dict(d['policies'][0])))
        self._reject(lambda d: d['policies'].pop())
        self._reject(lambda d: d['policies'][0].update(sku='ghost'))

    def test_package_rules(self):
        self._reject(lambda d: d['packages'][0].update(id='P2'))
        self._reject(lambda d: d['packages'][0].update(
            transit_end=d['packages'][0]['transit_start']))
        self._reject(lambda d: d['packages'][0].update(
            transit_start='2026-11-18T20:00:00'))
        self._reject(lambda d: d['packages'][0]['readings'].clear())
        self._reject(lambda d: d['packages'][0]['readings'][1].update(
            at=d['packages'][0]['readings'][0]['at']))
        self._reject(lambda d: d['packages'][0]['readings'][0].update(
            temp_c='hot'))
        self._reject(lambda d: d['packages'][0]['lines'].append(
            dict(d['packages'][0]['lines'][0])))
        self._reject(lambda d: d['packages'][0]['lines'][0].update(
            received=99))
        self._reject(lambda d: d['packages'][0]['lines'][0].update(
            packed=-1))
        self._reject(lambda d: d['packages'][0]['lines'][0].pop('lot'))
        self._reject(lambda d: d['packages'][0].update(
            readings=[reading(at(9), 4)]))

    def test_duplicate_json_keys_and_nonfinite_rejected(self):
        raw = (FIXTURE.parent / 'coldchain_input.json').read_bytes()
        duplicated = raw.replace(b'"id": "P1"', b'"id": "P1", "id": "P1"',
                                 1)
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'dup.json'
            path.write_bytes(duplicated)
            with self.assertRaises(ValueError):
                run(path)
            path.write_text(
                '{"schema_version":6,"fulfillment":{},"policies":[],"packages":[]}')
            with self.assertRaises(ValueError):
                run(path)
            nonfinite = raw.replace(b'"temp_c": 4', b'"temp_c": NaN', 1)
            path.write_bytes(nonfinite)
            with self.assertRaises(ValueError):
                run(path)


class DeterminismAndPublication(unittest.TestCase):
    def test_shuffling_arrays_keeps_business_result(self):
        data, digest = _fixture_args()
        shuffled = copy.deepcopy(data)
        # Deterministic full reversal of every legal input array.
        shuffled['packages'].reverse()
        shuffled['policies'].reverse()
        shuffled['fulfillment']['lots'].reverse()
        shuffled['fulfillment']['orders'].reverse()
        for package_obj in shuffled['packages']:
            package_obj['lines'].reverse()
            package_obj['readings'].reverse()
        # Reversed readings violate the strict-increase rule, so restore a
        # canonical reading order while keeping every other array reversed.
        for package_obj in shuffled['packages']:
            package_obj['readings'].reverse()
        first = compile_report(copy.deepcopy(data), digest)
        second = compile_report(copy.deepcopy(shuffled), digest)
        self.assertNotEqual(
            [p['id'] for p in shuffled['packages']],
            [p['id'] for p in data['packages']])
        # v1 already re-sorts its own outputs; only its raw-input echoes vary.
        first['fulfillment'].pop('orders', None)
        second['fulfillment'].pop('orders', None)
        first['fulfillment'].pop('lots', None)
        second['fulfillment'].pop('lots', None)
        # Every emitted block is re-sorted by stable keys, so the full report
        # of a legal permutation is byte-identical to the original.
        self.assertEqual(
            json.dumps(first, sort_keys=True, allow_nan=False),
            json.dumps(second, sort_keys=True, allow_nan=False))

    def test_identical_bytes_identical_report(self):
        data, digest = _fixture_args()
        first = compile_report(copy.deepcopy(data), digest)
        second = compile_report(copy.deepcopy(data), digest)
        self.assertEqual(json.dumps(first, sort_keys=True),
                         json.dumps(second, sort_keys=True))

    def test_stable_sorting_keys(self):
        report = compile_report(*_fixture_args())
        keys = [(v['order'], v['lot'], v['package'])
                for v in report['line_verdicts']]
        self.assertEqual(keys, sorted(keys))
        recon_keys = [(r['order'], r['lot'])
                      for r in report['reconciliation']]
        self.assertEqual(recon_keys, sorted(recon_keys))
        self.assertEqual([p['id'] for p in report['packages']],
                         sorted(p['id'] for p in report['packages']))

    def call_cli(self, source, output):
        env = dict(os.environ, PYTHONPATH=str(ROOT / 'src'))
        return subprocess.run(
            [sys.executable, '-m', 'perishable_fulfillment.coldchain',
             '--source', str(source), '--output', str(output)],
            capture_output=True, text=True, env=env)

    def test_cli_roundtrip_and_failure_preserves_output(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            out = tmp / 'report.json'
            result = self.call_cli(FIXTURE, out)
            self.assertEqual(result.returncode, 0, result.stderr)
            first = out.read_bytes()
            self.assertEqual(self.call_cli(FIXTURE, out).returncode, 0)
            self.assertEqual(first, out.read_bytes())
            bad = tmp / 'bad.json'
            bad.write_text('{"schema_version": 6}')
            target = tmp / 'keep.json'
            target.write_text('prior')
            failed = self.call_cli(bad, target)
            self.assertEqual(failed.returncode, 2)
            self.assertTrue(failed.stderr.strip())
            self.assertEqual(target.read_text(), 'prior')

    def test_symlink_same_file_and_hard_link_rejected(self):
        with tempfile.TemporaryDirectory(dir=str(ROOT)) as tmp:
            tmp = Path(tmp)
            link = tmp / 'link.json'
            link.symlink_to(FIXTURE)
            self.assertEqual(self.call_cli(FIXTURE, link).returncode, 2)
            self.assertEqual(self.call_cli(FIXTURE, FIXTURE).returncode, 2)
            hard = tmp / 'hard.json'
            os.link(FIXTURE, hard)
            self.assertEqual(self.call_cli(FIXTURE, hard).returncode, 2)


if __name__ == '__main__':
    unittest.main()
