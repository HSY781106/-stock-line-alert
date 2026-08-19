# stock_alert.py V2.1
# 完整版本：PE歷史日期防錯、產業化估值、技術面、三大法人、融資融券、LINE通知

import os
import json
import time
import requests
import yfinance as yf

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo


# ============================================================
# 基本設定
# ============================================================

LINE_TOKEN = os.environ["LINE_CHANNEL_ACCESS_TOKEN"]

TWSE_BASE = "https://openapi.twse.com.tw/v1"
TWSE_WEB_BASE = "https://www.twse.com.tw/rwd/zh"

STATE_FILE = "alert_state.json"
PE_HISTORY_FILE = "pe_history.json"
FUNDAMENTAL_HISTORY_FILE = "fundamental_history.json"
CHIP_HISTORY_FILE = "chip_history.json"

DAILY_THRESHOLD = -0.05
WEEK_THRESHOLD = -0.10

TW_TZ = ZoneInfo("Asia/Taipei")

STRONG_SCORE = 8
GOOD_SCORE = 6
PE_MIN_HISTORY = 60

TWSE_TIMEOUT = 20
T86_RETRIES = 2
API_SLEEP = 0.20


# ============================================================
# 監控標的
# ============================================================

STOCKS = {
    "0050 元大台灣50": "0050.TW",
    "2330 台積電": "2330.TW",
    "3711 日月光投控": "3711.TW",
    "QQQ": "QQQ",
    "台灣加權指數": "^TWII",
}


# ============================================================
# 估值股票
# ============================================================

VALUATION_STOCKS = {
    "2330": {
        "name": "2330 台積電",
        "symbol": "2330.TW",
        "industry": "晶圓代工",
    },
    "3711": {
        "name": "3711 日月光投控",
        "symbol": "3711.TW",
        "industry": "封裝測試",
    },
}


# ============================================================
# 產業同業池
# ============================================================

INDUSTRY_POOL = {
    "晶圓代工": [
        "2330", "2303", "5347", "6770",
    ],
    "封裝測試": [
        "3711", "6239", "2449", "6147", "6257",
        "3264", "8150", "2441", "2369", "2329",
    ],
    "IC設計": [
        "2454", "2379", "3034", "3661", "3529",
        "6415", "3443", "5269", "3035", "6533",
    ],
    "金融": [
        "2881", "2882", "2886", "2891", "5880",
        "2884", "2885", "2890", "2880", "2834",
    ],
    "電信": [
        "2412", "3045", "4904",
    ],
    "成熟傳產": [
        "1101", "1102", "1216", "1301", "1303",
        "2002", "2105", "2207", "2603", "2615",
    ],
}


# ============================================================
# 產業估值模型
# ============================================================

INDUSTRY_MODEL = {
    "晶圓代工": {
        "pe": True, "peg": True, "pb": True,
        "yield": False, "dcf": False, "roe": False,
    },
    "封裝測試": {
        "pe": True, "peg": True, "pb": True,
        "yield": False, "dcf": False, "roe": False,
    },
    "IC設計": {
        "pe": True, "peg": True, "pb": False,
        "yield": False, "dcf": False, "roe": False,
    },
    "金融": {
        "pe": False, "peg": False, "pb": True,
        "yield": True, "dcf": False, "roe": True,
    },
    "電信": {
        "pe": True, "peg": False, "pb": False,
        "yield": True, "dcf": False, "roe": False,
    },
    "成熟傳產": {
        "pe": True, "peg": False, "pb": True,
        "yield": True, "dcf": False, "roe": False,
    },
}


# ============================================================
# API
# ============================================================

def twse_get(endpoint, timeout=TWSE_TIMEOUT):
    response = requests.get(
        TWSE_BASE + endpoint,
        timeout=timeout,
        headers={"User-Agent": "Mozilla/5.0"},
    )
    response.raise_for_status()
    return response.json()


def twse_web_get(endpoint, params=None, timeout=TWSE_TIMEOUT):
    response = requests.get(
        TWSE_WEB_BASE + endpoint,
        params=params,
        timeout=timeout,
        headers={"User-Agent": "Mozilla/5.0"},
    )
    response.raise_for_status()
    return response.json()


# ============================================================
# LINE
# ============================================================

def send_line(message):
    response = requests.post(
        "https://api.line.me/v2/bot/message/broadcast",
        headers={
            "Authorization": f"Bearer {LINE_TOKEN}",
            "Content-Type": "application/json",
        },
        json={
            "messages": [
                {"type": "text", "text": message}
            ]
        },
        timeout=20,
    )

    if response.status_code != 200:
        raise Exception(
            f"LINE API error: {response.status_code} {response.text}"
        )


