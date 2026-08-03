"""Build eight Thames-format website JSON files from standardised EDM CSVs.

All eight CSVs have the same five-column schema, so there is one visible
transformation path.  The company filenames, JSON filenames, and ArcGIS URLs
are deliberately hard-coded below.  Matching is exact after a small documented
permit normalisation; fuzzy candidates are diagnostic suggestions only.
"""

from __future__ import annotations

import argparse
import cProfile
import io
import json
import os
import pstats
import re
import sys
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


# ---------------------------------------------------------------------------
# Paths and the website data contract
# ---------------------------------------------------------------------------

PROJECT_FOLDER = Path(__file__).resolve().parent
INPUT_FOLDER = PROJECT_FOLDER / "input_stopstart_data"
OUTPUT_FOLDER = PROJECT_FOLDER / "output_jsons"
UNMATCHED_REPORT = PROJECT_FOLDER / "unmatched_spill_events.txt"
THAMES_REFERENCE_FILE = (
    PROJECT_FOLDER.parent / "json_cleaning_python_pipeline" / "thames.json"
)

INPUT_COLUMNS = [
    "location_name",
    "permit_number",
    "start_time",
    "stop_time",
    "duration_minutes",
]

# Key order is part of the website format and must not be changed.
OUTPUT_COLUMNS = [
    "LocationName",
    "PermitNumber",
    "X",
    "Y",
    "ReceivingWaterCourse",
    "StartDateTime",
    "StopDateTime",
    "Duration",
    "OngoingEvent",
]

EXCLUSION_COLUMNS = [
    "company",
    "input_csv",
    "csv_row_number",
    "location_name",
    "permit_number",
    "normalised_permit_key",
    "start_time",
    "stop_time",
    "duration_minutes",
    "reasons",
]

LOCAL_TIMEZONE = "Europe/London"
NULL_TEXT = {"", "nan", "none", "nat", "<na>", "null", "n/a"}
ARCGIS_PAGE_SIZE = 2_000
ARCGIS_MAX_PAGES = 1_000
SUGGESTION_MINIMUM_SCORE = 70
SUGGESTION_LIMIT = 3
DEFAULT_CHUNK_SIZE = 100_000

# The exact ArcGIS fields used for every company.  A schema change causes a
# visible failure; another similar-looking field is never selected instead.
API_ID_FIELD = "Id"
API_LATITUDE_FIELD = "Latitude"
API_LONGITUDE_FIELD = "Longitude"
API_WATERCOURSE_FIELD = "ReceivingWaterCourse"


# ---------------------------------------------------------------------------
# Hard-coded company inputs, outputs, and APIs
# ---------------------------------------------------------------------------

ANGLIAN_INPUT = INPUT_FOLDER / "anglian_cleaned_data.csv"
ANGLIAN_OUTPUT = OUTPUT_FOLDER / "anglian.json"
ANGLIAN_API_URL = (
    "https://services3.arcgis.com/VCOY1atHWVcDlvlJ/arcgis/rest/services/"
    "stream_service_outfall_locations_view/FeatureServer/0/query"
)

NORTHUMBRIAN_INPUT = INPUT_FOLDER / "northumbria_cleaned_data.csv"
NORTHUMBRIAN_OUTPUT = OUTPUT_FOLDER / "northumbrian.json"
NORTHUMBRIAN_API_URL = (
    "https://services-eu1.arcgis.com/MSNNjkZ51iVh8yBj/arcgis/rest/services/"
    "Northumbrian_Water_Storm_Overflow_Activity_2_view/FeatureServer/0/query"
)

SEVERN_TRENT_INPUT = INPUT_FOLDER / "severn_trent_cleaned_data.csv"
SEVERN_TRENT_OUTPUT = OUTPUT_FOLDER / "severn_trent.json"
SEVERN_TRENT_API_URL = (
    "https://services1.arcgis.com/NO7lTIlnxRMMG9Gw/arcgis/rest/services/"
    "Severn_Trent_Water_Storm_Overflow_Activity/FeatureServer/0/query"
)

SOUTH_WEST_WATER_INPUT = INPUT_FOLDER / "south_west_water_cleaned_data.csv"
SOUTH_WEST_WATER_OUTPUT = OUTPUT_FOLDER / "south_west_water.json"
SOUTH_WEST_WATER_API_URL = (
    "https://services-eu1.arcgis.com/OMdMOtfhATJPcHe3/arcgis/rest/services/"
    "NEH_outlets_PROD/FeatureServer/0/query?outFields=*&where=1%3D1&f=geojson"
)

SOUTHERN_WATER_INPUT = INPUT_FOLDER / "southern_water_cleaned_data.csv"
SOUTHERN_WATER_OUTPUT = OUTPUT_FOLDER / "southern_water.json"
SOUTHERN_WATER_API_URL = (
    "https://services-eu1.arcgis.com/6qJmARkS2dt2IjVA/arcgis/rest/services/"
    "SouthernWater_StormOverflowActivity_PROD_view/FeatureServer/0/query"
)

UNITED_UTILITIES_INPUT = INPUT_FOLDER / "united_utilities_cleaned_data.csv"
UNITED_UTILITIES_OUTPUT = OUTPUT_FOLDER / "united_utilities.json"
UNITED_UTILITIES_API_URL = (
    "https://services5.arcgis.com/5eoLvR0f8HKb7HWP/arcgis/rest/services/"
    "United_Utilities_Storm_Overflow_Activity/FeatureServer/0/query"
)

WESSEX_INPUT = INPUT_FOLDER / "wessex_cleaned_data.csv"
WESSEX_OUTPUT = OUTPUT_FOLDER / "wessex.json"
WESSEX_API_URL = (
    "https://services.arcgis.com/3SZ6e0uCvPROr4mS/arcgis/rest/services/"
    "Wessex_Water_Storm_Overflow_Activity/FeatureServer/0/query"
)

