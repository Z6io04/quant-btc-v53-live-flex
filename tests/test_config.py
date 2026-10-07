"""Local configuration, credential and deployment-template checks only."""
import configparser
import hashlib
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from livecore import config


ROOT = Path(__file__).resolve().parents[1]


class ConfigTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.directory = Path(self.temp.name)
        self.path = self.directory / "config.json"

    def tearDown(self):
        self.temp.cleanup()

    def write(self, **changes):
        value = dict(config.DEFAULT)
        value.update(changes)
        self.path.write_text(json.dumps(value), encoding="utf-8")
        return value

    def test_shipped_config_is_live_with_original_initial_budget(self):
        cfg = config.load_config(ROOT / "config.json")
        self.assertEqual(cfg["mode"], "live")
        self.assertEqual(cfg["capital_usdt"], 30)
        self.assertEqual(cfg["leverage"], 15)
        self.assertEqual(cfg["risk"], 0.06)
        self.assertEqual(cfg["proxy"], "http://127.0.0.1:17890")

    def test_strategy_parameters_cannot_silently_change(self):
        for name in ("leverage", "risk", "max_notional_multiple",
                     "reserve", "fee", "slip", "halt_dd", "cooldown_hours", "warmup_days"):
            with self.subTest(field=name):
                self.write(**{name: config.DEFAULT[name] + 1})
                with self.assertRaises(ValueError):
                    config.load_config(self.path)
                self.write(**{name: True})
                with self.assertRaises(ValueError):
                    config.load_config(self.path)

    def test_initial_principal_is_not_a_lifetime_cap(self):
        self.write(capital_usdt=1000,max_loss_usdt=30)
        self.assertEqual(config.load_config(self.path)['capital_usdt'],1000)
        for field in ('capital_usdt','max_loss_usdt'):
            for value in (True,False,0,-1,float('inf'),float('nan'),'30',None):
                self.write(**{field:value})
                with self.subTest(field=field,value=value), self.assertRaises(ValueError):
                    config.load_config(self.path)

    def test_unknown_and_missing_fields_are_rejected(self):
        self.write(allow_live=True)
        with self.assertRaises(ValueError):
            config.load_config(self.path)
        cfg = self.write()
        del cfg["risk"]
        self.path.write_text(json.dumps(cfg))
        with self.assertRaises(ValueError):
            config.load_config(self.path)

    def test_network_destinations_and_environment_are_not_free_form(self):
        for change in ({"mode": "demo"}, {"mode": "production"}, {"origin": "https://example.invalid"},
                       {"origin": "https://openapi.okx.com@evil.invalid"},
                       {"instrument": "ETH-USDT-SWAP"}, {"proxy": ""},
                       {"proxy": "http://proxy-user:proxy-pass@127.0.0.1:17890"},
                       {"proxy": "http://192.168.1.1:17890"}):
            with self.subTest(change=change):
                self.write(**change)
                with self.assertRaises(ValueError):
                    config.load_config(self.path)

    def test_timing_bounds_are_validated(self):
        for change in ({"poll_seconds": 0}, {"poll_seconds": 31}, {"poll_seconds": True},
                       {"quote_age_seconds": 300}, {"signal_ttl_seconds": 3600}):
            with self.subTest(change=change):
                self.write(**change)
                with self.assertRaises(ValueError):
                    config.load_config(self.path)
        self.write(poll_seconds=5)
        self.assertEqual(config.load_config(self.path)["poll_seconds"], 5)

    def test_data_paths_separate_demo_and_live_without_touching_old_paper(self):
        with patch.object(config, "ROOT", self.directory):
            self.assertEqual(config.data_dir({"mode": "demo"}), self.directory / "var/demo")
            self.assertEqual(config.data_dir({"mode": "live"}), self.directory / "var/live")

    def test_code_identity_covers_launcher_core_and_frozen_source(self):
        (self.directory / "livecore").mkdir()
        (self.directory / "launch.py").write_text("# launcher\n")
        (self.directory / "livecore/store.py").write_text("# storage\n")
        frozen = self.directory / "strategy.py"
        frozen.write_text("# fixed strategy\n")
        manifest = {"strategy.py": hashlib.sha256(frozen.read_bytes()).hexdigest()}
        (self.directory / "frozen-source-manifest.json").write_text(json.dumps(manifest))
        with patch.object(config, "ROOT", self.directory):
            first = config.code_identity(config.DEFAULT)
            self.assertEqual(set(first["files"]), {"launch.py", "livecore/store.py", "strategy.py"})
            (self.directory / "livecore/store.py").write_text("# changed storage\n")
            second = config.code_identity(config.DEFAULT)
            self.assertNotEqual(first["files"]["livecore/store.py"], second["files"]["livecore/store.py"])
            frozen.write_text("# modified strategy\n")
            with self.assertRaisesRegex(ValueError, "冻结"):
                config.code_identity(config.DEFAULT)

    def test_raw_exceptions_are_not_copied_into_user_status(self):
        secret_marker = "test-secret-must-not-leak"
        for error in (RuntimeError(secret_marker), ValueError(secret_marker), OSError(secret_marker)):
            self.assertNotIn(secret_marker, config.safe_error(error))
        self.assertEqual(config.safe_error(config.Attention("安全的操作提示")), "安全的操作提示")


class CredentialTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.directory = Path(self.temp.name)
        self.path = self.directory / "credentials.env"
        self.body = "OKX_API_KEY=example-key\nOKX_SECRET_KEY=example-secret\nOKX_PASSPHRASE=example-pass\n"

    def tearDown(self):
        self.temp.cleanup()

    def write(self, body=None, mode=0o600):
        self.path.write_text(self.body if body is None else body)
        self.path.chmod(mode)

    def test_credentials_are_loaded_only_from_explicit_local_file(self):
        self.write()
        with patch.dict(os.environ, {"OKX_API_KEY": "ambient-key-not-used"}):
            value = config.credentials(self.path)
        self.assertEqual(value, {"api_key": "example-key", "secret_key": "example-secret", "passphrase": "example-pass"})

    def test_missing_file_or_placeholder_or_duplicate_key_is_rejected(self):
        with self.assertRaises(ValueError):
            config.credentials(self.path)
        for body in (self.body.replace("example-key", "FILL_API_KEY"),
                     self.body + "OKX_API_KEY=second-key\n", self.body.replace("OKX_PASSPHRASE=example-pass\n", ""),
                     self.body + "UNEXPECTED_SECRET=test\n"):
            with self.subTest(case=body.splitlines()[-1].split("=")[0]):
                self.write(body)
                with self.assertRaises(ValueError):
                    config.credentials(self.path)

    @unittest.skipIf(os.name == "nt", "POSIX permission and owner checks")
    def test_group_readable_world_readable_or_wrong_owner_is_rejected(self):
        for mode in (0o640, 0o644, 0o604):
            self.write(mode=mode)
            with self.subTest(mode=oct(mode)), self.assertRaisesRegex(ValueError, "600"):
                config.credentials(self.path)
        self.write()
        with patch("livecore.config.os.getuid", return_value=os.getuid() + 1), self.assertRaises(ValueError):
            config.credentials(self.path)

    def test_symlink_credentials_are_rejected(self):
        real = self.directory / "real.env"
        real.write_text(self.body)
        real.chmod(0o600)
        self.path.symlink_to(real)
        with self.assertRaises(ValueError):
            config.credentials(self.path)


class DeploymentTemplateTests(unittest.TestCase):
    def test_user_unit_keeps_runtime_alive_if_proxy_stops(self):
        # systemd permits repeated Environment= directives.
        unit = configparser.ConfigParser(interpolation=None, strict=False)
        unit.read(ROOT / "deploy/btc-v53-live.service")
        self.assertEqual(unit["Unit"]["Wants"], "mihomo-proxy.service")
        self.assertEqual(unit["Unit"]["After"], "mihomo-proxy.service")
        self.assertNotIn("Requires", unit["Unit"])
        self.assertNotIn("BindsTo", unit["Unit"])
        self.assertEqual(unit["Install"]["WantedBy"], "default.target")
        self.assertEqual(unit["Service"]["WorkingDirectory"], "%h/btc-v53-live")
        self.assertEqual(unit["Service"]["ExecStart"], "/usr/bin/python3 -u %h/btc-v53-live/launch.py run")
        self.assertNotIn("User", unit["Service"])
        self.assertEqual(unit["Service"]["KillSignal"], "SIGTERM")

    def test_installer_shell_syntax_and_no_start_or_arm_commands(self):
        path = ROOT / "deploy/install-service.sh"
        result = subprocess.run(["bash", "-n", str(path)], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        commands = [line.strip() for line in path.read_text().splitlines()
                    if line.strip() and not line.lstrip().startswith(("#", "echo "))]
        self.assertTrue(any(line.startswith("systemctl --user enable ") for line in commands))
        self.assertFalse(any("--now" in line for line in commands))
        self.assertFalse(any(line.startswith("systemctl --user " + verb)
                             for line in commands for verb in ("start ", "restart ")))
        self.assertFalse(any("arm-live" in line for line in commands))
        self.assertFalse(any("launch.py run" in line for line in commands))


if __name__ == "__main__":
    unittest.main()
