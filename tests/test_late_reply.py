#!/usr/bin/env python3
"""Late teammate answers: prefix, queue, drop, and a request id that does not match."""
import json, os, shutil, stat, sys, tempfile, time, unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "relay"))
import late_answer  # noqa: E402


def _relay():
    import relay
    return relay


class Prefix(unittest.TestCase):
    def test_calendar_prefix(self):
        line = late_answer.late_line("Rhea", "what's on my calendar tomorrow", "Dinner is at 6.")
        self.assertEqual(line, "Rhea, about your calendar: Dinner is at 6.")

    def test_known_topics_and_a_fallback(self):
        self.assertEqual(late_answer.late_topic("show the score"), "score")
        self.assertEqual(late_answer.late_topic("add a note about milk"), "note")
        self.assertEqual(late_answer.late_topic("read my notes"), "notes")
        self.assertEqual(late_answer.late_topic("tell me a joke"), "joke")
        self.assertEqual(late_answer.late_topic("hey"), "question")

    def test_decide_speaks_queues_and_drops(self):
        now, hard = 1000.0, 1100.0
        self.assertEqual(late_answer.late_decide(now, hard, False, now + 120), "speak")
        self.assertEqual(late_answer.late_decide(now, hard, True, now + 120), "queue")
        self.assertEqual(late_answer.late_decide(hard + 1, hard, False, hard + 120), "drop")
        self.assertEqual(late_answer.late_decide(now + 121, hard, True, now + 120), "drop")
        self.assertEqual(late_answer.late_decide(now + 121, hard, False, now + 120), "drop")

    def test_windows_are_about_three_minutes_and_eight_seconds(self):
        self.assertEqual(late_answer.ANSWER_BY_DEFAULT, 180)
        self.assertEqual(late_answer.CHECKING_AFTER_DEFAULT, 8)
        self.assertEqual(late_answer.LATE_QUEUE_DEFAULT, 120)
        self.assertEqual(late_answer.CHECKING_LINE, "Checking, one moment.")
        src = open(os.path.join(os.path.dirname(_relay().__file__), "relay.py")).read()
        self.assertNotIn("STILL_AFTER", src)
        self.assertIn('E("ANSWER_BY", str(botreply.REPLY_BY_S))', src)
        self.assertIn('E("CHECKING_AFTER", "8")', src)
        self.assertIn('os.path.join(STATE_DIR, "late")', src)
        self.assertNotIn('os.path.join(STATE_DIR, "voice", "late")', src)
        self.assertNotIn("_emit", src)
        wake = open(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                                 "wake", "grok_wake.py")).read()
        self.assertIn('"/var/lib/grok-station/late"', wake)
        self.assertNotIn("voice/late", wake)


