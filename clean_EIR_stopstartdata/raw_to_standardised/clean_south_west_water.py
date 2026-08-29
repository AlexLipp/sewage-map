"""Clean South West Water date/time columns with verified WASC-name fallback."""

if __package__:
    from . import common
else:
    from _bootstrap import common

COMPANY = "south_west_water"
INPUT_FOLDER = "raw_data/south_west_water"
OUTPUT_FILENAME = "south_west_water_cleaned_data.csv"
SCHEMA_CONFIG = {
    "location": ["Overflow Name"],
    "location_fallback": ["WASC Name"],
    "permit": ["Unique ID"],
    "start_date": ["Discharge Start Date"],
    "start_time_component": ["Discharge Start Time"],
    "stop_date": ["Discharge Stop Date"],
    "stop_time_component": ["Discharge Stop Time"],
}


def clean(frame, context, load_meta):
    return common.clean_company_source(frame, COMPANY, context, load_meta)


common.register_company(COMPANY, OUTPUT_FILENAME, SCHEMA_CONFIG, clean)


def main() -> int:
    return common.main(COMPANY)


if __name__ == "__main__":
    raise SystemExit(main())
