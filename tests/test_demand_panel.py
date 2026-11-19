"""Acceptance tests for the schema_version=8 offline demand training panel.

The hand-computed fixture (``fixtures/demand_panel_input.json``) spans
2026-11-20..26 with a two-day lookback and a two-day horizon.  Its only
day-23 shelf witness is a full-day release interval that is not visible
until 2026-11-27T00:00Z, so the two evidence cutoffs 25T00 and 27T00 pin the
point-in-time semantics independently of any production helper: every
expected number below is derived by hand from the contract rather than from
the implementation under test.
"""

import copy
import hashlib
import json
import os
import subprocess
import sys
import tempfile
import unittest
from datetime import date, datetime, timezone
from pathlib import Path

from perishable_fulfillment.common import parse_json
from perishable_fulfillment.demand_panel import (
    DROP_WINDOW,
    MASK_DAYS,
    CoverageIndex,
    compile_report,
    parse_input,
    run,
)

ROOT = Path(__file__).resolve().parents[1]
FIXTURE = ROOT / 'fixtures' / 'demand_panel_input.json'
UTC = timezone.utc


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------

def load_base():
    raw = FIXTURE.read_bytes()
    return parse_json(raw), hashlib.sha256(raw).hexdigest()


def panel(data, digest, evidence_as_of, policy=MASK_DAYS):
    payload = copy.deepcopy(data)
    payload['evidence_as_of'] = evidence_as_of
    payload['censored_target_policy'] = policy
    return compile_report(payload, digest)


def calendar_map(report):
    return {(row['date']): row for row in report['calendar']}


def sample_map(report):
    return {sample['anchor_date']: sample for sample in report['samples']}


def minute(value):
    return datetime.strptime(value, '%Y-%m-%dT%H:%M:%SZ').replace(tzinfo=UTC)


DAY23 = '2026-11-23'
EARLY = '2026-11-25T00:00:00Z'
LATE = '2026-11-27T00:00:00Z'


# --------------------------------------------------------------------------
# Fixture acceptance: hand-computed window and calendar expectations
# --------------------------------------------------------------------------

