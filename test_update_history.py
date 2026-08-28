"""Tests for how update_history.py locates the history master.

Which table a run starts from decides whether it makes two API requests or a
hundred and fifty, so the fallback order is worth pinning down. These tests stub
S3 rather than touching it.
"""

import os
import sys

import pandas as pd
import pytest

os.environ.setdefault("S3_PROFILE_NAME", "test-profile")
sys.path.insert(0, os.path.dirname(__file__))

import update_history as uh  # noqa: E402
from poopy.history_io import HISTORY_COLUMNS, write_history_json  # noqa: E402


def make_history(rows=3):
    """Build a small but schema-complete history table."""
    return pd.DataFrame(
        [
            {
                "LocationName": f"Site {i}",
                "PermitNumber": f"TEMP.{i}",
                "X": 100 + i,
                "Y": 200 + i,
                "ReceivingWaterCourse": "River Test",
                "StartDateTime": pd.Timestamp("2026-01-01") + pd.Timedelta(days=i),
                "StopDateTime": pd.Timestamp("2026-01-01")
                + pd.Timedelta(days=i, hours=1),
                "Duration": 60.0,
                "OngoingEvent": False,
            }
            for i in range(rows)
        ],
        columns=HISTORY_COLUMNS,
    )


@pytest.fixture
def s3(tmp_path, monkeypatch):
    """Stub S3 with a dict of key -> history table, writing to a temp dir."""
    monkeypatch.setattr(uh, "LOCAL_MASTER_DIR", str(tmp_path) + "/")
    objects: dict[str, pd.DataFrame] = {}
    requested: list[str] = []

    def fake_download(bucket_name, object_name, file_path, profile_name):
        requested.append(object_name)
        if object_name not in objects:
            return False
        write_history_json(objects[object_name], file_path)
        return True

    monkeypatch.setattr(uh, "download_file_from_s3", fake_download)
    return objects, requested


def test_uses_the_master_when_one_exists(s3):
    """The master is preferred, and nothing else is fetched."""
    objects, requested = s3
    objects[uh.AWS_MASTER_DIR + uh.AWS_DISCHARGE_MASTER_FILENAME] = make_history(5)

    df = uh.fetch_master_table(
        uh.AWS_DISCHARGE_MASTER_FILENAME, uh.AWS_JSON_FILENAME, dry_run=False
    )

    assert len(df) == 5
    assert requested == [uh.AWS_MASTER_DIR + uh.AWS_DISCHARGE_MASTER_FILENAME]


def test_seeds_from_the_published_table_when_no_master_exists(s3):
    """
    A missing master falls back to the published history, not a full rebuild.

    The published table is a complete history in the same schema, so refetching
    it from the API would re-download what is already in the bucket.
    """
    objects, requested = s3
    objects[uh.AWS_HISTORICAL_DIR + uh.AWS_JSON_FILENAME] = make_history(7)

    df = uh.fetch_master_table(
        uh.AWS_DISCHARGE_MASTER_FILENAME, uh.AWS_JSON_FILENAME, dry_run=False
    )

    assert df is not None, "should have seeded from the published table"
    assert len(df) == 7
    assert requested == [
        uh.AWS_MASTER_DIR + uh.AWS_DISCHARGE_MASTER_FILENAME,
        uh.AWS_HISTORICAL_DIR + uh.AWS_JSON_FILENAME,
    ]


def test_returns_none_only_when_neither_exists(s3):
    """With nothing to start from, the caller is left to do a full rebuild."""
    _, requested = s3

    df = uh.fetch_master_table(
        uh.AWS_DISCHARGE_MASTER_FILENAME, uh.AWS_JSON_FILENAME, dry_run=False
    )

    assert df is None
    assert len(requested) == 2


def test_offline_table_falls_back_to_the_offline_published_file(s3):
    """Each table seeds from its own published counterpart, not the discharge one."""
    objects, requested = s3
    objects[uh.AWS_HISTORICAL_DIR + uh.AWS_OFFLINE_JSON_FILENAME] = make_history(2)

    df = uh.fetch_master_table(
        uh.AWS_OFFLINE_MASTER_FILENAME, uh.AWS_OFFLINE_JSON_FILENAME, dry_run=False
    )

    assert len(df) == 2
    assert requested[-1] == uh.AWS_HISTORICAL_DIR + uh.AWS_OFFLINE_JSON_FILENAME


def test_dry_run_prefers_an_existing_local_master(s3, tmp_path):
    """A dry run reuses the local copy and does not reach for S3 at all."""
    _, requested = s3
    write_history_json(
        make_history(4), str(tmp_path / uh.AWS_DISCHARGE_MASTER_FILENAME)
    )

    df = uh.fetch_master_table(
        uh.AWS_DISCHARGE_MASTER_FILENAME, uh.AWS_JSON_FILENAME, dry_run=True
    )

    assert len(df) == 4
    assert requested == []
