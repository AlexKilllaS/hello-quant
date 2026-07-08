# 迁移报告：聚宽策略 -> QMT 内置 Python

## 1. 基本信息

- 原始文件：`quantcode/低波动小市值.py`
- QMT 文件：`quantcode/低波动小市值_qmt.py`
- 策略类型：小市值选股 + Barra 量价因子 + 小盘趋势仓位控制 + 风险预算缓冲调仓 + 可选止损
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
- 核心数据接口采用严格口径：板块名、行情字段、涨跌停计算口径、股本字段、Barra 因子字段缺失时记录排错日志，关键路径直接抛异常。
- 工程结构已整理为初始化、QMT 适配、调度、组合、选股、交易几块；关键概念改为 `g.universe_index_code`、`g.factor_benchmark`、`g.report_benchmark`，避免股票池、因子基准和 QMT 报告基准混淆。
- 已增加 `get_instrumentdetail()` 合约详情缓存；默认关闭耗时日志，只保留过滤数量、字段缺失和 Barra 跳过原因等关键排错日志。
- 已增加副图诊断指标，使用 `ContextInfo.paint()` 输出过滤留存率、因子有效率、持仓完成度、现金比例、新目标比例、交易事件和目标仓位，并用 `draw_text()`、`draw_vertline()` 标记关键操作。
- 交易仍使用 QMT 回测高层函数 `order_shares()`，并用 `get_trade_detail_data()` 同步账户可用资金和持仓。
- 原固定交易日调仓已替换为每日 10:00 检查、条件触发的缓冲调仓，避开开盘初段波动和价差。
- 买入和调权从可用资金等额分配改为近 60 日反波动权重，并用 20 只目标持仓、单票 8% 上限控制集中度。
- 已增加小盘趋势仓位控制：`399101.SZ` 高于 60 日线时目标仓位 100%，跌破 60 日线时 50%，跌破 250 日线时 30%。

## 3. QMT 调度方式

1 分钟周期下脚本按以下时点执行：

| 时间 | 函数 | 说明 |
| --- | --- | --- |
| `09:25` | `prepare_stock_list()` | 同步持仓、昨日涨停列表和禁买列表 |
| `10:00` | `rebalance_check()` | 每日做调仓检查；触发后执行卖出、买入、降仓和目标权重校准 |
| `10:30` | `check_stop_loss()` | 盘中止损检查，默认关闭 |
| `14:00` | `check_stop_loss()` | 第二次止损检查，默认关闭 |
| `14:30` | `trade_afternoon()` | 处理涨停打开卖出和补仓 |

如果 QMT 回测周期设置为日线，脚本会降级为每天顺序执行一次主流程。这种模式适合快速检查选股逻辑，但不能复刻原聚宽日内任务时间。

当前调仓触发条件包括：

- 当前空仓或持仓数量不足。
- 目标股票仓位与当前股票仓位偏离超过阈值。
- 每月首次调仓检查做一次强制校准。
- 持仓跌出 Top `g.stock_num * g.buffer_multiplier` 缓冲池的数量达到阈值。
- 新进入 TopN 的股票数量达到 `g.rebalance_min_new_targets`。
- 已持仓股票当前权重相对目标权重偏离超过 `g.weight_drift_threshold`。

## 4. 需要人工确认

- QMT 本地板块名称是否包含 `中小综指`。若该板块取不到成分股，脚本会直接抛异常，不再降级到其他股票池。
- QMT 历史行情是否已下载，尤其是 1 分钟周期和日线 `open/high/low/close/volume/amount/preClose/suspendFlag`。
- 脚本固定使用 `front` 复权口径，并在初始化日志中打印 QMT 界面 `ContextInfo.dividend_type` 供核对。
- 涨跌停过滤不再读取未确认的 `upLimit/downLimit` K 线字段；当前策略已排除创业板、科创板、北交所和 ST 股票，沪深主板普通股涨跌停价用未复权 `preClose` 按 10% 计算。
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
| `total_value` | 未复权 `close * FloatVolume` 生成流通市值序列 | 已转换，不再依赖未确认 K 线字段 |
| `high_limit/low_limit` | `preClose` + 沪深主板普通股 10% 涨跌停价计算 | 已转换，不再依赖未确认 K 线字段 |
| `order_target_value()` | 计算目标差额后调用 `order_shares()` | 已转换 |
| `run_daily()` | `handlebar()` 内按 bar 时间触发日内任务 | 已转换 |
| 聚宽日志/回测后分析 | `ContextInfo.paint()`、`draw_text()`、`draw_vertline()` 副图诊断 | 已增加，用于定位过滤、仓位和交易事件 |

## 6. 验证结果

- `python -m py_compile quantcode/低波动小市值_qmt.py`：通过。
- 裸聚宽 API 残留扫描：未发现 `jqdata`、`.XSHG/.XSHE`、`context.portfolio`、`get_current_data`、`get_fundamentals`、`run_daily/run_weekly/run_monthly`、`OrderStatus`。
- QMT 迁移反模式扫描：未发现 `ContextInfo.get_history_data`、`ContextInfo.get_sector`、`ContextInfo.benchmark =`、`C.set_commission`、`subscribe=True`。
- 严格性扫描：未发现 `getattr(`、`hasattr(`、字段多候选表、板块默认池、Barra 排序降级、`volume * close` 成交额替代。
- 副图诊断扫描：已确认脚本包含 `ContextInfo.paint()`、`ContextInfo.draw_text()`、`ContextInfo.draw_vertline()`，用于回测时定位策略操作和数据筛选断点。

## 7. 回测建议

1. 在 QMT 中优先用 1 分钟副图回测，并确认 1 分钟和日线历史行情都已下载。
2. 若日志提示 `中小综指` 未返回成分股，先确认 QMT 左侧板块列表和本地板块数据。
3. 若日志提示“日线历史不足”，扩大回测起始日期或补齐日线数据。
4. 若选股数量很少，重点检查 `amount`、`preClose`、`close`、`TotalVolume/FloatVolume` 的本地返回和单位。
5. 与聚宽对齐时，优先比较每次触发调仓时的候选池、市值过滤结果、Top5 因子排序、目标权重和成交记录。
6. 若回测效果差但原因不清，先看副图：`D1-D4` 定位股票池和因子数据问题，`D5-D6` 定位仓位利用问题，`D7-D8` 定位换仓压力和实际交易事件，`D9` 定位小盘趋势目标仓位。
