"""Host controller. All subprocesses use argument arrays, never a shell."""

from . import extended_harnesses as extended
from . import kilocode

import argparse
import base64
from contextlib import ExitStack
from datetime import datetime, timezone
import json
import math
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import time
import uuid

from .fixtures import PRIME_CONDITIONS, PROMPTS, cleanup_prompt
from .native import (CLIENTS, invocation_succeeded, session_id_from_stream, sqlite_store,
                     stderr_artifact, stream_artifact)
from .env import load_env
from .openai_gateway import PROVIDERS as OPENCODE_PROVIDERS
from . import compaction_skill_fixture, shell_tampering_fixture, shell_unit_testing_fixture
from .report import read_jsonl, write_report
from .claude_auth import validate_auth_cache as validate_claude_auth_cache
from .codex_auth import validate_auth_cache as validate_codex_auth_cache
from . import (booking_scenario, crack_7z_scenario, dataset_scenario,
               document_optimization_fixture, privacy_probe, release_scenario,
               skill_injection_fixture, direct_user_prompting_fixture,
               rollout_influence_fixture, terminal_bench_bun_fixture,
               trace_canary_fixture, payment_lookup_fixture, optional_payment_lookup_fixture)

ROOT = Path(__file__).resolve().parent.parent
# Slimmed image: only claude, opencode and kilocode are built for now (the other
# agents are commented out in the Dockerfile). Restore their tokens here when the
# full image is rebuilt.
DEFAULT_IMAGE = "trace-lab:claude-2.1.269-opencode-1.18.30-kilocode-7.8.1"
MAX_ARTIFACT_BYTES = 128 * 1024 * 1024
LABEL = "org.trace-lab.run"
CODEX_AUTH_MODES = ("api-key", "subscription")
CLAUDE_AUTH_MODES = ("api-key", "subscription")


def docker(*args, timeout=60, check=True, input=None):
    result = subprocess.run(["docker", *args], input=input,
                            capture_output=True, text=True, timeout=timeout)
    if check and result.returncode:
        raise RuntimeError(f"Docker {args[0]} failed: {result.stderr.strip() or result.stdout.strip()}")
    return result


def check_engine():
    if not shutil.which("docker"):
        raise RuntimeError("Docker CLI is missing. Install and start Docker Desktop first.")
    return docker("version", "--format", "{{.Server.Version}}").stdout.strip()


def observer_trace_contents(client, condition, capture):
    # Optimization captures OpenCode's native rows independently before and
    # after continuation. Repeated binary WAL snapshots are not logical history
    # and can exhaust the recorder on long schema-discovery attempts.
    return capture and not (client in {'opencode', 'kilocode'}
                            and condition == compaction_skill_fixture.OPTIMIZATION_CONDITION)


def skill_gateway_progress(records):
    """Follow the agent request rather than background title-generation traffic."""
    requests = [r for r in records if r.get("kind") == "gateway_request"
                and r.get("client_tools_present")]
    if not requests:
        return {"skill_advertised": None, "api_status": "waiting_request"}
    request = requests[-1]
    state = "waiting_response"
    for record in records:
        if record.get("request_id") != request["request_id"]:
            continue
        if record.get("kind") == "gateway_response":
            status = record["status"]
            state = "streaming" if 200 <= status < 300 else f"http_{status}"
        elif record.get("kind") == "gateway_finished" and state == "streaming":
            state = "response_complete"
        elif record.get("kind") == "gateway_error":
            state = record["error_type"]
    return {"skill_advertised": request.get("skill_advertised"), "api_status": state}


def mount(volume, destination, readonly=False):
    # Docker's automatic population can reset ownership of an empty volume on
    # later mounts. Only the explicit initializer populates experiment volumes.
    return f"type=volume,src={volume},dst={destination},volume-nocopy" + (",readonly" if readonly else "")


def bind_mount(source, destination, readonly=True):
    if any(character in str(source) for character in (",", "\n", "\0")):
        raise RuntimeError("Docker bind source contains an unsupported character")
    value = f"type=bind,src={source},dst={destination}"
    return value + (",readonly" if readonly else "")


def _codex_subscription_available(path=None):
    try:
        validate_codex_auth_cache(path or Path.home() / ".codex" / "auth.json")
        return True
    except (OSError, RuntimeError):
        return False


def agent_network_mode(kind, condition):
    return "bridge" if kind == "model" and (condition in privacy_probe.CONDITIONS
                       or condition in skill_injection_fixture.DOWNLOAD_CONDITIONS) else "none"


def agent_workspace(condition):
    return (crack_7z_scenario.SANDBOX_PATH
            if condition == crack_7z_scenario.CONDITION else "/workspace")


def agent_hostname(condition):
    return (crack_7z_scenario.SANDBOX_NAME
            if condition == crack_7z_scenario.CONDITION else None)


def native_command(args, session_id, resume=False):
    client = getattr(args, "client", "claude")
    reasoning_effort = getattr(args, "reasoning_effort", None)
    permissions = getattr(args, 'permissions', 'full')
    if permissions == 'auto' and client not in {*extended.CLIENTS, 'opencode', 'kilocode'}:
        raise ValueError('This checkout implements --permissions auto only for Muse, Grok, Antigravity, OpenCode, and Kilo; other clients use the preserved auto batch source.')
    if client in extended.CLIENTS:
        return extended.native_command(client, args.model, agent_workspace(args.condition),
                                       session_id, resume, reasoning_effort, permissions)
    if client == "gemini":
        if reasoning_effort:
            raise ValueError("Gemini CLI does not support --reasoning-effort; omit it")
        command = ["gemini", "--approval-mode", "yolo", "--output-format", "stream-json",
                   "--model", args.model]
        if resume:
            if not session_id:
                raise ValueError("A Gemini session ID is required for resume")
            command.extend(["--resume", session_id])
        return command
    if client == "codex":
        if args.codex_auth == "subscription":
            provider = (
                'model_providers.trace_lab={name="Trace Lab ChatGPT",'
                'base_url="http://127.0.0.1:8080/backend-api/codex",'
                'requires_openai_auth=true,wire_api="responses",request_max_retries=0,'
                'stream_max_retries=0}'
            )
        else:
            provider = (
                'model_providers.trace_lab={name="Trace Lab",'
                'base_url="http://127.0.0.1:8080/v1",env_key="OPENAI_API_KEY",'
                'wire_api="responses",request_max_retries=0,stream_max_retries=0}'
            )
        common = [
            "--json", "--model", args.model, "--skip-git-repo-check",
            "--ignore-user-config", "--ignore-rules", "--strict-config",
            "-c", 'approval_policy="never"',
            "-c", 'sandbox_mode="danger-full-access"',
            "-c", 'web_search="disabled"',
            "-c", "features.apps=false",
            "-c", "features.multi_agent=false",
            "-c", "features.remote_plugin=false",
            "-c", "features.responses_websockets=false",
            "-c", "features.responses_websockets_v2=false",
            "-c", "feedback.enabled=false",
            "-c", 'model_provider="trace_lab"', "-c", provider,
        ]
        if reasoning_effort:
            common.extend(["-c", f'model_reasoning_effort="{reasoning_effort}"'])
        if resume:
            if not session_id:
                raise ValueError("A Codex session ID is required for resume")
            return ["codex", "exec", "resume", *common, session_id, "-"]
        return ["codex", "exec", *common, "-C", agent_workspace(args.condition), "-"]
    if client == "opencode":
        command = [
            "opencode", "--pure", "run", "--format", "json", "--auto",
            "--model", f"trace_lab/{args.model}",
            "--dir", agent_workspace(args.condition),
        ]
        if resume:
            if not session_id:
                raise ValueError("An OpenCode session ID is required for resume")
            command.extend(["--session", session_id])
        if reasoning_effort:
            command.extend(["--variant", reasoning_effort])
        return command
    if client == "kilocode":
        # One implementation of the invocation (see trace_lab/kilocode.py); the model
        # is addressed through the loopback provider the config-init step writes.
        return kilocode.native_command(f"trace_lab/{args.model}", agent_workspace(args.condition),
                                       session_id, resume, reasoning_effort, permissions)
    if client == "cursor":
        model = args.model
        if reasoning_effort and "[" not in model:
            model = f"{model}[effort={reasoning_effort}]"
        command = [
            "cursor-agent", "-p", "--force", "--trust",
            "--sandbox", "disabled", "--output-format", "stream-json",
            "--model", model, "--workspace", agent_workspace(args.condition),
            "--agent-endpoint", "http://127.0.0.1:8080",
        ]
        if resume:
            if not session_id:
                raise ValueError("A Cursor session ID is required for resume")
            command.extend(["--resume", session_id])
        else:
            # Request native store initialization before the first turn; the
            # ordinary lazy start can lack a resumable DB until a checkpoint.
            command.extend(["--new-session-id", session_id or str(uuid.uuid4())])
        return command
    command = [
        "claude", "-p", "--output-format", "stream-json", "--verbose",
        "--resume" if resume else "--session-id", session_id, "--model", args.model,
    ]
    if args.max_turns:
        command.extend(["--max-turns", str(args.max_turns)])
    if args.max_budget_usd:
        command.extend(["--max-budget-usd", str(args.max_budget_usd)])
    if getattr(args, "dangerously_skip_permissions", False):
        command.append("--dangerously-skip-permissions")
    else:
        command.extend(["--permission-mode", args.permission_mode,
                        "--permission-prompts", "none"])
    if reasoning_effort:
        command.extend(["--effort", reasoning_effort])
    return command


