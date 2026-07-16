"""Clean Northumbrian data using its verified detailed-EDM schema."""

if __package__:
    from . import common
else:
    from _bootstrap import common

COMPANY = "northumbria"
INPUT_FOLDER = "raw_data/northumbria"
OUTPUT_FILENAME = "northumbria_cleaned_data.csv"
SCHEMA_CONFIG = {
    "location": ["Site name"],
    "permit": ["Unique ID"],
    "start_datetime": ["Discharge start (GMT)"],
    "stop_datetime": ["Discharge end (GMT)"],
}


def clean(frame, context, load_meta):
    return common.clean_company_source(frame, COMPANY, context, load_meta)


common.register_company(COMPANY, OUTPUT_FILENAME, SCHEMA_CONFIG, clean)


def main() -> int:
    return common.main(COMPANY)


if __name__ == "__main__":
    raise SystemExit(main())
