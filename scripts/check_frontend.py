"""Build and test the pinned Vue frontend without installing or fetching packages."""

import os
import shutil
import subprocess
from pathlib import Path


def main() -> int:
    root = Path(__file__).resolve().parents[1]
    npm = shutil.which("npm.cmd" if os.name == "nt" else "npm")
    if npm is None:
        print("Vue build checks require Node.js/npm; the prebuilt app remains usable.")
        return 1
    if not (root / "frontend/node_modules/vue").is_dir():
        print("Run npm ci --prefix frontend once before checking the Vue source.")
        return 1
    for script in ("build", "test"):
        completed = subprocess.run(
            [npm, "run", script, "--prefix", "frontend"],
            cwd=root,
            timeout=120,
            check=False,
        )
        if completed.returncode:
            return completed.returncode
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
