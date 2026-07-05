# QMT 迅投内置 Python API 参考（迁移用精简版）

官方参考入口：

- https://dict.thinktrader.net/innerApi/start_now.html
- https://dict.thinktrader.net/innerApi/user_attention.html
- https://dict.thinktrader.net/innerApi/variable_convention.html
- https://dict.thinktrader.net/innerApi/system_function.html
- https://dict.thinktrader.net/innerApi/data_function.html
- https://dict.thinktrader.net/innerApi/trading_function.html
- https://dict.thinktrader.net/strategy/JoinQuant2QMT.html

本文档面向聚宽策略迁移。目标只包括 QMT 客户端里的内置 Python 策略模型，不包括 miniQMT/xtquant 脚本。

## 0. 转换目标模式

v1 优先覆盖 A 股股票策略：

- `backtest`: 默认模式。生成 QMT 副图回测脚本，历史行情读取必须 `subscribe=False`，下单优先使用 QMT 高层回测交易函数。
- `live`: 生成策略交易/模拟信号脚本，`handlebar(C)` 必须用 `C.is_last_bar()` 跳过历史 K 线，当前资金/持仓以 `get_trade_detail_data()` 为准，下单默认走 `passorder()` adapter。
- `both`: 同时生成回测版和实盘版。回测版用于对齐逻辑，实盘版用于连接账号和实时行情。

可复制运行的最低标准：

- 语法正确，包含 `init(C)` 和 `handlebar(C)`。
- 不残留裸 JoinQuant API。
- 账号、股票池、行情、财务数据缺失时打印提示并跳过，不因 NameError 直接退出。
- 报告列出 QMT UI 参数、数据下载、板块名、账号和 `passorder()` 参数确认项。

## 1. 运行环境和基本结构

QMT 内置 Python 文档说明客户端内置 Python 3.6。粘贴到 QMT 策略编辑器的脚本首行应使用 GBK 编码声明：

```python
#coding:gbk
import pandas as pd
import numpy as np

class G:
    pass

g = G()

def init(C):
    # init/handlebar 入参是 ContextInfo 对象，官方示例常缩写为 C。
    g.account_id = "testS"
    g.stock_list = ["000001.SZ"]
    C.capital = 1000000
    C.start = "2017-01-01 00:00:00"
    C.end = "2020-01-01 00:00:00"

def handlebar(C):
    if hasattr(C, "is_new_bar") and not C.is_new_bar():
        return
    bar_time = timetag_to_datetime(C.get_bar_timetag(C.barpos), "%Y%m%d%H%M%S")
```

常用生命周期函数：

- `init(C)`: 策略初始化。回测开始/结束时间、初始资金等只在 `init` 中设置。
- `after_init(C)`: 初始化后执行，可用于 `get_trading_dates()`、预加载财务/股票池数据等。
- `handlebar(C)`: K 线驱动主函数。回测逐根历史 K 线调用，实盘随主图 tick 推动。
- `stop(C)`: 策略停止前回调，不应在其中报单/撤单。

QMT 中所有策略在同一线程执行。不要假设多线程、多进程可用，也不要在回调中写阻塞循环。

## 2. ContextInfo 和状态保存

`ContextInfo` 是平台上下文对象。官方“使用须知”提醒，自定义变量存入 `ContextInfo` 后可能在下一次 `handlebar` 调用时回滚；在完全理解机制前，应避免用它保存可变策略状态。

迁移规则：

- `ContextInfo` 用于调用平台 API 和读取/设置官方属性。
- 聚宽 `g.xxx` 默认迁移到模块级 `g.xxx`，不要默认迁移成 `ContextInfo.xxx`。
- 如果用户提供的 QMT 示例已经使用 `ContextInfo.holdings/money/buypoint` 等本地状态，可以沿用示例，但报告必须说明回滚风险和适用模式。
- `passorder(..., quickTrade=2, ...)` 立即下单模式下，委托状态更应放在普通全局对象中，而不是 `ContextInfo` 中。

常用官方属性：

| 属性 | 用法 |
| --- | --- |
| `C.start` / `C.end` | 回测开始/结束时间，只在回测模式且 `init` 设置有效，格式 `%Y-%m-%d %H:%M:%S` |
| `C.capital` | 回测初始资金，只支持回测模式 |
| `C.period` | 当前周期，只读，如 `1d`, `1m`, `5m`, `1w`, `1mon` |
| `C.barpos` | 当前运行到的 K 线索引，只读，从 0 开始 |
| `C.time_tick_size` | 当前图 K 线数量，只读 |
| `C.stockcode` / `C.market` | 当前主图代码和市场，可拼成 `C.stockcode + "." + C.market` |
| `C.dividend_type` | 当前主图复权方式，如 `none`, `front`, `back`, `front_ratio`, `back_ratio` |
| `C.benchmark` | 获取回测基准标的，官方文档标为只读 |
| `C.do_back_test` | 是否回测模式，只读 |

