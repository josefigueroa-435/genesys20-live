"""What gets shipped to users, shared by publish.py (updates) and the hand-over zip."""
import getpass
import hashlib
import re
import subprocess
import time
import zipfile
from pathlib import Path

SRC = Path(__file__).resolve().parent.parent
GUIDE_HTML = SRC / "docs" / "GENESYS_20_Live_Guide.html"
GUIDE_PDF = "GENESYS 20 Live - Guide.pdf"
EDGE = [r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
        r"C:\Program Files\Microsoft\Edge\Application\msedge.exe"]

# Installed layout == repository layout for these files. Updates replace exactly these.
SHIPPED = [
    "app.py", "genesys.py", "link.py", "analysis.py", "presets.py", "scan.py", "runs.py",
    "updater.py", "version.py", "requirements.txt", "genesys20.ico", "README.md",
    "READ ME FIRST.txt", GUIDE_PDF,
    "GENESYS 20 Live.bat", "GENESYS 20 Live (demo).bat", "Install requirements.bat", "Make desktop shortcut.bat",
    "genesys20_reader.py", "tools/loopback_check.py", "tools/send_test.py", "tools/line_check.py",
    "tools/query_and_listen.py", "tools/gto_test.py",
]
TESTS = ["test_core", "test_scan_presets", "test_runs", "test_updater", "test_app"]


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def read_version() -> str:
    ns: dict = {}
    exec((SRC / "version.py").read_text(encoding="utf-8"), ns)
    return ns["__version__"]


def write_version(version: str) -> None:
    (SRC / "version.py").write_text(
        '"""Version of this copy of GENESYS 20 Live. tools/publish.py bumps it."""\n'
        f'__version__ = "{version}"\n', encoding="utf-8", newline="\n")


def render_guide(scratch: Path) -> Path:
    """Print docs/GENESYS_20_Live_Guide.html to the shipped PDF with Edge (headless). The shipped copy is
    only replaced when the content changed, so an unchanged guide isn't re-downloaded by every update."""
    edge = next((e for e in EDGE if Path(e).exists()), None)
    assert edge, "Microsoft Edge is needed to print the guide to PDF"
    scratch.mkdir(parents=True, exist_ok=True)
    tmp = scratch / "guide.pdf"
    subprocess.run([edge, "--headless=new", "--disable-gpu", f"--user-data-dir={scratch / 'edge-profile'}",
                    "--no-pdf-header-footer", f"--print-to-pdf={tmp}", GUIDE_HTML.as_uri()],
                   capture_output=True, timeout=180)
    size, deadline = -1, time.monotonic() + 120          # msedge may return before the PDF is written
    while time.monotonic() < deadline:
        now = tmp.stat().st_size if tmp.exists() else -1
        if now > 100_000 and now == size:
            break
        size = now
        time.sleep(1)
    assert tmp.exists() and tmp.stat().st_size > 100_000, "guide PDF not written"
    pdf = SRC / GUIDE_PDF
    new = tmp.read_bytes()
    if not pdf.exists() or _pdf_body(pdf.read_bytes()) != _pdf_body(new):
        pdf.write_bytes(new)
    return pdf


def _pdf_body(data: bytes) -> bytes:
    return re.sub(rb"/(CreationDate|ModDate) \(D:[^)]*\)", b"", data)


def personal_strings() -> list[str]:
    """This computer's user name and home folder: must never appear in published text files."""
    out = {getpass.getuser(), Path.home().name, str(Path.home())}
    return [s for s in out if s and len(s) >= 3]


def check_no_personal_data(root: Path, names) -> None:
    bad = personal_strings()
    for rel in names:
        p = root / rel
        if p.suffix.lower() in (".py", ".bat", ".txt", ".json", ".html", ".md"):
            text = p.read_text(encoding="utf-8", errors="ignore").lower()
            hits = [b for b in bad if b.lower() in text]
            assert not hits, f"personal data {hits} in {rel}"


def build_zip(dest_dir: Path) -> Path:
    """Hand-over zip for a new user: unzip anywhere, run 'Install requirements.bat', then the app."""
    dest_dir.mkdir(parents=True, exist_ok=True)
    zp = dest_dir / f"GENESYS_20_Live_{read_version()}.zip"
    with zipfile.ZipFile(zp, "w", zipfile.ZIP_DEFLATED) as z:
        for rel in SHIPPED:
            z.write(SRC / rel, f"GENESYS 20 Live/{rel}")
    return zp
