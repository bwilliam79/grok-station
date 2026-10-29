#!/usr/bin/env python3
"""0.13.7 Homebridge Voice names (Noor SPEC gs-hb-alias rev1.1).

No clash: Pick saves with the Homebridge names and the stepper drops "Name".
A clash: Voice names shows a fix card per clashing row and "Rename others".
The save payload ({uid, name, aliases}) and the server clash check are unchanged.
"""
import json
import os
import subprocess
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "tests"))
import test_gs130_integrations_api as base  # noqa: E402

ga = base.ga
JS = open(os.path.join(ROOT, "admin", "web", "app.js"), encoding="utf-8").read()
CSS = open(os.path.join(ROOT, "admin", "web", "admin.css"), encoding="utf-8").read()

PURE = ("spokenKey", "sourceLabel", "voiceClash", "voiceRowClash", "voiceRowLabels", "voiceRowEntries",
        "voiceClashes", "voiceClashBody", "voiceRoomSuggestion", "voiceSuggestions", "voiceFootState",
        "voiceFootHtml", "voiceTypeWord", "voiceFixCardHtml", "voiceFixTitle", "voiceDisclosureHtml",
        "voiceRowHtml", "voiceChipHits", "voiceEditHtml", "voiceAddChips", "hbFooterState", "hbPickGate",
        "hbPickFootHtml")


def _fn(name, src=JS):
    start = src.index("function " + name + "(")
    depth, i = 0, src.index("{", start)
    while True:
        c = src[i]
        depth += (c == "{") - (c == "}")
        i += 1
        if depth == 0:
            return src[start:i]


def _const(name):
    at = JS.index("const " + name + " = ")
    return JS[at:JS.index(";\n", at) + 1]


def _run(expr):
    consts = ("esc", "SPOKEN_RESERVED", "HB_TYPE_LABEL", "VOICE_READY_BODY", "VOICE_MAX_CHIPS")
    src = "\n".join([_const(c) for c in consts] + [_fn(n) for n in PURE])
    out = subprocess.run(["node", "-e", src + "\nprocess.stdout.write(JSON.stringify(" + expr + "));"],
                         capture_output=True, text=True, check=True)
    return json.loads(out.stdout)


CTX = '{bots: ["Max"], reserved: SPOKEN_RESERVED, saved: [{name: "Desk Lamp", src: "wled"}]}'


class ClashRules(unittest.TestCase):
    def test_no_clash(self):
        self.assertEqual(_run('voiceClashes([{name: "Porch Light"}, {name: "Fan"}], %s)' % CTX), {})

    def test_bot_reserved_device_and_empty(self):
        self.assertEqual(_run('voiceClash("max", %s)' % CTX)["kind"], "bot")
        self.assertEqual(_run('voiceClash("Stop", %s)' % CTX)["kind"], "reserved")
        hit = _run('voiceClash("desk lamp", %s)' % CTX)
        self.assertEqual((hit["kind"], hit["other"], hit["src"]), ("device", "Desk Lamp", "wled"))
        self.assertEqual(_run('voiceClash("  ", %s)' % CTX)["kind"], "empty")

    def test_batch_flags_only_the_later_row(self):
        got = _run('voiceClashes([{name: "Lamp"}, {name: "lamp"}, {name: "Fan"}], %s)' % CTX)
        self.assertEqual(list(got), ["1"])
        self.assertEqual(got["1"]["kind"], "batch")

    def test_edited_name_and_aliases_count(self):
        self.assertEqual(_run('voiceClashes([{name: "Max", _name: "Porch"}], %s)' % CTX), {})
        got = _run('voiceClashes([{name: "Porch", _aliases: ["Max"]}], %s)' % CTX)
        self.assertEqual(got["0"]["kind"], "bot")
        got = _run('voiceClashes([{name: "Porch", _aliases: ["Fan"]}, {name: "fan"}], %s)' % CTX)
        self.assertEqual(list(got), ["1"])

    def test_missing_earlier_is_safe(self):
        self.assertIsNone(_run('voiceRowClash({name: "Fan", _aliases: ["Porch"]}, %s)' % CTX))

    def test_bodies_exact_rev12(self):
        self.assertEqual(_run('voiceClashBody({kind: "device", other: "Desk Lamp", src: "wled"})'),
                         'It sounds the same as "Desk Lamp" (WLED) when you say it, so Grok can\'t tell them apart.')
        self.assertEqual(_run('voiceClashBody({kind: "bot", other: "Max"})'),
                         'It sounds the same as your bot "Max" when you say it, so Grok can\'t tell them apart.')
        self.assertEqual(_run('voiceClashBody({kind: "reserved", other: "stop"})'),
                         'Grok already uses "stop" as a command word, so it can\'t be a device name.')
        self.assertEqual(_run('voiceClashBody({kind: "batch", other: "Lamp"})'),
                         'It sounds the same as "Lamp", another device you\'re adding now.')
        self.assertEqual(_run('voiceClashBody(null)'), "")
        page = JS[JS.index("function voiceClashBody("):JS.index("function voiceRoomSuggestion(")]
        self.assertNotIn("above", page.split("\n", 2)[2])
        self.assertNotIn("below", page)

    def test_batch_names_the_other_device_even_for_an_extra_name(self):
        got = _run('voiceClashes([{name: "Porch", _aliases: ["Fan"]}, {name: "fan"}], %s)' % CTX)
        self.assertEqual(got["1"], {"kind": "batch", "other": "Porch"})

    def test_room_suggestion_rule(self):
        self.assertEqual(_run('voiceRoomSuggestion("Bar lights")'), "Bar room lights")
        self.assertEqual(_run('voiceRoomSuggestion("Desk Light")'), "Desk room light")
        self.assertEqual(_run('voiceRoomSuggestion("Lights")'), "room lights")
        self.assertEqual(_run('voiceRoomSuggestion("Maple")'), "Maple room")
        self.assertEqual(_run('voiceRoomSuggestion("Lights by door")'), "Lights by door room")

    def test_suggestion_hidden_when_it_clashes(self):
        self.assertEqual(_run('voiceSuggestions("Maple", () => false)'), ["Maple room"])
        self.assertEqual(_run('voiceSuggestions("Maple", () => true)'), [])
        html = _run('voiceFixCardHtml({name: "Max"}, 0, {kind: "bot", other: "Max"}, [])')
        self.assertNotIn("Try:", html)


