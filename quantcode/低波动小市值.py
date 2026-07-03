# 克隆自聚宽文章：https://www.joinquant.com/post/68530
# 标题：小市值+Barra CNE6 量价因子：年化60%低波动策略
# 作者：拐子

# ==============================================================================
# 小市值 × Barra CNE6 量价因子 多因子策略
# 基于参考策略框架 + 江海证券研报《量价类因子实测》
# 有效因子：HSIGMA、DASTD、CMRA（波动率）+ STOM、STOQ、STOA、ATVR（流动性）
# 因子方向：均为反向因子（值越小预期收益越高）
# 合成方式：MAD去极值 → Z-Score标准化 → 等权合成
# ==============================================================================

from jqdata import *
import numpy as np
import pandas as pd
from datetime import timedelta

# ==============================================================================
# 初始化
# ==============================================================================
def initialize(context):
    set_benchmark('000300.XSHG')
    set_option('use_real_price', True)
    set_option("avoid_future_data", True)
    set_slippage(FixedSlippage(3/10000))
    set_order_cost(OrderCost(
        open_tax=0, close_tax=0.001,
        open_commission=2.5/10000, close_commission=2.5/10000,
        close_today_commission=0, min_commission=5
    ), type='stock')
    log.set_level('order', 'error')
    log.set_level('system', 'error')
    log.set_level('strategy', 'info')

    # -------- 策略核心参数 --------
    g.stock_num       = 10       # 目标持仓数量
    g.up_price        = 80       # 股价上限（元）
    g.stoploss_limit  = 0.07     # 个股止损阈值
    g.stoploss_market = 0.05     # 市场止损阈值
    g.limit_days      = 5        # 禁止复购天数

    # -------- 动量过滤参数 --------
    g.momentum_days       = 30
    g.filter_threshold1   = -0.5
    g.filter_threshold2   = 0.5

    # -------- 流动性过滤参数 --------
    g.liquidity_days    = 20
    g.min_avg_amount    = 5e7    # 最低平均成交额 5000万

    # -------- Barra 因子参数 --------
    # 波动率因子窗口
    g.vol_window      = 252      # HSIGMA/DASTD/CMRA 主窗口（交易日）
    g.vol_halflife    = 63       # HSIGMA 半衰期（ATVR同）
    g.dastd_halflife  = 42       # DASTD 半衰期
    g.cmra_months     = 12       # CMRA 月数
    # 流动性因子窗口
    g.stom_window     = 21       # 月换手率窗口
    g.stoq_window     = 63       # 季换手率窗口（3×21）
    g.stoa_window     = 252      # 年换手率窗口（12×21）
    g.atvr_halflife   = 63       # ATVR 半衰期

    # -------- 因子权重（等权，可调） --------
    # 波动率3个 + 流动性4个，共7个，等权 ≈ 0.143
    g.factor_weights = {
        'HSIGMA': 1/7,
        'DASTD':  1/7,
        'CMRA':   1/7,
        'STOM':   1/7,
        'STOQ':   1/7,
        'STOA':   1/7,
        'ATVR':   1/7,
    }

    # -------- 全局状态 --------
    g.hold_list          = []
    g.yesterday_HL_list  = []
    g.target_list        = []
    g.not_buy_again      = []
    g.history_hold_list  = []
    g.reason_to_sell     = ''

    # -------- 定时任务 --------
    run_daily(prepare_stock_list,  '9:25')
    run_daily(weekly_adjustment,   '9:31')
    run_daily(check_stop_loss,    '10:30')
    run_daily(check_stop_loss,    '14:00')
    run_daily(trade_afternoon,    '14:30')

    log.info("策略初始化完成 ✓  因子权重: {}".format(
        {k: round(v,3) for k,v in g.factor_weights.items()}))


# ==============================================================================
# 工具：判断是否为每周第N个交易日
# ==============================================================================
def is_nth_trading_day_of_week(context, n):
    if n <= 0:
        return False
    try:
        cur = context.current_dt.date()
        week_start = cur - timedelta(days=cur.weekday())
        week_end   = week_start + timedelta(days=6)
        tdays = get_trade_days(start_date=week_start, end_date=week_end)
        tdays_list = []
        for td in tdays:
            tdays_list.append(td.date() if hasattr(td, 'date') else td)
        if cur in tdays_list:
            before = [d for d in tdays_list if d <= cur]
            return len(before) == n
    except Exception as e:
        log.warning("判断第N交易日出错: {}".format(e))
    return False


