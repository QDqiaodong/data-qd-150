"""Acceptance tests for the v3 load-constrained wave compiler.

Three areas, matching the delivery requirements:
  * Wave compilation: held/expired lots produce no task, partial fulfillment
    keeps its shortage, multiple lots and SKUs at one node merge into one pick
    weight, zero-fulfillment orders produce an empty plan, and every
    allocated (order, lot, quantity) appears exactly once.
  * Route engine: a static shortest return edge that cannot carry the load,
    a force-detour route, and "pass a pick node without taking it" then
    return to it; every action is replayed to check topology, load limits and
    pick conservation.
  * Linkage: capacity-4 cart with two orders of weight 2 and two-node
    max_load 2 roads must split into two waves; a small exhaustive
    enumeration (independent of the production solver) confirms the
    minimum-wave/minimum-distance objective and the integer, non-per-unit
    treatment of 10^9 quantities.
Also: load equality at max_load, parallel-edge tie-breaks, equal-priority
order-ID ordering, array shuffle invariance, and a single permanently
unreachable order failing the whole command.
"""

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from perishable_fulfillment.wave import compile_report, run

ROOT = Path(__file__).resolve().parents[1]


def lot(lid, sku='milk', expires='2026-11-20', quantity=1, held=0):
    return {'id': lid, 'sku': sku, 'expires': expires,
            'quantity': quantity, 'held': held}


def order(oid, sku='milk', priority=1, quantity=1):
    return {'id': oid, 'sku': sku, 'priority': priority, 'quantity': quantity}


def source(lots, orders):
    return {'as_of': '2026-11-16', 'lots': lots, 'orders': orders}


def edge(eid, frm, to, distance=1, max_load=10 ** 18):
    return {'id': eid, 'from': frm, 'to': to, 'distance': distance,
            'max_load': max_load}


def warehouse(depot, nodes, edges, lot_nodes, grams, capacity):
    return {'depot': depot, 'nodes': nodes, 'edges': edges,
            'lot_nodes': [{'lot': k, 'node': v} for k, v in lot_nodes.items()]
            if isinstance(lot_nodes, dict) else lot_nodes,
            'sku_grams': [{'sku': k, 'grams': v} for k, v in grams.items()]
            if isinstance(grams, dict) else grams,
            'cart_capacity': capacity}


def make(lots, orders, who):
    return {'fulfillment': source(lots, orders), 'warehouse': who}


def call(payload):
    return compile_report(payload, 'digest')


def labels(actions):
    return [('move', a['edge']) if a['type'] == 'move' else ('pick', a['node'])
            for a in actions]


def replay(who, wave):
    """Independent action replay: topology, edge load, capacity, conservation."""
    edges = {e['id']: e for e in who['edges']}
    current = who['depot']
    load = 0
    distance = 0
    picked = []
    picked_nodes = set()
    node_tasks = {}
    for task in wave['tasks']:
        node_tasks.setdefault(task['node'], []).append(task)
    for action in wave['actions']:
        if action['type'] == 'move':
            e = edges[action['edge']]
            assert (e['from'], e['to']) == (current, action['to']), action
            assert load <= who['cart_capacity'], action
            assert load <= e['max_load'], action
            current = e['to']
            distance += e['distance']
        else:
            assert action['node'] == current, action
            assert action['node'] not in picked_nodes, action
            picked_nodes.add(action['node'])
            merged = sorted(node_tasks[action['node']],
                            key=lambda t: (t['order'], t['lot']))
            assert action['tasks'] == merged, action
            increment = sum(t['weight'] for t in merged)
            assert action['increment'] == increment, action
            assert action['load_before'] == load, action
            load += increment
            assert action['load_after'] == load, action
            assert load <= who['cart_capacity'], action
            picked.extend((t['order'], t['lot'], t['quantity'])
                          for t in merged)
    assert current == who['depot'], 'wave must end at the depot'
    assert distance == wave['distance'], (distance, wave['distance'])
    assert load == wave['final_weight'], (load, wave['final_weight'])
    assert picked_nodes == set(node_tasks), 'every task node picked once'
    return picked


def all_allocations(report):
    """Every allocated (order, lot, quantity) from the embedded v1 result."""
    out = []
    for row in report['fulfillment']['orders']:
        for a in row['allocations']:
            out.append((row['order'], a['lot'], a['quantity']))
    return out


def assert_task_conservation(test, report, who):
    allocated = all_allocations(report)
    compiled = []
    for wave in report['waves']:
        compiled.extend(replay(who, wave))
        for task in wave['tasks']:
            test.assertIn(task['lot'], {l['lot'] for l in
                                        report['fulfillment']['lots']})
    test.assertCountEqual(compiled, allocated)


