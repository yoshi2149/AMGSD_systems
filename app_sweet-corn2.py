# -*- coding: utf-8 -*-
from flask import Flask, request, jsonify
from datetime import datetime, timedelta, date
from pathlib import Path
import pandas as pd
import numpy as np
import math

import AMD_Tools4 as amd

app = Flask(__name__)


# =========================================================
# 定数
# =========================================================
DATE_FMT = "%Y-%m-%d"
DL_CSV_PATH = Path(__file__).resolve().parent / "sweetcorn_data-DL.csv"


# =========================================================
# 基本ユーティリティ
# =========================================================
def to_date(s: str) -> date:
    return datetime.fromisoformat(s).date()


def parse_float(value, allow_none=False):
    """
    空文字・None を許容する float パーサ
    """
    if value is None or value == "":
        if allow_none:
            return None
        raise ValueError("Required numeric field is missing.")
    return float(value)


def parse_int(value):
    if value is None or value == "":
        raise ValueError("Required integer field is missing.")
    return int(float(value))


def round_or_none(x, ndigits=1):
    if x is None:
        return None
    if isinstance(x, float) and math.isnan(x):
        return None
    return round(float(x), ndigits)


def replace_nan_with_none(data):
    if isinstance(data, list):
        return [replace_nan_with_none(x) for x in data]
    if isinstance(data, dict):
        return {k: replace_nan_with_none(v) for k, v in data.items()}
    if isinstance(data, float) and math.isnan(data):
        return None
    return data


def to_iso_or_none(d):
    if d is None:
        return None
    if isinstance(d, (datetime, date)):
        return d.isoformat()
    return d


# =========================================================
# 入力パラメータ
# =========================================================
def parse_request_payload(d: dict) -> dict:
    """
    GAS から受け取る JSON を厳密に解釈
    """
    params = {
        "lat": float(d["lat"]),
        "lon": float(d["lon"]),

        "ct1_start": to_date(d["ct1_start"]),
        "ct1_end": to_date(d["ct1_end"]),
        "method1": parse_int(d["method1"]),
        "base_threshold1": parse_float(d["base_threshold1"]),
        "ceiling_threshold1": parse_float(d.get("ceiling_threshold1"), allow_none=True),
        "gdd1_target": parse_float(d["gdd1"]),

        "ct2_start": to_date(d["ct2_start"]),
        "ct2_end": to_date(d["ct2_end"]),
        "method2": parse_int(d["method2"]),
        "base_threshold2": parse_float(d["base_threshold2"]),
        "ceiling_threshold2": parse_float(d.get("ceiling_threshold2"), allow_none=True),
        "gdd2_target": parse_float(d["gdd2"]),
    }
    return params


# =========================================================
# 年度計算
# =========================================================
def get_fiscal_year_today():
    today = datetime.utcnow().date()
    fiscal_year = today.year if today.month >= 4 else today.year - 1
    return today, fiscal_year


# =========================================================
# 気象データ取得
# =========================================================
def fetch_point_series(var_name: str, start_date: str, end_date: str, lat: float, lon: float):
    arr, tim, *_ = amd.GetMetData(var_name, [start_date, end_date], [lat, lat, lon, lon])
    values = arr[:, 0, 0]
    dates = pd.to_datetime(tim).map(lambda x: x.date())
    return dates, values


def build_average_temperature(lat: float, lon: float, fiscal_year: int, n_years: int = 3) -> pd.DataFrame:
    """
    過去 n_years の 4/1〜翌3/31 の TMP_mea から month_day 平年値を作る
    """
    all_years = []
    start_year = fiscal_year - n_years

    for year in range(start_year, fiscal_year):
        start = f"{year}-04-01"
        end = f"{year + 1}-03-31"
        dates, values = fetch_point_series("TMP_mea", start, end, lat, lon)

        df = pd.DataFrame({
            "datetime": pd.to_datetime(dates),
            "tave": values
        })
        df["month_day"] = df["datetime"].dt.strftime("%m-%d")
        all_years.append(df[["month_day", "tave"]])

    df_concat = pd.concat(all_years, ignore_index=True)
    df_avg = df_concat.groupby("month_day", as_index=False)["tave"].mean()
    df_avg.rename(columns={"tave": "tave_avg"}, inplace=True)
    df_avg["tave_avg"] = df_avg["tave_avg"].round(1)

    # 4/1 始まりに並べ替え
    df_avg["sort_key"] = pd.to_datetime("2000-" + df_avg["month_day"])
    df_avg = df_avg.sort_values("sort_key").reset_index(drop=True)
    start_idx = df_avg.index[df_avg["month_day"] == "04-01"][0]
    df_avg = pd.concat([df_avg.iloc[start_idx:], df_avg.iloc[:start_idx]], ignore_index=True)
    df_avg = df_avg.drop(columns="sort_key")

    return df_avg


