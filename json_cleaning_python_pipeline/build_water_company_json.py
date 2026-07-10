"""
Reusable EDM CSV to sewagemap JSON pipeline.

How to run in VS Code:
1. Install requirements:
   pip install -r requirements.txt
2. Put stop/start CSV files inside the correct input_stopstart_data/{company} folder.
3. Run build_water_company_json.py.
4. Read the printed validation summary before trusting the JSON.
5. Start with ONLY_COMPANIES = ["anglian"].
6. Once Anglian validates, change ONLY_COMPANIES = None to process all companies.

The pipeline reads input_stopstart_data/ and writes one JSON per company to
outputs/. It writes nothing else: API responses are fetched fresh each run and
QC is reported to stdout.

The target output schema is declared by OUTPUT_COLUMNS below. It is the single
source of truth: the JSON is built from it and validated against it.
"""

from __future__ import annotations

import json
import re
from collections import defaultdict
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, urlsplit, urlunsplit

import pandas as pd
import requests
from pyproj import Transformer


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

ONLY_COMPANIES = ["yorkshire","southern_water", "anglian", "northumbrian", "severn_trent", "south_west_water", "united_utilities", "wessex"]

PROJECT_ROOT = Path(__file__).resolve().parent
INPUT_ROOT = PROJECT_ROOT / "input_stopstart_data"
OUTPUT_ROOT = PROJECT_ROOT / "outputs"

LOCAL_TIMEZONE = "Europe/London"
ARCGIS_PAGE_SIZE = 2000
ARCGIS_MAX_PAGES = 1000

# Target JSON schema: keys, and their order, of the column-oriented output.
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

REQUIRED_INPUT_COLUMNS = [
    "location_name",
    "permit_number",
    "start_time",
    "stop_time",
    "duration_minutes",
]

COMPANIES: dict[str, dict[str, Any]] = {
    "anglian": {
        "folders": ["anglian_data"],
        "api": "https://services3.arcgis.com/VCOY1atHWVcDlvlJ/arcgis/rest/services/stream_service_outfall_locations_view/FeatureServer/0/query",
    },
    "northumbrian": {
        "folders": ["northumbrian_data", "northumbria_data"],
        "api": "https://services-eu1.arcgis.com/MSNNjkZ51iVh8yBj/arcgis/rest/services/Northumbrian_Water_Storm_Overflow_Activity_2_view/FeatureServer/0/query",
    },
    "severn_trent": {
        "folders": ["severn_trent_data"],
        "api": "https://services1.arcgis.com/NO7lTIlnxRMMG9Gw/arcgis/rest/services/Severn_Trent_Water_Storm_Overflow_Activity/FeatureServer/0/query",
    },
    "south_west_water": {
        "folders": ["south_west_water", "south_west_water_data"],
        "api": "https://services-eu1.arcgis.com/OMdMOtfhATJPcHe3/arcgis/rest/services/NEH_outlets_PROD/FeatureServer/0/query?outFields=*&where=1%3D1&f=geojson",
    },
    "southern_water": {
        "folders": ["southern_water_data"],
        "api": "https://services-eu1.arcgis.com/6qJmARkS2dt2IjVA/arcgis/rest/services/SouthernWater_StormOverflowActivity_PROD_view/FeatureServer/0/query",
    },
    "united_utilities": {
        "folders": ["united_utilities_data"],
        "api": "https://services5.arcgis.com/5eoLvR0f8HKb7HWP/arcgis/rest/services/United_Utilities_Storm_Overflow_Activity/FeatureServer/0/query",
    },
    "wessex": {
        "folders": ["wessex_data"],
        "api": "https://services.arcgis.com/3SZ6e0uCvPROr4mS/arcgis/rest/services/Wessex_Water_Storm_Overflow_Activity/FeatureServer/0/query",
    },
    "yorkshire": {
        "folders": ["yorkshire_data"],
        "api": "https://services-eu1.arcgis.com/1WqkK5cDKUbF0CkH/arcgis/rest/services/Yorkshire_Water_Storm_Overflow_Activity/FeatureServer/0/query",
    },
}


