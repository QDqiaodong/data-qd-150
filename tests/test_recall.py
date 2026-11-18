"""Acceptance tests for the schema_version=5 offline recall analysis.

The production engine in :mod:`perishable_fulfillment.recall` is checked
against an independent brute-force oracle that enumerates every small
integer edge/segment tainted combination and filters by the raw conservation
definitions.  The oracle imports none of the production network, residual or
witness-normalization code; it only reuses input parsing to obtain validated
facts and re-derives every conservation rule from scratch.
"""

import copy
import itertools
import os
import random
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from perishable_fulfillment.common import read
from perishable_fulfillment import recall
from perishable_fulfillment.recall import (
    SCHEMA_VERSION,
    build_recipients,
    build_segments,
    canonical_witness,
    compile_report,
    feasible_flows,
    parse_input,
    recipient_extrema,
    recipient_totals_from_witness,
    verify_witness,
)

ROOT = Path(__file__).resolve().parents[1]
FIXTURE = ROOT / 'fixtures' / 'recall_input.json'
AS_OF = '2026-11-18'


def base_input():
    data, _ = read(FIXTURE)
    return copy.deepcopy(data)


def tightened_input():
    """Canonical case with the M->A observation raised to a hard lower bound."""
    data = base_input()
    for edge in data['trace']['transfers']:
        if edge['id'] == 'e3':
            edge['lower'] = 1
    return data


class CanonicalAcceptance(unittest.TestCase):
    """The exact mixed-then-split scenario from the task statement."""

    def test_individual_ranges_and_joint_infeasibility(self):
        report = compile_report(base_input(), 'digest')
        orders = {row['id']: row for row in report['recipients']
                  if row['kind'] == 'order'}
        # Either order alone may receive zero or one tainted unit.
        self.assertEqual((orders['O1']['min_tainted'],
                          orders['O1']['max_tainted']), (0, 1))
        self.assertEqual((orders['O2']['min_tainted'],
                          orders['O2']['max_tainted']), (0, 1))
        self.assertEqual(orders['O1']['status'], 'possible')
        self.assertEqual(orders['O2']['status'], 'possible')
        # The "both receive at least one" query is infeasible even though
        # each single-object interval is feasible on its own.
        joint = next(q for q in report['queries']
                     if q['id'] == 'q_joint_at_least_one')
        self.assertFalse(joint['feasible'])
        self.assertIsNone(joint['witness'])

    def test_force_one_order_pins_the_other_to_zero(self):
        report = compile_report(base_input(), 'digest')
        forced = next(q for q in report['queries']
                      if q['id'] == 'q_force_o1')
        self.assertTrue(forced['feasible'])
        witness = forced['witness']
        edge_taint = {t['edge']: t['tainted']
                      for t in witness['transfers']}
        seg_taint = {(s['kind'], s['id'], s['lot']): s['tainted']
                     for s in witness['segments']}
        self.assertEqual(edge_taint,
                         {'e1': 1, 'e2': 0, 'e3': 1, 'e4': 0})
        self.assertEqual(seg_taint[('order', 'O1', 'A')], 1)
        self.assertEqual(seg_taint[('order', 'O2', 'B')], 0)
        parsed = parse_input(base_input())
        segments = build_segments(parsed)
        segment_taint = {('seg', s['kind'], s['id'], s['lot']):
                         s['tainted'] for s in witness['segments']}
        verify_witness(parsed, segments, edge_taint, segment_taint)

    def test_base_witness_is_lexicographic_minimum(self):
        report = compile_report(base_input(), 'digest')
        # Smallest canonical vector routes the single tainted unit through
        # e3=0/e4=1 (e4 sorts after e3, so e3 is minimized first).
        self.assertEqual(
            [t['tainted'] for t in
             report['base_witness']['transfers']], [1, 0, 0, 1])

    def test_tightened_observation_contracts_range(self):
        report = compile_report(tightened_input(), 'digest')
        orders = {row['id']: row for row in report['recipients']
                  if row['kind'] == 'order'}
        self.assertEqual((orders['O1']['min_tainted'],
                          orders['O1']['max_tainted']), (1, 1))
        self.assertEqual((orders['O2']['min_tainted'],
                          orders['O2']['max_tainted']), (0, 0))
        self.assertEqual(orders['O1']['status'], 'certain')
        self.assertEqual(orders['O2']['status'], 'clear')
        self.assertEqual(
            [t['tainted'] for t in
             report['base_witness']['transfers']], [1, 0, 1, 0])


