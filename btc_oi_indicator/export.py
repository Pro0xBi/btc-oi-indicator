from __future__ import annotations

import json
from pathlib import Path

import pandas as pd

from .metrics import OiMetricSettings, calculate_oi_indicators
from .charts import (
    create_anchored_oi_divergence_chart,
    create_rolling_oi_funding_chart,
)


def _write_html(figure: object, path: Path) -> None:
    figure.write_html(  # type: ignore[attr-defined]
        path,
        include_plotlyjs="directory",
        full_html=True,
        default_width="100%",
        config={"responsive": True, "displaylogo": False},
    )


def export_artifacts(
    history: pd.DataFrame,
    *,
    output_dir: str | Path,
    symbol: str = "BTCUSDT",
    settings: OiMetricSettings | None = None,
    write_chart: bool = True,
    allow_missing_open_interest: bool = False,
) -> dict[str, Path]:
    """Export only the two OI-focused metrics and their HTML charts."""

    settings = settings or OiMetricSettings()
    result = calculate_oi_indicators(
        history,
        settings=settings,
        allow_missing_open_interest=allow_missing_open_interest,
    )
    target = Path(output_dir).expanduser().resolve()
    target.mkdir(parents=True, exist_ok=True)

    data_path = target / "btc_oi_metrics.csv"
    result.to_csv(data_path, index=False, float_format="%.12g")

    manifest_path = target / "run_manifest.json"
    manifest = {
        "project": "btc-oi-indicator",
        "version": "2.0.0",
        "symbol": symbol,
        "metrics": {
            "anchored_oi_price_divergence": (
                "open_interest / first_valid_open_interest "
                "- close / first_valid_close"
            ),
            "rolling_oi_price_divergence": (
                "open_interest / mean_60d(open_interest) "
                "- close / mean_60d(close)"
            ),
            "funding_rate_7d_sum": "rolling_sum_7d(funding_rate)",
        },
        "rows": len(result),
        "start_timestamp": result["timestamp"].min().isoformat(),
        "end_timestamp": result["timestamp"].max().isoformat(),
        "resolved_baseline": result.attrs["baseline"],
        "input_coverage": result.attrs["input_coverage"],
        "settings": settings.to_dict(),
        "outputs": {
            "metrics_csv": data_path.name,
            "anchored_oi_divergence_chart_html": (
                "btc_anchored_oi_divergence_chart.html" if write_chart else None
            ),
            "rolling_oi_funding_chart_html": (
                "btc_rolling_oi_funding_chart.html" if write_chart else None
            ),
            "plotly_js": "plotly.min.js" if write_chart else None,
        },
    }
    manifest_path.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )

    paths = {"data": data_path, "manifest": manifest_path}
    if not write_chart:
        return paths

    anchored_path = target / "btc_anchored_oi_divergence_chart.html"
    _write_html(
        create_anchored_oi_divergence_chart(
            result,
            symbol=symbol,
            settings=settings,
        ),
        anchored_path,
    )
    paths["anchored_oi_html"] = anchored_path

    rolling_path = target / "btc_rolling_oi_funding_chart.html"
    _write_html(
        create_rolling_oi_funding_chart(
            result,
            symbol=symbol,
            settings=settings,
        ),
        rolling_path,
    )
    paths["rolling_oi_funding_html"] = rolling_path

    return paths
