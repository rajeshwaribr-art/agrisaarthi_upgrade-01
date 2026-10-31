"""Short-term mandi price forecasting with scikit-learn.

Approach (kept deliberately simple and honest):
  * Prices are put on a continuous daily grid (short gaps forward-filled).
  * A Ridge regression predicts the next-day change in log price from recent
    changes, deviation from the 7-day mean and the day of year (seasonality).
  * It forecasts 7 days ahead recursively.
  * A rolling-origin back-test compares the model with a "price stays the same"
    baseline. If the model does not beat the baseline, the baseline is used.
"""
import numpy as np
import pandas as pd
from sklearn.linear_model import Ridge
from sklearn.pipeline import make_pipeline
from sklearn.preprocessing import StandardScaler

HORIZON = 7
MIN_POINTS = 50          # continuous daily points needed before we forecast
MIN_TRAIN = 40           # smallest training window used in the back-test
MAX_DAILY_MOVE = 0.08    # clip predicted one-day log change (about 8 %)
_N_FEATURES = 8


def prepare_series(dates, prices):
    """Return a continuous daily pandas Series (latest unbroken stretch)."""
    s = pd.Series(np.asarray(prices, dtype=float), index=pd.to_datetime(list(dates)))
    s = s[~s.index.duplicated(keep="last")].sort_index()
    s = s[s > 0]
    if s.empty:
        return s
    s = s.resample("D").last().ffill(limit=7).dropna()
    gaps = np.where(np.diff(s.index.values).astype("timedelta64[D]").astype(int) > 1)[0]
    if len(gaps):
        s = s.iloc[gaps[-1] + 1:]
    return s


def _features(y, date):
    doy = date.dayofyear
    ang = 2 * np.pi * doy / 365.25
    last = y[-1]
    return [
        last - y[-2], last - y[-3], last - y[-4], last - y[-8], last - y[-15],
        last - float(np.mean(y[-8:-1])),
        np.sin(ang), np.cos(ang),
    ]


def _fit(y, index):
    X, t = [], []
    for i in range(15, len(y)):
        X.append(_features(y[:i], index[i]))
        t.append(y[i] - y[i - 1])
    model = make_pipeline(StandardScaler(), Ridge(alpha=10.0))
    model.fit(np.asarray(X), np.asarray(t))
    return model


def _forecast_log(model, y, last_date, horizon):
    y = list(y)
    out = []
    for k in range(1, horizon + 1):
        d = last_date + pd.Timedelta(days=k)
        step = float(model.predict(np.asarray([_features(np.asarray(y), d)]))[0])
        step = max(-MAX_DAILY_MOVE, min(MAX_DAILY_MOVE, step))
        y.append(y[-1] + step)
        out.append(y[-1])
    return np.asarray(out)


def _backtest(y, index):
    """Mean abs % error over 7-day horizons for the model and the naive baseline."""
    n = len(y)
    model_err, naive_err = [], []
    for back in range(0, 22, 3):
        origin = n - HORIZON - back
        if origin < MIN_TRAIN:
            break
        m = _fit(y[:origin], index[:origin])
        pred = np.exp(_forecast_log(m, y[:origin], index[origin - 1], HORIZON))
        actual = np.exp(y[origin:origin + HORIZON])
        naive = np.full(HORIZON, np.exp(y[origin - 1]))
        model_err.append(float(np.mean(np.abs(pred - actual) / actual)))
        naive_err.append(float(np.mean(np.abs(naive - actual) / actual)))
    if not model_err:
        return None, None
    return 100 * float(np.mean(model_err)), 100 * float(np.mean(naive_err))


def forecast_series(dates, prices, horizon=HORIZON):
    """Forecast the next `horizon` days of modal price (Rs/quintal)."""
    s = prepare_series(dates, prices)
    result = {"n_points": int(len(s)), "forecast": [], "method": "insufficient_history",
              "mape": None, "naive_mape": None}
    if len(s) < MIN_POINTS:
        return result
    y = np.log(s.values)
    index = s.index
    model_mape, naive_mape = _backtest(y, index)
    use_model = model_mape is not None and model_mape < 0.98 * naive_mape
    if use_model:
        yf = _forecast_log(_fit(y, index), y, index[-1], horizon)
        vals = np.exp(yf)
        result["method"] = "ridge"
        result["mape"] = round(model_mape, 1)
    else:
        vals = np.full(horizon, float(s.values[-1]))
        result["method"] = "persistence"
        result["mape"] = None if naive_mape is None else round(naive_mape, 1)
    result["naive_mape"] = None if naive_mape is None else round(naive_mape, 1)
    last = index[-1]
    result["forecast"] = [
        {"date": (last + pd.Timedelta(days=k + 1)).strftime("%Y-%m-%d"), "modal": round(float(v), 1)}
        for k, v in enumerate(vals)
    ]
    return result
