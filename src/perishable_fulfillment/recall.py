"""Offline batch recall impact analysis (schema_version=5).

Recall analysts know the confirmed tainted unit count of every source batch
plus the production split/merge transfer records, but not which destination
the tainted packages took after each mixing step.  A *unit* here is an
indivisible packaged traceability item: transfers only move whole units, so
there is no contamination spread, concentration or proportional mixing.  SKUs
may change when packaging changes, while the unit count is always conserved.

This is a one-shot, read-only analysis.  It never writes physical stock, never
re-runs the commercial allocation: the unchanged v1 ``model.analyze`` result
fixes where every lot quantity ends up (order allocations, held, expired,
available), and the tainted units are then explained over:

  * the directed acyclic material graph (transfers carry known total unit
    counts and observed ``[lower, upper]`` tainted-count bounds), and
  * the fixed lot fulfillment segments (each segment may hold 0..its physical
    quantity of tainted units; segment tainted totals equal lot inflow).

Every unknown tainted quantity is an integer.  The set of complete integer
*contamination explanations* is a bounded multi-source network flow, solved
with compact bulk capacities (never per-unit expansion, never floats):

  * structural quantity conservation is validated separately -- a violation
    is an illegal input (exit 2);
  * tainted-flow feasibility is a separate fact -- structurally legal inputs
    with no integer tainted explanation are reported ``inconsistent``;
  * tight per-recipient ``min_tainted``/``max_tainted`` come from monotone
    binary searches over fresh lower-bound feasibility checks, so each extreme
    is attained by at least one complete explanation;
  * the canonical witness lexicographically fixes the vector of transfer then
    segment tainted counts: each variable in turn is pinned at the smallest
    value that keeps the growing prefix feasible (a fresh feasibility check
    per probe, never a per-arc greedy choice that could break downstream
    feasibility);
  * each query independently re-states extra recipient bounds on top of the
    same original facts and gets its own feasibility verdict and witness.

Only the Python standard library is used.
"""

from __future__ import annotations

import os
from collections import defaultdict
from pathlib import Path

from .common import ident, require, rows, read, save
from .model import analyze

SCHEMA_VERSION = 5
MAX_NODES = 96
MAX_EDGES = 256
MAX_SOURCES = 32
MAX_LOTS = 32
MAX_ORDERS = 32
MAX_QUERIES = 16
MAX_WASTE = 32
MAX_QUANTITY = 10 ** 9
MAX_BOUND = 10 ** 18
# Residual capacities never need to exceed the largest legal integer bound.
INF = MAX_BOUND

SEGMENT_KINDS = ('held', 'expired', 'available')
RECIPIENT_KINDS = ('order', 'held', 'expired', 'available', 'waste')


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


def _kind(value, allowed, label):
    if not isinstance(value, str) or value not in allowed:
        raise ValueError('%s must be one of %s' % (label, ', '.join(allowed)))
    return value


# --------------------------------------------------------------------------
# Strict input parsing and structural (physical) validation
# --------------------------------------------------------------------------

