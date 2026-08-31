# BTC OI Indicator

一个轻量的 BTC OI（Open Interest）指标项目：读取每日 CSV，计算两套 OI/价格指标，并生成两张交互式 HTML 图。

## 项目结构

```text
btc_oi_indicator/
  data.py       输入 CSV 标准化与校验
  metrics.py    两套 OI 指标计算
  charts.py     两张 Plotly 图
  export.py     CSV、HTML 和运行信息导出
  cli.py        命令行入口
  backfill.py   公开数据回补工具
tests/           自动化测试
data/            输入和回补后的 CSV
```

## 能做什么

### 1. 计算指标

```text
anchored_oi_price_divergence
  = OI / 首个有效 OI - close / 首个有效 close

rolling_oi_price_divergence
  = OI / 60 日 OI 均值 - close / 60 日 close 均值

funding_rate_7d_sum
  = Funding Rate 7 日累计
```

### 2. 生成图表

- 锚定 OI / Price Divergence 图；
- 滚动 OI / Price Divergence + Funding 图。

### 3. 回补历史数据

当输入 CSV 有缺失时，`btc-oi-backfill` 会尝试补齐价格、OI 和 Funding，并输出覆盖报告。

## 数据源

回补使用 Binance 提供的公开数据：

- Binance Spot 日线 K 线：价格；
- Binance Futures Metrics：历史 OI；
- Binance USD-M Futures API：最近 OI 和 Funding Rate。

项目不需要 API Key，也不连接数据库、服务器或私有系统。输入 CSV 可以由使用者自行准备，也可以使用已有的公开历史数据文件。

## 使用

首次安装：

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -e .
```

生成指标和图表：

```bash
python -m btc_oi_indicator.cli \
  --input data/btc_history_backfilled.csv \
  --output-dir output
```

回补数据：

```bash
btc-oi-backfill \
  --input data/btc_history.csv \
  --output data/btc_history_backfilled.csv \
  --report data/btc_history_backfill_report.json
```

## 输出文件

```text
btc_oi_metrics.csv
btc_anchored_oi_divergence_chart.html
btc_rolling_oi_funding_chart.html
plotly.min.js
run_manifest.json
```

HTML 依赖同目录的 `plotly.min.js`，分享时需要一起保留。

## 测试

```bash
python -m unittest discover -s tests -v
```