class Footer(unittest.TestCase):
    def test_fix_count_blocks_save(self):
        st = _run("voiceFootState(2, 5)")
        self.assertEqual((st["summary"], st["disabled"]), ("2 names to fix", True))
        html = _run("voiceFootHtml(voiceFootState(1, 5))")
        self.assertIn("1 name to fix", html)
        self.assertIn('disabled aria-describedby="vnh"', html)
        self.assertIn(">Save devices</button>", html)

    def test_ready(self):
        st = _run("voiceFootState(0, 1)")
        self.assertEqual((st["summary"], st["disabled"]), ("1 name ready", False))

    def test_pick_label(self):
        self.assertEqual(_run('hbFooterState(2, "ok", "Save devices")')["label"], "Save devices")
        self.assertEqual(_run('hbFooterState(2, "ok")')["label"], "Continue")


class FixedCard(unittest.TestCase):
    def test_ready_state_in_place(self):
        html = _run('voiceFixCardHtml({name: "Max", _name: "Porch Max"}, 2, null, [])')
        self.assertIn('class="vnfix ok"', html)
        self.assertIn('data-i="2"', html)
        self.assertIn("\u2713", html)
        self.assertNotIn("\u26a0", html)
        self.assertIn("&quot;Porch Max&quot; is ready", html)
        self.assertIn("Sounds different from your other devices now.", html)
        self.assertIn('id="vnf2" data-fix="2"', html)  # still editable
        self.assertNotIn("aria-invalid", html)

    def test_live_update_keeps_card_and_updates_footer(self):
        fn = _fn("hbVoiceNames")
        self.assertIn('card.classList.toggle("ok", !bad)', fn)
        self.assertIn("voiceFixTitle(rows[i], bad)", fn)
        self.assertIn("VOICE_READY_BODY", fn)
        self.assertIn("drawFoot(hits)", fn)
        self.assertEqual(_run("voiceFootState(0, 3)")["disabled"], False)

    def test_ready_card_hides_try_line_at_once(self):
        html = _run('voiceFixCardHtml({name: "Maple", _name: "Maple room"}, 0, null, ["Maple room"])')
        self.assertNotIn("Try:", html)
        self.assertNotIn("vntry", html)
        fn = _fn("hbVoiceNames")
        live = fn[fn.index('card.classList.toggle("ok", !bad)'):fn.index("drawFoot(hits);", fn.index('card.classList.toggle("ok", !bad)'))]
        self.assertIn('card.querySelector(".vntry")', live)
        self.assertIn("tryLine.hidden = !bad", live)

    def test_css(self):
        self.assertIn(".vnfix.ok{border-color:rgba(63,211,182,.4)}", CSS)
        self.assertIn(".vnfix.ok .vnwarn{color:#3FD3B6}", CSS)


