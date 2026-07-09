"""
Reusable EDM CSV to Thames-compatible JSON pipeline.

How to run in VS Code:
1. Install requirements:
   pip install -r requirements.txt
2. Put thames.json in the project root.
3. Put standardised CSV files inside the correct standardised_data/{company}_data folder.
4. Run build_water_company_json.py.
5. Inspect outputs/qc before trusting the JSON.
6. Start with ONLY_COMPANIES = ["anglian"].
7. Once Anglian validates, change ONLY_COMPANIES = None to process all companies.
"""

from __future__ import annotations

import json
import re
from collections import Counter, defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, urlsplit, urlunsplit

import pandas as pd
import requests
from pyproj import Transformer


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

REFRESH_API_CACHE = False
ONLY_COMPANIES = ["yorkshire"]

PROJECT_ROOT = Path(__file__).resolve().parent
INPUT_ROOT = PROJECT_ROOT / "standardised_data"
OUTPUT_ROOT = PROJECT_ROOT / "outputs"
THAMES_JSON_PATH = PROJECT_ROOT / "thames.json"

LOCAL_TIMEZONE = "Europe/London"
ARCGIS_PAGE_SIZE = 2000
ARCGIS_MAX_PAGES = 1000

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


@dataclass
class ThamesContract:
    keys: list[str]


@dataclass
class ApiFields:
    id_field: str | None
    x_field: str | None
    y_field: str | None
    lon_field: str | None
    lat_field: str | None
    watercourse_field: str | None
    duplicate_api_ids: int


def load_thames_contract() -> ThamesContract:
    if not THAMES_JSON_PATH.exists():
        raise FileNotFoundError(
            f"Cannot find Thames reference JSON at {THAMES_JSON_PATH}. "
            "Edit THAMES_JSON_PATH if your file has a different name."
        )

    with THAMES_JSON_PATH.open("r", encoding="utf-8") as handle:
        thames = json.load(handle)

    if not isinstance(thames, dict):
        raise ValueError("Thames JSON must be a top-level object.")

    keys = list(thames.keys())
    if keys != OUTPUT_COLUMNS:
        raise ValueError(
            "Thames JSON keys do not match the expected output columns.\n"
            f"Expected: {OUTPUT_COLUMNS}\n"
            f"Found:    {keys}"
        )

    for key, value in thames.items():
        if not isinstance(value, dict):
            raise ValueError(f"Thames JSON column {key!r} is not a dictionary.")

    print(f"Loaded Thames contract from {THAMES_JSON_PATH.name}: {keys}")
    return ThamesContract(keys=keys)


def ensure_output_folders() -> None:
    for folder in ["csv_clean", "json", "qc", "api_cache"]:
        (OUTPUT_ROOT / folder).mkdir(parents=True, exist_ok=True)


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


def load_or_fetch_api_data(company: str, api_url: str) -> dict[str, Any]:
    cache_path = OUTPUT_ROOT / "api_cache" / f"{company}_api.geojson"

    if cache_path.exists() and not REFRESH_API_CACHE:
        with cache_path.open("r", encoding="utf-8") as handle:
            payload = json.load(handle)
        print(f"Loaded cached API data for {company}: {len(payload.get('features', []))} features.")
        return payload

    payload = fetch_arcgis_geojson(company, api_url)
    with cache_path.open("w", encoding="utf-8") as handle:
        json.dump(payload, handle)
    print(f"Saved API cache: {cache_path}")
    return payload


def normalise_permit(value: Any) -> str:
    if pd.isna(value):
        return ""
    text = str(value).strip().upper()
    text = re.sub(r"\s+", " ", text)
    return text


def alphanumeric_key(value: Any) -> str:
    return re.sub(r"[^A-Z0-9]", "", normalise_permit(value))


def compact_field_name(value: str) -> str:
    return re.sub(r"[^a-z0-9]", "", value.lower())


def feature_properties(feature: dict[str, Any]) -> dict[str, Any]:
    props = feature.get("properties")
    return props if isinstance(props, dict) else {}


def all_property_fields(features: list[dict[str, Any]]) -> list[str]:
    seen: dict[str, None] = {}
    for feature in features:
        for field in feature_properties(feature).keys():
            seen.setdefault(field, None)
    return list(seen.keys())


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


