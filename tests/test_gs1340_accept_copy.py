"""0.13.40: a key with no 16-hex fingerprint does not offer a dead Accept button.

The help line names the bot and says Accept key is off. The fingerprint line is
"No fingerprint yet", ordinary text, and the tabindex -1 focus target. Accept is
not .disabled: it stays focusable, aria-disabled, and a click sends no request.
A real 16-hex fingerprint is unchanged.
"""
import json
import os
import subprocess
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CSS = open(os.path.join(ROOT, "admin", "web", "admin.css"), encoding="utf-8").read()
# Same declarations as .btn:disabled, including :hover, so the look does not change on hover.
DISABLED_LOOK = '.btn[aria-disabled="true"],.btn[aria-disabled="true"]:hover{opacity:.45;cursor:not-allowed}'
sys_path = os.path.dirname(os.path.abspath(__file__))
import sys
sys.path.insert(0, sys_path)
from test_gs1311_bots_merge import FAKE_DOM, _fn, _icons  # noqa: E402

HELP = "{bot}'s key arrived without a fingerprint, so Accept key is off. Reload, or press Set up again."
COMPARE = "Compare this fingerprint with the one {bot} printed in its chat. If they match, press Accept key. Nothing connects until you do."
FP = "0123456789abcdef"
GROUPED = "0123 4567 89ab cdef"


def _src():
    names = ["formatKeyFp", "setupKeyReviewEl", "bindSetupRows"]
    return "\n".join(_fn(n) for n in names)


DOM = r"""
class Node {
  constructor(tag) {
    this.tagName = tag; this.children = []; this.attrs = {}; this.className = "";
    this.disabled = false; this._text = ""; this.parentNode = null; this.onclick = null;
  }
  setAttribute(k, v) { this.attrs[k] = String(v); }
  getAttribute(k) { return (k in this.attrs) ? this.attrs[k] : null; }
  appendChild(c) { c.parentNode = this; this.children.push(c); return c; }
  set textContent(v) { this._text = String(v); this.children = []; }
  get textContent() { return this._text + this.children.map(c => c.textContent).join(""); }
  focus() { document.activeElement = this; }
  scrollIntoView() {}
  querySelector(sel) { return this.querySelectorAll(sel)[0] || null; }
  querySelectorAll(sel) {
    const all = [];
    (function walk(n) { all.push(n); n.children.forEach(walk); })(this);
    return all.filter(n => nodeMatches(n, sel));
  }
}
function nodeMatches(n, sel) {
  return sel.trim().split(/\s+/).every(() => false) ? false : matchCompound(n, sel);
}
function matchCompound(n, sel) {
  if (sel.indexOf(" ") >= 0) {
    const parts = sel.split(/\s+/);
    const last = parts.pop();
    if (!matchOne(n, last)) return false;
    let p = n.parentNode;
    for (let i = parts.length - 1; i >= 0; i--) {
      while (p && !matchOne(p, parts[i])) p = p.parentNode;
      if (!p) return false;
      p = p.parentNode;
    }
    return true;
  }
  const bits = sel.split(/(?=\[)/);
  return bits.every(b => b && matchOne(n, b));
}
function matchOne(n, sel) {
  if (sel.charAt(0) === ".") return (" " + n.className + " ").indexOf(" " + sel.slice(1).split("[")[0] + " ") >= 0 &&
    (sel.indexOf("[") < 0 || matchCompound(n, sel.slice(sel.indexOf("["))));
  if (sel.charAt(0) === "[") {
    const m = sel.match(/^\[([^\]=]+)(?:="([^"]*)")?\]$/);
    if (!m) return false;
    const v = n.getAttribute(m[1]);
    return m[2] === undefined ? v !== null : v === m[2];
  }
  return false;
}
const document = {
  activeElement: null,
  createElement: t => new Node(t)
};
function fillSetupDetails() {}
function openPasteValues() {}
function setupToggle() {}
function setupEdit() {}
function setupSend() {}
function saveSetupWebhook() {}
function keyFingerprint() { return ""; }
const sent = [];
function decideSetupPair(row, el, decision) { sent.push({slug: row && row.slug, decision: decision, fp: row && row.relay_fp}); }
"""


def _run(expr):
    prog = _icons() + "\n" + DOM + "\n" + _src() + "\nprocess.stdout.write(JSON.stringify(" + expr + "));"
    out = subprocess.run(["node", "-e", prog], capture_output=True, text=True)
    if out.returncode:
        raise AssertionError(out.stderr or out.stdout)
    return json.loads(out.stdout)


