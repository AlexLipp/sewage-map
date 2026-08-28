
# Mapping Sewage Discharges into rivers across England <!--and Wales-->

![opengraphsocial](https://github.com/user-attachments/assets/45473ed6-309c-419a-b8fe-f70575043b2b)

[![License: GPL v3](https://img.shields.io/badge/License-GPLv3-blue.svg)](https://www.gnu.org/licenses/gpl-3.0)


Realtime mapping the downstream impact of Combined Sewage Overflow discharge events in non-tidal rivers across England and Scotland <!--and Wales-->. This repository provides the back-end for [`www.sewagemap.co.uk`](https://www.sewagemap.co.uk/). The repository  for the front-end is available at [`github.com/JonnyDawe/UK-Sewage-Map/`](https://github.com/JonnyDawe/UK-Sewage-Map/).

This was developed by [Alex Lipp](https://alexlipp.github.io/), [Jonny Dawe](https://www.linkedin.com/in/jonathan-dawe-46180212a) and Sudhir Balaji. Please feel free to raise an issue above or contact us directly.

[![Twitter Follow](https://img.shields.io/twitter/follow/alexglipp?style=social)](https://twitter.com/intent/follow?screen_name=AlexGLipp)
[![Twitter Follow](https://img.shields.io/twitter/follow/JdMapDev?style=social)](https://twitter.com/intent/follow?screen_name=JdMapDev)
[![GitHub followers](https://img.shields.io/github/followers/AlexLipp?label=AlexLipp&style=social)](https://github.com/AlexLipp)
[![GitHub followers](https://img.shields.io/github/followers/JonnyDawe?label=JonnyDawe&style=social)](https://github.com/JonnyDawe)
[![GitHub followers](https://img.shields.io/github/followers/sudhir-b?label=sudhir-b&style=social)](https://github.com/sudhir-b)

- [Installation](#installation)
- [Usage](#usage)
- [Output Data](#output-data)
   - [Water Company Data Table](#water-company-data-table)
- [Source Data](#source-data)

## Installation

This script relies on the `POOPy` package which I ([Alex](https://alexlipp.github.io/)) have created to allow easy interaction with Water Company Event Duration Monitoring (EDM) APIs, and analysis of the data. This is freely available at: [`github.com/AlexLipp/POOPy`](https://github.com/AlexLipp/POOPy).

## Usage

Two scripts run on a cron schedule, both using POOPy:

- **`update_downstream.py`** calculates, for all eleven water companies, geoJSON files containing the downstream impact of active or recently active CSO spills. These are uploaded to the Amazon Web Services bucket which hosts them, fronted by the CloudFront delivery service, and read by the `www.sewagemap.co.uk` front-end.
- **`update_history.py`** does the same for Thames Water's downstream impact, and additionally maintains the spill *history* tables (Thames is the only company publishing a historical API). It calls **`split_history.py`** to publish those tables as one file per CSO as well.

### Historical data: incremental updates

The Thames alerts API is paginated newest-first at 1000 records per page, and the record now runs to ~77,000 events. Fetching all of it costs ~150 sequential requests, and a single failure part-way through aborts the whole run — which is why the published history went stale.

`update_history.py` therefore keeps a long-lived **master** table on S3 covering the whole record, and each run re-fetches only a recent window from the API and splices it in. In the window the API is the source of truth; outside it the master is left untouched. A typical incremental run makes **one or two API requests instead of ~150**, and takes under a minute.

The window is set by two constants at the top of `update_history.py`:

```python
LOOKBACK_DAYS = 92   # 3 months. Days the API is treated as authoritative for.
BUFFER_DAYS = 7      # Extra days fetched, but not trusted, for overlap.
```

Lower `LOOKBACK_DAYS` for faster runs and fewer API calls; raise it to tolerate a longer cron outage and pick up more of Thames's retrospective edits. Set it to `31` for a one-month window. Both are overridable per run with `--lookback-days` / `--buffer-days`.

If the master is *staler* than the configured window, the window is widened automatically so a cron outage of any length self-heals rather than tearing a permanent hole in the history.

```bash
python update_history.py                    # normal incremental run
python update_history.py --dry-run          # fetch, merge and split locally; upload nothing
python update_history.py --full             # rebuild the whole master from the API
python update_history.py --skip-downstream  # history only
```

Because Thames occasionally edits older events, run `--full` periodically (e.g. weekly). That run is slow and may fail — but the incremental runs keep the site current in the meantime, so a failed rebuild is no longer an outage. A suggested crontab:

```cron
# Incremental history + Thames downstream impact, every 3 hours
0 */3 * * * cd $SEWAGE && $PY update_history.py >> history.log 2>&1
# Full rebuild once a week, to pick up retrospective edits
30 3 * * 0  cd $SEWAGE && $PY update_history.py --full >> history.log 2>&1
```

### History artefacts

Layout in the `thamessewage` bucket:

| Key | Purpose |
|---|---|
| `discharges_to_date/up_to_now.json` | Published discharge history, whole network |
| `discharges_to_date/up_to_now_offline.json` | Published offline-period history |
| `discharges_to_date/timestamp.txt` | Last-updated stamp |
| `discharge_histories/thames/<permit>.json` | Per-CSO discharge slices (what the site reads) |
| `discharge_histories/thames_offline/<permit>.json` | Per-CSO offline slices |
| `history_master/discharge_master.json` | Long-lived master, whole record |
| `history_master/offline_master.json` | Long-lived offline master |
| `history_master/backups/` | Dated copy of each master before it is overwritten |

The master and the slices each live under their own top-level prefix deliberately: `discharges_to_date/` is emptied wholesale before each publish, which would otherwise delete them.

The three layers are independent. The master is what makes the *update* incremental; the slices are what make the *download* small. `split_history.py` consumes whatever monolithic table was just published, so the two concerns compose without knowing about each other.

**The masters create themselves.** On a run where `history_master/` is empty, each
table is seeded from its published counterpart in `discharges_to_date/` — that file
is already a complete history in the same schema, so there is nothing to rebuild.
No manual step is needed to deploy this, and losing a master is a recoverable
inconvenience rather than a slow rebuild. Only if the published table is missing too
does a run fall back to refetching everything from the API.

`seed_history_master.py` remains for seeding a master by hand from a specific file,
and validates it (schema, date range, duplicate event keys) before uploading:

```bash
python seed_history_master.py --discharge up_to_now.json --offline up_to_now_offline.json --dry-run
```

## Source data 

The live EDM data used to map downstream sections is sourced as follows:

- In England, from the [Stream Storm Overflow Data Hub](https://www.streamwaterdata.co.uk/pages/storm-overflows-data), which is provided under a CC-BY license.
- For Thames Water, from the [Thames Water API](https://data.thameswater.co.uk/s/).
- In Scotland, from the [Scottish Water API](https://www.scottishwater.co.uk/Help-and-Resources/Open-Data/Overflow-Map-Data).

<!-- For Wales, we use data presented on the [WelshWater Storm Overflow map](https://corporate.dwrcymru.com/en/community/environment/storm-overflow-map).-->
