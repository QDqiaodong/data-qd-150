"""Perishable commitment ledger (v2): adjudication, SQLite persistence, reports.

The ledger extends the single-day v1 allocator into a serial event journal:
lot stock is immutable after book-init, order reservations may change while
picked physical units are facts that can never be reallocated.  Every event
batch is validated, adjudicated and re-planned entirely before a single
serialized SQLite transaction commits facts, revision, reservations and the
immutable receipt together.
"""

import json
import sqlite3
from datetime import date, timedelta

from .common import require, ident, number, rows
from .planner import plan as planner_plan

MAX_LOTS = 64
MAX_ORDERS = 64
MAX_EVENTS = 128
MAX_MIN_LIFE = 3650
MAX_QUANTITY = 10**9
SCHEMA_VERSION = 2
# SQLite storage sentinel for "no admission restriction" (allowed_lots is
# None in the model).  An empty admission list is stored separately as '[]',
# so the two meanings never collapse.
ALLOW_ALL_SENTINEL = '*'


def encode_allowed(allowed):
    if allowed is None:
        return ALLOW_ALL_SENTINEL
    return json.dumps(list(allowed), separators=(',', ':'),
                      ensure_ascii=False)


def decode_allowed(stored):
    if stored == ALLOW_ALL_SENTINEL:
        return None
    return tuple(json.loads(stored))


class LedgerError(ValueError):
    """A rejected request; no ledger state changes when raised pre-commit."""


# --------------------------------------------------------------------------
# Strict parsing
# --------------------------------------------------------------------------

def day(value):
    if not isinstance(value, str):
        raise LedgerError('expected ISO date string')
    try:
        return date.fromisoformat(value)
    except ValueError:
        raise LedgerError('invalid ISO date: ' + value)


def add_days(day_value, days):
    """Date arithmetic that refuses integer overflow instead of wrapping."""
    try:
        return day_value + timedelta(days=days)
    except OverflowError:
        raise LedgerError('date arithmetic overflow')


def _allowed_lots(value, lot_ids_by_sku, sku):
    if value is None:
        return None
    rows(value, MAX_LOTS)
    result = []
    for lid in value:
        lid = ident(lid)
        if lid in result:
            raise LedgerError('duplicate lot id in allowed_lots')
        if lid not in lot_ids_by_sku.get(sku, set()):
            raise LedgerError('allowed_lots references unknown or wrong-SKU lot')
        result.append(lid)
    return tuple(result)


def parse_source(data):
    """Validate a v2 book-init source; returns normalized, immutable records."""
    require(data, 'as_of lots orders')
    as_of = day(data['as_of'])
    lots = []
    seen_lots = set()
    lot_skus = {}
    lot_ids_by_sku = {}
    for raw_lot in rows(data['lots'], MAX_LOTS):
        require(raw_lot, 'id sku expires quantity held')
        lid = ident(raw_lot['id'])
        sku = ident(raw_lot['sku'])
        expires = day(raw_lot['expires'])
        quantity = number(raw_lot['quantity'], 0, MAX_QUANTITY)
        held = number(raw_lot['held'], 0, quantity)
        if lid in seen_lots:
            raise LedgerError('duplicate lot id')
        seen_lots.add(lid)
        lot_skus[lid] = sku
        lot_ids_by_sku.setdefault(sku, set()).add(lid)
        lots.append({'id': lid, 'sku': sku, 'expires': expires.isoformat(),
                     'expires_d': expires, 'quantity': quantity, 'held': held})
    orders = []
    seen_orders = set()
    for raw_order in rows(data['orders'], MAX_ORDERS):
        require(raw_order, 'id sku priority quantity ship_date min_life allowed_lots')
        oid = ident(raw_order['id'])
        sku = ident(raw_order['sku'])
        priority = number(raw_order['priority'], 0, 10**6)
        quantity = number(raw_order['quantity'], 0, MAX_QUANTITY)
        ship = day(raw_order['ship_date'])
        if ship < as_of:
            raise LedgerError('ship_date must not be earlier than as_of')
        min_life = number(raw_order['min_life'], 0, MAX_MIN_LIFE)
        allowed = _allowed_lots(raw_order['allowed_lots'], lot_ids_by_sku, sku)
        deadline = add_days(ship, min_life)
        if oid in seen_orders:
            raise LedgerError('duplicate order id')
        seen_orders.add(oid)
        orders.append({'id': oid, 'sku': sku, 'priority': priority,
                       'quantity': quantity, 'ship_date': ship.isoformat(),
                       'min_life': min_life, 'allowed_lots': allowed,
                       'deadline': deadline.isoformat(), 'canceled': False,
                       'picked': 0})
    return as_of, lots, orders


