"""Chemistry on top of the readings: Beer-Lambert, calibration curves, kinetics fits, lambda-max.

Everything here is plain numpy so it can be unit-tested without the GUI or an instrument.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field

import numpy as np

# concentration units -> factor to mol/L (molar units) or to g/L (mass units)
MOLAR = {"M": 1.0, "mM": 1e-3, "µM": 1e-6, "nM": 1e-9}
MASS = {"g/L": 1.0, "mg/L": 1e-3, "mg/mL": 1.0, "µg/mL": 1e-3, "µg/L": 1e-6, "ppm": 1e-3}
UNITS = tuple(MOLAR) + tuple(MASS)


def to_molar(value: float, unit: str, mw: float | None = None) -> float | None:
    """Concentration in mol/L, or None if a mass unit is given without a molar mass."""
    if unit in MOLAR:
        return value * MOLAR[unit]
    if unit in MASS:
        if not mw:
            return None
        return value * MASS[unit] / mw
    raise ValueError(f"unknown unit {unit!r}")


def from_molar(c_m: float, unit: str, mw: float | None = None) -> float | None:
    if unit in MOLAR:
        return c_m / MOLAR[unit]
    if unit in MASS:
        if not mw:
            return None
        return c_m * mw / MASS[unit]
    raise ValueError(f"unknown unit {unit!r}")


def beer_lambert_conc(a: float, eps: float, path_cm: float) -> float:
    """c (mol/L) = A / (epsilon * b), with epsilon in L mol^-1 cm^-1 and b in cm."""
    if not eps or not path_cm:
        raise ValueError("need a molar absorptivity and a path length")
    return a / (eps * path_cm)


# --------------------------------------------------------------------------- straight lines
@dataclass
class LineFit:
    slope: float
    intercept: float
    r2: float
    n: int
    s_yx: float | None          # standard error of the regression (needs n >= 3)
    se_slope: float | None
    se_intercept: float | None

    def y(self, x):
        return self.slope * np.asarray(x, dtype=float) + self.intercept

    def x_for(self, y: float) -> float:
        return (y - self.intercept) / self.slope


def fit_line(x, y) -> LineFit:
    x = np.asarray(x, dtype=float)
    y = np.asarray(y, dtype=float)
    n = len(x)
    if n < 2:
        raise ValueError("need at least 2 points")
    xm, ym = x.mean(), y.mean()
    sxx = float(((x - xm) ** 2).sum())
    if sxx == 0:
        raise ValueError("all x values are the same")
    slope = float(((x - xm) * (y - ym)).sum() / sxx)
    intercept = float(ym - slope * xm)
    resid = y - (slope * x + intercept)
    ss_res = float((resid ** 2).sum())
    ss_tot = float(((y - ym) ** 2).sum())
    r2 = 1.0 - ss_res / ss_tot if ss_tot > 0 else 1.0
    s_yx = se_slope = se_int = None
    if n >= 3:
        s_yx = math.sqrt(ss_res / (n - 2))
        se_slope = s_yx / math.sqrt(sxx)
        se_int = s_yx * math.sqrt(1.0 / n + xm ** 2 / sxx)
    return LineFit(slope, intercept, r2, n, s_yx, se_slope, se_int)


# --------------------------------------------------------------------------- calibration
@dataclass
class Calibration:
    fit: LineFit
    unit: str
    path_cm: float
    lod: float | None = None      # 3.3 s_yx / slope (concentration units)
    loq: float | None = None      # 10 s_yx / slope
    eps: float | None = None      # slope / b, in L mol^-1 cm^-1 (molar units only)

    def conc(self, a: float) -> float:
        return self.fit.x_for(a)


def calibrate(concs, absorbances, unit: str, path_cm: float = 1.0) -> Calibration:
    fit = fit_line(concs, absorbances)
    if fit.slope == 0:
        raise ValueError("the standards give a flat line")
    cal = Calibration(fit, unit, path_cm)
    if fit.s_yx is not None:
        cal.lod = 3.3 * fit.s_yx / abs(fit.slope)
        cal.loq = 10.0 * fit.s_yx / abs(fit.slope)
    if unit in MOLAR and path_cm:
        cal.eps = fit.slope / MOLAR[unit] / path_cm
    return cal


# --------------------------------------------------------------------------- kinetics
@dataclass
class OrderFit:
    order: int
    k: float | None
    k_units: str
    r2: float | None
    half_life_s: float | None
    x0: float | None               # fitted starting amount (same units as the tracked quantity)
    fit: LineFit | None
    used: int                      # points used (ln and 1/x need positive values)
    note: str = ""


@dataclass
class Kinetics:
    basis: str                     # human description of what was tracked
    unit: str                      # unit of the tracked quantity
    t: np.ndarray
    x: np.ndarray                  # tracked reactant amount (concentration or absorbance signal)
    fits: dict = field(default_factory=dict)
    best: int | None = None
    initial_rate: float | None = None   # -dx/dt at the start, unit per s

    def transformed(self, order: int):
        """(t, y) used for that order's straight line."""
        ok = self.x > 0
        if order == 0:
            return self.t, self.x
        if order == 1:
            return self.t[ok], np.log(self.x[ok])
        return self.t[ok], 1.0 / self.x[ok]