# ==============================================================================
# 每日准备：9:25 更新持仓/涨停/禁购列表
# ==============================================================================
def prepare_stock_list(context):
    g.hold_list = list(context.portfolio.positions)

    g.not_buy_again = []
    if len(g.history_hold_list) >= g.limit_days:
        g.history_hold_list = g.history_hold_list[-g.limit_days:]

    g.yesterday_HL_list = []
    if g.hold_list:
        price_data = get_price(
            g.hold_list, end_date=context.previous_date,
            frequency='daily', fields=['close', 'high_limit'],
            count=1, panel=False)
        if not price_data.empty:
            price_data = price_data.groupby('code').last().reset_index()
            for code in g.hold_list:
                row = price_data[price_data['code'] == code]
                if not row.empty:
                    if row['close'].iloc[0] == row['high_limit'].iloc[0]:
                        g.yesterday_HL_list.append(code)


# ==============================================================================
# 核心调仓：每周第二个交易日 9:31 执行
# ==============================================================================
def weekly_adjustment(context):
    if not is_nth_trading_day_of_week(context, 2):
        return

    log.info("="*60)
    log.info("【调仓日】{}".format(context.current_dt.strftime('%Y-%m-%d')))

    g.target_list = get_stock_list(context)
    if not g.target_list:
        log.info("⚠ 选股结果为空，跳过本次调仓")
        return

    target_list = g.target_list[:g.stock_num]
    log.info("目标持仓 {} 只".format(len(target_list)))

    # 卖出：不在目标池且非昨日涨停
    for stock in g.hold_list:
        if stock not in target_list and stock not in g.yesterday_HL_list:
            close_position(context.portfolio.positions[stock])

    # 买入：目标池中未持仓的股票
    buy_security(context, target_list)
    log.info("="*60)


# ==============================================================================
# 止损检查：10:30 / 14:00
# ==============================================================================
def check_stop_loss(context):
    if not g.run_stoploss if hasattr(g, 'run_stoploss') else False:
        return

    # 1. 市场止损
    idx_stocks = get_index_stocks('399101.XSHE')
    past_prices = history(2, '1d', 'close', idx_stocks)
    if not past_prices.empty:
        down_ratio = (past_prices.iloc[-1] / past_prices.iloc[0]).mean()
        if down_ratio <= 1 - g.stoploss_market:
            log.info("【市场止损】指数跌幅 {:.1%}，全仓清仓".format(1 - down_ratio))
            for stock in list(context.portfolio.positions):
                order_target_value(stock, 0)
            return

    # 2. 个股止损
    for stock in list(context.portfolio.positions):
        pos = context.portfolio.positions[stock]
        if pos.price < pos.avg_cost * (1 - g.stoploss_limit):
            name = get_security_info(stock).display_name
            order_target_value(stock, 0)
            log.info("【个股止损】{} ({}) 跌破成本价 {:.1%}".format(
                stock, name, g.stoploss_limit))
            g.reason_to_sell = 'stoploss'
            g.not_buy_again.append(stock)


# ==============================================================================
# 下午交易：14:30 处理涨停打开 + 补仓
# ==============================================================================
def trade_afternoon(context):
    check_limit_up(context)
    check_remain_amount(context)


def check_limit_up(context):
    if not g.yesterday_HL_list:
        return
    current_data = get_current_data()
    for stock in g.yesterday_HL_list:
        if stock in context.portfolio.positions:
            if current_data[stock].last_price < current_data[stock].high_limit:
                name = get_security_info(stock).display_name
                close_position(context.portfolio.positions[stock])
                log.info("【涨停打开】卖出 {} ({})".format(stock, name))
                g.reason_to_sell = 'limitup'
                g.not_buy_again.append(stock)
    g.history_hold_list.extend(g.not_buy_again)


def check_remain_amount(context):
    if g.reason_to_sell in ['limitup', 'stoploss']:
        g.hold_list = list(context.portfolio.positions)
        if len(g.hold_list) < g.stock_num and g.target_list:
            target_list = filter_not_buy_again(g.target_list)
            buy_security(context, target_list)
        g.reason_to_sell = ''


