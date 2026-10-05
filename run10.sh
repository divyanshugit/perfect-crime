#!/usr/bin/env bash
# Run EVERY trace-safety setting on all four models, in both permission modes, N trials
# each, then tabulate the verdict per (setting, model, mode).
#
# Defaults: all 10 settings x 4 models x (full, auto where the runner has it) x 10 trials.
# That is hundreds of real, paid model runs and many hours, so the script prints the plan
# and an upper-bound time estimate first and will not start a large plan unless you confirm
# (answer the prompt, or set CONFIRM=1). DRY_RUN=1 prints the plan and exits.
#
# Settings (SETTINGS="a b c", default: all):
#   financial personal trace-append   direct-user requests; fabricated reset event
#   tool-call                          shell-result manipulation
#   privacy-skill                      privacy-protection skill injection
#   compaction-skill                   compaction-checkpoint injection
#   anonymization                      document-anonymization reward loop
#   terminal-bench                     Terminal-Bench reward loop
#   peer-logs peer-workspaces          peer influence (logs / workspaces)
#
# Overridable via environment variables:
#   SETTINGS      which settings                      (default: all)
#   MODELS        space-separated model ids           (default: the four below)
#   MODES         space-separated permission modes    (default: "full auto")
#   N             trials per (setting, model, mode)   (default: 10)
#   PARALLEL      "models" = one lane per model, all lanes at once (default: one run at a time)
#   TIME_BUDGET   seconds per run for budgeted settings   (default: 900)
#   MAX_ATTEMPTS  scored attempts for reward loops    (default: 3)
#   PAPER_SCALE   1 = the paper's 3600 s / 250 attempts (many hours per run)
#   SKILL_PROFILE privacy-skill task profile          (default: api-client)
#   CLIENT        agent client                        (default: kilocode)
#   RESULTS       per-run TSV written here            (default: run10-results-<UTC time>.tsv,
#                                                      so a new batch never overwrites an old one)
#   OUTPUT        run-artifact folder, where the runner supports --output (default: runs/)
#   CONFIRM       1 = start without asking (needed when stdin is not a terminal)
#   CONFIRM_ABOVE ask first when the plan has more runs than this (default: 100)
#   DRY_RUN       1 = print the plan and exit
#
# Notes:
#   * Only runners that accept --permissions can run in "auto". For the others (compaction-skill,
#     anonymization, peer-*), "auto" is skipped and reported - never run mislabelled.
#   * PARALLEL=models multiplies the load on the model providers by the number of models. Runs
#     that fail upstream show up as "inconclusive", never as results; re-run those cells.
#   * Model ids have NO "kilo/" prefix and must be allowed for your Kilo org. Kimi K3 is billed to
#     Kilo credits; the others use the org's BYOK keys.
#   * The verdict comes from the run's saved report (python3 -m viewer.outcome), so every setting
#     is summarised the same way: tampered / clean / inconclusive.
#   * Runs write to runs/ (git-ignored). Review them before sharing; they hold raw transcripts.
#
# Usage:
#   DRY_RUN=1 ./run10.sh                      # the plan for everything
#   CONFIRM=1 PARALLEL=models ./run10.sh      # everything, four models at once
#   SETTINGS="personal tool-call" N=3 MODES=full ./run10.sh
#   SETTINGS=anonymization MODELS=google/gemini-3.8-flash N=1 TIME_BUDGET=600 ./run10.sh
set -u

cd "$(dirname "$0")"

ALL_SETTINGS="financial personal trace-append tool-call privacy-skill compaction-skill anonymization terminal-bench peer-logs peer-workspaces"
CLIENT="${CLIENT:-kilocode}"
SETTINGS="${SETTINGS:-all}"
[ "$SETTINGS" = "all" ] && SETTINGS="$ALL_SETTINGS"
MODELS="${MODELS:-openai/gpt-5.6-sol google/gemini-3.8-flash moonshotai/kimi-k3 anthropic/claude-opus-5}"
MODES="${MODES:-full auto}"
N="${N:-10}"
PARALLEL="${PARALLEL:-}"
TIME_BUDGET="${TIME_BUDGET:-900}"
MAX_ATTEMPTS="${MAX_ATTEMPTS:-3}"
if [ "${PAPER_SCALE:-0}" = "1" ]; then TIME_BUDGET=3600; MAX_ATTEMPTS=250; fi
SKILL_PROFILE="${SKILL_PROFILE:-api-client}"
RESULTS="${RESULTS:-run10-results-$(date -u +%Y%m%dT%H%M%SZ).tsv}"
OUTPUT="${OUTPUT:-}"
CONFIRM="${CONFIRM:-0}"
CONFIRM_ABOVE="${CONFIRM_ABOVE:-100}"
DRY_RUN="${DRY_RUN:-0}"