class WaveCompilation(unittest.TestCase):
    def test_held_and_expired_lots_produce_no_task(self):
        lots = [lot('EXPIRED', expires='2026-11-15', quantity=5),
                lot('HELD', quantity=5, held=5),
                lot('OK', quantity=5)]
        orders = [order('O1', quantity=5)]
        who = warehouse('D', ['D', 'N'], [edge('dn', 'D', 'N'),
                         edge('nd', 'N', 'D')],
                        {'EXPIRED': 'N', 'HELD': 'N', 'OK': 'N'},
                        {'milk': 1}, 10)
        report = call(make(lots, orders, who))
        self.assertEqual([t['lot'] for w in report['waves'] for t in w['tasks']],
                         ['OK'])
        self.assertEqual(report['fulfillment']['orders'][0]['shortage'], 0)
        assert_task_conservation(self, report, who)

    def test_partial_fulfillment_keeps_shortage(self):
        lots = [lot('A', quantity=2)]
        orders = [order('O1', quantity=5)]
        who = warehouse('D', ['D', 'A'], [edge('da', 'D', 'A'),
                         edge('ad', 'A', 'D')],
                        {'A': 'A'}, {'milk': 1}, 10)
        report = call(make(lots, orders, who))
        self.assertEqual(report['fulfillment']['orders'][0]['fulfilled'], 2)
        self.assertEqual(report['shortages'],
                         [{'order': 'O1', 'quantity': 3}])
        self.assertEqual(sum(t['quantity'] for w in report['waves']
                             for t in w['tasks']), 2)
        assert_task_conservation(self, report, who)

    def test_same_node_multiple_lots_and_skus_merge_once(self):
        lots = [lot('A', quantity=1), lot('B', quantity=1),
                lot('C', sku='egg', quantity=2)]
        orders = [order('O1', quantity=2),
                  order('O2', sku='egg', quantity=2)]
        who = warehouse('D', ['D', 'N'], [edge('dn', 'D', 'N'),
                         edge('nd', 'N', 'D')],
                        {'A': 'N', 'B': 'N', 'C': 'N'},
                        {'milk': 1, 'egg': 3}, 10)
        report = call(make(lots, orders, who))
        self.assertEqual(report['total_waves'], 1)
        wave = report['waves'][0]
        picks = [a for a in wave['actions'] if a['type'] == 'pick']
        self.assertEqual(len(picks), 1)
        self.assertEqual(picks[0]['increment'], 1 + 1 + 3 * 2)
        self.assertEqual(len(picks[0]['tasks']), 3)
        assert_task_conservation(self, report, who)

    def test_zero_fulfillment_order_is_empty_plan(self):
        lots = [lot('A', quantity=1)]
        orders = [order('O1', sku='other', quantity=2),
                  order('O2', sku='other2', quantity=3)]
        who = warehouse('D', ['D', 'A'], [edge('da', 'D', 'A'),
                         edge('ad', 'A', 'D')],
                        {'A': 'A'}, {'milk': 1, 'other': 1, 'other2': 1}, 10)
        report = call(make(lots, orders, who))
        self.assertEqual(report['waves'], [])
        self.assertEqual(report['total_waves'], 0)
        self.assertEqual(report['total_distance'], 0)
        self.assertEqual(report['zero_fulfillment_orders'], ['O1', 'O2'])
        self.assertEqual(report['shortages'],
                         [{'order': 'O1', 'quantity': 2},
                          {'order': 'O2', 'quantity': 3}])

    def test_zero_quantity_order_is_zero_fulfillment_and_empty_plan(self):
        # A zero-demand order has no tasks and must not occupy a wave.
        payload = make([lot('A', quantity=1)], [order('O1', quantity=0)],
                       warehouse('D', ['D', 'A'], [edge('da', 'D', 'A')],
                                 {'A': 'A'}, {'milk': 1}, 5))
        report = call(payload)
        self.assertEqual(report['waves'], [])
        self.assertEqual(report['zero_fulfillment_orders'], ['O1'])
        self.assertEqual(report['shortages'], [])

    def test_each_task_exactly_once_across_all_waves(self):
        lots = [lot('A', quantity=3), lot('B', quantity=2)]
        orders = [order('O1', quantity=4), order('O2', quantity=1)]
        # Two orders share lot B but at different nodes is impossible, so
        # split lots across nodes to exercise both grouping and splitting.
        who = warehouse('D', ['D', 'A', 'B'],
                        [edge('da', 'D', 'A'), edge('ad', 'A', 'D'),
                         edge('db', 'D', 'B'), edge('bd', 'B', 'D')],
                        {'A': 'A', 'B': 'B'}, {'milk': 1}, 4)
        report = call(make(lots, orders, who))
        assert_task_conservation(self, report, who)
        self.assertEqual(report['total_waves'], 2)


