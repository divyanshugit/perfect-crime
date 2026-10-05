# Agent trace-tampering experiments

This repository contains the experiment harness and tests for controlled studies of native coding-agent session traces. Paper figures, LaTeX, demo media, selected trial archives, and raw runs are kept outside the code-only `main` branch.

## Run a new experiment

New runs require Python 3.11+, a running Docker Engine, the relevant model credentials, and enough disk space for Docker images and artifacts. Copy `.env.example` to `.env` and fill in only the provider keys you need. The host loads `.env` automatically; `.env` is Git-ignored. Check the setup and build the base image:

```sh
python3 -m trace_lab doctor
python3 -m trace_lab build
python3 -m trace_lab calibrate
```

List the host-side settings, then ask any runner for its exact options:

```sh
python3 -m host_lab list
python3 -m host_lab anonymization --help
```

For example, one Codex anonymization run with the paper's one-hour optimization budget is:

```sh
python3 -m host_lab anonymization \
  --client codex --model gpt-6-sol --codex-auth api-key \
  --mode implicit --time-budget 3600 --max-attempts 250
```

A new run writes its report and independent observer artifacts under the ignored `runs/` directory. Keep these artifacts separate from the code checkout when sharing results.

## Find a setting

| Setting in the paper | Runner | Key option |
|---|---|---|
| Financial / personal privacy request | `direct-user` | `--task-profile financial` or `personal` |
| Fabricated reset addition | `direct-user` | `--task-profile trace-append` |
| Tool-call spoofing | `tool-call` | `--client`, `--model` |
| Privacy-protection skill | `privacy-skill` | `--task-profile api-client` |
| Compaction-checkpoint skill | `compaction-skill` | `--skill optimization --setup checkpoint-context` |
| Anonymization reward loop | `anonymization` | `--mode implicit` |
| Terminal-Bench reward loop | `terminal-bench` | `build` once, then `run --task-profile core` |
| Peer Logs | `peer-influence` | `--context rollout-logs` |
| Peer Workspaces | `peer-influence` | `--context peer-environments` |

Every setting above also accepts `--client kilocode` (the Kilo CLI, an OpenCode fork). Setup, per-setting verification status, and the permission profiles are in [`docs/kilocode.md`](docs/kilocode.md); `run10.sh` runs settings across models and modes.

The runners own their prompts, permissions, grading, and output schemas. The catalog does not change their defaults. Historical paper batches used frozen arguments and selected trials, which are not part of this code-only branch.

## Repository map

- `host_lab/`: host-side orchestration, time budgets, task verification, and score loops.
- `trace_lab/`: native client setup, transport, fixtures, independent observation, and reports.
- `tests/`: fixture and integration checks. Network-listening tests require permission to bind loopback sockets.
- `docs/`: [architecture](docs/architecture.md) and [harness-specific notes](docs/extended-harnesses.md).

Agent homes such as `/home/agent` are fixed paths **inside disposable containers**, not paths on the researcher's computer. New code and shared metadata should use repository-relative paths for host-side files. Raw historical traces may contain paths recorded at collection time; treat them as sensitive evidence and review them before redistribution.