def _panel(row):
    return _run(
        "(function(){ const n = setupKeyReviewEl(%s, ''); const nodes = [];"
        " (function walk(x){ nodes.push(x); x.children.forEach(walk); })(n);"
        " const acc = nodes.filter(x => x.getAttribute && x.getAttribute('data-pair')==='accept')[0];"
        " const fp = nodes.filter(x => x.getAttribute && x.getAttribute('data-fpmissing')==='1')[0];"
        " const helps = nodes.filter(x => x.className==='help').map(x => x.textContent);"
        " return {help: helps, fpText: fp ? fp.textContent : null, fpClass: fp ? fp.className : null,"
        " fpTab: fp ? fp.getAttribute('tabindex') : null,"
        " acceptDisabled: !!(acc && acc.disabled), aria: acc ? acc.getAttribute('aria-disabled') : null,"
        " acceptClass: acc ? acc.className : '',"
        " acceptTab: acc ? acc.getAttribute('tabindex') : null,"
        " mono: nodes.filter(x => x.className==='mono').map(x => x.textContent)}; })()"
        % json.dumps(row)
    )


class MissingFingerprintCopy(unittest.TestCase):
    def test_missing_fingerprint_copy_and_focus_target(self):
        for raw in ("", "abcd", "0123456789abcdeG", "0123456789abcde", None):
            got = _panel({"slug": "nix", "name": "Nix", "relay_fp": raw})
            self.assertEqual(got["help"], [HELP.format(bot="Nix")], raw)
            self.assertEqual(got["fpText"], "No fingerprint yet", raw)
            self.assertNotIn("mono", got["fpClass"] or "", raw)
            self.assertEqual(got["fpTab"], "-1", raw)
            self.assertFalse(got["acceptDisabled"], raw)
            self.assertEqual(got["aria"], "true", raw)
            self.assertIsNone(got["acceptTab"], raw)
            self.assertEqual(got["mono"], [], raw)
            self.assertNotIn("Compare this fingerprint", " ".join(got["help"]), raw)
            self.assertTrue(DISABLED_LOOK in CSS,
                            "admin.css has no .btn[aria-disabled=true] rule matching .btn:disabled")
            self.assertTrue(self._rule_matches_accept(got), raw)

    def _rule_matches_accept(self, got):
        """The Accept button in this state is a .btn with aria-disabled=true, which is the new rule."""
        classes = (got.get("acceptClass") or "").split()
        return "btn" in classes and got.get("aria") == "true" and got.get("acceptDisabled") is False

    def test_unnamed_bot_uses_this_bot(self):
        got = _panel({"slug": "nix", "relay_fp": ""})
        self.assertEqual(got["help"], [HELP.format(bot="this bot")])

    def test_present_fingerprint_is_unchanged(self):
        got = _panel({"slug": "nix", "name": "Nix", "relay_fp": FP, "paired_computer": "den-mac"})
        self.assertEqual(got["help"], [COMPARE.format(bot="Nix")])
        self.assertIsNone(got["fpText"])
        self.assertEqual(got["mono"], [GROUPED])
        self.assertFalse(got["acceptDisabled"])
        self.assertIsNone(got["aria"])

    def test_click_on_aria_disabled_accept_sends_nothing(self):
        got = _run(
            "(function(){"
            " const list = document.createElement('div');"
            " const slot = document.createElement('div');"
            " slot.setAttribute('data-reviewfor', 'nix');"
            " list.appendChild(slot);"
            " const row = {slug:'nix', name:'Nix', relay_fp:''};"
            " const rows = {nix: row};"
            " bindSetupRows(list, {_bots: []}, rows);"
            " const acc = list.querySelector('[data-pair=\"accept\"]');"
            " const rej = list.querySelector('[data-pair=\"reject\"]');"
            " acc.onclick({stopPropagation(){}});"
            " rej.onclick({stopPropagation(){}});"
            " const go = document.createElement('button');"
            " go.setAttribute('data-gotoaccept', 'nix');"
            " list.appendChild(go);"
            " bindSetupRows(list, {_bots: []}, rows);"
            " const btn = list.querySelector('[data-gotoaccept]');"
            " btn.onclick({stopPropagation(){}});"
            " return {sent: sent.slice(), disabled: acc.disabled, aria: acc.getAttribute('aria-disabled'),"
            " focus: document.activeElement && document.activeElement.getAttribute('data-fpmissing')};"
            "})()"
        )
        self.assertEqual(got["sent"], [{"slug": "nix", "decision": "reject", "fp": ""}])
        self.assertFalse(got["disabled"])
        self.assertEqual(got["aria"], "true")
        self.assertEqual(got["focus"], "1")


if __name__ == "__main__":
    unittest.main()