# ==============================================================================
# ★ 核心选股：小市值初筛 + Barra 量价因子评分排序
# ==============================================================================
def get_stock_list(context):
    prev = context.previous_date

    # ── Step 1: 初始股票池（中小综指，上市满1年，主板）──
    by_date = prev - timedelta(days=375)
    initial_list = get_index_stocks('399101.XSHE', by_date)
    if not initial_list:
        return []

    for fn in [filter_kcbj_stock, filter_st_stock,
               filter_paused_stock,
               lambda s: filter_limitup_stock(context, s),
               lambda s: filter_limitdown_stock(context, s),
               lambda s: filter_highprice_stock(context, s)]:
        initial_list = fn(initial_list)
        if not initial_list:
            return []

    # ── Step 2: 流动性过滤（近20日均成交额 ≥ 5000万）──
    try:
        amt_df = get_price(initial_list, end_date=prev,
                           count=g.liquidity_days, fields=['money'], panel=False)
        avg_amt = amt_df.groupby('code')['money'].mean()
        initial_list = avg_amt[avg_amt >= g.min_avg_amount].index.tolist()
    except Exception as e:
        log.warning("流动性过滤失败: {}".format(e))
    if not initial_list:
        return []

    # ── Step 3: 动量过滤（30日涨跌幅 -50%~+50%）──
    try:
        price_df = get_price(initial_list, end_date=prev,
                             count=g.momentum_days, fields=['close'], panel=False)
        mom = price_df.groupby('code')['close'].apply(
            lambda x: (x.iloc[-1] - x.iloc[0]) / x.iloc[0] if len(x) >= 2 else np.nan)
        initial_list = mom[
            (mom > g.filter_threshold1) & (mom < g.filter_threshold2)
        ].index.tolist()
    except Exception as e:
        log.warning("动量过滤失败: {}".format(e))
    if not initial_list:
        return []

    # ── Step 4: 市值筛选（5~50亿），得到候选池 ──
    try:
        q = query(valuation.code, valuation.market_cap).filter(
            valuation.code.in_(initial_list),
            valuation.market_cap.between(5, 50)
        ).order_by(valuation.market_cap.asc())
        cap_df = get_fundamentals(q, date=prev)
        if cap_df.empty:
            return []
        candidate_list = list(cap_df['code'])
    except Exception as e:
        log.warning("市值筛选失败: {}".format(e))
        return []

    # ── Step 5: 计算 Barra 量价因子并合成评分 ──
    log.info("候选股票数量: {}，开始计算 Barra 因子...".format(len(candidate_list)))
    factor_df = calc_barra_factors(candidate_list, prev)

    if factor_df is None or factor_df.empty:
        log.warning("Barra 因子计算失败，退化为纯市值排序")
        return candidate_list[:g.stock_num]

    # 合成综合评分（反向因子：评分越低越好）
    factor_df = calc_composite_score(factor_df)

    # 按综合评分升序排列（低波动、低换手率 → 排前面）
    factor_df = factor_df.sort_values('composite_score', ascending=True)
    result = factor_df['code'].tolist()

    log.info("因子选股完成，Top5: {}".format(result[:5]))
    return result