# --------------------------------------------------------------------------
# SQLite schema and connection management
# --------------------------------------------------------------------------

SCHEMA_SQL = """
CREATE TABLE meta(key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE lots(
    id TEXT PRIMARY KEY, sku TEXT NOT NULL, expires TEXT NOT NULL,
    quantity INTEGER NOT NULL, held INTEGER NOT NULL, picked INTEGER NOT NULL
);
CREATE TABLE orders(
    id TEXT PRIMARY KEY, sku TEXT NOT NULL, priority INTEGER NOT NULL,
    quantity INTEGER NOT NULL, ship_date TEXT NOT NULL, min_life INTEGER NOT NULL,
    allowed_lots TEXT NOT NULL, deadline TEXT NOT NULL,
    picked INTEGER NOT NULL, canceled INTEGER NOT NULL
);CREATE TABLE picks(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    request_id TEXT NOT NULL, seq INTEGER NOT NULL,
    order_id TEXT NOT NULL, lot_id TEXT NOT NULL, quantity INTEGER NOT NULL
);
CREATE TABLE reservations(
    order_id TEXT NOT NULL, lot_id TEXT NOT NULL, quantity INTEGER NOT NULL,
    PRIMARY KEY(order_id, lot_id)
);
CREATE TABLE commits(
    request_id TEXT PRIMARY KEY,
    request_sha256 TEXT NOT NULL,
    revision INTEGER NOT NULL,
    report_json TEXT NOT NULL
);
"""


def connect(db_path):
    path = str(db_path)
    conn = sqlite3.connect(path)
    conn.execute('PRAGMA journal_mode=WAL')
    conn.execute('PRAGMA synchronous=FULL')
    conn.execute('PRAGMA foreign_keys=ON')
    return conn


def initialize(db_path, source_data, source_sha):
    """Create a brand-new ledger; refuses if any file already exists."""
    from pathlib import Path
    path = Path(db_path)
    if path.exists() or path.with_name(path.name + '-wal').exists() or \
            path.with_name(path.name + '-shm').exists():
        raise LedgerError('ledger already exists: ' + str(db_path))
    if path.is_symlink():
        raise LedgerError('database path cannot be a symlink')
    path.parent.mkdir(parents=True, exist_ok=True)
    as_of, lots, orders = parse_source(data=source_data)

    # In-memory adjudication state at revision 0 (no picks yet).
    state = _fresh_state(as_of, lots, orders)
    old_residual = {}
    allocations, movement = _replan(state, old_residual)

    conn = connect(path)
    try:
        conn.execute('BEGIN IMMEDIATE')
        conn.executescript(SCHEMA_SQL)
        conn.executemany(
            'INSERT INTO meta(key,value) VALUES(?,?)',
            [('revision', '0'), ('as_of', as_of.isoformat()),
             ('source_sha256', source_sha), ('last_request_id', '')])
        conn.executemany(
            'INSERT INTO lots(id,sku,expires,quantity,held,picked) '
            'VALUES(:id,:sku,:expires,:quantity,:held,0)', lots)
        for o in orders:
            _insert_order(conn, o)
        _write_reservations(conn, allocations)
        report = build_report(state, allocations, movement, revision=0,
                              request_id='', request_sha='', source_sha=source_sha)
        conn.execute(
            'INSERT INTO commits(request_id,request_sha256,revision,report_json)'
            " VALUES('','',0,?)", (json.dumps(report, sort_keys=True,
                                               ensure_ascii=False, allow_nan=False),))
        conn.commit()
    except Exception:
        conn.rollback()
        conn.close()
        path.unlink(missing_ok=True)
        for suffix in ('-wal', '-shm'):
            path.with_name(path.name + suffix).unlink(missing_ok=True)
        raise
    conn.close()
    return report


