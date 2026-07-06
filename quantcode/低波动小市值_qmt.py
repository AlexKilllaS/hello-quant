#coding:gbk
"""
QMT 转换版：低波动小市值

原始策略来自聚宽：小市值 + Barra CNE6 量价因子。
本文件参考 quantcode/qmt示例-多因子选股回测示例.py 的回测写法：
init(ContextInfo) 初始化，handlebar(ContextInfo) 在日线 bar 中执行每日任务。
"""

import datetime

import numpy as np
import pandas as pd


class G:
    pass


g = G()

LOT_SIZE = 100
TRADING_DAYS_PER_MONTH = 21
LIMIT_PRICE_MIN_DIFF = 0.01
LIMIT_PRICE_REL_DIFF = 0.0001
FACTOR_NAMES = ("HSIGMA", "DASTD", "CMRA", "STOM", "STOQ", "STOA", "ATVR")


FIELD_ALIASES = {
    "close": ["close"],
    "open": ["open"],
    "high": ["high"],
    "low": ["low"],
    "volume": ["volume", "vol"],
    "money": ["amount", "money", "turnover"],
    "total_value": [
        "total_value",
        "float_market_value",
        "float_mv",
        "circulating_market_value",
        "market_value",
    ],
    "high_limit": ["upLimit", "high_limit", "up_limit", "uplimit"],
    "low_limit": ["downLimit", "low_limit", "down_limit", "downlimit"],
    "suspend": ["suspendFlag"],
}

INDEX_SECTOR_NAME_MAP = {
    "000300.SH": ["沪深300"],
    "000905.SH": ["中证500"],
    "000016.SH": ["上证50"],
    "399101.SZ": ["中小综指", "中小板综", "中小企业综合指数"],
}


def init(ContextInfo):
    # -------- 标的与账户 --------
    g.benchmark = ContextInfo.benchmark
    g.index_code = "399101.SZ"
    g.accountID = "testS"

    # -------- 策略核心参数 --------
    g.stock_num = 10
    g.up_price = 80
    g.stoploss_limit = 0.07
    g.stoploss_market = 0.05
    g.limit_days = 5
    g.run_stoploss = False

    # -------- 动量过滤参数 --------
    g.momentum_days = 30
    g.filter_threshold1 = -0.5
    g.filter_threshold2 = 0.5

    # -------- 流动性过滤参数 --------
    g.liquidity_days = 20
    g.min_avg_amount = 5e7

    # -------- 市值过滤参数（单位：亿元）--------
    g.min_market_cap = 5
    g.max_market_cap = 50

    # -------- Barra 因子参数 --------
    g.vol_window = 252
    g.vol_halflife = 63
    g.dastd_halflife = 42
    g.cmra_months = 12
    g.stom_window = 21
    g.stoq_window = 63
    g.stoa_window = 252
    g.atvr_halflife = 63

    g.factor_weights = {name: 1 / len(FACTOR_NAMES) for name in FACTOR_NAMES}

    # -------- QMT 回测状态 --------
    g.s = _get_sector_stocks(ContextInfo, g.index_code)
    print("策略初始化: 市场整体标的 {}, 指数池 {} 只".format(g.benchmark, len(g.s)))

    g.holdings = {stock: 0 for stock in g.s}
    g.buypoint = {}
    g.hold_list = []
    g.yesterday_HL_list = []
    g.target_list = []
    g.not_buy_again = []
    g.history_hold_list = []
    g.reason_to_sell = ""
    g.market_cap_map = {}

    g.initial_cash = float(ContextInfo.capital)
    g.money = float(g.initial_cash)
    g.profit = 0.0
    g.profit_ratio = 0.0
    g.commission_rate = 2.5 / 10000
    g.close_tax_rate = 0.001
    g.min_commission = 5.0
    g.order_price_field = "open"

    g.min_bars = g.vol_window + 10
    g._week_key = None
    g._week_trading_day = 0
    g._current_date = None
    g._previous_trade_date = None
    g._last_processed_bar = None
    g._selection_end_time = None

    print("策略初始化完成, 指数池数量: {}, 因子权重: {}".format(
        len(g.s), {k: round(v, 3) for k, v in g.factor_weights.items()}
    ))


def handlebar(ContextInfo):
    if not ContextInfo.is_new_bar():
        return

    _update_calendar_state(ContextInfo)

    d = ContextInfo.barpos
    if d < g.min_bars:
        return
    if g._last_processed_bar == d:
        return

    g._last_processed_bar = d
    prepare_stock_list(ContextInfo)
    weekly_adjustment(ContextInfo)
    check_stop_loss(ContextInfo)
    trade_afternoon(ContextInfo)

    g.profit_ratio = g.profit / float(g.initial_cash or 1)


