"""Compatibility launcher for running the src-layout package from the repo root."""

from pathlib import Path
import sys

PACKAGE_DIR = Path(__file__).resolve().parent / "src" / "sloth_ears"
sys.path.insert(0, str(PACKAGE_DIR.parent))
__path__ = [str(PACKAGE_DIR)]

from sloth_ears.__main__ import main


if __name__ == "__main__":
    main()
