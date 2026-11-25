"""Tests for runs.py (saved runs, report, share zip).  py -m unittest test_runs"""
import os
import tempfile
import unittest
import zipfile
from datetime import datetime

import runs

PNG = (b"\x89PNG\r\n\x1a\n" + b"\x00" * 16)


def read(path, enc="utf-8"):
    with open(path, encoding=enc) as fh:
        return fh.read()


def sample_run(name="Crystal violet / NaOH: fade?"):
    return {
        "app": "GENESYS 20 Live", "version": "0.4.0", "name": name, "notes": "15 µM CV, 0.15 M NaOH",
        "simulated": False, "instrument": {"MODEL": "GENESYS 20", "REV": "GENESYS 20 Revision = 2.10"},
        "sample": {"wl": "590", "eps": "87000", "path": "1.000", "preset": "Crystal violet"},
        "readings": [{"t": 0.0, "source": "SND", "A": 0.874, "T": 13.4, "conc": 10.05, "conc_unit": "µM",
                      "wl": 590.0}, {"t": 2.0, "source": "Print", "A": 0.861, "T": 13.8, "conc": None,
                                     "conc_unit": "", "wl": 590.0}],
        "kinetics": {"basis": "signal A", "best": 1, "fits": {
            "0": {"k": 0.001, "k_units": "µM s⁻¹", "r2": 0.99, "half_life_s": 300.0},
            "1": {"k": 0.004, "k_units": "s⁻¹", "r2": 0.9999, "half_life_s": 173.3},
            "2": {"k": 0.0001, "k_units": "µM⁻¹ s⁻¹", "r2": 0.98, "half_life_s": None}}},
        "calibration": {"unit": "µM", "fit": {"slope": 0.087, "intercept": 0.004, "r2": 0.9995, "n": 4}},
        "scan": {"lambda_max": [589.6]},
    }


class TestRuns(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.d = self.tmp.name

    def tearDown(self):
        self.tmp.cleanup()

    def save(self, name="Crystal violet / NaOH: fade?", when=datetime(2026, 11, 25, 14, 50)):
        return runs.save_run(self.d, sample_run(name), {"live": PNG, "kinetics": PNG},
                             (["t_s", "A"], [[0, 0.874], [2, 0.861]]),
                             (["wavelength_nm", "Scan 1 corrected_A"], [[580, 0.8], [590, 0.87]]), when=when)

    def test_save_writes_everything(self):
        folder = self.save()
        self.assertEqual(os.path.basename(folder), "2026-11-25 1450 Crystal violet NaOH fade")   # safe name
        for f in ("run.json", "readings.csv", "scan.csv", "report.html", "live.png", "kinetics.png"):
            self.assertTrue(os.path.exists(os.path.join(folder, f)), f)
        html = read(os.path.join(folder, "report.html"))
        self.assertIn("data:image/png;base64,", html)
        self.assertEqual(html.count("15 µM CV, 0.15 M NaOH"), 1)
        self.assertIn("(best)", html)
        self.assertIn("GENESYS 20 Revision = 2.10", html)
        csv_text = read(os.path.join(folder, "readings.csv"), "utf-8-sig")
        self.assertTrue(csv_text.startswith("t_s,A"))

    def test_same_minute_does_not_overwrite(self):
        a, b = self.save("x"), self.save("x")
        self.assertNotEqual(a, b)
        self.assertTrue(b.endswith("(2)"))

    def test_list_load_and_summary(self):
        self.save("older", datetime(2026, 11, 24, 9, 0))
        self.save("newer", datetime(2026, 11, 25, 9, 0))
        lst = runs.list_runs(self.d)
        self.assertEqual([r["name"] for r in lst], ["newer", "older"])
        self.assertEqual(lst[0]["readings"], 2)
        d = runs.load_run(lst[0]["folder"])
        text = runs.summary_text(d)
        self.assertIn("best fit order 1, k = 0.004 s⁻¹", text)
        self.assertIn("t½ = 173.3 s", text)
        self.assertIn("λmax: 590 nm", text)

    def test_share_zip_and_delete(self):
        folder = self.save()
        z = runs.share_zip(folder, os.path.join(self.d, "share"))
        self.assertTrue(z.endswith(".zip"))
        with zipfile.ZipFile(z) as zf:
            names = zf.namelist()
        self.assertTrue(any(n.endswith("/report.html") for n in names))
        self.assertTrue(any(n.endswith("/readings.csv") for n in names))
        moved = runs.delete_run(folder)
        self.assertFalse(os.path.exists(folder))
        self.assertTrue(os.path.exists(os.path.join(moved, "run.json")))
        self.assertEqual(runs.list_runs(self.d), [])          # _deleted is not listed

    def test_missing_folder_lists_nothing(self):
        self.assertEqual(runs.list_runs(os.path.join(self.d, "nope")), [])


if __name__ == "__main__":
    unittest.main()