def reactant_amount(a, species: str = "reactant", eps: float | None = None, path_cm: float = 1.0,
                    a_inf: float | None = None, c0_m: float | None = None, unit: str = "M",
                    mw: float | None = None):
    """Turn absorbances into the amount of reactant left. Returns (x, unit_label, basis_text).

    reactant absorbs:  [R] = (A - A_inf) / (eps b), or the signal A - A_inf without eps
    product absorbs:   [R] = (A_inf - A) / (eps b) with A_inf, or c0 - A / (eps b) without it,
                       or the signal A_inf - A without eps (1:1 stoichiometry assumed)
    """
    a = np.asarray(a, dtype=float)
    base = a_inf or 0.0
    if species == "reactant":
        sig = a - base
        what = "A - A∞" if a_inf else "A"
    elif species == "product":
        if a_inf is not None:
            sig = a_inf - a
            what = "A∞ - A"
        elif eps and c0_m:
            c = c0_m - a / (eps * path_cm)
            return _in_unit(c, unit, mw), unit, f"[reactant] = c₀ - A/(εb), ε = {eps:g}"
        else:
            raise ValueError("when the product absorbs, enter A∞ (final absorbance), "
                             "or both ε and the starting concentration")
    else:
        raise ValueError(f"unknown species {species!r}")
    if eps:
        c = sig / (eps * path_cm)
        return _in_unit(c, unit, mw), unit, f"[reactant] = ({what})/(εb), ε = {eps:g}, b = {path_cm:g} cm"
    return sig, "A", f"signal {what} (no ε, so k for orders 0 and 2 is in absorbance units)"


def _in_unit(c_m, unit, mw):
    out = from_molar(1.0, unit, mw)
    if out is None:
        raise ValueError(f"{unit} needs a molar mass")
    return np.asarray(c_m, dtype=float) * out


def fit_kinetics(t, x, unit: str, basis: str = "") -> Kinetics:
    t = np.asarray(t, dtype=float)
    x = np.asarray(x, dtype=float)
    if len(t) < 3:
        raise ValueError("need at least 3 readings in the fit window")
    kin = Kinetics(basis, unit, t, x)
    per_s = "s⁻¹"
    units = {0: f"{unit} {per_s}", 1: per_s, 2: f"{unit}⁻¹ {per_s}"}
    for order in (0, 1, 2):
        tt, yy = kin.transformed(order)
        if len(tt) < 3 or np.ptp(tt) == 0:
            kin.fits[order] = OrderFit(order, None, units[order], None, None, None, None, len(tt),
                                       "not enough positive values")
            continue
        lf = fit_line(tt, yy)
        if order == 0:
            k, x0 = -lf.slope, lf.intercept
            hl = x0 / (2 * k) if k > 0 and x0 > 0 else None
        elif order == 1:
            k, x0 = -lf.slope, math.exp(lf.intercept)
            hl = math.log(2) / k if k > 0 else None
        else:
            k = lf.slope
            x0 = 1.0 / lf.intercept if lf.intercept > 0 else None
            hl = 1.0 / (k * x0) if k > 0 and x0 else None
        note = "" if len(tt) == len(t) else f"{len(t) - len(tt)} non-positive points left out"
        kin.fits[order] = OrderFit(order, k, units[order], lf.r2, hl, x0, lf, len(tt), note)
    ranked = [o for o in (0, 1, 2) if kin.fits[o].r2 is not None]
    if ranked:
        kin.best = max(ranked, key=lambda o: kin.fits[o].r2)
    kin.initial_rate = initial_rate(t, x)
    return kin


def initial_rate(t, x, fraction: float = 0.15, min_points: int = 3) -> float | None:
    """-dx/dt from a straight line through the first part of the run (default first 15 % of the time)."""
    t = np.asarray(t, dtype=float)
    x = np.asarray(x, dtype=float)
    if len(t) < min_points:
        return None
    cut = t[0] + fraction * (t[-1] - t[0])
    n = max(min_points, int((t <= cut).sum()))
    lf = fit_line(t[:n], x[:n])
    return -lf.slope


# --------------------------------------------------------------------------- spectrum
def lambda_max(wl, a) -> tuple[float, float] | None:
    """(wavelength, absorbance) of the highest point, refined by a parabola through it and its
    neighbours when they are evenly spaced enough to make that meaningful."""
    wl = np.asarray(wl, dtype=float)
    a = np.asarray(a, dtype=float)
    if len(wl) == 0:
        return None
    order = np.argsort(wl)
    wl, a = wl[order], a[order]
    i = int(np.argmax(a))
    if 0 < i < len(wl) - 1:
        x = wl[i - 1:i + 2]
        y = a[i - 1:i + 2]
        c2, c1, c0 = np.polyfit(x, y, 2)
        if c2 < 0:
            xv = -c1 / (2 * c2)
            if x[0] <= xv <= x[2]:
                return float(xv), float(c0 + c1 * xv + c2 * xv * xv)
    return float(wl[i]), float(a[i])
