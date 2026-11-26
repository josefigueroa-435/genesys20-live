"""GENESYS 20 serial helpers: settings, line assembly, reading parser, raw session log, simulator.

What the manuals say (Thermo GENESYS 20 user guide 269-223100 Rev A p. 6, service manual p. 4-12):
- RS232C on a DE-9. The instrument is a DCE device, so a straight-through cable is needed.
- Baud 300-19200, data bits 7 or 8, parity off/even/odd, handshake off/CTS-RTS/XON-XOFF,
  terminator CR, LF or CR/LF (Utility > RS232).
- Print sends the currently displayed data to the selected printer (Utility > Printer setup:
  OFF, Internal, Parallel, Serial; format: Print WL yes/no, Print Units yes/no, decimal point).
- Print on a screen that isn't showing data prints that screen's parameters (e.g. the RS232 settings).

Queries (from datalyse.dk, not in Thermo's user guide) VERIFIED on Jose's GENESYS 20 (model 4001/4,
firmware "Revision = 2.10") on 2026-11-25 at 9600 8N1, CR+LF: every query is echoed, then answered,
then closed with "OK".  MODEL -> "GENESYS 20";  REV -> "GENESYS 20 Revision = 2.10";
SND -> the absorbance with 4 decimals, e.g. "0.0003". There is no scan command and no command that
reports the wavelength. Print (Printer setup = Serial, Print WL = Yes) sends e.g. "   530nm  1.351 A\\r\\n":
wavelength, then absorbance with 3 decimals and its unit; lines can arrive split across chunks.
A set-wavelength command must exist (Datalyse stepped wavelengths for its scans) but it is not public:
never guess it, because this port also accepts wavelength-calibration table uploads (MONOTAB).
Tested 2026-11-25 with Jose's OK: the GENESYS 2/5 commands WAVELENGTH (query), DATA and WAVELENGTH 550
are all refused by this GENESYS 20 with "ER" (echo, then "ER"), and the wavelength does not move.
Thermo's VISIONlite 5.4 GENESYS 20 driver (clsSP_Genesys20; Thermo said to use it) sends MODEL, REV,
SND, GTO <nm>, ZER, TRN, CCM 0/2, ECH 1 and handles ER / ER 17 / ER 18. GTO <whole nm> was verified:
"GTO 550" -> OK in 0.4 s and the instrument moved; "GTO 530" returned it exactly. SND answering a
number followed by "ER" means the reading is outside the photometric range -0.1..2.5 A (stable over
time: 560 nm gave -0.1810 + ER twice, 3 s apart); the number is kept and flagged.
Only GTO is used by this app; ZER/TRN/CCM/ECH are not sent.
A real automatic scan (500-540 nm, GTO + 1.5 s settle + SND) ran on Jose's GENESYS 20 on 2026-11-25.

Wiring lesson: the GENESYS is DCE (transmits on pin 2). Jose's DTECH female USB adapter is ALSO wired
DCE, so it only works through a male-to-female null-modem adapter.
"""
from __future__ import annotations

import json
import math
import os
import random
import re
import time
from dataclasses import asdict, dataclass
from datetime import datetime, timezone

BAUDS = (300, 600, 1200, 2400, 4800, 9600, 19200)
TERMINATORS = {"CR": b"\r", "LF": b"\n", "CRLF": b"\r\n"}
FLOWS = ("none", "rtscts", "xonxoff")
PARITIES = ("N", "E", "O")

# Read-only queries. The app refuses to send anything else, so it can never change the instrument.
READ_ONLY_COMMANDS = ("SND", "MODEL", "REV")


@dataclass
class SerialSettings:
    port: str = "COM11"
    baud: int = 9600
    bits: int = 8
    parity: str = "N"
    stop: int = 1
    flow: str = "none"
    terminator: str = "CRLF"   # what the instrument ends lines with; also appended to our queries

    def describe(self) -> str:
        flow = {"none": "no handshake", "rtscts": "CTS/RTS", "xonxoff": "XON/XOFF"}[self.flow]
        return f"{self.port} {self.baud} {self.bits}{self.parity}{self.stop}, {flow}, {self.terminator}"

    @classmethod
    def from_dict(cls, d: dict) -> "SerialSettings":
        s = cls()
        for k, v in (d or {}).items():
            if hasattr(s, k):
                setattr(s, k, type(getattr(s, k))(v))
        return s

    def to_dict(self) -> dict:
        return asdict(self)


