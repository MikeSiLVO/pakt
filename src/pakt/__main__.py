"""Entry point for python -m pakt."""

import os
import sys

# Fix for pythonw (no console) - replace None stdin/stdout/stderr with devnull
# Must happen before importing cli which creates Rich Console at module level
if sys.stdin is None:
    sys.stdin = open(os.devnull, "r")
if sys.stdout is None:
    sys.stdout = open(os.devnull, "w")
if sys.stderr is None:
    sys.stderr = open(os.devnull, "w")

from pakt.cli import main

if __name__ == "__main__":
    main()
