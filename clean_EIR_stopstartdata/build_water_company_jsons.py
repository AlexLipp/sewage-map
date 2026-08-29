"""Build exact-match, website-format JSON from consolidated EDM event CSVs."""

from __future__ import annotations

import argparse
import cProfile
import io
import json
import logging
import math
import os
import pstats
import re
import sys
import tempfile
import time
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, urlsplit, urlunsplit

import numpy as np
import pandas as pd
import requests
from pyproj import Transformer
from rapidfuzz import fuzz, process
from tqdm.auto import tqdm


ONLY_COMPANIES: list[str] | None = None  # Backwards-compatible imported setting.
PROJECT_ROOT = Path(__file__).resolve().parent
INPUT_ROOT = PROJECT_ROOT / "input_stopstart_data"
OUTPUT_ROOT = PROJECT_ROOT / "output_jsons"
UNMATCHED_REPORT = PROJECT_ROOT / "unmatched_spill_events.txt"
REFERENCE_JSON = PROJECT_ROOT / "reference_thames_format.json"

LOCAL_TIMEZONE = "Europe/London"
ARCGIS_PAGE_SIZE = 2000
ARCGIS_MAX_PAGES = 1000
API_SUGGESTION_MINIMUM_SCORE = 0.70
API_SUGGESTION_LIMIT = 3
DEFAULT_CHUNK_SIZE = 100_000
REPORT_BATCH_SIZE = 1_000
NULL_TEXT = {"", "nan", "none", "nat", "<na>", "null", "n/a"}

OUTPUT_COLUMNS = [
    "LocationName", "PermitNumber", "X", "Y", "ReceivingWaterCourse",
    "StartDateTime", "StopDateTime", "Duration", "OngoingEvent",
]
REQUIRED_INPUT_COLUMNS = [
    "location_name", "permit_number", "start_time", "stop_time", "duration_minutes",
]
API_LOOKUP_COLUMNS = [
    "normalised_permit_key", "api_matched_id", "X", "Y", "ReceivingWaterCourse",
]
EXCLUSION_COLUMNS = [
    "company", "input_csv", "csv_row_number", "location_name", "permit_number",
    "normalised_permit_key", "start_time", "stop_time", "duration_minutes", "reasons",
]

COMPANIES: dict[str, str] = {
    "anglian": "https://services3.arcgis.com/VCOY1atHWVcDlvlJ/arcgis/rest/services/stream_service_outfall_locations_view/FeatureServer/0/query",
    "northumbrian": "https://services-eu1.arcgis.com/MSNNjkZ51iVh8yBj/arcgis/rest/services/Northumbrian_Water_Storm_Overflow_Activity_2_view/FeatureServer/0/query",
    "severn_trent": "https://services1.arcgis.com/NO7lTIlnxRMMG9Gw/arcgis/rest/services/Severn_Trent_Water_Storm_Overflow_Activity/FeatureServer/0/query",
    "south_west_water": "https://services-eu1.arcgis.com/OMdMOtfhATJPcHe3/arcgis/rest/services/NEH_outlets_PROD/FeatureServer/0/query?outFields=*&where=1%3D1&f=geojson",
    "southern_water": "https://services-eu1.arcgis.com/6qJmARkS2dt2IjVA/arcgis/rest/services/SouthernWater_StormOverflowActivity_PROD_view/FeatureServer/0/query",
    "united_utilities": "https://services5.arcgis.com/5eoLvR0f8HKb7HWP/arcgis/rest/services/United_Utilities_Storm_Overflow_Activity/FeatureServer/0/query",
    "wessex": "https://services.arcgis.com/3SZ6e0uCvPROr4mS/arcgis/rest/services/Wessex_Water_Storm_Overflow_Activity/FeatureServer/0/query",
    "yorkshire": "https://services-eu1.arcgis.com/1WqkK5cDKUbF0CkH/arcgis/rest/services/Yorkshire_Water_Storm_Overflow_Activity/FeatureServer/0/query",
}
API_ID_FIELD = "Id"
API_LAT_FIELD = "Latitude"
API_LON_FIELD = "Longitude"
API_WATERCOURSE_FIELD = "ReceivingWaterCourse"

logger = logging.getLogger(__name__)


class LevelPrefixFormatter(logging.Formatter):
    """Prefix warning/error messages while keeping INFO output compact."""

    def format(self, record: logging.LogRecord) -> str:
        message = super().format(record)
        return message if record.levelno <= logging.INFO else f"{record.levelname}: {message}"


def configure_logging(level: str = "INFO") -> None:
    handler = logging.StreamHandler()
    handler.setFormatter(LevelPrefixFormatter("%(message)s"))
    logging.basicConfig(level=getattr(logging, level), handlers=[handler], force=True)


def clean_arcgis_url(url: str) -> tuple[str, dict[str, Any]]:
    parts = urlsplit(url)
    return urlunsplit((parts.scheme, parts.netloc, parts.path, "", "")), dict(parse_qsl(parts.query))


def fetch_arcgis_geojson(company: str, api_url: str) -> dict[str, Any]:
    """Fetch every ArcGIS GeoJSON page, freshly, for one company."""
    base_url, base_params = clean_arcgis_url(api_url)
    all_features: list[dict[str, Any]] = []
    offset = 0
    seen_page_signatures: set[str] = set()
    for _page_number in range(ARCGIS_MAX_PAGES):
        params = {
            **base_params, "where": base_params.get("where", "1=1"),
            "outFields": base_params.get("outFields", "*"), "f": "geojson",
            "returnGeometry": "true", "resultOffset": offset,
            "resultRecordCount": ARCGIS_PAGE_SIZE,
        }
        response = requests.get(base_url, params=params, timeout=60)
        response.raise_for_status()
        payload = response.json()
        if "error" in payload:
            raise RuntimeError(f"ArcGIS error for {company}: {payload['error']}")
        features = payload.get("features") or []
        signature = json.dumps(features[:3], sort_keys=True, default=str)
        if signature in seen_page_signatures and features:
            logger.warning("ArcGIS returned a repeated page; pagination stopped safely.")
            break
        seen_page_signatures.add(signature)
        all_features.extend(features)
        exceeded = bool(payload.get("exceededTransferLimit"))
        logger.info("  API page offset=%s features=%s", offset, len(features))
        if not exceeded and len(features) < ARCGIS_PAGE_SIZE or not features:
            break
        offset += len(features)
    else:
        raise RuntimeError(f"ArcGIS pagination exceeded {ARCGIS_MAX_PAGES} pages for {company}.")
    return {"type": "FeatureCollection", "features": all_features}


