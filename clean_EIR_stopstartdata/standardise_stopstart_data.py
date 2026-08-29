"""Run the complete stop/start standardisation pipeline."""

if __package__:
    from .raw_to_standardised import common
else:
    from raw_to_standardised._bootstrap import common


def main() -> int:
    """Run all companies or the company selected on the shared CLI."""
    common.load_company_modules()
    return common.main()


if __name__ == "__main__":
    raise SystemExit(main())
