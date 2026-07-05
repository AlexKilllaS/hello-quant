# coding:gbk
# ============================================================
# 网格交易策略
# 策略逻辑：以昨收价为基准，日内最高/最低价达到阈值时分档买卖
#   最高涨 3% → 卖1档(5万)  最高涨 4% → 卖2档(5万)  最高涨 5% → 卖3档(5万)
#   最低跌 3% → 买1档(5万)  最低跌 4% → 买2档(5万)  最低跌 5% → 买3档(5万)
# ============================================================
account = "testS"


def init(C):
    C.stock = C.stockcode + '.' + C.market
    # ── 网格参数 ──
    A.trade_amount = 50000  # 单笔交易金额（元）
    A.init_ratio = 0.03  # 初始触发涨跌幅（3%）
    A.step_ratio = 0.01  # 每档递增比例（1%）
    A.grid_levels = 3  # 网格档位数
    # ── 日内状态 ──
    A.today = ''  # 当前日期
    A.pre_close = 0  # 昨收价
    A.day_high = 0  # 当日最高价（累计）
    A.day_low = 999999  # 当日最低价（累计）
    A.buy_done = []  # 已买入的档位 [True/False, ...]
    A.sell_done = []  # 已卖出的档位


class a(): pass


A = a()


def handlebar(C):
    if not C.do_back_test and not C.is_last_bar():
        return
    trade_type = 0 if C.do_back_test else 2
    # ── 当前K线日期 ──
    bar_date = timetag_to_datetime(C.get_bar_timetag(C.barpos), '%Y%m%d%H%M%S')
    today = bar_date[:8]  # YYYYMMDD

    # ── 新交易日：重置状态 ──
    if today != A.today:
        A.today = today
        A.buy_done = [False] * A.grid_levels
        A.sell_done = [False] * A.grid_levels
        A.day_high = 0
        A.day_low = 999999
        # 获取昨收价
        pre_data = C.get_market_data_ex(
            ['close'], [C.stock], end_time=bar_date,
            period='1d', count=2, subscribe=False
        )
        if len(pre_data[C.stock]) >= 2:
            A.pre_close = pre_data[C.stock].iloc[-2, 0]
    if A.pre_close <= 0:
        print(f"{bar_date} 昨收价异常 跳过")
        return
    # ── 获取当前K线数据（含最高、最低） ──
    local_data = C.get_market_data_ex(
        ['close', 'high', 'low'], [C.stock], end_time=bar_date,
        period=C.period, count=1, subscribe=False
    )
    if len(local_data[C.stock]) < 1:
        return
    row = local_data[C.stock].iloc[-1]
    price = row['close']
    bar_high = row['high']
    bar_low = row['low']
    if price <= 0:
        return
    # ── 更新当日最高/最低价（累计） ──
    if bar_high > A.day_high:
        A.day_high = bar_high
    if bar_low < A.day_low:
        A.day_low = bar_low
    # ── 卖出用最高价判断，买入用最低价判断 ──
    sell_pct = (A.day_high - A.pre_close) / A.pre_close
    buy_pct = (A.day_low - A.pre_close) / A.pre_close
    # ── 账户信息 ──
    acct = get_trade_detail_data(account, 'stock', 'account')
    if not acct:
        return
    available_cash = int(acct[0].m_dAvailable)
    holdings = get_trade_detail_data(account, 'stock', 'position')
    holdings = {i.m_strInstrumentID + '.' + i.m_strExchangeID: i.m_nCanUseVolume for i in holdings}
    hold_vol = holdings.get(C.stock, 0)
    # ── 逐档判断 ──
    for i in range(A.grid_levels):
        threshold = A.init_ratio + i * A.step_ratio  # 3%, 4%, 5%
        buy_price = round(A.pre_close * (1 - threshold), 2)  # 买入档价格
        sell_price = round(A.pre_close * (1 + threshold), 2)  # 卖出档价格
        # ── 买入：最低价跌幅达到阈值 ──
        if not A.buy_done[i] and buy_pct <= -threshold:
            buy_vol = int(A.trade_amount / buy_price / 100) * 100  # 按档位价格算整手
            if buy_vol >= 100 and available_cash >= buy_vol * buy_price:
                # prType=11 对手价, 价格=档位价
                passorder(23, 1101, account, C.stock, 11, buy_price, buy_vol, '内置网格', trade_type, C)
                A.buy_done[i] = True
                available_cash -= buy_vol * buy_price
                print(f"{bar_date} 买入第{i + 1}档 最低跌{buy_pct * 100:.1f}% "
                      f"触发-{threshold * 100:.0f}% 价格{buy_price} 数量{buy_vol}")
                C.draw_text(1, 1, f'买{i + 1}')
        # ── 卖出：最高价涨幅达到阈值 ──
        if not A.sell_done[i] and sell_pct >= threshold:
            sell_vol = int(A.trade_amount / sell_price / 100) * 100  # 按档位价格算整手
            sell_vol = min(sell_vol, hold_vol)
            if sell_vol >= 100:
                # prType=11 对手价, 价格=档位价
                passorder(24, 1101, account, C.stock, 11, sell_price, sell_vol, '内置网格', trade_type, C)
                A.sell_done[i] = True
                hold_vol -= sell_vol
                print(f"{bar_date} 卖出第{i + 1}档 最高涨{sell_pct * 100:.1f}% "
                      f"触发+{threshold * 100:.0f}% 价格{sell_price} 数量{sell_vol}")
                C.draw_text(1, 1, f'卖{i + 1}')
