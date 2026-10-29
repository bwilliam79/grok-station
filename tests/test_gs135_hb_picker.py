#!/usr/bin/env python3
"""Homebridge pick step: collapsed categories, tri-state select-all, unchanged server list."""
import json
import os
import shutil
import subprocess
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "admin"))
import gs_integrations as gsi  # noqa: E402

JS = open(os.path.join(ROOT, "admin", "web", "app.js"), encoding="utf-8").read()
CSS = open(os.path.join(ROOT, "admin", "web", "admin.css"), encoding="utf-8").read()
ADMIN = open(os.path.join(ROOT, "admin", "grok_admin.py"), encoding="utf-8").read()

FUNCS = (
    "hbCategories", "hbDefaultPicked", "hbEnsurePicked", "hbPickedMap", "hbPickedCount",
    "hbPickKind", "hbTriState", "hbCountText", "hbCountHtml", "hbRoomLine", "hbFooterState",
    "hbPickFootHtml", "hbEmptyHtml", "hbPickNoteHtml", "hbPickLoadingHtml", "hbCbxHtml",
    "hbPickListHtml",
)
WARN = " \u00b7 \u26a0 check for locks or garage doors"


def _fn(name):
    start = JS.index("function " + name + "(")
    depth, i = 0, JS.index("{", start)
    while True:
        c = JS[i]
        depth += (c == "{") - (c == "}")
        i += 1
        if depth == 0:
            return JS[start:i]


def _src():
    esc = JS[JS.index("const esc = "):JS.index(";\n", JS.index("const esc = ")) + 1]
    return esc + "\n" + "\n".join(_fn(n) for n in FUNCS)


def _run(expr):
    out = subprocess.run(
        ["node", "-e", _src() + "\nprocess.stdout.write(JSON.stringify(" + expr + "));"],
        capture_output=True, text=True, check=True)
    return json.loads(out.stdout)


def _svc(typ, uid, name, char, value=0, **extra):
    item = {"type": typ, "uniqueId": uid, "serviceName": name,
            "serviceCharacteristics": [{"type": char, "value": value, "perms": ["pr", "pw"]}]}
    item.update(extra)
    return item


class ServerListUnchanged(unittest.TestCase):
    def test_accessories_stay_typed_and_carry_no_room(self):
        rows = gsi.hb_accessories([
            _svc("Lightbulb", "light-1", "Porch Light", "On", room="Front door"),
            _svc("Switch", "sw-1", "Hall Switch", "On", room="Hall"),
            _svc("Outlet", "out-1", "Coffee Maker", "On"),
            _svc("Fan", "fan-1", "Bedroom Fan", "Active"),
            _svc("Fanv2", "fan-2", "Desk Fan", "On"),
            _svc("Television", "tv-1", "Living Room TV", "Active"),
            _svc("Camera", "cam-1", "Driveway", "On"),
            _svc("LockMechanism", "lock-1", "Front Door", "On"),
            _svc("GarageDoorOpener", "gar-1", "Garage", "On"),
            _svc("Thermostat", "therm-1", "Hall", "On"),
        ])
        self.assertEqual([r["type"] for r in rows], ["light", "switch", "outlet", "fan", "fan", "tv"])
        self.assertEqual([r["uid"] for r in rows], ["light-1", "sw-1", "out-1", "fan-1", "fan-2", "tv-1"])
        for row in rows:
            self.assertEqual(set(row), {"uid", "name", "type", "char", "value"})
            self.assertNotIn("room", row)
        start = ADMIN.index("def _hb_accessories")
        body = ADMIN[start:ADMIN.index("def _hb_save", start)]
        self.assertIn('{"uid": row["uid"], "name": row["name"], "type": row["type"], "char": row["char"]}', body)
        self.assertNotIn("room", body)

    def test_picker_still_filters_devices_already_on_the_station(self):
        self.assertIn("!have[row.uid] && HB_TYPE[row.type]", JS)
        self.assertIn("rows.filter(row => row && row.uid && !have[row.uid] && HB_TYPE[row.type])", JS)


