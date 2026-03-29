# -*- coding: utf-8 -*-
from flask import Flask, request, jsonify
from datetime import datetime, timedelta, date
from pathlib import Path
import pandas as pd
import numpy as np
import math

import AMD_Tools4 as amd  # メッシュ農業気象データ取得モジュール

app = Flask(__name__)

# =========================================================
# 定数
# =========================================================
DATE_FMT = "%Y-%m-%d"

# 日長データCSVのパス（同一リポジトリ内）
DL_CSV_PATH = Path(__file__).resolve().parent / "sweetcorn_data-DL.csv"


# =========================================================
# 基本ユーティリティ
# =========================================================

def to_date(s: str) -> date:
    """ISO文字列 → date型に変換"""
    return datetime.fromisoformat(s).date()


def parse_float(value, allow_none=False):
    """
    float型変換
    ・空文字やNoneの扱いも制御
    """
    if value is None or value == "":
        if allow_none:
            return None
        raise ValueError("Required numeric field is missing.")
    return float(value)


def parse_int(value):
    """int変換（GAS由来でfloatになるためfloat→int）"""
    if value is None or value == "":
        raise ValueError("Required integer field is missing.")
    return int(float(value))


def round_or_none(x, ndigits=1):
    """NaNやNoneを考慮したround"""
    if x is None:
        return None
    if isinstance(x, float) and math.isnan(x):
        return None
    return round(float(x), ndigits)


def replace_nan_with_none(data):
    """
    JSONに載せるために NaN → None に変換
    （再帰的に処理）
    """
    if isinstance(data, list):
        return [replace_nan_with_none(x) for x in data]
    if isinstance(data, dict):
        return {k: replace_nan_with_none(v) for k, v in data.items()}
    if isinstance(data, float) and math.isnan(data):
        return None
    return data


def to_iso_or_none(d):
    """date → ISO文字列"""
    if d is None:
        return None
    if isinstance(d, (datetime, date)):
        return d.isoformat()
    return d


# =========================================================
# 入力パラメータ処理
# =========================================================

def parse_request_payload(d: dict) -> dict:
    """
    GASから送られてきたJSONを厳密にパース
    （型変換・必須チェック）
    """
    params = {
        "lat": float(d["lat"]),
        "lon": float(d["lon"]),

        # --- ct1（絹糸） ---
        "ct1_start": to_date(d["ct1_start"]),
        "ct1_end": to_date(d["ct1_end"]),
        "method1": parse_int(d["method1"]),
        "base_threshold1": parse_float(d["base_threshold1"]),
        "ceiling_threshold1": parse_float(d.get("ceiling_threshold1"), allow_none=True),
        "gdd1_target": parse_float(d["gdd1"]),

        # --- ct2（収穫） ---
        "ct2_start": to_date(d["ct2_start"]),
        "ct2_end": to_date(d["ct2_end"]),
        "method2": parse_int(d["method2"]),
        "base_threshold2": parse_float(d["base_threshold2"]),
        "ceiling_threshold2": parse_float(d.get("ceiling_threshold2"), allow_none=True),
        "gdd2_target": parse_float(d["gdd2"]),
    }
    return params


# =========================================================
# 年度判定（4月始まり）
# =========================================================
def get_fiscal_year_today():
    """
    4月〜翌3月を1年度とする
    """
    today = datetime.utcnow().date()
    fiscal_year = today.year if today.month >= 4 else today.year - 1
    return today, fiscal_year


# =========================================================
# 気象データ取得
# =========================================================

def fetch_point_series(var_name, start_date, end_date, lat, lon):
    """
    指定地点の気象データを取得
    ・1点のみ取得（配列から[0,0]抽出）
    """
    arr, tim, *_ = amd.GetMetData(var_name, [start_date, end_date], [lat, lat, lon, lon])
    values = arr[:, 0, 0]
    dates = pd.to_datetime(tim).map(lambda x: x.date())
    return dates, values


# =========================================================
# 平年値（3年平均）
# =========================================================