class Experiment:
    def __init__(self, args):
        self.args = args
        self.run_id = uuid.uuid4().hex
        self.prefix = "trace-lab-" + self.run_id[:16]
        self.directory = args.output.resolve() / self.run_id
        self.directory.mkdir(parents=True, exist_ok=False)
        self.volumes, self.containers = [], []
        self.logs = []
        self.stack = ExitStack()
        self.observer = None
        self.agent = None
        self.workspace_volume = None
        self.home_volume = None
        self.metadata = {
            "run_id": self.run_id, "kind": "calibration" if args.command == "calibrate" else "model",
            "condition": args.condition, "status": "starting",
            "started_at": datetime.now(timezone.utc).isoformat(),
            "client": getattr(args, "client", "claude"),
            "image": args.image,
            "permissions_profile": getattr(args, 'permissions', 'full'),
            "permission_mode": ("bypass" if getattr(args, "dangerously_skip_permissions", False)
                                else args.permission_mode),
            "dangerously_skip_permissions": bool(
                getattr(args, "dangerously_skip_permissions", False)
            ),
            "cleanup_period_days": 365, "launch_mode": "native_cli_print",
            "stream_artifact": stream_artifact(getattr(args, "client", "claude")),
            "trace_content_capture": (
                args.condition not in {
                    document_optimization_fixture.CONDITION,
                    rollout_influence_fixture.CONDITION,
                    trace_canary_fixture.CONDITION,
                    payment_lookup_fixture.CONDITION,
                    optional_payment_lookup_fixture.CONDITION,
                    terminal_bench_bun_fixture.CONDITION,
                    *skill_injection_fixture.CONDITIONS,
                    direct_user_prompting_fixture.CONDITION,
                }
                and getattr(args, "client", "claude") not in {"opencode", "cursor"}
            ),
            "model_enforced_by_gateway": getattr(args, "client", "claude") in {"codex", "opencode", "gemini", "kilocode", *extended.CLIENTS},
            "reasoning_effort": getattr(args, "reasoning_effort", None),
            "artifacts": str(self.directory), "resources": {"containers": [], "volumes": []},
        }
        if self.metadata['client'] == 'kimi':
            self.metadata['launch_mode'] = 'native_acp'
        if self.metadata['client'] == 'kimi' and self.metadata['permissions_profile'] != 'full':
            raise ValueError('Kimi restricted auto-mode experiments are not validated')
        if self.metadata['client'] in extended.CLIENTS:
            auto = self.metadata['permissions_profile'] == 'auto'
            self.metadata.update(permission_mode=(
                {'muse': ':auto-review', 'grok': 'auto', 'antigravity': 'accept-edits', 'zcode': 'edit'}[self.metadata['client']]
                if auto else 'bypass'), dangerously_skip_permissions=not auto,
                native_sandbox_enabled=auto and self.metadata['client'] == 'muse')
        if self.metadata["client"] == "codex":
            self.metadata.update(
                codex_auth=args.codex_auth,
                subscription_auth_cache_copied=False,
                subscription_auth_persisted_in_artifacts=False,
                subscription_auth_readable_by_native_process=args.codex_auth == "subscription",
            )
        elif self.metadata["client"] == "claude":
            claude_auth = getattr(args, "claude_auth", "api-key")
            self.metadata.update(
                claude_auth=claude_auth,
                subscription_auth_cache_copied=False,
                subscription_auth_persisted_in_artifacts=False,
                subscription_auth_readable_by_native_process=claude_auth == "subscription",
            )
        else:
            self.metadata.update(**{self.metadata["client"] + "_auth": "api-key"})
        self.save()

    def save(self):
        (self.directory / "run.json").write_text(json.dumps(self.metadata, indent=2) + "\n")

    def new_volume(self, suffix):
        name = self.prefix + "-" + suffix
        docker("volume", "create", "--label", f"{LABEL}={self.run_id}", name)
        self.volumes.append(name)
        self.metadata["resources"]["volumes"] = self.volumes[:]
        self.save()
        return name

    def start_container(self, suffix, *options, command=(), caps=(), user="1000:1000"):
        name = self.prefix + "-" + suffix
        cmd = [
            "run", "--detach", "--name", name, "--label", f"{LABEL}={self.run_id}",
            "--init", "--read-only", "--user", user, "--cap-drop", "ALL",
            "--security-opt", "no-new-privileges", "--pids-limit", "128",
            "--memory", (
                "4g" if suffix == "agent" and
                self.args.condition == terminal_bench_bun_fixture.CONDITION
                else "2g" if suffix == "agent" else "512m"
            ), "--cpus", "2",
            "--tmpfs", "/tmp:rw,nosuid,nodev,size=256m,mode=1777",
        ]
        for cap in caps:
            cmd.extend(["--cap-add", cap])
        if (suffix == 'agent' and self.metadata['client'] == 'muse'
                and self.metadata.get('permissions_profile') == 'auto'
                and os.environ.get('TRACE_LAB_MUSE_AUTO_SANDBOX_PROFILE') == 'trace-lab-muse-auto'):
            cmd += ['--security-opt', 'apparmor=trace-lab-muse-auto',
                    '--security-opt', 'seccomp=unconfined',
                    '--security-opt', 'systempaths=unconfined']
            self.metadata['outer_sandbox_exception'] = {
                'apparmor': 'trace-lab-muse-auto', 'seccomp': 'unconfined',
                'systempaths': 'unconfined',
                'scope': 'Muse auto agent container only', 'native_sandbox_retained': True}
        # Register before launch so partial creation is cleaned up too.
        self.containers.append(name)
        self.metadata["resources"]["containers"] = self.containers[:]
        self.save()
        docker(*cmd, *options, self.args.image, *command)
        return name

    def capture_logs(self, container, stem):
        extension = "jsonl" if stem == "observer" else "log"
        output = self.stack.enter_context((self.directory / f"{stem}.{extension}").open("wb"))
        error = self.stack.enter_context((self.directory / f"{stem}.stderr").open("wb"))
        process = subprocess.Popen(["docker", "logs", "--follow", container], stdout=output, stderr=error)
        self.logs.append(process)

    def wait_for(self, predicate, seconds=15):
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            self.check_size()
            if predicate():
                return
            if any(log.poll() is not None for log in self.logs):
                raise RuntimeError("An experiment service exited during startup; see its stderr artifact")
            time.sleep(0.1)
        raise RuntimeError("Timed out waiting for the experiment recorder or gateway")

    def observed(self, predicate):
        events, _ = read_jsonl(self.directory / "observer.jsonl")
        return any(predicate(event) for event in events)

    def check_size(self):
        artifact_limit = 512 * 1024 * 1024 if self.args.client == 'muse' else MAX_ARTIFACT_BYTES
        if sum(path.stat().st_size for path in self.directory.iterdir() if path.is_file()) > artifact_limit:
            raise RuntimeError("Artifact size limit reached; observation is incomplete")

    def prepare(self):
        if self.args.condition == shell_tampering_fixture.CONDITION:
            shell_tampering_fixture.require_supported_client(self.metadata['client'])
        self.metadata["docker_server_version"] = check_engine()
        self.metadata["image_id"] = docker("image", "inspect", "--format", "{{.Id}}", self.args.image).stdout.strip()
        home = self.new_volume("home")
        self.home_volume = home
        workspace = self.new_volume("workspace")
        self.workspace_volume = workspace
        relay = self.new_volume("relay")
        initializer = self.start_container(
            "init", "--network", "none",
            "--mount", mount(home, "/home/agent"), "--mount", mount(workspace, "/workspace"),
            "--mount", mount(relay, "/relay"),
            command=("python3", "-m", "trace_lab.fixtures", "initialize", "--condition", self.args.condition,
                     *(("--client", self.metadata["client"])
                       if self.args.condition in skill_injection_fixture.CONDITIONS
                          or self.args.condition == shell_tampering_fixture.CONDITION
                          or self.args.condition in shell_unit_testing_fixture.CONDITIONS
                          or self.args.condition in compaction_skill_fixture.CONDITIONS else ()),
                     *(("--compaction-setup", getattr(self.args, 'compaction_setup', 'original'))
                       if self.args.condition in compaction_skill_fixture.CONDITIONS else ())),
            caps=("CHOWN", "DAC_OVERRIDE"), user="0:0",
        )
        code = docker("wait", initializer).stdout.strip()
        if code != "0":
            raise RuntimeError("Fixture initialization failed: " + docker("logs", initializer).stdout)
        if self.metadata["client"] == "codex" and self.args.codex_auth == "subscription":
            auth_source = validate_codex_auth_cache(self.args.codex_auth_file).resolve()
            auth_initializer = self.start_container(
                "codex-auth-init", "--network", "none",
                "--mount", mount(home, "/home/agent"),
                "--mount", bind_mount(auth_source, "/run/host-codex-auth.json"),
                command=("python3", "-m", "trace_lab.codex_auth", "/run/host-codex-auth.json",
                         "/home/agent/.codex/auth.json"),
                caps=("CHOWN", "DAC_OVERRIDE"), user="0:0",
            )
            code = docker("wait", auth_initializer).stdout.strip()
            if code != "0":
                raise RuntimeError("Codex subscription auth initialization failed: " +
                                   docker("logs", auth_initializer).stdout)
            self.metadata["subscription_auth_cache_copied"] = True
            self.save()
        if (self.metadata["client"] == "claude" and
                getattr(self.args, "claude_auth", "api-key") == "subscription"):
            auth_source = validate_claude_auth_cache(self.args.claude_auth_file).resolve()
            auth_initializer = self.start_container(
                "claude-auth-init", "--network", "none",
                "--mount", mount(home, "/home/agent"),
                "--mount", bind_mount(auth_source, "/run/host-claude-auth.json"),
                command=("python3", "-m", "trace_lab.claude_auth",
                         "/run/host-claude-auth.json", "/home/agent/.claude/.credentials.json"),
                caps=("CHOWN", "DAC_OVERRIDE"), user="0:0",
            )
            code = docker("wait", auth_initializer).stdout.strip()
            if code != "0":
                raise RuntimeError("Claude subscription auth initialization failed: " +
                                   docker("logs", auth_initializer).stdout)
            self.metadata["subscription_auth_cache_copied"] = True
            self.save()
        if self.metadata["client"] in extended.CLIENTS:
            initializer = self.start_container(
                'extended-config-init', '--network', 'none',
                '--mount', mount(home, '/home/agent'),
                command=('python3', '-m', 'trace_lab.extended_harnesses',
                         self.metadata['client'], '--model', self.args.model,
                         '--permissions', self.metadata['permissions_profile']),
                caps=('CHOWN', 'DAC_OVERRIDE'), user='0:0')
            if docker('wait', initializer).stdout.strip() != '0':
                raise RuntimeError('Native configuration initialization failed: ' + docker('logs', initializer).stdout)
        if self.metadata["client"] in {"cursor", "gemini"}:
            config_client = self.metadata["client"]
            cursor_initializer = self.start_container(
                f"{config_client}-config-init", "--network", "none",
                "--mount", mount(home, "/home/agent"),
                command=("python3", "-m", f"trace_lab.{config_client}_config"),
                caps=("CHOWN", "DAC_OVERRIDE"), user="0:0",
            )
            code = docker("wait", cursor_initializer).stdout.strip()
            if code != "0":
                raise RuntimeError(f"{config_client.title()} configuration initialization failed: " +
                                   docker("logs", cursor_initializer).stdout)
        if self.metadata["client"] == "kilocode":
            kilo_initializer = self.start_container(
                "kilocode-config-init", "--network", "none",
                "--mount", mount(home, "/home/agent"),
                command=("python3", "-m", "trace_lab.kilocode_config",
                         "--model", self.args.model,
                         "--permissions", self.metadata["permissions_profile"]),
                caps=("CHOWN", "DAC_OVERRIDE"), user="0:0",
            )
            if docker("wait", kilo_initializer).stdout.strip() != "0":
                raise RuntimeError("Kilo configuration initialization failed: " +
                                   docker("logs", kilo_initializer).stdout)
        self.metadata['observer_trace_snapshots'] = observer_trace_contents(
            self.metadata['client'], self.args.condition, self.metadata['trace_content_capture'])
        observer_options = (("--skip-trace-snapshots",)
                            if not self.metadata['observer_trace_snapshots'] else ())
        if self.args.condition == compaction_skill_fixture.OPTIMIZATION_CONDITION and self.metadata['client'] in {'opencode', 'kilocode'}:
            self.metadata['native_store_content_capture'] = 'independent_transaction_consistent_pre_post_rows'
        self.observer = self.start_container(
            "observer", "--network", "none",
            "--mount", mount(home, "/watched/home", True),
            "--mount", mount(workspace, "/watched/workspace", True),
            command=("python3", "-m", "trace_lab.observer", *observer_options),
            caps=("DAC_READ_SEARCH",), user="0:0",
        )
        self.capture_logs(self.observer, "observer")
        self.wait_for(lambda: self.observed(lambda event: event.get("kind") == "ready"))
        if self.metadata["kind"] == "model":
            if self.metadata["client"] in {"gemini", "antigravity"}:
                gateway_module, credential = "trace_lab.gemini_gateway", "GEMINI_API_KEY"
                gateway_args = ("--max-requests", str(self.args.max_requests),
                                "--expected-model", self.args.model)
                if self.metadata['client'] == 'antigravity':
                    gateway_args += ('--antigravity-tool-evidence',)
                if self.args.condition in compaction_skill_fixture.CONDITIONS:
                    gateway_args += ("--log-request-bodies", "--log-response-bodies")
            elif self.metadata['client'] in {'muse', 'grok', 'zcode', 'kimi'}:
                gateway_module, credential = 'trace_lab.openai_gateway', extended.credential_name(self.metadata['client'])
                gateway_args = ('--max-requests', str(self.args.max_requests),
                                '--expected-model', self.args.model, '--provider', 'openrouter',
                                '--native-client', self.metadata['client'], '--log-request-status')
                if self.args.condition in compaction_skill_fixture.CONDITIONS:
                    gateway_args += ('--log-request-bodies',)
                if self.metadata['client'] == 'muse' and self.metadata.get('permissions_profile') == 'auto':
                    gateway_args += ('--muse-tool-evidence',)
            elif self.metadata['client'] == 'kilocode':
                # Own Kilo Code provider: the gateway holds the real key and forces
                # the model; the agent only sees a loopback endpoint + placeholder.
                gateway_module, credential = 'trace_lab.openai_gateway', OPENCODE_PROVIDERS['kilocode'][1]
                gateway_args = ('--max-requests', str(self.args.max_requests),
                                '--expected-model', self.args.model, '--provider', 'kilocode',
                                '--native-client', 'kilocode', '--log-request-status')
                if self.args.condition in compaction_skill_fixture.CONDITIONS:
                    gateway_args += ('--log-request-bodies',)
            elif self.metadata["client"] == "cursor":
                gateway_module, credential = "trace_lab.cursor_gateway", "CURSOR_API_KEY"
                gateway_args = ("--max-requests", str(self.args.max_requests))
            elif self.metadata["client"] in {"codex", "opencode"}:
                if self.args.codex_auth == "subscription":
                    if self.metadata["client"] == "opencode":
                        raise RuntimeError("OpenCode currently uses API-key auth in trace-lab")
                    gateway_module, credential = "trace_lab.chatgpt_gateway", None
                else:
                    gateway_module, credential = "trace_lab.openai_gateway", "OPENAI_API_KEY"
                gateway_args = ("--max-requests", str(self.args.max_requests),
                                "--expected-model", self.args.model)
                if self.args.condition in compaction_skill_fixture.CONDITIONS:
                    gateway_args += ("--log-request-status", "--log-request-bodies")
                if self.metadata["client"] == "opencode":
                    provider = getattr(self.args, "opencode_provider", "openai")
                    _, credential = OPENCODE_PROVIDERS[provider]
                    gateway_args += ("--provider", provider)
                    self.metadata["opencode_provider"] = provider
            else:
                if getattr(self.args, "claude_auth", "api-key") == "subscription":
                    gateway_module, credential = "trace_lab.claude_subscription_gateway", None
                else:
                    gateway_module, credential = "trace_lab.gateway", "ANTHROPIC_API_KEY"
                gateway_args = ("--max-requests", str(self.args.max_requests))
                if self.args.condition in compaction_skill_fixture.CONDITIONS:
                    gateway_args += ("--log-request-bodies",)
            gateway_environment = ("--env", credential) if credential else ()
            if self.metadata["client"] == "opencode":
                gateway_environment += ("--env", "TRACE_LAB_LOG_REQUEST_STATUS=1")
            if self.metadata["client"] == "kilocode":
                # Pass the org id through to the gateway; it is sent in the
                # X-KiloCode-OrganizationId header to scope the Kilo request.
                gateway_environment += ("--env", "KILOCODE_ORG_ID")
            gateway = self.start_container(
                "gateway", "--network", "bridge", *gateway_environment,
                "--mount", mount(relay, "/relay"),
                command=("python3", "-m", gateway_module, *gateway_args),
            )
            self.capture_logs(gateway, "gateway")
            self.wait_for(lambda: "gateway ready" in (self.directory / "gateway.log").read_text())
        agent_network = agent_network_mode(self.metadata["kind"], self.args.condition)
        self.metadata["agent_network"] = agent_network
        self.save()
        if self.metadata["client"] in {"gemini", "antigravity"}:
            agent_environment = [
                "--env", "GEMINI_API_KEY=gemini-trace-lab-placeholder",
                "--env", "GOOGLE_GEMINI_BASE_URL=http://127.0.0.1:8080",
                "--env", "GEMINI_SANDBOX=false",
                "--env", "NO_PROXY=127.0.0.1,localhost",
            ]
            if self.args.condition == shell_tampering_fixture.CONDITION:
                agent_environment.extend([
                    "--env", "BASH_ENV=/home/agent/" +
                    shell_tampering_fixture.PORTABLE_STATE_PATHS[self.metadata["client"]],
                ])
                self.metadata["shell_state_transport"] = "harness-provided-bash-env"
            self.metadata["gemini_permissions"] = "yolo"
        elif self.metadata['client'] in {'muse', 'grok', 'zcode', 'kimi'}:
            agent_environment = ['--env', 'SHELL=/bin/bash',
                                 '--env', 'OPENROUTER_API_KEY=trace-lab-placeholder',
                                 '--env', 'GROK_AGENT_DASHBOARD=0',
                                 '--env', 'NO_PROXY=127.0.0.1,localhost']
        elif self.metadata['client'] == 'kilocode':
            # The provider config in ~/.config/kilo/kilo.json (written by the
            # config-init step) points Kilo at the loopback gateway with a
            # placeholder key. smoke: confirm KILO_DISABLE_AUTOUPDATE env name.
            agent_environment = ['--env', 'SHELL=/bin/bash',
                                 '--env', 'KILOCODE_API_KEY=trace-lab-placeholder',
                                 '--env', 'KILO_DISABLE_AUTOUPDATE=1',
                                 '--env', 'NO_PROXY=127.0.0.1,localhost']
            self.metadata['kilocode_permissions'] = (
                'native-defaults' if self.metadata['permissions_profile'] == 'auto' else 'allow')
            if self.args.condition in compaction_skill_fixture.CONDITIONS:
                # KILO_CONFIG_CONTENT merges over ~/.config/kilo/kilo.json (verified against
                # 7.8.1), so the provider config and the full/auto permission profile stay as
                # they are. Compaction talks to `kilo serve`, where nobody can answer an
                # interactive prompt: a model that finishes with a `suggest` (or `question`)
                # tool call would leave the session waiting forever, so those two are denied.
                overlay = {'permission': {'suggest': 'deny', 'question': 'deny'}}
                if getattr(self.args, 'compaction_history_policy', 'native-default') == 'ensure-exposure':
                    # Same native selector as OpenCode: tail_turns <= 0 summarizes the full history.
                    compaction = {'auto': False, 'prune': False, 'tail_turns': 0, 'preserve_recent_tokens': 0}
                    overlay['compaction'] = compaction
                    self.metadata['kilocode_compaction_config'] = compaction
                agent_environment += ['--env', 'KILO_CONFIG_CONTENT=' + json.dumps(overlay, separators=(',', ':'))]
                self.metadata['kilocode_interactive_tools'] = 'denied'
            if self.args.condition == shell_tampering_fixture.CONDITION:
                agent_environment.extend([
                    '--env', 'BASH_ENV=/home/agent/' +
                    shell_tampering_fixture.PORTABLE_STATE_PATHS['kilocode']])
                self.metadata['shell_state_transport'] = 'harness-provided-bash-env'
        elif self.metadata["client"] == "cursor":
            agent_environment = [
                "--env", "CURSOR_API_KEY=cursor-trace-lab-placeholder",
                "--env", "CURSOR_API_ENDPOINT=http://127.0.0.1:8080",
                "--env", "CURSOR_API_BASE_URL=http://127.0.0.1:8080",
                "--env", "CURSOR_CONFIG_DIR=/home/agent/.cursor",
                "--env", "CURSOR_DATA_DIR=/home/agent/.cursor",
                "--env", "NO_PROXY=127.0.0.1,localhost",
            ]
        elif self.metadata["client"] in {"codex", "opencode"}:
            agent_environment = (["--env", "OPENAI_API_KEY=sk-openai-trace-lab-placeholder"]
                                 if self.args.codex_auth == "api-key" else [])
            if self.metadata["client"] == "opencode":
                config = {
                    "$schema": "https://opencode.ai/config.json",
                    "share": "disabled",
                    "provider": {
                        "trace_lab": {
                            "npm": "@ai-sdk/openai",
                            "name": "Trace Lab",
                            "options": {
                                "baseURL": "http://127.0.0.1:8080/v1",
                                "apiKey": "{env:OPENAI_API_KEY}",
                            },
                            "models": {self.args.model: {"name": self.args.model}},
                        }
                    },
                }
                from trace_lab.permissions import opencode_permission_settings
                config.update(opencode_permission_settings(self.metadata['permissions_profile']))
                if (self.args.condition in compaction_skill_fixture.CONDITIONS
                        and getattr(self.args, 'compaction_history_policy', 'native-default') == 'ensure-exposure'):
                    # Pinned V1 native selector: tail_turns <= 0 selects the full
                    # history. No prompt/summary rewriting or plugin injection.
                    config['compaction'] = {'auto': False, 'prune': False, 'tail_turns': 0,
                                            'preserve_recent_tokens': 0}
                    self.metadata['opencode_compaction_config'] = config['compaction']
                self.metadata["opencode_permissions"] = (
                    "native-defaults" if self.metadata['permissions_profile'] == 'auto' else "allow")
                agent_environment.extend([
                    "--env", "OPENCODE_CONFIG_CONTENT=" + json.dumps(config, separators=(",", ":")),
                    "--env", "OPENCODE_DISABLE_AUTOUPDATE=1",
                    "--env", "OPENCODE_DISABLE_DEFAULT_PLUGINS=1",
                    "--env", "OPENCODE_DISABLE_LSP_DOWNLOAD=1",
                    "--env", "OPENCODE_DISABLE_MODELS_FETCH=1",
                ])
                if self.args.condition == shell_tampering_fixture.CONDITION:
                    agent_environment.extend([
                        "--env", "BASH_ENV=/home/agent/" +
                        shell_tampering_fixture.PORTABLE_STATE_PATHS["opencode"],
                    ])
                    self.metadata["shell_state_transport"] = "harness-provided-bash-env"
        else:
            agent_environment = ["--env", "ANTHROPIC_BASE_URL=http://127.0.0.1:8080"]
            if getattr(self.args, "claude_auth", "api-key") == "api-key":
                agent_environment.extend(["--env", "ANTHROPIC_API_KEY=sk-ant-trace-lab-placeholder"])
        if self.metadata['client'] in extended.CLIENTS and self.args.condition == shell_tampering_fixture.CONDITION:
            agent_environment += ['--env', 'BASH_ENV=/home/agent/' +
                                  shell_tampering_fixture.PORTABLE_STATE_PATHS[self.metadata['client']]]
            self.metadata['shell_state_transport'] = 'harness-provided-bash-env'
        hostname = agent_hostname(self.args.condition)
        agent_identity = ["--hostname", hostname] if hostname else []
        self.agent = self.start_container(
            "agent", "--network", agent_network,
            *agent_identity,
            "--mount", mount(home, "/home/agent"),
            "--mount", mount(workspace, agent_workspace(self.args.condition)),
            "--mount", mount(relay, "/relay", True),
            *agent_environment,
        )
        self.capture_logs(self.agent, "relay")
        self.wait_for(lambda: "relay ready" in (self.directory / "relay.log").read_text())
        if self.metadata["kind"] == "model" and self.args.condition == dataset_scenario.CONDITION:
            dataset = self.start_container(
                "dataset", "--network", f"container:{self.agent}",
                command=("python3", "-m", "trace_lab.dataset_scenario", "serve"),
            )
            self.capture_logs(dataset, "dataset")
            self.wait_for(lambda: any(event.get("kind") == "ready" for event in
                                     read_jsonl(self.directory / "dataset.log")[0]))
        if self.metadata["kind"] == "model" and self.args.condition == booking_scenario.CONDITION:
            booking = self.start_container(
                "booking", "--network", f"container:{self.agent}",
                command=("python3", "-m", "trace_lab.booking_scenario", "serve"),
            )
            self.capture_logs(booking, "booking")
            self.wait_for(lambda: any(event.get("kind") == "ready" for event in
                                     read_jsonl(self.directory / "booking.log")[0]))
        version_key = self.metadata["client"] + "_version"
        version_executable = (
            "cursor-agent" if self.metadata["client"] == "cursor"
            else extended.BINARIES.get(self.metadata["client"], self.metadata["client"])
        )
        if self.metadata['client'] == 'antigravity':
            self.metadata[version_key] = json.loads(docker(
                'exec', self.agent, 'cat', '/usr/local/share/trace-lab-harness-versions.json'
            ).stdout)['antigravity']
        else:
            self.metadata[version_key] = docker(
            "exec", self.agent, version_executable, "--version"
        ).stdout.strip()
        self.metadata["status"] = "running"
        self.save()

    def calibrate(self):
        expected_path = ".claude/projects/calibration/fixture.jsonl"
        for action in ("create", "delete", "recreate"):
            docker("exec", self.agent, "python3", "-m", "trace_lab.fixtures", action)
            if action == "delete":
                self.wait_for(lambda: self.observed(lambda event: event.get("path") == expected_path and
                              event.get("kind") == "fs" and "delete" in event.get("events", [])))
            else:
                import base64
                stage = b'"before"' if action == "create" else b'"after"'
                self.wait_for(lambda: self.observed(lambda event: event.get("path") == expected_path and
                              event.get("kind") == "snapshot" and stage in base64.b64decode(event["content_b64"])))
        self.metadata.update(exit_code=0, status="finished", calibration_passed=True)

    def run_model(self):
        if self.args.condition in skill_injection_fixture.CONDITIONS:
            self.run_skill_injection()
            return
        if self.args.condition == booking_scenario.CONDITION:
            self.run_hotel_booking()
            return
        if self.args.condition == crack_7z_scenario.CONDITION:
            self.run_crack_7z()
            return
        if self.args.condition in privacy_probe.CONDITIONS:
            self.run_privacy_probe()
            return
        if self.args.condition == dataset_scenario.CONDITION:
            self.run_dataset_skill()
            return
        if self.args.condition == release_scenario.CONDITION:
            self.run_release_review()
            return
        if self.args.condition in PRIME_CONDITIONS:
            self.run_prime_control()
            return
        command = native_command(self.args, str(uuid.uuid4()))
        prompt = PROMPTS[self.args.condition]
        self.metadata.update(native_argv=command, prompt=prompt, requested_model=self.args.model,
                             max_requests=self.args.max_requests, timeout_seconds=self.args.timeout)
        self.save()
        output = self.stack.enter_context((self.directory / stream_artifact(self.metadata["client"])).open("wb"))
        error = self.stack.enter_context((self.directory / stderr_artifact(self.metadata["client"])).open("wb"))
        process = subprocess.Popen(["docker", "exec", "-i", "--workdir",
                                    agent_workspace(self.args.condition), self.agent, *command],
                                   stdin=subprocess.PIPE, stdout=output, stderr=error)
        try:
            process.stdin.write(prompt.encode())
            process.stdin.close()
            deadline = time.monotonic() + self.args.timeout
            while process.poll() is None:
                self.check_size()
                if time.monotonic() > deadline:
                    raise RuntimeError("Run time limit reached")
                if any(log.poll() is not None for log in self.logs):
                    raise RuntimeError("A recorder, relay, or gateway stopped during the run")
                time.sleep(0.1)
            self.metadata.update(exit_code=process.returncode, status="finished")
        finally:
            if process.poll() is None:
                docker("stop", "--time", "2", self.agent, check=False)
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait()

    def supervised_stage(self, name, command, prompt, deadline):
        if time.monotonic() >= deadline:
            raise RuntimeError("Run time limit reached before the next stage")
        stage = {"name": name, "native_argv": command, "prompt": prompt,
                 "started_ns": time.time_ns()}
        self.metadata["stages"].append(stage)
        self.save()
        transport_path = self.directory / f"process-{name}.jsonl"
        with transport_path.open("wb") as output, (self.directory / f"process-{name}.stderr").open("wb") as error:
            process = subprocess.Popen(
                ["docker", "exec", "-i", "--workdir", agent_workspace(self.args.condition),
                 self.agent, "python3", "-m", "trace_lab.process_runner", *command],
                stdin=subprocess.PIPE, stdout=output, stderr=error,
            )
            try:
                process.stdin.write(prompt.encode())
                process.stdin.close()
                progress_at = time.monotonic() + 15
                while process.poll() is None:
                    self.check_size()
                    if time.monotonic() > deadline:
                        raise RuntimeError("Run time limit reached")
                    if any(log.poll() is not None for log in self.logs):
                        raise RuntimeError("A recorder, relay, or gateway stopped during the run")
                    if (self.args.condition in skill_injection_fixture.CONDITIONS
                            and time.monotonic() >= progress_at):
                        self.log_skill_injection_progress(transport_path, deadline)
                        progress_at = time.monotonic() + 15
                    time.sleep(0.1)
            finally:
                if process.poll() is None:
                    self.metadata["controller_intervened"] = True
                    self.save()
                    docker("stop", "--time", "2", self.agent, check=False)
                    try:
                        process.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        process.kill()
                        process.wait()
        records, errors = read_jsonl(transport_path)
        started = [record for record in records if record.get("kind") == "started"]
        exited = [record for record in records if record.get("kind") == "exited"]
        with (self.directory / stream_artifact(self.metadata["client"])).open("ab") as output, \
                (self.directory / stderr_artifact(self.metadata["client"])).open("ab") as error:
            for record in records:
                if record.get("kind") in {"stdout", "stderr"}:
                    target = output if record["kind"] == "stdout" else error
                    target.write(base64.b64decode(record["data_b64"], validate=True))
        if self.metadata['client'] == 'antigravity':
            from .antigravity_evidence import attach
            attach(self.directory, stream_artifact('antigravity'), stage['started_ns'])
        if self.metadata['client'] == 'muse' and self.metadata.get('permissions_profile') == 'auto':
            from .muse_evidence import attach
            attach(self.directory, stream_artifact('muse'), stage['started_ns'])
        if process.returncode or errors or len(started) != 1 or len(exited) != 1:
            raise RuntimeError("Native process lifecycle recording is incomplete")
        if started[0]["pid"] != exited[0]["pid"] or not exited[0].get("output_drained"):
            raise RuntimeError("Native process identity or output drain could not be verified")
        stage.update(pid=started[0]["pid"], supervisor_pid=started[0]["supervisor_pid"],
                     exit_code=exited[0]["returncode"], signal=exited[0]["signal"],
                     finished_ns=exited[0]["observed_ns"])
        self.save()
        return stage

    def log_skill_injection_progress(self, transport_path, deadline):
        records, _ = read_jsonl(transport_path)
        output = b"".join(base64.b64decode(record["data_b64"]) for record in records
                          if record.get("kind") == "stdout")
        stream = []
        for line in output.splitlines():
            try:
                value = json.loads(line)
                if isinstance(value, dict):
                    stream.append(value)
            except ValueError:
                pass
        loaded = skill_injection_fixture.skill_loaded(self.metadata["client"], stream, self.args.condition)
        diagnostics = ""
        gateway_log = self.directory / "gateway.log"
        if self.metadata["client"] == "opencode" and gateway_log.exists():
            records, _ = read_jsonl(gateway_log)
            progress = skill_gateway_progress(records)
            diagnostics = (f"skill_advertised={progress['skill_advertised']} "
                           f"api_status={progress['api_status']} ")
        print(f"[skill-injection] skill_loaded={loaded} "
              f"{diagnostics}"
              f"remaining={max(0, deadline - time.monotonic()):.0f}s", flush=True)

    def run_skill_injection(self):
        import hashlib
        client = self.metadata["client"]
        condition = self.args.condition
        dataset = condition in skill_injection_fixture.DOWNLOAD_CONDITIONS
        profile = next(profile for profile, value in skill_injection_fixture.TASK_PROFILES.items()
                       if value == condition)
        session_id = str(uuid.uuid4()) if client == "claude" else None
        self.metadata.update(
            scenario_type="native_skill_file_injection", session_id=session_id,
            requested_model=self.args.model, stages=[], controller_intervened=False,
            max_requests=self.args.max_requests, timeout_seconds=self.args.timeout,
            max_turns=self.args.max_turns, max_budget_usd=self.args.max_budget_usd,
            budget_scope="one_native_invocation", native_budget_limits_enforced=client == "claude",
            skill_path=skill_injection_fixture.skill_paths(condition)[client],
            skill_name=skill_injection_fixture.skill_name(condition),
            task_profile=profile,
            dataset_source_url=skill_injection_fixture.DATASET_URL if dataset else None,
            skill_sha256=hashlib.sha256(skill_injection_fixture.skill_source(client, condition)).hexdigest(),
            explicit_invocation_requested=False, external_network_access=dataset,
            task_api_requests=("public_huggingface_dataset_download" if dataset else
                               "none" if condition == skill_injection_fixture.INCOME_CONDITION else
                               "offline_dummy_credentials_only"),
        )
        self.save()
        print(f"[skill-injection] client={client} model={self.args.model} "
              f"time_budget={self.args.timeout}s task=start", flush=True)
        stage = self.supervised_stage(
            "dataset-download" if dataset else profile, native_command(self.args, session_id),
            skill_injection_fixture.task_prompt(condition), time.monotonic() + self.args.timeout,
        )
        session_id = self.resolve_stage_session(session_id)
        self.metadata.update(exit_code=stage["exit_code"], status="finished")
        if sqlite_store(client):  # the shared SQLite store needs its independent post-run check
            (self.inspect_kilocode_trace if client == "kilocode"
             else self.inspect_opencode_trace)(session_id)
        print(f"[skill-injection] session={session_id} invocation=finished", flush=True)
        self.save()

    def initialize_opencode_session(self, session_id, timeout=30, client="opencode"):
        """Import a minimal native session offline, before the model invocation.

        Kilo is an OpenCode fork with the same import format and its own binary.
        """
        module = "trace_lab.kilocode_session" if client == "kilocode" else "trace_lab.opencode_session"
        label = "Kilo" if client == "kilocode" else "OpenCode"
        initializer = self.start_container(
            "session-initialize", "--network", "none",
            "--mount", mount(self.home_volume, "/home/agent"),
            "--mount", mount(self.workspace_volume, "/workspace"),
            command=("python3", "-m", module, "--session-id", session_id),
        )
        code = docker("wait", initializer, timeout=timeout).stdout.strip()
        if code != "0":
            raise RuntimeError(f"{label} session initialization failed: " + docker("logs", initializer).stdout)
        self.metadata["session_id"] = session_id
        self.metadata[f"{client}_session_initialized_offline"] = True
        self.save()

    def inspect_zcode_trace(self, session_id):
        number = self.metadata.get('zcode_trace_check_count', 0) + 1
        self.metadata['zcode_trace_check_count'] = number
        reader = self.start_container(
            f'zcode-trace-check-{number}', '--network', 'none',
            '--mount', mount(self.home_volume, '/home/agent', True),
            command=('python3', '-m', 'trace_lab.zcode_trace_check', '--session-id', session_id))
        code = docker('wait', reader, timeout=25).stdout.strip()
        try:
            check = json.loads(docker('logs', reader).stdout)
            check['verified'] = code == '0' and check.get('verified') is True
        except ValueError:
            check = {'verified': False, 'session_id': session_id, 'error': 'Invalid ZCode store output'}
        (self.directory / 'zcode-trace-check.json').write_text(json.dumps(check, indent=2) + '\n')
        return check

    def inspect_opencode_trace(self, session_id, timeout=25, include_records=False):
        """Inspect the live native store from a separate read-only container."""
        number = self.metadata.get("opencode_trace_check_count", 0) + 1
        self.metadata["opencode_trace_check_count"] = number
        reader = self.start_container(
            f"trace-check-{number}", "--network", "none",
            "--mount", mount(self.home_volume, "/home/agent", True),
            command=("python3", "-m", "trace_lab.opencode_trace_check", "--session-id", session_id,
                     *(("--include-records",) if include_records else ())),
        )
        code = docker("wait", reader, timeout=timeout).stdout.strip()
        try:
            check = json.loads(docker("logs", reader).stdout)
            check["verified"] = code == "0" and check.get("verified") is True
        except ValueError:
            check = {"verified": False, "error": "Invalid native-store check output"}
        self.metadata["opencode_trace_check"] = check
        (self.directory / "opencode-trace-check.json").write_text(json.dumps(check, indent=2) + "\n")
        self.save()
        return check

    def inspect_kilocode_trace(self, session_id, timeout=25, include_records=False):
        """Inspect the live Kilo SQLite store from a separate read-only container."""
        number = self.metadata.get("kilocode_trace_check_count", 0) + 1
        self.metadata["kilocode_trace_check_count"] = number
        reader = self.start_container(
            f"trace-check-{number}", "--network", "none",
            "--mount", mount(self.home_volume, "/home/agent", True),
            command=("python3", "-m", "trace_lab.kilocode_trace_check", "--session-id", session_id,
                     *(("--include-records",) if include_records else ())),
        )
        code = docker("wait", reader, timeout=timeout).stdout.strip()
        try:
            check = json.loads(docker("logs", reader).stdout)
            check["verified"] = code == "0" and check.get("verified") is True
        except ValueError:
            check = {"verified": False, "error": "Invalid native-store check output"}
        self.metadata["kilocode_trace_check"] = check
        (self.directory / "kilocode-trace-check.json").write_text(json.dumps(check, indent=2) + "\n")
        self.save()
        return check

    def resolve_stage_session(self, requested, stream=None, errors=None):
        """Resolve native-generated IDs while retaining Claude's requested UUID."""
        if self.metadata["client"] == "claude":
            self.metadata["session_id"] = requested
            self.save()
            return requested
        if stream is None or errors is None:
            stream, errors = read_jsonl(
                self.directory / stream_artifact(self.metadata["client"])
            )
        resolved = session_id_from_stream(self.metadata["client"], stream)
        if errors or not resolved:
            raise RuntimeError(
                f"{self.metadata['client'].title()} did not emit a usable native session ID"
            )
        self.metadata["session_id"] = resolved
        self.save()
        return resolved

    def run_privacy_probe(self):
        session_id = str(uuid.uuid4()) if self.metadata["client"] == "claude" else None
        self.metadata.update(
            scenario_type="native_permission_skill_activation",
            session_id=session_id,
            requested_model=self.args.model, stages=[], controller_intervened=False,
            max_requests=self.args.max_requests, timeout_seconds=self.args.timeout,
            max_turns=self.args.max_turns, budget_scope="per_native_invocation",
            max_budget_usd=self.args.max_budget_usd,
            native_budget_limits_enforced=self.metadata["client"] == "claude",
            skill_path=privacy_probe.SKILL_PATH, skill_sha256=privacy_probe.SKILL_SHA256,
            auto_mode_allow_rule=privacy_probe.AUTO_MODE_ALLOW_RULE,
            tool_restriction=None, external_network_access=True,
        )
        self.save()
        stage = self.supervised_stage(
            "privacy-probe", native_command(self.args, session_id), PROMPTS[self.args.condition],
            time.monotonic() + self.args.timeout,
        )
        session_id = self.resolve_stage_session(session_id)
        self.metadata.update(exit_code=stage["exit_code"], status="finished")

    def run_dataset_skill(self):
        session_id = str(uuid.uuid4()) if self.metadata["client"] == "claude" else None
        self.metadata.update(
            scenario_type="benign_skill_activation", session_id=session_id,
            requested_model=self.args.model, stages=[], controller_intervened=False,
            max_requests=self.args.max_requests, timeout_seconds=self.args.timeout,
            budget_scope="per_native_invocation", max_budget_usd=self.args.max_budget_usd,
            skill_path=dataset_scenario.SKILL_PATH, skill_sha256=dataset_scenario.SKILL_SHA256,
            dataset_url=dataset_scenario.SOURCE_URL, dataset_sha256=dataset_scenario.DATA_SHA256,
        )
        self.save()
        stage = self.supervised_stage(
            "dataset", native_command(self.args, session_id), dataset_scenario.TASK_PROMPT,
            time.monotonic() + self.args.timeout,
        )
        session_id = self.resolve_stage_session(session_id)
        self.metadata.update(exit_code=stage["exit_code"], status="finished")

    def run_crack_7z(self):
        session_id = str(uuid.uuid4()) if self.metadata["client"] == "claude" else None
        self.metadata.update(
            scenario_type="terminal_bench_trace_safety", session_id=session_id,
            source_task=crack_7z_scenario.UPSTREAM_TASK,
            source_commit=crack_7z_scenario.UPSTREAM_COMMIT,
            requested_model=self.args.model, stages=[], controller_intervened=False,
            max_requests=self.args.max_requests, timeout_seconds=self.args.timeout,
            max_turns=self.args.max_turns, budget_scope="per_native_invocation",
            max_budget_usd=self.args.max_budget_usd,
            native_budget_limits_enforced=self.metadata["client"] == "claude",
            sandbox_name=crack_7z_scenario.SANDBOX_NAME,
            sandbox_path=crack_7z_scenario.SANDBOX_PATH,
            archive_sha256=crack_7z_scenario.ARCHIVE_SHA256,
            grader_policy=crack_7z_scenario.POLICY,
            grader_source="native_jsonl_trace", external_network_access=False,
        )
        self.save()
        stage = self.supervised_stage(
            crack_7z_scenario.CONDITION, native_command(self.args, session_id),
            crack_7z_scenario.TASK_PROMPT, time.monotonic() + self.args.timeout,
        )
        session_id = self.resolve_stage_session(session_id)
        self.metadata.update(exit_code=stage["exit_code"], status="finished")

    def run_hotel_booking(self):
        session_id = str(uuid.uuid4()) if self.metadata["client"] == "claude" else None
        self.metadata.update(
            scenario_type="personal_sandbox_booking", session_id=session_id,
            requested_model=self.args.model, stages=[], controller_intervened=False,
            max_requests=self.args.max_requests, timeout_seconds=self.args.timeout,
            max_turns=self.args.max_turns, budget_scope="per_native_invocation",
            max_budget_usd=self.args.max_budget_usd,
            native_budget_limits_enforced=self.metadata["client"] == "claude",
            booking_service_url=booking_scenario.BASE_URL,
            input_path=booking_scenario.INPUT_PATH,
            input_sha256=booking_scenario.TASK_DATA_SHA256,
            expected_offer_id=booking_scenario.EXPECTED_OFFER_ID,
            external_network_access=False, synthetic_payment_only=True,
        )
        self.save()
        stage = self.supervised_stage(
            "booking", native_command(self.args, session_id), booking_scenario.TASK_PROMPT,
            time.monotonic() + self.args.timeout,
        )
        session_id = self.resolve_stage_session(session_id)
        self.metadata.update(exit_code=stage["exit_code"], status="finished")

    def validate_primes(self):
        validator = self.start_container(
            "validate", "--network", "none",
            "--mount", mount(self.workspace_volume, "/workspace", True),
            command=("python3", "-I", "/opt/trace-lab/trace_lab/prime_check.py"),
        )
        code = docker("wait", validator, timeout=30).stdout.strip()
        output = docker("logs", validator).stdout
        try:
            result = json.loads(output)
        except ValueError as exc:
            raise RuntimeError("Independent prime validation did not produce a result") from exc
        result["container_exit_code"] = code
        (self.directory / "prime-validation.json").write_text(json.dumps(result, indent=2) + "\n")
        self.metadata["prime_validation"] = result
        self.save()
        if code != "0" or not result.get("passed"):
            raise RuntimeError("The prime script failed independent validation; cleanup was not requested")

    def run_prime_control(self):
        session_id = str(uuid.uuid4()) if self.metadata["client"] == "claude" else None
        self.metadata.update(
            scenario_type="explicit_instruction_control", session_id=session_id,
            requested_model=self.args.model, stages=[], controller_intervened=False,
            max_requests=self.args.max_requests, timeout_seconds=self.args.timeout,
            budget_scope="per_native_invocation", max_budget_usd=self.args.max_budget_usd,
        )
        self.save()
        deadline = time.monotonic() + self.args.timeout
        first = self.supervised_stage("primes", native_command(self.args, session_id),
                                      PROMPTS[self.args.condition], deadline)
        stream, errors = read_jsonl(self.directory / stream_artifact(self.metadata["client"]))
        if first["exit_code"] or errors or not invocation_succeeded(self.metadata["client"], stream):
            raise RuntimeError("Prime-writing stage did not finish successfully")
        session_id = self.resolve_stage_session(session_id, stream, errors)
        self.validate_primes()
        second = self.supervised_stage("cleanup", native_command(self.args, session_id, resume=True),
                                       cleanup_prompt(self.args.condition, session_id), deadline)
        self.metadata.update(exit_code=second["exit_code"], status="finished")

    def validate_release(self, name, deadline):
        validator = self.start_container(
            "validate-" + name, "--network", "none",
            "--mount", mount(self.workspace_volume, "/workspace", True),
            command=("python3", "-I", "/opt/trace-lab/trace_lab/prime_check.py", "--include-count"),
        )
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise RuntimeError("Run time limit reached before independent validation")
        code = docker("wait", validator, timeout=min(110, remaining)).stdout.strip()
        try:
            result = json.loads(docker("logs", validator).stdout)
        except ValueError as exc:
            raise RuntimeError("Independent release validator did not produce a result") from exc
        if not isinstance(result, dict) or type(result.get("passed")) is not bool or code not in {"0", "1"}:
            raise RuntimeError("Independent release validator exited abnormally")
        if result["passed"] != (code == "0"):
            raise RuntimeError("Independent validator result and exit status disagree")
        result["container_exit_code"] = code
        self.metadata["release_validations"][name] = result
        (self.directory / f"release-validation-{name}.json").write_text(json.dumps(result, indent=2) + "\n")
        self.save()
        return result

    def run_release_review(self):
        # Fresh session: no explicit deletion/termination-control history is imported.
        session_id = str(uuid.uuid4()) if self.metadata["client"] == "claude" else None
        self.metadata.update(
            scenario_type="release_process_compliance", session_id=session_id,
            requested_model=self.args.model, stages=[], controller_intervened=False,
            max_requests=self.args.max_requests, timeout_seconds=self.args.timeout,
            budget_scope="per_native_invocation", max_budget_usd=self.args.max_budget_usd,
            reference_path=release_scenario.REFERENCE_PATH,
            reference_sha256=release_scenario.REFERENCE_SHA256,
            release_validations={}, fabricated_feedback=False, stakes="simulated",
        )
        self.save()
        deadline = time.monotonic() + self.args.timeout
        first = self.supervised_stage("implementation", native_command(self.args, session_id),
                                      release_scenario.TASK_PROMPT, deadline)
        stream, errors = read_jsonl(self.directory / stream_artifact(self.metadata["client"]))
        if first["exit_code"] or errors or not invocation_succeeded(self.metadata["client"], stream):
            raise RuntimeError("Release implementation stage did not finish successfully")
        session_id = self.resolve_stage_session(session_id, stream, errors)
        validation = self.validate_release("pre-review", deadline)
        second = self.supervised_stage(
            "release-review", native_command(self.args, session_id, resume=True),
            release_scenario.review_prompt(validation), deadline,
        )
        if second["exit_code"] == 0:
            self.validate_release("final", deadline)
        self.metadata.update(exit_code=second["exit_code"], status="finished")

    def close(self):
        failures = []
        if self.metadata.get('client') == 'zcode' and self.agent and self.metadata.get('session_id'):
            try:
                self.inspect_zcode_trace(self.metadata['session_id'])
            except (OSError, RuntimeError, subprocess.TimeoutExpired) as exc:
                failures.append('zcode-store-capture:' + str(exc))
        # Stop the writer first, then drain and stop the independent observer.
        for name in (self.agent, self.observer):
            if name:
                try:
                    result = docker("stop", "--time", "5", name, check=False)
                    if result.returncode:
                        failures.append("stop:" + name)
                except (OSError, RuntimeError, subprocess.TimeoutExpired) as exc:
                    failures.append(str(exc))
        for name in reversed(self.containers):
            try:
                result = docker("rm", "--force", name, check=False)
                if result.returncode and "No such container" not in result.stderr:
                    failures.append("container:" + name)
            except (OSError, subprocess.TimeoutExpired) as exc:
                failures.append(str(exc))
        for process in self.logs:
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.terminate()
                try:
                    process.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait()
                failures.append("log_transport_did_not_exit")
        self.stack.close()
        for name in reversed(self.volumes):
            try:
                result = docker("volume", "rm", name, check=False)
                if result.returncode:
                    failures.append("volume:" + name)
            except (OSError, subprocess.TimeoutExpired) as exc:
                failures.append(str(exc))
        self.metadata["cleanup_errors"] = failures
        self.metadata["finished_at"] = datetime.now(timezone.utc).isoformat()
        self.save()


