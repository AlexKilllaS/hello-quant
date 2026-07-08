# coding: utf-8
"""
MiniQMT + Backtrader 回测版：低波动小市值

数据层：MiniQMT / xtdata 下载并读取历史日线。
回测层：Backtrader 负责资金、持仓、成交和绩效统计。
策略层：复用同包 core.py 的选股、调仓和风控逻辑。

运行前需要：
1. MiniQMT 已启动并能通过 xtdata 访问行情。
2. 当前 Python 环境已安装 backtrader：pip install backtrader
"""

import argparse
import datetime
import pathlib
import sys
from types import SimpleNamespace

import numpy as np
import pandas as pd

try:
    from . import core as default_strategy_module
except ImportError:
    import core as default_strategy_module

sys.path.insert(0, r"C:\Users\alex\Documents\qmt_lib\xtquant_250807")

try:
    from xtquant import xtdata
except ImportError as exc:
    raise RuntimeError("未找到 xtquant，请先确认本地 xtquant 路径配置正确。") from exc


HISTORY_FIELDS = ["open", "high", "low", "close", "volume", "amount", "preClose", "suspendFlag"]
DEFAULT_OUTPUT_DIR = pathlib.Path(__file__).with_name("backtrader_output")


def require_backtrader():
    try:
        import backtrader as bt
    except ImportError as exc:
        raise RuntimeError("未安装 backtrader，请先执行：pip install backtrader") from exc
    return bt


def parse_date(value):
    if isinstance(value, datetime.datetime):
        return value.date()
    if isinstance(value, datetime.date):
        return value
    return datetime.datetime.strptime(str(value), "%Y%m%d").date()


def yyyymmdd(value):
    if isinstance(value, datetime.datetime):
        value = value.date()
    if isinstance(value, datetime.date):
        return value.strftime("%Y%m%d")
    return str(value)[:8]


def parse_xt_datetime(value):
    if isinstance(value, pd.Timestamp):
        return value.to_pydatetime()
    if isinstance(value, datetime.datetime):
        return value
    if isinstance(value, datetime.date):
        return datetime.datetime.combine(value, datetime.time())

    text = str(value).strip()
    if not text:
        return None
    if text.endswith(".0"):
        text = text[:-2]
    digits = "".join(ch for ch in text if ch.isdigit())
    try:
        if len(digits) >= 14:
            return datetime.datetime.strptime(digits[:14], "%Y%m%d%H%M%S")
        if len(digits) == 13:
            return datetime.datetime.fromtimestamp(int(digits) / 1000.0)
        if len(digits) == 10 and digits.startswith(("1", "2")):
            return datetime.datetime.fromtimestamp(int(digits))
        if len(digits) >= 8:
            return datetime.datetime.strptime(digits[:8], "%Y%m%d")
    except Exception:
        return None
    try:
        parsed = pd.to_datetime(value, errors="coerce")
    except Exception:
        return None
    if pd.isna(parsed):
        return None
    return parsed.to_pydatetime()


def stock_sample(stocks, limit=5):
    stocks = list(stocks or [])
    suffix = "..." if len(stocks) > limit else ""
    return "{}{}".format(stocks[:limit], suffix)


def download_history(stocks, period, start_time, end_time, batch_size):
    stocks = list(dict.fromkeys(stocks))
    for start in range(0, len(stocks), batch_size):
        batch = stocks[start:start + batch_size]
        try:
            if hasattr(xtdata, "download_history_data2"):
                xtdata.download_history_data2(
                    batch,
                    period,
                    start_time=start_time,
                    end_time=end_time,
                    callback=None,
                    incrementally=True,
                )
            else:
                for stock in batch:
                    xtdata.download_history_data(
                        stock,
                        period=period,
                        start_time=start_time,
                        end_time=end_time,
                        incrementally=True,
                    )
            print("历史行情下载完成 {}/{} 示例 {}".format(
                min(start + len(batch), len(stocks)), len(stocks), stock_sample(batch)
            ))
        except Exception as exc:
            print("历史行情下载失败 period={} stocks={}: {}".format(period, stock_sample(batch), exc))


