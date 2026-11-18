"""Acceptance tests for the v4 offline retail markdown schedule analyzer.

The production optimizer (per-SKU memoized enumeration, resource-Pareto
pruning and the cross-SKU slot DP) is checked against an *independent*
exhaustive oracle on small instances: the oracle imports none of the
production candidate/pruning/DP code, enumerates every combination of
per-(date, SKU) actions with itertools, replays FEFO bulk consumption over
the original lots and applies the three-level objective directly.  Larger
instances are validated through lot-by-lot conservation replays and the
published cardinality/quantity bounds.

Coverage includes the canonical 19-point cross-horizon case, all three tie
levels, same-expiry different lot ids, different-cost batches, the
as_of/first-day/last-day expiry boundary, held and today-order deductions,
negative-margin no-sale, zero-demand discount slot occupancy, empty options,
zero inventory, 10**9 quantities without per-unit expansion, input reorder
invariance, strict input validation and CLI publication safety.
"""

import copy
import hashlib
import itertools
import json
import os
import random
import subprocess
import sys
import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path

from perishable_fulfillment.markdown import compile_report, run
from perishable_fulfillment.model import analyze

ROOT = Path(__file__).resolve().parents[1]
AS_OF = '2026-11-16'
DAY1 = '2026-11-17'
DAY2 = '2026-11-18'


# --------------------------------------------------------------------------
# Payload builders
# --------------------------------------------------------------------------

def lot(lid, sku, expires, quantity, held=0):
    return {'id': lid, 'sku': sku, 'expires': expires, 'quantity': quantity,
            'held': held}


def order(oid, sku, quantity, priority=0):
    return {'id': oid, 'sku': sku, 'priority': priority, 'quantity': quantity}


def option(oid, price, demand, discounted):
    return {'id': oid, 'price': price, 'demand': demand,
            'discounted': discounted}


def offer(dt, sku, options):
    return {'date': dt, 'sku': sku, 'options': options}


def slot(dt, slots):
    return {'date': dt, 'slots': slots}


def make(lots, orders, dates, offers, promo, costs=None):
    if costs is None:
        costs = [{'lot': l['id'], 'unit_cost': 0} for l in lots]
    return {
        'schema_version': 4,
        'fulfillment': {'as_of': AS_OF, 'lots': lots, 'orders': orders},
        'dates': dates, 'lot_costs': costs, 'offers': offers,
        'promo_slots': promo,
    }


def call(payload):
    return compile_report(payload, 'digest')


# --------------------------------------------------------------------------
# Independent exhaustive oracle (no production optimizer code)
# --------------------------------------------------------------------------

def brute_force(payload):
    """Enumerate every SKU x day action combination and replay raw lots."""
    fulfillment = payload['fulfillment']
    v1 = analyze(fulfillment)
    remaining_by_lot = {row['lot']: row['available_remaining']
                        for row in v1['lots']}
    unit_cost = {row['lot']: row['unit_cost']
                 for row in payload['lot_costs']}
    days = [date.fromisoformat(d) for d in payload['dates']]
    skus = sorted({l['sku'] for l in fulfillment['lots']}
                  | {o['sku'] for o in fulfillment['orders']})
    first_day = days[0]

    lots_by_sku = {s: [] for s in skus}
    opening = {}
    for raw in fulfillment['lots']:
        expires = date.fromisoformat(raw['expires'])
        initial = remaining_by_lot[raw['id']]
        expired_before = initial if expires < first_day else 0
        opening[raw['id']] = initial - expired_before
        lots_by_sku[raw['sku']].append(
            {'lot': raw['id'], 'expires': expires,
             'unit_cost': unit_cost[raw['id']]})
    for sku in skus:
        lots_by_sku[sku].sort(key=lambda l: (l['expires'], l['lot']))

    cells = {}
    for row in payload['offers']:
        cells[(row['date'], row['sku'])] = row['options']
    caps = {row['date']: row['slots'] for row in payload['promo_slots']}

    cell_choices = []
    for dt in days:
        for sku in skus:
            # None represents the implicit no-sale action (id "").
            cell_choices.append([None] + list(cells[(dt.isoformat(), sku)]))

    best = None
    nsku = len(skus)
    for combo in itertools.product(*cell_choices):
        discount_days = 0
        feasible = True
        for di, dt in enumerate(days):
            used = 0
            for si in range(nsku):
                act = combo[di * nsku + si]
                if act is not None and act['discounted']:
                    used += 1
            if used > caps[dt.isoformat()]:
                feasible = False
                break
        if not feasible:
            continue

        stock = dict(opening)
        total_profit = 0
        for di, dt in enumerate(days):
            for si, sku in enumerate(skus):
                act = combo[di * nsku + si]
                if act is None:
                    continue
                saleable = sum(
                    stock[l['lot']] for l in lots_by_sku[sku]
                    if stock[l['lot']] > 0 and l['expires'] >= dt)
                left = min(act['demand'], saleable)
                for l in lots_by_sku[sku]:
                    if left == 0:
                        break
                    qty = stock[l['lot']]
                    if qty <= 0 or l['expires'] < dt:
                        continue
                    take = min(left, qty)
                    stock[l['lot']] = qty - take
                    left -= take
                    total_profit += take * (act['price'] - l['unit_cost'])
            for sku in skus:
                for l in lots_by_sku[sku]:
                    if l['expires'] == dt:
                        stock[l['lot']] = 0
        discount_days = sum(
            1 for act in combo if act is not None and act['discounted'])
        ids = tuple('' if act is None else act['id'] for act in combo)
        candidate = (total_profit, discount_days, ids)
        if best is None or total_profit > best[0] or \
                (total_profit == best[0] and
                 (discount_days < best[1] or
                  (discount_days == best[1] and ids < best[2]))):
            best = candidate
    return best


