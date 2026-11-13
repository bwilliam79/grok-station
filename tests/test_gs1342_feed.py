"""0.13.42: the update feed survives a restart, and a bot's latest update does not age out."""
import json
import os
import stat
import sys
import tempfile
import threading
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "app"))
import cards  # noqa: E402


def _row(kind="Release", line="Shipped", when=1000, typ="update"):
    row = {"type": typ, "line": line, "time": when}
    if typ == "update":
        row["kind"] = kind
        row["detail"] = "Ready"
    return row


class FeedPersistence(unittest.TestCase):
    def test_survives_restart(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "update-feed.json")
            try:
                feed = cards.UpdateFeed(path=path)
            except TypeError:
                self.fail("UpdateFeed does not load a state file")
            feed.put("otto", "a", _row())
            mode = stat.S_IMODE(os.stat(path).st_mode)
            self.assertEqual(mode, 0o600)
            again = cards.UpdateFeed(path=path)
            self.assertEqual(len(again.items), 1)
            self.assertEqual(again.seq, feed.seq)
            self.assertEqual(again.rules, feed.rules)
            again.put("otto", "b", _row(kind="Release", line="Again", when=2000))
            third = cards.UpdateFeed(path=path)
            self.assertEqual(len(third.items), 1)
            self.assertEqual(next(iter(third.items.values()))["line"], "Again")

    def test_latest_update_persists_past_24_hours(self):
        feed = cards.UpdateFeed()
        feed.put("otto", "a", _row(when=1000))
        feed.expire(1000 + 86400 + 10)
        self.assertEqual(len(feed.items), 1)
        feed.put("ivy", "c", _row(kind="Mail", line="Hello", when=1000))
        feed.expire(1000 + 86400 * 3)
        bots = {row["bot"] for row in feed.items.values()}
        self.assertEqual(bots, {"otto", "ivy"})

    def test_corrupt_and_oversized_files_are_ignored(self):
        with tempfile.TemporaryDirectory() as d:
            bad = os.path.join(d, "bad.json")
            with open(bad, "w") as fh:
                fh.write("{")
            try:
                feed = cards.UpdateFeed(path=bad)
            except (TypeError, ValueError, json.JSONDecodeError):
                self.fail("a corrupt feed file is not ignored")
            self.assertEqual(feed.items, {})
            huge = os.path.join(d, "huge.json")
            with open(huge, "w") as fh:
                fh.write("{" + ("x" * 80000))
            try:
                feed = cards.UpdateFeed(path=huge)
            except TypeError:
                self.fail("an oversized feed file is not ignored")
            self.assertEqual(feed.items, {})
            self.assertTrue(open(huge).read().startswith("{"))

    def test_heads_up_and_done_still_expire(self):
        feed = cards.UpdateFeed()
        feed.put("otto", "d", {"type": "done", "line": "Finished", "time": 1000})
        feed.put("otto", "h", {"type": "heads_up", "line": "Soon", "time": 1000, "expires_at": 1500})
        feed.put("otto", "w", {"type": "working_on", "line": "On it", "time": 1000})
        feed.put("otto", "n", {"type": "needs_you", "line": "Decide", "time": 1000})
        feed.expire(1500)
        kinds = {row["type"] for row in feed.items.values()}
        self.assertNotIn("heads_up", kinds)
        self.assertIn("done", kinds)
        self.assertIn("needs_you", kinds)
        feed.expire(1000 + cards.DROP_S)
        kinds = {row["type"] for row in feed.items.values()}
        self.assertEqual(kinds, {"needs_you"})

    def test_non_finite_and_huge_seq_are_ignored(self):
        """NaN, Infinity, and a seq above 2**53 must not load or be returned."""
        huge = 10 ** 23  # the integer 1e23; JSON 1e23 is a float and was already refused
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "update-feed.json")
            with open(path, "w") as fh:
                fh.write(
                    '{"items":[{"bot":"otto","id":"a","type":"update","line":"Hi",'
                    '"time":NaN,"expires_at":Infinity,"seq":%d}],"rules":{},"seq":%d}'
                    % (huge, huge))
            feed = cards.UpdateFeed(path=path, now=1000)
            needs, rest = feed.ordered(5000)
            self.assertEqual(feed.items, {})
            self.assertEqual(needs + rest, [])
            # The same seq bound, with finite times, so the file is not saved back.
            with open(path, "w") as fh:
                fh.write(
                    '{"items":[{"bot":"otto","id":"a","type":"update","line":"Hi",'
                    '"time":1000,"seq":%d}],"rules":{},"seq":%d}' % (huge, huge))
            again = cards.UpdateFeed(path=path, now=1000)
            self.assertEqual(again.items, {})
            with open(path) as fh:
                self.assertTrue(fh.read().startswith("{"))
            top = os.path.join(d, "top.json")
            with open(top, "w") as fh:
                fh.write(
                    '{"items":[{"bot":"otto","id":"a","type":"update","line":"Hi",'
                    '"time":1000,"seq":1}],"rules":{},"seq":%d}' % (2 ** 53 + 1))
            self.assertEqual(cards.UpdateFeed(path=top, now=1000).items, {})
            ok = os.path.join(d, "ok.json")
            with open(ok, "w") as fh:
                fh.write(
                    '{"items":[{"bot":"otto","id":"a","type":"update","line":"Hi",'
                    '"time":1000,"seq":%d}],"rules":{},"seq":%d}' % (2 ** 53, 2 ** 53))
            kept = cards.UpdateFeed(path=ok, now=1000)
            self.assertEqual(len(kept.items), 1)
            self.assertEqual(kept.seq, 2 ** 53)

    def test_save_holds_the_lock_across_replace(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "update-feed.json")
            feed = cards.UpdateFeed(path=path)
            seen = {}
            real = os.replace

            def spy(src, dst):
                def other():
                    got = feed._lock.acquire(blocking=False)
                    seen["held"] = not got
                    if got:
                        feed._lock.release()
                worker = threading.Thread(target=other)
                worker.start()
                worker.join()
                real(src, dst)

            os.replace = spy
            try:
                feed.put("otto", "a", _row())
            finally:
                os.replace = real
            self.assertTrue(seen.get("held"), seen)
            # Concurrent puts and ordered() calls keep every row.
            errors = []

            def writer(n):
                try:
                    feed.put("otto", "id%d" % n, _row(kind="K%d" % n, line="Line %d" % n, when=3000 + n))
                except Exception as exc:
                    errors.append(exc)

            def reader():
                try:
                    for _ in range(20):
                        feed.ordered(4000)
                except Exception as exc:
                    errors.append(exc)

            threads = [threading.Thread(target=reader)]
            threads += [threading.Thread(target=writer, args=(n,)) for n in range(12)]
            for th in threads:
                th.start()
            for th in threads:
                th.join()
            self.assertEqual(errors, [])
            again = cards.UpdateFeed(path=path)
            self.assertEqual(set(again.items), set(feed.items))