def normalize_frame(frame):
    if frame is None or len(frame) == 0:
        return pd.DataFrame()

    frame = frame.copy()
    if not isinstance(frame, pd.DataFrame):
        frame = pd.DataFrame(frame)

    if "time" in frame.columns:
        raw_index = frame.pop("time")
    elif "datetime" in frame.columns:
        raw_index = frame.pop("datetime")
    else:
        raw_index = frame.index

    parsed_index = [parse_xt_datetime(value) for value in raw_index]
    frame.index = pd.DatetimeIndex(parsed_index)
    frame = frame[~frame.index.isna()].sort_index()
    frame = frame[~frame.index.duplicated(keep="last")]

    for col in HISTORY_FIELDS:
        if col in frame.columns:
            frame[col] = pd.to_numeric(frame[col], errors="coerce")

    if "preClose" not in frame.columns and "close" in frame.columns:
        frame["preClose"] = frame["close"].shift(1)
    if "suspendFlag" not in frame.columns:
        frame["suspendFlag"] = 0.0
    if "amount" not in frame.columns:
        frame["amount"] = 0.0

    return frame


def field_dict_to_stock_frames(raw_data, fields, stocks):
    result = {}
    for stock in stocks:
        columns = {}
        index = None
        for field in fields:
            frame = raw_data.get(field)
            if not isinstance(frame, pd.DataFrame) or frame.empty:
                continue
            if stock in frame.index:
                series = frame.loc[stock]
            elif stock in frame.columns:
                series = frame[stock]
            else:
                continue
            if index is None:
                index = series.index
            columns[field] = series
        if columns:
            result[stock] = normalize_frame(pd.DataFrame(columns, index=index))
    return result


def normalize_market_result(raw_data, fields, stocks):
    if not isinstance(raw_data, dict):
        return {}

    if any(stock in raw_data for stock in stocks):
        return {
            stock: normalize_frame(raw_data.get(stock))
            for stock in stocks
            if raw_data.get(stock) is not None
        }

    if all(field in raw_data for field in fields):
        return field_dict_to_stock_frames(raw_data, fields, stocks)

    return {}


def fetch_history(stocks, period, start_time, end_time, dividend_type, batch_size):
    result = {}
    stocks = list(dict.fromkeys(stocks))
    for start in range(0, len(stocks), batch_size):
        batch = stocks[start:start + batch_size]
        try:
            raw_data = xtdata.get_market_data_ex(
                HISTORY_FIELDS,
                batch,
                period=period,
                start_time=start_time,
                end_time=end_time,
                count=-1,
                dividend_type=dividend_type,
                fill_data=True,
            )
        except Exception as exc:
            print("读取历史行情失败 stocks={}: {}".format(stock_sample(batch), exc))
            continue

        parsed = normalize_market_result(raw_data, HISTORY_FIELDS, batch)
        result.update({stock: frame for stock, frame in parsed.items() if not frame.empty})
        print("历史行情读取完成 {}/{} 有效累计 {}".format(
            min(start + len(batch), len(stocks)), len(stocks), len(result)
        ))
    return result


def get_sector_stocks(strategy_module, index_code, max_universe):
    sector_name = strategy_module.INDEX_SECTOR_NAME_MAP[index_code]
    try:
        if hasattr(xtdata, "download_sector_data"):
            xtdata.download_sector_data()
    except Exception as exc:
        print("下载板块数据失败: {}".format(exc))

    stocks = xtdata.get_stock_list_in_sector(sector_name) or []
    stocks = list(dict.fromkeys(stocks))
    if max_universe and max_universe > 0:
        stocks = stocks[:max_universe]
    if not stocks:
        raise RuntimeError("板块 {} 未返回成分股，请检查 MiniQMT 本地数据。".format(sector_name))
    print("股票池 {} 使用板块 {}, 成分 {} 只".format(index_code, sector_name, len(stocks)))
    return stocks


