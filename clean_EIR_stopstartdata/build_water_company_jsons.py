"""
Convert water-company EDM stop/start records into sewagemap JSON.

Water companies publish historical Event Duration Monitoring (EDM) records, in
response to EIR requests, as CSVs of discharge events: a site name, a permit
number, a start and stop time, and a duration. They carry no location. The Stream
Storm Overflow Hub publishes the missing half: for each permit, a position and
the watercourse it discharges into. This script joins the two.

For each company in ONLY_COMPANIES it reads every CSV in
input_stopstart_data/{company}/, fetches that company's Storm Overflow Hub
endpoint, and joins events to sites on the permit number. It then writes
output_jsons/{company}.json, whose schema is declared by OUTPUT_COLUMNS below.
OUTPUT_COLUMNS is the single source of truth: the JSON is built from it and
validated against it.

Along the way the script:

- projects the API's WGS84 lon/lat onto the British National Grid, since that is
  what the front end plots;
- reads the CSVs' naive timestamps as UK local time, BST included, and converts
  them to epoch milliseconds;
- sets OngoingEvent false on every row, because these are historical records.

It transcribes rather than corrects. Nothing is silently dropped or repaired:
a permit with no match in the Hub, a site the company has placed outside the
grid, and an ID the Hub publishes twice are each reported by name on stdout, and
processing continues. A permit that matches nothing keeps its event rows, with
null X, Y and ReceivingWaterCourse. Read the warnings and the closing validation
summary before publishing a file.

Matching is exact. Permits and API IDs are compared after trimming, uppercasing
and collapsing internal whitespace, and in no other way.

output_jsons/{company}.json is the only thing written. API responses are fetched
fresh on each run and never cached, and no intermediate CSVs are produced.

See README.md for how to run the script, the input format it expects, and where
the source data comes from.
"""

from __future__ import annotations

import json
import logging
import math
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

# Companies to process this run. For all of them:
# ["anglian", "northumbrian", "severn_trent", "south_west_water",
#  "southern_water", "united_utilities", "wessex", "yorkshire"]
ONLY_COMPANIES = [
    "yorkshire",
    "united_utilities",
    "southern_water",
    "south_west_water",
    "severn_trent",
    "northumbrian",
    "anglian",
    "wessex",
]

PROJECT_ROOT = Path(__file__).resolve().parent
INPUT_ROOT = PROJECT_ROOT / "input_stopstart_data"
OUTPUT_ROOT = PROJECT_ROOT / "output_jsons"

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
# Required columns in the input CSVs. The pipeline will fail if any are missing.
REQUIRED_INPUT_COLUMNS = [
    "location_name",
    "permit_number",
    "start_time",
    "stop_time",
    "duration_minutes",
]

# URLs of the ArcGIS REST API endpoints for each water company. Each endpoint
# returns a GeoJSON FeatureCollection of storm overflow locations, with a
# Point geometry and properties including the permit number, coordinates and
# receiving watercourse. The pipeline fetches all pages of each endpoint and
# builds a lookup of permit numbers to coordinates and watercourses.
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


# Every Stream storm-overflow endpoint returns the same property names and a
# Point geometry. Only South West Water differs, and only in capitalisation, so
# properties are looked up case-insensitively rather than per-company. None of
# the endpoints publish British National Grid eastings/northings: coordinates
# always arrive as WGS84 lon/lat and are projected to BNG on the way out.
API_ID_FIELD = "Id"
API_LAT_FIELD = "Latitude"
API_LON_FIELD = "Longitude"
API_WATERCOURSE_FIELD = "ReceivingWaterCourse"


logger = logging.getLogger(__name__)


class LevelPrefixFormatter(logging.Formatter):
    """Print INFO lines bare, and prefix anything more severe with its level."""

    def format(self, record: logging.LogRecord) -> str:
        """Render a record, prefixing its level name when more severe than INFO."""
        message = super().format(record)
        if record.levelno <= logging.INFO:
            return message
        return f"{record.levelname}: {message}"


def configure_logging() -> None:
    """Send INFO and above to stdout, bare for INFO and level-prefixed above it."""
    handler = logging.StreamHandler()
    handler.setFormatter(LevelPrefixFormatter("%(message)s"))
    logging.basicConfig(level=logging.INFO, handlers=[handler], force=True)


def ensure_output_folder() -> None:
    """Create the output folder if it doesn't exist."""
    OUTPUT_ROOT.mkdir(parents=True, exist_ok=True)


