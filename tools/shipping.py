"""What gets shipped to users, shared by publish.py (updates) and the hand-over zip."""
import getpass
import hashlib
import zipfile
from pathlib import Path

SRC = Path(__file__).resolve().parent.parent

# Installed layout == repository layout for these files. Updates replace exactly these.
SHIPPED = [
    "app.py", "genesys.py", "link.py", "analysis.py", "presets.py", "scan.py", "runs.py",
    "updater.py", "version.py", "requirements.txt", "genesys20.ico", "README.md",
    "GENESYS 20 Live.bat", "GENESYS 20 Live (demo).bat", "Install requirements.bat", "Make desktop shortcut.bat",
    "genesys20_reader.py", "tools/loopback_check.py", "tools/send_test.py", "tools/line_check.py",
    "tools/query_and_listen.py",
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
