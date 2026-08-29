"""Standardise Southern Water's explicitly verified EDM source schemas.

The 2023/2025 files contain complete timestamps.  The 2021 files contain
separate date and time columns.  Both paths are written out below so that no
schema detection or header guessing is needed.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import re
from datetime import date, datetime, time, timedelta
from pathlib import Path

import pandas as pd


LABEL = "SOUTHERN WATER"
ROOT = Path(__file__).resolve().parents[2]
RAW_FOLDER = ROOT / "raw_data" / "southern_water"
OUTPUT_FOLDER = ROOT / "clean_EIR_stopstartdata" / "input_stopstart_data"
OUTPUT_FILE = OUTPUT_FOLDER / "southern_water_cleaned_data.csv"
REJECTED_FILE = OUTPUT_FOLDER / "rejected_rows" / "southern_water_rejected_rows.csv"
OUTPUT_COLUMNS = [
    "location_name", "permit_number", "start_time", "stop_time", "duration_minutes"
]
OUTPUT_TIME_FORMAT = "%Y-%m-%dT%H:%M:%S"
NULL_TEXT = {"", "nan", "none", "nat", "<na>", "null", "n/a", "na"}
LOCATION_NULL_TEXT = NULL_TEXT | {"tbc", "unknown", "not available", "not known"}
TIMESTAMP_FORMATS = [
    "%Y-%m-%dT%H:%M:%S.%fZ", "%Y-%m-%dT%H:%M:%SZ",
    "%Y-%m-%d %H:%M:%S.%f", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M",
    "%d/%m/%Y %H:%M:%S", "%d/%m/%Y %H:%M",
]

# These workbook/sheet pairs use Overflow Name, an optional permit field, and
# complete Start Time / End Time timestamp columns on header row 1.
COMBINED_TIMESTAMP_SOURCES = [
    ("ar25-individual-spill-data-for-website-upload (1).xlsx", "2025 Storm Overflows", "UniqueID"),
    ("ar25-individual-spill-data-for-website-upload (1).xlsx", "2025 Emergency Overflows", "UniqueID"),
    # The 2023 releases do not contain a verified permit column.  OTE and EA
    # Number are not silently substituted, so permit_number remains empty.
    ("individual-spill-data-2023-final (1).xlsx", "2023 Storm Overflows", None),
    ("individual-spill-data-2023-final (1).xlsx", "2023 Emergency Overflows", None),
]

# The CSV and workbook contain the same 2021 events and are both intentionally
# retained: duplicate-looking supplier events must not be deleted.
SPLIT_TIMESTAMP_SOURCES = [
    ("southernwater-water-individual-spill-data-2021.csv", None),
    ("southernwater-water-individual-spill-data-2021.xlsx", "2021 Calculated"),
]


def is_missing(value: object) -> bool:
    if value is None or value is pd.NA:
        return True
    try:
        if bool(pd.isna(value)):
            return True
    except (TypeError, ValueError):
        pass
    return str(value).strip().casefold() in NULL_TEXT


def clean_text(series: pd.Series, *, location: bool = False) -> pd.Series:
    nulls = LOCATION_NULL_TEXT if location else NULL_TEXT

    def clean(value: object) -> object:
        if is_missing(value):
            return pd.NA
        text_value = re.sub(r"\s+", " ", str(value).strip())
        return pd.NA if text_value.casefold() in nulls else text_value

    return series.map(clean).astype("string")


def clean_permits(series: pd.Series) -> pd.Series:
    return clean_text(series).str.replace(r"^([+-]?\d+)\.0$", r"\1", regex=True)


def parse_timestamps(series: pd.Series) -> pd.Series:
    parsed = pd.Series(pd.NaT, index=series.index, dtype="datetime64[ns]")
    timestamp_objects = series.map(
        lambda value: isinstance(value, (datetime, date, pd.Timestamp))
    )
    parsed.loc[timestamp_objects] = pd.to_datetime(
        series.loc[timestamp_objects], errors="coerce"
    )
    unresolved = ~timestamp_objects & ~series.map(is_missing)
    text_values = series.astype("string").str.strip()
    for timestamp_format in TIMESTAMP_FORMATS:
        converted = pd.to_datetime(
            text_values.loc[unresolved], format=timestamp_format, errors="coerce"
        )
        successful = converted.notna()
        successful_indices = converted.index[successful]
        parsed.loc[successful_indices] = converted.loc[successful_indices]
        unresolved.loc[successful_indices] = False
        if not unresolved.any():
            break
    return parsed


def render_date(value: object) -> str | None:
    if is_missing(value):
        return None
    if isinstance(value, (datetime, date, pd.Timestamp)):
        return pd.Timestamp(value).strftime("%Y-%m-%d")
    for date_format in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d", "%d/%m/%Y", "%m/%d/%Y"):
        try:
            return datetime.strptime(str(value).strip(), date_format).strftime("%Y-%m-%d")
        except ValueError:
            continue
    return None


def render_time(value: object) -> str | None:
    if is_missing(value):
        return None
    if isinstance(value, (datetime, pd.Timestamp)):
        return pd.Timestamp(value).strftime("%H:%M:%S.%f")
    if isinstance(value, time):
        return value.strftime("%H:%M:%S.%f")
    if isinstance(value, timedelta):
        seconds = value.total_seconds() % 86_400
        return f"{int(seconds // 3600):02d}:{int(seconds % 3600 // 60):02d}:{seconds % 60:09.6f}"
    if isinstance(value, (int, float)) and 0 <= float(value) < 1:
        seconds = float(value) * 86_400
        return f"{int(seconds // 3600):02d}:{int(seconds % 3600 // 60):02d}:{seconds % 60:09.6f}"
    for time_format in ("%H:%M:%S.%f", "%H:%M:%S", "%H:%M"):
        try:
            return datetime.strptime(str(value).strip(), time_format).strftime("%H:%M:%S.%f")
        except ValueError:
            continue
    return None


def combine_date_and_time(date_values: pd.Series, time_values: pd.Series) -> pd.Series:
    combined = []
    for date_value, time_value in zip(date_values, time_values):
        date_text = render_date(date_value)
        time_text = render_time(time_value)
        combined.append(f"{date_text} {time_text}" if date_text and time_text else pd.NA)
    return pd.Series(combined, index=date_values.index, dtype="string")


def add_reason(reasons: pd.Series, mask: pd.Series, reason: str) -> None:
    reasons.loc[mask] = reasons.loc[mask].map(
        lambda existing: f"{existing};{reason}" if existing else reason
    )


def validate_columns(data: pd.DataFrame, required: list[str], source: str) -> None:
    missing_columns = []
    for column in required:
        if column not in data.columns:
            missing_columns.append(column)
    if missing_columns:
        raise KeyError(
            f"Expected columns are missing from {source}: {missing_columns}. "
            "The supplier schema may have changed."
        )


def clean_selected_rows(
    selected: pd.DataFrame,
    *,
    source_file: str,
    source_sheet: str,
    permit_column_present: bool,
) -> tuple[pd.DataFrame, pd.DataFrame, dict]:
    core_columns = ["location_original", "start_original", "stop_original"]
    if permit_column_present:
        core_columns.append("permit_original")
    structurally_blank = pd.Series(True, index=selected.index)
    for column in core_columns:
        structurally_blank &= selected[column].map(is_missing)
    selected = selected.loc[~structurally_blank].copy()

    location = clean_text(selected["location_original"], location=True)
    if permit_column_present:
        permit = clean_permits(selected["permit_original"])
    else:
        permit = pd.Series(pd.NA, index=selected.index, dtype="string")
    start = parse_timestamps(selected["start_original"])
    stop = parse_timestamps(selected["stop_original"])
    duration = ((stop - start).dt.total_seconds() / 60).round(6)

    missing_start = selected["start_original"].map(is_missing)
    missing_stop = selected["stop_original"].map(is_missing)
    start_failure = ~missing_start & start.isna()
    stop_failure = ~missing_stop & stop.isna()
    negative_duration = start.notna() & stop.notna() & stop.lt(start)
    non_finite_duration = duration.notna() & ~duration.map(
        lambda value: math.isfinite(float(value))
    )
    reasons = pd.Series("", index=selected.index, dtype="string")
    add_reason(reasons, missing_start, "missing_start_time")
    add_reason(reasons, missing_stop, "missing_stop_time")
    add_reason(reasons, start_failure, "unparseable_start_time")
    add_reason(reasons, stop_failure, "unparseable_stop_time")
    add_reason(reasons, negative_duration, "negative_duration")
    add_reason(reasons, non_finite_duration, "non_finite_duration")
    rejected_mask = reasons.ne("")
    retained = ~rejected_mask

    cleaned = pd.DataFrame({
        "location_name": location.loc[retained],
        "permit_number": permit.loc[retained],
        "start_time": start.loc[retained].dt.strftime(OUTPUT_TIME_FORMAT),
        "stop_time": stop.loc[retained].dt.strftime(OUTPUT_TIME_FORMAT),
        "duration_minutes": duration.loc[retained].astype(float),
    })
    rejected_records = []
    original_columns = [column for column in selected.columns if column.endswith("_original")]
    for index in selected.index[rejected_mask]:
        originals = {column: selected.at[index, column] for column in original_columns}
        rejected_records.append({
            "company": "southern_water",
            "source_file": f"raw_data/southern_water/{source_file}",
            "source_sheet": source_sheet,
            "source_row_number": int(selected.at[index, "source_row_number"]),
            "relevant original source values": json.dumps(originals, ensure_ascii=False, default=str),
            "rejection_reason": reasons.at[index],
        })
    rejected = pd.DataFrame(rejected_records, columns=[
        "company", "source_file", "source_sheet", "source_row_number",
        "relevant original source values", "rejection_reason",
    ])
    summary = {
        "retained": len(cleaned), "rejected": len(rejected),
        "start_failures": int(start_failure.sum()),
        "stop_failures": int(stop_failure.sum()),
        "negative": int(negative_duration.sum()),
    }
    return cleaned, rejected, summary


def read_combined_timestamp_source(
    filename: str, sheet_name: str, permit_column: str | None
) -> tuple[pd.DataFrame, pd.DataFrame, dict]:
    print(f"[{LABEL}] Reading: {filename} [{sheet_name}]")
    data = pd.read_excel(RAW_FOLDER / filename, sheet_name=sheet_name, header=0, dtype=object)
    rows_loaded = len(data)
    required = ["Overflow Name", "Start Time", "End Time", "Discharge Period"]
    if permit_column:
        required.append(permit_column)
    validate_columns(data, required, f"{filename} [{sheet_name}]")
    selected_columns = required + []
    selected = data[selected_columns].copy()
    selected["source_row_number"] = range(2, rows_loaded + 2)
    rename = {
        "Overflow Name": "location_original", "Start Time": "start_original",
        "End Time": "stop_original", "Discharge Period": "duration_original",
    }
    if permit_column:
        rename[permit_column] = "permit_original"
    selected = selected.rename(columns=rename)
    cleaned, rejected, summary = clean_selected_rows(
        selected, source_file=filename, source_sheet=sheet_name,
        permit_column_present=permit_column is not None,
    )
    summary["loaded"] = rows_loaded
    print(f"[{LABEL}] Rows loaded: {rows_loaded:,}")
    print(f"[{LABEL}] Rows retained: {len(cleaned):,}")
    print(f"[{LABEL}] Rows rejected: {len(rejected):,}")
    return cleaned, rejected, summary


def read_split_timestamp_source(
    filename: str, sheet_name: str | None
) -> tuple[pd.DataFrame, pd.DataFrame, dict]:
    source_description = filename if sheet_name is None else f"{filename} [{sheet_name}]"
    print(f"[{LABEL}] Reading: {source_description}")
    if sheet_name is None:
        data = pd.read_csv(RAW_FOLDER / filename, header=0, dtype=object)
    else:
        data = pd.read_excel(RAW_FOLDER / filename, sheet_name=sheet_name, header=0, dtype=object)
    rows_loaded = len(data)
    required = [
        "Overflow", "Overflow Name", "CR_StartDate", "CR_StartTime",
        "CR_EndDate", "CR_EndTime", "CR_DischargePeriod",
    ]
    validate_columns(data, required, source_description)
    selected = data[required].copy()
    selected["source_row_number"] = range(2, rows_loaded + 2)
    selected["start_original"] = combine_date_and_time(
        selected["CR_StartDate"], selected["CR_StartTime"]
    )
    selected["stop_original"] = combine_date_and_time(
        selected["CR_EndDate"], selected["CR_EndTime"]
    )
    selected = selected.rename(columns={
        "Overflow": "permit_original", "Overflow Name": "location_original",
        "CR_DischargePeriod": "duration_original",
    })
    cleaned, rejected, summary = clean_selected_rows(
        selected, source_file=filename, source_sheet=sheet_name or "",
        permit_column_present=True,
    )
    summary["loaded"] = rows_loaded
    print(f"[{LABEL}] Rows loaded: {rows_loaded:,}")
    print(f"[{LABEL}] Rows retained: {len(cleaned):,}")
    print(f"[{LABEL}] Rows rejected: {len(rejected):,}")
    return cleaned, rejected, summary


def validate_output(data: pd.DataFrame) -> None:
    if list(data.columns) != OUTPUT_COLUMNS or data.empty:
        raise ValueError("Southern Water output does not satisfy the five-column contract.")
    start = pd.to_datetime(data["start_time"], format=OUTPUT_TIME_FORMAT, errors="coerce")
    stop = pd.to_datetime(data["stop_time"], format=OUTPUT_TIME_FORMAT, errors="coerce")
    duration = pd.to_numeric(data["duration_minutes"], errors="coerce")
    if start.isna().any() or stop.isna().any() or stop.lt(start).any():
        raise ValueError("Southern Water output contains an invalid timestamp pair.")
    if duration.isna().any() or duration.lt(0).any() or not duration.map(math.isfinite).all():
        raise ValueError("Southern Water output contains an invalid duration.")
    expected = (stop - start).dt.total_seconds() / 60
    if duration.sub(expected).abs().gt((1 / 60) + 1e-9).any():
        raise ValueError("Southern Water duration differs from timestamps by over one second.")


def clean_southern_water(*, dry_run: bool = False, strict: bool = False) -> int:
    cleaned_frames = []
    rejected_frames = []
    summaries = []
    for filename, sheet_name, permit_column in COMBINED_TIMESTAMP_SOURCES:
        cleaned, rejected, summary = read_combined_timestamp_source(filename, sheet_name, permit_column)
        cleaned_frames.append(cleaned); rejected_frames.append(rejected); summaries.append(summary)
    for filename, sheet_name in SPLIT_TIMESTAMP_SOURCES:
        cleaned, rejected, summary = read_split_timestamp_source(filename, sheet_name)
        cleaned_frames.append(cleaned); rejected_frames.append(rejected); summaries.append(summary)

    combined = pd.concat(cleaned_frames, ignore_index=True)
    combined = combined.sort_values(
        ["start_time", "stop_time", "permit_number", "location_name"], kind="mergesort"
    ).reset_index(drop=True)[OUTPUT_COLUMNS]
    validate_output(combined)
    rejected = pd.concat(rejected_frames, ignore_index=True)
    if not dry_run:
        OUTPUT_FOLDER.mkdir(parents=True, exist_ok=True)
        REJECTED_FILE.parent.mkdir(parents=True, exist_ok=True)
        temporary = OUTPUT_FILE.with_name(f"{OUTPUT_FILE.name}.tmp-{os.getpid()}")
        combined.to_csv(temporary, index=False, encoding="utf-8", na_rep="")
        temporary.replace(OUTPUT_FILE)
        rejected.to_csv(REJECTED_FILE, index=False, encoding="utf-8")

    print(f"[{LABEL}] Files processed: {len(COMBINED_TIMESTAMP_SOURCES) + len(SPLIT_TIMESTAMP_SOURCES)} source blocks")
    print(f"[{LABEL}] Total rows loaded: {sum(s['loaded'] for s in summaries):,}")
    print(f"[{LABEL}] Total rows retained: {len(combined):,}")
    print(f"[{LABEL}] Total rows rejected: {len(rejected):,}")
    print(f"[{LABEL}] Start parse failures: {sum(s['start_failures'] for s in summaries):,}")
    print(f"[{LABEL}] Stop parse failures: {sum(s['stop_failures'] for s in summaries):,}")
    print(f"[{LABEL}] Negative durations: {sum(s['negative'] for s in summaries):,}")
    if dry_run:
        print(f"[{LABEL}] Dry run: no files written")
    else:
        print(f"[{LABEL}] Output written to: {OUTPUT_FILE}")
        print(f"[{LABEL}] Rejected rows written to: {REJECTED_FILE}")
    return 1 if strict and len(rejected) else 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--strict", action="store_true")
    parser.add_argument("--self-check", action="store_true")
    arguments = parser.parse_args()
    if arguments.self_check:
        assert parse_timestamps(pd.Series(["2025-01-02T03:04:05Z"])).notna().all()
        assert combine_date_and_time(pd.Series([datetime(2021, 1, 2)]), pd.Series([time(3, 4, 5)])).iloc[0] == "2021-01-02 03:04:05.000000"
        print(f"[{LABEL}] Self-check passed")
        return 0
    return clean_southern_water(dry_run=arguments.dry_run, strict=arguments.strict)


if __name__ == "__main__":
    raise SystemExit(main())
