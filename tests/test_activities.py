"""In-chat activities: /coin, /dice, /rng, /poll and /vote."""
import time
import unittest

import activity
import server
from chat_core import ChatCore
from tests.support import Events, LoopbackServer

GROUP = ("group", "room")


class ActivityRuleTests(unittest.TestCase):
    def setUp(self):
        activity.polls.clear()

    def test_random_results_stay_in_range(self):
        coins = {activity.apply("A", GROUP, "/coin").rsplit(" ", 1)[1] for _ in range(200)}
        dice = {int(activity.apply("A", GROUP, "/dice").rsplit(" ", 1)[1]) for _ in range(300)}
        numbers = {int(activity.apply("A", GROUP, "/rng 3 5").rsplit(" ", 1)[1]) for _ in range(200)}
        self.assertEqual(coins, {"Heads", "Tails"})
        self.assertEqual(dice, {1, 2, 3, 4, 5, 6})
        self.assertEqual(numbers, {3, 4, 5})
        self.assertTrue(1 <= int(activity.apply("A", GROUP, "/RNG").rsplit(" ", 1)[1]) <= 100)
        for bad in ("/rng 5 2", "/rng 5", "/rng a b", "/rng 1 99999999999999"):
            self.assertTrue(activity.apply("A", GROUP, bad).startswith("⚠️"), bad)

    def test_other_text_is_left_alone(self):
        for text in ("hello", "/unknown thing", "coin", " /coinflip"):
            self.assertEqual(activity.apply("A", GROUP, text), text)

    def test_poll_counts_one_vote_per_person(self):
        created = activity.apply("A", GROUP, "/poll Lunch? | Pizza | Sushi")
        poll_id = int(created.split("#")[1].split()[0])
        self.assertIn("1. Pizza: 0 votes", created)
        activity.apply("A", GROUP, f"/vote {poll_id} 1")
        activity.apply("B", GROUP, f"/vote {poll_id} 1")
        result = activity.apply("A", GROUP, f"/vote {poll_id} 2")  # A changes their vote
        self.assertIn("1. Pizza: 1 vote\n", result)
        self.assertIn("2. Sushi: 1 vote", result)

    def test_bad_polls_and_votes_are_refused(self):
        for bad in ("/poll", "/poll Q | only one", "/poll | a | b", "/poll Q" + " | x" * 11,
                    "/poll " + "q" * 201 + " | a | b", "/poll Q | a | " + "b" * 101):
            self.assertTrue(activity.apply("A", GROUP, bad).startswith("⚠️"), bad[:30])
        poll_id = int(activity.apply("A", GROUP, "/poll Q | a | b").split("#")[1].split()[0])
        for bad in (f"/vote {poll_id} 3", f"/vote {poll_id} 0", f"/vote {poll_id + 1} 1", "/vote x 1", "/vote 1"):
            self.assertTrue(activity.apply("A", GROUP, bad).startswith("⚠️"), bad)
        elsewhere = activity.apply("A", ("group", "other"), f"/vote {poll_id} 1")
        self.assertIn("no poll", elsewhere)
        self.assertEqual(activity.polls[poll_id]["votes"], {})

    def test_spamming_polls_in_one_chat_keeps_other_chats_polls(self):
        other = ("group", "other")
        kept = int(activity.apply("B", other, "/poll Keep me? | a | b").split("#")[1].split()[0])
        for _ in range(activity.MAX_POLLS + 5):
            activity.apply("A", GROUP, "/poll Spam | a | b")
        self.assertIn(kept, activity.polls)
        self.assertEqual(sum(poll["room"] == GROUP for poll in activity.polls.values()), activity.MAX_POLLS_PER_ROOM)
        self.assertIn("1 vote", activity.apply("B", other, f"/vote {kept} 1"))