def _fresh_state(as_of, lots, orders):
    return {
        'as_of': as_of,
        'lots': [dict(l, picked=0) for l in lots],
        'orders': [dict(o, picked=0) for o in orders],
    }


def _insert_order(conn, o):
    conn.execute(
        'INSERT INTO orders(id,sku,priority,quantity,ship_date,min_life,'
        'allowed_lots,deadline,picked,canceled) VALUES(?,?,?,?,?,?,?,?,?,?)',
        (o['id'], o['sku'], o['priority'], o['quantity'], o['ship_date'],
         o['min_life'], encode_allowed(o['allowed_lots']), o['deadline'],
         o['picked'], 1 if o['canceled'] else 0))


def _write_reservations(conn, allocations):
    conn.execute('DELETE FROM reservations')
    rows_to_write = [(oid, lid, q) for oid, lots_here in allocations.items()
                     for lid, q in lots_here.items() if q]
    conn.executemany(
        'INSERT INTO reservations(order_id,lot_id,quantity) VALUES(?,?,?)',
        rows_to_write)


# --------------------------------------------------------------------------
# State loading
# --------------------------------------------------------------------------

def load_state(conn):
    meta = dict(conn.execute('SELECT key,value FROM meta').fetchall())
    as_of = day(meta['as_of'])
    lots = []
    for lid, sku, expires, quantity, held, picked in conn.execute(
            'SELECT id,sku,expires,quantity,held,picked FROM lots ORDER BY id'):
        lots.append({'id': lid, 'sku': sku, 'expires': expires,
                     'expires_d': day(expires), 'quantity': quantity,
                     'held': held, 'picked': picked})
    orders = []
    for row in conn.execute(
            'SELECT id,sku,priority,quantity,ship_date,min_life,allowed_lots,'
            'deadline,picked,canceled FROM orders ORDER BY id'):
        oid, sku, priority, quantity, ship, min_life, allowed_s, deadline, \
            picked, canceled = row
        allowed = decode_allowed(allowed_s)
        orders.append({'id': oid, 'sku': sku, 'priority': priority,
                       'quantity': quantity, 'ship_date': ship,
                       'min_life': min_life, 'allowed_lots': allowed,
                       'deadline': deadline, 'picked': picked,
                       'canceled': bool(canceled)})
    reservations = {(oid, lid): q for oid, lid, q in conn.execute(
        'SELECT order_id,lot_id,quantity FROM reservations')}
    return {'as_of': as_of, 'lots': lots, 'orders': orders,
            'revision': int(meta['revision']),
            'source_sha': meta['source_sha256'],
            'last_request_id': meta.get('last_request_id', '')}, reservations


# --------------------------------------------------------------------------
# Event batch adjudication
# --------------------------------------------------------------------------

def parse_apply_request(payload):
    require(payload, 'request_id expected_revision events')
    request_id = ident(payload['request_id'])
    expected = payload['expected_revision']
    if type(expected) is not int or expected < 0:
        raise LedgerError('expected_revision must be a non-negative integer')
    events = rows(payload['events'], MAX_EVENTS)
    if not events:
        raise LedgerError('events must contain at least one entry')
    for event in events:
        _check_event_shape(event)
    return request_id, expected, events


def _check_event_shape(event):
    if not isinstance(event, dict) or 'type' not in event:
        raise LedgerError('each event must be an object with a type')
    etype = event['type']
    shapes = {
        'set_hold': 'type lot held',
        'put_order': ('type id sku priority quantity ship_date min_life '
                      'allowed_lots'),
        'cancel_order': 'type order',
        'pick': 'type order lot quantity',
    }
    if etype not in shapes:
        raise LedgerError('unknown event type: ' + str(etype))
    require(event, shapes[etype])