# Every Stream storm-overflow endpoint returns the same property names and a
# Point geometry. Only South West Water differs, and only in capitalisation, so
# properties are looked up case-insensitively rather than per-company. None of
# the endpoints publish British National Grid eastings/northings: coordinates
# always arrive as WGS84 lon/lat and are projected to BNG on the way out.
API_ID_FIELD = "Id"
API_LAT_FIELD = "Latitude"
API_LON_FIELD = "Longitude"
API_WATERCOURSE_FIELD = "ReceivingWaterCourse"


def ensure_output_folder() -> None:
    OUTPUT_ROOT.mkdir(parents=True, exist_ok=True)


def clean_arcgis_url(url: str) -> tuple[str, dict[str, Any]]:
    parts = urlsplit(url)
    base_url = urlunsplit((parts.scheme, parts.netloc, parts.path, "", ""))
    query_params = dict(parse_qsl(parts.query))
    return base_url, query_params


def fetch_arcgis_geojson(company: str, api_url: str) -> dict[str, Any]:
    base_url, base_params = clean_arcgis_url(api_url)
    all_features: list[dict[str, Any]] = []
    offset = 0
    seen_page_signatures: set[str] = set()

    print(f"Fetching {company} API data from ArcGIS...")
    for _page_number in range(ARCGIS_MAX_PAGES):
        params = {
            **base_params,
            "where": base_params.get("where", "1=1"),
            "outFields": base_params.get("outFields", "*"),
            "f": "geojson",
            "returnGeometry": "true",
            "resultOffset": offset,
            "resultRecordCount": ARCGIS_PAGE_SIZE,
        }

        response = requests.get(base_url, params=params, timeout=60)
        response.raise_for_status()
        payload = response.json()

        if "error" in payload:
            raise RuntimeError(f"ArcGIS error for {company}: {payload['error']}")

        features = payload.get("features") or []
        page_signature = json.dumps(features[:3], sort_keys=True, default=str)
        if page_signature in seen_page_signatures and features:
            print("  WARNING: ArcGIS returned a repeated page; stopping pagination to avoid duplicates.")
            break
        seen_page_signatures.add(page_signature)

        all_features.extend(features)

        exceeded = bool(payload.get("exceededTransferLimit"))
        print(
            f"  fetched page offset={offset}, features={len(features)}, "
            f"exceededTransferLimit={exceeded}"
        )

        if not exceeded and len(features) < ARCGIS_PAGE_SIZE:
            break
        if len(features) == 0:
            break

        offset += len(features)
    else:
        print(f"  WARNING: reached ARCGIS_MAX_PAGES={ARCGIS_MAX_PAGES}; stopping pagination.")

    full_payload = {
        "type": "FeatureCollection",
        "features": all_features,
        "metadata": {
            "company": company,
            "source_url": api_url,
            "features_fetched": len(all_features),
        },
    }
    print(f"Fetched {len(all_features)} API features for {company}.")
    return full_payload


def normalise_permit(value: Any) -> str:
    if pd.isna(value):
        return ""
    text = str(value).strip().upper()
    text = re.sub(r"\s+", " ", text)
    return text


def alphanumeric_key(value: Any) -> str:
    return re.sub(r"[^A-Z0-9]", "", normalise_permit(value))


def feature_properties(feature: dict[str, Any]) -> dict[str, Any]:
    props = feature.get("properties")
    return props if isinstance(props, dict) else {}


def get_property(props: dict[str, Any], field: str) -> Any:
    """Read an API property, tolerating the capitalisation South West Water uses."""
    if field in props:
        return props[field]
    target = field.lower()
    for key, value in props.items():
        if key.lower() == target:
            return value
    return None