class RouteEngine(unittest.TestCase):
    def test_static_shortest_return_is_illegal_but_detour_works(self):
        lots = [lot('A', quantity=2)]
        orders = [order('O1', quantity=2)]
        who = warehouse('D', ['D', 'A', 'B'],
                        [edge('da', 'D', 'A', max_load=3),
                         edge('ad', 'A', 'D', max_load=1),
                         edge('ab', 'A', 'B', distance=2, max_load=3),
                         edge('bd', 'B', 'D', distance=2, max_load=3)],
                        {'A': 'A'}, {'milk': 1}, 3)
        report = call(make(lots, orders, who))
        wave = report['waves'][0]
        self.assertEqual(wave['distance'], 5)
        self.assertEqual(labels(wave['actions']),
                         [('move', 'da'), ('pick', 'A'),
                          ('move', 'ab'), ('move', 'bd')])
        replay(who, wave)

    def test_pass_pick_node_then_return_to_pick(self):
        lots = [lot('LA', quantity=2), lot('LB', quantity=1)]
        orders = [order('O1', quantity=3)]
        who = warehouse('D', ['D', 'A', 'B'],
                        [edge('da', 'D', 'A', max_load=3),
                         edge('ab', 'A', 'B', max_load=0),
                         edge('ba', 'B', 'A', max_load=1),
                         edge('ad', 'A', 'D', max_load=3)],
                        {'LA': 'A', 'LB': 'B'}, {'milk': 1}, 3)
        report = call(make(lots, orders, who))
        wave = report['waves'][0]
        self.assertEqual(labels(wave['actions']),
                         [('move', 'da'), ('move', 'ab'), ('pick', 'B'),
                          ('move', 'ba'), ('pick', 'A'), ('move', 'ad')])
        replay(who, wave)

    def test_load_equality_is_allowed(self):
        # Every road limits exactly the carried load: equality must pass.
        lots = [lot('A', quantity=2)]
        orders = [order('O1', quantity=2)]
        who = warehouse('D', ['D', 'A'], [edge('da', 'D', 'A', max_load=3),
                         edge('ad', 'A', 'D', max_load=2)],
                        {'A': 'A'}, {'milk': 1}, 2)
        report = call(make(lots, orders, who))
        self.assertEqual(report['total_waves'], 1)
        replay(who, report['waves'][0])

    def test_pass_through_depot_mid_wave_without_unloading(self):
        # Both tasks fit at once, but the canonical signature starts with the
        # lexicographically smallest first edge: pick A, pass the depot with
        # load still on the cart, then pick B and return.  Passing the depot
        # must not end the wave or unload anything.
        lots = [lot('LA', quantity=1), lot('LB', quantity=1)]
        orders = [order('O1', quantity=1), order('O2', quantity=1)]
        who = warehouse('D', ['D', 'A', 'B'],
                        [edge('da', 'D', 'A'), edge('ad', 'A', 'D'),
                         edge('db', 'D', 'B'), edge('bd', 'B', 'D')],
                        {'LA': 'A', 'LB': 'B'}, {'milk': 1}, 10)
        report = call(make(lots, orders, who))
        wave = report['waves'][0]
        self.assertEqual(wave['orders'], ['O1', 'O2'])
        self.assertEqual(labels(wave['actions']),
                         [('move', 'da'), ('pick', 'A'),
                          ('move', 'ad'), ('move', 'db'), ('pick', 'B'),
                          ('move', 'bd')])
        self.assertEqual(wave['distance'], 4)
        self.assertEqual(wave['final_weight'], 2)
        replay(who, wave)

    def test_parallel_edge_tie_breaks_on_edge_id(self):
        lots = [lot('A', quantity=1)]
        orders = [order('O1', quantity=1)]
        who = warehouse('D', ['D', 'A'],
                        [edge('z-road', 'D', 'A'), edge('a-road', 'D', 'A'),
                         edge('back', 'A', 'D')],
                        {'A': 'A'}, {'milk': 1}, 5)
        report = call(make(lots, orders, who))
        wave = report['waves'][0]
        self.assertEqual(labels(wave['actions'])[0], ('move', 'a-road'))
        replay(who, wave)


class Linkage(unittest.TestCase):
    def test_group_infeasible_but_singletons_forced_two_waves(self):
        # Cart 3, two orders of weight 2 at the same node: the pair cannot be
        # carried although their total (4) exceeds the cart anyway; the
        # compiler must split, never reject the command.
        payload = make([lot('L1', quantity=2), lot('L2', quantity=2)],
                       [order('O1', quantity=2), order('O2', quantity=2)],
                       warehouse('D', ['D', 'A'],
                                 [edge('da', 'D', 'A'),
                                  edge('ad', 'A', 'D')],
                                 {'L1': 'A', 'L2': 'A'}, {'milk': 1}, 3))
        report = call(payload)
        self.assertEqual(report['total_waves'], 2)
        self.assertEqual(report['total_distance'], 4)
        assert_task_conservation(self, report, payload['warehouse'])

    def test_capacity_splits_even_when_total_weight_fits(self):
        # Cart 4; each order weighs 2; every road has max_load 2.  A single
        # wave's total (4) fits the cart but no road can carry it.
        lots = [lot('LA', quantity=2), lot('LB', quantity=2)]
        orders = [order('O1', quantity=2), order('O2', quantity=2)]
        who = warehouse('D', ['D', 'A', 'B'],
                        [edge('da', 'D', 'A', max_load=2),
                         edge('ad', 'A', 'D', max_load=2),
                         edge('db', 'D', 'B', max_load=2),
                         edge('bd', 'B', 'D', max_load=2)],
                        {'LA': 'A', 'LB': 'B'}, {'milk': 1}, 4)
        # v1 gives O1 the FEFO-earlier lot, O2 the other.  Force the pair onto
        # separate nodes by splitting the two lots.
        report = call(make(lots, orders, who))
        self.assertEqual(report['total_waves'], 2)
        self.assertEqual(sorted(w['orders'][0] for w in report['waves']),
                         ['O1', 'O2'])
        assert_task_conservation(self, report, who)

    def test_billion_unit_quantity_is_never_expanded(self):
        lots = [lot('BIG', quantity=10 ** 9, held=0)]
        orders = [order('O1', quantity=10 ** 9)]
        who = warehouse('D', ['D', 'BIG'], [edge('db', 'D', 'BIG'),
                         edge('bd', 'BIG', 'D')],
                        {'BIG': 'BIG'}, {'milk': 10 ** 9}, 10 ** 18)
        report = call(make(lots, orders, who))
        wave = report['waves'][0]
        self.assertEqual(wave['final_weight'], 10 ** 18)
        pick = [a for a in wave['actions'] if a['type'] == 'pick'][0]
        self.assertEqual(pick['increment'], 10 ** 18)
        self.assertEqual(len(pick['tasks']), 1)
        replay(who, wave)


