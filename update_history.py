#!/usr/bin/python
"""
Publish Thames Water's downstream discharge impact and spill history.

History is maintained incrementally. A long-lived "master" table on S3 holds the
whole record back to 2022; each run re-fetches only a recent window from the
Thames Water API and splices it into that master. This matters because the API is
paginated newest-first at 1000 records per page, so fetching the whole record
costs ~150 sequential requests and a single failure part-way through aborts the
run. Fetching a few months costs one or two requests instead.

The API is treated as the source of truth inside the window and the master is
left untouched outside it. Thames does occasionally edit older events, so a
periodic `--full` run rebuilds the master from scratch; because the incremental
runs keep the site current, a failed rebuild is no longer an outage.
"""

import argparse
import json
import os
import time
from datetime import datetime, timedelta

import pandas as pd
from geojson import Feature, FeatureCollection

from aux_funcs import (
    download_file_from_s3,
    empty_s3_folder,
    project_featurecollection_BNG_WGS84,
    upload_file_to_s3,
    write_timestamp,
)
from poopy.companies import ThamesWater
from split_history import (
    AWS_SPLIT_DISCHARGE_DIR,
    AWS_SPLIT_OFFLINE_DIR,
    split_and_upload_history_file,
    split_history,
    write_split_files,
)
from poopy.history_io import (
    EVENT_KEY,
    events_to_history_df,
    merge_history_tables,
    read_history_json,
    straddling_event_mask,
    write_history_json,
)

# ---------------------------------------------------------------------------
# HOW FAR BACK TO RE-FETCH FROM THE THAMES WATER API ON EACH INCREMENTAL RUN.
# Everything newer than this is taken from the API and overwrites the master;
# everything older is kept from the master untouched. Lower = faster runs and
# fewer API calls; higher = more tolerance for a long cron outage and more of
# Thames's retrospective edits picked up. 92 = 3 months, 31 = 1 month.
LOOKBACK_DAYS = 92
# Extra days fetched *beyond* the window above but NOT treated as authoritative.
# The alert stream pairs each stop with the preceding start, so truncating
# pagination leaves dangling half-events at the oldest edge; this buffer keeps
# those artefacts safely outside the window we actually trust.
BUFFER_DAYS = 7
# ---------------------------------------------------------------------------

# Beyond this, an incremental run is fetching so much that a --full rebuild is
# the more sensible option. Guards against a silently ballooning window.
MAX_INCREMENTAL_LOOKBACK_DAYS = 365

# Name of the bucket to upload to
BUCKET_NAME = "thamessewage"

# Name of the AWS profile to use (set as an environment variable)
PROFILE_NAME = os.getenv("S3_PROFILE_NAME")
if PROFILE_NAME is None:
    raise ValueError(
        "AWS profile name is missing from the environment!\n Please set it and try again."
    )

# Local directory to save outputs to
LOCAL_OUTPUT_DIR = "output_dir/"
# Local directory to save geojsons to
LOCAL_GEOJSON_DIR = LOCAL_OUTPUT_DIR + "geojsons/"
# Local directory to save historical data to
LOCAL_HISTORICAL_DATA_DIR = LOCAL_OUTPUT_DIR + "discharges_to_date/"
# Local directory to save the history master tables to
LOCAL_MASTER_DIR = LOCAL_OUTPUT_DIR + "history_master/"
# Local directory to save per-CSO history slices to on a dry run
LOCAL_SPLIT_DIR = LOCAL_OUTPUT_DIR + "split/"
# AWS directory to save current outputs to
AWS_NOW_DIR = "now/"
# AWS directory to save current info outputs to
AWS_INFO_NOW_DIR = "info_now/"
# AWS directory to save historical outputs to
AWS_HISTORICAL_DIR = "discharges_to_date/"
# AWS directory holding the long-lived history master tables. Deliberately NOT
# under AWS_HISTORICAL_DIR, which gets emptied wholesale by empty_s3_folder.
AWS_MASTER_DIR = "history_master/"
# AWS directory holding dated backups of the master, written before each overwrite
AWS_MASTER_BACKUP_DIR = AWS_MASTER_DIR + "backups/"
# AWS directory to save long-term outputs to
AWS_PAST_DIR = "past/"
# Name of the timestamp file to upload (locally + in AWS)
TIMESTAMP_FILENAME = "timestamp.txt"
# Name of the geojson files in the AWS bucket for current discharges
AWS_GEOJSON_FILENAME = "now.geojson"
# Nam of the geojson files in the AWS bucket for information on current discharges
AWS_INFO_GEOJSON_FILENAME = "info_now.geojson"
# Name of the json file in the AWS bucket for historical discharges
AWS_JSON_FILENAME = "up_to_now.json"
# Name of the json file in the AWS bucket for historical offline discharges
AWS_OFFLINE_JSON_FILENAME = "up_to_now_offline.json"
# Names of the master tables in the AWS bucket
AWS_DISCHARGE_MASTER_FILENAME = "discharge_master.json"
AWS_OFFLINE_MASTER_FILENAME = "offline_master.json"
# Name of the log file
LOCAL_LOG = "history.log"
# Name of the AWS log folder
AWS_LOG_DIR = "history_log/"

