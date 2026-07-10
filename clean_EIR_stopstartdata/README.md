# Water Company EDM JSON Pipeline

This directory turns historical water-company EDM stop/start records, obtained
through EIR requests, into the column-oriented JSON that the sewage overflow
website consumes.

The EDM CSVs describe *when* a discharge happened but not *where*. The Stream
Storm Overflow Hub supplies the missing half: for each permit number, a position
and the watercourse it discharges into. `build_water_company_jsons.py` joins the
two, projects the coordinates onto the British National Grid, converts the
timestamps to epoch milliseconds, and writes one JSON file per company.

It reads `input_stopstart_data/` and writes `output_jsons/`. Nothing else is
written — no intermediate CSVs, no API cache. QC is reported to stdout.

## Install requirements

```shell
pip install -r requirements.txt
```

## Prepare the inputs

Each input CSV must contain exactly these columns:

```
location_name,permit_number,start_time,stop_time,duration_minutes
Example Site,AWS00009,2025-01-01T10:00:00,2025-01-01T11:15:00,75
```

Place them in the folder named after the company, where the folder name is
exactly one of the keys of `COMPANIES` in `build_water_company_jsons.py`:

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
in and picked up by rerunning the script. Filenames are not significant.

`start_time` and `stop_time` must be ISO-8601 without a timezone offset. They are
read as UK local time with British Summer Time already applied, which is how the
companies publish them. Fractional seconds are accepted.

`permit_number` must hold the site's Stream identifier — `AWS00009`, `YWS00231`,
`SWS00004`. Some EIR responses instead use a company's own internal site codes;
those rows cannot be matched, and the script will name each one.

## Run the script

Choose the companies to process by editing `ONLY_COMPANIES` near the top of
`build_water_company_jsons.py`. The comment beside it lists all eight, to paste
in when you want the lot.

```shell
python build_water_company_jsons.py
```

Or press **Run Python File** in VS Code.

## Output

One file per company, at `output_jsons/{company}.json`.

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
`OngoingEvent` is `false` on every row, because these are historical records.

## Reading the QC output

The script transcribes rather than corrects, and reports anything it could not
reconcile. Always read the output before publishing a file.

A permit with no exact match in the Storm Overflow Hub is named individually.
Its event rows are kept, with null `X`, `Y` and `ReceivingWaterCourse`:

```
WARNING: !!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!
12 yorkshire permit(s) did not match the Storm Overflow Hub, affecting
3547 event row(s).
These rows have null X, Y and ReceivingWaterCourse.
!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!
WARNING: EIR ID YWS00082 not found in matching stormoverflow hub
```

An ID the Hub publishes more than once is reported in full, showing every
occurrence, and resolved by taking the first. A site the company has placed
outside the British National Grid is reported and passed through as published.

A run ends with a summary table:

```
         company  total_output_rows  matched_rows  unmatched_rows  json_validation_passed
       yorkshire             694915        691368            3547                    True
united_utilities             257573        256904             669                    True
```

`json_validation_passed` only checks the *shape* of the JSON — its keys, their
order, epoch-millisecond timestamps, numeric coordinates, and `OngoingEvent`
being boolean. It does **not** check that rows matched. A company whose every
permit failed to match still passes, because null coordinates are permitted.
Read `matched_rows` against `unmatched_rows` before trusting a file.

## Notes on matching

Matching is exact. A permit matches an API `Id` only after both have been
trimmed, uppercased, and had internal whitespace runs collapsed. There is no
fuzzy fallback: a permit that differs in substance or punctuation is reported
rather than guessed at.

The APIs publish longitude/latitude, not `X`/`Y`. The script projects them to
British National Grid (EPSG:27700). Never use longitude/latitude as `X`/`Y`.

The API is fetched fresh on every run. There is no cache, so a network failure
means that company is skipped, with the traceback logged, and the run continues
to the next.

---

# Data sources and transparency