def clean_arcgis_url(url: str) -> tuple[str, dict[str, Any]]:
    """Split an ArcGIS REST API URL into a base URL and query parameters."""
    parts = urlsplit(url)
    base_url = urlunsplit((parts.scheme, parts.netloc, parts.path, "", ""))
    query_params = dict(parse_qsl(parts.query))
    return base_url, query_params


def fetch_arcgis_geojson(company: str, api_url: str) -> list[dict[str, Any]]:
    """Fetch every GeoJSON feature from an ArcGIS REST API endpoint, following pagination."""
    base_url, base_params = clean_arcgis_url(api_url)
    all_features: list[dict[str, Any]] = []
    offset = 0
    seen_page_signatures: set[str] = set()

    logger.info("Fetching %s API data from ArcGIS...", company)
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
            logger.warning(
                "ArcGIS returned a repeated page; stopping pagination to avoid duplicates."
            )
            break
        seen_page_signatures.add(page_signature)

        all_features.extend(features)

        exceeded = bool(payload.get("exceededTransferLimit"))
        logger.info(
            "  fetched page offset=%s, features=%s, exceededTransferLimit=%s",
            offset,
            len(features),
            exceeded,
        )

        if not exceeded and len(features) < ARCGIS_PAGE_SIZE:
            break
        if len(features) == 0:
            break

        offset += len(features)
    else:
        logger.warning(
            "reached ARCGIS_MAX_PAGES=%s; stopping pagination.", ARCGIS_MAX_PAGES
        )

    logger.info("Fetched %s API features for %s.", len(all_features), company)
    return all_features


def normalise_permit(value: Any) -> str:
    """Normalise an EDM permit number to a canonical form for matching against the API."""
    if pd.isna(value):
        return ""
    text = str(value).strip().upper()
    text = re.sub(r"\s+", " ", text)
    return text


def feature_properties(feature: dict[str, Any]) -> dict[str, Any]:
    """Return the properties of a GeoJSON feature, or an empty dict if missing."""
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
        if get_property(props, field) is None
        and field.lower() not in {k.lower() for k in props}
    ]
    if missing:
        raise RuntimeError(
            f"{company} API is missing expected field(s) {missing}. "
            f"Fields returned: {sorted(props)}"
        )


def to_number(value: Any) -> float | None:
    """Convert a value to a float, or return None if it cannot be converted."""
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


def in_bng_bounds(x_value: float, y_value: float) -> bool:
    """Check whether eastings/northings fall inside the British National Grid."""
    return 0 <= x_value <= 700000 and 0 <= y_value <= 1300000


def convert_lonlat_to_bng(
    lon: Any, lat: Any, transformer: Transformer
) -> tuple[int | None, int | None]:
    """
    Convert WGS84 lon/lat to British National Grid eastings/northings.

    The result is not range-checked: a site the company has mislocated is
    converted and returned as-is, for the caller to report on. Only values that
    cannot be projected at all yield None, which pyproj signals with a
    non-finite result (an absurd latitude, say) and which round() would
    otherwise raise OverflowError on.
    """
    lon_num = to_number(lon)
    lat_num = to_number(lat)
    if lon_num is None or lat_num is None:
        return None, None

    x_value, y_value = transformer.transform(lon_num, lat_num)
    if not math.isfinite(x_value) or not math.isfinite(y_value):
        return None, None
    return round(x_value), round(y_value)


def extract_coordinates(
    feature: dict[str, Any],
    transformer: Transformer,
) -> tuple[int | None, int | None]:
    """Project a feature's lon/lat properties to British National Grid."""
    props = feature_properties(feature)
    lon = get_property(props, API_LON_FIELD)
    lat = get_property(props, API_LAT_FIELD)
    return convert_lonlat_to_bng(lon, lat, transformer)


API_LOOKUP_COLUMNS = [
    "normalised_permit_key",
    "api_matched_id",
    "X",
    "Y",
    "ReceivingWaterCourse",
]


def report_duplicate_api_id(key: str, records: list[dict[str, Any]]) -> None:
    """Warn that an API ID appears more than once, showing every row it appears on."""
    banner = "!" * 70
    logger.info("")
    logger.warning(
        "%s\nAPI ID %s appears %s times in the Storm Overflow Hub. "
        "Taking the first and ignoring the rest.\n%s",
        banner,
        key,
        len(records),
        banner,
    )
    for position, record in enumerate(records, start=1):
        props = feature_properties(record["feature"])
        rendered = ", ".join(f"{name}={props[name]!r}" for name in sorted(props))
        logger.warning(
            "  occurrence %s -> X=%s Y=%s | %s",
            position,
            record["X"],
            record["Y"],
            rendered,
        )