def random_payload(rng):
    nsku = rng.randint(1, 3)
    horizon = rng.randint(1, 3)
    d0 = date.fromisoformat(AS_OF)
    days = [(d0 + timedelta(days=i + 1)).isoformat()
            for i in range(horizon)]
    skus = ['S%d' % i for i in range(nsku)]
    lots = []
    costs = []
    for sku in skus:
        for k in range(rng.randint(1, 3)):
            lid = 'L-%s-%d' % (sku, k)
            expires = (d0 + timedelta(days=rng.randint(0, horizon + 1))) \
                .isoformat()
            qty = rng.randint(0, 4)
            held = rng.randint(0, qty)
            lots.append(lot(lid, sku, expires, qty, held))
            costs.append({'lot': lid, 'unit_cost': rng.randint(0, 20)})
    orders = []
    if rng.random() < 0.5:
        orders.append(order('O0', rng.choice(skus), rng.randint(0, 4)))
    offers = []
    for dt in days:
        for sku in skus:
            options = []
            for k in range(rng.randint(0, 3)):
                options.append(option('o%d' % k, rng.randint(0, 25),
                                      rng.randint(0, 4),
                                      rng.random() < 0.5))
            offers.append(offer(dt, sku, options))
    promo = [slot(dt, rng.randint(0, nsku)) for dt in days]
    return make(lots, orders, days, offers, promo, costs)


def assert_conservation(test, report, payload):
    """Independently replay selected actions and reconcile every lot."""
    lots = payload['fulfillment']['lots']
    # Every horizon lot identity and balance.
    for row in report['lots']:
        test.assertEqual(
            row['initial_available'],
            row['expired_before_horizon'] + row['sold'] + row['expired']
            + row['ending'])
    # Each day: opening == sold + expired + closing and roll-forward.
    previous_closing = {row['lot']:
                        row['initial_available'] -
                        row['expired_before_horizon']
                        for row in report['lots']}
    for day in report['days']:
        for ledger in day['lot_ledger']:
            test.assertEqual(
                ledger['opening'],
                ledger['sold'] + ledger['expired'] + ledger['closing'])
            test.assertEqual(previous_closing[ledger['lot']],
                             ledger['opening'])
            previous_closing[ledger['lot']] = ledger['closing']
        # Day money equals action money; per-SKU ledger equals its action.
        test.assertEqual(
            sum(a['revenue'] for a in day['actions']), day['revenue'])
        test.assertEqual(sum(a['cost'] for a in day['actions']),
                         day['cost'])
        test.assertEqual(sum(a['profit'] for a in day['actions']),
                         day['profit'])
        sku_of_lot = {l['id']: l['sku'] for l in lots}
        for action in day['actions']:
            rows = [r for r in day['lot_ledger']
                    if sku_of_lot[r['lot']] == action['sku']]
            test.assertEqual(sum(r['sold'] for r in rows), action['sold'])
            test.assertEqual(sum(r['revenue'] for r in rows),
                             action['revenue'])
            test.assertEqual(sum(r['cost'] for r in rows), action['cost'])
            test.assertEqual(sum(r['profit'] for r in rows),
                             action['profit'])
        test.assertLessEqual(day['promo_used'],
                             next(s['slots'] for s in payload['promo_slots']
                                  if s['date'] == day['date']))
    # Horizon totals.
    test.assertEqual(sum(d['profit'] for d in report['days']),
                     report['objective']['profit'])
    test.assertEqual(sum(d['promo_used'] for d in report['days']),
                     report['objective']['discount_sku_days'])
    test.assertEqual(
        sum(l['profit'] for l in report['lots']),
        report['objective']['profit'])


# --------------------------------------------------------------------------
# Canonical cross-horizon case
# --------------------------------------------------------------------------