# The event types held by each of the two history tables.
DISCHARGE_EVENT_TYPE = "Discharging"
OFFLINE_EVENT_TYPE = "Offline"


def upload_downstream_impact_files_to_s3(
    geojson_file_path: str, timestamp: str
) -> None:
    """Uploads the downstream impact files to the ThamesSewage AWS bucket"""
    # Empty the 'now' folder
    empty_s3_folder(
        bucket_name=BUCKET_NAME, folder_name=AWS_NOW_DIR, profile_name=PROFILE_NAME
    )
    # Upload file to current 'now' output and also the long-term storage 'past' folder
    upload_file_to_s3(
        file_path=LOCAL_GEOJSON_DIR + geojson_file_path,
        bucket_name=BUCKET_NAME,
        object_name=AWS_NOW_DIR + AWS_GEOJSON_FILENAME,
        profile_name=PROFILE_NAME,
    )
    upload_file_to_s3(
        file_path=LOCAL_GEOJSON_DIR + geojson_file_path,
        bucket_name=BUCKET_NAME,
        object_name=AWS_PAST_DIR + geojson_file_path,
        profile_name=PROFILE_NAME,
    )
    # Add timestamp file to now folder
    write_timestamp(
        datetime_string=timestamp,
        timestamp_filename=LOCAL_OUTPUT_DIR + TIMESTAMP_FILENAME,
    )
    upload_file_to_s3(
        file_path=LOCAL_OUTPUT_DIR + TIMESTAMP_FILENAME,
        bucket_name=BUCKET_NAME,
        object_name=AWS_NOW_DIR + TIMESTAMP_FILENAME,
        profile_name=PROFILE_NAME,
    )


def upload_downstream_impact_info_files_to_s3(
    info_geojson_file_path: str, timestamp: str
) -> None:
    """Uploads the downstream impact info files to the ThamesSewage AWS bucket.
    These info files contain more specific information about the discharge impact."""
    # Empty the 'now' folder
    empty_s3_folder(
        bucket_name=BUCKET_NAME, folder_name=AWS_INFO_NOW_DIR, profile_name=PROFILE_NAME
    )
    # Upload file to current 'now' output and also the long-term storage 'past' folder
    upload_file_to_s3(
        file_path=LOCAL_GEOJSON_DIR + info_geojson_file_path,
        bucket_name=BUCKET_NAME,
        object_name=AWS_INFO_NOW_DIR + AWS_INFO_GEOJSON_FILENAME,
        profile_name=PROFILE_NAME,
    )
    # Add timestamp file to info_now folder
    write_timestamp(
        datetime_string=timestamp,
        timestamp_filename=LOCAL_OUTPUT_DIR + TIMESTAMP_FILENAME,
    )
    upload_file_to_s3(
        file_path=LOCAL_OUTPUT_DIR + TIMESTAMP_FILENAME,
        bucket_name=BUCKET_NAME,
        object_name=AWS_INFO_NOW_DIR + TIMESTAMP_FILENAME,
        profile_name=PROFILE_NAME,
    )