# ==============================================================================
# 日历与 QMT 兼容工具
# ==============================================================================
def _get_current_date(ContextInfo):
    barpos = ContextInfo.barpos
    try:
        tag = ContextInfo.get_bar_timetag(barpos)
        date_text = timetag_to_datetime(tag, "%Y%m%d")
        if isinstance(date_text, datetime.datetime):
            return date_text.date()
        if isinstance(date_text, datetime.date):
            return date_text
        return datetime.datetime.strptime(str(date_text)[:8], "%Y%m%d").date()
    except Exception:
        return datetime.date.today()


def _bar_time(ContextInfo, fmt="%Y%m%d%H%M%S"):
    try:
        return timetag_to_datetime(ContextInfo.get_bar_timetag(ContextInfo.barpos), fmt)
    except Exception:
        return ""


def _previous_bar_end_time(ContextInfo):
    trade_date = g._previous_trade_date or _get_current_date(ContextInfo)
    return _date_to_yyyymmdd(trade_date) + "150000"


def _update_calendar_state(ContextInfo):
    current_date = _get_current_date(ContextInfo)
    if g._current_date == current_date:
        return

    g._previous_trade_date = g._current_date
    g._current_date = current_date
    week_key = current_date.isocalendar()[:2]

    if g._week_key != week_key:
        g._week_key = week_key
        g._week_trading_day = 1
    else:
        g._week_trading_day += 1


def is_nth_trading_day_of_week(ContextInfo, n):
    if n <= 0:
        return False
    return g._week_trading_day == n


def _date_to_yyyymmdd(date_value):
    if isinstance(date_value, datetime.datetime):
        date_value = date_value.date()
    if isinstance(date_value, datetime.date):
        return date_value.strftime("%Y%m%d")
    return str(date_value)[:8]


def _instrument_detail(ContextInfo, stock):
    try:
        detail = ContextInfo.get_instrumentdetail(stock)
    except Exception:
        return None
    if isinstance(detail, dict) and detail:
        return detail
    return None


def _detail_bool(value):
    if isinstance(value, bool):
        return value
    if value is None:
        return None
    text = str(value).strip().lower()
    if text in ("1", "true", "yes", "y", "交易", "可交易"):
        return True
    if text in ("0", "false", "no", "n", "停牌", "不可交易"):
        return False
    return None


def _is_suspended_detail(detail):
    is_trading = _detail_bool(detail.get("IsTrading") if isinstance(detail, dict) else None)
    if is_trading is False:
        return True

    status = detail.get("InstrumentStatus") if isinstance(detail, dict) else None
    if status in (None, ""):
        return False
    text = str(status)
    return "停" in text or "暂停" in text


def _is_limit_price(price, limit_price):
    return (
        np.isfinite(price)
        and np.isfinite(limit_price)
        and abs(price - limit_price) <= max(
            LIMIT_PRICE_MIN_DIFF,
            limit_price * LIMIT_PRICE_REL_DIFF,
        )
    )


def _held_stocks():
    return [stock for stock, shares in g.holdings.items() if shares > 0]


def _mark_sell_reason(stock, reason):
    g.reason_to_sell = reason
    g.not_buy_again.append(stock)


def _get_sector_stocks(ContextInfo, sector_code):
    sector_names = INDEX_SECTOR_NAME_MAP.get(sector_code, [sector_code])
    for sector_name in sector_names:
        try:
            stocks = ContextInfo.get_stock_list_in_sector(sector_name)
        except Exception:
            stocks = []
        if stocks:
            return list(dict.fromkeys([stock for stock in stocks if stock]))
    print("获取板块成分股失败 {}, 请确认 QMT 本地板块名: {}".format(sector_code, sector_names))
    return []


def _to_float_list(values):
    if values is None:
        return []
    if isinstance(values, pd.Series):
        values = values.tolist()
    elif isinstance(values, np.ndarray):
        values = values.tolist()
    elif not isinstance(values, (list, tuple)):
        values = [values]

    result = []
    for value in values:
        try:
            if value is None:
                result.append(np.nan)
            else:
                result.append(float(value))
        except Exception:
            result.append(np.nan)
    return result


def _extract_market_series(raw_data, stock, field, count):
    if raw_data is None:
        return []

    if isinstance(raw_data, dict):
        if stock in raw_data:
            stock_data = raw_data.get(stock)
            if isinstance(stock_data, pd.DataFrame):
                if field in stock_data.columns:
                    return _to_float_list(stock_data[field].tail(count))
                return []
            if isinstance(stock_data, pd.Series):
                if field in stock_data.index:
                    return _to_float_list([stock_data[field]])
                return _to_float_list(stock_data.tail(count))
            if isinstance(stock_data, dict):
                return _to_float_list(stock_data.get(field, []))[-count:]
            return _to_float_list(stock_data)[-count:]

        if field in raw_data and isinstance(raw_data[field], dict):
            return _to_float_list(raw_data[field].get(stock, []))[-count:]

    if isinstance(raw_data, pd.DataFrame):
        frame = raw_data
        if isinstance(frame.columns, pd.MultiIndex):
            for col in frame.columns:
                if stock in col and field in col:
                    return _to_float_list(frame[col].tail(count))
        if field in frame.columns:
            if "code" in frame.columns:
                sub = frame[frame["code"] == stock]
                if not sub.empty:
                    return _to_float_list(sub[field].tail(count))
            return _to_float_list(frame[field].tail(count))

    return []


