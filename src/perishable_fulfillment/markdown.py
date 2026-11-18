"""Offline retail markdown schedule analysis (schema_version=4).

The store already used the unchanged v1 allocator (``model.analyze``) to
assign today's fulfillment orders.  This mode never redoes that allocation:
the per-lot ``available_remaining`` of the v1 result is taken as the starting
stock for a one-shot, read-only analysis of future retail prices.  Held
units, units already assigned to orders and units v1 judged expired are not
for sale.  Nothing here writes stock or prices, reads the v2 ledger or uses
the v3 road network.

Each planned SKU picks exactly one public price option per future day, or
the implicit no-sale action (id ``""``).  A day's sales are
``min(option demand, currently unexpired available units)`` and consume lots
in FEFO order, ``(expires, lot id)`` ascending, in bulk -- the 10**9 stock is
never expanded unit by unit.  Lot-level gross profit uses the actual sold
lots' own unit costs.

The schedule is globally exact over all SKUs and days under the daily
discount-slot cap.  Objectives, in strict priority order:
  1. maximize total horizon gross profit (integer pence),
  2. minimize the number of selected discounted SKU-day cells,
  3. lexicographically minimize the action-id sequence interleaved by
     (date, SKU); the empty no-sale id sorts before any public id.

Algorithm: for each SKU a memoized day-by-day DFS enumerates its compact
action schedules (inventory is a tuple of at most four bulk lot quantities),
keeping, per per-day discount vector, the schedule with the best profit and
the smallest lexicographic block rank.  Resource-Pareto pruning then drops
any single-SKU schedule dominated on (discount slots, profit, rank) by
another schedule of the same SKU, which is exact because such a substitution
can never break a slot-feasible combination or worsen any objective.  A
cross-SKU dynamic program combines the surviving schedules while tracking
each day's used discount slots as a base-7 integer and the interleaved
action ranks as one big integer, so the slot cap is checked per day rather
than as a horizon total.  Only the standard library is used and every
quantity stays a Python integer; stock is never expanded unit by unit.
"""

from __future__ import annotations

import os
from datetime import timedelta
from pathlib import Path

from .common import ident, require, rows, read, save
from .model import analyze, day as parse_iso_day

SCHEMA_VERSION = 4
MAX_SKUS = 6
MAX_DAYS = 8
MAX_LOTS_PER_SKU = 4
MAX_OPTIONS = 4
MAX_SLOTS = 6
MAX_VALUE = 10 ** 9
EMPTY_ACTION = ''


# --------------------------------------------------------------------------
# Strict scalar validation
# --------------------------------------------------------------------------

def _integer(value, minimum, maximum, label):
    # type(...) is int deliberately: bool is an int subclass and is rejected,
    # floats and numeric strings are not ints either.
    if type(value) is not int or not minimum <= value <= maximum:
        raise ValueError('%s must be an integer in [%d, %d]'
                         % (label, minimum, maximum))
    return value


def _canonical_day(value, label):
    """Strict YYYY-MM-DD calendar date; re-serialization must round-trip."""
    if not isinstance(value, str):
        raise ValueError('%s must be an ISO date string' % label)
    parsed = parse_iso_day(value)
    if parsed.isoformat() != value:
        raise ValueError('%s must use canonical YYYY-MM-DD format' % label)
    return parsed


def _boolean(value, label):
    # isinstance(True, int) is True, so an explicit type check is required.
    if type(value) is not bool:
        raise ValueError('%s must be a JSON boolean' % label)
    return value


# --------------------------------------------------------------------------
# Input contract
# --------------------------------------------------------------------------