The EDM start-and-stop records processed here come from eight water companies:
Anglian, Northumbrian, Severn Trent, South West Water, Southern, United
Utilities, Wessex and Yorkshire.

The available data mainly covers 2024 to 2026. We prioritised these recent
datasets because they carry the most up-to-date unique identifiers for Combined
Sewer Overflow (CSO) monitoring sites, which is what lets each EDM record be
matched to a CSO location through the StormHub ArcGIS APIs.

We have also requested historical data from before 2024, asking the companies to
supply those older start-and-stop records against the *current* CSO identifiers,
so that older discharge events can be linked to the right monitoring locations
and shown over a longer period.

We have additionally requested information on periods when CSO sensors were
inactive or unavailable. Showing these on the SewageMap website matters, because
a gap in recorded discharge activity may reflect sensor downtime rather than an
absence of overflows.

The sources below record where the original EDM data came from and how each
site's location and receiving watercourse were obtained, so that the path from
published record to final JSON is traceable.

## Water company EDM data sources

| Company | Source |
| --- | --- |
| Anglian Water | https://www.anglianwater.co.uk/environment/storm-overflows/monthly-edm-publication |
| Northumbrian Water | https://ckan.publishing.service.gov.uk/dataset/event-duration-monitoring-storm-overflow-start-stop-detailed-data |
| Severn Trent Water | https://www.stwater.co.uk/get-river-positive/event-duration-monitor-edm-report-5/ |
| Southern Water | https://www.southernwater.co.uk/about-us/environmental-performance/healthy-rivers-and-seas/flow-and-spill-reporting/#flowdata |
| South West Water | https://www.southwestwater.co.uk/environment/rivers-and-bathing-waters/waterfitlive/storm-overflow-map |
| United Utilities | https://www.unitedutilities.com/better-rivers/our-challenges/storm-overflow-performance/ |
| Wessex Water | https://corporate.wessexwater.co.uk/our-purpose/rivers-and-coastal-waters/storm-overflows |
| Yorkshire Water | https://www.yorkshirewater.com/environment/river-health/storm-overflow-investment/event-duration-monitoring/ |

## StormHub API sources

These StormHub ArcGIS endpoints supply each CSO site's geographical location and
receiving watercourse. They are the values of `COMPANIES` in
`build_water_company_jsons.py`.

| Company | Endpoint |
| --- | --- |
| Anglian Water | https://services3.arcgis.com/VCOY1atHWVcDlvlJ/arcgis/rest/services/stream_service_outfall_locations_view/FeatureServer/0/query |
| Northumbrian Water | https://services-eu1.arcgis.com/MSNNjkZ51iVh8yBj/arcgis/rest/services/Northumbrian_Water_Storm_Overflow_Activity_2_view/FeatureServer/0/query |
| Severn Trent Water | https://services1.arcgis.com/NO7lTIlnxRMMG9Gw/arcgis/rest/services/Severn_Trent_Water_Storm_Overflow_Activity/FeatureServer/0/query |
| Southern Water | https://services-eu1.arcgis.com/6qJmARkS2dt2IjVA/arcgis/rest/services/SouthernWater_StormOverflowActivity_PROD_view/FeatureServer/0/query |
| South West Water | https://services-eu1.arcgis.com/OMdMOtfhATJPcHe3/arcgis/rest/services/NEH_outlets_PROD/FeatureServer/0/query?outFields=*&where=1%3D1&f=geojson |
| United Utilities | https://services5.arcgis.com/5eoLvR0f8HKb7HWP/arcgis/rest/services/United_Utilities_Storm_Overflow_Activity/FeatureServer/0/query |
| Wessex Water | https://services.arcgis.com/3SZ6e0uCvPROr4mS/arcgis/rest/services/Wessex_Water_Storm_Overflow_Activity/FeatureServer/0/query |
| Yorkshire Water | https://services-eu1.arcgis.com/1WqkK5cDKUbF0CkH/arcgis/rest/services/Yorkshire_Water_Storm_Overflow_Activity/FeatureServer/0/query |