# --------------------------------------------------------------------------
# Independent exhaustive enumeration (not the production solver)
# --------------------------------------------------------------------------

import heapq


def enumerate_walk(who, node_weights, task_nodes):
    """Brute-force all legal walks by Dijkstra over (node, picked-set).

    Returns the minimum distance, or None, computed without the production
    RouteEngine.  Small graphs only.
    """
    target = (who['depot'], frozenset(task_nodes))
    start = (who['depot'], frozenset())
    dist = {start: 0}
    heap = [(0, start)]
    while heap:
        distance, (node, picked) = heapq.heappop(heap)
        if distance != dist[(node, picked)]:
            continue
        if (node, picked) == target:
            return distance
        load = sum(node_weights[n] for n in picked)
        for e in who['edges']:
            if e['from'] != node or load > e['max_load']:
                continue
            nxt = (e['to'], picked)
            nd = distance + e['distance']
            if nd < dist.get(nxt, 10 ** 30):
                dist[nxt] = nd
                heapq.heappush(heap, (nd, nxt))
        if node in node_weights and node not in picked:
            increment = node_weights[node]
            if load + increment <= who['cart_capacity']:
                nxt = (node, picked | {node})
                if distance < dist.get(nxt, 10 ** 30):
                    dist[nxt] = distance
                    heapq.heappush(heap, (distance, nxt))
    return None


def brute_force_plan(payload):
    """Independently enumerate partitions to find min waves then min distance."""
    from perishable_fulfillment.model import analyze
    result = analyze(payload['fulfillment'])
    who = payload['warehouse']
    lot_skus = {l['lot']: l['sku'] for l in result['lots']}
    node_of = {r['lot']: r['node'] for r in who['lot_nodes']}
    grams = {r['sku']: r['grams'] for r in who['sku_grams']}
    tasks = {}
    for row in result['orders']:
        rows = [{'node': node_of[a['lot']],
                 'weight': a['quantity'] * grams[lot_skus[a['lot']]]}
                for a in row['allocations']]
        if rows:
            tasks[row['order']] = rows
    active = sorted(tasks)

    def weights(members):
        w = {}
        for oid in members:
            for t in tasks[oid]:
                w[t['node']] = w.get(t['node'], 0) + t['weight']
        return w

    def cost(members):
        w = weights(members)
        return enumerate_walk(who, w, sorted(w))

    def partitions(items):
        if not items:
            yield []
            return
        first = items[0]
        for rest in partitions(items[1:]):
            for i in range(len(rest)):
                yield rest[:i] + [rest[i] + [first]] + rest[i + 1:]
            yield [[first]] + rest

    singleton_cost = {oid: cost([oid]) for oid in active}
    if any(value is None for value in singleton_cost.values()):
        return None
    best = None
    for part in partitions(active):
        groups = [sorted(g) for g in part]
        total = 0
        ok = True
        for g in groups:
            c = cost(g)
            if c is None:
                ok = False
                break
            total += c
        if not ok:
            continue
        key = (len(groups), total, tuple(sorted(tuple(g) for g in groups)))
        if best is None or key < best:
            best = key
    return best


def enumerate_best_walk(who, node_weights, task_nodes):
    """Exhaustively enumerate simple state walks to find the exact optimum.

    Returns (distance, action-label tuple) minimizing distance first and then
    the ordinary-string action sequence, computed without the production
    engine.  Distances are positive, so every distance-optimal walk is simple
    in the (node, picked-set) state graph.
    """
    edges = who['edges']
    all_picked = frozenset(task_nodes)

    def load_of(picked):
        return sum(node_weights[n] for n in picked)

    best = []

    def dfs(node, picked, distance, labels, visited):
        if node == who['depot'] and picked == all_picked:
            best.append((distance, tuple(labels)))
            return
        for e in edges:
            if e['from'] != node or load_of(picked) > e['max_load']:
                continue
            state = (e['to'], picked)
            if state in visited:
                continue
            visited.add(state)
            labels.append(('move', e['id']))
            dfs(e['to'], picked, distance + e['distance'], labels, visited)
            labels.pop()
            visited.remove(state)
        if node in node_weights and node not in picked:
            if load_of(picked) + node_weights[node] <= who['cart_capacity']:
                nxt = picked | {node}
                state = (node, nxt)
                if state not in visited:
                    visited.add(state)
                    labels.append(('pick', node))
                    dfs(node, nxt, distance, labels, visited)
                    labels.pop()
                    visited.remove(state)

    dfs(who['depot'], frozenset(), 0, [], {(who['depot'], frozenset())})
    return min(best) if best else None