# --------------------------------------------------------------------------
# Independent brute-force oracle
# --------------------------------------------------------------------------

def oracle_explanations(parsed, segments):
    """Enumerate every feasible tainted explanation from raw conservation.

    Returns (edge_ids, segment_keys, explanations) where each explanation is
    a pair of tuples aligned to those key orders.  Only usable on small
    integer domains; it never calls the production flow engine.
    """
    edge_ids = [e['id'] for e in
                sorted(parsed['transfers'], key=lambda row: row['id'])]
    segment_keys = [(s['kind'], s['id'], s['lot']) for s in
                    sorted(segments,
                           key=lambda row: (row['kind'], row['id'],
                                            row['lot']))]
    edge_domains = [
        range(next(e for e in parsed['transfers'] if e['id'] == eid)
              ['lower'],
              next(e for e in parsed['transfers']
                   if e['id'] == eid)['upper'] + 1)
        for eid in edge_ids]
    segment_domains = [
        range(0, s['quantity'] + 1) for s in
        sorted(segments, key=lambda row: (row['kind'], row['id'],
                                          row['lot']))]
    source_nodes = {s['node'] for s in parsed['sources']}
    terminals = set(parsed['lot_to_node'].values()) | \
        parsed['waste_nodes']
    explanations = []
    for edge_vector in itertools.product(*edge_domains):
        edge_taint = dict(zip(edge_ids, edge_vector))
        ok = True
        for source in parsed['sources']:
            if sum(edge_taint[e['id']] for e in parsed['transfers']
                   if e['from'] == source['node']) != source['tainted']:
                ok = False
                break
        if not ok:
            continue
        for node in parsed['nodes']:
            if node in source_nodes or node in terminals:
                continue
            inflow = sum(edge_taint[e['id']] for e in parsed['transfers']
                         if e['to'] == node)
            outflow = sum(edge_taint[e['id']] for e in parsed['transfers']
                          if e['from'] == node)
            if inflow != outflow:
                ok = False
                break
        if not ok:
            continue
        for segment_vector in itertools.product(*segment_domains):
            segment_taint = {('seg',) + key: value
                             for key, value in
                             zip(segment_keys, segment_vector)}
            lot_balance = all(
                sum(value for key, value in segment_taint.items()
                    if key[3] == lot)
                == sum(edge_taint[e['id']] for e in parsed['transfers']
                       if e['to'] == node)
                for lot, node in parsed['lot_to_node'].items())
            if lot_balance:
                explanations.append((edge_vector, segment_vector))
    return edge_ids, segment_keys, explanations


