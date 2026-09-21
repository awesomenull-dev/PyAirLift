"""Allow ``python -m pyairlift`` to run the CLI."""

import sys

from pyairlift.cli import main

if __name__ == "__main__":
    sys.exit(main())