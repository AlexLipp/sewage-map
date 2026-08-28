"""Tests for `split_history`, the per-CSO slicing of the Thames Water history tables.

The property that matters is that slicing loses nothing: the union of every slice must
reproduce the source table exactly, because the front-end reads a slice with the same
code it used to read the whole table.
"""

import json

import pytest

from split_history import sanitise_permit, split_history

# A source table in the layout `DataFrame.to_json()` produces: {column: {row_label: value}}.
# 'TEMP.2920' appears twice under two location names, as Thames Water's paired outfalls do,
# and 'RET/TH/24' carries the '/' that must not become an S3 folder separator.
SAMPLE_HISTORY = {
    "LocationName": {
        "0": "Streatham & Balham Storm Relief A",
        "1": "Hillside Avenue",
        "2": "Streatham & Balham Storm Relief B",
        "3": "Some Retention Tank",
    },
    "PermitNumber": {
        "0": "TEMP.2920",
        "1": "EPRAB3890AS",
        "2": "TEMP.2920",
        "3": "RET/TH/24",
    },
    "X": {"0": 527100, "1": 541000, "2": 527100, "3": 500000},
    "Y": {"0": 170900, "1": 190000, "2": 170900, "3": 180000},
    "ReceivingWaterCourse": {
        "0": "River Graveney",
        "1": "River Roding",
        "2": "River Graveney",
        "3": "River Thames",
    },
    "StartDateTime": {
        "0": 1782905400000,
        "1": 1782887400000,
        "2": 1782800000000,
        "3": 1782700000000,
    },
    "StopDateTime": {
        "0": 1782909900000,
        "1": 1782900900000,
        "2": 1782810000000,
        "3": 1782710000000,
    },
    "Duration": {"0": 75.0, "1": 225.0, "2": 166.0, "3": 166.0},
    "OngoingEvent": {"0": False, "1": False, "2": False, "3": False},
}


def test_slices_are_addressed_by_sanitised_permit_number():
    slices = split_history(SAMPLE_HISTORY)
    assert set(slices) == {"TEMP.2920", "EPRAB3890AS", "RET_TH_24"}


def test_slice_keeps_every_column_of_the_source():
    slice_ = json.loads(split_history(SAMPLE_HISTORY)["EPRAB3890AS"])
    assert set(slice_) == set(SAMPLE_HISTORY)


def test_slice_contains_only_that_permits_rows_with_original_labels_and_values():
    slice_ = json.loads(split_history(SAMPLE_HISTORY)["EPRAB3890AS"])
    assert slice_["LocationName"] == {"1": "Hillside Avenue"}
    assert slice_["StartDateTime"] == {"1": 1782887400000}
    assert slice_["OngoingEvent"] == {"1": False}


def test_a_shared_permit_keeps_both_outfalls_so_the_frontend_can_still_split_them():
    # Thames Water reuses one permit across paired outfalls. The slice must retain both,
    # because the front-end filters by LocationName within it.
    slice_ = json.loads(split_history(SAMPLE_HISTORY)["TEMP.2920"])
    assert set(slice_["LocationName"].values()) == {
        "Streatham & Balham Storm Relief A",
        "Streatham & Balham Storm Relief B",
    }


def test_union_of_slices_reproduces_the_source_table_exactly():
    slices = split_history(SAMPLE_HISTORY)

    rebuilt = {column: {} for column in SAMPLE_HISTORY}
    for body in slices.values():
        for column, cells in json.loads(body).items():
            rebuilt[column].update(cells)

    for column in SAMPLE_HISTORY:
        assert rebuilt[column] == SAMPLE_HISTORY[column], column


def test_rows_without_a_permit_number_are_skipped_rather_than_misfiled():
    history = {
        "LocationName": {"0": "Somewhere", "1": "Nowhere"},
        "PermitNumber": {"0": "CSAB.0557", "1": None},
    }
    slices = split_history(history)
    assert set(slices) == {"CSAB.0557"}


def test_permit_numbers_that_would_collide_raise_rather_than_overwrite():
    history = {
        "LocationName": {"0": "A", "1": "B"},
        "PermitNumber": {"0": "RET/TH/24", "1": "RET_TH_24"},
    }
    with pytest.raises(ValueError, match="sanitise"):
        split_history(history)


@pytest.mark.parametrize(
    "permit, expected",
    [
        ("EPRAB3890AS", "EPRAB3890AS"),
        ("CSAB.0557", "CSAB.0557"),
        ("RET/TH/24", "RET_TH_24"),
        ("TEMP.2920", "TEMP.2920"),
        (" CSAB.0557 ", "CSAB.0557"),
    ],
)
def test_sanitise_permit(permit, expected):
    assert sanitise_permit(permit) == expected


@pytest.mark.parametrize("permit", ["", "   ", "/", "..", "///"])
def test_sanitise_permit_rejects_permits_with_no_usable_file_name(permit):
    with pytest.raises(ValueError):
        sanitise_permit(permit)
