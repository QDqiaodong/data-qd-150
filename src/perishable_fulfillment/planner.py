"""Global commitment planning with three strictly ordered objectives.

Inputs describe a single candidate ledger state (the state an event batch
would leave behind).  The planner computes integer lot reservations x[o, l]
for the still-unpicked demand.  Objectives, in strict priority order:

1. Lexicographically maximize the fulfillment vector (picked + reserved) of
   orders sorted by (priority, id).
2. Among all optima of (1), minimize total movement away from the previous
   committed reservations (sum of positive reductions of the old residual).
3. Among all optima of (1) and (2), lexicographically maximize the new
   reservation vector over order-lot pairs ordered by order (priority, id)
   then lot (expires, id) -- the deterministic FEFO tie-break.

The implementation uses compact integer network flow (order -> lot arcs with
bulk capacities): it never expands a 10**9 unit stock and never uses floats.
Layers 1 and 2 are separate, independently justified flow programs; layer 3
uses one exact integer lexicographic encoding whose numeric base is bounded by
the total demand, so the ordering is provable rather than a guessed weight.
"""

from .flow import Graph, max_flow, bounded_min_cost_flow


def _eligible(order, lot):
    """Marketability gate: same SKU, customer admission, remaining shelf life."""
    if order['sku'] != lot['sku']:
        return False
    allowed = order.get('allowed_lots')
    if allowed is not None and lot['id'] not in allowed:
        return False
    deadline = order.get('deadline')
    if deadline is None:
        from datetime import date, timedelta
        deadline = (date.fromisoformat(order['ship_date'])
                    + timedelta(days=order.get('min_life', 0))).isoformat()
    return lot['expires'] >= deadline


def plan(orders, lots, old_residual):
    """Compute the commitment plan for one candidate state.

    orders: dicts id, sku, priority, quantity (current demand), picked,
            deadline(ISO), allowed_lots (None or tuple of lot ids), canceled
    lots:   dicts id, sku, expires(ISO), quantity, held, picked
    old_residual: {(order_id, lot_id): units} -- last committed reservation
            minus picks consumed inside the current batch; entries for
            canceled or modified orders are still present.
    Returns (allocations, movement); allocations maps
    order_id -> {lot_id: reserved units} with positive entries only.
    """
    ordered_orders = sorted(orders, key=lambda o: (o['priority'], o['id']))
    ordered_lots = sorted(lots, key=lambda l: (l['expires'], l['id']))
    no, nl = len(ordered_orders), len(ordered_lots)
    source = 0
    order_base = 1
    lot_base = 1 + no
    sink = 1 + no + nl
    n_nodes = sink + 1
    order_idx = {o['id']: order_base + i for i, o in enumerate(ordered_orders)}
    lot_idx = {l['id']: lot_base + i for i, l in enumerate(ordered_lots)}

    free_cap = {l['id']: l['quantity'] - l.get('held', 0) - l.get('picked', 0)
                for l in lots}
    for cap in free_cap.values():
        if cap < 0:
            raise ValueError('frozen or picked quantity exceeds lot stock')
    remaining = {o['id']: max(o['quantity'] - o.get('picked', 0), 0)
                 for o in orders}

    pairs = [(o['id'], l['id']) for o in ordered_orders for l in ordered_lots
             if _eligible(o, l)]
    eligible = set(pairs)

    # Preserve the original residual (including entries later picked away or
    # made ineligible): forced losses there are real movement and must appear
    # in the reported total.  A cleaned view drives the flow arcs, whose
    # capacities cannot reference vanished pairs.
    original_old = dict(old_residual)
    cleaned_old = {}
    for (oid, lid), r in old_residual.items():
        if r <= 0:
            continue
        if (oid, lid) not in eligible:
            continue
        cap = min(free_cap[lid], remaining[oid])
        if cap <= 0:
            continue
        cleaned_old[(oid, lid)] = min(r, cap)
    # Forced movement on residuals that can no longer be preserved (picked
    # away, frozen, admission removed or shelf life lost).  It is a constant
    # added on top of the live-pair movement optimized by layers 2 and 3.
    forced_movement = sum(
        max(original_old.get(p, 0) - cleaned_old.get(p, 0), 0)
        for p in set(original_old) | set(cleaned_old))
    old_residual = cleaned_old

    def pair_upper(p):
        oid, lid = p
        # Both endpoints already exclude picked units: free_cap is stock left
        # after holds and picks, remaining is demand left after picks.
        return min(free_cap[lid], remaining[oid])

    # ---- Layer 1 ---------------------------------------------------------
    # Lexicographically maximize each order's reserved units by (priority,id):
    # prefix max flows, earlier orders fixed at their found optimum.
    fixed = {}
    for o in ordered_orders:
        graph = Graph(n_nodes)
        for oo in ordered_orders:
            cap = fixed[oo['id']] if oo['id'] in fixed else remaining[oo['id']]
            if cap:
                graph.add(source, order_idx[oo['id']], cap)
        for l in ordered_lots:
            if free_cap[l['id']]:
                graph.add(lot_idx[l['id']], sink, free_cap[l['id']])
        for p in pairs:
            upper = pair_upper(p)
            if upper:
                graph.add(order_idx[p[0]], lot_idx[p[1]], upper)
        max_flow(graph, source, sink)
        onode = order_idx[o['id']]
        mine = sum(graph.flow_of(e) for e in graph.g[onode]
                   if lot_base <= e.to < sink)
        fixed[o['id']] = mine
    total_flow = sum(fixed.values())

    # ---- Layer 2 ---------------------------------------------------------
    # Minimize movement = sum max(old[o,l] - new[o,l], 0).  Every pair gets
    # two parallel arcs: the first r=old_residual units at cost -1, extra
    # units at cost 0.  With total flow fixed, minimizing cost maximizes the
    # preserved old residual and hence minimizes movement.
    arcs = [(source, order_idx[o['id']], 0, remaining[o['id']], 0, None)
            for o in ordered_orders]
    arcs += [(lot_idx[l['id']], sink, 0, free_cap[l['id']], 0, None)
             for l in ordered_lots]
    for p in pairs:
        oid, lid = p
        upper = pair_upper(p)
        r = min(old_residual.get(p, 0), upper)
        if r:
            arcs.append((order_idx[oid], lot_idx[lid], 0, r, -1, ('d', p)))
        if upper - r:
            arcs.append((order_idx[oid], lot_idx[lid], 0, upper - r, 0,
                         ('e', p)))
    cost, named_flow = bounded_min_cost_flow(n_nodes, arcs, source, sink,
                                             total_flow)
    x = {p: 0 for p in pairs}
    for (kind, p), f in named_flow.items():
        x[p] += f
    # Movement optimized on the live pairs plus the constant forced loss on
    # residuals that vanished before this planning round.
    live_movement = sum(max(cleaned_old.get(p, 0) - x.get(p, 0), 0)
                        for p in pairs)
    movement = live_movement + forced_movement

    # ---- Layer 3 ---------------------------------------------------------
    x = _layer3(ordered_orders, ordered_lots, pairs, order_idx, lot_idx,
                source, sink, free_cap, remaining, total_flow, live_movement,
                cleaned_old, fixed)

    allocations = {}
    for (oid, lid), q in x.items():
        if q:
            allocations.setdefault(oid, {})[lid] = q
    return allocations, movement