def _history(ContextInfo, stock_list, count, field, end_time=None):
    stocks = list(dict.fromkeys([stock for stock in stock_list if stock]))
    if not stocks:
        return {}

    aliases = FIELD_ALIASES.get(field, [field])
    end_time = end_time or g._selection_end_time or _bar_time(ContextInfo)

    try:
        raw_data = ContextInfo.get_market_data_ex(
            aliases,
            stocks,
            period="1d",
            end_time=end_time,
            count=count,
            dividend_type="front",
            fill_data=True,
            subscribe=False,
        )
    except Exception as exc:
        print("获取行情失败 fields={} count={}: {}".format(aliases, count, exc))
        return {}

    for qmt_field in aliases:
        parsed = {}
        for stock in stocks:
            values = _extract_market_series(raw_data, stock, qmt_field, count)
            if values:
                parsed[stock] = values[-count:]
        if parsed:
            return parsed

    return {}


def _latest(history_map, stock, default=np.nan):
    values = history_map.get(stock, [])
    if not values:
        return default
    return values[-1]


def _previous(history_map, stock, default=np.nan):
    values = history_map.get(stock, [])
    if len(values) >= 2:
        return values[-2]
    if values:
        return values[-1]
    return default


def _last_price(ContextInfo, stock, field=None):
    price_field = field or g.order_price_field
    price_data = _history(ContextInfo, [stock], 1, price_field)
    price = _latest(price_data, stock)
    if not np.isfinite(price) or price <= 0:
        close_data = _history(ContextInfo, [stock], 1, "close")
        price = _latest(close_data, stock)
    return price


def _stock_name(ContextInfo, stock):
    detail = _instrument_detail(ContextInfo, stock)
    if detail:
        name = detail.get("InstrumentName")
        if name:
            return str(name)
    return stock


def _sync_trade_state(ContextInfo):
    if "get_trade_detail_data" not in globals():
        return

    try:
        accounts = get_trade_detail_data(g.accountID, "stock", "account")
        if accounts:
            available = accounts[0].m_dAvailable
            if available is not None:
                g.money = float(available)
    except Exception:
        pass

    try:
        positions = get_trade_detail_data(g.accountID, "stock", "position")
    except Exception:
        return

    if not positions:
        return

    synced = {stock: 0 for stock in g.holdings.keys()}
    for pos in positions:
        code = pos.m_strInstrumentID
        exchange = pos.m_strExchangeID
        volume = pos.m_nVolume
        if not code:
            continue
        stock = code if "." in code else "{}.{}".format(code, exchange)
        synced[stock] = int(volume or 0)
        cost = pos.m_dOpenPrice
        if cost is not None and synced[stock] > 0:
            g.buypoint[stock] = float(cost)

    g.holdings.update(synced)


# ==============================================================================
# 每日准备：更新持仓/涨停/禁购列表
# ==============================================================================
def prepare_stock_list(ContextInfo):
    _sync_trade_state(ContextInfo)
    g.hold_list = _held_stocks()

    g.not_buy_again = []
    if len(g.history_hold_list) >= g.limit_days:
        g.history_hold_list = g.history_hold_list[-g.limit_days:]

    g.yesterday_HL_list = []
    if not g.hold_list:
        return

    close_data = _history(ContextInfo, g.hold_list, 2, "close")
    high_limit_data = _history(ContextInfo, g.hold_list, 2, "high_limit")
    if not high_limit_data:
        return

    for code in g.hold_list:
        close_price = _previous(close_data, code)
        high_limit = _previous(high_limit_data, code)
        if _is_limit_price(close_price, high_limit):
            g.yesterday_HL_list.append(code)


# ==============================================================================
# 核心调仓：每周第二个交易日执行
# ==============================================================================
def weekly_adjustment(ContextInfo):
    if not is_nth_trading_day_of_week(ContextInfo, 2):
        return

    cur_date = _date_to_yyyymmdd(g._current_date)
    print("=" * 60)
    print("【调仓日】{}".format(cur_date))

    g.target_list = get_stock_list(ContextInfo)
    if not g.target_list:
        print("选股结果为空, 跳过本次调仓")
        return

    target_list = g.target_list[:g.stock_num]
    print("目标持仓 {} 只: {}".format(len(target_list), target_list))

    for stock in list(g.hold_list):
        if stock not in target_list and stock not in g.yesterday_HL_list:
            close_position(ContextInfo, stock)

    buy_security(ContextInfo, target_list)
    print("=" * 60)