class CanonicalCrossHorizon(unittest.TestCase):
    def payload(self):
        # Two days, one slot per day, no orders or held stock.
        # A: 1 unit expiring day 2; B: 1 unit expiring day 1.
        lots = [lot('LA', 'A', DAY2, 1), lot('LB', 'B', DAY1, 1)]
        offers = [
            offer(DAY1, 'A', [option('a11', 11, 1, True)]),
            offer(DAY2, 'A', [option('a10', 10, 1, True)]),
            offer(DAY1, 'B', [option('b9', 9, 1, True)]),
            offer(DAY2, 'B', []),
        ]
        promo = [slot(DAY1, 1), slot(DAY2, 1)]
        return make(lots, [], [DAY1, DAY2], offers, promo)

    def test_joint_optimum_sells_b_then_a_for_19(self):
        report = call(self.payload())
        self.assertEqual(report['objective']['profit'], 19)
        self.assertEqual(report['objective']['discount_sku_days'], 2)
        # Interleaved (date, SKU): day1 A,B then day2 A,B.
        self.assertEqual(report['objective']['action_ids'],
                         ['', 'b9', 'a10', ''])
        first, second = report['days']
        self.assertEqual([a['action_id'] for a in first['actions']],
                         ['', 'b9'])
        self.assertEqual([a['action_id'] for a in second['actions']],
                         ['a10', ''])
        assert_conservation(self, report, self.payload())

    def test_matches_independent_oracle(self):
        payload = self.payload()
        self.assertEqual(
            (19, 2, ('', 'b9', 'a10', '')), brute_force(payload))


# --------------------------------------------------------------------------
# Business semantics
# --------------------------------------------------------------------------