def fetch_master_table(
    aws_filename: str, published_filename: str, dry_run: bool
) -> pd.DataFrame | None:
    """Download a history master table from S3.

    If no master exists yet, seed one from the currently published history table.
    That table is a complete history in exactly this schema - it is what previous
    runs produced - so rebuilding it from ~150 paginated API calls would be
    re-fetching what is already sitting in the bucket. This makes the first run
    after deployment cheap, and means losing the master is a recoverable
    inconvenience rather than a slow, failure-prone rebuild.

    Returns None only when neither exists, in which case the caller has no choice
    but a full rebuild.
    """
    local_path = LOCAL_MASTER_DIR + aws_filename
    if dry_run and os.path.exists(local_path):
        print(f"[dry-run] Using existing local master at {local_path}")
        return read_history_json(local_path)

    if download_file_from_s3(
        bucket_name=BUCKET_NAME,
        object_name=AWS_MASTER_DIR + aws_filename,
        file_path=local_path,
        profile_name=PROFILE_NAME,
    ):
        return read_history_json(local_path)

    print(
        f"\033[93m! No master at {AWS_MASTER_DIR + aws_filename}. "
        f"Seeding one from the published {published_filename} instead.\033[0m"
    )
    if download_file_from_s3(
        bucket_name=BUCKET_NAME,
        object_name=AWS_HISTORICAL_DIR + published_filename,
        file_path=local_path,
        profile_name=PROFILE_NAME,
    ):
        return read_history_json(local_path)

    return None


def compute_window(
    master: pd.DataFrame | None, lookback_days: int, buffer_days: int, now: datetime
) -> tuple[datetime, datetime]:
    """Work out the authoritative cutoff and the (earlier) date to fetch back to.

    If the master is staler than the configured window, the window is widened so
    that a cron outage of any length self-heals on the next successful run rather
    than tearing a permanent hole in the history.
    """
    if master is not None and not master.empty:
        newest = master["StartDateTime"].max().to_pydatetime()
        stale_days = (now - newest).days
        required = stale_days + buffer_days
        if required > lookback_days:
            print(
                f"\033[93m! The master's newest event is {newest} ({stale_days} days old), "
                f"which is staler than the configured {lookback_days} day window.\n"
                f"  Widening the window to {required} days so no events are missed.\033[0m"
            )
            lookback_days = required

    if lookback_days > MAX_INCREMENTAL_LOOKBACK_DAYS:
        raise ValueError(
            f"An incremental run would need to fetch {lookback_days} days of history, "
            f"more than the {MAX_INCREMENTAL_LOOKBACK_DAYS} day limit. "
            f"Run with --full to rebuild the master from scratch instead."
        )

    cutoff = now - timedelta(days=lookback_days)
    fetch_since = cutoff - timedelta(days=buffer_days)
    return cutoff, fetch_since


def refresh_straddling_events(
    tw: ThamesWater, tables: dict[str, pd.DataFrame], cutoff: datetime
) -> dict[str, pd.DataFrame]:
    """Refresh events that began before the window but have not resolved within it.

    The window rule replaces master rows by *start* time, so an event that
    started earlier and is still ongoing keeps whatever stop time it last had -
    on the website that renders as a bar that never ends. These are rare (a
    handful at any time), so each affected monitor is re-fetched individually
    using the API's server-side locationName filter, which is cheap.

    Only the straddling rows themselves are updated. The refetch returns a
    monitor's *whole* history, but rewriting all of it would silently rewrite
    records outside the window, which is exactly the invariant this design
    exists to protect. If a refetch fails the master rows are left as they were,
    so this can never lose data.
    """
    masks = {
        event_type: straddling_event_mask(df, cutoff)
        for event_type, df in tables.items()
    }

    needed: dict[str, set[str]] = {}
    for event_type, df in tables.items():
        for name in df.loc[masks[event_type], "LocationName"].unique():
            needed.setdefault(name, set()).add(event_type)

    if not needed:
        print("No straddling events needed a targeted refresh.")
        return tables

    print(
        f"Refreshing {len(needed)} monitor(s) with events straddling the window boundary..."
    )
    fetched: dict[str, list] = {}
    for name in sorted(needed):
        monitor = tw.active_monitors.get(name)
        if monitor is None:
            print(
                f"\033[93m  '{name}' is no longer active; leaving its rows as-is.\033[0m"
            )
            continue
        try:
            monitor.get_history()
            fetched[name] = monitor.history
        except Exception as exc:
            print(
                f"\033[93m  Could not refresh '{name}' ({exc}); leaving its rows as-is.\033[0m"
            )

    if not fetched:
        return tables

    updated = {}
    for event_type, df in tables.items():
        frames = [
            events_to_history_df([e for e in history if e.event_type == event_type])
            for history in fetched.values()
        ]
        frames = [f for f in frames if not f.empty]
        if not frames:
            updated[event_type] = df
            continue

        fresh = pd.concat(frames, ignore_index=True).drop_duplicates(
            subset=EVENT_KEY, keep="first"
        )
        updated[event_type] = _apply_stop_time_updates(df, fresh, masks[event_type])
    return updated