def make_master_calendar(history, benchmark, start_date, end_date):
    frame = history.get(benchmark)
    if frame is None or frame.empty:
        frames = [item for item in history.values() if item is not None and not item.empty]
        if not frames:
            raise RuntimeError("没有可用历史行情，无法生成回测日历。")
        index = frames[0].index
    else:
        index = frame.index

    start_dt = datetime.datetime.combine(start_date, datetime.time())
    end_dt = datetime.datetime.combine(end_date, datetime.time(23, 59, 59))
    index = pd.DatetimeIndex(index)
    return index[(index >= start_dt) & (index <= end_dt)].unique().sort_values()


def prepare_backtrader_frame(frame, calendar):
    frame = frame.reindex(calendar)
    price_cols = ["open", "high", "low", "close"]
    frame[price_cols] = frame[price_cols].ffill()
    frame["volume"] = frame["volume"].fillna(0.0)
    frame["amount"] = frame["amount"].fillna(0.0)
    frame["preClose"] = frame["preClose"].fillna(frame["close"].shift(1))
    frame["suspendFlag"] = frame["suspendFlag"].fillna(1.0)
    frame = frame.dropna(subset=["open", "high", "low", "close"])
    return frame


class BacktestAsset(object):
    def __init__(self, cash, total_asset):
        self.cash = cash
        self.available_cash = cash
        self.enable_balance = cash
        self.total_asset = total_asset


class BacktestPosition(object):
    def __init__(self, stock_code, size, price):
        self.stock_code = stock_code
        self.volume = int(size)
        self.avg_price = float(price or 0.0)


class BacktraderContext(object):
    xtquant_mode = True

    def __init__(self, strategy_module, history, universe, args):
        self.strategy_module = strategy_module
        self.history = history
        self.universe = universe
        self.args = args
        self.strategy = None
        self.data_by_code = {}
        self.now = datetime.datetime.combine(parse_date(args.start), datetime.time())
        self.barpos = 0
        self.do_back_test = True
        self.benchmark = args.report_benchmark
        self.dividend_type = args.dividend_type
        self.period = args.period
        self.capital = float(args.cash)
        self.config = SimpleNamespace(
            account_id="backtrader",
            account_type="STOCK",
            period=args.period,
            dividend_type=args.dividend_type,
            universe_index_code=args.universe_index,
            factor_benchmark=args.factor_benchmark,
            report_benchmark=args.report_benchmark,
            dry_run=False,
            price_type="fix",
            strategy_name="低波动小市值_backtrader",
            auto_download_history=False,
            download_batch_size=args.batch_size,
            sleep_seconds=0,
        )

    def attach_strategy(self, strategy):
        self.strategy = strategy
        self.data_by_code = {data._name: data for data in strategy.datas}

    def set_now(self, value):
        if isinstance(value, datetime.date) and not isinstance(value, datetime.datetime):
            value = datetime.datetime.combine(value, datetime.time())
        self.now = value.replace(tzinfo=None)
        self.barpos += 1

    def refresh_clock(self):
        return None

    def get_stock_list_in_sector(self, sector_name):
        del sector_name
        return list(self.universe)

    def get_instrumentdetail(self, stock):
        try:
            return xtdata.get_instrument_detail(stock, False)
        except TypeError:
            return xtdata.get_instrument_detail(stock)
        except Exception:
            return {}

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
        del period, dividend_type, fill_data, subscribe
        end_dt = parse_xt_datetime(end_time) if end_time else self.now
        start_dt = parse_xt_datetime(start_time) if start_time else None
        if end_dt is None:
            end_dt = self.now

        result = {}
        for stock in stock_list:
            frame = self.history.get(stock)
            if frame is None or frame.empty:
                continue
            sub = frame.loc[frame.index <= end_dt]
            if start_dt is not None:
                sub = sub.loc[sub.index >= start_dt]
            if count is not None and int(count) > 0:
                sub = sub.tail(int(count))
            columns = [field for field in field_list if field in sub.columns]
            if columns:
                result[stock] = sub.loc[:, columns].copy()
        return result

    def get_full_tick(self, code_list):
        ticks = {}
        for stock in code_list:
            frame = self.history.get(stock)
            price = np.nan
            if frame is not None and not frame.empty:
                sub = frame.loc[frame.index <= self.now]
                if not sub.empty:
                    price = float(sub["close"].iloc[-1])
            ticks[stock] = {
                "lastPrice": price,
                "bidPrice": [price],
                "askPrice": [price],
            }
        return ticks

    def query_stock_asset(self):
        if self.strategy is None:
            return BacktestAsset(self.capital, self.capital)
        return BacktestAsset(self.strategy.broker.getcash(), self.strategy.broker.getvalue())

    def query_stock_positions(self):
        if self.strategy is None:
            return []
        positions = []
        for stock, data in self.data_by_code.items():
            pos = self.strategy.getposition(data)
            if pos.size:
                positions.append(BacktestPosition(stock, pos.size, pos.price))
        return positions

    def previous_trading_date(self, current_date):
        current_dt = datetime.datetime.combine(current_date, datetime.time())
        dates = [item for item in self.calendar if item < current_dt]
        return dates[-1].date() if dates else current_date

    def is_trading_day(self, current_date):
        return any(item.date() == current_date for item in self.calendar)

    def paint(self, *args, **kwargs):
        del args, kwargs

    def draw_text(self, *args, **kwargs):
        del args, kwargs

    def draw_vertline(self, *args, **kwargs):
        del args, kwargs


