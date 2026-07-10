# Water Company EDM JSON Pipeline

This directory converts standardised water-company EDM stop/start CSV files into
the column-oriented JSON that the sewage overflow website consumes.

For each company the script reads every CSV in its input folder, matches each
`permit_number` against the company's Stream Storm Overflow Hub API, attaches
British National Grid `X`/`Y` coordinates and a `ReceivingWaterCourse`, converts
timestamps to epoch milliseconds, and writes one JSON file.

The pipeline reads `input_stopstart_data/` and writes `outputs/`. It writes
nothing else — no intermediate CSVs, no API cache. QC is reported to stdout.

## Install requirements

```shell
pip install -r requirements.txt
```

## Input format

Each input CSV must contain exactly these columns:

```
location_name,permit_number,start_time,stop_time,duration_minutes
Example Site,AWS00009,2025-01-01 10:00:00,2025-01-01 11:15:00,75
```

Place them in the folder named after the company, where the folder name is
exactly one of the keys of `COMPANIES` in `build_water_company_json.py`:

```
input_stopstart_data/
    anglian/
    northumbrian/
    severn_trent/
    south_west_water/
    southern_water/
    united_utilities/
    wessex/
    yorkshire/
```

Every CSV inside a folder is read and concatenated, so new files can be dropped
in and picked up by rerunning the script.

`permit_number` must hold the site's Stream identifier (`AWS00009`, `YWS00231`,
`SWS00004`, …). Some EIR responses use a company's own internal site codes
instead; those rows cannot be matched and will be reported as unmatched.

## Run

Choose the companies to process by editing `ONLY_COMPANIES` near the top of
`build_water_company_json.py`. The comment beside it lists all eight, to paste
in when you want the lot.

```shell
python build_water_company_json.py
```

Or press **Run Python File** in VS Code.

## Output

One file per company, written to `outputs/{company}.json`.

The JSON is column-oriented: an outer object keyed by column name, each mapping
to an inner object keyed by row index.

```json
{
  "LocationName": {"0": "Example Site"},
  "PermitNumber": {"0": "AWS00009"},
  "X": {"0": 464190},
  "Y": {"0": 246470},
  "ReceivingWaterCourse": {"0": "THE RIVER TOVE"},
  "StartDateTime": {"0": 1735725600000},
  "StopDateTime": {"0": 1735730100000},
  "Duration": {"0": 75},
  "OngoingEvent": {"0": false}
}
```

The schema is declared by `OUTPUT_COLUMNS` in the script, which is the single
source of truth: the JSON is built from it and validated against it.

## Reading the QC output

Always read the printed output before using a JSON on the website.

Any permit that has no exact match in the Storm Overflow Hub is named
individually:

```
WARNING: !!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!
12 yorkshire permit(s) did not match the Storm Overflow Hub, affecting
3547 event row(s).
These rows have null X, Y and ReceivingWaterCourse.
!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!
WARNING: EIR ID YWS00082 not found in matching stormoverflow hub
```

A run ends with a summary table:

```
         company  total_output_rows  matched_rows  unmatched_rows  json_validation_passed
       yorkshire             694915        691368            3547                    True
united_utilities             257573        256904             669                    True
```

`json_validation_passed` only checks the *shape* of the JSON — its keys, their
order, epoch-millisecond timestamps, coordinates inside the British National
Grid, and `OngoingEvent` being boolean. It does **not** check that rows matched.
A company whose every permit failed to match still passes, because its rows have
null `X`/`Y` and null coordinates are permitted. Read `matched_rows` against
`unmatched_rows` before trusting a file.

## Notes

Matching is exact. A permit matches an API `Id` only after both are trimmed,
uppercased, and internal whitespace runs are collapsed. There is no fuzzy
fallback: a permit that differs in substance or punctuation is reported rather
than guessed at.

The APIs publish longitude/latitude, not `X`/`Y`. The script projects them to
British National Grid (EPSG:27700). Never use longitude/latitude as `X`/`Y`.

Historical EDM records are not live events, so `OngoingEvent` is `false` for
every row.

The API is fetched fresh on every run. There is no cache, so a network failure
means that company is skipped, with the traceback logged, and the run continues.
