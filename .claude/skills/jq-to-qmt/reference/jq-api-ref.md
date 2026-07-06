# JoinQuant 聚宽 API 参考（迁移用精简版）

官方参考入口：

- https://www.joinquant.com/help/api/help#name:api
- https://www.joinquant.com/help/api/help?name=Stock

本文档只记录迁移到 QMT 内置 Python 时最容易影响行为对齐的聚宽语义。

## 1. 策略结构和运行时间

```python
def initialize(context):
    set_benchmark("000300.XSHG")
    set_option("use_real_price", True)
    set_slippage(FixedSlippage(0.002))
    set_order_cost(OrderCost(open_commission=0.0003, close_commission=0.0003, min_commission=5), type="stock")

def before_trading_start(context):
    pass

def handle_data(context, data):
    pass

def after_trading_end(context):
    pass

run_daily(func, time="09:30")
run_weekly(func, weekday=1, time="09:30")
run_monthly(func, trading_day=1, time="09:30")
```

官方语义要点：

- 聚宽支持日、分钟、tick 频率。
- 日频 `handle_data` 在 9:30:00 运行，`data` 是上一交易日的日数据。
- 分钟频 `handle_data` 在每分钟第一秒运行，每天 240 次，不包括 11:30 和 15:00；`data` 是上一分钟数据。
- 聚宽 K 线数据为后对齐，K 线时间表示该段数据的结束时间。
- `run_xxx` 指 `run_daily/run_weekly/run_monthly`，被调函数只能有一个 `context` 参数，不再提供 `data` 参数。
- 聚宽文档建议不要在同一策略中混用 `run_xxx` 和 `handle_data`，优先使用 `run_xxx`。
- `run_daily(func, time="every_bar")` 表示按策略频率每个 bar 调用。

迁移要点：

- `initialize(context)` -> `init(ContextInfo)`
- `before_trading_start(context)` -> QMT 中可用 `after_init(C)` 预加载，或在每日第一根 bar 触发 helper。
- `handle_data(context, data)` -> `handlebar(ContextInfo)`
- `after_trading_end(context)` -> QMT 回测中用日期变化/收盘 bar helper 模拟；实盘可用定时或 `is_last_bar` 逻辑。
- `run_daily/run_weekly/run_monthly` -> QMT 回测中用 `handlebar` + 日期/bar helper 模拟，不能机械转 `run_time`。
- `g.xxx` -> 模块级 `g.xxx`；不要默认迁到 `ContextInfo.xxx`。
- 严格对齐聚宽 `data` 时，要注意聚宽给的是上一完整 bar，而 QMT `handlebar` 里取当前 bar 可能提前。报告中说明采用“当前 bar”还是“上一完整 bar”口径。

## 2. 行情数据

```python
history(count, unit="1d", field="close", security_list=None, df=True)
attribute_history(security, count, unit="1d", fields=None, skip_paused=True, df=True, fq="pre")
get_price(security, start_date=None, end_date=None, frequency="1d", fields=None, skip_paused=False, fq="pre", count=None, panel=True, fill_paused=True)
get_current_data()
```

常见字段：

- `open`, `high`, `low`, `close`
- `volume`，聚宽股票成交量单位是股
- `money`
- `high_limit`, `low_limit`
- `paused`, `is_st`, `name`, `last_price`

官方语义要点：

- 开启 `set_option("use_real_price", True)` 后，`history/attribute_history/get_price/SecurityUnitData.mavg/vwap` 等 API 返回基于当天日期的前复权价格；不同日期调用得到的前复权序列可能不同，不要跨日期缓存这些返回结果。
- `get_current_data()` 用于取当前数据对象，如停牌、ST、涨跌停、名称、最新价等。
- `get_extras("is_st", security_list, start_date, end_date, df=True)` 可取历史 ST 状态。

迁移要点：

- 历史 K 线 -> `ContextInfo.get_market_data_ex(..., subscribe=False, end_time=bar_time)`。
- 如果要严格模拟聚宽 `handle_data` 的 `data`，日频/分钟频通常应取上一完整 bar；如果取 QMT 当前 bar，报告中注明回测时点差异。
- `get_current_data()[stock].paused` -> QMT K 线字段 `suspendFlag`，实盘 tick 字段 `stockStatus`。
- `get_current_data()[stock].is_st` / `get_extras("is_st", ...)` -> `get_st_status()` / `ContextInfo.get_his_st_data()`；名称包含 ST 只作近似。
- `get_current_data()[stock].name` / `get_security_info(stock).display_name` -> `ContextInfo.get_instrument_detail(stock)["InstrumentName"]`，旧示例可用 `get_stock_name`。
- `high_limit/low_limit` -> QMT 行情字段候选 `upLimit/downLimit` 或本地字段确认；若无法确认，报告标人工确认。
- 聚宽 `fq="pre"` 通常对应 QMT `dividend_type="front"` 或 `front_ratio`，需要与用户 QMT 客户端复权口径确认。

## 3. 股票池和基础信息

```python
get_security_info(code)
get_all_securities(types=["stock"], date=None)
get_index_stocks(index_symbol, date=None)
get_industry_stocks(industry_code, date=None)
get_concept_stocks(concept_code, date=None)
get_trade_days(start_date=None, end_date=None, count=None)
```

官方语义要点：

