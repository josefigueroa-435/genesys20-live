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
                              logs_dir=os.path.join(self.tmp.name, "logs"))

    def tearDown(self):
        self.app.on_close()
        for n, f in self._saved.items():
            setattr(appmod.messagebox, n, f)
        self.tmp.cleanup()

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
