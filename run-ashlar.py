#!/usr/bin/env python3
"""Thin launcher for run-ashlar — delegates to the run_ashlar package.

Kept at the repo root so the existing launchers (run-ashlar.bat/.sh/.lnk and the
`gui` pixi task) keep working. With no arguments (or --gui) it opens the GUI;
otherwise it dispatches CLI subcommands. See `python run-ashlar.py --help`.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from run_ashlar.cli import main

if __name__ == "__main__":
    main()
