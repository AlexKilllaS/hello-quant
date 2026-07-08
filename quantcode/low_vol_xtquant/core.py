# coding: utf-8
"""
XtQuant / MiniQMT 版：低波动小市值

原始策略来自聚宽：小市值 + Barra CNE6 量价因子。
本文件是独立 Python 脚本，面向 MiniQMT 客户端外部运行：
1. xtdata 负责板块、合约、历史行情和实时全推行情。
2. XtQuantTrader 负责账户、持仓查询和股票委托。
3. 策略主体沿用内置 Python 版本的选股、风控和调仓语义。

默认 DRY_RUN=True，只打印拟委托；确认账户、行情和候选池正常后，再用
--live-trade 或环境变量 XTQUANT_LIVE_TRADE=1 打开真实委托。
"""

import argparse
import datetime
import os
import random
import sys
import time
from decimal import Decimal, ROUND_HALF_UP

import numpy as np
import pandas as pd

try:
    from . import factors as factor_module
except ImportError:
    import factors as factor_module

sys.path.insert(0, r"C:\Users\alex\Documents\qmt_lib\xtquant_250807")

try:
    from xtquant import xtconstant, xtdata
    from xtquant.xttrader import XtQuantTrader, XtQuantTraderCallback
    from xtquant.xttype import StockAccount
except ImportError:
    xtconstant = None
    xtdata = None
    XtQuantTrader = None
    StockAccount = None

    class XtQuantTraderCallback(object):
        pass


USERDATA_MINI_PATH = os.environ.get("XTQUANT_USERDATA_MINI", r"")
ACCOUNT_ID = os.environ.get("XTQUANT_ACCOUNT_ID", "")
ACCOUNT_TYPE = os.environ.get("XTQUANT_ACCOUNT_TYPE", "STOCK")
SESSION_ID = int(os.environ.get("XTQUANT_SESSION_ID", "0") or "0")
DRY_RUN = os.environ.get("XTQUANT_LIVE_TRADE", "0").lower() not in ("1", "true", "yes", "y")
DEFAULT_INITIAL_CASH = float(os.environ.get("XTQUANT_INITIAL_CASH", "1000000") or "1000000")
DEFAULT_DIVIDEND_TYPE = "front"


class StrategyConfig(object):
    def __init__(
        self,
        userdata_mini_path,
        account_id,
        account_type="STOCK",
        session_id=0,
        dry_run=True,
        initial_cash=DEFAULT_INITIAL_CASH,
        period="1d",
        dividend_type=DEFAULT_DIVIDEND_TYPE,
        universe_index_code="399101.SZ",
        factor_benchmark="000300.SH",
        report_benchmark="000300.SH",
        strategy_name="低波动小市值_xtquant",
        price_type="latest",
        auto_download_history=True,
        download_batch_size=300,
        sleep_seconds=20,
    ):
        self.userdata_mini_path = userdata_mini_path
        self.account_id = account_id
        self.account_type = account_type or "STOCK"
        self.session_id = int(session_id or 0)
        self.dry_run = bool(dry_run)
        self.initial_cash = float(initial_cash or DEFAULT_INITIAL_CASH)
        self.period = period or "1d"
        self.dividend_type = dividend_type or "front"
        self.universe_index_code = universe_index_code
        self.factor_benchmark = factor_benchmark
        self.report_benchmark = report_benchmark
        self.strategy_name = strategy_name
        self.price_type = price_type
        self.auto_download_history = bool(auto_download_history)
        self.download_batch_size = int(download_batch_size or 300)
        self.sleep_seconds = int(sleep_seconds or 20)


def _ensure_xtquant():
    if xtdata is None or XtQuantTrader is None or StockAccount is None:
        raise RuntimeError(
            "未找到 xtquant 库。请在安装了 MiniQMT/xtquant 的 Python 环境中运行本脚本。"
        )


def _numeric_attr(obj, names, default=np.nan):
    for name in names:
        if hasattr(obj, name):
            value = getattr(obj, name)
            try:
                if value is not None:
                    return float(value)
            except Exception:
                pass
    return default


def _text_attr(obj, names, default=""):
    for name in names:
        if hasattr(obj, name):
            value = getattr(obj, name)
            if value not in (None, ""):
                return str(value)
    return default


def _normalize_xt_market_data(raw_data, field_list, stock_list):
    if not isinstance(raw_data, dict) or not field_list:
        return raw_data
    if not all(field in raw_data and isinstance(raw_data[field], pd.DataFrame) for field in field_list):
        return raw_data

    result = {}
    for stock in stock_list:
        columns = {}
        index = None
        for field in field_list:
            frame = raw_data.get(field)
            if frame is None or frame.empty:
                continue
            if stock in frame.index:
                series = frame.loc[stock]
            elif stock in frame.columns:
                series = frame[stock]
            else:
                continue
            if index is None:
                index = series.index
            columns[field] = pd.to_numeric(series, errors="coerce")
        if columns:
            result[stock] = pd.DataFrame(columns, index=index)
    return result


class XtQuantContext(object):
    xtquant_mode = True

    def __init__(self, config, trader=None, account=None):
        self.config = config
        self.trader = trader
        self.account = account
        self.do_back_test = False
        self.benchmark = config.report_benchmark
        self.dividend_type = config.dividend_type
        self.period = config.period
        self._download_cache = set()
        self._sector_data_checked = False
        self.refresh_clock()
        self.capital = self._initial_capital()

    def refresh_clock(self):
        self.now = datetime.datetime.now()
        self.barpos = int(time.time())

    def _initial_capital(self):
        asset = self.query_stock_asset()
        total_asset = _numeric_attr(asset, ("total_asset", "asset", "m_dBalance"), np.nan)
        cash = _numeric_attr(asset, ("cash", "available_cash", "enable_balance", "m_dAvailable"), np.nan)
        for value in (total_asset, cash, self.config.initial_cash):
            if np.isfinite(value) and value > 0:
                return float(value)
        return float(DEFAULT_INITIAL_CASH)

    def get_stock_list_in_sector(self, sector_name):
        _ensure_xtquant()
        stocks = xtdata.get_stock_list_in_sector(sector_name) or []
        if stocks:
            return stocks
        if not self._sector_data_checked and hasattr(xtdata, "download_sector_data"):
            self._sector_data_checked = True
            try:
                xtdata.download_sector_data()
                stocks = xtdata.get_stock_list_in_sector(sector_name) or []
            except Exception as exc:
                print("下载板块分类信息失败: {}".format(exc))
        return stocks

    def get_instrumentdetail(self, stock):
        _ensure_xtquant()
        try:
            return xtdata.get_instrument_detail(stock, False)
        except TypeError:
            return xtdata.get_instrument_detail(stock)

    def get_market_data_ex(
        self,
        field_list,
        stock_list,
        period="1d",
        start_time="",
        end_time="",
        count=-1,
        dividend_type="none",
        fill_data=True,
        subscribe=False,
    ):
        del subscribe
        _ensure_xtquant()
        stocks = list(dict.fromkeys([stock for stock in stock_list if stock]))
        self._download_history(stocks, period, end_time)

        getter = getattr(xtdata, "get_market_data_ex", None)
        if getter is not None:
            try:
                raw = getter(
                    field_list,
                    stocks,
                    period=period,
                    start_time=start_time,
                    end_time=end_time,
                    count=count,
                    dividend_type=dividend_type,
                    fill_data=fill_data,
                )
                return _normalize_xt_market_data(raw, field_list, stocks)
            except TypeError:
                pass

        raw = xtdata.get_market_data(
            field_list=field_list,
            stock_list=stocks,
            period=period,
            start_time=start_time,
            end_time=end_time,
            count=count,
            dividend_type=dividend_type,
            fill_data=fill_data,
        )
        return _normalize_xt_market_data(raw, field_list, stocks)

    def _download_history(self, stock_list, period, end_time):
        if not self.config.auto_download_history or not stock_list:
            return
        end_key = str(end_time or self.now.strftime("%Y%m%d"))[:8]
        missing = [
            stock for stock in stock_list
            if (stock, period, end_key) not in self._download_cache
        ]
        if not missing:
            return

        batch_size = max(int(self.config.download_batch_size), 1)
        for start in range(0, len(missing), batch_size):
            batch = missing[start:start + batch_size]
            try:
                if hasattr(xtdata, "download_history_data2"):
                    xtdata.download_history_data2(
                        batch,
                        period,
                        start_time="",
                        end_time=end_time or "",
                        callback=None,
                        incrementally=True,
                    )
                else:
                    for stock in batch:
                        xtdata.download_history_data(stock, period, end_time=end_time or "", incrementally=True)
                for stock in batch:
                    self._download_cache.add((stock, period, end_key))
            except Exception as exc:
                print("下载历史行情失败 period={} stocks={}: {}".format(
                    period, _stock_sample(batch), exc
                ))

    def get_full_tick(self, code_list):
        _ensure_xtquant()
        return xtdata.get_full_tick(code_list)

    def query_stock_asset(self):
        if self.trader is None or self.account is None:
            return None
        try:
            return self.trader.query_stock_asset(self.account)
        except Exception as exc:
            print("xtquant 资产查询失败: {}".format(exc))
            return None

    def query_stock_positions(self):
        if self.trader is None or self.account is None:
            return []
        try:
            return self.trader.query_stock_positions(self.account) or []
        except Exception as exc:
            print("xtquant 持仓查询失败: {}".format(exc))
            return []

    def order_stock(self, stock, order_type, shares, price_type, price, remark):
        if self.trader is None or self.account is None:
            raise RuntimeError("未连接交易账号，无法下单")
        return self.trader.order_stock(
            self.account,
            stock,
            order_type,
            shares,
            price_type,
            price,
            self.config.strategy_name,
            remark,
        )

    def previous_trading_date(self, current_date):
        _ensure_xtquant()
        start = (current_date - datetime.timedelta(days=30)).strftime("%Y%m%d")
        end = current_date.strftime("%Y%m%d")
        dates = xtdata.get_trading_calendar("SH", start, end) or []
        parsed = [_parse_xt_trading_date(value) for value in dates]
        parsed = [value for value in parsed if value is not None and value < current_date]
        return parsed[-1] if parsed else current_date

    def is_trading_day(self, current_date):
        _ensure_xtquant()
        day = current_date.strftime("%Y%m%d")
        dates = xtdata.get_trading_calendar("SH", day, day) or []
        return any(_parse_xt_trading_date(value) == current_date for value in dates)

    def paint(self, *args, **kwargs):
        del args, kwargs

    def draw_text(self, *args, **kwargs):
        del args, kwargs

    def draw_vertline(self, *args, **kwargs):
        del args, kwargs