class ExpiryAndStockSemantics(unittest.TestCase):
    def test_expires_on_as_of_is_expired_before_horizon(self):
        # v1 still counts an as_of-expiring lot in available_remaining, but
        # the horizon starts the next day, so it never enters an opening.
        payload = make(
            [lot('L', 'A', AS_OF, 5)], [], [DAY1],
            [offer(DAY1, 'A', [option('p', 10, 9, False)])], [slot(DAY1, 6)])
        report = call(payload)
        row = report['lots'][0]
        self.assertEqual(row['initial_available'], 5)
        self.assertEqual(row['expired_before_horizon'], 5)
        self.assertEqual(row['sold'], 0)
        self.assertEqual(report['days'][0]['lot_ledger'][0]['opening'], 0)

    def test_first_day_expiry_sells_then_expires_remainder(self):
        payload = make(
            [lot('L', 'A', DAY1, 5)], [], [DAY1, DAY2],
            [offer(DAY1, 'A', [option('p', 10, 2, False)]),
             offer(DAY2, 'A', [option('p', 10, 2, False)])],
            [slot(DAY1, 6), slot(DAY2, 6)])
        report = call(payload)
        ledger = report['days'][0]['lot_ledger'][0]
        self.assertEqual((ledger['opening'], ledger['sold'],
                          ledger['expired'], ledger['closing']),
                         (5, 2, 3, 0))
        self.assertEqual(report['days'][1]['lot_ledger'][0]['opening'], 0)

    def test_last_day_expiry_carries_then_expires(self):
        payload = make(
            [lot('L', 'A', DAY2, 5)], [], [DAY1, DAY2],
            [offer(DAY1, 'A', [option('p', 10, 1, False)]),
             offer(DAY2, 'A', [option('p', 10, 1, False)])],
            [slot(DAY1, 6), slot(DAY2, 6)])
        report = call(payload)
        self.assertEqual(report['days'][0]['lot_ledger'][0]['closing'], 4)
        last = report['days'][1]['lot_ledger'][0]
        self.assertEqual((last['sold'], last['expired']), (1, 3))

    def test_held_and_today_order_reduce_initial_available(self):
        payload = make(
            [lot('L', 'A', DAY2, 10, held=2)],
            [order('O1', 'A', 3)], [DAY1],
            [offer(DAY1, 'A', [option('p', 10, 100, False)])],
            [slot(DAY1, 6)])
        report = call(payload)
        # qty 10 - held 2 - order 3 = 5 available for future retail.
        self.assertEqual(report['lots'][0]['initial_available'], 5)

    def test_fefo_tie_break_by_lot_id_uses_actual_costs(self):
        lots = [lot('LZ', 'A', DAY2, 2), lot('LA', 'A', DAY2, 2)]
        costs = [{'lot': 'LZ', 'unit_cost': 8},
                 {'lot': 'LA', 'unit_cost': 1}]
        payload = make(lots, [], [DAY1],
                       [offer(DAY1, 'A', [option('p', 10, 3, False)])],
                       [slot(DAY1, 6)], costs)
        report = call(payload)
        by = {r['lot']: r for r in report['days'][0]['lot_ledger']}
        # LA (smaller id) is consumed first: 2 units, then 1 from LZ.
        self.assertEqual(by['LA']['sold'], 2)
        self.assertEqual(by['LZ']['sold'], 1)
        # 2*(10-1) + 1*(10-8) = 18 + 2 = 20, not an average-cost result.
        self.assertEqual(report['days'][0]['actions'][0]['profit'], 20)
        self.assertEqual(by['LA']['cost'], 2)
        self.assertEqual(by['LZ']['cost'], 8)

    def test_negative_margin_chooses_no_sale(self):
        costs = [{'lot': 'L', 'unit_cost': 100}]
        payload = make([lot('L', 'A', DAY2, 5)], [], [DAY1],
                       [offer(DAY1, 'A', [option('p', 10, 5, True)])],
                       [slot(DAY1, 1)], costs)
        report = call(payload)
        self.assertEqual(report['objective']['profit'], 0)
        self.assertEqual(report['days'][0]['actions'][0]['action_id'], '')
        self.assertEqual(report['objective']['discount_sku_days'], 0)

    def test_zero_inventory_only_no_sale(self):
        payload = make([lot('L', 'A', DAY2, 0)], [], [DAY1],
                       [offer(DAY1, 'A', [option('p', 10, 3, False)])],
                       [slot(DAY1, 6)])
        report = call(payload)
        action = report['days'][0]['actions'][0]
        self.assertEqual(action['action_id'], '')
        self.assertEqual(action['sold'], 0)
        assert_conservation(self, report, payload)

    def test_empty_options_force_no_sale_that_day(self):
        payload = make(
            [lot('L', 'A', DAY2, 3)], [], [DAY1, DAY2],
            [offer(DAY1, 'A', []),
             offer(DAY2, 'A', [option('p', 10, 3, False)])],
            [slot(DAY1, 6), slot(DAY2, 6)])
        report = call(payload)
        self.assertEqual(report['days'][0]['actions'][0]['action_id'], '')
        self.assertEqual(report['days'][1]['actions'][0]['sold'], 3)

    def test_unmet_demand_and_zero_demand_tie_to_no_sale(self):
        payload = make([lot('L', 'A', DAY2, 2)], [], [DAY1],
                       [offer(DAY1, 'A', [option('p', 10, 5, False)])],
                       [slot(DAY1, 6)])
        report = call(payload)
        action = report['days'][0]['actions'][0]
        self.assertEqual((action['sold'], action['unmet']), (2, 3))
        # With zero demand the public action and no-sale both yield zero and
        # neither uses a discount slot; the empty no-sale id sorts first.
        payload2 = make(
            [lot('L', 'A', DAY2, 2)], [], [DAY1],
            [offer(DAY1, 'A', [option('a', 10, 0, False)])],
            [slot(DAY1, 6)])
        report2 = call(payload2)
        action2 = report2['days'][0]['actions'][0]
        self.assertEqual(action2['action_id'], '')
        self.assertEqual((action2['sold'], action2['unmet']), (0, 0))

    def test_later_day_sale_dominates_early_discount(self):
        # One unit expires day 2; the higher day-2 price wins, day 1 chooses
        # no-sale because the discounted early option only forfeits profit.
        payload = make(
            [lot('L', 'A', DAY2, 1)], [], [DAY1, DAY2],
            [offer(DAY1, 'A', [option('early', 5, 1, True)]),
             offer(DAY2, 'A', [option('late', 99, 1, False)])],
            [slot(DAY1, 1), slot(DAY2, 1)])
        report = call(payload)
        self.assertEqual(report['days'][0]['actions'][0]['action_id'], '')
        self.assertEqual(report['days'][1]['actions'][0]['action_id'],
                         'late')
        self.assertEqual(report['objective']['profit'], 99)


# --------------------------------------------------------------------------
# Three tie levels and slot competition
# --------------------------------------------------------------------------

class ObjectiveTieLevels(unittest.TestCase):
    def test_level_one_profit_beats_discount_count(self):
        # A profitable discounted option must beat a smaller no-discount one.
        payload = make(
            [lot('L', 'A', DAY2, 5)], [], [DAY1],
            [offer(DAY1, 'A', [option('rich', 100, 5, True),
                               option('poor', 1, 5, False)])],
            [slot(DAY1, 1)])
        report = call(payload)
        self.assertEqual(report['days'][0]['actions'][0]['action_id'],
                         'rich')

    def test_level_two_fewer_discounts_when_profit_ties(self):
        # Same profit; the non-discounted option avoids occupying a slot.
        payload = make(
            [lot('L', 'A', DAY2, 5)], [], [DAY1],
            [offer(DAY1, 'A', [option('d', 10, 5, True),
                               option('n', 10, 5, False)])],
            [slot(DAY1, 6)])
        report = call(payload)
        self.assertEqual(report['days'][0]['actions'][0]['action_id'], 'n')
        self.assertEqual(report['objective']['discount_sku_days'], 0)

    def test_level_three_lexicographic_id_when_profit_and_discount_tie(self):
        payload = make(
            [lot('L', 'A', DAY2, 5)], [], [DAY1],
            [offer(DAY1, 'A', [option('b', 10, 5, False),
                               option('a', 10, 5, False)])],
            [slot(DAY1, 6)])
        report = call(payload)
        self.assertEqual(report['days'][0]['actions'][0]['action_id'], 'a')

    def test_empty_string_sorts_before_public_id_on_full_tie(self):
        # Zero stock: every public action earns zero and uses no discount
        # here, so the no-sale id "" must win the lexicographic tie.
        payload = make(
            [lot('L', 'A', DAY2, 0)], [], [DAY1],
            [offer(DAY1, 'A', [option('x', 10, 5, False),
                               option('y', 10, 5, False)])],
            [slot(DAY1, 6)])
        report = call(payload)
        self.assertEqual(report['days'][0]['actions'][0]['action_id'], '')


