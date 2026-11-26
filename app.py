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
import io
import json
import math
import os
import queue
import re
import sys
import threading
import time
import webbrowser
from dataclasses import asdict
from datetime import datetime

import tkinter as tk
from tkinter import filedialog, messagebox, simpledialog, ttk
from tkinter import font as tkfont


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
from cycler import cycler  # noqa: E402  (ships with matplotlib)
from matplotlib.backends.backend_tkagg import FigureCanvasTkAgg, NavigationToolbar2Tk  # noqa: E402
from matplotlib.figure import Figure  # noqa: E402

# ---------------------------------------------------------------- look
# Neutral surfaces + one accent, from the dataviz reference palette. Chart series colours were run
# through its validator (blue/orange/aqua: CVD-safe all-pairs; aqua is light, so it always gets a label).
UI = {
    "page": "#f1f0ec", "surface": "#fcfcfb", "border": "#e1e0d9", "field": "#ffffff",
    "ink": "#0b0b0b", "ink2": "#52514e", "muted": "#898781", "axis": "#c3c2b7",
    "accent": "#2a78d6", "accent_hover": "#256abf", "accent_soft": "#e8f1fc",
    "header": "#1a1a19", "header_ink": "#f0efec", "header_muted": "#a6a59c",
    "button": "#f0efec", "button_hover": "#e6e5df",
}
SERIES = {"snd": "#2a78d6", "print": "#eb6834", "sample": "#1baf7a", "fit": "#52514e"}
FONT = "Segoe UI"

matplotlib.rcParams.update({
    "font.family": "sans-serif", "font.sans-serif": [FONT, "DejaVu Sans"], "font.size": 9,
    "figure.facecolor": UI["surface"], "axes.facecolor": UI["surface"], "savefig.facecolor": UI["surface"],
    "axes.edgecolor": UI["axis"], "axes.linewidth": 0.8, "axes.labelcolor": UI["ink2"],
    "axes.titlesize": 10, "axes.titleweight": "semibold", "axes.titlecolor": UI["ink"],
    "axes.titlelocation": "left", "axes.titlepad": 8,
    "axes.spines.top": False, "axes.spines.right": False,
    "axes.grid": True, "grid.color": UI["border"], "grid.linewidth": 0.8, "grid.linestyle": "-",
    "xtick.color": UI["axis"], "ytick.color": UI["axis"], "xtick.labelcolor": UI["ink2"],
    "ytick.labelcolor": UI["ink2"], "legend.frameon": False, "legend.fontsize": 8.5,
    "legend.labelcolor": UI["ink2"], "lines.linewidth": 2, "lines.solid_capstyle": "round",
    "axes.prop_cycle": cycler(color=[SERIES["snd"], SERIES["print"], SERIES["sample"]]),
})
# markers: >= 8 px with a 1.5 px ring in the surface colour so they stay legible over lines
MARK = dict(ms=6.5, mec=UI["surface"], mew=1.5)

import analysis as an  # noqa: E402
import genesys as g  # noqa: E402
import presets  # noqa: E402
import runs  # noqa: E402
import updater  # noqa: E402
from link import Link  # noqa: E402
from scan import ScanConfig, ScanEngine  # noqa: E402
from version import __version__ as VERSION  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
APP_NAME = "GENESYS 20 Live"
# sequential blue ramp (dataviz reference, steps 250 -> 700) for repeated scans: older = lighter
SCAN_RAMP = ["#86b6ef", "#6da7ec", "#5598e7", "#3987e5", "#2a78d6", "#256abf", "#1c5cab", "#184f95",
             "#104281", "#0d366b"]
SCAN_MODES = {"command": "Automatic: GENESYS moves itself (GTO)", "guided": "Guided: you set λ, press Print"}

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
    "data_dir": "",          # runs, saved presets and calibration curves; a OneDrive folder shares them
    "preset": "",
    "scan": {"start": 400, "stop": 700, "step": 10, "blank_first": True, "repeats": 1, "interval_min": 0,
             "settle_s": 1.5, "mode": "command"},
    # go-to-wavelength command: from Thermo's VISIONlite GENESYS 20 driver, verified on the instrument
    "wl_command": "GTO {wl}",
}

DEMO_SAMPLE = {"wl": "590", "eps": "87000", "unit": "µM", "species": "reactant", "a_inf": "0.004",
               "path": "1.000"}

