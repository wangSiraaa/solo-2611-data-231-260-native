#!/usr/bin/env python3
"""Build delivery.h5 plus manifest.json from a small linked HDF5 fixture.

This is the thin operator-facing wrapper; see README.md for the contract and
``python -m hdf5_delivery build --help`` for options.
"""

from __future__ import annotations

import sys

from hdf5_delivery.__main__ import main

if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
