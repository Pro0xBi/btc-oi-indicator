from __future__ import annotations

from typing import Mapping

import numpy as np
import pandas as pd


_ALIASES: Mapping[str, tuple[str, ...]] = {
    "timestamp": ("timestamp", "stat_date", "stat_ts"),
    "open_interest": ("open_interest", "OI", "oi"),
    "funding_rate": ("funding_rate", "FR", "funding"),
}

_REQUIRED_COLUMNS = (
    "timestamp",
    "open",
    "high",
    "low",
    "close",
    "open_interest",
)


def prepare_history(
    history: pd.DataFrame,
    *,
    allow_missing_open_interest: bool = False,
) -> pd.DataFrame:
    """Normalize and validate the daily OHLC/OI/Funding CSV contract."""

    if history.empty:
        raise ValueError("history is empty")

    frame = history.copy()
    rename: dict[str, str] = {}
    for canonical, candidates in _ALIASES.items():
        if canonical in frame.columns:
            continue
        match = next(
            (candidate for candidate in candidates if candidate in frame.columns),
            None,
        )
        if match is not None:
            rename[match] = canonical
    frame = frame.rename(columns=rename)

    missing = [column for column in _REQUIRED_COLUMNS if column not in frame.columns]
    if missing:
        raise ValueError(f"missing required columns: {', '.join(missing)}")

    frame["timestamp"] = pd.to_datetime(frame["timestamp"], utc=True, errors="raise")
    if frame["timestamp"].duplicated().any():
        duplicates = frame.loc[
            frame["timestamp"].duplicated(keep=False), "timestamp"
        ]
        preview = ", ".join(str(value) for value in duplicates.head(3))
        raise ValueError(f"duplicate timestamps are not allowed: {preview}")

    numeric_columns = ["open", "high", "low", "close", "open_interest"]
    if "funding_rate" in frame.columns:
        numeric_columns.append("funding_rate")
    for column in numeric_columns:
        frame[column] = pd.to_numeric(frame[column], errors="coerce")

    required_price = frame[["open", "high", "low", "close"]]
    if required_price.isna().any().any() or not np.isfinite(
        required_price.to_numpy(dtype=float)
    ).all():
        raise ValueError("required OHLC values must be finite")

    if not allow_missing_open_interest and frame["open_interest"].isna().any():
        bad_rows = frame.index[frame["open_interest"].isna()].tolist()[:5]
        raise ValueError(
            "required numeric values are missing or invalid at rows: "
            f"{bad_rows}"
        )
    present_oi = frame["open_interest"].dropna()
    if present_oi.empty:
        raise ValueError("open_interest has no valid values")
    if not np.isfinite(present_oi.to_numpy(dtype=float)).all():
        raise ValueError("open_interest values must be finite")

    if (required_price <= 0).any().any() or (present_oi <= 0).any():
        raise ValueError("OHLC and open_interest values must be positive")

    invalid_ohlc = (
        frame["high"] < frame[["open", "close", "low"]].max(axis=1)
    ) | (frame["low"] > frame[["open", "close", "high"]].min(axis=1))
    if invalid_ohlc.any():
        bad_rows = frame.index[invalid_ohlc].tolist()[:5]
        raise ValueError(f"invalid OHLC relationships at rows: {bad_rows}")

    return frame.sort_values("timestamp").reset_index(drop=True)