def build_this_year_dataframe(lat: float, lon: float, fiscal_year: int, today: date, df_avg: pd.DataFrame) -> pd.DataFrame:
    """
    今年度の実測値＋予報/平年補完用データを作る
    必要変数:
      TMP_mea, TMP_max, TMP_min, APCPRA
    """
    start_this = f"{fiscal_year}-04-01"
    end_this = f"{fiscal_year + 1}-03-31"

    dates, tmean = fetch_point_series("TMP_mea", start_this, end_this, lat, lon)
    _, tmax = fetch_point_series("TMP_max", start_this, end_this, lat, lon)
    _, tmin = fetch_point_series("TMP_min", start_this, end_this, lat, lon)
    _, prcp = fetch_point_series("APCPRA", start_this, end_this, lat, lon)

    df = pd.DataFrame({
        "date": dates,
        "tave_this": tmean,
        "tmax_this": tmax,
        "tmin_this": tmin,
        "prcp_this": prcp
    })

    yesterday = today - timedelta(days=1)
    forecast_end = today + timedelta(days=26)

    def assign_tag(d):
        if d <= yesterday:
            return "past"
        elif d <= forecast_end:
            return "forecast"
        else:
            return "normal"

    df["tag"] = df["date"].map(assign_tag)

    # 平年値 merge
    df["month_day"] = df["date"].map(lambda d: d.strftime("%m-%d"))
    df = df.merge(df_avg[["month_day", "tave_avg"]], on="month_day", how="left")

    # normal 部分だけ 平年値で tave を置換
    normal_mask = df["tag"] == "normal"
    df.loc[normal_mask, "tave_this"] = df.loc[normal_mask, "tave_avg"]

    df.drop(columns=["month_day", "tave_avg"], inplace=True)
    return df


# =========================================================
# 日長CSV
# =========================================================
def load_daylength_table(csv_path: Path = DL_CSV_PATH) -> pd.DataFrame:
    """
    sweetcorn_data-DL.csv を読み込む
    想定列:
      date, DL
    例:
      4/1, 0.5305555556
    """
    if not csv_path.exists():
        raise FileNotFoundError(f"Daylength CSV not found: {csv_path}")

    df_dl = pd.read_csv(csv_path)

    required_cols = {"date", "DL"}
    if not required_cols.issubset(df_dl.columns):
        raise ValueError(f"Daylength CSV must contain columns: {required_cols}")

    df_dl = df_dl.copy()
    df_dl["month_day"] = df_dl["date"].astype(str).str.strip()
    df_dl["DL"] = pd.to_numeric(df_dl["DL"], errors="coerce")

    if df_dl["DL"].isna().any():
        bad_rows = df_dl[df_dl["DL"].isna()]
        raise ValueError(f"Invalid DL values found in daylength CSV: {bad_rows.to_dict(orient='records')}")

    # 念のため重複除去
    df_dl = df_dl.drop_duplicates(subset=["month_day"]).reset_index(drop=True)

    return df_dl[["month_day", "DL"]]


def add_daylength_from_csv(df: pd.DataFrame, df_dl_master: pd.DataFrame) -> pd.DataFrame:
    """
    date 列から month_day を作り、CSVの日長を結合して DL_hours を追加
    """
    if df.empty:
        out = df.copy()
        out["DL_hours"] = pd.Series(dtype=float)
        return out

    out = df.copy()
    out["month_day"] = out["date"].map(lambda d: f"{d.month}/{d.day}")
    out = out.merge(df_dl_master, on="month_day", how="left")

    if out["DL"].isna().any():
        missing = out.loc[out["DL"].isna(), "month_day"].drop_duplicates().tolist()
        raise ValueError(f"Missing daylength data for month/day: {missing}")

    out["DL_hours"] = out["DL"].astype(float)
    out = out.drop(columns=["month_day", "DL"])
    return out


