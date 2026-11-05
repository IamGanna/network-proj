"""Bounded policy tests; admission cases exercise the loop used by main()."""
import gc
import json
import socket
import threading
import time
import unittest
from unittest.mock import patch

import protocol
import server
from chat_core import ChatCore
from tests.support import Events, LoopbackServer, frame, shutdown


class AdmissionServer:
    def __init__(self, observe=None, **options):
        gc.collect()  # Tk finalization belongs to the main thread.
        self.listener = socket.socket()
        self.listener.bind(('127.0.0.1', 0))
        self.port = self.listener.getsockname()[1]
        self.listener.listen()
        self.stop = threading.Event()
        self.sessions = []
        self.readers = []
        self.errors = []
        def admitted(session):
            self.sessions.append(session)
            self.readers.append(threading.current_thread())
            if observe is not None:
                observe(session)
        def run():
            try:
                server.serve(self.listener, self.stop, on_session=admitted, **options)
            except BaseException as exc:
                self.errors.append(exc)
        self.thread = threading.Thread(target=run, daemon=True)
        self.thread.start()

    def connect(self):
        sock = socket.create_connection(('127.0.0.1', self.port), timeout=1)
        sock.settimeout(1)
        return sock

    def close(self):
        self.stop.set()
        self.thread.join(3)
        assert not self.thread.is_alive(), 'Admission loop leaked'
        assert not self.errors, self.errors
        assert all(s.closed.is_set() and not s.writer.is_alive() for s in self.sessions)
        assert all(not reader.is_alive() for reader in self.readers)


