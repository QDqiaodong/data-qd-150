"""Exact integer network-flow primitives for the v2 commitment planner.

The ledger never expands stock into per-unit nodes and never uses floats or
fixed magic weights: capacities stay as Python integers up to 10**9 and every
algorithm here works on a compact order -> lot bipartite network.

Costs are integers.  Lower-bound feasibility uses the standard
super-source/super-sink balance transformation with a sink->source return
edge; the return edge is given a balancing cost so the residual network never
contains a negative-cost cycle, which keeps successive shortest paths
terminating with plain integer SPFA.
"""

INF = 10**18


class Edge:
    __slots__ = ('frm', 'to', 'cap', 'cost', 'rev', 'lower', 'key')

    def __init__(self, frm, to, cap, cost, rev, lower=0, key=None):
        self.frm = frm
        self.to = to
        self.cap = cap          # remaining (residual) capacity
        self.cost = cost        # integer unit cost
        self.rev = rev          # reverse edge index on node `to`
        self.lower = lower      # preloaded mandatory flow (forward arcs only)
        self.key = key          # caller label for reading the final flow


class Graph:
    def __init__(self, n):
        self.g = [[] for _ in range(n)]

    def add(self, frm, to, cap, cost=0, lower=0, key=None):
        fwd = Edge(frm, to, cap, cost, len(self.g[to]), lower, key)
        rev = Edge(to, frm, 0, -cost, len(self.g[frm]))
        self.g[frm].append(fwd)
        self.g[to].append(rev)
        return fwd

    def flow_of(self, edge):
        """Net flow on a forward edge: preloaded lower bound plus residual use."""
        return edge.lower + self.g[edge.to][edge.rev].cap


def max_flow(graph, source, sink, limit=None):
    """Dinic max flow with integer bulk capacities; returns pushed units."""
    n = len(graph.g)
    pushed = 0
    while True:
        level = [-1] * n
        level[source] = 0
        queue = [source]
        head = 0
        while head < len(queue):
            v = queue[head]
            head += 1
            for e in graph.g[v]:
                if e.cap > 0 and level[e.to] == -1:
                    level[e.to] = level[v] + 1
                    queue.append(e.to)
        if level[sink] < 0:
            return pushed
        it = [0] * n

        def dfs(v, f):
            if v == sink:
                return f
            while it[v] < len(graph.g[v]):
                e = graph.g[v][it[v]]
                if e.cap > 0 and level[e.to] == level[v] + 1:
                    d = dfs(e.to, min(f, e.cap))
                    if d:
                        e.cap -= d
                        graph.g[e.to][e.rev].cap += d
                        return d
                it[v] += 1
            return 0

        while True:
            budget = INF if limit is None else limit - pushed
            if budget <= 0:
                return pushed
            f = dfs(source, budget)
            if not f:
                break
            pushed += f
    return pushed


def spfa(graph, source):
    """Shortest path distances and parents by integer cost (SPFA).

    Callers guarantee the residual network has no negative-cost cycle.
    """
    n = len(graph.g)
    dist = [INF] * n
    in_queue = [False] * n
    prev_v = [-1] * n
    prev_e = [-1] * n
    dist[source] = 0
    queue = [source]
    in_queue[source] = True
    head = 0
    while head < len(queue):
        v = queue[head]
        head += 1
        in_queue[v] = False
        dv = dist[v]
        for i, e in enumerate(graph.g[v]):
            if e.cap <= 0:
                continue
            nd = dv + e.cost
            if nd < dist[e.to]:
                dist[e.to] = nd
                prev_v[e.to] = v
                prev_e[e.to] = i
                if not in_queue[e.to]:
                    queue.append(e.to)
                    in_queue[e.to] = True
    return dist, prev_v, prev_e


def min_cost_flow(graph, source, sink, required):
    """Push exactly `required` bulk units at minimum integer cost.

    Successive shortest paths with SPFA.  The callers' network construction
    (a positive-cost sink->source return edge for bounded flows) guarantees the
    residual graph has no negative-cost cycle, so SPFA always terminates and
    handles the negative integer arc costs directly without potentials.
    """
    n = len(graph.g)
    remaining = required
    total_cost = 0
    while remaining > 0:
        dist, prev_v, prev_e = spfa(graph, source)
        if dist[sink] == INF:
            raise ValueError('required flow is infeasible')
        add = remaining
        v = sink
        while v != source:
            pv = prev_v[v]
            e = graph.g[pv][prev_e[v]]
            add = min(add, e.cap)
            v = pv
        v = sink
        while v != source:
            pv = prev_v[v]
            e = graph.g[pv][prev_e[v]]
            e.cap -= add
            graph.g[v][e.rev].cap += add
            total_cost += e.cost * add
            v = pv
        remaining -= add
    return total_cost




def bounded_min_cost_flow(n, arcs, source, sink, required_flow):
    """Minimum-cost feasible s-t flow with per-edge lower bounds.

    arcs: list of (frm, to, lower, upper, cost, key).
    Standard lower-bound transformation: preload each edge's mandatory lower
    flow, record node balances, then satisfy the balance with a super source
    and super sink.  The balancing circulation is routed with min-cost flow
    (not plain max flow), so the result is already minimum cost; the required
    s-t volume is then adjusted with successive shortest paths around a
    sink->source return edge.  All arithmetic is integer with no per-unit
    expansion.
    """
    graph = Graph(n + 2)
    super_source, super_sink = n, n + 1
    balance = [0] * n
    named = []
    for frm, to, lower, upper, cost, key in arcs:
        if lower > upper:
            raise ValueError('lower bound exceeds upper bound')
        edge = graph.add(frm, to, upper - lower, cost, lower, key)
        if key is not None:
            named.append(edge)
        balance[frm] -= lower
        balance[to] += lower
    need = 0
    for v, b in enumerate(balance):
        if b > 0:
            graph.add(super_source, v, b, 0)
            need += b
        elif b < 0:
            graph.add(v, super_sink, -b, 0)
    # The sink->source return edge must exist before the balance circulation:
    # satisfying a lower bound on a source-side arc needs a path back into the
    # source through this edge.  Its positive unit cost removes zero-cost
    # cycles and never enters the reported named-arc total.
    return_edge = graph.add(sink, source, INF, 1)
    # Route the mandatory balance as a minimum-cost circulation.  Super arcs
    # are zero cost, and negative-cost real arcs are reachable as ordinary
    # shortest paths, so successive shortest paths pick the cheapest feasible
    # routing directly.
    min_cost_flow(graph, super_source, super_sink, need)
    current = graph.flow_of(return_edge)
    if current < required_flow:
        return_edge.cap += required_flow - current
        min_cost_flow(graph, source, sink, required_flow - current)
    elif current > required_flow:
        min_cost_flow(graph, sink, source, current - required_flow)
    flows = {e.key: graph.flow_of(e) for e in named}
    cost = sum(graph.flow_of(e) * e.cost for e in named)
    return cost, flows
