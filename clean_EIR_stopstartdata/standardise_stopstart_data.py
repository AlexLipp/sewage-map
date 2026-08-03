"""Run the eight explicit raw-to-standardised company cleaners."""

from __future__ import annotations

import argparse

from raw_to_standardised.clean_anglian import clean_anglian
from raw_to_standardised.clean_northumbria import clean_northumbria
from raw_to_standardised.clean_severn_trent import clean_severn_trent
from raw_to_standardised.clean_south_west_water import clean_south_west_water
from raw_to_standardised.clean_southern_water import clean_southern_water
from raw_to_standardised.clean_united_utilities import clean_united_utilities
from raw_to_standardised.clean_wessex import clean_wessex
from raw_to_standardised.clean_yorkshire import clean_yorkshire


COMPANY_NAMES = [
    "anglian",
    "northumbria",
    "severn_trent",
    "south_west_water",
    "southern_water",
    "united_utilities",
    "wessex",
    "yorkshire",
]


def run_selected_cleaners(
    company: str | None, *, dry_run: bool, strict: bool
) -> int:
    """Call each selected cleaner explicitly and continue after safe failures."""
    exit_status = 0

    if company is None or company == "anglian":
        exit_status |= clean_anglian(dry_run=dry_run, strict=strict)
    if company is None or company == "northumbria":
        exit_status |= clean_northumbria(dry_run=dry_run, strict=strict)
    if company is None or company == "severn_trent":
        exit_status |= clean_severn_trent(dry_run=dry_run, strict=strict)
    if company is None or company == "south_west_water":
        exit_status |= clean_south_west_water(dry_run=dry_run, strict=strict)
    if company is None or company == "southern_water":
        exit_status |= clean_southern_water(dry_run=dry_run, strict=strict)
    if company is None or company == "united_utilities":
        exit_status |= clean_united_utilities(dry_run=dry_run, strict=strict)
    if company is None or company == "wessex":
        exit_status |= clean_wessex(dry_run=dry_run, strict=strict)
    if company is None or company == "yorkshire":
        exit_status |= clean_yorkshire(dry_run=dry_run, strict=strict)

    return exit_status


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--company", choices=COMPANY_NAMES)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--strict", action="store_true")
    parser.add_argument("--self-check", action="store_true")
    arguments = parser.parse_args()

    if arguments.self_check:
        cleaners = [
            clean_anglian,
            clean_northumbria,
            clean_severn_trent,
            clean_south_west_water,
            clean_southern_water,
            clean_united_utilities,
            clean_wessex,
            clean_yorkshire,
        ]
        if not all(callable(cleaner) for cleaner in cleaners):
            raise TypeError("A company cleaner is not callable.")
        print("All eight explicit company cleaners imported successfully.")
        return 0

    return run_selected_cleaners(
        arguments.company, dry_run=arguments.dry_run, strict=arguments.strict
    )


if __name__ == "__main__":
    raise SystemExit(main())
