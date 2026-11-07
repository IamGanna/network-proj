import unittest
from ai_history import SessionHistory, HistoryError


class HistoryTests(unittest.TestCase):
    def test_room_and_session_isolation_immutable_and_forget(self):
        a, b = SessionHistory(), SessionHistory()
        a.record(('pm','Bob'), 'Alice', 'hello')
        snap = a.snapshot(('pm','Bob'))
        a.record(('pm','Bob'), 'Bob', 'สวัสดี')
        a.record(('group','Bob'), 'Alice', 'group only')
        self.assertEqual(len(snap.messages), 1)
        self.assertEqual(len(a.snapshot(('pm','Bob')).messages), 2)
        self.assertEqual(a.snapshot(('group','Bob')).messages[0].seq, 3)
        with self.assertRaises(HistoryError): b.snapshot(('pm','Bob'))
        a.forget_room(('pm','Bob'))
        with self.assertRaises(HistoryError): a.snapshot(('pm','Bob'))
        self.assertEqual(len(a.snapshot(('group','Bob')).messages), 1)

    def test_count_overflow_latches_and_disables_all_history(self):
        history = SessionHistory(max_messages=2)
        for text in ('one','two'): history.record(('pm','B'), 'A', text)
        self.assertEqual(len(history.snapshot(('pm','B')).messages), 2)
        history.record(('pm','B'), 'A', 'three')
        self.assertTrue(history.overflowed)
        for room in [('pm','B'), ('group','new')]:
            with self.assertRaises(HistoryError) as e: history.snapshot(room)
            self.assertEqual(e.exception.code, 'history_limit')
        history.clear()
        history.record(('pm','B'), 'A', 'new session')
        self.assertEqual(history.snapshot(('pm','B')).cutoff, 1)

    def test_byte_limit_includes_escaping_and_surrogates(self):
        first = SessionHistory()
        first.record(('pm','B'), 'A', 'ไทย\n\ud800')
        size = first.byte_count
        exact = SessionHistory(max_bytes=size)
        exact.record(('pm','B'), 'A', 'ไทย\n\ud800')
        self.assertFalse(exact.overflowed)
        short = SessionHistory(max_bytes=size-1)
        short.record(('pm','B'), 'A', 'ไทย\n\ud800')
        self.assertTrue(short.overflowed)