def require_api_schema(company: str, features: list[dict[str, Any]]) -> None:
    """Fail loudly if an endpoint stops returning the fields we assume."""
    if not features:
        return
    props = feature_properties(features[0])
    missing = [
        field
        for field in (API_ID_FIELD, API_LAT_FIELD, API_LON_FIELD, API_WATERCOURSE_FIELD)
        if get_property(props, field) is None and field.lower() not in {k.lower() for k in props}
    ]
    if missing:
        raise RuntimeError(
            f"{company} API is missing expected field(s) {missing}. "
            f"Fields returned: {sorted(props)}"
        )


def to_number(value: Any) -> float | None:
    if value is None or pd.isna(value):
        return None
    if isinstance(value, str):
        value = value.strip().replace(",", "")
        if value == "":
            return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def valid_bng(x_value: Any, y_value: Any) -> bool:
    x_num = to_number(x_value)
    y_num = to_number(y_value)
    return (
        x_num is not None
        and y_num is not None
        and 0 <= x_num <= 700000
        and 0 <= y_num <= 1300000
    )


def valid_lonlat(lon: Any, lat: Any) -> bool:
    lon_num = to_number(lon)
    lat_num = to_number(lat)
    return lon_num is not None and lat_num is not None and -8.5 <= lon_num <= 2.5 and 49 <= lat_num <= 61


def first_geometry_lonlat(feature: dict[str, Any]) -> tuple[float | None, float | None]:
    geometry = feature.get("geometry") or {}
    coordinates = geometry.get("coordinates")
    if not isinstance(coordinates, list):
        return None, None

    # GeoJSON point coordinates are [longitude, latitude]. If a service returns
    # a line or polygon, walk down to the first coordinate pair.
    current = coordinates
    while isinstance(current, list) and current and isinstance(current[0], list):
        current = current[0]

    if isinstance(current, list) and len(current) >= 2 and valid_lonlat(current[0], current[1]):
        return float(current[0]), float(current[1])
    return None, None




def convert_lonlat_to_bng(lon: Any, lat: Any, transformer: Transformer) -> tuple[int | None, int | None]:
    if not valid_lonlat(lon, lat):
        return None, None

    x_value, y_value = transformer.transform(float(lon), float(lat))
    if not valid_bng(x_value, y_value):
        return None, None
    return round(x_value), round(y_value)


def extract_coordinates(
    feature: dict[str, Any],
    transformer: Transformer,
) -> tuple[int | None, int | None]:
    props = feature_properties(feature)

    lon = get_property(props, API_LON_FIELD)
    lat = get_property(props, API_LAT_FIELD)
    if not valid_lonlat(lon, lat):
        lon, lat = first_geometry_lonlat(feature)

    return convert_lonlat_to_bng(lon, lat, transformer)


def build_api_lookup(
    features: list[dict[str, Any]],
    transformer: Transformer,
) -> dict[str, Any]:
    records_by_exact: dict[str, list[dict[str, Any]]] = defaultdict(list)
    records_by_alpha: dict[str, list[dict[str, Any]]] = defaultdict(list)

    for feature in features:
        props = feature_properties(feature)
        raw_id = get_property(props, API_ID_FIELD)
        exact_key = normalise_permit(raw_id)
        alpha_key = alphanumeric_key(raw_id)
        if not exact_key:
            continue

        x_value, y_value = extract_coordinates(feature, transformer)
        watercourse = get_property(props, API_WATERCOURSE_FIELD)
        record = {
            "feature": feature,
            "raw_id": raw_id,
            "exact_key": exact_key,
            "alpha_key": alpha_key,
            "X": x_value,
            "Y": y_value,
            "ReceivingWaterCourse": watercourse if pd.notna(watercourse) else None,
        }
        records_by_exact[exact_key].append(record)
        if alpha_key:
            records_by_alpha[alpha_key].append(record)

    exact_lookup = {key: values[0] for key, values in records_by_exact.items() if len(values) == 1}
    duplicate_exact_keys = {key for key, values in records_by_exact.items() if len(values) > 1}
    alpha_lookup = {key: values[0] for key, values in records_by_alpha.items() if len(values) == 1}
    duplicate_alpha_keys = {key for key, values in records_by_alpha.items() if len(values) > 1}

    return {
        "exact_lookup": exact_lookup,
        "duplicate_exact_keys": duplicate_exact_keys,
        "alpha_lookup": alpha_lookup,
        "duplicate_alpha_keys": duplicate_alpha_keys,
        "records_by_exact": records_by_exact,
        "duplicate_api_ids": len(duplicate_exact_keys),
    }


