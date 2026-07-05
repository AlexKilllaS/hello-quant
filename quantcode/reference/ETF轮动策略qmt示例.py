# coding:gbk
# 内置 Python / QMT
# ETF轮动策略 -
# 版本: 2026-06-16
# ============================================================
# 【策略逻辑说明】
# ============================================================
# 一、核心思想
#   价格动量（选强）：用近N日涨幅衡量，涨得多的说明短期动能更强。
#   成交量确认（过滤）：当日成交额 > 过去M日平均成交额才算放量，
#     无量上涨视为"伪信号"，跳过。
#
# 二、备选池
#   5只不同方向的高流动性ETF，覆盖科技成长、周期、消费、避险：
#     - 588000.SH  科创50ETF（科技成长）
#     - 512880.SH  证券ETF（周期风向标）
#     - 159928.SZ  消费ETF（稳定价值）
#     - 518880.SH  黄金ETF（避险）
#     - 511010.SH  国债ETF（避险）
#   可自行增删，流动性越好的标的越稳定。
#
# 三、调仓频率
#   每周五 14:50 检查一次。
#
# 四、买入规则
#   1. 计算所有ETF的近20日涨幅，按降序排列。
#   2. 从第一名开始选，要求当日成交额 > 过去20日均成交额（放量确认）。
#   3. 如果第一名缩量上涨，顺延到下一名。
#   4. TOP_N 参数控制选前几名，默认选1只。
#
# 五、卖出规则
#   持仓ETF不再是"涨幅前TOP_N且放量"时卖出。
#     ① 每周五14:50重新计算全排名
#     ② 如果当前持仓不在目标名单中，先全卖
#     ③ 再按新目标买入
#     ④ 先卖后买，中间隔1根1分钟K线（约1分钟）完成换仓
#
# 六、参数说明（在init中修改）
#   LOOKBACK_DAYS  = 20   涨幅回看周期（交易日）
#   REBALANCE_DAY  = 5    调仓日：5=周五
#   REBALANCE_TIME = '14:50'  调仓执行时间
#   TOP_N          = 1    选前N支等分买入
#   TOTAL_AMOUNT   = 10万 总投入金额（元）
#   MIN_LOTS       = 100  最小委托单位（ETF 100份起）
#   accountid      = 资金账号
#
# 七、回测设置
#   周期：1分钟K线（handlebar每1分钟触发一次）
#   数据复权：前复权（dividend_type='front_ratio'）
#   手续费：千1（可在set_commission调整）
#   成交量字段：使用成交额 amount 而非成交量 volume
#
# 八、注意事项
#   1. 回测模式自动使用K线时间，实盘使用系统时间
#   4. 交易价格优先取实时行情lastPrice，取不到则用最新收盘价
#   5. 买入量自动向下取整到100的倍数
import datetime


class G(): pass


g = G()


def init(C):
    # ============================================================
    # 【策略配置区】 ── 修改以下变量即可调整策略行为
    # ============================================================
    # 回测系统参数设置
    C.start = "2023-12-21 00:00:00"  # 注意格式，不要写错
    # ContextInfo.end = time.strftime('%Y-%m-%d')+ " 00:00:00"  # 注意格式，不要写错
    C.end = "2026-06-16 00:00:00"  # 注意格式，不要写错
    C.set_commission(0, [0.001, 0, 0, 0, 0, 0.0002])  # 手续费设置为万1 具体用法参考左侧函数说明
    g.backtest = C.do_back_test  # True 表示是回测模式，False表示不是回测模式
    # --- 备选ETF池 ---
    g.ETF_POOL = [
        '588000.SH',  # 科创50ETF（科技成长）
        '512880.SH',  # 证券ETF（周期风向标）
        '159928.SZ',  # 消费ETF（稳定价值）
        '518880.SH',  # 黄金ETF（避险）
        '511010.SH',  # 国债ETF（避险）
    ]
    """初始化"""
    # --- 策略参数 ---
    g.LOOKBACK_DAYS = 20  # 涨幅回看周期（交易日）
    g.REBALANCE_DAY = 5  # 调仓日：1=周一 ... 5=周五
    g.REBALANCE_TIME = '14:50'  # 调仓执行时间（HH:MM）
    g.TOP_N = 1  # 选前N支等分买入
    g.TOTAL_AMOUNT = C.capital if g.backtest else 10_0000  # 总投入金额（元）
    g.MIN_LOTS = 100  # 最小委托单位（股，ETF为100份）

    # --- 交易账户 ---
    g.accountid = 'test' if g.backtest else '2000060'  # 资金账号

    # ---变量设置---
    g.signal_triggered = None
    g.sub = not g.backtest  # 取行情时是否订阅实时行情  True:订阅，False:不订阅


