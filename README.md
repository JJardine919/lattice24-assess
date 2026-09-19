# lattice24-assess

Measure how much of your cluster's job energy sits in **timed-out jobs**, and
how well those jobs can be identified **before they start** — using nothing but
your own scheduler history.

Runs entirely on your machine. No network calls, no telemetry, no upload.
Read `lattice24_assess/core.py` before you run it; it is short and it is the
whole method.

## Why this exists

A job that hits its wall-clock limit runs for the full time requested and then
produces nothing. Your scheduler already recorded every one of them. On the
published reference cluster those jobs carried **46% of job energy**.

## Install

Python 3.9+. Depends only on `numpy` and `scikit-learn`.

Most current Linux distributions refuse a system-wide `pip install`
(PEP 668), so use a virtual environment:

```bash
git clone https://github.com/JJardine919/lattice24-assess
cd lattice24-assess
python3 -m venv .venv
.venv/bin/pip install -e .
```

Or, if your site already provides numpy and scikit-learn as modules, skip the
install entirely and run it from the clone:

```bash
python3 -m lattice24_assess.cli export.psv --out ./report
```

`pipx install git+https://github.com/JJardine919/lattice24-assess` also works
if you have pipx.

## Use

```bash
sacct --allocations --parsable2 \
      --starttime=$(date -d '12 months ago' +%F) \
      -o User,End,Timelimit,Elapsed,State,ConsumedEnergyRaw > export.psv

.venv/bin/lattice24-assess export.psv --site "Our Cluster" --out ./report
```

Writes two files: an HTML report you can read in a browser, and a JSON summary.

## What it needs

Five columns, by any of the usual names:

| Column | What it is |
|---|---|
| `User` | any stable per-user identifier — hashed on read |
| `End` | when the job finished |
| `Timelimit` | wall clock **requested** |
| `Elapsed` | wall clock **used** |
| `State` | `TIMEOUT`, `COMPLETED`, `FAILED`, … |

`ConsumedEnergyRaw` is optional. Without it you still get recall and
false-flag rates; you just don't get the energy column.

CSV works too, with the same column names.

## What it does with your data

- User IDs are hashed on read with a salt generated for that run and then
  discarded, so hashes cannot be linked between runs or back to a person.
- Job names, working directories, command lines, account codes and comments
  are dropped at parse time and **never parsed**.
- The JSON summary contains aggregate statistics only — no job records, no
  identifiers. Sending it to us is optional and the report is complete
  without it.

## The method

For each job, take that user's previous **24** jobs and compute four
statistics of `Elapsed / Timelimit`: mean, standard deviation, range, and mean
absolute successive difference. Logistic regression on those four numbers.
Nothing about the job being predicted enters its own features.

Validation is **forward-chained**: fit on months 1..k, score month k+1, never
the reverse. Both pooled and per-split figures are reported, because the
per-split spread is what you plan against.

A **label-shuffle control** runs every time. If a model trained on shuffled
labels scores meaningfully above chance, the report says the run is invalid
and prints no headline figure.

Method and reference data: <https://doi.org/10.5281/zenodo.21913139>

## When it refuses

It refuses rather than printing a number it cannot stand behind:

- fewer than 6 months of scoreable jobs
- fewer than 50,000 scoreable windows
- fewer than 200 `TIMEOUT` events
- fewer than 3 usable forward-chained splits

Each refusal says what would fix it.

## What this tool will not tell you

**It reports energy at stake, not energy saved.** A flagged job avoids no power
until somebody acts on the flag — resizes the request, fixes the job, or
declines it. That multiplier belongs to your site, and this tool does not guess
it. There is no code path that multiplies recall by an assumed action rate.

It also claims no GPU measurement, computes no carbon tonnage or credit value,
and counts job energy only — no idle capacity, cooling, or facility overhead.

Timed-out jobs only; `CANCELLED` and `FAILED` are excluded. Your recoverable
waste is therefore probably **larger** than what this reports, not smaller.

## Licence

Apache-2.0. See `LICENSE`.

---

Built by [Lattice24](https://lattice24.com). If your numbers come out low, that
is a real result and you should treat it as one.
