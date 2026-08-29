"""Clean Wessex discharge-time worksheets and hour-based duration fields."""

if __package__:
    from . import common
else:
    from _bootstrap import common

COMPANY = "wessex"
INPUT_FOLDER = "raw_data/wessex"
OUTPUT_FILENAME = "wessex_cleaned_data.csv"
SCHEMA_CONFIG = {
    "location": ["SO Name"],
    "permit": ["Unique ID"],
    "start_datetime": ["Discharge Start"],
    "stop_datetime": ["Discharge End"],
    "raw_duration": ["Total Duration (Hrs)"],
    "duration_unit": "hours",
}


def clean(frame, context, load_meta):
    return common.clean_company_source(frame, COMPANY, context, load_meta)


common.register_company(COMPANY, OUTPUT_FILENAME, SCHEMA_CONFIG, clean)


def main() -> int:
    return common.main(COMPANY)


if __name__ == "__main__":
    raise SystemExit(main())
