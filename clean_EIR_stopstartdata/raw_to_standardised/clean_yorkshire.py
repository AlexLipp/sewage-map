"""Standardise Yorkshire Water's six explicitly known EDM workbooks."""

from __future__ import annotations

import argparse
import json
import math
import os
import re
from datetime import date, datetime
from pathlib import Path

import pandas as pd


LABEL = "YORKSHIRE"
ROOT = Path(__file__).resolve().parents[2]
RAW_FOLDER = ROOT / "raw_data" / "yorkshire"
OUTPUT_FOLDER = ROOT / "clean_EIR_stopstartdata" / "input_stopstart_data"
OUTPUT_FILE = OUTPUT_FOLDER / "yorkshire_cleaned_data.csv"
REJECTED_FILE = OUTPUT_FOLDER / "rejected_rows" / "yorkshire_rejected_rows.csv"
DURATION_QC_FILE = OUTPUT_FOLDER / "duration_qc" / "yorkshire_duration_discrepancies.csv"
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

# Filename, exact worksheet, exact location header, and supplier-duration field. Header row 1 is
# verified for every workbook.  The 2026 release changed only the duration
# header; its unit remains hours and it remains QC-only.
SOURCE_WORKSHEETS = [
    ("stop_start-file-2021-iso-format (1).xlsx", "EDM Event Data 2021", "Site Name ", "Event Duration (Hours)"),
    ("stop_start-file-2022-iso-format.xlsx", "EDM Event Data 2022", "Site Name ", "Event Duration (Hours)"),
    ("stop_start-file-2023-iso-format.xlsx", "EDM Event Data 2023", "Site Name ", "Event Duration (Hours)"),
    ("stop_start-file-2024-iso-format.xlsx", "EDM Event Data 2024", "Site Name ", "Event Duration (Hours)"),
    ("stop-start-file-2025-iso-format.xlsx", "EDM Event Data 2025", "Site Name ", "Event Duration (Hours)"),
    ("stop-start-file-2026-iso-format.xlsx", "EDM Event Data 2026", "Site Name", "Duration of Spill (hrs)"),
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


def parse_timestamps(series: pd.Series) -> pd.Series:
    parsed = pd.Series(pd.NaT, index=series.index, dtype="datetime64[ns]")
    timestamp_objects = series.map(
        lambda value: isinstance(value, (datetime, date, pd.Timestamp))
    )
    parsed.loc[timestamp_objects] = pd.to_datetime(series.loc[timestamp_objects], errors="coerce")
    unresolved = ~timestamp_objects & ~series.map(is_missing)
    text = series.astype("string").str.strip()
    for timestamp_format in TIMESTAMP_FORMATS:
        converted = pd.to_datetime(text.loc[unresolved], format=timestamp_format, errors="coerce")
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


def clean_workbook(
    filename: str, sheet_name: str, location_column: str, supplier_duration_column: str
) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame, dict]:
    print(f"[{LABEL}] Reading: {filename} [{sheet_name}]")
    data = pd.read_excel(RAW_FOLDER / filename, sheet_name=sheet_name, header=0, dtype=object)
    rows_loaded = len(data)
    required = [
        "Unique ID", location_column, "Discharge Start (GMT)",
        "Discharge Stop (GMT)", supplier_duration_column,
    ]
    require_columns(data, required, f"{filename} [{sheet_name}]")
    selected = data[required].copy()
    selected["source_row_number"] = range(2, rows_loaded + 2)
    selected = selected.rename(columns={
        "Unique ID": "permit_original", location_column: "location_original",
        "Discharge Start (GMT)": "start_original", "Discharge Stop (GMT)": "stop_original",
        supplier_duration_column: "supplier_duration_hours",
    })

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
            "Unique ID": selected.at[index, "permit_original"],
            "Site Name": selected.at[index, "location_original"],
            "Discharge Start (GMT)": selected.at[index, "start_original"],
            "Discharge Stop (GMT)": selected.at[index, "stop_original"],
            supplier_duration_column: selected.at[index, "supplier_duration_hours"],
        }
        rejected_rows.append({
            "company": "yorkshire", "source_file": f"raw_data/yorkshire/{filename}",
            "source_sheet": sheet_name,
            "source_row_number": int(selected.at[index, "source_row_number"]),
            "relevant original source values": json.dumps(originals, ensure_ascii=False, default=str),
            "rejection_reason": reasons.at[index],
        })
    rejected = pd.DataFrame(rejected_rows, columns=[
        "company", "source_file", "source_sheet", "source_row_number",
        "relevant original source values", "rejection_reason",
    ])

    # Supplier duration is measured in hours but is not authoritative.
    supplier_minutes = pd.to_numeric(selected["supplier_duration_hours"], errors="coerce") * 60
    discrepancy = supplier_minutes.notna() & duration.notna()
    discrepancy &= supplier_minutes.sub(duration).abs().gt((1 / 60) + 1e-9)
    duration_qc = pd.DataFrame({
        "company": "yorkshire", "source_file": f"raw_data/yorkshire/{filename}",
        "source_sheet": sheet_name,
        "source_row_number": selected.loc[discrepancy, "source_row_number"].astype(int),
        "supplier_duration_hours": selected.loc[discrepancy, "supplier_duration_hours"],
        "calculated_duration_minutes": duration.loc[discrepancy],
        "absolute_difference_minutes": supplier_minutes.loc[discrepancy].sub(duration.loc[discrepancy]).abs().round(6),
    })
    summary = {
        "loaded": rows_loaded, "retained": len(cleaned), "rejected": len(rejected),
        "start_failures": int(start_failure.sum()), "stop_failures": int(stop_failure.sum()),
        "negative": int(negative.sum()),
    }
    print(f"[{LABEL}] Rows loaded: {rows_loaded:,}")
    print(f"[{LABEL}] Rows retained: {len(cleaned):,}")
    print(f"[{LABEL}] Rows rejected: {len(rejected):,}")
    return cleaned, rejected, duration_qc, summary