class Chips(unittest.TestCase):
    def test_add_by_comma_and_limit(self):
        self.assertEqual(_run('voiceAddChips([], "Porch, front ,")'), ["Porch", "front"])
        self.assertEqual(_run('voiceAddChips(["a", "b"], "c,d,e")'), ["a", "b", "c"])

    def test_remove_label_and_limit_in_markup(self):
        html = _run('voiceEditHtml({name: "Fan", type: "fan"}, 1, {name: "Fan", aliases: ["Ceiling"], adding: true}, null, [null])')
        self.assertIn('aria-label="Remove &quot;Ceiling&quot;"', html)
        self.assertIn('id="vnadd"', html)
        full = _run('voiceEditHtml({name: "Fan"}, 1, {name: "Fan", aliases: ["a", "b", "c"], adding: true}, null, [null, null, null])')
        self.assertNotIn('id="vnadd"', full)
        self.assertNotIn("+ Add another", full)

    def test_each_chip_is_clash_checked(self):
        hits = _run('voiceChipHits({name: "Fan", aliases: ["Ceiling", "max", "ceiling"]}, %s, [])' % CTX)
        self.assertIsNone(hits[0])
        self.assertEqual(hits[1]["kind"], "bot")
        self.assertEqual(hits[2]["kind"], "batch")
        html = _run('voiceEditHtml({name: "Fan"}, 1, {name: "Fan", aliases: ["max"], adding: false}, null, [{kind: "bot", other: "Max"}])')
        self.assertIn('class="vnchip bad"', html)
        self.assertIn('id="vnce1"', html)
        self.assertIn('your bot &quot;Max&quot;', html)
        self.assertIn('id="vndone" disabled aria-describedby="vnce1"', html)

    def test_enter_or_comma_adds_backspace_never_deletes(self):
        fn = _fn("hbVoiceNames")
        self.assertIn('t.id === "vnadd" && ev.key === "Enter"', fn)
        self.assertIn('t.value.indexOf(",") >= 0', fn)
        self.assertNotIn("Backspace\"", fn)
        self.assertNotIn("ev.key === \"Backspace\"", fn)


class RowDone(unittest.TestCase):
    def test_done_blocked_with_reason_and_cancel_kept(self):
        html = _run('voiceEditHtml({name: "Fan"}, 4, {name: "Max", aliases: [], adding: false}, {kind: "bot", other: "Max"}, [])')
        self.assertIn('id="vndone" disabled aria-describedby="vnee4"', html)
        self.assertIn('id="vnee4"', html)
        self.assertIn('id="vncancel">Cancel</button>', html)
        ok = _run('voiceEditHtml({name: "Fan"}, 4, {name: "Porch fan", aliases: [], adding: false}, null, [])')
        self.assertIn('id="vndone">Done</button>', ok)

    def test_cancel_reverts(self):
        fn = _fn("hbVoiceNames")
        cancel = fn[fn.index('b.id === "vncancel"'):fn.index('b.id === "vndone"')]
        self.assertIn("draft = null", cancel)
        self.assertNotIn("rows[i]._name", cancel)


class ServerClash(unittest.TestCase):
    def test_batch_409_flags_later_rows_and_names_the_first(self):
        fn = _fn("serverFlag")
        self.assertIn('hit.with === "device" && !saved && match.length > 1', fn)
        self.assertIn('match.slice(1).forEach(i => { flagged[i] = {kind: "batch", other: voiceRowLabels(first)[0]}; })', fn)


class PickLoading(unittest.TestCase):
    def test_gate_only_the_button(self):
        st = _run('hbPickGate(hbFooterState(2, "ok", "Continue"), "loading")')
        self.assertEqual((st["disabled"], st["busy"], st["label"], st["summary"]), (True, True, "Checking names\u2026", "2 devices selected"))
        html = _run('hbPickFootHtml(hbPickGate(hbFooterState(2, "ok"), "loading"))')
        self.assertIn('<span class="vnspin" aria-hidden="true"></span>Checking names\u2026', html)
        self.assertIn('aria-busy="true"', html)
        self.assertEqual(_run('hbPickGate(hbFooterState(2, "ok", "Save devices"), "ok")')["label"], "Save devices")
        self.assertEqual(_run('hbPickGate(hbFooterState(0, "ok"), "loading")')["label"], "Continue")

    def test_pick_not_held_and_failure_goes_to_voice_names(self):
        load = _fn("hbLoadPick")
        self.assertNotIn("await loadNames()", load)
        self.assertIn("loadNames().then(", load)
        self.assertIn('hbNamesState = ok ? "ok" : "failed"', load)
        pick = _fn("hbPick")
        self.assertIn('hbNamesState === "ok" && !Object.keys(clashes).length', pick)
        self.assertIn("hbPickRedraw = () => draw()", pick)
        self.assertIn('hbNamesState === "failed") return hbVoiceNames(rows, backFn, clashes)', _fn("hbNames"))

    def test_spinner_css_and_reduced_motion(self):
        self.assertIn(".vnspin{display:inline-block;width:16px;height:16px", CSS)
        self.assertIn('@media (prefers-reduced-motion:reduce){.vnspin{', CSS)
        self.assertIn('.vnspin::before{content:"\\00b7\\00b7\\00b7"}', CSS)


