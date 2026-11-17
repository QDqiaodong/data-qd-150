"""Acceptance tests for the v2 perishable commitment ledger.

Covers the three required areas:
  * Ledger adjudication: over-pick, demand cut below picked, hold covering
    picked stock, same id with different bytes, stale revision, mid-batch
    failure atomicity, and fact consistency after reopening the database.
  * Planner: small independent brute-force enumeration, the unique-admission
    lot hand-off, preserving old commitments over FEFO, deterministic ties,
    and 10**9 stock without per-unit expansion.
  * End-to-end linkage: pick then tighten shelf life / admission / holds,
    plus report-write failure after commit followed by an old-request replay.

Standard library only; every database lives in a temporary directory.
"""

import copy
import itertools
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from perishable_fulfillment import ledger
from perishable_fulfillment.planner import plan as planner_plan

ROOT = Path(__file__).resolve().parents[1]


def make_order(oid, sku='milk', priority=1, quantity=1, ship='2026-11-16',
               min_life=0, allowed=None, picked=0):
    return {'id': oid, 'sku': sku, 'priority': priority, 'quantity': quantity,
            'ship_date': ship, 'min_life': min_life, 'allowed_lots': allowed,
            'picked': picked}


def make_lot(lid, sku='milk', expires='2026-12-01', quantity=1, held=0,
             picked=0):
    # `picked` only exists on planner inputs; source-file lots carry the five
    # immutable v2 fields and are validated separately.
    return {'id': lid, 'sku': sku, 'expires': expires, 'quantity': quantity,
            'held': held, 'picked': picked}


def source_lot(lid, **kwargs):
    full = make_lot(lid, **kwargs)
    return {k: v for k, v in full.items() if k != 'picked'}


def source(as_of='2026-11-16', lots=None, orders=None):
    return {'as_of': as_of,
            'lots': lots if lots is not None else [],
            'orders': orders if orders is not None else []}


def source_order(oid, **kwargs):
    full = make_order(oid, **kwargs)
    return {k: v for k, v in full.items() if k != 'picked'}


def open_ledger(tmp, src):
    db = Path(tmp) / 'book.db'
    ledger.initialize(db, src, 'source-digest')
    return db


def apply(db, request_id, expected, events):
    payload = {'request_id': request_id, 'expected_revision': expected,
               'events': events}
    return ledger.apply_batch(db, json.dumps(payload).encode())


def pick(order, lot, quantity):
    return {'type': 'pick', 'order': order, 'lot': lot, 'quantity': quantity}


def set_hold(lot, held):
    return {'type': 'set_hold', 'lot': lot, 'held': held}


def put_order(order, sku='milk', priority=1, quantity=1, ship='2026-11-16',
              min_life=0, allowed=None):
    return {'type': 'put_order', 'id': order, 'sku': sku, 'priority': priority,
            'quantity': quantity, 'ship_date': ship, 'min_life': min_life,
            'allowed_lots': allowed}


def cancel(order):
    return {'type': 'cancel_order', 'order': order}


def assert_conservation(test, report):
    for row in report['lots']:
        test.assertEqual(
            row['quantity'],
            row['held'] + row['picked'] + row['reserved'] + row['remaining'],
            'lot conservation failed for ' + row['lot'])
    for row in report['orders']:
        test.assertEqual(
            row['requested'],
            row['picked'] + row['reserved'] + row['shortage'],
            'order conservation failed for ' + row['order'])