# ==============================================================================
# 止损检查
# ==============================================================================
def check_stop_loss(ContextInfo):
    if not g.run_stoploss:
        return

    idx_stocks = _get_sector_stocks(ContextInfo, g.index_code)
    close_data = _history(ContextInfo, idx_stocks, 2, "close")
    ratios = []
    for stock in idx_stocks:
        values = close_data.get(stock, [])
        if len(values) >= 2 and values[0] > 0:
            ratios.append(values[-1] / values[0])

    if ratios:
        down_ratio = float(np.nanmean(ratios))
        if down_ratio <= 1 - g.stoploss_market:
            print("【市场止损】指数池跌幅 {:.1%}, 全仓清仓".format(1 - down_ratio))
            for stock in list(g.holdings.keys()):
                if g.holdings.get(stock, 0) > 0:
                    close_position(ContextInfo, stock)
            return

    for stock, shares in list(g.holdings.items()):
        if shares <= 0:
            continue
        price = _last_price(ContextInfo, stock, "close")
        cost = g.buypoint.get(stock, price)
        if np.isfinite(price) and price < cost * (1 - g.stoploss_limit):
            close_position(ContextInfo, stock)
            print("【个股止损】{} ({}) 跌破成本价 {:.1%}".format(
                stock, _stock_name(ContextInfo, stock), g.stoploss_limit
            ))
            _mark_sell_reason(stock, "stoploss")


# ==============================================================================
# 下午交易：处理涨停打开 + 补仓
# ==============================================================================
def trade_afternoon(ContextInfo):
    check_limit_up(ContextInfo)
    check_remain_amount(ContextInfo)


def check_limit_up(ContextInfo):
    if not g.yesterday_HL_list:
        return

    close_data = _history(ContextInfo, g.yesterday_HL_list, 1, "close")
    high_limit_data = _history(ContextInfo, g.yesterday_HL_list, 1, "high_limit")
    if not high_limit_data:
        return

    for stock in g.yesterday_HL_list:
        if g.holdings.get(stock, 0) <= 0:
            continue
        last_price = _latest(close_data, stock)
        high_limit = _latest(high_limit_data, stock)
        if np.isfinite(last_price) and np.isfinite(high_limit) and last_price < high_limit:
            close_position(ContextInfo, stock)
            print("【涨停打开】卖出 {} ({})".format(stock, _stock_name(ContextInfo, stock)))
            _mark_sell_reason(stock, "limitup")

    g.history_hold_list.extend(g.not_buy_again)


def check_remain_amount(ContextInfo):
    if g.reason_to_sell in ["limitup", "stoploss"]:
        g.hold_list = _held_stocks()
        if len(g.hold_list) < g.stock_num and g.target_list:
            target_list = filter_not_buy_again(ContextInfo, g.target_list)
            buy_security(ContextInfo, target_list)
        g.reason_to_sell = ""


# ==============================================================================
# 核心选股：小市值初筛 + Barra 量价因子评分排序
# ==============================================================================
def get_stock_list(ContextInfo):
    g._selection_end_time = _previous_bar_end_time(ContextInfo)
    try:
        return _get_stock_list_impl(ContextInfo)
    finally:
        g._selection_end_time = None


def _get_stock_list_impl(ContextInfo):
    initial_list = _get_sector_stocks(ContextInfo, g.index_code)
    if not initial_list:
        return []

    base_date = g._previous_trade_date or g._current_date or _get_current_date(ContextInfo)
    cutoff_date = base_date - datetime.timedelta(days=375)
    initial_list = filter_listed_before(ContextInfo, initial_list, cutoff_date)
    if not initial_list:
        return []

    filters = [
        filter_kcbj_stock,
        filter_st_stock,
        filter_paused_stock,
        filter_limitup_stock,
        filter_limitdown_stock,
        filter_highprice_stock,
    ]

    for func in filters:
        initial_list = func(ContextInfo, initial_list)
        if not initial_list:
            return []

    try:
        money_data = _history(ContextInfo, initial_list, g.liquidity_days, "money")
        avg_amount = {}
        for stock in initial_list:
            values = np.array(money_data.get(stock, []), dtype=float)
            if len(values) >= g.liquidity_days:
                avg_amount[stock] = np.nanmean(values)
        if avg_amount:
            initial_list = [
                stock for stock in initial_list
                if avg_amount.get(stock, 0) >= g.min_avg_amount
            ]
    except Exception as exc:
        print("流动性过滤失败: {}".format(exc))
    if not initial_list:
        return []

    try:
        close_data = _history(ContextInfo, initial_list, g.momentum_days, "close")
        momentum = {}
        for stock in initial_list:
            close = np.array(close_data.get(stock, []), dtype=float)
            close = close[np.isfinite(close)]
            if len(close) >= 2 and close[0] > 0:
                momentum[stock] = (close[-1] - close[0]) / close[0]
        if momentum:
            initial_list = [
                stock for stock in initial_list
                if g.filter_threshold1 < momentum.get(stock, np.nan) < g.filter_threshold2
            ]
    except Exception as exc:
        print("动量过滤失败: {}".format(exc))
    if not initial_list:
        return []

    cap_df = _get_market_cap_df(ContextInfo, initial_list)
    market_cap_map = {}
    if cap_df is not None and not cap_df.empty and cap_df["market_cap"].notna().any():
        cap_df = cap_df[
            (cap_df["market_cap"] >= g.min_market_cap)
            & (cap_df["market_cap"] <= g.max_market_cap)
        ].sort_values("market_cap")
        if cap_df.empty:
            return []
        candidate_list = cap_df["code"].tolist()
        cap_for_turnover = cap_df["circ_market_cap"] if "circ_market_cap" in cap_df.columns else cap_df["market_cap"]
        market_cap_map = dict(zip(cap_df["code"], cap_for_turnover.fillna(cap_df["market_cap"])))
    else:
        print("市值筛选失败")
        return []

    print("候选股票数量: {}, 开始计算 Barra 因子...".format(len(candidate_list)))
    factor_df = calc_barra_factors(ContextInfo, candidate_list, market_cap_map)

    if factor_df is None or factor_df.empty:
        print("Barra 因子计算失败, 退化为市值/原始顺序排序")
        return candidate_list[:g.stock_num]

    factor_df = calc_composite_score(factor_df)
    factor_df = factor_df.sort_values("composite_score", ascending=True)
    result = factor_df["code"].tolist()

    print("因子选股完成, Top5: {}".format(result[:5]))
    return result