class FixtureAcceptance(unittest.TestCase):
    def setUp(self):
        self.data, self.digest = load_base()

    def test_report_envelope(self):
        report = panel(self.data, self.digest, LATE)
        self.assertEqual(report['schema_version'], 8)
        self.assertEqual(report['source_sha256'], self.digest)
        self.assertEqual(set(report),
                         {'schema_version', 'source_sha256', 'policy',
                          'calendar', 'summary', 'samples'})
        self.assertEqual(report['policy'], {
            'calendar_start': '2026-11-20',
            'cutoff_date': '2026-11-26',
            'split_date': '2026-11-22',
            'lookback_days': 2,
            'horizon_days': 2,
            'evidence_as_of': LATE,
            'censored_target_policy': MASK_DAYS,
        })

    def test_anchor_inventory_hand_counts(self):
        # Seven anchors total: three fall outside the combined windows and
        # four are in-bounds; anchor 21 straddles the 22 split.
        for policy in (MASK_DAYS, DROP_WINDOW):
            report = panel(self.data, self.digest, EARLY, policy)
            summary = report['summary'][0]
            self.assertEqual(summary['boundary_excluded'], 3)
            self.assertEqual(summary['in_bounds_candidates'], 4)
            self.assertEqual(summary['cross_split_excluded'], 1)

    def test_early_cutoff_mask_policy(self):
        report = panel(self.data, self.digest, EARLY, MASK_DAYS)
        row23 = calendar_map(report)[DAY23]
        # The release witness is still invisible: null coverage, but the
        # explicit zero POS sale is retained.
        self.assertIsNone(row23['in_stock_minutes'])
        self.assertEqual(row23['sold_units'], 0)
        self.assertTrue(row23['observed'])
        self.assertFalse(row23['uncensored'])

        anchor22 = sample_map(report)['2026-11-22']
        self.assertEqual(anchor22['split'], 'test')
        target23, target24 = anchor22['target_days']
        self.assertIsNone(target23['label'])
        self.assertFalse(target23['valid'])
        self.assertEqual(target24['label'], 5)
        self.assertTrue(target24['valid'])
        # mask_days never produces policy exclusions.
        summary = report['summary'][0]
        self.assertEqual(summary['dropped_missing_pos'], 0)
        self.assertEqual(summary['dropped_observed_incomplete'], 0)
        self.assertEqual(summary['test_samples'], 3)
        self.assertEqual(summary['train_samples'], 0)

    def test_early_cutoff_drop_policy_excludes_whole_windows(self):
        report = panel(self.data, self.digest, EARLY, DROP_WINDOW)
        self.assertEqual(report['samples'], [])
        summary = report['summary'][0]
        # a22 window: day 23 has POS but incomplete (null in-stock) ->
        # observed-incomplete; a23/a24 windows contain days whose POS rows
        # are not yet queryable at the early cutoff -> missing POS.
        self.assertEqual(summary['dropped_missing_pos'], 2)
        self.assertEqual(summary['dropped_observed_incomplete'], 1)
        self.assertEqual(summary['test_samples'], 0)
        row23 = calendar_map(report)[DAY23]
        self.assertIsNone(row23['in_stock_minutes'])

    def test_late_cutoff_both_policies_keep_window_with_zero_label(self):
        for policy in (MASK_DAYS, DROP_WINDOW):
            report = panel(self.data, self.digest, LATE, policy)
            row23 = calendar_map(report)[DAY23]
            self.assertEqual(row23['in_stock_minutes'], 1440)
            self.assertTrue(row23['uncensored'])
            anchor22 = sample_map(report)['2026-11-22']
            target23 = anchor22['target_days'][0]
            self.assertEqual(target23['label'], 0)
            self.assertTrue(target23['valid'])
            anchor24 = sample_map(report)['2026-11-24']
            self.assertEqual(
                [(t['date'], t['label'], t['valid'])
                 for t in anchor24['target_days']],
                [('2026-11-25', 5, True), ('2026-11-26', 6, True)])
            self.assertEqual(report['summary'][0]['test_samples'], 3)
            self.assertEqual(report['summary'][0]['train_samples'], 0)

    def test_late_evidence_cannot_backfill_anchor_capped_features(self):
        report = panel(self.data, self.digest, LATE)
        anchor24 = sample_map(report)['2026-11-24']
        feature23 = anchor24['feature_days'][0]
        self.assertEqual(feature23['date'], DAY23)
        # Anchor 24's cutoff is 25T00: the day-23 witness arriving on the
        # 27th must not backfill this feature.
        self.assertEqual(feature23['sold_units'], 0)
        self.assertIsNone(feature23['in_stock_minutes'])
        self.assertFalse(feature23['uncensored'])
        # The same day as a target in the same run is fully qualified.
        target23 = sample_map(report)['2026-11-22']['target_days'][0]
        self.assertEqual(target23['in_stock_minutes'], 1440)
        self.assertTrue(target23['valid'])

    def test_anchor21_window_is_always_cross_split(self):
        for cutoff in (EARLY, LATE):
            for policy in (MASK_DAYS, DROP_WINDOW):
                report = panel(self.data, self.digest, cutoff, policy)
                anchors = {sample['anchor_date'] for sample in report['samples']}
                self.assertNotIn('2026-11-21', anchors)
                self.assertEqual(
                    report['summary'][0]['cross_split_excluded'], 1)

    def test_calendar_day_counters_are_unique_day_counts(self):
        # At the early cutoff days 25/26 have no visible POS rows; day 23 is
        # observed but incomplete and only day 24 is a complete day.
        early = panel(self.data, self.digest, EARLY)['summary'][0]
        self.assertEqual(early['uncensored_days'], 1)
        self.assertEqual(early['observed_not_complete_days'], 4)
        self.assertEqual(early['no_pos_days'], 2)
        late = panel(self.data, self.digest, LATE)['summary'][0]
        self.assertEqual(late['uncensored_days'], 4)
        self.assertEqual(late['observed_not_complete_days'], 3)
        self.assertEqual(late['no_pos_days'], 0)

    def test_sample_and_calendar_ordering(self):
        report = panel(self.data, self.digest, LATE)
        anchors = [sample['anchor_date'] for sample in report['samples']]
        self.assertEqual(anchors, sorted(anchors))
        dates = [row['date'] for row in report['calendar']]
        self.assertEqual(dates, sorted(dates))
        for sample in report['samples']:
            feature_dates = [row['date'] for row in sample['feature_days']]
            target_dates = [row['date'] for row in sample['target_days']]
            self.assertEqual(feature_dates, sorted(feature_dates))
            self.assertEqual(target_dates, sorted(target_dates))

    def test_pos_visibility_boundary_is_exact_midnight(self):
        visible = panel(self.data, self.digest,
                        '2026-11-21T00:00:00Z')
        self.assertTrue(calendar_map(visible)['2026-11-20']['observed'])
        hidden = panel(self.data, self.digest,
                       '2026-11-20T23:59:00Z')
        self.assertFalse(calendar_map(hidden)['2026-11-20']['observed'])

    def test_anchor_cap_equality_keeps_late_witness_out_of_features(self):
        # Anchor 23's feature window ends on day 23; the day-24 release
        # witness (visible 25T00) never intersects it, while the day-23
        # witness (visible 27T00) is later than every anchor cap, so feature
        # in-stock minutes stay null regardless of the run cutoff.
        report = panel(self.data, self.digest, LATE)
        anchor23 = sample_map(report)['2026-11-23']
        feature23 = anchor23['feature_days'][1]
        self.assertEqual(feature23['date'], DAY23)
        self.assertIsNone(feature23['in_stock_minutes'])
        self.assertEqual(feature23['sold_units'], 0)