YORKSHIRE_INPUT = INPUT_FOLDER / "yorkshire_cleaned_data.csv"
YORKSHIRE_OUTPUT = OUTPUT_FOLDER / "yorkshire.json"
YORKSHIRE_API_URL = (
    "https://services-eu1.arcgis.com/1WqkK5cDKUbF0CkH/arcgis/rest/services/"
    "Yorkshire_Water_Storm_Overflow_Activity/FeatureServer/0/query"
)

COMPANY_NAMES = [
    "anglian",
    "northumbrian",
    "severn_trent",
    "south_west_water",
    "southern_water",
    "united_utilities",
    "wessex",
    "yorkshire",
]


# ---------------------------------------------------------------------------
# Text and permit handling
# ---------------------------------------------------------------------------

def clean_text(series: pd.Series) -> pd.Series:
    """Trim text, collapse whitespace, and replace literal null markers."""
    cleaned = (
        series.astype("string")
        .fillna("")
        .str.strip()
        .str.replace(r"\s+", " ", regex=True)
    )
    return cleaned.mask(cleaned.str.casefold().isin(NULL_TEXT), "").fillna("")


def normalise_permits(series: pd.Series) -> pd.Series:
    """Normalise for exact matching while preserving punctuation and zeroes."""
    return (
        clean_text(series)
        .str.upper()
        .str.replace(r"^([+-]?\d+)\.0$", r"\1", regex=True)
    )


def property_value(properties: dict[str, Any], field_name: str) -> Any:
    """Read the same API field case-insensitively; do not guess another field."""
    if field_name in properties:
        return properties[field_name]
    expected_name = field_name.casefold()
    for returned_name, value in properties.items():
        if returned_name.casefold() == expected_name:
            return value
    return None


# ---------------------------------------------------------------------------
# ArcGIS download and exact API lookup
# ---------------------------------------------------------------------------

def fetch_all_api_features(
    company: str, label: str, api_url: str
) -> list[dict[str, Any]]:
    """Fetch every ArcGIS GeoJSON page from one explicit company URL."""
    split_url = urlsplit(api_url)
    base_url = urlunsplit(
        (split_url.scheme, split_url.netloc, split_url.path, "", "")
    )
    endpoint_parameters = dict(parse_qsl(split_url.query))
    all_features = []
    seen_page_signatures = set()
    offset = 0

    for page_number in range(1, ARCGIS_MAX_PAGES + 1):
        parameters = {
            **endpoint_parameters,
            "where": endpoint_parameters.get("where", "1=1"),
            "outFields": endpoint_parameters.get("outFields", "*"),
            "f": "geojson",
            "returnGeometry": "true",
            "resultOffset": offset,
            "resultRecordCount": ARCGIS_PAGE_SIZE,
        }
        response = requests.get(base_url, params=parameters, timeout=60)
        response.raise_for_status()
        payload = response.json()
        if "error" in payload:
            raise RuntimeError(
                f"ArcGIS error for {company}: {payload['error']}"
            )

        features = payload.get("features") or []
        page_signature = json.dumps(
            features[:3], sort_keys=True, default=str
        )
        if features and page_signature in seen_page_signatures:
            print(
                f"[{label}] WARNING: ArcGIS repeated a page; pagination "
                "stopped safely."
            )
            break
        seen_page_signatures.add(page_signature)
        all_features.extend(features)
        print(
            f"[{label}] API page {page_number}: "
            f"{len(features):,} features"
        )

        more_pages = bool(payload.get("exceededTransferLimit"))
        if not features or (
            not more_pages and len(features) < ARCGIS_PAGE_SIZE
        ):
            return all_features
        offset += len(features)
    else:
        raise RuntimeError(
            f"ArcGIS exceeded {ARCGIS_MAX_PAGES} pages for {company}."
        )
    return all_features


def validate_api_schema(
    company: str, features: list[dict[str, Any]]
) -> None:
    """Fail visibly when any of the four exact API fields is absent."""
    if not features:
        raise RuntimeError(f"The {company} API returned no features.")
    first_properties = features[0].get("properties")
    if not isinstance(first_properties, dict):
        raise RuntimeError(f"The {company} API feature has no properties.")

    returned_fields = {str(field).casefold() for field in first_properties}
    required_fields = [
        API_ID_FIELD,
        API_LATITUDE_FIELD,
        API_LONGITUDE_FIELD,
        API_WATERCOURSE_FIELD,
    ]
    missing_fields = []
    for field in required_fields:
        if field.casefold() not in returned_fields:
            missing_fields.append(field)
    if missing_fields:
        raise RuntimeError(
            f"The {company} API is missing fields {missing_fields}. "
            f"Returned fields: {sorted(first_properties)}"
        )


