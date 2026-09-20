"""Snapshot the current Qoder login into a per-account auth dir.

Usage:
    python scripts/snapshot_qoder_auth.py acc1
    python scripts/snapshot_qoder_auth.py acc2 --src C:/Users/zhaoj/.qoder/.auth

Why: Qoder keeps ONE global login at %USERPROFILE%\\.qoder\\.auth. To hold several
accounts at once, log in with the Qoder CLI, then snapshot the whole .auth dir
(the AES key is machine_id[:16], so user + machine_id must be copied together).

Default destination root: %USERPROFILE%\\.qoder\\snapshots\\  (outside the repo)
"""

from __future__ import annotations

import argparse
import os
import shutil
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
# Credentials stay out of the repo: snapshots live in %USERPROFILE%\.qoder\snapshots
DEFAULT_DEST_ROOT = Path.home() / ".qoder" / "snapshots"
FILES = ("user", "machine_id")


def snapshot(name: str, src: Path, dest_root: Path) -> Path:
    if not src.is_dir():
        raise SystemExit(f"source auth dir not found: {src}")
    missing = [f for f in FILES if not (src / f).is_file()]
    if missing:
        raise SystemExit(f"source auth dir incomplete, missing: {', '.join(missing)}")
    dest = dest_root / name / ".auth"
    dest.mkdir(parents=True, exist_ok=True)
    for f in FILES:
        shutil.copy2(src / f, dest / f)
        try:
            os.chmod(dest / f, 0o600)
        except OSError:
            pass
    return dest


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("name", help="account label, e.g. acc1 / work")
    ap.add_argument("--src", default=str(Path.home() / ".qoder" / ".auth"), help="source .auth dir")
    ap.add_argument("--dest-root", default=str(DEFAULT_DEST_ROOT), help="destination root")
    args = ap.parse_args()

    dest = snapshot(args.name, Path(args.src), Path(args.dest_root))
    print(f"snapshotted -> {dest}")
    print("next: log in with the Qoder CLI as the next account, then run this again with another name")
    return 0


if __name__ == "__main__":
    sys.exit(main())
