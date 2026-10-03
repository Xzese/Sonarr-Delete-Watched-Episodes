#!/usr/bin/env python3
"""Compatibility launcher for the application in src/."""

import runpy
import sys
from pathlib import Path

if __name__ == "__main__":
    source_dir = Path(__file__).resolve().parent / "src"
    sys.path.insert(0, str(source_dir))
    runpy.run_path(str(source_dir / "delete_watched_episodes.py"), run_name="__main__")
