
import copy,json,unittest,tempfile,subprocess,sys,hashlib,random
from pathlib import Path
from fractions import Fraction
from perishable_fulfillment.model import analyze
ROOT=Path(__file__).resolve().parents[1]
def sample():return json.loads((ROOT/'fixtures/input.json').read_text())
class Business(unittest.TestCase):
    def bad(self,d):
        with self.assertRaises((ValueError,TypeError)):analyze(d)
    def test_unknown_field(self):
        d=sample();d['extra']=1;self.bad(d)

    def test_fefo_and_shortage(self):
        r=analyze(sample());a,b=r['orders'];self.assertEqual(a['allocations'],[{'lot':'L1','quantity':6},{'lot':'L2','quantity':3}]);self.assertEqual((b['fulfilled'],b['shortage']),(7,1));self.assertEqual(r['total_fulfilled'],16)
    def test_expiry_inclusive(self):
        d=sample();d['orders']=d['orders'][:1];r=analyze(d);self.assertEqual(r['orders'][0]['allocations'][0]['lot'],'L1')
    def test_holds(self):
        d=sample();d['lots'][0]['held']=8;r=analyze(d);self.assertEqual(r['orders'][0]['allocations'][0]['lot'],'L2')
    def test_priority_before_order_id(self):
        d=sample();d['orders']=[{'id':'z','sku':'milk','priority':0,'quantity':10},{'id':'a','sku':'milk','priority':1,'quantity':10}];r=analyze(d);self.assertEqual(r['orders'][0]['order'],'z');self.assertEqual(r['orders'][0]['fulfilled'],10)
    def test_other_sku(self):
        d=sample();d['orders']=[{'id':'x','sku':'other','priority':0,'quantity':1}];self.assertEqual(analyze(d)['orders'][0]['shortage'],1)
    def test_hold_exceeds_stock(self):
        d=sample();d['lots'][0]['held']=9;self.bad(d)
    def test_duplicate_lot(self):
        d=sample();d['lots'].append(d['lots'][0]);self.bad(d)
    def test_duplicate_order(self):
        d=sample();d['orders'].append(d['orders'][0]);self.bad(d)
    def test_order_independent_lot_order(self):
        d=sample();d['lots'].reverse();self.assertEqual(analyze(d),analyze(sample()))

class Command(unittest.TestCase):
    def call(self,source,output):
        return subprocess.run([sys.executable,'-m','perishable_fulfillment.cli','--source',str(source),'--output',str(output)],capture_output=True,text=True)
    def test_cli_replay(self):
        with tempfile.TemporaryDirectory() as td:
            out=Path(td)/'out.json';src=ROOT/'fixtures/input.json';r=self.call(src,out);self.assertEqual(r.returncode,0,r.stderr);first=out.read_bytes();self.assertEqual(json.loads(first)['source_sha256'],hashlib.sha256(src.read_bytes()).hexdigest());self.assertEqual(self.call(src,out).returncode,0);self.assertEqual(first,out.read_bytes())
    def test_failure_preserves_output(self):
        with tempfile.TemporaryDirectory() as td:
            src=Path(td)/'bad.json';out=Path(td)/'out.json';out.write_text('prior')
            for body in ['{}','{"a":1,"a":2}','NaN']:
                src.write_text(body);self.assertEqual(self.call(src,out).returncode,2);self.assertEqual(out.read_text(),'prior')
    def test_output_safety(self):
        with tempfile.TemporaryDirectory() as td:
            src=Path(td)/'in.json';src.write_bytes((ROOT/'fixtures/input.json').read_bytes());before=src.read_bytes();self.assertEqual(self.call(src,src).returncode,2);self.assertEqual(src.read_bytes(),before);out=Path(td)/'link.json';out.symlink_to(src);self.assertEqual(self.call(src,out).returncode,2);self.assertEqual(src.read_bytes(),before)