## 3. 运行模式和调度

QMT 策略运行模式包括调试运行、回测、模拟信号、实盘交易。

回测模型要点：

- 回测使用本地历史数据，首次运行前需要在客户端补充行情并保持数据更新。
- 回测行情读取应使用 `get_market_data_ex(..., subscribe=False)`，避免订阅实时行情。
- 回测撮合会按当前 K 线高低点和收盘价规则模拟。
- 回测必须在副图模式执行，不要选择主图/主图叠加。
- 基准、手续费、滑点、默认周期、主图等很多参数可在 QMT 编辑器右侧面板设置；迁移报告要写明这些 UI 假设。

实盘模型要点：

- `handlebar` 在实盘会被主图 tick 推动。历史 K 线阶段可用 `C.is_last_bar()` 跳过。
- `C.is_new_bar()` 在历史 K 线每根返回 True，盘中只有新 K 线第一个 tick 返回 True，可用于防止重复计算。
- `ContextInfo.run_time()` 是定时运行机制，官方文档注明模型回测时无效。只在实盘/实时场景使用。
- 事件驱动可用 `C.subscribe_quote()` / `C.subscribe_whole_quote()`，只适合实盘行情推送模型。

聚宽调度迁移：

| JoinQuant | QMT 回测迁移方式 |
| --- | --- |
| `run_daily(func, time='9:30')` | 在 `handlebar(C)` 中按新 bar 或 bar 时间调用 |
| `run_weekly(func, weekday=1, time=...)` | 用 `barpos`、`get_bar_timetag()`、交易日 helper 判断 |
| `run_monthly(func, trading_day=...)` | 用月变更或交易日序号 helper 判断 |
| 实盘定时 | 可用 `C.run_time("func", "5nSecond", "YYYY-MM-DD HH:MM:SS")` |

## 4. 股票代码

QMT 统一代码格式为 `stockcode.market`，例如 `000001.SZ`、`600000.SH`。股票后缀迁移：

```python
def jq_to_qmt_code(code):
    return code.replace(".XSHG", ".SH").replace(".XSHE", ".SZ")
```

常见映射：

| JoinQuant | QMT |
| --- | --- |
| `000300.XSHG` | `000300.SH` |
| `000905.XSHG` | `000905.SH` |
| `000016.XSHG` | `000016.SH` |
| `399101.XSHE` | `399101.SZ` |

股票、基金、北交所等常见后缀为 `SH`、`SZ`、`BJ`。期货代码大小写敏感，不要粗暴 upper/lower。

## 5. 行情数据

优先使用 `ContextInfo.get_market_data_ex()`。官方原型：

```python
C.get_market_data_ex(
    fields=[],
    stock_code=[],
    period="follow",
    start_time="",
    end_time="",
    count=-1,
    dividend_type="follow",
    fill_data=True,
    subscribe=True,
)
```

迁移建议：

```python
def _history(C, stocks, fields, count, period="1d", end_time=None):
    if end_time is None:
        end_time = timetag_to_datetime(C.get_bar_timetag(C.barpos), "%Y%m%d%H%M%S")
    data = C.get_market_data_ex(
        fields,
        stocks,
        period=period,
        end_time=end_time,
        count=count,
        dividend_type="front",
        fill_data=True,
        subscribe=False,
    )
    return data
```

注意：

- 官方不建议在 `init` 中调用 `get_market_data_ex()`；在 `init` 中只能取到本地数据。
- 回测多个品种前必须下载对应周期的历史行情。
- 返回值通常为 `dict[str, pandas.DataFrame]`，每个标的一个 DataFrame，index 为时间，columns 为字段。
- `fields=[]` 表示取默认字段。
- K 线字段包括 `time`, `open`, `high`, `low`, `close`, `volume`, `amount`, `settle`, `openInterest`, `preClose`, `suspendFlag`。
- tick 字段停牌状态为 `stockStatus`，K 线字段停牌状态为 `suspendFlag`，1 表示停牌、0 表示不停牌。
- 旧接口 `C.get_history_data()` 和 `C.get_market_data()` 官方标注不推荐。只有用户示例已经使用时才沿用，并在报告中建议替换。
- `C.get_history_data()` 使用前需要 `C.set_universe()`，`get_market_data_ex()` 不依赖这个股票池设置。

