"""GUI smoke test in demo mode: drives the real App with the simulated GENESYS.
Run:  py -m unittest test_app -v     (opens a window for a few seconds)"""
import os
import tempfile
import time
import tkinter as tk
import unittest

import app as appmod


class TestDemoApp(unittest.TestCase):
    def setUp(self):
        # dialogs would block an unattended test: answer "yes"/OK and record what was shown
        self.dialogs = []
        mb = appmod.messagebox
        self._saved = {n: getattr(mb, n) for n in ("askyesno", "showinfo", "showwarning", "showerror")}
        mb.askyesno = lambda *a, **k: self.dialogs.append(a) or True
        for n in ("showinfo", "showwarning", "showerror"):
            setattr(mb, n, lambda *a, **k: self.dialogs.append(a))
        self.tmp = tempfile.TemporaryDirectory()
        self.root = tk.Tk()
        self.root.withdraw()
        self.app = appmod.App(self.root, demo=True, settings_path=os.path.join(self.tmp.name, "s.json"),
                              logs_dir=os.path.join(self.tmp.name, "logs"),
                              data_dir=os.path.join(self.tmp.name, "data"), check_updates=False)
        # stub the name/notes prompts and file dialogs
        self._sd = appmod.simpledialog.askstring
        appmod.simpledialog.askstring = lambda title, prompt, **k: (
            "" if "Notes" in prompt or "note" in prompt else "Test run")

    def tearDown(self):
        self.app.on_close()
        for n, f in self._saved.items():
            setattr(appmod.messagebox, n, f)
        appmod.simpledialog.askstring = self._sd
        self.tmp.cleanup()

    def test_presets_scan_runs(self):
        a = self.app
        # reference preset fills the sample
        a.v_preset.set("Brilliant Blue FCF (FD&C Blue 1)")
        a.apply_preset()
        self.assertEqual(a.v_wl.get(), "630")
        self.assertEqual(a.v_unit.get(), "mg/L")
        self.assertAlmostEqual(float(a.v_eps.get()), 164.0 * 792.85, delta=1)
        a.v_preset.set("Bradford protein assay (method)")
        a.apply_preset()
        self.assertEqual((a.v_eps.get(), a.v_method.get()), ("", "cal"))
        # user preset round trip
        a.v_wl.set("590")
        a.v_eps.set("87000")
        a.save_preset()
        self.assertIn("Test run", a.user_presets.names())
        # automatic scan on the simulator: blank pass, then sample pass after Continue
        for k, v in (("start", "560"), ("stop", "620"), ("step", "20"), ("repeats", "2"), ("interval_min", "0"),
                     ("settle_s", "0")):
            a.v_sc[k].set(v)
        a.v_sim_c.set("10")
        a.sim_put_in()
        a.sim.cuvette.conc_m = 0.0                       # the blank: no dye
        a.start_scan()
        end = time.time() + 20
        while a._scan_wait != "hold" and time.time() < end:
            self.spin(0.05)
        self.assertEqual(a._scan_wait, "hold")           # waits for the cuvette swap
        a.sim.cuvette.conc_m = 10e-6
        a.scan_continue()
        while a.scan.state != "done" and time.time() < end + 20:
            self.spin(0.05)
        self.assertEqual(a.scan.state, "done")
        self.assertEqual(len(a.scan.samples()), 2)
        wl, absb = a.scan.corrected(a.scan.samples()[0])
        self.assertEqual(wl, [560, 580, 600, 620])
        self.assertAlmostEqual(max(absb), 0.87, delta=0.03)        # peak 590 nm band, blank removed
        a.nb.select(4)
        self.spin(0.5)
        self.assertEqual(len(a.tv_scan.get_children()), 2)
        # save the run, list it, load it back
        a.send("SND")
        self.wait_for(1)
        a.save_run()
        rows = a._run_rows
        self.assertEqual(len(rows), 1)
        info = next(iter(rows.values()))
        for f in ("run.json", "report.html", "readings.csv", "scan.csv", "scan.png"):
            self.assertTrue(os.path.exists(os.path.join(info["folder"], f)), f)
        n = len(a.readings)
        a.readings.clear()
        a.scan = None
        a.load_run_data(appmod.runs.load_run(info["folder"]))
        self.assertEqual(len(a.readings), n)
        self.assertEqual(len(a.scan.samples()), 2)
        # saved calibration curve round trip
        a.s["standards"] = [[0, 0.004, 590], [5, 0.44, 590], [10, 0.874, 590]]
        a._recalibrate()
        a.save_curve()
        a.s["standards"] = []
        a.v_curve.set("Test run")
        a.load_curve()
        self.assertEqual(len(a.s["standards"]), 3)
        self.assertIsNotNone(a.cal)

    def spin(self, seconds):
        end = time.time() + seconds
        while time.time() < end:
            self.root.update()
            time.sleep(0.02)

    def wait_for(self, n, timeout=10):
        end = time.time() + timeout
        while len(self.app.readings) < n and time.time() < end:
            self.spin(0.05)
        return len(self.app.readings)

    def test_print_snd_polling_and_analysis(self):
        a = self.app
        self.assertTrue(a.link.is_open)
        # Print and a single SND
        a.sim_print()
        self.assertGreaterEqual(self.wait_for(1), 1)
        self.assertEqual(a.readings[0]["source"], "Print")
        self.assertEqual(a.readings[0]["wl"], 590.0)
        self.assertAlmostEqual(a.readings[0]["A"], 0.874, delta=0.01)
        self.assertAlmostEqual(a.readings[0]["conc"], 10.05, delta=0.15)   # µM via eps = 87,000
        a.send("SND")
        self.assertGreaterEqual(self.wait_for(2), 2)
        self.assertEqual(a.readings[1]["source"], "SND")
        # calibration from three simulated standards
        for c in (2, 5, 10):
            a.v_sim_c.set(str(c))
            a.sim_put_in()
            n = len(a.readings)
            a.send("SND")
            self.wait_for(n + 1)
            a.v_cal_c.set(str(c))
            a.add_standard()
        self.assertIsNotNone(a.cal)
        self.assertAlmostEqual(a.cal.eps, 87000, delta=2500)
        # spectrum: three wavelengths
        for wl in (550, 590, 630):
            a.v_wl.set(str(wl))
            n = len(a.readings)
            a.send("SND")
            self.wait_for(n + 1)
            a.add_spec_point()
        self.assertEqual(len(a.s["spectrum"]), 3)
        a.v_wl.set("590")
        # kinetics with hands-free polling
        a.new_run()
        a.v_sim_c.set("10")
        a.sim_put_in()
        a.v_sim_k.set("0.05")
        a.sim_start()
        a.v_poll_s.set("0.5")
        a.v_poll.set(True)
        a.on_poll_toggle()
        self.assertGreaterEqual(self.wait_for(12, timeout=15), 12)
        a.v_poll.set(False)
        a.on_poll_toggle()
        for i in range(4):          # draw every tab
            a.nb.select(i)
            self.spin(0.5)
        self.assertIsNotNone(a.kin)
        self.assertEqual(a.kin.best, 1)
        self.assertAlmostEqual(a.kin.fits[1].k, 0.05, delta=0.01)
        # files
        self.assertTrue(os.path.exists(a.csv_path))
        self.assertTrue(os.path.exists(a.log.path))
        out = os.environ.get("GENESYS_TEST_SHOTS")
        if out:
            for name, fig in (("live", a.live_fig), ("kin", a.kin_fig), ("cal", a.cal_fig), ("spec", a.spec_fig)):
                fig.savefig(os.path.join(out, f"{name}.png"), dpi=80)

    def test_refuses_non_queries(self):
        with self.assertRaises(ValueError):
            self.app.link.send("WL 500")


if __name__ == "__main__":
    unittest.main()