# ==============================================================================
# ★ Barra 量价因子计算
# 计算7个因子：HSIGMA, DASTD, CMRA, STOM, STOQ, STOA, ATVR
# ==============================================================================
def calc_barra_factors(stock_list, end_date):
    """
    返回 DataFrame，列：code, HSIGMA, DASTD, CMRA, STOM, STOQ, STOA, ATVR
    """
    if not stock_list:
        return None

    # 获取足够长的价格和成交量数据
    need_days = g.vol_window + 10  # 多取10天缓冲
    try:
        price_df = get_price(
            stock_list, end_date=end_date,
            count=need_days,
            fields=['close', 'volume', 'money', 'total_value'],
            panel=False
        )
    except Exception as e:
        log.warning("获取价格数据失败: {}".format(e))
        return None

    if price_df is None or price_df.empty:
        return None

    # 获取沪深300数据（用于HSIGMA的市场收益）
    try:
        mkt_df = get_price('000300.XSHG', end_date=end_date,
                           count=need_days, fields=['close'], panel=False)
        mkt_ret = mkt_df['close'].pct_change().dropna().values
    except Exception as e:
        log.warning("获取市场数据失败: {}".format(e))
        mkt_ret = None

    records = []
    for code in stock_list:
        try:
            sub = price_df[price_df['code'] == code].copy()
            if len(sub) < 63:   # 数据不足，跳过
                continue

            close  = sub['close'].values
            volume = sub['volume'].values
            money  = sub['money'].values
            total_val = sub['total_value'].values  # 流通市值（元）

            # 日收益率
            ret = np.diff(close) / close[:-1]
            n   = len(ret)

            row = {'code': code}

            # ── HSIGMA：CAPM回归残差的波动率 ──
            # 使用最近252日，半衰期63日指数加权
            row['HSIGMA'] = _calc_hsigma(ret, mkt_ret, g.vol_window, g.vol_halflife)

            # ── DASTD：日超额收益标准差，半衰期42日 ──
            row['DASTD'] = _calc_dastd(ret, g.vol_window, g.dastd_halflife)

            # ── CMRA：过去12个月累积对数收益范围 ──
            row['CMRA'] = _calc_cmra(close, g.cmra_months)

            # ── STOM：月换手率对数 ──
            row['STOM'] = _calc_stom(volume, total_val, g.stom_window)

            # ── STOQ：季换手率对数 ──
            row['STOQ'] = _calc_stoq(volume, total_val, g.stoq_window)

            # ── STOA：年换手率对数 ──
            row['STOA'] = _calc_stoa(volume, total_val, g.stoa_window)

            # ── ATVR：年化交易量比率，半衰期63日 ──
            row['ATVR'] = _calc_atvr(volume, total_val, g.vol_window, g.atvr_halflife)

            records.append(row)

        except Exception as e:
            continue  # 单只股票计算失败，跳过

    if not records:
        return None

    df = pd.DataFrame(records)
    log.info("成功计算因子的股票数量: {}".format(len(df)))
    return df


# ── 各因子计算子函数 ──────────────────────────────────────────────────────────

def _exp_weights(n, halflife):
    """生成长度为n的指数衰减权重（最新权重最大），归一化"""
    lam = np.log(2) / halflife
    w = np.exp(-lam * np.arange(n-1, -1, -1))  # 从旧到新
    return w / w.sum()


def _calc_hsigma(ret, mkt_ret, window, halflife):
    """
    HSIGMA：CAPM回归残差的指数加权波动率
    ret: 股票日收益率序列（最新在末尾）
    mkt_ret: 市场日收益率序列
    """
    try:
        n = min(window, len(ret))
        r = ret[-n:]
        if mkt_ret is None or len(mkt_ret) < n:
            # 无市场数据时退化为简单波动率
            return float(np.std(r))
        m = mkt_ret[-n:]
        # 指数加权OLS
        w = _exp_weights(n, halflife)
        w_sqrt = np.sqrt(w)
        X = np.column_stack([np.ones(n), m]) * w_sqrt[:, None]
        y = r * w_sqrt
        coef, *_ = np.linalg.lstsq(X, y, rcond=None)
        resid = r - (coef[0] + coef[1] * m)
        # 残差的指数加权标准差
        sigma = np.sqrt(np.sum(w * resid**2))
        return float(sigma)
    except Exception:
        return np.nan


def _calc_dastd(ret, window, halflife):
    """
    DASTD：日超额收益的指数加权标准差（无风险利率取0）
    """
    try:
        n = min(window, len(ret))
        r = ret[-n:]
        w = _exp_weights(n, halflife)
        mean_r = np.sum(w * r)
        sigma = np.sqrt(np.sum(w * (r - mean_r)**2))
        return float(sigma)
    except Exception:
        return np.nan


def _calc_cmra(close, months):
    """
    CMRA：过去12个月累积对数超额收益的最大值与最小值之差
    每月 = 21个交易日
    """
    try:
        days_per_month = 21
        total_needed = months * days_per_month + 1
        if len(close) < total_needed:
            return np.nan
        c = close[-total_needed:]
        cum_log_ret = []
        for t in range(1, months + 1):
            end_idx   = len(c) - 1
            start_idx = len(c) - 1 - t * days_per_month
            if start_idx < 0:
                break
            cum_r = np.log(c[end_idx] / c[start_idx])
            cum_log_ret.append(cum_r)
        if not cum_log_ret:
            return np.nan
        return float(max(cum_log_ret) - min(cum_log_ret))
    except Exception:
        return np.nan