def resolve_company_folder(config: dict[str, Any]) -> Path:
    for folder_name in config["folders"]:
        folder = INPUT_ROOT / folder_name
        if folder.exists():
            return folder
    return INPUT_ROOT / config["folders"][0]


def load_company_csvs(company: str, config: dict[str, Any]) -> tuple[pd.DataFrame, list[Path], list[str]]:
    folder = resolve_company_folder(config)
    if not folder.exists():
        return pd.DataFrame(), [], [f"Input folder not found: {folder}"]

    csv_files = sorted(folder.glob("*.csv"))
    if not csv_files:
        return pd.DataFrame(), [], [f"No CSV files found in {folder}"]

    frames = []
    errors = []
    for csv_path in csv_files:
        try:
            frame = pd.read_csv(csv_path)
        except Exception as exc:
            errors.append(f"{csv_path.name}: could not read CSV: {exc}")
            continue

        missing = [column for column in REQUIRED_INPUT_COLUMNS if column not in frame.columns]
        if missing:
            errors.append(f"{csv_path.name}: missing required columns: {missing}")
            continue

        frame = frame[REQUIRED_INPUT_COLUMNS].dropna(how="all")
        frame["_source_file"] = csv_path.name
        frames.append(frame)

    if not frames:
        return pd.DataFrame(), csv_files, errors

    combined = pd.concat(frames, ignore_index=True)
    print(f"Loaded {len(combined)} rows for {company} from {len(frames)} CSV file(s).")
    for error in errors:
        print(f"  WARNING: {error}")
    return combined, csv_files, errors


def parse_datetime_to_epoch_ms_scalar(series: pd.Series) -> tuple[pd.Series, pd.Series]:
    output_values: list[int | None] = []
    bad_values: list[bool] = []

    for value in series:
        if pd.isna(value) or str(value).strip() == "":
            output_values.append(None)
            bad_values.append(True)
            continue

        timestamp = pd.to_datetime(value, errors="coerce")
        if pd.isna(timestamp):
            output_values.append(None)
            bad_values.append(True)
            continue

        try:
            if timestamp.tzinfo is None:
                timestamp = timestamp.tz_localize(
                    LOCAL_TIMEZONE,
                    ambiguous="NaT",
                    nonexistent="shift_forward",
                )
            else:
                timestamp = timestamp.tz_convert(LOCAL_TIMEZONE)

            if pd.isna(timestamp):
                output_values.append(None)
                bad_values.append(True)
                continue

            utc_timestamp = timestamp.tz_convert("UTC")
            output_values.append(int(utc_timestamp.timestamp() * 1000))
            bad_values.append(False)
        except Exception:
            output_values.append(None)
            bad_values.append(True)

    return pd.Series(output_values, dtype="Int64"), pd.Series(bad_values, dtype=bool)


def parse_datetime_to_epoch_ms(series: pd.Series) -> tuple[pd.Series, pd.Series]:
    source = series.astype("string")
    blank = source.isna() | source.str.strip().eq("")
    parsed = pd.to_datetime(source, errors="coerce", cache=True)

    try:
        if parsed.dt.tz is None:
            parsed_utc = parsed.dt.tz_localize(
                LOCAL_TIMEZONE,
                ambiguous="NaT",
                nonexistent="shift_forward",
            ).dt.tz_convert("UTC")
        else:
            parsed_utc = parsed.dt.tz_convert("UTC")
    except (AttributeError, TypeError, ValueError):
        # Mixed aware and naive timestamps are uncommon in these files. If they
        # appear, use the scalar policy-preserving parser instead of guessing.
        return parse_datetime_to_epoch_ms_scalar(series)

    bad_values = blank | parsed_utc.isna()
    utc_epoch = pd.Timestamp("1970-01-01", tz="UTC")
    epoch_ms = ((parsed_utc - utc_epoch) / pd.Timedelta(milliseconds=1)).round().astype("Int64")
    epoch_ms = epoch_ms.mask(bad_values, pd.NA)
    return epoch_ms, bad_values.astype(bool)