def _get_market_cap_df(ContextInfo, stock_list):
    override = g.market_cap_map
    close_data = _history(ContextInfo, stock_list, 1, "close")
    total_value_data = _history(ContextInfo, stock_list, 1, "total_value")
    rows = []

    for stock in stock_list:
        if stock in override:
            market_cap = _normalize_market_cap_to_yi(override[stock])
            rows.append({"code": stock, "market_cap": market_cap, "circ_market_cap": market_cap})
            continue

        close_price = _latest(close_data, stock)
        detail = _instrument_detail(ContextInfo, stock)
        total_share = _normalize_share_count(detail.get("TotalVolume") if detail else None)
        float_share = _normalize_share_count(detail.get("FloatVolume") if detail else None)

        market_cap = np.nan
        circ_market_cap = np.nan
        if np.isfinite(close_price) and close_price > 0:
            if np.isfinite(total_share) and total_share > 0:
                market_cap = close_price * total_share / 1e8
            if np.isfinite(float_share) and float_share > 0:
                circ_market_cap = close_price * float_share / 1e8

        if not np.isfinite(market_cap):
            total_value = _latest(total_value_data, stock)
            if np.isfinite(total_value) and total_value > 0:
                market_cap = _normalize_market_value_to_yuan([total_value])[-1] / 1e8

        if not np.isfinite(circ_market_cap):
            circ_market_cap = market_cap
        rows.append({
            "code": stock,
            "market_cap": market_cap,
            "circ_market_cap": circ_market_cap,
        })

    return pd.DataFrame(rows)


def _normalize_share_count(value):
    value = _extract_scalar(value, "")
    if value is None:
        return np.nan
    try:
        value = float(value)
    except Exception:
        return np.nan
    if not np.isfinite(value) or value <= 0:
        return np.nan
    if value < 1e6:
        return value * 10000
    return value


def _extract_scalar(value, field):
    if value is None:
        return None
    if isinstance(value, (int, float, np.integer, np.floating)):
        return float(value)
    if isinstance(value, pd.Series):
        if field in value and pd.notna(value[field]):
            return float(value[field])
        numeric = value.dropna()
        if not numeric.empty:
            return float(numeric.iloc[-1])
    if isinstance(value, pd.DataFrame):
        if field in value.columns:
            series = value[field].dropna()
            if not series.empty:
                return float(series.iloc[-1])
        numeric = value.select_dtypes(include=[np.number])
        if not numeric.empty:
            return float(numeric.iloc[-1, -1])
    if isinstance(value, dict):
        if field in value:
            return _extract_scalar(value[field], field)
        for item in reversed(list(value.values())):
            result = _extract_scalar(item, field)
            if result is not None:
                return result
    if isinstance(value, (list, tuple)) and value:
        for item in reversed(value):
            result = _extract_scalar(item, field)
            if result is not None:
                return result
    return None


def _normalize_market_cap_to_yi(value):
    try:
        value = float(value)
    except Exception:
        return np.nan
    if not np.isfinite(value) or value <= 0:
        return np.nan
    if value > 1e8:
        return value / 1e8
    if value > 1e4:
        return value / 1e4
    return value


def _normalize_market_value_to_yuan(values):
    arr = np.array(values, dtype=float)
    finite = arr[np.isfinite(arr) & (arr > 0)]
    if len(finite) == 0:
        return arr
    median = float(np.nanmedian(finite))
    if median < 1e4:
        return arr * 1e8
    if median < 1e8:
        return arr * 1e4
    return arr


