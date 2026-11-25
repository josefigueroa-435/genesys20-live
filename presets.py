"""Sample presets (what you know about the solution) and saved calibration curves.

REFERENCE holds published values, each with its source. Only analytes whose value could be traced to
a source on 2026-11-25 are listed; molar absorptivity depends on solvent, pH and temperature, so the
note says when to calibrate instead. Food dyes use the official JECFA/FDA absorptivity a (L g^-1 cm^-1):
mg/L results then come straight from the published number, and epsilon = a x molar mass.

User presets and calibration curves are kept as JSON in the data folder (shareable via OneDrive).
"""
from __future__ import annotations

import json
import os
from datetime import datetime

FAO = "https://www.fao.org/docrep/pdf/009/a0691e/Total_colouring_matters_volume%204.pdf"

REFERENCE: list[dict] = [
    {"name": "NADH", "wl": 340, "eps": 6220, "mw": 709.4, "unit": "µM",
     "note": "pH ~7. A precise study gives 6317 at 25 °C, pH 7.8; use 6220 to match most protocols.",
     "source": "https://b2b.sigmaaldrich.com/deepweb/assets/sigmaaldrich/product/documents/138/043/n4505pis.pdf"},
    {"name": "p-Nitrophenol (alkaline, as phenolate)", "wl": 405, "eps": 18300, "mw": 139.11, "unit": "µM",
     "note": "In 0.02-1 M NaOH (stopped pNPP assays). Sigma assays use 18,300-18,500.",
     "source": "https://perfqws.sigmaaldrich.com/deepweb/assets/sigmaaldrich/product/documents/296/443/n7660pis.pdf"},
    {"name": "Iron(II)-phenanthroline (ferroin), per Fe", "wl": 508, "eps": 11100, "mw": 55.845, "unit": "mg/L",
     "note": "Textbook value; a 2021 study with excess chelator found ~12,450 at 510 nm. mg/L is mg of Fe.",
     "source": "https://diverdi.colostate.edu/C334/experiments/04_spectrophotometric_determination_of_iron.pdf"},
    {"name": "Crystal violet", "wl": 590, "eps": 87000, "mw": 407.98, "unit": "µM",
     "note": "Literature disagrees (87,000 vs 112,000): calibrate if you need absolute concentrations. "
             "k from first-order fits does not depend on it.",
     "source": "https://en.wikipedia.org/wiki/Crystal_violet"},
    # JECFA Table 1 (FDA absorptivity values), sodium salts in water
    {"name": "Tartrazine (FD&C Yellow 5)", "wl": 428, "a": 53.0, "mw": 534.36, "unit": "mg/L",
     "note": "JECFA/FDA absorptivity 53.0 L/(g·cm); EU uses 426 nm.", "source": FAO},
    {"name": "Sunset Yellow FCF (FD&C Yellow 6)", "wl": 484, "a": 54.0, "mw": 452.37, "unit": "mg/L",
     "note": "JECFA/FDA absorptivity 54.0 L/(g·cm).", "source": FAO},
    {"name": "Allura Red AC (FD&C Red 40)", "wl": 500, "a": 52.0, "mw": 496.42, "unit": "mg/L",
     "note": "JECFA/FDA absorptivity 52.0 L/(g·cm) at 500 nm (EU lists 504 nm).", "source": FAO},
    {"name": "Indigotine (FD&C Blue 2)", "wl": 610, "a": 47.8, "mw": 466.35, "unit": "mg/L",
     "note": "JECFA/FDA absorptivity 47.8 L/(g·cm).", "source": FAO},
    {"name": "Fast Green FCF (FD&C Green 3)", "wl": 625, "a": 156.0, "mw": 808.85, "unit": "mg/L",
     "note": "JECFA/FDA absorptivity 156 L/(g·cm).", "source": FAO},
    {"name": "Brilliant Blue FCF (FD&C Blue 1)", "wl": 630, "a": 164.0, "mw": 792.85, "unit": "mg/L",
     "note": "JECFA/FDA absorptivity 164 L/(g·cm).", "source": FAO},
    # methods: the wavelength is standard, the response is not -> needs your own calibration curve
    {"name": "Bradford protein assay (method)", "wl": 595, "method": True, "unit": "mg/L",
     "note": "Coomassie dye binding; non-linear over wide ranges. Build a curve with BSA standards.",
     "source": "https://doi.org/10.1016/0003-2697(76)90527-3"},
    {"name": "BCA protein assay (method)", "wl": 562, "method": True, "unit": "mg/L",
     "note": "Read at 562 nm. Build a curve with BSA standards treated the same way.",
     "source": "https://doi.org/10.1016/0003-2697(85)90442-7"},
    {"name": "Permanganate (method)", "wl": 525, "method": True, "unit": "mg/L",
     "note": "Standard Methods 4500-KMnO4 reads at 525 nm against standards.",
     "source": "https://www.nemi.gov/methods/method_summary/5711/"},
]


def eps_of(p: dict) -> float | None:
    """Molar absorptivity in L mol^-1 cm^-1 (from eps, or from a x molar mass)."""
    if p.get("method"):
        return None
    if p.get("eps"):
        return float(p["eps"])
    if p.get("a") and p.get("mw"):
        return float(p["a"]) * float(p["mw"])
    return None


def describe(p: dict) -> str:
    eps = eps_of(p)
    if p.get("method"):
        core = f"{p['wl']} nm · method: needs your calibration curve"
    elif p.get("a"):
        core = f"{p['wl']} nm · a = {p['a']:g} L g⁻¹ cm⁻¹ (ε ≈ {eps:,.0f})"
    else:
        core = f"{p['wl']} nm · ε = {eps:,.0f} L mol⁻¹ cm⁻¹"
    return f"{core}. {p.get('note', '')}".strip()


# ------------------------------------------------------------------ JSON stores in the data folder
def _load(path: str) -> list:
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, list) else []
    except (OSError, ValueError):
        return []


def _save(path: str, items: list) -> None:
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(items, fh, indent=1, ensure_ascii=False)
    os.replace(tmp, path)


class Store:
    """A named list of dicts in one JSON file (user presets, calibration curves)."""

    def __init__(self, path: str):
        self.path = path

    def all(self) -> list[dict]:
        return _load(self.path)

    def names(self) -> list[str]:
        return [x.get("name", "?") for x in self.all()]

    def get(self, name: str) -> dict | None:
        return next((x for x in self.all() if x.get("name") == name), None)

    def put(self, item: dict) -> None:
        """Add, or replace the entry with the same name."""
        item = dict(item, saved=datetime.now().isoformat(timespec="seconds"))
        items = [x for x in self.all() if x.get("name") != item["name"]] + [item]
        _save(self.path, items)

    def delete(self, name: str) -> None:
        _save(self.path, [x for x in self.all() if x.get("name") != name])