`get_current_data()` 迁移：

- 回测中用当前 bar 的 `get_market_data_ex(..., end_time=bar_time, subscribe=False)` 近似。
- 实盘中用 `C.get_full_tick(stock_list)` 或订阅回调。
- 停牌用 `suspendFlag/stockStatus`，不要默认使用未在官方文档中确认的 `is_suspended_stock()`。

## 6. 股票池、行业和基础信息

官方成分股接口：

```python
stocks = C.get_stock_list_in_sector("沪深300")
custom = C.get_stock_list_in_sector("我的自选")
```

规则：

- `C.get_stock_list_in_sector(sectorname, realtime)` 支持客户端左侧板块列表中的任意板块，包括自定义板块。
- 常见聚宽指数成分股优先映射为 QMT 板块名：`000300.XSHG` -> `沪深300`，`000905.XSHG` -> `中证500`，`000016.XSHG` -> `上证50`。
- 若只拿到指数代码而本地板块名不明确，不要虚构 `C.get_sector("000300.SH")`；改为报告中列为本地板块名需确认。
- 官方内置 Python 文档未把 `get_industry_stocks()` 对应到 `C.get_industry()`。行业/概念股票池应优先用本地板块名、自定义板块或用户提供的数据表，并在报告中说明。
- `C.get_trading_dates(stockcode, start_date, end_date, count, period="1d")` 只能在 `after_init` 或 `handlebar` 中调用。

基础信息：

```python
detail = C.get_instrument_detail("000001.SZ")
name = detail.get("InstrumentName")
open_date = C.get_open_date("000001.SZ")
```

注意：

- `C.get_stock_name()` 官方提示后续可能废弃，优先使用 `C.get_instrument_detail("stockcode")["InstrumentName"]`。
- ST 历史状态优先使用 `get_st_status()` 或 `C.get_his_st_data()`；用证券名称包含 `ST`, `*`, `退` 只能作为近似。
- `get_open_date()` 返回上市日期数值，如 `19910403`。

## 7. 财务数据

官方财务接口有两类用法：

```python
field_list = ["CAPITALSTRUCTURE.total_capital", "ASHAREINCOME.net_profit_excl_min_int_inc"]
data = C.get_financial_data(
    field_list,
    ["000001.SZ", "600000.SH"],
    "20240101",
    "20241231",
    report_type="announce_time",
)
```

```python
value = C.get_financial_data("ASHAREBALANCESHEET", "fix_assets", "SH", "600000", "report_time", C.barpos)
```

注意：

- 默认 `report_type="announce_time"`，按公告日期取数，更适合避免未来函数。
- `report_type="report_time"` 按报告期取数，官方提示可能取到未来数据。
- 多股票、多时间、多字段时，返回可能是 Series、DataFrame 或 pandas Panel。迁移代码要写解析适配器。
- `C.get_raw_financial_data()` 返回未按交易日填充的原始财务数据。
- 市值类字段优先用行情价格和股本计算，而不是猜财务字段。

常用字段映射：

| JoinQuant 字段 | QMT 字段/做法 | 备注 |
| --- | --- | --- |
| `valuation.market_cap` | `close * C.get_total_share(stock) / 1e8` | 单位按亿元统一 |
| `valuation.circ_market_cap` | `close * C.get_last_volume(stock) / 1e8` | 单位按亿元统一 |
| `valuation.turnover_ratio` | `C.get_turnover_rate(stock_list, start, end)` | 需下载数据 |
| `balance_statement.total_liability` | `ASHAREBALANCESHEET.tot_liab` | 财务字段表 |
| `balance_statement.total_assets` | `ASHAREBALANCESHEET.tot_assets` | 财务字段表 |
| `income_statement.net_profit` | `ASHAREINCOME.net_profit_excl_min_int_inc` 或中文字段 `利润表.净利润` | 按本地字段确认 |
| `income_statement.operating_revenue` | `ASHAREINCOME.revenue` | 官方示例字段 |
| `cashflow_statement.net_operate_cash_flow` | `ASHARECASHFLOW.net_cash_flows_oper_act` | 按字段表确认 |

## 8. 交易接口

账户、持仓、委托、成交查询：

