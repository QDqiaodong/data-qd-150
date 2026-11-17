"""Warehouse wave compiler for fixed v1 allocations (schema_version=3).

This mode is deliberately additive: it calls the unchanged v1 allocator
(``model.analyze``) for the commercial decision (which quantity of which lot
belongs to which order), then compiles those fixed tasks into an executable
in-warehouse pick-and-walk plan.  It never changes an allocation, never reads
the v2 commitment ledger, and never writes physical stock.

The warehouse is a directed load-constrained graph.  Reverse movement is not
implied: a road may be traversable in one direction only.  Route state is
(node, set_of_pick_nodes_already_taken); the vehicle load is exactly the
weight of that picked set.  A road edge may be used only while the current
load is <= both the cart capacity and the edge's own ``max_load`` (equality
included).  Passing a pick node never picks it up automatically, and the
vehicle may not unload anywhere before the wave is complete.

Wave selection is exact and integer:
  1. minimum number of waves,
  2. minimum total directed distance,
  3. lexicographically smallest sequence of sorted order-id groups,
  4. lexicographically smallest sequence of per-wave action signatures.
Action signatures use ordinary Python string ordering over
("move", edge_id) / ("pick", node_id) pairs, not serialized JSON bytes.
"""

from __future__ import annotations

import heapq
import os
from collections import defaultdict
from pathlib import Path

from .common import ident, require, rows, read, save
from .model import analyze

SCHEMA_VERSION = 3
MAX_ORDERS = 8
MAX_LOTS = 64
MAX_NODES = 32
MAX_EDGES = 128
MAX_TASK_NODES = 10
MAX_DISTANCE = 10 ** 9
MAX_LOAD = 10 ** 18
MAX_GRAMS = 10 ** 9
INF = 10 ** 30


# --------------------------------------------------------------------------
# Strict input parsing
# --------------------------------------------------------------------------

def _integer(value, minimum, maximum, label):
    # type(...) is int deliberately rejects bool, which is an int subclass.
    if type(value) is not int or not minimum <= value <= maximum:
        raise ValueError('%s must be an integer in [%d, %d]'
                         % (label, minimum, maximum))
    return value


def _bounded_fulfillment(data):
    """Validate the outer shape before handing fulfillment to v1.

    ``model.analyze`` remains the single authority for the v1 contract; the
    new mode only adds its tighter row limits (8 orders / 64 lots) so a huge
    legacy-shaped file is rejected before allocation work begins.
    """
    require(data, 'fulfillment warehouse')
    fulfillment = data['fulfillment']
    if isinstance(fulfillment, dict):
        for field, limit in (('lots', MAX_LOTS), ('orders', MAX_ORDERS)):
            value = fulfillment.get(field)
            if isinstance(value, list) and len(value) > limit:
                raise ValueError('%s exceeds new-mode limit of %d'
                                 % (field, limit))
    return fulfillment


