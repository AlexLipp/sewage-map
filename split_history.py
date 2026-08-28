#!/usr/bin/python
"""Split the monolithic Thames Water history tables into one JSON file per CSO.

`update_history.py` writes two history tables to S3, each covering *every* Thames Water
CSO at once: `discharges_to_date/up_to_now.json` (discharge events) and
`discharges_to_date/up_to_now_offline.json` (offline periods). The discharge table has
grown to ~13 MB across ~77,000 rows, and the www.sewagemap.co.uk front-end downloads the
whole thing every time a user opens a CSO popup -- to display the ~0.2% of it that
belongs to the CSO they clicked. That download intermittently fails, which is why the
"Discharge History" tab shows an error while the 0.35 MB "Offline History" tab, which
runs identical code, does not.

This module slices those tables into one file per permit number, so a popup fetches a
median of ~14 KB instead of ~13 MB. Each slice keeps the schema of the table it came from
(pandas' `orient="columns"` layout, original row labels, epoch-ms datetimes), so the
front-end parses it with exactly the code it already uses on the monolithic file.

The slices are uploaded under a *separate top-level prefix*, `discharge_histories/`,
because `update_history.py` calls `empty_s3_folder("discharges_to_date/")` and that
prefix match would otherwise delete them.

Files are addressed by permit number but still carry their `LocationName` column, because
Thames Water reuses a single permit across paired outfalls (`TEMP.2920` covers both
"Streatham & Balham Storm Relief A" and "B"). The front-end filters by location name
within the slice, which preserves exactly the CSO-to-history matching it does today.

Run standalone to split whatever is currently on S3, without waiting on the
`update_history.py` pipeline:

    python split_history.py --from-s3 --out output_dir/split/   # inspect locally
    python split_history.py --from-s3 --upload                  # publish to S3

`update_history.py` calls `split_and_upload_history_file()` on every run to keep the
slices in step with the monolithic tables.
"""

import argparse
import json
import os
import re
import sys
import urllib.request
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Dict, Iterable, List, Set

import boto3

# Name of the bucket the history tables live in
BUCKET_NAME = "thamessewage"
# Public CloudFront distribution fronting that bucket, used to read the source tables
CLOUDFRONT_BASE = "https://d1kmd884co9q6x.cloudfront.net"
# Keys of the monolithic history tables within the bucket
SOURCE_DISCHARGE_KEY = "discharges_to_date/up_to_now.json"
SOURCE_OFFLINE_KEY = "discharges_to_date/up_to_now_offline.json"
# Prefixes the per-CSO slices are written to. Deliberately NOT under
# 'discharges_to_date/', which update_history.py empties on every run.
AWS_SPLIT_DISCHARGE_DIR = "discharge_histories/thames/"
AWS_SPLIT_OFFLINE_DIR = "discharge_histories/thames_offline/"
# How long CloudFront and browsers may reuse a slice before revalidating
SPLIT_CACHE_CONTROL = "public, max-age=300"
# Number of concurrent S3 uploads
UPLOAD_WORKERS = 16
# S3 rejects delete requests of more than 1000 keys
DELETE_BATCH_SIZE = 1000

# Everything outside this set is replaced in permit numbers before they are used as
# object keys. Only 'RET/TH/23' and 'RET/TH/24' currently need it, but the '/' in those
# would otherwise create phantom folders in the bucket.
_UNSAFE_KEY_CHARACTERS = re.compile(r"[^A-Za-z0-9._-]")


def sanitise_permit(permit: str) -> str:
    """Converts a permit number into a string safe to use as an S3 object key.

    The front-end applies the identical transformation when building the URL to fetch, so
    any change here must be mirrored in `src/utils/discharge/historyUrls.ts`.
    """
    name = _UNSAFE_KEY_CHARACTERS.sub("_", str(permit).strip())
    # A name with nothing alphanumeric left in it carried no usable characters to begin
    # with, so it identifies nothing -- fail rather than publish a meaningless key.
    if not any(character.isalnum() for character in name):
        raise ValueError(f"Permit number {permit!r} does not yield a usable file name")
    return name


