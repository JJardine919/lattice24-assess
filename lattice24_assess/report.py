"""
Report rendering.

The constraints below are enforced in code, not left to a writer's judgement.
A flagged job saves no energy until someone acts on the flag, so this module
reports energy AT STAKE and never energy saved. There is no code path that
multiplies a recall figure by an assumed action rate, and there is no GPU
figure anywhere, because none has ever been measured.
"""

from __future__ import annotations

import html
import json
from datetime import datetime, timezone

DOI_VERSION = "10.5281/zenodo.21913139"   # v3 — the version these numbers come from
DOI_CONCEPT = "10.5281/zenodo.21911351"   # resolves to latest

# Published reference figures. A prior, not a prediction for anyone's cluster.
REF = {
    "cluster": "NLR Kestrel",
    "windows": 6_828_355,
    "timeouts": 401_152,
    "months": 24,
    "timeout_energy_share": 0.461,
    "recall_80": 0.954,
    "false_flag_80": 0.169,
    "recall_80_split_sd": 0.063,
    "false_flag_80_split_sd": 0.125,
}

FORBIDDEN = ("energy saved", "savings of", "we saved", "carbon credit",
             "verified tonne", "gpu energy reduc")

# Sentences that contain a forbidden phrase precisely in order to deny it.
# These are removed before scanning, so the guard cannot be defeated by
# wording but also cannot fire on its own disclaimers.
DISCLAIMERS = (
    "energy at stake is not energy saved",
    "energy at stake, never energy saved",
    "reports energy at stake and never energy saved",
    "no energy saving is claimed",
)


def _assert_clean(text: str) -> str:
    """
    Last line of defence: refuse to emit a report containing a claim we have
    agreed is not supportable. Checked after the disclaimers are stripped, so
    stating 'X is not energy saved' is allowed and asserting it is not.
    """
    low = text.lower()
    for ok in DISCLAIMERS:
        low = low.replace(ok, "")
    for phrase in FORBIDDEN:
        if phrase in low:
            raise AssertionError(
                f"Report generation aborted: contains forbidden claim {phrase!r}. "
                "This tool reports energy at stake, never energy saved."
            )
    return text


def build_summary(meta, windows_stats, fc, control) -> dict:
    return {
        "schema": "lattice24-assess/1",
        "generated_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "tool_version": meta.get("version"),
        "site_label": meta.get("site_label"),
        "input": windows_stats,
        "forward_chained": fc,
        "shuffle_control": control,
        "reference": REF,
        "doi_version": DOI_VERSION,
        "doi_concept": DOI_CONCEPT,
        "statement": (
            "Figures are job recall, false-flag rate, and energy at stake. "
            "Energy at stake is not energy saved: a flagged job avoids no "
            "power unless the site acts on the flag."
        ),
    }


def _pct(x) -> str:
    return "—" if x is None else f"{100 * x:.1f}%"


