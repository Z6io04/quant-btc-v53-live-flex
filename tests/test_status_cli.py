import contextlib
import io
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch, MagicMock

import launch
from livecore.config import DEFAULT, Attention
from livecore.execution import initial_state
from livecore.status import display, network_probe, status
from livecore.store import Store, read_status


class StatusTests(unittest.TestCase):
    def test_proxy_listener_is_not_claimed_to_be_okx_connectivity(self):
        socket = MagicMock()
        with patch('livecore.status.socket.create_connection',return_value=socket), \
             patch('livecore.status.service_state',return_value='active'), \
             patch('livecore.status.OKXClient') as client:
            client.return_value.sync_time.side_effect = RuntimeError('private-secret-test')
            report = network_probe(DEFAULT)
        self.assertTrue(report['proxy_listening'])
        self.assertFalse(report['okx_reachable'])
        self.assertNotIn('private-secret-test',report['error'])

    def test_dead_listener_does_not_directly_bypass_proxy(self):
        with patch('livecore.status.socket.create_connection',side_effect=OSError()), \
             patch('livecore.status.service_state',return_value='inactive'), \
             patch('livecore.status.OKXClient') as client:
            report = network_probe(DEFAULT)
        client.assert_not_called()
        self.assertFalse(report['okx_reachable'])

    def test_successful_probe_is_unsigned_readonly_explicit_proxy(self):
        with patch('livecore.status.socket.create_connection',return_value=MagicMock()), \
             patch('livecore.status.service_state',return_value='active'), \
             patch('livecore.status.OKXClient') as client:
            client.return_value.sync_time.return_value = 1000
            report = network_probe(DEFAULT)
        self.assertTrue(report['okx_reachable'])
        self.assertEqual(client.call_args.kwargs['proxy'],'http://127.0.0.1:17890')
        self.assertNotIn('credentials',client.call_args.kwargs)
        self.assertNotIn('allow_orders',client.call_args.kwargs)
        client.return_value.write.assert_not_called()

    def test_local_status_never_creates_ledger_or_probes(self):
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp)/'uninitialized'
            with patch('livecore.status.data_dir',return_value=folder), \
                 patch('livecore.status.service_state',return_value='inactive'), \
                 patch('livecore.status.network_probe') as probe:
                report = status(DEFAULT,probe=False)
            probe.assert_not_called()
            self.assertFalse(folder.exists())
            self.assertFalse(report['local']['exists'])

    def test_stale_active_service_does_not_claim_current_position(self):
        report = {'mode':'demo','service':'active','network':None,
                  'local':{'state':initial_state(1000,DEFAULT),'health':{'checked_at':1000,'phase':'running'},'unresolved':[]}}
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            display(report)
        self.assertIn('记录过期',out.getvalue())
        self.assertIn('最近成功对账时点',out.getvalue())

    def test_stopped_has_no_real_time_claim(self):
        state = initial_state(1000,DEFAULT)
        report = {'mode':'demo','service':'inactive','network':None,
                  'local':{'state':state,'health':{'checked_at':1000,'phase':'stopped'},'unresolved':[]}}
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            display(report)
        self.assertNotIn('运行记录：近期有更新',out.getvalue())