def handlebar(C):
    """
    每根K线回调。策略设为1分钟周期，只在第一只ETF的handlebar中执行调仓逻辑，避免重复。
    """
    if not g.backtest and not C.is_last_bar():
        return
    now = get_trade_time(C)
    # === 阶段2：检查是否到达调仓时间 ===
    if now.isoweekday() != g.REBALANCE_DAY:
        if not g.backtest:
            print('今天不是调仓日', now)
        return
    if not g.backtest and now.strftime('%H:%M') < g.REBALANCE_TIME:
        return
    print(f'今天{get_trade_time(C)}是调仓日，执行交易判断'.ljust(100, '-'), )
    if g.signal_triggered is None or g.signal_triggered.date != now.date:
        rebalance(C)
        g.signal_triggered = now
        print(f'执行交易结束'.rjust(100, '-'), '\n')


def rebalance(C):
    """执行调仓"""
    # 1. 获取K线数据
    print("[调仓] 获取ETF数据...")
    data = get_etf_data(C)
    # 2. 计算排名
    ranking = calc_ranking(data)
    if not ranking:
        print("[调仓] 无可用数据，跳过")
        return
    print("\n[调仓] ETF排名（近%d日涨幅）:" % g.LOOKBACK_DAYS)
    for i, r in enumerate(ranking):
        vol_flag = "放量" if r['is_volume_up'] else "缩量"
        print("  %d. %s  涨幅=%.2f%%  %s" % (
            i + 1, r['code'], r['return'] * 100, vol_flag))
    # 3. 确定目标
    targets = decide_targets(ranking)
    if not targets:
        print("\n[调仓] 无符合条件的标的（均缩量），今日不交易")
        return
    print("\n[调仓] 目标持仓:")
    for t in targets:
        print("  %s  涨幅=%.2f%%" % (t['code'], t['return'] * 100))
    # 4. 查询持仓
    holding = get_positions(C)
    if holding:
        print("\n[调仓] 当前持仓: %s" % ', '.join(
            '%s=%d' % (c, v) for c, v in holding.items()))
    else:
        print("\n[调仓] 当前无持仓")
    # 5. 执行调仓
    target_codes = set(t['code'] for t in targets)
    holding_codes = set(holding.keys())
    print(target_codes)
    need_buy = [t for t in targets if t['code'] not in holding_codes]
    need_sell = [stock for stock in holding if stock not in target_codes]
    if not need_buy:
        print("\n[调仓] 持仓不变，无需调仓")
        return
    if not holding:
        # 无持仓，直接买入
        print("\n[调仓] 无持仓，直接买入")
        do_buy(C, targets)
        return
    # 有持仓且需要换仓：先卖，下一分钟买
    print("\n[调仓] 先卖出全部持仓...")
    if need_sell:
        do_sell(C, need_sell, holding)
    if need_buy:
        do_buy(C, need_buy)
    print("[调仓] 等待1分钟后买入...")


def get_trade_time(C) -> datetime.datetime:
    '''
    返回交易日，回测模式时返回当前bar日期，否则返回当前电脑系统日期
    '''
    timetag = C.get_bar_timetag(C.barpos)
    bar_date = datetime.datetime.fromtimestamp(timetag / 1000)
    return bar_date if g.backtest else datetime.datetime.now()


def get_etf_data(C):
    """获取ETF日K线数据（close + amount）"""
    end_date = get_trade_time(C).strftime('%Y%m%d')

    return C.get_market_data_ex(
        ['close', 'amount'], g.ETF_POOL, '1d', '', end_date,
        dividend_type=C.dividend_type, count=g.LOOKBACK_DAYS * 3 + 30, subscribe=g.sub)