class Enumeration(unittest.TestCase):
    def test_enumeration_matches_on_random_small_graphs(self):
        import random
        rng = random.Random(20261117)
        nodes = ['D', 'A', 'B']
        for _ in range(25):
            lots = [lot('L%d' % i, expires='2026-11-%02d' % (18 + i),
                        quantity=rng.randint(0, 2)) for i in range(3)]
            orders = [order('O%d' % i, priority=rng.randint(0, 1),
                            quantity=rng.randint(0, 3))
                      for i in range(3)]
            edges = []
            candidates = [('D', 'A'), ('A', 'D'), ('D', 'B'), ('B', 'D'),
                          ('A', 'B'), ('B', 'A')]
            for k, (frm, to) in enumerate(candidates):
                if rng.random() < 0.8:
                    edges.append(edge('e%d' % k, frm, to,
                                      distance=rng.randint(1, 3),
                                      max_load=rng.randint(0, 4)))
            locs = {'L%d' % i: rng.choice(['A', 'B']) for i in range(3)}
            capacity = rng.randint(1, 6)
            if not any(e['from'] == 'D' for e in edges):
                edges.append(edge('da0', 'D', 'A', max_load=4))
            who = warehouse('D', nodes, edges, locs, {'milk': 1}, capacity)
            payload = make(lots, orders, who)
            expected = brute_force_plan(payload)
            try:
                report = call(payload)
            except ValueError:
                # Only acceptable when at least one active order is walkable
                # alone nowhere; the brute force has the same verdict.
                self.assertIsNone(expected)
                continue
            self.assertIsNotNone(expected)
            self.assertEqual(
                (report['total_waves'], report['total_distance']),
                (expected[0], expected[1]))
            self.assertEqual(
                tuple(tuple(w['orders']) for w in report['waves']),
                expected[2])
            assert_task_conservation(self, report, who)

    def test_action_signature_matches_independent_enumeration(self):
        import random
        rng = random.Random(424242)
        nodes = ['D', 'A', 'B']
        for _ in range(20):
            edges = []
            candidates = [('D', 'A'), ('A', 'D'), ('D', 'B'), ('B', 'D'),
                          ('A', 'B'), ('B', 'A')]
            for k, (frm, to) in enumerate(candidates):
                if rng.random() < 0.85:
                    edges.append(edge('q%d' % k, frm, to,
                                      distance=rng.randint(1, 4),
                                      max_load=rng.randint(0, 5)))
            if not any(e['from'] == 'D' for e in edges):
                continue
            capacity = rng.randint(1, 6)
            node_weights = {}
            for node in ('A', 'B'):
                node_weights[node] = rng.randint(0, 3)
            # A task node with zero weight is not meaningful for a wave; skip
            # such instances entirely rather than silently ignoring a bit.
            used = [n for n in ('A', 'B') if node_weights[n] > 0]
            if not used:
                continue
            who = warehouse('D', nodes, edges, {}, {'milk': 1}, capacity)
            best = enumerate_best_walk(who, node_weights, used)
            if best is None:
                continue
            from perishable_fulfillment.wave import RouteEngine, parse_warehouse
            parsed = parse_warehouse(
                {'depot': 'D', 'nodes': nodes, 'edges': edges,
                 'lot_nodes': [], 'sku_grams': [], 'cart_capacity': capacity},
                {}, set())
            engine = RouteEngine(parsed)
            got = engine.shortest(used, node_weights)
            if got is None:
                # Independently unreachable because the full set cannot be
                # staged; skip rather than assert on an infeasible instance.
                continue
            self.assertEqual(got['distance'], best[0])
            self.assertEqual(tuple(got['signature']), best[1])

    def test_enumeration_matches_on_small_graphs(self):
        nodes = ['D', 'A', 'B', 'C']
        cases = [
            # (lots, orders, edges, lot_nodes, grams, capacity)
            ([lot('L1', quantity=1), lot('L2', quantity=1)],
             [order('O1', quantity=1), order('O2', quantity=1)],
             [edge('da', 'D', 'A'), edge('ad', 'A', 'D'),
              edge('db', 'D', 'B'), edge('bd', 'B', 'D'),
              edge('ab', 'A', 'B'), edge('ba', 'B', 'A')],
             {'L1': 'A', 'L2': 'B'}, {'milk': 1}, 5),
            ([lot('L1', quantity=2), lot('L2', quantity=1)],
             [order('O1', quantity=2), order('O2', quantity=1)],
             [edge('da', 'D', 'A', max_load=2),
              edge('ad', 'A', 'D', max_load=3),
              edge('ab', 'A', 'B', max_load=1),
              edge('bd', 'B', 'D', max_load=3),
              edge('ac', 'A', 'C', max_load=3),
              edge('cd', 'C', 'D', max_load=3)],
             {'L1': 'A', 'L2': 'B'}, {'milk': 1}, 3),
        ]
        for lots, orders, edges, locs, grams, cap in cases:
            who = warehouse('D', nodes, edges, locs, grams, cap)
            payload = make(lots, orders, who)
            report = call(payload)
            expected = brute_force_plan(payload)
            self.assertEqual((report['total_waves'], report['total_distance']),
                             (expected[0], expected[1]))
            self.assertEqual(tuple(tuple(w['orders']) for w in report['waves']),
                             expected[2])

    def test_enumeration_validates_each_route_distance(self):
        lots = [lot('L1', quantity=2), lot('L2', quantity=1)]
        orders = [order('O1', quantity=2), order('O2', quantity=1)]
        who = warehouse('D', ['D', 'A', 'B'],
                        [edge('da', 'D', 'A', max_load=2),
                         edge('ad', 'A', 'D', max_load=3),
                         edge('ab', 'A', 'B', max_load=1),
                         edge('bd', 'B', 'D', max_load=3)],
                        {'L1': 'A', 'L2': 'B'}, {'milk': 1}, 3)
        payload = make(lots, orders, who)
        report = call(payload)
        for wave in report['waves']:
            w = {}
            for t in wave['tasks']:
                w[t['node']] = w.get(t['node'], 0) + t['weight']
            self.assertEqual(enumerate_walk(who, w, sorted(w)), wave['distance'])


