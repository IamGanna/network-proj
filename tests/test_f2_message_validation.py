"""F2 raw protocol and real Tk event validation regressions."""
import tkinter as tk
import unittest
from unittest.mock import patch

from chat_core import ChatCore
import client
import server
from tests.support import Events, LoopbackServer, frame


class MessageValidationTests(unittest.TestCase):
    def setUp(self):
        self.assertEqual(server.clients, {})
        self.peer = LoopbackServer(server.handle)
        self.cores = []

    def tearDown(self):
        for core in self.cores:
            core.close()
        self.peer.close()
        self.assertEqual(server.clients, {})
        self.assertEqual(server.groups, {})

    def connect(self, name):
        core = ChatCore('127.0.0.1', self.peer.port)
        self.cores.append(core)
        self.assertEqual(core.login(name), (True, name))
        events = Events()
        core.start(events.put)
        return core, events

    def raw(self, core, message):
        # Deliberately bypass the honest core's outgoing checks.
        core.sock.sendall(frame(message))

    def barrier(self, core, events, marker):
        self.assertTrue(core.pm(core.name, marker))
        events.wait(lambda m: m.get('text') == marker)

    def test_nonstring_pm_and_group_text_rejected(self):
        alice, ae = self.connect('Alice')
        bob, be = self.connect('Bob')
        carol, ce = self.connect('Carol')
        alice.create_group('room')
        be.wait(lambda m: m.get('groups') == {'room': ['Alice']})
        bob.join_group('room')
        ae.wait(lambda m: m.get('groups') == {'room': ['Alice', 'Bob']})
        expected_chats = []
        for kind, target in [('pm', {'to': 'Bob'}), ('group_msg', {'group': 'room'})]:
            for index, text in enumerate([{'bad': 'type'}, ['bad'], None, 42, True]):
                with self.subTest(kind=kind, text=text):
                    self.raw(alice, {'type': kind, **target, 'text': text})
                    ae.wait(lambda m: m['type'] == 'error', timeout=.5)
                    marker = f'{kind} valid {index}'
                    self.raw(alice, {'type': kind, **target, 'text': marker, 'from': 'Carol'})
                    expected = {'type': kind, **target, 'from': 'Alice', 'text': marker}
                    expected_chats.append(expected)
                    self.assertEqual(ae.wait(lambda m: m.get('text') == marker), expected)
                    self.assertEqual(be.wait(lambda m: m.get('text') == marker), expected)
        for core, events in [(alice, ae), (bob, be), (carol, ce)]:
            self.barrier(core, events, core.name + ' barrier')
            self.assertFalse(any(m['type'] in ('pm', 'group_msg') and not isinstance(m['text'], str)
                                 for m in events.seen))
        for events in (ae, be):
            received = [m for m in events.seen if m['type'] in ('pm', 'group_msg')
                        and not m['text'].endswith(' barrier')]
            self.assertEqual(received, expected_chats)  # also rejects stringified poison
        self.assertFalse(any(m['type'] == 'group_msg' or m.get('to') == 'Bob' for m in ce.seen))

    def test_bad_envelopes_and_targets_are_controlled(self):
        alice, ae = self.connect('Alice')
        bob, be = self.connect('Bob')
        alice.create_group('private')
        be.wait(lambda m: m.get('groups') == {'private': ['Alice']})
        bad = [None, [], 7, True, 'pm', {}, {'type': []}, {'type': 'unknown'},
               {'type': 'pm', 'to': 'Alice'}, {'type': 'pm', 'to': [], 'text': 'bad'},
               {'type': 'create_group', 'group': {}}, {'type': 'join_group', 'group': []},
               {'type': 'group_msg', 'group': {}, 'text': 'bad'},
               {'type': 'group_msg', 'group': 'private', 'text': None}]
        for index, message in enumerate(bad):
            with self.subTest(message=message):
                self.raw(bob, message)
                be.wait(lambda m: m['type'] == 'error', timeout=.5)
                self.barrier(bob, be, f'following {index}')
        self.raw(bob, {'type': 'group_msg', 'group': 'private', 'text': 'outsider', 'from': 'Alice'})
        be.wait(lambda m: m['type'] == 'error')
        self.raw(bob, {'type': 'join_group', 'group': 'private', 'name': 'Alice'})
        ae.wait(lambda m: m.get('groups') == {'private': ['Alice', 'Bob']})
        self.barrier(alice, ae, 'alice barrier')
        self.assertFalse(any(m.get('text') in ('bad', 'outsider') for m in ae.seen))

    def test_bad_login_names_are_not_coerced(self):
        import socket
        with socket.create_connection(('127.0.0.1', self.peer.port), timeout=1) as sock:
            with sock.makefile('r', encoding='utf-8') as reader:
                import json
                for name in [None, {}, [], 7, True, '   ']:
                    sock.sendall(frame({'type': 'login', 'name': name}))
                    reply = json.loads(reader.readline())
                    self.assertEqual(reply['type'], 'error')
                sock.sendall(frame({'type': 'login', 'name': ' Alice '}))
                self.assertEqual(json.loads(reader.readline()), {'type': 'login_ok', 'name': 'Alice'})

    def test_malformed_json_cleans_up_only_sender(self):
        alice, ae = self.connect('Alice')
        bob, be = self.connect('Bob')
        alice.create_group('room')
        be.wait(lambda m: m.get('groups') == {'room': ['Alice']})
        bob.join_group('room')
        ae.wait(lambda m: m.get('groups') == {'room': ['Alice', 'Bob']})
        alice.sock.sendall(b'{malformed json\n')
        ae.wait(lambda m: m['type'] == 'disconnected')
        be.wait(lambda m: m.get('groups') == {'room': ['Bob']})
        self.barrier(bob, be, 'still connected')
        replacement, re = self.connect('Alice')
        re.wait(lambda m: m.get('groups') == {'room': ['Bob']})


