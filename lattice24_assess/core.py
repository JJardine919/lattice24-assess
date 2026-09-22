"""
Core analysis: parse scheduler records, build windows, forward-chain, sweep.

Nothing in this module performs network I/O. Nothing writes outside the
output directory the caller names. That is deliberate and load-bearing:
the person running this is an engineer at a site that will not upload its
job records anywhere, and they are expected to read this file first.
"""

from __future__ import annotations

import csv
import hashlib
import math
import os
import re
import secrets
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from bisect import bisect_left

import numpy as np

WINDOW = 24           # jobs of history used to predict the next job
MIN_HISTORY = WINDOW + 1
PERCENTILES = (50, 75, 80, 90, 95)

# Refusal thresholds. Below these the answer is not meaningful and the tool
# says so rather than printing a confident number.
MIN_MONTHS = 6
MIN_WINDOWS = 50_000
MIN_TIMEOUTS = 200

# Checkpoint-restart heuristic. A TIMEOUT is marked "likely restart chain" when
# the same user starts another job with the SAME time limit within this many
# seconds of the timeout. Sites that checkpoint and resubmit (or use
# --dependency=afterany chains) look exactly like that. It is a proxy: the job
# name, which would make it sharper, is deliberately never parsed.
RESTART_GAP_S = 2 * 3600
RESTART_EARLY_S = 60      # tolerate a resubmission that starts just before the end stamp

# Columns we need. Everything else in the export is ignored and never parsed.
REQUIRED = ("user", "end", "timelimit", "elapsed", "state")
# Fields that may carry sensitive content. Dropped at parse time, never read.
NEVER_PARSE = (
    "jobname", "workdir", "command", "comment", "submitline",
    "account", "wckey", "cluster", "reservation",
)


class Refusal(Exception):
    """Raised when the input cannot support an honest answer."""


# --------------------------------------------------------------------------
# parsing
# --------------------------------------------------------------------------

_DUR = re.compile(
    r"^(?:(?P<d>\d+)-)?(?:(?P<h>\d+):)?(?P<m>\d+):(?P<s>\d+(?:\.\d+)?)$"
)


def parse_duration(text: str) -> float | None:
    """Slurm durations: [DD-[HH:]]MM:SS. Returns seconds, or None."""
    if text is None:
        return None
    t = text.strip()
    if not t or t.upper() in {"UNLIMITED", "INVALID", "PARTITION_LIMIT", "NOT_SET", ""}:
        return None
    m = _DUR.match(t)
    if not m:
        # bare integer minutes, which sacct emits for some TimeLimit configs
        if t.isdigit():
            return float(t) * 60.0
        return None
    d = int(m.group("d") or 0)
    h = int(m.group("h") or 0)
    mi = int(m.group("m"))
    s = float(m.group("s"))
    return d * 86400 + h * 3600 + mi * 60 + s


def parse_timestamp(text: str) -> datetime | None:
    t = (text or "").strip()
    if not t or t in {"Unknown", "None", "N/A"}:
        return None
    for fmt in ("%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M", "%Y-%m-%d"):
        try:
            return datetime.strptime(t, fmt)
        except ValueError:
            continue
    return None


def normalise_state(text: str) -> str:
    """sacct states can carry detail, e.g. 'CANCELLED by 1234'."""
    return (text or "").strip().upper().split()[0] if (text or "").strip() else ""


@dataclass
class Job:
    user: str
    end: datetime
    ratio: float          # elapsed / timelimit
    timed_out: bool
    kwh: float | None     # energy of THIS job, if the site reports it
    limit: float = 0.0    # requested wall clock, seconds
    start: datetime | None = None   # end - elapsed
    restart_likely: bool = False    # set by mark_restart_chains