# ============================================================
# JSON
# ============================================================

def load_json(filename):
    if not os.path.exists(filename):
        return {}

    try:
        with open(filename, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except Exception as e:
        print(f"{filename} 讀取失敗：{e}")
        return {}


def save_json(filename, data):
    tmp = filename + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp, filename)


# ============================================================
# 數字工具
# ============================================================

def to_float(value):
    if value is None:
        return None

    try:
        text = str(value).strip()

        if text in ["", "-", "--", "N/A", "nan", "None", "null"]:
            return None

        return float(text.replace(",", "").replace("%", ""))
    except Exception:
        return None


def find_value(row, names):
    if not isinstance(row, dict):
        return None

    for name in names:
        if name in row:
            value = to_float(row[name])
            if value is not None:
                return value

    return None


def format_number(value, digits=2):
    if value is None:
        return "N/A"
    return f"{value:,.{digits}f}"


# ============================================================
# TWSE PE / PB / 殖利率
# ============================================================

def get_twse_pe_data():
    try:
        data = twse_get("/exchangeReport/BWIBBU_ALL")
        result = {}

        if not isinstance(data, list):
            return result

        for row in data:
            code = str(row.get("Code", "")).strip()

            if not code:
                continue

            result[code] = {
                "name": row.get("Name", ""),
                "pe": find_value(
                    row, ["PEratio", "PER", "本益比"]
                ),
                "yield": find_value(
                    row, ["DividendYield", "殖利率", "殖利率(%)"]
                ),
                "pb": find_value(
                    row, ["PBratio", "PBR", "股價淨值比"]
                ),
            }

        return result

    except Exception as e:
        print(f"取得 TWSE PE/PB/殖利率失敗：{e}")
        return {}


# ============================================================
# 指定日期 PE
# ============================================================

def get_twse_pe_by_date(date_string):
    try:
        data = twse_get(
            f"/exchangeReport/BWIBBU_d?date={date_string}"
        )

        result = {}

        if not isinstance(data, list):
            return result

        for row in data:
            code = str(row.get("Code", "")).strip()

            if not code:
                continue

            result[code] = find_value(
                row, ["PEratio", "PER", "本益比"]
            )

        return result

    except Exception as e:
        print(f"取得 {date_string} PE 失敗：{e}")
        return {}


# ============================================================
# 交易日確認
# ============================================================

def is_twse_trading_day(date_string):
    # 00:15 執行時，STOCK_DAY_ALL 仍可能是前一交易日。
    # 因此只把「指定日期 PE API 有資料」視為當日 PE 的必要條件，
    # 不會使用前一天 PE 填入今天。
    #
    # 週末直接排除。
    try:
        date_obj = datetime.strptime(
            date_string, "%Y%m%d"
        ).date()

        if date_obj.weekday() >= 5:
            return False

        return True

    except Exception:
        return False


# ============================================================
# TAIEX 市場 PE
# ============================================================

def calculate_taiex_market_pe():
    print("計算 TAIEX 官方口徑市場 PE...")

    try:
        data = twse_get("/exchangeReport/BWIBBU_ALL")
        values = []

        if isinstance(data, list):
            for row in data:
                pe = find_value(
                    row, ["PEratio", "PER", "本益比"]
                )

                if pe is None or pe <= 0 or pe > 200:
                    continue

                values.append(pe)

        print(f"有效市場股票：{len(values)} 家")

        if not values:
            return None

        market_pe = sum(values) / len(values)

        print(
            f"TAIEX 官方口徑市場 PE：{market_pe:.2f}"
        )

        return market_pe

    except Exception as e:
        print(f"TAIEX 市場 PE 失敗：{e}")
        return None


# ============================================================
# 市值 / 同業
# ============================================================

def get_market_cap(code):
    try:
        info = yf.Ticker(f"{code}.TW").fast_info
        value = getattr(info, "market_cap", None)

        if value is not None:
            return float(value)

    except Exception as e:
        print(f"{code} 市值取得失敗：{e}")

    return None


def get_top_industry_companies(industry, exclude_code=None):
    result = []

    for code in INDUSTRY_POOL.get(industry, []):
        if code == exclude_code:
            continue

        market_cap = get_market_cap(code)

        if market_cap is not None:
            result.append({
                "code": code,
                "market_cap": market_cap,
            })

        time.sleep(API_SLEEP)

    result.sort(
        key=lambda x: x["market_cap"],
        reverse=True,
    )

    return result[:10]