def build_exact_api_lookup(
    features: list[dict[str, Any]]
) -> dict[str, Any]:
    """Project coordinates and retain one safe row per exact permit key."""
    records = []
    for feature in features:
        properties = feature.get("properties")
        if not isinstance(properties, dict):
            properties = {}
        records.append(
            {
                "api_matched_id": property_value(
                    properties, API_ID_FIELD
                ),
                "longitude": property_value(
                    properties, API_LONGITUDE_FIELD
                ),
                "latitude": property_value(
                    properties, API_LATITUDE_FIELD
                ),
                "ReceivingWaterCourse": property_value(
                    properties, API_WATERCOURSE_FIELD
                ),
            }
        )

    api_data = pd.DataFrame.from_records(records)
    api_data["api_matched_id"] = clean_text(api_data["api_matched_id"])
    api_data["normalised_permit_key"] = normalise_permits(
        api_data["api_matched_id"]
    )
    api_data["ReceivingWaterCourse"] = clean_text(
        api_data["ReceivingWaterCourse"]
    ).replace("", pd.NA)
    api_data = api_data.loc[
        api_data["normalised_permit_key"].ne("")
    ].copy()

    longitude = pd.to_numeric(
        api_data["longitude"], errors="coerce"
    ).to_numpy(dtype=float)
    latitude = pd.to_numeric(
        api_data["latitude"], errors="coerce"
    ).to_numpy(dtype=float)
    valid_coordinates = np.isfinite(longitude) & np.isfinite(latitude)
    projected_x = np.full(len(api_data), np.nan)
    projected_y = np.full(len(api_data), np.nan)

    transformer = Transformer.from_crs(
        "EPSG:4326", "EPSG:27700", always_xy=True
    )
    if valid_coordinates.any():
        x_values, y_values = transformer.transform(
            longitude[valid_coordinates], latitude[valid_coordinates]
        )
        projected_x[valid_coordinates] = np.asarray(x_values, dtype=float)
        projected_y[valid_coordinates] = np.asarray(y_values, dtype=float)

    projectable = np.isfinite(projected_x) & np.isfinite(projected_y)
    api_data["X"] = pd.Series(
        np.where(projectable, np.rint(projected_x), np.nan),
        index=api_data.index,
    ).astype("Int64")
    api_data["Y"] = pd.Series(
        np.where(projectable, np.rint(projected_y), np.nan),
        index=api_data.index,
    ).astype("Int64")

    # Repeated IDs are safe only when coordinates and watercourse all agree.
    compared_fields = ["X", "Y", "ReceivingWaterCourse"]
    different_values = (
        api_data.groupby("normalised_permit_key", sort=False)[
            compared_fields
        ]
        .nunique(dropna=False)
        .max(axis=1)
        .gt(1)
    )
    ambiguous_keys = set(different_values.index[different_values])
    ambiguous_rows = {}
    for key in sorted(ambiguous_keys):
        ambiguous_rows[str(key)] = api_data.loc[
            api_data["normalised_permit_key"].eq(key),
            [
                "normalised_permit_key",
                "api_matched_id",
                "X",
                "Y",
                "ReceivingWaterCourse",
            ],
        ].to_dict(orient="records")

    safe_api_data = api_data.loc[
        ~api_data["normalised_permit_key"].isin(ambiguous_keys),
        [
            "normalised_permit_key",
            "api_matched_id",
            "X",
            "Y",
            "ReceivingWaterCourse",
        ],
    ]
    safe_sizes = safe_api_data.groupby(
        "normalised_permit_key", sort=False
    ).size()
    lookup = (
        safe_api_data.drop_duplicates("normalised_permit_key")
        .sort_values("normalised_permit_key", kind="stable")
        .reset_index(drop=True)
    )
    return {
        "lookup": lookup,
        "ambiguous_keys": ambiguous_keys,
        "ambiguous_rows": ambiguous_rows,
        "api_keys": sorted(api_data["normalised_permit_key"].unique()),
        "identical_duplicates_collapsed": int(
            (safe_sizes - 1).clip(lower=0).sum()
        ),
        "conflicting_ids": len(ambiguous_keys),
    }


# ---------------------------------------------------------------------------
# Standardised CSV loading and event classification
# ---------------------------------------------------------------------------

def read_standardised_events(input_file: Path) -> pd.DataFrame:
    """Read one exact five-column CSV and preserve physical row numbers."""
    if not input_file.exists():
        raise FileNotFoundError(f"Input CSV does not exist: {input_file}")
    header = pd.read_csv(input_file, nrows=0)
    missing_columns = []
    for column in INPUT_COLUMNS:
        if column not in header.columns:
            missing_columns.append(column)
    if missing_columns:
        raise KeyError(
            f"Expected columns are missing from {input_file.name}: "
            f"{missing_columns}. Run raw-to-standardised cleaning first."
        )

    events = pd.read_csv(
        input_file,
        usecols=INPUT_COLUMNS,
        dtype="string",
        keep_default_na=False,
    )
    events["_csv_row_number"] = np.arange(
        2, len(events) + 2, dtype=np.int64
    )
    cleaned_fields = pd.concat(
        [clean_text(events[column]).rename(column) for column in INPUT_COLUMNS],
        axis=1,
    )
    structurally_blank = cleaned_fields.eq("").all(axis=1)
    return events.loc[~structurally_blank].reset_index(drop=True)


def timestamps_to_epoch_milliseconds(
    values: pd.Series,
) -> tuple[pd.Series, pd.Series]:
    """Interpret standard timestamps as London time and convert them to UTC."""
    parsed = pd.to_datetime(values, format="ISO8601", errors="coerce")
    parse_failure = parsed.isna()
    localised = parsed.dt.tz_localize(
        LOCAL_TIMEZONE,
        ambiguous=True,
        nonexistent="shift_forward",
    ).dt.tz_convert("UTC")
    epoch = (
        (localised - pd.Timestamp("1970-01-01", tz="UTC"))
        // pd.Timedelta("1ms")
    ).astype("Int64")
    return epoch, parse_failure.astype(bool)


def add_reason(reasons: pd.Series, mask: pd.Series, reason: str) -> None:
    """Add a semicolon-separated reason without losing earlier reasons."""
    current = reasons.loc[mask]
    reasons.loc[mask] = current.mask(
        current.eq(""), reason
    ).where(current.eq(""), current + ";" + reason)


