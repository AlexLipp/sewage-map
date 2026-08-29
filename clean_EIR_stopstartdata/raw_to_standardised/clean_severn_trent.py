"""Clean Severn Trent data, preferring EA-consents names with WaSC fallback."""

if __package__:
    from . import common
else:
    from _bootstrap import common

COMPANY = "severn_trent"
INPUT_FOLDER = "raw_data/severn_trent"
OUTPUT_FILENAME = "severn_trent_cleaned_data.csv"
SCHEMA_CONFIG = {
    "location": ["Site Name (EA Consents Database)", "Site Name"],
    "location_fallback": ["Site Name (WaSC operational)"],
    "permit": ["Unique ID"],
    "start_datetime": ["Discharge Start (GMT)"],
    "stop_datetime": ["Discharge Stop (GMT)"],
}


def clean(frame, context, load_meta):
    return common.clean_company_source(frame, COMPANY, context, load_meta)


common.register_company(COMPANY, OUTPUT_FILENAME, SCHEMA_CONFIG, clean)


def main() -> int:
    return common.main(COMPANY)


if __name__ == "__main__":
    raise SystemExit(main())
