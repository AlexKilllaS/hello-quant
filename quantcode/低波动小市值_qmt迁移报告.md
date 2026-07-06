# 迁移报告：聚宽策略 -> QMT 内置 Python

## 1. 基本信息

- 原始文件：`quantcode/低波动小市值.py`
- QMT 文件：`quantcode/低波动小市值_qmt.py`
- 策略类型：小市值选股 + Barra 量价因子 + 风险预算缓冲调仓 + 可选止损
- 目标模式：QMT 客户端内置 Python 回测脚本
- 推荐回测周期：1 分钟周期，用 `handlebar(ContextInfo)` 按 bar 时间模拟聚宽日内任务

## 2. 已转换内容

- `initialize(context)` 已转换为 `init(ContextInfo)`，日内任务调度已转换为 `handlebar(ContextInfo)` 内的时间门控。
- 聚宽 `g.xxx` 已迁移为模块级 `g.xxx`，避免把可变策略状态写入 `ContextInfo`。
- 聚宽证券后缀 `.XSHG/.XSHE` 已转换为 QMT `.SH/.SZ`。
- 股票池使用 `ContextInfo.get_stock_list_in_sector('中小综指')`，`399101.SZ` 只映射到这个已验证可用的 QMT 本地板块名。
- 历史行情封装为 `_history()`，底层使用 `ContextInfo.get_market_data_ex(..., subscribe=False)`。
- 选股和因子计算统一使用日线数据，并在调仓前检查至少 `g.vol_window + 10` 根日线历史。
- 市值和流通市值通过 `ContextInfo.get_instrumentdetail(stock)["TotalVolume"/"FloatVolume"] * close / 1e8` 计算，不再改用行情候选市值字段。
- 停牌过滤使用 K 线字段 `suspendFlag`，证券名称使用 `get_instrumentdetail()["InstrumentName"]`。
- 核心数据接口采用严格口径：板块名、行情字段、涨跌停字段、股本字段、Barra 因子字段缺失时记录排错日志，关键路径直接抛异常。
- 交易仍使用 QMT 回测高层函数 `order_shares()`，并用 `get_trade_detail_data()` 同步账户可用资金和持仓。
- 原固定交易日调仓已替换为每日 09:31 检查、条件触发的缓冲调仓。
- 买入和调权从可用资金等额分配改为近 60 日反波动权重，并用单票 15% 上限控制集中度。

## 3. QMT 调度方式

1 分钟周期下脚本按以下时点执行：

| 时间 | 函数 | 说明 |
| --- | --- | --- |
| `09:25` | `prepare_stock_list()` | 同步持仓、昨日涨停列表和禁买列表 |
| `09:31` | `weekly_adjustment()` | 每日做调仓检查；触发后执行卖出、买入和目标权重校准 |
| `10:30` | `check_stop_loss()` | 盘中止损检查，默认关闭 |
| `14:00` | `check_stop_loss()` | 第二次止损检查，默认关闭 |
| `14:30` | `trade_afternoon()` | 处理涨停打开卖出和补仓 |

如果 QMT 回测周期设置为日线，脚本会降级为每天顺序执行一次主流程。这种模式适合快速检查选股逻辑，但不能复刻原聚宽日内任务时间。

当前调仓触发条件包括：

- 当前空仓或持仓数量不足。
- 每月首次调仓检查做一次强制校准。
- 持仓跌出 Top `g.stock_num * g.buffer_multiplier` 缓冲池的数量达到阈值。
- 新进入 TopN 的股票数量达到 `g.rebalance_min_new_targets`。
- 已持仓股票当前权重相对目标权重偏离超过 `g.weight_drift_threshold`。

## 4. 需要人工确认

- QMT 本地板块名称是否包含 `中小综指`。若该板块取不到成分股，脚本会直接抛异常，不再降级到其他股票池。
- QMT 历史行情是否已下载，尤其是 1 分钟周期和日线 `open/high/low/close/volume/amount/suspendFlag/upLimit/downLimit`。
- 脚本固定使用 `front` 复权口径，并在初始化日志中打印 QMT 界面 `ContextInfo.dividend_type` 供核对。
- `upLimit/downLimit` 字段名需用本地回测验证；代码不再尝试候选字段替换。
- `TotalVolume` 和 `FloatVolume` 的单位需用本地数据核对；代码要求其为股数，异常小的数值会被视为无效。
- 原聚宽 `set_benchmark()`、`set_slippage()`、`set_order_cost()` 仍作为 QMT 回测 UI 参数处理，没有生成未确认的 `ContextInfo.set_slippage/set_commission` 调用。
- 代码文件当前为 UTF-8 编码头，便于本地 Python 语法检查；若复制到 QMT 编辑器后编码异常，可按 QMT 编辑器要求另存为 GBK。
- 当前文件是回测版。若要实盘/模拟信号版，应单独改为 `is_last_bar()` + `passorder()` 适配器，不建议混在这份回测脚本里。

## 5. 字段/API 映射

| 聚宽字段/API | QMT 转换 | 状态 |
| --- | --- | --- |
| `get_index_stocks('399101.XSHE')` | `ContextInfo.get_stock_list_in_sector('中小综指')` | 已转换 |
| `history/get_price` | `ContextInfo.get_market_data_ex(..., subscribe=False)` | 已转换 |
| `get_current_data().paused` | `suspendFlag` | 已转换 |
| `get_current_data().name` | `get_instrumentdetail()["InstrumentName"]` | 已转换 |
| `valuation.market_cap` | `close * TotalVolume / 1e8` | 已转换 |
| `valuation.circ_market_cap` | `close * FloatVolume / 1e8` | 已转换 |
| `high_limit/low_limit` | `upLimit/downLimit` | 已转换，需本地验证字段可用 |
| `order_target_value()` | 计算目标差额后调用 `order_shares()` | 已转换 |
| `run_daily()` | `handlebar()` 内按 bar 时间触发日内任务 | 已转换 |

## 6. 验证结果

- `python -m py_compile quantcode/低波动小市值_qmt.py`：通过。
- 裸聚宽 API 残留扫描：未发现 `jqdata`、`.XSHG/.XSHE`、`context.portfolio`、`get_current_data`、`get_fundamentals`、`run_daily/run_weekly/run_monthly`、`OrderStatus`。
- QMT 迁移反模式扫描：未发现 `ContextInfo.get_history_data`、`ContextInfo.get_sector`、`ContextInfo.benchmark =`、`C.set_commission`、`subscribe=True`。
- 严格性扫描：未发现 `getattr(`、`hasattr(`、字段多候选表、板块默认池、Barra 排序降级、`volume * close` 成交额替代。

## 7. 回测建议

1. 在 QMT 中优先用 1 分钟副图回测，并确认 1 分钟和日线历史行情都已下载。
2. 若日志提示 `中小综指` 未返回成分股，先确认 QMT 左侧板块列表和本地板块数据。
3. 若日志提示“日线历史不足”，扩大回测起始日期或补齐日线数据。
4. 若选股数量很少，重点检查 `amount`、`upLimit/downLimit`、`TotalVolume/FloatVolume` 的本地返回和单位。
5. 与聚宽对齐时，优先比较每次触发调仓时的候选池、市值过滤结果、Top5 因子排序、目标权重和成交记录。
