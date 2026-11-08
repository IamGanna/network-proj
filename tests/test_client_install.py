"""A PC without the client packages installs requirements.txt, then starts the client again."""
import runpy
import sys
import unittest
from pathlib import Path
from unittest import mock

CLIENT = str(Path(__file__).resolve().parents[1] / "client.py")


class AutoInstallTests(unittest.TestCase):
    def run_client_without_voice(self, results):
        with mock.patch.dict(sys.modules, {"voice": None}), \
             mock.patch("subprocess.call", side_effect=results) as call, \
             mock.patch.object(sys, "argv", [CLIENT, "13.210.141.200"]), \
             self.assertRaises(SystemExit) as stop:
            runpy.run_path(CLIENT)
        return call, stop.exception.code

    def test_missing_package_installs_then_restarts(self):
        call, code = self.run_client_without_voice([0, 0])
        install, restart = call.call_args_list
        self.assertEqual(install.args[0][1:4], ["-m", "pip", "install"])
        self.assertTrue(install.args[0][-1].endswith("requirements.txt"))
        self.assertEqual(restart.args[0], [sys.executable, CLIENT, "13.210.141.200"])
        self.assertEqual(code, 0)

    def test_failed_install_stops_with_the_manual_command(self):
        call, code = self.run_client_without_voice([1])
        self.assertEqual(call.call_count, 1)
        self.assertIn("pip install -r requirements.txt", code)
