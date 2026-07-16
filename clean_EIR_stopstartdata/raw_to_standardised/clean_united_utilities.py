"""Clean United Utilities data while preserving verified Unique ID/UUG identifiers."""

if __package__:
    from . import common
else:
    from _bootstrap import common

COMPANY = "united_utilities"
INPUT_FOLDER = "raw_data/united_utilities"
OUTPUT_FILENAME = "united_utilities_cleaned_data.csv"
SCHEMA_CONFIG = {
    "location": ["Site Name (EA consent database)", "Site Name"],
    "permit": ["Unique ID", "UUG Reference"],
    "start_datetime": ["Discharge Start (GMT)", "Spill Start Time (UTC)", "Spill Start Time", "Discharge Start"],
    "stop_datetime": ["Discharge Stop (GMT)", "Spill End Time (UTC)", "Spill End Time", "Discharge Stop"],
    "raw_duration": ["Duration (hh:mm:ss)"],
    "duration_unit": "hh:mm:ss",
}


def clean(frame, context, load_meta):
    return common.clean_company_source(frame, COMPANY, context, load_meta)


common.register_company(COMPANY, OUTPUT_FILENAME, SCHEMA_CONFIG, clean)


def main() -> int:
    return common.main(COMPANY)


if __name__ == "__main__":
    raise SystemExit(main())
