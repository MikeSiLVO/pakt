"""Entry point for python -m pakt."""

import os
import sys

# pythonw leaves the std streams as None, which the Rich consoles created on import cannot use
if sys.stdin is None:
    sys.stdin = open(os.devnull, "r")
if sys.stdout is None:
    sys.stdout = open(os.devnull, "w")
if sys.stderr is None:
    sys.stderr = open(os.devnull, "w")

from pakt.cli import main

if __name__ == "__main__":
    main()