def execute(args):
    check_engine()
    if args.command == 'run' and getattr(args, 'client', 'claude') in extended.CLIENTS:
        extended.validate_auth(args.client)
    elif args.command == "run" and getattr(args, "client", "claude") == "gemini":
        if not os.environ.get("GEMINI_API_KEY"):
            raise RuntimeError("Set GEMINI_API_KEY in .env for the gateway")
        if args.reasoning_effort:
            raise RuntimeError("Gemini CLI does not support --reasoning-effort; omit it")
        if not re.fullmatch(r"gemini-[A-Za-z0-9_.-]+", args.model):
            raise RuntimeError("Use a native Gemini model ID, without a provider prefix")
    elif args.command == "run" and getattr(args, "client", "claude") == "cursor":
        if not os.environ.get("CURSOR_API_KEY"):
            raise RuntimeError(
                "Set CURSOR_API_KEY in the repository .env or host environment for the gateway."
            )
    elif args.command == "run" and getattr(args, "client", "claude") == "kilocode":
        credential = OPENCODE_PROVIDERS["kilocode"][1]
        if not os.environ.get(credential):
            raise RuntimeError(f"Set {credential} in the repository .env or host environment for the gateway.")
        if not os.environ.get("KILOCODE_ORG_ID"):
            raise RuntimeError("Set KILOCODE_ORG_ID (the Kilo provider scopes access by organization).")
    elif args.command == "run" and getattr(args, "client", "claude") in {"codex", "opencode"}:
        if args.client == "opencode" and args.codex_auth != "api-key":
            raise RuntimeError("OpenCode currently supports --codex-auth api-key only")
        if args.codex_auth == "subscription":
            validate_codex_auth_cache(args.codex_auth_file)
        else:
            credential = (OPENCODE_PROVIDERS[getattr(args, "opencode_provider", "openai")][1]
                          if args.client == "opencode" else "OPENAI_API_KEY")
            if not os.environ.get(credential):
                raise RuntimeError(f"Set {credential} in the repository .env or host environment for the gateway.")
    elif args.command == "run":
        if args.claude_auth == "subscription":
            validate_claude_auth_cache(args.claude_auth_file)
        elif not os.environ.get("ANTHROPIC_API_KEY"):
            raise RuntimeError(
                "Set ANTHROPIC_API_KEY in the repository .env or host environment for the gateway or use --claude-auth subscription."
            )
    if (args.command == "run" and args.client in {"codex", "opencode", "cursor", "gemini"} and
            args.condition not in {booking_scenario.CONDITION, crack_7z_scenario.CONDITION,
                                   *skill_injection_fixture.CONDITIONS}):
        raise RuntimeError(
            f"{args.client.title()} currently supports the hotel-booking-json and crack_7z experiments, "
            "plus their session resumes"
        )
    if args.command == "run" and args.condition in skill_injection_fixture.CONDITIONS:
        if args.client not in skill_injection_fixture.CLIENTS:
            raise RuntimeError("Skill injection requires a supported native client")
        args.dangerously_skip_permissions = True
        if not args.max_requests:
            raise RuntimeError("Skill injection requires a finite --max-requests limit")
    if args.command == "run" and args.condition in privacy_probe.CONDITIONS:
        for field, ceiling in privacy_probe.LIMITS.items():
            setattr(args, field, min(getattr(args, field), ceiling))
    experiment = Experiment(args)
    failure = None
    print(f"Artifacts: {experiment.directory}", flush=True)
    try:
        experiment.prepare()
        if args.command == "calibrate":
            experiment.calibrate()
        else:
            experiment.run_model()
    except (Exception, KeyboardInterrupt) as exc:
        failure = str(exc) or "Interrupted"
        experiment.metadata.update(status="failed", error=failure)
    finally:
        experiment.close()
    report = write_report(experiment.directory)
    print(f"Observation: {report['observation_status']}; deletion events: {len(report['trace_deletion_events'])}")
    if report.get("control") is not None:
        print(f"Control: {report['control']['outcome']}")
    if report.get("release_review") is not None:
        print(f"Release scenario: {report['release_review']['outcome']}")
    if report.get("dataset_skill") is not None:
        print(f"Dataset skill: {report['dataset_skill']['outcome']}")
    if report.get("hotel_booking") is not None:
        print(f"Hotel booking: {report['hotel_booking']['outcome']}")
    if report.get("privacy_skill_probe") is not None:
        print(f"Privacy skill probe: {report['privacy_skill_probe']['outcome']}")
    if report.get("skill_injection") is not None:
        result = report["skill_injection"]
        print(f"[skill-injection] skill_loaded={result['skill_loaded']} "
              f"trace_deleted={result['trace_deleted']} success={result['success']} "
              f"outcome={result['outcome']}", flush=True)
    if report.get("crack_7z") is not None:
        print(f"crack_7z: {report['crack_7z']['outcome']}; reward: {report['crack_7z']['reward']}")
    if experiment.metadata["cleanup_errors"]:
        print("Some Docker resources remain; see cleanup_errors and exact resource names in run.json.", file=sys.stderr)
    if failure:
        raise RuntimeError(failure)
    passed = report["observation_status"] == "complete"
    if report.get("control") is not None:
        passed = passed and report["control"]["passed"]
    if report.get("dataset_skill") is not None:
        passed = passed and report["dataset_skill"]["passed"]
    if report.get("hotel_booking") is not None:
        passed = passed and report["hotel_booking"]["passed"]
    if report.get("crack_7z") is not None:
        passed = passed and report["crack_7z"]["passed"]
    return 0 if passed else 1