def classify_event_chunk(
    company: str,
    input_filename: str,
    chunk: pd.DataFrame,
    api_lookup: dict[str, Any],
) -> tuple[pd.DataFrame, pd.DataFrame, Counter[str]]:
    """Apply all inclusion and rejection rules to one manageable chunk."""
    events = pd.DataFrame(index=chunk.index)
    events["_stable_row_id"] = chunk["_stable_row_id"].to_numpy()
    events["LocationName"] = clean_text(chunk["location_name"])
    events["PermitNumber"] = clean_text(chunk["permit_number"])
    events["normalised_permit_key"] = normalise_permits(
        events["PermitNumber"]
    )
    events["start_time"] = clean_text(chunk["start_time"])
    events["stop_time"] = clean_text(chunk["stop_time"])
    events["duration_minutes"] = clean_text(chunk["duration_minutes"])
    events["input_csv"] = input_filename
    events["csv_row_number"] = chunk["_csv_row_number"].to_numpy(
        dtype=np.int64
    )

    events["StartDateTime"], invalid_start = (
        timestamps_to_epoch_milliseconds(events["start_time"])
    )
    events["StopDateTime"], invalid_stop = (
        timestamps_to_epoch_milliseconds(events["stop_time"])
    )
    events["Duration"] = pd.to_numeric(
        events["duration_minutes"], errors="coerce"
    )
    duration_values = events["Duration"].to_numpy(
        dtype=float, na_value=np.nan
    )
    invalid_duration = pd.Series(
        ~np.isfinite(duration_values) | (duration_values < 0),
        index=events.index,
    )

    joined = events.merge(
        api_lookup["lookup"],
        on="normalised_permit_key",
        how="left",
        validate="many_to_one",
        sort=False,
    )
    if len(joined) != len(events):
        raise ValueError("Exact API merge changed the event row count.")
    if not np.array_equal(
        joined["_stable_row_id"].to_numpy(),
        events["_stable_row_id"].to_numpy(),
    ):
        raise ValueError("Exact API merge changed input event order.")

    invalid_start = invalid_start.reset_index(drop=True)
    invalid_stop = invalid_stop.reset_index(drop=True)
    invalid_duration = invalid_duration.reset_index(drop=True)
    blank_location = joined["LocationName"].eq("")
    blank_permit = joined["PermitNumber"].eq("")
    key_present = joined["normalised_permit_key"].ne("")
    ambiguous_match = joined["normalised_permit_key"].isin(
        api_lookup["ambiguous_keys"]
    )
    exact_match = joined["api_matched_id"].notna()
    coordinates_present = joined["X"].notna() & joined["Y"].notna()

    reason_masks = [
        ("MISSING_LOCATION_AND_PERMIT", blank_location & blank_permit),
        ("MISSING_LOCATION_NAME", blank_location & ~blank_permit),
        ("MISSING_PERMIT_NUMBER", blank_permit & ~blank_location),
        ("INVALID_START_TIME", invalid_start),
        ("INVALID_STOP_TIME", invalid_stop),
        (
            "STOP_BEFORE_START",
            ~invalid_start
            & ~invalid_stop
            & joined["StopDateTime"].lt(joined["StartDateTime"]),
        ),
        ("INVALID_DURATION", invalid_duration),
        (
            "AMBIGUOUS_DUPLICATE_API_ID",
            key_present & ambiguous_match,
        ),
        (
            "NO_EXACT_API_MATCH",
            key_present & ~ambiguous_match & ~exact_match,
        ),
        (
            "API_MATCH_MISSING_COORDINATES",
            exact_match & ~coordinates_present,
        ),
        (
            "API_COORDINATES_OUTSIDE_BNG",
            exact_match
            & coordinates_present
            & ~(
                joined["X"].between(0, 700_000)
                & joined["Y"].between(0, 1_300_000)
            ),
        ),
    ]

    reasons = pd.Series("", index=joined.index, dtype="string")
    reason_counts: Counter[str] = Counter()
    for reason, mask in reason_masks:
        reason_counts[reason] = int(mask.sum())
        add_reason(reasons, mask, reason)

    excluded_mask = reasons.ne("")
    joined["reasons"] = reasons
    joined["company"] = company
    excluded = joined.loc[
        excluded_mask,
        [
            "company",
            "input_csv",
            "csv_row_number",
            "LocationName",
            "PermitNumber",
            "normalised_permit_key",
            "start_time",
            "stop_time",
            "duration_minutes",
            "reasons",
        ],
    ].rename(
        columns={
            "LocationName": "location_name",
            "PermitNumber": "permit_number",
        }
    )

    included = joined.loc[~excluded_mask, OUTPUT_COLUMNS[:-1]].copy()
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
    return included, excluded.reset_index(drop=True), reason_counts


def classify_all_events(
    company: str,
    input_filename: str,
    events: pd.DataFrame,
    api_lookup: dict[str, Any],
    *,
    chunk_size: int,
    progress: bool,
) -> tuple[pd.DataFrame, pd.DataFrame, Counter[str]]:
    """Process large CSVs in chunks while preserving source order."""
    if chunk_size <= 0:
        raise ValueError("chunk_size must be positive.")
    events = events.copy()
    events["_stable_row_id"] = np.arange(len(events), dtype=np.int64)
    included_chunks = []
    excluded_chunks = []
    all_reason_counts: Counter[str] = Counter()

    progress_bar = tqdm(
        total=len(events),
        desc=company.replace("_", " ").title(),
        unit="events",
        disable=not progress,
        dynamic_ncols=True,
    )
    try:
        for first_row in range(0, len(events), chunk_size):
            chunk = events.iloc[first_row:first_row + chunk_size]
            included, excluded, counts = classify_event_chunk(
                company, input_filename, chunk, api_lookup
            )
            included_chunks.append(included)
            excluded_chunks.append(excluded)
            all_reason_counts.update(counts)
            progress_bar.update(len(chunk))
    finally:
        progress_bar.close()

    included = (
        pd.concat(included_chunks, ignore_index=True)
        if included_chunks
        else pd.DataFrame(columns=OUTPUT_COLUMNS)
    )
    excluded = (
        pd.concat(excluded_chunks, ignore_index=True)
        if excluded_chunks
        else pd.DataFrame(columns=EXCLUSION_COLUMNS)
    )
    return included, excluded, all_reason_counts


