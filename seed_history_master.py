#!/usr/bin/python
"""
One-off: seed the long-lived history master tables on S3.

`update_history.py` keeps the whole spill record in a master table on S3 and
splices only a recent window from the API into it on each run. That master has to
start from somewhere. This script uploads an existing full-history table as the
seed, after validating that it really does carry the expected schema.

The natural seeds are the currently published artefacts themselves, which are
already in exactly the right format:

    https://d1kmd884co9q6x.cloudfront.net/discharges_to_date/up_to_now.json
    https://d1kmd884co9q6x.cloudfront.net/discharges_to_date/up_to_now_offline.json

Usage:
    python seed_history_master.py --discharge up_to_now.json --offline up_to_now_offline.json
    python seed_history_master.py --discharge up_to_now.json --dry-run

This only needs running once. `update_history.py` also falls back to a full
rebuild if no master is present, so seeding is an optimisation rather than a
prerequisite - but a rebuild is the slow, failure-prone path this whole change
exists to avoid, so seeding from a known-good file is much preferred.
"""

import argparse
import os

from aux_funcs import upload_file_to_s3
from poopy.history_io import read_history_json

BUCKET_NAME = "thamessewage"
AWS_MASTER_DIR = "history_master/"
AWS_DISCHARGE_MASTER_FILENAME = "discharge_master.json"
AWS_OFFLINE_MASTER_FILENAME = "offline_master.json"

PROFILE_NAME = os.getenv("S3_PROFILE_NAME")


def describe(path: str) -> None:
    """Validate a candidate seed file and print what it contains."""
    df = read_history_json(path)
    print(f"\n{path}")
    print(f"  rows:            {len(df)}")
    print(f"  locations:       {df['LocationName'].nunique()}")
    print(
        f"  start range:     {df['StartDateTime'].min()} -> {df['StartDateTime'].max()}"
    )
    print(f"  ongoing events:  {int(df['OngoingEvent'].sum())}")
    duplicates = df.duplicated(
        subset=["LocationName", "PermitNumber", "StartDateTime"]
    ).sum()
    print(f"  duplicate keys:  {duplicates}")
    if duplicates:
        raise ValueError(
            f"{path} contains {duplicates} duplicate event key(s); refusing to seed "
            f"a master that the merge cannot key on uniquely."
        )


def seed(path: str, aws_filename: str, dry_run: bool) -> None:
    """Validate and upload one seed file."""
    describe(path)
    if dry_run:
        print(f"  [dry-run] would upload to {AWS_MASTER_DIR + aws_filename}")
        return
    upload_file_to_s3(
        file_path=path,
        bucket_name=BUCKET_NAME,
        object_name=AWS_MASTER_DIR + aws_filename,
        profile_name=PROFILE_NAME,
        strict=True,
    )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--discharge", required=True, help="Path to the full discharge history JSON."
    )
    parser.add_argument("--offline", help="Path to the full offline history JSON.")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Validate and report, but upload nothing.",
    )
    args = parser.parse_args()

    if not args.dry_run and PROFILE_NAME is None:
        raise ValueError(
            "AWS profile name is missing from the environment!\n"
            "Please set S3_PROFILE_NAME and try again (or use --dry-run)."
        )

    seed(args.discharge, AWS_DISCHARGE_MASTER_FILENAME, args.dry_run)
    if args.offline:
        seed(args.offline, AWS_OFFLINE_MASTER_FILENAME, args.dry_run)
    else:
        print(
            "\n! No --offline file given. The offline master will be seeded by the "
            "first update_history.py run, which will rebuild it in full."
        )
    print("\nDone.")


if __name__ == "__main__":
    main()
