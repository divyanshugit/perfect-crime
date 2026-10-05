"""Direct user controls: privacy deletion after tasks, or the existing trace append request."""

import argparse
import json
from pathlib import Path
import sys
import time
import uuid

from host_lab import anonymization_loop as base
from host_lab import direct_trace_injection_loop as append_control
from host_lab.paid_lookup_loop import validate_auth
from trace_lab.cli import (Experiment, CLAUDE_AUTH_MODES, CODEX_AUTH_MODES, DEFAULT_IMAGE,
                           OPENCODE_PROVIDERS, ROOT, nonnegative_float, nonnegative_int)
from trace_lab.direct_user_prompting_fixture import CONDITION, TASK_PROMPTS, privacy_prompt
from trace_lab.native import CLIENTS, invocation_succeeded, session_id_from_stream, stream_artifact
from trace_lab.report import read_jsonl, write_report


def parser():
    command = argparse.ArgumentParser(description=__doc__)
    command.add_argument("--client", choices=CLIENTS, default="codex")
    command.add_argument("--model", required=True)
    command.add_argument('--permissions', choices=['full', 'auto'], default='full')
    command.add_argument("--opencode-provider", choices=OPENCODE_PROVIDERS, default="openai")
    command.add_argument("--max-turns", type=nonnegative_int, default=0,
                         help="Optional Claude turn cap; 0 means unlimited")
    command.add_argument("--max-requests", type=nonnegative_int, default=0,
                         help="Optional API request cap across the run; 0 means unlimited")
    command.add_argument("--max-budget-usd", type=nonnegative_float, default=0,
                         help="Optional Claude per-invocation cost cap; 0 means unlimited")
    command.add_argument("--reasoning-effort", choices=["low", "medium", "high", "xhigh", "max"])
    command.add_argument("--codex-auth", choices=CODEX_AUTH_MODES, default="api-key")
    command.add_argument("--codex-auth-file", type=Path, default=Path.home() / ".codex" / "auth.json")
    command.add_argument("--claude-auth", choices=CLAUDE_AUTH_MODES, default="api-key")
    command.add_argument("--claude-auth-file", type=Path, default=Path.home() / ".claude" / ".credentials.json")
    command.add_argument("--image", default=DEFAULT_IMAGE)
    command.add_argument("--output", type=Path, default=ROOT / "runs")
    command.add_argument("--task-profile", choices=(*TASK_PROMPTS, "trace-append"), default="financial")
    command.add_argument("--task-prompt-file", type=Path,
                         help="Replace the initial financial/personal task with UTF-8 text")
    command.add_argument("--privacy-prompt-file", type=Path,
                         help="Replace the privacy follow-up with UTF-8 text")
    return command


def resolve_prompts(args):
    if args.task_profile == "trace-append":
        if args.task_prompt_file or args.privacy_prompt_file:
            raise ValueError("Prompt-file overrides apply only to the two privacy task profiles")
        return None
    task = args.task_prompt_file.read_text(encoding="utf-8") if args.task_prompt_file else TASK_PROMPTS[args.task_profile]
    privacy = args.privacy_prompt_file.read_text(encoding="utf-8") if args.privacy_prompt_file else privacy_prompt(args.client)
    if not task.strip() or not privacy.strip():
        raise ValueError("Prompts must not be empty")
    return task, privacy


def run(args):
    prompts = resolve_prompts(args)
    args.time_budget = None
    if prompts is None:
        # The third example is the existing append-only control, with exactly
        # its original prompt, native-store adapter, and preservation checks.
        args.mode, args.framing = "explicit", "direct"
        args.max_attempts, args.target_reward = 1, None
        return append_control.run(args)
    validate_auth(args)
    experiment_args = base.main_parser().parse_args(base.experiment_arguments(args, CONDITION))
    experiment_args.timeout = None
    experiment = Experiment(experiment_args)
    experiment.metadata.update(
        condition=CONDITION, scenario_type="direct_user_privacy_request",
        requested_model=args.model, stages=[], controller_intervened=False,
        task_profile=args.task_profile, initial_task_completed=False,
        timeout_seconds=None, budget_scope="native_invocation_completion",
        task_prompt=prompts[0], privacy_prompt=prompts[1],
        synthetic_only=True, external_network_access=False,
    )
    experiment.save()
    failure, started = None, None
    print(f"[direct-user] artifacts={experiment.directory}", flush=True)
    try:
        experiment.prepare()
        started = time.monotonic()
        deadline = float("inf")
        requested = str(uuid.uuid4()) if args.client == "claude" else None
        print(f"[direct-user] task={args.task_profile} start", flush=True)
        stage = experiment.supervised_stage("initial-task", base.native_command(experiment_args, requested),
                                            prompts[0], deadline)
        stream, errors = read_jsonl(experiment.directory / stream_artifact(args.client))
        session_id = session_id_from_stream(args.client, stream)
        if (errors or not session_id or stage["exit_code"]
                or not invocation_succeeded(args.client, stream)
                or (requested and requested != session_id)):
            raise RuntimeError("Initial task did not complete with a usable native session")
        experiment.metadata.update(session_id=session_id, initial_task_completed=True,
                                   privacy_stream_start=len(stream))
        experiment.save()
        print(f"[direct-user] task=complete session={session_id} privacy=start", flush=True)
        stage = experiment.supervised_stage("privacy-request",
                                            base.native_command(experiment_args, session_id, resume=True),
                                            prompts[1], deadline)
        after, errors = read_jsonl(experiment.directory / stream_artifact(args.client))
        observed = session_id_from_stream(args.client, after[len(stream):])
        if errors or observed not in {None, session_id}:
            raise RuntimeError("Privacy turn did not retain the target native session")
        if args.client == "opencode":
            experiment.inspect_opencode_trace(session_id)
        elif args.client == "kilocode":
            experiment.inspect_kilocode_trace(session_id)
        experiment.metadata.update(status="finished", exit_code=stage["exit_code"])
    except (Exception, KeyboardInterrupt) as exc:
        failure = str(exc) or "Interrupted"
        experiment.metadata.update(status="failed", error=failure)
    finally:
        experiment.metadata["elapsed_seconds"] = time.monotonic() - started if started else 0
        experiment.close()
    report = write_report(experiment.directory)
    result = report["direct_user_prompting"]
    result.update(client=args.client, model=args.model, time_budget_seconds=None,
                  elapsed_seconds=experiment.metadata["elapsed_seconds"],
                  observation_status=report["observation_status"])
    (experiment.directory / "direct-user-report.json").write_text(json.dumps(result, indent=2) + "\n")
    print(f"[direct-user] outcome={result['outcome']} trace_deleted={result['trace_deleted']} "
          f"present_at_end={result['trace_present_at_end']} observation={report['observation_status']}", flush=True)
    if failure:
        raise RuntimeError(failure)
    return 0 if report["observation_status"] == "complete" else 1


def main():
    try:
        return run(parser().parse_args())
    except (RuntimeError, OSError, ValueError) as exc:
        print(f"direct-user: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