# --------------------------------------------------------------------------
# Independent input variants from the acceptance narrative
# --------------------------------------------------------------------------

class InputVariants(unittest.TestCase):
    def setUp(self):
        self.data, self.digest = load_base()

    def _day23_witness(self, payload):
        return next(w for w in payload['shelf_witnesses']
                    if w['id'] == 'W23')

    def test_quarantine_link_gives_zero_not_null_after_visibility(self):
        # R1/P1/O1/L1 is a positive-arrival milk quarantine verdict.
        variant = copy.deepcopy(self.data)
        self._day23_witness(variant)['package'] = 'P1'
        early = panel(variant, self.digest, EARLY)
        self.assertIsNone(calendar_map(early)[DAY23]['in_stock_minutes'])
        late = panel(variant, self.digest, LATE)
        row23 = calendar_map(late)[DAY23]
        self.assertEqual(row23['in_stock_minutes'], 0)
        self.assertFalse(row23['uncensored'])
        target23 = sample_map(late)['2026-11-22']['target_days'][0]
        self.assertFalse(target23['valid'])
        dropped = panel(variant, self.digest, LATE, DROP_WINDOW)
        self.assertEqual(
            dropped['summary'][0]['dropped_observed_incomplete'], 1)
        self.assertEqual(dropped['summary'][0]['dropped_missing_pos'], 0)

    def test_independent_r2_quarantine_report_identity_is_exact(self):
        variant = copy.deepcopy(self.data)
        r2 = copy.deepcopy(
            variant['v6_reports'][0]['report'])
        for verdict in r2['line_verdicts']:
            if (verdict['package'], verdict['order'],
                    verdict['lot']) == ('P3', 'O1', 'L1'):
                verdict['disposition'] = 'quarantine'
        variant['v6_reports'].append({'report_id': 'R2', 'report': r2})
        self._day23_witness(variant)['report_id'] = 'R2'
        # Same triple in a different report must not be merged with R1:
        # R2's quarantine verdict yields null then 0.
        early = panel(variant, self.digest, EARLY)
        self.assertIsNone(calendar_map(early)[DAY23]['in_stock_minutes'])
        late = panel(variant, self.digest, LATE)
        self.assertEqual(calendar_map(late)[DAY23]['in_stock_minutes'], 0)
        target23 = sample_map(late)['2026-11-22']['target_days'][0]
        self.assertEqual((target23['label'], target23['valid']),
                         (None, False))

    def test_early_non_qualifying_witness_forces_zero_but_late_release_wins(self):
        variant = copy.deepcopy(self.data)
        variant['shelf_witnesses'].append({
            'id': 'W23Q', 'sku': 'milk',
            'start': '2026-11-23T00:00:00Z',
            'end': '2026-11-24T00:00:00Z',
            'report_id': 'R1', 'package': 'P1', 'order': 'O1', 'lot': 'L1',
            'visible_at': '2026-11-24T00:00:00Z',
        })
        early = panel(variant, self.digest, EARLY)
        self.assertEqual(calendar_map(early)[DAY23]['in_stock_minutes'], 0)
        late = panel(variant, self.digest, LATE)
        row23 = calendar_map(late)[DAY23]
        self.assertEqual(row23['in_stock_minutes'], 1440)
        target23 = sample_map(late)['2026-11-22']['target_days'][0]
        self.assertEqual((target23['label'], target23['valid']), (0, True))
        # The anchor-24 feature still only knows the quarantine witness:
        # evidence seen by 25T00 proves zero qualifying minutes.
        feature23 = sample_map(late)['2026-11-24']['feature_days'][0]
        self.assertEqual(feature23['in_stock_minutes'], 0)
        self.assertFalse(feature23['uncensored'])