# ==============================================================================
# Barra 量价因子计算
# ==============================================================================
def calc_barra_factors(ContextInfo, stock_list, market_cap_map=None):
    if not stock_list:
        return None

    market_cap_map = market_cap_map or {}
    need_days = g.vol_window + 10

    close_data = _history(ContextInfo, stock_list, need_days, "close")
    volume_data = _history(ContextInfo, stock_list, need_days, "volume")
    money_data = _history(ContextInfo, stock_list, need_days, "money")
    total_value_data = _history(ContextInfo, stock_list, need_days, "total_value")

    mkt_close = _history(ContextInfo, [g.benchmark], need_days, "close").get(
        g.benchmark, []
    )
    if len(mkt_close) >= 2:
        mkt_close = np.array(mkt_close, dtype=float)
        mkt_ret = np.diff(mkt_close) / mkt_close[:-1]
    else:
        mkt_ret = None

    records = []
    for code in stock_list:
        try:
            close = np.array(close_data.get(code, []), dtype=float)
            volume = np.array(volume_data.get(code, []), dtype=float)
            money = np.array(money_data.get(code, []), dtype=float)
            total_value = np.array(total_value_data.get(code, []), dtype=float)

            if len(close) < 63:
                continue
            if len(volume) != len(close):
                volume = np.full(len(close), np.nan)
            if len(money) != len(close):
                money = volume * close

            finite_total_value = total_value[np.isfinite(total_value)]
            if (
                len(total_value) != len(close)
                or len(finite_total_value) == 0
                or np.nanmax(finite_total_value) <= 0
            ):
                cap_yi = market_cap_map.get(code, np.nan)
                if np.isfinite(cap_yi) and cap_yi > 0:
                    total_value = np.full(len(close), cap_yi * 1e8)
                else:
                    total_value = np.full(len(close), np.nan)
            else:
                total_value = _normalize_market_value_to_yuan(total_value)

            valid_mask = np.isfinite(close) & (close > 0)
            if valid_mask.sum() < 63:
                continue
            close = close[valid_mask]
            volume = volume[valid_mask]
            money = money[valid_mask]
            total_value = total_value[valid_mask]

            ret = np.diff(close) / close[:-1]
            if len(ret) < 62:
                continue

            row = {"code": code}
            row["HSIGMA"] = _calc_hsigma(ret, mkt_ret, g.vol_window, g.vol_halflife)
            row["DASTD"] = _calc_dastd(ret, g.vol_window, g.dastd_halflife)
            row["CMRA"] = _calc_cmra(close, g.cmra_months)
            row["STOM"] = _calc_stom(volume, total_value, g.stom_window)
            row["STOQ"] = _calc_stoq(volume, total_value, g.stoq_window)
            row["STOA"] = _calc_stoa(volume, total_value, g.stoa_window)
            row["ATVR"] = _calc_atvr(volume, total_value, g.vol_window, g.atvr_halflife)
            records.append(row)
        except Exception:
            continue

    if not records:
        return None

    df = pd.DataFrame(records)
    print("成功计算因子的股票数量: {}".format(len(df)))
    return df


def _exp_weights(n, halflife):
    lam = np.log(2) / halflife
    w = np.exp(-lam * np.arange(n - 1, -1, -1))
    return w / w.sum()


def _calc_hsigma(ret, mkt_ret, window, halflife):
    try:
        n = min(window, len(ret))
        r = ret[-n:]
        if mkt_ret is None or len(mkt_ret) < n:
            return float(np.std(r))
        m = mkt_ret[-n:]
        w = _exp_weights(n, halflife)
        w_sqrt = np.sqrt(w)
        X = np.column_stack([np.ones(n), m]) * w_sqrt[:, None]
        y = r * w_sqrt
        coef, *_ = np.linalg.lstsq(X, y, rcond=None)
        resid = r - (coef[0] + coef[1] * m)
        sigma = np.sqrt(np.sum(w * resid ** 2))
        return float(sigma)
    except Exception:
        return np.nan


def _calc_dastd(ret, window, halflife):
    try:
        n = min(window, len(ret))
        r = ret[-n:]
        w = _exp_weights(n, halflife)
        mean_r = np.sum(w * r)
        sigma = np.sqrt(np.sum(w * (r - mean_r) ** 2))
        return float(sigma)
    except Exception:
        return np.nan


def _calc_cmra(close, months):
    try:
        total_needed = months * TRADING_DAYS_PER_MONTH + 1
        if len(close) < total_needed:
            return np.nan
        c = close[-total_needed:]
        cum_log_ret = []
        for t in range(1, months + 1):
            end_idx = len(c) - 1
            start_idx = len(c) - 1 - t * TRADING_DAYS_PER_MONTH
            if start_idx < 0:
                break
            cum_r = np.log(c[end_idx] / c[start_idx])
            cum_log_ret.append(cum_r)
        if not cum_log_ret:
            return np.nan
        return float(max(cum_log_ret) - min(cum_log_ret))
    except Exception:
        return np.nan


