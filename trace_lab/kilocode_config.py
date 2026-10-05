"""Write the isolated Kilo provider config (~/.config/kilo/kilo.json) in-container.

Runs as root in the disposable agent home; the provider points Kilo at the
loopback gateway with a placeholder key. See docs/kilocode.md.
"""
import argparse
import os
from pathlib import Path

from .kilocode import initialize


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model", required=True)
    parser.add_argument("--home", default="/home/agent")
    parser.add_argument("--permissions", choices=("full", "auto"), default="full")
    args = parser.parse_args()
    path = initialize(args.model, args.home, args.permissions)
    if os.geteuid() == 0:
        home = Path(args.home)
        for parent in [path, *path.parents]:
            if parent == home:
                break
            os.chown(parent, 1000, 1000)


if __name__ == "__main__":
    main()