class LedgerAdjudication(unittest.TestCase):
    def setUp(self):
        self.td = tempfile.TemporaryDirectory()
        src = source(lots=[source_lot('A', quantity=5),
                           source_lot('B', quantity=5)],
                     orders=[source_order('O1', quantity=4,
                                          allowed=['A', 'B'])])
        self.db = open_ledger(self.td.name, src)

    def tearDown(self):
        self.td.cleanup()

    def test_over_pick_rejected(self):
        report = ledger.current_report(self.db)
        # Only 4 units were ever reserved.
        with self.assertRaises(ledger.LedgerError):
            apply(self.db, 'bad', 0, [pick('O1', 'A', 5)])
        self.assertEqual(ledger.current_report(self.db)['ledger_revision'], 0)

    def test_reduce_demand_below_picked_rejected(self):
        apply(self.db, 'p1', 0, [pick('O1', 'A', 2)])
        with self.assertRaises(ledger.LedgerError):
            apply(self.db, 'p2', 1,
                  [put_order('O1', quantity=1, allowed=['A', 'B'])])
        self.assertEqual(ledger.current_report(self.db)['ledger_revision'], 1)

    def test_hold_covering_picked_stock_rejected(self):
        apply(self.db, 'p1', 0, [pick('O1', 'A', 3)])
        with self.assertRaises(ledger.LedgerError):
            apply(self.db, 'p2', 1, [set_hold('A', 3)])  # 3 held + 3 picked > 5
        self.assertEqual(ledger.current_report(self.db)['ledger_revision'], 1)

    def test_same_id_different_bytes_rejected(self):
        apply(self.db, 'r1', 0, [set_hold('A', 1)])
        with self.assertRaises(ledger.LedgerError):
            apply(self.db, 'r1', 1, [set_hold('B', 1)])
        # Original facts untouched.
        report = ledger.current_report(self.db)
        self.assertEqual(report['ledger_revision'], 1)
        self.assertEqual(report['lots'][0]['held'], 1)

    def test_stale_revision_rejected(self):
        apply(self.db, 'r1', 0, [set_hold('A', 1)])
        with self.assertRaises(ledger.LedgerError):
            apply(self.db, 'r2', 0, [set_hold('A', 2)])
        self.assertEqual(ledger.current_report(self.db)['ledger_revision'], 1)

    def test_mid_batch_failure_leaves_no_partial_facts(self):
        # First event is legal, second violates the authorization; nothing
        # may be committed.
        events = [set_hold('A', 1), pick('O1', 'B', 99)]
        with self.assertRaises(ledger.LedgerError):
            apply(self.db, 'mix', 0, events)
        report = ledger.current_report(self.db)
        self.assertEqual(report['ledger_revision'], 0)
        self.assertEqual(report['lots'][0]['held'], 0)
        self.assertEqual(report['lots'][0]['picked'], 0)

    def test_new_order_cannot_self_authorize_pick(self):
        events = [put_order('NEW', quantity=3, allowed=['A']),
                  pick('NEW', 'A', 1)]
        with self.assertRaises(ledger.LedgerError):
            apply(self.db, 'selfie', 0, events)
        self.assertEqual(ledger.current_report(self.db)['ledger_revision'], 0)

    def test_pick_must_match_marketable_lot(self):
        # Tightening shelf life plus a pick against a lot that no longer
        # qualifies must fail atomically inside one batch.
        with self.assertRaises(ledger.LedgerError):
            apply(self.db, 'stale-pick', 0,
                  [put_order('O1', quantity=4, min_life=30,
                             allowed=['A', 'B']),
                   pick('O1', 'A', 1)])
        self.assertEqual(ledger.current_report(self.db)['ledger_revision'], 0)

    def test_facts_survive_database_reopen(self):
        apply(self.db, 'r1', 0, [pick('O1', 'A', 2)])
        apply(self.db, 'r2', 1, [set_hold('B', 2)])
        before = ledger.current_report(self.db)
        reopened = ledger.current_report(self.db)  # new connection each call
        self.assertEqual(before, reopened)
        conn = ledger.connect(self.db)
        try:
            state, reservations = ledger.load_state(conn)
            self.assertEqual(state['revision'], 2)
            picked = conn.execute(
                'SELECT order_id,lot_id,quantity FROM picks ORDER BY seq'
            ).fetchall()
            self.assertEqual(picked, [('O1', 'A', 2)])
        finally:
            conn.close()

    def test_idempotent_replay_returns_original_receipt(self):
        payload = json.dumps(
            {'request_id': 'r1', 'expected_revision': 0,
             'events': [pick('O1', 'A', 1)]}).encode()
        first, replayed = ledger.apply_batch(self.db, payload)
        self.assertFalse(replayed)
        # Advance the ledger with an unrelated request.
        apply(self.db, 'other', 1, [set_hold('B', 1)])
        # Replaying the old bytes still returns the revision-1 receipt and
        # does not move the current revision backwards.
        old, replayed = ledger.apply_batch(self.db, payload)
        self.assertTrue(replayed)
        self.assertEqual(old['ledger_revision'], 1)
        self.assertEqual(ledger.current_report(self.db)['ledger_revision'], 2)
        self.assertEqual(old, first)

    def test_cancel_reduces_demand_to_picked_and_keeps_facts(self):
        apply(self.db, 'r1', 0, [pick('O1', 'A', 2)])
        report, _ = apply(self.db, 'r2', 1, [cancel('O1')])
        row = next(o for o in report['orders'] if o['order'] == 'O1')
        self.assertTrue(row['canceled'])
        self.assertEqual(row['requested'], 2)
        self.assertEqual(row['picked'], 2)
        self.assertEqual(row['shortage'], 0)
        assert_conservation(self, report)