def _layer3(ordered_orders, ordered_lots, pairs, order_idx, lot_idx,
            source, sink, free_cap, remaining, total_flow, movement,
            old_residual, order_reserved):
    """FEFO deterministic tie-break on the layer-1/layer-2 optimal face.

    Solved as ONE minimum-cost flow with a provable integer lexicographic
    weight rather than thousands of repeated flow programs.  Each pair is
    visited in the deterministic order (order (priority,id), then lot
    (expires,id)) and assigned a positional weight -B**(P-1-k) (negative
    because the flow program minimizes cost while we maximize the vector).
    The base B is one plus the total remaining demand, so it is strictly
    larger than the maximum flow any suffix of positions could ever carry:
    one extra unit on a higher-positioned pair therefore always outweighs
    every possible gain on all lower pairs combined.  This is the standard
    exact integer encoding of lexicographic maximization -- no floats, no
    per-unit expansion, and the base is derived from the data with an explicit
    bound rather than chosen as an unproven magic constant.  Movement
    preservation sits above it: preserving an old residual unit earns a
    discount larger than the whole positional-weight range, keeping the three
    objectives strictly ordered.  Order fulfillment is additionally pinned to
    its layer-1 optimum through fixed-capacity source arcs.
    """
    n_nodes = sink + 1

    def pair_upper(p):
        oid, lid = p
        return min(free_cap[lid], remaining[oid])

    p_count = len(pairs)
    base = sum(remaining.values()) + 1
    positional = [-base ** (p_count - 1 - k) for k in range(p_count)]
    position_of = {p: k for k, p in enumerate(pairs)}
    movement_scale = (-positional[0]) * base if positional else 1

    arcs = [(source, order_idx[o['id']], order_reserved[o['id']],
             order_reserved[o['id']], 0, None) for o in ordered_orders]
    arcs += [(lot_idx[l['id']], sink, 0, free_cap[l['id']], 0, None)
             for l in ordered_lots]
    for p in pairs:
        oid, lid = p
        upper = pair_upper(p)
        r = min(old_residual.get(p, 0), upper)
        onode, lnode = order_idx[oid], lot_idx[lid]
        weight = positional[position_of[p]]
        if r > 0:
            # A preserved old residual unit costs (positional - movement
            # discount); the discount makes it strictly cheaper than routing
            # the same unit through a non-residual arc.
            arcs.append((onode, lnode, 0, r,
                         weight - movement_scale, ('d', p)))
        if upper - r > 0:
            arcs.append((onode, lnode, 0, upper - r, weight, ('e', p)))
    _, named_flow = bounded_min_cost_flow(n_nodes, arcs, source, sink,
                                          total_flow)
    result = {p: 0 for p in pairs}
    for (kind, p), f in named_flow.items():
        result[p] += f

    if sum(result.values()) != total_flow:
        raise ValueError('internal: layer-3 changed total fulfillment')
    live_movement = sum(max(old_residual.get(p, 0) - result.get(p, 0), 0)
                        for p in pairs)
    if live_movement != movement:
        raise ValueError('internal: layer-3 changed movement objective')
    return result