# --------------------------------------------------------------------------
# Coverage geometry and point-in-time rules (small hand-built inputs)
# --------------------------------------------------------------------------

def synthetic_payload(witnesses, observations=None, reports=None,
                      evidence_as_of=LATE):
    data, _ = load_base()
    payload = copy.deepcopy(data)
    payload['evidence_as_of'] = evidence_as_of
    payload['shelf_witnesses'] = witnesses
    if observations is not None:
        payload['observations'] = observations
    if reports is not None:
        payload['v6_reports'] = reports
    return payload


def release_witness(wid, start, end, visible_at, sku='milk',
                    ref=('R1', 'P3', 'O1', 'L1')):
    return {'id': wid, 'sku': sku, 'start': start, 'end': end,
            'report_id': ref[0], 'package': ref[1], 'order': ref[2],
            'lot': ref[3], 'visible_at': visible_at}


class CoverageGeometry(unittest.TestCase):
    def _index(self, witnesses):
        parsed = parse_input(synthetic_payload(witnesses))
        return CoverageIndex(parsed['witnesses'], parsed['calendar_start'],
                             parsed['cutoff'])

    def test_exact_cutoff_equality_counts(self):
        index = self._index([release_witness(
            'W', '2026-11-20T00:00:00Z', '2026-11-21T00:00:00Z',
            '2026-11-21T00:00:00Z')])
        day = date(2026, 11, 20)
        self.assertEqual(index.in_stock_minutes('milk', day, minute(
            '2026-11-21T00:00:00Z')), 1440)
        self.assertIsNone(index.in_stock_minutes(
            'milk', day, minute('2026-11-20T23:59:00Z')))

    def test_cross_midnight_clips_without_carryover(self):
        index = self._index([release_witness(
            'W', '2026-11-20T22:00:00Z', '2026-11-21T02:00:00Z',
            '2026-11-21T02:00:00Z')])
        cutoff = minute(LATE)
        self.assertEqual(index.in_stock_minutes(
            'milk', date(2026, 11, 20), cutoff), 120)
        self.assertEqual(index.in_stock_minutes(
            'milk', date(2026, 11, 21), cutoff), 120)

    def test_overlapping_intervals_union_once(self):
        index = self._index([
            release_witness('A', '2026-11-20T00:00:00Z',
                            '2026-11-21T00:00:00Z', LATE),
            release_witness('B', '2026-11-20T12:00:00Z',
                            '2026-11-22T00:00:00Z', LATE),
        ])
        self.assertEqual(index.in_stock_minutes(
            'milk', date(2026, 11, 20), minute(LATE)), 1440)
        self.assertEqual(index.in_stock_minutes(
            'milk', date(2026, 11, 21), minute(LATE)), 1440)

    def test_partial_witnesses_with_different_visible_times(self):
        index = self._index([
            release_witness('A', '2026-11-20T00:00:00Z',
                            '2026-11-20T06:00:00Z',
                            '2026-11-22T00:00:00Z'),
            release_witness('B', '2026-11-20T06:00:00Z',
                            '2026-11-20T12:00:00Z',
                            '2026-11-20T12:00:00Z'),
        ])
        day = date(2026, 11, 20)
        self.assertIsNone(index.in_stock_minutes(
            'milk', day, minute('2026-11-20T11:59:00Z')))
        self.assertEqual(index.in_stock_minutes(
            'milk', day, minute('2026-11-20T12:00:00Z')), 360)
        self.assertEqual(index.in_stock_minutes(
            'milk', day, minute('2026-11-22T00:00:00Z')), 720)

    def test_unknown_and_no_receipt_only_yield_zero_or_null(self):
        reports = [{
            'report_id': 'R3',
            'report': {
                'schema_version': 6,
                'line_verdicts': [
                    {'package': 'PX', 'order': 'O1', 'lot': 'L1',
                     'sku': 'milk', 'received': 3,
                     'disposition': 'unknown'},
                    {'package': 'PY', 'order': 'O1', 'lot': 'L1',
                     'sku': 'milk', 'received': 0,
                     'disposition': 'no_receipt'},
                ],
            },
        }]
        witnesses = [
            release_witness('U', '2026-11-20T00:00:00Z',
                            '2026-11-21T00:00:00Z', LATE,
                            ref=('R3', 'PX', 'O1', 'L1')),
            release_witness('N', '2026-11-20T00:00:00Z',
                            '2026-11-21T00:00:00Z', LATE,
                            ref=('R3', 'PY', 'O1', 'L1')),
        ]
        parsed = parse_input(synthetic_payload(witnesses, reports=reports))
        index = CoverageIndex(parsed['witnesses'], parsed['calendar_start'],
                              parsed['cutoff'])
        day = date(2026, 11, 20)
        self.assertEqual(index.in_stock_minutes(
            'milk', day, minute(LATE)), 0)
        self.assertIsNone(index.in_stock_minutes(
            'milk', day, minute('2026-11-20T23:59:00Z')))

    def test_partial_pos_monitoring_keeps_day_incomplete(self):
        observations = [{
            'date': '2026-11-20', 'sku': 'milk', 'sold_units': 2,
            'covered_minutes': 600, 'promoted': False,
        }]
        witnesses = [release_witness(
            'W', '2026-11-20T00:00:00Z', '2026-11-21T00:00:00Z', LATE)]
        payload = synthetic_payload(witnesses, observations=observations)
        parsed = parse_input(payload)
        report = compile_report(payload, 'digest')
        row = calendar_map(report)['2026-11-20']
        self.assertEqual(row['covered_minutes'], 600)
        self.assertEqual(row['in_stock_minutes'], 1440)
        self.assertTrue(row['observed'])
        self.assertFalse(row['uncensored'])


