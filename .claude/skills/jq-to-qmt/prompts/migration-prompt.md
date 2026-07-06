# 迁移执行流程

## 输入

- `strategy_file`: 聚宽策略 `.py` 文件路径。
- 可选：`qmt_example_file`: 用户给出的 QMT 示例文件。
- 可选：`output_mode`: `code_only` / `report_only` / `both`，默认 `both`。
- 可选：`target_mode`: `backtest` / `live` / `both`，默认 `backtest`。

## Step 1: 读取上下文

1. 读取聚宽策略文件。
2. 若用户给出 QMT 示例，读取示例并记录：
   - 编码头，如 `#coding:gbk`
   - 函数签名：`init(ContextInfo)` / `handlebar(ContextInfo)` / `after_init(ContextInfo)`
   - 状态保存：模块级 `g/A` 对象，还是 `ContextInfo.xxx`
   - 行情接口：`get_market_data_ex`、`get_history_data`、`get_full_tick` 或订阅回调
   - 下单接口：高层 `order_*` 函数或 `passorder`
   - 账号和账号类型：`account`、`accountType`、`account_id/accountid`
   - 持仓/资金口径：`get_trade_detail_data` 还是本地手工状态
   - 调仓触发方式：`is_new_bar`、`is_last_bar`、`barpos % n`、交易日 helper
   - 回测参数是否通过 QMT UI 设置：基准、手续费、滑点、周期、主图

## Step 2: 解析聚宽策略

使用 `lib/parser.py` 或 AST 手工检查：

- 初始化参数、全局状态、`g.xxx`
- `handle_data` 和所有调度函数
- `run_daily/run_weekly/run_monthly`
- 行情 API：`history/get_price/attribute_history/get_current_data`
- 股票池 API：`get_index_stocks/get_industry_stocks/get_all_securities`
- 财务 API：`query/get_fundamentals`
- 交易 API：`order/order_value/order_target_value/order_target_percent`
- 平台设置：`set_benchmark/set_slippage/set_order_cost/set_option`
- 聚宽订单对象状态判断
- 是否存在未来函数风险：未按当前 bar 时间限制行情/财务数据
- 是否依赖聚宽 `handle_data` 中上一完整 bar 的 `data` 语义
- 是否跨日期缓存 `history/attribute_history/get_price` 返回的前复权序列

## Step 3: 设计 QMT 外壳

默认使用 QMT 内置 Python 回测结构：

```python
#coding:gbk
import pandas as pd
import numpy as np

class G:
    pass

g = G()

def init(C):
    g.account_id = "testS"
    g.stock_list = ["000001.SZ"]
    C.capital = 1000000

def handlebar(C):
    if hasattr(C, "is_new_bar") and not C.is_new_bar():
        return
    bar_time = timetag_to_datetime(C.get_bar_timetag(C.barpos), "%Y%m%d%H%M%S")
```

原则：

- 聚宽 `g.xxx` 默认迁到模块级 `g.xxx`，不要默认迁到 `C.xxx`。
- 官方提醒 `ContextInfo` 自定义变量可能回滚；只有用户示例已这么写时才沿用，并在报告说明。
- 若示例代码使用 `ContextInfo.get_history_data()`，可以沿用示例，但报告中建议新迁移优先考虑 `ContextInfo.get_market_data_ex(..., subscribe=False)`。
- 不要默认生成 `C.get_sector()`、`C.get_industry()`、`C.is_suspended_stock()`、`C.benchmark = ...`、`C.set_slippage()`、`C.set_commission()` 这些官方当前文档未确认或标为 UI/只读口径的调用。
- 如果 `target_mode="live"`，`handlebar(C)` 开头必须跳过历史 K 线：
  ```python
  if hasattr(C, "is_last_bar") and not C.is_last_bar():
      return
  ```

## Step 4: 转换核心模块

### 代码和状态

- `context` -> `C` 或 `ContextInfo`
- `g.xxx` -> 模块级 `g.xxx`
- `context.portfolio` -> `get_trade_detail_data()` 或本地状态适配器
- `log.info/warning/error` -> `print()` 或本地 logger

### 代码后缀

- `.XSHG` -> `.SH`
- `.XSHE` -> `.SZ`

### 数据