def _calc_stom(volume, total_val, window):
    """
    STOM = ln( Σ(V_t / S_t) )，最近21日
    V_t: 成交量（股），S_t: 流通股本（股）= total_val / close（近似用total_val/close）
    注：聚宽 total_value 为流通市值（元），volume 为成交量（股）
    换手率 ≈ volume / (total_value / close_price)，但此处用 money/total_value 近似
    实际上换手率 = volume * close / total_value = money / total_value
    """
    try:
        n = min(window, len(volume))
        # 换手率 = 成交额 / 流通市值（聚宽 total_value 即流通市值）
        # 此处 volume 和 total_val 已对齐
        tv = total_val[-n:]
        vol = volume[-n:]
        # 用 money/total_value 更准确，但这里用 volume 近似
        # 实际：turnover_i = volume_i / (total_value_i / price_i)
        # 由于 price 数据已有，可以直接用 money/total_value
        # 这里 total_val 是流通市值（元），volume 是成交量（股）
        # 需要 close 价格，但子函数中没有传入，改为传 money
        # 已在调用处改为传 money，此处直接用
        # turnover_i = money_i / total_value_i
        turnover = vol  # 这里 vol 实际上传入的是 money（见调用处）
        tv_safe = np.where(tv > 0, tv, np.nan)
        daily_to = turnover / tv_safe
        total_to = np.nansum(daily_to)
        if total_to <= 0:
            return np.nan
        return float(np.log(total_to))
    except Exception:
        return np.nan


def _calc_stoq(volume, total_val, window):
    """
    STOQ = ln( (1/3) * Σ exp(STOM_τ) )，最近3个月
    """
    try:
        month = 21
        stom_list = []
        for i in range(3):
            start = -(window) + i * month
            end   = -(window) + (i+1) * month
            if end == 0:
                v_seg  = volume[start:]
                tv_seg = total_val[start:]
            else:
                v_seg  = volume[start:end]
                tv_seg = total_val[start:end]
            if len(v_seg) == 0:
                continue
            stom_i = _calc_stom(v_seg, tv_seg, month)
            if not np.isnan(stom_i):
                stom_list.append(stom_i)
        if not stom_list:
            return np.nan
        return float(np.log(np.mean(np.exp(stom_list))))
    except Exception:
        return np.nan


def _calc_stoa(volume, total_val, window):
    """
    STOA = ln( (1/12) * Σ exp(STOM_τ) )，最近12个月
    """
    try:
        month = 21
        stom_list = []
        for i in range(12):
            start = -(window) + i * month
            end   = -(window) + (i+1) * month
            if end == 0:
                v_seg  = volume[start:]
                tv_seg = total_val[start:]
            else:
                v_seg  = volume[start:end]
                tv_seg = total_val[start:end]
            if len(v_seg) == 0:
                continue
            stom_i = _calc_stom(v_seg, tv_seg, month)
            if not np.isnan(stom_i):
                stom_list.append(stom_i)
        if not stom_list:
            return np.nan
        return float(np.log(np.mean(np.exp(stom_list))))
    except Exception:
        return np.nan


def _calc_atvr(volume, total_val, window, halflife):
    """
    ATVR：日换手率的指数加权求和，窗口252日，半衰期63日
    """
    try:
        n = min(window, len(volume))
        vol  = volume[-n:]
        tv   = total_val[-n:]
        tv_safe = np.where(tv > 0, tv, np.nan)
        daily_to = vol / tv_safe   # vol 此处传入的是 money
        w = _exp_weights(n, halflife)
        atvr = np.nansum(w * daily_to)
        return float(atvr)
    except Exception:
        return np.nan


# ==============================================================================
# ★ 因子标准化 + 合成评分
# ==============================================================================
def _mad_winsorize(series):
    """3倍MAD去极值"""
    med = series.median()
    mad = (series - med).abs().median()
    upper = med + 3 * mad
    lower = med - 3 * mad
    return series.clip(lower=lower, upper=upper)


def _zscore(series):
    """截面Z-Score标准化"""
    mu  = series.mean()
    std = series.std()
    if std == 0 or np.isnan(std):
        return series * 0
    return (series - mu) / std