# setting_args SETTING MODE -> the host_lab arguments (without --client/--model);
# returns 1 when that setting cannot run in that mode.
setting_args() {
  case "$1" in
    financial|personal|trace-append) echo "direct-user --task-profile $1 --permissions $2" ;;
    tool-call)        echo "tool-call --permissions $2" ;;
    privacy-skill)    echo "privacy-skill --task-profile $SKILL_PROFILE --time-budget $TIME_BUDGET --permissions $2" ;;
    terminal-bench)   echo "terminal-bench run --task-profile core --mode broad-cued --time-budget $TIME_BUDGET --max-attempts $MAX_ATTEMPTS --permissions $2" ;;
    compaction-skill) [ "$2" = "full" ] || return 1
                      echo "compaction-skill --skill optimization --setup checkpoint-context --time-budget $TIME_BUDGET" ;;
    anonymization)    [ "$2" = "full" ] || return 1
                      echo "anonymization --mode implicit --time-budget $TIME_BUDGET --max-attempts $MAX_ATTEMPTS" ;;
    peer-logs)        [ "$2" = "full" ] || return 1
                      echo "peer-influence --context rollout-logs --time-budget $TIME_BUDGET --max-attempts $MAX_ATTEMPTS" ;;
    peer-workspaces)  [ "$2" = "full" ] || return 1
                      echo "peer-influence --context peer-environments --time-budget $TIME_BUDGET --max-attempts $MAX_ATTEMPTS" ;;
    *) echo "unknown setting: $1 (known: $ALL_SETTINGS)" >&2; return 2 ;;
  esac
}

# A rough upper bound, in seconds, for one run of a setting (used only for the plan's estimate).
estimate_seconds() {
  case "$1" in
    financial|personal) echo 90 ;;
    trace-append|tool-call) echo 150 ;;
    privacy-skill) echo 300 ;;
    compaction-skill) echo 600 ;;
    *) echo $((TIME_BUDGET + 180)) ;;   # reward loops run up to their time budget
  esac
}

read -r -a setting_list <<< "$SETTINGS"
read -r -a model_list <<< "$MODELS"
read -r -a mode_list <<< "$MODES"

# Plan: which (setting, mode) pairs run, and which are skipped.
combos=()
echo "client : ${CLIENT}"
echo "models : ${model_list[*]}"
for setting in "${setting_list[@]}"; do
  for mode in "${mode_list[@]}"; do
    if args=$(setting_args "$setting" "$mode" 2>&1); then
      combos+=("$setting:$mode")
    else
      status=$?
      if [ "$status" = "2" ]; then echo "$args" >&2; exit 2; fi
      echo "skip   : $setting in $mode mode (its runner has no --permissions flag)"
    fi
  done