def _energy_kwh(row: dict) -> float | None:
    """
    Prefer the scheduler's own measurement. ConsumedEnergyRaw is joules.
    Anything we reconstruct is marked by the caller as reconstructed.
    """
    raw = (row.get("consumedenergyraw") or "").strip()
    if raw:
        try:
            j = float(raw)
            if j > 0:
                return j / 3_600_000.0
        except ValueError:
            pass
    ce = (row.get("consumedenergy") or "").strip()
    if ce:
        mult = {"K": 1e3, "M": 1e6, "G": 1e9, "T": 1e12}
        try:
            if ce[-1].upper() in mult:
                return float(ce[:-1]) * mult[ce[-1].upper()] / 3_600_000.0
            return float(ce) / 3_600_000.0
        except ValueError:
            pass
    return None


def _salted_hash(value: str, salt: str) -> str:
    return hashlib.sha256((salt + value).encode("utf-8")).hexdigest()[:12]


def read_records(path: str, delimiter: str | None = None) -> tuple[list[Job], dict]:
    """
    Read an sacct --parsable2 export or a CSV with the same column names.

    User identifiers are hashed immediately with a salt generated fresh for
    this run and never written to disk, so the hashes cannot be linked across
    runs or back to a person.
    """
    salt = secrets.token_hex(16)

    with open(path, "r", newline="", encoding="utf-8", errors="replace") as fh:
        sample = fh.readline()
        fh.seek(0)
        if delimiter is None:
            delimiter = "|" if sample.count("|") > sample.count(",") else ","
        reader = csv.DictReader(fh, delimiter=delimiter)
        if not reader.fieldnames:
            raise Refusal(f"{path} has no header row.")

        fields = {(f or "").strip().lower(): (f or "") for f in reader.fieldnames}
        alias = {
            "user": ("user", "username", "uid"),
            "end": ("end", "endtime", "end_time", "completion"),
            "timelimit": ("timelimit", "timelimitraw", "time_limit", "reqtime"),
            "elapsed": ("elapsed", "elapsedraw", "runtime", "used"),
            "state": ("state", "jobstate", "status"),
        }
        col = {}
        for want, options in alias.items():
            for o in options:
                if o in fields:
                    col[want] = fields[o]
                    break
        missing = [k for k in REQUIRED if k not in col]
        if missing:
            raise Refusal(
                "Missing required column(s): " + ", ".join(missing) + ".\n"
                "Minimum export:\n"
                "  sacct --allocations --parsable2 --starttime=<12mo ago> \\\n"
                "        -o User,End,Timelimit,Elapsed,State,ConsumedEnergyRaw"
            )

        jobs: list[Job] = []
        stats = {
            "rows_read": 0, "rows_dropped_unparseable": 0,
            "rows_dropped_no_timelimit": 0, "rows_dropped_running": 0,
            "energy_rows": 0,
        }
        for row in reader:
            stats["rows_read"] += 1
            low = {(k or "").strip().lower(): v for k, v in row.items()}
            for f in NEVER_PARSE:
                low.pop(f, None)

            state = normalise_state(low.get(col["state"].lower(), ""))
            if state in {"RUNNING", "PENDING", "SUSPENDED", "REQUEUED", ""}:
                stats["rows_dropped_running"] += 1
                continue

            limit = parse_duration(low.get(col["timelimit"].lower(), ""))
            used = parse_duration(low.get(col["elapsed"].lower(), ""))
            end = parse_timestamp(low.get(col["end"].lower(), ""))
            user = (low.get(col["user"].lower(), "") or "").strip()

            if limit is None:
                stats["rows_dropped_no_timelimit"] += 1
                continue
            if used is None or end is None or not user or limit <= 0:
                stats["rows_dropped_unparseable"] += 1
                continue

            kwh = _energy_kwh(low)
            if kwh is not None:
                stats["energy_rows"] += 1

            jobs.append(Job(
                user=_salted_hash(user, salt),
                end=end,
                ratio=used / limit,
                timed_out=(state == "TIMEOUT"),
                kwh=kwh,
                limit=limit,
                start=end - timedelta(seconds=used),
            ))

    jobs.sort(key=lambda j: j.end)
    return jobs, stats