def adjudicate(state, reservations_at_start, events):
    """Apply events in array order onto an in-memory candidate state.

    Raises LedgerError on the first violation; callers discard the candidate
    on any error, so no partial facts can survive.  Returns the candidate
    state, per-batch picks and the old-residual reservation matrix.
    """
    lots = {l['id']: l for l in state['lots']}
    orders = {o['id']: o for o in state['orders']}
    lot_ids_by_sku = {}
    for l in state['lots']:
        lot_ids_by_sku.setdefault(l['sku'], set()).add(l['id'])

    # Reservations committed when the batch began; picks consume them.
    committed = {key: q for key, q in reservations_at_start.items()}
    batch_picks = []          # (seq, order, lot, qty)
    new_order_ids = set()     # orders created inside this batch
    for seq, event in enumerate(events):
        etype = event['type']
        if etype == 'set_hold':
            lot = lots[ident(event['lot'])]
            held = number(event['held'], 0, lot['quantity'])
            if held + lot['picked'] > lot['quantity']:
                raise LedgerError('hold would cover already picked stock')
            lot['held'] = held
        elif etype == 'put_order':
            _apply_put_order(event, orders, lots, lot_ids_by_sku, state,
                             new_order_ids)
        elif etype == 'cancel_order':
            oid = ident(event['order'])
            if oid not in orders:
                raise LedgerError('cancel_order references unknown order')
            order = orders[oid]
            order['quantity'] = order['picked']
            order['canceled'] = True
        elif etype == 'pick':
            _apply_pick(event, orders, lots, committed, batch_picks,
                        new_order_ids, seq)
        # Invariant after every event.
        for lot in lots.values():
            if lot['held'] + lot['picked'] > lot['quantity']:
                raise LedgerError('lot physical balance violated')
        for order in orders.values():
            if order['picked'] > order['quantity']:
                raise LedgerError('order demand reduced below picked quantity')

    # `committed` has already been debited by every pick as it was authorized,
    # so its final values are precisely the previous reservations' residual
    # after this batch's picks (the old vector layer 2 tries to preserve).
    old_residual = {key: q for key, q in committed.items() if q != 0}
    return state, batch_picks, old_residual, new_order_ids


def _apply_put_order(event, orders, lots, lot_ids_by_sku, state, new_order_ids):
    oid = ident(event['id'])
    sku = ident(event['sku'])
    priority = number(event['priority'], 0, 10**6)
    quantity = number(event['quantity'], 0, MAX_QUANTITY)
    ship = day(event['ship_date'])
    if ship < state['as_of']:
        raise LedgerError('ship_date must not be earlier than as_of')
    min_life = number(event['min_life'], 0, MAX_MIN_LIFE)
    allowed = _allowed_lots(event['allowed_lots'], lot_ids_by_sku, sku)
    deadline = add_days(ship, min_life)
    if oid in orders:
        existing = orders[oid]
        if existing['sku'] != sku:
            raise LedgerError('existing order SKU is immutable')
        if existing['picked'] > quantity:
            raise LedgerError('order quantity cannot be reduced below picked')
        existing.update({'priority': priority, 'quantity': quantity,
                         'ship_date': ship.isoformat(), 'min_life': min_life,
                         'allowed_lots': allowed,
                         'deadline': deadline.isoformat(), 'canceled': False})
    else:
        order = {'id': oid, 'sku': sku, 'priority': priority,
                 'quantity': quantity, 'ship_date': ship.isoformat(),
                 'min_life': min_life, 'allowed_lots': allowed,
                 'deadline': deadline.isoformat(), 'picked': 0,
                 'canceled': False}
        orders[oid] = order
        state['orders'].append(order)
        new_order_ids.add(oid)


