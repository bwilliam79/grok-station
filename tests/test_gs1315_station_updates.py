"""0.13.15 bq-0041: the bot instructions have a Station updates section, and the home helper's show command
accepts the update / update_clear ops the relay already takes (it refused them before, so no home bot could post)."""
import importlib.machinery, importlib.util, json, os, re, unittest
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def load(name, rel):
    loader = importlib.machinery.SourceFileLoader(name, os.path.join(ROOT, rel))
    mod = importlib.util.module_from_spec(importlib.util.spec_from_loader(name, loader))
    loader.exec_module(mod)
    return mod


class Reached(Exception):
    pass


class HelperOps(unittest.TestCase):
    def _show(self, doc):
        home = load("gsr_home_1315", "scripts/gs-seal-reply")
        with mock.patch.object(home, "read_stdin", lambda: json.dumps(doc)), \
             mock.patch.object(home, "_check_tree", mock.Mock(side_effect=Reached)):
            try:
                home.cmd_show("max")
            except Reached:
                return "past-op-check"
            except home.Refused:
                return "refused"
        return "?"

    def test_update_ops_pass_the_op_check(self):
        up = {"v": 1, "op": "update", "id": "build", "update": {"type": "working_on", "line": "Building 0.13.15",
                                                                 "time": "2026-11-01T13:00:00-06:00"}}
        self.assertEqual(self._show(up), "past-op-check")
        self.assertEqual(self._show({"v": 1, "op": "update_clear", "id": "build"}), "past-op-check")

    def test_other_ops_and_replay_fields_still_refused(self):
        self.assertEqual(self._show({"v": 1, "op": "speak", "id": "x"}), "refused")
        self.assertEqual(self._show({"v": 1, "op": "update", "id": "x", "nonce": "n"}), "refused")
        self.assertEqual(self._show({"v": 1, "op": "put", "id": "x"}), "past-op-check")


class Templates(unittest.TestCase):
    def test_section_in_every_template(self):
        gs = load("gs_routine_1315", "admin/gs_routine.py")
        for mode in ("home", "cloud"):
            t = gs.TEMPLATES[mode]
            self.assertIn("\nStation updates.", t)
            self.assertIn('"op":"update_clear"', t)
            self.assertLess(t.index("\nStation updates."), t.index("\nStation update. When"))
            with open(os.path.join(ROOT, "admin", "routine-%s.txt" % mode)) as f:
                self.assertEqual(f.read(), t)
        sh = open(os.path.join(ROOT, "scripts", "gs-update.sh")).read()
        self.assertEqual(sh.count("\nStation updates."), 2)


if __name__ == "__main__":
    unittest.main()