# ============================================================
# KD
# ============================================================

def calculate_kd(symbol):
    try:
        data = yf.Ticker(symbol).history(
            period="6mo",
            interval="1d",
            auto_adjust=False,
        )

        if data.empty:
            return None, None

        data = data.dropna(
            subset=["High", "Low", "Close"]
        )

        if len(data) < 14:
            return None, None

        low14 = data["Low"].rolling(14).min()
        high14 = data["High"].rolling(14).max()
        denominator = high14 - low14

        rsv = (
            (data["Close"] - low14)
            / denominator
            * 100
        )

        rsv = rsv.replace(
            [float("inf"), float("-inf")],
            None,
        )

        k = 50.0
        d = 50.0

        for value in rsv.dropna():
            value = float(value)
            k = k * 2 / 3 + value / 3
            d = d * 2 / 3 + k / 3

        return float(k), float(d)

    except Exception as e:
        print(f"{symbol} KD失敗：{e}")
        return None, None


# ============================================================
# RSI
# ============================================================

def calculate_rsi(symbol, period=14):
    try:
        data = yf.Ticker(symbol).history(
            period="6mo",
            interval="1d",
            auto_adjust=False,
        )

        if data.empty:
            return None

        close = data["Close"].dropna()

        if len(close) < period + 2:
            return None

        delta = close.diff()

        gain = delta.clip(lower=0)
        loss = -delta.clip(upper=0)

        avg_gain = gain.ewm(
            alpha=1 / period,
            adjust=False,
        ).mean()

        avg_loss = loss.ewm(
            alpha=1 / period,
            adjust=False,
        ).mean()

        if avg_loss.iloc[-1] == 0:
            return 100.0

        rs = avg_gain.iloc[-1] / avg_loss.iloc[-1]

        return float(100 - 100 / (1 + rs))

    except Exception as e:
        print(f"{symbol} RSI失敗：{e}")
        return None


# ============================================================
# 基本面
# ============================================================

def get_company_fundamentals(symbol):
    result = {
        "earnings_growth": None,
        "roe": None,
    }

    try:
        info = yf.Ticker(symbol).info

        growth = info.get("earningsGrowth")

        if growth is not None:
            growth = float(growth)

            if abs(growth) < 5:
                growth *= 100

            result["earnings_growth"] = growth

        roe = info.get("returnOnEquity")

        if roe is not None:
            roe = float(roe)

            if abs(roe) < 5:
                roe *= 100

            result["roe"] = roe

    except Exception as e:
        print(f"{symbol} 基本面資料失敗：{e}")

    return result


def calculate_peg(pe, growth):
    if pe is None or growth is None:
        return None

    if pe <= 0 or growth <= 0:
        return None

    return pe / growth


# ============================================================
# PE 歷史
#
# V2.1 核心：
# 不會把前一天 PE 寫到今天。
# 今天沒有當日 PE，就保持缺資料。
# ============================================================

def update_pe_history(target_codes, history):
    today = datetime.now(TW_TZ).date()
    today_string = today.strftime("%Y%m%d")

    if all(
        today_string in history.get(code, {})
        for code in target_codes
    ):
        print(f"{today_string} PE 已存在，略過")
        return history

    if today.weekday() >= 5:
        print(f"{today_string} 為週末，不寫入 PE 歷史")
        return history

    print(f"確認 {today_string} 是否已有當日 PE...")

    pe_data = get_twse_pe_by_date(today_string)

    if not pe_data:
        print(
            f"{today_string} 尚未有當日 PE，"
            "不寫入"
        )
        return history

    valid_count = 0

    for code in target_codes:
        pe = pe_data.get(code)

        if pe is not None and 0 < pe <= 200:
            valid_count += 1

    if valid_count == 0:
        print(
            "沒有有效目標股票 PE，不寫入"
        )
        return history

    for code in target_codes:
        pe = pe_data.get(code)

        if pe is None or pe <= 0 or pe > 200:
            continue

        history.setdefault(code, {})
        history[code][today_string] = pe

        print(f"{code} PE：{pe:.2f}")

    return history


# ============================================================
# 一年平均 PE
# ============================================================

