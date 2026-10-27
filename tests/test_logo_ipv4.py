"""0.12.6: logo downloads pin IPv4 when the answer has one, and skips log a reason (never the URL)."""
import os, sys, tempfile, unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "app"))
import cards

URL = "https://a.espncdn.com/i/teamlogos/ncaa/500/8.png"


class LogoPin(unittest.TestCase):
    def test_ipv4_preferred_over_ipv6_first(self):
        got = cards.pin_ip("a.espncdn.com", resolve=lambda h: ["2600:1404:ec00:4a::17dc:6697", "23.47.48.88"])
        self.assertEqual(got, "23.47.48.88")

    def test_ipv6_only_still_pins(self):
        self.assertEqual(cards.pin_ip("x.example", resolve=lambda h: ["2600:1404:ec00:4a::1"]), "2600:1404:ec00:4a::1")

    def test_any_private_answer_still_refuses(self):
        self.assertIsNone(cards.pin_ip("x.example", resolve=lambda h: ["23.47.48.88", "10.0.0.5"]))

    def _cache(self, hosts, opener):
        logs = []
        c = cards.LogoCache(tempfile.mkdtemp(), hosts, opener=opener, resolve=lambda h: True, log=logs.append)
        return c, logs

    def test_skip_reasons_are_logged_without_url(self):
        def boom(url):
            raise OSError("no route")
        c, logs = self._cache(["a.espncdn.com"], boom)
        self.assertIsNone(c.fetch(URL))
        c2, logs2 = self._cache([], boom)
        self.assertIsNone(c2.id_for(URL))
        self.assertEqual(logs, ["logo skipped: download failed (OSError)"])
        self.assertEqual(logs2, ["logo skipped: no logo hosts"])
        for line in logs + logs2:
            self.assertNotIn("espncdn", line)
            self.assertNotIn("http", line)


if __name__ == "__main__":
    unittest.main()