class OracleValidation(unittest.TestCase):
    """Cross-check tight ranges and the canonical vector by brute force."""

    def _assert_matches_oracle(self, data):
        parsed = parse_input(data)
        segments = build_segments(parsed)
        edge_ids, segment_keys, explanations = \
            oracle_explanations(parsed, segments)
        recipients = build_recipients(parsed, segments)
        if not explanations:
            self.assertIsNone(feasible_flows(parsed, segments))
            return
        self.assertIsNotNone(feasible_flows(parsed, segments))
        minima, maxima = {}, {}
        for edge_vector, segment_vector in explanations:
            edge_taint = dict(zip(edge_ids, edge_vector))
            segment_taint = {('seg',) + key: value
                             for key, value in
                             zip(segment_keys, segment_vector)}
            totals = recipient_totals_from_witness(
                parsed, segments, edge_taint, segment_taint)
            for key, value in totals.items():
                minima[key] = min(minima.get(key, 10 ** 18), value)
                maxima[key] = max(maxima.get(key, -1), value)
        engine = recipient_extrema(parsed, segments, recipients)
        for key in minima:
            self.assertEqual(engine[key], (minima[key], maxima[key]))
        witness = canonical_witness(parsed, segments)
        engine_vector = (
            tuple(witness[('edge', eid)] for eid in edge_ids),
            tuple(witness[('seg',) + key] for key in segment_keys))
        self.assertEqual(engine_vector, sorted(explanations)[0])

    def test_canonical_fixture(self):
        self._assert_matches_oracle(base_input())

    def test_tightened_fixture(self):
        self._assert_matches_oracle(tightened_input())

    def test_random_small_networks(self):
        random.seed(20261115)
        checked = 0
        for _ in range(400):
            data = _random_small_input(random)
            if data is None:
                continue
            try:
                parse_input(data)
            except ValueError:
                continue
            # Keep brute-force domains small enough to enumerate.
            parsed = parse_input(data)
            if len(parsed['transfers']) > 7:
                continue
            if any(e['upper'] > 2 for e in parsed['transfers']):
                continue
            self._assert_matches_oracle(data)
            checked += 1
        self.assertGreater(checked, 40)