def parse_warehouse(warehouse, lot_skus, order_skus):
    """Validate the warehouse graph and its lot/SKU mappings.

    Returns a normalized immutable-in-practice view used by the compiler.
    """
    require(warehouse,
            'depot nodes edges lot_nodes sku_grams cart_capacity')
    depot = ident(warehouse['depot'])

    node_list = rows(warehouse['nodes'], MAX_NODES)
    node_ids = []
    seen_nodes = set()
    for raw in node_list:
        node = ident(raw)
        if node in seen_nodes:
            raise ValueError('duplicate node id: ' + node)
        seen_nodes.add(node)
        node_ids.append(node)
    if depot not in seen_nodes:
        raise ValueError('depot must be one of the nodes')

    edge_list = rows(warehouse['edges'], MAX_EDGES)
    edges = []
    edge_by_id = {}
    for raw in edge_list:
        require(raw, 'id from to distance max_load')
        edge_id = ident(raw['id'])
        frm = ident(raw['from'])
        to = ident(raw['to'])
        if edge_id in edge_by_id:
            raise ValueError('duplicate edge id: ' + edge_id)
        if frm not in seen_nodes or to not in seen_nodes:
            raise ValueError('edge endpoint is not a known node')
        if frm == to:
            raise ValueError('self-loop edge is not allowed')
        distance = _integer(raw['distance'], 1, MAX_DISTANCE, 'edge distance')
        max_load = _integer(raw['max_load'], 0, MAX_LOAD, 'edge max_load')
        edge = {'id': edge_id, 'from': frm, 'to': to,
                'distance': distance, 'max_load': max_load}
        edge_by_id[edge_id] = edge
        edges.append(edge)

    lot_node_rows = rows(warehouse['lot_nodes'], 20000)
    lot_nodes = {}
    for raw in lot_node_rows:
        require(raw, 'lot node')
        lot = ident(raw['lot'])
        node = ident(raw['node'])
        if lot in lot_nodes:
            raise ValueError('duplicate lot in lot_nodes: ' + lot)
        if lot not in lot_skus:
            raise ValueError('lot_nodes references an unknown source lot: '
                             + lot)
        if node not in seen_nodes:
            raise ValueError('lot_nodes references an unknown node: ' + node)
        if node == depot:
            raise ValueError('a lot cannot be stored at the depot')
        lot_nodes[lot] = node
    if set(lot_nodes) != set(lot_skus):
        missing = sorted(set(lot_skus) - set(lot_nodes))
        raise ValueError('lot_nodes must cover every source lot exactly once'
                         + (': missing ' + ', '.join(missing) if missing else ''))

    grams_rows = rows(warehouse['sku_grams'], 20000)
    grams = {}
    for raw in grams_rows:
        require(raw, 'sku grams')
        sku = ident(raw['sku'])
        gram = _integer(raw['grams'], 1, MAX_GRAMS, 'grams')
        if sku in grams:
            raise ValueError('duplicate sku in sku_grams: ' + sku)
        grams[sku] = gram
    all_skus = set(lot_skus.values()) | set(order_skus)
    if set(grams) != all_skus:
        missing = sorted(all_skus - set(grams))
        extra = sorted(set(grams) - all_skus)
        detail = []
        if missing:
            detail.append('missing ' + ', '.join(missing))
        if extra:
            detail.append('unknown ' + ', '.join(extra))
        raise ValueError('sku_grams must cover every source and order SKU '
                         'exactly once' + (': ' + '; '.join(detail)) )

    capacity = _integer(warehouse['cart_capacity'], 1, MAX_LOAD,
                        'cart_capacity')
    return {
        'depot': depot,
        'nodes': node_ids,
        'edges': edges,
        'edge_by_id': edge_by_id,
        'lot_nodes': lot_nodes,
        'grams': grams,
        'cart_capacity': capacity,
    }


# --------------------------------------------------------------------------
# Fixed tasks from the unchanged v1 result
# --------------------------------------------------------------------------

def build_tasks(result, warehouse):
    """Turn every positive v1 allocation into one immutable pick task."""
    lot_skus = {lot['lot']: lot['sku'] for lot in result['lots']}
    tasks_by_order = {}
    all_tasks = []
    seen = set()
    for order_row in result['orders']:
        oid = order_row['order']
        order_tasks = []
        for allocation in order_row['allocations']:
            lot = allocation['lot']
            quantity = allocation['quantity']
            if type(quantity) is not int or quantity <= 0:
                raise ValueError('v1 returned a non-positive allocation')
            if (oid, lot) in seen:
                raise ValueError('duplicate (order, lot) task')
            seen.add((oid, lot))
            if lot not in lot_skus:
                raise ValueError('allocation references an unknown lot')
            if lot not in warehouse['lot_nodes']:
                raise ValueError('allocation lot has no warehouse location')
            sku = lot_skus[lot]
            node = warehouse['lot_nodes'][lot]
            grams = warehouse['grams'][sku]
            task = {'order': oid, 'lot': lot, 'quantity': quantity,
                    'sku': sku, 'node': node, 'grams': grams,
                    'weight': quantity * grams}
            order_tasks.append(task)
            all_tasks.append(task)
        if order_tasks:
            tasks_by_order[oid] = order_tasks
    task_nodes = {task['node'] for task in all_tasks}
    if len(task_nodes) > MAX_TASK_NODES:
        raise ValueError('more than %d nodes carry positive tasks'
                         % MAX_TASK_NODES)
    return tasks_by_order, all_tasks