def match_keys_to_api(
    normalised_key: str,
    alpha_key: str,
    lookup: dict[str, Any],
) -> tuple[dict[str, Any] | None, str, str]:
    if not normalised_key:
        return None, "unmatched_blank_permit", ""

    if normalised_key in lookup["exact_lookup"]:
        return lookup["exact_lookup"][normalised_key], "matched", "exact_normalised"

    if normalised_key in lookup["duplicate_exact_keys"]:
        return None, "unmatched_duplicate_api_id", ""

    if alpha_key and alpha_key in lookup["alpha_lookup"]:
        return lookup["alpha_lookup"][alpha_key], "matched", "alphanumeric_unique"

    if alpha_key and alpha_key in lookup["duplicate_alpha_keys"]:
        return None, "unmatched_duplicate_api_alphanumeric_id", ""

    return None, "unmatched", ""


def validate_output_json(json_path: Path) -> dict[str, bool]:
    with json_path.open("r", encoding="utf-8") as handle:
        data = json.load(handle)

    keys_match = list(data.keys()) == OUTPUT_COLUMNS
    orientation_matches = all(isinstance(data.get(key), dict) for key in OUTPUT_COLUMNS)

    row_counts = [len(data.get(key, {})) for key in OUTPUT_COLUMNS]
    row_counts_consistent = len(set(row_counts)) <= 1

    def numeric_or_null(column: str) -> bool:
        return all(value is None or (isinstance(value, (int, float)) and not isinstance(value, bool)) for value in data[column].values())

    datetime_epoch_ms = True
    for column in ["StartDateTime", "StopDateTime"]:
        datetime_epoch_ms = datetime_epoch_ms and all(
            value is None or (isinstance(value, (int, float)) and value > 100000000000)
            for value in data[column].values()
        )

    duration_numeric = numeric_or_null("Duration")
    xy_numeric = numeric_or_null("X") and numeric_or_null("Y")
    xy_bng = True
    for row_key, x_value in data["X"].items():
        y_value = data["Y"].get(row_key)
        if x_value is None and y_value is None:
            continue
        if not valid_bng(x_value, y_value):
            xy_bng = False
            break

    ongoing_false = all(value is False for value in data["OngoingEvent"].values())

    return {
        "keys_match": keys_match,
        "orientation_matches": orientation_matches,
        "row_counts_consistent": row_counts_consistent,
        "datetime_epoch_ms": datetime_epoch_ms,
        "duration_numeric": duration_numeric,
        "xy_numeric": xy_numeric,
        "xy_bng": xy_bng,
        "ongoing_false": ongoing_false,
        "passed": all(
            [
                keys_match,
                orientation_matches,
                row_counts_consistent,
                datetime_epoch_ms,
                duration_numeric,
                xy_numeric,
                xy_bng,
                ongoing_false,
            ]
        ),
    }


def empty_company_summary(company: str, csv_files: list[Path], message: str) -> dict[str, Any]:
    return {
        "company": company,
        "total_input_rows": 0,
        "total_output_rows": 0,
        "input_csv_files": ";".join(path.name for path in csv_files),
        "unique_edm_permits": 0,
        "api_features_fetched": 0,
        "api_id_field_used": "",
        "api_x_field_used": "",
        "api_y_field_used": "",
        "api_watercourse_field_used": "",
        "matched_rows": 0,
        "unmatched_rows": 0,
        "matched_unique_permits": 0,
        "unmatched_unique_permits": 0,
        "rows_missing_x": 0,
        "rows_missing_y": 0,
        "rows_missing_receiving_watercourse": 0,
        "rows_bad_start_time": 0,
        "rows_bad_stop_time": 0,
        "duplicate_api_ids": 0,
        "json_validation_passed": False,
        "error_message": message,
    }


