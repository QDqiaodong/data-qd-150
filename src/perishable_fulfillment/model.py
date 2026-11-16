from datetime import date
from .common import require,ident,number,rows
from .fulfill import allocate

def day(v):
    if not isinstance(v,str):raise ValueError('expected ISO date')
    try:return date.fromisoformat(v)
    except ValueError:raise ValueError('invalid ISO date')

def analyze(data):
    require(data,'as_of lots orders');as_of=day(data['as_of']);lots=[];seen=set()
    for l in rows(data['lots'],20000):
        require(l,'id sku expires quantity held');lid=ident(l['id']);sku=ident(l['sku']);expires=day(l['expires']);qty=number(l['quantity'],0,10**9);held=number(l['held'],0,qty)
        if lid in seen:raise ValueError('duplicate lot')
        seen.add(lid);lots.append({'id':lid,'sku':sku,'expires':expires.isoformat(),'quantity':qty,'held':held})
    orders=[];seen=set()
    for o in rows(data['orders'],20000):
        require(o,'id sku priority quantity');oid=ident(o['id']);sku=ident(o['sku']);priority=number(o['priority'],0,10**6);qty=number(o['quantity'],0,10**9)
        if oid in seen:raise ValueError('duplicate order')
        seen.add(oid);orders.append({'id':oid,'sku':sku,'priority':priority,'quantity':qty})
    return allocate(as_of.isoformat(),lots,orders)
