"""Kör paketet som program: python -m prenly_dl."""

import sys

from .cli import main

if __name__ == "__main__":
    sys.exit(main())