def parse_input(data):
    """Validate the whole v5 source and return a normalized fact view.

    Structural checks (fields, references, topology, physical quantity
    conservation) raise ValueError.  Whether the tainted integer flow itself
    is feasible is decided later by the flow engine; an infeasible fact set is
    a legal input reported as ``inconsistent``.
    """
    require(data, 'schema_version fulfillment trace queries')
    if type(data['schema_version']) is not int \
            or data['schema_version'] != SCHEMA_VERSION:
        raise ValueError('schema_version must be the integer 5')

    fulfillment = data['fulfillment']
    require(fulfillment, 'as_of lots orders')
    rows(fulfillment['lots'], MAX_LOTS)
    rows(fulfillment['orders'], MAX_ORDERS)
    # model.analyze remains the single authority for the v1 object contract.
    v1_result = analyze(fulfillment)
    raw_lots = fulfillment['lots']
    raw_orders = fulfillment['orders']

    trace = data['trace']
    require(trace, 'nodes sources transfers lot_nodes waste_nodes')

    # --- nodes -------------------------------------------------------------
    node_list = rows(trace['nodes'], MAX_NODES)
    nodes = []
    seen_nodes = set()
    for raw in node_list:
        node = ident(raw)
        if node in seen_nodes:
            raise ValueError('duplicate node id: ' + node)
        seen_nodes.add(node)
        nodes.append(node)

    # --- sources -----------------------------------------------------------
    source_list = rows(trace['sources'], MAX_SOURCES)
    sources = []
    source_nodes = set()
    for raw in source_list:
        require(raw, 'node quantity tainted')
        node = ident(raw['node'])
        if node not in seen_nodes:
            raise ValueError('source references an unknown node: ' + node)
        if node in source_nodes:
            raise ValueError('duplicate source for node: ' + node)
        quantity = _integer(raw['quantity'], 0, MAX_QUANTITY, 'source quantity')
        tainted = _integer(raw['tainted'], 0, quantity, 'source tainted')
        source_nodes.add(node)
        sources.append({'node': node, 'quantity': quantity,
                        'tainted': tainted})

    # --- transfers ---------------------------------------------------------
    transfer_list = rows(trace['transfers'], MAX_EDGES)
    transfers = []
    edge_ids = set()
    for raw in transfer_list:
        require(raw, 'id from to quantity lower upper')
        edge_id = ident(raw['id'])
        if edge_id in edge_ids:
            raise ValueError('duplicate transfer id: ' + edge_id)
        edge_ids.add(edge_id)
        frm = ident(raw['from'])
        to = ident(raw['to'])
        if frm not in seen_nodes or to not in seen_nodes:
            raise ValueError('transfer endpoint is not a known node')
        if frm == to:
            raise ValueError('self-loop transfer is not allowed')
        quantity = _integer(raw['quantity'], 0, MAX_QUANTITY,
                            'transfer quantity')
        lower = _integer(raw['lower'], 0, quantity, 'transfer lower')
        upper = _integer(raw['upper'], lower, quantity, 'transfer upper')
        transfers.append({'id': edge_id, 'from': frm, 'to': to,
                          'quantity': quantity, 'lower': lower,
                          'upper': upper})

    # --- lot_nodes: exactly one row per fulfillment lot --------------------
    lot_node_rows = rows(trace['lot_nodes'], MAX_LOTS)
    lot_to_node = {}
    lot_terminal_nodes = set()
    raw_lot_ids = {lot['id'] for lot in raw_lots}
    for raw in lot_node_rows:
        require(raw, 'node lot')
        node = ident(raw['node'])
        lot = ident(raw['lot'])
        if node not in seen_nodes:
            raise ValueError('lot_nodes references an unknown node: ' + node)
        if lot not in raw_lot_ids:
            raise ValueError('lot_nodes references an unknown lot: ' + lot)
        if lot in lot_to_node:
            raise ValueError('duplicate lot_nodes entry for lot: ' + lot)
        if node in lot_terminal_nodes:
            raise ValueError('duplicate lot terminal node: ' + node)
        lot_to_node[lot] = node
        lot_terminal_nodes.add(node)
    if set(lot_to_node) != raw_lot_ids:
        missing = sorted(raw_lot_ids - set(lot_to_node))
        extra = sorted(set(lot_to_node) - raw_lot_ids)
        detail = []
        if missing:
            detail.append('missing ' + ', '.join(missing))
        if extra:
            detail.append('unknown ' + ', '.join(extra))
        raise ValueError('lot_nodes must cover every fulfillment lot exactly '
                         'once' + (': ' + '; '.join(detail) if detail else ''))

    # --- waste terminals ---------------------------------------------------
    waste_rows = rows(trace['waste_nodes'], MAX_WASTE)
    waste_nodes = set()
    for raw in waste_rows:
        node = ident(raw)
        if node not in seen_nodes:
            raise ValueError('waste_nodes references an unknown node: ' + node)
        if node in waste_nodes:
            raise ValueError('duplicate waste node: ' + node)
        waste_nodes.add(node)

    # --- the three terminal kinds are mutually exclusive -------------------
    overlap = (source_nodes & lot_terminal_nodes) \
        or (source_nodes & waste_nodes) \
        or (lot_terminal_nodes & waste_nodes)
    if overlap:
        raise ValueError('source, lot and waste node sets must be disjoint')
    terminal_nodes = lot_terminal_nodes | waste_nodes
    intermediate_nodes = seen_nodes - source_nodes - terminal_nodes

    # --- physical quantity totals per node --------------------------------
    in_qty = {node: 0 for node in nodes}
    out_qty = {node: 0 for node in nodes}
    incoming = defaultdict(list)   # node -> transfer ids
    outgoing = defaultdict(list)
    for edge in transfers:
        out_qty[edge['from']] += edge['quantity']
        in_qty[edge['to']] += edge['quantity']
        incoming[edge['to']].append(edge['id'])
        outgoing[edge['from']].append(edge['id'])

    lot_quantity = {lot['id']: lot['quantity'] for lot in raw_lots}
    for source in sources:
        node = source['node']
        if incoming[node]:
            raise ValueError('source node must have no incoming transfer: '
                             + node)
        if out_qty[node] != source['quantity']:
            raise ValueError('source outgoing quantity does not balance: '
                             + node)
    for node in sorted(lot_terminal_nodes):
        if outgoing[node]:
            raise ValueError('lot terminal must have no outgoing transfer: '
                             + node)
        lot_id = next(lot for lot, terminal in lot_to_node.items()
                      if terminal == node)
        if in_qty[node] != lot_quantity[lot_id]:
            raise ValueError('lot terminal incoming quantity does not match '
                             'lot.quantity: ' + lot_id)
    for node in sorted(waste_nodes):
        if outgoing[node]:
            raise ValueError('waste terminal must have no outgoing transfer: '
                             + node)
    for node in sorted(intermediate_nodes):
        if in_qty[node] != out_qty[node]:
            raise ValueError('intermediate node quantity is not conserved: '
                             + node)
        if not incoming[node] and not outgoing[node]:
            raise ValueError('intermediate node is not connected to any '
                             'transfer: ' + node)

    # --- directed acyclicity (Kahn) ----------------------------------------
    indegree = {node: len(incoming[node]) for node in nodes}
    queue = sorted(node for node in nodes if indegree[node] == 0)
    head = 0
    while head < len(queue):
        node = queue[head]
        head += 1
        for edge_id in sorted(outgoing[node]):
            edge = next(e for e in transfers if e['id'] == edge_id)
            child = edge['to']
            indegree[child] -= 1
            if indegree[child] == 0:
                queue.append(child)
    if len(queue) != len(nodes):
        raise ValueError('material graph must be acyclic')

    # The empty graph (no nodes) can only accompany empty lots; zero
    # fulfillment orders remain legal.
    if not nodes and (raw_lots or sources or transfers or lot_to_node
                      or waste_nodes):
        raise ValueError('empty graph cannot carry sources, lots or waste')
    # A graph with nodes but no edges cannot route anything, so lot and waste
    # terminals in it are unreachable; reject rather than silently accept.
    if nodes and not transfers:
        raise ValueError('graph with nodes but no transfers cannot reach its '
                         'lot or waste terminals')

    # --- queries: structural validation against the recipient universe ----
    order_ids = {order['id'] for order in raw_orders}
    queries = _parse_queries(data['queries'], raw_lot_ids, order_ids,
                             waste_nodes)

    return {
        'nodes': nodes,
        'sources': sources,
        'transfers': transfers,
        'lot_to_node': lot_to_node,
        'waste_nodes': waste_nodes,
        'in_qty': in_qty,
        'out_qty': out_qty,
        'raw_lots': raw_lots,
        'raw_orders': raw_orders,
        'queries': queries,
        'v1_result': v1_result,
    }