class Parked(unittest.TestCase):
    def setUp(self):
        self.relay = _relay()
        self.old_sink = self.relay._LATE_SINK
        self.old_late = dict(self.relay._LATE)
        self.old_busy = self.relay.stats["busy"]
        self.spoken = []
        self.relay._LATE.clear()
        self.relay._LATE_SINK = self.spoken.append
        self.relay.stats["busy"] = 0

    def tearDown(self):
        self.relay._LATE_SINK = self.old_sink
        self.relay._LATE.clear()
        self.relay._LATE.update(self.old_late)
        self.relay.stats["busy"] = self.old_busy

    def _park(self, rid="vs_" + "a" * 22, question="what's on my calendar", nonce="nonce-1", hard=None):
        self.relay._park_late(rid, "rhea", question, nonce, time.time() + 180 if hard is None else hard)
        self.relay._LATE[rid]["name"] = "Rhea"
        return rid

    def test_idle_speaks_with_the_bot_name(self):
        rid = self._park()
        self.assertTrue(self.relay.late_accept(rid, "Dinner is at 6.", "nonce-1"))
        self.assertEqual(self.spoken[0]["text"], "Rhea, about your calendar: Dinner is at 6.")
        self.assertNotIn(rid, self.relay._LATE)

    def test_a_different_request_id_is_not_spoken(self):
        rid = self._park()
        self.assertFalse(self.relay.late_accept("vs_" + "b" * 22, "Dinner is at 6.", "nonce-1"))
        self.assertEqual(self.spoken, [])
        self.assertIn(rid, self.relay._LATE)

    def test_a_wrong_nonce_is_dropped(self):
        rid = self._park()
        self.assertTrue(self.relay.late_accept(rid, "Dinner is at 6.", "other-nonce"))
        self.assertEqual(self.spoken, [])
        self.assertNotIn(rid, self.relay._LATE)

    def test_busy_queues_until_idle_and_drops_after_two_minutes(self):
        rid = self._park()
        self.relay.stats["busy"] = 1
        self.assertTrue(self.relay.late_accept(rid, "Dinner is at 6.", "nonce-1"))
        self.assertEqual(self.spoken, [])
        self.assertIn(rid, self.relay._LATE)
        self.relay.stats["busy"] = 0
        self.relay._flush_late()
        self.assertEqual(len(self.spoken), 1)
        self.assertNotIn(rid, self.relay._LATE)

        rid = self._park(rid="vs_" + "c" * 22)
        self.relay.stats["busy"] = 1
        self.assertTrue(self.relay.late_accept(rid, "Lunch is at noon.", "nonce-1"))
        self.relay._LATE[rid]["queue_until"] = time.time() - 1
        self.relay._flush_late()
        self.assertEqual(len(self.spoken), 1)
        self.assertNotIn(rid, self.relay._LATE)

    def test_past_the_hard_limit_drops(self):
        rid = self._park(hard=time.time() - 1)
        self.assertTrue(self.relay.late_accept(rid, "too late", "nonce-1"))
        self.assertEqual(self.spoken, [])
        self.assertNotIn(rid, self.relay._LATE)

    def test_a_card_for_that_bot_waits_for_the_answer(self):
        rid = self._park()
        show = {"bot": "rhea", "op": "put", "id": "cal"}
        self.assertTrue(self.relay.late_hold_show(show))
        self.assertFalse(self.relay.late_hold_show({"bot": "hank", "op": "put", "id": "x"}))
        self.relay.late_accept(rid, "Dinner is at 6.", "nonce-1")
        self.assertEqual(self.spoken[0]["card"]["id"], "cal")
        self.assertFalse(self.relay.late_hold_show(show))

    def test_job_file_is_group_readable_only(self):
        d = tempfile.mkdtemp()
        old = os.environ.get("GROK_LATE_DIR")
        os.environ["GROK_LATE_DIR"] = d
        try:
            self.assertFalse(self.relay._write_late_job("../etc/passwd", "Hi", "Rhea", b""))
            self.assertEqual(os.listdir(d), [])
            rid = "vs_" + "d" * 22
            self.assertTrue(self.relay._write_late_job(rid, "Dinner is at 6.", "Rhea", b"\x00\x01"))
            for name in (rid + ".json", rid + ".pcm"):
                mode = os.stat(os.path.join(d, name)).st_mode
                self.assertEqual(stat.S_IMODE(mode) & 0o007, 0)  # not world-readable
                self.assertTrue(mode & stat.S_IRGRP)
            doc = json.load(open(os.path.join(d, rid + ".json")))
            self.assertEqual((doc["text"], doc["who"], doc["pcm"]), ("Dinner is at 6.", "Rhea", rid + ".pcm"))
            self.assertEqual(open(os.path.join(d, rid + ".pcm"), "rb").read(), b"\x00\x01")
        finally:
            if old is None:
                os.environ.pop("GROK_LATE_DIR", None)
            else:
                os.environ["GROK_LATE_DIR"] = old
            shutil.rmtree(d, ignore_errors=True)