def _apply_stop_time_updates(
    master: pd.DataFrame, fresh: pd.DataFrame, eligible_mask: pd.Series
) -> pd.DataFrame:
    """Overwrite stop times for the masked master rows that also appear in fresh.

    `eligible_mask` restricts the update to the straddling rows; without it a
    monitor's whole refetched history would overwrite the master, including
    records deliberately frozen outside the authoritative window.
    """
    columns = ["StopDateTime", "Duration", "OngoingEvent"]
    original_order = list(master.columns)

    indexed_master = master.set_index(EVENT_KEY)
    eligible = indexed_master.index[eligible_mask.to_numpy()]
    common = eligible.intersection(fresh.set_index(EVENT_KEY).index)
    if len(common) == 0:
        return master

    indexed_fresh = fresh.set_index(EVENT_KEY)
    indexed_master.loc[common, columns] = indexed_fresh.loc[common, columns]
    print(f"  Updated stop times for {len(common)} straddling event(s).")
    return indexed_master.reset_index()[original_order]


def publish_history_table(
    table: pd.DataFrame, local_filename: str, aws_filename: str, dry_run: bool
) -> None:
    """Write and upload the website-facing copy of a history table.

    Ongoing events get their (absent) stop time filled with 'now', because the
    website does `new Date(value)` and a null renders as 1970, drawing a bar
    decades long. The master keeps the honest null.
    """
    published = table.copy()
    published["StopDateTime"] = published["StopDateTime"].fillna(pd.Timestamp.now())

    local_path = LOCAL_HISTORICAL_DATA_DIR + local_filename
    write_history_json(published, local_path)
    print(f"Wrote {len(published)} rows to {local_path}")

    if dry_run:
        print(f"[dry-run] Skipping upload of {aws_filename}")
        return

    upload_file_to_s3(
        file_path=local_path,
        bucket_name=BUCKET_NAME,
        object_name=AWS_HISTORICAL_DIR + aws_filename,
        profile_name=PROFILE_NAME,
        strict=True,
    )


def publish_history_slices(
    local_filename: str, prefix: str, out_subdir: str, dry_run: bool
) -> None:
    """Publish the per-CSO slices of a history table.

    The website fetches one file per CSO rather than the whole table, so the
    slices have to move in step with the table they came from. The file that was
    just written is split, rather than the DataFrame re-serialised, so the slices
    are guaranteed identical to what is served.
    """
    local_path = LOCAL_HISTORICAL_DATA_DIR + local_filename

    if dry_run:
        with open(local_path) as f:
            slices = split_history(json.load(f))
        out_dir = os.path.join(LOCAL_SPLIT_DIR, out_subdir)
        write_split_files(slices, out_dir)
        print(f"[dry-run] Skipping upload of {len(slices)} slice(s) to {prefix}")
        return

    split_and_upload_history_file(
        local_path=local_path,
        prefix=prefix,
        profile_name=PROFILE_NAME,
    )