class SlotCompetition(unittest.TestCase):
    def test_shared_daily_slot_forces_sku_to_non_discount(self):
        lots = [lot('L0', 'S00', DAY1, 5), lot('L1', 'S01', DAY1, 5)]
        offers = [
            offer(DAY1, 'S00', [option('d0', 10, 5, True),
                                option('n0', 8, 5, False)]),
            offer(DAY1, 'S01', [option('d1', 10, 5, True),
                                option('n1', 8, 5, False)])]
        payload = make(lots, [], [DAY1], offers, [slot(DAY1, 1)])
        report = call(payload)
        self.assertEqual(report['days'][0]['promo_used'], 1)
        discounted = [a for a in report['days'][0]['actions']
                      if a['discounted']]
        self.assertEqual(len(discounted), 1)
        # Lexicographic SKU order gives the slot to S00; S01 still sells via
        # its non-discounted option, preserving total profit.
        self.assertEqual(discounted[0]['sku'], 'S00')
        self.assertEqual(sum(a['profit'] for a in
                             report['days'][0]['actions']), 50 + 40)

    def test_zero_demand_discount_does_not_displace_profitable_one(self):
        lots = [lot('L0', 'S00', DAY1, 5), lot('L1', 'S01', DAY1, 5)]
        offers = [
            offer(DAY1, 'S00', [option('zd', 10, 0, True)]),
            offer(DAY1, 'S01', [option('pd', 10, 5, True)])]
        payload = make(lots, [], [DAY1], offers, [slot(DAY1, 1)])
        report = call(payload)
        actions = report['days'][0]['actions']
        chosen = {a['sku']: a['action_id'] for a in actions}
        self.assertEqual(chosen['S00'], '')
        self.assertEqual(chosen['S01'], 'pd')
        self.assertEqual(report['days'][0]['promo_used'], 1)

    def test_discount_occupies_slot_independent_of_units_sold(self):
        # A discounted action with demand far above stock still occupies the
        # slot even though most demand is unmet; occupancy is an action-level
        # property, never scaled by sold units.
        lots = [lot('L0', 'S00', DAY1, 2), lot('L1', 'S01', DAY1, 2)]
        offers = [
            offer(DAY1, 'S00', [option('d0', 10, 9, True),
                                option('n0', 8, 9, False)]),
            offer(DAY1, 'S01', [option('d1', 10, 9, True),
                                option('n1', 8, 9, False)])]
        payload = make(lots, [], [DAY1], offers, [slot(DAY1, 1)])
        report = call(payload)
        day = report['days'][0]
        self.assertEqual(day['promo_used'], 1)
        discounted = next(a for a in day['actions'] if a['discounted'])
        self.assertEqual(discounted['sold'], 2)
        self.assertEqual(discounted['unmet'], 7)
        # The other SKU falls back to its non-discounted public option and
        # still sells, so total profit is preserved without a second slot.
        self.assertEqual(
            sorted(a['action_id'] for a in day['actions']),
            ['d0', 'n1'])

    def test_profitless_discount_is_never_preferred_to_no_sale(self):
        # Every option earns zero and there is no stock to sell; level 2
        # minimizes discount count, so all-no-sale wins and no slot is used.
        # This pins why a zero-demand discount only occupies a slot when it is
        # genuinely part of the objective-winning schedule.
        lots = [lot('L0', 'S00', DAY1, 0), lot('L1', 'S01', DAY1, 0)]
        offers = [
            offer(DAY1, 'S00', [option('zd0', 10, 0, True)]),
            offer(DAY1, 'S01', [option('zd1', 10, 0, True)])]
        payload = make(lots, [], [DAY1], offers, [slot(DAY1, 1)])
        report = call(payload)
        self.assertEqual(report['days'][0]['promo_used'], 0)
        self.assertEqual(
            [a['action_id'] for a in report['days'][0]['actions']],
            ['', ''])


# --------------------------------------------------------------------------
# Large quantities and published upper bounds (no per-unit expansion)
# --------------------------------------------------------------------------