def permit_field_score(field: str) -> int:
    compact = compact_field_name(field)
    lower = field.lower()
    score = 0

    if compact in {"id", "permit", "permitnumber", "permitno", "csoid", "assetid", "outfallid"}:
        score += 50
    if "permit" in compact:
        score += 40
    if "cso" in compact:
        score += 30
    if "asset" in compact:
        score += 25
    if "outfall" in compact:
        score += 25
    if compact.endswith("id") or compact == "id":
        score += 15
    if compact in {"objectid", "fid", "globalid"} or lower.startswith("shape"):
        score -= 40

    return score


def detect_permit_field(features: list[dict[str, Any]], edm_permits: pd.Series) -> tuple[str | None, int]:
    fields = all_property_fields(features)
    edm_keys = {normalise_permit(value) for value in edm_permits if normalise_permit(value)}
    candidate_fields = [field for field in fields if permit_field_score(field) > 0]
    if not candidate_fields:
        candidate_fields = fields

    best_field = None
    best_tuple = (-1, -1, -1)

    for field in candidate_fields:
        values = [normalise_permit(feature_properties(feature).get(field)) for feature in features]
        unique_values = {value for value in values if value}
        exact_matches = len(edm_keys & unique_values)
        base_score = permit_field_score(field)
        non_empty = len(unique_values)
        ranking = (exact_matches, base_score, non_empty)
        if ranking > best_tuple:
            best_tuple = ranking
            best_field = field

    duplicate_count = 0
    if best_field:
        counts = Counter(
            normalise_permit(feature_properties(feature).get(best_field))
            for feature in features
            if normalise_permit(feature_properties(feature).get(best_field))
        )
        duplicate_count = sum(1 for count in counts.values() if count > 1)
        print(
            f"API permit field selected: {best_field} "
            f"(matched {best_tuple[0]} unique EDM permits, duplicate API IDs={duplicate_count})"
        )
    else:
        print("No API permit field could be detected.")

    return best_field, duplicate_count


def coordinate_field_score(field: str, axis: str) -> int:
    compact = compact_field_name(field)
    if axis == "x":
        exact = {"x", "easting", "east", "bnge", "grideasting", "oseasting"}
        contains = ["easting", "bnge", "grid_easting", "oseasting"]
    else:
        exact = {"y", "northing", "north", "bngn", "gridnorthing", "osnorthing"}
        contains = ["northing", "bngn", "grid_northing", "osnorthing"]

    score = 0
    if compact in exact:
        score += 50
    for needle in contains:
        if compact_field_name(needle) in compact:
            score += 35
    if compact == axis:
        score += 30
    if "longitude" in compact or compact in {"lon", "long"}:
        score -= 60
    if "latitude" in compact or compact == "lat":
        score -= 60
    return score


def detect_coordinate_fields(features: list[dict[str, Any]]) -> tuple[str | None, str | None, str | None, str | None]:
    fields = all_property_fields(features)
    x_candidates = [field for field in fields if coordinate_field_score(field, "x") > 0]
    y_candidates = [field for field in fields if coordinate_field_score(field, "y") > 0]

    best_pair: tuple[str | None, str | None] = (None, None)
    best_score = (-1, -1)
    sample = features[:500]

    for x_field in x_candidates:
        for y_field in y_candidates:
            valid_count = sum(
                1
                for feature in sample
                if valid_bng(
                    feature_properties(feature).get(x_field),
                    feature_properties(feature).get(y_field),
                )
            )
            name_score = coordinate_field_score(x_field, "x") + coordinate_field_score(y_field, "y")
            if (valid_count, name_score) > best_score:
                best_score = (valid_count, name_score)
                best_pair = (x_field, y_field)

    if best_pair[0] and best_pair[1] and best_score[0] > 0:
        print(f"API BNG coordinate fields selected: X={best_pair[0]}, Y={best_pair[1]}")
        return best_pair[0], best_pair[1], None, None

    lon_field = None
    lat_field = None
    for field in fields:
        compact = compact_field_name(field)
        if compact in {"longitude", "lon", "long"}:
            lon_field = lon_field or field
        if compact in {"latitude", "lat"}:
            lat_field = lat_field or field

    if lon_field and lat_field:
        print(f"API lon/lat fields selected for BNG conversion: lon={lon_field}, lat={lat_field}")
    else:
        print("No BNG coordinate fields found; will try GeoJSON geometry lon/lat.")

    return None, None, lon_field, lat_field


