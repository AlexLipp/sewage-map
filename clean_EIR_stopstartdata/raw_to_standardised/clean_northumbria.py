"""Standardise Northumbrian Water's historical EDM event files.

This module is intentionally explicit.  The filenames, source columns,
timestamp formats, rejection rules, and output locations are all visible here
so that this company's cleaning can be audited without reading another module.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import re
from datetime import date, datetime
from pathlib import Path

import pandas as pd


COMPANY_LABEL = "NORTHUMBRIA"
REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
RAW_DATA_FOLDER = REPOSITORY_ROOT / "raw_data" / "northumbria"
OUTPUT_FOLDER = REPOSITORY_ROOT / "clean_EIR_stopstartdata" / "input_stopstart_data"
OUTPUT_FILE = OUTPUT_FOLDER / "northumbria_cleaned_data.csv"
REJECTED_FILE = OUTPUT_FOLDER / "rejected_rows" / "northumbria_rejected_rows.csv"

SOURCE_FILES = [
    "Northumbrian 2024 Detailed EDM Data (3).csv",
    "Northumbrian 2025 Detailed EDM Data (1).csv",
]

SOURCE_COLUMNS = [
    "Unique ID",
    "Site name",
    "Discharge start (GMT)",
    "Discharge end (GMT)",
]

OUTPUT_COLUMNS = [
    "location_name",
    "permit_number",
    "start_time",
    "stop_time",
    "duration_minutes",
]

TIMESTAMP_FORMATS = [
    "%Y-%m-%dT%H:%M:%S.%fZ",
    "%Y-%m-%dT%H:%M:%SZ",
    "%Y-%m-%d %H:%M:%S.%f",
    "%Y-%m-%d %H:%M:%S",
    "%Y-%m-%d %H:%M",
    "%d/%m/%Y %H:%M:%S",
    "%d/%m/%Y %H:%M",
]
OUTPUT_TIMESTAMP_FORMAT = "%Y-%m-%dT%H:%M:%S"
NULL_TEXT = {"", "nan", "none", "nat", "<na>", "null", "n/a", "na"}
LOCATION_PLACEHOLDERS = NULL_TEXT | {"tbc", "unknown", "not available", "not known"}


def source_value_is_missing(value: object) -> bool:
    """Return True for an empty cell or a supplier's literal null marker."""
    if value is None or value is pd.NA:
        return True
    try:
        if bool(pd.isna(value)):
            return True
    except (TypeError, ValueError):
        pass
    return str(value).strip().casefold() in NULL_TEXT


def clean_text(values: pd.Series, *, location: bool = False) -> pd.Series:
    """Trim text and collapse whitespace without inventing missing values."""
    null_markers = LOCATION_PLACEHOLDERS if location else NULL_TEXT

    def clean_one_value(value: object) -> object:
        if source_value_is_missing(value):
            return pd.NA
        cleaned = re.sub(r"\s+", " ", str(value).strip())
        if cleaned.casefold() in null_markers:
            return pd.NA
        return cleaned

    return values.map(clean_one_value).astype("string")


def clean_permit_numbers(values: pd.Series) -> pd.Series:
    """Keep permits as text, including leading zeroes and punctuation."""
    permits = clean_text(values)
    return permits.str.replace(r"^([+-]?\d+)\.0$", r"\1", regex=True)


def parse_timestamps(values: pd.Series) -> pd.Series:
    """Parse only the timestamp formats explicitly listed above."""
    parsed = pd.Series(pd.NaT, index=values.index, dtype="datetime64[ns]")
    object_timestamps = values.map(
        lambda value: isinstance(value, (datetime, date, pd.Timestamp))
    )
    if object_timestamps.any():
        parsed.loc[object_timestamps] = pd.to_datetime(
            values.loc[object_timestamps], errors="coerce"
        )

    nonempty_text = ~object_timestamps & ~values.map(source_value_is_missing)
    unresolved = nonempty_text.copy()
    text = values.astype("string").str.strip()

    for timestamp_format in TIMESTAMP_FORMATS:
        if not unresolved.any():
            break
        converted = pd.to_datetime(
            text.loc[unresolved], format=timestamp_format, errors="coerce"
        )
        successfully_parsed = converted.notna()
        parsed_indices = converted.index[successfully_parsed]
        parsed.loc[parsed_indices] = converted.loc[parsed_indices]
        unresolved.loc[parsed_indices] = False

    return parsed