def calc_ranking(data):
    """
    计算每只ETF的近lookback_days日涨幅和放量情况
    返回按涨幅降序排列的列表
    """
    results = []
    need = g.LOOKBACK_DAYS + 1
    for code in g.ETF_POOL:
        if code not in data:
            print("[警告] %s 无数据，跳过" % code)
            continue
        df = data[code]
        if len(df) < need:
            print("[警告] %s 数据不足(%d<%d)，跳过" % (code, len(df), need))
            continue
        close_list = list(df['close'])
        amount_list = list(df['amount'])
        # 近20日涨幅
        ret = close_list[-1] / close_list[-g.LOOKBACK_DAYS - 1] - 1
        # 放量：当日amount > 过去20日平均amount
        avg_amount = sum(amount_list[-g.LOOKBACK_DAYS - 1:-1]) / g.LOOKBACK_DAYS
        is_volume_up = amount_list[-1] > avg_amount
        results.append({
            'code': code,
            'return': ret,
            'is_volume_up': is_volume_up,
            'close': close_list[-1]
        })
    results.sort(key=lambda x: x['return'], reverse=True)
    return results


def decide_targets(ranking):
    """标准模式：从排名中选前N支放量的ETF"""
    volume_up = [r for r in ranking if r['is_volume_up']]
    return volume_up[:g.TOP_N]


def get_positions(C):
    """查询当前持仓，返回 {stock_code: volume}"""
    positions = get_trade_detail_data(g.accountid, 'stock', 'POSITION')
    holding = {}
    if positions:
        for p in positions:
            code = p.m_strInstrumentID + '.' + p.m_strExchangeID
            if p.m_nVolume > 0:
                holding[code] = p.m_nVolume
    return holding


def get_trade_price(C, code):
    """获取交易价格：优先实时价，后备收盘价"""
    end_date = get_trade_time(C).strftime('%Y%m%d')
    # 备选：K线收盘价
    d = C.get_market_data_ex(['close'], [code], '1d', '', end_date, count=1, dividend_type=C.dividend_type,
                             subscribe=g.sub)
    if code in d and len(d[code]) > 0:
        return float(d[code].iloc[-1, 0])
    return 0


def calc_buy_volume(amount_per_etf, price):
    """计算买入数量（向下取整到MIN_LOTS的倍数）"""
    if price <= 0:
        return 0
    return int(amount_per_etf / price / g.MIN_LOTS) * g.MIN_LOTS


def do_buy(C, targets):
    """买入目标ETF"""

    n = len(targets)
    if n == 0:
        return

    accounts = get_trade_detail_data(g.accountid, 'stock', 'account')
    account_result = {}
    for acc in accounts:
        account_result = {
            '资金账号': acc.m_strAccountID,
            '总资产': acc.m_dBalance,
            '可用金额': acc.m_dAvailable,
            '初始权益': acc.m_dInitBalance,
            '总市值': acc.m_dInstrumentValue,
            '期初权益': acc.m_dPreBalance,
            '持仓盈亏': acc.m_dPositionProfit,
            '平仓盈亏': acc.m_dCloseProfit,

        }
        break
    available_money = account_result['可用金额']
    amount_per_etf = min(g.TOTAL_AMOUNT, available_money) / n
    amount_per_etf = amount_per_etf * 0.99
    for t in targets:
        price = get_trade_price(C, t['code'])  # 注意，该处用了close（收盘价）作为买入价格可以自行修改
        if price <= 0:
            print("[买入] %s 无法获取价格，跳过" % t['code'])
            continue
        volume = calc_buy_volume(amount_per_etf, price)
        if volume < g.MIN_LOTS:
            print("[买入] %s 金额不足，跳过（需%.0f元/每股%.3f）" % (
                t['code'], amount_per_etf, price))
            continue
        print("[买入] %s 数量=%d 价格=%.3f 金额=%.0f" % (
            t['code'], volume, price, volume * price))
        passorder(23, 1101, g.accountid, t['code'], 11, price, volume,
                  "ETF轮动", 1, "买入", C)


def do_sell(C, need_sell, holding):
    """卖出所有持仓"""
    for code, volume in holding.items():
        if code not in need_sell:
            continue
        price = get_trade_price(C, code)  # 注意，该处用了close（收盘价）作为买入价格可以自行修改
        if price <= 0:
            print("[卖出] %s 无法获取价格，跳过" % code)
            continue
        print("[卖出] %s 数量=%d 价格=%.3f 金额=%.0f" % (
            code, volume, price, volume * price))
        passorder(24, 1101, g.accountid, code, 11, price, volume,
                  "ETF轮动", 1, "卖出", C)
