#!/usr/bin/env python3
"""
Collect Freelancer.com projects posted in a date range for selected job
categories, together with their status, and summarise how many ended with
no freelancer selected.

Uses the public (no-auth) endpoint:
    GET https://www.freelancer.com/api/projects/0.1/projects/all/

Notes discovered while building this:
  * The search returns at most ~1 day of results per query, whatever
    from_time/to_time you pass, so we query one UTC day at a time and page
    with `offset` inside each day.
  * Status fields on each project:
        status       active | closed | frozen | ...
        sub_status   closed_expired  -> closed, nobody awarded
                                        ("No freelancer selected" on the site)
                     closed_awarded  -> a freelancer was awarded
                     frozen_timeout  -> bidding period ended, nobody awarded; Freelancer
                                        later turns these into closed_expired, so they
                                        count as "no freelancer selected"
                     frozen_awarded  -> awarded, waiting for the freelancer to accept
                     cancel_buyer / cancel_seller / cancel_admin -> cancelled
                     (empty while the project is still open)

Usage:
    python collect.py --start 2026-01-01 --end 2026-12-31
    python collect.py --start 2026-01-01 --jobs 1420:data_analysis 20:graphic_design
"""
from __future__ import annotations

import argparse
import csv
import datetime as dt
import json
import os
import sys
import time
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed

import requests

API = "https://www.freelancer.com/api/projects/0.1/projects/all/"
DEFAULT_JOBS = {1420: "data_analysis", 20: "graphic_design"}
PAGE = 100
HEADERS = {"User-Agent": "freelancer-project-status-research/1.0"}


def classify(p: dict) -> str:
    """Collapse raw status/sub_status into analysis groups."""
    sub = (p.get("sub_status") or "").lower()
    status = (p.get("status") or "").lower()
    if sub in ("closed_expired", "frozen_timeout"):
        return "no_freelancer_selected"
    if sub == "closed_awarded" or sub.startswith("frozen") or status == "frozen":
        return "awarded"
    if sub.startswith("cancel"):
        return "cancelled"
    if status in ("active", "pending", "draft") or not sub:
        return "still_open"
    return "other"


def get(session: requests.Session, params: dict, retries: int = 12) -> dict:
    for attempt in range(retries):
        try:
            r = session.get(API, params=params, headers=HEADERS, timeout=30)
            if r.status_code == 429:
                time.sleep(300)  # Freelancer blocks bursts; wait 5 min
                continue
            r.raise_for_status()
            j = r.json()
            if j.get("status") == "success":
                return j["result"]
        except (requests.RequestException, ValueError):
            pass
        time.sleep(2 * (attempt + 1))
    raise RuntimeError(f"Request failed after {retries} attempts: {params}")


def fetch_day(job_id: int, day: dt.date, delay: float) -> list[dict]:
    start = int(dt.datetime(day.year, day.month, day.day, tzinfo=dt.timezone.utc).timestamp())
    end = start + 86400
    session = requests.Session()
    rows, offset, total = [], 0, 1
    while offset < total and offset < 10000:
        res = get(session, {
            "jobs[]": job_id, "from_time": start, "to_time": end,
            "limit": PAGE, "offset": offset, "compact": "true",
        })
        total = res.get("total_count") or 0
        projects = res.get("projects") or []
        if not projects:
            break
        for p in projects:
            bs = p.get("bid_stats") or {}
            budget = p.get("budget") or {}
            cur = p.get("currency") or {}
            rows.append({
                "id": p["id"],
                "time_submitted": dt.datetime.fromtimestamp(p["time_submitted"], dt.timezone.utc).isoformat(),
                "title": p.get("title", ""),
                "type": p.get("type"),
                "status": p.get("status"),
                "sub_status": p.get("sub_status"),
                "frontend_status": p.get("frontend_project_status"),
                "group": classify(p),
                "bid_count": bs.get("bid_count"),
                "bid_avg": bs.get("bid_avg"),
                "budget_min": budget.get("minimum"),
                "budget_max": budget.get("maximum"),
                "currency": cur.get("code"),
                "url": f"https://www.freelancer.com/projects/{p.get('seo_url', p['id'])}",
            })
        offset += PAGE
        time.sleep(delay)
    return rows