class LargeScale(unittest.TestCase):
    def test_billion_quantity_single_bulk_transaction(self):
        costs = [{'lot': 'L', 'unit_cost': 30}]
        payload = make([lot('L', 'A', DAY2, 10 ** 9)], [], [DAY1],
                       [offer(DAY1, 'A', [option('p', 100, 10 ** 9,
                                                  False)])],
                       [slot(DAY1, 1)], costs)
        report = call(payload)
        action = report['days'][0]['actions'][0]
        self.assertEqual(action['sold'], 10 ** 9)
        self.assertEqual(action['revenue'], 100 * 10 ** 9)
        self.assertEqual(action['cost'], 30 * 10 ** 9)
        self.assertEqual(action['profit'], 70 * 10 ** 9)
        self.assertEqual(report['lots'][0]['sold'], 10 ** 9)

    def test_published_maximum_bounds_complete_and_conservative(self):
        # 6 SKUs, 8 consecutive days, 4 lots per SKU, 4 options per cell,
        # slots 6, billion-unit stock and large (non-zero) demand.
        d0 = date.fromisoformat(AS_OF)
        horizon = 8
        days = [(d0 + timedelta(days=i + 1)).isoformat()
                for i in range(horizon)]
        skus = ['S%02d' % i for i in range(6)]
        lots = []
        costs = []
        rng = random.Random(20261118)
        for si, sku in enumerate(skus):
            for k in range(4):
                lid = 'L-%s-%d' % (sku, k)
                expires = (d0 + timedelta(
                    days=1 + (k % (horizon + 2)))).isoformat()
                lots.append(lot(lid, sku, expires, 10 ** 9))
                costs.append({'lot': lid, 'unit_cost': rng.randint(0, 50)})
        offers = []
        for dt in days:
            for sku in skus:
                options = [option('o%d' % k, 60 + k * 10, 10 ** 9,
                                  k % 2 == 0) for k in range(4)]
                offers.append(offer(dt, sku, options))
        promo = [slot(dt, 6) for dt in days]
        payload = make(lots, [], days, offers, promo, costs)
        report = call(payload)
        self.assertEqual(len(report['days']), 8)
        self.assertEqual(len(report['lots']), 24)
        self.assertEqual(
            len(report['objective']['action_ids']), 8 * 6)
        for day in report['days']:
            self.assertEqual(len(day['actions']), 6)
            self.assertEqual(len(day['lot_ledger']), 24)
            self.assertLessEqual(day['promo_used'], 6)
        assert_conservation(self, report, payload)


# --------------------------------------------------------------------------
# Independent exhaustive oracle over randomized small instances
# --------------------------------------------------------------------------

class ExhaustiveOracle(unittest.TestCase):
    def test_random_small_instances_match_brute_force(self):
        rng = random.Random(20261118)
        for _ in range(120):
            payload = random_payload(rng)
            report = call(payload)
            expected = brute_force(payload)
            got = (report['objective']['profit'],
                   report['objective']['discount_sku_days'],
                   tuple(report['objective']['action_ids']))
            self.assertEqual(got, expected)
            assert_conservation(self, report, payload)

    def test_tie_adversarial_instances_match_brute_force(self):
        rng = random.Random(424242)
        ids = ['aa', 'mm', 'zz', 'b', 'q']
        for _ in range(60):
            nsku = rng.randint(2, 3)
            horizon = rng.randint(1, 2)
            d0 = date.fromisoformat(AS_OF)
            days = [(d0 + timedelta(days=i + 1)).isoformat()
                    for i in range(horizon)]
            skus = ['k%d' % i for i in range(nsku)]
            lots = []
            costs = []
            for sku in skus:
                for k in range(rng.randint(1, 3)):
                    lid = '%s-%d' % (sku, k)
                    expires = (d0 + timedelta(
                        days=rng.randint(0, horizon + 1))).isoformat()
                    qty = rng.randint(0, 3)
                    lots.append(lot(lid, sku, expires, qty,
                                    rng.randint(0, qty)))
                    costs.append({'lot': lid,
                                  'unit_cost': rng.choice([0, 1, 5, 10])})
            offers = []
            for dt in days:
                for sku in skus:
                    chosen = rng.sample(ids, rng.randint(0, 3))
                    options = [option(oid, rng.choice([0, 5, 10, 20]),
                                      rng.randint(0, 3),
                                      rng.random() < 0.6)
                               for oid in chosen]
                    offers.append(offer(dt, sku, options))
            promo = [slot(dt, rng.randint(0, min(3, nsku)))
                     for dt in days]
            payload = make(lots, [], days, offers, promo, costs)
            report = call(payload)
            got = (report['objective']['profit'],
                   report['objective']['discount_sku_days'],
                   tuple(report['objective']['action_ids']))
            self.assertEqual(got, brute_force(payload))
            assert_conservation(self, report, payload)


# --------------------------------------------------------------------------
# Strict input contract
# --------------------------------------------------------------------------