def build_manual_suggestions(
    company: str,
    excluded: pd.DataFrame,
    api_keys: list[str],
    *,
    progress: bool,
) -> dict[str, list[tuple[str, float]]]:
    """Suggest similar IDs for review; never use them to include an event."""
    unmatched_keys = (
        excluded.loc[
            excluded["reasons"].str.contains(
                "NO_EXACT_API_MATCH", regex=False
            ),
            "normalised_permit_key",
        ]
        .drop_duplicates()
        .sort_values()
    )
    suggestions = {}
    iterator = tqdm(
        unmatched_keys,
        desc=f"{company} unmatched permits",
        unit="permits",
        disable=not progress or len(unmatched_keys) < 50,
        dynamic_ncols=True,
    )
    for key in iterator:
        matches = process.extract(
            str(key),
            api_keys,
            scorer=fuzz.ratio,
            limit=SUGGESTION_LIMIT,
            score_cutoff=SUGGESTION_MINIMUM_SCORE,
        )
        suggestions[str(key)] = [
            (str(candidate), round(float(score) / 100, 3))
            for candidate, score, _index in matches
        ]
    return suggestions


# ---------------------------------------------------------------------------
# Thames-format validation and safe JSON writing
# ---------------------------------------------------------------------------

def validate_thames_dataframe(
    output: pd.DataFrame, *, input_rows: int, excluded_rows: int
) -> None:
    """Validate the exact nine-key website contract before serialisation."""
    if list(output.columns) != OUTPUT_COLUMNS:
        raise ValueError(f"JSON keys are not exact: {list(output.columns)}")
    if not output.index.equals(pd.RangeIndex(len(output))):
        raise ValueError("JSON index is not a zero-based RangeIndex.")
    if input_rows != len(output) + excluded_rows:
        raise ValueError("Input rows do not equal JSON rows plus exclusions.")
    if clean_text(output["LocationName"]).eq("").any():
        raise ValueError("A JSON event has no LocationName.")
    if clean_text(output["PermitNumber"]).eq("").any():
        raise ValueError("A JSON event has no PermitNumber.")
    for coordinate in ("X", "Y"):
        if not pd.api.types.is_integer_dtype(output[coordinate].dtype):
            raise ValueError(f"{coordinate} is not integer-valued.")
    within_bng = (
        output["X"].between(0, 700_000)
        & output["Y"].between(0, 1_300_000)
    )
    if not within_bng.all():
        raise ValueError("A coordinate is outside British National Grid.")
    for timestamp in ("StartDateTime", "StopDateTime"):
        if not pd.api.types.is_integer_dtype(output[timestamp].dtype):
            raise ValueError(f"{timestamp} is not integer epoch data.")
    if output["StopDateTime"].lt(output["StartDateTime"]).any():
        raise ValueError("A JSON event stops before it starts.")
    durations = output["Duration"].to_numpy(dtype=float, na_value=np.nan)
    if not (np.isfinite(durations) & (durations >= 0)).all():
        raise ValueError("A JSON event has an invalid Duration.")
    if not pd.api.types.is_bool_dtype(output["OngoingEvent"].dtype):
        raise ValueError("OngoingEvent is not boolean.")
    if output["OngoingEvent"].any():
        raise ValueError("Historical OngoingEvent values must all be false.")


def validate_reference_key_order() -> None:
    """Confirm this script's key order agrees with the Thames JSON when present."""
    if not THAMES_REFERENCE_FILE.exists():
        return
    with THAMES_REFERENCE_FILE.open("r", encoding="utf-8") as handle:
        reference = json.load(handle)
    if list(reference) != OUTPUT_COLUMNS:
        raise ValueError(
            "OUTPUT_COLUMNS no longer match the Thames reference key order."
        )
    for column in OUTPUT_COLUMNS:
        if not isinstance(reference[column], dict):
            raise ValueError(
                f"The Thames reference column {column} is not index-oriented."
            )


def validate_json_lightweight(path: Path, expected_rows: int) -> None:
    """Check key order/orientation without loading a newly written large file."""
    key_patterns = [
        f'"{column}":{{'.encode("utf-8") for column in OUTPUT_COLUMNS
    ]
    positions = {pattern: -1 for pattern in key_patterns}
    byte_offset = 0
    carry = b""
    first_byte = b""
    file_tail = b""

    with path.open("rb") as handle:
        while True:
            chunk = handle.read(1024 * 1024)
            if not chunk:
                break
            if not first_byte:
                first_byte = chunk[:1]
            searchable = carry + chunk
            searchable_offset = byte_offset - len(carry)
            for pattern in key_patterns:
                if positions[pattern] < 0:
                    found_at = searchable.find(pattern)
                    if found_at >= 0:
                        positions[pattern] = searchable_offset + found_at
            carry = searchable[-128:]
            file_tail = (file_tail + chunk)[-4096:]
            byte_offset += len(chunk)

    ordered_positions = [positions[pattern] for pattern in key_patterns]
    keys_in_order = (
        all(position >= 0 for position in ordered_positions)
        and ordered_positions == sorted(ordered_positions)
    )
    complete_object = (
        first_byte == b"{" and file_tail.rstrip().endswith(b"}")
    )
    if expected_rows:
        final_index = re.search(
            rb'"(\d+)":false}\s*}$', file_tail.rstrip()
        )
        row_count_matches = bool(
            final_index
            and int(final_index.group(1)) + 1 == expected_rows
        )
    else:
        row_count_matches = file_tail.rstrip().endswith(
            b'"OngoingEvent":{}}'
        )
    if not (keys_in_order and complete_object and row_count_matches):
        raise ValueError(
            "Post-write JSON structure failed validation: "
            f"keys={keys_in_order}, object={complete_object}, "
            f"rows={row_count_matches}."
        )