class CLITests(unittest.TestCase):
    def test_demo_run_denied_before_credentials_or_client(self):
        with patch('launch.load_config',return_value=dict(DEFAULT,mode='demo')), \
             patch('launch.credentials') as credentials,patch('launch.OKXClient') as client:
            with self.assertRaises(Attention):
                launch.main(['run'])
        credentials.assert_not_called()
        client.assert_not_called()

    def test_live_arming_from_pipe_is_denied_before_credentials_or_client(self):
        with patch('launch.load_config',return_value=dict(DEFAULT,mode='live')), \
             patch('launch.sys.stdin.isatty',return_value=False), \
             patch('launch.credentials') as creds,patch('launch.OKXClient') as client:
            with self.assertRaises(Attention):
                launch.main(['arm-live'])
        creds.assert_not_called()
        client.assert_not_called()

    def test_live_arming_cancel_does_not_call_arming_or_write(self):
        with patch('launch.load_config',return_value=dict(DEFAULT,mode='live')), \
             patch('launch.sys.stdin.isatty',return_value=True), \
             patch('builtins.input',return_value='yes'), \
             patch('launch.credentials',return_value={}),patch('launch.OKXClient') as client, \
             patch('launch.runtime.arm_live') as arm,contextlib.redirect_stdout(io.StringIO()):
            with self.assertRaises(Attention):
                launch.main(['arm-live'])
        arm.assert_not_called()
        self.assertFalse(client.call_args.kwargs['allow_orders'])
        client.return_value.write.assert_not_called()

    def test_exact_confirmation_reaches_arm_without_write_permission(self):
        with patch('launch.load_config',return_value=dict(DEFAULT,mode='live')), \
             patch('launch.sys.stdin.isatty',return_value=True), \
             patch('builtins.input',return_value='LIVE 30U'), \
             patch('launch.credentials',return_value={'api_key':'test'}),patch('launch.OKXClient') as client, \
             patch('launch.runtime.arm_live',return_value={'armed':True,'orders_sent':0}) as arm, \
             contextlib.redirect_stdout(io.StringIO()):
            launch.main(['arm-live'])
        self.assertEqual(arm.call_args.kwargs['confirmation'],'LIVE 30U')
        self.assertEqual(client.call_args.kwargs['mode'],'live')
        self.assertFalse(client.call_args.kwargs['allow_orders'])

    def test_small_buy_pipe_never_opens_client(self):
        with patch('launch.load_config',return_value=dict(DEFAULT,mode='live')), \
             patch('launch.sys.stdin.isatty',return_value=False),patch('launch.OKXClient') as client:
            with self.assertRaises(Attention):
                launch.main(['test-buy'])
        client.assert_not_called()

    def test_small_buy_confirmation_exact_and_no_secret_instructions(self):
        out=io.StringIO()
        with patch('launch.sys.stdin.isatty',return_value=True), \
             patch('builtins.input',side_effect=['yes','BUY 1U']),contextlib.redirect_stdout(out):
            self.assertFalse(launch.confirm_small_buy({'margin':'0.95'}))
            self.assertTrue(launch.confirm_small_buy({'margin':'0.95'}))
        self.assertIn('不自动止损或平仓',out.getvalue())

    def test_test_status_client_has_no_write_permission(self):
        with patch('launch.load_config',return_value=dict(DEFAULT,mode='live')), \
             patch('launch.credentials',return_value={}),patch('launch.OKXClient') as client, \
             patch('livecore.smoke.status',return_value={'orders_attempted':0}) as check, \
             contextlib.redirect_stdout(io.StringIO()):
            launch.main(['test-status'])
        self.assertFalse(client.call_args.kwargs['allow_orders'])
        check.assert_called_once_with(client.return_value,dict(DEFAULT,mode='live'))
        client.return_value.write.assert_not_called()

    def test_public_check_never_requests_credentials_or_order_permission(self):
        with patch('launch.load_config',return_value=DEFAULT), \
             patch('launch.credentials') as credentials,patch('launch.OKXClient') as client, \
             patch('launch.specification',return_value={}),patch('launch.ticker',return_value={'ts':1}), \
             contextlib.redirect_stdout(io.StringIO()):
            launch.main(['check'])
        credentials.assert_not_called()
        self.assertIsNone(client.call_args.kwargs['credentials'])
        self.assertFalse(client.call_args.kwargs['allow_orders'])
        client.return_value.write.assert_not_called()

    def test_control_queued_without_state_mutation_or_network(self):
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp)
            with Store(folder) as store:
                state = initial_state(1000,DEFAULT)
                store.initialize({'account':'test'},state)
            with patch('launch.load_config',return_value=DEFAULT),patch('launch.data_dir',return_value=folder), \
                 patch('launch.OKXClient') as client,contextlib.redirect_stdout(io.StringIO()):
                launch.main(['flatten'])
            client.assert_not_called()
            with Store(folder) as store:
                self.assertEqual(store.get_state(),state)
                self.assertEqual(store.records('control'),[{'command':'flatten'}])

    def test_credentials_input_not_echoed_or_overwritten(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            output = io.StringIO()
            with patch('launch.ROOT',root),patch('launch.sys.stdin.isatty',return_value=True), \
                 patch('launch.getpass.getpass',side_effect=['demo-key-unique','secret-unique','phrase-unique']), \
                 contextlib.redirect_stdout(output):
                launch.setup_credentials()
            self.assertEqual((root/'credentials.env').stat().st_mode&0o777,0o600)
            self.assertNotIn('secret-unique',output.getvalue())
            with patch('launch.ROOT',root),patch('launch.sys.stdin.isatty',return_value=True), \
                 patch('launch.getpass.getpass') as prompt:
                with self.assertRaises(Attention):
                    launch.setup_credentials()
                prompt.assert_not_called()


if __name__ == '__main__':
    unittest.main()