def add_rejection_reason(
    reasons: pd.Series, rejected_rows: pd.Series, reason: str
) -> None:
    """Add one readable reason, retaining all reasons that apply to a row."""
    current_reasons = reasons.loc[rejected_rows]
    reasons.loc[rejected_rows] = current_reasons.map(
        lambda current: f"{current};{reason}" if current else reason
    )


def validate_output(data: pd.DataFrame) -> None:
    """Enforce the five-column website data contract before writing a CSV."""
    if list(data.columns) != OUTPUT_COLUMNS:
        raise ValueError(f"Output columns are not exact: {list(data.columns)}")
    if data.empty:
        raise ValueError("The Northumbrian output is empty.")

    for identity_column in ("location_name", "permit_number"):
        populated = data[identity_column].dropna().astype("string").str.strip()
        if populated.str.casefold().isin(NULL_TEXT - {""}).any():
            raise ValueError(f"{identity_column} contains literal null text.")

    start = pd.to_datetime(
        data["start_time"], format=OUTPUT_TIMESTAMP_FORMAT, errors="coerce"
    )
    stop = pd.to_datetime(
        data["stop_time"], format=OUTPUT_TIMESTAMP_FORMAT, errors="coerce"
    )
    if start.isna().any() or stop.isna().any():
        raise ValueError("An output timestamp cannot be reparsed exactly.")
    if stop.lt(start).any():
        raise ValueError("An output event has stop_time earlier than start_time.")

    duration = pd.to_numeric(data["duration_minutes"], errors="coerce")
    finite_duration = duration.map(lambda value: math.isfinite(float(value)))
    if duration.isna().any() or not finite_duration.all() or duration.lt(0).any():
        raise ValueError("duration_minutes contains an invalid value.")

    expected_duration = (stop - start).dt.total_seconds() / 60
    if duration.sub(expected_duration).abs().gt((1 / 60) + 1e-9).any():
        raise ValueError("duration_minutes differs from timestamps by over one second.")