def make_bt_classes(bt, strategy_module):
    class PandasStockData(bt.feeds.PandasData):
        params = (
            ("datetime", None),
            ("open", "open"),
            ("high", "high"),
            ("low", "low"),
            ("close", "close"),
            ("volume", "volume"),
            ("openinterest", -1),
        )

    class EquityCurve(bt.Analyzer):
        def start(self):
            self.rows = []

        def next(self):
            dt = self.strategy.datetime.date(0)
            self.rows.append({
                "date": dt.strftime("%Y-%m-%d"),
                "value": float(self.strategy.broker.getvalue()),
                "cash": float(self.strategy.broker.getcash()),
            })

        def get_analysis(self):
            return self.rows

    class LowVolBacktraderStrategy(bt.Strategy):
        params = (
            ("context", None),
            ("run_stoploss", False),
        )

        def start(self):
            self.context = self.p.context
            self.context.attach_strategy(self)
            self._last_date = None
            self.completed_orders = []
            self._old_submit_order = strategy_module._submit_order
            strategy_module._submit_order = self._submit_order

            strategy_module.g = strategy_module.G()
            strategy_module.init(self.context)
            strategy_module.g.enable_chart_diagnostics = False
            strategy_module.g.enable_timing_log = False
            if self.p.run_stoploss:
                strategy_module.g.run_stoploss = True
            strategy_module._update_calendar_state(self.context)
            strategy_module._sync_trade_state(self.context)

        def stop(self):
            strategy_module._submit_order = self._old_submit_order

        def next(self):
            dt = self.datetime.datetime(0).replace(tzinfo=None)
            if self._last_date == dt.date():
                return
            self._last_date = dt.date()
            self.context.set_now(dt)

            strategy_module._update_calendar_state(self.context)
            strategy_module._sync_trade_state(self.context)
            strategy_module._reset_bar_diagnostics()

            try:
                strategy_module.prepare_stock_list(self.context)
                strategy_module.rebalance_check(self.context)
                if strategy_module.g.run_stoploss:
                    strategy_module.check_stop_loss(self.context)
                strategy_module.trade_afternoon(self.context)
            except Exception as exc:
                print("Backtrader 日线任务失败 {}: {}".format(dt.strftime("%Y%m%d"), exc))

            strategy_module.g.profit_ratio = (
                self.broker.getvalue() - float(self.context.capital)
            ) / float(self.context.capital or 1.0)

        def _submit_order(self, ContextInfo, security, shares, side, reference_price):
            del ContextInfo, reference_price
            data = self.context.data_by_code.get(security)
            if data is None:
                print("Backtrader 下单跳过: {} 不在数据 feed 中".format(security))
                return False
            shares = int(shares)
            if shares <= 0:
                return False
            if side == "BUY":
                self.buy(data=data, size=shares)
            else:
                self.sell(data=data, size=shares)
            return True

        def notify_order(self, order):
            if order.status != order.Completed:
                return
            side = "BUY" if order.isbuy() else "SELL"
            row = {
                "date": self.datetime.date(0).strftime("%Y-%m-%d"),
                "stock": order.data._name,
                "side": side,
                "size": int(order.executed.size),
                "price": float(order.executed.price),
                "value": float(order.executed.value),
                "commission": float(order.executed.comm),
            }
            self.completed_orders.append(row)
            print("成交 {} {} {}股 price={:.3f} value={:.2f}".format(
                row["date"], row["stock"], row["size"], row["price"], row["value"]
            ))

    return PandasStockData, EquityCurve, LowVolBacktraderStrategy