def split_history(history: Dict[str, Dict[str, Any]]) -> Dict[str, str]:
    """Slices a history table into one JSON document per permit number.

    Takes a history table in the layout `update_history.py` writes it, i.e. the result of
    `DataFrame.to_json()` parsed back into a dict: `{column: {row_label: value}}`. Returns
    a mapping of sanitised permit number to the serialised slice for that permit, each
    slice carrying every column of the source and only that permit's rows, in their
    original order.

    Raises if two distinct permit numbers sanitise to the same file name, rather than
    letting one silently overwrite the other.
    """
    if "PermitNumber" not in history:
        raise ValueError("History table has no 'PermitNumber' column")

    columns = list(history)
    rows_by_permit: Dict[str, List[str]] = defaultdict(list)
    unaddressable = 0

    for row_label, permit in history["PermitNumber"].items():
        if permit is None or str(permit).strip() == "":
            # No permit number means no stable file name to publish the row under.
            unaddressable += 1
            continue
        rows_by_permit[permit].append(row_label)

    if unaddressable:
        print(f"Skipped {unaddressable} row(s) with no permit number")

    slices: Dict[str, str] = {}
    source_of: Dict[str, str] = {}

    for permit, row_labels in rows_by_permit.items():
        name = sanitise_permit(permit)
        if name in source_of:
            raise ValueError(
                f"Permit numbers {source_of[name]!r} and {permit!r} both sanitise to "
                f"{name!r}; refusing to overwrite one with the other"
            )
        source_of[name] = permit
        slices[name] = json.dumps(
            {
                column: {label: history[column][label] for label in row_labels}
                for column in columns
            }
        )

    return slices


def read_history_from_url(url: str) -> Dict[str, Dict[str, Any]]:
    """Reads a monolithic history table from a URL (the public CloudFront distribution)."""
    print(f"Downloading \033[92m{url}\033[0m ...")
    with urllib.request.urlopen(url, timeout=300) as response:
        history = json.load(response)
    print(f"  read {len(history.get('PermitNumber', {}))} rows")
    return history


def read_history_from_file(path: str) -> Dict[str, Dict[str, Any]]:
    """Reads a monolithic history table from a local file."""
    with open(path) as f:
        history = json.load(f)
    print(
        f"Read \033[92m{path}\033[0m " f"({len(history.get('PermitNumber', {}))} rows)"
    )
    return history


def write_split_files(slices: Dict[str, str], out_dir: str) -> None:
    """Writes per-CSO slices to a local directory, for inspection and testing."""
    os.makedirs(out_dir, exist_ok=True)
    for name, body in slices.items():
        with open(os.path.join(out_dir, f"{name}.json"), "w") as f:
            f.write(body)
    print(f"Wrote {len(slices)} file(s) to \033[92m{out_dir}\033[0m")


def _list_keys(s3: Any, bucket_name: str, prefix: str) -> Set[str]:
    """Lists every object key under a prefix.

    Paginates, unlike `aux_funcs.empty_s3_folder`, which uses a bare `list_objects_v2`
    and so silently sees at most the first 1000 keys -- fewer than we publish here.
    """
    keys: Set[str] = set()
    for page in s3.get_paginator("list_objects_v2").paginate(
        Bucket=bucket_name, Prefix=prefix
    ):
        for obj in page.get("Contents", []):
            keys.add(obj["Key"])
    return keys


def _delete_keys(s3: Any, bucket_name: str, keys: Iterable[str]) -> None:
    """Deletes object keys from a bucket, in batches S3 will accept."""
    batch: List[Dict[str, str]] = []
    for key in keys:
        batch.append({"Key": key})
        if len(batch) == DELETE_BATCH_SIZE:
            s3.delete_objects(Bucket=bucket_name, Delete={"Objects": batch})
            batch = []
    if batch:
        s3.delete_objects(Bucket=bucket_name, Delete={"Objects": batch})