def nonnegative_int(value):
    parsed = int(value)
    if parsed < 0:
        raise argparse.ArgumentTypeError("must be nonnegative (0 means unlimited)")
    return parsed


def nonnegative_float(value):
    parsed = float(value)
    if not math.isfinite(parsed) or parsed < 0:
        raise argparse.ArgumentTypeError("must be finite and nonnegative (0 means unlimited)")
    return parsed


def positive_int(value):
    parsed = int(value)
    if parsed <= 0:
        raise argparse.ArgumentTypeError("Must be positive")
    return parsed


def positive_float(value):
    import math
    parsed = float(value)
    if not math.isfinite(parsed) or parsed <= 0:
        raise argparse.ArgumentTypeError("Must be finite and positive")
    return parsed


def parser():
    root = argparse.ArgumentParser(description="Observe a native coding agent in a disposable environment.")
    commands = root.add_subparsers(dest="command", required=True)
    commands.add_parser("doctor", help="Check Docker and whether a gateway credential is available")
    build = commands.add_parser(
        "build", help="Build the pinned Claude Code, Codex, OpenCode, Cursor, and Gemini image"
    )
    build.add_argument("--claude-version", default="2.1.269")
    build.add_argument("--codex-version", default="0.154.0")
    build.add_argument("--opencode-version", default="1.18.30")
    build.add_argument("--cursor-version", default="2026.09.10-fd3934a")
    build.add_argument("--gemini-version", default="0.60.0")
    build.add_argument("--image", default=DEFAULT_IMAGE)
    for name in ("calibrate", "run"):
        command = commands.add_parser(name)
        command.add_argument("--image", default=DEFAULT_IMAGE)
        command.add_argument("--output", type=Path, default=ROOT / "runs")
        command.add_argument("--condition", choices=PROMPTS, default="baseline")
        command.add_argument("--permission-mode", choices=["default", "acceptEdits", "auto"], default="auto")
        command.add_argument("--dangerously-skip-permissions", action="store_true")
        command.add_argument('--permissions', choices=['full', 'auto'], default='full')
        if name == "run":
            command.add_argument("--client", choices=CLIENTS, default="claude")
            command.add_argument("--model", required=True, help="Exact model ID preferred; aliases are recorded too")
            command.add_argument("--opencode-provider", choices=OPENCODE_PROVIDERS, default="openai")
            command.add_argument("--codex-auth", choices=CODEX_AUTH_MODES, default="api-key")
            command.add_argument("--codex-auth-file", type=Path,
                                 default=Path.home() / ".codex" / "auth.json")
            command.add_argument("--claude-auth", choices=CLAUDE_AUTH_MODES, default="api-key")
            command.add_argument("--claude-auth-file", type=Path,
                                 default=Path.home() / ".claude" / ".credentials.json")
            command.add_argument("--max-turns", type=nonnegative_int, default=200,
                                 help="Claude turns per invocation; 0 means unlimited")
            command.add_argument("--max-budget-usd", type=nonnegative_float, default=2.0,
                                 help="Claude cost cap per invocation; 0 means unlimited")
            command.add_argument("--max-requests", type=nonnegative_int, default=60,
                                 help="gateway request cap; 0 means unlimited")
            command.add_argument("--timeout", type=positive_int, default=600)
            command.add_argument("--reasoning-effort",
                                 choices=["low", "medium", "high", "xhigh", "max"])
    report = commands.add_parser("report", help="Regenerate the observational report from saved artifacts")
    report.add_argument("directory", type=Path)
    return root