- `get_security_info(code)` 返回对象字段：`display_name`, `name`, `start_date`, `end_date`, `type`, `parent`。
- `get_all_securities(types=["stock"], date=None)` 返回 DataFrame；`date` 可指定某日有效证券，避免幸存者偏差。
- `get_index_stocks(index_symbol, date=None)` 支持历史任意时刻指数成分股；`date=None` 时取当前日期。
- `get_industry_stocks/get_concept_stocks` 也支持 `date` 参数；行业代码如 `I64`，概念代码如 `GN036`。

迁移要点：

- `get_index_stocks("000300.XSHG")` -> `ContextInfo.get_stock_list_in_sector("沪深300")`，板块名需本地确认。
- 常见指数：`000300.XSHG` -> `沪深300`，`000905.XSHG` -> `中证500`，`000016.XSHG` -> `上证50`。
- 聚宽按日期取历史成分股，QMT 本地板块是否支持历史时间戳要用客户端数据确认；不能确认时报告中列为差异。
- `get_industry_stocks/get_concept_stocks` -> QMT 本地行业/概念板块名、自定义板块或用户数据表；不要虚构 `ContextInfo.get_industry()`。
- `get_all_securities` -> QMT 本地 `沪深A股`、`沪深300` 等板块或下载的证券列表；退市/上市日期过滤需用 `get_instrument_detail/get_open_date` 或本地数据补齐。
- `get_trade_days` -> `ContextInfo.get_trading_dates()` 或从 `barpos/get_bar_timetag` 构建交易日序列。

## 4. 财务与估值

```python
q = query(valuation.code, valuation.market_cap).filter(...).order_by(...)
df = get_fundamentals(q, date=context.previous_date)
```

常见字段：

| 表 | 字段 |
| --- | --- |
| `valuation` | `code`, `market_cap`, `circ_market_cap`, `pe_ttm`, `pb`, `ps_ttm`, `turnover_ratio` |
| `income_statement` | `net_profit`, `operating_revenue`, `operating_cost`, `total_profit` |
| `balance_statement` | `total_assets`, `total_liability`, `equity` |
| `cashflow_statement` | `net_operate_cash_flow`, `net_invest_cash_flow` |

迁移要点：

- `get_fundamentals(query(...))` 不能机械替换；需要把实际字段列出来。
- 聚宽查询常用 `date=context.previous_date` 避免未来函数。QMT 默认应使用 `report_type="announce_time"`，并用当前 bar 日期限制取数。
- 市值类字段优先用 QMT 股本接口 + 行情价格计算。
- 财报字段用 `ContextInfo.get_financial_data(fieldList, stockList, startDate, endDate, report_type=...)` 或 `get_raw_financial_data`。
- 迁移报告必须列出未确认字段、单位和数据下载要求。

## 5. 持仓、账户、下单

```python
context.portfolio.positions
context.portfolio.available_cash
context.portfolio.total_value
context.portfolio.positions[stock].amount
context.portfolio.positions[stock].closeable_amount
context.portfolio.positions[stock].avg_cost
context.portfolio.positions[stock].price

order(stock, amount)
order_value(stock, value)
order_percent(stock, percent)
order_target(stock, target)
order_target_value(stock, value)
order_target_percent(stock, percent)
```

官方语义要点：

- 聚宽回测/模拟中 `order_*` 函数成功后会创建并返回 `Order` 对象，失败返回 `None`。
- 市价单在交易时间下单会立即撮合；限价单未完全成交会挂单，未完成订单会在本交易日结束后撤销。
- 股票数量会按一手 100 股处理。
- `order_value/order_percent/order_target_value/order_target_percent` 都有资金不足、目标仓位与当前仓位差额的语义。

迁移要点：

- 聚宽股票 `order_*` 默认优先映射到 QMT 高层回测交易函数：`order_shares/order_value/order_percent/order_target_value/order_target_percent`。
- QMT 高层函数返回 `None`，不等价于聚宽 `Order`；依赖 `order.filled/order.status/order_id` 的逻辑必须改为本地状态或 `get_trade_detail_data()` 查询。
- `context.portfolio.available_cash/total_value/positions` -> QMT `get_trade_detail_data(account, "stock", "account/position")` 或用户示例中的本地状态。
- 如果本地 QMT 示例维护 `ContextInfo.holdings/money/buypoint`，沿用示例状态模型，但报告中说明官方 QMT 文档提示的 `ContextInfo` 自定义状态回滚风险。

## 6. 其他常见 API

```python
log.info(...)
log.warning(...)
record(...)
set_option(...)
set_benchmark(...)
set_slippage(...)
set_order_cost(...)
FixedSlippage(...)
OrderCost(...)
```

迁移要点：

- `log.info/warning/error` -> `print()` 或本地 logger。
- `record(...)` -> QMT `draw_text/paint` 或迁移报告中标注为绘图输出差异。
- `set_benchmark` -> 默认写入报告，提示在 QMT 回测参数/基准设置中配置；官方 QMT 文档中 `ContextInfo.benchmark` 是只读获取项。
- `set_slippage(FixedSlippage(x))` -> 默认写入报告，提示在 QMT 回测参数中设置；只有用户示例已有本地方法时才生成代码。
- `set_order_cost(OrderCost(...))` -> 默认写入报告，提示在 QMT 回测参数中设置；只有用户示例已有本地方法时才生成代码。
- `set_option("use_real_price", True)` -> QMT 行情 `dividend_type` + 客户端复权口径。
- `set_option("avoid_future_data", True)` -> 通过当前 bar 时间、上一完整 bar 口径、`subscribe=False`、`announce_time` 避免未来函数。