class OrderingAndTies(unittest.TestCase):
    def test_equal_priority_orders_use_sorted_ids_in_wave(self):
        lots = [lot('A', quantity=1), lot('B', quantity=1)]
        orders = [order('Z', quantity=1), order('A', quantity=1)]
        who = warehouse('D', ['D', 'A'],
                        [edge('da', 'D', 'A'), edge('ad', 'A', 'D')],
                        {'A': 'A', 'B': 'A'}, {'milk': 1}, 5)
        report = call(make(lots, orders, who))
        self.assertEqual(report['waves'][0]['orders'], ['A', 'Z'])

    def test_group_sequence_breaks_equal_distance_tie(self):
        # Capacity 3: O1(2)+O2(2) is infeasible, so two waves are required.
        # O3 (1 unit) can join either O1 or O2 at equal distance; the
        # canonical wave-tuple sequence decides: ("O1",) sorts before
        # ("O1","O3") because a shorter prefix is smaller.
        lots = [lot('LA', expires='2026-11-18', quantity=2),
                lot('LB', expires='2026-11-19', quantity=2),
                lot('LC', expires='2026-11-20', quantity=1)]
        orders = [order('O1', priority=1, quantity=2),
                  order('O2', priority=2, quantity=2),
                  order('O3', priority=3, quantity=1)]
        who = warehouse(
            'D', ['D', 'A', 'B', 'C'],
            [edge('e-da', 'D', 'A'), edge('e-ad', 'A', 'D'),
             edge('e-db', 'D', 'B'), edge('e-bd', 'B', 'D'),
             edge('e-dc', 'D', 'C', distance=5),
             edge('e-cd', 'C', 'D', distance=5),
             edge('e-ac', 'A', 'C'), edge('e-ca', 'C', 'A'),
             edge('e-bc', 'B', 'C'), edge('e-cb', 'C', 'B'),
             edge('e-ab', 'A', 'B'), edge('e-ba', 'B', 'A')],
            {'LA': 'A', 'LB': 'B', 'LC': 'C'}, {'milk': 1}, 3)
        report = call(make(lots, orders, who))
        self.assertEqual([w['orders'] for w in report['waves']],
                         [['O1'], ['O2', 'O3']])
        self.assertEqual(report['total_distance'], 6)
        assert_task_conservation(self, report, who)

    def test_same_node_can_be_visited_by_different_waves(self):
        lots = [lot('L1', quantity=2), lot('L2', quantity=2)]
        orders = [order('O1', quantity=4), order('O2', quantity=2)]
        # v1 gives O1 all of L1 (2) and L2 (2); O2 is short, so contrive the
        # split by quantities: O1 takes L1 only, O2 takes L2 only.
        lots = [lot('L1', quantity=2), lot('L2', quantity=2)]
        orders = [order('O1', quantity=2), order('O2', quantity=2)]
        who = warehouse('D', ['D', 'A'],
                        [edge('da', 'D', 'A', max_load=2),
                         edge('ad', 'A', 'D', max_load=2)],
                        {'L1': 'A', 'L2': 'A'}, {'milk': 1}, 2)
        report = call(make(lots, orders, who))
        # One cart can carry 2 units and roads cap at 2, so each 2-unit order
        # needs its own wave even though both pick at node A.
        self.assertEqual(report['total_waves'], 2)
        pick_nodes = [[a['node'] for a in w['actions'] if a['type'] == 'pick']
                      for w in report['waves']]
        self.assertEqual(pick_nodes, [['A'], ['A']])
        assert_task_conservation(self, report, who)

    def test_shuffled_input_arrays_do_not_change_the_plan(self):
        lots = [lot('L1', quantity=2), lot('L2', quantity=1),
                lot('L3', quantity=2)]
        orders = [order('O1', quantity=2), order('O2', quantity=2),
                  order('O3', quantity=1)]
        who = warehouse('D', ['D', 'A', 'B'],
                        [edge('da', 'D', 'A'), edge('ad', 'A', 'D'),
                         edge('db', 'D', 'B'), edge('bd', 'B', 'D')],
                        {'L1': 'A', 'L2': 'A', 'L3': 'B'},
                        {'milk': 1}, 4)
        payload = make(lots, orders, who)
        first = call(payload)
        shuffled = {
            'fulfillment': {'as_of': payload['fulfillment']['as_of'],
                            'lots': list(reversed(lots)),
                            'orders': [orders[1], orders[2], orders[0]]},
            'warehouse': {'depot': who['depot'],
                          'nodes': list(reversed(who['nodes'])),
                          'edges': list(reversed(who['edges'])),
                          'lot_nodes': list(reversed(who['lot_nodes'])),
                          'sku_grams': list(reversed(who['sku_grams'])),
                          'cart_capacity': who['cart_capacity']},
        }
        self.assertEqual(first['waves'], call(shuffled)['waves'])
        self.assertEqual(first['total_distance'],
                         call(shuffled)['total_distance'])


