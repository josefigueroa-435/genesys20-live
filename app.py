"""GENESYS 20 Live - readings from a Thermo Scientific GENESYS 20 over RS-232, with live graphs,
Beer-Lambert concentrations, kinetics fits, calibration curves and point-by-point spectra.

Run:  py app.py           (real instrument)
      py app.py --demo    (simulated instrument; everything is labelled SIMULATED)

The app only ever sends the read-only queries SND, MODEL and REV (see genesys.READ_ONLY_COMMANDS),
and only when you ask for them. Print on the instrument works with no commands at all.
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import sys
import time
from datetime import datetime

import tkinter as tk
from tkinter import filedialog, messagebox, simpledialog, ttk


def _require_packages() -> None:
    import importlib.util

    missing = [m for m in ("numpy", "matplotlib", "serial") if importlib.util.find_spec(m) is None]
    if missing:
        root = tk.Tk()
        root.withdraw()
        messagebox.showerror(
            "GENESYS 20 Live",
            "Some Python packages this app needs are not installed:\n\n  " + ", ".join(missing) +
            "\n\nDouble-click 'Install requirements.bat' in the app folder (needs internet), "
            "then start the app again.")
        root.destroy()
        sys.exit(1)


_require_packages()

import numpy as np  # noqa: E402
import matplotlib  # noqa: E402

matplotlib.use("TkAgg")
from matplotlib.backends.backend_tkagg import FigureCanvasTkAgg, NavigationToolbar2Tk  # noqa: E402
from matplotlib.figure import Figure  # noqa: E402

import analysis as an  # noqa: E402
import genesys as g  # noqa: E402
from link import Link  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
APP_NAME = "GENESYS 20 Live"
VERSION = "0.2.0"

DEFAULTS = {
    "serial": g.SerialSettings().to_dict(),
    "decimal": ".",
    "default_kind": "A",
    "poll_s": "2",
    "polling_ack": False,
    "format_verified": False,
    "verified_example": "",
    "wl": "",
    "path": "1.000",
    "species": "reactant",
    "eps": "",
    "mw": "",
    "unit": "µM",
    "c0": "",
    "a_inf": "",
    "conc_method": "beer",
    "live_y": "Absorbance",
    "xunit": "s",
    "cal_unit": "µM",
    "standards": [],
    "spectrum": [],
    "kin_from": "",
    "kin_to": "",
    "geometry": "zoomed",
}

DEMO_SAMPLE = {"wl": "590", "eps": "87000", "unit": "µM", "species": "reactant", "a_inf": "0.004",
               "path": "1.000"}

STATUS_STYLE = {
    "off": ("Not connected", "#6b7280"),
    "open": ("Port open - waiting for a reading (press Print, or ask with SND)", "#b45309"),
    "bytes": ("Receiving - reading format NOT yet checked against the display", "#1d4ed8"),
    "verified": ("Receiving - reading format checked against the display", "#15803d"),
    "sim": ("SIMULATED instrument (demo) - not real data", "#7e22ce"),
}
PARITY_NAMES = {"N": "None", "E": "Even", "O": "Odd"}
FLOW_NAMES = {"none": "Off", "rtscts": "CTS/RTS", "xonxoff": "XON/XOFF"}
Y_CHOICES = ("Absorbance", "%Transmittance", "Concentration")


def load_settings(path: str) -> dict:
    s = json.loads(json.dumps(DEFAULTS))
    try:
        with open(path, encoding="utf-8") as fh:
            s.update({k: v for k, v in json.load(fh).items() if k in DEFAULTS})
    except (OSError, ValueError):
        pass
    return s


def fmt_sig(x: float | None, sig: int = 4) -> str:
    if x is None or not np.isfinite(x):
        return "-"
    if x == 0:
        return "0"
    mag = abs(x)
    if 1e-3 <= mag < 1e5:
        return f"{x:.{max(0, sig - 1 - int(np.floor(np.log10(mag))))}f}"
    return f"{x:.{sig - 1}e}"


def list_ports() -> list[str]:
    try:
        from serial.tools import list_ports as lp
        return [f"{p.device} - {p.description}" for p in sorted(lp.comports(), key=lambda p: p.device)]
    except Exception:
        return []


class ScrollFrame(ttk.Frame):
    """A vertically scrolling frame for the sidebar."""

    def __init__(self, master, width=330):
        super().__init__(master)
        self.canvas = tk.Canvas(self, width=width, highlightthickness=0, borderwidth=0)
        bar = ttk.Scrollbar(self, orient="vertical", command=self.canvas.yview)
        self.body = ttk.Frame(self.canvas)
        self.body.bind("<Configure>", lambda e: self.canvas.configure(scrollregion=self.canvas.bbox("all")))
        self._win = self.canvas.create_window((0, 0), window=self.body, anchor="nw")
        self.canvas.bind("<Configure>", lambda e: self.canvas.itemconfigure(self._win, width=e.width))
        self.canvas.configure(yscrollcommand=bar.set)
        self.canvas.pack(side="left", fill="both", expand=True)
        bar.pack(side="right", fill="y")
        self.bind_all("<MouseWheel>", self._wheel, add="+")

    def _wheel(self, e):
        """Scroll only when the pointer is somewhere over the sidebar (combobox lists etc. keep theirs)."""
        w = self.winfo_containing(e.x_root, e.y_root)
        while w is not None:
            if w is self:
                if isinstance(e.widget, (ttk.Combobox, ttk.Spinbox)):
                    return
                self.canvas.yview_scroll(int(-e.delta / 120), "units")
                return
            w = w.master


class App:
    def __init__(self, root: tk.Tk, demo: bool = False, settings_path: str | None = None,
                 logs_dir: str | None = None, auto_connect: bool = True):
        self.root = root
        self.demo = demo
        default_settings = os.path.join(HERE, "settings.demo.json" if demo else "settings.json")
        self.settings_path = settings_path or default_settings
        self.s = load_settings(self.settings_path)
        if demo:
            for k, v in DEMO_SAMPLE.items():
                if not self.s.get(k):
                    self.s[k] = v
        self.logs = logs_dir or os.path.join(HERE, "logs")
        self.link = Link()
        self.sim: g.SimulatedGenesys | None = None
        self.log: g.SessionLog | None = None
        self.csv_path: str | None = None
        self.readings: list[dict] = []
        self.marks: list[tuple[float, str]] = []
        self.t0: float | None = None
        self.last: dict | None = None
        self.last_parsed_text = ""
        self.ident: dict = {}
        self.snd_misses = 0
        self.cal: an.Calibration | None = None
        self.kin: an.Kinetics | None = None
        self._dirty = {"live", "kin", "cal", "spec"}
        self._last_draw = 0.0
        self._closing = False

        root.title(f"{APP_NAME} {VERSION}" + ("  -  SIMULATED INSTRUMENT (demo)" if demo else ""))
        root.minsize(960, 560)
        if self.s.get("geometry") == "zoomed":
            try:
                root.state("zoomed")
            except tk.TclError:
                root.geometry("1280x800")
        else:
            root.geometry(self.s["geometry"])
        self._style()
        self._build()
        self._load_tables()
        self._recalibrate()
        self._set_status("sim" if demo else "off")
        root.protocol("WM_DELETE_WINDOW", self.on_close)
        root.after(100, self._pump)
        if demo and auto_connect:
            self.connect()

    # ================================================================== layout
    def _style(self):
        st = ttk.Style(self.root)
        try:
            st.theme_use("vista" if sys.platform == "win32" else "clam")
        except tk.TclError:
            pass
        st.configure("Big.TLabel", font=("Segoe UI", 30, "bold"))
        st.configure("Sub.TLabel", font=("Segoe UI", 11))
        st.configure("Hint.TLabel", foreground="#555555", font=("Segoe UI", 9))
        st.configure("Head.TLabel", font=("Segoe UI", 10, "bold"))

    def _build(self):
        r = self.root
        self._build_connection_bar(r)
        main = ttk.PanedWindow(r, orient="horizontal")
        main.pack(fill="both", expand=True, padx=6, pady=(0, 4))
        side = ScrollFrame(main)
        main.add(side, weight=0)
        self._build_sidebar(side.body)
        self.nb = ttk.Notebook(main)
        main.add(self.nb, weight=1)
        self._build_live_tab()
        self._build_kin_tab()
        self._build_cal_tab()
        self._build_spec_tab()
        self._build_raw_tab()
        self.nb.bind("<<NotebookTabChanged>>", lambda e: self._dirty.update({"live", "kin", "cal", "spec"}))
        self.statusbar = ttk.Label(r, text="", style="Hint.TLabel", anchor="w")
        self.statusbar.pack(fill="x", padx=8, pady=(0, 4))

    def _build_connection_bar(self, r):
        top = ttk.Frame(r, padding=(6, 6, 6, 0))
        top.pack(fill="x")
        bar = ttk.Frame(r, padding=(6, 4, 6, 4))
        bar.pack(fill="x")
        ser = g.SerialSettings.from_dict(self.s["serial"])
        self.v_port = tk.StringVar(value=ser.port)
        self.v_baud = tk.StringVar(value=str(ser.baud))
        self.v_bits = tk.StringVar(value=str(ser.bits))
        self.v_parity = tk.StringVar(value=PARITY_NAMES[ser.parity])
        self.v_stop = tk.StringVar(value=str(ser.stop))
        self.v_flow = tk.StringVar(value=FLOW_NAMES[ser.flow])
        self.v_term = tk.StringVar(value=ser.terminator)
        self.v_decimal = tk.StringVar(value=self.s["decimal"])

        def field(label, var, values, width):
            ttk.Label(bar, text=label).pack(side="left", padx=(8, 2))
            cb = ttk.Combobox(bar, textvariable=var, values=values, width=width, state="readonly")
            cb.pack(side="left")
            return cb

        ttk.Label(top, text="Port").pack(side="left")
        self.cb_port = ttk.Combobox(top, textvariable=self.v_port, width=34)
        self.cb_port.pack(side="left", padx=(2, 0))
        ttk.Button(top, text="↻", width=3, command=self.refresh_ports).pack(side="left", padx=2)
        self.btn_connect = ttk.Button(top, text="Connect", command=self.toggle_connect, width=12)
        self.btn_connect.pack(side="left", padx=(10, 6))
        self.lbl_status = tk.Label(top, text="", fg="white", bg="#6b7280", padx=8, pady=2,
                                   font=("Segoe UI", 9, "bold"))
        self.lbl_status.pack(side="left", padx=4)
        ttk.Label(bar, text="Must match Utility > RS232 on the GENESYS:", style="Hint.TLabel").pack(side="left")
        field("Baud", self.v_baud, [str(b) for b in g.BAUDS], 6)
        field("Bits", self.v_bits, ["8", "7"], 3)
        field("Parity", self.v_parity, list(PARITY_NAMES.values()), 6)
        field("Stop", self.v_stop, ["1", "2"], 3)
        field("Handshake", self.v_flow, list(FLOW_NAMES.values()), 9)
        field("Line end", self.v_term, list(g.TERMINATORS), 6)
        field("Decimal", self.v_decimal, [".", ","], 3)
        self.refresh_ports()

    def _section(self, parent, title):
        f = ttk.LabelFrame(parent, text=title, padding=(8, 4))
        f.pack(fill="x", padx=4, pady=4)
        return f

    def _build_sidebar(self, p):
        # --- reading now
        f = self._section(p, "Reading now")
        self.lbl_big = ttk.Label(f, text="---", style="Big.TLabel")
        self.lbl_big.pack(anchor="w")
        self.lbl_sub = ttk.Label(f, text="", style="Sub.TLabel")
        self.lbl_sub.pack(anchor="w")
        self.lbl_src = ttk.Label(f, text="No readings yet", style="Hint.TLabel", wraplength=300)
        self.lbl_src.pack(anchor="w")

        # --- live readings
        f = self._section(p, "Hands-free readings")
        self.v_poll = tk.BooleanVar(value=False)
        self.v_poll_s = tk.StringVar(value=self.s["poll_s"])
        row = ttk.Frame(f)
        row.pack(fill="x")
        ttk.Checkbutton(row, text="Ask for a reading every", variable=self.v_poll,
                        command=self.on_poll_toggle).pack(side="left")
        sp = ttk.Spinbox(row, from_=0.5, to=600, increment=0.5, width=5, textvariable=self.v_poll_s,
                         command=self.on_poll_toggle)
        sp.pack(side="left", padx=2)
        sp.bind("<FocusOut>", lambda e: self.on_poll_toggle())
        ttk.Label(row, text="s").pack(side="left")
        row = ttk.Frame(f)
        row.pack(fill="x", pady=(4, 0))
        ttk.Button(row, text="Ask once (SND)", command=lambda: self.send("SND")).pack(side="left")
        ttk.Button(row, text="Identify", command=self.identify).pack(side="left", padx=4)
        ttk.Label(f, text="Uses the read-only query SND (absorbance only). Pressing Print on the "
                          "GENESYS always works too.", style="Hint.TLabel", wraplength=300).pack(anchor="w", pady=(4, 0))

        # --- run
        f = self._section(p, "Run")
        row = ttk.Frame(f)
        row.pack(fill="x")
        ttk.Button(row, text="New run (t = 0 now)", command=self.new_run).pack(side="left")
        ttk.Button(row, text="Mark event...", command=self.mark_event).pack(side="left", padx=4)
        row = ttk.Frame(f)
        row.pack(fill="x", pady=(4, 0))
        ttk.Button(row, text="Save CSV...", command=self.save_csv).pack(side="left")
        self.lbl_run = ttk.Label(row, text="", style="Hint.TLabel")
        self.lbl_run.pack(side="left", padx=6)

        # --- sample
        f = self._section(p, "Sample - what you know")
        self.v_wl = tk.StringVar(value=self.s["wl"])
        self.v_path = tk.StringVar(value=self.s["path"])
        self.v_species = tk.StringVar(value=self.s["species"])
        self.v_eps = tk.StringVar(value=self.s["eps"])
        self.v_mw = tk.StringVar(value=self.s["mw"])
        self.v_unit = tk.StringVar(value=self.s["unit"])
        self.v_c0 = tk.StringVar(value=self.s["c0"])
        self.v_ainf = tk.StringVar(value=self.s["a_inf"])
        self.v_method = tk.StringVar(value=self.s["conc_method"])
        grid = ttk.Frame(f)
        grid.pack(fill="x")
        rows = [
            ("Wavelength set on GENESYS (nm)", ttk.Entry(grid, textvariable=self.v_wl, width=10)),
            ("Path length b (cm)", ttk.Entry(grid, textvariable=self.v_path, width=10)),
            ("ε (L mol⁻¹ cm⁻¹) at that λ", ttk.Entry(grid, textvariable=self.v_eps, width=10)),
            ("Molar mass (g/mol, for mg/L)", ttk.Entry(grid, textvariable=self.v_mw, width=10)),
            ("Show concentration in", ttk.Combobox(grid, textvariable=self.v_unit, values=an.UNITS, width=8,
                                                  state="readonly")),
        ]
        for i, (lab, w) in enumerate(rows):
            ttk.Label(grid, text=lab).grid(row=i, column=0, sticky="w", pady=1)
            w.grid(row=i, column=1, sticky="w", padx=(6, 0), pady=1)
        ttk.Label(f, text="Concentration from", style="Head.TLabel").pack(anchor="w", pady=(6, 0))
        ttk.Radiobutton(f, text="Beer-Lambert: c = A / (ε b)", value="beer",
                        variable=self.v_method).pack(anchor="w")
        ttk.Radiobutton(f, text="My calibration curve (Calibration tab)", value="cal",
                        variable=self.v_method).pack(anchor="w")
        ttk.Label(f, text="For kinetics", style="Head.TLabel").pack(anchor="w", pady=(6, 0))
        ttk.Radiobutton(f, text="The reactant absorbs (colour fades)", value="reactant",
                        variable=self.v_species).pack(anchor="w")
        ttk.Radiobutton(f, text="The product absorbs (colour grows)", value="product",
                        variable=self.v_species).pack(anchor="w")
        grid = ttk.Frame(f)
        grid.pack(fill="x", pady=(2, 0))
        ttk.Label(grid, text="Final absorbance A∞ (optional)").grid(row=0, column=0, sticky="w")
        ttk.Entry(grid, textvariable=self.v_ainf, width=10).grid(row=0, column=1, padx=(6, 0))
        ttk.Label(grid, text="Starting conc. c₀ (in the unit above)").grid(row=1, column=0, sticky="w")
        ttk.Entry(grid, textvariable=self.v_c0, width=10).grid(row=1, column=1, padx=(6, 0))
        ttk.Label(f, text="Leave ε empty to work in absorbance (first-order k doesn't need it).",
                  style="Hint.TLabel", wraplength=300).pack(anchor="w", pady=(4, 0))
        for v in (self.v_wl, self.v_path, self.v_species, self.v_eps, self.v_mw, self.v_unit, self.v_c0,
                  self.v_ainf, self.v_method):
            v.trace_add("write", lambda *a: self._sample_changed())

        # --- simulator (demo only)
        if self.demo:
            f = self._section(p, "Simulator (demo only)")
            self.v_sim_c = tk.StringVar(value="10")
            self.v_sim_k = tk.StringVar(value="0.01")
            grid = ttk.Frame(f)
            grid.pack(fill="x")
            ttk.Label(grid, text="Dye in cuvette (µM)").grid(row=0, column=0, sticky="w")
            ttk.Entry(grid, textvariable=self.v_sim_c, width=8).grid(row=0, column=1, padx=4)
            ttk.Button(grid, text="Put in", command=self.sim_put_in).grid(row=0, column=2)
            ttk.Label(grid, text="Fading k (s⁻¹)").grid(row=1, column=0, sticky="w")
            ttk.Entry(grid, textvariable=self.v_sim_k, width=8).grid(row=1, column=1, padx=4)
            ttk.Button(grid, text="Start reaction", command=self.sim_start).grid(row=1, column=2)
            ttk.Button(f, text="Press Print on the simulated GENESYS", command=self.sim_print).pack(
                anchor="w", pady=(4, 0))
            ttk.Label(f, text="The simulated dye has ε = 87,000 at 590 nm (a broad band); its wavelength "
                              "follows the box above. Made-up numbers for trying the app.",
                      style="Hint.TLabel", wraplength=300).pack(anchor="w", pady=(4, 0))

    def _figure(self, parent, nrows=1, ncols=1, **kw):
        fig = Figure(figsize=(6, 3.4), dpi=100, layout="constrained")
        axes = fig.subplots(nrows, ncols, **kw)
        canvas = FigureCanvasTkAgg(fig, master=parent)
        tb = NavigationToolbar2Tk(canvas, parent, pack_toolbar=False)
        tb.update()
        tb.pack(side="bottom", fill="x")
        canvas.get_tk_widget().pack(side="top", fill="both", expand=True)
        return fig, axes, canvas

    def _watermark(self, fig):
        if self.demo:
            fig.text(0.5, 0.5, "SIMULATED", fontsize=46, color="#7e22ce", alpha=0.10, ha="center",
                     va="center", rotation=20)

    def _build_live_tab(self):
        tab = ttk.Frame(self.nb, padding=4)
        self.nb.add(tab, text="Live")
        row = ttk.Frame(tab)
        row.pack(fill="x")
        self.v_live_y = tk.StringVar(value=self.s["live_y"])
        self.v_xunit = tk.StringVar(value=self.s["xunit"])
        ttk.Label(row, text="Plot").pack(side="left")
        cb = ttk.Combobox(row, textvariable=self.v_live_y, values=Y_CHOICES, state="readonly", width=16)
        cb.pack(side="left", padx=4)
        ttk.Label(row, text="Time in").pack(side="left", padx=(10, 0))
        cb2 = ttk.Combobox(row, textvariable=self.v_xunit, values=("s", "min"), state="readonly", width=5)
        cb2.pack(side="left", padx=4)
        for v in (self.v_live_y, self.v_xunit):
            v.trace_add("write", lambda *a: self._dirty.update({"live"}))
        self.lbl_live_note = ttk.Label(row, text="", style="Hint.TLabel")
        self.lbl_live_note.pack(side="left", padx=10)
        holder = ttk.Frame(tab)
        holder.pack(fill="both", expand=True)
        self.live_fig, self.live_ax, self.live_canvas = self._figure(holder)
        self._watermark(self.live_fig)

    def _build_kin_tab(self):
        tab = ttk.Frame(self.nb, padding=4)
        self.nb.add(tab, text="Kinetics")
        row = ttk.Frame(tab)
        row.pack(fill="x")
        self.v_kfrom = tk.StringVar(value=self.s["kin_from"])
        self.v_kto = tk.StringVar(value=self.s["kin_to"])
        self.v_kauto = tk.BooleanVar(value=True)
        ttk.Label(row, text="Fit readings from").pack(side="left")
        ttk.Entry(row, textvariable=self.v_kfrom, width=7).pack(side="left", padx=2)
        ttk.Label(row, text="to").pack(side="left")
        ttk.Entry(row, textvariable=self.v_kto, width=7).pack(side="left", padx=2)
        ttk.Label(row, text="s (blank = whole run)").pack(side="left")
        ttk.Button(row, text="Fit now", command=lambda: self._dirty.add("kin")).pack(side="left", padx=8)
        ttk.Checkbutton(row, text="Refit as readings arrive", variable=self.v_kauto).pack(side="left")
        for v in (self.v_kfrom, self.v_kto):
            v.trace_add("write", lambda *a: self._dirty.add("kin"))
        self.lbl_kin_basis = ttk.Label(tab, text="", style="Hint.TLabel")
        self.lbl_kin_basis.pack(fill="x", pady=(4, 0))
        cols = ("order", "k", "units", "r2", "half", "x0", "n")
        self.tv_kin = ttk.Treeview(tab, columns=cols, show="headings", height=3)
        for c, (title, w) in zip(cols, [("Order", 150), ("k", 110), ("k units", 110), ("R²", 90),
                                         ("Half-life", 110), ("Start amount", 110), ("Points", 70)]):
            self.tv_kin.heading(c, text=title)
            self.tv_kin.column(c, width=w, anchor="center")
        self.tv_kin.tag_configure("best", background="#dcfce7")
        self.tv_kin.pack(fill="x", pady=4)
        self.lbl_kin_rate = ttk.Label(tab, text="", style="Sub.TLabel")
        self.lbl_kin_rate.pack(fill="x")
        holder = ttk.Frame(tab)
        holder.pack(fill="both", expand=True)
        self.kin_fig, self.kin_axes, self.kin_canvas = self._figure(holder, 1, 3)
        self._watermark(self.kin_fig)

    def _build_cal_tab(self):
        tab = ttk.Frame(self.nb, padding=4)
        self.nb.add(tab, text="Calibration")
        left = ttk.Frame(tab, width=330)
        left.pack(side="left", fill="y", padx=(0, 6))
        self.v_cal_c = tk.StringVar()
        self.v_cal_unit = tk.StringVar(value=self.s["cal_unit"])
        row = ttk.Frame(left)
        row.pack(fill="x")
        ttk.Label(row, text="Standard").pack(side="left")
        ttk.Entry(row, textvariable=self.v_cal_c, width=9).pack(side="left", padx=3)
        ttk.Combobox(row, textvariable=self.v_cal_unit, values=an.UNITS, state="readonly", width=7).pack(side="left")
        self.v_cal_unit.trace_add("write", lambda *a: self._recalibrate())
        ttk.Button(left, text="Add the current reading as this standard", command=self.add_standard).pack(
            fill="x", pady=4)
        self.tv_cal = ttk.Treeview(left, columns=("c", "a", "wl"), show="headings", height=7)
        for c, t, w in (("c", "Concentration", 110), ("a", "Absorbance", 100), ("wl", "λ (nm)", 70)):
            self.tv_cal.heading(c, text=t)
            self.tv_cal.column(c, width=w, anchor="center")
        self.tv_cal.pack(fill="x")
        row = ttk.Frame(left)
        row.pack(fill="x", pady=4)
        ttk.Button(row, text="Remove selected", command=self.remove_standard).pack(side="left")
        ttk.Button(row, text="Clear all", command=self.clear_standards).pack(side="left", padx=4)
        self.lbl_cal = ttk.Label(left, text="", justify="left", wraplength=320)
        self.lbl_cal.pack(fill="x", pady=4)
        ttk.Label(left, text="Blank the GENESYS first (0 ABS/100%T), then put in each standard and add "
                             "its reading. Tick 'My calibration curve' under Sample to turn readings into "
                             "concentrations with this line.", style="Hint.TLabel", wraplength=320).pack(fill="x")
        holder = ttk.Frame(tab)
        holder.pack(side="left", fill="both", expand=True)
        self.cal_fig, self.cal_ax, self.cal_canvas = self._figure(holder)
        self._watermark(self.cal_fig)

    def _build_spec_tab(self):
        tab = ttk.Frame(self.nb, padding=4)
        self.nb.add(tab, text="Spectrum")
        left = ttk.Frame(tab, width=330)
        left.pack(side="left", fill="y", padx=(0, 6))
        ttk.Button(left, text="Add the current reading at the wavelength box λ",
                   command=self.add_spec_point).pack(fill="x")
        self.v_spec_auto = tk.BooleanVar(value=False)
        ttk.Checkbutton(left, text="Add every new reading here", variable=self.v_spec_auto).pack(anchor="w", pady=4)
        self.tv_spec = ttk.Treeview(left, columns=("wl", "a"), show="headings", height=8)
        for c, t in (("wl", "λ (nm)"), ("a", "Absorbance")):
            self.tv_spec.heading(c, text=t)
            self.tv_spec.column(c, width=110, anchor="center")
        self.tv_spec.pack(fill="x")
        row = ttk.Frame(left)
        row.pack(fill="x", pady=4)
        ttk.Button(row, text="Remove selected", command=self.remove_spec).pack(side="left")
        ttk.Button(row, text="Clear all", command=self.clear_spec).pack(side="left", padx=4)
        self.lbl_spec = ttk.Label(left, text="", style="Sub.TLabel")
        self.lbl_spec.pack(fill="x", pady=4)
        ttk.Button(left, text="Use λmax as the working wavelength", command=self.use_lmax).pack(fill="x")
        ttk.Label(left, text="The GENESYS 20 can't scan or report its wavelength over serial. Step the "
                             "wavelength by hand (re-blank at each one), type it in the wavelength box, "
                             "then add the reading. With 'Print WL = Yes', Print readings bring their own "
                             "wavelength.", style="Hint.TLabel", wraplength=320).pack(fill="x", pady=6)
        holder = ttk.Frame(tab)
        holder.pack(side="left", fill="both", expand=True)
        self.spec_fig, self.spec_ax, self.spec_canvas = self._figure(holder)
        self._watermark(self.spec_fig)

    def _build_raw_tab(self):
        tab = ttk.Frame(self.nb, padding=4)
        self.nb.add(tab, text="Raw data")
        top = ttk.Frame(tab)
        top.pack(fill="x")
        self.v_hex = tk.BooleanVar(value=False)
        ttk.Checkbutton(top, text="Show bytes as hex", variable=self.v_hex).pack(side="left")
        ttk.Button(top, text="Clear view", command=lambda: self.txt.delete("1.0", "end")).pack(side="left", padx=6)
        ttk.Label(top, text="Bare numbers are read as").pack(side="left", padx=(16, 2))
        self.v_kind = tk.StringVar(value=self.s["default_kind"])
        ttk.Combobox(top, textvariable=self.v_kind, values=("A", "%T"), state="readonly", width=4).pack(side="left")
        ver = ttk.LabelFrame(tab, text="Check the format once", padding=6)
        ver.pack(fill="x", pady=6)
        self.lbl_verify = ttk.Label(ver, text="No reading yet.", wraplength=900)
        self.lbl_verify.pack(anchor="w")
        row = ttk.Frame(ver)
        row.pack(anchor="w", pady=(4, 0))
        ttk.Button(row, text="Yes, it matches the GENESYS display", command=lambda: self.verify(True)).pack(side="left")
        ttk.Button(row, text="No, it doesn't", command=lambda: self.verify(False)).pack(side="left", padx=6)
        ttk.Label(ver, text="First connection test without any commands: on the GENESYS press Utility, scroll to "
                            "RS232, and press Print. Its RS232 settings should appear below. Then set Utility > "
                            "Printer setup to Serial, measure, and press Print.",
                  style="Hint.TLabel", wraplength=900).pack(anchor="w", pady=(6, 0))
        frame = ttk.Frame(tab)
        frame.pack(fill="both", expand=True)
        self.txt = tk.Text(frame, font=("Consolas", 10), wrap="none", height=20)
        sb = ttk.Scrollbar(frame, command=self.txt.yview)
        self.txt.configure(yscrollcommand=sb.set)
        self.txt.pack(side="left", fill="both", expand=True)
        sb.pack(side="right", fill="y")
        for tag, color in (("in", "#111827"), ("out", "#7e22ce"), ("info", "#2563eb"), ("warn", "#b91c1c"),
                           ("reading", "#15803d"), ("dim", "#6b7280")):
            self.txt.tag_configure(tag, foreground=color)

    # ================================================================== connection
    def refresh_ports(self):
        ports = list_ports()
        self.cb_port["values"] = ports
        if ports and not any(p.split(" ")[0] == self.v_port.get().split(" ")[0] for p in ports):
            prolific = [p for p in ports if "prolific" in p.lower() or "usb-to-serial" in p.lower()]
            if prolific:
                self.v_port.set(prolific[0])

    def serial_settings(self) -> g.SerialSettings:
        inv_par = {v: k for k, v in PARITY_NAMES.items()}
        inv_flow = {v: k for k, v in FLOW_NAMES.items()}
        return g.SerialSettings(port=self.v_port.get().split(" ")[0] or "COM11", baud=int(self.v_baud.get()),
                                bits=int(self.v_bits.get()), parity=inv_par[self.v_parity.get()],
                                stop=int(self.v_stop.get()), flow=inv_flow[self.v_flow.get()],
                                terminator=self.v_term.get())

    def toggle_connect(self):
        if self.link.is_open:
            self.disconnect()
        else:
            self.connect()

    def connect(self):
        ser = self.serial_settings()
        self.s["serial"] = ser.to_dict()
        port = None
        if self.demo:
            ser.port = "SIMULATED"
            self.sim = g.SimulatedGenesys(terminator=g.TERMINATORS[ser.terminator])
            self._sync_sim()
            port = self.sim
        try:
            self.link.open(ser, port=port)
        except Exception as exc:
            messagebox.showerror(APP_NAME, f"Could not open {ser.port}:\n\n{exc}\n\nIs another program "
                                           "(or another copy of this app) using the port?")
            return
        self.log = g.SessionLog(self.logs, simulated=self.demo)
        self.log.write("info", b"", ser, note="connected")
        self.btn_connect.configure(text="Disconnect")
        self._set_status("sim" if self.demo else "open")
        self._raw(f"Connected: {ser.describe()}" + ("  [SIMULATED]" if self.demo else ""), "info")
        self.on_poll_toggle()
        self.save_settings()

    def disconnect(self):
        self.link.poll_interval = 0
        self.link.close()
        if self.log:
            self.log.close()
        self.btn_connect.configure(text="Connect")
        self._set_status("off")
        self._raw("Disconnected; the port is free for other programs.", "info")

    def send(self, cmd: str):
        if not self.link.is_open:
            messagebox.showinfo(APP_NAME, "Connect first.")
            return
        try:
            self.link.send(cmd)
        except Exception as exc:
            self._raw(f"Could not send {cmd}: {exc}", "warn")

    def identify(self):
        self.send("MODEL")       # the link sends them one at a time
        self.send("REV")

    def on_poll_toggle(self):
        want = self.v_poll.get()
        try:
            secs = max(0.5, float(self.v_poll_s.get()))
        except ValueError:
            secs = 2.0
        self.s["poll_s"] = str(secs)
        if want and not self.demo and not self.s.get("polling_ack"):
            ok = messagebox.askyesno(
                APP_NAME,
                "Hands-free readings work by sending the query SND to the GENESYS every few seconds.\n\n"
                "SND only asks for the current absorbance (4 decimals); it changes nothing on the "
                "instrument. It isn't in Thermo's user guide, but it was checked on this GENESYS 20 "
                "(firmware 2.10). If the GENESYS stops answering, the app turns this off and you can "
                "keep using Print.\n\nThe GENESYS can't report its wavelength, so keep the wavelength box "
                "in the Sample section up to date.\n\nTurn on hands-free readings?")
            if not ok:
                self.v_poll.set(False)
                want = False
            else:
                self.s["polling_ack"] = True
        self.snd_misses = 0
        self.link.poll_interval = secs if want else 0.0

    # ================================================================== event pump
    def _pump(self):
        if self._closing:
            return
        n = 0
        while n < 500:
            try:
                ev = self.link.events.get_nowait()
            except Exception:
                break
            n += 1
            self._handle(ev)
        now = time.time()
        if self.t0 is not None:
            el = int(now - self.t0)
            self.lbl_run.configure(text=f"Run time {el // 60:02d}:{el % 60:02d}  |  {len(self.readings)} readings")
        if self._dirty and now - self._last_draw > 0.35:
            self._redraw()
            self._last_draw = now
        self.root.after(100, self._pump)

    def _handle(self, ev):
        kind = ev[0]
        if kind == "in":
            if self.log:
                self.log.write("in", ev[2])
            if self.v_hex.get():
                self._raw(f"<- {ev[2].hex(' ')}", "dim", ev[1])
        elif kind == "out":
            if self.log:
                self.log.write("out", ev[2])
            self._raw(f"-> {ev[2].decode('ascii', 'replace').strip()}", "out", ev[1])
        elif kind == "line":
            self._on_line(*ev[1:])
        elif kind == "timeout":
            cmd = ev[2]
            self._raw(f"No reply to {cmd} within a few seconds.", "warn", ev[1])
            if cmd == "SND":
                self.snd_misses += 1
                if self.snd_misses >= 3 and self.v_poll.get():
                    self.v_poll.set(False)
                    self.link.poll_interval = 0
                    self._raw("Turned hands-free readings off: the GENESYS didn't answer SND three times. "
                              "Check baud/parity/handshake/line end against Utility > RS232 on the "
                              "instrument, or use Print.", "warn")
        elif kind == "error":
            self._raw(f"Port error: {ev[2]}", "warn", ev[1])
            messagebox.showwarning(APP_NAME, f"The serial port stopped working:\n\n{ev[2]}")
        elif kind == "closed":
            if self.btn_connect.cget("text") == "Disconnect":
                self.btn_connect.configure(text="Connect")
                self._set_status("off")

    def _on_line(self, t: float, raw: bytes, source: str, cmd: str | None):
        text = g.decode(raw)
        if cmd in ("MODEL", "REV"):
            self.ident[cmd] = text.strip()
            self._raw(f"<- {text.strip()}   ({cmd} reply)", "info", t)
            self._status_text()
            return
        if cmd == "SND":
            self.snd_misses = 0
        default_kind = "A" if cmd == "SND" else self.v_kind.get()
        r = g.parse_line(text, decimal=self.v_decimal.get(), default_kind=default_kind)
        if r is None:
            self._raw(f"<- {text}" + ("   (SND reply, not understood)" if cmd else ""), "in", t)
            return
        if self.t0 is None:
            self.t0 = t
        wl_set = self._num(self.v_wl)
        row = {
            "t": t - self.t0, "wall": t, "source": "SND" if cmd == "SND" else "Print",
            "kind": r.kind, "value": r.value, "unit": r.unit, "wl_printed": r.wl,
            "wl": r.wl if r.wl is not None else wl_set, "A": r.absorbance, "T": r.transmittance,
            "text": r.text, "simulated": self.demo, "verified": bool(self.s.get("format_verified")),
        }
        conc, cunit, how = self._conc_of(row)
        row.update(conc=conc, conc_unit=cunit, conc_how=how)
        self.readings.append(row)
        self.last = row
        self.last_parsed_text = r.text
        self._append_csv(row)
        tagtxt = f"{r.value:g} {r.unit or '(' + r.kind + ', no unit printed)'}"
        self._raw(f"<- {r.text}    => {tagtxt}" + (f" at {r.wl:g} nm" if r.wl else ""), "reading", t)
        self._show_now(row)
        if self.v_spec_auto.get() and row["A"] is not None and row["wl"]:
            self._add_spec(row["wl"], row["A"])
        self.lbl_verify.configure(text=f"Last reading as the app understood it:  {tagtxt}"
                                       + (f" at {r.wl:g} nm" if r.wl else "")
                                       + f"      (raw text: “{r.text}”)")
        if not self.demo:
            self._set_status("verified" if self.s.get("format_verified") else "bytes")
        self._dirty.update({"live", "cal"})
        if self.v_kauto.get():
            self._dirty.add("kin")

    # ================================================================== readings -> numbers
    @staticmethod
    def _num(var) -> float | None:
        try:
            v = float(str(var.get()).replace(",", "."))
        except (ValueError, tk.TclError):
            return None
        return v

    def _conc_of(self, row) -> tuple[float | None, str, str]:
        if row["kind"] == "C":
            return row["value"], row["unit"] or "C", "instrument"
        a = row["A"]
        if a is None:
            return None, "", ""
        if self.v_method.get() == "cal":
            if self.cal is None:
                return None, "", ""
            return self.cal.conc(a), self.cal.unit, "calibration"
        eps, b = self._num(self.v_eps), self._num(self.v_path)
        if not eps or not b:
            return None, "", ""
        c = an.from_molar(an.beer_lambert_conc(a, eps, b), self.v_unit.get(), self._num(self.v_mw))
        return c, self.v_unit.get(), "Beer-Lambert"

    def _show_now(self, row):
        if row["kind"] == "C":
            self.lbl_big.configure(text=f"{row['value']:g} {row['unit']}")
            self.lbl_sub.configure(text="(GENESYS in concentration mode)")
        else:
            a = row["A"]
            places = 4 if row["source"] == "SND" else 3      # SND answers with 4 decimals
            self.lbl_big.configure(text=f"{a:.{places}f} A" if a is not None else "---")
            parts = [f"{row['T']:.1f} %T"] if row["T"] is not None else []
            conc, cu, _ = self._conc_of(row)
            if conc is not None:
                parts.append(f"c = {fmt_sig(conc)} {cu}")
            self.lbl_sub.configure(text="     ".join(parts))
        when = datetime.fromtimestamp(row["wall"]).strftime("%H:%M:%S")
        src = "hands-free (SND)" if row["source"] == "SND" else "Print"
        wl = f" at {row['wl']:g} nm" if row["wl"] else ""
        sim = "  SIMULATED" if self.demo else ""
        self.lbl_src.configure(text=f"{src}{wl}, {when}, t = {row['t']:.1f} s{sim}")

    def _sample_changed(self):
        for k, v in (("wl", self.v_wl), ("path", self.v_path), ("species", self.v_species), ("eps", self.v_eps),
                     ("mw", self.v_mw), ("unit", self.v_unit), ("c0", self.v_c0), ("a_inf", self.v_ainf),
                     ("conc_method", self.v_method)):
            self.s[k] = v.get()
        for row in self.readings:
            row["conc"], row["conc_unit"], row["conc_how"] = self._conc_of(row)
        if self.last:
            self._show_now(self.last)
        if self.sim is not None:
            self._sync_sim()
        self._dirty.update({"live", "kin", "cal", "spec"})

    # ================================================================== run / events / saving
    def new_run(self):
        if self.readings and not messagebox.askyesno(
                APP_NAME, f"Start a new run? The {len(self.readings)} readings on screen are cleared "
                          "(they are already saved in the logs folder)."):
            return
        self.readings.clear()
        self.marks.clear()
        self.last = None
        self.t0 = time.time()
        self.csv_path = None
        self.lbl_big.configure(text="---")
        self.lbl_sub.configure(text="")
        self.lbl_src.configure(text="New run started: t = 0 now")
        self._dirty.update({"live", "kin"})

    def mark_event(self):
        if self.t0 is None:
            self.t0 = time.time()
        t = time.time() - self.t0
        label = simpledialog.askstring(APP_NAME, f"Label for the event at t = {t:.1f} s "
                                                 "(e.g. 'added NaOH'):", parent=self.root)
        if label:
            self.marks.append((t, label))
            self._raw(f"Event at t = {t:.1f} s: {label}", "info")
            if self.log:
                self.log.write("info", b"", note=f"event t={t:.3f}s: {label}")
            self._dirty.add("live")

    CSV_FIELDS = ("t_s", "clock", "source", "value", "kind", "unit_printed", "wavelength_nm", "absorbance",
                  "transmittance_pct", "concentration", "conc_unit", "conc_from", "raw_text", "format_checked",
                  "simulated")

    def _csv_row(self, row):
        return [f"{row['t']:.3f}", datetime.fromtimestamp(row["wall"]).isoformat(timespec="milliseconds"),
                row["source"], row["value"], row["kind"], row["unit"], "" if row["wl"] is None else row["wl"],
                "" if row["A"] is None else f"{row['A']:.4f}", "" if row["T"] is None else f"{row['T']:.3f}",
                "" if row["conc"] is None else f"{row['conc']:.6g}", row["conc_unit"], row["conc_how"],
                row["text"], "yes" if row["verified"] else "no", "yes" if row["simulated"] else "no"]

    def _append_csv(self, row):
        """Every reading is also appended to logs/readings-<time>.csv as it arrives."""
        try:
            if self.csv_path is None:
                os.makedirs(self.logs, exist_ok=True)
                stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
                self.csv_path = os.path.join(self.logs, f"readings-{stamp}{'-SIMULATED' if self.demo else ''}.csv")
                with open(self.csv_path, "w", newline="", encoding="utf-8-sig") as fh:
                    csv.writer(fh).writerow(self.CSV_FIELDS)
            with open(self.csv_path, "a", newline="", encoding="utf-8-sig") as fh:
                csv.writer(fh).writerow(self._csv_row(row))
        except OSError as exc:
            self.statusbar.configure(text=f"Could not write the readings file: {exc}")

    def save_csv(self):
        if not self.readings:
            messagebox.showinfo(APP_NAME, "No readings yet.")
            return
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        path = filedialog.asksaveasfilename(
            parent=self.root, title="Save readings", defaultextension=".csv", initialdir=self.logs,
            initialfile=f"genesys20-{stamp}{'-SIMULATED' if self.demo else ''}.csv", filetypes=[("CSV", "*.csv")])
        if not path:
            return
        with open(path, "w", newline="", encoding="utf-8-sig") as fh:
            w = csv.writer(fh)
            w.writerow(self.CSV_FIELDS)
            for row in self.readings:
                w.writerow(self._csv_row(row))
            if self.marks:
                w.writerow([])
                w.writerow(["event_t_s", "event"])
                for t, lab in self.marks:
                    w.writerow([f"{t:.3f}", lab])
        self.statusbar.configure(text=f"Saved {len(self.readings)} readings to {path}")

    def verify(self, ok: bool):
        if not self.last_parsed_text:
            messagebox.showinfo(APP_NAME, "Take a reading first (Print on the GENESYS, or Ask once).")
            return
        if ok:
            self.s["format_verified"] = True
            self.s["verified_example"] = f"{self.last_parsed_text}  ({self.serial_settings().describe()})"
            self._raw(f"Format checked against the display: {self.last_parsed_text}", "info")
            if not self.demo and self.link.is_open:
                self._set_status("verified")
        else:
            self.s["format_verified"] = False
            messagebox.showinfo(APP_NAME, "Thanks - the readings stay marked as unchecked. Save the raw log "
                                          "(logs folder) and note what the display showed, so the parser "
                                          "can be fixed for this format.")
            if not self.demo and self.link.is_open:
                self._set_status("bytes")
        self.save_settings()

    # ================================================================== calibration
    def _load_tables(self):
        for c, a, wl in self.s.get("standards", []):
            self.tv_cal.insert("", "end", values=(fmt_sig(c), f"{a:.4f}", "" if wl is None else f"{wl:g}"))
        for wl, a in self.s.get("spectrum", []):
            self.tv_spec.insert("", "end", values=(f"{wl:g}", f"{a:.4f}"))

    def add_standard(self):
        c = self._num(self.v_cal_c)
        if c is None:
            messagebox.showinfo(APP_NAME, "Type the standard's concentration first.")
            return
        if not self.last or self.last["A"] is None:
            messagebox.showinfo(APP_NAME, "No absorbance reading yet - read the standard first.")
            return
        a, wl = self.last["A"], self.last["wl"]
        self.s["standards"].append([c, a, wl])
        self.tv_cal.insert("", "end", values=(fmt_sig(c), f"{a:.4f}", "" if wl is None else f"{wl:g}"))
        self.v_cal_c.set("")
        self._recalibrate()

    def remove_standard(self):
        rows = list(self.tv_cal.get_children())
        for item in self.tv_cal.selection():
            i = rows.index(item)
            self.s["standards"][i] = None
        self.s["standards"] = [x for x in self.s["standards"] if x is not None]
        for item in self.tv_cal.selection():
            self.tv_cal.delete(item)
        self._recalibrate()

    def clear_standards(self):
        if self.s["standards"] and messagebox.askyesno(APP_NAME, "Remove all standards?"):
            self.s["standards"] = []
            self.tv_cal.delete(*self.tv_cal.get_children())
            self._recalibrate()

    def _recalibrate(self):
        self.s["cal_unit"] = self.v_cal_unit.get()
        pts = self.s.get("standards", [])
        self.cal = None
        msg = "Add at least two standards (three or more for LOD/LOQ)."
        if len(pts) >= 2:
            try:
                self.cal = an.calibrate([p[0] for p in pts], [p[1] for p in pts], self.v_cal_unit.get(),
                                        self._num(self.v_path) or 1.0)
            except ValueError as exc:
                msg = str(exc)
        if self.cal:
            f = self.cal.fit
            u = self.cal.unit
            lines = [f"A = {fmt_sig(f.slope)} · c + {fmt_sig(f.intercept)}   (c in {u})",
                     f"R² = {f.r2:.5f}   n = {f.n}"]
            if self.cal.lod is not None:
                lines.append(f"LOD ≈ {fmt_sig(self.cal.lod, 3)} {u}   LOQ ≈ {fmt_sig(self.cal.loq, 3)} {u}")
            if self.cal.eps is not None:
                lines.append(f"ε = slope / b = {fmt_sig(self.cal.eps)} L mol⁻¹ cm⁻¹")
            if f.r2 < 0.99:
                lines.append("R² below 0.99: check for an outlier or a standard outside the linear range.")
            msg = "\n".join(lines)
        self.lbl_cal.configure(text=msg)
        for row in self.readings:
            row["conc"], row["conc_unit"], row["conc_how"] = self._conc_of(row)
        self._dirty.update({"cal", "live"})

    # ================================================================== spectrum
    def add_spec_point(self):
        if not self.last or self.last["A"] is None:
            messagebox.showinfo(APP_NAME, "No absorbance reading yet.")
            return
        wl = self.last["wl_printed"] or self._num(self.v_wl)
        if not wl:
            messagebox.showinfo(APP_NAME, "Type the wavelength set on the GENESYS in the Sample box first.")
            return
        self._add_spec(wl, self.last["A"])

    def _add_spec(self, wl, a):
        self.s["spectrum"] = [p for p in self.s["spectrum"] if p[0] != wl] + [[wl, a]]
        self.s["spectrum"].sort()
        self.tv_spec.delete(*self.tv_spec.get_children())
        for w, aa in self.s["spectrum"]:
            self.tv_spec.insert("", "end", values=(f"{w:g}", f"{aa:.4f}"))
        self._dirty.add("spec")

    def remove_spec(self):
        rows = list(self.tv_spec.get_children())
        drop = {rows.index(i) for i in self.tv_spec.selection()}
        self.s["spectrum"] = [p for i, p in enumerate(self.s["spectrum"]) if i not in drop]
        for item in self.tv_spec.selection():
            self.tv_spec.delete(item)
        self._dirty.add("spec")

    def clear_spec(self):
        if self.s["spectrum"] and messagebox.askyesno(APP_NAME, "Remove all spectrum points?"):
            self.s["spectrum"] = []
            self.tv_spec.delete(*self.tv_spec.get_children())
            self._dirty.add("spec")

    def use_lmax(self):
        pts = self.s["spectrum"]
        lm = an.lambda_max([p[0] for p in pts], [p[1] for p in pts]) if pts else None
        if lm:
            self.v_wl.set(f"{round(lm[0])}")
            messagebox.showinfo(APP_NAME, f"Wavelength box set to {round(lm[0])} nm. Set the same wavelength "
                                          "on the GENESYS and blank it again.")

    # ================================================================== drawing
    def _redraw(self):
        current = self.nb.index(self.nb.select())
        todo = {0: "live", 1: "kin", 2: "cal", 3: "spec"}.get(current)
        if todo and todo in self._dirty:
            self._dirty.discard(todo)
            getattr(self, f"_draw_{todo}")()
        if "kin" in self._dirty and current != 1:
            pass  # drawn when the tab is shown

    def _times(self, rows):
        t = np.array([r["t"] for r in rows], dtype=float)
        return (t / 60.0, "min") if self.v_xunit.get() == "min" else (t, "s")

    def _draw_live(self):
        ax = self.live_ax
        ax.clear()
        which = self.v_live_y.get()
        if which == "Absorbance":
            rows = [r for r in self.readings if r["A"] is not None]
            y = [r["A"] for r in rows]
            ylab = "Absorbance (A)"
        elif which == "%Transmittance":
            rows = [r for r in self.readings if r["T"] is not None]
            y = [r["T"] for r in rows]
            ylab = "%T"
        else:
            rows = [r for r in self.readings if r["conc"] is not None]
            y = [r["conc"] for r in rows]
            unit = rows[-1]["conc_unit"] if rows else self.v_unit.get()
            ylab = f"Concentration ({unit})"
        note = ""
        if which == "Concentration" and not rows and self.readings:
            note = ("Enter ε (or pick the calibration curve) under Sample to see concentrations."
                    if self.v_method.get() == "beer" else "Add calibration standards first.")
        if rows:
            t, tu = self._times(rows)
            src = np.array([r["source"] for r in rows])
            y = np.array(y, dtype=float)
            ax.plot(t, y, "-", color="#94a3b8", lw=1, zorder=1)
            for s_name, style in (("SND", dict(marker="o", ms=4, color="#2563eb", label="hands-free (SND)")),
                                  ("Print", dict(marker="D", ms=6, color="#ea580c", label="Print"))):
                m = src == s_name
                if m.any():
                    ax.plot(t[m], y[m], ls="none", zorder=2, **style)
            ax.legend(loc="best", fontsize=8)
        else:
            tu = self.v_xunit.get()
        for tm, lab in self.marks:
            x = tm / 60 if tu == "min" else tm
            ax.axvline(x, color="#7e22ce", ls="--", lw=1)
            ax.annotate(lab, (x, 1), xycoords=("data", "axes fraction"), rotation=90, va="top", ha="right",
                        fontsize=8, color="#7e22ce")
        ax.set_xlabel(f"Time ({tu})")
        ax.set_ylabel(ylab)
        title = "Readings"
        if not self.demo and self.readings and not self.s.get("format_verified"):
            title += "  (format not yet checked against the display)"
        ax.set_title(title, fontsize=10)
        ax.grid(alpha=0.3)
        self.lbl_live_note.configure(text=note)
        self.live_canvas.draw_idle()

    def _kin_window(self):
        rows = [r for r in self.readings if r["A"] is not None]
        lo, hi = self._num(self.v_kfrom), self._num(self.v_kto)
        if lo is not None:
            rows = [r for r in rows if r["t"] >= lo]
        if hi is not None:
            rows = [r for r in rows if r["t"] <= hi]
        return rows

    def _draw_kin(self):
        self.s["kin_from"], self.s["kin_to"] = self.v_kfrom.get(), self.v_kto.get()
        for ax in self.kin_axes:
            ax.clear()
            ax.grid(alpha=0.3)
        self.tv_kin.delete(*self.tv_kin.get_children())
        rows = self._kin_window()
        self.kin = None
        msg = ""
        if len(rows) < 3:
            msg = "Need at least 3 absorbance readings in the fit window."
        else:
            t = np.array([r["t"] for r in rows])
            a = np.array([r["A"] for r in rows])
            eps = self._num(self.v_eps)
            if self.v_method.get() == "cal" and self.cal and self.cal.eps:
                eps = self.cal.eps
            unit = self.v_unit.get()
            c0 = self._num(self.v_c0)
            try:
                c0_m = an.to_molar(c0, unit, self._num(self.v_mw)) if c0 else None
                x, xu, basis = an.reactant_amount(a, self.v_species.get(), eps=eps, path_cm=self._num(self.v_path) or 1.0,
                                                  a_inf=self._num(self.v_ainf), c0_m=c0_m, unit=unit,
                                                  mw=self._num(self.v_mw))
                self.kin = an.fit_kinetics(t, x, xu, basis)
            except ValueError as exc:
                msg = str(exc)
        if self.kin:
            k = self.kin
            names = {0: "Zero order  [R] vs t", 1: "First order  ln[R] vs t", 2: "Second order  1/[R] vs t"}
            ylabs = {0: f"[R] ({k.unit})", 1: f"ln [R]", 2: f"1/[R] ({k.unit}⁻¹)"}
            for o in (0, 1, 2):
                f = k.fits[o]
                self.tv_kin.insert("", "end", tags=("best",) if o == k.best else (), values=(
                    names[o].split("  ")[0] + ("  ← best fit" if o == k.best else ""), fmt_sig(f.k),
                    f.k_units, "-" if f.r2 is None else f"{f.r2:.5f}",
                    "-" if f.half_life_s is None else f"{fmt_sig(f.half_life_s)} s", fmt_sig(f.x0), f.used))
                ax = self.kin_axes[o]
                tt, yy = k.transformed(o)
                ax.plot(tt, yy, "o", ms=3, color="#2563eb")
                if f.fit is not None:
                    xs = np.array([tt.min(), tt.max()])
                    ax.plot(xs, f.fit.y(xs), "-", color="#dc2626", lw=1.5)
                    ax.set_title(f"{names[o]}\nR² = {f.r2:.4f}", fontsize=9,
                                 fontweight="bold" if o == k.best else "normal")
                else:
                    ax.set_title(names[o], fontsize=9)
                ax.set_xlabel("t (s)")
                ax.set_ylabel(ylabs[o])
            rate = k.initial_rate
            rate_txt = "" if rate is None else f"Initial rate ≈ {fmt_sig(rate)} {k.unit} s⁻¹   "
            best = k.fits[k.best] if k.best is not None else None
            if best and best.k is not None:
                rate_txt += f"Best straight line: order {k.best}, k = {fmt_sig(best.k)} {best.k_units}"
            self.lbl_kin_rate.configure(text=rate_txt)
            msg = (f"Tracking {k.basis}.  {len(rows)} readings, t = {rows[0]['t']:.0f}-{rows[-1]['t']:.0f} s.  "
                   "R² values from different transforms are a guide, not proof of the order: "
                   "also look for curvature in the plots.")
        else:
            self.lbl_kin_rate.configure(text="")
        self.lbl_kin_basis.configure(text=msg)
        self.kin_canvas.draw_idle()

    def _draw_cal(self):
        ax = self.cal_ax
        ax.clear()
        ax.grid(alpha=0.3)
        pts = self.s.get("standards", [])
        u = self.v_cal_unit.get()
        if pts:
            c = np.array([p[0] for p in pts])
            a = np.array([p[1] for p in pts])
            ax.plot(c, a, "o", color="#2563eb", label="standards")
            if self.cal:
                xs = np.linspace(min(0, c.min()), c.max() * 1.05, 50)
                ax.plot(xs, self.cal.fit.y(xs), "-", color="#dc2626", lw=1.5,
                        label=f"fit, R² = {self.cal.fit.r2:.4f}")
                if self.last and self.last["A"] is not None:
                    ca = self.cal.conc(self.last["A"])
                    ax.plot([ca], [self.last["A"]], "*", ms=14, color="#ea580c",
                            label=f"latest reading → {fmt_sig(ca)} {u}")
            ax.legend(loc="best", fontsize=8)
        ax.set_xlabel(f"Concentration ({u})")
        ax.set_ylabel("Absorbance (A)")
        ax.set_title("Calibration curve (Beer-Lambert check)", fontsize=10)
        self.cal_canvas.draw_idle()

    def _draw_spec(self):
        ax = self.spec_ax
        ax.clear()
        ax.grid(alpha=0.3)
        pts = self.s.get("spectrum", [])
        if pts:
            wl = np.array([p[0] for p in pts])
            a = np.array([p[1] for p in pts])
            ax.plot(wl, a, "o-", color="#2563eb", ms=4)
            lm = an.lambda_max(wl, a)
            if lm:
                ax.axvline(lm[0], color="#dc2626", ls="--", lw=1)
                ax.annotate(f"λmax ≈ {lm[0]:.0f} nm", (lm[0], lm[1]), textcoords="offset points",
                            xytext=(6, 6), fontsize=9, color="#dc2626")
                self.lbl_spec.configure(text=f"λmax ≈ {lm[0]:.0f} nm  (A = {lm[1]:.3f})")
        else:
            self.lbl_spec.configure(text="")
        ax.set_xlabel("Wavelength (nm)")
        ax.set_ylabel("Absorbance (A)")
        ax.set_title("Absorption spectrum, point by point", fontsize=10)
        self.spec_canvas.draw_idle()

    # ================================================================== status / misc
    def _set_status(self, state: str):
        if self.demo and state != "off":
            state = "sim"
        self._state = state
        text, color = STATUS_STYLE[state]
        self.lbl_status.configure(text=text, bg=color)
        self._status_text()

    def _status_text(self):
        parts = []
        if self.ident:
            parts.append("Instrument: " + ", ".join(f"{k} {v}" for k, v in self.ident.items()))
        if self.log and self.log.path:
            parts.append(f"Raw log: {self.log.path}")
        if self.csv_path:
            parts.append(f"Readings: {self.csv_path}")
        self.statusbar.configure(text="   |   ".join(parts))

    def _raw(self, text: str, tag: str = "in", t: float | None = None):
        stamp = datetime.fromtimestamp(t or time.time()).strftime("%H:%M:%S.%f")[:-3]
        self.txt.insert("end", f"{stamp}  {text}\n", tag)
        if int(self.txt.index("end-1c").split(".")[0]) > 5000:
            self.txt.delete("1.0", "1000.0")
        self.txt.see("end")
        self._status_text()

    # demo helpers ------------------------------------------------------------
    def _sync_sim(self):
        if self.sim is None:
            return
        wl = self._num(self.v_wl)
        if wl:
            self.sim.wl = wl

    def sim_put_in(self):
        if self.sim is None:
            return
        c = self._num(self.v_sim_c)
        if c is not None:
            self.sim.cuvette.conc_m = c * 1e-6
            self.sim.cuvette.react_t0 = None
            self._raw(f"[simulator] cuvette now holds {c:g} µM dye", "dim")

    def sim_start(self):
        if self.sim is None:
            return
        k = self._num(self.v_sim_k)
        if k:
            self.sim.cuvette.k = k
        self.sim.cuvette.react_t0 = time.monotonic()
        self._raw(f"[simulator] reaction started, k = {self.sim.cuvette.k:g} s⁻¹", "dim")

    def sim_print(self):
        if self.sim is not None:
            self.sim.press_print()

    # ================================================================== settings / close
    def save_settings(self):
        try:
            self.s["serial"] = self.serial_settings().to_dict()
        except (ValueError, KeyError):
            pass
        self.s.update(decimal=self.v_decimal.get(), default_kind=self.v_kind.get(), live_y=self.v_live_y.get(),
                      xunit=self.v_xunit.get(), cal_unit=self.v_cal_unit.get())
        try:
            self.s["geometry"] = "zoomed" if self.root.state() == "zoomed" else self.root.geometry()
        except tk.TclError:
            pass
        try:
            tmp = self.settings_path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump(self.s, fh, indent=1)
            os.replace(tmp, self.settings_path)
        except OSError:
            pass

    def on_close(self):
        self._closing = True
        self.save_settings()
        self.link.poll_interval = 0
        self.link.close()
        if self.log:
            self.log.close()
        self.root.destroy()


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--demo", action="store_true", help="simulated GENESYS 20 (no hardware)")
    args = ap.parse_args(argv)
    root = tk.Tk()
    try:
        root.iconbitmap(os.path.join(HERE, "genesys20.ico"))
    except tk.TclError:
        pass
    App(root, demo=args.demo)
    root.mainloop()


if __name__ == "__main__":
    main()
