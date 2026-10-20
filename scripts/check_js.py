"""Syntax-check every shipped JavaScript module with the local Node installation."""

import shutil
import subprocess
from pathlib import Path


def main() -> int:
    node = shutil.which("node")
    if node is None:
        print("Node.js unavailable; JavaScript syntax check skipped.")
        return 0
    root = Path(__file__).resolve().parents[1]
    modules = sorted((root / "src/avd/api/static").rglob("*.js"))
    modules += sorted((root / "scripts").glob("*.mjs"))
    for module in modules:
        result = subprocess.run([node, "--check", str(module)], check=False, timeout=30)
        if result.returncode:
            return result.returncode
    print(f"JavaScript syntax passed: {len(modules)} modules.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