class ResourceTests(unittest.TestCase):
    def setUp(self):
        self.assertFalse(server.clients)
        self.assertFalse(server.groups)

    def start_server(self, **options):
        peer = AdmissionServer(**options)
        self.addCleanup(peer.close)
        return peer

    def raw(self, peer):
        sock = peer.connect()
        self.addCleanup(shutdown, sock)
        return sock

    def core(self, peer, name):
        core = ChatCore('127.0.0.1', peer.port)
        self.addCleanup(core.close)
        self.assertEqual(core.login(name), (True, name))
        events = Events()
        core.start(events.put)
        return core, events

    def wait(self, predicate, timeout=1):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if predicate():
                return
            time.sleep(.005)
        self.fail('Resource/state condition did not complete')

    def test_field_and_encoded_size_limits(self):
        self.assertEqual((protocol.MAX_FRAME_BYTES, protocol.MAX_TEXT_CHARS,
                          protocol.MAX_NAME_CHARS, protocol.MAX_SESSIONS,
                          protocol.MAX_GROUPS, protocol.OUTBOUND_QUEUE_FRAMES,
                          protocol.LOGIN_TIMEOUT), (65536, 4096, 64, 32, 64, 64, 10))
        for command in ({'type': 'login', 'name': 'a' * 65},
                        {'type': 'create_group', 'group': 'ก' * 65},
                        {'type': 'pm', 'to': 'Bob', 'text': 'x' * 4097}):
            with self.subTest(command=command['type']):
                with self.assertRaises(ValueError) as rejected:
                    protocol.validate_client_message(command)
                self.assertIn('limit', str(rejected.exception))
                if command['type'] != 'pm':
                    self.assertIn('64-character name limit', str(rejected.exception))
        for text in ('a' * 4096, '🌟' * 4096, '\\"\n' * 1365):
            command = {'type': 'pm', 'to': 'ก' * 64, 'text': text}
            self.assertEqual(protocol.validate_client_message(command), command)
            data = protocol.encode_frame(command)
            self.assertEqual(json.loads(data), command)
            with patch.object(protocol, 'MAX_FRAME_BYTES', len(data) - 1):
                with self.assertRaises(ValueError):
                    protocol.encode_frame(command)

    def test_frame_limit_before_newline(self):
        # A fake recv stream makes the exact allocated/read byte bound observable.
        class Source:
            def __init__(self, data, chunk=4096):
                self.data, self.sizes = data, []
                self.chunk = chunk
            def recv(self, size):
                self.sizes.append(size)
                size = min(size, self.chunk)
                result, self.data = self.data[:size], self.data[size:]
                return result
            def settimeout(self, _timeout):
                pass
        valid = frame({'type': 'pm', 'to': 'B', 'text': '🌟'})
        reader = protocol.FrameReader(Source(valid), max_bytes=len(valid))
        self.assertEqual(json.loads(reader.readline()), json.loads(valid))
        for data in (valid[:-1] + b'x\n', b'x' * 100000, b'x' * (len(valid) - 2) + b'\xf0\x9f\x8c\x9f'):
            source = Source(data)
            reader = protocol.FrameReader(source, max_bytes=len(valid))
            with self.assertRaises(ValueError):
                reader.readline()
            self.assertLessEqual(len(reader.buffer), len(valid))
            self.assertEqual(len(data) - len(source.data), len(valid))
        # A real parser can receive half of a UTF-8 character in either recv.
        reader = protocol.FrameReader(Source(valid + valid, chunk=1), max_bytes=len(valid))
        self.assertEqual(json.loads(reader.readline()), json.loads(valid))
        self.assertEqual(json.loads(reader.readline()), json.loads(valid))
        base = frame({'type': 'login', 'name': 'A', 'padding': ''})
        full = frame({'type': 'login', 'name': 'A', 'padding': 'x' * (65536 - len(base))})
        self.assertEqual(len(full), 65536)
        self.assertEqual(json.loads(protocol.FrameReader(Source(full)).readline())['name'], 'A')
        with self.assertRaises(ValueError):
            protocol.FrameReader(Source(full[:-1] + b'x\n')).readline()

    def test_login_waiting_for_state_lock_cannot_outlive_deadline(self):
        peer = self.start_server(login_timeout=.1)
        raw = self.raw(peer)
        self.wait(lambda: len(peer.sessions) == 1)
        # Simulate delayed admission to the mutation lock without network load.
        with server.lock:
            raw.sendall(frame({'type': 'login', 'name': 'Late'}))
            time.sleep(.15)
        self.assertEqual(raw.recv(1024), b'')
        self.wait(lambda: not peer.readers[0].is_alive())
        self.assertNotIn('Late', server.clients)

    def test_login_deadline_is_absolute(self):
        peer = self.start_server(login_timeout=.18)
        alice, events = self.core(peer, 'Alice')
        idle = self.raw(peer)
        trickle = self.raw(peer)
        retry = self.raw(peer)
        start = time.monotonic()
        retry.sendall(frame({'type': 'login', 'name': 'Alice'}))
        self.assertIn(b'error', retry.recv(1024))
        for _ in range(3):
            trickle.sendall(b' ')
            retry.sendall(frame({'type': 'login', 'name': 'Alice'}))
            time.sleep(.04)
        self.assertEqual(idle.recv(1024), b'')
        self.wait(lambda: len([s for s in peer.sessions if not s.closed.is_set()]) == 1)
        self.assertLess(time.monotonic() - start, .7)
        time.sleep(.08)
        self.assertTrue(alice.pm('Alice', 'still here'))
        events.wait(lambda m: m.get('text') == 'still here')

    def test_session_capacity_and_release(self):
        peer = self.start_server(max_sessions=2, login_timeout=.5)
        alice, events = self.core(peer, 'Alice')
        pending = self.raw(peer)
        self.wait(lambda: len(peer.sessions) == 2)
        denied = self.raw(peer)
        self.assertEqual(denied.recv(1024), b'')
        for payload in (b'{bad}\n', b'x' * 65536):
            pending.sendall(payload)
            self.assertEqual(pending.recv(1024), b'')
            self.wait(lambda: not peer.readers[-1].is_alive())
            pending = self.raw(peer)
            self.wait(lambda: len([s for s in peer.sessions if not s.closed.is_set()]) == 2)
        shutdown(pending)
        self.wait(lambda: not peer.readers[-1].is_alive())
        bob, _ = self.core(peer, 'Bob')
        self.assertTrue(alice.pm('Bob', 'slot reused'))
        events.wait(lambda m: m.get('text') == 'slot reused')

    def test_local_limit_failure_is_boolean_and_keeps_connection(self):
        peer = self.start_server()
        core, events = self.core(peer, 'Alice')
        self.assertIs(core.pm('Alice', 'x' * 4097), False)
        events.wait(lambda m: m['type'] == 'error')
        self.assertTrue(core.pm('Alice', 'after rejection'))
        events.wait(lambda m: m.get('text') == 'after rejection')

    def test_serialized_command_and_forwarded_output_budgets(self):
        peer = self.start_server()
        alice, ae = self.core(peer, 'Alice')
        bob, be = self.core(peer, 'Bob')
        command = {'type': 'pm', 'to': 'Bob', 'text': 'x' * 100}
        budget = len(server.encode(command))
        with patch.object(protocol, 'MAX_FRAME_BYTES', budget):
            self.assertTrue(alice.pm('Bob', command['text']))
            ae.wait(lambda m: m.get('text') == 'Frame limit reached')
            # Characters fit, but escaped non-ASCII bytes exceed the budget.
            self.assertFalse(alice.pm('Bob', '🌟' * 20))
            ae.wait(lambda m: m['type'] == 'error' and 'byte limit' in m['text'])
            bob.pm('Bob', 'barrier')
            be.wait(lambda m: m.get('text') == 'barrier')
            self.assertFalse(any(m.get('text') == command['text'] for m in be.seen))
        alice.pm('Bob', 'fits again')
        be.wait(lambda m: m.get('text') == 'fits again')

    def test_invalid_json_parse_failure_releases_slot_without_thread_error(self):
        peer = self.start_server(max_sessions=2)
        alice, ae = self.core(peer, 'Alice')
        errors = []
        for payload in (b'{' * 2000 + b'\n', b'[' * 32000 + b']' * 32000 + b'\n'):
            count = len(peer.sessions)
            raw = self.raw(peer)
            self.wait(lambda: len(peer.sessions) > count)
            with patch.object(threading, 'excepthook', errors.append):
                raw.sendall(payload)
                self.assertEqual(raw.recv(1024), b'')
                self.wait(lambda: not peer.readers[-1].is_alive())
            self.assertEqual(errors, [])
            alice.pm('Alice', 'unrelated after parse failure')
            ae.wait(lambda m: m.get('text') == 'unrelated after parse failure')
        core, events = self.core(peer, 'replacement')
        core.pm('replacement', 'after parse failure')
        events.wait(lambda m: m.get('text') == 'after parse failure')

    def test_deep_server_json_returns_login_result_and_disconnects_reader(self):
        deep = b'[' * 32000 + b']' * 32000 + b'\n'
        for during_login in (True, False):
            def malicious(conn, _addr, during_login=during_login):
                conn.recv(1024)
                if not during_login:
                    conn.sendall(frame({'type': 'login_ok', 'name': 'A'}))
                try:
                    conn.sendall(deep)
                    conn.recv(1)
                except OSError:
                    pass
            with self.subTest(during_login=during_login), LoopbackServer(malicious) as remote:
                core = ChatCore('127.0.0.1', remote.port)
                result, errors = [], []
                worker = threading.Thread(target=lambda: result.append(core.login('A')), daemon=True)
                try:
                    with patch.object(threading, 'excepthook', errors.append):
                        worker.start()
                        worker.join(1)
                        self.assertFalse(worker.is_alive())
                        self.assertEqual(len(result), 1, 'Login worker must always post a result')
                        self.assertEqual(result[0][0], not during_login)
                        if not during_login:
                            events = Events()
                            core.start(events.put)
                            events.wait(lambda m: m['type'] == 'disconnected')
                            core.receive_thread.join(1)
                            self.assertFalse(core.receive_thread.is_alive())
                            self.assertEqual(events.drain(), [{'type': 'disconnected'}])
                    self.assertEqual(errors, [])
                    self.assertIsNone(core.sock)
                    self.assertIsNone(core.reader)
                finally:
                    core.close()
                    worker.join(1)

    def test_default_admission_cap_counts_pending_and_stop_owns_resources(self):
        peer = self.start_server()
        for _ in range(32):
            self.raw(peer)
        self.wait(lambda: len(peer.sessions) == 32)
        self.assertTrue(all(s.name is None for s in peer.sessions))
        denied = self.raw(peer)
        self.assertEqual(denied.recv(1024), b'')
        self.assertEqual(len(peer.sessions), 32)
        peer.close()  # stop while all 32 readers are pending, without external socket cleanup
        self.assertFalse(server.clients)
        self.assertFalse(server.groups)

    def test_raw_field_rejection_and_inbound_limits_release_resources(self):
        peer = self.start_server(max_sessions=1)
        raw = self.raw(peer)
        raw.sendall(frame({'type': 'login', 'name': 'x' * 65}))
        self.assertIn(b'error', raw.recv(1024))
        self.assertFalse(server.clients)
        raw.sendall(frame({'type': 'login', 'name': 'x' * 64}))
        self.assertIn(b'login_ok', raw.recv(1024))
        raw.sendall(frame({'type': 'pm', 'to': 'x' * 64, 'text': 'x' * 4097}))
        reader = protocol.FrameReader(raw)
        while json.loads(reader.readline()).get('type') != 'error':
            pass
        # An oversized incomplete server frame is bounded on both client paths.
        for during_login in (True, False):
            def oversized(conn, _addr, during_login=during_login):
                conn.recv(1024)
                if not during_login:
                    conn.sendall(frame({'type': 'login_ok', 'name': 'A'}))
                try:
                    conn.sendall(b'x' * 65536)
                    conn.recv(1)
                except OSError:
                    pass
            with LoopbackServer(oversized) as remote:
                core = ChatCore('127.0.0.1', remote.port)
                try:
                    ok, _ = core.login('A')
                    self.assertEqual(ok, not during_login)
                    if ok:
                        events = Events()
                        core.start(events.put)
                        events.wait(lambda m: m['type'] == 'disconnected')
                        core.receive_thread.join(1)
                        self.assertFalse(core.receive_thread.is_alive())
                    self.assertIsNone(core.sock)
                    self.assertIsNone(core.reader)
                finally:
                    core.close()

    def test_queue_and_writer_failure_release_actual_admission_slot(self):
        entered, release = threading.Event(), threading.Event()
        def observe(session):
            raw = session.conn
            class WriterSocket:
                def sendall(self, data):
                    text = json.loads(data).get('text')
                    if text == 'block':
                        entered.set()
                        if not release.wait(2):
                            raise OSError('Test gate expired')
                    if text == 'fail':
                        raise OSError('Controlled write failure')
                    raw.sendall(data)
                def shutdown(self, how):
                    release.set()
                    raw.shutdown(how)
                def close(self):
                    raw.close()
            session.conn = WriterSocket()
        with patch.object(server, 'OUTBOUND_QUEUE_FRAMES', 4):
            peer = self.start_server(max_sessions=2, observe=observe)
            alice, ae = self.core(peer, 'Alice')
            bob, be = self.core(peer, 'Bob')
            # Drain initial state before blocking exactly one writer.
            alice.pm('Alice', 'ready')
            ae.wait(lambda m: m.get('text') == 'ready')
            alice.pm('Alice', 'block')
            self.assertTrue(entered.wait(1))
            old = peer.sessions[0]
            for i in range(5):
                bob.pm('Alice', str(i))
                be.wait(lambda m: m.get('text') == str(i))
                self.assertLessEqual(old.outbound.qsize(), 4)
            ae.wait(lambda m: m['type'] == 'disconnected')
            self.wait(lambda: not peer.readers[0].is_alive())
            self.assertFalse(old.writer.is_alive())
            replacement, re = self.core(peer, 'Alice')
            replacement.pm('Alice', 'fail')
            re.wait(lambda m: m['type'] == 'disconnected')
            self.wait(lambda: not peer.readers[-1].is_alive())
            self.assertFalse(peer.sessions[-1].writer.is_alive())
            replacement, re = self.core(peer, 'Alice')
            replacement.pm('Alice', 'after failures')
            re.wait(lambda m: m.get('text') == 'after failures')

    def test_queue_group_and_state_budgets(self):
        peer = self.start_server()
        alice, ae = self.core(peer, 'Alice')
        bob, be = self.core(peer, 'Bob')
        with patch.object(server, 'MAX_GROUPS', 1):
            self.assertTrue(alice.create_group('room'))
            ae.wait(lambda m: m['type'] == 'groups' and 'room' in m['groups'])
            self.assertTrue(alice.create_group('second'))
            ae.wait(lambda m: m['type'] == 'error')
            self.assertEqual(server.groups, {'room': ['Alice']})
        # Exact projected frame lengths force each mutation to exceed the budget.
        users_size = len(server.encode({'type': 'users', 'users': ['Alice', 'Bob', 'C' * 64]}))
        with patch.object(protocol, 'MAX_FRAME_BYTES', users_size - 1):
            raw = self.raw(peer)
            raw.sendall(frame({'type': 'login', 'name': 'C' * 64}))
            self.assertIn(b'error', raw.recv(1024))
            self.assertNotIn('C' * 64, server.clients)
        size = len(server.encode({'type': 'groups', 'groups': {'room': ['Alice', 'Bob']}}))
        with patch.object(protocol, 'MAX_FRAME_BYTES', size - 1):
            self.assertTrue(bob.join_group('room'))
            be.wait(lambda m: m['type'] == 'error')
            self.assertEqual(server.groups, {'room': ['Alice']})
        size = len(server.encode({'type': 'groups', 'groups': {'room': ['Alice'], 'new': ['Alice']}}))
        with patch.object(protocol, 'MAX_FRAME_BYTES', size - 1):
            self.assertTrue(alice.create_group('new'))
            ae.wait(lambda m: m['type'] == 'error')
            self.assertEqual(server.groups, {'room': ['Alice']})
        self.assertTrue(bob.join_group('room'))
        be.wait(lambda m: m['type'] == 'groups' and m['groups']['room'] == ['Alice', 'Bob'])


if __name__ == '__main__':
    unittest.main()
