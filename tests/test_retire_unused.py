"""Offline cleanup guards; every target lives in a disposable temporary home."""
import copy
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import tempfile
import types
import unittest
from livecore.store import Store, process_lock
ROOT=Path(__file__).resolve().parents[1]
spec=importlib.util.spec_from_file_location('retire_unused_test_tool',ROOT/'tools/retire_unused.py')
tool=importlib.util.module_from_spec(spec);spec.loader.exec_module(tool)
NOW=1791331200000
UID='fixture_uid'
def digest(data):return hashlib.sha256(data).hexdigest()

class RetirementTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.home=Path(self.tmp.name).resolve()
        self.root=self.home/'btc-v53-live';self.root.mkdir(mode=0o700);(self.root/'livecore').mkdir()
        for name,text in [('launch.py','# old entry\n'),('livecore/config.py','# old core\n'),('strategy.py','# frozen\n')]:
            (self.root/name).write_text(text)
        self.files={name:digest((self.root/name).read_bytes()) for name in ('launch.py','livecore/config.py','strategy.py')}
        (self.root/'frozen-source-manifest.json').write_text(json.dumps({'strategy.py':self.files['strategy.py']}))
        self.cfg={'mode':'live','instrument':'BTC-USDT-SWAP','capital_usdt':30,'leverage':15,'max_loss_usdt':30,
                  'origin':'https://openapi.okx.com','proxy':'http://127.0.0.1:17890'}
        (self.root/'config.json').write_text(json.dumps(self.cfg))
        self.creds=self.root/'credentials.env';self.creds.write_text('TEST_FIXTURE_NOT_REAL_CREDENTIALS\n');self.creds.chmod(0o600)
        self.directory=self.root/'var/live';self.spec={'ct_value':.01,'lot':.01,'tick':.1}
        self.identity={'config':self.cfg,'files':self.files,'uid':UID,'mode':'live','instrument':'BTC-USDT-SWAP',
                       'key_fingerprint':digest(b'fixture_key'),'spec':self.spec}
        self.state={'schema':1,'armed':True,'paused':False,'halted':None,'position':None,'protection':None,'closing':False,
                    'realized_pnl':0.,'fees_signed':0.,'funding':0.,'max_drawdown':0.,'closed_trades':0,'cooldown_until':0,
                    'cash':30.,'peak_equity':30.,'last_equity':30.,'initialized_at':NOW-86400000,
                    'fill_audit':{'ok':True,'checked_at':NOW-1000},'live_consent':{'uid':UID,'binding_id':'fixture-binding'}}
        with Store(self.directory) as store:store.initialize(self.identity,self.state)
        self.db=self.directory/'state.sqlite3'
        self.binding=self.home/'.local/state/btc-v53-live/accounts'/(digest(('live:'+UID).encode())+'.json')
        self.binding.parent.mkdir(mode=0o700,parents=True)
        self.binding.write_text(json.dumps({'uid':UID,'binding_id':'fixture-binding','directory':str(self.directory)}));self.binding.chmod(0o600)
        self.neighbor=self.binding.parent/'f3-account.json';self.neighbor.write_text('keep F3\n')
        self.unit=self.home/'.config/systemd/user/btc-v53-live.service';self.unit.parent.mkdir(parents=True);self.unit.write_text('fixture unit\n')
        self.unit_hash=digest(self.unit.read_bytes())
        self.service={'Id':'btc-v53-live.service','UnitFileState':'disabled','ActiveState':'inactive','MainPID':'0','FragmentPath':str(self.unit)}
        self.account={'uid':UID,'position':None,'available':30}
        self.fills=[];self.funding=[];self.orders=[];self.algos=[];self.api_calls=[];self.clients=[];self.remote_error=None
        def client(**kwargs):
            self.assertIs(kwargs['allow_orders'],False);self.clients.append(kwargs);parent=self
            class API:
                def read(self,path,params=None,private=False):
                    parent.api_calls.append((path,params,private))
                    if parent.remote_error:raise parent.remote_error
                    return copy.deepcopy(parent.orders if path.endswith('/orders-pending') else parent.algos)
                def trade_fills(self,begin):return copy.deepcopy(parent.fills)
                def funding_bills(self,begin):return copy.deepcopy(parent.funding)
            return API()
        def get_identity(cfg,creds,uid,spec):
            files={name:digest((self.root/name).read_bytes()) for name in self.files}
            return dict(self.identity,config=cfg,files=files,uid=uid,spec=spec,key_fingerprint=digest(creds['api_key'].encode()))
        def validate_binding(mode,uid,directory,binding_id):
            row=json.loads(self.binding.read_text())
            if row!={'uid':uid,'binding_id':binding_id,'directory':str(directory)}:raise tool.RetirementError('binding mismatch')
        def lockpath(mode,uid):
            path=self.home/'.local/state/btc-v53-live/locks'/(digest((mode+':'+uid).encode())+'.lock')
            path.parent.mkdir(mode=0o700,parents=True,exist_ok=True);return path
        self.modules=types.SimpleNamespace(
          config=types.SimpleNamespace(load_config=lambda:json.loads((self.root/'config.json').read_text()),
            credentials=lambda path:{'api_key':'fixture_key'},account_binding_path=lambda mode,uid:self.binding,
            validate_account_binding=validate_binding,account_lock_path=lockpath),
          runtime=types.SimpleNamespace(identity=get_identity,validate_live_consent=lambda state,cfg:state['live_consent'],
            preflight=lambda api,cfg,require_flat=False:(self.spec,{},copy.deepcopy(self.account))),
          okx=types.SimpleNamespace(OKXClient=client),store=types.SimpleNamespace(process_lock=process_lock))
        self.tool=tool.Retirement(home=self.home,modules=self.modules,service_check=lambda:self.service,
           reload=lambda:types.SimpleNamespace(returncode=0),accepted_code=self.files,accepted_service=self.unit_hash)
    def tearDown(self):self.tmp.cleanup()
    def state_change(self,**kwargs):
        with Store(self.directory) as store:
            state=store.get_state();state.update(kwargs);store.set_state(state)
    def intact(self):
        self.assertTrue(self.root.exists());self.assertTrue(self.binding.exists());self.assertTrue(self.unit.exists())
    def assert_refused(self,confirm=lambda:'DELETE V53'):
        with self.assertRaises(Exception):self.tool.run(confirm)
        self.intact()
    def test_readonly_check_preserves_database(self):
        with tool.open_database(self.db) as db:before=tool.database_digest(db)
        result=self.tool.run();self.assertFalse(result['deleted']);self.assertTrue(result['ready']);self.intact()
        with tool.open_database(self.db) as db:self.assertEqual(before,tool.database_digest(db))
    def test_cleanup_exact_scope_prearm_test_allowed(self):
        self.fills=[{'fillTime':str(self.state['initialized_at']-1000)}]
        for name in ('mihomo','btc-F3-live','btc-v53-localsim'):(self.home/name).mkdir();(self.home/name/'sentinel').write_text(name)
        receipt=self.home/'.local/state/btc-v53-live/smoke/receipt.json';receipt.parent.mkdir(parents=True);receipt.write_text('test buy history')
        result=self.tool.run(lambda:'DELETE V53');self.assertTrue(result['deleted']);self.assertEqual(result['orders_sent'],0)
        self.assertFalse(self.root.exists());self.assertFalse(self.binding.exists());self.assertFalse(self.unit.exists())
        self.assertTrue(self.neighbor.exists());self.assertTrue(receipt.exists())
        for name in ('mihomo','btc-F3-live','btc-v53-localsim'):self.assertTrue((self.home/name/'sentinel').exists())
        self.assertEqual(len(self.clients),2)
        algos={params['ordType'] for path,params,_ in self.api_calls if path.endswith('/orders-algo-pending')}
        self.assertEqual(algos,set(tool.ALGO_TYPES))
    def test_cancel(self):self.assert_refused(lambda:'no')
    def test_source_modified(self):
        (self.root/'launch.py').write_text('# changed');self.assert_refused()
    def test_wrong_release(self):
        self.tool.accepted_code=dict(self.files,extra='0'*64);self.assert_refused()
    def test_active_service(self):
        self.service['ActiveState']='active';self.service['MainPID']='123';self.assert_refused()
    def test_enabled_service(self):
        self.service['UnitFileState']='enabled';self.assert_refused()
    def test_wrong_unit(self):
        self.service['FragmentPath']='/etc/systemd/system/btc-v53-live.service';self.assert_refused()
    def test_fill_afterarm(self):
        self.fills=[{'fillTime':str(self.state['initialized_at'])}];self.assert_refused()
    def test_funding_afterarm(self):
        self.funding=[{'ts':str(self.state['initialized_at']+1)}];self.assert_refused()
    def test_unknown_fill_time(self):
        self.fills=[{'fillTime':None}];self.assert_refused()
    def test_network_failure(self):
        self.remote_error=RuntimeError('fixture unavailable');self.assert_refused()
    def test_different_account(self):
        self.account['uid']='F3';self.assert_refused()
    def test_open_position(self):
        self.account['position']={'pos':'1'};self.assert_refused()
    def test_pending_order(self):
        self.orders=[{'ordId':'fixture'}];self.assert_refused()
    def test_pending_algo(self):
        self.algos=[{'algoId':'fixture'}];self.assert_refused()
    def test_local_strategy_intent(self):
        with Store(self.directory) as store:store.create_intent('example','entry',{'instId':'BTC-USDT-SWAP'},{},NOW)
        self.assert_refused()
    def test_local_fill(self):
        with Store(self.directory) as store:store.append_record('fill','example',{},NOW)
        self.assert_refused()
    def test_pnl_fee_drawdown_halt(self):
        for key,value in [('realized_pnl',1),('fees_signed',-.001),('funding',.01),('max_drawdown',.001),('closed_trades',1),('cash',29.9),('halted','risk halt')]:
            with self.subTest(key=key):
                self.state_change(**{key:value});self.assert_refused();self.state_change(**{key:self.state[key]})
    def test_failed_fill_audit(self):
        self.state_change(fill_audit={'ok':False});self.assert_refused()
    def test_foreign_binding(self):
        self.binding.write_text(json.dumps({'uid':UID,'binding_id':'fixture-binding','directory':str(self.home/'btc-F3-live/var/live')}));self.assert_refused()
    def test_symlink_tree(self):
        (self.root/'outside').symlink_to(self.home/'mihomo');self.assert_refused()
    def test_hardlink_tree(self):
        (self.home/'outside').write_text('external');os.link(self.home/'outside',self.root/'linked');self.assert_refused()
    def test_ledger_changes_during_confirmation(self):
        def changed():
            with Store(self.directory) as store:store.append_record('control','elsewhere',{'action':'resume'},NOW)
            return 'DELETE V53'
        self.assert_refused(changed)
    def test_position_appears_during_confirmation(self):
        def changed():self.account['position']={'pos':'1'};return 'DELETE V53'
        self.assert_refused(changed)
    def test_reload_failure(self):
        self.tool.reload=lambda:types.SimpleNamespace(returncode=1)
        result=self.tool.run(lambda:'DELETE V53');self.assertTrue(result['deleted']);self.assertFalse(result['daemon_reloaded']);self.assertTrue(self.neighbor.exists())
    def test_runtime_lock_busy(self):
        with process_lock(self.directory/'runtime.lock'):self.assert_refused()
    def test_account_lock_busy(self):
        with process_lock(self.modules.config.account_lock_path('live',UID)):self.assert_refused()

    def test_interrupted_deletion_can_finish_at_each_commit_boundary(self):
        # Rebuild an independent fixture for each destructive fault.
        for point in ('directory_renamed','binding_deleted','unit_deleted','directory_deleted'):
            with self.subTest(point=point):
                nested=RetirementTests();nested.setUp()
                try:
                    def crash(where):
                        if where==point:raise RuntimeError('simulated crash')
                    nested.tool.fault=crash
                    with self.assertRaises(RuntimeError):nested.tool.run(lambda:'DELETE V53')
                    self.assertTrue(tool.receipt_path(nested.home).exists())
                    tool.recover_retirement(home=nested.home,service_check=lambda:None,reload=lambda:None)
                    self.assertFalse(nested.root.exists());self.assertFalse(nested.binding.exists());self.assertFalse(nested.unit.exists())
                    self.assertFalse(tool.receipt_path(nested.home).exists());self.assertTrue(nested.neighbor.exists())
                finally:nested.tearDown()
    def test_recovery_does_not_delete_replacement_root(self):
        def crash(where):
            if where=='directory_deleted':raise RuntimeError('simulated crash')
        self.tool.fault=crash
        with self.assertRaises(RuntimeError):self.tool.run(lambda:'DELETE V53')
        self.root.mkdir();(self.root/'new_program').write_text('new install')
        result=tool.recover_retirement(home=self.home,service_check=lambda:None,reload=lambda:None)
        self.assertEqual(result,'canceled_recheck')
        self.assertTrue((self.root/'new_program').exists());self.assertTrue(self.neighbor.exists())
    def test_recovery_does_not_remove_changed_account_binding(self):
        def crash(where):
            if where=='directory_renamed':raise RuntimeError('simulated crash')
        self.tool.fault=crash
        with self.assertRaises(RuntimeError):self.tool.run(lambda:'DELETE V53')
        self.binding.write_text('another account registration')
        with self.assertRaises(tool.RetirementError):
            tool.recover_retirement(home=self.home,service_check=lambda:None,reload=lambda:None)
        self.assertEqual(self.binding.read_text(),'another account registration')

    def test_receipt_before_rename_never_authorizes_stale_ledger_deletion(self):
        def crash(where):
            if where=='receipt_prepared':raise RuntimeError('crash before rename')
        self.tool.fault=crash
        with self.assertRaises(RuntimeError):self.tool.run(lambda:'DELETE V53')
        self.intact()
        # The service could have been restarted while its original path exists.
        self.state_change(realized_pnl=1,cash=31)
        result=tool.recover_retirement(home=self.home,service_check=lambda:None,reload=lambda:None)
        self.assertEqual(result,'canceled_recheck');self.intact()
        self.assertFalse(tool.receipt_path(self.home).exists())
        self.tool.fault=lambda point:None
        self.assert_refused()

if __name__=='__main__':unittest.main()