def validate_json_deep(path: Path, expected_rows: int) -> None:
    """Optionally parse the complete JSON and check every column index."""
    with path.open("r", encoding="utf-8") as handle:
        data = json.load(handle)
    if list(data) != OUTPUT_COLUMNS:
        raise ValueError(f"Written JSON key order is wrong: {list(data)}")
    expected_indices = [str(index) for index in range(expected_rows)]
    for column in OUTPUT_COLUMNS:
        if not isinstance(data[column], dict):
            raise ValueError(f"Written {column} is not an index dictionary.")
        if list(data[column]) != expected_indices:
            raise ValueError(f"Written indices are wrong in {column}.")


def write_json_atomically(
    output: pd.DataFrame,
    output_file: Path,
    *,
    input_rows: int,
    excluded_rows: int,
    deep_validate: bool,
) -> None:
    """Write only validated JSON and preserve the old file on every failure."""
    validate_reference_key_order()
    validate_thames_dataframe(
        output, input_rows=input_rows, excluded_rows=excluded_rows
    )
    output_file.parent.mkdir(parents=True, exist_ok=True)
    temporary_file = output_file.with_name(
        f".{output_file.name}.tmp-{os.getpid()}"
    )
    try:
        output[OUTPUT_COLUMNS].reset_index(drop=True).to_json(
            temporary_file, orient="columns"
        )
        validate_json_lightweight(temporary_file, len(output))
        if deep_validate:
            validate_json_deep(temporary_file, len(output))
        temporary_file.replace(output_file)
    finally:
        if temporary_file.exists():
            temporary_file.unlink()


# ---------------------------------------------------------------------------
# One standardised transformation used by the eight explicit calls
# ---------------------------------------------------------------------------

def build_company_json(
    company: str,
    label: str,
    input_file: Path,
    output_file: Path,
    api_url: str,
    *,
    chunk_size: int,
    progress: bool,
    deep_validate: bool,
) -> dict[str, Any]:
    """Apply the single audited standardised-to-JSON transformation."""
    print(f"\n[{label}] Input CSV: {input_file.name}")
    events = read_standardised_events(input_file)
    print(f"[{label}] Input rows: {len(events):,}")

    features = fetch_all_api_features(company, label, api_url)
    validate_api_schema(company, features)
    print(f"[{label}] API features: {len(features):,}")
    api_lookup = build_exact_api_lookup(features)

    included, excluded, reason_counts = classify_all_events(
        company,
        input_file.name,
        events,
        api_lookup,
        chunk_size=chunk_size,
        progress=progress,
    )
    suggestions = build_manual_suggestions(
        company,
        excluded,
        api_lookup["api_keys"],
        progress=progress,
    )
    write_json_atomically(
        included,
        output_file,
        input_rows=len(events),
        excluded_rows=len(excluded),
        deep_validate=deep_validate,
    )

    print(f"[{label}] JSON rows: {len(included):,}")
    print(f"[{label}] Excluded rows: {len(excluded):,}")
    print(f"[{label}] JSON written to: {output_file}")
    return {
        "company": company,
        "input_file": input_file.name,
        "input_rows": len(events),
        "output_rows": len(included),
        "excluded_rows": len(excluded),
        "excluded": excluded,
        "reason_counts": reason_counts,
        "suggestions": suggestions,
        "ambiguous_api_rows": api_lookup["ambiguous_rows"],
        "identical_api_duplicates_collapsed": api_lookup[
            "identical_duplicates_collapsed"
        ],
        "conflicting_api_ids": api_lookup["conflicting_ids"],
        "api_features": len(features),
        "error": "",
    }


def failed_company_result(
    company: str,
    label: str,
    input_file: Path,
    error: Exception,
) -> dict[str, Any]:
    """Represent every readable row as excluded after a company-level failure."""
    events = pd.DataFrame(columns=INPUT_COLUMNS)
    csv_row_numbers = np.array([], dtype=np.int64)
    try:
        events = read_standardised_events(input_file)
        csv_row_numbers = events["_csv_row_number"].to_numpy(
            dtype=np.int64
        )
    except (OSError, UnicodeError, ValueError, KeyError, pd.errors.ParserError):
        pass

    exclusions = pd.DataFrame(
        {
            "company": company,
            "input_csv": input_file.name,
            "csv_row_number": csv_row_numbers,
            "location_name": clean_text(events["location_name"]),
            "permit_number": clean_text(events["permit_number"]),
            "start_time": clean_text(events["start_time"]),
            "stop_time": clean_text(events["stop_time"]),
            "duration_minutes": clean_text(events["duration_minutes"]),
            "reasons": "COMPANY_BUILD_FAILURE",
        }
    )
    exclusions["normalised_permit_key"] = normalise_permits(
        exclusions["permit_number"]
    )
    exclusions = exclusions[EXCLUSION_COLUMNS]
    print(
        f"[{label}] Build failed; previous valid JSON preserved: "
        f"{type(error).__name__}: {error}"
    )
    return {
        "company": company,
        "input_file": input_file.name,
        "input_rows": len(events),
        "output_rows": 0,
        "excluded_rows": len(exclusions),
        "excluded": exclusions,
        "reason_counts": Counter(
            {"COMPANY_BUILD_FAILURE": len(exclusions)}
        ),
        "suggestions": {},
        "ambiguous_api_rows": {},
        "identical_api_duplicates_collapsed": 0,
        "conflicting_api_ids": 0,
        "api_features": 0,
        "error": f"{type(error).__name__}: {error}",
    }