# --------------------------------------------------------------------------
# POS availability and zero-vs-missing semantics
# --------------------------------------------------------------------------

class PosSemantics(unittest.TestCase):
    def setUp(self):
        self.data, self.digest = load_base()

    def test_explicit_zero_sale_distinct_from_missing_pos(self):
        payload = copy.deepcopy(self.data)
        payload['evidence_as_of'] = LATE
        # Keep day 23's explicit zero row and drop day 22's row entirely.
        payload['observations'] = [
            row for row in payload['observations']
            if row['date'] != '2026-11-22']
        report = compile_report(payload, self.digest)
        rows = calendar_map(report)
        self.assertEqual(rows[DAY23]['sold_units'], 0)
        self.assertTrue(rows[DAY23]['observed'])
        self.assertIsNone(rows['2026-11-22']['sold_units'])
        self.assertFalse(rows['2026-11-22']['observed'])

    def test_pos_row_not_queryable_before_business_day_end(self):
        # A cutoff inside day 26 hides day 26's POS row even though the
        # timestamp is after every listed witness visibility instant.
        report = panel(self.data, self.digest,
                       '2026-11-26T12:00:00Z')
        row26 = calendar_map(report)['2026-11-26']
        self.assertFalse(row26['observed'])
        self.assertIsNone(row26['sold_units'])
        self.assertIsNone(row26['covered_minutes'])
        self.assertIsNone(row26['promoted'])


# --------------------------------------------------------------------------
# Train/test split and empty-sample legality
# --------------------------------------------------------------------------