def feature_properties(feature: dict[str, Any]) -> dict[str, Any]:
    props = feature.get("properties")
    return props if isinstance(props, dict) else {}


def get_property(props: dict[str, Any], field: str) -> Any:
    if field in props:
        return props[field]
    target = field.lower()
    return next((value for key, value in props.items() if key.lower() == target), None)


def require_api_schema(company: str, features: list[dict[str, Any]]) -> None:
    if not features:
        raise RuntimeError(f"{company} API returned no features.")
    props = feature_properties(features[0])
    keys = {key.lower() for key in props}
    missing = [
        field for field in (API_ID_FIELD, API_LAT_FIELD, API_LON_FIELD, API_WATERCOURSE_FIELD)
        if field.lower() not in keys
    ]
    if missing:
        raise RuntimeError(f"{company} API is missing expected fields {missing}; returned {sorted(props)}")


def _clean_optional_text(value: Any) -> str:
    """Scalar cleaner retained for API values and report rendering only."""
    if value is None or pd.isna(value):
        return ""
    text = re.sub(r"\s+", " ", str(value).strip())
    return "" if text.casefold() in NULL_TEXT else text


def normalise_permit(value: Any) -> str:
    text = _clean_optional_text(value).upper()
    return re.sub(r"^([+-]?\d+)\.0$", r"\1", text)


def clean_optional_text_series(series: pd.Series) -> pd.Series:
    """Clean an event-sized Series without one Python call per row."""
    cleaned = series.astype("string").fillna("").str.strip().str.replace(r"\s+", " ", regex=True)
    return cleaned.mask(cleaned.str.casefold().isin(NULL_TEXT), "").fillna("")


def normalise_permit_series(series: pd.Series) -> pd.Series:
    """Apply the exact permit policy to a complete Series."""
    return clean_optional_text_series(series).str.upper().str.replace(
        r"^([+-]?\d+)\.0$", r"\1", regex=True
    )


def in_bng_bounds(x_value: float, y_value: float) -> bool:
    return 0 <= x_value <= 700000 and 0 <= y_value <= 1300000


def build_api_lookup(features: list[dict[str, Any]], transformer: Transformer) -> dict[str, Any]:
    """Vector-project API coordinates and return one safe row per exact permit."""
    records = [{
        "api_matched_id": get_property(feature_properties(feature), API_ID_FIELD),
        "longitude": get_property(feature_properties(feature), API_LON_FIELD),
        "latitude": get_property(feature_properties(feature), API_LAT_FIELD),
        "ReceivingWaterCourse": get_property(feature_properties(feature), API_WATERCOURSE_FIELD),
    } for feature in features]
    if not records:
        return {"frame": pd.DataFrame(columns=API_LOOKUP_COLUMNS), "ambiguous": {},
                "ambiguous_keys": set(), "identical_api_duplicates_collapsed": 0,
                "conflicting_api_ids": 0, "api_keys": []}
    raw = pd.DataFrame.from_records(records)
    raw["api_matched_id"] = clean_optional_text_series(raw["api_matched_id"])
    raw["normalised_permit_key"] = normalise_permit_series(raw["api_matched_id"])
    raw["ReceivingWaterCourse"] = clean_optional_text_series(raw["ReceivingWaterCourse"]).replace("", pd.NA)
    raw = raw.loc[raw["normalised_permit_key"].ne("")].copy()
    lon = pd.to_numeric(raw["longitude"], errors="coerce").to_numpy(dtype=float)
    lat = pd.to_numeric(raw["latitude"], errors="coerce").to_numpy(dtype=float)
    valid = np.isfinite(lon) & np.isfinite(lat)
    x_values = np.full(len(raw), np.nan)
    y_values = np.full(len(raw), np.nan)
    if valid.any():
        projected_x, projected_y = transformer.transform(lon[valid], lat[valid])
        x_values[valid] = np.asarray(projected_x, dtype=float)
        y_values[valid] = np.asarray(projected_y, dtype=float)
    projectable = np.isfinite(x_values) & np.isfinite(y_values)
    raw["X"] = pd.Series(np.where(projectable, np.rint(x_values), np.nan), index=raw.index).astype("Int64")
    raw["Y"] = pd.Series(np.where(projectable, np.rint(y_values), np.nan), index=raw.index).astype("Int64")

    relevant = ["X", "Y", "ReceivingWaterCourse"]
    disagreement = raw.groupby("normalised_permit_key", sort=False)[relevant].nunique(dropna=False).max(axis=1).gt(1)
    ambiguous_keys = set(disagreement.index[disagreement])
    ambiguous = {
        key: raw.loc[raw["normalised_permit_key"].eq(key), API_LOOKUP_COLUMNS].to_dict(orient="records")
        for key in sorted(ambiguous_keys)
    }
    safe = raw.loc[~raw["normalised_permit_key"].isin(ambiguous_keys), API_LOOKUP_COLUMNS]
    sizes = safe.groupby("normalised_permit_key", sort=False).size()
    frame = safe.drop_duplicates("normalised_permit_key").sort_values(
        "normalised_permit_key", kind="stable"
    ).reset_index(drop=True)
    return {
        "frame": frame, "ambiguous": ambiguous, "ambiguous_keys": ambiguous_keys,
        "identical_api_duplicates_collapsed": int((sizes - 1).clip(lower=0).sum()),
        "conflicting_api_ids": len(ambiguous_keys),
        "api_keys": sorted(raw["normalised_permit_key"].unique()),
    }


def _company_csv_files(company: str) -> list[Path]:
    folder = INPUT_ROOT / company
    files = sorted(folder.glob("*.csv")) if folder.exists() else []
    if files:
        return files
    stem = "northumbria" if company == "northumbrian" else company
    flat = INPUT_ROOT / f"{stem}_cleaned_data.csv"
    return [flat] if flat.exists() else []