def _parse_queries(raw_queries, lot_ids, order_ids, waste_nodes):
    query_list = rows(raw_queries, MAX_QUERIES)
    queries = []
    seen_query_ids = set()
    # The complete recipient key universe, independent of tainted feasibility.
    recipient_keys = set()
    for oid in order_ids:
        recipient_keys.add(('order', oid))
    for lot_id in lot_ids:
        for kind in SEGMENT_KINDS:
            recipient_keys.add((kind, lot_id))
    for node in waste_nodes:
        recipient_keys.add(('waste', node))
    for raw in query_list:
        require(raw, 'id bounds')
        query_id = ident(raw['id'])
        if query_id in seen_query_ids:
            raise ValueError('duplicate query id: ' + query_id)
        seen_query_ids.add(query_id)
        bounds = rows(raw['bounds'], len(recipient_keys) or 1)
        parsed_bounds = []
        seen_targets = set()
        for bound in bounds:
            require(bound, 'kind id lower upper')
            kind = _kind(bound['kind'], RECIPIENT_KINDS, 'query bound kind')
            target_id = ident(bound['id'])
            key = (kind, target_id)
            if key not in recipient_keys:
                raise ValueError('query bound references an unknown recipient: '
                                 + kind + '/' + target_id)
            if key in seen_targets:
                raise ValueError('duplicate query bound for recipient: '
                                 + kind + '/' + target_id)
            seen_targets.add(key)
            lower = _integer(bound['lower'], 0, MAX_BOUND, 'query lower')
            upper = _integer(bound['upper'], lower, MAX_BOUND, 'query upper')
            parsed_bounds.append({'kind': kind, 'id': target_id,
                                  'lower': lower, 'upper': upper})
        queries.append({'id': query_id, 'bounds': parsed_bounds})
    return queries