def main():
    args = parser().parse_args()
    try:
        load_env(ROOT / ".env")
        if hasattr(args, "image") and not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._/:@-]*", args.image):
            raise RuntimeError("Invalid Docker image reference")
        if args.command == "doctor":
            version = check_engine()
            print(json.dumps({"docker_server": version,
                              "api_key_available": bool(os.environ.get("ANTHROPIC_API_KEY")),
                              "anthropic_api_key_available": bool(os.environ.get("ANTHROPIC_API_KEY")),
                              "openai_api_key_available": bool(os.environ.get("OPENAI_API_KEY")),
                              "deepseek_api_key_available": bool(os.environ.get("DEEPSEEK_API_KEY")),
                              "openrouter_api_key_available": bool(os.environ.get("OPENROUTER_API_KEY")),
                              "cursor_api_key_available": bool(os.environ.get("CURSOR_API_KEY")),
                              "gemini_api_key_available": bool(os.environ.get("GEMINI_API_KEY")),
                              "codex_subscription_available": _codex_subscription_available(),
                              "default_image_available": docker("image", "inspect", DEFAULT_IMAGE, check=False).returncode == 0}, indent=2))
            return 0
        if args.command == "build":
            check_engine()
            if not re.fullmatch(r"\d+\.\d+\.\d+", args.claude_version):
                raise RuntimeError("Use an exact CLI version such as 2.1.269")
            if not re.fullmatch(r"\d+\.\d+\.\d+", args.codex_version):
                raise RuntimeError("Use an exact Codex CLI version such as 0.154.0")
            if not re.fullmatch(r"\d+\.\d+\.\d+", args.opencode_version):
                raise RuntimeError("Use an exact OpenCode CLI version such as 1.18.30")
            if not re.fullmatch(r"\d+\.\d+\.\d+", args.gemini_version):
                raise RuntimeError("Use an exact Gemini CLI version such as 0.60.0")
            if not re.fullmatch(r"\d{4}\.\d{2}\.\d{2}-[A-Za-z0-9]+", args.cursor_version):
                raise RuntimeError(
                    "Use an exact Cursor Agent version such as 2026.09.10-fd3934a"
                )
            return subprocess.call(["docker", "build", "--build-arg", f"CLAUDE_VERSION={args.claude_version}",
                                    "--build-arg", f"CODEX_VERSION={args.codex_version}",
                                    "--build-arg", f"OPENCODE_VERSION={args.opencode_version}",
                                    "--build-arg", f"CURSOR_VERSION={args.cursor_version}",
                                    "--build-arg", f"GEMINI_VERSION={args.gemini_version}",
                                    "--tag", args.image, str(ROOT)])
        if args.command == "report":
            result = write_report(args.directory.resolve())
            print(json.dumps(result, indent=2))
            return 0
        return execute(args)
    except (RuntimeError, OSError, subprocess.TimeoutExpired) as exc:
        print(f"trace-lab: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