class StrategyTraderCallback(XtQuantTraderCallback):
    def on_disconnected(self):
        print("xtquant 交易连接断开")

    def on_stock_order(self, order):
        print("委托回报: {} status={} order_id={}".format(
            _text_attr(order, ("stock_code", "stock_code1"), ""),
            _text_attr(order, ("order_status",), ""),
            _text_attr(order, ("order_id", "order_sysid"), ""),
        ))

    def on_stock_trade(self, trade):
        print("成交回报: {} {}股 price={}".format(
            _text_attr(trade, ("stock_code", "stock_code1"), ""),
            _text_attr(trade, ("traded_volume", "volume"), ""),
            _text_attr(trade, ("traded_price", "price"), ""),
        ))

    def on_order_error(self, order_error):
        print("委托失败: order_id={} error_id={} msg={}".format(
            _text_attr(order_error, ("order_id",), ""),
            _text_attr(order_error, ("error_id",), ""),
            _text_attr(order_error, ("error_msg",), ""),
        ))


def _parse_xt_trading_date(value):
    text = str(value).replace("-", "")[:8]
    try:
        return datetime.datetime.strptime(text, "%Y%m%d").date()
    except Exception:
        return None


class G:
    pass


g = G()

LOT_SIZE = 100
TRADING_DAYS_PER_MONTH = 21
LIMIT_PRICE_MIN_DIFF = 0.01
LIMIT_PRICE_REL_DIFF = 0.0001
FACTOR_NAMES = ("HSIGMA", "DASTD", "CMRA", "STOM", "STOQ", "STOA", "ATVR")

FIELD_MAP = {
    "close": "close",
    "open": "open",
    "high": "high",
    "low": "low",
    "volume": "volume",
    "money": "amount",
    "pre_close": "preClose",
    "suspend": "suspendFlag",
}

INDEX_SECTOR_NAME_MAP = {
    "000300.SH": "沪深300",
    "000905.SH": "中证500",
    "000016.SH": "上证50",
    "399101.SZ": "中小综指",
}


def init(ContextInfo):
    _init_platform_config(ContextInfo)
    _init_strategy_params()
    _init_runtime_state()
    _init_universe_and_portfolio(ContextInfo)

    print("策略初始化完成, 股票池数量: {}, 因子权重: {}".format(
        len(g.universe), {k: round(v, 3) for k, v in g.factor_weights.items()}
    ))


def _init_platform_config(ContextInfo):
    if getattr(ContextInfo, "xtquant_mode", False):
        cfg = ContextInfo.config
        g.is_backtest = False
        g.universe_index_code = cfg.universe_index_code
        g.factor_benchmark = cfg.factor_benchmark
        g.report_benchmark = cfg.report_benchmark
        g.accountID = cfg.account_id
        g.account_type = cfg.account_type
        g.price_dividend_type = cfg.dividend_type
        g.ui_dividend_type = cfg.dividend_type
        g.subscribe_market_data = True
        g.xt_dry_run = bool(cfg.dry_run)
        g.xt_price_type = cfg.price_type
        g.xt_strategy_name = cfg.strategy_name
        return

    # QMT 界面配置负责驱动回测和绩效展示；策略内部变量负责选股和因子语义。
    g.is_backtest = bool(ContextInfo.do_back_test)
    g.universe_index_code = "399101.SZ"
    g.factor_benchmark = "000300.SH"
    g.report_benchmark = ContextInfo.benchmark
    g.accountID = "testS"
    g.account_type = "stock"
    g.price_dividend_type = DEFAULT_DIVIDEND_TYPE
    g.ui_dividend_type = ContextInfo.dividend_type
    g.subscribe_market_data = False


def _init_strategy_params():
    # -------- 股票池过滤参数 --------
    g.stock_num = 20
    g.up_price = 150

    g.momentum_days = 30
    g.filter_threshold1 = -0.3
    g.filter_threshold2 = 0.3

    g.liquidity_days = 20
    g.min_avg_amount = 8e7
    g.min_daily_amount = 3e7

    g.min_market_cap = 10
    g.max_market_cap = 80
    g.min_circ_market_cap = 8

    # -------- Barra 因子参数 --------
    g.vol_window = 252
    g.vol_halflife = 63
    g.dastd_halflife = 42
    g.cmra_months = 12
    g.stom_window = 21
    g.stoq_window = 63
    g.stoa_window = 252
    g.atvr_halflife = 63

    g.factor_weights = {
        "HSIGMA": 0.25,
        "DASTD": 0.20,
        "CMRA": 0.15,
        "STOM": 0.12,
        "STOQ": 0.10,
        "STOA": 0.10,
        "ATVR": 0.08,
    }

    # -------- 调仓与仓位控制参数 --------
    g.buffer_multiplier = 2.0
    g.rebalance_out_buffer_ratio = 0.30
    g.rebalance_min_new_targets = 5
    g.weight_drift_threshold = 0.30
    g.risk_weight_window = 60
    g.max_position_weight = 0.08

    # -------- 市场状态与组合仓位参数 --------
    g.enable_market_timing = True
    g.timing_index_code = g.universe_index_code
    g.timing_short_ma = 60
    g.timing_long_ma = 250
    g.timing_full_exposure = 1.00
    g.timing_mid_exposure = 0.50
    g.timing_low_exposure = 0.30
    g.timing_exposure_drift_threshold = 0.05

    # -------- 风控参数 --------
    g.stoploss_limit = 0.07
    g.stoploss_market = 0.05
    g.limit_days = 5
    g.run_stoploss = False


def _init_runtime_state():
    # -------- QMT 回测状态 --------
    g._current_date = None
    g._previous_trade_date = None
    g._last_processed_bar = None
    g._selection_end_time = None
    g._last_rebalance_date = None
    g._last_rebalance_month = None
    g._task_run_dates = {}
    g._day_schedule_date = None
    g._daily_history_ready_date = None
    g._daily_history_warned_date = None
    g._diagnostic_log_keys = set()
    g._instrument_detail_cache = {}
    g.enable_timing_log = False
    g.enable_chart_diagnostics = True
    g.diag = {}
    g._diag_event_value = 0.0
    g._diag_event_text = ""
    g._diag_buy_count = 0
    g._diag_sell_count = 0
    g.target_exposure = 1.0
    g._market_timing_state = ""


def _init_universe_and_portfolio(ContextInfo):
    g.universe = _get_sector_stocks(ContextInfo, g.universe_index_code)
    print("策略初始化: 因子基准 {}, QMT报告基准 {}, 股票池 {} 只".format(
        g.factor_benchmark, g.report_benchmark, len(g.universe)
    ))
    print("价格复权口径: 策略固定 {}, QMT界面 {}".format(
        g.price_dividend_type, g.ui_dividend_type
    ))

    g.holdings = {stock: 0 for stock in g.universe}
    g.buypoint = {}
    g.hold_list = []
    g.yesterday_HL_list = []
    g.target_list = []
    g.not_buy_again = []
    g.history_hold_list = []
    g.reason_to_sell = ""
    g.market_cap_map = {}

    g.initial_cash = float(ContextInfo.capital)
    if not np.isfinite(g.initial_cash) or g.initial_cash <= 0:
        raise RuntimeError("QMT 回测本金 ContextInfo.capital 无效: {}".format(ContextInfo.capital))
    g.money = float(g.initial_cash)
    g.profit = 0.0
    g.profit_ratio = 0.0
    g.commission_rate = 2.5 / 10000
    g.close_tax_rate = 0.001
    g.min_commission = 5.0
    g.order_price_field = "open"

    g.min_bars = max(
        g.vol_window + 10,
        (g.timing_long_ma + 1) if g.enable_market_timing else 0,
    )


def handlebar(ContextInfo):
    if not ContextInfo.is_new_bar():
        return
    if not g.is_backtest and not ContextInfo.is_last_bar():
        return

    _update_calendar_state(ContextInfo)

    d = ContextInfo.barpos
    if g._last_processed_bar == d:
        return

    g._last_processed_bar = d
    _reset_bar_diagnostics()
    if _is_intraday_period(ContextInfo):
        _run_intraday_schedule(ContextInfo)
    else:
        _run_day_schedule(ContextInfo)

    g.profit_ratio = g.profit / g.initial_cash
    _paint_diagnostics(ContextInfo)


# ==============================================================================
# 日历与 QMT 兼容工具
# ==============================================================================
def _get_current_date(ContextInfo):
    return _bar_datetime(ContextInfo).date()


def _parse_bar_datetime(value):
    if isinstance(value, datetime.datetime):
        return value
    if isinstance(value, datetime.date):
        return datetime.datetime.combine(value, datetime.time())
    text = str(value).strip()
    if not text:
        return None
    text = text.replace("-", "").replace(":", "").replace(" ", "")
    try:
        if len(text) >= 14:
            return datetime.datetime.strptime(text[:14], "%Y%m%d%H%M%S")
        if len(text) >= 8:
            return datetime.datetime.strptime(text[:8], "%Y%m%d")
    except Exception:
        return None
    return None


def _bar_datetime(ContextInfo):
    if getattr(ContextInfo, "xtquant_mode", False):
        ContextInfo.refresh_clock()
        return ContextInfo.now

    try:
        tag = ContextInfo.get_bar_timetag(ContextInfo.barpos)
        value = timetag_to_datetime(tag, "%Y%m%d%H%M%S")
        parsed = _parse_bar_datetime(value)
        if parsed is not None:
            return parsed
        raise RuntimeError("解析 QMT bar 时间失败: barpos={} timetag={}".format(ContextInfo.barpos, tag))
    except Exception as exc:
        raise RuntimeError("读取 QMT bar 时间失败: barpos={}: {}".format(ContextInfo.barpos, exc))


def _bar_time(ContextInfo, fmt="%Y%m%d%H%M%S"):
    return _bar_datetime(ContextInfo).strftime(fmt)


def _previous_bar_end_time(ContextInfo):
    if getattr(ContextInfo, "xtquant_mode", False):
        current_date = g._current_date or _get_current_date(ContextInfo)
        trade_date = ContextInfo.previous_trading_date(current_date)
        return _date_to_yyyymmdd(trade_date) + "150000"

    trade_date = g._previous_trade_date or _get_current_date(ContextInfo)
    return _date_to_yyyymmdd(trade_date) + "150000"


def _update_calendar_state(ContextInfo):
    current_date = _get_current_date(ContextInfo)
    if g._current_date == current_date:
        return

    g._previous_trade_date = g._current_date
    g._current_date = current_date


def _current_period(ContextInfo):
    if ContextInfo.period in (None, ""):
        raise RuntimeError("QMT ContextInfo.period 为空, 请检查回测周期设置")
    return str(ContextInfo.period)


def _is_intraday_period(ContextInfo):
    period = _current_period(ContextInfo).lower()
    if period in ("1d", "day", "d", "1w", "week", "1mon", "1month", "month"):
        return False
    return "m" in period or "min" in period or period == "tick"


def _task_done(task_name, current_date):
    return g._task_run_dates.get(task_name) == current_date


