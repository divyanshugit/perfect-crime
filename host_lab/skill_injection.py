"""Standalone skill file injection experiment using native coding clients."""

import argparse
from pathlib import Path
import subprocess
import sys

from trace_lab.cli import (CLAUDE_AUTH_MODES, CODEX_AUTH_MODES, DEFAULT_IMAGE, ROOT, OPENCODE_PROVIDERS,
                           execute, nonnegative_float, positive_int)
from trace_lab.skill_injection_fixture import CONDITION, CLIENTS, TASK_PROFILES
from trace_lab.env import load_env


def parser():
    command = argparse.ArgumentParser(description=__doc__)
    command.add_argument("--client", choices=CLIENTS, default="codex")
    command.add_argument("--model", required=True)
    command.add_argument("--task-profile", choices=TASK_PROFILES, default="api-client")
    command.add_argument("--permissions", choices=["full", "auto"], default="full")
    command.add_argument("--opencode-provider", choices=OPENCODE_PROVIDERS, default="openai")
    command.add_argument("--time-budget", "--timeout", dest="timeout", type=positive_int, default=600)
    command.add_argument("--max-turns", type=positive_int, default=60,
                         help="Claude turn cap; other clients are bounded by time and requests")
    command.add_argument("--max-requests", type=positive_int, default=60)
    command.add_argument("--max-budget-usd", type=nonnegative_float, default=2.0,
                         help="Claude native cost cap; 0 disables this cost cap")
    command.add_argument("--reasoning-effort", choices=["low", "medium", "high", "xhigh", "max"])
    command.add_argument("--codex-auth", choices=CODEX_AUTH_MODES, default="api-key")
    command.add_argument("--codex-auth-file", type=Path, default=Path.home() / ".codex" / "auth.json")
    command.add_argument("--claude-auth", choices=CLAUDE_AUTH_MODES, default="api-key")
    command.add_argument("--claude-auth-file", type=Path, default=Path.home() / ".claude" / ".credentials.json")
    command.add_argument("--image", default=DEFAULT_IMAGE)
    command.add_argument("--output", type=Path, default=ROOT / "runs")
    command.set_defaults(command="run", condition=CONDITION,
                         permission_mode="auto", dangerously_skip_permissions=True)
    return command


def main():
    try:
        load_env(ROOT / ".env")
        args = parser().parse_args()
        args.condition = TASK_PROFILES[args.task_profile]
        return execute(args)
    except (OSError, RuntimeError, subprocess.TimeoutExpired) as exc:
        print(f"skill-injection: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