def store_master_table(
    table: pd.DataFrame, aws_filename: str, stamp: str, dry_run: bool
) -> None:
    """Back up the previous master, then write the new one."""
    local_path = LOCAL_MASTER_DIR + aws_filename
    previous_path = local_path + ".previous"

    if dry_run:
        # Leave the downloaded master untouched so repeated dry runs stay
        # comparable and never compound one merge on top of another.
        preview_path = local_path + ".dryrun"
        write_history_json(table, preview_path)
        print(
            f"[dry-run] Wrote merged master preview ({len(table)} rows) to {preview_path}"
        )
        return

    # The freshly downloaded master is still on disk; keep it as the backup.
    if os.path.exists(local_path):
        os.replace(local_path, previous_path)

    write_history_json(table, local_path)
    print(f"Wrote master {aws_filename} with {len(table)} rows.")

    # Back up the outgoing master BEFORE overwriting the live one, so a failure
    # here stops the run with the old master still intact on S3.
    if os.path.exists(previous_path):
        upload_file_to_s3(
            file_path=previous_path,
            bucket_name=BUCKET_NAME,
            object_name=f"{AWS_MASTER_BACKUP_DIR}{stamp}_{aws_filename}",
            profile_name=PROFILE_NAME,
            strict=True,
        )

    upload_file_to_s3(
        file_path=local_path,
        bucket_name=BUCKET_NAME,
        object_name=AWS_MASTER_DIR + aws_filename,
        profile_name=PROFILE_NAME,
        strict=True,
    )


def update_history(tw: ThamesWater, now: datetime, args: argparse.Namespace) -> None:
    """Fetch a window of history, merge it into the master, and publish."""
    stamp = now.strftime("%y%m%d_%H%M%S")
    cutoff = None

    if args.full:
        print("Running a FULL history rebuild (ignoring any existing master)...")
        discharge_master = offline_master = None
        fetch_since = None
    else:
        print("Fetching history masters from AWS bucket...")
        discharge_master = fetch_master_table(
            AWS_DISCHARGE_MASTER_FILENAME, AWS_JSON_FILENAME, args.dry_run
        )
        offline_master = fetch_master_table(
            AWS_OFFLINE_MASTER_FILENAME, AWS_OFFLINE_JSON_FILENAME, args.dry_run
        )
        if discharge_master is None:
            print(
                "\033[93m! No history master on S3 and no published table to seed "
                "from. Falling back to a full rebuild; this run will be slow but "
                "will seed the master.\033[0m"
            )
            fetch_since = None
        else:
            cutoff, fetch_since = compute_window(
                discharge_master, args.lookback_days, args.buffer_days, now
            )
            print(
                f"Incremental update: fetching back to {fetch_since}, "
                f"treating events from {cutoff} onwards as authoritative."
            )

    print("Fetching historical event information...")
    tw.set_all_histories(since=fetch_since)
    new_discharge = tw.history_to_discharge_df()
    new_offline = tw.history_to_offline_df()

    if fetch_since is None:
        # Full rebuild: what the API returned *is* the new master.
        discharge_table = new_discharge
        offline_table = new_offline
    else:
        discharge_table = merge_history_tables(discharge_master, new_discharge, cutoff)
        offline_table = (
            merge_history_tables(offline_master, new_offline, cutoff)
            if offline_master is not None
            else new_offline
        )
        tables = refresh_straddling_events(
            tw,
            {
                DISCHARGE_EVENT_TYPE: discharge_table,
                OFFLINE_EVENT_TYPE: offline_table,
            },
            cutoff,
        )
        discharge_table = tables[DISCHARGE_EVENT_TYPE]
        offline_table = tables[OFFLINE_EVENT_TYPE]

    print("Storing history masters...")
    store_master_table(
        discharge_table, AWS_DISCHARGE_MASTER_FILENAME, stamp, args.dry_run
    )
    store_master_table(offline_table, AWS_OFFLINE_MASTER_FILENAME, stamp, args.dry_run)

    print("Publishing history for the website...")
    publish_history_table(
        discharge_table, f"{stamp}.json", AWS_JSON_FILENAME, args.dry_run
    )
    publish_history_table(
        offline_table, f"{stamp}_offline.json", AWS_OFFLINE_JSON_FILENAME, args.dry_run
    )

    print("Splitting history tables into one file per CSO...")
    publish_history_slices(
        f"{stamp}.json", AWS_SPLIT_DISCHARGE_DIR, "thames", args.dry_run
    )
    publish_history_slices(
        f"{stamp}_offline.json", AWS_SPLIT_OFFLINE_DIR, "thames_offline", args.dry_run
    )

    # Only stamp the history as updated once both the tables and their slices are
    # safely uploaded, so the site never reads a fresh timestamp against stale data.
    if not args.dry_run:
        write_timestamp(
            datetime_string=now.isoformat(timespec="seconds"),
            timestamp_filename=LOCAL_OUTPUT_DIR + TIMESTAMP_FILENAME,
        )
        upload_file_to_s3(
            file_path=LOCAL_OUTPUT_DIR + TIMESTAMP_FILENAME,
            bucket_name=BUCKET_NAME,
            object_name=AWS_HISTORICAL_DIR + TIMESTAMP_FILENAME,
            profile_name=PROFILE_NAME,
            strict=True,
        )