def upload_split_files(
    slices: Dict[str, str],
    bucket_name: str,
    prefix: str,
    profile_name: str,
) -> None:
    """Uploads per-CSO slices to a prefix in an AWS bucket, then prunes stale ones.

    New slices are written before old ones are removed, so a CSO is never briefly
    missing. Objects are stored as `application/json` (rather than the
    `binary/octet-stream` that `aux_funcs.upload_file_to_s3` leaves behind) so that
    CloudFront is able to compress them, and with a real `Cache-Control` response header
    -- note that `aux_funcs.upload_file_to_s3` sets a *tag* named `Cache-Control`, which
    has no effect on how anything caches.
    """
    session = boto3.Session(profile_name=profile_name)
    s3 = session.client("s3")

    def put(item: Any) -> None:
        name, body = item
        s3.put_object(
            Bucket=bucket_name,
            Key=f"{prefix}{name}.json",
            Body=body.encode("utf-8"),
            ContentType="application/json",
            CacheControl=SPLIT_CACHE_CONTROL,
        )

    print(
        f"Uploading {len(slices)} file(s) to \033[92m{bucket_name}/{prefix}\033[0m ..."
    )
    with ThreadPoolExecutor(max_workers=UPLOAD_WORKERS) as executor:
        # list() forces the lazy map, so any upload failure is raised here.
        list(executor.map(put, slices.items()))

    wanted = {f"{prefix}{name}.json" for name in slices}
    stale = _list_keys(s3, bucket_name, prefix) - wanted
    if stale:
        _delete_keys(s3, bucket_name, stale)
        print(f"  pruned {len(stale)} slice(s) no longer present in the source table")
    print(f"  uploaded {len(slices)} file(s) successfully")


def split_and_upload_history_file(
    local_path: str,
    prefix: str,
    profile_name: str,
) -> None:
    """Splits a published history table and uploads one file per CSO.

    This is the entry point `update_history.py` calls once it has written and uploaded a
    monolithic table. Reading back the file it just published, rather than re-serialising
    the DataFrame, keeps the slices guaranteed identical to what the site is served.
    """
    with open(local_path) as f:
        history = json.load(f)
    upload_split_files(
        slices=split_history(history),
        bucket_name=BUCKET_NAME,
        prefix=prefix,
        profile_name=profile_name,
    )


def _report(label: str, slices: Dict[str, str]) -> None:
    """Prints a short size summary for a set of slices."""
    sizes = sorted(len(body) for body in slices.values())
    if not sizes:
        print(f"{label}: no slices produced")
        return
    median = sizes[len(sizes) // 2]
    print(
        f"{label}: {len(sizes)} file(s), "
        f"median {median / 1024:.1f} KB, max {max(sizes) / 1024:.0f} KB, "
        f"total {sum(sizes) / 1e6:.1f} MB"
    )


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Split the Thames Water history tables into one JSON file per CSO."
    )
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument(
        "--from-s3",
        action="store_true",
        help="read the history tables currently published on CloudFront",
    )
    source.add_argument(
        "--discharge",
        metavar="PATH",
        help="local path to a discharge history table (up_to_now.json)",
    )
    parser.add_argument(
        "--offline",
        metavar="PATH",
        help="local path to an offline history table (up_to_now_offline.json); "
        "used with --discharge",
    )
    parser.add_argument(
        "--out",
        metavar="DIR",
        help="write the slices under this directory instead of (or as well as) uploading",
    )
    parser.add_argument(
        "--upload",
        action="store_true",
        help="upload the slices to S3 (requires the S3_PROFILE_NAME environment variable)",
    )
    args = parser.parse_args()

    if not args.out and not args.upload:
        parser.error("nothing to do: pass --out, --upload, or both")

    if args.from_s3:
        discharge = read_history_from_url(f"{CLOUDFRONT_BASE}/{SOURCE_DISCHARGE_KEY}")
        offline = read_history_from_url(f"{CLOUDFRONT_BASE}/{SOURCE_OFFLINE_KEY}")
    else:
        if not args.offline:
            parser.error("--discharge requires --offline")
        discharge = read_history_from_file(args.discharge)
        offline = read_history_from_file(args.offline)

    discharge_slices = split_history(discharge)
    offline_slices = split_history(offline)
    _report("Discharge history", discharge_slices)
    _report("Offline history", offline_slices)

    if args.out:
        write_split_files(discharge_slices, os.path.join(args.out, "thames"))
        write_split_files(offline_slices, os.path.join(args.out, "thames_offline"))

    if args.upload:
        profile_name = os.getenv("S3_PROFILE_NAME")
        if profile_name is None:
            print(
                "AWS profile name is missing from the environment!\n"
                "Please set S3_PROFILE_NAME and try again.",
                file=sys.stderr,
            )
            raise SystemExit(1)
        upload_split_files(
            slices=discharge_slices,
            bucket_name=BUCKET_NAME,
            prefix=AWS_SPLIT_DISCHARGE_DIR,
            profile_name=profile_name,
        )
        upload_split_files(
            slices=offline_slices,
            bucket_name=BUCKET_NAME,
            prefix=AWS_SPLIT_OFFLINE_DIR,
            profile_name=profile_name,
        )


if __name__ == "__main__":
    main()