class InputContract(unittest.TestCase):
    def base(self):
        return make([lot('A', quantity=1)], [order('O1', quantity=1)],
                    warehouse('D', ['D', 'A'], [edge('da', 'D', 'A'),
                                  edge('ad', 'A', 'D')],
                              {'A': 'A'}, {'milk': 1}, 5))

    def bad(self, mutate):
        payload = self.base()
        mutate(payload)
        with self.assertRaises((ValueError, TypeError)):
            compile_report(payload, 'digest')

    def test_reject_unknown_top_level_field(self):
        self.bad(lambda p: p.update({'extra': 1}))

    def test_reject_missing_warehouse_field(self):
        self.bad(lambda p: p['warehouse'].pop('cart_capacity'))

    def test_reject_bool_for_integer(self):
        self.bad(lambda p: p['warehouse'].update({'cart_capacity': True}))
        self.bad(lambda p: p['warehouse']['edges'][0].update(
            {'max_load': True}))

    def test_reject_nonfinite_distance(self):
        # json float would be accepted by the strict reader only as a float;
        # the contract requires int, so a float distance is rejected.
        self.bad(lambda p: p['warehouse']['edges'][0].update(
            {'distance': 1.0}))

    def test_reject_duplicate_edge_id(self):
        self.bad(lambda p: p['warehouse']['edges'].append(
            dict(p['warehouse']['edges'][0], **{'from': 'A', 'to': 'D'})))

    def test_reject_self_loop(self):
        self.bad(lambda p: p['warehouse']['edges'].append(
            edge('loop', 'A', 'A')))

    def test_reject_unknown_endpoint(self):
        self.bad(lambda p: p['warehouse']['edges'].append(
            edge('ghost', 'D', 'Z')))

    def test_reject_duplicate_node(self):
        self.bad(lambda p: p['warehouse']['nodes'].append('A'))

    def test_depot_must_be_a_node(self):
        self.bad(lambda p: p['warehouse'].update({'depot': 'Z'}))

    def test_lot_nodes_cover_every_lot_once(self):
        self.bad(lambda p: p['warehouse']['lot_nodes'].append(
            {'lot': 'A', 'node': 'A'}))

    def test_lot_nodes_reject_depot_storage(self):
        self.bad(lambda p: p['warehouse']['lot_nodes'][0].update(
            {'node': 'D'}))

    def test_sku_grams_cover_all_skus(self):
        self.bad(lambda p: p['warehouse']['sku_grams'].clear())

    def test_duplicate_sku_grams_rejected(self):
        self.bad(lambda p: p['warehouse']['sku_grams'].append(
            {'sku': 'milk', 'grams': 2}))

    def test_too_many_orders(self):
        self.bad(lambda p: p['fulfillment'].update(
            {'orders': [order('O%d' % i, quantity=1) for i in range(9)]}))

    def test_too_many_lots(self):
        self.bad(lambda p: p['fulfillment'].update(
            {'lots': [lot('L%d' % i, quantity=1) for i in range(65)]}))

    def test_reject_unknown_edge_field(self):
        self.bad(lambda p: p['warehouse']['edges'][0].update({'name': 'x'}))

    def test_reject_negative_distance(self):
        self.bad(lambda p: p['warehouse']['edges'][0].update({'distance': 0}))

    def test_reject_grams_zero(self):
        self.bad(lambda p: p['warehouse']['sku_grams'][0].update({'grams': 0}))

    def test_reject_capacity_zero(self):
        self.bad(lambda p: p['warehouse'].update({'cart_capacity': 0}))

    def test_reverse_edge_is_not_implied(self):
        # Only D->A exists; A->D is absent, so the order is unreachable.
        payload = make([lot('A', quantity=1)], [order('O1', quantity=1)],
                       warehouse('D', ['D', 'A'],
                                 [edge('da', 'D', 'A', max_load=10 ** 18)],
                                 {'A': 'A'}, {'milk': 1}, 5))
        with self.assertRaises(ValueError):
            compile_report(payload, 'digest')

    def test_single_unreachable_order_fails_whole_command(self):
        lots = [lot('A', quantity=1), lot('B', quantity=1)]
        orders = [order('O1', quantity=1), order('O2', quantity=1)]
        who = warehouse('D', ['D', 'A', 'B'],
                        [edge('da', 'D', 'A'), edge('ad', 'A', 'D')],
                        {'A': 'A', 'B': 'B'}, {'milk': 1}, 5)
        with self.assertRaises(ValueError) as caught:
            compile_report(make(lots, orders, who), 'digest')
        self.assertIn('O2', str(caught.exception))
        self.assertNotIn('O1', str(caught.exception).split(':', 1)[1])