def parse_input(data):
    """Validate the whole v4 source and return a normalized view."""
    require(data, 'schema_version fulfillment dates lot_costs offers '
                  'promo_slots')
    if type(data['schema_version']) is not int or \
            data['schema_version'] != SCHEMA_VERSION:
        raise ValueError('schema_version must be the integer 4')

    fulfillment = data['fulfillment']
    require(fulfillment, 'as_of lots orders')
    # model.analyze remains the single authority for the v1 object contract.
    v1_result = analyze(fulfillment)

    as_of = _canonical_day(fulfillment['as_of'], 'as_of')
    # A future horizon needs a real next calendar day; date.max has none.
    try:
        first_day = as_of + timedelta(days=1)
    except OverflowError:
        raise ValueError('as_of has no next calendar day')

    raw_lots = fulfillment['lots']
    raw_orders = fulfillment['orders']

    # Planned SKUs: union of lot SKUs and order SKUs.
    sku_set = {lot['sku'] for lot in raw_lots}
    sku_set.update(order['sku'] for order in raw_orders)
    if not 1 <= len(sku_set) <= MAX_SKUS:
        raise ValueError('planned SKU count must be in 1..%d' % MAX_SKUS)
    skus = sorted(sku_set)

    lots_by_sku = {sku: [] for sku in skus}
    for lot in raw_lots:
        if lot['sku'] not in lots_by_sku:  # defensive: sku set is derived
            raise ValueError('lot references an unknown planned SKU')
        lots_by_sku[lot['sku']].append(lot)
    for sku, members in lots_by_sku.items():
        if len(members) > MAX_LOTS_PER_SKU:
            raise ValueError('SKU %s has more than %d source lots'
                             % (sku, MAX_LOTS_PER_SKU))

    # Dates: already ascending, unique, consecutive calendar days, starting
    # exactly on the day after as_of.
    date_values = rows(data['dates'], MAX_DAYS)
    if not date_values:
        raise ValueError('dates must contain 1..%d entries' % MAX_DAYS)
    days = []
    previous = None
    for index, value in enumerate(date_values):
        current = _canonical_day(value, 'dates entry')
        if previous is not None:
            if current <= previous:
                raise ValueError('dates must be strictly ascending and unique')
            if current != previous + timedelta(days=1):
                raise ValueError('dates must be consecutive calendar days')
        previous = current
        days.append(current)
    if days[0] != first_day:
        raise ValueError('first planning date must be the calendar day '
                         'after as_of')

    # lot_costs: every source lot mapped exactly once to an integer cost.
    if not isinstance(data['lot_costs'], list) or \
            len(data['lot_costs']) > len(raw_lots):
        raise ValueError('lot_costs must contain exactly one entry per '
                         'source lot')
    cost_rows = data['lot_costs']
    unit_cost = {}
    for row in cost_rows:
        require(row, 'lot unit_cost')
        lot_id = ident(row['lot'])
        if lot_id in unit_cost:
            raise ValueError('duplicate lot_costs entry for lot: ' + lot_id)
        unit_cost[lot_id] = _integer(row['unit_cost'], 0, MAX_VALUE,
                                     'unit_cost')
    source_lot_ids = {lot['id'] for lot in raw_lots}
    if set(unit_cost) != source_lot_ids:
        missing = sorted(source_lot_ids - set(unit_cost))
        extra = sorted(set(unit_cost) - source_lot_ids)
        detail = []
        if missing:
            detail.append('missing ' + ', '.join(missing))
        if extra:
            detail.append('unknown ' + ', '.join(extra))
        raise ValueError('lot_costs must cover every source lot exactly once'
                         + (': ' + '; '.join(detail) if detail else ''))

    # offers: exactly one cell per (date, planned SKU).
    offer_rows = rows(data['offers'], MAX_DAYS * MAX_SKUS)
    cell_count = len(days) * len(skus)
    if len(offer_rows) != cell_count:
        raise ValueError('offers must contain exactly one entry per '
                         'date x planned SKU cell')
    day_index = {day.isoformat(): i for i, day in enumerate(days)}
    sku_index = {sku: i for i, sku in enumerate(skus)}
    # options_by_day_sku[d][s] = list of validated option dicts.
    options_grid = [[None for _ in skus] for _ in days]
    for row in offer_rows:
        require(row, 'date sku options')
        date_text = row['date']
        if not isinstance(date_text, str) or date_text not in day_index:
            raise ValueError('offer references an unknown planning date')
        sku = ident(row['sku'])
        if sku not in sku_index:
            raise ValueError('offer references an unknown planned SKU: ' + sku)
        di, si = day_index[date_text], sku_index[sku]
        if options_grid[di][si] is not None:
            raise ValueError('duplicate offer cell for %s / %s'
                             % (date_text, sku))
        option_rows = rows(row['options'], MAX_OPTIONS)
        options = []
        seen_ids = set()
        for option in option_rows:
            require(option, 'id price demand discounted')
            raw_id = option['id']
            if isinstance(raw_id, str) and raw_id == EMPTY_ACTION:
                raise ValueError('option id "" is reserved for the no-sale '
                                 'action')
            option_id = ident(raw_id)  # non-string/blank ids rejected here
            if option_id in seen_ids:
                raise ValueError('duplicate option id within a cell: '
                                 + option_id)
            seen_ids.add(option_id)
            price = _integer(option['price'], 0, MAX_VALUE, 'option price')
            demand = _integer(option['demand'], 0, MAX_VALUE,
                              'option demand')
            discounted = _boolean(option['discounted'], 'option discounted')
            options.append({'id': option_id, 'price': price,
                            'demand': demand, 'discounted': discounted})
        options_grid[di][si] = options

    # promo_slots: exactly one row per planning date.
    slot_rows = rows(data['promo_slots'], len(days))
    if len(slot_rows) != len(days):
        raise ValueError('promo_slots must contain exactly one entry per '
                         'planning date')
    slots_by_day = {}
    for row in slot_rows:
        require(row, 'date slots')
        date_text = row['date']
        if not isinstance(date_text, str) or date_text not in day_index:
            raise ValueError('promo_slots references an unknown planning date')
        if date_text in slots_by_day:
            raise ValueError('duplicate promo_slots entry for ' + date_text)
        slots_by_day[date_text] = _integer(row['slots'], 0, MAX_SLOTS,
                                           'promo slots')
    slots = [slots_by_day[day.isoformat()] for day in days]

    return {
        'as_of': as_of,
        'days': days,
        'skus': skus,
        'raw_lots': raw_lots,
        'unit_cost': unit_cost,
        'options_grid': options_grid,
        'slots': slots,
        'v1_result': v1_result,
    }


