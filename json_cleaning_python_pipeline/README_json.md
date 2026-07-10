# Water Company EDM JSON Pipeline

This project converts standardised water-company EDM start/stop CSV files into Thames-compatible JSON files for the sewage overflow website.

The script reads all CSV files from each company folder, matches each `permit_number` to company StormHub/API data, adds British National Grid `X` and `Y` coordinates, adds `ReceivingWaterCourse`, converts dates to epoch milliseconds, and outputs enriched CSV and JSON files.

## Required input format

Each input CSV must contain these columns exactly:

location_name
permit_number
start_time
stop_time
duration_minutes

Example:

location_name,permit_number,start_time,stop_time,duration_minutes
Example Site,AWS00009,2025-01-01 10:00:00,2025-01-01 11:15:00,75

Place CSV files in the correct company folder:

standardised_data/anglian_data/
standardised_data/northumbrian_data/
standardised_data/severn_trent_data/
standardised_data/southern_water_data/
standardised_data/united_utilities_data/
standardised_data/wessex_data/
standardised_data/yorkshire_data/

The script automatically reads every CSV inside each folder, so new standardised files can be added and processed by rerunning the script.

```



## Install requirements
pip install -r requirements.txt


Required packages:

pandas
requests
pyproj


## Run the script
python build_water_company_json.py

Or press **Run Python File** in VS Code.

At the top of `build_water_company_json.py`, choose which companies to run:

ONLY_COMPANIES = ["anglian"]


Examples:


ONLY_COMPANIES = ["northumbrian"]
ONLY_COMPANIES = ["anglian", "northumbrian", "severn_trent"]
ONLY_COMPANIES = None  # runs all companies



The script creates:

outputs/
    csv_clean/
    json/
    qc/
    api_cache/


Enriched CSV output:

outputs/csv_clean/{company}_enriched.csv


JSON output:

outputs/json/{company}.json


QC outputs:

outputs/qc/{company}_summary.csv
outputs/qc/{company}_match_report.csv
outputs/qc/overall_summary.csv



## Final output schema
Both the enriched CSV and JSON use these final columns:


LocationName
PermitNumber
X
Y
ReceivingWaterCourse
StartDateTime
StopDateTime
Duration
OngoingEvent


The JSON is Thames-compatible and column-oriented:


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


## QC checks

Always inspect the QC files before using the JSON on the website.

The QC reports show matched permits, unmatched permits, missing coordinates, missing watercourses, bad dates, and JSON validation status.


Important Notes

The APIs often provide longitude/latitude, not Thames-style `X/Y`.

The script converts longitude/latitude into British National Grid coordinates using EPSG:27700.

Do not manually use longitude/latitude as `X/Y`.

Historical EDM records are not live events, so `OngoingEvent` is set to `false` for every row.