def build_arg_parser():
    parser = argparse.ArgumentParser(description="MiniQMT + Backtrader 低波动小市值回测")
    parser.add_argument("--start", required=True, help="回测开始日期，如 20240101")
    parser.add_argument("--end", required=True, help="回测结束日期，如 20251231")
    parser.add_argument("--cash", type=float, default=1000000.0, help="初始资金")
    parser.add_argument("--period", default="1d", help="xtdata 周期，当前脚本按日线设计")
    parser.add_argument("--dividend-type", default="front", help="复权方式")
    parser.add_argument("--universe-index", default="399101.SZ", help="股票池指数/板块代码")
    parser.add_argument("--factor-benchmark", default="000300.SH", help="Barra HSIGMA 使用的市场基准")
    parser.add_argument("--report-benchmark", default="000300.SH", help="报告基准，仅用于记录")
    parser.add_argument("--warmup-days", type=int, default=760, help="开始日前额外拉取的预热自然日")
    parser.add_argument("--max-universe", type=int, default=0, help="调试用：限制股票池数量，0 表示不限制")
    parser.add_argument("--batch-size", type=int, default=300, help="xtdata 批量下载/读取数量")
    parser.add_argument("--no-download", action="store_true", help="不调用 download_history_data，仅读取本地已有数据")
    parser.add_argument("--run-stoploss", action="store_true", help="打开原策略止损逻辑")
    parser.add_argument("--commission", type=float, default=0.00025, help="佣金率")
    parser.add_argument("--stamp-duty", type=float, default=0.001, help="卖出印花税率，Backtrader 标准 broker 不单独建模，仅写入日志提示")
    parser.add_argument("--output-dir", default=str(DEFAULT_OUTPUT_DIR), help="输出目录")
    parser.add_argument("--plot", action="store_true", help="回测后绘图")
    return parser