class FeedBots(unittest.TestCase):
    def test_bots_is_a_locked_snapshot_used_by_updates_only(self):
        os.environ.setdefault("SDL_VIDEODRIVER", "dummy")
        os.environ.setdefault("PYGAME_HIDE_SUPPORT_PROMPT", "1")
        import grok_station as gs
        feed = cards.UpdateFeed()
        feed.put("otto", "a", _row())
        feed.put("ivy", "c", _row(kind="Mail", line="Hello", when=1100))
        self.assertEqual(feed.bots(), {"otto", "ivy"})
        seen = []
        real = feed.bots

        def wrapped():
            seen.append(True)
            return real()

        feed.bots = wrapped
        gs.apply_update({"v": 1, "op": "updates_only", "bots": ["otto"]}, feed)
        self.assertEqual(seen, [True])
        self.assertEqual(feed.bots(), {"otto"})


class FeedKindCap(unittest.TestCase):
    def test_load_caps_kind_at_18_characters(self):
        long_kind = "Calendar-mailbox-extra"
        self.assertGreater(len(long_kind), 18)
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, "update-feed.json")
            doc = {"items": [{
                "bot": "otto", "id": "a", "type": "update", "line": "Shipped",
                "time": 1000, "seq": 1, "kind": long_kind, "detail": "Ready",
            }], "rules": {}, "seq": 1}
            with open(path, "w") as fh:
                json.dump(doc, fh)
            os.chmod(path, 0o600)
            feed = cards.UpdateFeed(path=path)
            self.assertEqual(len(feed.items), 1)
            kind = next(iter(feed.items.values()))["kind"]
            self.assertEqual(kind, long_kind[:18])
            self.assertEqual(len(kind), 18)

    def test_cut_kind_with_a_space_at_18_still_clears(self):
        """The 18th character is a space. The stored kind is cleaned, so a clear matches."""
        raw = "12345678901234567 WXYZ"
        self.assertEqual(len(raw), 22)
        self.assertEqual(raw[17], " ")
        cleaned = cards.escape_text(raw[:18], 18)
        self.assertNotEqual(cleaned, raw[:18])
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, "update-feed.json")
            doc = {"items": [{
                "bot": "otto", "id": "a", "type": "update", "line": "Shipped",
                "time": 1000, "seq": 1, "kind": raw, "detail": "Ready",
            }], "rules": {}, "seq": 1}
            with open(path, "w") as fh:
                json.dump(doc, fh)
            os.chmod(path, 0o600)
            feed = cards.UpdateFeed(path=path)
            self.assertEqual(next(iter(feed.items.values()))["kind"], cleaned)
            feed.clear_type_updates("otto", kind=cleaned)
            self.assertEqual(feed.items, {})


if __name__ == "__main__":
    unittest.main()
