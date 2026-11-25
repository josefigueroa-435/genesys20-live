"""Wavelength scans for a single-beam instrument, as a small state machine.

The GENESYS 20 has no scan command. A scan here is: set a wavelength, wait for it to settle, take a
reading, move on. Who sets the wavelength is up to the app (a "driver"):
  - guided:    the app shows the next wavelength; you set it on the GENESYS and press Print
               (the printed wavelength is checked against the target), or click Read now (SND);
  - simulator: the demo instrument is set directly;
  - command:   a set-wavelength command from Thermo's RS232 command set, once someone enters it.

Single-beam correction: the GENESYS is zeroed at one wavelength, so a blank's absorbance is not zero
elsewhere. A blank pass is recorded first and subtracted point by point from every sample pass:
A_corrected(λ) = A_sample(λ) - A_blank(λ), which equals -log10(I_sample / I_blank).

Repeats: sample pass k is due at  t_first_sample + k * interval  (or straight after the previous one
when the interval is shorter than a pass).
"""
from __future__ import annotations

import math
import time
from dataclasses import dataclass, field

WL_MIN, WL_MAX = 325.0, 1100.0      # GENESYS 20 range (service manual, Specifications)


@dataclass
class ScanConfig:
    start: float = 400.0
    stop: float = 700.0
    step: float = 10.0
    blank_first: bool = True
    repeats: int = 1                # number of sample passes
    interval_s: float = 0.0         # time from one sample pass start to the next
    settle_s: float = 2.0           # automatic drivers: wait after changing the wavelength

    def wavelengths(self) -> list[float]:
        if self.step <= 0:
            raise ValueError("the step must be positive")
        lo, hi = sorted((self.start, self.stop))
        if lo < WL_MIN or hi > WL_MAX:
            raise ValueError(f"the GENESYS 20 covers {WL_MIN:g}-{WL_MAX:g} nm")
        n = int(math.floor((hi - lo) / self.step + 1e-9)) + 1
        wls = [round(lo + i * self.step, 3) for i in range(n)]
        if wls[-1] < hi - 1e-9:
            wls.append(hi)
        return wls if self.start <= self.stop else wls[::-1]


@dataclass
class Pass:
    kind: str                       # "blank" or "sample"
    number: int                     # sample pass number (1, 2, ...); 0 for the blank
    t_start: float | None = None    # seconds since the scan started
    points: dict = field(default_factory=dict)   # wavelength -> absorbance (NaN = skipped)

    @property
    def label(self) -> str:
        return "Blank" if self.kind == "blank" else f"Scan {self.number}"


class ScanEngine:
    def __init__(self, cfg: ScanConfig, clock=time.monotonic):
        self.cfg = cfg
        self.clock = clock
        self.wls = cfg.wavelengths()
        self.passes: list[Pass] = []
        self.state = "idle"         # idle | running | waiting | done | stopped
        self.pos = 0
        self.t0: float | None = None
        self.next_due: float | None = None
        self._first_sample_t: float | None = None

    # ---------------------------------------------------------------- control
    def start(self) -> None:
        self.t0 = self.clock()
        self.passes = []
        self._begin(Pass("blank", 0) if self.cfg.blank_first else Pass("sample", 1))

    def _begin(self, p: Pass) -> None:
        p.t_start = self.clock() - self.t0
        if p.kind == "sample" and self._first_sample_t is None:
            self._first_sample_t = self.clock()
        self.passes.append(p)
        self.pos = 0
        self.state = "running"
        self.next_due = None

    def stop(self) -> None:
        if self.state not in ("done",):
            self.state = "stopped"

    @property
    def current(self) -> Pass | None:
        return self.passes[-1] if self.passes else None

    def target(self) -> float | None:
        """The wavelength to read next, or None when not running."""
        if self.state != "running":
            return None
        return self.wls[self.pos]

    def record(self, a: float) -> None:
        """Store the reading for the current target and advance."""
        if self.state != "running":
            raise RuntimeError("no scan point is waiting for a reading")
        self.current.points[self.wls[self.pos]] = float(a)
        self._advance()

    def skip(self) -> None:
        if self.state == "running":
            self.current.points[self.wls[self.pos]] = float("nan")
            self._advance()

    def _advance(self) -> None:
        self.pos += 1
        if self.pos < len(self.wls):
            return
        done_samples = sum(1 for p in self.passes if p.kind == "sample")
        if done_samples >= self.cfg.repeats:
            self.state = "done"
            return
        if self.current.kind == "blank":
            self._begin(Pass("sample", 1))
            return
        due = self._first_sample_t + done_samples * max(0.0, self.cfg.interval_s)
        if self.clock() >= due:
            self._begin(Pass("sample", done_samples + 1))
        else:
            self.state = "waiting"
            self.next_due = due

    def tick(self) -> bool:
        """Call regularly. Starts the next sample pass when it is due; True if it did."""
        if self.state == "waiting" and self.clock() >= self.next_due:
            n = sum(1 for p in self.passes if p.kind == "sample") + 1
            self._begin(Pass("sample", n))
            return True
        return False

    # ---------------------------------------------------------------- results
    @property
    def blank(self) -> Pass | None:
        return next((p for p in self.passes if p.kind == "blank"), None)

    def corrected(self, p: Pass) -> tuple[list[float], list[float]]:
        """(wavelengths, absorbances) of a sample pass, blank-subtracted where a blank exists."""
        b = self.blank.points if self.blank else {}
        wls, out = [], []
        for wl in self.wls:
            if wl not in p.points:
                continue
            a = p.points[wl]
            if b and wl in b and not math.isnan(b[wl]):
                a = a - b[wl]
            wls.append(wl)
            out.append(a)
        return wls, out

    def samples(self) -> list[Pass]:
        return [p for p in self.passes if p.kind == "sample"]

    def progress(self) -> str:
        if self.state == "idle":
            return "Not started"
        if self.state == "done":
            return f"Done: {len(self.samples())} scan(s) of {len(self.wls)} points"
        if self.state == "stopped":
            return "Stopped"
        if self.state == "waiting":
            left = max(0, self.next_due - self.clock())
            return f"Next scan in {int(left // 60)}:{int(left % 60):02d}"
        p = self.current
        return f"{p.label}: {self.wls[self.pos]:g} nm  ({self.pos + 1} of {len(self.wls)})"

    def table(self) -> tuple[list[str], list[list]]:
        """Header and rows for CSV: wavelength, blank, raw and corrected absorbance per scan."""
        head = ["wavelength_nm"]
        if self.blank:
            head.append("blank_A")
        for p in self.samples():
            head += [f"{p.label} raw_A (t={p.t_start:.0f}s)", f"{p.label} corrected_A"]
        rows = []
        for wl in self.wls:
            row = [wl]
            if self.blank:
                row.append(self.blank.points.get(wl, ""))
            for p in self.samples():
                raw = p.points.get(wl, "")
                cw, ca = self.corrected(p)
                cor = ca[cw.index(wl)] if wl in cw else ""
                row += [raw, cor]
            rows.append(row)
        return head, rows