```python
accounts = get_trade_detail_data(g.account_id, "stock", "account")
positions = get_trade_detail_data(g.account_id, "stock", "position")
orders = get_trade_detail_data(g.account_id, "stock", "order")
deals = get_trade_detail_data(g.account_id, "stock", "deal")
```

常用对象属性：

- account: `m_dAvailable`, `m_dBalance`, `m_dInstrumentValue`, `m_dPositionProfit`
- position: `m_strInstrumentID`, `m_strExchangeID`, `m_nVolume`, `m_nCanUseVolume`, `m_dOpenPrice`, `m_dInstrumentValue`
- order/deal: `m_strInstrumentID`, `m_strExchangeID`, `m_nVolumeTraded`, `m_dTradedPrice`, `m_dPrice`, `m_strRemark`

高层回测交易函数（官方标注“仅回测可用”）：

```python
order_shares("000002.SZ", 100, C, g.account_id)
order_value("000002.SZ", 10000, C, g.account_id)
order_percent("000002.SZ", 0.10, C, g.account_id)
order_target_value("000002.SZ", 20000, C, g.account_id)
order_target_percent("000002.SZ", 0.10, C, g.account_id)

order_shares("000002.SZ", -200, "FIX", 37.5, C, g.account_id)
order_target_value("000002.SZ", 20000, "COMPETE", C, g.account_id)
```

规则：

- 买入数量/金额/比例为正，卖出为负。
- A 股股数会按 100 股一手取整。
- 下单价格类型常用 `LATEST`, `FIX`, `HANG`, `COMPETE`, `MARKET`, `SALE1`-`SALE5`, `BUY1`-`BUY5`。
- 高层函数返回 `None`，不等价于聚宽 `Order` 对象。遇到 `order.filled/order.status` 逻辑时，改为平台查询或本地状态机。
- 资金不足时，`order_value/order_percent/order_target_value/order_target_percent` 可能不会创建订单。

`passorder()`：

- 官方示例和聚宽迁移页经常使用 `passorder()`，它是 QMT 综合下单函数。
- 当用户示例使用 `passorder()`、需要实盘/模拟信号报单、或高层函数覆盖不了的业务时，优先保持 `passorder()` 风格。
- 使用 `passorder()` 时必须说明 `opType`、`orderType`、`prType`、`quickTrade`、账号类型和价格参数假设。
- 实盘默认逐 K 线生效；`quickTrade=2` 是立即下单，此时委托状态不要放在 `ContextInfo`。

## 9. 回测设置

```python
def init(C):
    C.capital = 1000000
    C.start = "2020-01-01 00:00:00"
    C.end = "2024-12-31 00:00:00"
```

迁移规则：

- `set_benchmark()`：QMT 文档中 `C.benchmark` 是只读获取项，默认把基准作为 UI/backtest 参数写入报告，不要默认生成 `C.benchmark = ...`。
- `set_slippage()` / `set_order_cost()`：官方内置 Python API 页未列出 `C.set_slippage()` / `C.set_commission()`，默认写入报告，提示在 QMT 回测参数面板设置。
- 如果用户本地 QMT 示例已有 `C.set_slippage()`、`C.set_commission()` 等方法，可以沿用示例，并标注为本地版本特性。
- `set_option("use_real_price", True)`：用 `get_market_data_ex()` 的 `dividend_type` 和 QMT 界面复权设置控制价格口径。
- `set_option("avoid_future_data", True)`：通过 `end_time=bar_time`、`subscribe=False`、`report_type="announce_time"` 和 bar helper 避免未来函数。

## 10. 迁移代码模板

复杂聚宽策略建议生成一层适配器，而不是把每个 API 调用机械替换：

```python
def _bar_time(C, fmt="%Y%m%d%H%M%S"):
    return timetag_to_datetime(C.get_bar_timetag(C.barpos), fmt)

def _history(C, stocks, fields, count, period="1d", dividend_type="front"):
    return C.get_market_data_ex(
        fields,
        stocks,
        period=period,
        end_time=_bar_time(C),
        count=count,
        dividend_type=dividend_type,
        fill_data=True,
        subscribe=False,
    )

def _position_dict(account_id, account_type="stock"):
    positions = get_trade_detail_data(account_id, account_type, "position")
    return {
        p.m_strInstrumentID + "." + p.m_strExchangeID: p
        for p in positions
    }

def _available_cash(account_id, account_type="stock"):
    accounts = get_trade_detail_data(account_id, account_type, "account")
    return accounts[0].m_dAvailable if accounts else 0
```

For every adapter, document any version-specific assumptions in the migration report.
