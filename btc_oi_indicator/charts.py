from __future__ import annotations

from typing import Any, Iterable

import pandas as pd

from .metrics import OiMetricSettings, select_chart_window


def _add_event_lines(
    figure: Any,
    frame: pd.DataFrame,
    *,
    signal_column: str,
    color: str,
) -> None:
    from plotly import graph_objects as go

    events = frame[frame[signal_column] == 1]
    if events.empty:
        return

    upper = float(frame["high"].max()) * 1.10
    x_values: list[Any] = []
    y_values: list[float | None] = []
    for row in events.itertuples(index=False):
        x_values.extend((row.timestamp, row.timestamp, None))
        y_values.extend((float(row.high), upper, None))

    figure.add_trace(
        go.Scatter(
            x=x_values,
            y=y_values,
            mode="lines",
            line={"color": color, "width": 1, "dash": "dot"},
            hoverinfo="skip",
            showlegend=False,
        ),
        row=1,
        col=1,
    )


def _candlestick(frame: pd.DataFrame, symbol: str) -> Any:
    from plotly import graph_objects as go

    return go.Candlestick(
        x=frame["timestamp"],
        open=frame["open"],
        high=frame["high"],
        low=frame["low"],
        close=frame["close"],
        increasing_line_color="#237700",
        decreasing_line_color="#DA2838",
        name=symbol,
        showlegend=False,
    )


def _add_line_panel(
    figure: Any,
    frame: pd.DataFrame,
    *,
    row: int,
    column: str,
    label: str,
    color: str,
    boundaries: Iterable[str],
) -> None:
    from plotly import graph_objects as go

    figure.add_trace(
        go.Scatter(
            x=frame["timestamp"],
            y=frame[column],
            mode="lines",
            line={"color": color, "width": 1.5},
            name=label,
        ),
        row=row,
        col=1,
    )
    for boundary in boundaries:
        figure.add_trace(
            go.Scatter(
                x=frame["timestamp"],
                y=frame[boundary],
                mode="lines",
                line={"color": "#222222", "width": 1},
                hoverinfo="skip",
                showlegend=False,
            ),
            row=row,
            col=1,
        )


def _apply_common_layout(
    figure: Any,
    *,
    title: str,
    height: int,
) -> None:
    figure.update_yaxes(type="log", row=1, col=1)
    figure.update_xaxes(rangeslider_visible=False)
    figure.update_layout(
        title={"text": title, "x": 0.5},
        template="plotly",
        autosize=True,
        height=height,
        font={"size": 14},
        margin={"l": 70, "r": 250, "t": 90, "b": 60},
        legend={"x": 1.01, "y": 1.0, "xanchor": "left"},
        hovermode="x unified",
        xaxis_rangeslider_visible=False,
    )


def create_anchored_oi_divergence_chart(
    frame: pd.DataFrame,
    *,
    symbol: str = "BTCUSDT",
    settings: OiMetricSettings | None = None,
) -> Any:
    """Chart OI/price divergence measured from the first complete observation."""

    try:
        from plotly.subplots import make_subplots
    except ImportError as exc:  # pragma: no cover - runtime dependency
        raise RuntimeError("Plotly is required. Install with: pip install -e .") from exc

    settings = settings or OiMetricSettings()
    chart_frame = select_chart_window(frame, settings)
    figure = make_subplots(
        rows=2,
        cols=1,
        shared_xaxes=True,
        vertical_spacing=0.012,
        row_heights=[0.47, 0.53],
    )
    figure.add_trace(_candlestick(chart_frame, symbol), row=1, col=1)
    _add_line_panel(
        figure,
        chart_frame,
        row=2,
        column="anchored_oi_price_divergence",
        label="Anchored OI / Price Divergence",
        color="#FF0500",
        boundaries=(
            "anchored_oi_price_divergence_upper_bound",
            "anchored_oi_price_divergence_lower_bound",
        ),
    )
    _add_event_lines(
        figure,
        chart_frame,
        signal_column="anchored_oi_price_divergence_high_signal",
        color="#FF003A",
    )
    _add_event_lines(
        figure,
        chart_frame,
        signal_column="anchored_oi_price_divergence_low_signal",
        color="#007740",
    )

    latest = chart_frame.iloc[-1]
    latest_time = pd.Timestamp(latest["timestamp"]).strftime("%Y-%m-%d %H:%M:%S")
    figure.add_annotation(
        x=1.02,
        y=0.80,
        xref="paper",
        yref="paper",
        text=(
            "Anchored OI / Price Divergence "
            f"{float(latest['anchored_oi_price_divergence']):.3f}"
        ),
        showarrow=False,
        align="left",
        xanchor="left",
        font={"size": 14, "color": "#FF0016"},
    )
    _apply_common_layout(
        figure,
        title=f"BTC Anchored OI / Price Divergence ({latest_time})",
        height=1400,
    )
    return figure


def create_rolling_oi_funding_chart(
    frame: pd.DataFrame,
    *,
    symbol: str = "BTCUSDT",
    settings: OiMetricSettings | None = None,
) -> Any:
    """Chart rolling OI/price divergence alongside the funding-rate sum."""

    try:
        from plotly.subplots import make_subplots
    except ImportError as exc:  # pragma: no cover - runtime dependency
        raise RuntimeError("Plotly is required. Install with: pip install -e .") from exc

    settings = settings or OiMetricSettings()
    chart_frame = select_chart_window(frame, settings)
    figure = make_subplots(
        rows=3,
        cols=1,
        shared_xaxes=True,
        vertical_spacing=0.012,
        row_heights=[0.32, 0.34, 0.34],
    )
    figure.add_trace(_candlestick(chart_frame, symbol), row=1, col=1)
    _add_line_panel(
        figure,
        chart_frame,
        row=2,
        column="rolling_oi_price_divergence",
        label="Rolling OI / Price Divergence",
        color="#001DFF",
        boundaries=(
            "rolling_oi_price_divergence_upper_bound",
            "rolling_oi_price_divergence_lower_bound",
        ),
    )
    _add_line_panel(
        figure,
        chart_frame,
        row=3,
        column="funding_rate_7d_sum",
        label="Funding Rate 7D Sum",
        color="#FF7200",
        boundaries=(
            "funding_rate_7d_sum_upper_bound",
            "funding_rate_7d_sum_lower_bound",
        ),
    )
    for signal_column in (
        "rolling_oi_price_divergence_high_signal",
        "rolling_oi_price_divergence_low_signal",
    ):
        _add_event_lines(
            figure,
            chart_frame,
            signal_column=signal_column,
            color="#8C00FF",
        )

    latest_time = pd.Timestamp(chart_frame.iloc[-1]["timestamp"]).strftime(
        "%Y-%m-%d %H:%M:%S"
    )
    _apply_common_layout(
        figure,
        title=f"BTC Rolling OI / Price Divergence + Funding ({latest_time})",
        height=1800,
    )
    return figure