def read_and_clean_source(source_filename: str) -> tuple[pd.DataFrame, pd.DataFrame, dict]:
    """Read one of the two files, then apply the one verified schema."""
    source_path = RAW_DATA_FOLDER / source_filename
    print(f"[{COMPANY_LABEL}] Reading: {source_filename}")

    # Both Northumbrian files are UTF-8 comma-separated files with headers on
    # physical row 1.  dtype=object prevents pandas from coercing permits.
    data = pd.read_csv(source_path, header=0, dtype=object)
    rows_loaded = len(data)
    data["source_row_number"] = range(2, rows_loaded + 2)

    missing_columns = []
    for column in SOURCE_COLUMNS:
        if column not in data.columns:
            missing_columns.append(column)
    if missing_columns:
        raise KeyError(
            f"Expected columns are missing from {source_filename}: "
            f"{missing_columns}. The supplier schema may have changed."
        )

    # Select exactly the four audited fields.  Additional supplier columns do
    # not silently become part of the cleaning decision.
    selected = data[SOURCE_COLUMNS + ["source_row_number"]].copy()
    selected = selected.rename(
        columns={
            "Unique ID": "permit_number_original",
            "Site name": "location_name_original",
            "Discharge start (GMT)": "start_time_original",
            "Discharge end (GMT)": "stop_time_original",
        }
    )

    # Completely blank records are structural spreadsheet/CSV space, not
    # supplier events, and therefore are excluded before row validation.
    structurally_blank = pd.Series(True, index=selected.index)
    for column in (
        "permit_number_original",
        "location_name_original",
        "start_time_original",
        "stop_time_original",
    ):
        structurally_blank &= selected[column].map(source_value_is_missing)
    selected = selected.loc[~structurally_blank].copy()

    location_name = clean_text(selected["location_name_original"], location=True)
    permit_number = clean_permit_numbers(selected["permit_number_original"])
    start_time = parse_timestamps(selected["start_time_original"])
    stop_time = parse_timestamps(selected["stop_time_original"])
    duration_minutes = ((stop_time - start_time).dt.total_seconds() / 60).round(6)

    missing_start_time = selected["start_time_original"].map(source_value_is_missing)
    missing_stop_time = selected["stop_time_original"].map(source_value_is_missing)
    unparseable_start_time = ~missing_start_time & start_time.isna()
    unparseable_stop_time = ~missing_stop_time & stop_time.isna()
    negative_duration = start_time.notna() & stop_time.notna() & stop_time.lt(start_time)
    non_finite_duration = duration_minutes.notna() & ~duration_minutes.map(
        lambda value: math.isfinite(float(value))
    )

    rejection_reason = pd.Series("", index=selected.index, dtype="string")
    add_rejection_reason(rejection_reason, missing_start_time, "missing_start_time")
    add_rejection_reason(rejection_reason, missing_stop_time, "missing_stop_time")
    add_rejection_reason(
        rejection_reason, unparseable_start_time, "unparseable_start_time"
    )
    add_rejection_reason(
        rejection_reason, unparseable_stop_time, "unparseable_stop_time"
    )
    add_rejection_reason(rejection_reason, negative_duration, "negative_duration")
    add_rejection_reason(
        rejection_reason, non_finite_duration, "non_finite_duration"
    )

    rejected_mask = rejection_reason.ne("")
    retained_mask = ~rejected_mask

    cleaned = pd.DataFrame(
        {
            "location_name": location_name.loc[retained_mask],
            "permit_number": permit_number.loc[retained_mask],
            "start_time": start_time.loc[retained_mask].dt.strftime(
                OUTPUT_TIMESTAMP_FORMAT
            ),
            "stop_time": stop_time.loc[retained_mask].dt.strftime(
                OUTPUT_TIMESTAMP_FORMAT
            ),
            "duration_minutes": duration_minutes.loc[retained_mask].astype(float),
        }
    )

    rejected_records = []
    for index in selected.index[rejected_mask]:
        original_values = {
            "Unique ID": selected.at[index, "permit_number_original"],
            "Site name": selected.at[index, "location_name_original"],
            "Discharge start (GMT)": selected.at[index, "start_time_original"],
            "Discharge end (GMT)": selected.at[index, "stop_time_original"],
        }
        rejected_records.append(
            {
                "company": "northumbria",
                "source_file": f"raw_data/northumbria/{source_filename}",
                "source_sheet": "",
                "source_row_number": int(selected.at[index, "source_row_number"]),
                "relevant original source values": json.dumps(
                    original_values, ensure_ascii=False, default=str
                ),
                "rejection_reason": rejection_reason.at[index],
            }
        )
    rejected = pd.DataFrame(
        rejected_records,
        columns=[
            "company",
            "source_file",
            "source_sheet",
            "source_row_number",
            "relevant original source values",
            "rejection_reason",
        ],
    )

    summary = {
        "rows_loaded": rows_loaded,
        "rows_retained": len(cleaned),
        "rows_rejected": len(rejected),
        "start_parse_failures": int(unparseable_start_time.sum()),
        "stop_parse_failures": int(unparseable_stop_time.sum()),
        "negative_durations": int(negative_duration.sum()),
    }
    print(f"[{COMPANY_LABEL}] Rows loaded: {rows_loaded:,}")
    print(f"[{COMPANY_LABEL}] Rows retained: {len(cleaned):,}")
    print(f"[{COMPANY_LABEL}] Rows rejected: {len(rejected):,}")
    return cleaned, rejected, summary