class SchemaContractTests(unittest.TestCase):
    def test_validators_preserve_valid_fields_and_reject_bad_nested_metadata(self):
        from protocol import validate_client_message, validate_server_event
        commands = [
            {'type': 'login', 'name': 'Alice'},
            {'type': 'pm', 'to': 'Bob', 'text': 'hello\nไทย'},
            {'type': 'create_group', 'group': 'room', 'members': ['Bob']},
            {'type': 'join_group', 'group': 'room'},
            {'type': 'group_msg', 'group': 'room', 'text': ''},
        ]
        events = [
            {'type': 'login_ok', 'name': 'Alice'},
            {'type': 'users', 'users': ['Alice', 'Bob']},
            {'type': 'groups', 'groups': {'room': ['Alice']}},
            {'type': 'pm', 'from': 'Alice', 'to': 'Bob', 'text': 'hello'},
            {'type': 'group_msg', 'from': 'Alice', 'group': 'room', 'text': ''},
            {'type': 'error', 'text': 'controlled error'},
            {'type': 'disconnected'},
        ]
        for validate, messages in [(validate_client_message, commands), (validate_server_event, events)]:
            for message in messages:
                self.assertEqual(validate(message), message)
                for field, value in message.items():
                    if isinstance(value, str):
                        for bad in [None, [], {}, 42, True]:
                            with self.subTest(message=message, field=field, bad=bad):
                                with self.assertRaises(ValueError):
                                    validate({**message, field: bad})
        for message in [
            {'type': 'users'}, {'type': 'users', 'users': 'Alice'},
            {'type': 'users', 'users': ['']}, {'type': 'groups'},
            {'type': 'groups', 'groups': []},
            {'type': 'groups', 'groups': {'': []}},
            {'type': 'groups', 'groups': {'room': [False]}},
            {'type': 'groups', 'groups': {'room': {'Alice': True}}},
        ]:
            with self.subTest(message=message):
                with self.assertRaises(ValueError):
                    validate_server_event(message)


class EventValidationTests(unittest.TestCase):
    def ui(self):
        root = tk.Tk()
        root.withdraw()
        app = client.ChatClient(root)
        app.name = 'Alice'
        app.login_frame.destroy()
        app.build_ui()
        self.addCleanup(app.close)
        return root, app

    def test_bad_event_does_not_stop_poll(self):
        root, app = self.ui()
        bad = [{'type': 'pm', 'from': 'Bob', 'to': 'Alice', 'text': {'bad': 'type'}},
               None, [], 42, {'type': []}, {}, {'type': 'unknown'},
               {'type': 'groups', 'groups': {'room': [None]}},
               {'type': 'groups', 'groups': {'room': 'Alice'}},
               {'type': 'users', 'users': ['Alice', {}]},
               {'type': 'group_msg', 'group': [], 'from': 'Bob', 'text': 'bad'},
               {'type': 'error', 'text': []}]
        for event in bad:
            app.inbox.put(event)
        app.inbox.put({'type': 'pm', 'from': 'Bob', 'to': 'Alice', 'text': 'valid ไทย'})
        app.inbox.put({'type': 'users', 'users': ['Alice', 'Bob']})
        app.inbox.put({'type': 'groups', 'groups': {'room': ['Alice', 'Bob']}})
        app.inbox.put({'type': 'group_msg', 'group': 'room', 'from': 'Bob', 'text': 'group valid'})
        app.poll()
        self.assertTrue(app.inbox.empty())
        self.assertEqual(app.users, ['Alice', 'Bob'])
        self.assertEqual(app.groups, {'room': ['Alice', 'Bob']})
        self.assertIn('valid ไทย', app.rooms[('pm', 'Bob')].get('1.0', 'end'))
        self.assertIn('group valid', app.rooms[('group', 'room')].get('1.0', 'end'))
        self.assertNotIn('bad', app.rooms[('pm', 'Bob')].get('1.0', 'end'))
        self.assertIn(app.poll_job, root.tk.call('after', 'info'))
        self.assertIn('Invalid server event', app.error_bar.cget('text'))

    def test_unexpected_ui_programming_error_remains_visible(self):
        _root, app = self.ui()
        app.inbox.put({'type': 'users', 'users': ['Alice']})
        with patch.object(app, 'render_sidebar', side_effect=RuntimeError('programming bug')):
            with self.assertRaisesRegex(RuntimeError, 'programming bug'):
                app.poll()


if __name__ == '__main__':
    unittest.main()
