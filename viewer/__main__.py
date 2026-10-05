"""Build a static trace browser from local run directories.

    python3 -m viewer                          # every run under runs/ -> runs/_viewer/
    python3 -m viewer --runs-dir some/runs     # every run under another folder
    python3 -m viewer runs/<id> ... -o out     # chosen runs, chosen output directory

Open <out>/index.html in a browser. It uses the official trace browser's own
stylesheet and scripts and makes no network requests. The output contains raw
agent transcripts and workspace evidence, so keep it local and review it before
sharing.
"""
import argparse
from pathlib import Path

from .model import load_run
from .site import write_site

ROOT = Path(__file__).resolve().parent.parent


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("runs", nargs="*", type=Path, help="run directories (default: every run under --runs-dir)")
    parser.add_argument("--runs-dir", type=Path, default=ROOT / "runs",
                        help="folder holding run directories (default: runs/)")
    parser.add_argument("-o", "--out", type=Path, help="output folder (default: <runs-dir>/_viewer)")
    parser.add_argument("--exclude", action="append", default=[], metavar="TEXT",
                        help="skip runs whose scenario type contains TEXT (repeatable), e.g. terminal_bench")
    args = parser.parse_args(argv)
    out = args.out or args.runs_dir / "_viewer"
    directories = args.runs or sorted(p for p in args.runs_dir.glob("*")
                                      if p.is_dir() and not p.name.startswith("_"))
    # Name the batch after the folder that holds the runs ("run-x/runs" -> "run-x").
    holder = args.runs_dir.resolve()
    batch = holder.parent.name if holder.name == "runs" else holder.name
    payloads = [p for p in (load_run(d, batch=batch) for d in directories) if p]
    if args.exclude:
        kept = [p for p in payloads if not any(x in p["meta"]["scenario"] for x in args.exclude)]
        print(f"excluded {len(payloads) - len(kept)} run(s) matching {args.exclude}")
        payloads = kept
    if not payloads:
        print("No readable runs found (a run directory needs run.json).")
        return 1
    index = write_site(out, payloads, source_label=str(args.runs_dir))
    print(f"wrote {len(payloads)} run(s) to {out}")
    print(f"open {index}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
