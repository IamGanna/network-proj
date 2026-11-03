"""F7 ordered output and lifecycle checks; localhost is not a physical demo."""
import json
import socket
import threading
import unittest

from chat_core import ChatCore
import server
from tests.support import Events, LoopbackServer
from tests.support import frame


class OwnedLock:
    """Real mutex with ownership observation for the socket boundary checks."""
    def __init__(self):
        self.mutex = threading.Lock()
        self.owner = None

    def __enter__(self):
        self.mutex.acquire()
        self.owner = threading.get_ident()
        return self

    def __exit__(self, *args):
        self.owner = None
        self.mutex.release()


class WriteGate:
    """Control only socket I/O; retain real handlers, queues and TCP readers."""
    def __init__(self):
        self.entered = threading.Event()
        self.release = threading.Event()
        self.lock_violations = []

    def check_lock(self, operation):
        if server.lock.owner == threading.get_ident():
            self.lock_violations.append(operation)

    def wrap(self, sock):
        gate = self

        class Socket:
            blocked = False

            def __getattr__(self, name):
                return getattr(sock, name)

            def sendall(self, data):
                gate.check_lock('sendall')
                message = json.loads(data)
                if message.get('text') in ('hold writer', 'hold then fail writer'):
                    self.blocked = True
                    gate.entered.set()
                    if not gate.release.wait(3):
                        raise OSError('Test writer gate expired')
                if message.get('text') in ('fail writer', 'hold then fail writer'):
                    raise OSError('Controlled socket write failure')
                sock.sendall(data)

            def shutdown(self, how):
                gate.check_lock('shutdown')
                if self.blocked:
                    gate.release.set()
                sock.shutdown(how)

            def close(self):
                gate.check_lock('close')
                sock.close()

        return Socket()