# --------------------------------------------------------------------------
# Fixed fulfillment segments from the unchanged v1 allocation
# --------------------------------------------------------------------------

def build_segments(parsed):
    """Split every lot's fixed physical quantity into recall segments.

    The v1 allocation is never redone.  Each lot keeps held, expired and
    available segments (zero-quantity ones included), plus one order segment
    per positive v1 allocation.  Segment quantities of a lot sum exactly to
    the lot's original quantity.  Expiry on as_of is still unexpired.
    """
    v1 = parsed['v1_result']
    lot_rows = {row['lot']: row for row in v1['lots']}
    segments = []
    # Order allocations grouped by lot from the fixed v1 result.
    allocations_by_lot = defaultdict(list)
    for order_row in v1['orders']:
        for allocation in order_row['allocations']:
            if allocation['quantity'] > 0:
                allocations_by_lot[allocation['lot']].append(
                    (order_row['order'], allocation['quantity']))
    for raw_lot in sorted(parsed['raw_lots'], key=lambda lot: lot['id']):
        lot_id = raw_lot['id']
        row = lot_rows[lot_id]
        quantity = row['quantity']
        held = row['held']
        if row['expired']:
            expired_qty = quantity - held
            available_qty = 0
        else:
            expired_qty = 0
            available_qty = row['available_remaining']
        segments.append({'kind': 'held', 'id': lot_id, 'lot': lot_id,
                         'quantity': held})
        segments.append({'kind': 'expired', 'id': lot_id, 'lot': lot_id,
                         'quantity': expired_qty})
        segments.append({'kind': 'available', 'id': lot_id, 'lot': lot_id,
                         'quantity': available_qty})
        for order_id, allocation_qty in allocations_by_lot[lot_id]:
            segments.append({'kind': 'order', 'id': order_id, 'lot': lot_id,
                             'quantity': allocation_qty})
        total = sum(seg['quantity'] for seg in segments
                    if seg['lot'] == lot_id)
        if total != quantity:
            raise ValueError('internal: lot segments do not sum to lot '
                             'quantity: ' + lot_id)
    return segments


def build_recipients(parsed, segments):
    """The complete recipient key universe with fixed physical quantities."""
    quantity_by_key = {}
    for order in sorted(parsed['raw_orders'], key=lambda row: row['id']):
        quantity_by_key[('order', order['id'])] = 0
    for seg in segments:
        if seg['kind'] == 'order':
            key = ('order', seg['id'])
            quantity_by_key[key] = quantity_by_key.get(key, 0) \
                + seg['quantity']
        else:
            quantity_by_key[(seg['kind'], seg['id'])] = seg['quantity']
    for node in sorted(parsed['waste_nodes']):
        quantity_by_key[('waste', node)] = parsed['in_qty'][node]
    return [{'kind': key[0], 'id': key[1], 'quantity': quantity_by_key[key]}
            for key in sorted(quantity_by_key)]


# --------------------------------------------------------------------------
# Compact bounded integer flow over tainted units
# --------------------------------------------------------------------------

class _Arc:
    """One logical arc with both directions of its residual capacity.

    Invariant: ``flow - lower == rev_cap`` and ``upper - flow == cap``, so
    ``flow = lower + rev_cap`` always holds.  The Dinic engine only mutates
    the two free capacities; the valid range is enforced by construction.
    """

    __slots__ = ('frm', 'to', 'cap', 'rev_cap', 'lower', 'upper', 'key')

    def __init__(self, frm, to, lower, upper, key):
        self.frm = frm
        self.to = to
        self.lower = lower
        self.upper = upper
        self.key = key
        self.cap = upper - lower
        self.rev_cap = 0

    def flow(self):
        return self.lower + self.rev_cap