def calculate_one_year_average_pe(code, history):
    stock_history = history.get(code, {})

    if not stock_history:
        return None, 0

    cutoff = (
        datetime.now(TW_TZ).date()
        - timedelta(days=365)
    )

    values = []

    for date_string, pe in stock_history.items():
        try:
            date_obj = datetime.strptime(
                date_string, "%Y%m%d"
            ).date()
        except Exception:
            continue

        if date_obj < cutoff:
            continue

        if pe is None or pe <= 0 or pe > 200:
            continue

        values.append(float(pe))

    if not values:
        return None, 0

    return sum(values) / len(values), len(values)


# ============================================================
# T86 三大法人
# ============================================================

def get_t86_data(date_string):
    for attempt in range(T86_RETRIES + 1):
        try:
            payload = twse_web_get(
                "/fund/T86",
                params={
                    "date": date_string,
                    "selectType": "ALL",
                    "response": "json",
                },
            )

            if not isinstance(payload, dict):
                return {}

            if payload.get("stat") != "OK":
                print(
                    f"T86 {date_string} 尚未有資料"
                )
                return {}

            fields = payload.get("fields", [])
            rows = payload.get("data", [])

            result = {}

            for raw_row in rows:
                if len(raw_row) != len(fields):
                    continue

                row = dict(zip(fields, raw_row))

                code = str(
                    row.get(
                        "證券代號",
                        row.get("代號", ""),
                    )
                ).strip()

                if not code:
                    continue

                result[code] = {
                    "total": find_value(
                        row,
                        [
                            "三大法人買賣超股數",
                            "三大法人買賣超",
                        ],
                    ),
                    "foreign": find_value(
                        row,
                        ["外陸資買賣超股數"],
                    ),
                    "trust": find_value(
                        row,
                        ["投信買賣超股數"],
                    ),
                    "dealer": find_value(
                        row,
                        ["自營商買賣超股數"],
                    ),
                }

            return result

        except requests.exceptions.RequestException as e:
            print(
                f"T86 {date_string} "
                f"取得失敗：{e}"
            )

            if attempt < T86_RETRIES:
                time.sleep(1)

        except Exception as e:
            print(
                f"T86 {date_string} "
                f"解析失敗：{e}"
            )
            return {}

    return {}


def get_recent_t86_history(count=20):
    result = []
    current = datetime.now(TW_TZ).date()

    for i in range(45):
        day = current - timedelta(days=i)

        if day.weekday() >= 5:
            continue

        date_string = day.strftime("%Y%m%d")
        data = get_t86_data(date_string)

        if data:
            result.append({
                "date": date_string,
                "data": data,
            })

            if len(result) >= count:
                break

        time.sleep(API_SLEEP)

    return result


def calculate_institutional_scores(code, t86_history):
    values = []

    for item in t86_history:
        stock = item.get("data", {}).get(code)

        if not stock:
            continue

        total = stock.get("total")

        if total is not None:
            values.append(total)

    if not values:
        return {
            "5d": None,
            "20d": None,
            "latest": None,
        }

    return {
        "5d": sum(values[:5]) if len(values) >= 5 else None,
        "20d": sum(values[:20]) if len(values) >= 20 else None,
        "latest": values[0],
    }


# ============================================================
# 融資融券
# ============================================================

def get_margin_data():
    try:
        data = twse_get(
            "/exchangeReport/MI_MARGN"
        )

        if not isinstance(data, list):
            return {}

        result = {}

        for row in data:
            if not isinstance(row, dict):
                continue

            code = str(
                row.get(
                    "股票代號",
                    row.get("Code", ""),
                )
            ).strip()

            if not code:
                continue

            result[code] = {
                "margin": find_value(
                    row,
                    [
                        "融資餘額",
                        "MarginBalance",
                        "融資餘額(張)",
                    ],
                ),
                "short": find_value(
                    row,
                    [
                        "融券餘額",
                        "ShortBalance",
                        "融券餘額(張)",
                    ],
                ),
            }

        return result

    except Exception as e:
        print(f"融資融券取得失敗：{e}")
        return {}


def update_margin_history(codes, history):
    today_string = datetime.now(
        TW_TZ
    ).strftime("%Y%m%d")

    if today_string in history.get("_dates", []):
        return history

    data = get_margin_data()

    if not data:
        print("今日融資融券尚未更新")
        return history

    for code in codes:
        item = data.get(code)

        if item:
            history.setdefault(code, {})
            history[code][today_string] = item

    history.setdefault("_dates", [])
    history["_dates"] = sorted(
        set(history["_dates"] + [today_string]),
        reverse=True,
    )[:100]

    return history