def daterange(a: dt.date, b: dt.date):
    d = a
    while d <= b:
        yield d
        d += dt.timedelta(days=1)


PRETTY = {"data_analysis": "Data Analysis", "graphic_design": "Graphic Design"}


def summarise(by_job: dict[str, list[dict]], end: dt.date, settle_days: int = 14) -> dict:
    """Totals per category. 'settled' covers months that ended at least
    `settle_days` before `end`, so nearly all their projects have a final status."""
    out = {}
    for name, rows in by_job.items():
        monthly, by_type = defaultdict(Counter), defaultdict(Counter)
        for r in rows:
            monthly[r["time_submitted"][:7]][r["group"]] += 1
            by_type[r["type"]][r["group"]] += 1
        out[name] = summarise_counts(dict(monthly), dict(by_type), end, settle_days)
    return out


def summarise_counts(monthly: dict, by_type: dict, end: dt.date, settle_days: int = 14) -> dict:
    def stats(c: Counter) -> dict:
        c = Counter(c)
        total = sum(c.values())
        fin = total - c["still_open"]
        return {
            "projects": total, "finished": fin, "groups": dict(c),
            "pct_no_freelancer": round(100 * c["no_freelancer_selected"] / fin, 1) if fin else None,
            "pct_awarded": round(100 * c["awarded"] / fin, 1) if fin else None,
            "pct_cancelled": round(100 * c["cancelled"] / fin, 1) if fin else None,
        }

    def month_end(m: str) -> dt.date:
        y, mo = map(int, m.split("-"))
        return dt.date(y + (mo == 12), mo % 12 + 1, 1) - dt.timedelta(days=1)

    settled_months = [m for m in sorted(monthly) if month_end(m) <= end - dt.timedelta(days=settle_days)]
    all_c, settled_c = Counter(), Counter()
    for m, c in monthly.items():
        all_c.update(c)
        if m in settled_months:
            settled_c.update(c)
    return {
        "all": stats(all_c),
        "settled": stats(settled_c),
        "settled_months": [settled_months[0], settled_months[-1]] if settled_months else None,
        "monthly": {m: stats(c) for m, c in sorted(monthly.items())},
        "by_type": {t: stats(c) for t, c in sorted(by_type.items())},
    }


