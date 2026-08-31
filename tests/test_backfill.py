from __future__ import annotations

import io
import json
import unittest
import zipfile
from datetime import date, datetime, timezone
from urllib.parse import parse_qs, urlparse

import pandas as pd

from btc_oi_indicator.backfill import (
    BinancePublicHistoryClient,
    backfill_history,
)


class FakeHistorySource:
    def __init__(self) -> None:
        self.prices = {
            date(2026, 1, 2): (102.0, 112.0, 92.0, 107.0),
            date(2026, 1, 4): (104.0, 114.0, 94.0, 109.0),
            date(2026, 1, 5): (105.0, 115.0, 95.0, 110.0),
        }
        self.archived_oi = {
            date(2026, 1, 2): 1_020_000.0,
            date(2026, 1, 4): 1_040_000.0,
        }
        self.recent_oi = {date(2026, 1, 5): 1_050_000.0}
        self.funding = {
            date(2026, 1, day): day / 100_000.0
            for day in range(1, 6)
        }

    def fetch_spot_daily_klines(self, symbol, start_date, end_date):
        return {
            day: values
            for day, values in self.prices.items()
            if start_date <= day <= end_date
        }

    def list_archived_oi_dates(self, symbol, start_date, end_date):
        return {
            day
            for day in self.archived_oi
            if start_date <= day <= end_date
        }

    def fetch_archived_oi_value(self, symbol, target_date):
        return self.archived_oi[target_date]

    def fetch_recent_oi_values(self, symbol, start_date, end_date):
        return {
            day: value
            for day, value in self.recent_oi.items()
            if start_date <= day <= end_date
        }

    def fetch_daily_funding_rates(self, symbol, start_date, end_date):
        return {
            day: value
            for day, value in self.funding.items()
            if start_date <= day <= end_date
        }


def make_gapped_history() -> pd.DataFrame:
    return pd.DataFrame(
        {
            "timestamp": [
                "2026-01-01T00:00:00Z",
                "2026-01-03T00:00:00Z",
            ],
            "open": [101.0, 103.0],
            "high": [111.0, 113.0],
            "low": [91.0, 93.0],
            "close": [106.0, 108.0],
            "open_interest": [1_010_000.0, 1_030_000.0],
        }
    )


class BackfillTests(unittest.TestCase):
    def test_backfill_fills_internal_and_trailing_price_gaps(self) -> None:
        result = backfill_history(
            make_gapped_history(),
            end_date=date(2026, 1, 5),
            client=FakeHistorySource(),
            today=date(2026, 1, 6),
            workers=2,
        )

        self.assertEqual(len(result.history), 5)
        self.assertEqual(result.history["open_interest"].isna().sum(), 0)
        self.assertEqual(result.history["funding_rate"].isna().sum(), 0)
        self.assertEqual(result.report["price"]["backfilled_rows"], 3)
        self.assertEqual(
            result.report["open_interest"]["archive_backfilled_rows"],
            2,
        )
        self.assertEqual(
            result.report["open_interest"]["recent_api_backfilled_rows"],
            1,
        )
        self.assertEqual(result.report["open_interest"]["missing_rows"], 0)
        self.assertEqual(result.report["funding_rate"]["backfilled_rows"], 5)

    def test_unavailable_oi_is_left_empty_and_reported(self) -> None:
        source = FakeHistorySource()
        source.archived_oi = {date(2026, 1, 2): 1_020_000.0}
        source.recent_oi = {}

        result = backfill_history(
            make_gapped_history(),
            end_date=date(2026, 1, 5),
            client=source,
            today=date(2026, 1, 6),
            workers=2,
        )

        self.assertEqual(result.history["open_interest"].isna().sum(), 2)
        self.assertEqual(result.report["open_interest"]["missing_rows"], 2)
        self.assertEqual(
            result.report["open_interest"]["missing_ranges"],
            [
                {"start": "2026-01-04", "end": "2026-01-05", "days": 2}
            ],
        )
        self.assertEqual(
            result.report["open_interest"]["contiguous_through"],
            "2026-01-03",
        )