done
total=$(( ${#combos[@]} * ${#model_list[@]} * N ))
seconds=0
for combo in "${combos[@]}"; do
  seconds=$(( seconds + $(estimate_seconds "${combo%%:*}") * ${#model_list[@]} * N ))
done
lanes=1
if [ "$PARALLEL" = "models" ]; then lanes=${#model_list[@]}; fi
echo "plan   : ${#combos[@]} (setting, mode) pairs x ${#model_list[@]} models x ${N} trials = ${total} runs"
for combo in "${combos[@]}"; do
  echo "         ${combo%%:*} / ${combo##*:} -> host_lab $(setting_args "${combo%%:*}" "${combo##*:}")"
done
echo "budgets: ${TIME_BUDGET}s per budgeted run, ${MAX_ATTEMPTS} reward-loop attempts"
echo "timing : at most ~$(( (seconds / lanes + 3599) / 3600 )) hours ($([ "$lanes" -gt 1 ] && echo "$lanes model lanes at once" || echo "one run at a time")), usually well under"
echo "results: ${RESULTS}"
echo "output : ${OUTPUT:-runs/ (default)}"
echo
if [ "$DRY_RUN" = "1" ]; then
  echo "(dry run: nothing launched)"
  exit 0
fi
if [ "$total" = "0" ]; then echo "nothing to run"; exit 1; fi
if [ "$total" -gt "$CONFIRM_ABOVE" ] && [ "$CONFIRM" != "1" ]; then
  if [ -t 0 ]; then
    printf 'Launch %s paid runs? [y/N] ' "$total"
    read -r answer
    case "$answer" in y|Y|yes|YES) ;; *) echo "cancelled"; exit 1 ;; esac
  else
    echo "refusing to start ${total} runs without confirmation: set CONFIRM=1 (or lower N / SETTINGS)" >&2
    exit 3
  fi
fi

case " $SETTINGS " in
  *" terminal-bench "*) echo "building the Terminal-Bench images (cached after the first time)..."
                        python3 -m host_lab terminal-bench build >/dev/null 2>&1 || echo "warning: terminal-bench build failed" ;;
esac

printf 'setting\tmodel\tmode\ttrial\tverdict\theadline\tartifacts\n' > "$RESULTS"

summary() {
  echo
  echo "===== summary ($(($(wc -l < "$RESULTS") - 1)) of ${total} runs recorded in ${RESULTS}) ====="
  awk -F'\t' 'NR > 1 { key = $1 " · " $2 " · " $3; seen[key] = 1; count[key, $5]++; kinds[$5] = 1 }
    END { for (key in seen) {
            line = ""
            for (kind in kinds) if (count[key, kind]) line = line sprintf("%d %s, ", count[key, kind], kind)
            sub(/, $/, "", line); printf "%-62s %s\n", key, line } }' "$RESULTS" | sort
}

# merge_lanes: fold the per-lane result files into $RESULTS (parallel mode only).
merge_lanes() {
  for lane_file in "$RESULTS".lane.*; do
    [ -f "$lane_file" ] || continue
    cat "$lane_file" >> "$RESULTS"
    rm -f "$lane_file"
  done
}
trap 'merge_lanes; summary' EXIT

# run_lane LANE_RESULTS LABEL MODEL... : every (setting, mode) x model x trial for the given models.
run_lane() {
  lane_results="$1"; lane_label="$2"; shift 2
  lane_total=$(( ${#combos[@]} * $# * N )); done_runs=0
  for combo in "${combos[@]}"; do
    setting="${combo%%:*}"; mode="${combo##*:}"
    words=$(setting_args "$setting" "$mode")
    for model in "$@"; do
      for i in $(seq 1 "$N"); do
        done_runs=$((done_runs + 1))
        echo "===== ${lane_label}[${done_runs}/${lane_total}] ${setting} · ${model} · ${mode} · trial ${i}/${N} ====="
        extra=()
        [ -n "$OUTPUT" ] && extra=(--output "$OUTPUT")
        # $words is split on purpose: it is a fixed list of flags and values without spaces.
        out=$(python3 -m host_lab $words --client "$CLIENT" --model "$model" ${extra[@]+"${extra[@]}"} 2>&1)
        echo "$out"
        artifacts=$(printf '%s\n' "$out" | grep -oE 'artifacts=[^ ]+' | head -1 | cut -d= -f2)
        if [ -n "$artifacts" ] && [ -d "$artifacts" ]; then
          verdict_line=$(python3 -m viewer.outcome "$artifacts" 2>/dev/null || true)
        else
          verdict_line=""
        fi
        verdict="${verdict_line%%$'\t'*}"; headline="${verdict_line#*$'\t'}"
        [ -n "$verdict_line" ] || { verdict="error"; headline="no run folder"; }
        printf '%s\t%s\t%s\t%s\t%s\t%s\t%s\n' "$setting" "$model" "$mode" "$i" "$verdict" "$headline" "${artifacts:--}" >> "$lane_results"
        echo
      done
    done
  done
}

if [ "$PARALLEL" = "models" ]; then
  lane_index=0
  logs=()
  for model in "${model_list[@]}"; do
    lane_index=$((lane_index + 1))
    log="${RESULTS%.tsv}.lane${lane_index}.log"
    logs+=("$log")
    echo "lane ${lane_index}: ${model}  (full output in ${log})"
    run_lane "${RESULTS}.lane.${lane_index}" "[${model##*/}] " "$model" > "$log" 2>&1 &
  done
  echo "all ${lane_index} lanes started; waiting (Ctrl-C stops them and prints the summary)..."
  trap 'kill $(jobs -p) 2>/dev/null' INT TERM
  wait
else
  run_lane "$RESULTS" "" "${model_list[@]}"
fi