class InputContract(unittest.TestCase):
    def base(self):
        lots = [lot('LA', 'A', DAY2, 3), lot('LB', 'B', DAY1, 2, held=1)]
        orders = [order('O1', 'A', 1)]
        offers = [
            offer(DAY1, 'A', [option('a', 10, 2, True)]),
            offer(DAY2, 'A', [option('a2', 9, 2, False)]),
            offer(DAY1, 'B', [option('b', 12, 1, False)]),
            offer(DAY2, 'B', [])]
        promo = [slot(DAY1, 1), slot(DAY2, 1)]
        return make(lots, orders, [DAY1, DAY2], offers, promo)

    def bad(self, mutate):
        payload = self.base()
        mutate(payload)
        with self.assertRaises((ValueError, TypeError)):
            call(payload)

    def test_valid_baseline_runs(self):
        self.assertIsInstance(call(self.base())['objective']['profit'], int)

    def test_top_level_fields(self):
        self.bad(lambda p: p.pop('offers'))
        self.bad(lambda p: p.update(extra=1))
        self.bad(lambda p: p.update(schema_version='4'))
        self.bad(lambda p: p.update(schema_version=3))
        self.bad(lambda p: p.update(schema_version=True))

    def test_dates(self):
        self.bad(lambda p: p.update(dates=[DAY2, DAY1]))
        self.bad(lambda p: p.update(dates=[DAY1, DAY1]))
        self.bad(lambda p: p.update(dates=[DAY1, '2026-11-19']))
        self.bad(lambda p: p.update(dates=['2026-1-17', DAY2]))
        self.bad(lambda p: p.update(dates=[DAY2, '2026-11-19']))

    def test_lot_costs(self):
        self.bad(lambda p: p['lot_costs'][0].update(unit_cost=-1))
        self.bad(lambda p: p['lot_costs'][0].update(unit_cost=10 ** 9 + 1))
        self.bad(lambda p: p['lot_costs'][0].update(unit_cost=True))
        self.bad(lambda p: p['lot_costs'][0].update(unit_cost='5'))
        self.bad(lambda p: p['lot_costs'].pop())
        self.bad(lambda p: p['lot_costs'].append(
            dict(p['lot_costs'][0])))
        self.bad(lambda p: p['lot_costs'][0].update(lot='NOPE'))
        self.bad(lambda p: p['lot_costs'][0].pop('unit_cost'))

    def test_offers_and_options(self):
        self.bad(lambda p: p['offers'][0]['options'][0].update(id=''))
        self.bad(lambda p: p['offers'][0]['options'].append(
            dict(p['offers'][0]['options'][0])))
        self.bad(lambda p: p['offers'][0]['options'][0]
                 .update(discounted='true'))
        self.bad(lambda p: p['offers'][0]['options'][0].update(price=1.0))
        self.bad(lambda p: p['offers'][0]['options'][0].update(demand=True))
        self.bad(lambda p: p['offers'][0].update(sku='C'))
        self.bad(lambda p: p['offers'][0].pop('sku'))
        self.bad(lambda p: p['offers'][0]['options'][0].update(extra=1))
        self.bad(lambda p: p['offers'].append(dict(p['offers'][0])))

    def test_promo_slots(self):
        self.bad(lambda p: p['promo_slots'][0].update(slots=7))
        self.bad(lambda p: p['promo_slots'][0].update(slots=-1))
        self.bad(lambda p: p['promo_slots'].pop())
        self.bad(lambda p: p['promo_slots'][0].update(date='2026-11-19'))

    def test_fulfillment_v1_shape_is_unchanged(self):
        self.bad(lambda p: p['fulfillment']['lots'][0].update(unit_cost=3))
        self.bad(lambda p: p['fulfillment']['lots'][0].pop('held'))

    def test_cardinality_limits(self):
        def build(nsku, lots_per_sku):
            d0 = date.fromisoformat(AS_OF)
            days = [DAY1, DAY2]
            skus = ['S%d' % i for i in range(nsku)]
            lots = []
            costs = []
            for sku in skus:
                for k in range(lots_per_sku):
                    lid = '%s-%d' % (sku, k)
                    lots.append(lot(lid, sku, DAY2, 1))
                    costs.append({'lot': lid, 'unit_cost': 1})
            offers = []
            for dt in days:
                for sku in skus:
                    offers.append(offer(dt, sku,
                                        [option('x', 2, 1, True)]))
            return make(lots, [], days, offers,
                        [slot(dt, 6) for dt in days], costs)
        call(build(6, 4))  # both maxima are accepted
        with self.assertRaises(ValueError):
            call(build(7, 1))
        with self.assertRaises(ValueError):
            call(build(1, 5))


# --------------------------------------------------------------------------
# Reorder invariance and deterministic replay
# --------------------------------------------------------------------------

