"""Tests for genesys.py, analysis.py and link.py (no instrument needed).  Run:  py -m unittest -v"""
import math
import os
import tempfile
import time
import unittest

import numpy as np

import analysis as an
import genesys as g
from link import Link


class TestLineAssembler(unittest.TestCase):
    def test_all_terminators(self):
        a = g.LineAssembler()
        self.assertEqual(a.feed(b"one\rtwo\nthree\r\nfour"), [b"one", b"two", b"three"])
        self.assertEqual(a.pending, b"four")
        self.assertEqual(a.flush(), b"four")
        self.assertIsNone(a.flush())

    def test_crlf_split_across_chunks(self):
        a = g.LineAssembler()
        self.assertEqual(a.feed(b"0.523 A\r"), [b"0.523 A"])
        self.assertEqual(a.feed(b"\n0.5"), [])          # the LF must not make an empty line
        self.assertEqual(a.feed(b"24 A\r\n"), [b"0.524 A"])

    def test_bytes_kept(self):
        a = g.LineAssembler()
        self.assertEqual(a.feed(b"\xb5M\xff\r\n"), [b"\xb5M\xff"])


class TestParser(unittest.TestCase):
    def p(self, s, **kw):
        return g.parse_line(s, **kw)

    def test_absorbance_with_units_and_wl(self):
        r = self.p("590nm   0.523 A")
        self.assertEqual((r.value, r.kind, r.wl), (0.523, "A", 590.0))
        r = self.p("  0.523A")
        self.assertEqual((r.value, r.kind, r.wl), (0.523, "A", None))
        self.assertAlmostEqual(r.transmittance, 100 * 10 ** -0.523)

    def test_transmittance(self):
        r = self.p("45.2 %T  500 nm")
        self.assertEqual((r.kind, r.value, r.wl), ("%T", 45.2, 500.0))
        self.assertAlmostEqual(r.absorbance, 2 - math.log10(45.2))
        self.assertIsNone(self.p("-1.0 %T"))

    def test_concentration_units(self):
        r = self.p("12.5 mg/L")
        self.assertEqual((r.kind, r.unit), ("C", "mg/L"))
        r = self.p("3.2 uM")
        self.assertEqual((r.kind, r.unit), ("C", "µM"))
        self.assertIsNone(r.absorbance)

    def test_bare_number_is_default_kind(self):
        r = self.p("  -0.004")
        self.assertEqual((r.value, r.kind, r.unit), (-0.004, "A", ""))
        r = self.p("45.0", default_kind="%T")
        self.assertEqual(r.kind, "%T")

    def test_decimal_comma(self):
        r = self.p("0,523 A", decimal=",")
        self.assertEqual(r.value, 0.523)

    def test_not_readings(self):
        for s in ("", "Baud Rate 9600", "Data bits 8", "GENESYS 20 (SIMULATED)", "1.11-SIM",
                  "Terminator CR/LF", "1  2", "COM11"):
            self.assertIsNone(self.p(s), s)

    def test_number_with_unit_wins_over_index(self):
        r = self.p("Sample 3   0.811 A")
        self.assertEqual(r.value, 0.811)


class TestConversions(unittest.TestCase):
    def test_a_t_roundtrip(self):
        for a in (0.0, 0.3, 1.0, 2.0):
            self.assertAlmostEqual(g.absorbance_from_t(g.transmittance_from_a(a)), a)
        self.assertIsNone(g.absorbance_from_t(0))

    def test_units(self):
        self.assertAlmostEqual(an.to_molar(5, "µM"), 5e-6)
        self.assertAlmostEqual(an.to_molar(40.8, "mg/L", mw=408.0), 1e-4)
        self.assertIsNone(an.to_molar(1, "mg/L"))
        self.assertAlmostEqual(an.from_molar(1e-4, "mg/L", mw=408.0), 40.8)
        self.assertAlmostEqual(an.beer_lambert_conc(0.87, 87000, 1.0), 1e-5)


class TestCalibration(unittest.TestCase):
    def test_exact_line(self):
        conc = [0, 2, 4, 6, 8]
        a = [0.01 + 0.087 * c for c in conc]           # µM, eps = 87,000
        cal = an.calibrate(conc, a, "µM", 1.0)
        self.assertAlmostEqual(cal.fit.slope, 0.087)
        self.assertAlmostEqual(cal.fit.r2, 1.0)
        self.assertAlmostEqual(cal.eps, 87000.0)
        self.assertAlmostEqual(cal.conc(0.01 + 0.087 * 5), 5.0)
        self.assertAlmostEqual(cal.lod, 0.0, places=9)

    def test_lod_with_noise(self):
        rng = np.random.default_rng(1)
        conc = np.linspace(0, 10, 8)
        a = 0.05 * conc + rng.normal(0, 0.002, conc.size)
        cal = an.calibrate(conc, a, "mg/L")
        self.assertIsNone(cal.eps)                     # mass unit: no molar absorptivity
        self.assertTrue(0 < cal.lod < cal.loq)
        self.assertAlmostEqual(cal.loq / cal.lod, 10 / 3.3)

    def test_needs_two_points(self):
        with self.assertRaises(ValueError):
            an.calibrate([1], [0.1], "M")


