# Freelancer.com project status tracker

What share of projects posted on Freelancer.com end with **no freelancer selected**?
This repo collects every project posted in 2026 in two categories — **Data Analysis** (job id 1420)
and **Graphic Design** (job id 20) — with its final status, and publishes the percentages in
[`RESULTS.md`](RESULTS.md).

## How it works

`collect.py` calls Freelancer's public, no-login API
`GET https://www.freelancer.com/api/projects/0.1/projects/all/`, one UTC day at a time
(the endpoint returns roughly one day per query), paging 100 projects per request.

Status mapping:

| API `sub_status` | Group |
|---|---|
| `closed_expired`, `frozen_timeout` | **no freelancer selected** (bidding ended, nobody awarded; `frozen_timeout` is the interim status before Freelancer marks a project `closed_expired`) |
| `closed_awarded`, `frozen_awarded` | awarded |
| `cancel_buyer`, `cancel_seller`, `cancel_admin` | cancelled |
| empty / `active` | still open (excluded from the "of finished" percentage) |

Outputs:
- `data/projects_<category>.csv` — one row per project (id, date, title, type, status, bids, budget, URL)
- `data/summary.json` — totals and monthly counts
- `RESULTS.md` — the summary tables. The headline uses only months that ended at least 14 days before the run, so their projects have a final status; the latest month is marked provisional.

## Run locally

```bash
pip install -r requirements.txt
python collect.py --start 2026-01-01            # up to yesterday
python collect.py --start 2026-01-01 --end 2026-06-30
python collect.py --jobs 1420:data_analysis 20:graphic_design 3:php   # other categories
```

Find other job ids at
`https://www.freelancer.com/api/projects/0.1/jobs/?job_names[]=Web%20Scraping`.

## Run on GitHub

The workflow in `.github/workflows/collect.yml` runs only when started by hand
(**Actions → Collect Freelancer.com project statuses → Run workflow**). It commits the refreshed
`data/` and `RESULTS.md` back to the repo. In *Settings → Actions → General → Workflow permissions*
choose **Read and write permissions** so the commit step can push.

## Caveats

- Only projects still visible through the public API are counted; deleted or private projects are not.
- A project tagged with both categories counts in both.
- Recent projects are often still open, so look at the "of finished" figure, and treat the last
  month or two as provisional.
