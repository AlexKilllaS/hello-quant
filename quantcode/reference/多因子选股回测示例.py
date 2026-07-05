# coding:gbk
# 内置 Python / QMT
# 小市值 + Barra CNE6 量价因子的多因子策略示例
# 版本: 2026-06-16
# ============================================================
# 【策略逻辑说明】
# ============================================================
# 一、核心思想
#   结合小市值因子与Barra CNE6量价因子（动量、波动率、流动性）进行多因子选股。
#   对各因子进行标准化（Z-Score）处理后等权合成，选取综合得分最高的N只股票。
#
# 二、备选池
#   沪深A股。
#
# 三、调仓频率
#   每周五 14:50 检查一次。
#
# 四、买入规则
#   1. 计算所有股票的因子值（小市值、20日反转、20日波动率、20日平均成交额）。
#   2. 对各因子进行极值处理和标准化。
#   3. 等权合成综合得分，按降序排列。
#   4. 选取前TOP_N只股票买入。
#
# 五、卖出规则
#   持仓股票不再是综合得分前TOP_N时卖出。
#     ① 每周五14:50重新计算全排名
#     ② 如果当前持仓不在目标名单中，先全卖
#     ③ 再按新目标买入
#     ④ 先卖后买，中间隔1根1分钟K线（约1分钟）完成换仓
#
# 六、参数说明（在init中修改）
#   LOOKBACK_DAYS  = 20   因子回看周期（交易日）
#   REBALANCE_DAY  = 5    调仓日：5=周五
#   REBALANCE_TIME = '14:50'  调仓执行时间
#   TOP_N          = 5    选前N支等分买入
#   TOTAL_AMOUNT   = 10万 总投入金额（元）
#   MIN_LOTS       = 100  最小委托单位（股）
#   accountid      = 资金账号
#
# 七、注意事项
#   1. 回测模式自动使用K线时间，实盘使用系统时间
#   2. 交易价格优先取实时行情，取不到则用最新收盘价
#   3. 买入量自动向下取整到100的倍数
#   4. 本示例使用基础量价数据近似计算Barra CNE6量价因子，实际应用中需根据完整Barra模型定义进行扩展
import datetime
import numpy as np


class G(): pass


g = G()


def init(C):
    # ============================================================
    # 【策略配置区】 ── 修改以下变量即可调整策略行为
    # ============================================================
    # 回测系统参数设置
    C.start = "2023-12-21 00:00:00"
    C.end = "2026-06-16 00:00:00"
    C.set_commission(0, [0.001, 0, 0, 0, 0, 0.0002])  # 手续费设置为万1
    g.backtest = C.do_back_test

    # --- 备选股票池 ---
    # 获取沪深A股
    g.STOCK_POOL = C.get_stock_list_in_sector('沪深A股')

    # --- 策略参数 ---
    g.LOOKBACK_DAYS = 20  # 因子回看周期（交易日）
    g.REBALANCE_DAY = 5  # 调仓日：1=周一 ... 5=周五
    g.REBALANCE_TIME = '14:50'  # 调仓执行时间（HH:MM）
    g.TOP_N = 5  # 选前N支等分买入
    g.TOTAL_AMOUNT = C.capital if g.backtest else 10_0000  # 总投入金额（元）
    g.MIN_LOTS = 100  # 最小委托单位（股）

    # --- 交易账户 ---
    g.accountid = 'test' if g.backtest else 'your_account_id'  # 实盘请替换为实际资金账号

    # ---变量设置---
    g.signal_triggered = None
    g.sub = not g.backtest  # 取行情时是否订阅实时行情


def handlebar(C):
    if not g.backtest and not C.is_last_bar():
        return
    now = get_trade_time(C)

    if now.isoweekday() != g.REBALANCE_DAY:
        return

    if not g.backtest and now.strftime('%H:%M') < g.REBALANCE_TIME:
        return

    if g.signal_triggered is None or g.signal_triggered.date != now.date:
        rebalance(C)
        g.signal_triggered = now


def rebalance(C):
    """执行调仓"""
    # 1. 获取K线数据
    data = get_stock_data(C)
    # 2. 计算因子值
    factor_df = calc_factors(data)
    if factor_df.empty:
        return

    # 3. 因子处理与合成
    factor_df = process_factors(factor_df)
    # 按综合得分降序排列
    ranking = factor_df.sort_values(by='score', ascending=False)

    # 4. 确定目标
    targets = ranking.head(g.TOP_N)
    target_codes = targets['code'].tolist()

    # 5. 查询持仓
    holding = get_positions(C)
    holding_codes = set(holding.keys())

    need_buy = [code for code in target_codes if code not in holding_codes]
    need_sell = [stock for stock in holding if stock not in target_codes]

    if not need_buy and not need_sell:
        return

    if need_sell:
        do_sell(C, need_sell, holding)

    if need_buy:
        do_buy(C, need_buy)


def get_trade_time(C):
    timetag = C.get_bar_timetag(C.barpos)
    bar_date = datetime.datetime.fromtimestamp(timetag / 1000)
    return bar_date if g.backtest else datetime.datetime.now()


def get_stock_data(C):
    """获取股票日K线数据"""
    end_date = get_trade_time(C).strftime('%Y%m%d')
    return C.get_market_data_ex(
        ['close', 'volume', 'amount'], g.STOCK_POOL, '1d', '', end_date,
        dividend_type=C.dividend_type, count=g.LOOKBACK_DAYS * 3 + 30, subscribe=g.sub)


def calc_factors(data):
    """计算小市值及Barra量价因子"""
    results = []
    need = g.LOOKBACK_DAYS + 1
    for code in g.STOCK_POOL:
        if code not in data:
            continue
        df = data[code]
        if len(df) < need:
            continue

        close = df['close'].values
        volume = df['volume'].values
        amount = df['amount'].values

        # 小市值因子：使用收盘价 * 总成交量作为近似（实际应使用总股本或流通股本数据）
        market_cap = close[-1] * volume[-1]  # 注意：此处为简化示例，实际需获取真实总股本

        # Barra CNE6 量价因子近似计算
        # 1. 动量/反转因子：近20日收益率
        momentum = close[-1] / close[-g.LOOKBACK_DAYS - 1] - 1

        # 2. 波动率因子：近20日日收益率标准差
        rets = np.diff(np.log(close[-g.LOOKBACK_DAYS - 1:]))
        volatility = np.std(rets)

        # 3. 流动性因子：近20日平均成交额
        liquidity = np.mean(amount[-g.LOOKBACK_DAYS:])

        results.append({
            'code': code,
            'market_cap': market_cap,
            'momentum': momentum,
            'volatility': volatility,
            'liquidity': liquidity
        })

    import pandas as pd
    return pd.DataFrame(results)


def process_factors(df):
    """因子去极值、标准化及合成"""
    if df.empty:
        return df

    # 因子列表
    factors = ['market_cap', 'momentum', 'volatility', 'liquidity']

    for f in factors:
        # 使用MAD法去极值
        median = df[f].median()
        mad = (df[f] - median).abs().median()
        df[f] = np.clip(df[f], median - 3 * mad, median + 3 * mad)

        # Z-Score标准化
        mean = df[f].mean()
        std = df[f].std()
        if std > 0:
            df[f] = (df[f] - mean) / std

# 等权