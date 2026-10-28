"""0.12.15 (third pass): no silent drops in helper status.

(1) A failed record write after a bot post is not ignored: the error type is logged once, the record is kept in
    memory (the row still shows it), the banner shows, and the write is retried at most once a minute.
(2) A helpers.json that parses but holds invalid entries keeps the valid ones and logs only a count.

Real handlers and node row render. Names and keys are made up.
"""
import json, os, sys, unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import test_gs1215_helper_failclosed as tfc  # noqa: E402


class _W(tfc._Clocked):
    def _reset(self):
        super()._reset()


class FailedWriteIsNotSilent(_W):
    def test_failed_write_logs_shows_banner_and_retries(self):
        r = self.st.r
        self.add_bot()
        self.assertIs(self.banner(), False)
        real = r.atomic_write
        tries = []

        def failing(path, data, mode=0o600):
            if path == r.HELPER_FILE:
                tries.append(self.t)
                raise PermissionError("read-only")
            return real(path, data, mode)
        r.atomic_write = failing
        self.addCleanup(setattr, r, "atomic_write", real)
        n0 = len(self.st.lines)
        ask = self.wait_hook()
        st, j = self.reply("ivy", ask["request_id"], "Hi, I'm here.", None)  # headerless: must be recorded bare
        self.assertEqual(st, 200, j)
        self.assertTrue(self.unconfirmed("ivy"))  # the record is kept, not lost
        self.assertIs(self.banner(), True)
        logged = "\n".join(self.st.lines[n0:])
        self.assertEqual(logged.count("PermissionError"), 1, logged)
        self.assertNotIn("ivy", logged.lower())
        k = len(tries)
        for _ in range(4):
            self.t += 5
            self.banner()
        self.assertEqual(len(tries), k)  # no retry inside the minute
        r.atomic_write = real
        self.t += 61
        self.assertIs(self.banner(), False)  # retried and saved
        with open(r.HELPER_FILE) as f:
            self.assertIs(json.load(f)["ivy"]["bare"], True)


class InvalidEntriesCounted(_W):
    def test_invalid_entries_logged_as_count_only(self):
        self.run_flow(reply_version=t15_release())
        path = self.st.r.HELPER_FILE
        with open(path) as f:
            doc = json.load(f)
        doc["Bad Name"] = {"header": "1.2.3"}
        doc["zed"] = 5
        with open(path, "w") as f:
            json.dump(doc, f)
        n0 = len(self.st.lines)
        self.assertIs(self.banner(), False)
        self.assertEqual(self.bot()["helper"]["state"], "current")
        logged = "\n".join(self.st.lines[n0:])
        self.assertIn("2 invalid entries ignored", logged)
        self.assertNotIn("zed", logged)
        self.assertNotIn("bad name", logged.lower())
        self.banner()
        self.assertEqual("\n".join(self.st.lines[n0:]).count("invalid entries"), 1)  # not every read


def t15_release():
    return tfc.t15.ga.HELPER_RELEASE


if __name__ == "__main__":
    unittest.main()