# --------------------------------------------------------------------------
# Load-aware directed route engine
# --------------------------------------------------------------------------

class RouteEngine:
    """Exact shortest walk for one wave under directed load constraints."""

    def __init__(self, warehouse):
        self.depot = warehouse['depot']
        self.nodes = warehouse['nodes']
        self.node_index = {node: i for i, node in enumerate(self.nodes)}
        self.degree = len(self.nodes)
        self.out = [[] for _ in self.nodes]
        self.incoming = [[] for _ in self.nodes]
        for edge in warehouse['edges']:
            frm = self.node_index[edge['from']]
            to = self.node_index[edge['to']]
            self.out[frm].append((edge['id'], to, edge['distance'],
                                  edge['max_load']))
            self.incoming[to].append((edge['id'], frm, edge['distance'],
                                      edge['max_load']))
        self.capacity = warehouse['cart_capacity']

    def shortest(self, task_nodes, node_weights):
        """Return the canonical route or None when the group is infeasible.

        ``task_nodes`` is a sorted list of node identifiers carrying tasks.
        ``node_weights`` maps each of those nodes to the merged pick weight.
        The returned dict has ``distance``, ``actions`` (execution order) and
        ``signature`` (used only for the lexicographic tie-break).
        """
        count = len(task_nodes)
        masks = 1 << count
        # State nodes use the integer node index, so the pick lookup is keyed
        # by index rather than by the identifier string.
        bit_of_node = {self.node_index[node]: bit
                       for bit, node in enumerate(task_nodes)}
        weight_bits = [node_weights[node] for node in task_nodes]
        total_weight = sum(weight_bits)
        if total_weight > self.capacity:
            return None

        loads = [0] * masks
        for mask in range(1, masks):
            low = mask & -mask
            bit = low.bit_length() - 1
            loads[mask] = loads[mask ^ low] + weight_bits[bit]

        degree = self.degree
        state_count = degree * masks
        depot_idx = self.node_index[self.depot]
        goal = depot_idx * masks + (masks - 1)
        start = depot_idx * masks

        # Reverse Dijkstra over (node, picked-set): the exact minimum distance
        # still to travel to finish the wave.  A forward pass is unnecessary:
        # a step is on an optimal walk iff its cost plus the successor's
        # remaining distance equals the current state's remaining distance.
        to_goal = [INF] * state_count
        to_goal[goal] = 0
        heap = [(0, goal)]
        while heap:
            distance, state = heapq.heappop(heap)
            if distance != to_goal[state]:
                continue
            node, mask = divmod(state, masks)
            load = loads[mask]
            for _edge_id, frm, step, max_load in self.incoming[node]:
                if load <= max_load:
                    prev = frm * masks + mask
                    candidate = distance + step
                    if candidate < to_goal[prev]:
                        to_goal[prev] = candidate
                        heapq.heappush(heap, (candidate, prev))
            bit = bit_of_node.get(node)
            if bit is not None and (mask >> bit) & 1:
                prev_mask = mask ^ (1 << bit)
                if loads[prev_mask] + weight_bits[bit] <= self.capacity:
                    prev = node * masks + prev_mask
                    if distance < to_goal[prev]:
                        to_goal[prev] = distance
                        heapq.heappush(heap, (distance, prev))

        total_distance = to_goal[start]
        if total_distance >= INF:
            return None

        # Reconstruct the lexicographically smallest action sequence among all
        # distance-optimal walks.  At a state, the first differing action
        # decides the comparison, so choosing the smallest label that keeps
        # the walk optimal is exact; equal labels imply the same next state
        # (edge ids are unique and a pick at a node is unique).
        current = start
        actions = []
        signature = []
        while current != goal:
            node, mask = divmod(current, masks)
            load = loads[mask]
            best = None
            for edge_id, to, step, max_load in self.out[node]:
                if load <= max_load:
                    nxt = to * masks + mask
                    if step + to_goal[nxt] == to_goal[current]:
                        label = ('move', edge_id)
                        if best is None or label < best[0]:
                            best = (label, nxt, 'move', edge_id, to, step)
            bit = bit_of_node.get(node)
            if bit is not None and not (mask >> bit) & 1 and \
                    load + weight_bits[bit] <= self.capacity:
                nxt = node * masks + (mask | (1 << bit))
                if to_goal[nxt] == to_goal[current]:
                    label = ('pick', task_nodes[bit])
                    if best is None or label < best[0]:
                        best = (label, nxt, 'pick', task_nodes[bit], None, None)
            if best is None:
                raise ValueError('internal: optimal route reconstruction '
                                 'has no successor')
            label, nxt, kind, ref, to, step = best
            signature.append(label)
            if kind == 'move':
                actions.append({'type': 'move', 'edge': ref,
                                'from': self.nodes[node],
                                'to': self.nodes[to],
                                'distance': step, 'load': load})
            else:
                weight = weight_bits[bit]
                actions.append({'type': 'pick', 'node': ref,
                                'increment': weight,
                                'load_before': load,
                                'load_after': load + weight})
            current = nxt

        return {'distance': total_distance, 'actions': actions,
                'signature': tuple(signature), 'final_weight': total_weight}