# --------------------------------------------------------------------------
# Starting stock from the unchanged v1 result
# --------------------------------------------------------------------------

def build_lots(parsed):
    """Per-SKU FEFO-ordered lot records seeded from v1 available_remaining.

    Each record keeps the source lot id, expiry and unit cost.  Units already
    expired before the first planning day (including lots expiring on as_of)
    are booked as expired_before_horizon and never enter an opening balance.
    """
    first_day = parsed['days'][0]
    remaining_by_lot = {row['lot']: row['available_remaining']
                        for row in parsed['v1_result']['lots']}
    lots_by_sku = {sku: [] for sku in parsed['skus']}
    all_lots = []
    for raw in parsed['raw_lots']:
        expires = _canonical_day(raw['expires'], 'lot expires')
        initial = remaining_by_lot[raw['id']]
        if type(initial) is not int or initial < 0:
            raise ValueError('internal: v1 available_remaining is invalid')
        expired_before = initial if expires < first_day else 0
        record = {
            'lot': raw['id'],
            'sku': raw['sku'],
            'expires': expires,
            'unit_cost': parsed['unit_cost'][raw['id']],
            'initial_available': initial,
            'expired_before_horizon': expired_before,
            'opening': initial - expired_before,
        }
        lots_by_sku[raw['sku']].append(record)
        all_lots.append(record)
    for sku in lots_by_sku:
        # FEFO consumption order: (expires, lot id) ascending.
        lots_by_sku[sku].sort(key=lambda lot: (lot['expires'], lot['lot']))
    all_lots.sort(key=lambda lot: lot['lot'])
    return lots_by_sku, all_lots


# --------------------------------------------------------------------------
# One-SKU compact schedule enumeration
# --------------------------------------------------------------------------

def _simulate_step(lot_records, remaining, current_day, option):
    """Bulk FEFO sale then same-day expiry for one SKU on one day."""
    lot_sales = {}
    if option is None:
        # Implicit no-sale: nothing is sold, profit is zero.
        sold_total = 0
        profit = 0
    else:
        saleable = sum(qty for lot, qty in zip(lot_records, remaining)
                       if qty > 0 and lot['expires'] >= current_day)
        sold_total = min(option['demand'], saleable)
        profit = 0
        left = sold_total
        next_remaining = list(remaining)
        for index, lot in enumerate(lot_records):
            if left == 0:
                break
            qty = next_remaining[index]
            if qty <= 0 or lot['expires'] < current_day:
                continue
            take = min(left, qty)
            next_remaining[index] = qty - take
            left -= take
            lot_sales[lot['lot']] = take
            profit += take * (option['price'] - lot['unit_cost'])
        if left != 0:
            raise ValueError('internal: FEFO sale could not satisfy demand')
        remaining = tuple(next_remaining)

    expired_amounts = {}
    next_remaining = list(remaining)
    for index, lot in enumerate(lot_records):
        if lot['expires'] == current_day and next_remaining[index] > 0:
            expired_amounts[lot['lot']] = next_remaining[index]
            next_remaining[index] = 0
    return sold_total, profit, lot_sales, expired_amounts, \
        tuple(next_remaining)


