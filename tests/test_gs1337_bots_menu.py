"""0.13.37: Bots header actions do nothing after the page paints.

pageBots binds the overflow button, paints the list, then calls noteStepup. With a grace window
open (the default 10 minutes after a password), stepupText reads GRACE_MAX_S. 0.13.37 deleted
that constant with the key-location control, so noteStepup throws and the handlers wired after
it never attach. The menu still opens. Refresh avatars, Rescan bots, Add bot and Revoke all
bots do nothing. The same four must still run after a refreshBots cycle.

The fixture is a main bot, three home bots, a setup row, and a posted key waiting on Accept.
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import gs1337_menu_chrome as H  # noqa: E402

# Open the menu is not required: a click on the control runs its handler. Each action leaves a
# trace the stub records, or a dialog the page adds.
PROBE = r"""
const tick = ms => new Promise(r => setTimeout(r, ms));
await tick(400);
const pg = document.getElementById('pg');
res.errs = window.__errs.slice();
res.review = !!document.querySelector('.setpanel.is-review');
res.wired = {
  avatars: !!(document.getElementById('avatars') && document.getElementById('avatars').onclick),
  rescan: !!(document.getElementById('rescan') && document.getElementById('rescan').onclick),
  add: !!(document.getElementById('add') && document.getElementById('add').onclick),
  revoke: !!(document.getElementById('revokeall') && document.getElementById('revokeall').onclick)
};
const sheets = () => document.querySelectorAll('body > .sheet');
const closeSheets = () => { sheets().forEach(n => n.remove()); };
async function hit() {
  const out = {};
  closeSheets();
  let n = window.__calls.length;
  const av = document.getElementById('avatars');
  if (av) av.click();
  await tick(40);
  out.avatars = window.__calls.slice(n).some(c => c === 'POST /api/bots/refresh-avatars');
  n = window.__calls.length;
  const rs = document.getElementById('rescan');
  out.rescanOff = !!(rs && rs.getAttribute('aria-disabled') === 'true');
  if (rs) rs.click();
  await tick(40);
  out.rescan = window.__calls.slice(n).some(c => c === 'POST /api/bots/rescan');
  closeSheets();
  const add = document.getElementById('add');
  if (add) add.click();
  await tick(20);
  out.add = !!document.querySelector('body > .sheet');
  closeSheets();
  const kill = document.getElementById('revokeall');
  if (kill) kill.click();
  await tick(20);
  out.revoke = !!document.querySelector('[aria-label="Revoke all bots"]');
  closeSheets();
  out.errs = window.__errs.slice();
  return out;
}
res.afterLoad = await hit();
res.refreshThrew = '';
try { await refreshBots(pg); }
catch (e) { res.refreshThrew = String(e && (e.stack || e.message) || e); }
res.afterRefresh = await hit();
res.errs = window.__errs.slice();
"""


@unittest.skipUnless(H.CHROME, "needs Chrome")
class BotsMenuActions(unittest.TestCase):
    def test_four_header_actions_run_after_load_and_after_refresh(self):
        r = H.chrome(PROBE)
        self.assertEqual(r["errs"], [], r["errs"])
        self.assertEqual(r["refreshThrew"], "")
        self.assertTrue(r["review"], r)
        self.assertEqual(r["wired"], {"avatars": True, "rescan": True, "add": True, "revoke": True})
        for phase in ("afterLoad", "afterRefresh"):
            got = r[phase]
            self.assertFalse(got["rescanOff"], (phase, got))
            self.assertTrue(got["avatars"], (phase, got))
            self.assertTrue(got["rescan"], (phase, got))
            self.assertTrue(got["add"], (phase, got))
            self.assertTrue(got["revoke"], (phase, got))
            self.assertEqual(got["errs"], [])


if __name__ == "__main__":
    unittest.main()