def mark_restart_chains(jobs: list[Job], gap_s: float = RESTART_GAP_S) -> dict:
    """
    Mark TIMEOUT jobs that look like one link of a checkpoint-restart chain:
    the same user starts a job with the same time limit within gap_s seconds
    of this job's end. Those timeouts are often intentional and did useful
    work, so the report shows timeout energy with and without them.

    Returns counts for the summary. Uses only fields already parsed.
    """
    by_user: dict[str, list[Job]] = defaultdict(list)
    for j in jobs:
        if j.start is not None:
            by_user[j.user].append(j)
    marked = 0
    timeouts = 0
    for seq in by_user.values():
        seq.sort(key=lambda j: j.start)
        starts = [j.start.timestamp() for j in seq]
        for j in seq:
            if not j.timed_out:
                continue
            timeouts += 1
            e = j.end.timestamp()
            i = bisect_left(starts, e - RESTART_EARLY_S)
            while i < len(seq) and starts[i] <= e + gap_s:
                k = seq[i]
                if k is not j and abs(k.limit - j.limit) <= 1.0:
                    j.restart_likely = True
                    marked += 1
                    break
                i += 1
    return {"restart_gap_hours": gap_s / 3600.0, "timeouts_seen": timeouts,
            "timeouts_restart_likely": marked}


# --------------------------------------------------------------------------
# windows and features
# --------------------------------------------------------------------------

@dataclass
class Windows:
    F: np.ndarray            # (n, 4) features
    y: np.ndarray            # (n,) 1 = the predicted job timed out
    kwh: np.ndarray          # (n,) energy of the predicted job, 0 where unknown
    kwh_known: np.ndarray    # (n,) bool
    month: np.ndarray        # (n,) 'YYYY-MM' of the predicted job
    restart: np.ndarray = None  # (n,) bool: predicted job is a likely restart-chain timeout
    months: list[str] = field(default_factory=list)


def build_windows(jobs: list[Job]) -> Windows:
    """
    For each user, the previous WINDOW ratios predict the next job.

    Four features, unchanged from the published method: mean, standard
    deviation, range, and mean absolute successive difference. Nothing about
    the predicted job itself enters its own features.
    """
    by_user: dict[str, list[Job]] = defaultdict(list)
    for j in jobs:
        by_user[j.user].append(j)

    F, y, kwh, known, month, restart = [], [], [], [], [], []
    for _user, seq in by_user.items():
        if len(seq) < MIN_HISTORY:
            continue
        ratios = [s.ratio for s in seq]
        for i in range(WINDOW, len(seq)):
            hist = np.asarray(ratios[i - WINDOW:i], dtype=np.float64)
            target = seq[i]
            F.append((
                hist.mean(),
                hist.std(),
                hist.max() - hist.min(),
                np.abs(np.diff(hist)).mean(),
            ))
            y.append(1 if target.timed_out else 0)
            kwh.append(target.kwh if target.kwh is not None else 0.0)
            known.append(target.kwh is not None)
            month.append(target.end.strftime("%Y-%m"))
            restart.append(bool(target.timed_out and target.restart_likely))

    if not F:
        raise Refusal(
            f"No scoreable windows. Every user needs at least {MIN_HISTORY} "
            "completed jobs in the export for any prediction to be possible."
        )

    w = Windows(
        F=np.asarray(F, dtype=np.float64),
        y=np.asarray(y, dtype=np.int8),
        kwh=np.asarray(kwh, dtype=np.float64),
        kwh_known=np.asarray(known, dtype=bool),
        month=np.asarray(month),
        restart=np.asarray(restart, dtype=bool),
    )
    w.months = sorted(set(w.month.tolist()))
    return w


def check_sufficient(w: Windows) -> None:
    """Refuse rather than degrade. Each message says what would fix it."""
    n, t, m = len(w.y), int(w.y.sum()), len(w.months)
    problems = []
    if m < MIN_MONTHS:
        problems.append(
            f"Only {m} calendar month(s) of scoreable jobs; {MIN_MONTHS} is the "
            "minimum. Forward-chaining needs enough months for the per-split "
            "spread to mean anything. Re-export with a longer --starttime."
        )
    if n < MIN_WINDOWS:
        problems.append(
            f"Only {n:,} scoreable windows; {MIN_WINDOWS:,} is the minimum."
        )
    if t < MIN_TIMEOUTS:
        problems.append(
            f"Only {t:,} TIMEOUT events; {MIN_TIMEOUTS:,} is the minimum. "
            "If your site rarely hits the wall clock, that is a real finding "
            "and there is little here for you to recover."
        )
    if problems:
        raise Refusal(
            "This export cannot support an honest answer:\n\n  - "
            + "\n  - ".join(problems)
        )


