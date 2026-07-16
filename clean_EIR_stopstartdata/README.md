# EDM stop/start pipeline

This folder contains the two-step historical spill pipeline:

1. Clean raw company files into eight standardised CSVs.
2. Match permits to the Storm Overflow Hub APIs and build website JSON.

## Setup

From the repository root in Windows PowerShell:

```powershell
python -m venv .venv
.venv\Scripts\Activate.ps1
python -m pip install -r clean_EIR_stopstartdata/requirements.txt
```

Place `.csv` or `.xlsx` files under the matching folder. Nested folders are
discovered and source files are never modified. Legacy `.xls` requires the
optional `xlrd` package; other workbook formats need explicit support first.

```text
raw_data/
  anglian/
  northumbria/
  severn_trent/
  south_west_water/
  southern_water/
  united_utilities/
  wessex/
  yorkshire/
```

## Clean raw data

Clean every company:

```powershell
python clean_EIR_stopstartdata/standardise_stopstart_data.py
```

Clean one company without changing the other seven outputs:

```powershell
python clean_EIR_stopstartdata/raw_to_standardised/clean_anglian.py
python clean_EIR_stopstartdata/raw_to_standardised/clean_northumbria.py
python clean_EIR_stopstartdata/raw_to_standardised/clean_severn_trent.py
python clean_EIR_stopstartdata/raw_to_standardised/clean_south_west_water.py
python clean_EIR_stopstartdata/raw_to_standardised/clean_southern_water.py
python clean_EIR_stopstartdata/raw_to_standardised/clean_united_utilities.py
python clean_EIR_stopstartdata/raw_to_standardised/clean_wessex.py
python clean_EIR_stopstartdata/raw_to_standardised/clean_yorkshire.py
```

Use `--dry-run` to validate without replacing CSVs, `--strict` for stricter
exit checks, or `--self-check` for built-in tests. The eight outputs replace
the files in `clean_EIR_stopstartdata/input_stopstart_data/` and always use:

```text
location_name,permit_number,start_time,stop_time,duration_minutes
```

Each row remains an individual event; repeated permits are not aggregated.

## Build website JSON

Build all eight JSON files:

```powershell
python clean_EIR_stopstartdata/build_water_company_jsons.py
```

Useful alternatives:

```powershell
python clean_EIR_stopstartdata/build_water_company_jsons.py --company united_utilities
python clean_EIR_stopstartdata/build_water_company_jsons.py --no-progress
python clean_EIR_stopstartdata/build_water_company_jsons.py --self-check
```

JSON is written to `clean_EIR_stopstartdata/output_jsons/`. It is
column-oriented with these ordered keys:

```text
LocationName, PermitNumber, X, Y, ReceivingWaterCourse,
StartDateTime, StopDateTime, Duration, OngoingEvent
```

Only complete, valid events with one safe exact API permit match enter JSON.
Every excluded event is written to
`clean_EIR_stopstartdata/unmatched_spill_events.txt`. For each company:

```text
input CSV rows = JSON rows + excluded report rows
```

Raw files, generated CSVs, JSON and reports are intentionally Gitignored. Add
new raw files to the correct company folder and rerun the relevant cleaner,
then rerun the JSON builder.

## Git data policy

The GitHub repository contains code, documentation and empty folder
placeholders only. Raw data must be obtained independently. These populated
paths stay local and remain usable by the scripts:

```text
raw_data/
clean_EIR_stopstartdata/input_stopstart_data/
clean_EIR_stopstartdata/output_jsons/
json_cleaning_python_pipeline/outputs/
```

Standardised CSVs, generated JSON, API caches, QC output and
`unmatched_spill_events.txt` must not be committed. Do not use `git add -f` to
bypass the ignore rules.
