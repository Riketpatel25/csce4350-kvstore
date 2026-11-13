"""Entry point: ``python3 main.py`` starts the key-value store on STDIN/STDOUT."""

import sys

from kvstore.cli import run

if __name__ == "__main__":
    sys.exit(run())