def load_company_csvs(company: str) -> tuple[pd.DataFrame, list[Path], list[str]]:
    """Read only required fields and preserve physical CSV row numbers."""
    csv_files = _company_csv_files(company)
    if not csv_files:
        return pd.DataFrame(), [], [f"No input CSV found for {company} in {INPUT_ROOT}"]
    frames: list[pd.DataFrame] = []
    errors: list[str] = []
    for csv_path in csv_files:
        try:
            header = pd.read_csv(csv_path, nrows=0)
            missing = [column for column in REQUIRED_INPUT_COLUMNS if column not in header.columns]
            if missing:
                errors.append(f"{csv_path.name}: missing required columns: {missing}")
                continue
            frame = pd.read_csv(
                csv_path, dtype="string", keep_default_na=False, usecols=REQUIRED_INPUT_COLUMNS
            )
        except (OSError, UnicodeError, ValueError, pd.errors.ParserError) as exc:
            errors.append(f"{csv_path.name}: could not read CSV: {exc}")
            continue
        physical_rows = np.arange(2, len(frame) + 2, dtype=np.int64)
        cleaned = pd.concat(
            [clean_optional_text_series(frame[column]).rename(column) for column in REQUIRED_INPUT_COLUMNS],
            axis=1,
        )
        keep = ~cleaned.eq("").all(axis=1)
        frame = frame.loc[keep].copy()
        frame["_source_file"] = csv_path.name
        frame["_csv_row_number"] = physical_rows[keep.to_numpy()]
        frames.append(frame)
    if not frames:
        return pd.DataFrame(), csv_files, errors
    return pd.concat(frames, ignore_index=True), csv_files, errors