def enumerate_sku_schedules(lot_records, options_by_day, days, rank_lookup,
                            radix):
    """Enumerate this SKU's schedules, dominated per discount vector.

    Returns a dict keyed by the per-day discount tuple (1 when the selected
    action occupies a discount slot, else 0).  Each value is a compact
    ``(profit, actions, rank)`` tuple where ``actions`` is the chosen action
    id per day (``""`` for no-sale) and ``rank`` is this SKU's block rank.

    The block rank uses radix ``radix`` (= 5**sku_count) with one action rank
    digit (0..4, since a cell has at most four public options plus no-sale)
    per planning day, most significant day first.  Digits never carry because
    4 < radix, and multiplying a block rank by 5**(sku_count-1-sku_position)
    places its digits exactly on that SKU's (day, SKU) positions of the global
    lexicographic rank, so block ranks of different SKUs are directly
    comparable as disjoint parts of one integer.  For a fixed discount vector
    only the maximum-profit, then smallest lexicographic, schedule survives.
    """
    start = tuple(lot['opening'] for lot in lot_records)
    horizon = len(days)
    # Memo key: (day index, remaining tuple).  Value: best terminal schedules
    # reachable from this state, keyed by suffix discount vector.  Every
    # horizon-ending inventory maps to the same empty suffix.
    memo = {}
    terminal = {(): (0, (), 0)}

    def visit(di, remaining):
        if di == horizon:
            return terminal
        key = (di, remaining)
        # Membership test, not get(): a cached dict may legitimately be empty,
        # and dict.get would conflate that with a missing cache entry.
        if key in memo:
            return memo[key]

        current_day = days[di]
        choices = [None] + list(options_by_day[di])
        merged = {}
        for option in choices:
            _sold, profit, _sales, _expired, next_remaining = \
                _simulate_step(lot_records, remaining, current_day, option)
            discount_bit = 1 if (option is not None and
                                 option['discounted']) else 0
            action_id = EMPTY_ACTION if option is None else option['id']
            cell_rank = rank_lookup[di][action_id]
            for suffix_vector, (suffix_profit, suffix_actions,
                                suffix_rank) in \
                    visit(di + 1, next_remaining).items():
                vector = (discount_bit,) + suffix_vector
                actions = (action_id,) + suffix_actions
                candidate_profit = profit + suffix_profit
                # The suffix rank holds the lower day positions; prepending
                # one day's digit shifts it up by one block-radix place.
                candidate_rank = suffix_rank + \
                    cell_rank * (radix ** (horizon - di - 1))
                incumbent = merged.get(vector)
                # Records are (profit, actions, rank).  For an equal discount
                # vector keep the larger profit, then the smaller block rank;
                # the action tuple is a defensive final tie-break because
                # equal ranks already imply equal action ids.
                better = incumbent is None
                if not better and candidate_profit == incumbent[0]:
                    better = candidate_rank < incumbent[2] or \
                        (candidate_rank == incumbent[2] and
                         actions < incumbent[1])
                elif not better and candidate_profit > incumbent[0]:
                    better = True
                if better:
                    merged[vector] = (candidate_profit, actions,
                                      candidate_rank)
        memo[key] = merged
        return merged

    return visit(0, start)


# --------------------------------------------------------------------------
# Cross-SKU exact dynamic program under per-day slot caps
# --------------------------------------------------------------------------

def _mask_digits(code, horizon):
    digits = []
    for _ in range(horizon):
        digits.append(code % 7)
        code //= 7
    return digits


