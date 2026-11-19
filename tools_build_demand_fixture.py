"""Build the schema_version=8 demand-panel acceptance fixture.

Run from the repository root with PYTHONPATH=src.  The generator lives
outside the fixtures directory: fixtures are data, this is a tool.
"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / 'src'))

from perishable_fulfillment.common import read
from perishable_fulfillment.coldchain import compile_report

ROOT = Path(__file__).resolve().parents[0]
r1_data, r1_digest = read(ROOT / 'fixtures' / 'coldchain_input.json')
r1 = compile_report(r1_data, r1_digest)

# Sanity pins for the lines the demand fixture relies on.
verdicts = {(v['package'], v['order'], v['lot']): v
            for v in r1['line_verdicts']}
assert verdicts[('P3', 'O1', 'L1')]['disposition'] == 'release'
assert verdicts[('P3', 'O1', 'L1')]['sku'] == 'milk'
assert verdicts[('P1', 'O1', 'L1')]['disposition'] == 'quarantine'
assert verdicts[('P1', 'O1', 'L1')]['sku'] == 'milk'


def obs(day, sold, covered=1440, promoted=False):
    return {'date': day, 'sku': 'milk', 'sold_units': sold,
            'covered_minutes': covered, 'promoted': promoted}


def witness(wid, day, ref, visible):
    return {
        'id': wid,
        'sku': 'milk',
        'start': '2026-11-%02dT00:00:00Z' % day,
        'end': '2026-11-%02dT00:00:00Z' % (day + 1),
        'report_id': ref[0],
        'package': ref[1],
        'order': ref[2],
        'lot': ref[3],
        'visible_at': visible,
    }


R1_P3 = ('R1', 'P3', 'O1', 'L1')
payload = {
    'schema_version': 8,
    'calendar_start': '2026-11-20',
    'cutoff_date': '2026-11-26',
    'split_date': '2026-11-22',
    'evidence_as_of': '2026-11-27T00:00:00Z',
    'lookback_days': 2,
    'horizon_days': 2,
    'censored_target_policy': 'mask_days',
    'skus': ['milk'],
    'observations': [
        obs('2026-11-20', 3),
        obs('2026-11-21', 3),
        obs('2026-11-22', 4),
        obs('2026-11-23', 0),
        obs('2026-11-24', 5),
        obs('2026-11-25', 5),
        obs('2026-11-26', 6),
    ],
    'v6_reports': [
        {'report_id': 'R1', 'report': r1},
    ],
    'shelf_witnesses': [
        # The only day-23 witness: full-day coverage, visible only at the 27th.
        witness('W23', 23, R1_P3, '2026-11-27T00:00:00Z'),
        witness('W24', 24, R1_P3, '2026-11-25T00:00:00Z'),
        witness('W25', 25, R1_P3, '2026-11-26T00:00:00Z'),
        witness('W26', 26, R1_P3, '2026-11-27T00:00:00Z'),
    ],
}

out = ROOT / 'fixtures' / 'demand_panel_input.json'
out.write_text(json.dumps(payload, sort_keys=True, ensure_ascii=False,
                          indent=2) + '\n')
print('wrote', out)