class ActivityRoutingTests(unittest.TestCase):
    def setUp(self):
        self.assertEqual(server.clients, {})
        activity.polls.clear()
        self.peer = LoopbackServer(server.handle)
        self.cores = []

    def tearDown(self):
        for core in self.cores:
            core.close()
        self.peer.close()
        self.assertEqual(server.clients, {})

    def connect(self, name):
        core = ChatCore("127.0.0.1", self.peer.port)
        self.cores.append(core)
        self.assertEqual(core.login(name), (True, name))
        events = Events()
        core.start(events.put)
        return core, events

    def test_group_sees_one_result_and_outsiders_cannot_vote(self):
        alice, ae = self.connect("Alice")
        bob, be = self.connect("Bob")
        carol, ce = self.connect("Carol")
        alice.create_group("room")
        be.wait(lambda m: "room" in (m.get("groups") or {}))
        bob.join_group("room")
        ae.wait(lambda m: m.get("groups", {}).get("room") == ["Alice", "Bob"])

        alice.group_msg("room", "/dice")
        seen_by_alice = ae.wait(lambda m: m.get("type") == "group_msg")
        seen_by_bob = be.wait(lambda m: m.get("type") == "group_msg")
        self.assertEqual(seen_by_alice, seen_by_bob)
        self.assertTrue(seen_by_alice["text"].startswith("🎲 rolled a die: "))

        alice.group_msg("room", "/poll Lunch? | Pizza | Sushi")
        poll = be.wait(lambda m: m.get("text", "").startswith("📊"))
        poll_id = poll["text"].split("#")[1].split()[0]
        carol.group_msg("room", f"/vote {poll_id} 1")  # not a member
        ce.wait(lambda m: m["type"] == "error")
        with server.lock:
            server.group_timeouts["room"]["Bob"] = time.time() + 60
        bob.group_msg("room", f"/vote {poll_id} 1")  # timed out
        be.wait(lambda m: m["type"] == "error")
        alice.group_msg("room", f"/vote {poll_id} 2")
        result = ae.wait(lambda m: m.get("text", "").startswith("🗳️"))
        self.assertIn("1. Pizza: 0 votes", result["text"])
        self.assertIn("2. Sushi: 1 vote", result["text"])
        carol.pm("Carol", "barrier")
        ce.wait(lambda m: m.get("text") == "barrier")
        self.assertFalse(any(m.get("type") == "group_msg" for m in ce.seen))

    def test_recreated_group_does_not_inherit_old_polls(self):
        alice, ae = self.connect("Alice")
        alice.create_group("room")
        ae.wait(lambda m: "room" in (m.get("groups") or {}))
        alice.group_msg("room", "/poll Old? | a | b")
        poll_id = ae.wait(lambda m: m.get("text", "").startswith("📊"))["text"].split("#")[1].split()[0]
        bob, be = self.connect("Bob")
        alice.close()  # last member leaves, so the group is deleted
        be.wait(lambda m: m.get("groups") == {})
        bob.create_group("room")
        be.wait(lambda m: "room" in (m.get("groups") or {}))
        bob.group_msg("room", f"/vote {poll_id} 1")
        self.assertIn("no poll", be.wait(lambda m: m.get("type") == "group_msg")["text"])
        self.assertEqual(activity.polls, {})

    def test_private_chat_poll_is_shared_by_both_people_only(self):
        alice, ae = self.connect("Alice")
        bob, be = self.connect("Bob")
        carol, ce = self.connect("Carol")
        alice.pm("Bob", "/poll Movie? | A | B")
        poll_id = be.wait(lambda m: m.get("text", "").startswith("📊"))["text"].split("#")[1].split()[0]
        bob.pm("Alice", f"/vote {poll_id} 2")  # Bob replies in the same private chat
        self.assertIn("2. B: 1 vote", ae.wait(lambda m: m.get("text", "").startswith("🗳️"))["text"])
        carol.pm("Alice", f"/vote {poll_id} 1")  # a different private chat
        self.assertIn("no poll", ae.wait(lambda m: m.get("from") == "Carol")["text"])