def parse_datetime_to_epoch_ms(series: pd.Series) -> tuple[pd.Series, pd.Series]:
    parsed = pd.to_datetime(series, format="ISO8601", errors="coerce")
    bad = parsed.isna()
    utc = parsed.dt.tz_localize(
        LOCAL_TIMEZONE, ambiguous=True, nonexistent="shift_forward"
    ).dt.tz_convert("UTC")
    epoch = ((utc - pd.Timestamp("1970-01-01", tz="UTC")) // pd.Timedelta("1ms")).astype("Int64")
    return epoch, bad.astype(bool)


def _process_event_chunk(
    company: str, chunk: pd.DataFrame, api_result: dict[str, Any]
) -> tuple[pd.DataFrame, pd.DataFrame, Counter[str]]:
    """Classify one chunk entirely with Series operations and a many-to-one merge."""
    events = pd.DataFrame(index=chunk.index)
    events["_stable_row_id"] = chunk["_stable_row_id"].to_numpy()
    events["LocationName"] = clean_optional_text_series(chunk["location_name"])
    events["PermitNumber"] = clean_optional_text_series(chunk["permit_number"])
    events["normalised_permit_key"] = normalise_permit_series(events["PermitNumber"])
    events["start_time"] = clean_optional_text_series(chunk["start_time"])
    events["stop_time"] = clean_optional_text_series(chunk["stop_time"])
    events["duration_minutes"] = clean_optional_text_series(chunk["duration_minutes"])
    events["input_csv"] = clean_optional_text_series(chunk["_source_file"])
    events["csv_row_number"] = chunk["_csv_row_number"].to_numpy(dtype=np.int64)
    events["StartDateTime"], bad_start = parse_datetime_to_epoch_ms(events["start_time"])
    events["StopDateTime"], bad_stop = parse_datetime_to_epoch_ms(events["stop_time"])
    events["Duration"] = pd.to_numeric(events["duration_minutes"], errors="coerce")
    duration_values = events["Duration"].to_numpy(dtype=float, na_value=np.nan)
    invalid_duration = pd.Series(~np.isfinite(duration_values) | (duration_values < 0), index=events.index)

    merged = events.merge(
        api_result["frame"], on="normalised_permit_key", how="left",
        validate="many_to_one", sort=False,
    )
    if len(merged) != len(events) or not np.array_equal(
        merged["_stable_row_id"].to_numpy(), events["_stable_row_id"].to_numpy()
    ):
        raise ValueError("Exact API merge changed event count or input order.")
    bad_start = bad_start.reset_index(drop=True)
    bad_stop = bad_stop.reset_index(drop=True)
    invalid_duration = invalid_duration.reset_index(drop=True)
    location_blank = merged["LocationName"].eq("")
    permit_blank = merged["PermitNumber"].eq("")
    key_present = merged["normalised_permit_key"].ne("")
    ambiguous = merged["normalised_permit_key"].isin(api_result["ambiguous_keys"])
    api_matched = merged["api_matched_id"].notna()
    coordinates_present = merged["X"].notna() & merged["Y"].notna()

    reason_masks: list[tuple[str, pd.Series]] = [
        ("MISSING_LOCATION_AND_PERMIT", location_blank & permit_blank),
        ("MISSING_LOCATION_NAME", location_blank & ~permit_blank),
        ("MISSING_PERMIT_NUMBER", permit_blank & ~location_blank),
        ("INVALID_START_TIME", bad_start),
        ("INVALID_STOP_TIME", bad_stop),
        ("STOP_BEFORE_START", ~bad_start & ~bad_stop & merged["StopDateTime"].lt(merged["StartDateTime"])),
        ("INVALID_DURATION", invalid_duration),
        ("AMBIGUOUS_DUPLICATE_API_ID", key_present & ambiguous),
        ("NO_EXACT_API_MATCH", key_present & ~ambiguous & ~api_matched),
        ("API_MATCH_MISSING_COORDINATES", api_matched & ~coordinates_present),
        ("API_COORDINATES_OUTSIDE_BNG", api_matched & coordinates_present & ~(
            merged["X"].between(0, 700000) & merged["Y"].between(0, 1300000)
        )),
    ]
    reasons = pd.Series("", index=merged.index, dtype="string")
    counts: Counter[str] = Counter()
    for code, mask in reason_masks:
        counts[code] = int(mask.sum())
        current = reasons.loc[mask]
        reasons.loc[mask] = current.mask(current.eq(""), code).where(current.eq(""), current + ";" + code)
    excluded_mask = reasons.ne("")
    merged["reasons"] = reasons
    merged["company"] = company
    excluded = merged.loc[excluded_mask, [
        "company", "input_csv", "csv_row_number", "LocationName", "PermitNumber",
        "normalised_permit_key", "start_time", "stop_time", "duration_minutes", "reasons",
    ]].rename(columns={"LocationName": "location_name", "PermitNumber": "permit_number"})
    included = merged.loc[~excluded_mask, OUTPUT_COLUMNS[:-1]].copy()
    included["OngoingEvent"] = False
    included = included[OUTPUT_COLUMNS].reset_index(drop=True)
    included["LocationName"] = included["LocationName"].astype("string")
    included["PermitNumber"] = included["PermitNumber"].astype("string")
    included["X"] = included["X"].astype("int64")
    included["Y"] = included["Y"].astype("int64")
    included["StartDateTime"] = included["StartDateTime"].astype("int64")
    included["StopDateTime"] = included["StopDateTime"].astype("int64")
    included["Duration"] = included["Duration"].astype("float64")
    included["OngoingEvent"] = included["OngoingEvent"].astype(bool)
    return included, excluded.reset_index(drop=True), counts


def closest_api_candidates(key: str, api_keys: list[str]) -> list[tuple[str, float]]:
    matches = process.extract(
        key, api_keys, scorer=fuzz.ratio, limit=API_SUGGESTION_LIMIT,
        score_cutoff=API_SUGGESTION_MINIMUM_SCORE * 100,
    )
    return [(str(candidate), round(float(score) / 100, 3)) for candidate, score, _index in matches]


def evaluate_events(
    company: str, raw_df: pd.DataFrame, api_result: dict[str, Any],
    chunk_size: int = DEFAULT_CHUNK_SIZE, progress: bool = False,
) -> tuple[pd.DataFrame, pd.DataFrame, dict[str, Any]]:
    """Process event chunks vectorially and concatenate each result once."""
    if chunk_size <= 0:
        raise ValueError("chunk_size must be positive.")
    raw_df = raw_df.copy()
    raw_df["_stable_row_id"] = np.arange(len(raw_df), dtype=np.int64)
    included_chunks: list[pd.DataFrame] = []
    excluded_chunks: list[pd.DataFrame] = []
    reason_counts: Counter[str] = Counter()
    label = company.replace("_", " ").title()
    with tqdm(total=len(raw_df), desc=label, unit="events", disable=not progress, dynamic_ncols=True) as bar:
        for start in range(0, len(raw_df), chunk_size):
            chunk = raw_df.iloc[start:start + chunk_size]
            included, excluded, counts = _process_event_chunk(company, chunk, api_result)
            included_chunks.append(included)
            excluded_chunks.append(excluded)
            reason_counts.update(counts)
            bar.update(len(chunk))
    output = pd.concat(included_chunks, ignore_index=True) if included_chunks else pd.DataFrame(columns=OUTPUT_COLUMNS)
    excluded = pd.concat(excluded_chunks, ignore_index=True) if excluded_chunks else pd.DataFrame(columns=EXCLUSION_COLUMNS)

    suggestion_started = time.perf_counter()
    unmatched = excluded.loc[
        excluded["reasons"].str.contains("NO_EXACT_API_MATCH", regex=False), "normalised_permit_key"
    ].drop_duplicates().sort_values()
    suggestion_map: dict[str, list[tuple[str, float]]] = {}
    for key in tqdm(
        unmatched, desc=f"{label} unmatched IDs", unit="IDs",
        disable=not progress or len(unmatched) < 50, dynamic_ncols=True,
    ):
        suggestion_map[str(key)] = closest_api_candidates(str(key), api_result["api_keys"])
    stats = {
        "reason_counts": reason_counts,
        "matched_unique_permits": int(normalise_permit_series(output["PermitNumber"]).nunique()) if not output.empty else 0,
        "unmatched_unique_permits": int(excluded.loc[excluded["normalised_permit_key"].ne(""), "normalised_permit_key"].nunique()),
        "suggestion_map": suggestion_map,
        "suggested_unique_permits": len(suggestion_map),
        "suggestion_seconds": time.perf_counter() - suggestion_started,
    }
    return output, excluded, stats


def validate_output_dataframe(output: pd.DataFrame, input_rows: int, excluded_rows: int) -> dict[str, bool]:
    """Perform all large-data validation before JSON serialisation."""
    keys_match = list(output.columns) == OUTPUT_COLUMNS
    range_index = output.index.equals(pd.RangeIndex(len(output)))
    identities = all(clean_optional_text_series(output[column]).ne("").all() for column in ("LocationName", "PermitNumber"))
    xy_integer = all(pd.api.types.is_integer_dtype(output[column].dtype) for column in ("X", "Y"))
    xy_bng = bool((output["X"].between(0, 700000) & output["Y"].between(0, 1300000)).all())
    datetime_integer = all(pd.api.types.is_integer_dtype(output[column].dtype) for column in ("StartDateTime", "StopDateTime"))
    chronology = bool(output["StopDateTime"].ge(output["StartDateTime"]).all())
    duration_values = output["Duration"].to_numpy(dtype=float, na_value=np.nan)
    duration_valid = bool((np.isfinite(duration_values) & (duration_values >= 0)).all())
    ongoing_false = bool(pd.api.types.is_bool_dtype(output["OngoingEvent"].dtype) and (~output["OngoingEvent"]).all())
    row_accounting = input_rows == len(output) + excluded_rows
    checks = {
        "keys_match": keys_match, "range_index": range_index, "identity_complete": identities,
        "xy_integer": xy_integer, "xy_bng": xy_bng, "datetime_integer": datetime_integer,
        "chronology_valid": chronology, "duration_valid": duration_valid,
        "ongoing_false": ongoing_false, "row_accounting": row_accounting,
    }
    checks["passed"] = all(checks.values())
    return checks


def _lightweight_json_validation(path: Path, expected_rows: int) -> dict[str, bool]:
    """Verify serializer structure without parsing the complete large JSON."""
    exists_nonempty = path.exists() and path.stat().st_size > 2
    patterns = [f'"{column}":{{'.encode() for column in OUTPUT_COLUMNS]
    positions = {pattern: -1 for pattern in patterns}
    offset = 0
    carry = b""
    first = b""
    tail = b""
    with path.open("rb") as handle:
        while chunk := handle.read(1024 * 1024):
            if not first:
                first = chunk[:1]
            haystack = carry + chunk
            base = offset - len(carry)
            for pattern in patterns:
                if positions[pattern] < 0:
                    found = haystack.find(pattern)
                    if found >= 0:
                        positions[pattern] = base + found
            carry = haystack[-128:]
            tail = (tail + chunk)[-4096:]
            offset += len(chunk)
    ordered_keys = [positions[pattern] for pattern in patterns]
    keys_match = all(position >= 0 for position in ordered_keys) and ordered_keys == sorted(ordered_keys)
    braces = first == b"{" and tail.rstrip().endswith(b"}")
    if expected_rows:
        match = re.search(rb'"(\d+)":false}\s*}$', tail.rstrip())
        row_count = bool(match and int(match.group(1)) + 1 == expected_rows)
    else:
        row_count = tail.rstrip().endswith(b'"OngoingEvent":{}}')
    result = {"file_exists_nonempty": exists_nonempty, "keys_match": keys_match,
              "orientation_matches": braces, "row_count_matches": row_count}
    result["passed"] = all(result.values())
    return result


def _deep_json_validation(path: Path, expected_rows: int) -> dict[str, bool]:
    with path.open("r", encoding="utf-8") as handle:
        data = json.load(handle)
    keys = list(data) == OUTPUT_COLUMNS
    orientation = all(isinstance(data.get(column), dict) for column in OUTPUT_COLUMNS)
    counts = [len(data.get(column, {})) for column in OUTPUT_COLUMNS]
    rows = bool(counts and len(set(counts)) == 1 and counts[0] == expected_rows)
    return {"keys_match": keys, "orientation_matches": orientation,
            "row_count_matches": rows, "passed": keys and orientation and rows}


def _atomic_write_json(
    output: pd.DataFrame, path: Path, input_rows: int, excluded_rows: int,
    deep_validate: bool = False,
) -> tuple[dict[str, Any], float, float]:
    validation_started = time.perf_counter()
    frame_validation = validate_output_dataframe(output, input_rows, excluded_rows)
    validation_seconds = time.perf_counter() - validation_started
    if not frame_validation["passed"]:
        raise ValueError(f"Pre-write JSON DataFrame validation failed: {frame_validation}")
    temporary = path.with_name(f".{path.name}.tmp-{os.getpid()}")
    try:
        writing_started = time.perf_counter()
        output[OUTPUT_COLUMNS].reset_index(drop=True).to_json(temporary, orient="columns")
        writing_seconds = time.perf_counter() - writing_started
        post_started = time.perf_counter()
        lightweight = _lightweight_json_validation(temporary, len(output))
        deep = _deep_json_validation(temporary, len(output)) if deep_validate else {"passed": True}
        validation_seconds += time.perf_counter() - post_started
        if not lightweight["passed"] or not deep["passed"]:
            raise ValueError(f"Post-write JSON validation failed: light={lightweight}, deep={deep}")
        temporary.replace(path)
        return {"passed": True, "frame": frame_validation, "lightweight": lightweight, "deep": deep}, validation_seconds, writing_seconds
    finally:
        if temporary.exists():
            temporary.unlink()


def _summary(company: str, raw_df: pd.DataFrame, csv_files: list[Path]) -> dict[str, Any]:
    return {
        "company": company, "total_input_rows": int(len(raw_df)), "eligible_rows": 0,
        "total_output_rows": 0, "excluded_rows": 0,
        "input_csv_files": ";".join(path.name for path in csv_files),
        "missing_location_rows": 0, "missing_permit_rows": 0, "missing_both_rows": 0,
        "no_exact_api_match_rows": 0, "ambiguous_api_match_rows": 0,
        "api_missing_coordinate_rows": 0, "outside_bng_rows": 0,
        "bad_start_rows": 0, "bad_stop_rows": 0, "stop_before_start_rows": 0,
        "invalid_duration_rows": 0, "matched_unique_permits": 0,
        "unmatched_unique_permits": 0, "identical_api_duplicates_collapsed": 0,
        "conflicting_api_ids": 0, "api_features_fetched": 0,
        "api_similarity_suggestions_generated": 0, "json_validation_passed": False,
        "error_message": "", "not_evaluated": False, "loading_seconds": 0.0,
        "api_fetching_seconds": 0.0, "api_lookup_seconds": 0.0,
        "classification_seconds": 0.0, "suggestion_seconds": 0.0,
        "json_validation_seconds": 0.0, "json_writing_seconds": 0.0,
        "unmatched_report_writing_seconds": 0.0, "total_seconds": 0.0,
        "events_per_second": 0.0, "_reason_counts": Counter(),
        "_suggestion_map": {}, "_ambiguous": {},
    }


def enrich_company(
    company: str, api_url: str, raw_df: pd.DataFrame, csv_files: list[Path],
    chunk_size: int, progress: bool, deep_validate: bool,
) -> tuple[dict[str, Any], pd.DataFrame]:
    total_started = time.perf_counter()
    summary = _summary(company, raw_df, csv_files)
    logger.info("[2/6] Fetching ArcGIS data")
    started = time.perf_counter()
    features = fetch_arcgis_geojson(company, api_url).get("features") or []
    summary["api_fetching_seconds"] = time.perf_counter() - started
    require_api_schema(company, features)
    logger.info("[3/6] Building API lookup")
    started = time.perf_counter()
    transformer = Transformer.from_crs("EPSG:4326", "EPSG:27700", always_xy=True)
    api_result = build_api_lookup(features, transformer)
    summary["api_lookup_seconds"] = time.perf_counter() - started
    logger.info("[4/6] Classifying and joining events")
    started = time.perf_counter()
    output, excluded, stats = evaluate_events(company, raw_df, api_result, chunk_size, progress)
    summary["classification_seconds"] = time.perf_counter() - started - stats["suggestion_seconds"]
    summary["suggestion_seconds"] = stats["suggestion_seconds"]
    logger.info("[5/6] Writing Thames-format JSON")
    validation, validation_seconds, writing_seconds = _atomic_write_json(
        output, OUTPUT_ROOT / f"{company}.json", len(raw_df), len(excluded), deep_validate
    )
    reasons = stats["reason_counts"]
    summary.update({
        "eligible_rows": len(output), "total_output_rows": len(output), "excluded_rows": len(excluded),
        "missing_location_rows": reasons["MISSING_LOCATION_NAME"],
        "missing_permit_rows": reasons["MISSING_PERMIT_NUMBER"],
        "missing_both_rows": reasons["MISSING_LOCATION_AND_PERMIT"],
        "no_exact_api_match_rows": reasons["NO_EXACT_API_MATCH"],
        "ambiguous_api_match_rows": reasons["AMBIGUOUS_DUPLICATE_API_ID"],
        "api_missing_coordinate_rows": reasons["API_MATCH_MISSING_COORDINATES"],
        "outside_bng_rows": reasons["API_COORDINATES_OUTSIDE_BNG"],
        "bad_start_rows": reasons["INVALID_START_TIME"], "bad_stop_rows": reasons["INVALID_STOP_TIME"],
        "stop_before_start_rows": reasons["STOP_BEFORE_START"],
        "invalid_duration_rows": reasons["INVALID_DURATION"],
        "matched_unique_permits": stats["matched_unique_permits"],
        "unmatched_unique_permits": stats["unmatched_unique_permits"],
        "identical_api_duplicates_collapsed": api_result["identical_api_duplicates_collapsed"],
        "conflicting_api_ids": api_result["conflicting_api_ids"], "api_features_fetched": len(features),
        "api_similarity_suggestions_generated": stats["suggested_unique_permits"],
        "json_validation_passed": validation["passed"], "json_validation_seconds": validation_seconds,
        "json_writing_seconds": writing_seconds, "_reason_counts": reasons,
        "_suggestion_map": stats["suggestion_map"], "_ambiguous": api_result["ambiguous"],
    })
    summary["total_seconds"] = time.perf_counter() - total_started
    summary["events_per_second"] = len(raw_df) / summary["classification_seconds"] if summary["classification_seconds"] else 0.0
    return summary, excluded


def _failure_events(raw_df: pd.DataFrame, company: str, reason: str) -> pd.DataFrame:
    """Create company-level failure exclusions with vectorised columns."""
    if raw_df.empty:
        return pd.DataFrame(columns=EXCLUSION_COLUMNS)
    excluded = pd.DataFrame({
        "company": company,
        "input_csv": clean_optional_text_series(raw_df["_source_file"]),
        "csv_row_number": raw_df["_csv_row_number"].astype("int64"),
        "location_name": clean_optional_text_series(raw_df["location_name"]),
        "permit_number": clean_optional_text_series(raw_df["permit_number"]),
        "start_time": clean_optional_text_series(raw_df["start_time"]),
        "stop_time": clean_optional_text_series(raw_df["stop_time"]),
        "duration_minutes": clean_optional_text_series(raw_df["duration_minutes"]),
        "reasons": reason,
    })
    excluded["normalised_permit_key"] = normalise_permit_series(excluded["permit_number"])
    return excluded[EXCLUSION_COLUMNS]


def _shown(value: Any) -> str:
    return _clean_optional_text(value) or "<blank>"


def _write_report_event(
    number: int, event: Any, suggestion_map: dict[str, list[tuple[str, float]]],
    ambiguous: dict[str, list[dict[str, Any]]],
) -> str:
    lines = ["", "-" * 70, f"Event {number}", "-" * 70,
             f"Reason(s): {event.reasons.replace(';', ', ')}", f"Input CSV: {_shown(event.input_csv)}",
             f"CSV row number: {event.csv_row_number}", f"location_name: {_shown(event.location_name)}",
             f"permit_number: {_shown(event.permit_number)}",
             f"normalised_permit_key: {_shown(event.normalised_permit_key)}",
             f"start_time: {_shown(event.start_time)}", f"stop_time: {_shown(event.stop_time)}",
             f"duration_minutes: {_shown(event.duration_minutes)}"]
    if not event.normalised_permit_key:
        lines.append("closest_api_id_candidates: not applicable because permit is blank")
    elif "NO_EXACT_API_MATCH" in event.reasons:
        candidates = suggestion_map.get(event.normalised_permit_key, [])
        if candidates:
            lines.append("closest_api_id_candidates:")
            lines.extend(f"  - {candidate} | similarity={score:.3f}" for candidate, score in candidates)
        else:
            lines.append("closest_api_id_candidates: none above threshold")
    if "AMBIGUOUS_DUPLICATE_API_ID" in event.reasons:
        lines.append("conflicting_api_candidates:")
        for candidate in ambiguous.get(event.normalised_permit_key, []):
            lines.append(f"  - id={_shown(candidate['api_matched_id'])} | X={candidate['X']} | "
                         f"Y={candidate['Y']} | ReceivingWaterCourse={_shown(candidate['ReceivingWaterCourse'])}")
    return "\n".join(lines) + "\n"


def write_unmatched_report(
    summaries: list[dict[str, Any]], exclusions: dict[str, pd.DataFrame], companies: list[str],
    progress: bool, report_path: Path = UNMATCHED_REPORT,
) -> None:
    """Atomically stream every excluded event in bounded buffered batches."""
    by_company = {summary["company"]: summary for summary in summaries}
    reasons: Counter[str] = Counter()
    for summary in summaries:
        reasons.update(summary["_reason_counts"])
    temporary = report_path.with_name(f".{report_path.name}.tmp-{os.getpid()}")
    report_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        with temporary.open("w", encoding="utf-8", newline="\n", buffering=1024 * 1024) as handle:
            handle.write("EDM UNMATCHED AND EXCLUDED SPILL EVENTS\n")
            handle.write(f"Generated: {datetime.now(timezone.utc).isoformat()}\n")
            handle.write(f"Companies processed: {len(companies)} ({', '.join(companies)})\n")
            handle.write(f"Total input events: {sum(s['total_input_rows'] for s in summaries)}\n")
            handle.write(f"Total JSON events written: {sum(s['total_output_rows'] for s in summaries)}\n")
            handle.write(f"Total events excluded: {sum(s['excluded_rows'] for s in summaries)}\n\n")
            handle.write("Similarity candidates are suggestions for manual investigation only.\n")
            handle.write("They were not used for API matching or JSON generation.\n\nSUMMARY BY COMPANY\n\n")
            handle.write(f"{'Company':24} {'Input':>10} {'JSON':>10} {'Excluded':>10}\n")
            for company in companies:
                summary = by_company[company]
                handle.write(f"{company:24} {summary['total_input_rows']:>10} {summary['total_output_rows']:>10} {summary['excluded_rows']:>10}\n")
            handle.write("\nSUMMARY BY REASON\n\n")
            if reasons:
                for reason, count in sorted(reasons.items()):
                    if count:
                        handle.write(f"{reason:40} {count}\n")
            else:
                handle.write("No unmatched or excluded spill events were found.\n")
            for company in companies:
                company_started = time.perf_counter()
                summary = by_company[company]
                handle.write(f"\n{'=' * 70}\nCOMPANY: {company.upper()}\n{'=' * 70}\n\n")
                handle.write(f"Input events: {summary['total_input_rows']}\nJSON events written: {summary['total_output_rows']}\nExcluded events: {summary['excluded_rows']}\n")
                if summary["error_message"]:
                    handle.write(f"Company/process error: {summary['error_message']}\nInput events evaluated: no\n")
                frame = exclusions.get(company, pd.DataFrame(columns=EXCLUSION_COLUMNS))
                batch: list[str] = []
                iterator = frame[EXCLUSION_COLUMNS].itertuples(index=False, name="ExcludedEvent")
                with tqdm(total=len(frame), desc=f"{company} report", unit="events",
                          disable=not progress or len(frame) < REPORT_BATCH_SIZE, dynamic_ncols=True) as bar:
                    for number, event in enumerate(iterator, start=1):
                        batch.append(_write_report_event(number, event, summary["_suggestion_map"], summary["_ambiguous"]))
                        if len(batch) >= REPORT_BATCH_SIZE:
                            handle.write("".join(batch)); bar.update(len(batch)); batch.clear()
                    if batch:
                        handle.write("".join(batch)); bar.update(len(batch))
                summary["unmatched_report_writing_seconds"] = time.perf_counter() - company_started
            handle.flush()
            os.fsync(handle.fileno())
        temporary.replace(report_path)
    finally:
        if temporary.exists():
            temporary.unlink()


def selected_companies(company: str | None = None) -> list[str]:
    if company:
        return [company]
    selected = list(COMPANIES) if ONLY_COMPANIES is None else list(ONLY_COMPANIES)
    unknown = [name for name in selected if name not in COMPANIES]
    if unknown:
        raise ValueError(f"Unknown company name(s) in ONLY_COMPANIES: {unknown}")
    return selected


def run_self_checks() -> None:
    """Exercise vectorisation, exact matching, chunking, JSON, and report streaming."""
    class IdentityTransformer:
        def transform(self, lon: Any, lat: Any) -> tuple[Any, Any]:
            return np.asarray(lon, dtype=float), np.asarray(lat, dtype=float)

    def feature(permit: str, x: Any, y: Any, watercourse: Any = "River") -> dict[str, Any]:
        return {"properties": {API_ID_FIELD: permit, API_LON_FIELD: x, API_LAT_FIELD: y,
                               API_WATERCOURSE_FIELD: watercourse}}

    scalar_values = [None, " nan ", "  ab  12 ", "0012", "123.0", "A-1/2"]
    assert normalise_permit_series(pd.Series(scalar_values)).tolist() == [normalise_permit(value) for value in scalar_values]
    assert clean_optional_text_series(pd.Series([None, " <NA> ", " A  B "])).tolist() == ["", "", "A B"]
    features = [feature("P1", 400000, 300000), feature("P10", 410000, 310000),
                feature("P2", 420000, 320000), feature("P2", 430000, 330000),
                feature("P3", 440000, 340000), feature("P3", 440000, 340000),
                feature("P4", None, None), feature("P5", 450000, 350000, None),
                feature("P6", 800000, 350000)]
    api = build_api_lookup(features, IdentityTransformer())  # type: ignore[arg-type]
    raw = pd.DataFrame({
        "location_name": ["", "", "Exact", "Similar", "Conflict", "Duplicate", "No coords", "No water",
                          "Bad start", "Bad stop", "Backwards", "Bad duration", "Exact again"],
        "permit_number": ["", "P1", "P1", "P11", "P2", "P3", "P4", "P5", "P1", "P1", "P1", "P1", "P1"],
        "start_time": ["2025-01-01T10:00:00"] * 8 + ["bad", "2025-01-01T10:00:00", "2025-01-01T12:00:00", "2025-01-01T10:00:00", "2025-01-02T10:00:00"],
        "stop_time": ["2025-01-01T11:00:00"] * 9 + ["bad", "2025-01-01T11:00:00", "2025-01-01T11:00:00", "2025-01-02T11:00:00"],
        "duration_minutes": ["60"] * 11 + ["-1", "60"],
        "_source_file": "test.csv", "_csv_row_number": np.arange(2, 15),
    })
    raw.loc[len(raw)] = ["Blank permit", "", "2025-01-03T10:00:00", "2025-01-03T11:00:00", "60", "test.csv", 15]
    raw.loc[len(raw)] = ["Outside grid", "P6", "2025-01-04T10:00:00", "2025-01-04T11:00:00", "60", "test.csv", 16]
    output_a, excluded_a, stats_a = evaluate_events("anglian", raw, api, chunk_size=3, progress=False)
    output_b, excluded_b, _ = evaluate_events("anglian", raw, api, chunk_size=100, progress=False)
    pd.testing.assert_frame_equal(output_a, output_b)
    pd.testing.assert_frame_equal(excluded_a, excluded_b)
    assert len(raw) == len(output_a) + len(excluded_a)
    assert output_a["PermitNumber"].tolist() == ["P1", "P3", "P5", "P1"]
    assert output_a["OngoingEvent"].eq(False).all() and output_a.index.equals(pd.RangeIndex(len(output_a)))
    assert pd.isna(output_a.loc[output_a["PermitNumber"].eq("P5"), "ReceivingWaterCourse"]).all()
    reason_text = ";".join(excluded_a["reasons"])
    for code in ("MISSING_LOCATION_AND_PERMIT", "MISSING_LOCATION_NAME", "MISSING_PERMIT_NUMBER", "INVALID_START_TIME",
                 "INVALID_STOP_TIME", "STOP_BEFORE_START", "INVALID_DURATION", "NO_EXACT_API_MATCH",
                 "AMBIGUOUS_DUPLICATE_API_ID", "API_MATCH_MISSING_COORDINATES", "API_COORDINATES_OUTSIDE_BNG"):
        assert code in reason_text

    # Small scalar oracle is self-check-only; production never classifies row by row.
    api_by_key = api["frame"].set_index("normalised_permit_key").to_dict(orient="index")
    def scalar_reference(row: Any) -> str:
        location = _clean_optional_text(row.location_name)
        permit = _clean_optional_text(row.permit_number)
        key = normalise_permit(permit)
        codes: list[str] = []
        if not location and not permit:
            codes.append("MISSING_LOCATION_AND_PERMIT")
        elif not location:
            codes.append("MISSING_LOCATION_NAME")
        elif not permit:
            codes.append("MISSING_PERMIT_NUMBER")
        start = pd.to_datetime(row.start_time, format="ISO8601", errors="coerce")
        stop = pd.to_datetime(row.stop_time, format="ISO8601", errors="coerce")
        if pd.isna(start): codes.append("INVALID_START_TIME")
        if pd.isna(stop): codes.append("INVALID_STOP_TIME")
        if pd.notna(start) and pd.notna(stop) and stop < start: codes.append("STOP_BEFORE_START")
        try: duration = float(row.duration_minutes)
        except (TypeError, ValueError): duration = math.nan
        if not math.isfinite(duration) or duration < 0: codes.append("INVALID_DURATION")
        if key:
            if key in api["ambiguous_keys"]: codes.append("AMBIGUOUS_DUPLICATE_API_ID")
            elif key not in api_by_key: codes.append("NO_EXACT_API_MATCH")
            else:
                metadata = api_by_key[key]
                if pd.isna(metadata["X"]) or pd.isna(metadata["Y"]): codes.append("API_MATCH_MISSING_COORDINATES")
                elif not in_bng_bounds(float(metadata["X"]), float(metadata["Y"])): codes.append("API_COORDINATES_OUTSIDE_BNG")
        return ";".join(codes)
    vector_reasons = dict(zip(excluded_a["csv_row_number"], excluded_a["reasons"]))
    scalar_rows = list(raw.itertuples())
    assert [vector_reasons.get(int(row_number), "") for row_number in raw["_csv_row_number"]] == [
        scalar_reference(row) for row in scalar_rows
    ]
    assert api["identical_api_duplicates_collapsed"] == 1 and api["conflicting_api_ids"] == 1
    assert stats_a["suggestion_map"]["P11"] and "P11" not in set(output_a["PermitNumber"])
    summary = _summary("anglian", raw, [Path("test.csv")])
    summary.update(total_output_rows=len(output_a), eligible_rows=len(output_a), excluded_rows=len(excluded_a),
                   json_validation_passed=True, _reason_counts=stats_a["reason_counts"],
                   _suggestion_map=stats_a["suggestion_map"], _ambiguous=api["ambiguous"])
    with tempfile.TemporaryDirectory() as directory:
        folder = Path(directory)
        json_path = folder / "anglian.json"
        validation, _, _ = _atomic_write_json(output_a, json_path, len(raw), len(excluded_a), True)
        assert validation["passed"]
        data = json.loads(json_path.read_text(encoding="utf-8"))
        assert list(data) == OUTPUT_COLUMNS and all(isinstance(data[key], dict) for key in OUTPUT_COLUMNS)
        assert list(data["LocationName"]) == [str(index) for index in range(len(output_a))]
        if REFERENCE_JSON.exists():
            reference = json.loads(REFERENCE_JSON.read_text(encoding="utf-8"))
            assert list(reference) == list(data)
            assert all(isinstance(reference[key], dict) for key in OUTPUT_COLUMNS)
        report = folder / "unmatched.txt"
        report.write_text("stale content", encoding="utf-8")
        write_unmatched_report([summary], {"anglian": excluded_a}, ["anglian"], False, report)
        first = report.read_text(encoding="utf-8")
        write_unmatched_report([summary], {"anglian": excluded_a}, ["anglian"], False, report)
        second = report.read_text(encoding="utf-8")
        assert "stale content" not in second and second.count("Reason(s):") == len(excluded_a)
        assert second.count("EDM UNMATCHED AND EXCLUDED SPILL EVENTS") == 1 and len(second) < len(first) + 200
    logger.info("All JSON internal self-checks passed.")


def run_pipeline(args: argparse.Namespace) -> int:
    companies = selected_companies(args.company)
    summaries: list[dict[str, Any]] = []
    exclusions: dict[str, pd.DataFrame] = {}
    failed = False
    OUTPUT_ROOT.mkdir(parents=True, exist_ok=True)
    for company in companies:
        company_total = time.perf_counter()
        logger.info("\n=== Processing %s ===", company)
        logger.info("[1/6] Loading CSV")
        started = time.perf_counter()
        raw_df, csv_files, load_errors = load_company_csvs(company)
        loading_seconds = time.perf_counter() - started
        if load_errors or raw_df.empty:
            message = "; ".join(load_errors) if load_errors else "No input rows found."
            summary = _summary(company, raw_df, csv_files)
            events = _failure_events(raw_df, company, "INPUT_CSV_ERROR")
            summary.update(loading_seconds=loading_seconds, excluded_rows=len(events), error_message=message,
                           not_evaluated=True, _reason_counts=Counter({"INPUT_CSV_ERROR": len(events)}))
            failed = True
        else:
            try:
                summary, events = enrich_company(
                    company, COMPANIES[company], raw_df, csv_files, args.chunk_size,
                    args.progress, args.deep_validate_json,
                )
                summary["loading_seconds"] = loading_seconds
            except (OSError, ValueError, TypeError, RuntimeError, requests.RequestException) as exc:
                summary = _summary(company, raw_df, csv_files)
                events = _failure_events(raw_df, company, "COMPANY_API_FAILURE")
                summary.update(loading_seconds=loading_seconds, excluded_rows=len(events), error_message=str(exc),
                               not_evaluated=True, _reason_counts=Counter({"COMPANY_API_FAILURE": len(events)}))
                logger.error("%s failed; previous valid JSON preserved: %s", company, exc)
                failed = True
        summary["total_seconds"] = time.perf_counter() - company_total
        summaries.append(summary)
        exclusions[company] = events
        del raw_df
    logger.info("[6/6] Writing unmatched report")
    write_unmatched_report(summaries, exclusions, companies, args.progress)
    for summary in summaries:
        if summary["total_input_rows"] != summary["total_output_rows"] + summary["excluded_rows"]:
            logger.error("Row accounting failed for %s.", summary["company"])
            failed = True
        logger.info(
            "%s: input=%s JSON=%s excluded=%s | load=%.2fs API=%.2fs lookup=%.2fs "
            "classify=%.2fs suggest=%.2fs validate=%.2fs JSON-write=%.2fs report=%.2fs "
            "total=%.2fs | %.0f events/s",
            summary["company"], summary["total_input_rows"], summary["total_output_rows"],
            summary["excluded_rows"], summary["loading_seconds"], summary["api_fetching_seconds"],
            summary["api_lookup_seconds"], summary["classification_seconds"], summary["suggestion_seconds"],
            summary["json_validation_seconds"], summary["json_writing_seconds"],
            summary["unmatched_report_writing_seconds"], summary["total_seconds"],
            summary["events_per_second"],
        )
    return 1 if failed else 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--company", choices=list(COMPANIES), help="Process one company only.")
    parser.add_argument("--chunk-size", type=int, default=DEFAULT_CHUNK_SIZE)
    progress_group = parser.add_mutually_exclusive_group()
    progress_group.add_argument("--progress", dest="progress", action="store_true")
    progress_group.add_argument("--no-progress", dest="progress", action="store_false")
    parser.set_defaults(progress=None)
    parser.add_argument("--profile", action="store_true", help="Print cumulative cProfile results.")
    parser.add_argument("--deep-validate-json", action="store_true")
    parser.add_argument("--self-check", action="store_true")
    parser.add_argument("--log-level", choices=["DEBUG", "INFO", "WARNING", "ERROR"], default="INFO")
    return parser


def main() -> int:
    args = build_parser().parse_args()
    args.progress = sys.stderr.isatty() if args.progress is None else args.progress
    configure_logging(args.log_level)
    if args.chunk_size <= 0:
        logger.error("--chunk-size must be positive.")
        return 2
    if args.self_check:
        run_self_checks()
        return 0
    if args.profile:
        profiler = cProfile.Profile()
        result = profiler.runcall(run_pipeline, args)
        stream = io.StringIO()
        pstats.Stats(profiler, stream=stream).strip_dirs().sort_stats("cumulative").print_stats(40)
        logger.info("\n%s", stream.getvalue())
        return result
    return run_pipeline(args)


if __name__ == "__main__":
    raise SystemExit(main())
