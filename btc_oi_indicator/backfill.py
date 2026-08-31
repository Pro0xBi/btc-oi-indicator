from __future__ import annotations

import argparse
import csv
import io
import json
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
import zipfile
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import date, datetime, time as datetime_time, timedelta, timezone
from pathlib import Path
from tempfile import NamedTemporaryFile
from typing import Callable, Iterable, Mapping, Protocol, Sequence

import numpy as np
import pandas as pd

from .data import prepare_history


SPOT_KLINES_URL = "https://data-api.binance.vision/api/v3/klines"
FUTURES_OI_URL = "https://fapi.binance.com/futures/data/openInterestHist"
FUNDING_RATE_URL = "https://fapi.binance.com/fapi/v1/fundingRate"
ARCHIVE_BUCKET_URL = "https://s3-ap-northeast-1.amazonaws.com/data.binance.vision"
ARCHIVE_DOWNLOAD_ROOT = "https://data.binance.vision"


class HistorySource(Protocol):
    def fetch_spot_daily_klines(
        self,
        symbol: str,
        start_date: date,
        end_date: date,
    ) -> Mapping[date, tuple[float, float, float, float]]: ...

    def list_archived_oi_dates(
        self,
        symbol: str,
        start_date: date,
        end_date: date,
    ) -> set[date]: ...

    def fetch_archived_oi_value(self, symbol: str, target_date: date) -> float: ...

    def fetch_recent_oi_values(
        self,
        symbol: str,
        start_date: date,
        end_date: date,
    ) -> Mapping[date, float]: ...

    def fetch_daily_funding_rates(
        self,
        symbol: str,
        start_date: date,
        end_date: date,
    ) -> Mapping[date, float]: ...


@dataclass(frozen=True)
class BackfillResult:
    history: pd.DataFrame
    report: dict[str, object]


def _utc_milliseconds(value: datetime) -> int:
    return int(value.timestamp() * 1000)


def _utc_start(target_date: date) -> datetime:
    return datetime.combine(target_date, datetime_time.min, tzinfo=timezone.utc)


def _parse_date(value: str) -> date:
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("date must use YYYY-MM-DD") from exc


def _date_range(start_date: date, end_date: date) -> list[date]:
    if end_date < start_date:
        return []
    days = (end_date - start_date).days
    return [start_date + timedelta(days=offset) for offset in range(days + 1)]


def _date_ranges(values: Iterable[date]) -> list[dict[str, object]]:
    ordered = sorted(set(values))
    if not ordered:
        return []

    ranges: list[dict[str, object]] = []
    range_start = ordered[0]
    previous = ordered[0]
    for current in ordered[1:]:
        if current != previous + timedelta(days=1):
            ranges.append(
                {
                    "start": range_start.isoformat(),
                    "end": previous.isoformat(),
                    "days": (previous - range_start).days + 1,
                }
            )
            range_start = current
        previous = current
    ranges.append(
        {
            "start": range_start.isoformat(),
            "end": previous.isoformat(),
            "days": (previous - range_start).days + 1,
        }
    )
    return ranges


def _finite_float(value: object, *, field: str) -> float:
    try:
        result = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"invalid {field}: {value!r}") from exc
    if not np.isfinite(result):
        raise ValueError(f"{field} must be finite")
    return result


def _positive_float(value: object, *, field: str) -> float:
    result = _finite_float(value, field=field)
    if result <= 0:
        raise ValueError(f"{field} must be positive and finite")
    return result


