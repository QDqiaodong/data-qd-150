from collections import defaultdict

def allocate(as_of,lots,orders):
    stock={l['id']:l for l in lots};available={l['id']:0 if l['expires']<as_of else l['quantity']-l['held'] for l in lots};assigned=defaultdict(list);result=[]
    for o in sorted(orders,key=lambda x:(x['priority'],x['id'])):
        remaining=o['quantity']
        candidates=sorted((l for l in lots if l['sku']==o['sku'] and l['expires']>=as_of),key=lambda l:(l['expires'],l['id']))
        for lot in candidates:
            take=min(remaining,available[lot['id']])
            if take:
                available[lot['id']]-=take;remaining-=take;assigned[o['id']].append({'lot':lot['id'],'quantity':take})
            if not remaining:break
        result.append({'order':o['id'],'priority':o['priority'],'requested':o['quantity'],'allocations':assigned[o['id']],'fulfilled':o['quantity']-remaining,'shortage':remaining})
    summaries=[]
    for l in sorted(lots,key=lambda x:x['id']):
        summaries.append({'lot':l['id'],'sku':l['sku'],'expired':l['expires']<as_of,'quantity':l['quantity'],'held':l['held'],'available_initial':0 if l['expires']<as_of else l['quantity']-l['held'],'available_remaining':available[l['id']]})
    return {'orders':result,'lots':summaries,'total_requested':sum(o['quantity'] for o in orders),'total_fulfilled':sum(o['quantity']-r['shortage'] for o,r in zip(sorted(orders,key=lambda x:(x['priority'],x['id'])),result)),'total_shortage':sum(r['shortage'] for r in result)}
