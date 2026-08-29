"""Standardise South West Water's split date/time EDM workbooks.

All seven known workbooks use header row 1 on the explicitly named Data sheet.
Their one-cell ReadMe/Read Me sheets are structural and are not processed.
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


LABEL = "SOUTH WEST WATER"
ROOT = Path(__file__).resolve().parents[2]
RAW_FOLDER = ROOT / "raw_data" / "south_west_water"
OUTPUT_FOLDER = ROOT / "clean_EIR_stopstartdata" / "input_stopstart_data"
OUTPUT_FILE = OUTPUT_FOLDER / "south_west_water_cleaned_data.csv"
REJECTED_FILE = OUTPUT_FOLDER / "rejected_rows" / "south_west_water_rejected_rows.csv"
EVENT_SHEET = "Data"
SOURCE_FILES = [
    "sww-2020-edm-start-stops---storm-overflows.xlsx",
    "sww-2021-edm-start-stops---storm-overflows.xlsx",
    "sww-2022-edm-start-stop---storm-overflows.xlsx",
    "sww-2023-edm-start-stop---storm-overflows.xlsx",
    "sww-2024-edm-start-stop---storm-overflows.xlsx",
    "sww-2025-edm-start-stop---storm-overflows.xlsx",
    "sww-2026-edm-start-stop---storm-overflows2 (2).xlsx",
]
SOURCE_COLUMNS = [
    "Overflow Name", "Unique ID", "WASC Name",
    "Discharge Start Date", "Discharge Start Time",
    "Discharge Stop Date", "Discharge Stop Time",
]
OUTPUT_COLUMNS = [
    "location_name", "permit_number", "start_time", "stop_time", "duration_minutes"
]
OUTPUT_TIME_FORMAT = "%Y-%m-%dT%H:%M:%S"
NULL_TEXT = {"", "nan", "none", "nat", "<na>", "null", "n/a", "na"}
LOCATION_NULLS = NULL_TEXT | {"tbc", "unknown", "not available", "not known"}
TIMESTAMP_FORMATS = [
    "%Y-%m-%dT%H:%M:%S.%fZ", "%Y-%m-%dT%H:%M:%SZ",
    "%Y-%m-%d %H:%M:%S.%f", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M",
    "%d/%m/%Y %H:%M:%S", "%d/%m/%Y %H:%M",
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
    nulls = LOCATION_NULLS if location else NULL_TEXT
    def clean(value: object) -> object:
        if is_missing(value):
            return pd.NA
        text = re.sub(r"\s+", " ", str(value).strip())
        return pd.NA if text.casefold() in nulls else text
    return series.map(clean).astype("string")


def clean_permits(series: pd.Series) -> pd.Series:
    return clean_text(series).str.replace(r"^([+-]?\d+)\.0$", r"\1", regex=True)


def render_date(value: object) -> str | None:
    if is_missing(value):
        return None
    if isinstance(value, (datetime, date, pd.Timestamp)):
        return pd.Timestamp(value).strftime("%Y-%m-%d")
    text = str(value).strip()
    for date_format in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d", "%d/%m/%Y", "%m/%d/%Y"):
        try:
            return datetime.strptime(text, date_format).strftime("%Y-%m-%d")
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
    text = str(value).strip()
    for time_format in ("%H:%M:%S.%f", "%H:%M:%S", "%H:%M"):
        try:
            return datetime.strptime(text, time_format).strftime("%H:%M:%S.%f")
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


def parse_timestamps(series: pd.Series) -> pd.Series:
    result = pd.Series(pd.NaT, index=series.index, dtype="datetime64[ns]")
    timestamp_objects = series.map(
        lambda value: isinstance(value, (datetime, date, pd.Timestamp))
    )
    result.loc[timestamp_objects] = pd.to_datetime(series.loc[timestamp_objects], errors="coerce")
    unresolved = ~timestamp_objects & ~series.map(is_missing)
    text = series.astype("string").str.strip()
    for timestamp_format in TIMESTAMP_FORMATS:
        converted = pd.to_datetime(text.loc[unresolved], format=timestamp_format, errors="coerce")
        successful = converted.notna()
        indices = converted.index[successful]
        result.loc[indices] = converted.loc[indices]
        unresolved.loc[indices] = False
        if not unresolved.any():
            break
    return result


def add_reason(reasons: pd.Series, mask: pd.Series, reason: str) -> None:
    reasons.loc[mask] = reasons.loc[mask].map(
        lambda current: f"{current};{reason}" if current else reason
    )


def require_source_columns(data: pd.DataFrame, filename: str) -> None:
    missing_columns = []
    for column in SOURCE_COLUMNS:
        if column not in data.columns:
            missing_columns.append(column)
    if missing_columns:
        raise KeyError(
            f"Expected columns are missing from {filename} [{EVENT_SHEET}]: "
            f"{missing_columns}. The supplier schema may have changed."
        )


def clean_workbook(filename: str) -> tuple[pd.DataFrame, pd.DataFrame, dict]:
    print(f"[{LABEL}] Reading: {filename} [{EVENT_SHEET}]")
    data = pd.read_excel(RAW_FOLDER / filename, sheet_name=EVENT_SHEET, header=0, dtype=object)
    rows_loaded = len(data)
    require_source_columns(data, filename)
    selected = data[SOURCE_COLUMNS].copy()
    selected["source_row_number"] = range(2, rows_loaded + 2)
    selected = selected.rename(columns={
        "Overflow Name": "location_original", "WASC Name": "fallback_location_original",
        "Unique ID": "permit_original", "Discharge Start Date": "start_date_original",
        "Discharge Start Time": "start_time_original", "Discharge Stop Date": "stop_date_original",
        "Discharge Stop Time": "stop_time_original",
    })

    structurally_blank = pd.Series(True, index=selected.index)
    for column in (
        "location_original", "fallback_location_original", "permit_original",
        "start_date_original", "start_time_original", "stop_date_original", "stop_time_original",
    ):
        structurally_blank &= selected[column].map(is_missing)
    selected = selected.loc[~structurally_blank].copy()

    location = clean_text(selected["location_original"], location=True)
    fallback_location = clean_text(selected["fallback_location_original"], location=True)
    fallback_rows = location.isna() & fallback_location.notna()
    location.loc[fallback_rows] = fallback_location.loc[fallback_rows]
    permit = clean_permits(selected["permit_original"])
    raw_start = combine_date_and_time(selected["start_date_original"], selected["start_time_original"])
    raw_stop = combine_date_and_time(selected["stop_date_original"], selected["stop_time_original"])
    start = parse_timestamps(raw_start)
    stop = parse_timestamps(raw_stop)
    duration = ((stop - start).dt.total_seconds() / 60).round(6)

    missing_start = raw_start.map(is_missing)
    missing_stop = raw_stop.map(is_missing)
    start_failure = ~missing_start & start.isna()
    stop_failure = ~missing_stop & stop.isna()
    negative = start.notna() & stop.notna() & stop.lt(start)
    non_finite = duration.notna() & ~duration.map(lambda value: math.isfinite(float(value)))
    reasons = pd.Series("", index=selected.index, dtype="string")
    add_reason(reasons, missing_start, "missing_start_time")
    add_reason(reasons, missing_stop, "missing_stop_time")
    add_reason(reasons, start_failure, "unparseable_start_time")
    add_reason(reasons, stop_failure, "unparseable_stop_time")
    add_reason(reasons, negative, "negative_duration")
    add_reason(reasons, non_finite, "non_finite_duration")
    rejected_mask = reasons.ne("")
    retained = ~rejected_mask
    cleaned = pd.DataFrame({
        "location_name": location.loc[retained], "permit_number": permit.loc[retained],
        "start_time": start.loc[retained].dt.strftime(OUTPUT_TIME_FORMAT),
        "stop_time": stop.loc[retained].dt.strftime(OUTPUT_TIME_FORMAT),
        "duration_minutes": duration.loc[retained].astype(float),
    })

    rejected_rows = []
    original_columns = [column for column in selected.columns if column.endswith("_original")]
    for index in selected.index[rejected_mask]:
        originals = {column: selected.at[index, column] for column in original_columns}
        rejected_rows.append({
            "company": "south_west_water",
            "source_file": f"raw_data/south_west_water/{filename}", "source_sheet": EVENT_SHEET,
            "source_row_number": int(selected.at[index, "source_row_number"]),
            "relevant original source values": json.dumps(originals, ensure_ascii=False, default=str),
            "rejection_reason": reasons.at[index],
        })
    rejected = pd.DataFrame(rejected_rows, columns=[
        "company", "source_file", "source_sheet", "source_row_number",
        "relevant original source values", "rejection_reason",
    ])
    summary = {
        "loaded": rows_loaded, "retained": len(cleaned), "rejected": len(rejected),
        "start_failures": int(start_failure.sum()), "stop_failures": int(stop_failure.sum()),
        "negative": int(negative.sum()), "fallback": int(fallback_rows.sum()),
    }
    print(f"[{LABEL}] Rows loaded: {rows_loaded:,}")
    print(f"[{LABEL}] Rows retained: {len(cleaned):,}")
    print(f"[{LABEL}] Rows rejected: {len(rejected):,}")
    return cleaned, rejected, summary


def validate_output(data: pd.DataFrame) -> None:
    if list(data.columns) != OUTPUT_COLUMNS or data.empty:
        raise ValueError("South West Water output does not have the exact five-column contract.")
    start = pd.to_datetime(data["start_time"], format=OUTPUT_TIME_FORMAT, errors="coerce")
    stop = pd.to_datetime(data["stop_time"], format=OUTPUT_TIME_FORMAT, errors="coerce")
    duration = pd.to_numeric(data["duration_minutes"], errors="coerce")
    if start.isna().any() or stop.isna().any() or stop.lt(start).any():
        raise ValueError("South West Water output contains an invalid timestamp pair.")
    if duration.isna().any() or duration.lt(0).any() or not duration.map(math.isfinite).all():
        raise ValueError("South West Water output contains an invalid duration.")
    expected = (stop - start).dt.total_seconds() / 60
    if duration.sub(expected).abs().gt((1 / 60) + 1e-9).any():
        raise ValueError("South West Water duration differs from timestamps by over one second.")


def clean_south_west_water(*, dry_run: bool = False, strict: bool = False) -> int:
    results = [clean_workbook(filename) for filename in SOURCE_FILES]
    combined = pd.concat([result[0] for result in results], ignore_index=True)
    combined = combined.sort_values(
        ["start_time", "stop_time", "permit_number", "location_name"], kind="mergesort"
    ).reset_index(drop=True)[OUTPUT_COLUMNS]
    validate_output(combined)
    rejected = pd.concat([result[1] for result in results], ignore_index=True)
    summaries = [result[2] for result in results]
    if not dry_run:
        OUTPUT_FOLDER.mkdir(parents=True, exist_ok=True)
        REJECTED_FILE.parent.mkdir(parents=True, exist_ok=True)
        temporary = OUTPUT_FILE.with_name(f"{OUTPUT_FILE.name}.tmp-{os.getpid()}")
        combined.to_csv(temporary, index=False, encoding="utf-8", na_rep="")
        temporary.replace(OUTPUT_FILE)
        rejected.to_csv(REJECTED_FILE, index=False, encoding="utf-8")
    print(f"[{LABEL}] Files processed: {len(SOURCE_FILES)}")
    print(f"[{LABEL}] Total rows loaded: {sum(s['loaded'] for s in summaries):,}")
    print(f"[{LABEL}] Total rows retained: {len(combined):,}")
    print(f"[{LABEL}] Total rows rejected: {len(rejected):,}")
    print(f"[{LABEL}] Start parse failures: {sum(s['start_failures'] for s in summaries):,}")
    print(f"[{LABEL}] Stop parse failures: {sum(s['stop_failures'] for s in summaries):,}")
    print(f"[{LABEL}] Negative durations: {sum(s['negative'] for s in summaries):,}")
    print(f"[{LABEL}] WASC-name fallbacks: {sum(s['fallback'] for s in summaries):,}")
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
        assert combine_date_and_time(pd.Series([datetime(2020, 1, 2)]), pd.Series([time(3, 4, 5)])).iloc[0] == "2020-01-02 03:04:05.000000"
        assert clean_permits(pd.Series(["001/ABC"])).iloc[0] == "001/ABC"
        print(f"[{LABEL}] Self-check passed")
        return 0
    return clean_south_west_water(dry_run=arguments.dry_run, strict=arguments.strict)


if __name__ == "__main__":
    raise SystemExit(main())