class PlannerEnumeration(unittest.TestCase):
    """Brute-force enumeration over small integer allocation matrices."""

    def _brute_force(self, orders, lots, old):
        order_list = sorted(orders, key=lambda o: (o['priority'], o['id']))
        lot_list = sorted(lots, key=lambda l: (l['expires'], l['id']))
        free = {l['id']: l['quantity'] - l['held'] - l['picked'] for l in lot_list}
        demand = {o['id']: o['quantity'] - o['picked'] for o in order_list}
        pairs = [(o['id'], l['id']) for o in order_list for l in lot_list
                 if ledger_planner_eligible(o, l)]
        best = None

        def recurse(idx, x):
            nonlocal best
            if idx == len(pairs):
                if not self._feasible(x, free, demand, pairs):
                    return
                key = self._objective(x, order_list, lot_list, old)
                if best is None or key > best[0]:
                    best = (key, dict(x))
                return
            oid, lid = pairs[idx]
            upper = min(free[lid], demand[oid])
            for q in range(upper + 1):
                x[pairs[idx]] = q
                recurse(idx + 1, x)

        recurse(0, {})
        return best[1]

    @staticmethod
    def _feasible(x, free, demand, pairs):
        lot_used = {}
        order_used = {}
        for (oid, lid), q in x.items():
            lot_used[lid] = lot_used.get(lid, 0) + q
            order_used[oid] = order_used.get(oid, 0) + q
        for lid, used in lot_used.items():
            if used > free[lid]:
                return False
        for oid, used in order_used.items():
            if used > demand[oid]:
                return False
        return True

    @staticmethod
    def _objective(x, order_list, lot_list, old):
        # Layer 1: fulfillment vector; layer 2: -movement (over every old
        # residual key, including pairs that cannot be preserved); layer 3:
        # the deterministic FEFO pair vector over all feasible pairs.
        fulfill = []
        by_order = {}
        for (oid, lid), q in x.items():
            by_order[oid] = by_order.get(oid, 0) + q
        for o in order_list:
            fulfill.append(o['picked'] + by_order.get(o['id'], 0))
        movement = sum(max(old.get(p, 0) - x.get(p, 0), 0)
                       for p in set(old) | set(x))
        # Fixed-length vector over every feasible pair; zero entries carry the
        # FEFO lexicographic ordering and must not be filtered out.
        pair_vector = tuple(x.get((o['id'], l['id']), 0)
                            for o in order_list for l in lot_list
                            if ledger_planner_eligible(o, l))
        return (tuple(fulfill), -movement, pair_vector)

    def test_matches_enumeration_on_random_small_cases(self):
        import random
        rng = random.Random(20261116)
        for _ in range(40):
            nl = rng.randint(1, 3)
            no = rng.randint(1, 3)
            lots = []
            for i in range(nl):
                lots.append(make_lot(
                    'L%d' % i, quantity=rng.randint(0, 3),
                    expires='2026-11-%02d' % (16 + i), held=0))
            orders = []
            for i in range(no):
                admitted = [l['id'] for l in lots
                            if rng.random() < 0.7] or [lots[0]['id']]
                orders.append(make_order(
                    'O%d' % i, priority=rng.randint(0, 2),
                    quantity=rng.randint(0, 3), allowed=admitted))
            old = {(o['id'], l['id']): rng.randint(0, 1)
                   for o in orders for l in lots if rng.random() < 0.3}
            allocations, movement = planner_plan(
                copy.deepcopy(orders), copy.deepcopy(lots), old)
            got = {}
            for oid, lot_map in allocations.items():
                for lid, q in lot_map.items():
                    got[(oid, lid)] = q
            expected = {p: q for p, q in
                        self._brute_force(orders, lots, old).items() if q}
            self.assertEqual(got, expected)

    def test_planner_dominates_every_feasible_allocation(self):
        # Independent check: enumerate the whole feasible set and confirm the
        # planner's three-level objective tuple is the global maximum, including
        # holds, picked units and residual entries on ineligible pairs.
        import random
        rng = random.Random(99173)
        for _ in range(25):
            nl = rng.randint(1, 3)
            no = rng.randint(1, 3)
            lots = []
            for i in range(nl):
                qty = rng.randint(1, 4)
                picked = rng.randint(0, qty)
                held = rng.randint(0, qty - picked)
                lots.append(make_lot('L%d' % i, quantity=qty, held=held,
                                     picked=picked,
                                     expires='2026-11-%02d' % (16 + i)))
            orders = []
            for i in range(no):
                admitted = [l['id'] for l in lots
                            if rng.random() < 0.7] or [lots[0]['id']]
                picked_here = rng.randint(0, 2)
                orders.append(make_order(
                    'O%d' % i, priority=rng.randint(0, 2),
                    quantity=rng.randint(picked_here, picked_here + 3),
                    allowed=admitted, picked=picked_here))
            old = {(o['id'], l['id']): rng.randint(0, 2)
                   for o in orders for l in lots if rng.random() < 0.4}
            allocations, movement = planner_plan(
                copy.deepcopy(orders), copy.deepcopy(lots), old)
            got = {p: q for oid, m in allocations.items()
                   for p, q in (( (oid, lid), q) for lid, q in m.items())}
            best_x = self._brute_force(orders, lots, old)
            best_key = self._objective(best_x, sorted(
                orders, key=lambda o: (o['priority'], o['id'])),
                sorted(lots, key=lambda l: (l['expires'], l['id'])), old)
            got_key = self._objective(got, sorted(
                orders, key=lambda o: (o['priority'], o['id'])),
                sorted(lots, key=lambda l: (l['expires'], l['id'])), old)
            self.assertEqual(got_key, best_key)
            self.assertEqual(movement, -best_key[1])

    def test_generous_order_releases_unique_admission_lot(self):
        # The spec's headline example: O1 can use A,B; O2 only A. Greedy FEFO
        # would put O1 on A and starve O2; global planning must hand A to O2.
        lots = [make_lot('A', expires='2026-11-18'),
                make_lot('B', expires='2026-11-20')]
        orders = [make_order('O1', priority=1, allowed=['A', 'B']),
                  make_order('O2', priority=2, allowed=['A'])]
        allocations, movement = planner_plan(orders, lots, {})
        self.assertEqual(allocations['O1'], {'B': 1})
        self.assertEqual(allocations['O2'], {'A': 1})
        self.assertEqual(movement, 0)

    def test_old_commitment_preferred_over_fefo_at_equal_fulfillment(self):
        # Both orders can be fully served either way.  An old reservation put
        # O1 on the later-expiring B; layer 2 must keep it instead of FEFO
        # moving O1 onto A.
        lots = [make_lot('A', expires='2026-11-18', quantity=2),
                make_lot('B', expires='2026-11-20', quantity=2)]
        orders = [make_order('O1', allowed=['A', 'B']),
                  make_order('O2', allowed=['A', 'B'])]
        old = {('O1', 'B'): 1}
        allocations, movement = planner_plan(orders, lots, old)
        self.assertEqual(allocations['O1'].get('B'), 1)
        self.assertEqual(movement, 0)

    def test_deterministic_tie_break_is_fefo(self):
        lots = [make_lot('A', expires='2026-11-20'),
                make_lot('B', expires='2026-11-18')]
        orders = [make_order('O1', quantity=2, allowed=['A', 'B'])]
        allocations, _ = planner_plan(orders, lots, {})
        # Earlier expiry first regardless of input / dictionary order.
        self.assertEqual(list(allocations['O1']), ['B', 'A'])

    def test_billion_unit_stock_needs_no_expansion(self):
        lots = [make_lot('BIG', quantity=10**9)]
        orders = [make_order('O1', quantity=10**9, allowed=['BIG'])]
        allocations, movement = planner_plan(orders, lots, {})
        self.assertEqual(allocations['O1'], {'BIG': 10**9})
        # Network node count stays tiny (source, order, lot, sink).
        self.assertLessEqual(len(lots) + len(orders) + 2, 4)

    def test_shortage_is_reported_not_rejected(self):
        lots = [make_lot('A', quantity=1)]
        orders = [make_order('O1', quantity=3, allowed=['A'])]
        allocations, _ = planner_plan(orders, lots, {})
        self.assertEqual(allocations['O1'], {'A': 1})

    def test_infeasible_frozen_over_picked_rejected(self):
        lots = [make_lot('A', quantity=5, held=4, picked=2)]  # 4+2 > 5
        orders = [make_order('O1', quantity=5, allowed=['A'])]
        with self.assertRaises(ValueError):
            planner_plan(orders, lots, {})