# =========================================================
# GDD 計算
# =========================================================
def validate_method_and_thresholds(method: int, t_base: float, t_ceiling):
    if method not in range(1, 9):
        raise ValueError(f"Unsupported method: {method}. method must be 1-8.")

    if method in [3, 4, 7, 8] and t_ceiling is None:
        raise ValueError(f"Method {method} requires ceiling_threshold.")

    if t_ceiling is not None and t_ceiling < t_base:
        raise ValueError("ceiling_threshold must be >= base_threshold.")


def calc_daily_gdd_core(tmean, tmax, method, t_base, t_ceiling=None):
    """
    method 1~4 のコア部分（日長なし）
    """
    if method == 1:
        return max(0.0, tmean - t_base)

    if method == 2:
        return max(0.0, tmax - t_base)

    if method == 3:
        if tmax <= t_base:
            return 0.0
        if tmax <= t_ceiling:
            val = tmax - t_base
        else:
            val = (2 * t_ceiling - tmax) - t_base
        return max(0.0, val)

    if method == 4:
        if tmean <= t_base:
            return 0.0
        if tmax <= t_ceiling:
            val = tmean - t_base
        else:
            val = (tmean - (tmax - t_ceiling)) - t_base
        return max(0.0, val)

    raise ValueError(f"Unsupported core method: {method}")


def calc_daily_gdd(row, method, t_base, t_ceiling=None):
    """
    method 1~8 を1日単位で計算
    5~8 は 1~4 に日長 DL を乗ずる
    """
    if method in [1, 2, 3, 4]:
        return calc_daily_gdd_core(
            tmean=row["tave_this"],
            tmax=row["tmax_this"],
            method=method,
            t_base=t_base,
            t_ceiling=t_ceiling
        )

    core_method = method - 4
    core = calc_daily_gdd_core(
        tmean=row["tave_this"],
        tmax=row["tmax_this"],
        method=core_method,
        t_base=t_base,
        t_ceiling=t_ceiling
    )
    dl = row["DL_hours"]
    return core * dl


def build_accumulation_dataframe(
    df_src: pd.DataFrame,
    start_date: date,
    end_date: date,
    method: int,
    t_base: float,
    t_ceiling,
    target_gdd: float,
    df_dl_master: pd.DataFrame
):
    """
    任意期間の積算 DataFrame を作成し、
    target_gdd に最も近い日も返す
    """
    validate_method_and_thresholds(method, t_base, t_ceiling)

    mask = (df_src["date"] >= start_date) & (df_src["date"] <= end_date)
    df = df_src.loc[mask].copy().reset_index(drop=True)

    if df.empty:
        return df, {
            "date": None,
            "cum_ct": None,
            "daily_ct": None,
            "abs_diff": None
        }

    if method in [5, 6, 7, 8]:
        df = add_daylength_from_csv(df, df_dl_master)
    else:
        df["DL_hours"] = np.nan

    df["daily_ct"] = df.apply(
        lambda row: calc_daily_gdd(row, method, t_base, t_ceiling),
        axis=1
    )
    df["daily_ct"] = df["daily_ct"].round(1)
    df["cum_ct"] = df["daily_ct"].cumsum().round(1)

    df["daily_pr"] = df["prcp_this"].round(1)
    df["cum_pr"] = df["daily_pr"].cumsum().round(1)

    df["abs_diff"] = (df["cum_ct"] - target_gdd).abs().round(1)
    idx = df["abs_diff"].idxmin()
    row_close = df.loc[idx]

    closest = {
        "date": row_close["date"].isoformat(),
        "cum_ct": round_or_none(row_close["cum_ct"], 1),
        "daily_ct": round_or_none(row_close["daily_ct"], 1),
        "abs_diff": round_or_none(row_close["abs_diff"], 1),
    }
    return df, closest


# =========================================================
# 履歴集計
# =========================================================
def make_hist_dict_simple_ct(date_start: date, date_end: date, df_src: pd.DataFrame):
    """
    開発概要の『単純な積算気温』版
    Σ max(0, TMP_mea)
    累積降水量 Σ prcp_this
    """
    if date_end < date_start:
        return {"date": None, "cum_ct": None, "cum_pr": None}

    mask = (df_src["date"] >= date_start) & (df_src["date"] <= date_end)
    if not mask.any():
        return {"date": None, "cum_ct": None, "cum_pr": None}

    tmp = df_src.loc[mask].copy()
    tmp["daily_ct"] = tmp["tave_this"].clip(lower=0)

    return {
        "date": date_end.isoformat(),
        "cum_ct": round_or_none(tmp["daily_ct"].sum(), 1),
        "cum_pr": round_or_none(tmp["prcp_this"].sum(), 1),
    }