def clean_northumbria(*, dry_run: bool = False, strict: bool = False) -> int:
    """Clean both explicit source files and write one standardised CSV."""
    cleaned_files = []
    rejected_files = []
    summaries = []

    for source_filename in SOURCE_FILES:
        cleaned, rejected, summary = read_and_clean_source(source_filename)
        cleaned_files.append(cleaned)
        rejected_files.append(rejected)
        summaries.append(summary)

    combined = pd.concat(cleaned_files, ignore_index=True)
    combined = combined.sort_values(
        ["start_time", "stop_time", "permit_number", "location_name"],
        kind="mergesort",
    ).reset_index(drop=True)
    combined = combined[OUTPUT_COLUMNS]
    validate_output(combined)

    all_rejected = pd.concat(rejected_files, ignore_index=True)
    if not dry_run:
        OUTPUT_FOLDER.mkdir(parents=True, exist_ok=True)
        REJECTED_FILE.parent.mkdir(parents=True, exist_ok=True)
        temporary_output = OUTPUT_FILE.with_name(
            f"{OUTPUT_FILE.name}.tmp-{os.getpid()}"
        )
        combined.to_csv(
            temporary_output, index=False, encoding="utf-8", na_rep=""
        )
        temporary_output.replace(OUTPUT_FILE)
        all_rejected.to_csv(REJECTED_FILE, index=False, encoding="utf-8")

    total_loaded = sum(summary["rows_loaded"] for summary in summaries)
    total_rejected = sum(summary["rows_rejected"] for summary in summaries)
    print(f"[{COMPANY_LABEL}] Files processed: {len(SOURCE_FILES)}")
    print(f"[{COMPANY_LABEL}] Total rows loaded: {total_loaded:,}")
    print(f"[{COMPANY_LABEL}] Total rows retained: {len(combined):,}")
    print(f"[{COMPANY_LABEL}] Total rows rejected: {total_rejected:,}")
    print(
        f"[{COMPANY_LABEL}] Start parse failures: "
        f"{sum(summary['start_parse_failures'] for summary in summaries):,}"
    )
    print(
        f"[{COMPANY_LABEL}] Stop parse failures: "
        f"{sum(summary['stop_parse_failures'] for summary in summaries):,}"
    )
    print(
        f"[{COMPANY_LABEL}] Negative durations: "
        f"{sum(summary['negative_durations'] for summary in summaries):,}"
    )
    if dry_run:
        print(f"[{COMPANY_LABEL}] Dry run: no files written")
    else:
        print(f"[{COMPANY_LABEL}] Output written to: {OUTPUT_FILE}")
        print(f"[{COMPANY_LABEL}] Rejected rows written to: {REJECTED_FILE}")

    if strict and total_rejected:
        return 1
    return 0


def run_self_check() -> None:
    """Exercise timestamp parsing, permit preservation, and zero duration."""
    sample = pd.Series(["2025-01-02T03:04:05.000Z", "bad"])
    parsed = parse_timestamps(sample)
    assert parsed.notna().tolist() == [True, False]
    permits = clean_permit_numbers(pd.Series(["00123", "AB/12.0"]))
    assert permits.tolist() == ["00123", "AB/12.0"]
    zero = ((parsed.iloc[[0]] - parsed.iloc[[0]]).dt.total_seconds() / 60).iloc[0]
    assert zero == 0
    print(f"[{COMPANY_LABEL}] Self-check passed")


def build_argument_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--strict", action="store_true")
    parser.add_argument("--self-check", action="store_true")
    return parser


def main() -> int:
    arguments = build_argument_parser().parse_args()
    if arguments.self_check:
        run_self_check()
        return 0
    return clean_northumbria(dry_run=arguments.dry_run, strict=arguments.strict)


if __name__ == "__main__":
    raise SystemExit(main())
