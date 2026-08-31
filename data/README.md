# Data

将输入历史数据保存为 `btc_history.csv`，或直接传入其他 CSV 路径。

指标计算需要每日 UTC 的 OHLC、OI 和 Funding Rate。历史 Funding Rate 需要完整，最新一天可以暂缺。

准备好 `btc_history.csv` 后，数据停止更新时回补：

```bash
btc-oi-backfill \
  --input data/btc_history.csv \
  --output data/btc_history_backfilled.csv \
  --report data/btc_history_backfill_report.json
```