class ResidualGraph:
    """Residual Dinic network over bulk integer free capacities.

    Capacities stay Python integers up to 10**18; nothing is ever expanded
    per unit and no float is used.
    """

    def __init__(self, n):
        self.n = n
        self.g = [[] for _ in range(n)]
        self.arc_index = {}

    def add_arc(self, frm, to, lower, upper, key):
        arc = _Arc(frm, to, lower, upper, key)
        self.g[frm].append(arc)
        self.g[to].append(arc)
        if key is not None:
            self.arc_index[key] = arc
        return arc

    def max_flow(self, source, sink, limit=None):
        n = self.n
        pushed = 0
        while True:
            level = [-1] * n
            level[source] = 0
            queue = [source]
            head = 0
            while head < len(queue):
                v = queue[head]
                head += 1
                for arc in self.g[v]:
                    if arc.frm == v:
                        nxt, cap = arc.to, arc.cap
                    else:
                        nxt, cap = arc.frm, arc.rev_cap
                    if cap > 0 and level[nxt] == -1:
                        level[nxt] = level[v] + 1
                        queue.append(nxt)
            if level[sink] < 0:
                return pushed

            def dfs(v, budget):
                if v == sink:
                    return budget
                while it[v] < len(self.g[v]):
                    arc = self.g[v][it[v]]
                    if arc.frm == v:
                        nxt, cap_attr, rev_attr = arc.to, 'cap', 'rev_cap'
                    else:
                        nxt, cap_attr, rev_attr = arc.frm, 'rev_cap', 'cap'
                    if getattr(arc, cap_attr) > 0 and \
                            level[nxt] == level[v] + 1:
                        add = dfs(nxt, min(budget, getattr(arc, cap_attr)))
                        if add:
                            setattr(arc, cap_attr,
                                    getattr(arc, cap_attr) - add)
                            setattr(arc, rev_attr,
                                    getattr(arc, rev_attr) + add)
                            return add
                    it[v] += 1
                return 0

            it = [0] * n
            while True:
                budget = INF if limit is None else limit - pushed
                if budget <= 0:
                    return pushed
                add = dfs(source, budget)
                if not add:
                    break
                pushed += add

    def arcs(self):
        seen = set()
        for adjacency in self.g:
            for arc in adjacency:
                if id(arc) not in seen:
                    seen.add(id(arc))
                    yield arc


def _recipient_arc_key(kind, target_id):
    if kind in ('order', 'waste'):
        return 'rec', kind, target_id
    # held/expired/available recipients are exactly their lot's own segment.
    return 'seg', kind, target_id, target_id


def _arc_specs(parsed, segments, order_ids, order_node, node_index,
               bound_overrides):
    """Canonical (frm, to, lower, upper, key) list for one bound setting."""
    specs = []
    # Fixed source contamination: exactly the confirmed tainted count leaves
    # each source through an origin -> source arc.
    for source in sorted(parsed['sources'], key=lambda row: row['node']):
        specs.append((order_node['__origin__'],
                      node_index[source['node']],
                      source['tainted'], source['tainted'],
                      ('src', source['node'])))
    for edge in sorted(parsed['transfers'], key=lambda row: row['id']):
        key = ('edge', edge['id'])
        lower, upper = bound_overrides.get(key,
                                           (edge['lower'], edge['upper']))
        if lower > upper:
            return None
        specs.append((node_index[edge['from']], node_index[edge['to']],
                      lower, upper, key))
    for node in sorted(parsed['waste_nodes']):
        key = ('rec', 'waste', node)
        lower, upper = bound_overrides.get(
            key, (0, parsed['in_qty'][node]))
        if lower > upper:
            return None
        specs.append((node_index[node], order_node['__sink__'], lower,
                      upper, key))
    order_quantity = defaultdict(int)
    for seg in sorted(segments,
                      key=lambda row: (row['kind'], row['id'], row['lot'])):
        key = ('seg', seg['kind'], seg['id'], seg['lot'])
        lower, upper = bound_overrides.get(key, (0, seg['quantity']))
        if lower > upper:
            return None
        terminal = node_index[parsed['lot_to_node'][seg['lot']]]
        if seg['kind'] == 'order':
            specs.append((terminal, order_node[seg['id']], lower, upper,
                          key))
            order_quantity[seg['id']] += seg['quantity']
        else:
            specs.append((terminal, order_node['__sink__'], lower, upper,
                          key))
    for oid in order_ids:
        key = ('rec', 'order', oid)
        lower, upper = bound_overrides.get(
            key, (0, order_quantity.get(oid, 0)))
        if lower > upper:
            return None
        specs.append((order_node[oid], order_node['__sink__'], lower,
                      upper, key))
    return specs


