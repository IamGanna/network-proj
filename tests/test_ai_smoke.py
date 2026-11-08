import contextlib
import io
import unittest
from unittest.mock import Mock, patch
from ai_service import AiConfig, AiFailure
from tools import ai_smoke


class SmokeTests(unittest.TestCase):
    def test_no_live_flag_does_not_read_config_or_call_api(self):
        with patch.object(ai_smoke,'load_server_config') as config, patch.object(ai_smoke,'AiService') as service:
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(ai_smoke.main([]),0)
            config.assert_not_called(); service.assert_not_called()

    def test_live_calls_only_two_generations_and_closes(self):
        service=Mock(); service.generate.return_value='synthetic response'
        with patch.object(ai_smoke,'load_server_config',return_value=AiConfig('fake')), patch.object(ai_smoke,'AiService',return_value=service):
            with contextlib.redirect_stdout(io.StringIO()): self.assertEqual(ai_smoke.main(['--live']),0)
        self.assertEqual([c.kwargs['action'] for c in service.generate.call_args_list],['summary','draft'])
        service.close.assert_called_once()

    def test_live_failure_does_not_retry_or_print_key(self):
        service=Mock(); service.generate.side_effect=AiFailure('provider')
        output=io.StringIO()
        with patch.object(ai_smoke,'load_server_config',return_value=AiConfig('fake-secret')), patch.object(ai_smoke,'AiService',return_value=service):
            with contextlib.redirect_stdout(output): self.assertEqual(ai_smoke.main(['--live']),1)
        self.assertEqual(service.generate.call_count,1)
        self.assertNotIn('fake-secret',output.getvalue())