class LineAssembler:
    """Splits a byte stream into lines ending in CR, LF or CR+LF, even when a line or the CR+LF
    pair is split across chunks. Bytes are kept as they came; nothing is decoded here."""

    def __init__(self):
        self._buf = bytearray()
        self._after_cr = False

    def feed(self, data: bytes) -> list[bytes]:
        lines = []
        for b in data:
            if b == 0x0A:
                if self._after_cr:          # the LF of a CR+LF pair
                    self._after_cr = False
                    continue
                lines.append(bytes(self._buf))
                self._buf.clear()
            elif b == 0x0D:
                lines.append(bytes(self._buf))
                self._buf.clear()
                self._after_cr = True
                continue
            else:
                self._buf.append(b)
            self._after_cr = False
        return lines

    @property
    def pending(self) -> bytes:
        return bytes(self._buf)

    def flush(self) -> bytes | None:
        """Hand over a partial line (used when the port has gone quiet)."""
        if not self._buf:
            return None
        line = bytes(self._buf)
        self._buf.clear()
        self._after_cr = False
        return line


@dataclass
class Reading:
    value: float
    kind: str            # "A", "%T" or "C"
    unit: str            # unit as printed ("A", "%T", "mg/L", ...) or "" when none was printed
    wl: float | None     # wavelength if the line included one (Print WL = Yes)
    text: str            # the decoded line

    @property
    def absorbance(self) -> float | None:
        if self.kind == "A":
            return self.value
        if self.kind == "%T":
            return absorbance_from_t(self.value)
        return None

    @property
    def transmittance(self) -> float | None:
        if self.kind == "%T":
            return self.value
        if self.kind == "A":
            return transmittance_from_a(self.value)
        return None


CONC_UNITS = ("ppm", "ppb", "g/L", "mg/L", "mg/dL", "mg/mL", "µg/L", "U/L", "M", "mM", "µM", "IU", "C")
_UNIT_RE = (r"%\s*T|T\s*%|%|Abs\b|ABS\b|A\b|mg/dL|mg/mL|mg/L|[µuμ]g/L|g/L|ppm|ppb|U/L|IU\b|mM\b|[µuμ]M\b|M\b|C\b")
_NUM_UNIT = re.compile(r"(?<![\w.])([-+]?(?:\d+\.\d*|\.\d+|\d+))(?![\d.])\s*(" + _UNIT_RE + r")?")
_WL = re.compile(r"(?<![\w.])(\d{3,4}(?:\.\d+)?)\s*nm\b", re.IGNORECASE)
_WORD = re.compile(r"[A-Za-z]{2,}")


def decode(line: bytes) -> str:
    """Latin-1 never fails and keeps one character per byte; the raw bytes are logged separately."""
    return line.decode("latin-1")


def _kind_of(unit: str) -> tuple[str, str]:
    u = unit.replace(" ", "")
    if u in ("%T", "T%", "%"):
        return "%T", "%T"
    if u in ("A", "Abs", "ABS"):
        return "A", "A"
    u = u.replace("u", "µ").replace("μ", "µ") if u[:1] in ("u", "μ") else u
    return "C", u


def parse_line(text: str, decimal: str = ".", default_kind: str = "A") -> Reading | None:
    """Pull one reading out of a printed line, or return None when the line isn't clearly one.

    The real GENESYS 20 layout has not been captured yet, so this is deliberately cautious:
    - a number with a unit (A, %T or a concentration unit) is a reading;
    - a line that is only a number (e.g. a reply to SND) is a reading of `default_kind`;
    - anything else with words or several bare numbers is left alone (it stays in the raw log).
    """
    s = text.strip()
    if not s:
        return None
    if decimal == ",":
        s = re.sub(r"(\d),(\d)", r"\1.\2", s)
    wl = None
    m = _WL.search(s)
    if m:
        wl = float(m.group(1))
        s = (s[:m.start()] + " " + s[m.end():]).strip()
    found = [(float(n), u) for n, u in _NUM_UNIT.findall(s)]
    if not found:
        return None
    with_unit = [(v, u) for v, u in found if u]
    if with_unit:
        value, unit = with_unit[0]
        kind, unit = _kind_of(unit)
        if kind == "%T" and value < 0:
            return None
        return Reading(value, kind, unit, wl, text.strip())
    leftover = _NUM_UNIT.sub(" ", s)
    if len(found) == 1 and not _WORD.search(leftover):
        return Reading(found[0][0], default_kind, "", wl, text.strip())
    return None


def absorbance_from_t(t_pct: float) -> float | None:
    if t_pct is None or t_pct <= 0:
        return None
    return 2.0 - math.log10(t_pct)


def transmittance_from_a(a: float) -> float:
    return 100.0 * 10.0 ** (-a)


class SessionLog:
    """Every byte in and out, as JSON Lines, flushed per chunk so nothing is lost on a crash."""

    def __init__(self, folder: str, simulated: bool = False):
        self.folder = folder
        self.simulated = simulated
        self.path: str | None = None
        self._fh = None

    def _open(self):
        os.makedirs(self.folder, exist_ok=True)
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        tag = "-SIMULATED" if self.simulated else ""
        self.path = os.path.join(self.folder, f"raw-{stamp}{tag}.jsonl")
        self._fh = open(self.path, "a", encoding="utf-8")

    def write(self, direction: str, data: bytes, settings: SerialSettings | None = None, note: str = ""):
        if self._fh is None:
            self._open()
        rec = {"utc": datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
               "dir": direction, "hex": data.hex(), "text": data.decode("ascii", errors="backslashreplace")}
        if settings is not None:
            rec["serial"] = settings.describe()
        if note:
            rec["note"] = note
        if self.simulated:
            rec["simulated"] = True
        self._fh.write(json.dumps(rec) + "\n")
        self._fh.flush()

    def close(self):
        if self._fh is not None:
            self._fh.close()
            self._fh = None


