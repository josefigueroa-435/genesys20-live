"""Tests for scan.py and presets.py.  py -m unittest test_scan_presets"""
import math
import os
import tempfile
import unittest

import presets
from scan import ScanConfig, ScanEngine


class Clock:
    def __init__(self):
        self.t = 100.0

    def __call__(self):
        return self.t


class TestScanConfig(unittest.TestCase):
    def test_wavelengths(self):
        self.assertEqual(ScanConfig(400, 440, 10).wavelengths(), [400, 410, 420, 430, 440])
        self.assertEqual(ScanConfig(400, 435, 10).wavelengths(), [400, 410, 420, 430, 435])   # end included
        self.assertEqual(ScanConfig(440, 400, 20).wavelengths(), [440, 420, 400])           # downward
        with self.assertRaises(ValueError):
            ScanConfig(300, 400, 10).wavelengths()        # below the GENESYS 20 range
        with self.assertRaises(ValueError):
            ScanConfig(400, 500, 0).wavelengths()


class TestScanEngine(unittest.TestCase):
    def run_pass(self, eng, values):
        for v in values:
            self.assertIsNotNone(eng.target())
            eng.record(v)

    def test_blank_then_sample_corrected(self):
        eng = ScanEngine(ScanConfig(500, 520, 10, blank_first=True), clock=Clock())
        eng.start()
        self.assertEqual(eng.current.kind, "blank")
        self.assertEqual(eng.target(), 500)
        self.run_pass(eng, [0.00, 0.05, 0.10])            # blank drifts away from the zeroing wavelength
        self.assertEqual(eng.current.kind, "sample")
        self.run_pass(eng, [0.50, 0.65, 0.40])
        self.assertEqual(eng.state, "done")
        wl, a = eng.corrected(eng.samples()[0])
        self.assertEqual(wl, [500, 510, 520])
        for got, want in zip(a, [0.50, 0.60, 0.30]):
            self.assertAlmostEqual(got, want)
        head, rows = eng.table()
        self.assertEqual(head[:2], ["wavelength_nm", "blank_A"])
        self.assertAlmostEqual(rows[1][-1], 0.60)

    def test_repeats_wait_for_interval(self):
        clk = Clock()
        eng = ScanEngine(ScanConfig(500, 510, 10, blank_first=False, repeats=3, interval_s=60), clock=clk)
        eng.start()
        self.run_pass(eng, [0.5, 0.6])
        self.assertEqual(eng.state, "waiting")
        self.assertIsNone(eng.target())
        clk.t += 30
        self.assertFalse(eng.tick())
        self.assertIn("Next scan in 0:30", eng.progress())
        clk.t += 30
        self.assertTrue(eng.tick())
        self.assertEqual(eng.current.number, 2)
        self.assertAlmostEqual(eng.current.t_start, 60)
        self.run_pass(eng, [0.4, 0.5])
        clk.t += 200                                          # late: next pass starts immediately
        self.assertTrue(eng.tick())
        self.run_pass(eng, [0.3, 0.4])
        self.assertEqual(eng.state, "done")
        self.assertEqual(len(eng.samples()), 3)

    def test_back_to_back_when_interval_short(self):
        eng = ScanEngine(ScanConfig(500, 500, 10, blank_first=False, repeats=2, interval_s=0), clock=Clock())
        eng.start()
        eng.record(0.1)
        self.assertEqual(eng.state, "running")
        self.assertEqual(eng.current.number, 2)

    def test_skip_and_stop(self):
        eng = ScanEngine(ScanConfig(500, 520, 10, blank_first=False), clock=Clock())
        eng.start()
        eng.record(0.2)
        eng.skip()
        self.assertTrue(math.isnan(eng.current.points[510]))
        eng.stop()
        self.assertIsNone(eng.target())
        with self.assertRaises(RuntimeError):
            eng.record(0.1)


class TestPresets(unittest.TestCase):
    def test_reference_values_are_sane(self):
        for p in presets.REFERENCE:
            self.assertTrue(325 <= p["wl"] <= 1100, p["name"])
            self.assertTrue(p["source"].startswith("https://"), p["name"])
            eps = presets.eps_of(p)
            if p.get("method"):
                self.assertIsNone(eps)
            else:
                self.assertGreater(eps, 1000, p["name"])
                self.assertIn("nm", presets.describe(p))

    def test_absorptivity_to_epsilon(self):
        tart = next(p for p in presets.REFERENCE if p["name"].startswith("Tartrazine"))
        self.assertAlmostEqual(presets.eps_of(tart), 53.0 * 534.36)
        nadh = next(p for p in presets.REFERENCE if p["name"] == "NADH")
        self.assertEqual(presets.eps_of(nadh), 6220)

    def test_store_round_trip(self):
        with tempfile.TemporaryDirectory() as d:
            st = presets.Store(os.path.join(d, "sub", "curves.json"))
            self.assertEqual(st.all(), [])
            st.put({"name": "BSA", "unit": "mg/L", "standards": [[0, 0.0], [10, 0.5]]})
            st.put({"name": "Dye", "unit": "µM", "standards": []})
            st.put({"name": "BSA", "unit": "mg/L", "standards": [[0, 0.0], [10, 0.6]]})   # replaces
            self.assertEqual(st.names(), ["Dye", "BSA"])
            self.assertEqual(st.get("BSA")["standards"][1][1], 0.6)
            self.assertIn("saved", st.get("BSA"))
            st.delete("Dye")
            self.assertEqual(st.names(), ["BSA"])


if __name__ == "__main__":
    unittest.main()