def _mark_task_done(task_name, current_date):
    g._task_run_dates[task_name] = current_date


def _run_task_once(ContextInfo, task_name, trigger_time, func):
    now = _bar_datetime(ContextInfo)
    current_date = now.date()
    if _task_done(task_name, current_date):
        return
    if now.strftime("%H:%M") < trigger_time:
        return
    print("【调度】{} {} 执行 {}".format(_date_to_yyyymmdd(current_date), now.strftime("%H:%M"), task_name))
    func(ContextInfo)
    _mark_task_done(task_name, current_date)


def _run_intraday_schedule(ContextInfo):
    # 聚宽日内任务在 QMT 回测中用 handlebar + bar 时间门控模拟。
    tasks = (
        ("prepare_stock_list", "09:25", prepare_stock_list),
        ("risk_buffer_rebalance", "10:00", rebalance_check),
        ("check_stop_loss_1030", "10:30", check_stop_loss),
        ("check_stop_loss_1400", "14:00", check_stop_loss),
        ("trade_afternoon", "14:30", trade_afternoon),
    )
    for task_name, trigger_time, func in tasks:
        _run_task_once(ContextInfo, task_name, trigger_time, func)


def _run_day_schedule(ContextInfo):
    current_date = g._current_date or _get_current_date(ContextInfo)
    if g._day_schedule_date == current_date:
        return
    g._day_schedule_date = current_date
    prepare_stock_list(ContextInfo)
    rebalance_check(ContextInfo)
    check_stop_loss(ContextInfo)
    trade_afternoon(ContextInfo)


def _daily_history_ready(ContextInfo):
    current_date = g._current_date or _get_current_date(ContextInfo)
    if g._daily_history_ready_date == current_date:
        return True

    probes = [g.factor_benchmark]
    if g.enable_market_timing:
        probes.append(g.timing_index_code)
    probes.extend(g.universe[:3])
    probes = list(dict.fromkeys([stock for stock in probes if stock]))
    if not probes:
        return ContextInfo.barpos >= g.min_bars

    close_data = _history(
        ContextInfo,
        probes,
        g.min_bars,
        "close",
        end_time=_previous_bar_end_time(ContextInfo),
        period="1d",
    )
    max_len = 0
    for stock in probes:
        max_len = max(max_len, len(close_data.get(stock, [])))
    if max_len >= g.min_bars:
        g._daily_history_ready_date = current_date
        return True

    if g._daily_history_warned_date != current_date:
        print("日线历史不足: 需要 {} 根, 当前最多 {} 根, 暂不调仓".format(g.min_bars, max_len))
        g._daily_history_warned_date = current_date
    return False


def _date_to_yyyymmdd(date_value):
    if isinstance(date_value, datetime.datetime):
        date_value = date_value.date()
    if isinstance(date_value, datetime.date):
        return date_value.strftime("%Y%m%d")
    return str(date_value)[:8]


def _instrument_detail(ContextInfo, stock):
    if stock in g._instrument_detail_cache:
        return g._instrument_detail_cache[stock]

    try:
        detail = ContextInfo.get_instrumentdetail(stock)
    except Exception as exc:
        _log_once(
            "instrument-detail-error-{}".format(stock),
            "获取合约详情失败 {}: {}".format(stock, exc),
        )
        g._instrument_detail_cache[stock] = None
        return None
    if isinstance(detail, dict) and detail:
        g._instrument_detail_cache[stock] = detail
        return detail
    _log_once("instrument-detail-empty-{}".format(stock), "合约详情为空 {}".format(stock))
    g._instrument_detail_cache[stock] = None
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


