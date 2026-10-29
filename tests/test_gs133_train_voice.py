#!/usr/bin/env python3
"""Hey Grok, train my wake word for <bot> opens the prompt tile and does not record."""
import io
import json
import os
import sys
import tempfile
import unittest
import urllib.error
import urllib.request

# Same import-time env as test_relay so a shared relay module stays usable by later tests.
_TMP = tempfile.mkdtemp()
os.environ.update(RELAY_TOKEN="test-token", XAI_API_KEY="test-key", ALERT_DIR=os.path.join(_TMP, "none"), PERSONAS_FILE=os.path.join(_TMP, "none.json"), NO_SPEECH="1.0")
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "relay"))
import relay  # noqa: E402

# relay reads its env at import. Drop the cached module so later test files import it with their own env.
sys.modules.pop("relay", None)


_BOTS = [
    {"slug": "juniper", "name": "Juniper", "aliases": ["Pip"]},
    {"slug": "otto", "name": "Otto", "aliases": [], "main": True},
    {"slug": "den-laptop", "name": "Den", "aliases": ["Den laptop"]},
    {"slug": "pip2", "name": "Piper", "aliases": []},
]


class TrainPhrase(unittest.TestCase):
    def test_the_sentence_names_the_bot_and_builds_a_legal_phrase(self):
        for said in (
            "Hey Grok, train my wake word for Juniper",
            "hey grok train my wake word for juniper",
            "HEY GROK, TRAIN MY WAKE WORD FOR JUNIPER.",
            "train my wake word for Juniper!",
            "Hey Grok, train my wake word for hey Juniper?",
            "train my wake word for Pip",
        ):
            hit = relay.train_wake_request(said, _BOTS)
            self.assertEqual(hit["slug"], "juniper", said)
            self.assertEqual(hit["name"], "Juniper", said)
            self.assertEqual(hit["phrase"], "hey_juniper", said)
        # "Pip" is also a bot name. The longer alias "Den laptop" still wins over a short name.
        den = relay.train_wake_request("train my wake word for Den laptop", _BOTS)
        self.assertEqual(den["slug"], "den-laptop")
        self.assertEqual(den["phrase"], "hey_den-laptop")
        digit = relay.train_wake_request("train my wake word for pip2", _BOTS)
        self.assertEqual(digit["phrase"], "hey_pip2")
        main = relay.train_wake_request("Hey Grok, train my wake word for the main bot", _BOTS)
        self.assertEqual(main["slug"], "otto")
        self.assertEqual(main["phrase"], "hey_otto")

    def test_grok_opens_without_the_builtin_phrase(self):
        for said in ("train my wake word for Grok", "Hey Grok, train my wake word for hey grok"):
            hit = relay.train_wake_request(said, _BOTS)
            self.assertEqual(hit["slug"], "grok", said)
            self.assertEqual(hit["name"], "Grok", said)
            self.assertIsNone(hit["phrase"], said)

    def test_other_sentences_and_unknown_names_do_not_record(self):
        for said in (
            "what's for dinner",
            "stop",
            "stop the music",
            "Hey Grok, stop",
            "train my wake word",
            "can you train my wake word for Juniper",
            "Hey Grok, connect me with Juniper",
            "",
        ):
            self.assertIsNone(relay.train_wake_request(said, _BOTS), said)
        missing = relay.train_wake_request("train my wake word for Nova", _BOTS)
        self.assertEqual(missing, {"slug": "", "name": "", "phrase": None, "spoken": "Nova"})
        no_main = relay.train_wake_request(
            "train my wake word for the main bot",
            [{"slug": "juniper", "name": "Juniper", "aliases": []}])
        self.assertEqual(no_main["slug"], "")

    def test_hey_grok_is_not_sent_and_extra_keys_are_refused(self):
        seen = []

        def post(body):
            seen.append(dict(body))
            return 200

        code = relay.post_wake_open_tile(
            {"persona": "grok", "phrase": "hey_grok", "name": "Grok"}, post=post)
        self.assertEqual(code, 200)
        self.assertEqual(seen, [{"persona": "grok", "name": "Grok"}])
        self.assertEqual(relay.post_wake_open_tile(
            {"persona": "juniper", "phrase": "hey_juniper", "password": "nope"}, post=post), 400)
        self.assertEqual(len(seen), 1)
        self.assertEqual(relay.post_wake_open_tile({"name": "Juniper"}, post=post), 400)
        self.assertEqual(relay.post_wake_open_tile("hey_juniper", post=post), 400)
        self.assertEqual(len(seen), 1)

        def boom(_body):
            raise OSError("down")

        self.assertEqual(relay.post_wake_open_tile(
            {"persona": "juniper", "phrase": "hey_juniper"}, post=boom), 503)

    def test_the_post_stays_on_loopback_with_proxies_off(self):
        seen = {}

        def build_opener(*handlers):
            seen["handlers"] = handlers

            class Op:
                def open(self, req, timeout=None):
                    seen["url"] = req.full_url
                    seen["timeout"] = timeout
                    seen["body"] = req.data
                    raise urllib.error.URLError("down")

            return Op()

        old = urllib.request.build_opener
        urllib.request.build_opener = build_opener
        try:
            code = relay.post_wake_open_tile(
                {"persona": "juniper", "phrase": "hey_juniper", "name": "Juniper"})
        finally:
            urllib.request.build_opener = old
        self.assertEqual(code, 503)
        self.assertEqual(len(seen["handlers"]), 1)
        self.assertIsInstance(seen["handlers"][0], urllib.request.ProxyHandler)
        self.assertEqual(seen["handlers"][0].proxies, {})
        self.assertTrue(seen["url"].startswith("http://127.0.0.1:"), seen["url"])
        self.assertTrue(seen["url"].endswith("/enroll/open-tile"), seen["url"])
        self.assertEqual(seen["timeout"], 3)
        self.assertEqual(json.loads(seen["body"]),
                         {"persona": "juniper", "phrase": "hey_juniper", "name": "Juniper"})

    def test_a_listener_error_is_its_status_and_a_miss_is_503(self):
        class Busy:
            def open(self, req, timeout=None):
                raise urllib.error.HTTPError(req.full_url, 409, "busy", hdrs=None, fp=io.BytesIO(b"{}"))

        class Down:
            def open(self, req, timeout=None):
                raise TimeoutError("slow")

        body = {"persona": "juniper", "phrase": "hey_juniper", "name": "Juniper"}
        self.assertEqual(relay.post_wake_open_tile(body, opener=Busy()), 409)
        self.assertEqual(relay.post_wake_open_tile(body, opener=Down()), 503)

    def test_open_train_tile_posts_only_a_matched_bot_and_swallows_errors(self):
        posted = []

        def post(body):
            posted.append(dict(body))
            return 200

        hit = relay.train_wake_request("Hey Grok, train my wake word for Juniper", _BOTS)
        self.assertTrue(relay.open_train_tile(hit, post=post))
        self.assertEqual(posted, [{"persona": "juniper", "phrase": "hey_juniper", "name": "Juniper"}])
        grok = relay.train_wake_request("train my wake word for Grok", _BOTS)
        self.assertTrue(relay.open_train_tile(grok, post=post))
        self.assertNotIn("phrase", posted[-1])
        self.assertEqual(posted[-1]["persona"], "grok")
        missing = relay.train_wake_request("train my wake word for Nova", _BOTS)
        self.assertFalse(relay.open_train_tile(missing, post=post))
        self.assertEqual(len(posted), 2)

        def boom(_body):
            raise OSError("down")

        self.assertTrue(relay.open_train_tile(hit, post=boom))

    def test_stop_is_still_first_and_the_train_phrase_never_reaches_the_model(self):
        src = open(relay.__file__).read()
        i_stop = src.find('if session_local_exit(heard, st.get("timers")):')
        i_train = src.find("train = train_wake_request(heard, connect_bots(roster, list_source_slug()))")
        i_recipe = src.find("taken = recipe_taken(heard, post_recipe_cmd)")
        i_heard = src.find('await pi.send_json({"type": "heard", "text": heard})')
        self.assertGreater(i_stop, 0)
        self.assertLess(i_stop, i_train)
        self.assertLess(i_train, i_recipe)
        self.assertLess(i_recipe, i_heard)
        block = src[i_train:src.find('return "user ended"', i_train)]
        self.assertIn("voice_train_plan(", block)
        self.assertIn("play_fixed(plan", block)
        self.assertIn("post_wake_enroll(", block)
        self.assertNotIn("open_train_tile(train)", block)
        self.assertIn('{"type": "done", "local": True}', block)
        self.assertNotIn("hey_", block)


if __name__ == "__main__":
    unittest.main()
