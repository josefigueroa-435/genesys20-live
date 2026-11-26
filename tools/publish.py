"""Publish an update: every installed copy of the app offers it the next time it starts.

    py tools/publish.py 0.5.0 "What changed, in one sentence"
    py tools/publish.py 0.5.0 "..." --no-push      # do everything except git push

Steps: tests -> version.py -> personal-data check -> commit (the release) -> release.json (points at
that commit, lists every shipped file with its SHA-256) -> commit -> tag -> git push -> hand-over zip
in dist/.

Day-to-day commits and pushes do NOT reach users; only release.json does, and only this script
writes it. Needs a GitHub remote named origin (the repository in updater.REPO).
"""
import argparse
import json
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import shipping  # noqa: E402

SRC = shipping.SRC
sys.path.insert(0, str(SRC))
from updater import parse_version  # noqa: E402


def git(*args, check=True) -> str:
    r = subprocess.run(["git", *args], cwd=SRC, capture_output=True, text=True)
    if check and r.returncode != 0:
        raise SystemExit(f"git {' '.join(args)} failed:\n{r.stdout}{r.stderr}")
    return r.stdout.strip()


def last_published() -> str:
    p = SRC / "release.json"
    if p.exists():
        return json.loads(p.read_text(encoding="utf-8"))["version"]
    return "0.0.0"


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("version", help="new version, e.g. 0.5.0")
    ap.add_argument("notes", help="what changed (shown to users in the update bar)")
    ap.add_argument("--no-push", action="store_true", help="commit and tag, but do not push")
    ap.add_argument("--trailer", action="append", default=[], help="extra commit-message trailer line")
    a = ap.parse_args()

    if not (SRC / ".git").exists():
        raise SystemExit("Not a git repository.")
    if git("rev-parse", "--abbrev-ref", "HEAD") != "main":
        raise SystemExit("Publish from the main branch.")
    if not a.no_push and not git("remote", check=False):
        raise SystemExit("No GitHub remote yet. Create the repository, then:\n"
                         "    git remote add origin https://github.com/<owner>/genesys20-live.git")
    prev = last_published()
    if parse_version(a.version) <= parse_version(prev):
        raise SystemExit(f"Version {a.version} must be newer than the last published {prev}.")

    print("1/6 tests")
    r = subprocess.run([sys.executable, "-m", "unittest", *shipping.TESTS], cwd=SRC, capture_output=True,
                       text=True, env={**os.environ, "PYTHONIOENCODING": "utf-8"})
    print("   ", " ".join(r.stderr.strip().splitlines()[-3:]))
    if r.returncode != 0:
        raise SystemExit("Tests failed; nothing was published.\n" + r.stderr[-3000:])

    print("2/6 version.py ->", a.version, "+ guide PDF")
    shipping.write_version(a.version)
    import tempfile
    shipping.render_guide(Path(tempfile.mkdtemp(prefix="g20_pub_")))
    print("3/6 personal-data check")
    git("add", "-A")
    shipping.check_no_personal_data(SRC, git("ls-files").splitlines())   # the whole repo is public

    trailers = "".join(f"\n{t}" for t in a.trailer)
    print("4/6 commit release")
    git("commit", "-m", f"Release v{a.version}: {a.notes}\n{trailers}".rstrip() + "\n", "--allow-empty")
    commit = git("rev-parse", "HEAD")
    if git("status", "--porcelain"):
        raise SystemExit("Working tree not clean after commit.")

    print("5/6 release.json ->", commit[:10])
    manifest = {
        "version": a.version, "commit": commit,
        "date": datetime.now(timezone.utc).isoformat(timespec="seconds"), "notes": a.notes,
        "files": [{"path": rel, "sha256": shipping.sha256(SRC / rel), "size": (SRC / rel).stat().st_size}
                  for rel in shipping.SHIPPED],
    }
    (SRC / "release.json").write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8", newline="\n")
    git("add", "release.json")
    git("commit", "-m", f"Publish v{a.version}\n{trailers}".rstrip() + "\n")
    git("tag", "-f", f"v{a.version}", commit)

    if a.no_push:
        print("6/6 push skipped (--no-push). Run:  git push origin main --tags")
    else:
        print("6/6 git push")
        r = subprocess.run(["git", "push", "origin", "main", "--tags"], cwd=SRC)
        if r.returncode != 0:
            raise SystemExit("Push failed; the release is committed locally. Fix the problem and run:\n"
                             "    git push origin main --tags")
    zp = shipping.build_zip(SRC / "dist")
    print(f"\nPublished {a.version}. Hand-over zip for new users: {zp}")


if __name__ == "__main__":
    main()