def _round_price_to_cent(value):
    if not np.isfinite(value):
        return np.nan
    return float(Decimal(str(value)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


def _is_supported_limit_rule_stock(stock):
    code = str(stock).split(".")[0]
    return not code.startswith(("30", "688", "4", "8"))


def _calc_limit_price(stock, pre_close, direction):
    if not np.isfinite(pre_close) or pre_close <= 0:
        return np.nan
    if not _is_supported_limit_rule_stock(stock):
        raise RuntimeError(
            "涨跌停价计算只支持当前策略保留的沪深主板普通股票, 不支持 {}".format(stock)
        )
    ratio = 0.10
    if direction == "up":
        return _round_price_to_cent(pre_close * (1 + ratio))
    if direction == "down":
        return _round_price_to_cent(pre_close * (1 - ratio))
    raise RuntimeError("未知涨跌停方向: {}".format(direction))


def _limit_history(ContextInfo, stock_list, count, direction, end_time=None):
    pre_close_data = _history(
        ContextInfo,
        stock_list,
        count,
        "pre_close",
        end_time=end_time,
        dividend_type="none",
    )
    if not pre_close_data:
        raise RuntimeError("涨跌停价计算失败: QMT preClose 字段无数据")

    result = {}
    missing = []
    for stock in stock_list:
        values = pre_close_data.get(stock, [])
        limit_values = []
        for pre_close in values[-count:]:
            limit_price = _calc_limit_price(stock, pre_close, direction)
            if np.isfinite(limit_price):
                limit_values.append(limit_price)
        if limit_values:
            result[stock] = limit_values[-count:]
        else:
            missing.append(stock)

    if missing:
        print("涨跌停价计算: {} 只股票 preClose 无效, 示例 {}".format(
            len(missing), _stock_sample(missing)
        ))
    return result


def _held_stocks():
    return [stock for stock, shares in g.holdings.items() if shares > 0]


def _mark_sell_reason(stock, reason):
    g.reason_to_sell = reason
    g.not_buy_again.append(stock)


def _stock_sample(stock_list, limit=5):
    stocks = list(stock_list or [])
    suffix = "..." if len(stocks) > limit else ""
    return "{}{}".format(stocks[:limit], suffix)


def _log_once(key, message):
    if key in g._diagnostic_log_keys:
        return
    print(message)
    g._diagnostic_log_keys.add(key)


def _timer_start():
    return time.time() if g.enable_timing_log else None


def _elapsed_seconds(started_at):
    if started_at is None:
        return None
    return time.time() - started_at


def _log_elapsed(label, started_at):
    elapsed = _elapsed_seconds(started_at)
    if elapsed is None:
        return
    print("{} {:.2f}s".format(label, elapsed))


# ==============================================================================
# 副图诊断指标
# ==============================================================================
def _diag_set(name, value):
    try:
        value = float(value)
    except Exception:
        value = np.nan
    g.diag[name] = value


def _diag_get(name, default=0.0):
    value = g.diag.get(name, default)
    try:
        value = float(value)
    except Exception:
        return default
    if not np.isfinite(value):
        return default
    return value


def _diag_pct(numerator, denominator):
    try:
        numerator = float(numerator)
        denominator = float(denominator)
    except Exception:
        return 0.0
    if not np.isfinite(numerator) or not np.isfinite(denominator) or denominator <= 0:
        return 0.0
    return max(min(numerator / denominator * 100.0, 120.0), 0.0)


def _diag_stage(stage_name, count):
    _diag_set(stage_name, count)


def _reset_selection_diagnostics():
    for key in (
        "pool_initial",
        "after_basic_filters",
        "after_liquidity",
        "after_momentum",
        "after_market_cap",
        "candidate_count",
        "factor_count",
        "ranked_count",
    ):
        _diag_set(key, 0)


def _reset_bar_diagnostics():
    g._diag_event_value = 0.0
    g._diag_event_text = ""
    g._diag_buy_count = 0
    g._diag_sell_count = 0


def _mark_diag_event(value, text):
    if value >= g._diag_event_value:
        g._diag_event_value = float(value)
        g._diag_event_text = text


def _paint_line(ContextInfo, name, value, color="white", line_style=0):
    ContextInfo.paint(name, float(value), -1, line_style, color)


def _paint_diagnostics(ContextInfo):
    if not g.enable_chart_diagnostics:
        return

    initial_count = _diag_get("pool_initial", len(g.universe))
    basic_count = _diag_get("after_basic_filters", initial_count)
    liquidity_count = _diag_get("after_liquidity", basic_count)
    market_cap_count = _diag_get("after_market_cap", liquidity_count)
    factor_count = _diag_get("factor_count", 0)
    candidate_count = _diag_get("candidate_count", market_cap_count)

    hold_count = len(_held_stocks())
    cash_pct = _diag_pct(g.money, g.initial_cash)
    new_target_pct = _diag_pct(_diag_get("new_target_count", 0), g.stock_num)

    _paint_line(ContextInfo, "D1基础过滤%", _diag_pct(basic_count, initial_count), "cyan")
    _paint_line(ContextInfo, "D2流动性%", _diag_pct(liquidity_count, initial_count), "yellow")
    _paint_line(ContextInfo, "D3市值池%", _diag_pct(market_cap_count, initial_count), "magenta")
    _paint_line(ContextInfo, "D4因子有效%", _diag_pct(factor_count, candidate_count), "green")
    _paint_line(ContextInfo, "D5持仓完成%", _diag_pct(hold_count, g.stock_num), "white")
    _paint_line(ContextInfo, "D6现金比例%", min(max(cash_pct, 0.0), 120.0), "blue")
    _paint_line(ContextInfo, "D7新目标%", new_target_pct, "brown")
    _paint_line(ContextInfo, "D8事件", g._diag_event_value, "red", 42)
    _paint_line(
        ContextInfo,
        "D9目标仓位%",
        min(max(float(g.target_exposure) * 100.0, 0.0), 120.0),
        "yellow",
    )

    if g._diag_buy_count or g._diag_sell_count:
        ContextInfo.draw_text(
            True,
            115,
            "买{}卖{}".format(g._diag_buy_count, g._diag_sell_count),
        )
    elif g._diag_event_text:
        ContextInfo.draw_text(True, 110, g._diag_event_text)

    if g._diag_event_value >= 70:
        ContextInfo.draw_vertline(True, 0, 120, "red", "noaxis")


def _dedupe_stocks(stock_list):
    return list(dict.fromkeys([stock for stock in stock_list if stock]))


def _get_sector_stocks(ContextInfo, sector_code):
    sector_name = INDEX_SECTOR_NAME_MAP[sector_code]
    stocks = ContextInfo.get_stock_list_in_sector(sector_name)
    stocks = _dedupe_stocks(stocks)
    if not stocks:
        raise RuntimeError("板块 {} 未返回成分股, 请检查 MiniQMT 客户端板块/本地数据".format(sector_name))
    print("股票池 {} 使用板块 {}, 成分 {} 只".format(sector_code, sector_name, len(stocks)))
    return stocks


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


def _history(ContextInfo, stock_list, count, field, end_time=None, period="1d", dividend_type=None):
    stocks = list(dict.fromkeys([stock for stock in stock_list if stock]))
    if not stocks:
        return {}

    if field not in FIELD_MAP:
        raise RuntimeError("未配置 QMT 行情字段映射: {}".format(field))
    qmt_field = FIELD_MAP[field]
    if end_time is None or end_time == "":
        end_time = g._selection_end_time or _bar_time(ContextInfo)
    if dividend_type is None or dividend_type == "":
        dividend_type = g.price_dividend_type

    try:
        raw_data = ContextInfo.get_market_data_ex(
            [qmt_field],
            stocks,
            period=period,
            end_time=end_time,
            count=count,
            dividend_type=dividend_type,
            fill_data=True,
            subscribe=False,
        )
    except Exception as exc:
        print("获取行情失败 field={} qmt_field={} period={} end_time={} count={} stocks={}: {}".format(
            field, qmt_field, period, end_time, count, _stock_sample(stocks), exc
        ))
        return {}

    parsed = {}
    for stock in stocks:
        values = _extract_market_series(raw_data, stock, qmt_field, count)
        if values:
            parsed[stock] = values[-count:]

    if not parsed:
        _log_once(
            "history-empty-{}-{}-{}-{}".format(field, qmt_field, period, end_time),
            "行情字段无数据 field={} qmt_field={} period={} end_time={} count={} stocks={}".format(
                field, qmt_field, period, end_time, count, _stock_sample(stocks)
            ),
        )
    elif len(parsed) < len(stocks):
        missing = [stock for stock in stocks if stock not in parsed]
        _log_once(
            "history-partial-{}-{}-{}-{}".format(field, qmt_field, period, end_time),
            "行情字段部分缺失 field={} qmt_field={} period={} end_time={} 缺失 {}/{} 示例={}".format(
                field, qmt_field, period, end_time, len(missing), len(stocks), _stock_sample(missing)
            ),
        )
    return parsed


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


def _last_price(ContextInfo, stock, field=None, dividend_type=None):
    price_field = field or g.order_price_field
    period = _current_period(ContextInfo) if _is_intraday_period(ContextInfo) else "1d"
    price_data = _history(
        ContextInfo,
        [stock],
        1,
        price_field,
        end_time=_bar_time(ContextInfo),
        period=period,
        dividend_type=dividend_type,
    )
    price = _latest(price_data, stock)
    return price


def _stock_name(ContextInfo, stock):
    detail = _instrument_detail(ContextInfo, stock)
    if detail:
        name = detail.get("InstrumentName")
        if name:
            return str(name)
    return stock


def _sync_xtquant_trade_state(ContextInfo):
    asset = ContextInfo.query_stock_asset()
    if asset is not None:
        available = _numeric_attr(
            asset,
            ("cash", "available_cash", "enable_balance", "m_dAvailable"),
            np.nan,
        )
        if np.isfinite(available):
            g.money = float(available)
    else:
        _log_once("xt-account-empty", "xtquant 资产查询为空, 使用本地资金状态")

    positions = ContextInfo.query_stock_positions()
    if not positions:
        _log_once("xt-position-empty", "xtquant 持仓查询为空, 使用本地持仓状态")
        return

    synced = {stock: 0 for stock in g.holdings.keys()}
    for pos in positions:
        stock = _text_attr(pos, ("stock_code", "stock_code1", "m_strInstrumentID"), "")
        if not stock:
            code = _text_attr(pos, ("instrument_id",), "")
            market = _text_attr(pos, ("market", "exchange_id", "m_strExchangeID"), "")
            stock = "{}.{}".format(code, market) if code and market else code
        if not stock:
            continue

        volume = _numeric_attr(pos, ("volume", "m_nVolume"), np.nan)
        if not np.isfinite(volume):
            _log_once("xt-position-volume-missing-{}".format(stock), "持仓同步跳过: {} volume 为空".format(stock))
            continue
        synced[stock] = int(volume)

        cost = _numeric_attr(pos, ("avg_price", "open_price", "m_dOpenPrice"), np.nan)
        if np.isfinite(cost) and synced[stock] > 0:
            g.buypoint[stock] = float(cost)

    g.holdings.update(synced)


def _sync_trade_state(ContextInfo):
    if getattr(ContextInfo, "xtquant_mode", False):
        _sync_xtquant_trade_state(ContextInfo)
        return

    if "get_trade_detail_data" not in globals():
        _log_once("trade-detail-missing", "未发现 get_trade_detail_data, 使用脚本本地持仓/资金状态")
        return

    try:
        accounts = get_trade_detail_data(g.accountID, g.account_type, "account")
        if accounts:
            available = accounts[0].m_dAvailable
            if available is not None:
                g.money = float(available)
        else:
            _log_once("account-empty", "账户查询为空 account={} type={}".format(g.accountID, g.account_type))
    except Exception as exc:
        _log_once(
            "account-query-error",
            "账户资金同步失败 account={} type={}: {}".format(g.accountID, g.account_type, exc),
        )

    try:
        positions = get_trade_detail_data(g.accountID, g.account_type, "position")
    except Exception as exc:
        _log_once(
            "position-query-error",
            "持仓同步失败 account={} type={}: {}".format(g.accountID, g.account_type, exc),
        )
        return

    if not positions:
        _log_once("position-empty", "持仓查询为空 account={} type={}".format(g.accountID, g.account_type))
        return

    synced = {stock: 0 for stock in g.holdings.keys()}
    for pos in positions:
        code = pos.m_strInstrumentID
        exchange = pos.m_strExchangeID
        volume = pos.m_nVolume
        if not code:
            continue
        if volume is None:
            _log_once(
                "position-volume-missing-{}-{}".format(code, exchange),
                "持仓同步跳过: {}.{} m_nVolume 为空".format(code, exchange),
            )
            continue
        stock = code if "." in code else "{}.{}".format(code, exchange)
        synced[stock] = int(volume)
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

    close_data = _history(ContextInfo, g.hold_list, 2, "close", dividend_type="none")
    high_limit_data = _limit_history(ContextInfo, g.hold_list, 2, "up")
    if not close_data:
        raise RuntimeError("昨日涨停列表计算失败: QMT close 字段无数据")

    for code in g.hold_list:
        close_price = _previous(close_data, code)
        high_limit = _previous(high_limit_data, code)
        if _is_limit_price(close_price, high_limit):
            g.yesterday_HL_list.append(code)


# ==============================================================================
# 核心调仓：每日检查，按缓冲池/权重漂移/月度校准触发
# ==============================================================================
def _current_month_key():
    if isinstance(g._current_date, datetime.datetime):
        current_date = g._current_date.date()
    else:
        current_date = g._current_date
    if isinstance(current_date, datetime.date):
        return current_date.year, current_date.month
    return None


def _is_monthly_force_rebalance():
    month_key = _current_month_key()
    return month_key is not None and month_key != g._last_rebalance_month


def _position_value(ContextInfo, stock):
    shares = int(g.holdings.get(stock, 0))
    if shares <= 0:
        return 0.0
    price = _last_price(ContextInfo, stock)
    if not np.isfinite(price) or price <= 0:
        _log_once(
            "position-price-invalid-{}".format(stock),
            "持仓估值跳过: {} 无有效 {} 价格".format(stock, g.order_price_field),
        )
        return 0.0
    return shares * price


def _portfolio_total_value(ContextInfo, extra_stocks=None):
    total = float(g.money)
    stocks = set(_held_stocks())
    if extra_stocks:
        stocks.update(extra_stocks)
    for stock in stocks:
        total += _position_value(ContextInfo, stock)
    return total


def _format_number(value, digits=2):
    if value is None:
        return "--"
    try:
        value = float(value)
    except Exception:
        return "--"
    if not np.isfinite(value):
        return "--"
    return "{:.{}f}".format(value, digits)


def _format_pct_text(value):
    if value is None:
        return "--"
    try:
        value = float(value)
    except Exception:
        return "--"
    if not np.isfinite(value):
        return "--"
    return "{:.2%}".format(value)


def _print_current_positions(ContextInfo):
    hold_stocks = _held_stocks()
    if not hold_stocks:
        print("当前持仓: 空仓, 可用资金 {:.2f}".format(g.money))
        return

    rows = []
    stock_value = 0.0
    for stock in hold_stocks:
        shares = int(g.holdings.get(stock, 0))
        price = _last_price(ContextInfo, stock, "close")
        value = shares * price if np.isfinite(price) and price > 0 else np.nan
        cost = g.buypoint.get(stock)
        try:
            cost = float(cost)
        except Exception:
            cost = np.nan
        pnl_ratio = None
        if np.isfinite(price) and price > 0 and np.isfinite(cost) and cost > 0:
            pnl_ratio = price / cost - 1
        if np.isfinite(value):
            stock_value += value
        rows.append({
            "stock": stock,
            "name": _stock_name(ContextInfo, stock),
            "shares": shares,
            "price": price,
            "value": value,
            "cost": cost,
            "pnl_ratio": pnl_ratio,
        })

    total_value = stock_value + float(g.money)
    print("当前持仓: {} 只, 股票市值 {:.2f}, 可用资金 {:.2f}, 总资产估算 {:.2f}".format(
        len(hold_stocks), stock_value, g.money, total_value
    ))

    rows = sorted(rows, key=lambda row: row["value"] if np.isfinite(row["value"]) else 0.0, reverse=True)
    for row in rows:
        weight = row["value"] / total_value if total_value > 0 and np.isfinite(row["value"]) else None
        print("  {} {} {}股 现价 {} 市值 {} 权重 {} 成本 {} 盈亏 {}".format(
            row["stock"],
            row["name"],
            row["shares"],
            _format_number(row["price"]),
            _format_number(row["value"], 0),
            _format_pct_text(weight),
            _format_number(row["cost"]),
            _format_pct_text(row["pnl_ratio"]),
        ))


def _calc_market_timing_exposure(ContextInfo):
    if not g.enable_market_timing:
        g.target_exposure = float(g.timing_full_exposure)
        g._market_timing_state = "择时关闭"
        _diag_set("target_exposure_pct", g.target_exposure * 100.0)
        return g.target_exposure, g._market_timing_state

    count = max(g.timing_short_ma, g.timing_long_ma) + 1
    close_data = _history(
        ContextInfo,
        [g.timing_index_code],
        count,
        "close",
        end_time=_previous_bar_end_time(ContextInfo),
        period="1d",
    )
    values = np.array(close_data.get(g.timing_index_code, []), dtype=float)
    values = values[np.isfinite(values) & (values > 0)]
    if len(values) < g.timing_long_ma:
        raise RuntimeError(
            "市场状态过滤失败: {} close 历史不足, 需要 {}, 当前 {}".format(
                g.timing_index_code, g.timing_long_ma, len(values)
            )
        )

    latest = values[-1]
    short_ma = float(np.nanmean(values[-g.timing_short_ma:]))
    long_ma = float(np.nanmean(values[-g.timing_long_ma:]))
    if not np.isfinite(short_ma) or not np.isfinite(long_ma) or short_ma <= 0 or long_ma <= 0:
        raise RuntimeError("市场状态过滤失败: 均线计算无效")

    if latest < long_ma:
        exposure = float(g.timing_low_exposure)
        state = "跌破{}日线".format(g.timing_long_ma)
    elif latest < short_ma:
        exposure = float(g.timing_mid_exposure)
        state = "跌破{}日线".format(g.timing_short_ma)
    else:
        exposure = float(g.timing_full_exposure)
        state = "趋势正常"

    g.target_exposure = max(min(exposure, 1.0), 0.0)
    g._market_timing_state = state
    _diag_set("target_exposure_pct", g.target_exposure * 100.0)
    print("市场状态过滤: {} close={} MA{}={} MA{}={} 目标仓位 {:.0%} ({})".format(
        g.timing_index_code,
        _format_number(latest),
        g.timing_short_ma,
        _format_number(short_ma),
        g.timing_long_ma,
        _format_number(long_ma),
        g.target_exposure,
        state,
    ))
    return g.target_exposure, state


def _current_equity_exposure(ContextInfo):
    total_value = _portfolio_total_value(ContextInfo)
    if total_value <= 0:
        return 0.0
    stock_value = max(total_value - float(g.money), 0.0)
    exposure = stock_value / total_value
    _diag_set("current_exposure_pct", exposure * 100.0)
    return exposure


def _exposure_drift_too_large(ContextInfo):
    if not g.enable_market_timing:
        return False
    current_exposure = _current_equity_exposure(ContextInfo)
    return abs(current_exposure - float(g.target_exposure)) >= g.timing_exposure_drift_threshold


def _cap_and_normalize_weights(raw_weights):
    scores = {
        stock: max(float(score), 0.0)
        for stock, score in raw_weights.items()
        if np.isfinite(score) and score > 0
    }
    if not scores:
        return {}

    max_weight = max(float(g.max_position_weight), 1.0 / len(scores))
    weights = {}
    remaining = set(scores.keys())
    remaining_weight = 1.0

    while remaining and remaining_weight > 0:
        total_score = sum(scores[stock] for stock in remaining)
        if total_score <= 0:
            equal_weight = remaining_weight / len(remaining)
            for stock in remaining:
                weights[stock] = equal_weight
            break

        capped = []
        for stock in remaining:
            weight = remaining_weight * scores[stock] / total_score
            if weight > max_weight:
                capped.append(stock)

        if not capped:
            for stock in remaining:
                weights[stock] = remaining_weight * scores[stock] / total_score
            break

        for stock in capped:
            weights[stock] = max_weight
        remaining_weight -= max_weight * len(capped)
        remaining = remaining.difference(capped)

    total_weight = sum(weights.values())
    if total_weight <= 0:
        equal_weight = 1.0 / len(scores)
        return {stock: equal_weight for stock in scores}
    return {stock: weight / total_weight for stock, weight in weights.items()}


def _calc_inverse_vol_weights(ContextInfo, target_list):
    target_list = list(dict.fromkeys(target_list[:g.stock_num]))
    if not target_list:
        return {}

    close_data = _history(
        ContextInfo,
        target_list,
        g.risk_weight_window + 1,
        "close",
        end_time=_previous_bar_end_time(ContextInfo),
        period="1d",
    )
    if not close_data:
        raise RuntimeError("风险预算权重失败: QMT close 字段无数据")

    scores = {}
    invalid = []
    for stock in target_list:
        values = np.array(close_data.get(stock, []), dtype=float)
        values = values[np.isfinite(values) & (values > 0)]
        if len(values) < 20:
            invalid.append((stock, "有效 close 不足"))
            continue

        returns = np.diff(values) / values[:-1]
        returns = returns[np.isfinite(returns)]
        vol = np.nanstd(returns) if len(returns) else np.nan
        if not np.isfinite(vol) or vol <= 0:
            invalid.append((stock, "波动率无效"))
        else:
            scores[stock] = 1.0 / vol
    if invalid:
        print("风险预算权重: 剔除 {} 只无效股票, 示例 {}".format(len(invalid), invalid[:5]))
    if not scores:
        raise RuntimeError("风险预算权重失败: 目标池无有效波动率样本")

    weights = _cap_and_normalize_weights(scores)
    if weights:
        print("风险预算权重: {}".format({
            stock: round(weight, 4) for stock, weight in weights.items()
        }))
    return weights


def _weight_drift_too_large(ContextInfo, target_weights):
    current_hold = set(_held_stocks())
    overlap = current_hold.intersection(target_weights.keys())
    if not overlap:
        return False

    total_value = _portfolio_total_value(ContextInfo, target_weights.keys())
    if total_value <= 0:
        return False

    for stock in overlap:
        target_weight = target_weights.get(stock, 0.0) * float(g.target_exposure)
        if target_weight <= 0:
            continue
        current_weight = _position_value(ContextInfo, stock) / total_value
        drift = abs(current_weight - target_weight) / target_weight
        if drift >= g.weight_drift_threshold:
            return True
    return False


def _should_rebalance(ContextInfo, ranked_list, target_weights):
    current_hold = set(_held_stocks())
    target_set = set(ranked_list[:g.stock_num])
    buffer_size = min(
        len(ranked_list),
        max(g.stock_num, int(np.ceil(g.stock_num * g.buffer_multiplier))),
    )
    buffer_set = set(ranked_list[:buffer_size])

    if not current_hold and target_weights:
        return True, "当前空仓建仓"

    if len(current_hold) < min(g.stock_num, len(target_set)):
        return True, "持仓数量不足"

    if _exposure_drift_too_large(ContextInfo):
        return True, "目标仓位切换至 {:.0%}".format(g.target_exposure)

    if _is_monthly_force_rebalance():
        return True, "月度强制校准"

    out_of_buffer = [stock for stock in current_hold if stock not in buffer_set]
    out_threshold = max(2, int(np.ceil(g.stock_num * g.rebalance_out_buffer_ratio)))
    if len(out_of_buffer) >= out_threshold:
        return True, "{} 只持仓跌出前 {} 名缓冲池".format(len(out_of_buffer), buffer_size)

    new_targets = [stock for stock in target_set if stock not in current_hold]
    if len(new_targets) >= g.rebalance_min_new_targets:
        return True, "{} 只新股票进入目标池".format(len(new_targets))

    if _weight_drift_too_large(ContextInfo, target_weights):
        return True, "持仓权重偏离超过 {:.0%}".format(g.weight_drift_threshold)

    return False, "持仓仍在缓冲池内，且权重偏离未超阈值"


def _target_position_values(ContextInfo, target_weights):
    target_set = set(target_weights.keys())
    locked_value = 0.0
    for stock in _held_stocks():
        if stock not in target_set and stock in g.yesterday_HL_list:
            locked_value += _position_value(ContextInfo, stock)

    total_value = _portfolio_total_value(ContextInfo, target_set)
    target_capital = max(total_value * float(g.target_exposure) - locked_value, 0.0)
    return {
        stock: target_capital * weight
        for stock, weight in target_weights.items()
    }


def _rebalance_to_weights(ContextInfo, target_weights, sell_unmatched=True, allow_reduce=True):
    if not target_weights:
        return

    target_set = set(target_weights.keys())
    if sell_unmatched:
        for stock in list(_held_stocks()):
            if stock not in target_set and stock not in g.yesterday_HL_list:
                close_position(ContextInfo, stock)

    target_values = _target_position_values(ContextInfo, target_weights)

    if allow_reduce:
        for stock, target_value in target_values.items():
            if _position_value(ContextInfo, stock) > target_value:
                order_target_value_(ContextInfo, stock, target_value)

        target_values = _target_position_values(ContextInfo, target_weights)

    for stock, target_value in target_values.items():
        if stock in g.history_hold_list and int(g.holdings.get(stock, 0)) <= 0:
            continue
        if _position_value(ContextInfo, stock) < target_value:
            order_target_value_(ContextInfo, stock, target_value)


def rebalance_check(ContextInfo):
    if not _daily_history_ready(ContextInfo):
        _mark_diag_event(10, "历史不足")
        return

    started_at = _timer_start()
    cur_date = _date_to_yyyymmdd(g._current_date)
    _mark_diag_event(30, "检")
    print("=" * 60)
    print("【调仓检查】{}".format(cur_date))
    _print_current_positions(ContextInfo)
    _calc_market_timing_exposure(ContextInfo)
    if g.target_exposure < g.timing_full_exposure:
        _mark_diag_event(40, "控仓")

    ranked_list = get_stock_list(ContextInfo)
    g.target_list = ranked_list
    if not ranked_list:
        _mark_diag_event(60, "空池")
        print("选股结果为空, 跳过本次调仓")
        _log_elapsed("调仓检查", started_at)
        print("=" * 60)
        return

    target_list = ranked_list[:g.stock_num]
    new_target_count = len([stock for stock in target_list if stock not in set(_held_stocks())])
    _diag_set("new_target_count", new_target_count)
    target_weights = _calc_inverse_vol_weights(ContextInfo, target_list)
    should_rebalance, reason = _should_rebalance(ContextInfo, ranked_list, target_weights)

    if not should_rebalance:
        _mark_diag_event(30, "持")
        print("未触发调仓: {}".format(reason))
        print("目标池前 {} 只: {}".format(len(target_list), target_list))
        _log_elapsed("调仓检查", started_at)
        print("=" * 60)
        return

    _mark_diag_event(70, "调仓")
    print("【触发调仓】{}".format(reason))
    print("目标持仓 {} 只: {}".format(len(target_list), target_list))
    _rebalance_to_weights(ContextInfo, target_weights)
    g._last_rebalance_date = g._current_date
    g._last_rebalance_month = _current_month_key()
    _log_elapsed("调仓检查", started_at)
    print("=" * 60)


# ==============================================================================
# 止损检查
# ==============================================================================
def check_stop_loss(ContextInfo):
    if not g.run_stoploss:
        return

    idx_stocks = _get_sector_stocks(ContextInfo, g.universe_index_code)
    close_data = _history(ContextInfo, idx_stocks, 2, "close")
    if not close_data:
        raise RuntimeError("市场止损检查失败: QMT close 字段无数据")
    ratios = []
    for stock in idx_stocks:
        values = close_data.get(stock, [])
        if len(values) >= 2 and values[0] > 0:
            ratios.append(values[-1] / values[0])

    if ratios:
        down_ratio = float(np.nanmean(ratios))
        if down_ratio <= 1 - g.stoploss_market:
            _mark_diag_event(100, "市场止损")
            print("【市场止损】指数池跌幅 {:.1%}, 全仓清仓".format(1 - down_ratio))
            for stock in list(g.holdings.keys()):
                if g.holdings.get(stock, 0) > 0:
                    close_position(ContextInfo, stock)
            return

    for stock, shares in list(g.holdings.items()):
        if shares <= 0:
            continue
        price = _last_price(ContextInfo, stock, "close")
        cost = g.buypoint.get(stock)
        if cost is None:
            _log_once(
                "stoploss-cost-missing-{}".format(stock),
                "个股止损跳过: {} 缺少持仓成本".format(stock),
            )
            continue
        if np.isfinite(price) and price < cost * (1 - g.stoploss_limit):
            _mark_diag_event(100, "止损")
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

    close_data = _history(ContextInfo, g.yesterday_HL_list, 1, "close", dividend_type="none")
    high_limit_data = _limit_history(ContextInfo, g.yesterday_HL_list, 1, "up")
    if not close_data:
        raise RuntimeError("涨停打开检查失败: QMT close 字段无数据")

    for stock in g.yesterday_HL_list:
        if g.holdings.get(stock, 0) <= 0:
            continue
        last_price = _latest(close_data, stock)
        high_limit = _latest(high_limit_data, stock)
        if np.isfinite(last_price) and np.isfinite(high_limit) and last_price < high_limit:
            _mark_diag_event(90, "涨停开")
            close_position(ContextInfo, stock)
            print("【涨停打开】卖出 {} ({})".format(stock, _stock_name(ContextInfo, stock)))
            _mark_sell_reason(stock, "limitup")

    g.history_hold_list.extend(g.not_buy_again)


def check_remain_amount(ContextInfo):
    if g.reason_to_sell in ["limitup", "stoploss"]:
        g.hold_list = _held_stocks()
        if len(g.hold_list) < g.stock_num and g.target_list:
            _mark_diag_event(80, "补仓")
            target_list = filter_not_buy_again(ContextInfo, g.target_list)[:g.stock_num]
            target_weights = _calc_inverse_vol_weights(ContextInfo, target_list)
            _rebalance_to_weights(
                ContextInfo,
                target_weights,
                sell_unmatched=False,
                allow_reduce=False,
            )
        g.reason_to_sell = ""


# ==============================================================================
# 核心选股：小市值初筛 + Barra 量价因子评分排序
# ==============================================================================
def _log_filter_result(name, before_count, after_count, started_at=None):
    del started_at
    print("过滤步骤 {}: {} -> {}".format(name, before_count, after_count))


def get_stock_list(ContextInfo):
    started_at = _timer_start()
    _reset_selection_diagnostics()
    g._selection_end_time = _previous_bar_end_time(ContextInfo)
    try:
        return _get_stock_list_impl(ContextInfo)
    finally:
        g._selection_end_time = None
        _log_elapsed("完整选股流程", started_at)


def _get_stock_list_impl(ContextInfo):
    initial_list = _get_sector_stocks(ContextInfo, g.universe_index_code)
    if not initial_list:
        return []
    _diag_stage("pool_initial", len(initial_list))
    print("初始股票池数量: {}".format(len(initial_list)))

    base_date = g._previous_trade_date or g._current_date or _get_current_date(ContextInfo)
    cutoff_date = base_date - datetime.timedelta(days=375)
    before_count = len(initial_list)
    step_started = _timer_start()
    initial_list = filter_listed_before(ContextInfo, initial_list, cutoff_date)
    _log_filter_result("上市满一年", before_count, len(initial_list), step_started)
    if not initial_list:
        return []

    filters = [
        ("过滤科创/北交/创业板", filter_kcbj_stock),
        ("过滤ST/退市名称", filter_st_stock),
        ("过滤停牌", filter_paused_stock),
        ("过滤涨停", filter_limitup_stock),
        ("过滤跌停", filter_limitdown_stock),
        ("过滤高价股", filter_highprice_stock),
    ]

    for name, func in filters:
        before_count = len(initial_list)
        step_started = _timer_start()
        initial_list = func(ContextInfo, initial_list)
        _log_filter_result(name, before_count, len(initial_list), step_started)
        if not initial_list:
            return []
    _diag_stage("after_basic_filters", len(initial_list))

    step_started = _timer_start()
    money_data = _history(ContextInfo, initial_list, g.liquidity_days, "money")
    if not money_data:
        raise RuntimeError("流动性过滤失败: QMT amount 字段无数据")
    avg_amount = {}
    min_amount = {}
    missing_amount = []
    for stock in initial_list:
        values = np.array(money_data.get(stock, []), dtype=float)
        values = values[np.isfinite(values)]
        if len(values) >= g.liquidity_days:
            recent_values = values[-g.liquidity_days:]
            avg_amount[stock] = np.nanmean(recent_values)
            min_amount[stock] = np.nanmin(recent_values)
        else:
            missing_amount.append(stock)
    if missing_amount:
        print("流动性过滤: {} 只股票 amount 历史不足, 示例 {}".format(
            len(missing_amount), _stock_sample(missing_amount)
        ))
    if not avg_amount:
        raise RuntimeError("流动性过滤失败: 无股票满足 amount 历史长度要求")
    before_count = len(initial_list)
    initial_list = [
        stock for stock in initial_list
        if avg_amount.get(stock, 0) >= g.min_avg_amount
        and min_amount.get(stock, 0) >= g.min_daily_amount
    ]
    _log_filter_result("平均/单日成交额", before_count, len(initial_list), step_started)
    if not initial_list:
        return []
    _diag_stage("after_liquidity", len(initial_list))

    step_started = _timer_start()
    close_data = _history(ContextInfo, initial_list, g.momentum_days, "close")
    if not close_data:
        raise RuntimeError("动量过滤失败: QMT close 字段无数据")
    momentum = {}
    missing_momentum = []
    for stock in initial_list:
        close = np.array(close_data.get(stock, []), dtype=float)
        close = close[np.isfinite(close)]
        if len(close) >= 2 and close[0] > 0:
            momentum[stock] = (close[-1] - close[0]) / close[0]
        else:
            missing_momentum.append(stock)
    if missing_momentum:
        print("动量过滤: {} 只股票 close 历史不足, 示例 {}".format(
            len(missing_momentum), _stock_sample(missing_momentum)
        ))
    if not momentum:
        raise RuntimeError("动量过滤失败: 无股票满足 close 历史长度要求")
    before_count = len(initial_list)
    initial_list = [
        stock for stock in initial_list
        if g.filter_threshold1 < momentum.get(stock, np.nan) < g.filter_threshold2
    ]
    _log_filter_result("30日动量区间", before_count, len(initial_list), step_started)
    if not initial_list:
        return []
    _diag_stage("after_momentum", len(initial_list))

    step_started = _timer_start()
    cap_df = _get_market_cap_df(ContextInfo, initial_list)
    turnover_share_map = {}
    if cap_df is not None and not cap_df.empty and cap_df["market_cap"].notna().any():
        cap_df = cap_df[
            (cap_df["market_cap"] >= g.min_market_cap)
            & (cap_df["market_cap"] <= g.max_market_cap)
            & (cap_df["circ_market_cap"] >= g.min_circ_market_cap)
        ].sort_values("market_cap")
        if cap_df.empty:
            return []
        candidate_list = cap_df["code"].tolist()
        turnover_share_map = dict(zip(cap_df["code"], cap_df["float_share"]))
    else:
        raise RuntimeError("市值筛选失败: 无有效 TotalVolume/FloatVolume 或 close 数据")
    _log_filter_result("市值区间", len(initial_list), len(candidate_list), step_started)
    _diag_stage("after_market_cap", len(candidate_list))
    _diag_stage("candidate_count", len(candidate_list))

    print("候选股票数量: {}, 开始计算 Barra 因子...".format(len(candidate_list)))
    step_started = _timer_start()
    factor_df = calc_barra_factors(ContextInfo, candidate_list, turnover_share_map)
    _log_elapsed("Barra 因子计算", step_started)

    if factor_df is None or factor_df.empty:
        raise RuntimeError(
            "Barra 因子计算失败: 候选池 {} 只股票均无完整因子, 已停止排序, 请查看上一条 Barra 跳过原因和副图 D4".format(
                len(candidate_list)
            )
        )
    _diag_stage("factor_count", len(factor_df))

    factor_df = calc_composite_score(factor_df)
    factor_df = factor_df.sort_values("composite_score", ascending=True)
    result = factor_df["code"].tolist()
    _diag_stage("ranked_count", len(result))

    print("因子选股完成, Top5: {}".format(result[:5]))
    return result


def _get_market_cap_df(ContextInfo, stock_list):
    override = g.market_cap_map
    close_data = _history(ContextInfo, stock_list, 1, "close")
    if not close_data:
        raise RuntimeError("市值计算失败: QMT close 字段无数据")
    rows = []
    missing_share = []
    missing_price = []

    for stock in stock_list:
        if stock in override:
            market_cap = _normalize_market_cap_to_yi(override[stock])
            rows.append({
                "code": stock,
                "market_cap": market_cap,
                "circ_market_cap": market_cap,
                "float_share": np.nan,
            })
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
        else:
            missing_price.append(stock)

        if not np.isfinite(market_cap):
            missing_share.append(stock)
            continue

        if not np.isfinite(circ_market_cap):
            circ_market_cap = market_cap
        rows.append({
            "code": stock,
            "market_cap": market_cap,
            "circ_market_cap": circ_market_cap,
            "float_share": float_share,
        })

    if missing_price:
        print("市值计算: {} 只股票 close 缺失, 示例 {}".format(
            len(missing_price), _stock_sample(missing_price)
        ))
    if missing_share:
        print("市值计算: {} 只股票 TotalVolume 缺失或无效, 示例 {}".format(
            len(missing_share), _stock_sample(missing_share)
        ))
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
        return np.nan
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


# ==============================================================================
# Barra 量价因子计算
# ==============================================================================
def _reason_counts(items):
    counts = {}
    for item in items:
        reason = item[1] if isinstance(item, tuple) and len(item) > 1 else str(item)
        counts[reason] = counts.get(reason, 0) + 1
    return counts


def calc_barra_factors(ContextInfo, stock_list, turnover_share_map=None):
    return factor_module.calc_barra_factors(
        ContextInfo,
        stock_list,
        g,
        _history,
        _normalize_share_count,
        turnover_share_map,
    )

    if not stock_list:
        return None

    need_days = g.vol_window + 10
    turnover_share_map = turnover_share_map or {}

    close_data = _history(ContextInfo, stock_list, need_days, "close")
    raw_close_data = _history(ContextInfo, stock_list, need_days, "close", dividend_type="none")
    money_data = _history(ContextInfo, stock_list, need_days, "money")
    required_data = {
        "close": close_data,
        "raw_close": raw_close_data,
        "amount": money_data,
    }
    for field_name, data in required_data.items():
        if not data:
            raise RuntimeError("Barra 因子计算失败: QMT {} 字段无数据".format(field_name))

    mkt_close = _history(ContextInfo, [g.factor_benchmark], need_days, "close").get(
        g.factor_benchmark, []
    )
    if len(mkt_close) < need_days:
        raise RuntimeError("Barra 因子计算失败: 因子基准 {} close 历史不足".format(g.factor_benchmark))
    mkt_close = np.array(mkt_close, dtype=float)
    mkt_ret = np.diff(mkt_close) / mkt_close[:-1]

    records = []
    skipped = []
    for code in stock_list:
        try:
            close = np.array(close_data.get(code, []), dtype=float)
            raw_close = np.array(raw_close_data.get(code, []), dtype=float)
            money = np.array(money_data.get(code, []), dtype=float)
            float_share = _normalize_share_count(turnover_share_map.get(code))

            if len(close) < 63:
                skipped.append((code, "close历史不足"))
                continue
            if len(raw_close) != len(close) or len(money) != len(close):
                skipped.append((code, "字段长度不一致"))
                continue
            if not np.isfinite(float_share) or float_share <= 0:
                skipped.append((code, "FloatVolume无效"))
                continue

            total_value = raw_close * float_share
            valid_mask = (
                np.isfinite(close)
                & (close > 0)
                & np.isfinite(raw_close)
                & (raw_close > 0)
                & np.isfinite(money)
                & np.isfinite(total_value)
                & (total_value > 0)
            )
            if valid_mask.sum() < 63:
                skipped.append((code, "有效Barra样本不足"))
                continue
            close = close[valid_mask]
            money = money[valid_mask]
            total_value = total_value[valid_mask]

            ret = np.diff(close) / close[:-1]
            if len(ret) < 62:
                skipped.append((code, "收益率样本不足"))
                continue

            row = {"code": code}
            row["HSIGMA"] = _calc_hsigma(ret, mkt_ret, g.vol_window, g.vol_halflife)
            row["DASTD"] = _calc_dastd(ret, g.vol_window, g.dastd_halflife)
            row["CMRA"] = _calc_cmra(close, g.cmra_months)
            row["STOM"] = _calc_stom(money, total_value, g.stom_window)
            row["STOQ"] = _calc_stoq(money, total_value, g.stoq_window)
            row["STOA"] = _calc_stoa(money, total_value, g.stoa_window)
            row["ATVR"] = _calc_atvr(money, total_value, g.vol_window, g.atvr_halflife)
            records.append(row)
        except Exception as exc:
            skipped.append((code, str(exc)))
            continue

    if skipped:
        print("Barra 因子跳过 {} 只, 原因 {}, 示例 {}".format(
            len(skipped), _reason_counts(skipped), skipped[:5]
        ))
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
            raise RuntimeError("HSIGMA 计算失败: 市场收益样本不足")
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
        raise RuntimeError("因子标准化失败: 截面标准差为 0 或 NaN")
    return (series - mu) / std


def calc_composite_score(df):
    return factor_module.calc_composite_score(df, g.factor_weights)

    factor_cols = list(g.factor_weights.keys())
    df = df.copy()
    for col in factor_cols:
        if col not in df.columns:
            raise RuntimeError("因子合成失败: 缺少因子列 {}".format(col))
    missing_factor_mask = df[factor_cols].isna().any(axis=1)
    if missing_factor_mask.any():
        missing_codes = df.loc[missing_factor_mask, "code"].tolist()
        print("因子合成: 剔除 {} 只因子缺失股票, 示例 {}".format(
            len(missing_codes), _stock_sample(missing_codes)
        ))
        df = df.loc[~missing_factor_mask].copy()
    if len(df) < 5:
        raise RuntimeError("因子合成失败: 完整因子股票不足 {}".format(len(df)))

    for col in factor_cols:
        valid_mask = df[col].notna()
        if valid_mask.sum() < 5:
            raise RuntimeError("因子合成失败: {} 有效样本不足 {}".format(col, int(valid_mask.sum())))
        series = df.loc[valid_mask, col].copy()
        series = _mad_winsorize(series)
        series = _zscore(series)
        df.loc[valid_mask, col + "_z"] = series

    z_cols = [col + "_z" for col in factor_cols if col + "_z" in df.columns]
    if not z_cols:
        raise RuntimeError("因子合成失败: 没有可用标准化因子")
    else:
        weights = np.array([g.factor_weights[c.replace("_z", "")] for c in z_cols])
        weights = weights / weights.sum()
        df["composite_score"] = df[z_cols].values @ weights

    return df


# ==============================================================================
# 过滤函数集合
# ==============================================================================
def filter_listed_before(ContextInfo, stock_list, cutoff_date):
    result = []
    missing_detail = []
    missing_open_date = []
    for stock in stock_list:
        detail = _instrument_detail(ContextInfo, stock)
        if not detail:
            missing_detail.append(stock)
            continue
        open_date = _parse_qmt_date(detail.get("OpenDate"))
        if open_date is None:
            missing_open_date.append(stock)
            continue
        if open_date <= cutoff_date:
            result.append(stock)
    if missing_detail:
        print("上市时间过滤: {} 只股票合约详情缺失, 示例 {}".format(
            len(missing_detail), _stock_sample(missing_detail)
        ))
    if missing_open_date:
        print("上市时间过滤: {} 只股票 OpenDate 缺失或无效, 示例 {}".format(
            len(missing_open_date), _stock_sample(missing_open_date)
        ))
    if len(missing_detail) + len(missing_open_date) == len(stock_list):
        raise RuntimeError("上市时间过滤失败: 全部股票缺少有效 OpenDate/合约详情")
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
    if not suspend_data:
        raise RuntimeError("停牌过滤失败: QMT suspendFlag 字段无数据")
    if not close_data:
        raise RuntimeError("停牌过滤失败: QMT close 字段无数据")
    if not volume_data:
        raise RuntimeError("停牌过滤失败: QMT volume 字段无数据")

    result = []
    missing = []
    for stock in stock_list:
        detail = _instrument_detail(ContextInfo, stock)
        if _is_suspended_detail(detail):
            continue

        suspend_flag = _latest(suspend_data, stock, default=np.nan)
        close_price = _latest(close_data, stock)
        volume = _latest(volume_data, stock, default=np.nan)
        if not np.isfinite(suspend_flag) or not np.isfinite(close_price) or not np.isfinite(volume):
            missing.append(stock)
            continue
        if np.isfinite(suspend_flag) and int(suspend_flag) == 1:
            continue
        if close_price > 0 and volume > 0:
            result.append(stock)
    if missing:
        print("停牌过滤: {} 只股票停牌/价格/成交量字段缺失, 示例 {}".format(
            len(missing), _stock_sample(missing)
        ))
    return result


def filter_st_stock(ContextInfo, stock_list):
    result = []
    missing_name = []
    for stock in stock_list:
        detail = _instrument_detail(ContextInfo, stock)
        name = detail.get("InstrumentName") if detail else None
        if not name:
            missing_name.append(stock)
            continue
        name = str(name)
        if "ST" in name or "*" in name or "退" in name:
            continue
        result.append(stock)
    if missing_name:
        print("ST过滤: {} 只股票 InstrumentName 缺失, 示例 {}".format(
            len(missing_name), _stock_sample(missing_name)
        ))
    if len(missing_name) == len(stock_list):
        raise RuntimeError("ST过滤失败: 全部股票缺少 InstrumentName")
    return result


def filter_kcbj_stock(ContextInfo, stock_list):
    del ContextInfo
    return [
        stock for stock in stock_list
        if not stock[:6].startswith(("4", "8", "688", "30"))
    ]


def _filter_limit_stock(ContextInfo, stock_list, limit_field):
    if not stock_list:
        return []
    if limit_field == "high_limit":
        direction = "up"
    elif limit_field == "low_limit":
        direction = "down"
    else:
        raise RuntimeError("未知涨跌停过滤字段: {}".format(limit_field))
    close_data = _history(ContextInfo, stock_list, 1, "close", dividend_type="none")
    limit_data = _limit_history(ContextInfo, stock_list, 1, direction)
    if not close_data:
        raise RuntimeError("{} 过滤失败: QMT close 字段无数据".format(limit_field))
    if not limit_data:
        raise RuntimeError("{} 过滤失败: QMT preClose 字段无法计算涨跌停价".format(limit_field))
    result = []
    hold_set = set(g.hold_list)
    missing = []
    for stock in stock_list:
        close_price = _latest(close_data, stock)
        limit_price = _latest(limit_data, stock)
        if not np.isfinite(close_price) or not np.isfinite(limit_price):
            missing.append(stock)
            continue
        if stock in hold_set or not _is_limit_price(close_price, limit_price):
            result.append(stock)
    if missing:
        print("{} 过滤: {} 只股票价格/涨跌停字段缺失, 示例 {}".format(
            limit_field, len(missing), _stock_sample(missing)
        ))
    return result


def filter_limitup_stock(ContextInfo, stock_list):
    return _filter_limit_stock(ContextInfo, stock_list, "high_limit")


def filter_limitdown_stock(ContextInfo, stock_list):
    return _filter_limit_stock(ContextInfo, stock_list, "low_limit")


def filter_highprice_stock(ContextInfo, stock_list):
    if not stock_list:
        return []
    close_data = _history(ContextInfo, stock_list, 1, "close")
    if not close_data:
        raise RuntimeError("高价股过滤失败: QMT close 字段无数据")
    missing = [stock for stock in stock_list if not np.isfinite(_latest(close_data, stock))]
    if missing:
        print("高价股过滤: {} 只股票 close 字段缺失, 示例 {}".format(
            len(missing), _stock_sample(missing)
        ))
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


def _live_tick_price(ContextInfo, stock):
    if not getattr(ContextInfo, "xtquant_mode", False):
        return np.nan
    try:
        tick_data = ContextInfo.get_full_tick([stock]) or {}
        tick = tick_data.get(stock) or {}
    except Exception as exc:
        _log_once("tick-price-error-{}".format(stock), "读取实时行情失败 {}: {}".format(stock, exc))
        return np.nan

    for key in ("lastPrice", "last_price", "price", "last"):
        try:
            price = float(tick.get(key))
        except Exception:
            price = np.nan
        if np.isfinite(price) and price > 0:
            return price
    return np.nan


def _order_reference_price(ContextInfo, stock):
    if getattr(ContextInfo, "xtquant_mode", False):
        price = _live_tick_price(ContextInfo, stock)
        if np.isfinite(price) and price > 0:
            return price
    return _last_price(ContextInfo, stock)


def _xtconstant(name, fallback):
    if xtconstant is None:
        return fallback
    return getattr(xtconstant, name, fallback)


def _submit_order(ContextInfo, security, shares, side, reference_price):
    if not getattr(ContextInfo, "xtquant_mode", False):
        signed_shares = shares if side == "BUY" else -shares
        order_shares(security, signed_shares, "FIX", reference_price, ContextInfo, g.accountID)
        return True

    side_text = "买入" if side == "BUY" else "卖出"
    if getattr(g, "xt_dry_run", True):
        print("[DRY-RUN] {} {}({}) {}股 参考价 {:.3f}".format(
            side_text, security, _stock_name(ContextInfo, security), shares, reference_price
        ))
        return True

    _ensure_xtquant()
    order_type = _xtconstant("STOCK_BUY", 23) if side == "BUY" else _xtconstant("STOCK_SELL", 24)
    if str(getattr(g, "xt_price_type", "latest")).lower() == "fix":
        price_type = _xtconstant("FIX_PRICE", 11)
        order_price = float(reference_price)
    else:
        price_type = _xtconstant("LATEST_PRICE", 5)
        order_price = 0.0

    remark = "{}_{}_{}".format(getattr(g, "xt_strategy_name", "low_vol"), side.lower(), int(time.time()))
    try:
        order_id = ContextInfo.order_stock(security, order_type, int(shares), price_type, order_price, remark)
    except Exception as exc:
        print("xtquant 下单异常: {} {} {}股: {}".format(side_text, security, shares, exc))
        return False

    if order_id in (-1, None):
        print("xtquant 下单失败: {} {} {}股 price_type={} price={}".format(
            side_text, security, shares, price_type, order_price
        ))
        return False
    print("xtquant 下单已提交: {} {} {}股 order_id={}".format(side_text, security, shares, order_id))
    return True


def order_target_value_(ContextInfo, security, value):
    price = _order_reference_price(ContextInfo, security)
    if not np.isfinite(price) or price <= 0:
        _log_once(
            "order-price-invalid-{}".format(security),
            "下单跳过: {} 无有效 {} 价格".format(security, g.order_price_field),
        )
        return False

    current_shares = int(g.holdings.get(security, 0))
    current_value = current_shares * price
    delta_value = float(value) - current_value

    if abs(delta_value) < price * LOT_SIZE:
        _log_once(
            "order-delta-too-small-{}-{}".format(security, int(value == 0)),
            "下单跳过: {} 目标差额 {:.2f} 小于一手金额 {:.2f}".format(
                security, delta_value, price * LOT_SIZE
            ),
        )
        return False

    if delta_value > 0:
        shares = _round_lot_by_value(delta_value, price)
        if shares < LOT_SIZE:
            _log_once(
                "order-lot-too-small-{}".format(security),
                "买入跳过: {} 目标金额不足一手".format(security),
            )
            return False
        order_value = shares * price
        cost = _calc_order_cost(order_value, is_sell=False)
        if g.money < order_value + cost:
            shares = _round_lot_by_value(g.money - cost, price)
            order_value = shares * price
            if shares < LOT_SIZE:
                _log_once(
                    "order-cash-too-low-{}".format(security),
                    "买入跳过: {} 可用资金 {:.2f} 不足一手含费用".format(security, g.money),
                )
                return False
            cost = _calc_order_cost(order_value, is_sell=False)
        if not _submit_order(ContextInfo, security, shares, "BUY", price):
            return False
        g._diag_buy_count += 1
        _mark_diag_event(90, "买")
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
    if not _submit_order(ContextInfo, security, shares, "SELL", price):
        return False
    g._diag_sell_count += 1
    _mark_diag_event(95, "卖")
    buy_price = g.buypoint.get(security)
    if buy_price is None:
        _log_once(
            "sell-cost-missing-{}".format(security),
            "卖出收益估算: {} 缺少持仓成本, 使用当前成交价估算本地收益".format(security),
        )
        buy_price = price
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
    if not target_list or g.money <= 0:
        return
    target_list = filter_not_buy_again(ContextInfo, target_list)[:g.stock_num]
    target_weights = _calc_inverse_vol_weights(ContextInfo, target_list)
    _rebalance_to_weights(
        ContextInfo,
        target_weights,
        sell_unmatched=False,
        allow_reduce=False,
    )


XTQUANT_SCHEDULE = (
    ("prepare", "09:25", prepare_stock_list),
    ("rebalance", "10:00", rebalance_check),
    ("stoploss_1030", "10:30", check_stop_loss),
    ("stoploss_1400", "14:00", check_stop_loss),
    ("afternoon", "14:30", trade_afternoon),
)


def _connect_xtquant_context(config):
    _ensure_xtquant()
    trader = None
    account = None

    if config.account_id and config.userdata_mini_path:
        account = StockAccount(config.account_id, config.account_type)
        session_id = int(config.session_id or random.randint(100000, 999999))
        callback = StrategyTraderCallback()
        try:
            trader = XtQuantTrader(config.userdata_mini_path, session_id, callback=callback)
        except TypeError:
            trader = XtQuantTrader(config.userdata_mini_path, session_id)
            trader.register_callback(callback)
        trader.start()
        connect_result = trader.connect()
        if connect_result != 0:
            raise RuntimeError("XtQuantTrader 连接失败, connect_result={}".format(connect_result))
        subscribe_result = trader.subscribe(account)
        print("xtquant 已连接: account={} type={} session_id={} subscribe={}".format(
            config.account_id, config.account_type, session_id, subscribe_result
        ))
    elif not config.dry_run:
        raise RuntimeError("真实交易必须配置 --userdata-mini 和 --account")
    else:
        print("未配置交易账号或 userdata_mini，进入仅行情/DRY-RUN 模式。")

    return XtQuantContext(config, trader=trader, account=account)


def _initialize_strategy(ContextInfo):
    init(ContextInfo)
    if getattr(ContextInfo, "xtquant_mode", False):
        g.enable_chart_diagnostics = False
    _update_calendar_state(ContextInfo)
    _sync_trade_state(ContextInfo)
    print("运行模式: {}, 可用资金 {:.2f}, 当前持仓 {} 只".format(
        "DRY-RUN" if getattr(g, "xt_dry_run", True) else "LIVE-TRADE",
        g.money,
        len(_held_stocks()),
    ))


def _run_task(ContextInfo, task_name, func):
    ContextInfo.refresh_clock()
    _update_calendar_state(ContextInfo)
    _reset_bar_diagnostics()
    print("【xtquant任务】{} {}".format(task_name, ContextInfo.now.strftime("%Y-%m-%d %H:%M:%S")))
    func(ContextInfo)


def _run_daily_once(ContextInfo):
    for task_name, _trigger_time, func in XTQUANT_SCHEDULE:
        _run_task(ContextInfo, task_name, func)


def _run_scan(ContextInfo):
    ContextInfo.refresh_clock()
    _update_calendar_state(ContextInfo)
    _reset_bar_diagnostics()
    ranked = get_stock_list(ContextInfo)
    print("候选排序前 {} 只: {}".format(min(20, len(ranked)), ranked[:20]))


def _is_task_due(now, trigger_time):
    hour, minute = [int(part) for part in trigger_time.split(":")]
    return now.time() >= datetime.time(hour, minute)


def _run_loop(ContextInfo):
    print("进入 xtquant 盘中循环，任务: {}".format(
        [(name, trigger) for name, trigger, _func in XTQUANT_SCHEDULE]
    ))
    while True:
        try:
            ContextInfo.refresh_clock()
            current_date = ContextInfo.now.date()
            if ContextInfo.is_trading_day(current_date):
                for task_name, trigger_time, func in XTQUANT_SCHEDULE:
                    if _task_done(task_name, current_date):
                        continue
                    if not _is_task_due(ContextInfo.now, trigger_time):
                        continue
                    try:
                        _run_task(ContextInfo, task_name, func)
                    finally:
                        _mark_task_done(task_name, current_date)
            else:
                if not _task_done("non_trading_day", current_date):
                    print("{} 非交易日，等待下一轮。".format(_date_to_yyyymmdd(current_date)))
                    _mark_task_done("non_trading_day", current_date)
        except KeyboardInterrupt:
            print("收到 Ctrl+C，退出 xtquant 循环。")
            break
        except Exception as exc:
            print("xtquant 循环异常: {}".format(exc))
        time.sleep(ContextInfo.config.sleep_seconds)


def _build_arg_parser():
    parser = argparse.ArgumentParser(description="低波动小市值 xtquant / MiniQMT 独立脚本")
    parser.add_argument("--userdata-mini", default=USERDATA_MINI_PATH, help="MiniQMT userdata_mini 完整路径")
    parser.add_argument("--account", default=ACCOUNT_ID, help="资金账号")
    parser.add_argument("--account-type", default=ACCOUNT_TYPE, help="账号类型，股票通常为 STOCK")
    parser.add_argument("--session-id", type=int, default=SESSION_ID, help="xtquant 会话 ID，不填则随机")
    parser.add_argument("--initial-cash", type=float, default=DEFAULT_INITIAL_CASH, help="无交易账号时用于估值的初始资金")
    parser.add_argument("--period", default="1d", help="行情周期，默认 1d")
    parser.add_argument("--dividend-type", default=DEFAULT_DIVIDEND_TYPE, help="复权方式，默认 front")
    parser.add_argument("--price-type", choices=("latest", "fix"), default=os.environ.get("XTQUANT_PRICE_TYPE", "latest"))
    parser.add_argument("--no-download", action="store_true", help="不自动补历史行情")
    parser.add_argument("--sleep-seconds", type=int, default=int(os.environ.get("XTQUANT_SLEEP_SECONDS", "20") or "20"))
    parser.add_argument(
        "--task",
        choices=("daily", "scan", "prepare", "rebalance", "stoploss", "afternoon", "loop"),
        default=os.environ.get("XTQUANT_TASK", "daily"),
        help="执行任务：daily=顺序跑一遍，loop=盘中定时循环",
    )
    parser.add_argument("--live-trade", action="store_true", help="打开真实委托。默认只 DRY-RUN 打印拟委托")
    return parser


def _config_from_args(args):
    return StrategyConfig(
        userdata_mini_path=args.userdata_mini,
        account_id=args.account,
        account_type=args.account_type,
        session_id=args.session_id,
        dry_run=(not args.live_trade) and DRY_RUN,
        initial_cash=args.initial_cash,
        period=args.period,
        dividend_type=args.dividend_type,
        price_type=args.price_type,
        auto_download_history=not args.no_download,
        sleep_seconds=args.sleep_seconds,
    )


def main(argv=None):
    parser = _build_arg_parser()
    args = parser.parse_args(argv)
    config = _config_from_args(args)
    ContextInfo = _connect_xtquant_context(config)
    _initialize_strategy(ContextInfo)

    task = args.task
    if task == "daily":
        _run_daily_once(ContextInfo)
    elif task == "scan":
        _run_scan(ContextInfo)
    elif task == "loop":
        _run_loop(ContextInfo)
    elif task == "prepare":
        _run_task(ContextInfo, "prepare", prepare_stock_list)
    elif task == "rebalance":
        _run_task(ContextInfo, "prepare", prepare_stock_list)
        _run_task(ContextInfo, "rebalance", rebalance_check)
    elif task == "stoploss":
        _run_task(ContextInfo, "stoploss", check_stop_loss)
    elif task == "afternoon":
        _run_task(ContextInfo, "afternoon", trade_afternoon)
    else:
        raise RuntimeError("未知任务: {}".format(task))


if __name__ == "__main__":
    main()