def calculate_margin_change(code, history):
    stock = history.get(code, {})
    dates = []

    for key in stock:
        try:
            datetime.strptime(key, "%Y%m%d")
            dates.append(key)
        except Exception:
            pass

    dates.sort(reverse=True)

    if len(dates) < 2:
        return None

    latest = stock[dates[0]].get("margin")
    previous = stock[
        dates[min(5, len(dates) - 1)]
    ].get("margin")

    if latest is None or previous is None:
        return None

    return latest - previous


def get_latest_margin_item(code, history):
    stock = history.get(code, {})
    dates = []

    for key in stock:
        try:
            datetime.strptime(key, "%Y%m%d")
            dates.append(key)
        except Exception:
            pass

    if not dates:
        return None

    return stock[max(dates)]


def calculate_short_margin_ratio(item):
    if not item:
        return None

    margin = item.get("margin")
    short = item.get("short")

    if margin is None or short is None or margin <= 0:
        return None

    return short / margin * 100


# ============================================================
# V2.1 評分
#
# 缺資料不扣分，也不進 possible_score。
# 所以新股票在歷史資料不足期間，不會因 PE 歷史缺資料
# 被錯誤壓低評分。
# ============================================================

def check_valuation_v21(
    code,
    stock_info,
    current_pe_data,
    market_pe,
    pe_history,
    margin_history,
    t86_history,
    state,
):
    name = stock_info["name"]
    symbol = stock_info["symbol"]
    industry = stock_info["industry"]

    print(
        f"\n========== V2.1估值檢查：{name} =========="
    )

    model = INDUSTRY_MODEL.get(
        industry,
        {
            "pe": True,
            "peg": False,
            "pb": True,
            "yield": False,
            "dcf": False,
            "roe": False,
        },
    )

    item = current_pe_data.get(code)

    if not item:
        print("無 TWSE 基本面資料")
        return

    stock_pe = item.get("pe")
    stock_yield = item.get("yield")
    stock_pb = item.get("pb")

    fundamentals = get_company_fundamentals(symbol)
    earnings_growth = fundamentals["earnings_growth"]
    roe = fundamentals["roe"]

    peg = calculate_peg(
        stock_pe,
        earnings_growth,
    )

    one_year_pe, sample_count = (
        calculate_one_year_average_pe(
            code,
            pe_history,
        )
    )

    # 同業 PE
    industry_pe = None

    peers = get_top_industry_companies(
        industry,
        exclude_code=code,
    )

    peer_values = []

    for peer in peers:
        peer_item = current_pe_data.get(
            peer["code"]
        )

        if not peer_item:
            continue

        peer_pe = peer_item.get("pe")

        if peer_pe is not None and 0 < peer_pe <= 200:
            peer_values.append(peer_pe)

    if peer_values:
        industry_pe = sum(peer_values) / len(peer_values)

    # 技術
    k, d = calculate_kd(symbol)
    rsi = calculate_rsi(symbol)

    # 法人
    institutional = calculate_institutional_scores(
        code,
        t86_history,
    )

    inst_5d = institutional["5d"]
    inst_20d = institutional["20d"]

    # 融資
    margin_change = calculate_margin_change(
        code,
        margin_history,
    )

    latest_margin = get_latest_margin_item(
        code,
        margin_history,
    )

    short_margin_ratio = calculate_short_margin_ratio(
        latest_margin
    )

    score = 0
    possible_score = 0
    reasons_good = []

    def add_score(available, positive, reason):
        nonlocal score, possible_score

        if not available:
            return

        possible_score += 1

        if positive:
            score += 1
            reasons_good.append(reason)

    # PE
    if model["pe"]:

        add_score(
            stock_pe is not None and market_pe is not None,
            stock_pe < market_pe
            if stock_pe is not None and market_pe is not None
            else False,
            "PE低於TAIEX",
        )

        add_score(
            stock_pe is not None and industry_pe is not None,
            stock_pe < industry_pe
            if stock_pe is not None and industry_pe is not None
            else False,
            "PE低於同業",
        )

        historical_active = (
            one_year_pe is not None
            and sample_count >= PE_MIN_HISTORY
        )

        add_score(
            historical_active,
            stock_pe < one_year_pe
            if stock_pe is not None and one_year_pe is not None
            else False,
            "PE低於一年平均",
        )

        if not historical_active:
            print(
                f"歷史 PE 僅 {sample_count} 筆，"
                f"未達 {PE_MIN_HISTORY} 筆，"
                "一年平均 PE 暫不計分"
            )

    # PEG
    if model["peg"]:
        add_score(
            peg is not None,
            peg < 1 if peg is not None else False,
            "PEG < 1",
        )

    # PB
    if model["pb"]:
        add_score(
            stock_pb is not None and stock_pb > 0,
            stock_pb < 1 if stock_pb is not None else False,
            "PB < 1",
        )

    # 殖利率
    if model["yield"]:
        add_score(
            stock_yield is not None,
            stock_yield >= 4
            if stock_yield is not None
            else False,
            "殖利率 >= 4%",
        )

    # ROE
    if model.get("roe"):
        add_score(
            roe is not None,
            roe >= 10 if roe is not None else False,
            "ROE >= 10%",
        )

    # KD
    add_score(
        k is not None and d is not None,
        k < 30 and d < 30
        if k is not None and d is not None
        else False,
        "KD < 30",
    )

    # RSI
    add_score(
        rsi is not None,
        rsi < 35 if rsi is not None else False,
        "RSI < 35",
    )

    # 法人
    add_score(
        inst_5d is not None,
        inst_5d > 0 if inst_5d is not None else False,
        "法人5日買超",
    )

    add_score(
        inst_20d is not None,
        inst_20d > 0 if inst_20d is not None else False,
        "法人20日買超",
    )

    # 融資
    add_score(
        margin_change is not None,
        margin_change < 0 if margin_change is not None else False,
        "融資5日下降",
    )

    warnings = []

    if k is not None and d is not None and k > 70 and d > 70:
        warnings.append("KD高檔")

    if rsi is not None and rsi > 70:
        warnings.append("RSI過熱")

    if inst_20d is not None and inst_20d < 0:
        warnings.append("法人20日賣超")

    if (
        short_margin_ratio is not None
        and short_margin_ratio < 3
    ):
        warnings.append("券資比偏低")

    # 顯示
    print(f"產業：{industry}")
    print(f"估值模型：{model}")
    print(f"PE：{format_number(stock_pe)}")
    print(f"TAIEX PE：{format_number(market_pe)}")
    print(f"同業 PE：{format_number(industry_pe)}")
    print(
        f"一年平均 PE："
        f"{format_number(one_year_pe)} "
        f"({sample_count}筆)"
    )
    print(f"PB：{format_number(stock_pb)}")
    print(f"殖利率：{format_number(stock_yield)}%")
    print(f"EPS成長：{format_number(earnings_growth)}%")
    print(f"PEG：{format_number(peg)}")
    print(f"ROE：{format_number(roe)}%")
    print(
        f"KD：K={format_number(k)} / D={format_number(d)}"
    )
    print(f"RSI：{format_number(rsi)}")
    print(
        f"法人5日：{format_number(inst_5d, 0)} 股"
    )
    print(
        f"法人20日：{format_number(inst_20d, 0)} 股"
    )
    print(
        f"融資5日變化："
        f"{format_number(margin_change, 0)} 張"
    )
    print(
        f"券資比："
        f"{format_number(short_margin_ratio)}%"
    )
    print(
        f"目前評分：{score}/{possible_score}"
    )
    print(
        "加分項目："
        + ("、".join(reasons_good) if reasons_good else "無")
    )

    if warnings:
        print("風險提示：" + "、".join(warnings))

    if possible_score <= 0:
        print("沒有足夠資料，跳過通知")
        return

    ratio = score / possible_score

    strong = (
        score >= STRONG_SCORE
        and ratio >= 0.80
    )

    good = (
        score >= GOOD_SCORE
        and ratio >= 0.65
    )

    if strong:
        level = "🟢 強烈建議加碼"
    elif good:
        level = "🟡 建議分批加碼"
    else:
        level = None

    if level is None:
        state.setdefault("valuation_v21", {})
        state["valuation_v21"][code] = False

        print("評分未達通知門檻")
        return

    state.setdefault("valuation_v21", {})

    if state["valuation_v21"].get(code, False):
        print(
            "V2.1估值條件已通知，"
            "略過重複通知"
        )
        return

    inst5_text = (
        f"{inst_5d:,.0f}股"
        if inst_5d is not None
        else "尚未更新"
    )

    inst20_text = (
        f"{inst_20d:,.0f}股"
        if inst_20d is not None
        else "尚未更新"
    )

    margin_text = (
        f"{margin_change:,.0f}張"
        if margin_change is not None
        else "尚未更新"
    )

    warning_text = (
        "、".join(warnings)
        if warnings
        else "無"
    )

    message = (
        f"{level}\n\n"
        f"標的：{name}\n"
        f"產業：{industry}\n\n"

        "【估值】\n"
        f"PE：{format_number(stock_pe)} 倍\n"
        f"TAIEX PE：{format_number(market_pe)} 倍\n"
        f"同業平均 PE：{format_number(industry_pe)} 倍\n"
        f"1年平均 PE：{format_number(one_year_pe)} 倍\n"
        f"歷史樣本：{sample_count} 筆\n"
        f"PB：{format_number(stock_pb)} 倍\n"
        f"殖利率：{format_number(stock_yield)}%\n"
        f"EPS成長：{format_number(earnings_growth)}%\n"
        f"PEG：{format_number(peg)}\n"
        f"ROE：{format_number(roe)}%\n\n"

        "【技術】\n"
        f"KD：K {format_number(k)} / D {format_number(d)}\n"
        f"RSI：{format_number(rsi)}\n\n"

        "【籌碼】\n"
        f"法人5日：{inst5_text}\n"
        f"法人20日：{inst20_text}\n"
        f"融資5日變化：{margin_text}\n"
        f"券資比：{format_number(short_margin_ratio)}%\n\n"

        "━━━━━━━━━━\n"
        f"加碼評分：{score}/{possible_score} ({ratio:.0%})\n"
        f"{level}\n"
        "━━━━━━━━━━\n\n"

        "加分項目：\n"
        + (
            "、".join(reasons_good)
            if reasons_good
            else "無"
        )
        + "\n\n"
        "風險提示：\n"
        + warning_text
    )

    send_line(message)

    state["valuation_v21"][code] = True

    print("🟢 已發送 V2.1 加碼通知")