def render_html(summary: dict) -> str:
    fc = summary["forward_chained"]
    pooled = fc["pooled"]
    spread = fc["per_split_spread"]
    energy = fc["energy"]
    ctl = summary["shuffle_control"]
    inp = summary["input"]

    rows = []
    for p in (50, 75, 80, 90, 95):
        t = pooled["thresholds"][str(p)] if str(p) in pooled["thresholds"] else pooled["thresholds"][p]
        s = spread[str(p)] if str(p) in spread else spread[p]
        stake = t["energy_at_stake_kwh"]
        rows.append(
            f"<tr{' class=rec' if p == 80 else ''}>"
            f"<td>{p}th</td>"
            f"<td>{_pct(t['recall'])}</td>"
            f"<td>{_pct(t['false_flag'])}</td>"
            f"<td>{_pct(s['recall_mean'])} ± {100*s['recall_sd']:.1f}</td>"
            f"<td>{_pct(s['false_flag_mean'])} ± {100*s['false_flag_sd']:.1f}</td>"
            f"<td>{stake:,.0f} kWh</td></tr>"
        )

    if not ctl.get("passed"):
        banner = (
            "<div class='bad'><b>This run is not valid.</b> The label-shuffle "
            "control did not land near chance (mean AUC "
            f"{ctl.get('mean'):.3f}, tolerance +/-{ctl.get('tolerance', 0):.3f}). "
            "A control that departs from "
            "~0.50 means the pipeline is leaking information, so no headline "
            "figure is shown. Do not quote anything from this report.</div>"
        )
        headline = ""
    else:
        banner = ""
        share = energy["timeout_share"]
        share_txt = (
            f"Timed-out jobs carry <b>{_pct(share)}</b> of the job energy this "
            "export reports."
            if share is not None else
            "Your export carried no energy column, so the share of energy in "
            "timed-out jobs could not be measured here."
        )
        t80 = pooled["thresholds"].get("80", pooled["thresholds"].get(80))
        headline = (
            f"<div class='head'><p>{share_txt}</p>"
            f"<p>At the 80th-percentile threshold, this model catches "
            f"<b>{_pct(t80['recall'])}</b> of them before they start, while "
            f"flagging <b>{_pct(t80['false_flag'])}</b> of jobs that would "
            f"have finished normally.</p></div>"
        )

    doc = f"""<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Timeout Exposure — {html.escape(str(summary.get('site_label') or 'your cluster'))}</title>
<style>
 :root {{ --bg:#fff; --fg:#111; --mut:#666; --line:#e3e3e3; --acc:#127a3d; --bad:#b00020; }}
 @media (prefers-color-scheme: dark) {{ :root:not([data-theme=light]) {{
   --bg:#0e0e0e; --fg:#ececec; --mut:#9a9a9a; --line:#2a2a2a; --acc:#3ddc84; }} }}
 :root[data-theme=dark] {{ --bg:#0e0e0e; --fg:#ececec; --mut:#9a9a9a; --line:#2a2a2a; --acc:#3ddc84; }}
 body {{ background:var(--bg); color:var(--fg); margin:0;
   font:16px/1.55 ui-sans-serif,system-ui,-apple-system,Segoe UI,Roboto,sans-serif; }}
 main {{ max-width:860px; margin:0 auto; padding:48px 16px 80px; }}
 h1 {{ font-size:1.9rem; line-height:1.2; margin:0 0 .3em; }}
 h2 {{ font-size:1.15rem; margin:2.4em 0 .6em; }}
 .sub {{ color:var(--mut); margin:0 0 2em; }}
 .head p {{ font-size:1.12rem; margin:.5em 0; }}
 table {{ border-collapse:collapse; width:100%; margin:1em 0; font-size:.93rem; }}
 th,td {{ text-align:left; padding:9px 10px; border-bottom:1px solid var(--line); }}
 th {{ color:var(--mut); font-weight:600; font-size:.8rem;
   text-transform:uppercase; letter-spacing:.04em; }}
 tr.rec td {{ background:color-mix(in srgb, var(--acc) 10%, transparent); font-weight:600; }}
 .note {{ color:var(--mut); font-size:.9rem; }}
 .bad {{ border-left:3px solid var(--bad); padding:12px 14px; margin:1.5em 0;
   background:color-mix(in srgb, var(--bad) 8%, transparent); }}
 .limit {{ border-left:3px solid var(--line); padding:4px 0 4px 16px; margin:1.2em 0; }}
 code {{ font-family:ui-monospace,SFMono-Regular,Menlo,monospace; font-size:.88em; }}
 a {{ color:var(--acc); }}
 @media (max-width:520px) {{ table {{ font-size:.82rem; }} th,td {{ padding:7px 5px; }} }}
</style></head><body><main>

<h1>Timeout exposure on {html.escape(str(summary.get('site_label') or 'your cluster'))}</h1>
<p class="sub">Measured on your own job records · generated {html.escape(summary['generated_utc'])}<br>
Nothing in this report left your machine.</p>

{banner}
{headline}

<h2>What this is</h2>
<p>Jobs that hit their wall-clock limit run to the full time requested and then
produce nothing. Your scheduler already recorded every one of them. This report
fits a model to <em>your</em> history — four summary statistics of each user's
previous {24} wall-clock ratios — and reports how well it identifies those jobs
<em>before they start</em>.</p>

<h2>Your numbers</h2>
<table>
<thead><tr><th>Threshold</th><th>Recall (pooled)</th><th>False flags (pooled)</th>
<th>Recall per split</th><th>False flags per split</th><th>Energy at stake</th></tr></thead>
<tbody>{''.join(rows)}</tbody></table>
<p class="note">Fit on history, applied to the following month, across
{fc['n_splits']} forward-chained splits — never in reverse. Pooled figures
combine all splits; the per-split columns show how much that varies month to
month, which is the number to plan against.</p>

<h2>Energy at stake is not energy saved</h2>
<p>The last column is the energy in timed-out jobs the model would have
flagged. It becomes a saving only to the extent that someone acts on a flag —
resizes the request, fixes the job, or declines it. That multiplier belongs to
your site and this tool does not guess it. At the 80th-percentile threshold,
{_pct((pooled['thresholds'].get('80') or pooled['thresholds'].get(80))['false_flag'])}
of jobs that would have completed normally are also flagged; whatever you do
with a flag, you do to those too.</p>

<h2>Controls</h2>
<p>Label-shuffle control, {len(ctl.get('aucs', []))} shuffled fits over
{ctl.get('repeats', 0)} repeats: mean AUC <b>{ctl.get('mean'):.3f}</b>
(tolerance &plusmn;{ctl.get('tolerance', 0):.3f} around 0.500, individual fits
{ctl.get('min'):.3f}&ndash;{ctl.get('max'):.3f})
{' &mdash; passed' if ctl.get('passed') else ' &mdash; FAILED'}.
Real AUC per split ranged {min(fc['auc_per_split']):.3f}–{max(fc['auc_per_split']):.3f}.
If the shuffled control had scored much above chance, the result would be an
artefact and this report says so instead of showing a headline.</p>

<h2>What this report does not claim</h2>
<div class="limit">
<p>No energy saving is claimed — only job recall and energy at stake.</p>
<p>No GPU measurement is claimed. None has ever been made.</p>
<p>No carbon tonnage, credit, or offset value is computed.</p>
<p>Timed-out jobs only. CANCELLED and FAILED jobs are excluded, so the
recoverable waste at your site is likely larger than the figure above, not
smaller.</p>
<p>Energy figures cover the {_pct(energy['coverage'])} of scored jobs for which
your export reported energy. Job energy only — no idle capacity, no cooling,
no facility overhead, no PUE.</p>
</div>

<h2>How this compares to the published baseline</h2>
<p>On {REF['cluster']} ({REF['windows']:,} windows, {REF['timeouts']:,} timeouts,
{REF['months']} months), timed-out jobs carried {_pct(REF['timeout_energy_share'])}
of job energy, and the same method caught {_pct(REF['recall_80'])} of them at a
{_pct(REF['false_flag_80'])} false-flag rate ({_pct(REF['recall_80_split_sd'])} and
{_pct(REF['false_flag_80_split_sd'])} per-split standard deviations). That is a
prior from one cluster, not a prediction for yours. If your numbers come out
lower, the opportunity here is smaller — that is a real result and you should
treat it as one.</p>
<p class="note">Method and data:
<a href="https://doi.org/{DOI_VERSION}">doi.org/{DOI_VERSION}</a>
(v3 — the version these reference figures come from).</p>

<h2>Input</h2>
<table>
<tr><td>Rows read</td><td>{inp.get('rows_read', 0):,}</td></tr>
<tr><td>Scoreable windows</td><td>{inp.get('windows', 0):,}</td></tr>
<tr><td>TIMEOUT events</td><td>{inp.get('timeouts', 0):,}</td></tr>
<tr><td>Months covered</td><td>{inp.get('n_months', 0)}</td></tr>
<tr><td>Rows without a time limit</td><td>{inp.get('rows_dropped_no_timelimit', 0):,}</td></tr>
<tr><td>Rows unparseable</td><td>{inp.get('rows_dropped_unparseable', 0):,}</td></tr>
</table>
<p class="note">User identifiers were hashed with a salt generated for this run
and discarded. Job names, working directories, command lines and account codes
are dropped at parse time and never read.</p>

</main></body></html>"""
    return _assert_clean(doc)


def write_outputs(outdir: str, summary: dict) -> tuple[str, str]:
    import os
    os.makedirs(outdir, exist_ok=True)
    jp = os.path.join(outdir, "timeout_exposure_summary.json")
    hp = os.path.join(outdir, "timeout_exposure_report.html")
    with open(jp, "w", encoding="utf-8") as fh:
        json.dump(summary, fh, indent=2, default=str)
    with open(hp, "w", encoding="utf-8") as fh:
        fh.write(render_html(summary))
    return jp, hp