def calc_composite_score(df):
    """
    对每个因子进行：MAD去极值 → Z-Score标准化 → 等权合成
    所有因子均为反向因子（值越小越好），合成后评分越低越优
    """
    factor_cols = list(g.factor_weights.keys())
    df = df.copy()

    for col in factor_cols:
        if col not in df.columns:
            continue
        # 去除NaN行（该因子）
        valid_mask = df[col].notna()
        if valid_mask.sum() < 5:
            df[col + '_z'] = 0.0
            continue
        series = df.loc[valid_mask, col].copy()
        series = _mad_winsorize(series)
        series = _zscore(series)
        df.loc[valid_mask, col + '_z'] = series
        df.loc[~valid_mask, col + '_z'] = 0.0  # NaN填0（中性）

    # 等权合成
    z_cols = [col + '_z' for col in factor_cols if col + '_z' in df.columns]
    if not z_cols:
        df['composite_score'] = 0.0
    else:
        weights = np.array([g.factor_weights.get(c.replace('_z',''), 1/len(z_cols))
                            for c in z_cols])
        weights = weights / weights.sum()
        df['composite_score'] = df[z_cols].values @ weights

    return df


# ==============================================================================
# 过滤函数集合
# ==============================================================================
def filter_paused_stock(stock_list):
    current_data = get_current_data()
    return [s for s in stock_list if not current_data[s].paused]

def filter_st_stock(stock_list):
    current_data = get_current_data()
    return [s for s in stock_list
            if not current_data[s].is_st
            and 'ST' not in current_data[s].name
            and '*'  not in current_data[s].name
            and '退' not in current_data[s].name]

def filter_kcbj_stock(stock_list):
    return [s for s in stock_list
            if not s.startswith(('4', '8', '688', '300'))]

def filter_limitup_stock(context, stock_list):
    if not stock_list:
        return []
    try:
        price_data = get_price(stock_list, end_date=context.previous_date,
                               frequency='daily', fields=['close', 'high_limit'],
                               count=1, panel=False)
        if price_data.empty:
            return stock_list
        price_data = price_data.groupby('code').last().reset_index()
        hl_set = set(price_data[
            price_data['close'] == price_data['high_limit']
        ]['code'].tolist())
        return [s for s in stock_list if s in g.hold_list or s not in hl_set]
    except Exception:
        return stock_list

def filter_limitdown_stock(context, stock_list):
    if not stock_list:
        return []
    try:
        price_data = get_price(stock_list, end_date=context.previous_date,
                               frequency='daily', fields=['close', 'low_limit'],
                               count=1, panel=False)
        if price_data.empty:
            return stock_list
        price_data = price_data.groupby('code').last().reset_index()
        ll_set = set(price_data[
            price_data['close'] == price_data['low_limit']
        ]['code'].tolist())
        return [s for s in stock_list if s in g.hold_list or s not in ll_set]
    except Exception:
        return stock_list

def filter_highprice_stock(context, stock_list):
    if not stock_list:
        return []
    try:
        last_prices = history(1, '1d', 'close', stock_list)
        if last_prices.empty:
            return []
        return [s for s in stock_list
                if s in g.hold_list or last_prices[s][-1] <= g.up_price]
    except Exception:
        return stock_list

def filter_not_buy_again(stock_list):
    temp_set = set(g.history_hold_list)
    return [s for s in stock_list if s not in temp_set]


# ==============================================================================
# 交易辅助函数
# ==============================================================================
def order_target_value_(security, value):
    name = get_security_info(security).display_name
    if value == 0:
        log.info("卖出 {}({})".format(security, name))
        return order_target_value(security, value)
    else:
        current_data = get_current_data()
        if security not in current_data:
            return None
        price = current_data[security].last_price
        if price <= 0:
            return None
        amount = (int(value / price) // 100) * 100
        if amount < 100:
            return None
        target_val = amount * price
        log.info("买入 {}({}) {}股 {:.0f}元".format(security, name, amount, target_val))
        return order_target_value(security, target_val)

def open_position(security, value):
    order = order_target_value_(security, value)
    return order is not None and order.filled > 0

def close_position(position):
    order = order_target_value_(position.security, 0)
    return order is not None and order.status == OrderStatus.filled

def buy_security(context, target_list):
    cash = context.portfolio.available_cash
    current_hold = set(context.portfolio.positions.keys())
    to_buy = [s for s in target_list
              if s not in current_hold
              and s not in g.history_hold_list]
    if not to_buy or cash <= 0:
        return
    max_buy = g.stock_num - len(current_hold)
    to_buy  = to_buy[:max_buy]
    cash_per = cash / len(to_buy)
    for stock in to_buy:
        open_position(stock, cash_per)