class Determinism(unittest.TestCase):
    def test_input_array_reorder_keeps_business_result(self):
        rng = random.Random(7)
        payload = random_payload(rng)
        first = call(payload)
        shuffled = copy.deepcopy(payload)
        rng.shuffle(shuffled['fulfillment']['lots'])
        rng.shuffle(shuffled['lot_costs'])
        rng.shuffle(shuffled['offers'])
        rng.shuffle(shuffled['promo_slots'])
        second = call(shuffled)
        self.assertEqual(first['objective'], second['objective'])
        self.assertEqual(first['days'], second['days'])
        self.assertEqual(first['lots'], second['lots'])

    def test_report_is_byte_stable_for_identical_bytes(self):
        payload = random_payload(random.Random(11))
        raw = json.dumps(payload, sort_keys=True).encode('utf-8')
        with tempfile.TemporaryDirectory() as td:
            src = Path(td) / 'in.json'
            out = Path(td) / 'out.json'
            src.write_bytes(raw)
            self._run(src, out)
            first = out.read_bytes()
            self._run(src, out)
            self.assertEqual(first, out.read_bytes())
            report = json.loads(first)
            self.assertEqual(report['source_sha256'],
                             hashlib.sha256(raw).hexdigest())

    def _run(self, src, out):
        result = subprocess.run(
            [sys.executable, '-m', 'perishable_fulfillment.markdown',
             '--source', str(src), '--output', str(out)],
            capture_output=True, text=True, cwd=str(ROOT))
        self.assertEqual(result.returncode, 0, result.stderr)


# --------------------------------------------------------------------------
# CLI publication safety
# --------------------------------------------------------------------------

class Cli(unittest.TestCase):
    def _payload(self):
        lots = [lot('LA', 'A', DAY2, 1), lot('LB', 'B', DAY1, 1)]
        offers = [
            offer(DAY1, 'A', [option('a11', 11, 1, True)]),
            offer(DAY2, 'A', [option('a10', 10, 1, True)]),
            offer(DAY1, 'B', [option('b9', 9, 1, True)]),
            offer(DAY2, 'B', [])]
        return make(lots, [], [DAY1, DAY2], offers,
                    [slot(DAY1, 1), slot(DAY2, 1)])

    def _run(self, src, out):
        return subprocess.run(
            [sys.executable, '-m', 'perishable_fulfillment.markdown',
             '--source', str(src), '--output', str(out)],
            capture_output=True, text=True, cwd=str(ROOT))

    def test_cli_writes_report_and_echoes_objective(self):
        with tempfile.TemporaryDirectory() as td:
            src = Path(td) / 'in.json'
            out = Path(td) / 'out.json'
            src.write_text(json.dumps(self._payload()))
            result = self._run(src, out)
            self.assertEqual(result.returncode, 0, result.stderr)
            report = json.loads(out.read_text())
            self.assertEqual(report['schema_version'], 4)
            self.assertEqual(report['objective']['profit'], 19)
            self.assertEqual(json.loads(result.stdout)['objective'],
                             report['objective'])

    def test_failure_exit_2_preserves_prior_report(self):
        with tempfile.TemporaryDirectory() as td:
            src = Path(td) / 'in.json'
            out = Path(td) / 'out.json'
            src.write_text(json.dumps(self._payload()))
            self.assertEqual(self._run(src, out).returncode, 0)
            prior = out.read_bytes()
            for body in ['{}', '{"a":1,"a":2}', 'NaN',
                         json.dumps({**self._payload(),
                                     'schema_version': 3})]:
                src.write_text(body)
                result = self._run(src, out)
                self.assertEqual(result.returncode, 2)
                self.assertEqual(out.read_bytes(), prior)

    def test_source_output_and_links_rejected(self):
        with tempfile.TemporaryDirectory() as td:
            src = Path(td) / 'in.json'
            src.write_text(json.dumps(self._payload()))
            before = src.read_bytes()
            self.assertEqual(self._run(src, src).returncode, 2)
            hard = Path(td) / 'hard.json'
            os.link(src, hard)
            self.assertEqual(self._run(src, hard).returncode, 2)
            sym = Path(td) / 'sym.json'
            sym.symlink_to(src)
            self.assertEqual(self._run(src, sym).returncode, 2)
            out_sym = Path(td) / 'outsym.json'
            out_sym.symlink_to(Path(td) / 'target.json')
            self.assertEqual(self._run(src, out_sym).returncode, 2)
            self.assertEqual(src.read_bytes(), before)


class FixtureContract(unittest.TestCase):
    def test_documented_fixture(self):
        report = run(ROOT / 'fixtures' / 'markdown_input.json')
        self.assertEqual(report['schema_version'], 4)
        self.assertEqual(report['objective']['profit'], 19)
        self.assertEqual(report['objective']['action_ids'],
                         ['', 'b9', 'a10', ''])
        assert_conservation(self, report,
                            json.loads((ROOT / 'fixtures' /
                                        'markdown_input.json').read_text()))


if __name__ == '__main__':
    unittest.main()