class BinanceClientParsingTests(unittest.TestCase):
    def test_archive_reader_uses_exact_2355_usd_value(self) -> None:
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, mode="w") as archive:
            archive.writestr(
                "BTCUSDT-metrics-2026-01-14.csv",
                "create_time,symbol,sum_open_interest,sum_open_interest_value\n"
                "2026-01-14 23:50:00,BTCUSDT,100,9000000\n"
                "2026-01-14 23:55:00,BTCUSDT,101,9100000\n",
            )
        client = BinancePublicHistoryClient(
            request_bytes=lambda _: buffer.getvalue()
        )

        value = client.fetch_archived_oi_value(
            "BTCUSDT",
            date(2026, 1, 14),
        )

        self.assertEqual(value, 9_100_000.0)

    def test_recent_midnight_sample_is_shifted_to_previous_day(self) -> None:
        timestamp = int(
            datetime(2026, 1, 6, tzinfo=timezone.utc).timestamp() * 1000
        )
        payload = json.dumps(
            [
                {
                    "timestamp": timestamp,
                    "sumOpenInterestValue": "12345678.9",
                }
            ]
        ).encode()
        client = BinancePublicHistoryClient(request_bytes=lambda _: payload)

        values = client.fetch_recent_oi_values(
            "BTCUSDT",
            date(2026, 1, 5),
            date(2026, 1, 5),
        )

        self.assertEqual(values, {date(2026, 1, 5): 12_345_678.9})

    def test_archive_listing_filters_zip_files_to_requested_dates(self) -> None:
        xml = b"""<?xml version="1.0" encoding="UTF-8"?>
        <ListBucketResult xmlns="http://s3.amazonaws.com/doc/2006-03-01/">
          <IsTruncated>false</IsTruncated>
          <Contents><Key>data/futures/um/daily/metrics/BTCUSDT/BTCUSDT-metrics-2026-01-14.zip</Key></Contents>
          <Contents><Key>data/futures/um/daily/metrics/BTCUSDT/BTCUSDT-metrics-2026-01-14.zip.CHECKSUM</Key></Contents>
          <Contents><Key>data/futures/um/daily/metrics/BTCUSDT/BTCUSDT-metrics-2026-01-15.zip</Key></Contents>
        </ListBucketResult>"""
        client = BinancePublicHistoryClient(request_bytes=lambda _: xml)

        values = client.list_archived_oi_dates(
            "BTCUSDT",
            date(2026, 1, 14),
            date(2026, 1, 14),
        )

        self.assertEqual(values, {date(2026, 1, 14)})

    def test_spot_kline_reader_uses_requested_utc_dates(self) -> None:
        rows = [
            [
                int(datetime(2026, 1, 14, tzinfo=timezone.utc).timestamp() * 1000),
                "100",
                "110",
                "90",
                "105",
            ]
        ]

        def request(url: str) -> bytes:
            query = parse_qs(urlparse(url).query)
            self.assertEqual(query["symbol"], ["BTCUSDT"])
            return json.dumps(rows).encode()

        client = BinancePublicHistoryClient(request_bytes=request)
        values = client.fetch_spot_daily_klines(
            "BTCUSDT",
            date(2026, 1, 14),
            date(2026, 1, 14),
        )

        self.assertEqual(values[date(2026, 1, 14)], (100.0, 110.0, 90.0, 105.0))

    def test_funding_reader_uses_next_days_first_regular_event(self) -> None:
        rows = [
            {
                "fundingTime": int(
                    datetime(2026, 1, 6, tzinfo=timezone.utc).timestamp() * 1000
                ),
                "fundingRate": "-0.0001",
                "rateType": "Regular",
            },
            {
                "fundingTime": int(
                    datetime(2026, 1, 6, 8, tzinfo=timezone.utc).timestamp()
                    * 1000
                ),
                "fundingRate": "0.0002",
                "rateType": "Regular",
            },
        ]
        client = BinancePublicHistoryClient(
            request_bytes=lambda _: json.dumps(rows).encode()
        )

        values = client.fetch_daily_funding_rates(
            "BTCUSDT",
            date(2026, 1, 5),
            date(2026, 1, 5),
        )

        self.assertEqual(values, {date(2026, 1, 5): -0.0001})


if __name__ == "__main__":
    unittest.main()
