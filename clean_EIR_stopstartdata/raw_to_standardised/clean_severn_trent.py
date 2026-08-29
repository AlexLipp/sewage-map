"""Standardise Severn Trent's verified detailed start/stop schemas.

Annual-return summary workbooks are structural, not individual event data.
The 2022 CSV uses unverified ``Site Code`` / ``Discharge start`` fields and is
reported without guessing that they mean the audited fields used below.
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


LABEL = "SEVERN TRENT"
ROOT = Path(__file__).resolve().parents[2]
RAW_FOLDER = ROOT / "raw_data" / "severn_trent"
OUTPUT_FOLDER = ROOT / "clean_EIR_stopstartdata" / "input_stopstart_data"
OUTPUT_FILE = OUTPUT_FOLDER / "severn_trent_cleaned_data.csv"
REJECTED_FILE = OUTPUT_FOLDER / "rejected_rows" / "severn_trent_rejected_rows.csv"
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

# These sheets are annual summaries, guidance, or metadata.  They do not have
# individual start/stop event fields and are intentionally not processed.
STRUCTURAL_SOURCES = [
    ("EDM Return Severn Trent Water 2021.xlsx", ["USER GUIDE - SO", "Storm Overflows", "Drop Downs - SO"]),
    ("EDM-Return-Severn-Trent-Water-Annual-2020 (2).xlsx", ["EDM Return - STW 2020"]),
    ("severn-trent-edm-2023.xlsx", ["Severn Trent 2023"]),
]
UNSUPPORTED_2022_FILE = "start-stop-2022 (1).csv"
ANNUAL_2024_FILE = "stw-start-stop-2024.xlsx"
ANNUAL_2024_SHEET = "Start-Stop 2024"
MONTHLY_2025_FILE = "st-edm-jan-dec-2025.xlsx"
MONTHLY_2025_SHEETS = [
    "January", "February", "March", "April", "May", "June",
    "July", "August", "September", "October", "November", "December",
]
MONTHLY_2026_FILE = "st-edm-data-jan-may-2026.xlsx"
MONTHLY_2026_SHEETS = ["January", "February", "March", "April", "May"]

PERMIT_COLUMN = "Unique ID"
EA_LOCATION_COLUMN = "Site Name\n(EA Consents Database)"
OPERATIONAL_LOCATION_COLUMN = "Site Name\n(WaSC operational)"
START_COLUMN = "Discharge Start (GMT)"
STOP_COLUMN = "Discharge Stop (GMT)"


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


def clean_event_sheet(
    filename: str, sheet_name: str, *, operational_name_fallback: bool
) -> tuple[pd.DataFrame, pd.DataFrame, dict]:
    print(f"[{LABEL}] Reading: {filename} [{sheet_name}]")
    data = pd.read_excel(RAW_FOLDER / filename, sheet_name=sheet_name, header=0, dtype=object)
    rows_loaded = len(data)

    if operational_name_fallback:
        required = [
            PERMIT_COLUMN, EA_LOCATION_COLUMN, OPERATIONAL_LOCATION_COLUMN,
            START_COLUMN, STOP_COLUMN,
        ]
        require_columns(data, required, f"{filename} [{sheet_name}]")
        selected = data[required].copy().rename(columns={
            PERMIT_COLUMN: "permit_original", EA_LOCATION_COLUMN: "location_original",
            OPERATIONAL_LOCATION_COLUMN: "fallback_location_original",
            START_COLUMN: "start_original", STOP_COLUMN: "stop_original",
        })
    else:
        # The 2024 annual file uses Site Name and has no operational fallback.
        required = ["Unique ID", "Site Name", "Discharge Start (GMT)", "Discharge Stop (GMT)"]
        require_columns(data, required, f"{filename} [{sheet_name}]")
        selected = data[required].copy().rename(columns={
            "Unique ID": "permit_original", "Site Name": "location_original",
            "Discharge Start (GMT)": "start_original", "Discharge Stop (GMT)": "stop_original",
        })
    selected["source_row_number"] = range(2, rows_loaded + 2)

    structural_columns = ["permit_original", "location_original", "start_original", "stop_original"]
    if operational_name_fallback:
        structural_columns.append("fallback_location_original")
    structurally_blank = pd.Series(True, index=selected.index)
    for column in structural_columns:
        structurally_blank &= selected[column].map(is_missing)
    selected = selected.loc[~structurally_blank].copy()

    location = clean_text(selected["location_original"], location=True)
    fallback_rows = pd.Series(False, index=selected.index)
    if operational_name_fallback:
        fallback = clean_text(selected["fallback_location_original"], location=True)
        fallback_rows = location.isna() & fallback.notna()
        location.loc[fallback_rows] = fallback.loc[fallback_rows]
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
        originals = {column: selected.at[index, column] for column in structural_columns}
        rejected_rows.append({
            "company": "severn_trent",
            "source_file": f"raw_data/severn_trent/{filename}", "source_sheet": sheet_name,
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
        raise ValueError("Severn Trent output does not have the exact five-column contract.")
    start = pd.to_datetime(data["start_time"], format=OUTPUT_TIME_FORMAT, errors="coerce")
    stop = pd.to_datetime(data["stop_time"], format=OUTPUT_TIME_FORMAT, errors="coerce")
    duration = pd.to_numeric(data["duration_minutes"], errors="coerce")
    if start.isna().any() or stop.isna().any() or stop.lt(start).any():
        raise ValueError("Severn Trent output contains an invalid timestamp pair.")
    if duration.isna().any() or duration.lt(0).any() or not duration.map(math.isfinite).all():
        raise ValueError("Severn Trent output contains an invalid duration.")
    expected = (stop - start).dt.total_seconds() / 60
    if duration.sub(expected).abs().gt((1 / 60) + 1e-9).any():
        raise ValueError("Severn Trent duration differs from timestamps by over one second.")


def clean_severn_trent(*, dry_run: bool = False, strict: bool = False) -> int:
    results = [clean_event_sheet(
        ANNUAL_2024_FILE, ANNUAL_2024_SHEET, operational_name_fallback=False
    )]
    for sheet_name in MONTHLY_2025_SHEETS:
        results.append(clean_event_sheet(
            MONTHLY_2025_FILE, sheet_name, operational_name_fallback=True
        ))
    for sheet_name in MONTHLY_2026_SHEETS:
        results.append(clean_event_sheet(
            MONTHLY_2026_FILE, sheet_name, operational_name_fallback=True
        ))
    print(
        f"[{LABEL}] Unsupported schema: {UNSUPPORTED_2022_FILE} uses Site Code, "
        "Discharge start, and Discharge stop; no verified substitution was made."
    )

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
    print(f"[{LABEL}] Event sheets processed: {len(results)}")
    print(f"[{LABEL}] Total event rows loaded: {sum(s['loaded'] for s in summaries):,}")
    print(f"[{LABEL}] Total rows retained: {len(combined):,}")
    print(f"[{LABEL}] Total rows rejected: {len(rejected):,}")
    print(f"[{LABEL}] Start parse failures: {sum(s['start_failures'] for s in summaries):,}")
    print(f"[{LABEL}] Stop parse failures: {sum(s['stop_failures'] for s in summaries):,}")
    print(f"[{LABEL}] Negative durations: {sum(s['negative'] for s in summaries):,}")
    print(f"[{LABEL}] Operational-name fallbacks: {sum(s['fallback'] for s in summaries):,}")
    if dry_run:
        print(f"[{LABEL}] Dry run: no files written")
    else:
        print(f"[{LABEL}] Output written to: {OUTPUT_FILE}")
        print(f"[{LABEL}] Rejected rows written to: {REJECTED_FILE}")
    # Preserve the old nonzero status for the unsupported 2022 source.
    return 1


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--strict", action="store_true")
    parser.add_argument("--self-check", action="store_true")
    arguments = parser.parse_args()
    if arguments.self_check:
        assert parse_timestamps(pd.Series(["2024-01-02T03:04:05Z"])).notna().all()
        assert clean_permits(pd.Series(["001/ABC"])).iloc[0] == "001/ABC"
        print(f"[{LABEL}] Self-check passed")
        return 0
    return clean_severn_trent(dry_run=arguments.dry_run, strict=arguments.strict)


if __name__ == "__main__":
    raise SystemExit(main())
