"""Offline native Kilo session initialization for the direct control.

Kilo is an OpenCode fork, so it uses the same session import format; no model or
network request is made.
"""

import argparse
import json
from pathlib import Path
import subprocess
import tempfile

from .opencode_session import seed_export


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--session-id", required=True)
    args = parser.parse_args()
    with tempfile.TemporaryDirectory(prefix="trace-lab-session-") as temporary:
        source = Path(temporary) / "session.json"
        source.write_text(json.dumps(seed_export(args.session_id)))
        result = subprocess.run(["kilo", "--pure", "import", str(source)],
                                capture_output=True, text=True, timeout=25)
    if result.returncode or f"Imported session: {args.session_id}" not in result.stdout:
        raise RuntimeError("Native Kilo session initialization failed: "
                           + result.stdout[-400:] + result.stderr[-400:])
    print(json.dumps({"session_id": args.session_id, "initialized": True}))


if __name__ == "__main__":
    main()