def print_json_comparison(company: str, json_path: Path, validation: dict[str, bool]) -> None:
    with json_path.open("r", encoding="utf-8") as handle:
        data = json.load(handle)

    print(f"\nFirst JSON keys for {company}: {list(data.keys())[:5]}")
    print("Structural comparison against the target schema:")
    print(f"  JSON keys match: {validation['keys_match']}")
    print(f"  JSON orientation matches: {validation['orientation_matches']}")
    print(f"  StartDateTime/StopDateTime are epoch milliseconds: {validation['datetime_epoch_ms']}")
    print(f"  X/Y are British National Grid values, not lon/lat: {validation['xy_bng']}")
    print(f"  OngoingEvent is boolean false: {validation['ongoing_false']}")


def enrich_company(company: str, config: dict[str, Any]) -> dict[str, Any]:
    print(f"\n=== Processing {company} ===")
    raw_df, csv_files, load_errors = load_company_csvs(company, config)

    if raw_df.empty:
        message = "; ".join(load_errors) if load_errors else "No input rows found."
        print(f"Skipping {company}: {message}")
        return empty_company_summary(company, csv_files, message)

    api_payload = fetch_arcgis_geojson(company, config["api"])
    features = api_payload.get("features") or []
    transformer = Transformer.from_crs("EPSG:4326", "EPSG:27700", always_xy=True)

    renamed = raw_df.rename(
        columns={
            "location_name": "LocationName",
            "permit_number": "PermitNumber",
            "start_time": "StartDateTime",
            "stop_time": "StopDateTime",
            "duration_minutes": "Duration",
        }
    )

    renamed["normalised_permit_key"] = renamed["PermitNumber"].apply(normalise_permit)
    renamed["alpha_permit_key"] = renamed["PermitNumber"].apply(alphanumeric_key)

    renamed["StartDateTime"], bad_start = parse_datetime_to_epoch_ms(renamed["StartDateTime"])
    renamed["StopDateTime"], bad_stop = parse_datetime_to_epoch_ms(renamed["StopDateTime"])
    renamed["Duration"] = pd.to_numeric(renamed["Duration"], errors="coerce")

    require_api_schema(company, features)
    lookup = build_api_lookup(features, transformer)

    match_rows = []
    x_values = []
    y_values = []
    watercourse_values = []

    for row_values in zip(
        renamed["LocationName"],
        renamed["PermitNumber"],
        renamed["normalised_permit_key"],
        renamed["alpha_permit_key"],
        bad_start,
        bad_stop,
    ):
        location_name, permit_number, normalised_key, alpha_key, row_bad_start, row_bad_stop = row_values
        record, match_status, match_type = match_keys_to_api(normalised_key, alpha_key, lookup)
        if record:
            x_value = record["X"]
            y_value = record["Y"]
            watercourse = record["ReceivingWaterCourse"]
            api_matched_id = record["raw_id"]
        else:
            x_value = None
            y_value = None
            watercourse = None
            api_matched_id = None

        x_values.append(x_value)
        y_values.append(y_value)
        watercourse_values.append(watercourse)

        match_rows.append(
            {
                "LocationName": location_name,
                "PermitNumber": permit_number,
                "normalised_permit_key": normalised_key,
                "match_status": match_status,
                "match_type": match_type,
                "api_id_field_used": API_ID_FIELD,
                "api_matched_id_value": api_matched_id,
                "X": x_value,
                "Y": y_value,
                "ReceivingWaterCourse": watercourse,
                "missing_x": x_value is None,
                "missing_y": y_value is None,
                "missing_receiving_watercourse": watercourse is None or pd.isna(watercourse) or str(watercourse).strip() == "",
                "bad_start_time": bool(row_bad_start),
                "bad_stop_time": bool(row_bad_stop),
            }
        )

    renamed["X"] = pd.Series(x_values, dtype="Int64")
    renamed["Y"] = pd.Series(y_values, dtype="Int64")
    renamed["ReceivingWaterCourse"] = watercourse_values
    renamed["OngoingEvent"] = False

    output_df = renamed[OUTPUT_COLUMNS].copy()

    json_path = OUTPUT_ROOT / f"{company}.json"
    output_df.to_json(json_path, orient="columns")

    validation = validate_output_json(json_path)
    match_report = pd.DataFrame(match_rows)

    matched_rows = int((match_report["match_status"] == "matched").sum())
    unmatched_rows = int(len(match_report) - matched_rows)
    matched_permits = match_report.loc[match_report["match_status"] == "matched", "PermitNumber"].apply(normalise_permit)
    unmatched_permits = match_report.loc[match_report["match_status"] != "matched", "PermitNumber"].apply(normalise_permit)

    summary = {
        "company": company,
        "total_input_rows": int(len(raw_df)),
        "total_output_rows": int(len(output_df)),
        "input_csv_files": ";".join(path.name for path in csv_files),
        "unique_edm_permits": int(renamed["normalised_permit_key"].nunique()),
        "api_features_fetched": int(len(features)),
        "api_id_field_used": API_ID_FIELD,
        "api_x_field_used": API_LON_FIELD,
        "api_y_field_used": API_LAT_FIELD,
        "api_watercourse_field_used": API_WATERCOURSE_FIELD,
        "matched_rows": matched_rows,
        "unmatched_rows": unmatched_rows,
        "matched_unique_permits": int(matched_permits[matched_permits != ""].nunique()),
        "unmatched_unique_permits": int(unmatched_permits[unmatched_permits != ""].nunique()),
        "rows_missing_x": int(match_report["missing_x"].sum()),
        "rows_missing_y": int(match_report["missing_y"].sum()),
        "rows_missing_receiving_watercourse": int(match_report["missing_receiving_watercourse"].sum()),
        "rows_bad_start_time": int(bad_start.sum()),
        "rows_bad_stop_time": int(bad_stop.sum()),
        "duplicate_api_ids": int(lookup["duplicate_api_ids"]),
        "json_validation_passed": bool(validation["passed"]),
        "error_message": "; ".join(load_errors),
    }

    print(f"Wrote JSON: {json_path}")
    print(f"\nFirst 3 enriched rows for {company}:")
    print(output_df.head(3).to_string(index=False))
    print_json_comparison(company, json_path, validation)

    return summary


