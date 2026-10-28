"""0.12.15: a failed save of a bot's signed update status (updates.json) is not silent.

The status stays in memory, so the Bots page still uses it; the error type is logged once; the banner shows; the
save is retried at most once a minute. Real handlers. Names and keys are made up.
"""
import json, os, sys, unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import test_gs1215_helper_degraded as td  # noqa: E402

ga = td.tfc.t15.ga


class UpdateStatusSaveFails(td._D):
    def test_signed_status_kept_logged_banner_and_retried(self):
        r = self.st.r
        self.st.webhooks.clear()
        self.run_flow(slug="ivy", reply_version=ga.HELPER_RELEASE)
        self.assertIs(self.banner(), False)
        real = r.atomic_write
        tries = []

        def failing(path, data, mode=0o600):
            if path == r.UPDATES_FILE:
                tries.append(self.t)
                raise PermissionError("read-only")
            return real(path, data, mode)
        r.atomic_write = failing
        self.addCleanup(setattr, r, "atomic_write", real)
        n0 = len(self.st.lines)
        self.signed("ivy")
        st, j = self.st.call("GET", "/admin/bots")
        b = next(x for x in j["bots"] if x["slug"] == "ivy")
        self.assertIsNotNone(b["update"])  # still used by the Bots page
        self.assertEqual(b["update"]["helper"], ga.HELPER_RELEASE)
        self.assertIs(self.banner(), True)
        logged = "\n".join(self.st.lines[n0:])
        self.assertEqual(logged.count("PermissionError"), 1, logged)
        self.assertNotIn("ivy", logged.lower())
        k = len(tries)
        for _ in range(3):
            self.t += 5
            self.banner()
        self.assertEqual(len(tries), k)  # no retry inside the minute
        r.atomic_write = real
        self.t += 61
        self.assertIs(self.banner(), False)
        with open(r.UPDATES_FILE) as f:
            self.assertIn("ivy", json.load(f))


if __name__ == "__main__":
    unittest.main()