# ============================================================
# 價格
# ============================================================

def get_history(symbol):
    end = datetime.now(TW_TZ)
    start = end - timedelta(days=14)

    try:
        data = yf.download(
            symbol,
            start=start.strftime("%Y-%m-%d"),
            end=(
                end + timedelta(days=1)
            ).strftime("%Y-%m-%d"),
            interval="1d",
            auto_adjust=False,
            progress=False,
            threads=False,
        )
    except Exception as e:
        print(f"{symbol} 歷史資料失敗：{e}")
        return None

    if data.empty:
        return None

    try:
        close = data["Close"]

        if hasattr(close, "columns"):
            close = close.iloc[:, 0]
    except Exception:
        close = data.iloc[:, 0]

    return close.dropna()


def get_latest_price(symbol):
    ticker = yf.Ticker(symbol)

    try:
        intraday = ticker.history(
            period="1d",
            interval="1m",
            prepost=False,
            auto_adjust=False,
        )

        if not intraday.empty:
            prices = intraday["Close"].dropna()

            if len(prices) > 0:
                return float(prices.iloc[-1])

    except Exception as e:
        print(f"{symbol} 1m資料失敗：{e}")

    try:
        daily = ticker.history(
            period="5d",
            interval="1d",
            auto_adjust=False,
        )

        if not daily.empty:
            prices = daily["Close"].dropna()

            if len(prices) > 0:
                return float(prices.iloc[-1])

    except Exception as e:
        print(f"{symbol} 日線資料失敗：{e}")

    return None