def _calc_stom(turnover_amount, total_value, window):
    try:
        n = min(window, len(turnover_amount), len(total_value))
        amount = turnover_amount[-n:]
        tv = total_value[-n:]
        tv_safe = np.where(tv > 0, tv, np.nan)
        daily_to = amount / tv_safe
        total_to = np.nansum(daily_to)
        if total_to <= 0:
            return np.nan
        return float(np.log(total_to))
    except Exception:
        return np.nan


def _calc_stoq(turnover_amount, total_value, window):
    try:
        return _calc_average_monthly_stom(turnover_amount, total_value, window, 3)
    except Exception:
        return np.nan


def _calc_stoa(turnover_amount, total_value, window):
    try:
        return _calc_average_monthly_stom(turnover_amount, total_value, window, 12)
    except Exception:
        return np.nan


def _calc_average_monthly_stom(turnover_amount, total_value, window, periods):
    stom_list = []
    for i in range(periods):
        start = -window + i * TRADING_DAYS_PER_MONTH
        end = -window + (i + 1) * TRADING_DAYS_PER_MONTH
        if end == 0:
            amount_seg = turnover_amount[start:]
            tv_seg = total_value[start:]
        else:
            amount_seg = turnover_amount[start:end]
            tv_seg = total_value[start:end]
        if len(amount_seg) == 0:
            continue
        stom_i = _calc_stom(amount_seg, tv_seg, TRADING_DAYS_PER_MONTH)
        if not np.isnan(stom_i):
            stom_list.append(stom_i)
    if not stom_list:
        return np.nan
    return float(np.log(np.mean(np.exp(stom_list))))


def _calc_atvr(turnover_amount, total_value, window, halflife):
    try:
        n = min(window, len(turnover_amount), len(total_value))
        amount = turnover_amount[-n:]
        tv = total_value[-n:]
        tv_safe = np.where(tv > 0, tv, np.nan)
        daily_to = amount / tv_safe
        w = _exp_weights(n, halflife)
        atvr = np.nansum(w * daily_to)
        return float(atvr)
    except Exception:
        return np.nan


# ==============================================================================
# 因子标准化 + 合成评分
# ==============================================================================
def _mad_winsorize(series):
    med = series.median()
    mad = (series - med).abs().median()
    upper = med + 3 * mad
    lower = med - 3 * mad
    return series.clip(lower=lower, upper=upper)


def _zscore(series):
    mu = series.mean()
    std = series.std()
    if std == 0 or np.isnan(std):
        return series * 0
    return (series - mu) / std


def calc_composite_score(df):
    factor_cols = list(g.factor_weights.keys())
    df = df.copy()

    for col in factor_cols:
        if col not in df.columns:
            continue
        valid_mask = df[col].notna()
        if valid_mask.sum() < 5:
            df[col + "_z"] = 0.0
            continue
        series = df.loc[valid_mask, col].copy()
        series = _mad_winsorize(series)
        series = _zscore(series)
        df.loc[valid_mask, col + "_z"] = series
        df.loc[~valid_mask, col + "_z"] = 0.0

    z_cols = [col + "_z" for col in factor_cols if col + "_z" in df.columns]
    if not z_cols:
        df["composite_score"] = 0.0
    else:
        weights = np.array([
            g.factor_weights.get(c.replace("_z", ""), 1 / len(z_cols))
            for c in z_cols
        ])
        weights = weights / weights.sum()
        df["composite_score"] = df[z_cols].values @ weights

    return df


# ==============================================================================
# 过滤函数集合
# ==============================================================================
def filter_listed_before(ContextInfo, stock_list, cutoff_date):
    result = []
    for stock in stock_list:
        detail = _instrument_detail(ContextInfo, stock)
        if not detail:
            result.append(stock)
            continue
        open_date = _parse_qmt_date(detail.get("OpenDate"))
        if open_date is None or open_date <= cutoff_date:
            result.append(stock)
    return result


def _parse_qmt_date(value):
    if value in (None, "", 0, "0", "19700101", "99999999"):
        return None
    try:
        return datetime.datetime.strptime(str(value)[:8], "%Y%m%d").date()
    except Exception:
        return None


def filter_paused_stock(ContextInfo, stock_list):
    suspend_data = _history(ContextInfo, stock_list, 1, "suspend")
    close_data = _history(ContextInfo, stock_list, 1, "close")
    volume_data = _history(ContextInfo, stock_list, 1, "volume")
    result = []
    for stock in stock_list:
        detail = _instrument_detail(ContextInfo, stock)
        if _is_suspended_detail(detail):
            continue

        suspend_flag = _latest(suspend_data, stock, default=np.nan)
        if np.isfinite(suspend_flag) and int(suspend_flag) == 1:
            continue
        close_price = _latest(close_data, stock)
        volume = _latest(volume_data, stock, default=np.nan)
        if np.isfinite(close_price) and close_price > 0:
            if not np.isfinite(volume) or volume > 0:
                result.append(stock)
    return result