def _apply_pick(event, orders, lots, committed, batch_picks, new_order_ids, seq):
    oid = ident(event['order'])
    lid = ident(event['lot'])
    qty = number(event['quantity'], 1, MAX_QUANTITY)
    if oid not in orders:
        raise LedgerError('pick references unknown order')
    order = orders[oid]
    if lid not in lots:
        raise LedgerError('pick references unknown lot')
    lot = lots[lid]
    if oid in new_order_ids:
        raise LedgerError('new order cannot be picked inside its own batch')
    if order['canceled']:
        raise LedgerError('cannot pick for a canceled order')
    if order['picked'] + qty > order['quantity']:
        raise LedgerError('pick exceeds order demand')
    # Authorization: committed per-lot reservation at batch start, after
    # earlier picks in this batch, must still cover the requested quantity.
    available_authorization = committed.get((oid, lid), 0)
    if available_authorization < qty:
        raise LedgerError('pick not authorized by committed reservation')
    committed[(oid, lid)] = available_authorization - qty
    # Marketability at the moment the event fires.
    if lot['sku'] != order['sku']:
        raise LedgerError('pick lot SKU does not match order')
    if order['allowed_lots'] is not None and lid not in order['allowed_lots']:
        raise LedgerError('pick lot is no longer admitted by the order')
    if lot['expires_d'] < day(order['deadline']):
        raise LedgerError('pick lot no longer satisfies the shelf-life rule')
    # Unfrozen physical balance at the moment the event fires.
    if lot['held'] + lot['picked'] + qty > lot['quantity']:
        raise LedgerError('pick exceeds free physical lot balance')
    lot['picked'] += qty
    order['picked'] += qty
    batch_picks.append((seq, oid, lid, qty))


def _replan(state, old_residual):
    plan_orders = [
        {'id': o['id'], 'sku': o['sku'], 'priority': o['priority'],
         'quantity': o['quantity'], 'picked': o['picked'],
         'deadline': o['deadline'],
         'allowed_lots': o['allowed_lots'], 'canceled': o['canceled']}
        for o in state['orders']]
    plan_lots = [
        {'id': l['id'], 'sku': l['sku'], 'expires': l['expires'],
         'quantity': l['quantity'], 'held': l['held'], 'picked': l['picked']}
        for l in state['lots']]
    return planner_plan(plan_orders, plan_lots, old_residual)


# --------------------------------------------------------------------------
# Reporting
# --------------------------------------------------------------------------

def build_report(state, allocations, movement, revision, request_id,
                 request_sha, source_sha):
    order_rows = []
    for o in sorted(state['orders'], key=lambda x: (x['priority'], x['id'])):
        reserved_by_lot = allocations.get(o['id'], {})
        reserved_total = sum(reserved_by_lot.values())
        shortage = max(o['quantity'] - o['picked'] - reserved_total, 0)
        lot_lines = sorted(
            ({'lot': lid, 'quantity': q} for lid, q in reserved_by_lot.items()),
            key=lambda line: line['lot'])
        order_rows.append({
            'order': o['id'], 'sku': o['sku'], 'priority': o['priority'],
            'requested': o['quantity'], 'picked': o['picked'],
            'reserved': reserved_total, 'shortage': shortage,
            'canceled': o['canceled'],
            'reservations': lot_lines,
        })
    lot_rows = []
    reserved_on_lot = {}
    for oid, lot_map in allocations.items():
        for lid, q in lot_map.items():
            reserved_on_lot[lid] = reserved_on_lot.get(lid, 0) + q
    for l in sorted(state['lots'], key=lambda x: x['id']):
        reserved = reserved_on_lot.get(l['id'], 0)
        remaining = l['quantity'] - l['held'] - l['picked'] - reserved
        if remaining < 0:
            raise LedgerError('internal: lot conservation broken')
        lot_rows.append({
            'lot': l['id'], 'sku': l['sku'], 'expires': l['expires'],
            'quantity': l['quantity'], 'held': l['held'],
            'picked': l['picked'], 'reserved': reserved,
            'remaining': remaining,
        })
    return {
        'schema_version': SCHEMA_VERSION,
        'ledger_revision': revision,
        'request_id': request_id,
        'request_sha256': request_sha,
        'source_sha256': source_sha,
        'movement': movement,
        'orders': order_rows,
        'lots': lot_rows,
    }