class ServerWriteTests(unittest.TestCase):
    def setUp(self):
        self.assertEqual(server.clients, {})
        self.assertEqual(server.groups, {})
        self.original_lock = server.lock
        self.addCleanup(setattr, server, 'lock', self.original_lock)
        server.lock = OwnedLock()
        self.gate = WriteGate()
        self.peer = LoopbackServer(lambda sock, addr: server.handle(self.gate.wrap(sock), addr))
        self.cores = []

    def tearDown(self):
        self.gate.release.set()
        for core in self.cores:
            core.close()
        self.peer.close()
        self.assertEqual(self.gate.lock_violations, [])
        self.assertEqual(server.clients, {})
        self.assertEqual(server.groups, {})

    def connect(self, name):
        core = ChatCore('127.0.0.1', self.peer.port)
        self.cores.append(core)
        self.assertEqual(core.login(name), (True, name))
        events = Events()
        core.start(events.put)
        return core, events

    def barrier(self, core, events, marker):
        self.assertTrue(core.pm(core.name, marker))
        return events.wait(lambda m: m.get('text') == marker, timeout=1)

    def test_blocked_writer_does_not_hold_state_lock(self):
        alice, ae = self.connect('Alice')
        bob, be = self.connect('Bob')
        self.barrier(alice, ae, 'Alice ready')
        self.barrier(bob, be, 'Bob ready')
        self.assertTrue(alice.pm('Alice', 'hold writer'))
        self.assertTrue(self.gate.entered.wait(1), 'Alice writer did not reach gate')
        self.assertTrue(bob.create_group('unrelated'))
        be.wait(lambda m: m.get('groups') == {'unrelated': ['Bob']}, timeout=.5)
        self.barrier(bob, be, 'unrelated work finished')

    def test_frames_do_not_interleave_and_state_does_not_go_backwards(self):
        alice, ae = self.connect('Alice')
        bob, be = self.connect('Bob')
        carol, ce = self.connect('Carol')
        for core, events in [(alice, ae), (bob, be), (carol, ce)]:
            self.barrier(core, events, core.name + ' ready')
        alice.pm('Alice', 'hold writer')
        self.assertTrue(self.gate.entered.wait(1))
        bob.create_group('room')
        be.wait(lambda m: m.get('groups') == {'room': ['Bob']})
        alice.join_group('room')
        be.wait(lambda m: m.get('groups') == {'room': ['Bob', 'Alice']})

        start = threading.Barrier(3)
        def create():
            start.wait(1)
            for i in range(4):
                bob.create_group(f'room {i}')
            self.barrier(bob, be, 'all groups created')

        def private():
            start.wait(1)
            for i in range(8):
                carol.pm('Alice', f'concurrent ไทย {i}\nline')
            self.barrier(carol, ce, 'all PMs sent')

        failures = []
        def run(fn):
            try:
                fn()
            except BaseException as exc:
                failures.append(exc)
        workers = [threading.Thread(target=run, args=(fn,)) for fn in (create, private)]
        for worker in workers:
            worker.start()
        start.wait(1)
        for worker in workers:
            worker.join(2)
            self.assertFalse(worker.is_alive())
        self.assertEqual(failures, [])
        self.gate.release.set()
        self.barrier(alice, ae, 'all Alice frames received')
        snapshots = [m['groups'] for m in ae.seen if m['type'] == 'groups' and m['groups']]
        self.assertEqual(snapshots[0], {'room': ['Bob']})
        self.assertEqual(snapshots[1], {'room': ['Bob', 'Alice']})
        self.assertEqual([len(s) for s in snapshots], [1, 1, 2, 3, 4, 5])
        self.assertEqual([m['text'] for m in ae.seen if m.get('from') == 'Carol'],
                         [f'concurrent ไทย {i}\nline' for i in range(8)])

    def test_login_ok_precedes_state_on_wire(self):
        alice, ae = self.connect('Alice')
        alice.create_group('room')
        ae.wait(lambda m: m.get('groups') == {'room': ['Alice']})
        with socket.create_connection(('127.0.0.1', self.peer.port), timeout=1) as sock:
            with sock.makefile('r', encoding='utf-8') as reader:
                sock.sendall(frame({'type': 'login', 'name': 'Bob'}))
                self.assertEqual(json.loads(reader.readline()), {'type': 'login_ok', 'name': 'Bob'})
                self.assertEqual(json.loads(reader.readline()), {'type': 'users', 'users': ['Alice', 'Bob']})
                self.assertEqual(json.loads(reader.readline()), {'type': 'groups', 'groups': {'room': ['Alice']}})

    def joined_pair(self):
        alice, ae = self.connect('Alice')
        bob, be = self.connect('Bob')
        alice.create_group('room')
        be.wait(lambda m: m.get('groups') == {'room': ['Alice']})
        bob.join_group('room')
        ae.wait(lambda m: m.get('groups') == {'room': ['Alice', 'Bob']})
        self.barrier(bob, be, 'joined')
        with server.lock:
            old_session = server.clients['Alice']
        return alice, ae, bob, be, old_session

    def test_failed_writer_cleanup_is_idempotent(self):
        alice, ae, bob, be, old_session = self.joined_pair()
        alice.pm('Alice', 'fail writer')
        ae.wait(lambda m: m['type'] == 'disconnected')
        be.wait(lambda m: m.get('groups') == {'room': ['Bob']})
        replacement, re = self.connect('Alice')
        replacement.join_group('room')
        re.wait(lambda m: m.get('groups') == {'room': ['Bob', 'Alice']})
        # Delayed reader cleanup and another failure must not delete reused identity.
        closers = [threading.Thread(target=old_session.close) for _ in range(3)]
        for closer in closers:
            closer.start()
        for closer in closers:
            closer.join(1)
            self.assertFalse(closer.is_alive())
        self.assertFalse(old_session.writer.is_alive())
        self.barrier(replacement, re, 'replacement lives')
        self.barrier(bob, be, 'Bob lives')
        with server.lock:
            self.assertIsNot(server.clients['Alice'], old_session)
            self.assertEqual(server.groups, {'room': ['Bob', 'Alice']})

    def test_queue_overflow_disconnects_only_slow_session(self):
        alice, ae, bob, be, old_session = self.joined_pair()
        self.barrier(alice, ae, 'ready to block')
        alice.pm('Alice', 'hold writer')
        self.assertTrue(self.gate.entered.wait(1))
        for i in range(server.OUTBOUND_QUEUE_FRAMES + 1):
            marker = f'queue item {i}'
            self.assertTrue(bob.pm('Alice', marker))
            be.wait(lambda m: m.get('text') == marker)
        ae.wait(lambda m: m['type'] == 'disconnected')
        be.wait(lambda m: m.get('groups') == {'room': ['Bob']})
        self.barrier(bob, be, 'healthy after overflow')
        old_session.close()
        self.assertFalse(old_session.writer.is_alive())
        self.assertLessEqual(old_session.outbound.qsize(), server.OUTBOUND_QUEUE_FRAMES)

    def test_reader_and_writer_failures_race_without_duplicate_removal(self):
        alice, ae, bob, be, old_session = self.joined_pair()
        alice.pm('Alice', 'hold then fail writer')
        self.assertTrue(self.gate.entered.wait(1))
        # Reader detects malformed JSON while writer is inside controlled I/O.
        # Reader shutdown releases the writer, which raises its own write error.
        alice.sock.sendall(b'{malformed json\n')
        ae.wait(lambda m: m['type'] == 'disconnected')
        be.wait(lambda m: m.get('groups') == {'room': ['Bob']})
        old_session.close()
        self.assertFalse(old_session.writer.is_alive())
        replacement, re = self.connect('Alice')
        self.barrier(replacement, re, 'replacement after race')
        self.barrier(bob, be, 'Bob after race')
        removals = [m for m in be.seen if m.get('groups') == {'room': ['Bob']}]
        # One removal plus the expected fresh-login snapshot; no double cleanup.
        self.assertEqual(len(removals), 2)

    def test_duplicate_login_and_join_send_disconnect_races(self):
        bob, be = self.connect('Bob')
        bob.create_group('room')
        be.wait(lambda m: m.get('groups') == {'room': ['Bob']})
        candidates = [ChatCore('127.0.0.1', self.peer.port) for _ in range(2)]
        self.cores.extend(candidates)
        start = threading.Barrier(3)
        results = [None, None]
        def login(index):
            start.wait(1)
            results[index] = candidates[index].login('Alice')
        workers = [threading.Thread(target=login, args=(i,)) for i in range(2)]
        for worker in workers:
            worker.start()
        start.wait(1)
        for worker in workers:
            worker.join(2)
            self.assertFalse(worker.is_alive())
        self.assertEqual(sorted(r[0] for r in results), [False, True])
        winner = next(i for i, result in enumerate(results) if result[0])
        alice, loser = candidates[winner], candidates[1-winner]
        ae, ce = Events(), Events()
        alice.start(ae.put)
        self.assertEqual(loser.login('Carol'), (True, 'Carol'))
        loser.start(ce.put)
        start = threading.Barrier(3)
        def join():
            start.wait(1)
            alice.join_group('room')
            alice.join_group('room')
        def send():
            start.wait(1)
            bob.group_msg('room', 'racing join')
        workers = [threading.Thread(target=fn) for fn in (join, send)]
        for worker in workers:
            worker.start()
        start.wait(1)
        for worker in workers:
            worker.join(1)
            self.assertFalse(worker.is_alive())
        ae.wait(lambda m: m.get('groups') == {'room': ['Bob', 'Alice']})
        bob.group_msg('room', 'after join')
        ae.wait(lambda m: m.get('text') == 'after join')
        self.barrier(alice, ae, 'join barrier')
        self.barrier(bob, be, 'send barrier')
        self.barrier(loser, ce, 'outsider barrier')
        self.assertFalse(any(m['type'] == 'group_msg' for m in ce.seen))
        # A message racing Join may precede membership; when delivered its state
        # snapshot must already establish membership on that connection.
        joined_at = next(i for i, m in enumerate(ae.seen) if m.get('groups') == {'room': ['Bob', 'Alice']})
        for i, message in enumerate(ae.seen):
            if message.get('text') in ('racing join', 'after join'):
                self.assertGreater(i, joined_at)
        start = threading.Barrier(3)
        def disconnect():
            start.wait(1)
            alice.close()
        def send_during_disconnect():
            start.wait(1)
            bob.group_msg('room', 'racing disconnect')
        workers = [threading.Thread(target=fn) for fn in (disconnect, send_during_disconnect)]
        for worker in workers:
            worker.start()
        start.wait(1)
        for worker in workers:
            worker.join(2)
            self.assertFalse(worker.is_alive())
        be.wait(lambda m: m.get('groups') == {'room': ['Bob']})
        self.barrier(bob, be, 'disconnect barrier')
        bob.group_msg('room', 'after disconnect')
        be.wait(lambda m: m.get('text') == 'after disconnect')


if __name__ == '__main__':
    unittest.main()