def build_average_temperature(lat, lon, fiscal_year, n_years=3):
    """
    過去3年の4/1〜翌3/31の平均気温から
    月日別の平年値を作成
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

        # 月日単位に変換
        df["month_day"] = df["datetime"].dt.strftime("%m-%d")
        all_years.append(df[["month_day", "tave"]])

    # 全年度結合 → 平均
    df_concat = pd.concat(all_years)
    df_avg = df_concat.groupby("month_day")["tave"].mean().reset_index()

    df_avg.rename(columns={"tave": "tave_avg"}, inplace=True)
    df_avg["tave_avg"] = df_avg["tave_avg"].round(1)

    # 4/1始まりに並べ替え
    df_avg["sort_key"] = pd.to_datetime("2000-" + df_avg["month_day"])
    df_avg = df_avg.sort_values("sort_key")

    start_idx = df_avg.index[df_avg["month_day"] == "04-01"][0]
    df_avg = pd.concat([df_avg.iloc[start_idx:], df_avg.iloc[:start_idx]])

    return df_avg.reset_index(drop=True)


# =========================================================
# 今年度データ作成
# =========================================================

def build_this_year_dataframe(lat, lon, fiscal_year, today, df_avg):
    """
    ・実測気温
    ・予報タグ
    ・平年補完
    をまとめたデータフレームを作成
    """
    start = f"{fiscal_year}-04-01"
    end = f"{fiscal_year + 1}-03-31"

    dates, tmean = fetch_point_series("TMP_mea", start, end, lat, lon)
    _, tmax = fetch_point_series("TMP_max", start, end, lat, lon)
    _, tmin = fetch_point_series("TMP_min", start, end, lat, lon)
    _, prcp = fetch_point_series("APCPRA", start, end, lat, lon)

    df = pd.DataFrame({
        "date": dates,
        "tave_this": tmean,
        "tmax_this": tmax,
        "tmin_this": tmin,
        "prcp_this": prcp
    })

    # --- タグ付け ---
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

    # --- 平年値マージ ---
    df["month_day"] = df["date"].map(lambda d: d.strftime("%m-%d"))
    df = df.merge(df_avg, on="month_day", how="left")

    # normal期間は平年値で補完
    df.loc[df["tag"] == "normal", "tave_this"] = df["tave_avg"]

    return df.drop(columns=["month_day", "tave_avg"])


# =========================================================
# 日長CSV読み込み
# =========================================================

def load_daylength_table(csv_path=DL_CSV_PATH):
    """
    日長CSVを読み込み
    例: 4/1 → DL
    """
    df = pd.read_csv(csv_path)

    df["month_day"] = df["date"].astype(str)
    df["DL"] = pd.to_numeric(df["DL"])

    return df[["month_day", "DL"]]


def add_daylength_from_csv(df, df_dl_master):
    """
    月日をキーに日長を付与
    """
    df["month_day"] = df["date"].map(lambda d: f"{d.month}/{d.day}")
    df = df.merge(df_dl_master, on="month_day", how="left")

    df["DL_hours"] = df["DL"]
    return df.drop(columns=["month_day", "DL"])


# =========================================================
# GDD計算
# =========================================================

def calc_daily_gdd(row, method, t_base, t_ceiling=None):
    """
    1日のGDD計算
    method1〜8対応
    """
    # コア部分
    val = max(0, row["tave_this"] - t_base)

    # 日長補正
    if method >= 5:
        val *= row["DL_hours"]

    return val


# =========================================================
# 積算計算
# =========================================================

def build_accumulation_dataframe(df_src, start_date, end_date, method, t_base, t_ceiling, target_gdd, df_dl_master):
    """
    ・期間内GDD
    ・累積GDD
    ・ターゲット到達日
    """
    df = df_src[(df_src["date"] >= start_date) & (df_src["date"] <= end_date)].copy()

    if method >= 5:
        df = add_daylength_from_csv(df, df_dl_master)

    df["daily_ct"] = df.apply(lambda r: calc_daily_gdd(r, method, t_base, t_ceiling), axis=1)
    df["cum_ct"] = df["daily_ct"].cumsum()

    # 最も近い日
    df["diff"] = (df["cum_ct"] - target_gdd).abs()
    idx = df["diff"].idxmin()

    return df, {
        "date": df.loc[idx, "date"].isoformat(),
        "cum_ct": df.loc[idx, "cum_ct"]
    }


# =========================================================
# API本体
# =========================================================

@app.route("/get_temp", methods=["POST"])
def get_climate_data():
    """
    メインAPI
    GASから呼ばれる
    """
    try:
        # --- 入力 ---
        params = parse_request_payload(request.get_json())

        lat, lon = params["lat"], params["lon"]

        # --- 年度 ---
        today, fiscal_year = get_fiscal_year_today()

        # --- 日長 ---
        df_dl = load_daylength_table()

        # --- 平年 ---
        df_avg = build_average_temperature(lat, lon, fiscal_year)

        # --- 今年 ---
        df_this = build_this_year_dataframe(lat, lon, fiscal_year, today, df_avg)

        # --- ct1 ---
        df_ct1, gdd1 = build_accumulation_dataframe(
            df_this,
            params["ct1_start"],
            params["ct1_end"],
            params["method1"],
            params["base_threshold1"],
            params["ceiling_threshold1"],
            params["gdd1_target"],
            df_dl
        )

        # --- ct2 ---
        df_ct2, gdd2 = build_accumulation_dataframe(
            df_this,
            params["ct2_start"],
            params["ct2_end"],
            params["method2"],
            params["base_threshold2"],
            params["ceiling_threshold2"],
            params["gdd2_target"],
            df_dl
        )

        # --- 出力 ---
        return jsonify({
            "average": replace_nan_with_none(df_avg.to_dict("records")),
            "this_year": replace_nan_with_none(df_this.to_dict("records")),
            "ct1": replace_nan_with_none(df_ct1.to_dict("records")),
            "ct2": replace_nan_with_none(df_ct2.to_dict("records")),
            "gdd1_target": gdd1,
            "gdd2_target": gdd2
        })

    except Exception as e:
        return jsonify({"error": str(e)}), 400


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8080)
