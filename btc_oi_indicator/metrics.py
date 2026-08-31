from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any

import numpy as np
import pandas as pd

from .data import prepare_history


@dataclass(frozen=True)
class OiMetricSettings:
    """Parameters for the anchored and rolling OI metric calculations."""

    anchored_threshold_window: int = 60
    anchored_lower_quantile: float = 0.008
    anchored_upper_quantile: float = 0.9995
    rolling_offset_window: int = 60
    rolling_threshold_window: int = 60
    rolling_lower_quantile: float = 0.01
    rolling_upper_quantile: float = 0.99
    funding_sum_window: int = 7
    funding_threshold_window: int = 120
    funding_lower_quantile: float = 0.008
    funding_upper_quantile: float = 0.9995
    chart_start_timestamp: str = "2024-01-01T00:00:00Z"

    def __post_init__(self) -> None:
        positive_windows = {
            "anchored_threshold_window": self.anchored_threshold_window,
            "rolling_offset_window": self.rolling_offset_window,
            "rolling_threshold_window": self.rolling_threshold_window,
            "funding_sum_window": self.funding_sum_window,
            "funding_threshold_window": self.funding_threshold_window,
        }
        for name, value in positive_windows.items():
            if value <= 0:
                raise ValueError(f"{name} must be positive")

        quantiles = {
            "anchored": (
                self.anchored_lower_quantile,
                self.anchored_upper_quantile,
            ),
            "rolling": (
                self.rolling_lower_quantile,
                self.rolling_upper_quantile,
            ),
            "funding": (
                self.funding_lower_quantile,
                self.funding_upper_quantile,
            ),
        }
        for name, (lower, upper) in quantiles.items():
            if not 0 <= lower < upper <= 1:
                raise ValueError(
                    f"{name} quantiles must satisfy 0 <= lower < upper <= 1"
                )
        pd.to_datetime(self.chart_start_timestamp, utc=True, errors="raise")

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _add_quantile_bounds(
    frame: pd.DataFrame,
    column: str,
    *,
    window: int,
    lower_quantile: float,
    upper_quantile: float,
) -> None:
    lower = f"{column}_lower_bound"
    upper = f"{column}_upper_bound"
    frame[lower] = frame[column].rolling(window).quantile(lower_quantile)
    frame[upper] = frame[column].rolling(window).quantile(upper_quantile)
    frame[f"{column}_low_signal"] = (frame[column] < frame[lower]).astype(int)
    frame[f"{column}_high_signal"] = (frame[column] > frame[upper]).astype(int)


def calculate_oi_indicators(
    history: pd.DataFrame,
    *,
    settings: OiMetricSettings | None = None,
    allow_missing_open_interest: bool = False,
) -> pd.DataFrame:
    """Calculate the two OI/price divergences and the funding-rate sum."""

    settings = settings or OiMetricSettings()
    calculated = prepare_history(
        history,
        allow_missing_open_interest=allow_missing_open_interest,
    )
    if "funding_rate" not in calculated.columns:
        raise ValueError(
            "funding_rate is required for the OI charts; run btc-oi-backfill "
            "or export the FR column from the server history"
        )

    funding_rate = pd.to_numeric(calculated["funding_rate"], errors="coerce")
    invalid_funding = funding_rate.isna() | ~np.isfinite(
        funding_rate.to_numpy(dtype=float)
    )
    if invalid_funding.any():
        # Binance's daily alignment uses the next UTC day's first funding
        # event.  The newest row can therefore be unavailable until the next
        # event arrives.  Permit only that trailing gap; a hole in the
        # historical portion still indicates incomplete input data.
        valid_positions = np.flatnonzero(~invalid_funding.to_numpy())
        if len(valid_positions) == 0:
            raise ValueError(
                "funding_rate is missing or invalid for every row; "
                "run btc-oi-backfill first"
            )
        last_valid_position = int(valid_positions[-1])
        interior_invalid = invalid_funding.to_numpy()[: last_valid_position + 1]
        if interior_invalid.any():
            bad_rows = calculated.index[invalid_funding].tolist()[:5]
            raise ValueError(
                "funding_rate is missing or invalid at historical rows: "
                f"{bad_rows}; run btc-oi-backfill first"
            )

    source_columns = [
        "timestamp",
        "open",
        "high",
        "low",
        "close",
        "open_interest",
        "funding_rate",
    ]
    frame = calculated[source_columns].copy()
    frame["funding_rate"] = funding_rate

    # Both series are anchored to the first complete OI/price observation.
    baseline = calculated.loc[calculated["open_interest"].notna()].iloc[0]
    baseline_oi = float(baseline["open_interest"])
    baseline_close = float(baseline["close"])
    frame["anchored_oi_price_divergence"] = (
        frame["open_interest"] / baseline_oi
        - frame["close"] / baseline_close
    )

    # Rolling divergence: each series is divided by its own 60-day mean.
    price_average = frame["close"].rolling(settings.rolling_offset_window).mean()
    oi_average = frame["open_interest"].rolling(
        settings.rolling_offset_window
    ).mean()
    frame["rolling_oi_price_divergence"] = (
        frame["open_interest"] / oi_average - frame["close"] / price_average
    )
    # Require a complete window so a trailing missing funding rate does not
    # silently turn a 7-day sum into a 6-day sum.
    frame["funding_rate_7d_sum"] = frame["funding_rate"].rolling(
        settings.funding_sum_window,
        min_periods=settings.funding_sum_window,
    ).sum()

    _add_quantile_bounds(
        frame,
        "anchored_oi_price_divergence",
        window=settings.anchored_threshold_window,
        lower_quantile=settings.anchored_lower_quantile,
        upper_quantile=settings.anchored_upper_quantile,
    )
    _add_quantile_bounds(
        frame,
        "rolling_oi_price_divergence",
        window=settings.rolling_threshold_window,
        lower_quantile=settings.rolling_lower_quantile,
        upper_quantile=settings.rolling_upper_quantile,
    )
    _add_quantile_bounds(
        frame,
        "funding_rate_7d_sum",
        window=settings.funding_threshold_window,
        lower_quantile=settings.funding_lower_quantile,
        upper_quantile=settings.funding_upper_quantile,
    )

    frame.attrs["baseline"] = {
        "source": "first_valid_row",
        "timestamp": pd.Timestamp(baseline["timestamp"]).isoformat(),
        "open_interest": baseline_oi,
        "close": baseline_close,
    }
    frame.attrs["settings"] = settings.to_dict()
    frame.attrs["input_coverage"] = {
        "rows": len(frame),
        "missing_open_interest_rows": int(frame["open_interest"].isna().sum()),
        "missing_funding_rate_rows": int(frame["funding_rate"].isna().sum()),
    }
    return frame


def select_chart_window(
    frame: pd.DataFrame,
    settings: OiMetricSettings,
) -> pd.DataFrame:
    """Filter only after all rolling calculations are complete."""

    start = pd.to_datetime(settings.chart_start_timestamp, utc=True)
    selected = frame.loc[frame["timestamp"] >= start].copy()
    if selected.empty:
        raise ValueError(
            "chart window is empty; chart_start_timestamp is after the data"
        )
    return selected