def watercourse_field_score(field: str) -> int:
    compact = compact_field_name(field)
    score = 0
    if compact == "receivingwatercourse":
        score += 100
    if "receiving" in compact:
        score += 40
    if "watercourse" in compact or "watercourse" in field.lower().replace("_", ""):
        score += 35
    if "river" in compact:
        score += 20
    if "stream" in compact:
        score += 15
    if "water" in compact:
        score += 10
    return score


def detect_watercourse_field(features: list[dict[str, Any]]) -> str | None:
    fields = all_property_fields(features)
    best_field = None
    best_score = 0
    for field in fields:
        score = watercourse_field_score(field)
        if score > best_score:
            best_score = score
            best_field = field

    if best_field:
        print(f"API receiving watercourse field selected: {best_field}")
    else:
        print("No receiving watercourse field could be detected.")
    return best_field


def detect_api_fields(features: list[dict[str, Any]], edm_permits: pd.Series) -> ApiFields:
    id_field, duplicate_api_ids = detect_permit_field(features, edm_permits)
    x_field, y_field, lon_field, lat_field = detect_coordinate_fields(features)
    watercourse_field = detect_watercourse_field(features)

    return ApiFields(
        id_field=id_field,
        x_field=x_field,
        y_field=y_field,
        lon_field=lon_field,
        lat_field=lat_field,
        watercourse_field=watercourse_field,
        duplicate_api_ids=duplicate_api_ids,
    )


def convert_lonlat_to_bng(lon: Any, lat: Any, transformer: Transformer) -> tuple[int | None, int | None]:
    if not valid_lonlat(lon, lat):
        return None, None

    x_value, y_value = transformer.transform(float(lon), float(lat))
    if not valid_bng(x_value, y_value):
        return None, None
    return round(x_value), round(y_value)


def extract_coordinates(
    feature: dict[str, Any],
    api_fields: ApiFields,
    transformer: Transformer,
) -> tuple[int | None, int | None]:
    props = feature_properties(feature)

    if api_fields.x_field and api_fields.y_field:
        x_value = to_number(props.get(api_fields.x_field))
        y_value = to_number(props.get(api_fields.y_field))
        if valid_bng(x_value, y_value):
            return round(float(x_value)), round(float(y_value))

    lon = props.get(api_fields.lon_field) if api_fields.lon_field else None
    lat = props.get(api_fields.lat_field) if api_fields.lat_field else None
    if not valid_lonlat(lon, lat):
        lon, lat = first_geometry_lonlat(feature)

    return convert_lonlat_to_bng(lon, lat, transformer)


def build_api_lookup(
    features: list[dict[str, Any]],
    api_fields: ApiFields,
    transformer: Transformer,
) -> dict[str, Any]:
    if not api_fields.id_field:
        return {
            "exact_lookup": {},
            "duplicate_exact_keys": set(),
            "alpha_lookup": {},
            "duplicate_alpha_keys": set(),
            "records_by_exact": {},
        }

    records_by_exact: dict[str, list[dict[str, Any]]] = defaultdict(list)
    records_by_alpha: dict[str, list[dict[str, Any]]] = defaultdict(list)

    for feature in features:
        props = feature_properties(feature)
        raw_id = props.get(api_fields.id_field)
        exact_key = normalise_permit(raw_id)
        alpha_key = alphanumeric_key(raw_id)
        if not exact_key:
            continue

        x_value, y_value = extract_coordinates(feature, api_fields, transformer)
        watercourse = props.get(api_fields.watercourse_field) if api_fields.watercourse_field else None
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


def validate_output_json(json_path: Path, contract: ThamesContract) -> dict[str, bool]:
    with json_path.open("r", encoding="utf-8") as handle:
        data = json.load(handle)

    keys_match = list(data.keys()) == contract.keys
    orientation_matches = all(isinstance(data.get(key), dict) for key in contract.keys)

    row_counts = [len(data.get(key, {})) for key in contract.keys]
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