# --------------------------------------------------------------------------
# model
# --------------------------------------------------------------------------

def _fit_predict(F_tr, y_tr, F_te, seed=42):
    from sklearn.linear_model import LogisticRegression
    from sklearn.preprocessing import StandardScaler

    sc = StandardScaler().fit(F_tr)
    clf = LogisticRegression(max_iter=1000, random_state=seed)
    clf.fit(sc.transform(F_tr), y_tr)
    return clf.predict_proba(sc.transform(F_te))[:, 1]


def _auc(y, s) -> float:
    """Rank-based AUC; ties averaged. No sklearn dependency at call sites."""
    y = np.asarray(y)
    pos, neg = int(y.sum()), int((y == 0).sum())
    if pos == 0 or neg == 0:
        return float("nan")
    order = np.argsort(s, kind="mergesort")
    ranks = np.empty(len(s), dtype=np.float64)
    sorted_s = np.asarray(s)[order]
    i = 0
    while i < len(sorted_s):
        j = i
        while j + 1 < len(sorted_s) and sorted_s[j + 1] == sorted_s[i]:
            j += 1
        ranks[order[i:j + 1]] = 0.5 * (i + j) + 1.0
        i = j + 1
    return (ranks[y == 1].sum() - pos * (pos + 1) / 2.0) / (pos * neg)


def forward_chain(w: Windows, seed: int = 42) -> dict:
    """
    Train on months 1..k, score month k+1. Never the reverse.
    This is the deployment case and the only arm quoted.
    """
    months = w.months
    per_split, pooled_scores, pooled_y, pooled_idx = [], [], [], []

    for k in range(1, len(months)):
        tr = np.isin(w.month, months[:k])
        te = w.month == months[k]
        if tr.sum() < 100 or te.sum() < 50:
            continue
        if w.y[tr].sum() < 5 or w.y[te].sum() < 5:
            continue

        s = _fit_predict(w.F[tr], w.y[tr], w.F[te], seed=seed)
        pooled_scores.append(s)
        pooled_y.append(w.y[te])
        pooled_idx.append(np.flatnonzero(te))

        split = {"month": months[k], "n": int(te.sum()),
                 "timeouts": int(w.y[te].sum()), "auc": _auc(w.y[te], s),
                 "thresholds": {}}
        for p in PERCENTILES:
            cut = np.percentile(s, p)
            flag = s >= cut
            yy = w.y[te].astype(bool)
            tp = int((flag & yy).sum())
            fp = int((flag & ~yy).sum())
            split["thresholds"][p] = {
                "recall": tp / max(int(yy.sum()), 1),
                "false_flag": fp / max(int((~yy).sum()), 1),
            }
        per_split.append(split)

    if len(per_split) < 3:
        raise Refusal(
            f"Only {len(per_split)} usable forward-chained split(s). "
            "At least 3 are needed. The export is too short or too sparse."
        )

    scores = np.concatenate(pooled_scores)
    ys = np.concatenate(pooled_y).astype(bool)
    idx = np.concatenate(pooled_idx)

    pooled = {"n": int(len(ys)), "timeouts": int(ys.sum()),
              "auc": _auc(ys.astype(int), scores), "thresholds": {}}

    kwh = w.kwh[idx]
    known = w.kwh_known[idx]
    rst = w.restart[idx]
    kwh_timeout_total = float(kwh[ys & known].sum())
    kwh_restart = float(kwh[ys & rst & known].sum())
    kwh_total = float(kwh[known].sum())

    for p in PERCENTILES:
        cut = np.percentile(scores, p)
        flag = scores >= cut
        tp = int((flag & ys).sum())
        fp = int((flag & ~ys).sum())
        caught = float(kwh[flag & ys & known].sum())
        pooled["thresholds"][p] = {
            "recall": tp / max(int(ys.sum()), 1),
            "false_flag": fp / max(int((~ys).sum()), 1),
            "energy_at_stake_kwh": caught,
            "energy_at_stake_excl_restart_kwh": float(kwh[flag & ys & ~rst & known].sum()),
        }

    spread = {}
    for p in PERCENTILES:
        r = np.array([s["thresholds"][p]["recall"] for s in per_split])
        f = np.array([s["thresholds"][p]["false_flag"] for s in per_split])
        spread[p] = {
            "recall_mean": float(r.mean()), "recall_sd": float(r.std(ddof=1)),
            "false_flag_mean": float(f.mean()), "false_flag_sd": float(f.std(ddof=1)),
        }

    return {
        "splits": per_split,
        "n_splits": len(per_split),
        "pooled": pooled,
        "per_split_spread": spread,
        "auc_per_split": [s["auc"] for s in per_split],
        "energy": {
            "timeout_kwh": kwh_timeout_total,
            "total_kwh": kwh_total,
            "timeout_share": (kwh_timeout_total / kwh_total) if kwh_total > 0 else None,
            "restart_kwh": kwh_restart,
            "timeout_kwh_excl_restart": kwh_timeout_total - kwh_restart,
            "timeout_share_excl_restart": ((kwh_timeout_total - kwh_restart) / kwh_total) if kwh_total > 0 else None,
            "restart_share_of_timeouts": (int((ys & rst).sum()) / max(int(ys.sum()), 1)),
            "coverage": float(known.mean()),
        },
    }


