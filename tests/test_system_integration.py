"""Real modules wired together against an in-memory HTTP transport, no sockets."""
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

from livecore.config import DEFAULT
from livecore.capital import bootstrap
from livecore.execution import initial_state
from livecore.okx import OKXClient
from livecore.runtime import Runtime, preflight
from livecore.store import Store

NOW = 1791093874503


class Response:
    status = 200
    def __init__(self,data):
        self.data = json.dumps({'code':'0','msg':'','data':data}).encode()
    def read(self,size=-1):
        return self.data if size < 0 else self.data[:size]
    def __enter__(self):
        return self
    def __exit__(self,*args):
        pass


class SystemIntegration(unittest.TestCase):
    def test_readonly_preflight_and_first_runtime_cycle_use_real_interfaces(self):
        calls = []
        def transport(req,timeout):
            self.assertEqual(req.method,'GET')
            self.assertEqual({k.lower():v for k,v in req.header_items()}['x-simulated-trading'],'0')
            calls.append(req.full_url)
            u = urlsplit(req.full_url)
            q = {k:v[0] for k,v in parse_qs(u.query).items()}
            if u.path.endswith('/public/time'):
                return Response([{'ts':str(NOW)}])
            if u.path.endswith('/public/instruments'):
                return Response([dict(instId='BTC-USDT-SWAP',instType='SWAP',
                     instFamily='BTC-USDT',groupId='2',state='live',ctType='linear',
                     ctValCcy='BTC',settleCcy='USDT',ctVal='.01',ctMult='1',
                     lotSz='.01',minSz='.01',maxLmtSz='1000',tickSz='.1')])
            if u.path.endswith('/public/position-tiers'):
                return Response([dict(tier='1',maxLever='100',maxSz='100',mmr='.004')])
            if u.path.endswith('/market/ticker'):
                return Response([dict(instId='BTC-USDT-SWAP',bidPx='99999',askPx='100001',last='100000',ts=str(NOW))])
            if u.path.endswith('/account/config'):
                return Response([dict(uid='offline-synthetic',acctLv='2',posMode='net_mode',perm='read_only,trade')])
            if u.path.endswith('/account/leverage-info'):
                return Response([dict(instId='BTC-USDT-SWAP',mgnMode='isolated',lever='15')])
            if u.path.endswith('/account/trade-fee'):
                self.assertEqual(q, {'instType':'SWAP', 'instFamily':'BTC-USDT'})
                return Response([dict(instType='SWAP',taker='',takerU='',feeGroup=[
                    dict(groupId='1',taker='-.001',maker='-.0002'),
                    dict(groupId='2',taker='-.0005',maker='-.0002')])])
            if u.path.endswith('/account/balance'):
                return Response([{'details':[{'ccy':'USDT','availBal':'50000','eq':'50000','cashBal':'50000'}]}])
            if u.path.endswith('/market/history-candles'):
                after = int(q['after'])//60000*60000
                return Response([[str(after-(i+1)*60000),'100000','100001','99999','100000','1','1','1','1']
                                  for i in range(300)])
            if u.path.endswith(('/account/positions','/orders-pending','/orders-algo-pending',
                                '/account/bills','/account/bills-archive','/trade/fills','/trade/fills-history')):
                return Response([])
            self.fail('Unexpected HTTP path '+u.path)

        with tempfile.TemporaryDirectory() as tmp, patch('livecore.okx.time.sleep'):
            api = OKXClient(mode='live',credentials={'api_key':'fake','secret_key':'fake','passphrase':'fake'},
                            allow_orders=True,transport=transport)
            spec,quote,snapshot = preflight(api,DEFAULT,require_flat=True)
            with Store(Path(tmp)/'data') as store:
                state=bootstrap(api,initial_state(api.now(),DEFAULT),DEFAULT)
                store.initialize({'mode':'offline'},state)
                runner = Runtime(api,store,DEFAULT,spec)
                runner.tick()
                self.assertEqual(store.get_state()['cash'],30)
                self.assertIsNone(store.get_state()['position'])
                self.assertEqual(store.get_health()['phase'],'warming')
                self.assertEqual(store.unresolved(),[])
                self.assertTrue(store.get_state()['fill_audit']['ok'])
                self.assertEqual(len(store.bars()),600)
        self.assertTrue(any('/trade/fills?' in path for path in calls))


if __name__ == '__main__':
    unittest.main()
