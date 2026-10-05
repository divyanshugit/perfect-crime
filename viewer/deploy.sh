#!/usr/bin/env bash
# Rebuild the trace browser from local runs and publish it to Vercel.
#
#   viewer/deploy.sh                     # all runs under runs/
#   RUNS_DIR=run-x/runs viewer/deploy.sh # another folder of runs
#
# Publishes https://enk-exp-perfect-crime.vercel.app (Enkrypt AI team) as a PUBLIC
# static site: anyone with the link can read every transcript in it. It is marked
# noindex, but that only keeps search engines away. Before uploading, the build is
# scanned for the values in .env and the run aborts if any appear.
#
# Terminal-Bench runs are LEFT OUT by default: their transcripts contain the benchmark's task
# text and its canary string, and publishing those publicly defeats the canary. Opt in with
# INCLUDE_TERMINAL_BENCH=1 only if that is intended.
#
# The upload folder is runs/_deploy (inside runs/, so it is never committed).
set -euo pipefail

cd "$(dirname "$0")/.."
RUNS_DIR="${RUNS_DIR:-runs}"
SCOPE="${VERCEL_SCOPE:-enkrypt-ai}"
PROJECT="${VERCEL_PROJECT:-enk-exp-perfect-crime}"
OUT="$RUNS_DIR/_deploy"

exclude=(--exclude terminal_bench)
[ "${INCLUDE_TERMINAL_BENCH:-0}" = "1" ] && exclude=()
python3 -m viewer --runs-dir "$RUNS_DIR" -o "$OUT" ${exclude[@]+"${exclude[@]}"}

cat > "$OUT/vercel.json" <<'JSON'
{
  "headers": [
    {
      "source": "/(.*)",
      "headers": [
        { "key": "X-Robots-Tag", "value": "noindex, nofollow, noarchive" },
        { "key": "X-Content-Type-Options", "value": "nosniff" },
        { "key": "Referrer-Policy", "value": "no-referrer" }
      ]
    }
  ]
}
JSON
printf 'User-agent: *\nDisallow: /\n' > "$OUT/robots.txt"

# Refuse to publish if anything from .env (keys, org ids) ended up in the build.
python3 - "$OUT" <<'PY'
import pathlib, sys
out = pathlib.Path(sys.argv[1])
env = pathlib.Path(".env")
values = {}
if env.exists():
    for line in env.read_text().splitlines():
        if "=" in line and not line.lstrip().startswith("#"):
            key, value = line.split("=", 1)
            if len(value.strip()) >= 8:
                values[key.strip()] = value.strip()
import os
leaks = {}
canary = os.environ.get("INCLUDE_TERMINAL_BENCH") != "1"
for path in out.rglob("*"):
    if path.is_file() and ".vercel" not in path.parts:
        text = path.read_text(errors="ignore")
        if canary and ("harbor-canary" in text or "canary GUID" in text):
            leaks.setdefault("benchmark canary string", []).append(str(path))
        for key, value in values.items():
            if value in text:
                leaks.setdefault(key, []).append(str(path))
if leaks:
    sys.exit("ABORT: refusing to publish; found in the build: " + ", ".join(sorted(leaks)))
print(f"secret scan: clean ({len(values)} .env values checked)")
PY

cd "$OUT"
[ -f .vercel/project.json ] || vercel link --yes --project "$PROJECT" --scope "$SCOPE"
vercel deploy --prod --yes --scope "$SCOPE"