class BinancePublicHistoryClient:
    """Read public Binance price data and USD-notional OI history."""

    def __init__(
        self,
        *,
        timeout_seconds: int = 30,
        retry_attempts: int = 3,
        request_bytes: Callable[[str], bytes] | None = None,
    ) -> None:
        if timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")
        if retry_attempts <= 0:
            raise ValueError("retry_attempts must be positive")
        self.timeout_seconds = timeout_seconds
        self.retry_attempts = retry_attempts
        self._request_bytes_override = request_bytes

    def _request_bytes(self, url: str) -> bytes:
        if self._request_bytes_override is not None:
            return self._request_bytes_override(url)

        request = urllib.request.Request(
            url,
            headers={"User-Agent": "btc-oi-indicator/1.0"},
        )
        last_error: Exception | None = None
        for attempt in range(self.retry_attempts):
            try:
                with urllib.request.urlopen(
                    request,
                    timeout=self.timeout_seconds,
                ) as response:
                    return response.read()
            except urllib.error.HTTPError as exc:
                if exc.code == 404:
                    raise FileNotFoundError(url) from exc
                last_error = exc
            except urllib.error.URLError as exc:
                last_error = exc
            if attempt + 1 < self.retry_attempts:
                time.sleep(2**attempt)
        raise RuntimeError(f"request failed after retries: {url}") from last_error

    def _request_json(self, url: str) -> object:
        payload = self._request_bytes(url)
        try:
            return json.loads(payload.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ValueError(f"invalid JSON response: {url}") from exc

    def fetch_spot_daily_klines(
        self,
        symbol: str,
        start_date: date,
        end_date: date,
    ) -> dict[date, tuple[float, float, float, float]]:
        if end_date < start_date:
            return {}

        cursor = _utc_start(start_date)
        end_exclusive = _utc_start(end_date + timedelta(days=1))
        result: dict[date, tuple[float, float, float, float]] = {}
        while cursor < end_exclusive:
            query = urllib.parse.urlencode(
                {
                    "symbol": symbol,
                    "interval": "1d",
                    "startTime": _utc_milliseconds(cursor),
                    "endTime": _utc_milliseconds(end_exclusive) - 1,
                    "limit": 1000,
                }
            )
            payload = self._request_json(f"{SPOT_KLINES_URL}?{query}")
            if not isinstance(payload, list) or not payload:
                break

            last_open_time: int | None = None
            for row in payload:
                if not isinstance(row, list) or len(row) < 5:
                    raise ValueError("unexpected Binance spot kline response")
                open_time = int(row[0])
                row_date = datetime.fromtimestamp(
                    open_time / 1000,
                    tz=timezone.utc,
                ).date()
                if start_date <= row_date <= end_date:
                    result[row_date] = (
                        _positive_float(row[1], field="open"),
                        _positive_float(row[2], field="high"),
                        _positive_float(row[3], field="low"),
                        _positive_float(row[4], field="close"),
                    )
                last_open_time = open_time

            if last_open_time is None:
                break
            next_cursor = datetime.fromtimestamp(
                last_open_time / 1000,
                tz=timezone.utc,
            ) + timedelta(days=1)
            if next_cursor <= cursor:
                break
            cursor = next_cursor
            if len(payload) < 1000:
                break
        return result

    def list_archived_oi_dates(
        self,
        symbol: str,
        start_date: date,
        end_date: date,
    ) -> set[date]:
        if end_date < start_date:
            return set()

        prefix = f"data/futures/um/daily/metrics/{symbol}/"
        previous_date = start_date - timedelta(days=1)
        start_after = (
            f"{prefix}{symbol}-metrics-{previous_date.isoformat()}.zip.CHECKSUM"
        )
        continuation_token: str | None = None
        result: set[date] = set()
        filename_pattern = re.compile(
            rf"^{re.escape(prefix + symbol)}-metrics-(\d{{4}}-\d{{2}}-\d{{2}})\.zip$"
        )

        while True:
            parameters: dict[str, object] = {
                "list-type": 2,
                "prefix": prefix,
                "max-keys": 1000,
            }
            if continuation_token:
                parameters["continuation-token"] = continuation_token
            else:
                parameters["start-after"] = start_after
            url = f"{ARCHIVE_BUCKET_URL}?{urllib.parse.urlencode(parameters)}"
            payload = self._request_bytes(url)
            try:
                root = ET.fromstring(payload)
            except ET.ParseError as exc:
                raise ValueError("invalid Binance archive listing") from exc

            page_dates: list[date] = []
            for key_node in root.findall(".//{*}Key"):
                key = key_node.text or ""
                match = filename_pattern.match(key)
                if not match:
                    continue
                archived_date = date.fromisoformat(match.group(1))
                page_dates.append(archived_date)
                if start_date <= archived_date <= end_date:
                    result.add(archived_date)

            is_truncated = (root.findtext(".//{*}IsTruncated") or "").lower()
            if is_truncated != "true":
                break
            if page_dates and max(page_dates) > end_date:
                break
            continuation_token = root.findtext(".//{*}NextContinuationToken")
            if not continuation_token:
                raise ValueError("truncated Binance archive listing has no token")
        return result

    def fetch_archived_oi_value(self, symbol: str, target_date: date) -> float:
        filename = f"{symbol}-metrics-{target_date.isoformat()}.zip"
        url = (
            f"{ARCHIVE_DOWNLOAD_ROOT}/data/futures/um/daily/metrics/"
            f"{symbol}/{filename}"
        )
        payload = self._request_bytes(url)
        try:
            with zipfile.ZipFile(io.BytesIO(payload)) as archive:
                csv_names = [name for name in archive.namelist() if name.endswith(".csv")]
                if len(csv_names) != 1:
                    raise ValueError(f"expected one metrics CSV in {filename}")
                with archive.open(csv_names[0]) as raw_handle:
                    text_handle = io.TextIOWrapper(raw_handle, encoding="utf-8")
                    reader = csv.DictReader(text_handle)
                    for row in reader:
                        timestamp = datetime.strptime(
                            row["create_time"],
                            "%Y-%m-%d %H:%M:%S",
                        )
                        if (
                            timestamp.date() == target_date
                            and timestamp.time() == datetime_time(23, 55)
                        ):
                            return _positive_float(
                                row["sum_open_interest_value"],
                                field="sum_open_interest_value",
                            )
        except zipfile.BadZipFile as exc:
            raise ValueError(f"invalid metrics archive: {filename}") from exc
        raise ValueError(f"23:55 UTC OI sample is missing from {filename}")

    def fetch_recent_oi_values(
        self,
        symbol: str,
        start_date: date,
        end_date: date,
    ) -> dict[date, float]:
        if end_date < start_date:
            return {}

        # The legacy daily job shifted the next UTC-midnight sample back one day.
        query_start = _utc_start(start_date + timedelta(days=1))
        query_end = _utc_start(end_date + timedelta(days=1))
        query = urllib.parse.urlencode(
            {
                "symbol": symbol,
                "period": "1d",
                "startTime": _utc_milliseconds(query_start),
                "endTime": _utc_milliseconds(query_end),
                "limit": 500,
            }
        )
        payload = self._request_json(f"{FUTURES_OI_URL}?{query}")
        if not isinstance(payload, list):
            raise ValueError("unexpected Binance OI response")

        result: dict[date, float] = {}
        for row in payload:
            if not isinstance(row, dict):
                raise ValueError("unexpected Binance OI row")
            timestamp = datetime.fromtimestamp(
                int(row["timestamp"]) / 1000,
                tz=timezone.utc,
            )
            target = timestamp.date() - timedelta(days=1)
            if start_date <= target <= end_date:
                result[target] = _positive_float(
                    row["sumOpenInterestValue"],
                    field="sumOpenInterestValue",
                )
        return result

    def fetch_daily_funding_rates(
        self,
        symbol: str,
        start_date: date,
        end_date: date,
    ) -> dict[date, float]:
        """Match the legacy daily alignment using the next day's first event."""

        if end_date < start_date:
            return {}

        first_event_date = start_date + timedelta(days=1)
        last_event_date = end_date + timedelta(days=1)
        cursor_ms = _utc_milliseconds(_utc_start(first_event_date))
        end_ms = _utc_milliseconds(_utc_start(last_event_date))
        first_rate_by_event_date: dict[date, float] = {}

        while cursor_ms <= end_ms:
            query = urllib.parse.urlencode(
                {
                    "symbol": symbol,
                    "startTime": cursor_ms,
                    "endTime": end_ms,
                    "limit": 1000,
                }
            )
            payload = self._request_json(f"{FUNDING_RATE_URL}?{query}")
            if not isinstance(payload, list) or not payload:
                break

            last_funding_time: int | None = None
            for row in payload:
                if not isinstance(row, dict):
                    raise ValueError("unexpected Binance funding-rate row")
                funding_time = int(row["fundingTime"])
                last_funding_time = funding_time
                if row.get("rateType", "Regular") != "Regular":
                    continue
                event_date = datetime.fromtimestamp(
                    funding_time / 1000,
                    tz=timezone.utc,
                ).date()
                if first_event_date <= event_date <= last_event_date:
                    first_rate_by_event_date.setdefault(
                        event_date,
                        _finite_float(row["fundingRate"], field="fundingRate"),
                    )

            if last_funding_time is None:
                break
            next_cursor_ms = last_funding_time + 1
            if next_cursor_ms <= cursor_ms:
                break
            cursor_ms = next_cursor_ms
            if len(payload) < 1000:
                break

        return {
            event_date - timedelta(days=1): value
            for event_date, value in first_rate_by_event_date.items()
        }


def _fill_archived_oi(
    client: HistorySource,
    symbol: str,
    target_dates: Sequence[date],
    *,
    workers: int,
) -> tuple[dict[date, float], dict[date, str]]:
    values: dict[date, float] = {}
    errors: dict[date, str] = {}
    if not target_dates:
        return values, errors

    with ThreadPoolExecutor(max_workers=workers) as executor:
        futures = {
            executor.submit(client.fetch_archived_oi_value, symbol, target): target
            for target in target_dates
        }
        for future in as_completed(futures):
            target = futures[future]
            try:
                values[target] = future.result()
            except Exception as exc:  # preserve partial progress and report the date
                errors[target] = str(exc)
    return values, errors


def backfill_history(
    history: pd.DataFrame,
    *,
    end_date: date,
    client: HistorySource,
    symbol: str = "BTCUSDT",
    workers: int = 8,
    today: date | None = None,
) -> BackfillResult:
    """Fill every missing price day and every authoritative OI value available."""

    if workers <= 0:
        raise ValueError("workers must be positive")
    frame = prepare_history(history, allow_missing_open_interest=True)
    if "funding_rate" not in frame.columns:
        frame["funding_rate"] = np.nan
    else:
        frame["funding_rate"] = pd.to_numeric(
            frame["funding_rate"],
            errors="coerce",
        )
    if not frame["timestamp"].dt.time.eq(datetime_time.min).all():
        raise ValueError("backfill input timestamps must be UTC day starts")

    first_date = frame["timestamp"].min().date()
    existing_end_date = frame["timestamp"].max().date()
    if end_date < existing_end_date:
        raise ValueError(
            f"end_date {end_date} is earlier than existing data {existing_end_date}"
        )

    full_calendar = _date_range(first_date, end_date)
    existing_dates = set(frame["timestamp"].dt.date)
    missing_price_dates = [day for day in full_calendar if day not in existing_dates]

    price_values: dict[date, tuple[float, float, float, float]] = {}
    for missing_range in _date_ranges(missing_price_dates):
        range_start = date.fromisoformat(str(missing_range["start"]))
        range_end = date.fromisoformat(str(missing_range["end"]))
        price_values.update(
            client.fetch_spot_daily_klines(symbol, range_start, range_end)
        )
    unavailable_prices = [
        target for target in missing_price_dates if target not in price_values
    ]
    if unavailable_prices:
        ranges = _date_ranges(unavailable_prices)
        raise ValueError(f"Binance spot prices are missing for: {ranges}")

    new_rows = []
    for target in missing_price_dates:
        open_price, high, low, close = price_values[target]
        new_rows.append(
            {
                "timestamp": pd.Timestamp(target, tz="UTC"),
                "open": open_price,
                "high": high,
                "low": low,
                "close": close,
                "open_interest": np.nan,
                "funding_rate": np.nan,
            }
        )
    if new_rows:
        frame = pd.concat([frame, pd.DataFrame(new_rows)], ignore_index=True)
        frame = frame.sort_values("timestamp").reset_index(drop=True)

    oi_target_dates = sorted(
        frame.loc[frame["open_interest"].isna(), "timestamp"].dt.date.tolist()
    )
    archive_values: dict[date, float] = {}
    archive_errors: dict[date, str] = {}
    if oi_target_dates:
        archive_dates = client.list_archived_oi_dates(
            symbol,
            min(oi_target_dates),
            max(oi_target_dates),
        )
        archive_targets = sorted(set(oi_target_dates).intersection(archive_dates))
        archive_values, archive_errors = _fill_archived_oi(
            client,
            symbol,
            archive_targets,
            workers=workers,
        )

    today = today or datetime.now(timezone.utc).date()
    unresolved = [target for target in oi_target_dates if target not in archive_values]
    recent_cutoff = today - timedelta(days=31)
    recent_targets = [target for target in unresolved if target >= recent_cutoff]
    recent_values: dict[date, float] = {}
    if recent_targets:
        recent_values = dict(
            client.fetch_recent_oi_values(
                symbol,
                min(recent_targets),
                max(recent_targets),
            )
        )

    oi_values = {**archive_values, **recent_values}
    for target, value in oi_values.items():
        mask = frame["timestamp"].dt.date == target
        frame.loc[mask, "open_interest"] = value

    funding_target_dates = sorted(
        frame.loc[frame["funding_rate"].isna(), "timestamp"].dt.date.tolist()
    )
    funding_values: dict[date, float] = {}
    if funding_target_dates:
        funding_values = dict(
            client.fetch_daily_funding_rates(
                symbol,
                min(funding_target_dates),
                max(funding_target_dates),
            )
        )
        for target, value in funding_values.items():
            mask = frame["timestamp"].dt.date == target
            frame.loc[mask, "funding_rate"] = value

    missing_oi_dates = sorted(
        frame.loc[frame["open_interest"].isna(), "timestamp"].dt.date.tolist()
    )
    missing_funding_dates = sorted(
        frame.loc[frame["funding_rate"].isna(), "timestamp"].dt.date.tolist()
    )

    complete_oi_dates = set(full_calendar).difference(missing_oi_dates)
    oi_contiguous_through: date | None = None
    for target in full_calendar:
        if target not in complete_oi_dates:
            break
        oi_contiguous_through = target

    complete_funding_dates = set(full_calendar).difference(missing_funding_dates)
    funding_contiguous_through: date | None = None
    for target in full_calendar:
        if target not in complete_funding_dates:
            break
        funding_contiguous_through = target

    report: dict[str, object] = {
        "project": "btc-oi-indicator",
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "symbol": symbol,
        "result": {
            "rows": len(frame),
            "start_date": first_date.isoformat(),
            "end_date": end_date.isoformat(),
        },
        "price": {
            "source": "Binance Spot daily klines",
            "backfilled_rows": len(missing_price_dates),
            "missing_rows": 0,
            "complete_through": end_date.isoformat(),
            "backfilled_ranges": _date_ranges(missing_price_dates),
        },
        "open_interest": {
            "unit": "USD notional value",
            "field": "sumOpenInterestValue / sum_open_interest_value",
            "archive_backfilled_rows": len(archive_values),
            "recent_api_backfilled_rows": len(
                set(recent_values).difference(archive_values)
            ),
            "missing_rows": len(missing_oi_dates),
            "missing_ranges": _date_ranges(missing_oi_dates),
            "contiguous_through": (
                oi_contiguous_through.isoformat() if oi_contiguous_through else None
            ),
            "archive_errors": [
                {"date": target.isoformat(), "error": archive_errors[target]}
                for target in sorted(archive_errors)
            ],
        },
        "funding_rate": {
            "source": "Binance USD-M Futures Funding Rate History",
            "field": "fundingRate",
            "daily_alignment": "first funding event on next UTC day",
            "backfilled_rows": len(
                set(funding_values).intersection(funding_target_dates)
            ),
            "missing_rows": len(missing_funding_dates),
            "missing_ranges": _date_ranges(missing_funding_dates),
            "contiguous_through": (
                funding_contiguous_through.isoformat()
                if funding_contiguous_through
                else None
            ),
        },
    }
    return BackfillResult(history=frame, report=report)


def _write_history_atomic(frame: pd.DataFrame, destination: Path) -> None:
    destination = destination.expanduser().resolve()
    destination.parent.mkdir(parents=True, exist_ok=True)
    export = frame[
        [
            "timestamp",
            "open",
            "high",
            "low",
            "close",
            "open_interest",
            "funding_rate",
        ]
    ].copy()
    export["timestamp"] = export["timestamp"].dt.strftime("%Y-%m-%dT%H:%M:%SZ")

    temp_path: Path | None = None
    try:
        with NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            newline="",
            dir=destination.parent,
            prefix=f".{destination.name}.",
            suffix=".tmp",
            delete=False,
        ) as handle:
            temp_path = Path(handle.name)
            # Seventeen significant digits preserve an IEEE-754 float on reload,
            # so existing source rows are not numerically changed by backfilling.
            export.to_csv(handle, index=False, float_format="%.17g")
        os.replace(temp_path, destination)
        destination.chmod(0o644)
    finally:
        if temp_path is not None and temp_path.exists():
            temp_path.unlink()