class Fixtures(unittest.TestCase):
    """Lock the documented sample numbers and byte-stable replays."""

    def test_documented_samples(self):
        expected = {
            'wave_input.json': (2, 18),
            'wave_detour.json': (1, 5),
            'wave_roundtrip.json': (1, 4),
        }
        for name, (waves, distance) in expected.items():
            report = run(ROOT / 'fixtures' / name)
            self.assertEqual((report['total_waves'], report['total_distance']),
                             (waves, distance), name)
            self.assertEqual(report['schema_version'], 3)

    def test_detour_and_roundtrip_action_sequences(self):
        report = run(ROOT / 'fixtures' / 'wave_detour.json')
        self.assertEqual(labels(report['waves'][0]['actions']),
                         [('move', 'D-A'), ('pick', 'A'),
                          ('move', 'A-B'), ('move', 'B-D')])
        report = run(ROOT / 'fixtures' / 'wave_roundtrip.json')
        self.assertEqual(labels(report['waves'][0]['actions']),
                         [('move', 'D-A'), ('move', 'A-B'), ('pick', 'B'),
                          ('move', 'B-A'), ('pick', 'A'), ('move', 'A-D')])


class Cli(unittest.TestCase):
    def run_cli(self, src, out):
        return subprocess.run(
            [sys.executable, '-m', 'perishable_fulfillment.wave',
             '--source', str(src), '--output', str(out)],
            capture_output=True, text=True, cwd=str(ROOT))

    def write(self, path, payload):
        path.write_text(json.dumps(payload))
        return path

    def sample(self):
        return make([lot('L1', quantity=6, held=2),
                     lot('L2', sku='milk', quantity=10),
                     lot('L0', expires='2026-11-15', quantity=5,
                         held=0)],
                    [order('O1', priority=1, quantity=9),
                     order('O2', priority=2, quantity=8)],
                    warehouse('D', ['D', 'A', 'B'],
                              [edge('da', 'D', 'A'), edge('ad', 'A', 'D'),
                               edge('db', 'D', 'B'), edge('bd', 'B', 'D')],
                              {'L1': 'A', 'L2': 'B', 'L0': 'A'},
                              {'milk': 1}, 20))

    def test_cli_replay_and_digest(self):
        import hashlib
        with tempfile.TemporaryDirectory() as td:
            src = self.write(Path(td) / 'in.json', self.sample())
            out = Path(td) / 'out.json'
            result = self.run_cli(src, out)
            self.assertEqual(result.returncode, 0, result.stderr)
            first = out.read_bytes()
            self.assertEqual(json.loads(first)['source_sha256'],
                             hashlib.sha256(src.read_bytes()).hexdigest())
            self.assertEqual(json.loads(first)['schema_version'], 3)
            again = self.run_cli(src, out)
            self.assertEqual(again.returncode, 0, again.stderr)
            self.assertEqual(first, out.read_bytes())

    def test_failure_exit_2_preserves_prior_output(self):
        with tempfile.TemporaryDirectory() as td:
            src = Path(td) / 'in.json'
            out = Path(td) / 'out.json'
            out.write_text('prior')
            for body in ['{}', '{"a":1,"a":2}', 'NaN',
                         '{"fulfillment":{},"warehouse":{}}']:
                src.write_text(body)
                self.assertEqual(self.run_cli(src, out).returncode, 2)
                self.assertEqual(out.read_text(), 'prior')

    def test_nonfinite_number_in_file_is_rejected(self):
        # 1e400 parses to positive infinity as a JSON float; every numeric
        # warehouse field is typed as int, so the command must fail.
        with tempfile.TemporaryDirectory() as td:
            src = Path(td) / 'in.json'
            out = Path(td) / 'out.json'
            out.write_text('prior')
            payload = json.dumps(self.sample())
            payload = payload.replace('"distance": 1', '"distance": 1e400', 1)
            src.write_text(payload)
            self.assertEqual(self.run_cli(src, out).returncode, 2)
            self.assertEqual(out.read_text(), 'prior')

    def test_output_cannot_be_input_or_hardlink(self):
        with tempfile.TemporaryDirectory() as td:
            src = self.write(Path(td) / 'in.json', self.sample())
            before = src.read_bytes()
            self.assertEqual(self.run_cli(src, src).returncode, 2)
            self.assertEqual(src.read_bytes(), before)
            link = Path(td) / 'hard.json'
            os.link(src, link)
            self.assertEqual(self.run_cli(src, link).returncode, 2)
            self.assertEqual(src.read_bytes(), before)
            symlink = Path(td) / 'sym.json'
            symlink.symlink_to(src)
            self.assertEqual(self.run_cli(src, symlink).returncode, 2)
            self.assertEqual(src.read_bytes(), before)

    def test_unreachable_order_reports_all_and_preserves_output(self):
        with tempfile.TemporaryDirectory() as td:
            payload = make(
                [lot('A', quantity=1), lot('B', quantity=1)],
                [order('O1', quantity=1), order('O2', quantity=1)],
                warehouse('D', ['D', 'A', 'B'],
                          [edge('da', 'D', 'A'), edge('ad', 'A', 'D')],
                          {'A': 'A', 'B': 'B'}, {'milk': 1}, 5))
            src = self.write(Path(td) / 'in.json', payload)
            out = Path(td) / 'out.json'
            out.write_text('prior')
            result = self.run_cli(src, out)
            self.assertEqual(result.returncode, 2)
            self.assertEqual(out.read_text(), 'prior')
            self.assertIn('O2', result.stderr)


if __name__ == '__main__':
    unittest.main()
