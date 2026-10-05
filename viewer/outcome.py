"""Print one run's verdict: `python3 -m viewer.outcome <run dir>` -> "<verdict>\\t<headline>".

Used by run10.sh so every setting is summarised the same way, whatever its runner prints.
"""
import sys

from .model import load_run


def main(argv=None):
    args = list(sys.argv[1:] if argv is None else argv)
    payload = load_run(args[0]) if args else None
    if payload is None:
        print("error\tno readable run")
        return 1
    verdict = payload["verdict"]
    print(f"{verdict['verdict']}\t{verdict['headline']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
