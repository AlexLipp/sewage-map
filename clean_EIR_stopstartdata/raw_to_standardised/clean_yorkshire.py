"""Clean Yorkshire's verified target-schema event data and hour durations."""

if __package__:
    from . import common
else:
    from _bootstrap import common

COMPANY = "yorkshire"
INPUT_FOLDER = "raw_data/yorkshire"
OUTPUT_FILENAME = "yorkshire_cleaned_data.csv"
SCHEMA_CONFIG = {
    "location": ["Site Name"],
    "permit": ["Unique ID"],
    "start_datetime": ["Discharge Start (GMT)"],
    "stop_datetime": ["Discharge Stop (GMT)"],
    "raw_duration": ["Event Duration (Hours)", "Duration of Spill (hrs)"],
    "duration_unit": "hours",
}


def clean(frame, context, load_meta):
    return common.clean_company_source(frame, COMPANY, context, load_meta)


common.register_company(COMPANY, OUTPUT_FILENAME, SCHEMA_CONFIG, clean)


def main() -> int:
    return common.main(COMPANY)


if __name__ == "__main__":
    raise SystemExit(main())