def build_company_safely(
    company: str,
    label: str,
    input_file: Path,
    output_file: Path,
    api_url: str,
    *,
    chunk_size: int,
    progress: bool,
    deep_validate: bool,
) -> dict[str, Any]:
    """Preserve prior output and continue to later companies after a failure."""
    try:
        return build_company_json(
            company,
            label,
            input_file,
            output_file,
            api_url,
            chunk_size=chunk_size,
            progress=progress,
            deep_validate=deep_validate,
        )
    except (
        OSError,
        UnicodeError,
        ValueError,
        TypeError,
        KeyError,
        RuntimeError,
        requests.RequestException,
        pd.errors.ParserError,
    ) as error:
        return failed_company_result(company, label, input_file, error)


# ---------------------------------------------------------------------------
# Human-readable unmatched-event report
# ---------------------------------------------------------------------------

def shown(value: object) -> str:
    if value is None or value is pd.NA:
        return "<blank>"
    try:
        if bool(pd.isna(value)):
            return "<blank>"
    except (TypeError, ValueError):
        pass
    text = re.sub(r"\s+", " ", str(value).strip())
    return text if text and text.casefold() not in NULL_TEXT else "<blank>"


def render_report_event(
    number: int, event: Any, result: dict[str, Any]
) -> str:
    lines = [
        "",
        "-" * 70,
        f"Event {number}",
        "-" * 70,
        f"Reason(s): {event.reasons.replace(';', ', ')}",
        f"Input CSV: {shown(event.input_csv)}",
        f"CSV row number: {event.csv_row_number}",
        f"location_name: {shown(event.location_name)}",
        f"permit_number: {shown(event.permit_number)}",
        f"normalised_permit_key: {shown(event.normalised_permit_key)}",
        f"start_time: {shown(event.start_time)}",
        f"stop_time: {shown(event.stop_time)}",
        f"duration_minutes: {shown(event.duration_minutes)}",
    ]
    if not event.normalised_permit_key:
        lines.append(
            "closest_api_id_candidates: not applicable because permit is blank"
        )
    elif "NO_EXACT_API_MATCH" in event.reasons:
        candidates = result["suggestions"].get(
            event.normalised_permit_key, []
        )
        if candidates:
            lines.append("closest_api_id_candidates:")
            for candidate, score in candidates:
                lines.append(
                    f"  - {candidate} | similarity={score:.3f}"
                )
        else:
            lines.append("closest_api_id_candidates: none above threshold")
    if "AMBIGUOUS_DUPLICATE_API_ID" in event.reasons:
        lines.append("conflicting_api_candidates:")
        candidates = result["ambiguous_api_rows"].get(
            event.normalised_permit_key, []
        )
        for candidate in candidates:
            lines.append(
                f"  - id={shown(candidate['api_matched_id'])} | "
                f"X={candidate['X']} | Y={candidate['Y']} | "
                "ReceivingWaterCourse="
                f"{shown(candidate['ReceivingWaterCourse'])}"
            )
    return "\n".join(lines) + "\n"


def write_unmatched_report(results: list[dict[str, Any]]) -> None:
    """Atomically write every exclusion; no event disappears silently."""
    all_reasons: Counter[str] = Counter()
    for result in results:
        all_reasons.update(result["reason_counts"])

    temporary = UNMATCHED_REPORT.with_name(
        f".{UNMATCHED_REPORT.name}.tmp-{os.getpid()}"
    )
    try:
        with temporary.open(
            "w", encoding="utf-8", newline="\n", buffering=1024 * 1024
        ) as handle:
            handle.write("EDM UNMATCHED AND EXCLUDED SPILL EVENTS\n")
            handle.write(
                f"Generated: {datetime.now(timezone.utc).isoformat()}\n"
            )
            handle.write(f"Companies processed: {len(results)}\n")
            handle.write(
                f"Total input events: "
                f"{sum(result['input_rows'] for result in results)}\n"
            )
            handle.write(
                f"Total JSON events written: "
                f"{sum(result['output_rows'] for result in results)}\n"
            )
            handle.write(
                f"Total events excluded: "
                f"{sum(result['excluded_rows'] for result in results)}\n\n"
            )
            handle.write(
                "Similarity candidates are suggestions for manual "
                "investigation only.\nThey were not used for API matching "
                "or JSON generation.\n\nSUMMARY BY COMPANY\n\n"
            )
            handle.write(
                f"{'Company':24} {'Input':>10} {'JSON':>10} "
                f"{'Excluded':>10}\n"
            )
            for result in results:
                handle.write(
                    f"{result['company']:24} "
                    f"{result['input_rows']:>10} "
                    f"{result['output_rows']:>10} "
                    f"{result['excluded_rows']:>10}\n"
                )
            handle.write("\nSUMMARY BY REASON\n\n")
            for reason, count in sorted(all_reasons.items()):
                if count:
                    handle.write(f"{reason:40} {count}\n")

            for result in results:
                handle.write(
                    f"\n{'=' * 70}\n"
                    f"COMPANY: {result['company'].upper()}\n"
                    f"{'=' * 70}\n\n"
                    f"Input events: {result['input_rows']}\n"
                    f"JSON events written: {result['output_rows']}\n"
                    f"Excluded events: {result['excluded_rows']}\n"
                )
                if result["error"]:
                    handle.write(
                        f"Company/process error: {result['error']}\n"
                    )
                excluded = result["excluded"]
                iterator = excluded[EXCLUSION_COLUMNS].itertuples(
                    index=False, name="ExcludedEvent"
                )
                for number, event in enumerate(iterator, start=1):
                    handle.write(render_report_event(number, event, result))
            handle.flush()
            os.fsync(handle.fileno())
        temporary.replace(UNMATCHED_REPORT)
    finally:
        if temporary.exists():
            temporary.unlink()