def _prune_alternatives(alternatives, horizon):
    """Resource-Pareto pruning of one SKU's schedules.

    An alternative i is dropped when another alternative j of the same SKU
    uses no more discount slots on any day (binary mask componentwise no
    larger), earns no less profit and has a no larger lexicographic block
    rank, with at least one strict improvement.  Substituting j for i can
    never break a slot-feasible combination and never worsens any of the
    three objectives, regardless of the other SKUs, so the pruning is exact.
    """
    masks = [_mask_digits(code, horizon) for code, _record in alternatives]
    kept = []
    for i in range(len(alternatives)):
        dominated = False
        for j in range(len(alternatives)):
            if i == j:
                continue
            if not all(masks[j][d] <= masks[i][d] for d in range(horizon)):
                continue
            profit_i, _actions_i, rank_i = alternatives[i][1]
            profit_j, _actions_j, rank_j = alternatives[j][1]
            if profit_j < profit_i or rank_j > rank_i:
                continue
            if any(masks[j][d] < masks[i][d] for d in range(horizon)) or \
                    profit_j > profit_i or rank_j < rank_i:
                dominated = True
                break
        if not dominated:
            kept.append(alternatives[i])
    return kept


def optimize(parsed, lots_by_sku):
    """Combine SKU schedules honoring each day's discount-slot cap exactly.

    Two integer encodings keep the exact search compact:

    * the used-slot vector is one base-7 integer (one digit per day, 0..6),
      so adding a SKU's binary discount vector and checking each day's cap
      are integer operations rather than tuple allocations;
    * the level-3 key is one big integer whose base-5 block digits, read most
      significant first, are the interleaved (date, SKU) action ranks, so
      integer order equals the required action-id lexicographic order.

    Each SKU first contributes only its resource-Pareto schedules (see
    ``_prune_alternatives``).  The cross-SKU dynamic program then keeps, for
    every active slot code, the Pareto frontier over (rank, profit): a state
    is dropped only when another state with the same slot usage has both no
    less profit and no larger rank.  Back-pointers reconstruct the winning
    per-SKU records in linear time.  Nothing expands stock per unit.
    """
    skus = parsed['skus']
    days = parsed['days']
    horizon = len(days)
    sku_count = len(skus)
    slots = parsed['slots']
    options_grid = parsed['options_grid']
    rank_lookup = _action_rank_lookup(options_grid, horizon, sku_count)
    block_radix = 5 ** sku_count
    seven_powers = [7 ** d for d in range(horizon)]

    # Per SKU: the best (max profit, then min block rank) schedule for each
    # distinct binary discount vector, then resource-Pareto pruning.  Stable
    # ordering by discount code gives every surviving alternative a fixed
    # index used by the reconstruction back-pointers.
    per_sku = []
    for si, sku in enumerate(skus):
        options_by_day = [options_grid[di][si] for di in range(horizon)]
        sku_rank_lookup = [column[si] for column in rank_lookup]
        schedules = enumerate_sku_schedules(
            lots_by_sku[sku], options_by_day, days, sku_rank_lookup,
            block_radix)
        alternatives = []
        for vector, record in schedules.items():
            code = sum(vector[d] * seven_powers[d] for d in range(horizon))
            alternatives.append((code, record))
        alternatives.sort(key=lambda item: item[0])
        per_sku.append(_prune_alternatives(alternatives, horizon))

    def within_caps(code):
        probe = code
        for d in range(horizon):
            if probe % 7 > slots[d]:
                return False
            probe //= 7
        return True

    # Each layer maps an active slot code to a Pareto frontier represented by
    # three parallel lists: ranks (ascending), profits (strictly increasing)
    # and back-pointers (parent slot code, parent point index, alternative
    # index).  Rank/profit pairs are therefore genuine Pareto points.
    layer_ranks = {0: [0]}
    layer_profits = {0: [0]}
    layer_back = {0: [None]}
    history = [(layer_ranks, layer_profits, layer_back)]

    for si in range(sku_count):
        place_power = 5 ** (sku_count - 1 - si)
        buckets = {}
        for parent_code, ranks in layer_ranks.items():
            profits = layer_profits[parent_code]
            for point_index in range(len(ranks)):
                state_rank = ranks[point_index]
                state_profit = profits[point_index]
                for alt_index, (discount_code, record) in \
                        enumerate(per_sku[si]):
                    new_code = parent_code + discount_code
                    if not within_caps(new_code):
                        continue
                    rank = state_rank + record[2] * place_power
                    profit = state_profit + record[0]
                    buckets.setdefault(new_code, []).append(
                        (rank, profit, parent_code, point_index, alt_index))

        new_ranks = {}
        new_profits = {}
        new_back = {}
        for new_code, bucket in buckets.items():
            # Rank-ascending sweep; on equal rank keep only the larger profit.
            # A point survives only on a new running profit high, which is
            # exactly Pareto dominance over (smaller rank, no-less profit).
            bucket.sort(key=lambda item: (item[0], -item[1]))
            kept_ranks = []
            kept_profits = []
            kept_back = []
            best_profit = None
            last_rank = None
            for rank, profit, parent_code, point_index, alt_index in bucket:
                if rank == last_rank:
                    continue
                last_rank = rank
                if best_profit is not None and profit <= best_profit:
                    continue
                best_profit = profit
                kept_ranks.append(rank)
                kept_profits.append(profit)
                kept_back.append((parent_code, point_index, alt_index))
            new_ranks[new_code] = kept_ranks
            new_profits[new_code] = kept_profits
            new_back[new_code] = kept_back
        if not new_ranks:
            raise ValueError('internal: no slot-feasible schedule combination')
        layer_ranks = new_ranks
        layer_profits = new_profits
        layer_back = new_back
        history.append((new_ranks, new_profits, new_back))

    # Level 1: max profit.  Level 2: min total discounted SKU-day actions
    # (sum of the base-7 digits).  Level 3: min interleaved action rank.
    best_key = None
    best_code = None
    best_index = None
    for code, ranks in layer_ranks.items():
        profits = layer_profits[code]
        discount_days = 0
        probe = code
        for _ in range(horizon):
            discount_days += probe % 7
            probe //= 7
        for point_index in range(len(ranks)):
            key = (profits[point_index], -discount_days, -ranks[point_index])
            if best_key is None or key > best_key:
                best_key = key
                best_code = code
                best_index = point_index

    # Follow back-pointers; each names the parent point and the alternative
    # chosen for this SKU, so reconstruction is one walk with no search.
    chosen_records = [None] * sku_count
    current_code = best_code
    current_index = best_index
    for si in range(sku_count - 1, -1, -1):
        _ranks, _profits, back = history[si + 1]
        parent_code, parent_index, alt_index = back[current_code][current_index]
        record = per_sku[si][alt_index][1]
        chosen_records[si] = record
        current_code = parent_code
        current_index = parent_index
    if current_code != 0 or current_index != 0:
        raise ValueError('internal: back-pointer reconstruction mismatch')

    selected = {skus[si]: {
        'profit': chosen_records[si][0],
        'actions': chosen_records[si][1],
    } for si in range(sku_count)}
    used = tuple((best_code // seven_powers[d]) % 7 for d in range(horizon))

    # Rebuild the nested (date -> SKU-ordered action ids) tuple used by the
    # report replay and the objective action-id array.
    nested = tuple(
        tuple(chosen_records[si][1][d] for si in range(sku_count))
        for d in range(horizon))
    return selected, used, nested


def _action_rank_lookup(options_grid, horizon, sku_count):
    """Per-cell mapping action id -> 1..k rank, 0 reserved for no-sale.

    Options are sorted by their id with ordinary Python string ordering, which
    is exactly the ordering used by the level-3 lexicographic objective.
    """
    lookup = [[None] * sku_count for _ in range(horizon)]
    for di in range(horizon):
        for si in range(sku_count):
            ordered = sorted(option['id'] for option in options_grid[di][si])
            mapping = {EMPTY_ACTION: 0}
            for rank, option_id in enumerate(ordered, start=1):
                mapping[option_id] = rank
            lookup[di][si] = mapping
    return lookup


# --------------------------------------------------------------------------
# Deterministic replay of the winning schedule into the full report
# --------------------------------------------------------------------------

def replay_report(parsed, lots_by_sku, all_lots, selected, used_discount,
                  nested):
    days = parsed['days']
    skus = parsed['skus']
    horizon = len(days)
    options_grid = parsed['options_grid']

    # Resolve each chosen action id back to its source option.
    chosen_option = {}
    for si, sku in enumerate(skus):
        actions = selected[sku]['actions']
        for di in range(horizon):
            action_id = actions[di]
            option = None
            if action_id != EMPTY_ACTION:
                for candidate in options_grid[di][si]:
                    if candidate['id'] == action_id:
                        option = candidate
                        break
                if option is None:
                    raise ValueError('internal: selected action id not found')
            chosen_option[(di, si)] = option

    # Mutable remaining bulk quantity per lot, seeded from opening stock.
    remaining = {lot['lot']: lot['opening'] for lot in all_lots}
    # Horizon-wide per-lot accumulators.
    lot_totals = {lot['lot']: {'sold': 0, 'expired': 0, 'revenue': 0,
                               'cost': 0, 'profit': 0} for lot in all_lots}

    report_days = []
    horizon_revenue = horizon_cost = horizon_profit = 0
    for di, current_day in enumerate(days):
        day_ledger = {lot['lot']: {
            'opening': remaining[lot['lot']], 'sold': 0, 'expired': 0,
            'closing': 0, 'revenue': 0, 'cost': 0, 'profit': 0}
            for lot in all_lots}
        actions = []
        day_revenue = day_cost = day_profit = 0
        promo_used = 0

        for si, sku in enumerate(skus):
            option = chosen_option[(di, si)]
            lot_records = lots_by_sku[sku]
            if option is None:
                action_row = {
                    'sku': sku, 'action_id': EMPTY_ACTION, 'price': 0,
                    'demand': 0, 'discounted': False, 'sold': 0,
                    'unmet': 0, 'revenue': 0, 'cost': 0, 'profit': 0,
                }
            else:
                saleable = sum(remaining[lot['lot']] for lot in lot_records
                               if remaining[lot['lot']] > 0
                               and lot['expires'] >= current_day)
                demand = option['demand']
                sold = min(demand, saleable)
                unmet = demand - sold
                revenue = cost = 0
                left = sold
                for lot in lot_records:
                    if left == 0:
                        break
                    qty = remaining[lot['lot']]
                    if qty <= 0 or lot['expires'] < current_day:
                        continue
                    take = min(left, qty)
                    remaining[lot['lot']] = qty - take
                    left -= take
                    lot_revenue = take * option['price']
                    lot_cost = take * lot['unit_cost']
                    ledger_row = day_ledger[lot['lot']]
                    ledger_row['sold'] += take
                    ledger_row['revenue'] += lot_revenue
                    ledger_row['cost'] += lot_cost
                    ledger_row['profit'] += lot_revenue - lot_cost
                    totals = lot_totals[lot['lot']]
                    totals['sold'] += take
                    totals['revenue'] += lot_revenue
                    totals['cost'] += lot_cost
                    totals['profit'] += lot_revenue - lot_cost
                    revenue += lot_revenue
                    cost += lot_cost
                if left != 0:
                    raise ValueError('internal: replay sale mismatch')
                action_row = {
                    'sku': sku, 'action_id': option['id'],
                    'price': option['price'], 'demand': demand,
                    'discounted': option['discounted'], 'sold': sold,
                    'unmet': unmet, 'revenue': revenue, 'cost': cost,
                    'profit': revenue - cost,
                }
                if option['discounted']:
                    promo_used += 1
            actions.append(action_row)
            day_revenue += action_row['revenue']
            day_cost += action_row['cost']
            day_profit += action_row['profit']

        # End of day: every still-open lot expiring today expires in full.
        for lot in all_lots:
            if lot['expires'] == current_day:
                qty = remaining[lot['lot']]
                if qty > 0:
                    remaining[lot['lot']] = 0
                    day_ledger[lot['lot']]['expired'] = qty
                    lot_totals[lot['lot']]['expired'] += qty

        for lot in all_lots:
            row = day_ledger[lot['lot']]
            row['closing'] = remaining[lot['lot']]
            if row['opening'] != row['sold'] + row['expired'] + \
                    row['closing']:
                raise ValueError('internal: lot ledger conservation broken')

        ledger_list = []
        for lot in all_lots:
            row = day_ledger[lot['lot']]
            ledger_list.append({
                'lot': lot['lot'], 'opening': row['opening'],
                'sold': row['sold'], 'expired': row['expired'],
                'closing': row['closing'], 'revenue': row['revenue'],
                'cost': row['cost'], 'profit': row['profit'],
            })
        if promo_used != used_discount[di]:
            raise ValueError('internal: promo usage mismatch')
        if promo_used > parsed['slots'][di]:
            raise ValueError('internal: daily promo slot cap violated')
        report_days.append({
            'date': current_day.isoformat(), 'promo_used': promo_used,
            'revenue': day_revenue, 'cost': day_cost, 'profit': day_profit,
            'actions': actions, 'lot_ledger': ledger_list,
        })
        horizon_revenue += day_revenue
        horizon_cost += day_cost
        horizon_profit += day_profit

    lot_summaries = []
    for lot in all_lots:
        totals = lot_totals[lot['lot']]
        ending = remaining[lot['lot']]
        record = {
            'lot': lot['lot'], 'sku': lot['sku'],
            'expires': lot['expires'].isoformat(),
            'unit_cost': lot['unit_cost'],
            'initial_available': lot['initial_available'],
            'expired_before_horizon': lot['expired_before_horizon'],
            'sold': totals['sold'], 'expired': totals['expired'],
            'ending': ending, 'revenue': totals['revenue'],
            'cost': totals['cost'], 'profit': totals['profit'],
        }
        if record['initial_available'] != \
                record['expired_before_horizon'] + record['sold'] + \
                record['expired'] + record['ending']:
            raise ValueError('internal: lot horizon conservation broken')
        lot_summaries.append(record)

    action_ids = [action_id for di in range(horizon)
                  for action_id in nested[di]]
    if horizon_profit != sum(selected[sku]['profit'] for sku in skus):
        raise ValueError('internal: replayed profit disagrees with optimum')
    if sum(lot_totals[lot['lot']]['profit'] for lot in all_lots) != \
            horizon_profit:
        raise ValueError('internal: lot profit totals disagree with horizon')

    return {
        'schema_version': SCHEMA_VERSION,
        'source_sha256': None,  # filled in by compile_report
        'fulfillment': parsed['v1_result'],
        'objective': {
            'profit': horizon_profit,
            'discount_sku_days': sum(used_discount),
            'action_ids': action_ids,
        },
        'days': report_days,
        'lots': lot_summaries,
    }


# --------------------------------------------------------------------------
# Public entry point
# --------------------------------------------------------------------------

def compile_report(data, source_sha256):
    """Validate, optimize and construct the full v4 report."""
    parsed = parse_input(data)
    lots_by_sku, all_lots = build_lots(parsed)
    selected, used_discount, nested = optimize(parsed, lots_by_sku)
    report = replay_report(parsed, lots_by_sku, all_lots, selected,
                           used_discount, nested)
    report['source_sha256'] = source_sha256
    # Cross-check the day money columns against their action rows.
    for day_row in report['days']:
        revenue = sum(action['revenue'] for action in day_row['actions'])
        cost = sum(action['cost'] for action in day_row['actions'])
        profit = sum(action['profit'] for action in day_row['actions'])
        if (revenue, cost, profit) != (day_row['revenue'],
                                       day_row['cost'], day_row['profit']):
            raise ValueError('internal: day money totals disagree')
        # Per-SKU ledger aggregates must equal the action row.
        sku_of_lot = {lot['lot']: lot['sku'] for lot in all_lots}
        for sku in parsed['skus']:
            action = next(row for row in day_row['actions']
                          if row['sku'] == sku)
            rows_of_sku = [row for row in day_row['lot_ledger']
                           if sku_of_lot[row['lot']] == sku]
            sold = sum(row['sold'] for row in rows_of_sku)
            rev = sum(row['revenue'] for row in rows_of_sku)
            cst = sum(row['cost'] for row in rows_of_sku)
            prf = sum(row['profit'] for row in rows_of_sku)
            if (sold, rev, cst, prf) != (action['sold'], action['revenue'],
                                         action['cost'], action['profit']):
                raise ValueError('internal: SKU ledger totals disagree with '
                                 'the selected action')
    if report['objective']['profit'] < 0:
        raise ValueError('internal: all-no-sale must guarantee nonnegative '
                         'profit')
    return report


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------

DESCRIPTION = ('Offline retail markdown schedule analysis over fixed v1 '
               'fulfillment lots')


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

    parser = argparse.ArgumentParser(prog='perishable-markdown',
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