def validate_output(data: pd.DataFrame) -> None:
    if list(data.columns) != OUTPUT_COLUMNS or data.empty:
        raise ValueError("Yorkshire output does not have the exact five-column contract.")
    start = pd.to_datetime(data["start_time"], format=OUTPUT_TIME_FORMAT, errors="coerce")
    stop = pd.to_datetime(data["stop_time"], format=OUTPUT_TIME_FORMAT, errors="coerce")
    duration = pd.to_numeric(data["duration_minutes"], errors="coerce")
    if start.isna().any() or stop.isna().any() or stop.lt(start).any():
        raise ValueError("Yorkshire output contains an invalid timestamp pair.")
    if duration.isna().any() or duration.lt(0).any() or not duration.map(math.isfinite).all():
        raise ValueError("Yorkshire output contains an invalid duration.")
    expected = (stop - start).dt.total_seconds() / 60
    if duration.sub(expected).abs().gt((1 / 60) + 1e-9).any():
        raise ValueError("Yorkshire duration differs from timestamps by over one second.")


def clean_yorkshire(*, dry_run: bool = False, strict: bool = False) -> int:
    results = [clean_workbook(*source) for source in SOURCE_WORKSHEETS]
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
    print(f"[{LABEL}] Files processed: {len(SOURCE_WORKSHEETS)}")
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
        assert parse_timestamps(pd.Series(["2025-01-02T03:04:05.000Z"])).notna().all()
        assert clean_permits(pd.Series(["001/ABC"])).iloc[0] == "001/ABC"
        print(f"[{LABEL}] Self-check passed")
        return 0
    return clean_yorkshire(dry_run=arguments.dry_run, strict=arguments.strict)


if __name__ == "__main__":
    raise SystemExit(main())
