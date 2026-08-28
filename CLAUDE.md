# CLAUDE.md

Guidance for Claude Code working in this repository.

## Project

The back-end for [www.sewagemap.co.uk](https://www.sewagemap.co.uk). It publishes two
kinds of artefact to the `thamessewage` S3 bucket, fronted by CloudFront at
`https://d1kmd884co9q6x.cloudfront.net`:

- **Downstream impact** — geoJSONs of river reaches downstream of active spills, for all
  eleven water companies (`update_downstream.py`).
- **Spill history** — per-CSO tables of past discharge events (`update_history.py`).

Three repositories are involved:

| Repo | Role |
|---|---|
| this one (`thames-sewage`) | the pipelines that build and publish artefacts |
| [`POOPy`](https://github.com/AlexLipp/POOPy) | library wrapping every water company's EDM API, plus the history data model |
| [`UK-Sewage-Map`](https://github.com/JonnyDawe/UK-Sewage-Map) | the front-end that reads the artefacts |

POOPy is installed editable from a sibling checkout, with no version pin. A change to
POOPy takes effect here immediately — and an out-of-date POOPy breaks this repo at
import, because `update_history.py` imports `poopy.history_io` at module level.

## Commands

```bash
export S3_PROFILE_NAME=<aws profile>          # required at import, even for --dry-run

python update_history.py                       # incremental history + Thames downstream
python update_history.py --dry-run             # fetch, merge and split locally, upload nothing
python update_history.py --full                # rebuild the whole master from the API
python update_history.py --skip-downstream     # history only
python update_downstream.py                    # all companies' downstream impact

python -m pytest test_update_history.py test_split_history.py -q
```

The deployment box runs these via local `run_*.sh` wrappers that are deliberately **not**
in this repo (they hold the AWS profile name and absolute paths). Cron: incremental every
6 hours, `--full` monthly.

## How history works

Thames Water is the **only** company with a historical API, so it is the only history
that updates automatically. This shapes everything below.

### Why it is incremental

The Thames alerts API is paginated newest-first at 1000 records per page and the record
is ~77,000 events, so a full fetch is ~150 sequential requests. One failure aborts the
run — which is exactly what happened: the published history sat frozen for two months
while cron retried and failed.

So a long-lived **master** table lives at `history_master/` on S3, holding the whole
record. Each run re-fetches only a recent window from the API and splices it in. Inside
the window the API is the source of truth; outside it the master is untouched. A typical
run makes one or two requests and takes under a minute.

`LOOKBACK_DAYS` (92) and `BUFFER_DAYS` (7) at the top of `update_history.py` are the
tuning knobs. If the master is staler than the window, the window widens automatically,
so a cron outage self-heals rather than tearing a permanent hole in the history.

If no master exists, one is seeded from the published `up_to_now.json` rather than
rebuilt from the API. The masters create themselves; `seed_history_master.py` is only for
seeding by hand from a specific file.

### Why it is split per CSO

The combined table reached 13 MB, and the front-end downloaded all of it to show the
~0.2% belonging to one CSO. `split_history.py` slices it into one file per CSO (median
~14 KB) under `discharge_histories/`.

### S3 layout

| Prefix | Contents |
|---|---|
| `discharges_to_date/` | the combined published tables and `timestamp.txt` |
| `discharge_histories/thames{,_offline}/` | per-CSO slices — **what the site reads** |
| `history_master/` | long-lived masters, plus dated `backups/` |
| `now/`, `info_now/`, `past/`, `downstream_impact/<company>/` | downstream impact |

The master and the slices live under their own top-level prefixes deliberately:
`empty_s3_folder("discharges_to_date/")` runs before each publish and would delete
anything stored beneath it.

## Things that will bite you

- **Timestamps are epoch-milliseconds integers, not strings.** The published JSON is
  `DataFrame.to_json(orient="columns")` with pandas' default date format. The front-end
  does `new Date(value)`, which is correct for a number and `Invalid Date` for the same
  value as a string. Never "tidy" this into ISO strings.
- **`StopDateTime` must never be null in published files.** `new Date(null)` is 1970 and
  draws a decades-long bar. Ongoing events get their stop filled with "now" at publish
  time; the master keeps the honest null.
- **Permit-number sanitisation is duplicated across repos.** `sanitise_permit` in
  `split_history.py` and `sanitisePermitNumber` in the front-end's
  `src/utils/discharge/historyUrls.ts` must stay identical, or slices 404 silently. Both
  are `[^A-Za-z0-9._-]` → `_`. Only permits containing `/` are affected.
- **The event key is `(LocationName, PermitNumber, StartDateTime)`.** Permit number alone
  is not unique — 518 Thames permits map to 542 location names.
- **`history_to_discharge_df()` only walks *active* monitors**, so a full rebuild silently
  drops history for decommissioned ones. Merging at the table level is what preserves them.
- **The bucket policy grants no `s3:ListBucket`**, so S3 answers 403 rather than 404 for a
  missing key. Both mean "nothing on record".
- **The Thames API returns an empty 200 body instead of an error** when rate-limiting.
  POOPy retries this rather than treating it as the end of the record.
- `upload_file_to_s3` swallows exceptions unless `strict=True`. Anything whose silent
  failure would leave artefacts inconsistent must pass `strict`.

## The EIR pipeline (`EIR_Data` branch)

Companies other than Thames publish no historical API, so their spill history is obtained
by EIR request and cleaned offline. This lives on the **`EIR_Data` branch**, not `main`:

```
clean_EIR_stopstartdata/
  standardise_stopstart_data.py     raw company files -> standardised CSVs
  raw_to_standardised/clean_*.py    one cleaner per company
  build_water_company_jsons.py      standardised CSVs + ArcGIS metadata -> output_jsons/
```

`output_jsons/<company>.json` uses the **same nine-column schema** as the Thames
artefacts, so those tables can be published with the existing splitter and loaded into
POOPy via `WaterCompany.set_all_histories_from_json()`.

### Timezones — read before touching `build_water_company_jsons.py`

Source timestamps are **local (`Europe/London`) wall-clock**. `parse_datetime_to_epoch_ms`
localises them and converts to UTC, using `ambiguous=True` (the repeated autumn hour is
assumed to be BST) and `nonexistent="shift_forward"`. That conversion is correct.

The companies' own `duration_minutes` column is the *naive* difference between their two
local timestamps, so it disagrees with reality for any event spanning a clock change: an
event running 00:15 BST to 02:15 GMT genuinely lasts three hours, while the paperwork says
two. `Duration` is therefore **derived from the converted timestamps**, not carried from
the source, and `validate_output_dataframe` asserts the two agree. Do not "restore" the
source figure.

### Known data-quality issues in the tables

As of 2026-08, before republishing check whether these persist:

- **Anglian**: 1,266 events dated in the future (to 2026-12-05); 15,701 zero-duration
  events (start == stop), ~2.3% of rows.
- **Anglian**: 363 CSOs log overlapping spills sharing a start time — genuine source
  ambiguity, not duplicate rows. Dropping either would understate spilling.
- Small numbers of exactly-identical duplicate rows (South West Water 22, Anglian 8).

Coverage against the live asset registers is 89–95% (Yorkshire 95.2%, Anglian 93.8%,
Severn Trent 88.9%, United Utilities 88.9%), so ~1 in 10 CSOs on the map would have no
history to show and the UI must degrade gracefully.

## Conventions

- Formatted with `black` (line length 88).
- Tests are plain `pytest` files at the repo root; they stub S3 rather than touching it.
- Data artefacts (`output_dir/`, `raw_data/`, `output_jsons/`) are gitignored — `.gitignore`
  has a blanket `*.json`/`*.txt`, so anything JSON you do want tracked needs `git add -f`.
- Never commit AWS credentials, profile names, or deployment paths; the `run_*.sh`
  wrappers are intentionally untracked.