def run_backtest(args):
    bt = require_backtrader()
    strategy_module = default_strategy_module

    start_date = parse_date(args.start)
    end_date = parse_date(args.end)
    fetch_start = start_date - datetime.timedelta(days=int(args.warmup_days))

    universe = get_sector_stocks(strategy_module, args.universe_index, args.max_universe)
    all_codes = list(dict.fromkeys(universe + [
        args.universe_index,
        args.factor_benchmark,
        args.report_benchmark,
    ]))

    if not args.no_download:
        download_history(
            all_codes,
            args.period,
            yyyymmdd(fetch_start),
            yyyymmdd(end_date),
            args.batch_size,
        )

    history = fetch_history(
        all_codes,
        args.period,
        yyyymmdd(fetch_start),
        yyyymmdd(end_date),
        args.dividend_type,
        args.batch_size,
    )

    calendar = make_master_calendar(history, args.factor_benchmark, start_date, end_date)
    if len(calendar) == 0:
        raise RuntimeError("回测区间内没有交易日: {} - {}".format(args.start, args.end))

    context = BacktraderContext(strategy_module, history, universe, args)
    context.calendar = calendar

    cerebro = bt.Cerebro(stdstats=True)
    cerebro.broker.setcash(float(args.cash))
    cerebro.broker.setcommission(commission=float(args.commission))
    cerebro.broker.set_coc(True)

    PandasStockData, EquityCurve, LowVolBacktraderStrategy = make_bt_classes(bt, strategy_module)
    loaded_count = 0
    skipped = []
    for stock in universe:
        frame = history.get(stock)
        if frame is None or frame.empty:
            skipped.append((stock, "无行情"))
            continue
        feed_frame = prepare_backtrader_frame(frame, calendar)
        if len(feed_frame) < 2:
            skipped.append((stock, "有效bar不足"))
            continue
        data = PandasStockData(dataname=feed_frame)
        cerebro.adddata(data, name=stock)
        loaded_count += 1

    if loaded_count == 0:
        raise RuntimeError("没有可加入 Backtrader 的股票数据。")
    if skipped:
        print("Backtrader feed 跳过 {} 只，示例 {}".format(len(skipped), skipped[:5]))
    print("Backtrader 已加载 {} 个股票数据 feed，交易日 {} 天".format(loaded_count, len(calendar)))
    print("提示: Backtrader 标准股票 broker 仅按 commission 建模，印花税 {} 未单独扣除。".format(args.stamp_duty))

    cerebro.addstrategy(LowVolBacktraderStrategy, context=context, run_stoploss=args.run_stoploss)
    cerebro.addanalyzer(bt.analyzers.DrawDown, _name="drawdown")
    cerebro.addanalyzer(bt.analyzers.SharpeRatio, _name="sharpe", timeframe=bt.TimeFrame.Days)
    cerebro.addanalyzer(bt.analyzers.TradeAnalyzer, _name="trades")
    cerebro.addanalyzer(EquityCurve, _name="equity")

    start_value = cerebro.broker.getvalue()
    print("Starting Portfolio Value: {:.2f}".format(start_value))
    results = cerebro.run()
    strategy = results[0]
    final_value = cerebro.broker.getvalue()
    print("Final Portfolio Value: {:.2f}".format(final_value))
    print("Total Return: {:.2%}".format(final_value / start_value - 1.0))

    drawdown = strategy.analyzers.drawdown.get_analysis()
    sharpe = strategy.analyzers.sharpe.get_analysis()
    trades = strategy.analyzers.trades.get_analysis()
    print("Max Drawdown: {:.2f}%".format(float(drawdown.get("max", {}).get("drawdown", 0.0))))
    print("Sharpe: {}".format(sharpe.get("sharperatio")))
    print("Closed Trades: {}".format(trades.get("total", {}).get("closed", 0)))

    output_dir = pathlib.Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    equity_rows = strategy.analyzers.equity.get_analysis()
    pd.DataFrame(equity_rows).to_csv(output_dir / "equity_curve.csv", index=False, encoding="utf-8-sig")
    pd.DataFrame(strategy.completed_orders).to_csv(output_dir / "orders.csv", index=False, encoding="utf-8-sig")
    print("结果已输出到: {}".format(output_dir.resolve()))

    if args.plot:
        cerebro.plot()


def main(argv=None):
    parser = build_arg_parser()
    args = parser.parse_args(argv)
    run_backtest(args)


if __name__ == "__main__":
    main()