def get_previous_close(history):
    if history is None or len(history) < 2:
        return None

    return float(history.iloc[-2])


def get_week_high(symbol):
    end = datetime.now(TW_TZ)
    start = end - timedelta(days=7)

    try:
        data = yf.Ticker(symbol).history(
            start=start.strftime("%Y-%m-%d"),
            end=(
                end + timedelta(days=1)
            ).strftime("%Y-%m-%d"),
            interval="1d",
            auto_adjust=False,
        )

        if data.empty:
            return None

        highs = data["High"].dropna()

        if len(highs) == 0:
            return None

        return float(highs.max())

    except Exception as e:
        print(f"{symbol} 7日高點失敗：{e}")
        return None


# ============================================================
# 跌幅通知
# ============================================================

def check_stock(name, symbol, state):
    print(f"\n========== {name} ==========")

    history = get_history(symbol)

    if history is None:
        print("無法取得歷史資料")
        return

    current = get_latest_price(symbol)

    if current is None:
        print("無法取得目前價格")
        return

    previous_close = get_previous_close(history)

    if previous_close is None:
        print("無法取得前一交易日收盤")
        return

    week_high = get_week_high(symbol)

    if week_high is None:
        print("無法取得7日最高價")
        return

    daily_change = current / previous_close - 1
    weekly_change = current / week_high - 1

    print(f"目前價格：{current}")
    print(f"前一交易日收盤：{previous_close}")
    print(f"單日跌幅：{daily_change:.2%}")
    print(f"過去7日最高價：{week_high}")
    print(f"距7日高點：{weekly_change:.2%}")

    today = datetime.now(TW_TZ).strftime("%Y-%m-%d")

    state.setdefault("daily", {})

    state["daily"].setdefault(
        name,
        {
            "daily_alert": False,
            "weekly_alert": False,
            "date": today,
        },
    )

    stock_state = state["daily"][name]

    if stock_state.get("date") != today:
        stock_state["daily_alert"] = False
        stock_state["weekly_alert"] = False
        stock_state["date"] = today

    if daily_change <= DAILY_THRESHOLD:
        if not stock_state["daily_alert"]:
            send_line(
                "🔴 跌幅通知\n\n"
                f"標的：{name}\n"
                f"目前價格：{current:,.2f}\n"
                f"前一交易日收盤：{previous_close:,.2f}\n"
                f"單日跌幅：{daily_change:.2%}\n\n"
                "⚠️ 已達到單日 -5%，可加碼"
            )

            stock_state["daily_alert"] = True
            print("已發送：單日 -5%")
    else:
        stock_state["daily_alert"] = False

    if weekly_change <= WEEK_THRESHOLD:
        if not stock_state["weekly_alert"]:
            send_line(
                "🔴 跌幅通知\n\n"
                f"標的：{name}\n"
                f"目前價格：{current:,.2f}\n"
                f"過去7日最高價：{week_high:,.2f}\n"
                f"距7日高點跌幅：{weekly_change:.2%}\n\n"
                "⚠️ 已達到一週 -10%，可加碼"
            )

            stock_state["weekly_alert"] = True
            print("已發送：一週 -10%")
    else:
        stock_state["weekly_alert"] = False