# --------------------------------------------------------------------------
# Wave compiler
# --------------------------------------------------------------------------

def _task_record(task):
    return {'order': task['order'], 'lot': task['lot'],
            'quantity': task['quantity'], 'sku': task['sku'],
            'node': task['node'], 'grams': task['grams'],
            'weight': task['weight']}


def _set_partitions(items):
    """Yield every set partition of ``items`` exactly once."""
    if not items:
        yield []
        return
    first = items[0]
    for rest in _set_partitions(items[1:]):
        for index in range(len(rest)):
            yield rest[:index] + [rest[index] + [first]] + rest[index + 1:]
        yield [[first]] + rest


def compile_waves(tasks_by_order, warehouse):
    """Choose the exact optimal wave partition and canonical routes."""
    active = sorted(tasks_by_order)
    if not active:
        return []

    engine = RouteEngine(warehouse)
    route_cache = {}

    def group_plan(mask):
        if mask in route_cache:
            return route_cache[mask]
        orders = tuple(active[i] for i in range(len(active))
                       if (mask >> i) & 1)
        node_tasks = defaultdict(list)
        for oid in orders:
            for task in tasks_by_order[oid]:
                node_tasks[task['node']].append(task)
        task_nodes = sorted(node_tasks)
        node_weights = {
            node: sum(task['weight'] for task in node_tasks[node])
            for node in task_nodes}
        route = engine.shortest(task_nodes, node_weights)
        plan = None
        if route is not None:
            plan = {'orders': orders, 'task_nodes': task_nodes,
                    'node_tasks': node_tasks, 'route': route}
        route_cache[mask] = plan
        return plan

    index_of = {oid: i for i, oid in enumerate(active)}
    infeasible = [oid for i, oid in enumerate(active)
                  if group_plan(1 << i) is None]
    if infeasible:
        # A single order that cannot be walked even alone is a hard input
        # error; every such order is named and no partial plan is returned.
        raise ValueError('orders with no legal route: ' + ', '.join(infeasible))

    best_key = None
    best = None
    for partition in _set_partitions(list(active)):
        groups = []
        feasible = True
        for members in partition:
            mask = 0
            for oid in members:
                mask |= 1 << index_of[oid]
            plan = group_plan(mask)
            if plan is None:
                feasible = False
                break
            groups.append(plan)
        if not feasible:
            continue
        groups.sort(key=lambda plan: plan['orders'])
        total_distance = sum(plan['route']['distance'] for plan in groups)
        key = (len(groups), total_distance,
               tuple(plan['orders'] for plan in groups),
               tuple(plan['route']['signature'] for plan in groups))
        if best_key is None or key < best_key:
            best_key = key
            best = groups

    if best is None:
        # Every singleton is feasible, so this is unreachable; keep it loud.
        raise ValueError('no feasible wave partition')
    return best