def build_api_lookup(
    features: list[dict[str, Any]],
    transformer: Transformer,
) -> pd.DataFrame:
    """
    Build a one-row-per-permit table of API metadata, keyed for joining.

    An ID that appears more than once is reported in full and resolved by taking
    the first occurrence, rather than dropped.
    """
    records_by_exact: dict[str, list[dict[str, Any]]] = defaultdict(list)

    for feature in features:
        props = feature_properties(feature)
        raw_id = get_property(props, API_ID_FIELD)
        exact_key = normalise_permit(raw_id)
        if not exact_key:
            continue

        x_value, y_value = extract_coordinates(feature, transformer)
        if x_value is None or y_value is None:
            logger.warning(
                "btw, %s has no projectable lon/lat in the API; X and Y will be null",
                raw_id,
            )
        elif not in_bng_bounds(x_value, y_value):
            logger.warning(
                "btw, %s converts to (%s, %s), outside the British National Grid; "
                "keeping it as published",
                raw_id,
                x_value,
                y_value,
            )

        watercourse = get_property(props, API_WATERCOURSE_FIELD)
        records_by_exact[exact_key].append(
            {
                "feature": feature,
                "normalised_permit_key": exact_key,
                "api_matched_id": raw_id,
                "X": x_value,
                "Y": y_value,
                "ReceivingWaterCourse": watercourse if pd.notna(watercourse) else None,
            }
        )

    chosen: list[dict[str, Any]] = []
    for key in sorted(records_by_exact):
        records = records_by_exact[key]
        if len(records) > 1:
            report_duplicate_api_id(key, records)
        chosen.append(records[0])

    frame = pd.DataFrame(chosen, columns=API_LOOKUP_COLUMNS)
    frame["X"] = frame["X"].astype("Int64")
    frame["Y"] = frame["Y"].astype("Int64")
    return frame


def load_company_csvs(company: str) -> tuple[pd.DataFrame, list[str]]:
    """Read and concatenate every input CSV for a company, returning it with any load errors."""
    folder = INPUT_ROOT / company
    if not folder.exists():
        return pd.DataFrame(), [f"Input folder not found: {folder}"]

    csv_files = sorted(folder.glob("*.csv"))
    if not csv_files:
        return pd.DataFrame(), [f"No CSV files found in {folder}"]

    frames = []
    errors = []
    for csv_path in csv_files:
        try:
            # Read everything as text. Left to infer, pandas types an all-numeric
            # permit column as float64 the moment it contains a blank, so permit
            # 103283 arrives as "103283.0" and matches nothing.
            frame = pd.read_csv(csv_path, dtype=str)
        except Exception as exc:
            errors.append(f"{csv_path.name}: could not read CSV: {exc}")
            continue

        missing = [
            column for column in REQUIRED_INPUT_COLUMNS if column not in frame.columns
        ]
        if missing:
            errors.append(f"{csv_path.name}: missing required columns: {missing}")
            continue

        frames.append(frame[REQUIRED_INPUT_COLUMNS].dropna(how="all"))

    if not frames:
        return pd.DataFrame(), errors

    combined = pd.concat(frames, ignore_index=True)
    logger.info(
        "Loaded %s rows for %s from %s CSV file(s).",
        len(combined),
        company,
        len(frames),
    )
    for error in errors:
        logger.warning("%s", error)
    return combined, errors


def parse_datetime_to_epoch_ms(series: pd.Series) -> tuple[pd.Series, pd.Series]:
    """
    Parse EDM timestamps to epoch milliseconds.

    We transcribe what the companies publish rather than second-guessing it.
    Their stop/start times are naive ISO-8601 strings carrying no offset, and we
    assume they mean UK local time, with British Summer Time already applied
    where it applies. That assumption is made explicit by localising to
    LOCAL_TIMEZONE before converting to UTC.

    Two hours a year cannot be transcribed literally, because UK local time does
    not map 1:1 onto UTC across a clock change. Both are resolved so that no
    event is ever dropped: a time in the hour that repeats each autumn is read
    as the first (BST) occurrence, and a time in the hour that is skipped each
    spring is nudged forward onto the hour that does exist.

    Values that cannot be parsed at all become NA and are counted as bad.
    """
    parsed = pd.to_datetime(series, format="ISO8601", errors="coerce")
    bad_values = parsed.isna()

    utc = parsed.dt.tz_localize(
        LOCAL_TIMEZONE,
        ambiguous=True,
        nonexistent="shift_forward",
    ).dt.tz_convert("UTC")

    epoch_ms = (
        (utc - pd.Timestamp("1970-01-01", tz="UTC")) // pd.Timedelta("1ms")
    ).astype("Int64")
    return epoch_ms, bad_values.astype(bool)