def feasible_flows(parsed, segments, bound_overrides=None):
    """Return one concrete feasible flow vector, or None when infeasible.

    The tainted flow is a lower-bound circulation: fixed origin->source arcs
    inject the confirmed taint, material and segment arcs carry their
    observed ranges, and one fixed sink->origin return edge closes the
    circulation at the total tainted volume.  Feasibility is the standard
    super-source/super-sink balance transformation solved by Dinic.
    """
    bound_overrides = bound_overrides or {}
    nodes = sorted(parsed['nodes'])
    node_index = {node: i for i, node in enumerate(nodes)}
    order_ids = sorted(order['id'] for order in parsed['raw_orders'])
    order_node = {oid: len(nodes) + i for i, oid in enumerate(order_ids)}
    origin = len(nodes) + len(order_ids)
    sink = origin + 1
    order_node['__origin__'] = origin
    order_node['__sink__'] = sink
    super_source = sink + 1
    super_sink = sink + 2
    total_taint = sum(source['tainted'] for source in parsed['sources'])

    specs = _arc_specs(parsed, segments, order_ids, order_node,
                       node_index, bound_overrides)
    if specs is None:
        return None

    graph = ResidualGraph(super_sink + 1)
    balance = [0] * (super_sink + 1)
    for frm, to, lower, upper, key in specs:
        arc = graph.add_arc(frm, to, lower, upper, key)
        arc.cap = upper - lower
        balance[frm] -= lower
        balance[to] += lower
    # Return edge closing the circulation.  It is excluded from the balance
    # transformation: the fixed source arcs already balance the origin and
    # the terminals balance globally, and giving it a balance entry would
    # double-count the total taint.  Its upper bound pins the return volume.
    graph.add_arc(sink, origin, 0, total_taint, ('__return__',))

    need = 0
    for vertex, bal in enumerate(balance):
        if bal > 0:
            graph.add_arc(super_source, vertex, 0, bal, None)
            need += bal
        elif bal < 0:
            graph.add_arc(vertex, super_sink, 0, -bal, None)
    if graph.max_flow(super_source, super_sink, need) != need:
        return None
    return_arc = graph.arc_index[('__return__',)]
    if return_arc.flow() != total_taint:
        return None
    return {key: graph.arc_index[key].flow()
            for _frm, _to, _lo, _hi, key in specs}


def _canonical_var_keys(parsed, segments):
    keys = [('edge', edge['id'])
            for edge in sorted(parsed['transfers'],
                               key=lambda row: row['id'])]
    keys += [('seg', seg['kind'], seg['id'], seg['lot'])
             for seg in sorted(segments,
                               key=lambda row: (row['kind'], row['id'],
                                                row['lot']))]
    return keys


def _arc_range(parsed, segments, key):
    """Original [lower, upper] range of a witness variable arc."""
    if key[0] == 'edge':
        edge = next(e for e in parsed['transfers'] if e['id'] == key[1])
        return edge['lower'], edge['upper']
    seg = next(s for s in segments
               if (s['kind'], s['id'], s['lot']) == key[1:])
    return 0, seg['quantity']


def recipient_extrema(parsed, segments, recipients):
    """Tight min/max tainted count for every recipient.

    Each extreme is found by binary search over the arc's integer value with
    a fresh lower-bound feasibility check at every probe, so both reported
    bounds are attained by at least one complete explanation.
    """
    result = {}
    for recipient in recipients:
        arc_key = _recipient_arc_key(recipient['kind'], recipient['id'])
        lower, upper = _recipient_native_range(parsed, segments, recipient)

        def at_least(v):
            return feasible_flows(
                parsed, segments, {arc_key: (v, upper)}) is not None

        def at_most(v):
            return feasible_flows(
                parsed, segments, {arc_key: (lower, v)}) is not None

        # Tight minimum: smallest v for which "at most v" is feasible.
        # The feasible exact values form a contiguous interval, and the
        # at-most predicate is monotone increasing, so its first success is
        # precisely the smallest attainable value.
        lo, hi = lower, upper
        while lo < hi:
            mid = (lo + hi) // 2
            if at_most(mid):
                hi = mid
            else:
                lo = mid + 1
        minimum = lo
        # Tight maximum: largest v for which "at least v" is feasible.  The
        # at-least predicate is monotone decreasing, so its last success is
        # precisely the largest attainable value.
        lo, hi = lower, upper
        while lo < hi:
            mid = (lo + hi + 1) // 2
            if at_least(mid):
                lo = mid
            else:
                hi = mid - 1
        maximum = lo
        result[(recipient['kind'], recipient['id'])] = (minimum, maximum)
    return result

def _recipient_native_range(parsed, segments, recipient):
    """Widest [0, upper] range the recipient arc can physically carry."""
    kind = recipient['kind']
    if kind == 'waste':
        node = next(n for n in parsed['waste_nodes']
                    if n == recipient['id'])
        return 0, parsed['in_qty'][node]
    if kind == 'order':
        # The order recipient arc's capacity is the sum of its order
        # segments' physical quantities (zero for an unfulfilled order).
        upper = sum(seg['quantity'] for seg in segments
                     if seg['kind'] == 'order' and seg['id'] ==
                     recipient['id'])
        return 0, upper
    # held/expired/available: exactly one lot segment of that kind.
    seg = next(s for s in segments
               if s['kind'] == kind and s['id'] == recipient['id'])
    return 0, seg['quantity']


