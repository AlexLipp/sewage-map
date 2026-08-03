"""Standardise United Utilities' known historical EDM workbook variants.

Every workbook, event worksheet, header row, and exact source field is named in
this file.  Summary sheets are structural and are never treated as events.
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


LABEL = "UNITED UTILITIES"
ROOT = Path(__file__).resolve().parents[2]
RAW_FOLDER = ROOT / "raw_data" / "united_utilities"
OUTPUT_FOLDER = ROOT / "clean_EIR_stopstartdata" / "input_stopstart_data"
OUTPUT_FILE = OUTPUT_FOLDER / "united_utilities_cleaned_data.csv"
REJECTED_FILE = OUTPUT_FOLDER / "rejected_rows" / "united_utilities_rejected_rows.csv"
DURATION_QC_FILE = OUTPUT_FOLDER / "duration_qc" / "united_utilities_duration_discrepancies.csv"
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

# January-April 2026 use one verified UTC schema on these exact sheets.
UTC_2026_WORKSHEETS = [
    ("January 2026 Start Stop Data.xlsx", "January 2026 EDM Data"),
    ("February 2026 Start Stop Data.xlsx", "February 2026 EDM Data"),
    ("March 2026 Start Stop Data.xlsx", "March 2026 EDM Data"),
    ("April 2026 Start Stop Data.xlsx", "April 2026 EDM Data"),
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
    parsed.loc[timestamp_objects] = pd.to_datetime(series.loc[timestamp_objects], errors="coerce")
    unresolved = ~timestamp_objects & ~series.map(is_missing)
    text_values = series.astype("string").str.strip()
    for timestamp_format in TIMESTAMP_FORMATS:
        converted = pd.to_datetime(text_values.loc[unresolved], format=timestamp_format, errors="coerce")
        successful = converted.notna()
        indices = converted.index[successful]
        parsed.loc[indices] = converted.loc[indices]
        unresolved.loc[indices] = False
        if not unresolved.any():
            break
    return parsed


def add_reason(reasons: pd.Series, mask: pd.Series, reason: str) -> None:
    reasons.loc[mask] = reasons.loc[mask].map(
        lambda current: f"{current};{reason}" if current else reason
    )


def require_columns(data: pd.DataFrame, required: list[str], source: str) -> None:
    missing_columns = []
    for column in required:
        if column not in data.columns:
            missing_columns.append(column)
    if missing_columns:
        raise KeyError(
            f"Expected columns are missing from {source}: {missing_columns}. "
            "The supplier schema may have changed."
        )


def duration_hhmmss_to_minutes(value: object) -> float | None:
    """Convert the May 2025 supplier duration for QC only."""
    if is_missing(value):
        return None
    if isinstance(value, time):
        seconds = value.hour * 3600 + value.minute * 60 + value.second + value.microsecond / 1_000_000
        return round(seconds / 60, 6)
    if isinstance(value, timedelta):
        return round(value.total_seconds() / 60, 6)
    match = re.fullmatch(r"(\d+):(\d{1,2}):(\d{1,2}(?:\.\d+)?)", str(value).strip())
    if not match:
        return None
    seconds = int(match.group(1)) * 3600 + int(match.group(2)) * 60 + float(match.group(3))
    return round(seconds / 60, 6)


def clean_selected_rows(
    selected: pd.DataFrame, *, filename: str, sheet_name: str,
    supplier_duration_present: bool = False,
) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame, dict]:
    structurally_blank = pd.Series(True, index=selected.index)
    for column in ("permit_original", "location_original", "start_original", "stop_original"):
        structurally_blank &= selected[column].map(is_missing)
    selected = selected.loc[~structurally_blank].copy()

    location = clean_text(selected["location_original"], location=True)
    permit = clean_permits(selected["permit_original"])
    start = parse_timestamps(selected["start_original"])
    stop = parse_timestamps(selected["stop_original"])
    duration = ((stop - start).dt.total_seconds() / 60).round(6)
    missing_start = selected["start_original"].map(is_missing)
    missing_stop = selected["stop_original"].map(is_missing)
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
    for index in selected.index[rejected_mask]:
        originals = {
            "permit": selected.at[index, "permit_original"],
            "location": selected.at[index, "location_original"],
            "start": selected.at[index, "start_original"],
            "stop": selected.at[index, "stop_original"],
        }
        rejected_rows.append({
            "company": "united_utilities",
            "source_file": f"raw_data/united_utilities/{filename}",
            "source_sheet": sheet_name,
            "source_row_number": int(selected.at[index, "source_row_number"]),
            "relevant original source values": json.dumps(originals, ensure_ascii=False, default=str),
            "rejection_reason": reasons.at[index],
        })
    rejected = pd.DataFrame(rejected_rows, columns=[
        "company", "source_file", "source_sheet", "source_row_number",
        "relevant original source values", "rejection_reason",
    ])

    duration_qc = pd.DataFrame(columns=[
        "company", "source_file", "source_sheet", "source_row_number",
        "supplier_duration_hhmmss", "calculated_duration_minutes", "absolute_difference_minutes",
    ])
    if supplier_duration_present:
        supplier_minutes = selected["supplier_duration"].map(duration_hhmmss_to_minutes)
        comparable = supplier_minutes.notna() & duration.notna()
        discrepancy = comparable & supplier_minutes.sub(duration).abs().gt((1 / 60) + 1e-9)
        duration_qc = pd.DataFrame({
            "company": "united_utilities",
            "source_file": f"raw_data/united_utilities/{filename}",
            "source_sheet": sheet_name,
            "source_row_number": selected.loc[discrepancy, "source_row_number"].astype(int),
            "supplier_duration_hhmmss": selected.loc[discrepancy, "supplier_duration"],
            "calculated_duration_minutes": duration.loc[discrepancy],
            "absolute_difference_minutes": supplier_minutes.loc[discrepancy].sub(duration.loc[discrepancy]).abs().round(6),
        })
    summary = {
        "loaded": len(selected) + int(structurally_blank.sum()), "retained": len(cleaned),
        "rejected": len(rejected), "start_failures": int(start_failure.sum()),
        "stop_failures": int(stop_failure.sum()), "negative": int(negative.sum()),
    }
    print(f"[{LABEL}] Rows loaded: {summary['loaded']:,}")
    print(f"[{LABEL}] Rows retained: {len(cleaned):,}")
    print(f"[{LABEL}] Rows rejected: {len(rejected):,}")
    return cleaned, rejected, duration_qc, summary


def read_exact_sheet(
    filename: str, sheet_name: str, *, location_column: str, permit_column: str,
    start_column: str, stop_column: str, duration_column: str | None = None,
) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame, dict]:
    print(f"[{LABEL}] Reading: {filename} [{sheet_name}]")
    data = pd.read_excel(RAW_FOLDER / filename, sheet_name=sheet_name, header=0, dtype=object)
    required = [permit_column, location_column, start_column, stop_column]
    if duration_column:
        required.append(duration_column)
    require_columns(data, required, f"{filename} [{sheet_name}]")
    selected = data[required].copy()
    selected["source_row_number"] = range(2, len(data) + 2)
    rename = {
        permit_column: "permit_original", location_column: "location_original",
        start_column: "start_original", stop_column: "stop_original",
    }
    if duration_column:
        rename[duration_column] = "supplier_duration"
    selected = selected.rename(columns=rename)
    return clean_selected_rows(
        selected, filename=filename, sheet_name=sheet_name,
        supplier_duration_present=duration_column is not None,
    )


def validate_output(data: pd.DataFrame) -> None:
    if list(data.columns) != OUTPUT_COLUMNS or data.empty:
        raise ValueError("United Utilities output does not have the exact five-column contract.")
    start = pd.to_datetime(data["start_time"], format=OUTPUT_TIME_FORMAT, errors="coerce")
    stop = pd.to_datetime(data["stop_time"], format=OUTPUT_TIME_FORMAT, errors="coerce")
    duration = pd.to_numeric(data["duration_minutes"], errors="coerce")
    if start.isna().any() or stop.isna().any() or stop.lt(start).any():
        raise ValueError("United Utilities output contains an invalid timestamp pair.")
    if duration.isna().any() or duration.lt(0).any() or not duration.map(math.isfinite).all():
        raise ValueError("United Utilities output contains an invalid duration.")
    expected = (stop - start).dt.total_seconds() / 60
    if duration.sub(expected).abs().gt((1 / 60) + 1e-9).any():
        raise ValueError("United Utilities duration differs from timestamps by over one second.")


def clean_united_utilities(*, dry_run: bool = False, strict: bool = False) -> int:
    results = []
    # 2023: complete UTC timestamps without the later '(GMT)' suffix.
    results.append(read_exact_sheet(
        "2023 Start and Stops.xlsx", "Sheet1", location_column="Site Name",
        permit_column="Unique ID", start_column="Discharge Start", stop_column="Discharge Stop",
    ))
    # 2024: the annual four-column GMT schema.
    results.append(read_exact_sheet(
        "2024 Start and Stops.xlsx", "Sheet1", location_column="Site Name",
        permit_column="Unique ID", start_column="Discharge Start (GMT)", stop_column="Discharge Stop (GMT)",
    ))
    # January-April 2026: identical UTC column names on explicitly listed sheets.
    for filename, sheet_name in UTC_2026_WORKSHEETS:
        results.append(read_exact_sheet(
            filename, sheet_name, location_column="Site Name", permit_column="Unique ID",
            start_column="Spill Start Time (UTC)", stop_column="Spill End Time (UTC)",
        ))
    # May 2025 uniquely uses UUG Reference.  It is the verified permit identifier
    # and must retain its punctuation.  Supplier hh:mm:ss duration is QC only.
    results.append(read_exact_sheet(
        "May 2025 EDM Data.xlsx", "Start Stop times May 25", location_column="Site Name",
        permit_column="UUG Reference", start_column="Spill Start Time", stop_column="Spill End Time",
        # The supplier header contains two trailing spaces; keeping them here
        # makes the exact physical schema visible instead of normalising it.
        duration_column="Duration (hh:mm:ss)  ",
    ))
    # May 2026 returns to the annual GMT schema and EA-consent site-name label.
    results.append(read_exact_sheet(
        "May 2026 Start Stop Data.xlsx", "United Utilities Detailed Data ",
        location_column="Site Name (EA consent database)", permit_column="Unique ID",
        start_column="Discharge Start (GMT)", stop_column="Discharge Stop (GMT)",
    ))

    combined = pd.concat([result[0] for result in results], ignore_index=True)
    combined = combined.sort_values(
        ["start_time", "stop_time", "permit_number", "location_name"], kind="mergesort"
    ).reset_index(drop=True)[OUTPUT_COLUMNS]
    validate_output(combined)
    rejected = pd.concat([result[1] for result in results], ignore_index=True)
    duration_qc = pd.concat([result[2] for result in results], ignore_index=True)
    summaries = [result[3] for result in results]
    if not dry_run:
        OUTPUT_FOLDER.mkdir(parents=True, exist_ok=True)
        REJECTED_FILE.parent.mkdir(parents=True, exist_ok=True)
        DURATION_QC_FILE.parent.mkdir(parents=True, exist_ok=True)
        temporary = OUTPUT_FILE.with_name(f"{OUTPUT_FILE.name}.tmp-{os.getpid()}")
        combined.to_csv(temporary, index=False, encoding="utf-8", na_rep="")
        temporary.replace(OUTPUT_FILE)
        rejected.to_csv(REJECTED_FILE, index=False, encoding="utf-8")
        duration_qc.to_csv(DURATION_QC_FILE, index=False, encoding="utf-8")
    print(f"[{LABEL}] Files processed: {len(results)}")
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
        assert parse_timestamps(pd.Series(["2024-01-02T03:04:05Z"])).notna().all()
        assert clean_permits(pd.Series(["00/UUG-1"])).iloc[0] == "00/UUG-1"
        print(f"[{LABEL}] Self-check passed")
        return 0
    return clean_united_utilities(dry_run=arguments.dry_run, strict=arguments.strict)


if __name__ == "__main__":
    raise SystemExit(main())