# =========================================================
# JSON 返却整形
# =========================================================
def dataframe_to_records_with_iso_date(df: pd.DataFrame):
    out = df.copy()
    if "date" in out.columns:
        out["date"] = out["date"].map(to_iso_or_none)
    return replace_nan_with_none(out.to_dict(orient="records"))


# =========================================================
# API
# =========================================================
@app.route("/get_temp", methods=["POST"])
def get_climate_data():
    try:
        d = request.get_json()
        params = parse_request_payload(d)

        lat = params["lat"]
        lon = params["lon"]

        today, fiscal_year = get_fiscal_year_today()
        yesterday = today - timedelta(days=1)
        forecast_end = today + timedelta(days=26)

        # 0. 日長マスタ
        df_dl_master = load_daylength_table()

        # 1. 平年値
        df_avg = build_average_temperature(lat, lon, fiscal_year, n_years=3)

        # 2. 今年度データ
        df_this = build_this_year_dataframe(lat, lon, fiscal_year, today, df_avg)

        # 3. 予報部
        df_forecast = df_this.loc[df_this["tag"] == "forecast"].reset_index(drop=True)

        # 4. ct1（絹糸発生日予測用）
        df_ct1, closest1 = build_accumulation_dataframe(
            df_src=df_this,
            start_date=params["ct1_start"],
            end_date=params["ct1_end"],
            method=params["method1"],
            t_base=params["base_threshold1"],
            t_ceiling=params["ceiling_threshold1"],
            target_gdd=params["gdd1_target"],
            df_dl_master=df_dl_master
        )

        # 5. ct2（収穫日予測用）
        df_ct2, closest2 = build_accumulation_dataframe(
            df_src=df_this,
            start_date=params["ct2_start"],
            end_date=params["ct2_end"],
            method=params["method2"],
            t_base=params["base_threshold2"],
            t_ceiling=params["ceiling_threshold2"],
            target_gdd=params["gdd2_target"],
            df_dl_master=df_dl_master
        )

        # 6. 履歴値（開発概要どおり単純積算）
        hist_dict1 = make_hist_dict_simple_ct(params["ct1_start"], yesterday, df_this)

        if closest1["date"] is not None:
            hist_start2 = to_date(closest1["date"])
        else:
            hist_start2 = params["ct2_start"]

        hist_dict2 = make_hist_dict_simple_ct(hist_start2, yesterday, df_this)

        # 7. JSON 整形
        df_this_json = df_this.copy()
        df_forecast_json = df_forecast.copy()

        df_this_json["date"] = df_this_json["date"].map(to_iso_or_none)
        df_forecast_json["date"] = df_forecast_json["date"].map(to_iso_or_none)
        df_avg_json = replace_nan_with_none(df_avg.to_dict(orient="records"))

        return jsonify({
            "average": df_avg_json,
            "this_year": replace_nan_with_none(df_this_json.to_dict(orient="records")),
            "forecast": replace_nan_with_none(df_forecast_json.to_dict(orient="records")),

            "ct1_period": dataframe_to_records_with_iso_date(
                df_this.loc[
                    (df_this["date"] >= params["ct1_start"]) &
                    (df_this["date"] <= params["ct1_end"])
                ].reset_index(drop=True)
            ),
            "ct1": dataframe_to_records_with_iso_date(df_ct1),
            "gdd1_target": replace_nan_with_none(closest1),

            "ct2_period": dataframe_to_records_with_iso_date(
                df_this.loc[
                    (df_this["date"] >= params["ct2_start"]) &
                    (df_this["date"] <= params["ct2_end"])
                ].reset_index(drop=True)
            ),
            "ct2": dataframe_to_records_with_iso_date(df_ct2),
            "gdd2_target": replace_nan_with_none(closest2),

            "ct1_until_yesterday": replace_nan_with_none(hist_dict1),
            "ct2_until_yesterday": replace_nan_with_none(hist_dict2),

            "meta": {
                "today_utc": today.isoformat(),
                "yesterday_utc": yesterday.isoformat(),
                "forecast_end_utc": forecast_end.isoformat(),
                "fiscal_year": fiscal_year,
                "daylength_csv": str(DL_CSV_PATH.name)
            }
        })

    except Exception as e:
        return jsonify({
            "status": "error",
            "message": str(e)
        }), 400


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8080)
