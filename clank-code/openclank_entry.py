#!/usr/bin/env python3
"""PyInstaller entrypoint for the public Open Clank command."""

import multiprocessing

from src.openclank.cli import main


if __name__ == "__main__":
    multiprocessing.freeze_support()
    raise SystemExit(main())