def validate_output_json(json_path: Path) -> dict[str, bool]:
    """Check a written JSON against the target schema, reporting each check and an overall verdict."""
    with json_path.open("r", encoding="utf-8") as handle:
        data = json.load(handle)

    keys_match = list(data.keys()) == OUTPUT_COLUMNS
    orientation_matches = all(isinstance(data.get(key), dict) for key in OUTPUT_COLUMNS)

    row_counts = [len(data.get(key, {})) for key in OUTPUT_COLUMNS]
    row_counts_consistent = len(set(row_counts)) <= 1

    def numeric_or_null(column: str) -> bool:
        """Check that every value in a column is a number or null, but never a bool."""
        return all(
            value is None
            or (isinstance(value, (int, float)) and not isinstance(value, bool))
            for value in data[column].values()
        )

    datetime_epoch_ms = True
    for column in ["StartDateTime", "StopDateTime"]:
        datetime_epoch_ms = datetime_epoch_ms and all(
            value is None or (isinstance(value, (int, float)) and value > 100000000000)
            for value in data[column].values()
        )

    duration_numeric = numeric_or_null("Duration")
    xy_numeric = numeric_or_null("X") and numeric_or_null("Y")
    # Reported, but deliberately not part of `passed`: a company that publishes a
    # mislocated site has already been warned about by name, and we pass its
    # coordinates through rather than second-guessing them.
    xy_bng = True
    for row_key, x_value in data["X"].items():
        y_value = data["Y"].get(row_key)
        if x_value is None or y_value is None:
            continue
        if not in_bng_bounds(x_value, y_value):
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
                ongoing_false,
            ]
        ),
    }


def report_unmatched_permits(company: str, events: pd.DataFrame) -> None:
    """Name every EDM permit that has no exact match in the Storm Overflow Hub."""
    unmatched = events[events["match_status"] != "matched"]
    if unmatched.empty:
        logger.info("\nAll %s permits matched the Storm Overflow Hub.", company)
        return

    reasons = {
        "unmatched": "not found in",
        "unmatched_blank_permit": "blank permit number, cannot look up in",
    }
    permits = unmatched[["normalised_permit_key", "match_status"]].drop_duplicates()
    rows = int(len(unmatched))

    banner = "!" * 70
    logger.info("")
    logger.warning(
        "%s\n%s %s permit(s) did not match the Storm Overflow Hub, "
        "affecting %s event row(s).\n"
        "These rows have null X, Y and ReceivingWaterCourse.\n%s",
        banner,
        len(permits),
        company,
        rows,
        banner,
    )
    for permit, status in permits.sort_values("normalised_permit_key").itertuples(
        index=False
    ):
        phrase = reasons.get(status, "unmatched against")
        shown = permit or "<blank>"
        logger.warning("EIR ID %s %s matching stormoverflow hub", shown, phrase)


def report_bad_timestamps(
    company: str, bad_start: pd.Series, bad_stop: pd.Series
) -> None:
    """Warn when any start or stop time could not be parsed, leaving a null in the JSON."""
    starts = int(bad_start.sum())
    stops = int(bad_stop.sum())
    if not starts and not stops:
        return

    logger.warning(
        "btw, %s has %s unparseable start time(s) and %s unparseable stop time(s); "
        "those rows keep a null StartDateTime or StopDateTime",
        company,
        starts,
        stops,
    )


def empty_company_summary(company: str) -> dict[str, Any]:
    """Build a zeroed summary row for a company that could not be processed."""
    return {
        "company": company,
        "total_output_rows": 0,
        "matched_rows": 0,
        "unmatched_rows": 0,
        "unmatched_ids": 0,
        "json_validation_passed": False,
    }


