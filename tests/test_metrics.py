from __future__ import annotations

import json
import tempfile
import unittest

import numpy as np
import pandas as pd

from btc_oi_indicator import (
    OiMetricSettings,
    calculate_oi_indicators,
    export_artifacts,
    prepare_history,
)
from btc_oi_indicator.charts import (
    create_anchored_oi_divergence_chart,
    create_rolling_oi_funding_chart,
)


def make_history(rows: int = 180) -> pd.DataFrame:
    index = np.arange(rows, dtype=float)
    close = 100.0 + index * 0.35 + np.sin(index / 5.0) * 2.0
    open_price = close * (1.0 + np.sin(index) * 0.002)
    return pd.DataFrame(
        {
            "stat_date": pd.date_range(
                "2024-01-01", periods=rows, freq="D", tz="UTC"
            ),
            "open": open_price,
            "high": np.maximum(open_price, close) * 1.01,
            "low": np.minimum(open_price, close) * 0.99,
            "close": close,
            "OI": 1_000.0 + index * 12.0 + np.cos(index / 7.0) * 20.0,
            "funding_rate": np.sin(index / 11.0) * 0.0001,
        }
    )


class DataContractTests(unittest.TestCase):
    def test_aliases_are_normalized_and_rows_are_sorted(self) -> None:
        result = prepare_history(make_history(4).iloc[::-1])
        self.assertTrue(result["timestamp"].is_monotonic_increasing)
        self.assertIn("open_interest", result.columns)

    def test_invalid_or_missing_oi_is_rejected_unless_explicitly_allowed(self) -> None:
        history = make_history(4)
        history.loc[2, "OI"] = np.nan
        with self.assertRaisesRegex(ValueError, "required numeric values"):
            prepare_history(history)
        result = prepare_history(history, allow_missing_open_interest=True)
        self.assertEqual(result["open_interest"].isna().sum(), 1)


class OiMetricTests(unittest.TestCase):
    def test_anchored_divergence_matches_formula(self) -> None:
        history = make_history()
        result = calculate_oi_indicators(history)
        baseline = history.iloc[0]
        target = history.iloc[100]
        expected = (
            float(target["OI"]) / float(baseline["OI"])
            - float(target["close"]) / float(baseline["close"])
        )
        self.assertAlmostEqual(
            float(result.loc[100, "anchored_oi_price_divergence"]), expected
        )

    def test_rolling_divergence_and_funding_sum_match_formulas(self) -> None:
        history = make_history()
        result = calculate_oi_indicators(history)
        index = 100
        window = history.iloc[index - 59 : index + 1]
        expected_divergence = (
            float(history.loc[index, "OI"]) / float(window["OI"].mean())
            - float(history.loc[index, "close"]) / float(window["close"].mean())
        )
        expected_funding = float(history.loc[index - 6 : index, "funding_rate"].sum())
        self.assertAlmostEqual(
            float(result.loc[index, "rolling_oi_price_divergence"]),
            expected_divergence,
        )
        self.assertAlmostEqual(
            float(result.loc[index, "funding_rate_7d_sum"]), expected_funding
        )

    def test_missing_funding_is_rejected_but_trailing_gap_is_allowed(self) -> None:
        with self.assertRaisesRegex(ValueError, "btc-oi-backfill"):
            calculate_oi_indicators(make_history().drop(columns="funding_rate"))

        trailing = make_history()
        trailing.loc[179, "funding_rate"] = None
        result = calculate_oi_indicators(trailing)
        self.assertTrue(pd.isna(result.loc[179, "funding_rate_7d_sum"]))

    def test_interior_missing_funding_is_rejected(self) -> None:
        history = make_history()
        history.loc[100, "funding_rate"] = None
        with self.assertRaisesRegex(ValueError, "historical rows"):
            calculate_oi_indicators(history)


class ChartAndExportTests(unittest.TestCase):
    def test_both_named_chart_structures_are_created(self) -> None:
        result = calculate_oi_indicators(make_history())
        settings = OiMetricSettings(chart_start_timestamp="2024-01-01T00:00:00Z")
        anchored = create_anchored_oi_divergence_chart(result, settings=settings)
        rolling = create_rolling_oi_funding_chart(result, settings=settings)
        self.assertIn("Anchored OI / Price Divergence", anchored.layout.title.text)
        self.assertIn("Rolling OI / Price Divergence", rolling.layout.title.text)
        self.assertTrue(anchored.layout.autosize)
        self.assertTrue(rolling.layout.autosize)

    def test_export_contains_only_two_charts_and_one_metrics_csv(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            paths = export_artifacts(make_history(), output_dir=directory)
            self.assertEqual(
                set(paths),
                {"data", "manifest", "anchored_oi_html", "rolling_oi_funding_html"},
            )
            self.assertTrue(paths["data"].name == "btc_oi_metrics.csv")
            manifest = json.loads(paths["manifest"].read_text(encoding="utf-8"))
            self.assertEqual(manifest["version"], "2.0.0")
            self.assertNotIn("btc_oi_indicator_chart.html", json.dumps(manifest))
            self.assertIn("anchored_oi_price_divergence", manifest["metrics"])


if __name__ == "__main__":
    unittest.main()