class TestKinetics(unittest.TestCase):
    def test_first_order_from_absorbance(self):
        t = np.arange(0, 600, 10.0)
        a = 0.02 + 0.85 * np.exp(-0.004 * t)
        x, unit, basis = an.reactant_amount(a, "reactant", eps=87000, a_inf=0.02, unit="µM")
        self.assertEqual(unit, "µM")
        self.assertAlmostEqual(x[0], 0.85 / 87000 * 1e6)
        kin = an.fit_kinetics(t, x, unit, basis)
        self.assertEqual(kin.best, 1)
        f = kin.fits[1]
        self.assertAlmostEqual(f.k, 0.004, places=6)
        self.assertAlmostEqual(f.half_life_s, math.log(2) / 0.004, places=3)
        self.assertGreater(f.r2, 0.999999)
        self.assertGreater(kin.initial_rate, 0)

    def test_zero_and_second_order(self):
        t = np.linspace(0, 100, 30)
        kin0 = an.fit_kinetics(t, 1.0 - 0.005 * t, "mM")
        self.assertEqual(kin0.best, 0)
        self.assertAlmostEqual(kin0.fits[0].k, 0.005)
        self.assertAlmostEqual(kin0.fits[0].half_life_s, 100.0)
        x2 = 1.0 / (1.0 / 2.0 + 0.03 * t)
        kin2 = an.fit_kinetics(t, x2, "mM")
        self.assertEqual(kin2.best, 2)
        self.assertAlmostEqual(kin2.fits[2].k, 0.03)
        self.assertAlmostEqual(kin2.fits[2].x0, 2.0)

    def test_product_absorbs(self):
        t = np.arange(0, 300, 5.0)
        a_inf = 0.9
        a = a_inf * (1 - np.exp(-0.01 * t))
        x, unit, _ = an.reactant_amount(a, "product", a_inf=a_inf)
        self.assertEqual(unit, "A")
        kin = an.fit_kinetics(t[:-1], x[:-1], unit)
        self.assertAlmostEqual(kin.fits[1].k, 0.01, places=6)
        # c0 route: [R] = c0 - A/(eps b)
        x2, unit2, _ = an.reactant_amount(a, "product", eps=9000, c0_m=1e-4, unit="mM")
        self.assertAlmostEqual(x2[0], 0.1)
        with self.assertRaises(ValueError):
            an.reactant_amount(a, "product")

    def test_non_positive_points_left_out(self):
        t = np.arange(10.0)
        x = np.array([5, 4, 3, 2, 1, 0.5, 0, -0.01, 0.2, 0.1])
        kin = an.fit_kinetics(t, x, "A")
        self.assertEqual(kin.fits[1].used, 8)
        self.assertIn("left out", kin.fits[1].note)


class TestSpectrum(unittest.TestCase):
    def test_lambda_max_refined(self):
        wl = np.arange(500, 681, 20.0)
        a = np.exp(-0.5 * ((wl - 593.0) / 45.0) ** 2)
        lm, am = an.lambda_max(wl, a)
        self.assertAlmostEqual(lm, 593.0, delta=1.0)
        self.assertAlmostEqual(am, 1.0, delta=0.01)

    def test_edge_max_not_refined(self):
        self.assertEqual(an.lambda_max([400, 410, 420], [0.1, 0.2, 0.3]), (420.0, 0.3))


class TestSessionLog(unittest.TestCase):
    def test_writes_and_flushes(self):
        with tempfile.TemporaryDirectory() as d:
            log = g.SessionLog(d, simulated=True)
            log.write("in", b"0.5 A\r\n", g.SerialSettings())
            with open(log.path, encoding="utf-8") as fh:
                line = fh.read()
            log.close()
            self.assertIn('"hex": "302e35204', line)
            self.assertIn("SIMULATED", os.path.basename(log.path))


class FakeClock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


class TestSimulatorAndLink(unittest.TestCase):
    def setUp(self):
        self.clock = FakeClock()
        self.sim = g.SimulatedGenesys(noise=0.0, clock=self.clock, read_timeout=0.005)
        self.link = Link()
        self.link.open(g.SerialSettings(port="SIM"), port=self.sim)

    def tearDown(self):
        self.link.close()

    def collect(self, kinds, timeout=2.0):
        out, end = [], time.time() + timeout
        while time.time() < end:
            try:
                ev = self.link.events.get(timeout=0.05)
            except Exception:
                continue
            out.append(ev)
            if all(any(e[0] == k for e in out) for k in kinds):
                break
        return out

    def test_snd_reply_is_tagged(self):
        self.link.send("SND")
        evs = self.collect(["line"])
        line = [e for e in evs if e[0] == "line"][0]
        self.assertEqual((line[3], line[4]), ("reply", "SND"))
        r = g.parse_line(g.decode(line[2]))
        self.assertAlmostEqual(r.value, 0.874, places=3)    # 87000 * 1e-5 + baseline 0.004

    def test_print_is_tagged(self):
        self.sim.press_print()
        evs = self.collect(["line"])
        line = [e for e in evs if e[0] == "line"][0]
        self.assertEqual(line[3], "print")
        r = g.parse_line(g.decode(line[2]))
        self.assertEqual((r.kind, r.wl), ("A", 590.0))

    def test_refuses_other_commands(self):
        for bad in ("WL 500", "ZERO", "snd", "SND\r\nZERO"):
            with self.assertRaises(ValueError):
                self.link.send(bad)
        self.assertEqual(self.sim.received, [])

    def test_polling(self):
        self.link.poll_interval = 0.1
        evs = self.collect(["line"], timeout=3)
        self.link.poll_interval = 0
        self.assertTrue(any(e[0] == "out" and e[2] == b"SND\r\n" for e in evs))
        self.assertIn("SND", self.sim.received)

    def test_reaction_fades(self):
        cuv = self.sim.cuvette
        cuv.react_t0 = self.clock.t
        a0 = self.sim.absorbance()
        self.clock.t += math.log(2) / cuv.k
        self.assertAlmostEqual(self.sim.absorbance() - cuv.baseline, (a0 - cuv.baseline) / 2)


if __name__ == "__main__":
    unittest.main()