def _write_report_atomic(report: Mapping[str, object], destination: Path) -> None:
    destination = destination.expanduser().resolve()
    destination.parent.mkdir(parents=True, exist_ok=True)
    temp_path: Path | None = None
    try:
        with NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=destination.parent,
            prefix=f".{destination.name}.",
            suffix=".tmp",
            delete=False,
        ) as handle:
            temp_path = Path(handle.name)
            json.dump(report, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
        os.replace(temp_path, destination)
        destination.chmod(0o644)
    finally:
        if temp_path is not None and temp_path.exists():
            temp_path.unlink()


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Backfill missing BTC daily prices and every authoritative Binance "
            "USD-notional OI and funding-rate values currently available."
        )
    )
    parser.add_argument(
        "--input",
        type=Path,
        default=Path("data/btc_history.csv"),
        help="Existing history CSV (default: data/btc_history.csv)",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("data/btc_history_backfilled.csv"),
        help="Backfilled CSV (default: data/btc_history_backfilled.csv)",
    )
    parser.add_argument(
        "--report",
        type=Path,
        default=Path("data/btc_history_backfill_report.json"),
        help="Coverage report JSON",
    )
    parser.add_argument("--symbol", default="BTCUSDT")
    parser.add_argument(
        "--end-date",
        type=_parse_date,
        help="Last complete UTC day; default is yesterday in UTC",
    )
    parser.add_argument("--workers", type=int, default=8)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    if not args.input.is_file():
        raise SystemExit(f"Input CSV not found: {args.input}")
    if args.workers <= 0:
        raise SystemExit("--workers must be positive")

    end_date = args.end_date or (datetime.now(timezone.utc).date() - timedelta(days=1))
    history = pd.read_csv(args.input, float_precision="round_trip")
    result = backfill_history(
        history,
        end_date=end_date,
        client=BinancePublicHistoryClient(),
        symbol=args.symbol,
        workers=args.workers,
    )

    _write_history_atomic(result.history, args.output)
    report = dict(result.report)
    # Keep the report portable when the CSV and repository are shared.
    report["input_csv"] = str(args.input)
    report["output_csv"] = str(args.output)
    _write_report_atomic(report, args.report)

    oi_report = report["open_interest"]
    assert isinstance(oi_report, dict)
    price_report = report["price"]
    assert isinstance(price_report, dict)
    funding_report = report["funding_rate"]
    assert isinstance(funding_report, dict)
    print(f"CSV: {args.output.expanduser().resolve()}")
    print(f"Report: {args.report.expanduser().resolve()}")
    print(f"Rows: {report['result']['rows']}")  # type: ignore[index]
    print(f"Price rows backfilled: {price_report['backfilled_rows']}")
    print(
        "OI rows backfilled: "
        f"{oi_report['archive_backfilled_rows']} archive + "
        f"{oi_report['recent_api_backfilled_rows']} recent API"
    )
    print(f"OI rows still missing: {oi_report['missing_rows']}")
    print(f"Funding rows backfilled: {funding_report['backfilled_rows']}")
    print(f"Funding rows still missing: {funding_report['missing_rows']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