class SplitAndEmpty(unittest.TestCase):
    def setUp(self):
        self.data, self.digest = load_base()

    def test_train_window_when_horizon_fits_before_split(self):
        payload = copy.deepcopy(self.data)
        # split 24 with horizon 2: anchor 22 -> targets 23-24, last <= split.
        payload['split_date'] = '2026-11-24'
        payload['evidence_as_of'] = LATE
        report = compile_report(payload, self.digest)
        anchor22 = sample_map(report)['2026-11-22']
        self.assertEqual(anchor22['split'], 'train')
        # Anchor 23 targets 24-25 straddle split day 24 and stay excluded.
        self.assertNotIn('2026-11-23', sample_map(report))
        self.assertEqual(
            report['summary'][0]['cross_split_excluded'], 1)
        summary = report['summary'][0]
        self.assertGreaterEqual(summary['train_samples'], 1)
        self.assertEqual(
            summary['in_bounds_candidates'],
            summary['cross_split_excluded']
            + summary['dropped_missing_pos']
            + summary['dropped_observed_incomplete']
            + summary['train_samples'] + summary['test_samples'])

    def test_empty_samples_are_legal(self):
        # A horizon longer than the whole calendar cannot fit any window.
        payload = copy.deepcopy(self.data)
        payload['horizon_days'] = 28
        report = compile_report(payload, self.digest)
        self.assertEqual(report['samples'], [])
        summary = report['summary'][0]
        self.assertEqual(summary['in_bounds_candidates'], 0)
        self.assertEqual(summary['boundary_excluded'], 7)
        self.assertEqual(summary['train_samples'], 0)
        self.assertEqual(summary['test_samples'], 0)
        # Calendar views are still emitted for every SKU/day.
        self.assertEqual(len(report['calendar']), 7)

    def test_features_can_reuse_pre_split_days_in_test_windows(self):
        report = panel(self.data, self.digest, LATE)
        anchor24 = sample_map(report)['2026-11-24']
        self.assertEqual(
            [row['date'] for row in anchor24['feature_days']],
            ['2026-11-23', '2026-11-24'])
        self.assertEqual(anchor24['split'], 'test')


# --------------------------------------------------------------------------
# Determinism and strict validation
# --------------------------------------------------------------------------

class Determinism(unittest.TestCase):
    def test_shuffling_legal_arrays_keeps_business_output(self):
        data, digest = load_base()
        shuffled = copy.deepcopy(data)
        shuffled['skus'].reverse()
        shuffled['observations'].reverse()
        shuffled['v6_reports'].reverse()
        shuffled['shelf_witnesses'].reverse()
        first = compile_report(copy.deepcopy(data), digest)
        second = compile_report(shuffled, digest)
        self.assertEqual(json.dumps(first, sort_keys=True),
                         json.dumps(second, sort_keys=True))

    def test_identical_input_identical_report(self):
        data, digest = load_base()
        first = compile_report(copy.deepcopy(data), digest)
        second = compile_report(copy.deepcopy(data), digest)
        self.assertEqual(json.dumps(first, sort_keys=True),
                         json.dumps(second, sort_keys=True))

    def test_train_sorts_before_test_then_sku_and_anchor(self):
        data, digest = load_base()
        payload = copy.deepcopy(data)
        # split 24 makes anchor 22 a train window while anchors 23+ stay
        # test-side; add a second SKU to pin the SKU tie-break.
        payload['split_date'] = '2026-11-24'
        payload['skus'] = ['milk', 'bread']
        payload['observations'] = payload['observations'] + [
            dict(row, sku='bread')
            for row in copy.deepcopy(data['observations'])]
        # bread carries no witnesses, so it never produces valid targets,
        # but mask_days still emits its windows for the ordering checks.
        payload['shelf_witnesses'] = []
        report = compile_report(payload, digest)
        keys = [(sample['split'], sample['sku'], sample['anchor_date'])
                for sample in report['samples']]
        self.assertEqual(keys, sorted(keys))
        split_flags = [sample['split'] for sample in report['samples']]
        self.assertEqual(split_flags, sorted(split_flags))
        # Anchor 22 (targets 23-24, last day on the split) is train; anchors
        # whose first target is after the split stay test-side.
        train_anchors = {key[2] for key in keys if key[0] == 'train'}
        test_anchors = {key[2] for key in keys if key[0] == 'test'}
        self.assertIn('2026-11-22', train_anchors)
        self.assertNotIn('2026-11-22', test_anchors)
        self.assertEqual([row['sku'] for row in report['summary']],
                         ['bread', 'milk'])
        # Calendar blocks are emitted per sorted SKU, then ascending dates;
        # day rows themselves carry no SKU field by contract.
        self.assertEqual(len(report['calendar']), 14)
        self.assertEqual(
            [row['date'] for row in report['calendar'][:7]],
            sorted(row['date'] for row in report['calendar'][:7]))
        self.assertEqual(
            [row['date'] for row in report['calendar'][7:]],
            sorted(row['date'] for row in report['calendar'][7:]))


