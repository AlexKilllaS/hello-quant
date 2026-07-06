---
name: jq-to-qmt
description: Convert JoinQuant/JQ 聚宽 Python strategies into QMT 迅投内置 Python strategy code and migration reports. Use when asked to migrate, translate, review, or implement 聚宽, JoinQuant, JQ, jq转qmt, jq-to-qmt strategies for QMT backtest or strategy trading, especially when a QMT example file using init(ContextInfo), handlebar(ContextInfo), ContextInfo APIs, or order_shares/order_target_value is provided.
---

# jq-to-qmt Skill

## Default Target

Convert only to **QMT 内置 Python 策略模型**. Default target mode is `backtest`:

```python
#coding:gbk

def init(ContextInfo):
    pass

def handlebar(ContextInfo):
    pass
```

Do not target miniQMT/xtquant. Prefer the user's local QMT example style over generic QMT snippets, but keep it within the official 内置 Python runtime constraints: Python 3.6, GBK script header in the QMT editor, no multiprocessing/threading assumptions, and no blocking loops in strategy callbacks.

Supported target modes:

- `backtest`: default. Generate a QMT backtest script using `get_market_data_ex(..., subscribe=False)` and high-level QMT backtest order helpers where possible.
- `live`: generate a QMT strategy-trading script that skips historical bars with `ContextInfo.is_last_bar()`, syncs account/position with `get_trade_detail_data()`, and routes orders through a `passorder()` adapter by default.
- `both`: generate `{original_stem}_qmt_backtest.py` and `{original_stem}_qmt_live.py`.

## Required Reading

Read these resources before converting:

- `reference/jq-api-ref.md` for common JoinQuant APIs.
- `reference/qmt-api-ref.md` for QMT built-in Python APIs and conversion patterns.
- `prompts/migration-prompt.md` for the migration workflow.

If a user provides a QMT example file, read it and mirror its conventions for account setup, data access, position state, order API, encoding header, and scheduling.

## Conversion Priorities

1. Preserve strategy logic; do not optimize factor formulas or trading rules unless asked.
2. Convert architecture to `init(ContextInfo)` and `handlebar(ContextInfo)`.
3. Convert symbols from JoinQuant suffixes to QMT suffixes:
   - `.XSHG` -> `.SH`
   - `.XSHE` -> `.SZ`
4. Use `ContextInfo` for platform APIs, callback arguments, and documented platform properties. Do not put mutable strategy state directly on `ContextInfo` unless a user-provided QMT example already does so and the report calls out the rollback risk. Prefer a module-level state object:
   ```python
   class G:
       pass

   g = G()
   ```
5. Use documented QMT built-in APIs before invented adapters:
   - historical/backtest market data: `ContextInfo.get_market_data_ex(..., subscribe=False)`
   - live snapshot data: `ContextInfo.get_full_tick()` or subscribed callbacks
   - sector/custom-board constituents: `ContextInfo.get_stock_list_in_sector()`
   - financial data: `ContextInfo.get_financial_data()` / `ContextInfo.get_raw_financial_data()`
   - trading dates: `ContextInfo.get_trading_dates()` from `after_init` or `handlebar`
   - instrument details: `ContextInfo.get_instrument_detail()`; tolerate old `get_instrumentdetail()` in existing examples
   - account/position/order/deal query: `get_trade_detail_data()`
6. For JoinQuant stock order APIs, prefer QMT high-level order helpers when the target client supports them because they preserve JoinQuant semantics:
   - `order_shares()`
   - `order_value()`
   - `order_percent()`
   - `order_target_value()`
   - `order_target_percent()`
7. Treat `passorder()` as the official comprehensive QMT order function, not as an error. Use it when the user provides a QMT example that uses it, when the target order type is not covered by the high-level helpers, or when the user asks for official-example style. When using it, preserve the official argument order and explain `opType/orderType/prType/quickTrade` assumptions.
8. Do not map JoinQuant `set_benchmark`, `set_slippage`, or `set_order_cost` to undocumented code calls by default. QMT docs put benchmark/fees largely in the UI/backtest settings; only emit local version-specific methods if they appear in a user example, and mark them as version-specific.
9. Generated code must be copy-runnable in QMT: syntactically complete, no naked JoinQuant imports/APIs, QMT lifecycle functions present, default account/stock-pool placeholders present, and missing local data handled by clear prints/empty results rather than immediate crashes.

## Scheduling Rule

For QMT backtests, `ContextInfo.run_time()` is not effective. Convert JoinQuant scheduling to `handlebar(ContextInfo)` logic:

- `run_daily(func, time=...)`: call the function from `handlebar(ContextInfo)` on each new bar, or gate by bar time when using intraday periods.
- `run_weekly(...)`: implement a helper using `ContextInfo.barpos`, `ContextInfo.get_bar_timetag()`, and `timetag_to_datetime()` to detect the target trading weekday or nth trading day.
- `run_monthly(...)`: implement a month-change/trading-day helper in `handlebar`.

For live strategy trading, `ContextInfo.run_time()` can be used with `ContextInfo.run_time("func_name", "5nSecond", "YYYY-MM-DD HH:MM:SS")`, but clearly label this as non-backtest behavior. Use `ContextInfo.is_last_bar()` to skip historical bars in live-only logic and `ContextInfo.is_new_bar()` to avoid duplicate intrabar work.

## Output

When asked to convert a strategy, create:

- `{original_stem}_qmt.py` for default `backtest` mode
- `{original_stem}_qmt_backtest.py` and `{original_stem}_qmt_live.py` for `target_mode="both"`
- `{original_stem}_qmt迁移报告.md` unless the user requests code only

The migration report must include:

- source and output files
- target mode and generated artifacts
- converted APIs
- APIs requiring manual confirmation
- financial/market field mapping
- QMT data download/setup assumptions
- state-storage assumptions (`ContextInfo` rollback vs module-level `g`)
- backtest UI assumptions for benchmark, fees, slippage, period, and main chart
- live trading assumptions for account id, account type, `passorder()` parameters, and `quickTrade`
- naked JQ API residual scan results
- validation commands and results

## Validation

Always run `python -m py_compile` on generated QMT code. If the file starts with `#coding:gbk` but contains UTF-8 Chinese text in the local workspace, compile with the actual file encoding or switch the header to `#coding:utf-8` and note the reason.

Use `lib/parser.py`, `lib/financial_mapper.py`, and `lib/validator.py` as helpers for inventory and reports, not as a substitute for reviewing the produced code. Complex strategies require hand-adjusting data adapters, scheduling, and order calls.

Before considering a conversion complete, scan generated code for blocking residuals:

- `jqdata`, `.XSHG`, `.XSHE`
- `context.portfolio`
- `get_current_data`, `get_fundamentals`
- `run_daily`, `run_weekly`, `run_monthly`
- `OrderStatus`
