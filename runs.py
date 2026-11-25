"""Saved runs: one folder per run in <data folder>/runs, easy to reopen and to share.

    runs/2026-11-25 1450 Crystal violet fade/
        run.json        everything needed to reopen the run in the app
        readings.csv    every reading (opens in Excel)
        scan.csv        wavelength scans, if any
        report.html     a self-contained page (charts embedded) to email or print
        live.png, kinetics.png, calibration.png, spectrum.png, scan.png   (whichever exist)

Sharing = the folder itself (put the data folder on OneDrive to share automatically), or a zip of it.
Deleting moves the folder to runs/_deleted, never to oblivion.
"""
from __future__ import annotations

import base64
import csv
import html
import json
import os
import re
import shutil
import zipfile
from datetime import datetime

RUNS = "runs"
DELETED = "_deleted"


def slug(name: str) -> str:
    s = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "", name).strip().rstrip(".")
    return re.sub(r"\s+", " ", s)[:60] or "run"


def save_run(data_dir: str, data: dict, figures: dict[str, bytes], readings_table: tuple[list, list],
             scan_table: tuple[list, list] | None = None, when: datetime | None = None) -> str:
    """Write a run folder and return its path. `data` is the run.json content."""
    when = when or datetime.now()
    root = os.path.join(data_dir, RUNS)
    os.makedirs(root, exist_ok=True)
    base = f"{when:%Y-%m-%d %H%M} {slug(data.get('name', 'run'))}"
    folder = os.path.join(root, base)
    n = 2
    while os.path.exists(folder):
        folder = os.path.join(root, f"{base} ({n})")
        n += 1
    os.makedirs(folder)
    data = dict(data, saved=when.isoformat(timespec="seconds"), figures=sorted(figures))
    with open(os.path.join(folder, "run.json"), "w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=1, ensure_ascii=False, default=_json_default)
    _write_csv(os.path.join(folder, "readings.csv"), *readings_table)
    if scan_table and scan_table[1]:
        _write_csv(os.path.join(folder, "scan.csv"), *scan_table)
    for name, png in figures.items():
        with open(os.path.join(folder, f"{name}.png"), "wb") as fh:
            fh.write(png)
    with open(os.path.join(folder, "report.html"), "w", encoding="utf-8") as fh:
        fh.write(report_html(data, figures))
    return folder


def _json_default(o):
    try:
        return float(o)
    except (TypeError, ValueError):
        return str(o)


def _write_csv(path: str, head: list, rows: list) -> None:
    with open(path, "w", newline="", encoding="utf-8-sig") as fh:
        w = csv.writer(fh)
        w.writerow(head)
        w.writerows(rows)


def list_runs(data_dir: str) -> list[dict]:
    """Newest first: [{folder, name, saved, readings, wl, notes, simulated}]."""
    root = os.path.join(data_dir, RUNS)
    out = []
    if not os.path.isdir(root):
        return out
    for entry in os.listdir(root):
        folder = os.path.join(root, entry)
        meta = os.path.join(folder, "run.json")
        if entry == DELETED or not os.path.isfile(meta):
            continue
        try:
            with open(meta, encoding="utf-8") as fh:
                d = json.load(fh)
        except (OSError, ValueError):
            continue
        out.append({"folder": folder, "name": d.get("name", entry), "saved": d.get("saved", ""),
                    "readings": len(d.get("readings", [])), "wl": (d.get("sample") or {}).get("wl", ""),
                    "notes": d.get("notes", ""), "simulated": bool(d.get("simulated"))})
    out.sort(key=lambda r: r["saved"], reverse=True)
    return out


def load_run(folder: str) -> dict:
    with open(os.path.join(folder, "run.json"), encoding="utf-8") as fh:
        return json.load(fh)


def share_zip(folder: str, dest: str) -> str:
    """Zip the run folder (report, CSVs, charts, run.json) to `dest` and return its path."""
    if not dest.lower().endswith(".zip"):
        dest += ".zip"
    with zipfile.ZipFile(dest, "w", zipfile.ZIP_DEFLATED) as z:
        top = os.path.basename(folder.rstrip("\\/"))
        for name in sorted(os.listdir(folder)):
            path = os.path.join(folder, name)
            if os.path.isfile(path):
                z.write(path, f"{top}/{name}")
    return dest


def delete_run(folder: str) -> str:
    """Move a run to runs/_deleted (recoverable). Returns the new location."""
    folder = folder.rstrip("\\/")
    base = os.path.basename(folder)
    trash = os.path.join(os.path.dirname(folder), DELETED)
    os.makedirs(trash, exist_ok=True)
    dest = os.path.join(trash, base)
    n = 2
    while os.path.exists(dest):
        dest = os.path.join(trash, f"{base} ({n})")
        n += 1
    shutil.move(folder, dest)
    return dest


def summary_text(d: dict) -> str:
    """Plain-text summary for the clipboard / an email."""
    s = d.get("sample") or {}
    lines = [f"{d.get('name', 'Run')}  ({d.get('saved', '')})"]
    if d.get("simulated"):
        lines.append("SIMULATED DATA (demo mode)")
    if d.get("notes"):
        lines.append(f"Notes: {d['notes']}")
    inst = d.get("instrument") or {}
    if inst:
        lines.append("Instrument: " + ", ".join(f"{v}" for v in inst.values()))
    lines.append(f"Readings: {len(d.get('readings', []))}" + (f" at {s.get('wl')} nm" if s.get("wl") else ""))
    if s.get("preset"):
        lines.append(f"Preset: {s['preset']}")
    if s.get("eps"):
        lines.append(f"ε = {s['eps']} L mol⁻¹ cm⁻¹, b = {s.get('path', '')} cm")
    cal = d.get("calibration") or {}
    if cal.get("fit"):
        f = cal["fit"]
        lines.append(f"Calibration: A = {f['slope']:.5g}·c + {f['intercept']:.4g} ({cal.get('unit', '')}), "
                     f"R² = {f['r2']:.5f}, n = {f['n']}")
    kin = d.get("kinetics") or {}
    if kin.get("best") is not None:
        b = kin["fits"][str(kin["best"])]
        lines.append(f"Kinetics: best fit order {kin['best']}, k = {b['k']:.4g} {b['k_units']}, "
                     f"R² = {b['r2']:.5f}" + (f", t½ = {b['half_life_s']:.4g} s" if b.get("half_life_s") else ""))
    sc = d.get("scan") or {}
    if sc.get("lambda_max"):
        lines.append("Scan λmax: " + ", ".join(f"{x:.0f} nm" for x in sc["lambda_max"]))
    return "\n".join(lines)


# ------------------------------------------------------------------ report
CSS = """
body{font-family:"Segoe UI",system-ui,sans-serif;background:#f1f0ec;color:#0b0b0b;margin:0}
main{max-width:960px;margin:0 auto;padding:28px 20px 48px}
header{background:#1a1a19;color:#f0efec;padding:20px 24px;border-radius:10px}
header h1{margin:0;font-size:22px} header p{margin:6px 0 0;color:#a6a59c;font-size:13px}
.sim{display:inline-block;background:#6250d6;color:#fff;border-radius:6px;padding:2px 8px;font-size:12px;margin-left:8px}
section{background:#fcfcfb;border:1px solid #e1e0d9;border-radius:10px;padding:16px 20px;margin-top:16px}
h2{font-size:15px;margin:0 0 10px}
.chips{display:flex;flex-wrap:wrap;gap:8px;margin-top:4px}
.chip{background:#f0efec;border-radius:8px;padding:6px 10px;font-size:13px}
.chip b{display:block;font-size:11px;color:#52514e;font-weight:600}
img{max-width:100%;border:1px solid #e1e0d9;border-radius:6px;margin-top:8px}
table{border-collapse:collapse;width:100%;font-size:12.5px;font-variant-numeric:tabular-nums}
th,td{text-align:left;padding:5px 8px;border-bottom:1px solid #e1e0d9}
th{background:#f0efec;color:#52514e;font-weight:600}
.note{color:#52514e;font-size:12.5px}
"""


def report_html(d: dict, figures: dict[str, bytes]) -> str:
    e = html.escape
    s = d.get("sample") or {}
    sim = '<span class="sim">SIMULATED</span>' if d.get("simulated") else ""
    inst = ", ".join(e(str(v)) for v in (d.get("instrument") or {}).values())
    chips = [("Readings", len(d.get("readings", [])))]
    for key, label in (("wl", "Wavelength (nm)"), ("eps", "ε (L mol⁻¹ cm⁻¹)"), ("path", "Path (cm)"),
                       ("preset", "Preset")):
        if s.get(key):
            chips.append((label, s[key]))
    parts = [f"<!doctype html><html><head><meta charset='utf-8'><title>{e(d.get('name', 'Run'))}</title>"
             f"<style>{CSS}</style></head><body><main>",
             f"<header><h1>{e(d.get('name', 'Run'))}{sim}</h1><p>Saved {e(d.get('saved', ''))} · "
             f"{e(d.get('app', 'GENESYS 20 Live'))} {e(d.get('version', ''))}"
             + (f" · {inst}" if inst else "") + "</p></header>"]
    parts.append("<section><h2>Summary</h2><div class='chips'>"
                 + "".join(f"<div class='chip'><b>{e(k)}</b>{e(str(v))}</div>" for k, v in chips)
                 + f"</div><pre class='note'>{e(summary_text(d))}</pre></section>")
    titles = {"live": "Readings over time", "kinetics": "Kinetics", "calibration": "Calibration curve",
              "spectrum": "Spectrum (point by point)", "scan": "Wavelength scans"}
    for key in ("live", "kinetics", "calibration", "spectrum", "scan"):
        if key in figures:
            b64 = base64.b64encode(figures[key]).decode("ascii")
            parts.append(f"<section><h2>{titles[key]}</h2><img alt='{titles[key]}' "
                         f"src='data:image/png;base64,{b64}'></section>")
    kin = d.get("kinetics") or {}
    if kin.get("fits"):
        rows = "".join(f"<tr><td>{o}{' (best)' if str(kin.get('best')) == o else ''}</td><td>{_g(f.get('k'))}</td>"
                       f"<td>{e(f.get('k_units', ''))}</td><td>{_g(f.get('r2'), 5)}</td><td>{_g(f.get('half_life_s'))}</td></tr>"
                       for o, f in kin["fits"].items())
        parts.append("<section><h2>Kinetics fits</h2><table><tr><th>Order</th><th>k</th><th>Units</th>"
                     f"<th>R²</th><th>Half-life (s)</th></tr>{rows}</table>"
                     f"<p class='note'>{e(kin.get('basis', ''))}</p></section>")
    readings = d.get("readings", [])
    if readings:
        shown = readings[:500]
        rows = "".join(f"<tr><td>{r.get('t', 0):.1f}</td><td>{e(str(r.get('source', '')))}</td>"
                       f"<td>{_g(r.get('A'), 4)}</td><td>{_g(r.get('T'), 4)}</td>"
                       f"<td>{_g(r.get('conc'), 4)} {e(str(r.get('conc_unit') or ''))}</td>"
                       f"<td>{e(str(r.get('wl') or ''))}</td></tr>" for r in shown)
        more = f"<p class='note'>First 500 of {len(readings)} readings; all are in readings.csv.</p>" \
            if len(readings) > 500 else ""
        parts.append("<section><h2>Readings</h2><table><tr><th>t (s)</th><th>Source</th><th>A</th><th>%T</th>"
                     f"<th>Concentration</th><th>λ (nm)</th></tr>{rows}</table>{more}</section>")
    parts.append("</main></body></html>")
    return "".join(parts)


def _g(x, sig: int = 4) -> str:
    if x is None or x == "":
        return ""
    try:
        return f"{float(x):.{sig}g}"
    except (TypeError, ValueError):
        return html.escape(str(x))