def write_report(summary: dict, start: dt.date, end: dt.date, path: str) -> None:
    names = list(summary)
    label = lambda n: PRETTY.get(n, n)
    first = next(iter(summary.values()))
    sm = first["settled_months"]
    lines = [
        "# Freelancer.com: projects with no freelancer selected",
        "",
        f"Projects posted {start} – {end} (UTC). Generated {dt.datetime.now(dt.timezone.utc):%Y-%m-%d %H:%M} UTC.",
        "",
    ]
    if sm:
        lines += [
            f"## Headline: projects posted {sm[0]} to {sm[1]} (months whose projects have all finished)",
            "",
            "| Category | Projects | No freelancer selected | Awarded | Cancelled | **% no freelancer selected** |",
            "|---|---:|---:|---:|---:|---:|",
        ]
        for n, s in summary.items():
            t = s["settled"]; g = t["groups"]
            lines.append(f"| {label(n)} | {t['projects']:,} | {g.get('no_freelancer_selected', 0):,} | "
                         f"{t['pct_awarded']}% | {t['pct_cancelled']}% | **{t['pct_no_freelancer']}%** |")
        lines.append("")
    lines += [
        "## Whole period, including the latest month (provisional)",
        "",
        "| Category | Projects | Still open | No freelancer selected | % no freelancer (of finished) |",
        "|---|---:|---:|---:|---:|",
    ]
    for n, s in summary.items():
        t = s["all"]; g = t["groups"]
        lines.append(f"| {label(n)} | {t['projects']:,} | {g.get('still_open', 0):,} | "
                     f"{g.get('no_freelancer_selected', 0):,} | {t['pct_no_freelancer']}% |")
    lines += [
        "",
        "## By month (% of finished projects with no freelancer selected)",
        "",
        "| Month | " + " | ".join(label(n) for n in names) + " |",
        "|---|" + "---:|" * len(names),
    ]
    months = sorted({m for s in summary.values() for m in s["monthly"]})
    for m in months:
        cells = []
        for s in summary.values():
            t = s["monthly"].get(m)
            cells.append(f"{t['pct_no_freelancer']}% (n={t['finished']:,})" if t and t["finished"] else "–")
        flag = "" if sm and m <= sm[1] else " *(provisional)*"
        lines.append(f"| {m}{flag} | " + " | ".join(cells) + " |")
    lines += [
        "",
        "## By project type (whole period)",
        "",
        "| Type | " + " | ".join(label(n) for n in names) + " |",
        "|---|" + "---:|" * len(names),
    ]
    types = sorted({t for s in summary.values() for t in s["by_type"]})
    for ty in types:
        cells = []
        for s in summary.values():
            t = s["by_type"].get(ty)
            cells.append(f"{t['pct_no_freelancer']}% (n={t['finished']:,})" if t and t["finished"] else "–")
        lines.append(f"| {ty} | " + " | ".join(cells) + " |")
    lines += [
        "",
        "Percentages are of *finished* projects (no freelancer selected + awarded + cancelled); projects still open are left out.",
        "",
        "Status mapping: `closed_expired` and `frozen_timeout` (bidding ended, nobody awarded) = *no freelancer selected*;",
        "`closed_awarded` and `frozen_awarded` = *awarded*; `cancel_*` = *cancelled*; `active` = *still open*.",
    ]
    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--start", default="2026-01-01")
    ap.add_argument("--end", default=None, help="inclusive; default = yesterday (UTC)")
    ap.add_argument("--jobs", nargs="*", help="job_id:name pairs, default 1420:data_analysis 20:graphic_design")
    ap.add_argument("--out", default="data")
    ap.add_argument("--workers", type=int, default=1)
    ap.add_argument("--delay", type=float, default=2.0, help="seconds between page requests per worker")
    a = ap.parse_args()

    start = dt.date.fromisoformat(a.start)
    end = dt.date.fromisoformat(a.end) if a.end else dt.datetime.now(dt.timezone.utc).date() - dt.timedelta(days=1)
    jobs = DEFAULT_JOBS if not a.jobs else {int(j.split(":")[0]): j.split(":")[1] for j in a.jobs}
    os.makedirs(a.out, exist_ok=True)

    tasks = [(jid, name, d) for jid, name in jobs.items() for d in daterange(start, end)]
    by_job: dict[str, dict[int, dict]] = {name: {} for name in jobs.values()}
    for attempt in range(3):  # retry days that failed (e.g. long rate-limit spells)
        failed = []
        with ThreadPoolExecutor(max_workers=a.workers) as ex:
            futs = {ex.submit(fetch_day, jid, d, a.delay): (jid, name, d) for jid, name, d in tasks}
            for i, fut in enumerate(as_completed(futs), 1):
                jid, name, d = futs[fut]
                try:
                    for r in fut.result():
                        by_job[name][r["id"]] = r
                except Exception as e:
                    print(f"[warn] {name} {d}: {e}", file=sys.stderr)
                    failed.append((jid, name, d))
                if i % 50 == 0:
                    print(f"{i}/{len(tasks)} day-queries done", file=sys.stderr)
        if not failed:
            break
        print(f"Retrying {len(failed)} failed days after a pause", file=sys.stderr)
        time.sleep(120)
        tasks = failed
    else:
        print(f"[warn] {len(failed)} day-queries still failed; results are incomplete", file=sys.stderr)

    rows_by_job = {n: sorted(v.values(), key=lambda r: r["time_submitted"]) for n, v in by_job.items()}
    for name, rows in rows_by_job.items():
        path = os.path.join(a.out, f"projects_{name}.csv")
        with open(path, "w", newline="", encoding="utf-8") as f:
            if rows:
                w = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
                w.writeheader()
                w.writerows(rows)
        print(f"{name}: {len(rows)} projects -> {path}")

    summary = summarise(rows_by_job, end)
    with open(os.path.join(a.out, "summary.json"), "w", encoding="utf-8") as f:
        json.dump({"start": str(start), "end": str(end), "categories": summary}, f, indent=2)
    write_report(summary, start, end, "RESULTS.md")
    print(json.dumps({k: {"settled": v["settled"]["pct_no_freelancer"], "all": v["all"]["pct_no_freelancer"]} for k, v in summary.items()}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