def canonical_witness(parsed, segments, bound_overrides=None):
    """Lexicographically smallest complete tainted-count vector.

    Variables are fixed one at a time in canonical order.  For each arc the
    tight minimum attainable while keeping every earlier variable fixed is
    found by binary search over fresh feasibility checks with the growing
    prefix pinned; this is a global lexicographic minimization over complete
    explanations, never a per-arc greedy that could break downstream
    feasibility.  Returns the locked vector keyed by the canonical arc keys,
    or None when the bound setting itself is infeasible.
    """
    overrides = dict(bound_overrides or {})
    locked = {}
    for key in _canonical_var_keys(parsed, segments):
        native_lower, native_upper = _arc_range(parsed, segments, key)
        override = overrides.get(key)
        low = max(native_lower, override[0]) if override else native_lower
        high = min(native_upper, override[1]) if override else native_upper
        if low > high:
            return None
        lo, hi = low, high
        while lo < hi:
            mid = (lo + hi) // 2
            probe = dict(overrides)
            probe.update(locked)
            probe[key] = (native_lower, mid)
            if feasible_flows(parsed, segments, probe) is not None:
                hi = mid
            else:
                lo = mid + 1
        locked[key] = (lo, lo)
    # Final feasibility confirmation and extraction of the concrete flow.
    final_overrides = dict(overrides)
    final_overrides.update(locked)
    flows = feasible_flows(parsed, segments, final_overrides)
    if flows is None:
        return None
    return {key: flows[key] for key in _canonical_var_keys(parsed, segments)}


# --------------------------------------------------------------------------
# Witness replay against the original facts
# --------------------------------------------------------------------------

def verify_witness(parsed, segments, edge_taint, segment_taint, bounds=None):
    """Replay every conservation rule from inputs and one witness.

    ``segment_taint`` is keyed by the full 4-tuple
    ``('seg', kind, id, lot)``.  Raises ValueError on any mismatch; used
    before a report is published and reused by the test suite's independent
    checks.
    """
    transfer_by_id = {edge['id']: edge for edge in parsed['transfers']}
    for edge_id, tainted in edge_taint.items():
        edge = transfer_by_id[edge_id]
        if not edge['lower'] <= tainted <= edge['upper']:
            raise ValueError('internal: witness violates transfer bounds')
    for seg in segments:
        key = ('seg', seg['kind'], seg['id'], seg['lot'])
        tainted = segment_taint[key]
        if not 0 <= tainted <= seg['quantity']:
            raise ValueError('internal: witness segment tainted out of range')

    inflow = defaultdict(int)
    outflow = defaultdict(int)
    for edge_id, tainted in edge_taint.items():
        edge = transfer_by_id[edge_id]
        outflow[edge['from']] += tainted
        inflow[edge['to']] += tainted
    for source in parsed['sources']:
        if outflow[source['node']] != source['tainted'] \
                or inflow[source['node']] != 0:
            raise ValueError('internal: source tainted balance broken')
    lot_segments = defaultdict(list)
    for seg in segments:
        lot_segments[seg['lot']].append(seg)
    for lot_id, node in parsed['lot_to_node'].items():
        segment_total = sum(
            segment_taint[('seg', seg['kind'], seg['id'], seg['lot'])]
            for seg in lot_segments[lot_id])
        if inflow[node] != segment_total or outflow[node] != 0:
            raise ValueError('internal: lot terminal tainted balance broken')
    for node in parsed['waste_nodes']:
        if outflow[node] != 0:
            raise ValueError('internal: waste terminal has tainted outflow')
    terminals = set(parsed['lot_to_node'].values()) | parsed['waste_nodes']
    source_nodes = {source['node'] for source in parsed['sources']}
    for node in parsed['nodes']:
        if node not in terminals and node not in source_nodes \
                and inflow[node] != outflow[node]:
            raise ValueError('internal: intermediate tainted balance broken')

    if bounds is not None:
        recipient_totals = recipient_totals_from_witness(
            parsed, segments, edge_taint, segment_taint)
        for bound in bounds:
            total = recipient_totals[(bound['kind'], bound['id'])]
            if not bound['lower'] <= total <= bound['upper']:
                raise ValueError('internal: witness violates query bound')


