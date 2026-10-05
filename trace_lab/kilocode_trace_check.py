"""Read-only current-session record counts for the Kilo (OpenCode fork) store.

Kilo inherits OpenCode's SQLite schema, so this delegates to the shared reader,
pointing it at Kilo's database. Never modifies the native store.
"""
import argparse
import json
from pathlib import Path

from .kilocode import DB
from .opencode_trace_check import check as opencode_check


def check(session_id, home=Path("/home/agent"), include_records=False):
    return opencode_check(session_id, home=home, include_records=include_records,
                          database=Path(home) / DB)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--session-id", required=True)
    parser.add_argument("--include-records", action="store_true")
    args = parser.parse_args()
    result = check(args.session_id, include_records=args.include_records)
    print(json.dumps(result))
    return 0 if result["verified"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
