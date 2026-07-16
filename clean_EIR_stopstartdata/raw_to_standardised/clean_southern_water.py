"""Clean Southern Water's verified modern and CR-prefixed schema variants."""

if __package__:
    from . import common
else:
    from _bootstrap import common

COMPANY = "southern_water"
INPUT_FOLDER = "raw_data/southern_water"
OUTPUT_FILENAME = "southern_water_cleaned_data.csv"
SCHEMA_CONFIG = {
    "location": ["Overflow Name"],
    "permit": ["UniqueID", "Overflow"],
    "start_datetime": ["Start Time"],
    "stop_datetime": ["End Time"],
    "start_date": ["CR_StartDate"],
    "start_time_component": ["CR_StartTime"],
    "stop_date": ["CR_EndDate"],
    "stop_time_component": ["CR_EndTime"],
    "raw_duration": ["Discharge Period", "CR_DischargePeriod"],
    "duration_unit": "hours",
}


def clean(frame, context, load_meta):
    return common.clean_company_source(frame, COMPANY, context, load_meta)


common.register_company(COMPANY, OUTPUT_FILENAME, SCHEMA_CONFIG, clean)


def main() -> int:
    return common.main(COMPANY)


if __name__ == "__main__":
    raise SystemExit(main())