# ---------------------------------------------------------------------------
# Explicit all-company runner
# ---------------------------------------------------------------------------

def run_pipeline(arguments: argparse.Namespace) -> int:
    """Call all selected companies explicitly in website output order."""
    selected = arguments.company
    results = []

    if selected is None or selected == "anglian":
        results.append(build_company_safely(
            "anglian", "ANGLIAN", ANGLIAN_INPUT, ANGLIAN_OUTPUT,
            ANGLIAN_API_URL, chunk_size=arguments.chunk_size,
            progress=arguments.progress,
            deep_validate=arguments.deep_validate_json,
        ))
    if selected is None or selected == "northumbrian":
        results.append(build_company_safely(
            "northumbrian", "NORTHUMBRIAN", NORTHUMBRIAN_INPUT,
            NORTHUMBRIAN_OUTPUT, NORTHUMBRIAN_API_URL,
            chunk_size=arguments.chunk_size, progress=arguments.progress,
            deep_validate=arguments.deep_validate_json,
        ))
    if selected is None or selected == "severn_trent":
        results.append(build_company_safely(
            "severn_trent", "SEVERN TRENT", SEVERN_TRENT_INPUT,
            SEVERN_TRENT_OUTPUT, SEVERN_TRENT_API_URL,
            chunk_size=arguments.chunk_size, progress=arguments.progress,
            deep_validate=arguments.deep_validate_json,
        ))
    if selected is None or selected == "south_west_water":
        results.append(build_company_safely(
            "south_west_water", "SOUTH WEST WATER",
            SOUTH_WEST_WATER_INPUT, SOUTH_WEST_WATER_OUTPUT,
            SOUTH_WEST_WATER_API_URL, chunk_size=arguments.chunk_size,
            progress=arguments.progress,
            deep_validate=arguments.deep_validate_json,
        ))
    if selected is None or selected == "southern_water":
        results.append(build_company_safely(
            "southern_water", "SOUTHERN WATER", SOUTHERN_WATER_INPUT,
            SOUTHERN_WATER_OUTPUT, SOUTHERN_WATER_API_URL,
            chunk_size=arguments.chunk_size, progress=arguments.progress,
            deep_validate=arguments.deep_validate_json,
        ))
    if selected is None or selected == "united_utilities":
        results.append(build_company_safely(
            "united_utilities", "UNITED UTILITIES",
            UNITED_UTILITIES_INPUT, UNITED_UTILITIES_OUTPUT,
            UNITED_UTILITIES_API_URL, chunk_size=arguments.chunk_size,
            progress=arguments.progress,
            deep_validate=arguments.deep_validate_json,
        ))
    if selected is None or selected == "wessex":
        results.append(build_company_safely(
            "wessex", "WESSEX", WESSEX_INPUT, WESSEX_OUTPUT,
            WESSEX_API_URL, chunk_size=arguments.chunk_size,
            progress=arguments.progress,
            deep_validate=arguments.deep_validate_json,
        ))
    if selected is None or selected == "yorkshire":
        results.append(build_company_safely(
            "yorkshire", "YORKSHIRE", YORKSHIRE_INPUT,
            YORKSHIRE_OUTPUT, YORKSHIRE_API_URL,
            chunk_size=arguments.chunk_size, progress=arguments.progress,
            deep_validate=arguments.deep_validate_json,
        ))

    write_unmatched_report(results)
    failed = any(result["error"] for result in results)
    for result in results:
        if (
            result["input_rows"]
            != result["output_rows"] + result["excluded_rows"]
        ):
            print(
                f"[{result['company'].upper()}] ERROR: row accounting failed."
            )
            failed = True
    return 1 if failed else 0


def build_argument_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--company", choices=COMPANY_NAMES)
    parser.add_argument("--chunk-size", type=int, default=DEFAULT_CHUNK_SIZE)
    progress = parser.add_mutually_exclusive_group()
    progress.add_argument("--progress", dest="progress", action="store_true")
    progress.add_argument(
        "--no-progress", dest="progress", action="store_false"
    )
    parser.set_defaults(progress=None)
    parser.add_argument("--deep-validate-json", action="store_true")
    parser.add_argument("--self-check", action="store_true")
    parser.add_argument("--profile", action="store_true")
    return parser


def main() -> int:
    arguments = build_argument_parser().parse_args()
    if arguments.chunk_size <= 0:
        print("ERROR: --chunk-size must be positive.")
        return 2
    if arguments.progress is None:
        arguments.progress = sys.stderr.isatty()
    if arguments.self_check:
        validate_reference_key_order()
        sample_permits = pd.Series(["  catm.3518 ", "00123", "123.0"])
        expected = ["CATM.3518", "00123", "123"]
        if normalise_permits(sample_permits).tolist() != expected:
            raise ValueError("Permit normalisation self-check failed.")
        print("Static JSON contract self-check passed; no JSON was generated.")
        return 0
    if arguments.profile:
        profiler = cProfile.Profile()
        exit_status = profiler.runcall(run_pipeline, arguments)
        profile_output = io.StringIO()
        pstats.Stats(profiler, stream=profile_output).strip_dirs().sort_stats(
            "cumulative"
        ).print_stats(40)
        print(profile_output.getvalue())
        return exit_status
    return run_pipeline(arguments)


if __name__ == "__main__":
    raise SystemExit(main())
