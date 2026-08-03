"""Standardise Anglian Water's explicit annual and monthly EDM schemas.

Anglian timestamps use month/day order.  Several monthly workbooks contain a
verified Excel coercion in which month and day were swapped; the repair below
is deliberately restricted to Anglian monthly files and their filename month.
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


LABEL = "ANGLIAN"
ROOT = Path(__file__).resolve().parents[2]
RAW_FOLDER = ROOT / "raw_data" / "anglian"
OUTPUT_FOLDER = ROOT / "clean_EIR_stopstartdata" / "input_stopstart_data"
OUTPUT_FILE = OUTPUT_FOLDER / "anglian_cleaned_data.csv"
REJECTED_FILE = OUTPUT_FOLDER / "rejected_rows" / "anglian_rejected_rows.csv"
DURATION_QC_FILE = OUTPUT_FOLDER / "duration_qc" / "anglian_duration_discrepancies.csv"
UNSUPPORTED_XLSB = "event-duration-monitor-edm-individual-discharges-2024 (2).xlsb"
OUTPUT_COLUMNS = [
    "location_name", "permit_number", "start_time", "stop_time", "duration_minutes"
]
OUTPUT_TIME_FORMAT = "%Y-%m-%dT%H:%M:%S"
NULL_TEXT = {"", "nan", "none", "nat", "<na>", "null", "n/a", "na"}
LOCATION_NULLS = NULL_TEXT | {"tbc", "unknown", "not available", "not known"}

# Unlike the other suppliers, Anglian's slash-formatted timestamps are
# month/day/year.  No day/month fallback is attempted.
TIMESTAMP_FORMATS = [
    "%Y-%m-%dT%H:%M:%S.%fZ", "%Y-%m-%dT%H:%M:%SZ",
    "%Y-%m-%d %H:%M:%S.%f", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M",
    "%m/%d/%Y %H:%M:%S", "%m/%d/%Y %H:%M",
]

# Filename, exact sheet, and exact location header.  Annual supplier duration
# is hours, but website duration is still calculated from Start and Stop.
ANNUAL_SOURCES = [
    ("event-duration-monitor-edm-individual-discharges-2021 (3).xlsx", "EDM 2021", "Site Name "),
    ("event-duration-monitor-edm-individual-discharges-2022 (1).xlsx", "EDM 2022", "Site Name"),
    ("event-duration-monitor-edm-individual-discharges-2023.xlsx", "EDM 2023", "Site Name"),
]

# These files use compact headers: SITECODE, SITENAME, STARTDATETIME,
# ENDDATETIME.  The third tuple value is the exact duration header.
COMPACT_MONTHLY_SOURCES = [
    ("storm-overflow-map-april-2025.xlsx", "Storm Overflow Map April 2025", "DURATION (min)"),
    ("storm-overflow-map-july-2025.xlsx", "Storm Overflow Map July 2025", "DURATION"),
    ("storm-overflow-map-august-2025.xlsx", "Storm Overflow Map August 2025", "DURATION"),
    ("storm-overflow-map-september-2025.xlsx", "Storm Overflow Map - Sept 2025", "DURATION"),
]

# These files use SITE CODE / SITE NAME and non-slashed DATE TIME headers.
SPACED_MONTHLY_SOURCES = [
    ("storm-overflow-map-may-2025.xlsx", "Sheet1", "DURATION"),
    ("storm-overflow-map-june-2025.xlsx", "Sheet1", "DURATION"),
    ("storm-overflow-map---october-2025.xlsx", "Storm Overflow Map - Oct 2025", "DURATION"),
    ("storm-overflow-map-march-2026.xlsx", "Sheet1", "DURATION\n(MINUTES)"),
]

# These files use SITE CODE / SITE NAME and slashed DATE/TIME headers.
SLASHED_MONTHLY_SOURCES = [
    ("storm-overflow-map---november-2025.xlsx", "Storm Overflow - November", "DURATION"),
    ("storm-overflow-map---december-2025.xlsx", "Storm Overflow - December", "DURATION"),
    ("storm-overflow-map---january-2026.xlsx", "Storm Overflow Map - January 20", "DURATION"),
    ("storm-overflow-map-february-2026.xlsx", "Storm Overflow Map - February 2", "DURATION"),
    ("storm-overflow-map-april-2026 (1).xlsx", "Sheet1", "DURATION (MINUTES)"),
    ("storm-overflow-map-may-2026.xlsx", "Sheet1", "DURATION\n(MINUTES)"),
]

MONTH_NUMBERS = {
    "january": 1, "february": 2, "march": 3, "april": 4,
    "may": 5, "june": 6, "july": 7, "august": 8,
    "september": 9, "october": 10, "november": 11, "december": 12,
}


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


def repair_verified_excel_month_day(values: pd.Series, filename: str) -> pd.Series:
    """Repair only the verified Anglian monthly Excel coercion."""
    filename_lower = filename.casefold()
    source_month = None
    for month_name, month_number in MONTH_NUMBERS.items():
        if month_name in filename_lower:
            source_month = month_number
            break
    if source_month is None:
        return values

    def repair(value: object) -> object:
        if not isinstance(value, (datetime, pd.Timestamp)):
            return value
        timestamp = pd.Timestamp(value)
        # Example: intended 12/2/2025 in December was stored as 2025-02-12.
        # The release month verifies the swap for otherwise ambiguous days 1-12.
        if timestamp.day == source_month and timestamp.month != source_month:
            return timestamp.replace(month=source_month, day=timestamp.month)
        return value

    return values.map(repair)


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


def clean_selected_rows(
    selected: pd.DataFrame, *, filename: str, sheet_name: str,
    supplier_duration_unit: str,
) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame, dict]:
    structurally_blank = pd.Series(True, index=selected.index)
    for column in ("permit_original", "location_original", "start_original", "stop_original"):
        structurally_blank &= selected[column].map(is_missing)
    selected = selected.loc[~structurally_blank].copy()

    location = clean_text(selected["location_original"], location=True)
    permit = clean_permits(selected["permit_original"])
    repaired_start = repair_verified_excel_month_day(selected["start_original"], filename)
    repaired_stop = repair_verified_excel_month_day(selected["stop_original"], filename)
    start = parse_timestamps(repaired_start)
    stop = parse_timestamps(repaired_stop)
    duration = ((stop - start).dt.total_seconds() / 60).round(6)

    missing_start = repaired_start.map(is_missing)
    missing_stop = repaired_stop.map(is_missing)
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
            "supplier duration": selected.at[index, "supplier_duration"],
        }
        rejected_rows.append({
            "company": "anglian", "source_file": f"raw_data/anglian/{filename}",
            "source_sheet": sheet_name,
            "source_row_number": int(selected.at[index, "source_row_number"]),
            "relevant original source values": json.dumps(originals, ensure_ascii=False, default=str),
            "rejection_reason": reasons.at[index],
        })
    rejected = pd.DataFrame(rejected_rows, columns=[
        "company", "source_file", "source_sheet", "source_row_number",
        "relevant original source values", "rejection_reason",
    ])

    # Supplier duration is QC-only: annual values are hours, monthly values are minutes.
    supplier_minutes = pd.to_numeric(selected["supplier_duration"], errors="coerce")
    if supplier_duration_unit == "hours":
        supplier_minutes = supplier_minutes * 60
    discrepancy = supplier_minutes.notna() & duration.notna()
    discrepancy &= supplier_minutes.sub(duration).abs().gt((1 / 60) + 1e-9)
    duration_qc = pd.DataFrame({
        "company": "anglian", "source_file": f"raw_data/anglian/{filename}",
        "source_sheet": sheet_name,
        "source_row_number": selected.loc[discrepancy, "source_row_number"].astype(int),
        "supplier_duration": selected.loc[discrepancy, "supplier_duration"],
        "supplier_duration_unit": supplier_duration_unit,
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


def read_annual_source(filename: str, sheet_name: str, location_column: str):
    print(f"[{LABEL}] Reading: {filename} [{sheet_name}]")
    data = pd.read_excel(RAW_FOLDER / filename, sheet_name=sheet_name, header=0, dtype=object)
    required = ["UniqueID", location_column, "Start", "Stop", "Duration (hrs)"]
    require_columns(data, required, f"{filename} [{sheet_name}]")
    selected = data[required].copy().rename(columns={
        "UniqueID": "permit_original", location_column: "location_original",
        "Start": "start_original", "Stop": "stop_original",
        "Duration (hrs)": "supplier_duration",
    })
    selected["source_row_number"] = range(2, len(data) + 2)
    return clean_selected_rows(
        selected, filename=filename, sheet_name=sheet_name, supplier_duration_unit="hours"
    )


def read_compact_monthly_source(filename: str, sheet_name: str, duration_column: str):
    print(f"[{LABEL}] Reading: {filename} [{sheet_name}]")
    data = pd.read_excel(RAW_FOLDER / filename, sheet_name=sheet_name, header=0, dtype=object)
    required = ["SITECODE", "SITENAME", "STARTDATETIME", "ENDDATETIME", duration_column]
    require_columns(data, required, f"{filename} [{sheet_name}]")
    selected = data[required].copy().rename(columns={
        "SITECODE": "permit_original", "SITENAME": "location_original",
        "STARTDATETIME": "start_original", "ENDDATETIME": "stop_original",
        duration_column: "supplier_duration",
    })
    selected["source_row_number"] = range(2, len(data) + 2)
    return clean_selected_rows(
        selected, filename=filename, sheet_name=sheet_name, supplier_duration_unit="minutes"
    )


def read_spaced_monthly_source(filename: str, sheet_name: str, duration_column: str):
    print(f"[{LABEL}] Reading: {filename} [{sheet_name}]")
    data = pd.read_excel(RAW_FOLDER / filename, sheet_name=sheet_name, header=0, dtype=object)
    required = ["SITE CODE", "SITE NAME", "START DATE TIME", "END DATE TIME", duration_column]
    require_columns(data, required, f"{filename} [{sheet_name}]")
    selected = data[required].copy().rename(columns={
        "SITE CODE": "permit_original", "SITE NAME": "location_original",
        "START DATE TIME": "start_original", "END DATE TIME": "stop_original",
        duration_column: "supplier_duration",
    })
    selected["source_row_number"] = range(2, len(data) + 2)
    return clean_selected_rows(
        selected, filename=filename, sheet_name=sheet_name, supplier_duration_unit="minutes"
    )


def read_slashed_monthly_source(filename: str, sheet_name: str, duration_column: str):
    print(f"[{LABEL}] Reading: {filename} [{sheet_name}]")
    data = pd.read_excel(RAW_FOLDER / filename, sheet_name=sheet_name, header=0, dtype=object)
    required = ["SITE CODE", "SITE NAME", "START DATE/TIME", "END DATE/TIME", duration_column]
    require_columns(data, required, f"{filename} [{sheet_name}]")
    selected = data[required].copy().rename(columns={
        "SITE CODE": "permit_original", "SITE NAME": "location_original",
        "START DATE/TIME": "start_original", "END DATE/TIME": "stop_original",
        duration_column: "supplier_duration",
    })
    selected["source_row_number"] = range(2, len(data) + 2)
    return clean_selected_rows(
        selected, filename=filename, sheet_name=sheet_name, supplier_duration_unit="minutes"
    )


def validate_output(data: pd.DataFrame) -> None:
    if list(data.columns) != OUTPUT_COLUMNS or data.empty:
        raise ValueError("Anglian output does not have the exact five-column contract.")
    start = pd.to_datetime(data["start_time"], format=OUTPUT_TIME_FORMAT, errors="coerce")
    stop = pd.to_datetime(data["stop_time"], format=OUTPUT_TIME_FORMAT, errors="coerce")
    duration = pd.to_numeric(data["duration_minutes"], errors="coerce")
    if start.isna().any() or stop.isna().any() or stop.lt(start).any():
        raise ValueError("Anglian output contains an invalid timestamp pair.")
    if duration.isna().any() or duration.lt(0).any() or not duration.map(math.isfinite).all():
        raise ValueError("Anglian output contains an invalid duration.")
    expected = (stop - start).dt.total_seconds() / 60
    if duration.sub(expected).abs().gt((1 / 60) + 1e-9).any():
        raise ValueError("Anglian duration differs from timestamps by over one second.")


def clean_anglian(*, dry_run: bool = False, strict: bool = False) -> int:
    results = [read_annual_source(*source) for source in ANNUAL_SOURCES]
    results.extend(read_compact_monthly_source(*source) for source in COMPACT_MONTHLY_SOURCES)
    results.extend(read_spaced_monthly_source(*source) for source in SPACED_MONTHLY_SOURCES)
    results.extend(read_slashed_monthly_source(*source) for source in SLASHED_MONTHLY_SOURCES)
    print(f"[{LABEL}] Unsupported workbook: {UNSUPPORTED_XLSB} (.xlsb is not read or guessed)")

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
    # Preserve the old nonzero status for the unsupported .xlsb source.
    return 1


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--strict", action="store_true")
    parser.add_argument("--self-check", action="store_true")
    arguments = parser.parse_args()
    if arguments.self_check:
        repaired = repair_verified_excel_month_day(pd.Series([datetime(2025, 2, 12)]), "december-2025.xlsx")
        assert repaired.iloc[0] == pd.Timestamp("2025-12-02")
        assert parse_timestamps(pd.Series(["12/02/2025 03:04"])).notna().all()
        print(f"[{LABEL}] Self-check passed")
        return 0
    return clean_anglian(dry_run=arguments.dry_run, strict=arguments.strict)


if __name__ == "__main__":
    raise SystemExit(main())