def shuffle_control(w: Windows, seed: int = 42, repeats: int = 3) -> dict:
    """
    Label-shuffle control. Training labels are shuffled; a model that still
    scores above chance is reading an artefact, not a signal.

    Judged on the MEAN of per-split AUCs across repeats, tested against 0.5.

    Two approaches were rejected. Requiring every split to sit in a fixed band
    fails good runs: on a short export a single month's AUC swings from 0.2 to
    0.9 for ordinary sampling reasons. Pooling the scores across months and
    taking one AUC is worse — months differ in score scale and in timeout base
    rate, and pooling a null across heterogeneous groups biases it away from
    0.5 even when nothing is leaking.

    So: collect every (repeat, split) AUC, and check that 0.5 lies within two
    standard errors of their mean. The tolerance widens automatically when
    there is little data and tightens when there is a lot, which is the
    behaviour you want from a control.
    """
    months = w.months
    all_aucs, per_split = [], []

    for rep in range(repeats):
        rng = np.random.default_rng(seed + rep)
        for k in range(1, len(months)):
            tr = np.isin(w.month, months[:k])
            te = w.month == months[k]
            if tr.sum() < 100 or te.sum() < 50:
                continue
            if w.y[tr].sum() < 5 or w.y[te].sum() < 5:
                continue
            y_tr = w.y[tr].copy()
            rng.shuffle(y_tr)
            s = _fit_predict(w.F[tr], y_tr, w.F[te], seed=seed)
            a = float(_auc(w.y[te], s))
            if not math.isnan(a):
                all_aucs.append(a)
                if rep == 0:
                    per_split.append(a)

    if len(all_aucs) < 3:
        return {"aucs": [], "mean": None, "sem": None, "min": None,
                "max": None, "per_split": per_split, "repeats": repeats,
                "passed": False,
                "reason": "too few usable splits to run a control"}

    arr = np.asarray(all_aucs, dtype=np.float64)
    mean = float(arr.mean())
    sem = float(arr.std(ddof=1) / math.sqrt(len(arr)))
    tol = max(2.0 * sem, 0.02)

    return {
        "aucs": all_aucs,
        "mean": mean,
        "sem": sem,
        "tolerance": tol,
        "min": float(arr.min()),
        "max": float(arr.max()),
        "per_split": per_split,
        "repeats": repeats,
        "passed": bool(abs(mean - 0.5) <= tol),
    }
