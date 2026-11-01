"""0.13.15: a refused /voice/reply logs its reason code (never a key, token or body)."""
import asyncio, os, sys, tempfile, unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "relay"))
from aiohttp.test_utils import make_mocked_request  # noqa: E402


class RejectLog(unittest.TestCase):
    def _run(self, rej):
        import botreply, relay as r
        lines = []
        req = make_mocked_request("POST", "/voice/reply", headers={"Content-Length": "2"})
        async def read():
            return b"{}"
        req.read = read
        tmp = tempfile.mkdtemp()
        with mock.patch.object(r, "log", lambda *a: lines.append(" ".join(str(x) for x in a))), \
             mock.patch.object(botreply, "load_or_create_station", lambda *_: object()), \
             mock.patch.object(botreply, "load_keys", lambda *_: {}), \
             mock.patch.object(botreply, "ingest", mock.Mock(side_effect=rej)), \
             mock.patch.object(r, "audit_reply", lambda *a: None), \
             mock.patch.object(r, "known_bot_slugs", lambda: set()), \
             mock.patch.object(r, "_pending_doc", lambda rid: None), \
             mock.patch.object(r, "bot_keys_path", lambda: os.path.join(tmp, "k")):
            resp = asyncio.run(r.voice_reply(req))
        return resp, lines

    def test_403_logs_reason(self):
        import botreply
        resp, lines = self._run(botreply.Reject(403, "request", "rhea", "vs_lre2m9jw2T9zKGrdkdFDIx"))
        self.assertEqual(resp.status, 403)
        hit = [l for l in lines if "reply 403" in l]
        self.assertEqual(len(hit), 1, lines)
        self.assertIn("reason=request", hit[0])
        self.assertIn("agent=rhea", hit[0])
        self.assertIn("rid=vs_lre2m9jw2T9zKGrdkdFDIx", hit[0])

    def test_bad_fields_are_not_logged_raw(self):
        import botreply
        resp, lines = self._run(botreply.Reject(403, "Bearer sk-SECRET value", "x" * 80, "not-a-rid"))
        hit = [l for l in lines if "reply 403" in l]
        self.assertEqual(len(hit), 1, lines)
        self.assertNotIn("SECRET", hit[0])
        self.assertIn("reason=error agent=- rid=-", hit[0])


if __name__ == "__main__":
    unittest.main()