def selected_companies() -> list[str]:
    if ONLY_COMPANIES is None:
        return list(COMPANIES.keys())
    unknown = [company for company in ONLY_COMPANIES if company not in COMPANIES]
    if unknown:
        raise ValueError(f"Unknown company name(s) in ONLY_COMPANIES: {unknown}")
    return ONLY_COMPANIES


def main() -> None:
    print("Starting reusable EDM CSV to JSON pipeline.")
    print(f"Target output schema: {OUTPUT_COLUMNS}")
    ensure_output_folder()

    summaries = []
    for company in selected_companies():
        try:
            summaries.append(enrich_company(company, COMPANIES[company]))
        except Exception as exc:
            print(f"ERROR: {company} failed, continuing to next company. Details: {exc}")
            summaries.append(empty_company_summary(company, [], str(exc)))

    print("\n=== Pipeline complete ===")
    print(f"JSON outputs: {OUTPUT_ROOT}")

    overall = pd.DataFrame(summaries)
    if not overall.empty:
        # QC is reported to stdout only: the pipeline writes JSON and nothing else.
        print("\nValidation summary:")
        print(overall[["company", "total_output_rows", "matched_rows", "unmatched_rows", "json_validation_passed"]].to_string(index=False))


if __name__ == "__main__":
    main()