class Markup(unittest.TestCase):
    def test_fix_card(self):
        html = _run('voiceFixCardHtml({name: "Max"}, 0, {kind: "bot", other: "Max"}, ["Max room"])')
        self.assertIn("Max needs a new name", html)
        self.assertIn('class="vnfix"', html)
        self.assertIn('aria-invalid="true"', html)
        self.assertIn("Try:", html)
        self.assertIn('data-v="Max room"', html)

    def test_fix_card_escapes(self):
        html = _run('voiceFixCardHtml({name: "<b>x"}, 0, {kind: "bot", other: "<i>"}, [])')
        self.assertNotIn("<b>x", html)
        self.assertNotIn("<i>", html)

    def test_disclosure_and_rows(self):
        html = _run('voiceDisclosureHtml("Rename others", 3, false)')
        self.assertIn('aria-expanded="false"', html)
        self.assertIn("3", html)
        self.assertIn("Rename", _run('voiceRowHtml({name: "Fan", type: "fan"}, 2)'))


class PageWiring(unittest.TestCase):
    def test_old_name_step_is_gone(self):
        self.assertNotIn('hbHead(6, "Name them")', JS)
        self.assertIn('head(3, "Name them")', JS)  # WLED keeps its own step
        self.assertNotIn("async function hbNames", JS)

    def test_pick_skips_name_without_clash(self):
        pick = _fn("hbPick")
        self.assertIn("voiceClashes(picked, voiceCtx())", pick)
        self.assertIn('noName ? "Save devices" : "Continue"', pick)
        self.assertIn("hbStepper(5, noName)", pick)
        names = _fn("hbNames")
        self.assertIn("hbVoiceNames(rows, backFn, clashes)", names)
        self.assertIn("hbSaveRows(rows, btn, reopen, true)", names)
        self.assertIn('hbHead(7, "All set", noName)', _fn("hbDone"))

    def test_payload_unchanged(self):
        save = _fn("hbSaveRows")
        self.assertIn("uid: row.uid", save)
        self.assertIn('"/api/integrations/homebridge/devices"', save)
        self.assertIn('r.data.error === "clash"', save)

    def test_css_targets(self):
        for rule in (".vnfix{", ".vnin.bad{", ".vntry{", ".vndh{", ".vnrow{", ".vnedit{", ".vnchip{", ".vnx{", ".vnerr{"):
            self.assertIn(rule, CSS)
        self.assertIn("border-left:4px solid #F5C04A", CSS)
        self.assertIn("min-height:68px", CSS)
        self.assertIn(".vnx{width:44px;height:44px", CSS)


def setUpModule():
    base.setUpModule()


def tearDownModule():
    base.tearDownModule()


class SaveAPI(unittest.TestCase):
    """Server side of the redesign: same payload, clash names the occupied label."""

    setUp = base.IntegrationsAPI.setUp
    tearDown = base.IntegrationsAPI.tearDown

    def _post(self, devices):
        return self.c.req("POST", "/api/integrations/homebridge/devices", {"devices": devices, "password": base.GOOD})

    def test_bot_clash_409(self):
        json.dump({"personas": [{"slug": "max", "name": "Max", "aliases": []}]}, open(self.personas, "w"))
        status, body = self._post([{"uid": "light-1", "name": "Max", "aliases": []}])
        self.assertEqual((status, body), (409, {"error": "clash", "with": "bot", "name": "Max"}))

    def test_alias_clash_409(self):
        json.dump({"personas": [{"slug": "max", "name": "Max", "aliases": []}]}, open(self.personas, "w"))
        status, body = self._post([{"uid": "light-1", "name": "Porch", "aliases": ["max"]}])
        self.assertEqual(status, 409)
        self.assertEqual(body["with"], "bot")

    def test_batch_clash_409_names_the_earlier_label(self):
        status, body = self._post([{"uid": "light-1", "name": "Lamp", "aliases": []},
                                   {"uid": "light-2", "name": "lamp", "aliases": []}])
        self.assertEqual((status, body["with"], body["name"]), (409, "device", "Lamp"))

    def test_shape_still_strict(self):
        status, _ = self._post([{"uid": "light-1", "name": "Porch"}])
        self.assertEqual(status, 400)


if __name__ == "__main__":
    unittest.main()