def _expand_wave(plan, tasks_by_order):
    tasks = []
    for oid in plan['orders']:
        tasks.extend(tasks_by_order[oid])
    tasks.sort(key=lambda task: (task['order'], task['lot']))
    node_tasks = defaultdict(list)
    for task in tasks:
        node_tasks[task['node']].append(task)

    actions = []
    for action in plan['route']['actions']:
        if action['type'] == 'pick':
            node = action['node']
            merged = sorted(node_tasks[node],
                            key=lambda task: (task['order'], task['lot']))
            expanded = dict(action)
            expanded['tasks'] = [_task_record(task) for task in merged]
            actions.append(expanded)
        else:
            actions.append(dict(action))

    return {
        'orders': list(plan['orders']),
        'tasks': [_task_record(task) for task in tasks],
        'actions': actions,
        'distance': plan['route']['distance'],
        'final_weight': plan['route']['final_weight'],
    }


# --------------------------------------------------------------------------
# Full-report validation
# --------------------------------------------------------------------------

def _validate_report(report, warehouse, all_tasks):
    expected = {(task['order'], task['lot']): task for task in all_tasks}
    seen = {}
    for wave in report['waves']:
        route_actions = wave['actions']
        current = warehouse['depot']
        load = 0
        picked_nodes = set()
        distance = 0
        pick_task_keys = []
        node_tasks = defaultdict(list)
        for task in wave['tasks']:
            node_tasks[task['node']].append(task)
        expected_wave_keys = {(task['order'], task['lot'])
                              for task in wave['tasks']}
        for action in route_actions:
            if action['type'] == 'move':
                edge = warehouse['edge_by_id'].get(action['edge'])
                if edge is None:
                    raise ValueError('internal: move references unknown edge')
                if edge['from'] != current or edge['to'] != action['to']:
                    raise ValueError('internal: move topology mismatch')
                if action['from'] != current:
                    raise ValueError('internal: move origin mismatch')
                if load > warehouse['cart_capacity'] or \
                        load > edge['max_load']:
                    raise ValueError('internal: move violates load limit')
                if action['load'] != load:
                    raise ValueError('internal: move load trace mismatch')
                distance += edge['distance']
                current = edge['to']
            elif action['type'] == 'pick':
                node = action['node']
                if node != current:
                    raise ValueError('internal: pick away from current node')
                if node in picked_nodes:
                    raise ValueError('internal: node picked twice in a wave')
                picked_nodes.add(node)
                merged = sorted(node_tasks.get(node, []),
                                key=lambda task: (task['order'], task['lot']))
                listed = sorted(action['tasks'],
                                key=lambda task: (task['order'], task['lot']))
                if listed != [_task_record(task) for task in merged]:
                    raise ValueError('internal: pick task list mismatch')
                increment = sum(task['weight'] for task in merged)
                if action['increment'] != increment:
                    raise ValueError('internal: pick increment mismatch')
                if action['load_before'] != load:
                    raise ValueError('internal: pick load trace mismatch')
                load += increment
                if action['load_after'] != load:
                    raise ValueError('internal: pick load after mismatch')
                if load > warehouse['cart_capacity']:
                    raise ValueError('internal: cart capacity exceeded')
                pick_task_keys.extend(
                    (task['order'], task['lot']) for task in merged)
            else:
                raise ValueError('internal: unknown action type')
        if current != warehouse['depot']:
            raise ValueError('internal: wave does not end at the depot')
        if picked_nodes != set(node_tasks):
            raise ValueError('internal: wave left tasks unpicked')
        if set(pick_task_keys) != expected_wave_keys or \
                len(pick_task_keys) != len(expected_wave_keys):
            raise ValueError('internal: pick actions do not match wave tasks')
        if distance != wave['distance']:
            raise ValueError('internal: wave distance mismatch')
        if load != wave['final_weight']:
            raise ValueError('internal: wave final weight mismatch')
        for task in wave['tasks']:
            key = (task['order'], task['lot'])
            if key in seen:
                raise ValueError('internal: task appears in two waves')
            seen[key] = task['quantity']
    if set(seen) != set(expected):
        raise ValueError('internal: wave task set is not the allocation set')
    for key, quantity in seen.items():
        if quantity != expected[key]['quantity']:
            raise ValueError('internal: wave task quantity changed')
    if report['total_waves'] != len(report['waves']):
        raise ValueError('internal: total_waves mismatch')
    total = sum(wave['distance'] for wave in report['waves'])
    if report['total_distance'] != total:
        raise ValueError('internal: total_distance mismatch')


