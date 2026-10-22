"""Frozen Linux public command; private child protocols use separate Python."""
import multiprocessing
import sys

if __name__ == '__main__':
    multiprocessing.freeze_support()
    sys.dont_write_bytecode = True
    from src.openclank.cli import main
    raise SystemExit(main())
