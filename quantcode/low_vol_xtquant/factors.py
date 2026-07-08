# coding: utf-8
"""Barra-style factor calculation for the low-volatility small-cap strategy."""

import numpy as np
import pandas as pd


TRADING_DAYS_PER_MONTH = 21


def reason_counts(items):
    counts = {}
    for item in items:
        reason = item[1] if isinstance(item, tuple) and len(item) > 1 else str(item)
        counts[reason] = counts.get(reason, 0) + 1
    return counts


def calc_barra_factors(
    ContextInfo,
    stock_list,
    g,
    history_func,
    normalize_share_count_func,
    turnover_share_map=None,
):
    if not stock_list:
        return None

    need_days = g.vol_window + 10
    turnover_share_map = turnover_share_map or {}

    close_data = history_func(ContextInfo, stock_list, need_days, "close")
    raw_close_data = history_func(ContextInfo, stock_list, need_days, "close", dividend_type="none")
    money_data = history_func(ContextInfo, stock_list, need_days, "money")
    required_data = {
        "close": close_data,
        "raw_close": raw_close_data,
        "amount": money_data,
    }
    for field_name, data in required_data.items():
        if not data:
            raise RuntimeError("Barra 因子计算失败: QMT {} 字段无数据".format(field_name))

    mkt_close = history_func(ContextInfo, [g.factor_benchmark], need_days, "close").get(
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
            float_share = normalize_share_count_func(turnover_share_map.get(code))

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
            len(skipped), reason_counts(skipped), skipped[:5]
        ))
    if not records:
        return None

    df = pd.DataFrame(records)
    print("成功计算因子的股票数量: {}".format(len(df)))
    return df


def calc_composite_score(df, factor_weights):
    factor_cols = list(factor_weights.keys())
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

    weights = np.array([factor_weights[c.replace("_z", "")] for c in z_cols])
    weights = weights / weights.sum()
    df["composite_score"] = df[z_cols].values @ weights
    return df


def _stock_sample(stock_list, limit=5):
    stocks = list(stock_list or [])
    suffix = "..." if len(stocks) > limit else ""
    return "{}{}".format(stocks[:limit], suffix)


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
