"""Clean Anglian data using verified site-name, site-code and event-time aliases."""

if __package__:
    from . import common
else:
    from _bootstrap import common

COMPANY = "anglian"
INPUT_FOLDER = "raw_data/anglian"
OUTPUT_FILENAME = "anglian_cleaned_data.csv"
SCHEMA_CONFIG = {
    "location": ["SITE NAME", "SITENAME"],
    "permit": ["SITE CODE", "SITECODE", "UniqueID"],
    "start_datetime": ["START DATE/TIME", "START DATE TIME", "STARTDATETIME", "Start"],
    "stop_datetime": ["END DATE/TIME", "END DATE TIME", "ENDDATETIME", "Stop"],
    "raw_duration": ["DURATION", "DURATION (min)", "DURATION (MINUTES)", "Duration (hrs)"],
    "duration_unit": "minutes",
}


def clean(frame, context, load_meta):
    return common.clean_company_source(frame, COMPANY, context, load_meta)


common.register_company(COMPANY, OUTPUT_FILENAME, SCHEMA_CONFIG, clean)


def main() -> int:
    return common.main(COMPANY)


if __name__ == "__main__":
    raise SystemExit(main())