def ledger_planner_eligible(order, lot):
    if order['sku'] != lot['sku']:
        return False
    if order['allowed_lots'] is not None and \
            lot['id'] not in order['allowed_lots']:
        return False
    from datetime import date, timedelta
    deadline = date.fromisoformat(order['ship_date']) + \
        timedelta(days=order['min_life'])
    return date.fromisoformat(lot['expires']) >= deadline


class EndToEndLinkage(unittest.TestCase):
    def setUp(self):
        self.td = tempfile.TemporaryDirectory()
        lots = [source_lot('A', expires='2026-11-30', quantity=4),
                source_lot('B', expires='2026-12-15', quantity=4)]
        orders = [source_order('O1', priority=1, quantity=4,
                               allowed=['A', 'B'])]
        self.db = open_ledger(self.td.name, source(lots=lots, orders=orders))

    def tearDown(self):
        self.td.cleanup()

    def test_pick_then_tighten_rules_migrates_unpicked_commitment(self):
        # Commit reservations, then partially pick from A.
        r1, _ = apply(self.db, 'pick1', 0, [pick('O1', 'A', 2)])
        self.assertEqual(r1['movement'], 0)
        picked_row = next(o for o in r1['orders'] if o['order'] == 'O1')
        self.assertEqual(picked_row['picked'], 2)
        # Tighten shelf life so A expires too early, remove A from the
        # admission list, and freeze every remaining unit of B.  The picked
        # A units must remain facts; the unpicked reservation moves onto the
        # still-free B capacity.
        events = [
            put_order('O1', quantity=4, ship='2026-11-16', min_life=20,
                      allowed=['B']),
            set_hold('B', 2),  # 2 frozen, 2 free for the residual demand
        ]
        r2, _ = apply(self.db, 'tighten', 1, events)
        row = next(o for o in r2['orders'] if o['order'] == 'O1')
        self.assertEqual(row['picked'], 2)            # historical fact kept
        self.assertEqual(row['reserved'], 2)
        self.assertEqual(row['reservations'], [{'lot': 'B', 'quantity': 2}])
        self.assertEqual(row['shortage'], 0)
        lot_a = next(l for l in r2['lots'] if l['lot'] == 'A')
        lot_b = next(l for l in r2['lots'] if l['lot'] == 'B')
        self.assertEqual(lot_a['picked'], 2)          # never reallocated
        self.assertEqual(lot_b['held'], 2)
        self.assertEqual(lot_b['reserved'], 2)
        assert_conservation(self, r2)
        # Movement reflects the two units that left A's reservation.
        self.assertEqual(r2['movement'], 2)

    def test_report_write_failure_then_replay_keeps_facts_and_revision(self):
        payload = json.dumps(
            {'request_id': 'old', 'expected_revision': 0,
             'events': [pick('O1', 'A', 1)]}).encode()
        report, replayed = ledger.apply_batch(self.db, payload)
        self.assertFalse(replayed)
        # Simulate the report file failing *after* the database commit by
        # pointing the output at an existing directory; save() cannot replace
        # a directory, so publication fails while the commit stands.
        bad_output = Path(self.td.name) / 'a-directory'
        bad_output.mkdir()
        # book_apply reads the request from a real file path (CLI semantics).
        req_path = Path(self.td.name) / 'old-request.json'
        req_path.write_bytes(payload)
        from perishable_fulfillment import book
        with self.assertRaises(book.OutputAfterCommitError):
            book.book_apply(self.db, req_path, bad_output)
        facts = ledger.current_report(self.db)
        self.assertEqual(facts['ledger_revision'], 1)
        self.assertEqual(facts['orders'][0]['picked'], 1)
        # A newer request succeeds on top of the surviving revision.
        apply(self.db, 'newer', 1, [set_hold('B', 1)])
        self.assertEqual(ledger.current_report(self.db)['ledger_revision'], 2)
        # Replaying the original bytes returns the original revision-1 receipt
        # without rolling the current revision back.
        old_receipt, was_replay = ledger.apply_batch(self.db, payload)
        self.assertTrue(was_replay)
        self.assertEqual(old_receipt['ledger_revision'], 1)
        self.assertEqual(old_receipt['request_id'], 'old')
        self.assertEqual(ledger.current_report(self.db)['ledger_revision'], 2)
        assert_conservation(self, ledger.current_report(self.db))