def parse_args() -> argparse.Namespace:
    """Parse command line arguments."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--full",
        action="store_true",
        help="Rebuild the whole history from the API instead of updating a window. "
        "Slow and failure-prone; intended for an occasional scheduled run to pick "
        "up Thames's retrospective edits.",
    )
    parser.add_argument(
        "--lookback-days",
        type=int,
        default=LOOKBACK_DAYS,
        help=f"Days of history to treat the API as authoritative for (default: {LOOKBACK_DAYS}).",
    )
    parser.add_argument(
        "--buffer-days",
        type=int,
        default=BUFFER_DAYS,
        help=f"Extra days to fetch beyond the authoritative window (default: {BUFFER_DAYS}).",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Fetch and merge, write results locally, but upload nothing.",
    )
    parser.add_argument(
        "--skip-downstream",
        action="store_true",
        help="Skip the downstream-impact geojsons and only update the history.",
    )
    return parser.parse_args()


def main():
    args = parse_args()

    print("~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~")
    startime = datetime.now()
    print("Starting @", datetime.now().strftime("%d/%m/%Y %H:%M:%S"))
    now = datetime.now()

    for directory in (
        LOCAL_GEOJSON_DIR,
        LOCAL_HISTORICAL_DATA_DIR,
        LOCAL_MASTER_DIR,
    ):
        os.makedirs(directory, exist_ok=True)

    tw = ThamesWater()

    if not args.skip_downstream:
        geojson_file_name = now.strftime("%y%m%d_%H%M%S.geojson")
        print("Calculating current downstream discharge extent...")
        geojson = tw.get_downstream_geojson(include_recent_discharges=True)
        # Save geojson to local directory

        # For legacy reasons we need to wrap the geojson in a FeatureCollection...
        feature_coll = FeatureCollection(
            [Feature(geometry=geojson, type="MultiLineString")]
        )
        feature_coll = project_featurecollection_BNG_WGS84(feature_coll)

        with open(LOCAL_GEOJSON_DIR + geojson_file_name, "w") as f:
            json.dump(feature_coll, f)

        if not args.dry_run:
            print("Uploading outputs to AWS bucket")
            upload_downstream_impact_files_to_s3(
                geojson_file_path=geojson_file_name,
                timestamp=now.isoformat(timespec="seconds"),
            )

        print("Calculating current downstream discharge information...")
        info_geojson = tw.get_downstream_info_geojson(include_recent_discharges=True)
        info_geojson = project_featurecollection_BNG_WGS84(info_geojson)
        info_geojson_file_name = now.strftime("%y%m%d_%H%M%S_info.geojson")
        with open(LOCAL_GEOJSON_DIR + info_geojson_file_name, "w") as f:
            json.dump(info_geojson, f)

        if not args.dry_run:
            print("Uploading outputs to AWS bucket")
            upload_downstream_impact_info_files_to_s3(
                info_geojson_file_path=info_geojson_file_name,
                timestamp=now.isoformat(timespec="seconds"),
            )

    update_history(tw, now, args)

    endtime = datetime.now()
    runtime = endtime - startime
    print("Finished @", datetime.now().strftime("%d/%m/%Y %H:%M:%S"))
    print(f"Total runtime: {runtime.seconds//60} minutes {runtime.seconds%60} seconds")
    print("~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~")

    if args.dry_run:
        return

    # Pause for 1 minute before uploading the cron-log
    time.sleep(60)
    print("Uploading cron-log...")

    # Empty the log folder
    empty_s3_folder(
        bucket_name=BUCKET_NAME, folder_name=AWS_LOG_DIR, profile_name=PROFILE_NAME
    )
    upload_file_to_s3(
        file_path=LOCAL_LOG,
        bucket_name=BUCKET_NAME,
        object_name=AWS_LOG_DIR + LOCAL_LOG,
        profile_name=PROFILE_NAME,
    )


if __name__ == "__main__":
    main()
