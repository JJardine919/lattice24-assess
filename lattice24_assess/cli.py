"""
Command line entry point.

    lattice24-assess sacct_export.txt --site "Big Cluster" --out ./report

Reads one file, writes two files, contacts nothing.
"""

from __future__ import annotations

import argparse
import sys

from . import __version__
from .core import Refusal, build_windows, check_sufficient, forward_chain, mark_restart_chains, read_records, shuffle_control
from .report import build_summary, write_outputs

BANNER = f"""lattice24-assess {__version__}
Runs entirely on this machine. No network calls. Nothing is uploaded.
"""


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        prog="lattice24-assess",
        description="Measure how much of your cluster's job energy sits in "
                    "timed-out jobs, and how well they can be identified "
                    "before they start — using only your scheduler history.",
        epilog="Recommended export:\n"
               "  sacct --allocations --parsable2 --starttime=$(date -d '12 months ago' +%F) \\\n"
               "        -o User,End,Timelimit,Elapsed,State,ConsumedEnergyRaw > export.psv",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    ap.add_argument("export", help="sacct --parsable2 output, or CSV with the same columns")
    ap.add_argument("--out", default="./lattice24_report", help="output directory")
    ap.add_argument("--site", dest="site_label", default=None,
                    help="label for the report heading (not transmitted; there is nowhere to transmit it)")
    ap.add_argument("--delimiter", default=None, help="override delimiter detection")
    ap.add_argument("--seed", type=int, default=42)
    ap.add_argument("--restart-gap", type=float, default=2.0, metavar="HOURS",
                    help="a TIMEOUT counts as a likely checkpoint-restart link if the same user "
                         "starts a job with the same time limit within this many hours (default 2)")
    ap.add_argument("--version", action="version", version=__version__)
    args = ap.parse_args(argv)

    print(BANNER)

    try:
        print(f"Reading {args.export} ...", flush=True)
        jobs, stats = read_records(args.export, delimiter=args.delimiter)
        print(f"  {stats['rows_read']:,} rows, {len(jobs):,} usable completed jobs")
        stats.update(mark_restart_chains(jobs, gap_s=args.restart_gap * 3600.0))
        print(f"  {stats['timeouts_restart_likely']:,} of {stats['timeouts_seen']:,} timeouts look like "
              f"checkpoint-restart links (same user, same limit, restarted within {args.restart_gap:g}h)")

        print("Building windows ...", flush=True)
        w = build_windows(jobs)
        stats.update({
            "windows": int(len(w.y)),
            "timeouts": int(w.y.sum()),
            "n_months": len(w.months),
            "first_month": w.months[0],
            "last_month": w.months[-1],
        })
        print(f"  {stats['windows']:,} scoreable windows, "
              f"{stats['timeouts']:,} timeouts, {stats['n_months']} months")

        check_sufficient(w)

        print("Forward-chaining ...", flush=True)
        fc = forward_chain(w, seed=args.seed)
        print(f"  {fc['n_splits']} splits")

        print("Running label-shuffle control ...", flush=True)
        ctl = shuffle_control(w, seed=args.seed)
        if not ctl["passed"]:
            print("  CONTROL FAILED — the report will show no headline figure.")
        else:
            print(f"  control mean AUC {ctl['mean']:.3f} (passed, tol ±{ctl['tolerance']:.3f})")

    except Refusal as e:
        print("\n" + "=" * 68, file=sys.stderr)
        print("No report was produced.", file=sys.stderr)
        print("=" * 68, file=sys.stderr)
        print(str(e), file=sys.stderr)
        print("\nThis tool refuses rather than printing a number it cannot "
              "stand behind.", file=sys.stderr)
        return 2

    summary = build_summary(
        {"version": __version__, "site_label": args.site_label}, stats, fc, ctl
    )
    jp, hp = write_outputs(args.out, summary)

    t80 = fc["pooled"]["thresholds"].get(80) or fc["pooled"]["thresholds"].get("80")
    share = fc["energy"]["timeout_share"]
    print("\n" + "-" * 68)
    if share is not None:
        print(f"Timed-out jobs hold {100*share:.1f}% of reported job energy.")
        ex = fc["energy"]["timeout_share_excl_restart"]
        print(f"Excluding likely checkpoint-restart chains: {100*ex:.1f}%.")
    else:
        print("No energy column in the export — energy share not measured.")
    print(f"At the 80th percentile: {100*t80['recall']:.1f}% of timed-out jobs "
          f"caught, {100*t80['false_flag']:.1f}% of good jobs flagged.")
    print("\nThat is energy AT STAKE, not energy saved. A flag saves nothing")
    print("until someone acts on it.")
    print("-" * 68)
    print(f"\n  report : {hp}")
    print(f"  summary: {jp}")
    print("\nThe summary JSON contains no job records and no user identifiers.")
    print("Send it to us only if you want to; the report is complete without it.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