def recipient_totals_from_witness(parsed, segments, edge_taint,
                                  segment_taint):
    totals = defaultdict(int)
    for order in parsed['raw_orders']:
        totals[('order', order['id'])] = 0
    for seg in segments:
        tainted = segment_taint[('seg', seg['kind'], seg['id'], seg['lot'])]
        if seg['kind'] == 'order':
            totals[('order', seg['id'])] += tainted
        else:
            totals[(seg['kind'], seg['id'])] += tainted
    transfer_by_id = {edge['id']: edge for edge in parsed['transfers']}
    for node in parsed['waste_nodes']:
        totals[('waste', node)] = sum(
            tainted for edge_id, tainted in edge_taint.items()
            if transfer_by_id[edge_id]['to'] == node)
    return dict(totals)


def _witness_document(parsed, segments, vector):
    transfers = []
    for edge in sorted(parsed['transfers'], key=lambda row: row['id']):
        transfers.append({'edge': edge['id'],
                          'tainted': vector[('edge', edge['id'])]})
    segment_rows = []
    for seg in sorted(segments,
                      key=lambda row: (row['kind'], row['id'], row['lot'])):
        key = ('seg', seg['kind'], seg['id'], seg['lot'])
        segment_rows.append({'kind': seg['kind'], 'id': seg['id'],
                             'lot': seg['lot'], 'quantity': seg['quantity'],
                             'tainted': vector[key]})
    return {'transfers': transfers, 'segments': segment_rows}


def _vector_maps(parsed, segments, vector):
    edge_taint = {edge['id']: vector[('edge', edge['id'])]
                  for edge in parsed['transfers']}
    segment_taint = {('seg', seg['kind'], seg['id'], seg['lot']):
                     vector[('seg', seg['kind'], seg['id'], seg['lot'])]
                     for seg in segments}
    return edge_taint, segment_taint


# --------------------------------------------------------------------------
# Public analysis entry point
# --------------------------------------------------------------------------

def compile_report(data, source_sha256):
    """Validate, analyze and construct the full v5 recall report."""
    parsed = parse_input(data)
    segments = build_segments(parsed)
    recipients = build_recipients(parsed, segments)

    if feasible_flows(parsed, segments) is None:
        # Structurally legal facts with no integer contamination explanation.
        query_rows = [{'id': query['id'], 'feasible': False, 'witness': None}
                      for query in sorted(parsed['queries'],
                                          key=lambda row: row['id'])]
        return {
            'schema_version': SCHEMA_VERSION,
            'source_sha256': source_sha256,
            'fulfillment': parsed['v1_result'],
            'status': 'inconsistent',
            'recipients': [],
            'base_witness': None,
            'queries': query_rows,
        }

    extrema = recipient_extrema(parsed, segments, recipients)
    recipient_rows = []
    for recipient in recipients:
        key = (recipient['kind'], recipient['id'])
        minimum, maximum = extrema[key]
        if maximum == 0:
            status = 'clear'
        elif minimum > 0:
            status = 'certain'
        else:
            status = 'possible'
        recipient_rows.append({
            'kind': recipient['kind'], 'id': recipient['id'],
            'quantity': recipient['quantity'],
            'min_tainted': minimum, 'max_tainted': maximum,
            'status': status,
        })

    # Base witness: the canonical minimum under the original facts alone.
    base_vector = canonical_witness(parsed, segments)
    edge_taint, segment_taint = _vector_maps(parsed, segments, base_vector)
    verify_witness(parsed, segments, edge_taint, segment_taint)
    base_witness = _witness_document(parsed, segments, base_vector)

    query_rows = []
    for query in sorted(parsed['queries'], key=lambda row: row['id']):
        overrides = {}
        for bound in query['bounds']:
            overrides[_recipient_arc_key(bound['kind'], bound['id'])] = \
                (bound['lower'], bound['upper'])
        vector = canonical_witness(parsed, segments, overrides)
        if vector is None:
            query_rows.append({'id': query['id'], 'feasible': False,
                               'witness': None})
            continue
        edge_taint, segment_taint = _vector_maps(parsed, segments, vector)
        verify_witness(parsed, segments, edge_taint, segment_taint,
                       query['bounds'])
        query_rows.append({'id': query['id'], 'feasible': True,
                           'witness': _witness_document(parsed, segments,
                                                        vector)})

    return {
        'schema_version': SCHEMA_VERSION,
        'source_sha256': source_sha256,
        'fulfillment': parsed['v1_result'],
        'status': 'consistent',
        'recipients': recipient_rows,
        'base_witness': base_witness,
        'queries': query_rows,
    }


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------

DESCRIPTION = ('Offline perishable batch recall impact analysis over fixed v1 '
               'fulfillment segments')


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

    parser = argparse.ArgumentParser(prog='perishable-recall',
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