@unittest.skipUnless(shutil.which("node"), "node not installed")
class PickerLogic(unittest.TestCase):
    def test_categories_split_switch_and_outlet_and_drop_empty(self):
        cats = _run('hbCategories(['
                    '{uid:"o",type:"outlet",name:"Plug"},'
                    '{uid:"s",type:"switch",name:"Hall"},'
                    '{uid:"l",type:"light",name:"Porch"},'
                    '{uid:"x",type:"lock",name:"Front"},'
                    '{uid:"c",type:"camera",name:"Drive"},'
                    '{uid:"f",type:"fan",name:"Bed"},'
                    '{uid:"t",type:"tv",name:"Living"},'
                    '{uid:"l2",type:"light",name:"Desk"},'
                    '{uid:"v",type:"Fanv2",name:"Raw"}'
                    ']).map(c => ({key:c.key, title:c.title, n:c.rows.length, types:c.rows.map(r => r.type)}))')
        self.assertEqual([c["key"] for c in cats], ["light", "switch", "outlet", "fan", "tv"])
        self.assertEqual([c["title"] for c in cats], ["Lights", "Switches", "Outlets", "Fans", "TVs"])
        self.assertEqual(cats[0]["n"], 2)
        self.assertEqual(cats[1]["types"], ["switch"])
        self.assertEqual(cats[2]["types"], ["outlet"])
        self.assertEqual(_run('hbCategories([{uid:"l",type:"light"}]).map(c => c.title)'), ["Lights"])
        self.assertEqual(_run('hbCategories([{uid:"z",type:"thermostat"}])'), [])

    def test_tri_state(self):
        self.assertEqual(_run('hbTriState([{uid:"a"},{uid:"b"}], {})'), "false")
        self.assertEqual(_run('hbTriState([{uid:"a"},{uid:"b"}], {a:true})'), "mixed")
        self.assertEqual(_run('hbTriState([{uid:"a"},{uid:"b"}], {a:true, b:true})'), "true")
        self.assertEqual(_run('hbTriState([], {a:true})'), "false")

    def test_count_copy_and_switch_warning(self):
        lights = '[{uid:"1"},{uid:"2"},{uid:"3"},{uid:"4"},{uid:"5"},{uid:"6"},{uid:"7"},{uid:"8"}]'
        all_on = "{1:true,2:true,3:true,4:true,5:true,6:true,7:true,8:true}"
        self.assertEqual(_run("hbCountText(" + lights + ", " + all_on + ', "light")'), "All 8 selected")
        five = '[{uid:"1"},{uid:"2"},{uid:"3"},{uid:"4"},{uid:"5"}]'
        self.assertEqual(_run("hbCountText(" + five + ", {1:true,2:true,3:true}, \"switch\")"),
                         "3 of 5 selected" + WARN)
        self.assertEqual(_run('hbCountText([{uid:"1"},{uid:"2"}], {}, "fan")'),
                         "2 devices \u00b7 none selected")
        self.assertEqual(_run('hbCountText([{uid:"1"},{uid:"2"}], {}, "switch")'),
                         "2 devices \u00b7 none selected" + WARN)
        self.assertEqual(_run('hbCountText([{uid:"1"}], {}, "tv")'), "1 device \u00b7 not selected")
        self.assertEqual(_run('hbCountText([{uid:"1"},{uid:"2"}], {1:true,2:true}, "switch")'),
                         "All 2 selected" + WARN)
        html = _run('hbCountHtml([{uid:"1"},{uid:"2"}], {1:true}, "switch")')
        self.assertIn("1 of 2 selected", html)
        self.assertIn('<span class="warnl">\u26a0 check for locks or garage doors</span>', html)
        self.assertNotIn("warnl", _run('hbCountHtml([{uid:"1"}], {1:true}, "outlet")'))

    def test_defaults_and_preserved_choice(self):
        self.assertEqual(_run('[hbDefaultPicked({type:"light"}), hbDefaultPicked({type:"switch"}),'
                              ' hbDefaultPicked({type:"outlet"}), hbDefaultPicked({type:"fan"}),'
                              ' hbDefaultPicked({type:"tv"}), hbDefaultPicked({type:"lock"}), hbDefaultPicked(null)]'),
                         [True, True, True, False, False, False, False])
        kept = _run('(() => { const light = {uid:"l", type:"light"}; const fan = {uid:"f", type:"fan", _picked:true};'
                    ' const off = {uid:"s", type:"switch", _picked:false};'
                    ' return [hbEnsurePicked(light), light._picked, hbEnsurePicked(fan), hbEnsurePicked(off), off._picked]; })()')
        self.assertEqual(kept, [True, True, True, False, False])
        sample = ('['
                  + ",".join('{uid:"l%d",type:"light"}' % i for i in range(8)) + ","
                  + ",".join('{uid:"s%d",type:"switch"}' % i for i in range(5)) + ","
                  + ",".join('{uid:"o%d",type:"outlet"}' % i for i in range(4)) + ","
                  + ",".join('{uid:"f%d",type:"fan"}' % i for i in range(2)) + ","
                  + ",".join('{uid:"t%d",type:"tv"}' % i for i in range(2))
                  + ']')
        self.assertEqual(_run("hbPickedCount(" + sample + ")"), 17)
        self.assertEqual(_run("hbFooterState(hbPickedCount(" + sample + '), "ready").summary'), "17 devices selected")

    def test_room_line_only_when_the_server_sent_one(self):
        self.assertEqual(_run('hbRoomLine({name:"Porch"})'), "")
        self.assertEqual(_run('hbRoomLine({room:""})'), "")
        self.assertEqual(_run('hbRoomLine({room:null})'), "")
        self.assertEqual(_run('hbRoomLine({room:12})'), "")
        self.assertEqual(_run('hbRoomLine({room:"Kitchen"})'), "Kitchen")
        bare = _run('hbPickListHtml([{uid:"a",type:"light",name:"Porch",_picked:true},'
                    '{uid:"b",type:"light",name:"Desk",room:"",_picked:true},'
                    '{uid:"c",type:"fan",name:"Fan",room:4,_picked:false}],'
                    '{a:true,b:true,c:false}, {light:true, fan:true})')
        self.assertNotIn('class="rm"', bare)
        self.assertIn('class="nm">Porch</div></div>', bare)
        self.assertNotIn('class="help"', bare)
        roomed = _run('hbPickListHtml([{uid:"a",type:"light",name:"Porch",room:"Front door",_picked:true},'
                      '{uid:"b",type:"light",name:"Desk",_picked:true}], {a:true,b:true}, {light:true})')
        self.assertIn('class="nm">Porch</div><div class="rm">Front door</div>', roomed)
        self.assertIn('class="nm">Desk</div></div>', roomed)
        self.assertNotIn('class="nm">Desk</div><div class="rm"', roomed)

    def test_continue_label_and_disabled_states(self):
        ready = _run('hbPickFootHtml(hbFooterState(17, "ready"))')
        one = _run('hbPickFootHtml(hbFooterState(1, "ready"))')
        none = _run('hbPickFootHtml(hbFooterState(0, "none"))')
        empty = _run('hbPickFootHtml(hbFooterState(0, "empty"))')
        loading = _run('hbPickFootHtml(hbFooterState(0, "loading"))')
        for foot in (ready, one, none, empty, loading):
            self.assertIn(">Continue</button>", foot)
            self.assertNotIn("Next \u00b7", foot)
        self.assertIn("17 devices selected", ready)
        self.assertNotIn("disabled", ready)
        self.assertIn("1 device selected", one)
        self.assertNotIn("disabled", one)
        self.assertIn("No devices selected", none)
        self.assertIn("Pick at least one device to continue.", none)
        self.assertIn("disabled", none)
        self.assertIn('aria-describedby="pkh"', none)
        self.assertIn('role="status"', none)
        self.assertIn("Nothing to pick yet.", empty)
        self.assertIn('aria-describedby="pkh"', empty)
        self.assertIn("disabled", empty)
        self.assertIn("Loading devices\u2026", loading)
        self.assertIn("disabled", loading)
        self.assertNotIn("aria-describedby", loading)
        self.assertNotIn("No devices selected", loading)

    def test_list_markup_collapsed_order_empty_and_loading(self):
        html = _run('hbPickListHtml(['
                    '{uid:"l",type:"light",name:"Porch",_picked:true},'
                    '{uid:"s1",type:"switch",name:"Coffee",room:"Kitchen",_picked:true},'
                    '{uid:"s2",type:"switch",name:"Garage",_picked:false},'
                    '{uid:"o",type:"outlet",name:"Heater",_picked:true},'
                    '{uid:"f",type:"fan",name:"Bed fan",_picked:false}'
                    '], hbPickedMap(['
                    '{uid:"l",type:"light",name:"Porch",_picked:true},'
                    '{uid:"s1",type:"switch",name:"Coffee",room:"Kitchen",_picked:true},'
                    '{uid:"s2",type:"switch",name:"Garage",_picked:false},'
                    '{uid:"o",type:"outlet",name:"Heater",_picked:true},'
                    '{uid:"f",type:"fan",name:"Bed fan",_picked:false}'
                    ']), {})')
        self.assertNotIn("<ul", html)
        self.assertLess(html.index('data-k="all-light"'), html.index('data-k="name-light"'))
        self.assertLess(html.index('data-k="name-light"'), html.index('data-k="exp-light"'))
        self.assertLess(html.index('data-k="exp-light"'), html.index('data-k="all-switch"'))
        self.assertLess(html.index('data-k="exp-switch"'), html.index('data-k="all-outlet"'))
        self.assertIn('aria-expanded="false"', html)
        self.assertIn('aria-label="Show Switches"', html)
        self.assertIn('aria-label="Select all Outlets"', html)
        self.assertIn('aria-checked="true"', html)
        self.assertIn("1 device \u00b7 selected", html)
        self.assertIn("1 of 2 selected", html)
        self.assertIn("aren\u2019t listed", html)
        self.assertNotIn("Garage", html)
        open_html = _run('hbPickListHtml([{uid:"s1",type:"switch",name:"Coffee",room:"Kitchen",_picked:true},'
                         '{uid:"s2",type:"switch",name:"Garage",_picked:false}],'
                         '{s1:true,s2:false}, {switch:true})')
        self.assertIn('aria-expanded="true"', open_html)
        self.assertIn('aria-label="Hide Switches"', open_html)
        self.assertIn('aria-checked="mixed"', open_html)
        self.assertIn('class="cbx some"', open_html)
        self.assertIn('id="d-switch"', open_html)
        self.assertIn('aria-controls="d-switch"', open_html)
        self.assertLess(open_html.index('data-k="exp-switch"'), open_html.index('data-k="one-s1"'))
        self.assertLess(open_html.index('data-k="one-s1"'), open_html.index('data-k="one-s2"'))
        self.assertIn('class="rm">Kitchen</div>', open_html)
        self.assertIn('class="nm">Garage</div></div>', open_html)
        empty = _run("hbEmptyHtml()")
        self.assertIn("No devices to add", empty)
        self.assertIn("Homebridge didn\u2019t share any lights, switches, outlets, fans or TVs that aren\u2019t already on the station.", empty)
        self.assertIn(">Try again</button>", empty)
        self.assertNotIn("tip3", empty)
        loading = _run("hbPickLoadingHtml()")
        self.assertEqual(loading.count("pksk"), 5)
        self.assertIn('aria-busy="true"', loading)
        self.assertIn('role="status"', loading)
        self.assertIn("Getting devices from Homebridge\u2026", loading)
        self.assertEqual(_run('hbPickKind([])'), "empty")
        self.assertEqual(_run('hbPickKind([{uid:"f",type:"fan",_picked:false}])'), "none")

    def test_page_uses_the_helpers_and_drops_the_old_copy(self):
        page_at = JS.index("async function pageIntegrations")
        for name in FUNCS:
            self.assertLess(JS.index("function " + name + "("), page_at, name)
        pick = _fn("hbPick")
        load = _fn("hbLoadPick")
        self.assertIn("hbEnsurePicked", pick)
        self.assertIn("hbPickListHtml(", pick)
        self.assertIn("hbFooterState(", pick)
        self.assertIn("hbNames(rows.filter(row => row && row._picked), () => hbPick(rows, backFn), next)", pick)
        self.assertIn("hbPickLoadingHtml()", load)
        self.assertIn("Choose what Grok can turn on and off by voice. Open a category to pick single devices.", JS)
        self.assertNotIn("Switches and outlets", JS)
        self.assertNotIn("Some plugins show locks or garage doors as plain switches.", JS)
        self.assertEqual(JS.count("Next \u00b7 "), 1)
        self.assertIn('.pk{max-width:720px}', CSS)
        self.assertIn("min-height:68px", CSS)
        self.assertIn("min-height:72px", CSS)
        self.assertIn("min-height:56px", CSS)
        self.assertIn("min-height:60px", CSS)
        self.assertIn("padding:4px 12px 12px 62px", CSS)
        self.assertIn("padding:4px 8px 10px 30px", CSS)
        self.assertIn(".devs{list-style:none;margin:0;padding:4px 12px 12px 62px;background:var(--gs-bg)}", CSS)
        self.assertIn("bottom:72px", CSS)