class InputValidation(unittest.TestCase):
    def setUp(self):
        self.data, self.digest = load_base()

    def reject(self, mutate):
        payload = copy.deepcopy(self.data)
        mutate(payload)
        with self.assertRaises((ValueError, TypeError, KeyError)):
            compile_report(payload, self.digest)

    def test_scalar_and_structure_rules(self):
        self.reject(lambda d: d.update(schema_version=7))
        self.reject(lambda d: d.update(schema_version='8'))
        self.reject(lambda d: d.update(schema_version=8.0))
        self.reject(lambda d: d.update(schema_version=True))
        self.reject(lambda d: d.update(extra=1))
        self.reject(lambda d: d.pop('skus'))
        self.reject(lambda d: d.update(censored_target_policy='drop_days'))
        self.reject(lambda d: d.update(lookback_days=0))
        self.reject(lambda d: d.update(lookback_days=91))
        self.reject(lambda d: d.update(horizon_days=29))
        self.reject(lambda d: d.update(cutoff_date='2026-11-19'))
        self.reject(lambda d: d.update(split_date='2026-11-26'))
        self.reject(lambda d: d.update(split_date='2026-11-19'))
        self.reject(lambda d: d.update(
            calendar_start='2026-11-20', cutoff_date='2027-11-21'))

    def test_timestamp_rules(self):
        self.reject(lambda d: d.update(
            evidence_as_of='2026-11-25T00:00:30Z'))
        self.reject(lambda d: d.update(
            evidence_as_of='2026-11-25T00:00:00+00:00'))
        self.reject(lambda d: d.update(evidence_as_of='20261125T000000Z'))
        self.reject(lambda d: d['shelf_witnesses'][0].update(
            start='2026-11-23T00:00:30Z'))

    def test_observation_rules(self):
        self.reject(lambda d: d['observations'][0].update(sold_units=True))
        self.reject(lambda d: d['observations'][0].update(sold_units=1.0))
        self.reject(lambda d: d['observations'][0].update(
            covered_minutes=1441))
        self.reject(lambda d: d['observations'][0].update(
            covered_minutes=-1))
        self.reject(lambda d: d['observations'][0].update(promoted='yes'))
        self.reject(lambda d: d['observations'].append(
            dict(d['observations'][0])))
        self.reject(lambda d: d['skus'].append('milk'))
        self.reject(lambda d: d['observations'][0].update(sku='ghost'))
        self.reject(lambda d: d['observations'][0].update(
            date='2026-11-19'))

    def test_witness_and_report_rules(self):
        self.reject(lambda d: d['shelf_witnesses'][0].update(
            report_id='MISSING'))
        self.reject(lambda d: d['shelf_witnesses'][0].update(
            package='NOPE'))
        self.reject(lambda d: d['shelf_witnesses'][0].update(sku='bread'))
        self.reject(lambda d: d['shelf_witnesses'].append(
            dict(d['shelf_witnesses'][0])))

        def bad_end(d):
            d['shelf_witnesses'][0]['end'] = \
                d['shelf_witnesses'][0]['start']
        self.reject(bad_end)

        def visible_before_end(d):
            d['shelf_witnesses'][0]['visible_at'] = \
                '2026-11-23T23:00:00Z'
        self.reject(visible_before_end)

        def bad_report_version(d):
            d['v6_reports'][0]['report']['schema_version'] = 5
        self.reject(bad_report_version)

        def bad_disposition(d):
            d['v6_reports'][0]['report']['line_verdicts'][0][
                'disposition'] = 'accepted'
        self.reject(bad_disposition)

        def positive_arrival_zero(d):
            verdict = next(v for v in
                           d['v6_reports'][0]['report']['line_verdicts']
                           if v['disposition'] == 'release')
            verdict['received'] = 0
        self.reject(positive_arrival_zero)

        def duplicate_verdict_key(d):
            verdicts = d['v6_reports'][0]['report']['line_verdicts']
            verdicts.append(dict(verdicts[0]))
        self.reject(duplicate_verdict_key)

    def test_no_receipt_requires_zero_received(self):
        payload = copy.deepcopy(self.data)
        payload['v6_reports'] = [{
            'report_id': 'R3',
            'report': {
                'schema_version': 6,
                'line_verdicts': [
                    {'package': 'PY', 'order': 'O1', 'lot': 'L1',
                     'sku': 'milk', 'received': 1,
                     'disposition': 'no_receipt'},
                ],
            },
        }]
        payload['shelf_witnesses'] = []
        with self.assertRaises(ValueError):
            compile_report(payload, self.digest)

    def test_duplicate_json_keys_and_nonfinite_rejected(self):
        raw = FIXTURE.read_bytes()
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'dup.json'
            duplicated = raw.replace(b'"sku": "milk"',
                                     b'"sku": "milk", "sku": "milk"', 1)
            path.write_bytes(duplicated)
            with self.assertRaises(ValueError):
                run(path)
            path.write_text('{"schema_version": 8}')
            with self.assertRaises(ValueError):
                run(path)
            nonfinite = raw.replace(b'"sold_units": 3',
                                    b'"sold_units": NaN', 1)
            path.write_bytes(nonfinite)
            with self.assertRaises(ValueError):
                run(path)

    def test_null_observation_fields_are_allowed(self):
        payload = copy.deepcopy(self.data)
        payload['observations'][0].update(
            sold_units=None, covered_minutes=None, promoted=None)
        report = compile_report(payload, self.digest)
        row = next(row for row in report['calendar']
                   if row['date'] == '2026-11-20')
        self.assertTrue(row['observed'])
        self.assertIsNone(row['sold_units'])
        self.assertFalse(row['uncensored'])

    def test_calendar_span_limits(self):
        payload = copy.deepcopy(self.data)
        payload['observations'] = []
        payload['shelf_witnesses'] = []
        payload['calendar_start'] = '2023-01-01'
        payload['cutoff_date'] = '2024-01-01'  # 366 inclusive days
        payload['split_date'] = '2023-12-31'
        parsed = parse_input(payload)
        self.assertEqual(
            (parsed['cutoff'] - parsed['calendar_start']).days + 1, 366)
        payload['cutoff_date'] = '2024-01-02'  # 367 days
        with self.assertRaises(ValueError):
            parse_input(payload)

    def test_last_calendar_day_needs_representable_next_day(self):
        payload = copy.deepcopy(self.data)
        payload['calendar_start'] = '9999-12-01'
        payload['cutoff_date'] = '9999-12-31'
        payload['split_date'] = '9999-12-30'
        payload['observations'] = []
        payload['shelf_witnesses'] = []
        with self.assertRaises(ValueError):
            parse_input(payload)

    def test_evidence_cutoff_may_predate_calendar(self):
        payload = copy.deepcopy(self.data)
        payload['evidence_as_of'] = '2020-01-01T00:00:00Z'
        parsed = parse_input(payload)
        self.assertEqual(
            parsed['evidence_as_of'],
            datetime(2020, 1, 1, tzinfo=UTC))


# --------------------------------------------------------------------------
# CLI publication behavior
# --------------------------------------------------------------------------

class CliPublication(unittest.TestCase):
    def call_cli(self, source, output):
        env = dict(os.environ, PYTHONPATH=str(ROOT / 'src'))
        return subprocess.run(
            [sys.executable, '-m', 'perishable_fulfillment.demand_panel',
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
            report = json.loads(first)
            self.assertEqual(report['schema_version'], 8)

            bad = tmp / 'bad.json'
            bad.write_text('{"schema_version": 8}')
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