STATUS_STYLE = {   # text, background, text colour (status colours always come with words)
    "off": ("Not connected", "#3a3a37", "#d8d7d0"),
    "open": ("Connected · waiting for a reading", "#fab219", "#0b0b0b"),
    "bytes": ("Receiving · format not yet checked", "#2a78d6", "#ffffff"),
    "verified": ("Receiving · format checked", "#0ca30c", "#ffffff"),
    "sim": ("SIMULATED instrument · not real data", "#6250d6", "#ffffff"),
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


ADAPTER_WORDS = ("prolific", "usb-to-serial", "usb serial", "ftdi", "ch340", "cp210", "usb-serial", "uart")


def list_ports() -> list[tuple[str, str]]:
    """[(device, friendly label)] for wired serial ports, USB adapters first. Bluetooth 'serial' links are
    left out: they are never the GENESYS and there are usually several of them."""
    try:
        from serial.tools import list_ports as lp
        found = list(lp.comports())
    except Exception:
        return []
    out = []
    for p in found:
        desc = p.description or ""
        if "bluetooth" in desc.lower():
            continue
        name = re.sub(r"\s*\(COM\d+\)\s*$", "", desc).replace(" Comm Port", "").strip() or "Serial port"
        out.append((p.device, f"{p.device}  ·  {name}"))

    def rank(item):
        num = int(re.sub(r"\D", "", item[0]) or 0)
        return (0 if any(w in item[1].lower() for w in ADAPTER_WORDS) else 1, num)

    return sorted(out, key=rank)


class ScrollFrame(ttk.Frame):
    """A vertically scrolling frame for the sidebar."""

    def __init__(self, master, width=330):
        super().__init__(master, style="Page.TFrame")
        self.canvas = tk.Canvas(self, width=width, highlightthickness=0, borderwidth=0, bg=UI["page"])
        bar = ttk.Scrollbar(self, orient="vertical", command=self.canvas.yview)
        self.body = ttk.Frame(self.canvas, style="Page.TFrame")
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
                 logs_dir: str | None = None, auto_connect: bool = True, data_dir: str | None = None,
                 check_updates: bool | None = None):
        self.root = root
        self.demo = demo
        default_settings = os.path.join(HERE, "settings.demo.json" if demo else "settings.json")
        self.settings_path = settings_path or default_settings
        self.s = load_settings(self.settings_path)
        if demo:
            for k, v in DEMO_SAMPLE.items():
                if not self.s.get(k):
                    self.s[k] = v
            self.s["scan"] = dict(self.s["scan"], mode="command")
        if not self.s.get("wl_command"):
            self.s["wl_command"] = DEFAULTS["wl_command"]
        if (self.s.get("scan") or {}).get("mode") not in SCAN_MODES:      # older settings ("sim")
            self.s["scan"] = dict(self.s.get("scan") or {}, mode="command")
        self.logs = logs_dir or os.path.join(HERE, "logs")
        self.data_dir = data_dir or self.s.get("data_dir") or os.path.join(HERE, "data")
        self.user_presets = presets.Store(os.path.join(self.data_dir, "presets.json"))
        self.curves = presets.Store(os.path.join(self.data_dir, "calibrations.json"))
        self.scan: ScanEngine | None = None
        self._scan_wait: str | None = None       # None | "settle" | "read" | "hold"
        self._scan_due = 0.0
        self._scan_set_wl: float | None = None
        self._scan_poll_restore = False
        self._scan_passes_seen = 0
        self._scan_retries = 0
        self._viewing: str | None = None          # name of a saved run loaded for viewing
        self._ui_calls: queue.SimpleQueue = queue.SimpleQueue()
        self._update_manifest: dict | None = None
        self._updating = False
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
        self._ports_q: queue.SimpleQueue | None = None

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
        if not demo:
            self._start_port_watch()
        root.after(100, self._pump)
        if demo and auto_connect:
            self.connect()
        if (not demo) if check_updates is None else check_updates:
            root.after(3000, self.check_updates)

    # ================================================================== layout
    def _style(self):
        r = self.root
        r.configure(bg=UI["page"])
        for name in ("TkDefaultFont", "TkTextFont", "TkMenuFont", "TkHeadingFont"):
            try:
                tkfont.nametofont(name).configure(family=FONT, size=10)
            except tk.TclError:
                pass
        r.option_add("*TCombobox*Listbox.font", (FONT, 10))
        r.option_add("*TCombobox*Listbox.background", UI["field"])
        r.option_add("*TCombobox*Listbox.selectBackground", UI["accent"])
        r.option_add("*TCombobox*Listbox.selectForeground", "#ffffff")
        st = ttk.Style(r)
        st.theme_use("clam")                       # fully stylable on every platform
        S, B = UI["surface"], UI["border"]
        st.configure(".", background=S, foreground=UI["ink"], font=(FONT, 10), bordercolor=B,
                     lightcolor=S, darkcolor=S, troughcolor=UI["page"], focuscolor=UI["accent"],
                     selectbackground=UI["accent"], selectforeground="#ffffff")
        st.configure("Page.TFrame", background=UI["page"])
        st.configure("TLabel", background=S, foreground=UI["ink"])
        st.configure("Big.TLabel", font=(FONT, 30, "bold"), foreground=UI["ink"])
        st.configure("Unit.TLabel", font=(FONT, 13), foreground=UI["ink2"])
        st.configure("Sub.TLabel", font=(FONT, 10), foreground=UI["ink2"])
        st.configure("Hint.TLabel", foreground=UI["muted"], font=(FONT, 9))
        st.configure("Head.TLabel", font=(FONT, 9, "bold"), foreground=UI["ink2"])
        st.configure("CardTitle.TLabel", font=(FONT, 10, "bold"), foreground=UI["ink"])
        st.configure("Status.TLabel", background=UI["page"], foreground=UI["muted"], font=(FONT, 9))
        # buttons: quiet default, one blue accent for the main action
        st.configure("TButton", background=UI["button"], foreground=UI["ink"], bordercolor=B,
                     lightcolor=UI["button"], darkcolor=UI["button"], padding=(10, 4), relief="flat")
        st.map("TButton", background=[("pressed", UI["axis"]), ("active", UI["button_hover"])],
               lightcolor=[("active", UI["button_hover"])], darkcolor=[("active", UI["button_hover"])])
        st.configure("Accent.TButton", background=UI["accent"], foreground="#ffffff", bordercolor=UI["accent"],
                     lightcolor=UI["accent"], darkcolor=UI["accent"], font=(FONT, 10, "bold"), padding=(12, 6))
        st.map("Accent.TButton", background=[("pressed", "#1c5cab"), ("active", UI["accent_hover"])],
               lightcolor=[("active", UI["accent_hover"])], darkcolor=[("active", UI["accent_hover"])])
        st.configure("Link.TButton", background=S, foreground=UI["accent"], bordercolor=S, lightcolor=S,
                     darkcolor=S, padding=(2, 0), font=(FONT, 9))
        st.map("Link.TButton", background=[("active", UI["accent_soft"])],
               lightcolor=[("active", UI["accent_soft"])], darkcolor=[("active", UI["accent_soft"])])
        # inputs
        for w in ("TEntry", "TCombobox", "TSpinbox"):
            st.configure(w, fieldbackground=UI["field"], bordercolor=UI["axis"], lightcolor=UI["field"],
                         darkcolor=UI["field"], arrowcolor=UI["ink2"], padding=(6, 3), insertcolor=UI["ink"])
            st.map(w, bordercolor=[("focus", UI["accent"])], lightcolor=[("focus", UI["accent"])],
                   fieldbackground=[("disabled", UI["page"]), ("readonly", UI["field"])],
                   foreground=[("disabled", UI["muted"])])
        st.configure("TCombobox", background=UI["field"])
        st.map("TCombobox", background=[("active", UI["field"]), ("readonly", UI["field"])])
        for w in ("TCheckbutton", "TRadiobutton"):
            st.configure(w, background=S, indicatorbackground=UI["field"], indicatorforeground=UI["accent"],
                         upperbordercolor=UI["axis"], lowerbordercolor=UI["axis"], padding=(0, 2))
            st.map(w, background=[("active", S)], indicatorbackground=[("selected", UI["accent_soft"])])
        # tabs
        st.configure("TNotebook", background=UI["page"], bordercolor=B, tabmargins=(0, 0, 0, 0))
        st.configure("TNotebook.Tab", background=UI["page"], foreground=UI["ink2"], padding=(16, 7),
                     bordercolor=UI["page"], lightcolor=UI["page"], font=(FONT, 10))
        st.map("TNotebook.Tab", background=[("selected", S), ("active", UI["button_hover"])],
               foreground=[("selected", UI["ink"])], font=[("selected", (FONT, 10, "bold"))],
               bordercolor=[("selected", B)], lightcolor=[("selected", S)])
        # tables, scrollbars, panes
        st.configure("Treeview", background=S, fieldbackground=S, foreground=UI["ink"], rowheight=24,
                     bordercolor=B, lightcolor=S, darkcolor=S)
        st.map("Treeview", background=[("selected", UI["accent_soft"])], foreground=[("selected", UI["ink"])])
        st.configure("Treeview.Heading", background=UI["button"], foreground=UI["ink2"], font=(FONT, 9, "bold"),
                     bordercolor=B, lightcolor=UI["button"], darkcolor=UI["button"], relief="flat")
        st.configure("TScrollbar", background=UI["button"], troughcolor=UI["page"], bordercolor=UI["page"],
                     lightcolor=UI["button"], darkcolor=UI["button"], arrowcolor=UI["muted"], gripcount=0)
        st.configure("TPanedwindow", background=UI["page"])
        st.configure("Sash", sashthickness=6, background=UI["page"], lightcolor=UI["page"], bordercolor=UI["page"])

    def _build(self):
        r = self.root
        self._build_header(r)
        self.statusbar = ttk.Label(r, text="", style="Status.TLabel", anchor="w")
        self.statusbar.pack(side="bottom", fill="x", padx=14, pady=(0, 6))
        main = ttk.PanedWindow(r, orient="horizontal")
        main.pack(fill="both", expand=True, padx=(4, 12), pady=(10, 6))
        side = ScrollFrame(main, width=340)
        main.add(side, weight=0)
        self._build_sidebar(side.body)
        self.nb = ttk.Notebook(main)
        main.add(self.nb, weight=1)
        self._build_live_tab()
        self._build_kin_tab()
        self._build_cal_tab()
        self._build_spec_tab()
        self._build_scan_tab()
        self._build_runs_tab()
        self._build_raw_tab()
        self.nb.bind("<<NotebookTabChanged>>", lambda e: self._dirty.update({"live", "kin", "cal", "spec", "scan"}))

    def _build_header(self, r):
        bar = tk.Frame(r, bg=UI["header"], height=52)
        bar.pack(fill="x")
        bar.pack_propagate(False)
        tk.Label(bar, text="GENESYS 20 Live", bg=UI["header"], fg=UI["header_ink"],
                 font=(FONT, 14, "bold")).pack(side="left", padx=(16, 8))
        self.lbl_version = tk.Label(bar, text=f"v{VERSION}  ·  Thermo Scientific GENESYS 20", bg=UI["header"],
                                    fg=UI["header_muted"], font=(FONT, 9), cursor="hand2")
        self.lbl_version.pack(side="left", pady=(4, 0))
        self.lbl_version.bind("<Button-1>", lambda _e: self.check_updates(manual=True))
        self.lbl_status = tk.Label(bar, text="", font=(FONT, 9, "bold"), padx=12, pady=4)
        self.lbl_status.pack(side="right", padx=16)
        # update bar (shown only when a newer release exists)
        self.update_bar = tk.Frame(r, bg=UI["accent_soft"])
        self.lbl_update = tk.Label(self.update_bar, text="", bg=UI["accent_soft"], fg=UI["ink"], font=(FONT, 10),
                                   anchor="w")
        self.lbl_update.pack(side="left", fill="x", expand=True, padx=16, pady=6)
        self.btn_update_later = ttk.Button(self.update_bar, text="Later", command=self.update_bar.pack_forget)
        self.btn_update_later.pack(side="right", padx=(4, 16), pady=4)
        self.btn_update = ttk.Button(self.update_bar, text="Update now", style="Accent.TButton",
                                     command=self.install_update)
        self.btn_update.pack(side="right", pady=4)
        self._header = bar

    def _card(self, parent, title, right=None):
        """A white card with a hairline border on the page background. Returns the inner frame."""
        outer = tk.Frame(parent, bg=UI["border"], padx=1, pady=1)
        outer.pack(fill="x", padx=(10, 6), pady=(0, 10))
        inner = ttk.Frame(outer, padding=(14, 10, 14, 12))
        inner.pack(fill="both", expand=True)
        head = ttk.Frame(inner)
        head.pack(fill="x", pady=(0, 8))
        ttk.Label(head, text=title, style="CardTitle.TLabel").pack(side="left")
        if right is not None:
            right(head)
        return inner

    _section = _card

    def _build_connection_card(self, p):
        ser = g.SerialSettings.from_dict(self.s["serial"])
        self.v_port = tk.StringVar(value=ser.port)          # the device, e.g. "COM12"
        self.v_port_label = tk.StringVar()                  # what the dropdown shows
        self.v_baud = tk.StringVar(value=str(ser.baud))
        self.v_bits = tk.StringVar(value=str(ser.bits))
        self.v_parity = tk.StringVar(value=PARITY_NAMES[ser.parity])
        self.v_stop = tk.StringVar(value=str(ser.stop))
        self.v_flow = tk.StringVar(value=FLOW_NAMES[ser.flow])
        self.v_term = tk.StringVar(value=ser.terminator)
        self.v_decimal = tk.StringVar(value=self.s["decimal"])
        self._port_map: dict[str, str] = {}

        f = self._card(p, "Connection")
        self.cb_port = ttk.Combobox(f, textvariable=self.v_port_label, state="readonly",
                                    postcommand=self.refresh_ports)
        self.cb_port.pack(fill="x")
        self.cb_port.bind("<<ComboboxSelected>>", self._port_chosen)
        self.lbl_port_hint = ttk.Label(f, text="", style="Hint.TLabel", wraplength=290)
        self.lbl_port_hint.pack(anchor="w", pady=(3, 0))
        self.btn_connect = ttk.Button(f, text="Connect", style="Accent.TButton", command=self.toggle_connect)
        self.btn_connect.pack(fill="x", pady=(8, 0))

        # serial settings: one summary line, the seven fields only when you open it
        sep = tk.Frame(f, bg=UI["border"], height=1)
        sep.pack(fill="x", pady=(12, 8))
        row = ttk.Frame(f)
        row.pack(fill="x")
        ttk.Label(row, text="Serial settings", style="Head.TLabel").pack(side="left")
        self.btn_serial = ttk.Button(row, text="Edit ▾", style="Link.TButton", command=self._toggle_serial)
        self.btn_serial.pack(side="right")
        self.lbl_serial = ttk.Label(f, text="", style="Sub.TLabel")
        self.lbl_serial.pack(anchor="w", pady=(2, 0))
        self.serial_box = ttk.Frame(f)
        ttk.Label(self.serial_box, text="Must match Utility > RS232 on the GENESYS.",
                  style="Hint.TLabel").grid(row=0, column=0, columnspan=4, sticky="w", pady=(6, 4))
        fields = [("Baud", self.v_baud, [str(b) for b in g.BAUDS]), ("Data bits", self.v_bits, ["8", "7"]),
                  ("Parity", self.v_parity, list(PARITY_NAMES.values())), ("Stop bits", self.v_stop, ["1", "2"]),
                  ("Handshake", self.v_flow, list(FLOW_NAMES.values())), ("Line end", self.v_term, list(g.TERMINATORS)),
                  ("Decimal", self.v_decimal, [".", ","])]
        self._serial_widgets = []
        for i, (lab, var, values) in enumerate(fields):
            r_, c = divmod(i, 2)
            ttk.Label(self.serial_box, text=lab, style="Hint.TLabel").grid(row=1 + 2 * r_, column=c, sticky="w",
                                                                          padx=(0, 8))
            cb = ttk.Combobox(self.serial_box, textvariable=var, values=values, state="readonly", width=11)
            cb.grid(row=2 + 2 * r_, column=c, sticky="we", padx=(0, 8), pady=(0, 6))
            self._serial_widgets.append(cb)
            var.trace_add("write", lambda *a: self._serial_summary())
        self.serial_box.columnconfigure((0, 1), weight=1)
        self._serial_summary()
        self.refresh_ports()

    def _toggle_serial(self):
        if self.serial_box.winfo_ismapped():
            self.serial_box.pack_forget()
            self.btn_serial.configure(text="Edit ▾")
        else:
            self.serial_box.pack(fill="x")
            self.btn_serial.configure(text="Hide ▴")

    def _serial_summary(self):
        par = {v: k for k, v in PARITY_NAMES.items()}.get(self.v_parity.get(), "?")
        flow = self.v_flow.get()
        flow = "no handshake" if flow == "Off" else flow
        term = {"CRLF": "CR+LF"}.get(self.v_term.get(), self.v_term.get())
        self.lbl_serial.configure(text=f"{self.v_baud.get()} baud  {self.v_bits.get()}{par}{self.v_stop.get()}  ·  "
                                       f"{flow}  ·  {term}")

    def _build_sidebar(self, p):
        self._build_connection_card(p)
        if self.demo:
            self.cb_port.configure(state="disabled")
            self.v_port_label.set("Simulated GENESYS 20")
        # --- reading now
        f = self._card(p, "Reading now")
        row = ttk.Frame(f)
        row.pack(anchor="w")
        self.lbl_big = ttk.Label(row, text="—", style="Big.TLabel")
        self.lbl_big.pack(side="left")
        self.lbl_unit = ttk.Label(row, text="", style="Unit.TLabel")
        self.lbl_unit.pack(side="left", padx=(6, 0), pady=(14, 0))
        self.lbl_sub = ttk.Label(f, text="", style="Sub.TLabel")
        self.lbl_sub.pack(anchor="w")
        self.lbl_src = ttk.Label(f, text="No readings yet", style="Hint.TLabel", wraplength=290)
        self.lbl_src.pack(anchor="w", pady=(4, 0))

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
        ttk.Label(f, text="Sends the read-only query SND (absorbance only); nothing on the GENESYS changes.",
                  style="Hint.TLabel", wraplength=290).pack(anchor="w", pady=(6, 0))

        # --- run
        f = self._section(p, "Run")
        row = ttk.Frame(f)
        row.pack(fill="x")
        ttk.Button(row, text="New run (t = 0 now)", command=self.new_run).pack(side="left")
        ttk.Button(row, text="Mark event...", command=self.mark_event).pack(side="left", padx=4)
        row = ttk.Frame(f)
        row.pack(fill="x", pady=(6, 0))
        ttk.Button(row, text="Save run...", style="Accent.TButton", command=self.save_run).pack(side="left")
        ttk.Button(row, text="Save CSV...", command=self.save_csv).pack(side="left", padx=4)
        self.lbl_run = ttk.Label(f, text="", style="Hint.TLabel")
        self.lbl_run.pack(anchor="w", pady=(4, 0))

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
        # presets: your saved ones first, then published reference values
        self.v_preset = tk.StringVar(value=self.s.get("preset", ""))
        row = ttk.Frame(f)
        row.pack(fill="x")
        self.cb_preset = ttk.Combobox(row, textvariable=self.v_preset, state="readonly",
                                      postcommand=self._fill_presets)
        self.cb_preset.pack(side="left", fill="x", expand=True)
        self.cb_preset.bind("<<ComboboxSelected>>", lambda e: self.apply_preset())
        ttk.Button(row, text="Save...", width=7, command=self.save_preset).pack(side="left", padx=(4, 0))
        self.lbl_preset = ttk.Label(f, text="Pick a preset to fill in λ and ε, or type your own below.",
                                    style="Hint.TLabel", wraplength=290)
        self.lbl_preset.pack(anchor="w", pady=(3, 8))
        self._fill_presets()
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
                  style="Hint.TLabel", wraplength=290).pack(anchor="w", pady=(4, 0))
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
                      style="Hint.TLabel", wraplength=290).pack(anchor="w", pady=(4, 0))

    def _figure(self, parent, nrows=1, ncols=1, **kw):
        fig = Figure(figsize=(6, 3.4), dpi=100, layout="constrained")
        axes = fig.subplots(nrows, ncols, **kw)
        canvas = FigureCanvasTkAgg(fig, master=parent)
        canvas.get_tk_widget().configure(bg=UI["surface"], highlightthickness=0)
        tb = NavigationToolbar2Tk(canvas, parent, pack_toolbar=False)
        tb.update()
        for w in [tb, *tb.winfo_children()]:          # blend the zoom/pan/save bar into the card
            try:
                w.configure(bg=UI["surface"])
            except tk.TclError:
                pass
        tb.pack(side="bottom", fill="x")
        canvas.get_tk_widget().pack(side="top", fill="both", expand=True)
        return fig, axes, canvas

    def _watermark(self, fig):
        if self.demo:
            fig.text(0.5, 0.5, "SIMULATED", fontsize=46, color=STATUS_STYLE["sim"][1], alpha=0.08,
                     ha="center", va="center", rotation=20, fontweight="bold")

    def _build_live_tab(self):
        tab = ttk.Frame(self.nb, padding=(14, 12, 14, 8))
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
        tab = ttk.Frame(self.nb, padding=(14, 12, 14, 8))
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
        self.tv_kin.tag_configure("best", background=UI["accent_soft"], font=(FONT, 10, "bold"))
        self.tv_kin.pack(fill="x", pady=4)
        self.lbl_kin_rate = ttk.Label(tab, text="", style="Sub.TLabel")
        self.lbl_kin_rate.pack(fill="x")
        holder = ttk.Frame(tab)
        holder.pack(fill="both", expand=True)
        self.kin_fig, self.kin_axes, self.kin_canvas = self._figure(holder, 1, 3)
        self._watermark(self.kin_fig)

    def _build_cal_tab(self):
        tab = ttk.Frame(self.nb, padding=(14, 12, 14, 8))
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
        ttk.Button(left, text="Add the current reading as this standard", style="Accent.TButton",
                   command=self.add_standard).pack(fill="x", pady=(8, 8))
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
        tk.Frame(left, bg=UI["border"], height=1).pack(fill="x", pady=(12, 8))
        ttk.Label(left, text="Saved curves", style="Head.TLabel").pack(anchor="w")
        self.v_curve = tk.StringVar()
        self.cb_curve = ttk.Combobox(left, textvariable=self.v_curve, state="readonly",
                                     postcommand=lambda: self.cb_curve.configure(values=self.curves.names()))
        self.cb_curve.pack(fill="x", pady=(4, 4))
        row = ttk.Frame(left)
        row.pack(fill="x")
        ttk.Button(row, text="Load", command=self.load_curve).pack(side="left")
        ttk.Button(row, text="Save current...", command=self.save_curve).pack(side="left", padx=4)
        ttk.Button(row, text="Delete", command=self.delete_curve).pack(side="left")
        ttk.Label(left, text="Saved in the data folder, so a shared (OneDrive) folder shares them too.",
                  style="Hint.TLabel", wraplength=320).pack(fill="x", pady=(4, 0))
        holder = ttk.Frame(tab)
        holder.pack(side="left", fill="both", expand=True)
        self.cal_fig, self.cal_ax, self.cal_canvas = self._figure(holder)
        self._watermark(self.cal_fig)

    def _build_spec_tab(self):
        tab = ttk.Frame(self.nb, padding=(14, 12, 14, 8))
        self.nb.add(tab, text="Spectrum")
        left = ttk.Frame(tab, width=330)
        left.pack(side="left", fill="y", padx=(0, 6))
        ttk.Button(left, text="Add the current reading at λ", style="Accent.TButton",
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

    def _build_scan_tab(self):
        tab = ttk.Frame(self.nb, padding=(14, 12, 14, 8))
        self.nb.add(tab, text="Scan")
        left = ttk.Frame(tab, width=340)
        left.pack(side="left", fill="y", padx=(0, 10))
        sc = {**DEFAULTS["scan"], **(self.s.get("scan") or {})}
        self.v_sc = {k: tk.StringVar(value=str(sc[k])) for k in ("start", "stop", "step", "repeats",
                                                                  "interval_min", "settle_s")}
        self.v_sc_blank = tk.BooleanVar(value=bool(sc["blank_first"]))
        grid = ttk.Frame(left)
        grid.pack(fill="x")
        for i, (key, lab) in enumerate((("start", "From (nm)"), ("stop", "To (nm)"), ("step", "Step (nm)"))):
            ttk.Label(grid, text=lab, style="Hint.TLabel").grid(row=0, column=i, sticky="w", padx=(0, 6))
            ttk.Entry(grid, textvariable=self.v_sc[key], width=8).grid(row=1, column=i, sticky="we", padx=(0, 6))
        for i, (key, lab) in enumerate((("repeats", "Scans"), ("interval_min", "Every (min)"),
                                        ("settle_s", "Settle (s)"))):
            ttk.Label(grid, text=lab, style="Hint.TLabel").grid(row=2, column=i, sticky="w", padx=(0, 6), pady=(6, 0))
            ttk.Entry(grid, textvariable=self.v_sc[key], width=8).grid(row=3, column=i, sticky="we", padx=(0, 6))
        ttk.Checkbutton(left, text="Blank pass first (single-beam correction)",
                        variable=self.v_sc_blank).pack(anchor="w", pady=(8, 2))
        ttk.Label(left, text="How the wavelength is set", style="Head.TLabel").pack(anchor="w", pady=(6, 2))
        modes = list(SCAN_MODES)
        self.v_sc_mode = tk.StringVar(value=SCAN_MODES[sc["mode"] if sc["mode"] in modes else "command"])
        self.cb_sc_mode = ttk.Combobox(left, textvariable=self.v_sc_mode, state="readonly",
                                       values=[SCAN_MODES[m] for m in modes])
        self.cb_sc_mode.pack(fill="x")
        ttk.Button(left, text="Set-wavelength command...", style="Link.TButton",
                   command=self.configure_wl_command).pack(anchor="w", pady=(2, 0))
        row = ttk.Frame(left)
        row.pack(fill="x", pady=(10, 0))
        self.btn_scan = ttk.Button(row, text="Start scan", style="Accent.TButton", command=self.start_scan)
        self.btn_scan.pack(side="left")
        ttk.Button(row, text="Stop", command=self.stop_scan).pack(side="left", padx=4)
        ttk.Button(row, text="Export CSV...", command=self.export_scan).pack(side="left")
        # the prompt: what to do next (big, because you're looking at the instrument)
        box = tk.Frame(left, bg=UI["border"], padx=1, pady=1)
        box.pack(fill="x", pady=(10, 0))
        inner = tk.Frame(box, bg=UI["accent_soft"], padx=12, pady=10)
        inner.pack(fill="x")
        self.lbl_scan_prompt = tk.Label(inner, text="Set the range, then Start scan.", bg=UI["accent_soft"],
                                        fg=UI["ink"], font=(FONT, 13, "bold"), wraplength=300, justify="left")
        self.lbl_scan_prompt.pack(anchor="w")
        self.lbl_scan_progress = tk.Label(inner, text="", bg=UI["accent_soft"], fg=UI["ink2"], font=(FONT, 9),
                                          wraplength=300, justify="left")
        self.lbl_scan_progress.pack(anchor="w", pady=(4, 0))
        row = ttk.Frame(left)
        row.pack(fill="x", pady=(8, 0))
        self.btn_scan_go = ttk.Button(row, text="Continue", command=self.scan_continue)
        self.btn_scan_go.pack(side="left")
        ttk.Button(row, text="Read now (SND)", command=self.scan_read_now).pack(side="left", padx=4)
        ttk.Button(row, text="Skip point", command=self.scan_skip).pack(side="left")
        self.tv_scan = ttk.Treeview(left, columns=("scan", "t", "lmax", "amax"), show="headings", height=5)
        for c, t, w in (("scan", "Scan", 70), ("t", "Start (min)", 80), ("lmax", "λmax (nm)", 80),
                        ("amax", "A at λmax", 80)):
            self.tv_scan.heading(c, text=t)
            self.tv_scan.column(c, width=w, anchor="center")
        self.tv_scan.pack(fill="x", pady=(10, 0))
        ttk.Label(left, text="Automatic: the GENESYS moves itself with GTO (the command Thermo's VISIONlite "
                             "uses for the GENESYS 20), waits Settle seconds, then reads; a reading taken "
                             "too early is refused by the instrument and repeated. Guided: you set each "
                             "wavelength and press Print.",
                  style="Hint.TLabel", wraplength=320).pack(fill="x", pady=(8, 0))
        holder = ttk.Frame(tab)
        holder.pack(side="left", fill="both", expand=True)
        self.scan_fig, self.scan_ax, self.scan_canvas = self._figure(holder)
        self._watermark(self.scan_fig)

    def _build_runs_tab(self):
        tab = ttk.Frame(self.nb, padding=(14, 12, 14, 8))
        self.nb.add(tab, text="Runs")
        row = ttk.Frame(tab)
        row.pack(fill="x")
        ttk.Label(row, text="Data folder", style="Head.TLabel").pack(side="left")
        self.lbl_data_dir = ttk.Label(row, text=self.data_dir, style="Sub.TLabel")
        self.lbl_data_dir.pack(side="left", padx=8)
        ttk.Button(row, text="Change...", command=self.choose_data_dir).pack(side="right")
        ttk.Button(row, text="Open", command=lambda: self._open_path(self.data_dir)).pack(side="right", padx=4)
        ttk.Label(tab, text="Runs, saved presets and calibration curves live here. Pick a OneDrive or shared "
                            "folder and everyone using it sees the same library.",
                  style="Hint.TLabel").pack(anchor="w", pady=(2, 8))
        row = ttk.Frame(tab)
        row.pack(fill="x", pady=(0, 8))
        ttk.Button(row, text="Save current run...", style="Accent.TButton", command=self.save_run).pack(side="left")
        for text, cmd in (("Open report", self.run_open_report), ("Open folder", self.run_open_folder),
                          ("Share (zip)...", self.run_share), ("Copy summary", self.run_copy_summary),
                          ("Load into app", self.run_load), ("Delete", self.run_delete),
                          ("Refresh", self.refresh_runs)):
            ttk.Button(row, text=text, command=cmd).pack(side="left", padx=(6, 0))
        frame = tk.Frame(tab, bg=UI["border"], padx=1, pady=1)
        frame.pack(fill="both", expand=True)
        cols = ("saved", "name", "readings", "wl", "notes")
        self.tv_runs = ttk.Treeview(frame, columns=cols, show="headings")
        for c, t, w in (("saved", "Saved", 140), ("name", "Name", 240), ("readings", "Readings", 80),
                        ("wl", "λ (nm)", 70), ("notes", "Notes", 380)):
            self.tv_runs.heading(c, text=t)
            self.tv_runs.column(c, width=w, anchor="w" if c in ("name", "notes") else "center")
        self.tv_runs.pack(side="left", fill="both", expand=True)
        sb = ttk.Scrollbar(frame, command=self.tv_runs.yview)
        self.tv_runs.configure(yscrollcommand=sb.set)
        sb.pack(side="right", fill="y")
        self.tv_runs.bind("<Double-1>", lambda e: self.run_open_report())
        self._run_rows: dict[str, dict] = {}
        self.refresh_runs()

    def _build_raw_tab(self):
        tab = ttk.Frame(self.nb, padding=(14, 12, 14, 8))
        self.nb.add(tab, text="Raw data")
        top = ttk.Frame(tab)
        top.pack(fill="x")
        self.v_hex = tk.BooleanVar(value=False)
        ttk.Checkbutton(top, text="Show bytes as hex", variable=self.v_hex).pack(side="left")
        ttk.Button(top, text="Clear view", command=lambda: self.txt.delete("1.0", "end")).pack(side="left", padx=6)
        ttk.Label(top, text="Bare numbers are read as").pack(side="left", padx=(16, 2))
        self.v_kind = tk.StringVar(value=self.s["default_kind"])
        ttk.Combobox(top, textvariable=self.v_kind, values=("A", "%T"), state="readonly", width=4).pack(side="left")
        ver_outer = tk.Frame(tab, bg=UI["border"], padx=1, pady=1)
        ver_outer.pack(fill="x", pady=(10, 10))
        ver = ttk.Frame(ver_outer, padding=(12, 10))
        ver.pack(fill="x")
        ttk.Label(ver, text="Check the format once", style="CardTitle.TLabel").pack(anchor="w")
        self.lbl_verify = ttk.Label(ver, text="No reading yet.", wraplength=900, style="Sub.TLabel")
        self.lbl_verify.pack(anchor="w", pady=(4, 0))
        row = ttk.Frame(ver)
        row.pack(anchor="w", pady=(8, 0))
        ttk.Button(row, text="Yes, it matches the GENESYS display", style="Accent.TButton",
                   command=lambda: self.verify(True)).pack(side="left")
        ttk.Button(row, text="No, it doesn't", command=lambda: self.verify(False)).pack(side="left", padx=8)
        frame = tk.Frame(tab, bg=UI["border"], padx=1, pady=1)
        frame.pack(fill="both", expand=True)
        self.txt = tk.Text(frame, font=("Consolas", 10), wrap="none", height=20, bg=UI["surface"], fg=UI["ink"],
                           relief="flat", borderwidth=0, padx=10, pady=8, insertbackground=UI["ink"],
                           selectbackground=UI["accent_soft"], selectforeground=UI["ink"])
        sb = ttk.Scrollbar(frame, command=self.txt.yview)
        self.txt.configure(yscrollcommand=sb.set)
        self.txt.pack(side="left", fill="both", expand=True)
        sb.pack(side="right", fill="y")
        for tag, color in (("in", UI["ink"]), ("out", "#6250d6"), ("info", "#256abf"), ("warn", "#d03b3b"),
                           ("reading", "#006300"), ("dim", UI["muted"])):
            self.txt.tag_configure(tag, foreground=color)

    # ================================================================== connection
    def refresh_ports(self, ports: list[tuple[str, str]] | None = None):
        """Fill the port list (called when the list opens, and by the background watcher every 2 s
        while disconnected, so plugging the adapter in or out shows up by itself)."""
        if self.demo:
            return
        if ports is None:
            ports = list_ports()
        self._port_map = {label: dev for dev, label in ports}
        self.cb_port.configure(values=[label for _, label in ports])
        if self.link.is_open:
            return
        devices = [dev for dev, _ in ports]
        cur = self.v_port.get()
        if cur not in devices:
            usb = [dev for dev, label in ports if any(w in label.lower() for w in ADAPTER_WORDS)]
            cur = (usb or devices or [""])[0]
            self.v_port.set(cur)
        if cur:
            self.v_port_label.set(next(label for dev, label in ports if dev == cur))
            self.lbl_port_hint.configure(text="")
            self.btn_connect.state(["!disabled"])
        else:
            self.v_port_label.set("No serial adapter found")
            self.lbl_port_hint.configure(text="Plug in the USB-serial adapter; it appears here by itself.")
            self.btn_connect.state(["disabled"])

    def _port_chosen(self, _e=None):
        dev = self._port_map.get(self.v_port_label.get())
        if dev:
            self.v_port.set(dev)

    def _start_port_watch(self):
        self._ports_q: queue.SimpleQueue = queue.SimpleQueue()

        def watch():
            last = None
            while not self._closing:
                if not self.link.is_open:
                    ports = list_ports()
                    if ports != last:
                        self._ports_q.put(ports)
                        last = ports
                time.sleep(2.0)

        threading.Thread(target=watch, name="port-watch", daemon=True).start()

    def _lock_serial(self, locked: bool):
        for w in [self.cb_port, *self._serial_widgets]:
            w.configure(state="disabled" if locked else "readonly")
        if self.demo:
            self.cb_port.configure(state="disabled")

    def serial_settings(self) -> g.SerialSettings:
        inv_par = {v: k for k, v in PARITY_NAMES.items()}
        inv_flow = {v: k for k, v in FLOW_NAMES.items()}
        return g.SerialSettings(port=self.v_port.get() or "COM1", baud=int(self.v_baud.get()),
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
        self.btn_connect.configure(text="Disconnect", style="TButton")
        self._lock_serial(True)
        self._set_status("sim" if self.demo else "open")
        self._raw(f"Connected: {ser.describe()}" + ("  [SIMULATED]" if self.demo else ""), "info")
        self.on_poll_toggle()
        self.save_settings()

    def disconnect(self):
        self.link.poll_interval = 0
        self.link.close()
        if self.log:
            self.log.close()
        self.btn_connect.configure(text="Connect", style="Accent.TButton")
        self._lock_serial(False)
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
        ports = None
        while self._ports_q is not None:
            try:
                ports = self._ports_q.get_nowait()
            except queue.Empty:
                break
        if ports is not None:
            self.refresh_ports(ports)
        while True:                                   # results from background threads (updates)
            try:
                fn = self._ui_calls.get_nowait()
            except queue.Empty:
                break
            fn()
        self._scan_step()
        now = time.time()
        if self.t0 is not None:
            el = int(now - self.t0)
            self.lbl_run.configure(text=f"Run time {el // 60:02d}:{el % 60:02d}   ·   {len(self.readings)} readings")
        if self._dirty and now - self._last_draw > 0.35:
            self._redraw()
            self._last_draw = now
        self._pump_id = self.root.after(100, self._pump)

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
        elif kind == "done":
            self._scan_event("done", ev[2])
        elif kind == "refused":
            cmd = ev[2]
            if cmd == "SND" and len(ev) > 4 and ev[4] and self._out_of_range(ev[1], ev[4]):
                return
            if self._scan_event("refused", cmd):
                self._raw(f"{cmd}: no reading (ER), asking again.", "dim", ev[1])
                return
            self._raw(f"The GENESYS answered {cmd} with {ev[3] if len(ev) > 3 else 'ER'}.", "warn", ev[1])
            if self.scan and self.scan.state == "running" and self._scan_mode() == "command" \
                    and cmd not in g.READ_ONLY_COMMANDS:
                self.stop_scan()
                messagebox.showwarning(APP_NAME, f"The GENESYS refused the set-wavelength command '{cmd}'. "
                                                 "Check it against Thermo's RS232 command set, or use Guided.")
            elif cmd == "SND":
                self.snd_misses += 1
        elif kind == "error":
            self._raw(f"Port error: {ev[2]}", "warn", ev[1])
            messagebox.showwarning(APP_NAME, f"The serial port stopped working:\n\n{ev[2]}")
        elif kind == "closed":
            if self.btn_connect.cget("text") == "Disconnect":
                self.btn_connect.configure(text="Connect", style="Accent.TButton")
                self._lock_serial(False)
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
        if self._scan_take(r, "SND" if cmd == "SND" else "Print"):    # a running scan gets first claim
            return
        if self._viewing:                             # new live data: stop calling it the saved run
            self._viewing = None
            self.readings.clear()
            self.marks.clear()
            self.t0 = None
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
            self.lbl_big.configure(text=f"{row['value']:g}")
            self.lbl_unit.configure(text=row["unit"])
            self.lbl_sub.configure(text="GENESYS in concentration mode")
        else:
            a = row["A"]
            places = 4 if row["source"] == "SND" else 3      # SND answers with 4 decimals
            self.lbl_big.configure(text=f"{a:.{places}f}" if a is not None else "—")
            self.lbl_unit.configure(text="A" if a is not None else "")
            parts = [f"{row['T']:.1f} %T"] if row["T"] is not None else []
            conc, cu, _ = self._conc_of(row)
            if conc is not None:
                parts.append(f"c = {fmt_sig(conc)} {cu}")
            self.lbl_sub.configure(text="   ·   ".join(parts))
        when = datetime.fromtimestamp(row["wall"]).strftime("%H:%M:%S")
        src = "Hands-free (SND)" if row["source"] == "SND" else "Print"
        wl = f" · {row['wl']:g} nm" if row["wl"] else ""
        sim = " · SIMULATED" if self.demo else ""
        self.lbl_src.configure(text=f"{src}{wl} · {when} · t = {row['t']:.1f} s{sim}")

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
        self.lbl_big.configure(text="—")
        self.lbl_unit.configure(text="")
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

    # ================================================================== presets
    def _fill_presets(self):
        mine = [f"★ {n}" for n in self.user_presets.names()]
        ref = [p["name"] for p in presets.REFERENCE]
        self.cb_preset.configure(values=mine + ref)

    def apply_preset(self):
        name = self.v_preset.get()
        if name.startswith("★ "):
            p = self.user_presets.get(name[2:])
            if not p:
                return
            for k, var in (("wl", self.v_wl), ("path", self.v_path), ("eps", self.v_eps), ("mw", self.v_mw),
                           ("unit", self.v_unit), ("species", self.v_species), ("c0", self.v_c0),
                           ("a_inf", self.v_ainf), ("conc_method", self.v_method)):
                if p.get(k) not in (None, ""):
                    var.set(p[k])
            self.lbl_preset.configure(text=f"Your preset (saved {p.get('saved', '')[:10]}). {p.get('note', '')}")
        else:
            p = next((x for x in presets.REFERENCE if x["name"] == name), None)
            if not p:
                return
            self.v_wl.set(str(p["wl"]))
            eps = presets.eps_of(p)
            self.v_eps.set("" if eps is None else f"{eps:.0f}")
            self.v_mw.set(str(p.get("mw", "")))
            self.v_unit.set(p.get("unit", "µM"))
            self.v_method.set("cal" if p.get("method") else "beer")
            self.lbl_preset.configure(text=presets.describe(p) + f"  Source: {p['source']}")
        self.s["preset"] = name
        if self.sim is None and self.link.is_open:
            self._raw(f"Preset {name}: set the GENESYS to {self.v_wl.get()} nm and blank it.", "info")

    def save_preset(self):
        name = simpledialog.askstring(APP_NAME, "Name for this preset (saved in the data folder):",
                                      parent=self.root)
        if not name:
            return
        note = simpledialog.askstring(APP_NAME, "Optional note (conditions, source):", parent=self.root) or ""
        self.user_presets.put({"name": name.strip(), "wl": self.v_wl.get(), "path": self.v_path.get(),
                               "eps": self.v_eps.get(), "mw": self.v_mw.get(), "unit": self.v_unit.get(),
                               "species": self.v_species.get(), "c0": self.v_c0.get(),
                               "a_inf": self.v_ainf.get(), "conc_method": self.v_method.get(), "note": note})
        self._fill_presets()
        self.v_preset.set(f"★ {name.strip()}")
        self.statusbar.configure(text=f"Saved preset '{name.strip()}' to {self.user_presets.path}")

    # ================================================================== saved calibration curves
    def save_curve(self):
        if len(self.s.get("standards", [])) < 2:
            messagebox.showinfo(APP_NAME, "Add at least two standards first.")
            return
        name = simpledialog.askstring(APP_NAME, "Name for this calibration curve:", parent=self.root,
                                      initialvalue=f"{self.v_preset.get().lstrip('★ ') or 'Curve'} "
                                                   f"{datetime.now():%Y-%m-%d}")
        if not name:
            return
        fit = self.cal.fit if self.cal else None
        self.curves.put({"name": name.strip(), "unit": self.v_cal_unit.get(), "wl": self.v_wl.get(),
                         "path": self.v_path.get(), "standards": self.s["standards"],
                         "fit": None if fit is None else {"slope": fit.slope, "intercept": fit.intercept,
                                                          "r2": fit.r2, "n": fit.n}})
        self.v_curve.set(name.strip())
        self.statusbar.configure(text=f"Saved calibration curve '{name.strip()}' to {self.curves.path}")

    def load_curve(self):
        c = self.curves.get(self.v_curve.get())
        if not c:
            messagebox.showinfo(APP_NAME, "Pick a saved curve first.")
            return
        if self.s.get("standards") and not messagebox.askyesno(APP_NAME, "Replace the current standards?"):
            return
        self.s["standards"] = [list(x) for x in c.get("standards", [])]
        self.tv_cal.delete(*self.tv_cal.get_children())
        self.v_cal_unit.set(c.get("unit", self.v_cal_unit.get()))
        for cc, a, wl in self.s["standards"]:
            self.tv_cal.insert("", "end", values=(fmt_sig(cc), f"{a:.4f}", "" if wl is None else f"{wl:g}"))
        self._recalibrate()
        if c.get("wl") and c["wl"] != self.v_wl.get():
            messagebox.showinfo(APP_NAME, f"This curve was made at {c['wl']} nm. Set the GENESYS (and the "
                                          "wavelength box) to the same wavelength before using it.")

    def delete_curve(self):
        name = self.v_curve.get()
        if name and messagebox.askyesno(APP_NAME, f"Delete the saved curve '{name}'?"):
            self.curves.delete(name)
            self.v_curve.set("")

    # ================================================================== wavelength scans
    def _scan_mode(self) -> str:
        label = self.v_sc_mode.get()
        return next((k for k, v in SCAN_MODES.items() if v == label), "guided")

    def _scan_config(self) -> ScanConfig:
        f = {k: float(str(v.get()).replace(",", ".")) for k, v in self.v_sc.items()}
        return ScanConfig(start=f["start"], stop=f["stop"], step=f["step"], blank_first=self.v_sc_blank.get(),
                          repeats=max(1, int(f["repeats"])), interval_s=max(0.0, f["interval_min"]) * 60,
                          settle_s=max(0.0, f["settle_s"]))

    def configure_wl_command(self):
        cur = self.s.get("wl_command", "")
        text = simpledialog.askstring(
            APP_NAME,
            "Go-to-wavelength command, with {wl} where the wavelength goes.\n\nThe GENESYS 20 uses "
            "GTO {wl} (from Thermo's VISIONlite GENESYS 20 driver, checked on this instrument). Only change "
            "it for a different instrument and only to a documented command: this port also accepts "
            "calibration-table uploads. Leave empty to switch automatic scans off.",
            initialvalue=cur or DEFAULTS["wl_command"], parent=self.root)
        if text is None:
            return
        text = text.strip()
        try:
            self.link.allow_wavelength_command(text or None)
        except ValueError as exc:
            messagebox.showerror(APP_NAME, str(exc))
            return
        self.s["wl_command"] = text
        self.save_settings()
        messagebox.showinfo(APP_NAME, "Automatic scans are " + ("on: pick 'Automatic' as the mode." if text
                                                                else "off."))

    def start_scan(self):
        if self.scan and self.scan.state in ("running", "waiting"):
            return
        try:
            cfg = self._scan_config()
            cfg.wavelengths()
        except ValueError as exc:
            messagebox.showerror(APP_NAME, f"Check the scan settings: {exc}")
            return
        mode = self._scan_mode()
        if not self.link.is_open:
            messagebox.showinfo(APP_NAME, "Connect first.")
            return
        if mode == "command":
            if not self.s.get("wl_command"):
                messagebox.showinfo(APP_NAME, "Automatic scans are switched off (no go-to-wavelength command). "
                                              "Set it under Set-wavelength command... (GTO {wl}), or use Guided.")
                return
            self.link.allow_wavelength_command(self.s["wl_command"])
        self._scan_retries = 0
        self.s["scan"] = {"start": cfg.start, "stop": cfg.stop, "step": cfg.step, "blank_first": cfg.blank_first,
                          "repeats": cfg.repeats, "interval_min": cfg.interval_s / 60, "settle_s": cfg.settle_s,
                          "mode": mode}
        self._scan_poll_restore = self.v_poll.get()
        if self._scan_poll_restore:               # hands-free polling would steal the scan's readings
            self.v_poll.set(False)
            self.link.poll_interval = 0
        self.scan = ScanEngine(cfg)
        self.scan.start()
        self._scan_passes_seen = len(self.scan.passes)
        self._scan_wait = None
        self._scan_set_wl = None
        self.btn_scan.state(["disabled"])
        first = "BLANK" if cfg.blank_first else "SAMPLE"
        self._raw(f"Scan started: {cfg.start:g}-{cfg.stop:g} nm every {cfg.step:g} nm, {cfg.repeats} scan(s), "
                  f"{SCAN_MODES[mode]}. {first} in first.", "info")
        self._dirty.add("scan")

    def stop_scan(self):
        if self.scan:
            self.scan.stop()
            self._scan_finished("Scan stopped.")

    def _scan_finished(self, text: str):
        self._scan_wait = None
        self.btn_scan.state(["!disabled"])
        home = self._num(self.v_wl)                   # put the GENESYS back at the working wavelength
        if self._scan_mode() == "command" and home and self.link.is_open and self.s.get("wl_command"):
            try:
                self.link.send(self.link.wavelength_command(home))
                text += f" Back at {home:g} nm."
            except ValueError:
                pass
        if self._scan_poll_restore:
            self._scan_poll_restore = False
            self.v_poll.set(True)
            self.on_poll_toggle()
        self.lbl_scan_prompt.configure(text=text)
        self._dirty.add("scan")

    def scan_continue(self):
        if self._scan_wait == "hold":
            self._scan_wait = None

    def scan_skip(self):
        if self.scan and self.scan.state == "running":
            self.scan.skip()
            self._scan_wait = None
            self._dirty.add("scan")

    def scan_read_now(self):
        if self.scan and self.scan.state == "running" and self._scan_wait in (None, "settle"):
            self.send("SND")
            self._scan_wait = "read"
            self._scan_due = time.time() + 6

    def _scan_step(self):
        """Called every 100 ms while a scan exists: advance timers and keep the prompt current."""
        sc = self.scan
        if sc is None or sc.state in ("idle", "done", "stopped"):
            return
        sc.tick()
        if len(sc.passes) != self._scan_passes_seen:           # a new pass began
            self._scan_passes_seen = len(sc.passes)
            prev = sc.passes[-2] if len(sc.passes) > 1 else None
            if prev is not None and prev.kind == "blank":
                self._scan_wait = "hold"                        # swap the cuvette before the sample pass
            self._scan_set_wl = None
            self._dirty.add("scan")
        mode = self._scan_mode()
        tgt = sc.target()
        self.lbl_scan_progress.configure(text=sc.progress())
        if tgt is None:
            self.lbl_scan_prompt.configure(text=f"Waiting: {sc.progress()}")
            return
        what = "BLANK" if sc.current.kind == "blank" else "SAMPLE"
        if self._scan_wait == "hold":
            self.lbl_scan_prompt.configure(text="Blank pass done. Put the SAMPLE in, then click Continue.")
            return
        if mode == "guided":
            if self._scan_wait != "read":
                self.lbl_scan_prompt.configure(text=f"{what} in · set the GENESYS to {tgt:g} nm, then press Print")
            elif time.time() > self._scan_due:
                self._scan_wait = None
            return
        # automatic: GTO <nm> -> wait for OK -> settle -> SND (re-read if the GENESYS answers ER)
        now = time.time()
        if self._scan_wait is None and self._scan_set_wl != tgt:
            try:
                self.link.send(self.link.wavelength_command(tgt))
            except Exception as exc:
                self.stop_scan()
                messagebox.showerror(APP_NAME, f"Could not set the wavelength: {exc}")
                return
            self._scan_set_wl = tgt
            self._scan_retries = 0
            self._scan_wait = "moving"                          # until the GENESYS says OK
            self._scan_due = now + 15
            self.lbl_scan_prompt.configure(text=f"{what} · moving to {tgt:g} nm")
        elif self._scan_wait == "moving" and now >= self._scan_due:
            self.stop_scan()
            messagebox.showwarning(APP_NAME, f"The GENESYS didn't confirm the move to {tgt:g} nm.")
        elif self._scan_wait == "settle" and now >= self._scan_due:
            self.send("SND")
            self._scan_wait = "read"
            self._scan_due = now + 6
            self.lbl_scan_prompt.configure(text=f"{what} · reading at {tgt:g} nm")
        elif self._scan_wait == "read" and now >= self._scan_due:
            self._raw(f"No reading at {tgt:g} nm; asking again.", "warn")
            self._scan_wait = "settle"
            self._scan_due = now

    def _scan_event(self, kind: str, cmd: str) -> bool:
        """GTO acknowledged ("done") or a command refused: drive the automatic scan. True if handled."""
        sc = self.scan
        if sc is None or sc.state != "running" or self._scan_mode() != "command":
            return False
        if kind == "done" and self._scan_wait == "moving":
            self._scan_wait = "settle"
            self._scan_due = time.time() + sc.cfg.settle_s
            return True
        if kind == "refused" and cmd == "SND" and self._scan_wait == "read":
            self._scan_retries += 1                             # a bare ER (no number): try again
            if self._scan_retries > 3:
                self._raw(f"No reading at {sc.target():g} nm after 3 tries; skipped.", "warn")
                self.scan_skip()
                return True
            self._scan_wait = "settle"
            self._scan_due = time.time() + max(0.7, sc.cfg.settle_s / 2)
            return True
        return False

    def _out_of_range(self, t: float, lines: list[bytes]) -> bool:
        """SND answered with a number and then ER: a reading outside -0.1..2.5 A. True if handled."""
        r = next((g.parse_line(g.decode(ln), default_kind="A") for ln in lines), None)
        if r is None or r.absorbance is None:
            return False
        if self.scan and self.scan.state == "running" and self._scan_wait == "read":
            return self._scan_take(r, "SND", flagged=True)
        self._raw(f"<- {r.value:g}  (outside the GENESYS 20's -0.1 to 2.5 A range; not recorded)", "warn", t)
        self.lbl_big.configure(text=f"{r.value:.4f}")
        self.lbl_unit.configure(text="A")
        self.lbl_sub.configure(text="Outside the -0.1 to 2.5 A range")
        self.lbl_src.configure(text=f"Hands-free (SND) · {datetime.fromtimestamp(t):%H:%M:%S} · not recorded")
        self.snd_misses = 0                                     # the GENESYS did answer
        return True

    def _scan_take(self, r: g.Reading, source: str, flagged: bool = False) -> bool:
        """Offer a reading to the running scan. True if the scan used it. `flagged` = the GENESYS
        answered the reading with ER (outside its -0.1 to 2.5 A range): kept but marked."""
        sc = self.scan
        if sc is None or sc.state != "running" or self._scan_wait == "hold":
            return False
        tgt = sc.target()
        mode = self._scan_mode()
        a = r.absorbance
        if a is None:
            return False
        if source == "SND":
            if self._scan_wait != "read":
                return False
        else:                                                   # Print, guided mode
            if mode != "guided":
                return False
            if r.wl is not None and abs(r.wl - tgt) > 1.0:
                self._raw(f"Print says {r.wl:g} nm but the scan wants {tgt:g} nm: set {tgt:g} nm and press "
                          "Print again.", "warn")
                self.lbl_scan_prompt.configure(text=f"Wrong wavelength ({r.wl:g} nm). Set {tgt:g} nm, press Print.")
                return True
        label = sc.current.label
        sc.record(a, flagged=flagged)
        self._scan_wait = None
        note = "  (outside the GENESYS 20's -0.1 to 2.5 A range: kept, marked)" if flagged else ""
        self._raw(f"{label} · {tgt:g} nm: A = {a:.4f}{note}", "warn" if flagged else "reading")
        self.lbl_big.configure(text=f"{a:.4f}")
        self.lbl_unit.configure(text="A")
        self.lbl_src.configure(text=f"Scan · {tgt:g} nm · {datetime.now():%H:%M:%S}")
        self._dirty.add("scan")
        if sc.state == "done":
            self._scan_finished(f"Scan finished: {len(sc.samples())} scan(s). Save the run to keep it.")
        return True

    def export_scan(self):
        if not self.scan or not self.scan.passes:
            messagebox.showinfo(APP_NAME, "No scan yet.")
            return
        path = filedialog.asksaveasfilename(parent=self.root, title="Export scan", defaultextension=".csv",
                                            initialfile=f"scan-{datetime.now():%Y%m%d-%H%M}.csv",
                                            filetypes=[("CSV", "*.csv")])
        if path:
            head, rows = self.scan.table()
            with open(path, "w", newline="", encoding="utf-8-sig") as fh:
                w = csv.writer(fh)
                w.writerow(head)
                w.writerows(rows)
            self.statusbar.configure(text=f"Saved the scan to {path}")

    # ================================================================== saved runs (store & share)
    def _later(self, fn):
        self._ui_calls.put(fn)

    def _open_path(self, path: str):
        if not os.path.splitext(path)[1]:                       # a folder: make sure it exists
            os.makedirs(path, exist_ok=True)
        try:
            os.startfile(path)                                  # Windows
        except AttributeError:
            webbrowser.open(f"file://{path}")

    def choose_data_dir(self):
        d = filedialog.askdirectory(parent=self.root, title="Folder for runs, presets and curves",
                                    initialdir=self.data_dir)
        if not d:
            return
        self.data_dir = d
        self.s["data_dir"] = d
        self.user_presets = presets.Store(os.path.join(d, "presets.json"))
        self.curves = presets.Store(os.path.join(d, "calibrations.json"))
        self.lbl_data_dir.configure(text=d)
        self._fill_presets()
        self.refresh_runs()
        self.save_settings()

    def _figures_png(self) -> dict[str, bytes]:
        """Render every chart that has something on it."""
        figs = {}
        for name, has, draw, fig in (
                ("live", bool(self.readings), self._draw_live, self.live_fig),
                ("kinetics", len(self._kin_window()) >= 3, self._draw_kin, self.kin_fig),
                ("calibration", bool(self.s.get("standards")), self._draw_cal, self.cal_fig),
                ("spectrum", bool(self.s.get("spectrum")), self._draw_spec, self.spec_fig),
                ("scan", bool(self.scan and self.scan.samples()), self._draw_scan, self.scan_fig)):
            if not has:
                continue
            draw()
            buf = io.BytesIO()
            fig.savefig(buf, format="png", dpi=110)
            figs[name] = buf.getvalue()
        return figs

    def _run_data(self, name: str, notes: str) -> dict:
        cal = None
        if self.s.get("standards"):
            f = self.cal.fit if self.cal else None
            cal = {"unit": self.v_cal_unit.get(), "standards": self.s["standards"],
                   "fit": None if f is None else {"slope": f.slope, "intercept": f.intercept, "r2": f.r2, "n": f.n},
                   "eps": self.cal.eps if self.cal else None, "lod": self.cal.lod if self.cal else None,
                   "loq": self.cal.loq if self.cal else None}
        kin = None
        if self.kin:
            k = self.kin
            kin = {"basis": k.basis, "unit": k.unit, "best": k.best, "initial_rate": k.initial_rate,
                   "fits": {str(o): {"k": f.k, "k_units": f.k_units, "r2": f.r2, "half_life_s": f.half_life_s,
                                     "x0": f.x0, "used": f.used} for o, f in k.fits.items()}}
        sc = None
        if self.scan and self.scan.passes:
            lmax = []
            for p in self.scan.samples():
                wl, a = self.scan.corrected(p)
                ok = [(w, v) for w, v in zip(wl, a) if not math.isnan(v)]
                lm = an.lambda_max([w for w, _ in ok], [v for _, v in ok]) if ok else None
                if lm:
                    lmax.append(lm[0])
            sc = {"config": asdict(self.scan.cfg), "lambda_max": lmax,
                  "passes": [{"kind": p.kind, "number": p.number, "t_start": p.t_start,
                              "points": [[w, a] for w, a in p.points.items()]} for p in self.scan.passes]}
        keep = ("t", "wall", "source", "kind", "value", "unit", "wl_printed", "wl", "A", "T", "conc", "conc_unit",
                "conc_how", "text", "simulated", "verified")
        return {"app": APP_NAME, "version": VERSION, "name": name, "notes": notes, "simulated": self.demo,
                "instrument": dict(self.ident), "serial": self.serial_settings().describe(),
                "sample": {k: self.s.get(k) for k in ("wl", "path", "species", "eps", "mw", "unit", "c0", "a_inf",
                                                      "conc_method", "preset")},
                "readings": [{k: r.get(k) for k in keep} for r in self.readings],
                "marks": [list(m) for m in self.marks], "calibration": cal, "kinetics": kin,
                "spectrum": self.s.get("spectrum", []), "scan": sc}

    def save_run(self):
        if not (self.readings or self.s.get("standards") or (self.scan and self.scan.passes)):
            messagebox.showinfo(APP_NAME, "Nothing to save yet: take some readings, standards or a scan first.")
            return
        default = (self.v_preset.get().lstrip("★ ") or "Run")
        name = simpledialog.askstring(APP_NAME, "Name this run:", initialvalue=default, parent=self.root)
        if not name:
            return
        notes = simpledialog.askstring(APP_NAME, "Notes (sample, conditions, who ran it). Optional:",
                                       parent=self.root) or ""
        try:
            folder = runs.save_run(self.data_dir, self._run_data(name.strip(), notes.strip()), self._figures_png(),
                                   (list(self.CSV_FIELDS), [self._csv_row(r) for r in self.readings]),
                                   self.scan.table() if self.scan and self.scan.passes else None)
        except OSError as exc:
            messagebox.showerror(APP_NAME, f"Could not save the run:\n\n{exc}")
            return
        self.refresh_runs()
        self.statusbar.configure(text=f"Saved run to {folder}")
        self._dirty.update({"live", "kin", "cal", "spec", "scan"})
        if messagebox.askyesno(APP_NAME, f"Saved '{name.strip()}'.\n\nOpen its report now?"):
            self._open_path(os.path.join(folder, "report.html"))

    def refresh_runs(self):
        self.tv_runs.delete(*self.tv_runs.get_children())
        self._run_rows = {}
        for r in runs.list_runs(self.data_dir):
            name = r["name"] + ("  (SIMULATED)" if r["simulated"] else "")
            iid = self.tv_runs.insert("", "end", values=(r["saved"].replace("T", " ")[:16], name, r["readings"],
                                                         r["wl"], r["notes"]))
            self._run_rows[iid] = r

    def _selected_run(self) -> dict | None:
        sel = self.tv_runs.selection()
        if not sel:
            messagebox.showinfo(APP_NAME, "Pick a run in the list first.")
            return None
        return self._run_rows.get(sel[0])

    def run_open_report(self):
        r = self._selected_run()
        if r:
            self._open_path(os.path.join(r["folder"], "report.html"))

    def run_open_folder(self):
        r = self._selected_run()
        if r:
            self._open_path(r["folder"])

    def run_share(self):
        r = self._selected_run()
        if not r:
            return
        dest = filedialog.asksaveasfilename(parent=self.root, title="Save the share package",
                                            defaultextension=".zip", filetypes=[("Zip", "*.zip")],
                                            initialfile=os.path.basename(r["folder"]) + ".zip")
        if dest:
            z = runs.share_zip(r["folder"], dest)
            self.statusbar.configure(text=f"Share package: {z} (report.html, CSVs, charts)")

    def run_copy_summary(self):
        r = self._selected_run()
        if r:
            self.root.clipboard_clear()
            self.root.clipboard_append(runs.summary_text(runs.load_run(r["folder"])))
            self.statusbar.configure(text="Summary copied: paste it into an email or chat.")

    def run_delete(self):
        r = self._selected_run()
        if r and messagebox.askyesno(APP_NAME, f"Move '{r['name']}' to runs\\_deleted? (You can get it back "
                                               "from there.)"):
            runs.delete_run(r["folder"])
            self.refresh_runs()

    def run_load(self):
        r = self._selected_run()
        if not r:
            return
        if self.readings and not messagebox.askyesno(APP_NAME, "Replace what's on screen with this saved run? "
                                                               "(Unsaved readings stay in the logs folder.)"):
            return
        self.load_run_data(runs.load_run(r["folder"]))

    def load_run_data(self, d: dict):
        rows = []
        for x in d.get("readings", []):
            row = {"t": 0.0, "wall": time.time(), "source": "Print", "kind": "A", "value": None, "unit": "",
                   "wl_printed": None, "wl": None, "A": None, "T": None, "conc": None, "conc_unit": "",
                   "conc_how": "", "text": "", "simulated": bool(d.get("simulated")), "verified": False}
            row.update({k: v for k, v in x.items() if k in row})
            rows.append(row)
        self.readings = rows
        self.marks = [tuple(m) for m in d.get("marks", [])]
        s = d.get("sample") or {}
        for k, var in (("wl", self.v_wl), ("path", self.v_path), ("species", self.v_species), ("eps", self.v_eps),
                       ("mw", self.v_mw), ("unit", self.v_unit), ("c0", self.v_c0), ("a_inf", self.v_ainf),
                       ("conc_method", self.v_method)):
            if s.get(k) not in (None,):
                var.set(s[k])
        cal = d.get("calibration") or {}
        self.s["standards"] = [list(x) for x in cal.get("standards", [])]
        if cal.get("unit"):
            self.v_cal_unit.set(cal["unit"])
        self.s["spectrum"] = [list(x) for x in d.get("spectrum", [])]
        self.tv_cal.delete(*self.tv_cal.get_children())
        self.tv_spec.delete(*self.tv_spec.get_children())
        self._load_tables()
        self._recalibrate()
        sc = d.get("scan")
        self.scan = None
        if sc and sc.get("passes"):
            from scan import Pass
            self.scan = ScanEngine(ScanConfig(**sc["config"]))
            self.scan.passes = [Pass(p["kind"], p["number"], p.get("t_start"), {float(w): a for w, a in p["points"]})
                                for p in sc["passes"]]
            self.scan.state = "done"
        self.t0 = None
        self.last = self.readings[-1] if self.readings else None
        if self.last:
            self._show_now(self.last)
        self._viewing = d.get("name")
        self.csv_path = None
        self.statusbar.configure(text=f"Viewing saved run '{d.get('name')}' ({d.get('saved', '')}).")
        self._dirty.update({"live", "kin", "cal", "spec", "scan"})

    # ================================================================== self-update
    def check_updates(self, manual: bool = False):
        if self._updating or self._closing:
            return
        if updater.is_dev_copy():
            if manual:
                self.statusbar.configure(text="This is the development copy: it doesn't update itself "
                                              "(publish updates from it with tools/publish.py).")
            return
        if manual:
            self.statusbar.configure(text="Checking for updates...")

        def work():
            try:
                result, error = updater.check(), None
            except Exception as exc:                       # offline, no release yet, ...
                result, error = None, f"{type(exc).__name__}: {exc}"
            self._later(lambda: self._update_checked(result, error, manual))

        threading.Thread(target=work, daemon=True).start()

    def _update_checked(self, manifest, error, manual):
        if manifest:
            self._update_manifest = manifest
            notes = (manifest.get("notes") or "").strip()
            self.lbl_update.configure(text=f"Version {manifest['version']} is available (you have {VERSION})."
                                           + (f"   What's new: {notes}" if notes else ""))
            self.btn_update.state(["!disabled"])
            self.update_bar.pack(side="top", fill="x", after=self._header)
        elif manual:
            self.statusbar.configure(text=f"Could not check for updates: {error}" if error
                                     else f"You have the latest version ({VERSION}).")

    def install_update(self):
        m = self._update_manifest
        if not m or self._updating:
            return
        if not messagebox.askyesno(APP_NAME, f"Update from {VERSION} to {m['version']}?\n\nThe new version is "
                                             "downloaded and checked, then the app restarts. Your runs, "
                                             "settings and logs are kept."):
            return
        self._updating = True
        self.btn_update.state(["disabled"])
        if self.link.is_open:
            self.disconnect()

        def say(text):
            self._later(lambda: self.lbl_update.configure(text=text))

        def work():
            try:
                changed = updater.apply(m, progress=say)
                if "requirements.txt" in changed:
                    say("Installing updated Python packages (this can take a minute)...")
                    ok, log = updater.install_requirements()
                    if not ok:
                        raise updater.UpdateError("installing the Python packages failed:\n" + log[-600:])
                self._later(lambda: self._update_done(m, changed, None))
            except Exception as exc:
                self._later(lambda e=exc: self._update_done(m, None, e))

        threading.Thread(target=work, daemon=True).start()

    def _update_done(self, manifest, changed, error):
        self._updating = False
        if error is not None:
            self.lbl_update.configure(text=f"Update failed: {error}")
            self.btn_update.state(["!disabled"])
            messagebox.showerror(APP_NAME, f"The update did not install:\n\n{error}")
            return
        messagebox.showinfo(APP_NAME, f"Updated to version {manifest['version']} ({len(changed)} file(s) "
                                      "changed). The app will now restart.")
        updater.restart(sys.argv[1:])
        self.on_close()

    # ================================================================== drawing
    def _redraw(self):
        current = self.nb.tab(self.nb.select(), "text")
        todo = {"Live": "live", "Kinetics": "kin", "Calibration": "cal", "Spectrum": "spec",
                "Scan": "scan"}.get(current)
        if todo and todo in self._dirty:
            self._dirty.discard(todo)
            getattr(self, f"_draw_{todo}")()

    def _draw_scan(self):
        ax = self.scan_ax
        ax.clear()
        self.tv_scan.delete(*self.tv_scan.get_children())
        sc = self.scan
        title = "Wavelength scans"
        flagged_any = False
        if sc and sc.passes:
            samples = sc.samples()
            if sc.blank and not samples:
                pts = sorted(sc.blank.points.items())
                ax.plot([w for w, _ in pts], [a for _, a in pts], "-", color=UI["muted"], lw=1.5,
                        label="Blank (raw)")
                title = "Blank pass (raw)"
            n = len(samples)
            for i, p in enumerate(samples):
                wl, a = sc.corrected(p)
                ok = [(w, v) for w, v in zip(wl, a) if not math.isnan(v)]
                if not ok:
                    continue
                color = SCAN_RAMP[min(len(SCAN_RAMP) - 1, int(round(i * (len(SCAN_RAMP) - 1) / max(1, n - 1))))] \
                    if n > 1 else SERIES["snd"]
                label = f"{p.label} · {p.t_start / 60:.1f} min" if (n <= 8 or i in (0, n - 1)) else None
                ax.plot([w for w, _ in ok], [v for _, v in ok], "-", color=color, lw=2, label=label, zorder=2)
                if len(ok) <= 40:
                    ax.plot([w for w, _ in ok], [v for _, v in ok], "o", color=color, zorder=3, **dict(MARK, ms=5))
                bad = set(p.flags) | (set(sc.blank.flags) if sc.blank else set())
                fl = [(w, v) for w, v in ok if w in bad]
                if fl:                                          # flagged by the GENESYS: hollow markers
                    ax.plot([w for w, _ in fl], [v for _, v in fl], "o", ms=7, mfc=UI["surface"], mec=color,
                            mew=1.8, zorder=4)
                    flagged_any = True
                lm = an.lambda_max([w for w, _ in ok], [v for _, v in ok])
                self.tv_scan.insert("", "end", values=(p.label, f"{p.t_start / 60:.1f}",
                                                       f"{lm[0]:.0f}" if lm else "-", f"{lm[1]:.4f}" if lm else "-"))
                if lm and i == n - 1:
                    ax.axvline(lm[0], color=UI["ink2"], lw=1, zorder=1)
                    ax.annotate(f"λmax ≈ {lm[0]:.0f} nm", (lm[0], 1), xycoords=("data", "axes fraction"),
                                textcoords="offset points", xytext=(6, -12), fontsize=9, color=UI["ink"],
                                fontweight="semibold")
            if n > 1 or (sc.blank and not samples):
                ax.legend(loc="best")
            if samples:
                title = "Blank-corrected scans" if sc.blank else "Scans (not blank-corrected)"
            if sc.blank and not samples and sc.blank.flags:
                flagged_any = True
        if flagged_any:
            ax.text(0.01, 0.01, "Hollow points: the GENESYS flagged the reading as outside its -0.1 to 2.5 A "
                                "range (value kept).", transform=ax.transAxes, fontsize=8, color=UI["ink2"],
                    va="bottom")
        ax.set_xlabel("Wavelength (nm)")
        ax.set_ylabel("Absorbance (A)")
        ax.set_title(title)
        self.scan_canvas.draw_idle()

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
        title = f"{ylab.split(' (')[0]} over time"
        if rows:
            t, tu = self._times(rows)
            src = np.array([r["source"] for r in rows])
            y = np.array(y, dtype=float)
            ax.plot(t, y, "-", color=UI["axis"], lw=1.5, zorder=1)          # neutral guide through all points
            kinds = [k for k in ("SND", "Print") if (src == k).any()]
            for s_name, color, marker, label in (("SND", SERIES["snd"], "o", "Hands-free (SND)"),
                                                 ("Print", SERIES["print"], "D", "Print")):
                m = src == s_name
                if m.any():
                    ax.plot(t[m], y[m], ls="none", marker=marker, color=color, label=label, zorder=3, **MARK)
            if len(kinds) > 1:
                ax.legend(loc="best")
            else:
                title += "  ·  " + ("hands-free (SND)" if kinds[0] == "SND" else "Print")
            places = 4 if which == "Absorbance" else 2
            ax.annotate(f"{y[-1]:.{places}f}" if which != "Concentration" else fmt_sig(y[-1]),
                        (t[-1], y[-1]), textcoords="offset points", xytext=(8, 0), va="center",
                        fontsize=9, color=UI["ink"], fontweight="semibold")
            ax.margins(x=0.06)
        else:
            tu = self.v_xunit.get()
        for tm, lab in self.marks:
            x = tm / 60 if tu == "min" else tm
            ax.axvline(x, color=UI["muted"], lw=1, zorder=0)
            ax.annotate(lab, (x, 1), xycoords=("data", "axes fraction"), rotation=90, va="top", ha="right",
                        fontsize=8, color=UI["ink2"], xytext=(-3, -4), textcoords="offset points")
        ax.set_xlabel(f"Time ({tu})")
        ax.set_ylabel(ylab)
        ax.set_title(title)
        if not note and not self.demo and self.readings and not self.s.get("format_verified"):
            note = "Readings not yet checked against the display (Raw data tab)."
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
            ax.set_facecolor(UI["surface"])
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
            short = {0: "Zero order · [R]", 1: "First order · ln[R]", 2: "Second order · 1/[R]"}
            ylabs = {0: f"[R] ({k.unit})", 1: "ln [R]", 2: f"1/[R] ({k.unit}⁻¹)"}
            for o in (0, 1, 2):
                f = k.fits[o]
                self.tv_kin.insert("", "end", tags=("best",) if o == k.best else (), values=(
                    names[o].split("  ")[0] + ("  ← best fit" if o == k.best else ""), fmt_sig(f.k),
                    f.k_units, "-" if f.r2 is None else f"{f.r2:.5f}",
                    "-" if f.half_life_s is None else f"{fmt_sig(f.half_life_s)} s", fmt_sig(f.x0), f.used))
                ax = self.kin_axes[o]
                if o == k.best:
                    ax.set_facecolor(UI["accent_soft"])            # the best straight line gets a tint
                tt, yy = k.transformed(o)
                ax.plot(tt, yy, "o", color=SERIES["snd"], zorder=3, **dict(MARK, ms=5.5))
                if f.fit is not None:
                    xs = np.array([tt.min(), tt.max()])
                    ax.plot(xs, f.fit.y(xs), "-", color=SERIES["fit"], lw=2, zorder=2)
                    best = "   ·   best fit" if o == k.best else ""
                    ax.set_title(f"{short[o]}\nR² = {f.r2:.4f}{best}", fontsize=9.5)
                else:
                    ax.set_title(short[o], fontsize=9.5)
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
        pts = self.s.get("standards", [])
        u = self.v_cal_unit.get()
        if pts:
            c = np.array([p[0] for p in pts])
            a = np.array([p[1] for p in pts])
            if self.cal:
                xs = np.linspace(min(0, c.min()), c.max() * 1.05, 50)
                ax.plot(xs, self.cal.fit.y(xs), "-", color=SERIES["fit"], lw=2, zorder=2,
                        label=f"Fit · R² = {self.cal.fit.r2:.4f}")
            ax.plot(c, a, "o", color=SERIES["snd"], label="Standards", zorder=3, **MARK)
            if self.cal and self.last and self.last["A"] is not None:
                ca = self.cal.conc(self.last["A"])
                ax.plot([ca], [self.last["A"]], "D", color=SERIES["sample"], label="Latest reading", zorder=4,
                        **dict(MARK, ms=8))
                ax.annotate(f"{fmt_sig(ca)} {u}", (ca, self.last["A"]), textcoords="offset points",
                            xytext=(10, -4), fontsize=9, color=UI["ink"], fontweight="semibold")
            ax.legend(loc="upper left")
        ax.set_xlabel(f"Concentration ({u})")
        ax.set_ylabel("Absorbance (A)")
        ax.set_title("Calibration curve")
        self.cal_canvas.draw_idle()

    def _draw_spec(self):
        ax = self.spec_ax
        ax.clear()
        pts = self.s.get("spectrum", [])
        if pts:
            wl = np.array([p[0] for p in pts])
            a = np.array([p[1] for p in pts])
            lm = an.lambda_max(wl, a)
            if lm:
                ax.axvline(lm[0], color=UI["ink2"], lw=1, zorder=1)
                ax.annotate(f"λmax ≈ {lm[0]:.0f} nm", (lm[0], 1), xycoords=("data", "axes fraction"),
                            textcoords="offset points", xytext=(6, -12), fontsize=9, color=UI["ink"],
                            fontweight="semibold")
                self.lbl_spec.configure(text=f"λmax ≈ {lm[0]:.0f} nm   ·   A = {lm[1]:.3f}")
            ax.plot(wl, a, "-", color=SERIES["snd"], lw=2, zorder=2)
            ax.plot(wl, a, "o", color=SERIES["snd"], zorder=3, **MARK)
        else:
            self.lbl_spec.configure(text="")
        ax.set_xlabel("Wavelength (nm)")
        ax.set_ylabel("Absorbance (A)")
        ax.set_title("Absorption spectrum, point by point")
        self.spec_canvas.draw_idle()

    # ================================================================== status / misc
    def _set_status(self, state: str):
        if self.demo and state != "off":
            state = "sim"
        self._state = state
        text, bg, fg = STATUS_STYLE[state]
        self.lbl_status.configure(text=f"●  {text}", bg=bg, fg=fg)
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
                      xunit=self.v_xunit.get(), cal_unit=self.v_cal_unit.get(), preset=self.v_preset.get())
        try:
            cfg = self._scan_config()
            self.s["scan"] = {"start": cfg.start, "stop": cfg.stop, "step": cfg.step, "blank_first": cfg.blank_first,
                              "repeats": cfg.repeats, "interval_min": cfg.interval_s / 60, "settle_s": cfg.settle_s,
                              "mode": self._scan_mode()}
        except (ValueError, tk.TclError):
            pass
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
        if getattr(self, "_pump_id", None):
            try:
                self.root.after_cancel(self._pump_id)
            except tk.TclError:
                pass
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