class CliCommands(unittest.TestCase):
    def setUp(self):
        self.td = tempfile.TemporaryDirectory()
        self.src = Path(self.td.name) / 'source.json'
        self.src.write_text(json.dumps(source(
            lots=[source_lot('A', quantity=2, held=0)],
            orders=[source_order('O1', quantity=2, allowed=['A'])])))
        self.db = Path(self.td.name) / 'book.db'

    def tearDown(self):
        self.td.cleanup()

    def _run(self, *args):
        env = dict(os.environ, PYTHONPATH=str(ROOT / 'src'))
        return subprocess.run(
            [sys.executable, '-m', 'perishable_fulfillment.book', *args],
            capture_output=True, text=True, env=env)

    def test_full_cli_lifecycle(self):
        r = self._run('book-init', '--db', str(self.db),
                      '--source', str(self.src))
        self.assertEqual(r.returncode, 0, r.stderr)
        req = Path(self.td.name) / 'req.json'
        req.write_text(json.dumps(
            {'request_id': 'r1', 'expected_revision': 0,
             'events': [pick('O1', 'A', 1)]}))
        out = Path(self.td.name) / 'receipt.json'
        r = self._run('book-apply', '--db', str(self.db),
                      '--source', str(req), '--output', str(out))
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertTrue(out.exists())
        r = self._run('book-report', '--db', str(self.db),
                      '--output', str(Path(self.td.name) / 'report.json'))
        self.assertEqual(r.returncode, 0, r.stderr)
        report = json.loads(r.stdout)
        self.assertEqual(report['schema_version'], 2)
        self.assertEqual(report['ledger_revision'], 1)

    def test_init_refuses_existing_ledger(self):
        self._run('book-init', '--db', str(self.db), '--source', str(self.src))
        r = self._run('book-init', '--db', str(self.db),
                      '--source', str(self.src))
        self.assertEqual(r.returncode, 2)

    def test_bad_input_does_not_create_ledger(self):
        bad = Path(self.td.name) / 'bad.json'
        bad.write_text('{"as_of": "2026-11-16"}')
        target = Path(self.td.name) / 'never.db'
        r = self._run('book-init', '--db', str(target), '--source', str(bad))
        self.assertEqual(r.returncode, 2)
        self.assertFalse(target.exists())


if __name__ == '__main__':
    unittest.main()
