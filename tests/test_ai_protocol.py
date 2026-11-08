import unittest
import threading
from unittest.mock import Mock
from chat_core import ChatCore
from protocol import validate_client_message, validate_server_event


def request(**changes):
    value = dict(type='ai_request', request_id='a' * 32, action='summary',
                 room_type='pm', room='Bob', instruction='')
    return value | changes


class AiProtocolTests(unittest.TestCase):
    def test_empty_instruction_and_core_transport(self):
        self.assertEqual(validate_client_message(request()), request())
        core = ChatCore('unused', 1)
        core.sock, core.name = Mock(), 'Alice'
        self.assertTrue(core.request_ai('a' * 32, 'summary', 'pm', 'Bob'))
        self.assertIn(b'"instruction": ""', core.sock.sendall.call_args.args[0])
        self.assertFalse(core.pm('Bob', ' '))

    def test_strict_identity_and_no_client_context_or_key(self):
        for change in [dict(request_id='A'*32), dict(request_id='a'), dict(room_type='x'),
                       dict(action='send'), dict(room='\n'), dict(instruction='x'),
                       dict(api_key='secret'), dict(messages=[]), dict(model='other'),
                       dict(action='draft', instruction='x'*501)]:
            with self.subTest(change=change), self.assertRaises(ValueError):
                validate_client_message(request(**change))
        validate_client_message(request(action='draft', instruction='ตอบสั้นๆ'))
        validate_client_message({'type':'login', 'name':'Alice Smith', 'legacy':True})

    def test_result_metadata_limits_and_errors(self):
        result = request(type='ai_result')
        del result['instruction']
        result.update(text='สรุป', message_count=1, cutoff=2)
        validate_server_event(result)
        for change in [dict(message_count=True), dict(message_count=513), dict(cutoff=-1),
                       dict(text=' '), dict(text='x'*3001), dict(text='\ud800'), dict(secret='x')]:
            with self.subTest(change=change), self.assertRaises(ValueError):
                validate_server_event(result | change)
        error = {k:v for k,v in result.items() if k not in ('message_count','cutoff')}
        validate_server_event(error | dict(type='ai_error', code='budget'))
        with self.assertRaises(ValueError):
            validate_server_event(error | dict(type='ai_error', code='unknown'))

    def test_concurrent_ai_write_does_not_block_another_local_send(self):
        core=ChatCore('unused',1); core.sock,core.name=Mock(),'Alice'
        done=threading.Event(); result=[]
        core._send_lock.acquire()
        worker=threading.Thread(target=lambda:(result.append(core.pm('Bob','keep draft')),done.set()),daemon=True)
        worker.start()
        try:
            self.assertTrue(done.wait(.2),'another write blocked behind the AI writer')
            self.assertEqual(result,[False])
            core.sock.sendall.assert_not_called()
        finally:
            core._send_lock.release(); worker.join(1)
