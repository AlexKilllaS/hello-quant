# 迁移报告：聚宽策略 -> QMT 内置 Python

## 1. 基本信息

- 原始文件：`quantcode/低波动小市值.py`
- QMT 文件：`quantcode/低波动小市值_qmt.py`
- 原始代码行数：749
- 转换后代码行数：1278
- 策略类型：小市值选股 + Barra 量价因子 + 周频调仓
- 预估转换率：约 80%

## 2. 已转换内容

- `initialize(context)` 已转换为 `init(ContextInfo)`。
- 聚宽 `g.xxx` 已改为模块级 `g.xxx` 状态对象，避免自定义状态放在 `ContextInfo` 上的回滚风险。
- 聚宽证券后缀 `.XSHG/.XSHE` 已改为 QMT `.SH/.SZ`。
- 聚宽 `run_daily()` 调度已改为 `handlebar(ContextInfo)` 中的新 bar + 周内第 2 个交易日判断。
- `get_index_stocks('399101.XSHE')` 已改为 `ContextInfo.get_stock_list_in_sector()`，默认尝试本地板块名 `中小综指`、`中小板综`、`中小企业综合指数`。
- `get_price()` / `history()` 已封装为 `_history()`，底层使用 `ContextInfo.get_market_data_ex(..., subscribe=False, end_time=当前bar时间)`。
- `valuation.market_cap` 已改为 `close * ContextInfo.get_total_share()` 计算总市值，单位统一为亿元。
- 流通市值/换手率分母优先使用 `close * ContextInfo.get_last_volume()`，无法获取时回退到总市值。
- 停牌过滤优先使用 K 线字段 `suspendFlag`。
- 证券名称优先使用 `ContextInfo.get_instrument_detail()["InstrumentName"]`，兼容旧接口 `get_stock_name()`。
- 交易使用 QMT 回测高层函数 `order_shares()`，并增加 `get_trade_detail_data()` 的可选资金/持仓同步。
- HSIGMA、DASTD、CMRA、STOM、STOQ、STOA、ATVR 因子计算逻辑已保留。

## 3. 需要人工确认

- QMT 本地板块名称是否包含 `中小综指`。如果取不到成分股，请在 `INDEX_SECTOR_NAME_MAP` 中改成客户端左侧板块的真实名称。
- QMT 历史行情是否已下载，尤其是日线 `close/volume/amount/suspendFlag/upLimit/downLimit`。
- `upLimit/downLimit` 字段名不同版本可能有差异，代码已提供候选字段但仍需本地回测验证。
- `ContextInfo.get_total_share()`、`ContextInfo.get_last_volume()` 的单位需用本地数据核对；代码对小数值做了“万股 -> 股”的启发式处理。
- 原聚宽 `set_benchmark()`、`set_slippage()`、`set_order_cost()` 默认作为 QMT 回测 UI 参数处理，没有生成未确认的 `ContextInfo.set_slippage/set_commission` 调用。
- 代码文件当前为 UTF-8 编码头，便于本地 Python 语法检查；若复制到 QMT 编辑器后编码异常，可按 QMT 编辑器要求另存为 GBK。
- QMT 高层回测交易函数不返回聚宽 `Order` 对象；本脚本的本地持仓状态仍是近似状态，已尽量用 `get_trade_detail_data()` 同步。

## 4. 字段/API 映射

| 聚宽字段/API | QMT 转换 | 状态 |
| --- | --- | --- |
| `get_index_stocks('399101.XSHE')` | `ContextInfo.get_stock_list_in_sector('中小综指')` 等本地板块名 | 需确认板块名 |
| `get_price/history close` | `ContextInfo.get_market_data_ex(['close'], ..., subscribe=False)` | 已转换 |
| `get_price money` | `amount/money/turnover` 候选字段 | 需本地验证 |
| `high_limit/low_limit` | `upLimit/downLimit` 等候选字段 | 需本地验证 |
| `valuation.market_cap` | `close * get_total_share() / 1e8` | 已转换 |
| `valuation.circ_market_cap` / 流通市值 | `close * get_last_volume() / 1e8` | 已转换 |
| `get_current_data().paused` | `suspendFlag` | 已转换 |
| `get_security_info().display_name` | `get_instrument_detail()["InstrumentName"]` | 已转换 |
| `order_target_value()` | 计算目标差额后调用 `order_shares()` | 已转换 |

## 5. 验证结果

- `python -m py_compile quantcode/低波动小市值_qmt.py`：通过。
- `jq-to-qmt` validator：通过，原聚宽 API 覆盖检查 `uncovered=[]`。
- 旧接口残留扫描：未发现 `jqdata`、`.XSHG/.XSHE`、`context.portfolio`、`get_fundamentals`、`run_daily/run_weekly/run_monthly`、`ContextInfo.get_history_data`、`ContextInfo.get_sector`。

## 6. 回测建议

1. 在 QMT 中先用日线副图回测，并确认历史行情、财务/股本数据已下载。
2. 若日志提示板块为空，先修改 `INDEX_SECTOR_NAME_MAP["399101.SZ"]`。
3. 若选股数量很少，重点检查 `amount`、`upLimit/downLimit`、`get_total_share/get_last_volume` 的本地返回。
4. 与聚宽对齐时，优先比较每次调仓日的候选池、市值过滤结果、Top5 因子排序和成交记录。