# ============================================================
# MAIN
# ============================================================

def main():
    print("================================")
    print(
        "股票跌幅 + V2.1估值 + "
        "技術 + 籌碼 LINE 通知"
    )
    print("================================")

    state = load_json(STATE_FILE)
    pe_history = load_json(PE_HISTORY_FILE)
    margin_history = load_json(CHIP_HISTORY_FILE)

    # 當日 PE / PB / 殖利率
    current_pe_data = get_twse_pe_data()

    if current_pe_data:
        print(
            f"取得 {len(current_pe_data)} "
            "筆上市 PE/PB/殖利率"
        )
    else:
        print("⚠️ TWSE 基本面資料取得失敗")

    # 市場 PE
    market_pe = calculate_taiex_market_pe()

    if market_pe is not None:
        print(
            f"TAIEX 官方市場 PE：{market_pe:.2f}"
        )
    else:
        print("⚠️ 無法取得 TAIEX 市場 PE")

    # PE 歷史
    target_codes = list(
        VALUATION_STOCKS.keys()
    )

    pe_history = update_pe_history(
        target_codes,
        pe_history,
    )

    save_json(
        PE_HISTORY_FILE,
        pe_history,
    )

    # 融資融券
    margin_history = update_margin_history(
        target_codes,
        margin_history,
    )

    save_json(
        CHIP_HISTORY_FILE,
        margin_history,
    )

    # 法人資料只抓一次
    print(
        "\n========== 取得三大法人最近20交易日資料 =========="
    )

    t86_history = get_recent_t86_history(20)

    print(
        f"法人有效交易日：{len(t86_history)}"
    )

    # 跌幅
    for name, symbol in STOCKS.items():
        try:
            check_stock(
                name,
                symbol,
                state,
            )
        except Exception as e:
            print(
                f"{name} 發生錯誤：{e}"
            )

    # V2.1
    if current_pe_data:
        for code, stock_info in VALUATION_STOCKS.items():
            try:
                check_valuation_v21(
                    code,
                    stock_info,
                    current_pe_data,
                    market_pe,
                    pe_history,
                    margin_history,
                    t86_history,
                    state,
                )
            except Exception as e:
                print(
                    f"{stock_info['name']} "
                    f"V2.1檢查錯誤：{e}"
                )

    save_json(
        STATE_FILE,
        state,
    )

    print("\n全部檢查完成")


if __name__ == "__main__":
    main()
