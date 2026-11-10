"""0.13.36: the Bots list repaint keeps what is typed into its open fields.

every(3000, refreshBots) repaints #blist with innerHTML. Before 0.13.36 that wiped the accepted-row Paste webhook
URL and key (and an inline setup edit) mid-entry, so they could not be saved. These run the real app.js in
headless Chrome against the real paintBots.
"""
import os
import re
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import gs1336_repaint_chrome as H  # noqa: E402

URL = "https://api2.cursor.sh/hooks/abc123"
PASTED = "pasted-webhook-value"

TYPE = """
hook('url').value = %r; hook('key').value = %r;
hook('key').focus(); hook('key').setSelectionRange(3, 7);
""" % (URL, PASTED)


@unittest.skipUnless(H.CHROME, "needs Chrome")
class Repaint(unittest.TestCase):
    def test_repaint_keeps_both_values_focus_and_caret(self):
        r = H.chrome(TYPE + """
const before = hook('key');
paintBots(el, el._bots);
res.replaced = before !== hook('key');
res.url = hook('url').value; res.key = hook('key').value; res.focus = ae();
res.caret = [hook('key').selectionStart, hook('key').selectionEnd];
paintBots(el, el._bots);
res.url2 = hook('url').value; res.key2 = hook('key').value; res.focus2 = ae();
""")
        self.assertTrue(r["replaced"], "the repaint should rebuild the row")
        self.assertEqual((r["url"], r["key"], r["focus"]), (URL, PASTED, "key"))
        self.assertEqual(r["caret"], [3, 7])
        self.assertEqual((r["url2"], r["key2"], r["focus2"]), (URL, PASTED, "key"))

    def test_repaint_keeps_focus_in_the_url_field(self):
        r = H.chrome("""
hook('url').value = %r; hook('url').focus(); hook('url').setSelectionRange(5, 5);
paintBots(el, el._bots);
res.url = hook('url').value; res.focus = ae(); res.caret = hook('url').selectionStart;
""" % URL)
        self.assertEqual((r["url"], r["focus"]), (URL, "url"))
        self.assertEqual(r["caret"], 5)

    def test_the_key_is_never_stored(self):
        r = H.chrome(TYPE + """
paintBots(el, el._bots); paintBots(el, el._bots);
const k = %r;
const dump = s => { let o = ''; for (let i = 0; i < s.length; i++) { const n = s.key(i); o += n + '=' + s.getItem(n) + ';'; } return o; };
res.ls = dump(localStorage).indexOf(k) >= 0; res.ss = dump(sessionStorage).indexOf(k) >= 0;
res.onEl = Object.keys(el).some(n => { try { return JSON.stringify(el[n]).indexOf(k) >= 0; } catch (e) { return false; } });
res.attr = L().outerHTML.indexOf(k) >= 0;  // the value is a property, never an attribute
""" % PASTED)
        self.assertEqual((r["ls"], r["ss"], r["onEl"], r["attr"]), (False, False, False, False))

    def test_save_clears_both_even_after_a_repaint_during_the_save(self):
        r = H.chrome(TYPE + """
let release;
const gate = new Promise(r => { release = r; });
window.answer = (url, opt) => {
  if (url.indexOf('/webhook') >= 0) return null;
  if (url.indexOf('/test') >= 0) return [200, {test_id: 't1'}];
  if (url.indexOf('setup-connected') >= 0) return [200, {}];
  return null;
};
const realFetch = window.fetch;
window.fetch = (url, opt) => {
  if (String(url).indexOf('/webhook') >= 0) {
    window.calls.push({url: String(url), method: opt.method, body: opt.body});
    return gate.then(() => new Response('{}', {status: 200, headers: {'Content-Type': 'application/json'}}));
  }
  return realFetch(url, opt);
};
L().querySelector('[data-act="savehook"]').click();
await tick(10);
paintBots(el, el._bots);  // the 3 s refresh lands while the PUT is out
res.during = [hook('url').value, hook('key').value];
release(); await tick(50);
paintBots(el, el._bots);
// 0.13.37: after the save the Test runs and the panel shows Testing (no fields) until it passes or fails.
res.after = hook('url') ? [hook('url').value, hook('key').value] : (L().textContent.indexOf('Waiting for') >= 0 ? 'testing' : 'gone');
const put = window.calls.filter(c => c.url.indexOf('/webhook') >= 0)[0];
res.sent = put ? JSON.parse(put.body) : null;
""")
        self.assertEqual(r["during"], [URL, PASTED])
        self.assertEqual(r["sent"], {"url": URL, "key": PASTED})
        self.assertIn(r["after"], (["", ""], "testing"))

    def test_failed_save_drops_the_key_and_keeps_the_url(self):
        r = H.chrome(TYPE + """
window.answer = (url) => url.indexOf('/webhook') >= 0 ? [400, {error: 'nope'}] : null;
L().querySelector('[data-act="savehook"]').click();
await tick(50);
paintBots(el, el._bots);
res.after = [hook('url').value, hook('key').value];
""")
        self.assertEqual(r["after"], [URL, ""])

    def test_cancel_with_escape_clears_the_draft(self):
        r = H.chrome(TYPE + """
hook('key').dispatchEvent(new KeyboardEvent('keydown', {key: 'Escape', bubbles: true, cancelable: true}));
paintBots(el, el._bots);
res.after = [hook('url').value, hook('key').value];
""")
        self.assertEqual(r["after"], ["", ""])

    def test_leaving_accepted_drops_the_draft(self):
        r = H.chrome(TYPE + """
el._setup.rows.otto.relay = 'relayed'; el._setup.rows.otto.status = 'sent';
paintBots(el, el._bots);
res.gone = !hook('url');
el._setup.rows.otto.relay = 'accepted'; el._setup.rows.otto.status = 'not_set_up';
paintBots(el, el._bots);
res.after = [hook('url').value, hook('key').value];
""")
        self.assertTrue(r["gone"])
        self.assertEqual(r["after"], ["", ""])

    def test_remove_clears_the_pending_rows_draft(self):
        r = H.chrome("""
el._bots = []; el._imported = {rows: [{name: 'Pia', state: 'new', key: 'k1'}]};
el._setup.rows = {pia: {status: 'not_set_up', relay: 'accepted'}};
paintBots(el, []);
const ph = f => L().querySelector('.setpanel[data-slug="pia"] input[data-hook="' + f + '"]');
res.has = !!ph('url');
ph('url').value = %r; ph('key').value = %r;
paintBots(el, []);
res.kept = [ph('url').value, ph('key').value];
window.answer = (url, opt) => url.indexOf('/api/bots/imported/') >= 0 ? [200, {}] : null;
L().querySelector('[data-remove="k1"]').click();
await tick(50);
res.after = ph('url') ? [ph('url').value, ph('key').value] : null;
paintBots(el, []);
res.after2 = ph('url') ? [ph('url').value, ph('key').value] : null;
""" % (URL, PASTED))
        self.assertTrue(r["has"], r)
        self.assertEqual(r["kept"], [URL, PASTED])
        self.assertEqual(r["after"], ["", ""])
        self.assertEqual(r["after2"], ["", ""])

    def test_inline_setup_edit_survives_a_repaint_without_a_save(self):
        r = H.chrome("""
el._setup.rows.otto = {status: 'not_set_up', relay: ''}; el._setupOpen = 'otto';
paintBots(el, el._bots);
const btn = L().querySelector('.setpanel[data-slug="otto"] [data-edit="name"]');
res.has = !!btn;
btn.click();
const ed = () => L().querySelector('input[data-setedit="name"]');
ed().value = 'Otto Two'; ed().setSelectionRange(2, 4);
const n = window.calls.length;
paintBots(el, el._bots);
await tick(10);
res.value = ed() && ed().value; res.focus = ae(); res.caret = ed() && [ed().selectionStart, ed().selectionEnd];
res.saved = window.calls.slice(n).filter(c => c.url.indexOf('setup-draft') >= 0).length;
""")
        self.assertTrue(r["has"], r)
        self.assertEqual((r["value"], r["focus"], r["caret"]), ("Otto Two", "name", [2, 4]))
        self.assertEqual(r["saved"], 0, "the repaint must not save a half-typed edit")


class Wiring(unittest.TestCase):
    def test_paint_bots_repaints_through_keep_bot_list_inputs(self):
        with open(os.path.join(H.ROOT, "admin", "web", "app.js"), encoding="utf-8") as f:
            js = f.read()
        body = js[js.index("\nfunction paintBots("):]
        body = body[:body.index("\n}\n")]
        self.assertEqual(body.count("list.innerHTML = rowsHTML"), 1)
        self.assertRegex(body, re.compile(r"keepBotListInputs\(list, \(\) => \{\s*keepAvatarImgs\(list, \(\) => \{ list\.innerHTML = rowsHTML; \}\);"))
        keep = js[js.index("\nfunction keepBotListInputs("):]
        keep = keep[:keep.index("\n}\n")]
        for word in ("localStorage", "sessionStorage", "el._"):
            self.assertNotIn(word, keep)


if __name__ == "__main__":
    unittest.main()