# --------------------------------------------------------------------------
# Public compiler entry point
# --------------------------------------------------------------------------

def compile_report(data, source_sha256):
    fulfillment = _bounded_fulfillment(data)
    result = analyze(fulfillment)

    lot_skus = {lot['id']: lot['sku'] for lot in fulfillment['lots']}
    order_skus = {order['sku'] for order in fulfillment['orders']}
    warehouse = parse_warehouse(data['warehouse'], lot_skus, order_skus)

    tasks_by_order, all_tasks = build_tasks(result, warehouse)
    groups = compile_waves(tasks_by_order, warehouse)

    waves = [_expand_wave(plan, tasks_by_order) for plan in groups]

    zero_orders = sorted(row['order'] for row in result['orders']
                         if row['fulfilled'] == 0)
    shortages = [{'order': row['order'], 'quantity': row['shortage']}
                 for row in result['orders'] if row['shortage'] > 0]

    report = {
        'schema_version': SCHEMA_VERSION,
        'source_sha256': source_sha256,
        'fulfillment': result,
        'waves': waves,
        'total_waves': len(waves),
        'total_distance': sum(wave['distance'] for wave in waves),
        'zero_fulfillment_orders': zero_orders,
        'shortages': shortages,
    }
    _validate_report(report, warehouse, all_tasks)
    # Task conservation against the v1 allocation quantities, independently of
    # the route replay performed by _validate_report.
    compiled = defaultdict(int)
    for wave in report['waves']:
        for task in wave['tasks']:
            compiled[(task['order'], task['lot'])] += task['quantity']
    for order_row in result['orders']:
        for allocation in order_row['allocations']:
            key = (order_row['order'], allocation['lot'])
            if compiled.get(key) != allocation['quantity']:
                raise ValueError('internal: allocation is not in exactly one '
                                 'wave task')
    return report


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------

DESCRIPTION = ('Compile fixed v1 lot allocations into load-aware warehouse '
               'pick waves')


def _check_paths(source, output):
    if output.is_symlink():
        raise ValueError('output cannot be a symlink')
    source_resolved = source.resolve()
    output_resolved = output.resolve()
    if source_resolved == output_resolved:
        raise ValueError('input and output must differ')
    # Hard links are the same inode under different names; resolve() cannot
    # detect them, so compare filesystem identity when both paths exist.
    if output.exists() and os.path.samefile(source, output):
        raise ValueError('input and output must not be the same file')


def run(source):
    data, digest = read(source)
    return compile_report(data, digest)


def main(argv=None):
    import argparse
    import json
    import sys

    parser = argparse.ArgumentParser(prog='perishable-wave',
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