# --------------------------------------------------------------------------- simulator
class SimulatedCuvette:
    """A dye in a 1 cm cuvette for the demo: Beer-Lambert absorbance, optional first-order fading.

    The defaults are illustrative demo numbers (a crystal-violet-like band at 590 nm), not data."""

    def __init__(self, conc_m: float = 1.0e-5, eps_max: float = 87000.0, peak_nm: float = 590.0,
                 width_nm: float = 45.0, k: float = 0.004, baseline: float = 0.004, path_cm: float = 1.0):
        self.conc_m = conc_m
        self.eps_max = eps_max
        self.peak_nm = peak_nm
        self.width_nm = width_nm
        self.k = k
        self.baseline = baseline
        self.path_cm = path_cm
        self.react_t0: float | None = None

    def eps(self, wl: float) -> float:
        return self.eps_max * math.exp(-0.5 * ((wl - self.peak_nm) / self.width_nm) ** 2)

    def conc(self, now: float) -> float:
        if self.react_t0 is None:
            return self.conc_m
        return self.conc_m * math.exp(-self.k * max(0.0, now - self.react_t0))

    def absorbance(self, wl: float, now: float) -> float:
        return self.eps(wl) * self.path_cm * self.conc(now) + self.baseline


class SimulatedGenesys:
    """Stands in for the serial port in --demo and in tests. It answers SND/MODEL/REV exactly like
    the real GENESYS 20 (echo, answer, OK) and prints on press_print(). The Print layout is still
    made up: the real one has not been captured yet."""

    def __init__(self, cuvette: SimulatedCuvette | None = None, terminator: bytes = b"\r\n",
                 noise: float = 0.0015, seed: int | None = None, clock=time.monotonic,
                 print_wl: bool = True, read_timeout: float = 0.05):
        self.cuvette = cuvette or SimulatedCuvette()
        self.wl = 590.0
        self.term = terminator
        self.noise = noise
        self.rng = random.Random(seed)
        self.clock = clock
        self.print_wl = print_wl
        self.read_timeout = read_timeout
        self.is_open = True
        self._out = bytearray()
        self._in = bytearray()
        self.received: list[str] = []
        self.range = (-0.1, 2.5)      # like the real GENESYS 20: SND outside this -> value + "ER"

    # serial.Serial look-alike ------------------------------------------------
    @property
    def in_waiting(self) -> int:
        return len(self._out)

    def read(self, n: int = 1) -> bytes:
        if not self._out and self.read_timeout:
            time.sleep(self.read_timeout)
        data = bytes(self._out[:n])
        del self._out[:n]
        return data

    def write(self, data: bytes) -> int:
        self._in += data
        while True:
            idx = [i for i in (self._in.find(b"\r"), self._in.find(b"\n")) if i >= 0]
            if not idx:
                break
            i = min(idx)
            cmd = bytes(self._in[:i]).decode("ascii", "replace").strip()
            del self._in[:i + 1]
            if cmd:
                self._command(cmd)
        return len(data)

    def flush(self):
        pass

    def close(self):
        self.is_open = False

    # instrument behaviour ----------------------------------------------------
    def absorbance(self) -> float:
        a = self.cuvette.absorbance(self.wl, self.clock())
        return a + self.rng.gauss(0.0, self.noise)

    def _command(self, cmd: str):
        self.received.append(cmd)
        self._out += cmd.encode() + self.term          # the real instrument echoes each query
        m = re.fullmatch(r"GTO (\d{3,4})", cmd)
        if m:
            wl = float(m.group(1))
            if not 325 <= wl <= 1100:
                self._out += b"ER" + self.term
                return
            self.wl = wl
            self._out += b"OK" + self.term
            return
        if cmd == "SND":
            a = self.absorbance()
            self._out += f"{a:.4f}".encode() + self.term
            if not self.range[0] <= a <= self.range[1]:
                self._out += b"ER" + self.term           # outside the photometric range: flagged
                return
        elif cmd == "MODEL":
            self._out += b"GENESYS 20 (SIMULATED)" + self.term
        elif cmd == "REV":
            self._out += b"GENESYS 20 Revision = 2.10-SIM" + self.term
        else:
            self._out += b"ER" + self.term               # what the real GENESYS 20 says to unknown commands
            return
        self._out += b"OK" + self.term

    def press_print(self):
        a = self.absorbance()
        line = f"{a:6.3f} A"
        if self.print_wl:
            line = f"{int(self.wl)}nm  " + line
        self._out += line.encode() + self.term