def _random_small_input(rng):
    n = rng.randint(3, 6)
    node_ids = ['N%d' % i for i in range(n)]
    source_count = rng.randint(1, 2)
    source_nodes = rng.sample(node_ids[: n // 2 + 1], source_count)
    available = [x for x in node_ids if x not in source_nodes]
    lot_nodes = rng.sample(available,
                           min(rng.randint(1, 2), len(available)))
    remaining = [x for x in available if x not in lot_nodes]
    waste = [rng.choice(remaining)] if remaining and rng.random() < 0.5 \
        else []
    transfers = []
    edge_id = 0
    for i in range(n):
        for j in range(i + 1, n):
            if rng.random() < 0.45:
                lower = rng.randint(0, 1)
                upper = rng.randint(lower, 2)
                quantity = rng.randint(upper, 3)
                transfers.append({'id': 't%d' % edge_id,
                                  'from': node_ids[i], 'to': node_ids[j],
                                  'quantity': quantity,
                                  'lower': lower, 'upper': upper})
                edge_id += 1
    if not transfers:
        return None
    sources = [{
        'node': node,
        'quantity': sum(e['quantity'] for e in transfers
                        if e['from'] == node),
        'tainted': rng.randint(0, sum(e['quantity'] for e in transfers
                                      if e['from'] == node)),
    } for node in source_nodes]
    lots, lot_node_rows = [], []
    for index, node in enumerate(lot_nodes):
        inflow = sum(e['quantity'] for e in transfers if e['to'] == node)
        lot_id = 'L%d' % index
        lots.append({'id': lot_id, 'sku': 'milk',
                     'expires': '2026-12-31', 'quantity': inflow,
                     'held': rng.randint(0, inflow)})
        lot_node_rows.append({'node': node, 'lot': lot_id})
    orders = [{'id': 'O%d' % k, 'sku': 'milk', 'priority': k,
               'quantity': rng.randint(0, 2)}
              for k in range(rng.randint(0, 2))]
    return {
        'schema_version': SCHEMA_VERSION,
        'fulfillment': {'as_of': AS_OF, 'lots': lots, 'orders': orders},
        'trace': {'nodes': node_ids, 'sources': sources,
                  'transfers': transfers, 'lot_nodes': lot_node_rows,
                  'waste_nodes': waste},
        'queries': [],
    }


class StructuralAndSemanticCases(unittest.TestCase):
    def test_empty_graph_with_zero_order(self):
        data = {'schema_version': SCHEMA_VERSION,
                'fulfillment': {'as_of': AS_OF, 'lots': [],
                                'orders': [{'id': 'O0', 'sku': 'milk',
                                            'priority': 0, 'quantity': 3}]},
                'trace': {'nodes': [], 'sources': [], 'transfers': [],
                          'lot_nodes': [], 'waste_nodes': []},
                'queries': []}
        report = compile_report(data, 'x')
        self.assertEqual(report['status'], 'consistent')
        self.assertEqual(report['base_witness'],
                         {'transfers': [], 'segments': []})
        self.assertEqual(report['recipients'],
                         [{'kind': 'order', 'id': 'O0', 'quantity': 0,
                           'min_tainted': 0, 'max_tainted': 0,
                           'status': 'clear'}])

    def test_inconsistent_fact_set(self):
        data = {'schema_version': SCHEMA_VERSION,
                'fulfillment': {'as_of': AS_OF,
                                'lots': [{'id': 'A', 'sku': 'milk',
                                          'expires': '2026-12-31',
                                          'quantity': 1, 'held': 0}],
                                'orders': []},
                'trace': {'nodes': ['S', 'L'],
                          'sources': [{'node': 'S', 'quantity': 1,
                                       'tainted': 0}],
                          'transfers': [{'id': 'e1', 'from': 'S', 'to': 'L',
                                         'quantity': 1, 'lower': 1,
                                         'upper': 1}],
                          'lot_nodes': [{'node': 'L', 'lot': 'A'}],
                          'waste_nodes': []},
                'queries': [{'id': 'q1', 'bounds': []}]}
        report = compile_report(data, 'x')
        self.assertEqual(report['status'], 'inconsistent')
        self.assertEqual(report['recipients'], [])
        self.assertIsNone(report['base_witness'])
        self.assertEqual(report['queries'],
                         [{'id': 'q1', 'feasible': False, 'witness': None}])

    def test_waste_recipient_range(self):
        data = {'schema_version': SCHEMA_VERSION,
                'fulfillment': {'as_of': AS_OF, 'lots': [], 'orders': []},
                'trace': {'nodes': ['S', 'W'],
                          'sources': [{'node': 'S', 'quantity': 2,
                                       'tainted': 1}],
                          'transfers': [{'id': 'e1', 'from': 'S', 'to': 'W',
                                         'quantity': 2, 'lower': 0,
                                         'upper': 2}],
                          'lot_nodes': [], 'waste_nodes': ['W']},
                'queries': []}
        report = compile_report(data, 'x')
        self.assertEqual(report['recipients'],
                         [{'kind': 'waste', 'id': 'W', 'quantity': 2,
                           'min_tainted': 1, 'max_tainted': 1,
                           'status': 'certain'}])

    def test_held_expired_available_segments(self):
        data = {'schema_version': SCHEMA_VERSION,
                'fulfillment': {'as_of': AS_OF,
                                'lots': [{'id': 'L', 'sku': 'milk',
                                          'expires': AS_OF,
                                          'quantity': 5, 'held': 1}],
                                'orders': []},
                'trace': {'nodes': ['S', 'T'],
                          'sources': [{'node': 'S', 'quantity': 5,
                                       'tainted': 3}],
                          'transfers': [{'id': 'e', 'from': 'S', 'to': 'T',
                                         'quantity': 5, 'lower': 0,
                                         'upper': 5}],
                          'lot_nodes': [{'node': 'T', 'lot': 'L'}],
                          'waste_nodes': []},
                'queries': []}
        report = compile_report(data, 'x')
        by_kind = {row['kind']: row for row in report['recipients']
                   if row['id'] == 'L'}
        # held=1, available=4; three tainted units split across them.
        self.assertEqual(by_kind['held']['quantity'], 1)
        self.assertEqual(by_kind['available']['quantity'], 4)
        self.assertEqual(by_kind['expired']['quantity'], 0)
        for segment in report['base_witness']['segments']:
            self.assertLessEqual(segment['tainted'], segment['quantity'])
            self.assertGreaterEqual(segment['tainted'], 0)

    def test_cross_lot_order_aggregates(self):
        data = {'schema_version': SCHEMA_VERSION,
                'fulfillment': {'as_of': AS_OF,
                                'lots': [
                                    {'id': 'A', 'sku': 'milk',
                                     'expires': '2026-12-31', 'quantity': 2,
                                     'held': 0},
                                    {'id': 'B', 'sku': 'milk',
                                     'expires': '2026-12-31', 'quantity': 3,
                                     'held': 0}],
                                'orders': [{'id': 'O', 'sku': 'milk',
                                            'priority': 0, 'quantity': 4}]},
                'trace': {'nodes': ['S', 'TA', 'TB'],
                          'sources': [{'node': 'S', 'quantity': 5,
                                       'tainted': 2}],
                          'transfers': [
                              {'id': 'e1', 'from': 'S', 'to': 'TA',
                               'quantity': 2, 'lower': 0, 'upper': 2},
                              {'id': 'e2', 'from': 'S', 'to': 'TB',
                               'quantity': 3, 'lower': 0, 'upper': 3}],
                          'lot_nodes': [{'node': 'TA', 'lot': 'A'},
                                        {'node': 'TB', 'lot': 'B'}],
                          'waste_nodes': []},
                'queries': []}
        report = compile_report(data, 'x')
        order = next(r for r in report['recipients']
                     if r['kind'] == 'order' and r['id'] == 'O')
        # Two order segments (one per lot) sum into the single recipient.
        order_segments = [s for s in report['base_witness']['segments']
                          if s['kind'] == 'order']
        self.assertEqual(len(order_segments), 2)
        self.assertEqual(order['quantity'], 4)
        self.assertGreaterEqual(order['max_tainted'], 1)

    def test_billion_quantities_are_bulk(self):
        data = {'schema_version': SCHEMA_VERSION,
                'fulfillment': {'as_of': AS_OF,
                                'lots': [{'id': 'L', 'sku': 'milk',
                                          'expires': '2026-12-31',
                                          'quantity': 10 ** 9, 'held': 0}],
                                'orders': [{'id': 'O', 'sku': 'milk',
                                            'priority': 0,
                                            'quantity': 10 ** 9}]},
                'trace': {'nodes': ['S', 'M', 'T'],
                          'sources': [{'node': 'S', 'quantity': 10 ** 9,
                                       'tainted': 10 ** 9}],
                          'transfers': [
                              {'id': 'e1', 'from': 'S', 'to': 'M',
                               'quantity': 10 ** 9, 'lower': 0,
                               'upper': 10 ** 9},
                              {'id': 'e2', 'from': 'M', 'to': 'T',
                               'quantity': 10 ** 9, 'lower': 0,
                               'upper': 10 ** 9}],
                          'lot_nodes': [{'node': 'T', 'lot': 'L'}],
                          'waste_nodes': []},
                'queries': []}
        report = compile_report(data, 'x')
        order = next(r for r in report['recipients']
                     if r['kind'] == 'order')
        self.assertEqual(order['min_tainted'], 10 ** 9)
        self.assertEqual(order['max_tainted'], 10 ** 9)

    def test_query_bound_above_physical_quantity_is_infeasible(self):
        data = base_input()
        data['queries'] = [{
            'id': 'impossible',
            'bounds': [{'kind': 'order', 'id': 'O1',
                        'lower': 5, 'upper': 10 ** 18}]}]
        report = compile_report(data, 'x')
        query = report['queries'][0]
        self.assertFalse(query['feasible'])
        self.assertIsNone(query['witness'])


class StrictInputValidation(unittest.TestCase):
    def _reject(self, mutate):
        data = base_input()
        mutate(data)
        with self.assertRaises(ValueError):
            compile_report(data, 'x')

    def test_wrong_schema_type(self):
        self._reject(lambda d: d.update(schema_version='5'))

    def test_boolean_and_float_and_string_rejected(self):
        self._reject(lambda d: d['trace']['transfers'][0].update(
            lower=True))
        self._reject(lambda d: d['trace']['transfers'][0].update(
            quantity=1.5))
        self._reject(lambda d: d['trace']['transfers'][0].update(
            upper='1'))

    def test_unknown_and_missing_fields(self):
        self._reject(lambda d: d['trace'].update(extra=1))
        self._reject(lambda d: d['trace']['transfers'][0].pop('upper'))

    def test_top_level_field_set(self):
        self._reject(lambda d: d.pop('queries'))
        self._reject(lambda d: d.update(extra=1))

    def test_duplicates_self_loop_and_cycle(self):
        self._reject(lambda d: d['trace']['nodes'].append('T'))
        self._reject(lambda d: d['trace']['transfers'][0].update(to='T'))
        self._reject(lambda d: d['trace']['transfers'].append(
            {'id': 'e1', 'from': 'T', 'to': 'M', 'quantity': 1,
             'lower': 0, 'upper': 1}))

    def test_quantity_conservation_and_mapping(self):
        self._reject(lambda d: d['trace']['sources'][0].update(quantity=9))
        self._reject(lambda d: d['trace']['lot_nodes'][0].update(lot='ZZ'))

    def test_empty_graph_restrictions(self):
        data = {'schema_version': SCHEMA_VERSION,
                'fulfillment': {'as_of': AS_OF,
                                'lots': [{'id': 'L', 'sku': 'milk',
                                          'expires': '2026-12-31',
                                          'quantity': 0, 'held': 0}],
                                'orders': []},
                'trace': {'nodes': ['X'], 'sources': [],
                          'transfers': [],
                          'lot_nodes': [{'node': 'X', 'lot': 'L'}],
                          'waste_nodes': []}, 'queries': []}
        with self.assertRaises(ValueError):
            compile_report(data, 'x')

    def test_isolated_intermediate_rejected(self):
        data = base_input()
        data['trace']['nodes'].append('ISO')
        with self.assertRaises(ValueError):
            compile_report(data, 'x')

    def test_query_rules(self):
        self._reject(lambda d: d['queries'][0].update(id='q_empty'))
        self._reject(lambda d: d['queries'][0]['bounds'].append(
            {'kind': 'order', 'id': 'O1', 'lower': 0, 'upper': 1}))
        self._reject(lambda d: d['queries'].append(
            {'id': 'q_empty', 'bounds': []}))
        self._reject(lambda d: d['queries'].append(
            {'id': 'bad',
             'bounds': [{'kind': 'order', 'id': 'NOPE',
                         'lower': 0, 'upper': 1}]}))


class CliPublication(unittest.TestCase):
    def call(self, source, output):
        env = dict(os.environ)
        env['PYTHONPATH'] = str(ROOT / 'src')
        return subprocess.run(
            [sys.executable, '-m', 'perishable_fulfillment.recall',
             '--source', str(source), '--output', str(output)],
            capture_output=True, text=True, env=env)

    def test_cli_roundtrip_and_failure_preserves_output(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            out = tmp / 'report.json'
            result = self.call(FIXTURE, out)
            self.assertEqual(result.returncode, 0, result.stderr)
            first = out.read_bytes()
            result = self.call(FIXTURE, out)
            self.assertEqual(result.returncode, 0)
            self.assertEqual(first, out.read_bytes())
            bad = tmp / 'bad.json'
            bad.write_text('{"schema_version":5}')
            target = tmp / 'keep.json'
            target.write_text('prior')
            failed = self.call(bad, target)
            self.assertEqual(failed.returncode, 2)
            self.assertEqual(target.read_text(), 'prior')

    def test_symlink_and_same_file_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            link = tmp / 'link.json'
            link.symlink_to(FIXTURE)
            self.assertEqual(self.call(FIXTURE, link).returncode, 2)
            self.assertEqual(self.call(FIXTURE, FIXTURE).returncode, 2)


if __name__ == '__main__':
    unittest.main()
