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
import queue
import re
import sys
import threading
import time
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
from link import Link  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
APP_NAME = "GENESYS 20 Live"
VERSION = "0.3.0"

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
        self._build_raw_tab()
        self.nb.bind("<<NotebookTabChanged>>", lambda e: self._dirty.update({"live", "kin", "cal", "spec"}))

    def _build_header(self, r):
        bar = tk.Frame(r, bg=UI["header"], height=52)
        bar.pack(fill="x")
        bar.pack_propagate(False)
        tk.Label(bar, text="GENESYS 20 Live", bg=UI["header"], fg=UI["header_ink"],
                 font=(FONT, 14, "bold")).pack(side="left", padx=(16, 8))
        tk.Label(bar, text=f"v{VERSION}  ·  Thermo Scientific GENESYS 20", bg=UI["header"],
                 fg=UI["header_muted"], font=(FONT, 9)).pack(side="left", pady=(4, 0))
        self.lbl_status = tk.Label(bar, text="", font=(FONT, 9, "bold"), padx=12, pady=4)
        self.lbl_status.pack(side="right", padx=16)

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
        now = time.time()
        if self.t0 is not None:
            el = int(now - self.t0)
            self.lbl_run.configure(text=f"Run time {el // 60:02d}:{el % 60:02d}   ·   {len(self.readings)} readings")
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
            ylabs = {0: f"[R] ({k.unit})", 1: f"ln [R]", 2: f"1/[R] ({k.unit}⁻¹)"}
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
