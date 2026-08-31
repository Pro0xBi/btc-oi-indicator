from __future__ import annotations

import argparse
from pathlib import Path
from typing import Sequence

import pandas as pd

from .export import export_artifacts
from .metrics import OiMetricSettings


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Calculate the BTC OI metrics and export two HTML charts.",
    )
    parser.add_argument(
        "--input",
        type=Path,
        default=Path("data/btc_history.csv"),
        help="Historical input CSV (default: data/btc_history.csv)",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=Path("output"),
        help="Artifact directory (default: output)",
    )
    parser.add_argument("--symbol", default="BTCUSDT", help="Chart symbol")
    parser.add_argument(
        "--skip-chart",
        action="store_true",
        help="Only export the calculated CSV and manifest",
    )
    parser.add_argument(
        "--chart-start",
        default="2024-01-01T00:00:00Z",
        help="Visible start of both charts after full-history calculation",
    )
    parser.add_argument(
        "--allow-partial-open-interest",
        action="store_true",
        help=(
            "Render known values with explicit OI gaps; default validation "
            "requires every OI row"
        ),
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    if not args.input.is_file():
        raise SystemExit(
            f"Input CSV not found: {args.input}. "
            "Place the server export at data/btc_history.csv or pass --input."
        )

    history = pd.read_csv(args.input, float_precision="round_trip")
    settings = OiMetricSettings(
        chart_start_timestamp=args.chart_start,
    )
    paths = export_artifacts(
        history,
        output_dir=args.output_dir,
        symbol=args.symbol,
        settings=settings,
        write_chart=not args.skip_chart,
        allow_missing_open_interest=args.allow_partial_open_interest,
    )
    for artifact, path in paths.items():
        print(f"{artifact}: {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