if __name__ == "__main__":
    unittest.main()


class PickerNoorAnswers(unittest.TestCase):
    """Noor's answers (2026-10-29): spec sizes, one-device wording, no empty room line."""

    def test_spec_font_sizes(self):
        css = open(os.path.join(ROOT, "admin", "web", "admin.css"), encoding="utf-8").read()
        self.assertIn("--fs-cat:16px", css)
        self.assertIn("--fs-dev:15px", css)
        self.assertIn(".catn b{font:600 var(--fs-cat) var(--body)", css)
        self.assertIn(".dev .nm{font-size:var(--fs-dev)", css)

    @unittest.skipUnless(shutil.which("node"), "node not installed")
    def test_one_device_wording(self):
        warn = " \u00b7 \u26a0 check for locks or garage doors"
        self.assertEqual(_run('hbCountText([{uid:"1"}], {1:true}, "light")'), "1 device \u00b7 selected")
        self.assertEqual(_run('hbCountText([{uid:"1"}], {}, "tv")'), "1 device \u00b7 not selected")
        self.assertEqual(_run('hbCountText([{uid:"1"}], {1:true}, "switch")'), "1 device \u00b7 selected" + warn)
        self.assertEqual(_run('hbCountText([{uid:"1"},{uid:"2"}], {1:true,2:true}, "light")'), "All 2 selected")
        self.assertEqual(_run('hbCountText([{uid:"1"},{uid:"2"}], {1:true}, "light")'), "1 of 2 selected")
        self.assertEqual(_run('hbCountText([{uid:"1"},{uid:"2"}], {}, "fan")'), "2 devices \u00b7 none selected")

    @unittest.skipUnless(shutil.which("node"), "node not installed")
    def test_blank_room_leaves_no_line(self):
        self.assertEqual(_run('hbRoomLine({room:"   "})'), "")
        html = _run('hbPickListHtml([{uid:"a",type:"light",name:"Porch",room:"  "}], {a:true}, {light:true})')
        self.assertNotIn('class="rm"', html)
        self.assertIn('class="nm">Porch</div></div>', html)