# --------------------------------------------------------------------------
# Public apply / report entry points
# --------------------------------------------------------------------------

def apply_batch(db_path, raw_bytes):
    """Serialize one request: idempotent retry, revision check, commit once."""
    from pathlib import Path
    payload = _parse_raw(raw_bytes)
    request_id, expected_revision, events = parse_apply_request(payload)
    request_sha = _sha256(raw_bytes)
    conn = connect(Path(db_path))
    try:
        conn.execute('BEGIN IMMEDIATE')
        prior = conn.execute(
            'SELECT request_sha256,report_json FROM commits WHERE request_id=?',
            (request_id,)).fetchone()
        if prior is not None:
            prior_sha, prior_report = prior
            if prior_sha != request_sha:
                raise LedgerError('request_id reused with different payload bytes')
            # Idempotent replay wins over the expected_revision check.
            conn.rollback()
            conn.close()
            return json.loads(prior_report), True
        state, reservations = load_state(conn)
        if expected_revision != state['revision']:
            raise LedgerError(
                'expected_revision %d does not match current revision %d'
                % (expected_revision, state['revision']))
        candidate, batch_picks, old_residual, _ = adjudicate(
            state, reservations, events)
        allocations, movement = _replan(candidate, old_residual)
        _persist(conn, candidate, batch_picks, allocations, request_id)
        new_revision = state['revision'] + 1
        report = build_report(candidate, allocations, movement,
                              revision=new_revision, request_id=request_id,
                              request_sha=request_sha,
                              source_sha=state['source_sha'])
        conn.execute(
            'INSERT INTO commits(request_id,request_sha256,revision,report_json)'
            ' VALUES(?,?,?,?)',
            (request_id, request_sha, new_revision,
             json.dumps(report, sort_keys=True, ensure_ascii=False,
                        allow_nan=False)))
        conn.execute('UPDATE meta SET value=? WHERE key=?',
                     (str(new_revision), 'revision'))
        conn.execute('UPDATE meta SET value=? WHERE key=?',
                     (request_id, 'last_request_id'))
        conn.commit()
        conn.close()
        return report, False
    except Exception:
        conn.rollback()
        conn.close()
        raise


def _persist(conn, candidate, batch_picks, allocations, request_id):
    for l in candidate['lots']:
        conn.execute('UPDATE lots SET held=?,picked=? WHERE id=?',
                     (l['held'], l['picked'], l['id']))
    for o in candidate['orders']:
        conn.execute(
            'UPDATE orders SET sku=?,priority=?,quantity=?,ship_date=?,'
            'min_life=?,allowed_lots=?,deadline=?,picked=?,canceled=? '
            'WHERE id=?',
            (o['sku'], o['priority'], o['quantity'], o['ship_date'],
             o['min_life'], encode_allowed(o['allowed_lots']),
             o['deadline'], o['picked'],
             1 if o['canceled'] else 0, o['id']))
    for seq, oid, lid, qty in batch_picks:
        conn.execute(
            'INSERT INTO picks(request_id,seq,order_id,lot_id,quantity)'
            ' VALUES(?,?,?,?,?)',
            (request_id, seq, oid, lid, qty))
    _write_reservations(conn, allocations)


def _parse_raw(raw_bytes):
    from .common import parse_json
    return parse_json(raw_bytes)


def _sha256(raw_bytes):
    import hashlib
    return hashlib.sha256(raw_bytes).hexdigest()


def current_report(db_path):
    from pathlib import Path
    conn = connect(Path(db_path))
    try:
        row = conn.execute(
            'SELECT report_json FROM commits ORDER BY revision DESC, '
            'rowid DESC LIMIT 1'
        ).fetchone()
    finally:
        conn.close()
    if row is None:
        raise LedgerError('ledger has no committed report')
    return json.loads(row[0])
