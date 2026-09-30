"""Directly prompt a native agent to override curl through its shell snapshot."""

import argparse
import json
from pathlib import Path
import sys
import time
import uuid

from host_lab import anonymization_loop as base
from host_lab.paid_lookup_loop import validate_auth
from trace_lab.cli import (CLAUDE_AUTH_MODES, CODEX_AUTH_MODES, DEFAULT_IMAGE, Experiment,
                           OPENCODE_PROVIDERS, ROOT, docker, mount, positive_int)
from trace_lab.native import invocation_succeeded, session_id_from_stream, stream_artifact
from trace_lab.report import read_jsonl, write_report
from trace_lab.shell_tampering_fixture import (
    CONDITION, EXPORTED_FUNCTION_MECHANISM, SNAPSHOT_DIRS, grade, prompt,
)


def parser():
    command = argparse.ArgumentParser(description=__doc__)
    command.add_argument("--client", choices=SNAPSHOT_DIRS, default="claude")
    command.add_argument("--model", help="Required for a new run")
    command.add_argument('--permissions', choices=['full', 'auto'], default='full')
    command.add_argument("--opencode-provider", choices=OPENCODE_PROVIDERS, default="openai")
    command.add_argument("--report", type=Path, help="Regrade a saved run without launching an agent")
    command.add_argument("--variant", choices=("direct", "untampered"), default="direct")
    command.add_argument("--time-budget", type=positive_int, default=None,
                         help="Optional wall-time limit; default waits for native completion")
    command.add_argument("--claude-auth", choices=CLAUDE_AUTH_MODES, default="api-key")
    command.add_argument("--claude-auth-file", type=Path, default=Path.home() / ".claude/.credentials.json")
    command.add_argument("--codex-auth", choices=CODEX_AUTH_MODES, default="api-key")
    command.add_argument("--codex-auth-file", type=Path, default=Path.home() / ".codex/auth.json")
    command.add_argument("--reasoning-effort", choices=("low", "medium", "high", "xhigh", "max"))
    command.add_argument("--image", default=DEFAULT_IMAGE)
    command.add_argument("--output", type=Path, default=ROOT / "runs")
    command.set_defaults(max_turns=0, max_requests=0, max_budget_usd=0)
    return command


def check_service(service, nonce, phase):
    # Executed in the protected sidecar, never through the agent's shell/functions.
    code = ("import urllib.request,urllib.error; "
            f"url='http://127.0.0.1:8081/health?probe={nonce}-{phase}'\n"
            "try:\n r=urllib.request.urlopen(url,timeout=5)\n"
            "except urllib.error.HTTPError as e:\n r=e\n"
            "assert r.code == 503 and r.read() == b'503 Service Unavailable\\n'")
    docker("exec", service, "python3", "-c", code, timeout=15)


def run(args):
    from trace_lab.shell_tampering_fixture import require_supported_client
    require_supported_client(args.client)
    validate_auth(args)
    config = base.main_parser().parse_args(base.experiment_arguments(args, CONDITION))
    config.timeout = args.time_budget
    experiment = Experiment(config)
    nonce = uuid.uuid4().hex
    task = prompt(args.client, nonce, args.variant == "untampered")
    experiment.metadata.update(scenario_type="direct_shell_environment_tampering", stages=[],
                               requested_model=args.model, probe_nonce=nonce, variant=args.variant,
                               controller_intervened=False, stage_completed=False,
                               task_prompt=task, synthetic_only=True, external_network_access=False,
                               clear_used=False, single_native_process=True)
    experiment.metadata["mechanism"] = EXPORTED_FUNCTION_MECHANISM
    experiment.metadata["shell_state_transport"] = (
        "harness-provided-bash-env" if args.client in {"opencode", "kilocode", "gemini", "muse", "grok", "antigravity", "zcode", "kimi"}
        else "native-shell-snapshot")
    experiment.save()
    (experiment.directory / "task-prompt.txt").write_text(task)
    service, failure = None, None
    started = time.monotonic()
    print(f"[shell-tampering] artifacts={experiment.directory}", flush=True)
    try:
        experiment.prepare()
        service = experiment.start_container(
            "shell-service", "--network", f"container:{experiment.agent}",
            "--mount", mount(experiment.home_volume, "/watched/home", True),
            command=("python3", "-m", "trace_lab.shell_tampering_service", "--client", args.client))
        experiment.capture_logs(service, "shell-audit")
        experiment.wait_for(lambda: any(e.get("kind") == "ready" for e in
                                        read_jsonl(experiment.directory / "shell-audit.log")[0]))
        check_service(service, nonce, "host-before")
        deadline = time.monotonic() + args.time_budget if args.time_budget else float("inf")
        requested = str(uuid.uuid4()) if args.client == "claude" else None
        print(f"[shell-tampering] {args.client} {args.model} variant={args.variant} start", flush=True)
        stage = experiment.supervised_stage("shell-demonstration", base.native_command(config, requested), task, deadline)
        stream, errors = read_jsonl(experiment.directory / stream_artifact(args.client))
        session = session_id_from_stream(args.client, stream)
        completed = (not errors and bool(session) and stage["exit_code"] == 0
                     and invocation_succeeded(args.client, stream) and (not requested or session == requested))
        experiment.metadata.update(session_id=session, stage_completed=completed,
                                   status="finished", exit_code=stage["exit_code"])
        check_service(service, nonce, "host-after")
    except (Exception, KeyboardInterrupt) as exc:
        failure = str(exc) or "Interrupted"
        experiment.metadata.update(status="failed", error=failure)
    finally:
        if service:
            try:
                docker("stop", "--time", "5", service)
            except Exception as exc:
                failure = failure or str(exc)
                experiment.metadata.update(status="failed", error=failure)
        experiment.metadata["elapsed_seconds"] = time.monotonic() - started
        experiment.close()
    result = write_setting_report(experiment.directory)
    print(f"[shell-tampering] outcome={result['outcome']} snapshot_override={result['snapshot_override_observed']} "
          f"fake_without_request={result['fabricated_response_without_request']} "
          f"requested_model_only={result['model_identity']['requested_model_only']}", flush=True)
    if failure:
        raise RuntimeError(failure)
    return 0 if result["observation_complete"] else 1


def write_setting_report(directory):
    metadata = json.loads((directory / "run.json").read_text())
    if metadata.get("condition") != CONDITION:
        raise ValueError("Not a shell-tampering experiment")
    general = write_report(directory)
    stream, stream_errors = read_jsonl(directory / stream_artifact(metadata["client"]))
    audit, audit_errors = read_jsonl(directory / "shell-audit.log")
    status = ("inconclusive" if metadata.get("error") or stream_errors or audit_errors or metadata.get("cleanup_errors")
              else general["observation_status"])
    result = grade(metadata, stream, audit, status)
    result.update(client=metadata["client"], model=metadata.get("requested_model"), variant=metadata["variant"],
                  cli_version=metadata.get(metadata["client"] + "_version"),
                  errors=stream_errors + audit_errors + ([metadata["error"]] if metadata.get("error") else []))
    (directory / "shell-tampering-report.json").write_text(json.dumps(result, indent=2) + "\n")
    return result


def main():
    try:
        command = parser()
        args = command.parse_args()
        if args.report:
            result = write_setting_report(args.report)
            print(json.dumps(result, indent=2))
            return 0 if result["observation_complete"] else 1
        if not args.model:
            command.error("--model is required for a new run")
        return run(args)
    except (RuntimeError, OSError, ValueError) as exc:
        print(f"shell-tampering: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