def print_json_comparison(
    company: str, json_path: Path, validation: dict[str, bool]
) -> None:
    """Log how a written JSON measures up against the target schema."""
    with json_path.open("r", encoding="utf-8") as handle:
        data = json.load(handle)

    logger.info("\nFirst JSON keys for %s: %s", company, list(data.keys())[:5])
    logger.info("Structural comparison against the target schema:")
    logger.info("  JSON keys match: %s", validation["keys_match"])
    logger.info("  JSON orientation matches: %s", validation["orientation_matches"])
    logger.info(
        "  StartDateTime/StopDateTime are epoch milliseconds: %s",
        validation["datetime_epoch_ms"],
    )
    logger.info(
        "  All X/Y fall inside the British National Grid: %s", validation["xy_bng"]
    )
    logger.info("  OngoingEvent is boolean false: %s", validation["ongoing_false"])


def enrich_company(company: str, api_url: str) -> dict[str, Any]:
    """Join one company's events to its API metadata, write its JSON, and return its summary."""
    logger.info("\n=== Processing %s ===", company)
    raw_df, load_errors = load_company_csvs(company)

    if raw_df.empty:
        message = "; ".join(load_errors) if load_errors else "No input rows found."
        logger.warning("Skipping %s: %s", company, message)
        return empty_company_summary(company)

    features = fetch_arcgis_geojson(company, api_url)
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

    renamed["StartDateTime"], bad_start = parse_datetime_to_epoch_ms(
        renamed["StartDateTime"]
    )
    renamed["StopDateTime"], bad_stop = parse_datetime_to_epoch_ms(
        renamed["StopDateTime"]
    )
    renamed["Duration"] = pd.to_numeric(renamed["Duration"], errors="coerce")

    report_bad_timestamps(company, bad_start, bad_stop)

    require_api_schema(company, features)
    api_frame = build_api_lookup(features, transformer)

    # One API row per permit, so a left join attaches metadata to every event
    # without changing the row count.
    merged = renamed.merge(api_frame, on="normalised_permit_key", how="left")
    matched = merged["api_matched_id"].notna()
    merged["match_status"] = "unmatched"
    merged.loc[matched, "match_status"] = "matched"
    merged.loc[merged["normalised_permit_key"].eq(""), "match_status"] = (
        "unmatched_blank_permit"
    )
    merged["OngoingEvent"] = False

    output_df = merged[OUTPUT_COLUMNS].copy()

    json_path = OUTPUT_ROOT / f"{company}.json"
    output_df.to_json(json_path, orient="columns")

    validation = validate_output_json(json_path)
    report_unmatched_permits(company, merged)

    matched_rows = int(matched.sum())
    summary = {
        "company": company,
        "total_output_rows": int(len(output_df)),
        "matched_rows": matched_rows,
        "unmatched_rows": int(len(merged) - matched_rows),
        # Distinct permits behind those rows, counted the same way
        # report_unmatched_permits counts them, so the two always agree.
        "unmatched_ids": int(merged.loc[~matched, "normalised_permit_key"].nunique()),
        "json_validation_passed": bool(validation["passed"]),
    }

    logger.info("Wrote JSON: %s", json_path)
    logger.info("\nFirst 3 enriched rows for %s:", company)
    logger.info("%s", output_df.head(3).to_string(index=False))
    print_json_comparison(company, json_path, validation)

    return summary


def selected_companies() -> list[str]:
    """Resolve ONLY_COMPANIES to a list of company names, rejecting any that are unknown."""
    if ONLY_COMPANIES is None:
        return list(COMPANIES.keys())
    unknown = [company for company in ONLY_COMPANIES if company not in COMPANIES]
    if unknown:
        raise ValueError(f"Unknown company name(s) in ONLY_COMPANIES: {unknown}")
    return ONLY_COMPANIES


def main() -> None:
    """Process every selected company, then log a validation summary across all of them."""
    configure_logging()
    logger.info("Starting reusable EDM CSV to JSON pipeline.")
    logger.info("Target output schema: %s", OUTPUT_COLUMNS)
    ensure_output_folder()

    summaries = []
    for company in selected_companies():
        try:
            summaries.append(enrich_company(company, COMPANIES[company]))
        except Exception as exc:
            logger.error(
                "%s failed, continuing to next company: %s", company, exc, exc_info=True
            )
            summaries.append(empty_company_summary(company))

    logger.info("\n=== Pipeline complete ===")
    logger.info("JSON outputs: %s", OUTPUT_ROOT)

    overall = pd.DataFrame(summaries)
    if not overall.empty:
        # QC is reported to stdout only: the pipeline writes JSON and nothing else.
        logger.info("\nValidation summary:")
        logger.info("%s", overall.to_string(index=False))


if __name__ == "__main__":
    main()