def write_qc_reports(company: str, summary: dict[str, Any], match_report: pd.DataFrame) -> None:
    summary_path = OUTPUT_ROOT / "qc" / f"{company}_summary.csv"
    match_path = OUTPUT_ROOT / "qc" / f"{company}_match_report.csv"

    pd.DataFrame([summary]).to_csv(summary_path, index=False)
    match_report.to_csv(match_path, index=False)
    print(f"Wrote QC reports: {summary_path.name}, {match_path.name}")


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
    print("Thames-vs-company structural comparison:")
    print(f"  JSON keys match: {validation['keys_match']}")
    print(f"  JSON orientation matches: {validation['orientation_matches']}")
    print(f"  StartDateTime/StopDateTime are epoch milliseconds: {validation['datetime_epoch_ms']}")
    print(f"  X/Y are British National Grid values, not lon/lat: {validation['xy_bng']}")
    print(f"  OngoingEvent is boolean false: {validation['ongoing_false']}")


def enrich_company(company: str, config: dict[str, Any], contract: ThamesContract) -> dict[str, Any]:
    print(f"\n=== Processing {company} ===")
    raw_df, csv_files, load_errors = load_company_csvs(company, config)

    if raw_df.empty:
        message = "; ".join(load_errors) if load_errors else "No input rows found."
        print(f"Skipping {company}: {message}")
        summary = empty_company_summary(company, csv_files, message)
        write_qc_reports(company, summary, pd.DataFrame())
        return summary

    api_payload = load_or_fetch_api_data(company, config["api"])
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

    api_fields = detect_api_fields(features, renamed["PermitNumber"])
    lookup = build_api_lookup(features, api_fields, transformer)

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
                "api_id_field_used": api_fields.id_field,
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

    csv_path = OUTPUT_ROOT / "csv_clean" / f"{company}_enriched.csv"
    json_path = OUTPUT_ROOT / "json" / f"{company}.json"

    output_df.to_csv(csv_path, index=False)
    output_df.to_json(json_path, orient="columns")

    validation = validate_output_json(json_path, contract)
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
        "api_id_field_used": api_fields.id_field or "",
        "api_x_field_used": api_fields.x_field or api_fields.lon_field or "geometry_lonlat",
        "api_y_field_used": api_fields.y_field or api_fields.lat_field or "geometry_lonlat",
        "api_watercourse_field_used": api_fields.watercourse_field or "",
        "matched_rows": matched_rows,
        "unmatched_rows": unmatched_rows,
        "matched_unique_permits": int(matched_permits[matched_permits != ""].nunique()),
        "unmatched_unique_permits": int(unmatched_permits[unmatched_permits != ""].nunique()),
        "rows_missing_x": int(match_report["missing_x"].sum()),
        "rows_missing_y": int(match_report["missing_y"].sum()),
        "rows_missing_receiving_watercourse": int(match_report["missing_receiving_watercourse"].sum()),
        "rows_bad_start_time": int(bad_start.sum()),
        "rows_bad_stop_time": int(bad_stop.sum()),
        "duplicate_api_ids": int(api_fields.duplicate_api_ids),
        "json_validation_passed": bool(validation["passed"]),
        "error_message": "; ".join(load_errors),
    }

    write_qc_reports(company, summary, match_report)

    print(f"Wrote enriched CSV: {csv_path}")
    print(f"Wrote Thames-compatible JSON: {json_path}")
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
    print("Starting reusable EDM CSV to Thames JSON pipeline.")
    ensure_output_folders()
    contract = load_thames_contract()

    summaries = []
    for company in selected_companies():
        try:
            summaries.append(enrich_company(company, COMPANIES[company], contract))
        except Exception as exc:
            print(f"ERROR: {company} failed, continuing to next company. Details: {exc}")
            summary = empty_company_summary(company, [], str(exc))
            write_qc_reports(company, summary, pd.DataFrame())
            summaries.append(summary)

    overall_path = OUTPUT_ROOT / "qc" / "overall_summary.csv"
    overall = pd.DataFrame(summaries)
    overall.to_csv(overall_path, index=False)

    print("\n=== Pipeline complete ===")
    print(f"CSV outputs:  {OUTPUT_ROOT / 'csv_clean'}")
    print(f"JSON outputs: {OUTPUT_ROOT / 'json'}")
    print(f"QC outputs:   {OUTPUT_ROOT / 'qc'}")
    print(f"API cache:    {OUTPUT_ROOT / 'api_cache'}")
    print(f"Overall QC:   {overall_path}")
    if not overall.empty:
        print("\nValidation summary:")
        print(overall[["company", "total_output_rows", "matched_rows", "unmatched_rows", "json_validation_passed"]].to_string(index=False))


if __name__ == "__main__":
    main()