- 将聚宽行情调用集中改到 `_history()` / `_get_market_panel()` 适配器。
- 回测行情必须使用 `subscribe=False`，并用当前 bar 时间 `end_time` 截止。
- 聚宽日频/分钟频 `handle_data` 的 `data` 是上一完整 bar；严格对齐时用 helper 排除 QMT 当前 bar，若使用当前 bar 口径必须写入报告。
- 聚宽 `set_option("use_real_price", True)` 会影响 `history/attribute_history/get_price` 的前复权返回；QMT 侧要明确 `dividend_type` 和客户端复权口径。
- `get_current_data().paused` 用 `suspendFlag` 或 tick `stockStatus`。
- ST 状态优先用 `get_st_status()` / `C.get_his_st_data()`，名称包含 ST 只能作为近似。
- 股票池优先使用 `C.get_stock_list_in_sector("沪深300")` 这类本地板块名；行业/概念映射无法确认时写入报告。
- 财务字段和市值计算集中封装，默认 `report_type="announce_time"`。

### 调度

- 日线回测：在 `handlebar` 顺序调用原 `run_daily` 函数。
- 周/月频：实现 helper，避免 `run_time`。
- 实盘：仅用户明确要求时使用 `ContextInfo.run_time()` 或订阅回调。

### 下单

- 聚宽 `order_*` 默认优先映射到高层回测交易函数：`order_shares/order_value/order_percent/order_target_value/order_target_percent`。
- 给每个 QMT 高层下单函数补齐 `ContextInfo` 和账号参数。
- 若目标是实盘/模拟信号、用户示例已使用 `passorder()`、或订单类型超出高层函数覆盖范围，使用 `passorder()` 并说明参数假设。
- 实盘版统一通过下单 adapter 调用 `passorder()`，并用 `get_trade_detail_data()` 同步平台持仓/资金。
- 若原策略依赖订单返回值，改为状态维护或 `get_trade_detail_data()` 查询，并标注人工确认。

### 回测设置

- `C.capital`、`C.start`、`C.end` 可在 `init` 中设置。
- `set_benchmark` 默认迁为报告中的 QMT UI 设置项；`C.benchmark` 官方为只读。
- `set_slippage/set_order_cost` 默认迁为报告中的 QMT UI 设置项；只有用户示例已使用本地方法时才生成代码。
- `set_option("use_real_price")` 转成行情复权参数和 UI 说明。

## Step 5: 生成迁移报告

报告必须包含：

```markdown
# 迁移报告：聚宽策略 -> QMT

## 1. 基本信息
- 原始文件
- 输出文件
- 策略类型
- 预估转换率

## 2. 已转换
- 架构
- 股票代码
- 行情
- 股票池
- 财务
- 交易
- 回测设置

## 3. 需要人工确认
- QMT 本地板块/行业名称
- QMT 本地财务字段名
- 财务数据和历史行情下载
- 账户/持仓对象属性
- 聚宽订单对象返回值差异
- QMT UI 中的基准、手续费、滑点、周期、主图设置
- ContextInfo 自定义状态是否存在回滚风险
- 聚宽上一完整 bar 与 QMT 当前 bar 的时点差异
- 聚宽动态复权与 QMT `dividend_type`/客户端复权口径差异

## 4. 字段映射
| 聚宽字段/API | QMT 字段/API | 状态 |

## 5. 验证
- py_compile 结果
- validator 结果
- 裸 JQ API 残留扫描
- 未覆盖 API
- 回测对齐建议
```

## Step 6: 验证

1. 运行 `python -m py_compile <output_file>`。
2. 使用 `lib/validator.py` 做 API 覆盖检查。
3. 人工扫描是否残留：
   - `jqdata`
   - `.XSHG` / `.XSHE`
   - `context.portfolio`
   - `get_current_data`
   - `get_fundamentals`
   - `run_daily/run_weekly/run_monthly`
   - `OrderStatus`
   - `C.get_sector` / `ContextInfo.get_sector`
   - `C.get_industry` / `ContextInfo.get_industry`
   - `C.is_suspended_stock` / `ContextInfo.is_suspended_stock`
   - `C.benchmark =` / `ContextInfo.benchmark =`
   - 回测版 `subscribe=True`
   - 实盘版缺少 `is_last_bar()`
4. 若残留是有意保留，必须写入报告。