def filter_st_stock(ContextInfo, stock_list):
    result = []
    for stock in stock_list:
        name = _stock_name(ContextInfo, stock)
        if "ST" in name or "*" in name or "退" in name:
            continue
        result.append(stock)
    return result


def filter_kcbj_stock(ContextInfo, stock_list):
    del ContextInfo
    return [
        stock for stock in stock_list
        if not stock[:6].startswith(("4", "8", "688", "300"))
    ]


def _filter_limit_stock(ContextInfo, stock_list, limit_field):
    if not stock_list:
        return []
    close_data = _history(ContextInfo, stock_list, 1, "close")
    limit_data = _history(ContextInfo, stock_list, 1, limit_field)
    if not limit_data:
        return stock_list
    result = []
    hold_set = set(g.hold_list)
    for stock in stock_list:
        close_price = _latest(close_data, stock)
        limit_price = _latest(limit_data, stock)
        if stock in hold_set or not _is_limit_price(close_price, limit_price):
            result.append(stock)
    return result


def filter_limitup_stock(ContextInfo, stock_list):
    return _filter_limit_stock(ContextInfo, stock_list, "high_limit")


def filter_limitdown_stock(ContextInfo, stock_list):
    return _filter_limit_stock(ContextInfo, stock_list, "low_limit")


def filter_highprice_stock(ContextInfo, stock_list):
    if not stock_list:
        return []
    close_data = _history(ContextInfo, stock_list, 1, "close")
    return [
        stock for stock in stock_list
        if stock in g.hold_list
        or _latest(close_data, stock, default=np.inf) <= g.up_price
    ]


def filter_not_buy_again(ContextInfo, stock_list):
    temp_set = set(g.history_hold_list)
    return [stock for stock in stock_list if stock not in temp_set]


# ==============================================================================
# 交易辅助函数
# ==============================================================================
def _calc_order_cost(value, is_sell=False):
    commission = max(abs(value) * g.commission_rate, g.min_commission)
    tax = abs(value) * g.close_tax_rate if is_sell else 0.0
    return commission + tax


def _round_lot_by_value(value, price):
    return int(value / price / LOT_SIZE) * LOT_SIZE


def order_target_value_(ContextInfo, security, value):
    price = _last_price(ContextInfo, security)
    if not np.isfinite(price) or price <= 0:
        return False

    current_shares = int(g.holdings.get(security, 0))
    current_value = current_shares * price
    delta_value = float(value) - current_value

    if abs(delta_value) < price * LOT_SIZE:
        return False

    if delta_value > 0:
        shares = _round_lot_by_value(delta_value, price)
        if shares < LOT_SIZE:
            return False
        order_value = shares * price
        cost = _calc_order_cost(order_value, is_sell=False)
        if g.money < order_value + cost:
            shares = _round_lot_by_value(g.money - cost, price)
            order_value = shares * price
            if shares < LOT_SIZE:
                return False
            cost = _calc_order_cost(order_value, is_sell=False)
        order_shares(security, shares, "FIX", price, ContextInfo, g.accountID)
        g.money -= order_value + cost
        g.profit -= cost
        g.holdings[security] = current_shares + shares
        g.buypoint[security] = price
        print("买入 {}({}) {}股 {:.0f}元".format(
            security, _stock_name(ContextInfo, security), shares, order_value
        ))
        return True

    shares = min(current_shares, _round_lot_by_value(abs(delta_value), price))
    if value == 0:
        shares = current_shares
    if shares <= 0:
        return False
    order_value = shares * price
    cost = _calc_order_cost(order_value, is_sell=True)
    order_shares(security, -shares, "FIX", price, ContextInfo, g.accountID)
    buy_price = g.buypoint.get(security, price)
    g.money += order_value - cost
    g.profit += (price - buy_price) * shares - cost
    g.holdings[security] = current_shares - shares
    if g.holdings[security] <= 0:
        g.holdings[security] = 0
        g.buypoint.pop(security, None)
    print("卖出 {}({}) {}股 {:.0f}元".format(
        security, _stock_name(ContextInfo, security), shares, order_value
    ))
    return True


def open_position(ContextInfo, security, value):
    return order_target_value_(ContextInfo, security, value)


def close_position(ContextInfo, security):
    return order_target_value_(ContextInfo, security, 0)


def buy_security(ContextInfo, target_list):
    current_hold = set(_held_stocks())
    to_buy = [
        stock for stock in target_list
        if stock not in current_hold and stock not in g.history_hold_list
    ]
    if not to_buy or g.money <= 0:
        return

    max_buy = g.stock_num - len(current_hold)
    if max_buy <= 0:
        return
    to_buy = to_buy[:max_buy]
    cash_per = g.money / len(to_buy)

    for stock in to_buy:
        open_position(ContextInfo, stock, cash_per)
