# ============================================================
# stock_alert.py V2.3
#
# V2.3 完整版
#
# 功能：
# 1. 15分鐘區間最低價跌破通知
# 2. 單日 -5% 跌幅通知
# 3. 7日高點 -10% 通知
# 4. TWSE / TPEX 股票自動辨識
# 5. LINE 輸入股票代號 / 股票名稱即可單股分析
# 6. 自動判斷產業
# 7. 自動套用產業估值模型
# 8. 自動找同業
# 9. PE / PB / PEG / 殖利率 / ROE
# 10. KD / RSI
# 11. 三大法人
# 12. 融資融券
# 13. PE 歷史不足60筆時，自動向過去日期回補
# 14. 不會拿舊資料假填今天
# 15. PE累積滿60筆後才正式啟用歷史平均PE評分
# ============================================================

import os
import json
import time
import threading
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
TPEX_BASE = "https://www.tpex.org.tw"

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

# 每15分鐘執行一次
INTERVAL_MINUTES = 15


# ============================================================
# 固定監控標的
# ============================================================

STOCKS = {
    "0050 元大台灣50": "0050.TW",
    "2330 台積電": "2330.TW",
    "3711 日月光投控": "3711.TW",
    "QQQ": "QQQ",
    "台灣加權指數": "^TWII",
}


# ============================================================
# 固定估值股票
# ============================================================

VALUATION_STOCKS = {
    "2330": {
        "name": "2330 台積電",
        "symbol": "2330.TW",
        "industry": "晶圓代工",
        "market": "TWSE",
    },
    "3711": {
        "name": "3711 日月光投控",
        "symbol": "3711.TW",
        "industry": "封裝測試",
        "market": "TWSE",
    },
}


# ============================================================
# 產業同業池
# ============================================================

INDUSTRY_POOL = {
    "晶圓代工": [
        "2330",
        "2303",
        "5347",
        "6770",
    ],

    "封裝測試": [
        "3711",
        "6239",
        "2449",
        "6147",
        "6257",
        "3264",
        "8150",
        "2441",
        "2369",
        "2329",
    ],

    "IC設計": [
        "2454",
        "2379",
        "3034",
        "3661",
        "3529",
        "6415",
        "3443",
        "5269",
        "3035",
        "6533",
    ],

    "金融": [
        "2881",
        "2882",
        "2886",
        "2891",
        "5880",
        "2884",
        "2885",
        "2890",
        "2880",
        "2834",
    ],

    "電信": [
        "2412",
        "3045",
        "4904",
    ],

    "成熟傳產": [
        "1101",
        "1102",
        "1216",
        "1301",
        "1303",
        "2002",
        "2105",
        "2207",
        "2603",
        "2615",
    ],
}


# ============================================================
# 產業模型
# ============================================================

INDUSTRY_MODEL = {
    "晶圓代工": {
        "pe": True,
        "peg": True,
        "pb": True,
        "yield": False,
        "dcf": False,
        "roe": False,
    },

    "封裝測試": {
        "pe": True,
        "peg": True,
        "pb": True,
        "yield": False,
        "dcf": False,
        "roe": False,
    },

    "IC設計": {
        "pe": True,
        "peg": True,
        "pb": False,
        "yield": False,
        "dcf": False,
        "roe": False,
    },

    "金融": {
        "pe": False,
        "peg": False,
        "pb": True,
        "yield": True,
        "dcf": False,
        "roe": True,
    },

    "電信": {
        "pe": True,
        "peg": False,
        "pb": False,
        "yield": True,
        "dcf": False,
        "roe": False,
    },

    "成熟傳產": {
        "pe": True,
        "peg": False,
        "pb": True,
        "yield": True,
        "dcf": False,
        "roe": False,
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
                {
                    "type": "text",
                    "text": message,
                }
            ],
        },
        timeout=20,
    )

    if response.status_code != 200:
        raise Exception(
            f"LINE API error: "
            f"{response.status_code} "
            f"{response.text}"
        )


def reply_line(user_id, message):
    """
    LINE webhook 回覆。
    若沒有 reply token，則使用 push message。
    """

    try:
        response = requests.post(
            "https://api.line.me/v2/bot/message/push",
            headers={
                "Authorization": f"Bearer {LINE_TOKEN}",
                "Content-Type": "application/json",
            },
            json={
                "to": user_id,
                "messages": [
                    {
                        "type": "text",
                        "text": message,
                    }
                ],
            },
            timeout=20,
        )

        if response.status_code != 200:
            print(
                f"LINE push error："
                f"{response.status_code} "
                f"{response.text}"
            )

    except Exception as e:
        print(f"LINE回覆失敗：{e}")


# ============================================================
# JSON
# ============================================================

def load_json(filename):
    if not os.path.exists(filename):
        return {}

    try:
        with open(
            filename,
            "r",
            encoding="utf-8",
        ) as f:
            data = json.load(f)

        return data if isinstance(data, dict) else {}

    except Exception as e:
        print(
            f"{filename} 讀取失敗：{e}"
        )

        return {}


def save_json(filename, data):
    tmp = filename + ".tmp"

    with open(
        tmp,
        "w",
        encoding="utf-8",
    ) as f:
        json.dump(
            data,
            f,
            ensure_ascii=False,
            indent=2,
        )

    os.replace(tmp, filename)


# ============================================================
# 數字工具
# ============================================================

def to_float(value):
    if value is None:
        return None

    try:
        text = str(value).strip()

        if text in [
            "",
            "-",
            "--",
            "N/A",
            "nan",
            "None",
            "null",
        ]:
            return None

        return float(
            text
            .replace(",", "")
            .replace("%", "")
        )

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
        data = twse_get(
            "/exchangeReport/BWIBBU_ALL"
        )

        result = {}

        if not isinstance(data, list):
            return result

        for row in data:
            code = str(
                row.get("Code", "")
            ).strip()

            if not code:
                continue

            result[code] = {
                "name": row.get(
                    "Name",
                    "",
                ),

                "pe": find_value(
                    row,
                    [
                        "PEratio",
                        "PER",
                        "本益比",
                    ],
                ),

                "yield": find_value(
                    row,
                    [
                        "DividendYield",
                        "殖利率",
                        "殖利率(%)",
                    ],
                ),

                "pb": find_value(
                    row,
                    [
                        "PBratio",
                        "PBR",
                        "股價淨值比",
                    ],
                ),
            }

        return result

    except Exception as e:
        print(
            f"取得 TWSE PE/PB/殖利率失敗：{e}"
        )

        return {}


# ============================================================
# 指定日期 PE
# ============================================================

def get_twse_pe_by_date(date_string):
    try:
        data = twse_get(
            f"/exchangeReport/BWIBBU_d"
            f"?date={date_string}"
        )

        result = {}

        if not isinstance(data, list):
            return result

        for row in data:
            code = str(
                row.get("Code", "")
            ).strip()

            if not code:
                continue

            result[code] = find_value(
                row,
                [
                    "PEratio",
                    "PER",
                    "本益比",
                ],
            )

        return result

    except Exception as e:
        print(
            f"取得 {date_string} PE 失敗：{e}"
        )

        return {}


# ============================================================
# TPEX 股票清單
# ============================================================

def get_tpex_stock_list():
    """
    取得上櫃股票清單。
    用於：
    1. 股票名稱搜尋
    2. 股票市場辨識
    3. Yahoo symbol 建立
    """

    result = {}

    urls = [
        (
            "https://www.tpex.org.tw"
            "/openapi/v1/"
            "tpex_mainboard_peratio"
        ),
        (
            "https://www.tpex.org.tw"
            "/openapi/v1/"
            "tpex_mainboard_quotes"
        ),
    ]

    for url in urls:
        try:
            response = requests.get(
                url,
                timeout=TWSE_TIMEOUT,
                headers={
                    "User-Agent": "Mozilla/5.0"
                },
            )

            if response.status_code != 200:
                continue

            data = response.json()

            if not isinstance(data, list):
                continue

            for row in data:
                if not isinstance(row, dict):
                    continue

                code = str(
                    row.get(
                        "SecuritiesCompanyCode",
                        row.get(
                            "SecuritiesCompanyCode",
                            row.get(
                                "Code",
                                "",
                            ),
                        ),
                    )
                ).strip()

                name = str(
                    row.get(
                        "CompanyName",
                        row.get(
                            "Name",
                            "",
                        ),
                    )
                ).strip()

                if code and name:
                    result[code] = {
                        "code": code,
                        "name": name,
                        "market": "TPEX",
                        "symbol": f"{code}.TWO",
                    }

            if result:
                break

        except Exception as e:
            print(
                f"TPEX股票清單取得失敗：{e}"
            )

    return result


# ============================================================
# 上市股票清單
# ============================================================

def get_twse_stock_list():
    result = {}

    try:
        data = twse_get(
            "/exchangeReport/STOCK_DAY_ALL"
        )

        if not isinstance(data, list):
            return result

        for row in data:
            if not isinstance(row, dict):
                continue

            code = str(
                row.get(
                    "Code",
                    "",
                )
            ).strip()

            name = str(
                row.get(
                    "Name",
                    "",
                )
            ).strip()

            if not code or not name:
                continue

            result[code] = {
                "code": code,
                "name": name,
                "market": "TWSE",
                "symbol": f"{code}.TW",
            }

    except Exception as e:
        print(
            f"TWSE股票清單取得失敗：{e}"
        )

    return result


# ============================================================
# 股票自動辨識
# ============================================================

def normalize_stock_query(text):
    if text is None:
        return ""

    return (
        str(text)
        .strip()
        .replace(" ", "")
        .replace("　", "")
        .upper()
    )


def find_stock(query):
    """
    支援：

    2330
    台積電
    5347
    世界
    """

    query = normalize_stock_query(query)

    if not query:
        return None

    # --------------------------------------------------------
    # 先檢查固定資料
    # --------------------------------------------------------

    if query in VALUATION_STOCKS:
        item = VALUATION_STOCKS[query].copy()
        return item

    # --------------------------------------------------------
    # 建立TWSE清單
    # --------------------------------------------------------

    twse = get_twse_stock_list()

    # 代號
    if query in twse:
        return twse[query]

    # 名稱
    for code, item in twse.items():
        name = normalize_stock_query(
            item.get("name", "")
        )

        if query == name:
            return item

    # --------------------------------------------------------
    # TPEX
    # --------------------------------------------------------

    tpex = get_tpex_stock_list()

    if query in tpex:
        return tpex[query]

    for code, item in tpex.items():
        name = normalize_stock_query(
            item.get("name", "")
        )

        if query == name:
            return item

    # --------------------------------------------------------
    # Yahoo symbol fallback
    # --------------------------------------------------------

    if query.isdigit():
        for suffix, market in [
            (".TW", "TWSE"),
            (".TWO", "TPEX"),
        ]:
            symbol = query + suffix

            try:
                ticker = yf.Ticker(symbol)

                info = ticker.fast_info

                if info:
                    return {
                        "code": query,
                        "name": query,
                        "market": market,
                        "symbol": symbol,
                    }

            except Exception:
                pass

    return None


# ============================================================
# 自動產業判斷
# ============================================================

def infer_industry(code, name=""):
    """
    優先使用既有產業池。
    找不到時使用名稱關鍵字。
    """

    for industry, codes in INDUSTRY_POOL.items():
        if code in codes:
            return industry

    text = (
        str(name)
        + " "
        + str(code)
    )

    keyword_map = {
        "晶圓代工": [
            "晶圓",
            "代工",
        ],

        "封裝測試": [
            "封裝",
            "測試",
            "封測",
        ],

        "IC設計": [
            "IC設計",
            "半導體",
            "晶片",
        ],

        "金融": [
            "金控",
            "銀行",
            "證券",
            "金融",
        ],

        "電信": [
            "電信",
        ],

        "成熟傳產": [
            "鋼",
            "水泥",
            "塑化",
            "食品",
            "汽車",
            "航運",
            "電子",
        ],
    }

    for industry, keywords in keyword_map.items():
        for keyword in keywords:
            if keyword in text:
                return industry

    # 預設
    return "成熟傳產"


# ============================================================
# 股票資訊補完
# ============================================================

def enrich_stock_info(stock):
    code = stock.get("code")
    name = stock.get("name", "")
    symbol = stock.get("symbol")

    if not code:
        return None

    industry = infer_industry(
        code,
        name,
    )

    stock["industry"] = industry

    if not symbol:
        if stock.get("market") == "TPEX":
            stock["symbol"] = f"{code}.TWO"
        else:
            stock["symbol"] = f"{code}.TW"

    if not name or name == code:
        try:
            pe_data = get_twse_pe_data()

            if code in pe_data:
                stock["name"] = pe_data[
                    code
                ].get(
                    "name",
                    code,
                )
        except Exception:
            pass

    return stock


# ============================================================
# 交易日
# ============================================================

def is_weekday(date_obj):
    return date_obj.weekday() < 5


# ============================================================
# PE歷史
#
# V2.3核心：
#
# 新股票加入：
#
# 今天
# ↓
# 取得今天PE
# ↓
# 若 < 60筆
# ↓
# 往過去交易日查
# ↓
# 只儲存真正存在且有效的PE
# ↓
# 累積60筆停止
#
# 絕對不會：
# 舊PE → 新日期
# """

def get_valid_pe_count(code, history):
    values = history.get(
        code,
        {},
    )

    count = 0

    for date_string, pe in values.items():

        if date_string.startswith("_"):
            continue

        try:
            datetime.strptime(
                date_string,
                "%Y%m%d",
            )
        except Exception:
            continue

        if (
            pe is not None
            and 0 < float(pe) <= 200
        ):
            count += 1

    return count


def update_pe_history(
    target_codes,
    history,
):
    """
    V2.3 PE自動回補。

    每個股票個別補。
    """

    today = datetime.now(
        TW_TZ
    ).date()

    for code in target_codes:

        history.setdefault(
            code,
            {},
        )

        current_count = get_valid_pe_count(
            code,
            history,
        )

        print(
            f"{code} 目前PE歷史："
            f"{current_count}筆"
        )

        # ----------------------------------------------------
        # 今天的PE
        # ----------------------------------------------------

        if (
            is_weekday(today)
            and today.strftime("%Y%m%d")
            not in history[code]
        ):

            today_string = today.strftime(
                "%Y%m%d"
            )

            pe_data = get_twse_pe_by_date(
                today_string
            )

            pe = pe_data.get(code)

            if (
                pe is not None
                and 0 < pe <= 200
            ):
                history[code][
                    today_string
                ] = pe

                current_count += 1

                print(
                    f"{code} 新增今日PE："
                    f"{pe:.2f}"
                )

        # ----------------------------------------------------
        # 已滿60，不需要回補
        # ----------------------------------------------------

        if current_count >= PE_MIN_HISTORY:
            print(
                f"{code} PE已滿"
                f"{PE_MIN_HISTORY}筆"
            )
            continue

        # ----------------------------------------------------
        # 往過去找有效PE
        # ----------------------------------------------------

        print(
            f"{code} PE不足"
            f"{PE_MIN_HISTORY}筆，"
            "開始往過去日期回補..."
        )

        date_cursor = today - timedelta(
            days=1
        )

        checked_days = 0

        while (
            current_count < PE_MIN_HISTORY
            and checked_days < 800
        ):

            if not is_weekday(
                date_cursor
            ):
                date_cursor -= timedelta(
                    days=1
                )
                continue

            date_string = date_cursor.strftime(
                "%Y%m%d"
            )

            # 已經有資料，不重新抓
            if (
                date_string
                in history[code]
            ):
                date_cursor -= timedelta(
                    days=1
                )
                checked_days += 1
                continue

            pe_data = get_twse_pe_by_date(
                date_string
            )

            pe = pe_data.get(code)

            if (
                pe is not None
                and 0 < pe <= 200
            ):
                history[code][
                    date_string
                ] = pe

                current_count += 1

                print(
                    f"{code} 回補 "
                    f"{date_string} "
                    f"PE={pe:.2f} "
                    f"({current_count}/"
                    f"{PE_MIN_HISTORY})"
                )

            date_cursor -= timedelta(
                days=1
            )

            checked_days += 1

            time.sleep(
                API_SLEEP
            )

        print(
            f"{code} PE回補完成："
            f"{current_count}筆"
        )

    return history


# ============================================================
# 一年平均PE
# ============================================================

def calculate_one_year_average_pe(
    code,
    history,
):
    stock_history = history.get(
        code,
        {},
    )

    if not stock_history:
        return None, 0

    cutoff = (
        datetime.now(
            TW_TZ
        ).date()
        - timedelta(
            days=365
        )
    )

    values = []

    for date_string, pe in stock_history.items():

        if date_string.startswith("_"):
            continue

        try:
            date_obj = datetime.strptime(
                date_string,
                "%Y%m%d",
            ).date()

        except Exception:
            continue

        if date_obj < cutoff:
            continue

        if (
            pe is None
            or pe <= 0
            or pe > 200
        ):
            continue

        values.append(
            float(pe)
        )

    if not values:
        return None, 0

    return (
        sum(values) / len(values),
        len(values),
    )


# ============================================================
# TAIEX 市場PE
# ============================================================

def calculate_taiex_market_pe():
    print(
        "計算 TAIEX 官方口徑市場 PE..."
    )

    try:
        data = twse_get(
            "/exchangeReport/BWIBBU_ALL"
        )

        values = []

        if isinstance(data, list):

            for row in data:

                pe = find_value(
                    row,
                    [
                        "PEratio",
                        "PER",
                        "本益比",
                    ],
                )

                if (
                    pe is None
                    or pe <= 0
                    or pe > 200
                ):
                    continue

                values.append(pe)

        print(
            f"有效市場股票："
            f"{len(values)} 家"
        )

        if not values:
            return None

        market_pe = (
            sum(values)
            / len(values)
        )

        print(
            f"TAIEX 官方口徑市場 PE："
            f"{market_pe:.2f}"
        )

        return market_pe

    except Exception as e:
        print(
            f"TAIEX 市場 PE 失敗：{e}"
        )

        return None


# ============================================================
# 市值
# ============================================================

def get_market_cap(
    code,
    market="TWSE",
):
    try:
        suffix = (
            ".TWO"
            if market == "TPEX"
            else ".TW"
        )

        info = yf.Ticker(
            f"{code}{suffix}"
        ).fast_info

        value = getattr(
            info,
            "market_cap",
            None,
        )

        if value is not None:
            return float(value)

    except Exception as e:
        print(
            f"{code} 市值取得失敗：{e}"
        )

    return None


# ============================================================
# 同業
# ============================================================

def get_top_industry_companies(
    industry,
    exclude_code=None,
):
    result = []

    for code in INDUSTRY_POOL.get(
        industry,
        [],
    ):

        if code == exclude_code:
            continue

        # 已知上櫃股票
        market = (
            "TPEX"
            if code in [
                "5347",
                "6770",
                "6239",
                "6147",
                "6257",
                "3264",
                "8150",
                "2441",
                "2369",
                "2329",
                "5269",
                "6533",
            ]
            else "TWSE"
        )

        market_cap = get_market_cap(
            code,
            market,
        )

        if market_cap is not None:
            result.append(
                {
                    "code": code,
                    "market_cap": market_cap,
                    "market": market,
                }
            )

        time.sleep(
            API_SLEEP
        )

    result.sort(
        key=lambda x:
        x["market_cap"],
        reverse=True,
    )

    return result[:10]


# ============================================================
# KD
# ============================================================

def calculate_kd(symbol):
    try:
        data = yf.Ticker(
            symbol
        ).history(
            period="6mo",
            interval="1d",
            auto_adjust=False,
        )

        if data.empty:
            return None, None

        data = data.dropna(
            subset=[
                "High",
                "Low",
                "Close",
            ]
        )

        if len(data) < 14:
            return None, None

        low14 = (
            data["Low"]
            .rolling(14)
            .min()
        )

        high14 = (
            data["High"]
            .rolling(14)
            .max()
        )

        denominator = (
            high14 - low14
        )

        rsv = (
            (
                data["Close"]
                - low14
            )
            / denominator
            * 100
        )

        rsv = rsv.replace(
            [
                float("inf"),
                float("-inf"),
            ],
            None,
        )

        k = 50.0
        d = 50.0

        for value in rsv.dropna():

            value = float(value)

            k = (
                k * 2 / 3
                + value / 3
            )

            d = (
                d * 2 / 3
                + k / 3
            )

        return (
            float(k),
            float(d),
        )

    except Exception as e:
        print(
            f"{symbol} KD失敗：{e}"
        )

        return None, None


# ============================================================
# RSI
# ============================================================

def calculate_rsi(
    symbol,
    period=14,
):
    try:
        data = yf.Ticker(
            symbol
        ).history(
            period="6mo",
            interval="1d",
            auto_adjust=False,
        )

        if data.empty:
            return None

        close = data[
            "Close"
        ].dropna()

        if len(close) < period + 2:
            return None

        delta = close.diff()

        gain = delta.clip(
            lower=0
        )

        loss = -delta.clip(
            upper=0
        )

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

        rs = (
            avg_gain.iloc[-1]
            / avg_loss.iloc[-1]
        )

        return float(
            100
            - 100 / (1 + rs)
        )

    except Exception as e:
        print(
            f"{symbol} RSI失敗：{e}"
        )

        return None


# ============================================================
# 基本面
# ============================================================

def get_company_fundamentals(
    symbol,
):
    result = {
        "earnings_growth": None,
        "roe": None,
    }

    try:
        info = yf.Ticker(
            symbol
        ).info

        growth = info.get(
            "earningsGrowth"
        )

        if growth is not None:

            growth = float(
                growth
            )

            if abs(growth) < 5:
                growth *= 100

            result[
                "earnings_growth"
            ] = growth

        roe = info.get(
            "returnOnEquity"
        )

        if roe is not None:

            roe = float(roe)

            if abs(roe) < 5:
                roe *= 100

            result["roe"] = roe

    except Exception as e:
        print(
            f"{symbol} 基本面資料失敗：{e}"
        )

    return result


def calculate_peg(
    pe,
    growth,
):
    if (
        pe is None
        or growth is None
    ):
        return None

    if (
        pe <= 0
        or growth <= 0
    ):
        return None

    return pe / growth


# ============================================================
# T86
# ============================================================

def get_t86_data(
    date_string,
):
    for attempt in range(
        T86_RETRIES + 1
    ):

        try:

            payload = twse_web_get(
                "/fund/T86",
                params={
                    "date": date_string,
                    "selectType": "ALL",
                    "response": "json",
                },
            )

            if not isinstance(
                payload,
                dict,
            ):
                return {}

            if payload.get(
                "stat"
            ) != "OK":

                print(
                    f"T86 {date_string} "
                    "尚未有資料"
                )

                return {}

            fields = payload.get(
                "fields",
                []
            )

            rows = payload.get(
                "data",
                []
            )

            result = {}

            for raw_row in rows:

                if len(raw_row) != len(
                    fields
                ):
                    continue

                row = dict(
                    zip(
                        fields,
                        raw_row,
                    )
                )

                code = str(
                    row.get(
                        "證券代號",
                        row.get(
                            "代號",
                            "",
                        ),
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
                        [
                            "外陸資買賣超股數",
                        ],
                    ),

                    "trust": find_value(
                        row,
                        [
                            "投信買賣超股數",
                        ],
                    ),

                    "dealer": find_value(
                        row,
                        [
                            "自營商買賣超股數",
                        ],
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


def get_recent_t86_history(
    count=20,
):
    result = []

    current = datetime.now(
        TW_TZ
    ).date()

    for i in range(45):

        day = (
            current
            - timedelta(days=i)
        )

        if day.weekday() >= 5:
            continue

        date_string = day.strftime(
            "%Y%m%d"
        )

        data = get_t86_data(
            date_string
        )

        if data:

            result.append(
                {
                    "date": date_string,
                    "data": data,
                }
            )

            if len(result) >= count:
                break

        time.sleep(
            API_SLEEP
        )

    return result


def calculate_institutional_scores(
    code,
    t86_history,
):
    values = []

    for item in t86_history:

        stock = (
            item
            .get("data", {})
            .get(code)
        )

        if not stock:
            continue

        total = stock.get(
            "total"
        )

        if total is not None:
            values.append(
                total
            )

    if not values:
        return {
            "5d": None,
            "20d": None,
            "latest": None,
        }

    return {
        "5d": (
            sum(values[:5])
            if len(values) >= 5
            else None
        ),

        "20d": (
            sum(values[:20])
            if len(values) >= 20
            else None
        ),

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

        if not isinstance(
            data,
            list,
        ):
            return {}

        result = {}

        for row in data:

            if not isinstance(
                row,
                dict,
            ):
                continue

            code = str(
                row.get(
                    "股票代號",
                    row.get(
                        "Code",
                        "",
                    ),
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
        print(
            f"融資融券取得失敗：{e}"
        )

        return {}


def update_margin_history(
    codes,
    history,
):
    today_string = datetime.now(
        TW_TZ
    ).strftime("%Y%m%d")

    if today_string in history.get(
        "_dates",
        [],
    ):
        return history

    data = get_margin_data()

    if not data:

        print(
            "今日融資融券尚未更新"
        )

        return history

    for code in codes:

        item = data.get(code)

        if item:

            history.setdefault(
                code,
                {}
            )

            history[code][
                today_string
            ] = item

    history.setdefault(
        "_dates",
        [],
    )

    history["_dates"] = sorted(
        set(
            history["_dates"]
            + [today_string]
        ),
        reverse=True,
    )[:100]

    return history


def calculate_margin_change(
    code,
    history,
):
    stock = history.get(
        code,
        {}
    )

    dates = []

    for key in stock:

        try:
            datetime.strptime(
                key,
                "%Y%m%d",
            )

            dates.append(key)

        except Exception:
            pass

    dates.sort(
        reverse=True
    )

    if len(dates) < 2:
        return None

    latest = stock[
        dates[0]
    ].get("margin")

    previous = stock[
        dates[min(
            5,
            len(dates) - 1
        )]
    ].get("margin")

    if (
        latest is None
        or previous is None
    ):
        return None

    return (
        latest
        - previous
    )


def get_latest_margin_item(
    code,
    history,
):
    stock = history.get(
        code,
        {}
    )

    dates = []

    for key in stock:

        try:
            datetime.strptime(
                key,
                "%Y%m%d",
            )

            dates.append(key)

        except Exception:
            pass

    if not dates:
        return None

    return stock[
        max(dates)
    ]


def calculate_short_margin_ratio(
    item,
):
    if not item:
        return None

    margin = item.get(
        "margin"
    )

    short = item.get(
        "short"
    )

    if (
        margin is None
        or short is None
        or margin <= 0
    ):
        return None

    return (
        short
        / margin
        * 100
    )


# ============================================================
# V2.3單股分析
# ============================================================

def analyze_stock(
    stock_info,
    current_pe_data,
    market_pe,
    pe_history,
    margin_history,
    t86_history,
):
    """
    回傳：
    {
        message: LINE文字,
        score: int,
        possible_score: int,
        level: str
    }
    """

    stock_info = enrich_stock_info(
        stock_info
    )

    code = stock_info["code"]
    name = stock_info.get(
        "name",
        code,
    )

    symbol = stock_info[
        "symbol"
    ]

    industry = stock_info[
        "industry"
    ]

    market = stock_info.get(
        "market",
        "TWSE",
    )

    print(
        "\n================================"
    )

    print(
        f"單股分析："
        f"{code} {name}"
    )

    print(
        f"產業：{industry}"
    )

    print(
        f"市場：{market}"
    )

    print(
        f"Yahoo：{symbol}"
    )

    print(
        "================================"
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

    # --------------------------------------------------------
    # PE
    # --------------------------------------------------------

    item = current_pe_data.get(
        code
    )

    # TPEX目前資料可能不在TWSE PE API
    if item is None:
        item = {
            "name": name,
            "pe": None,
            "yield": None,
            "pb": None,
        }

    stock_pe = item.get(
        "pe"
    )

    stock_yield = item.get(
        "yield"
    )

    stock_pb = item.get(
        "pb"
    )

    # --------------------------------------------------------
    # 基本面
    # --------------------------------------------------------

    fundamentals = (
        get_company_fundamentals(
            symbol
        )
    )

    earnings_growth = (
        fundamentals[
            "earnings_growth"
        ]
    )

    roe = fundamentals[
        "roe"
    ]

    peg = calculate_peg(
        stock_pe,
        earnings_growth,
    )

    # --------------------------------------------------------
    # 歷史PE
    # --------------------------------------------------------

    one_year_pe, sample_count = (
        calculate_one_year_average_pe(
            code,
            pe_history,
        )
    )

    historical_active = (
        sample_count
        >= PE_MIN_HISTORY
    )

    if not historical_active:

        print(
            f"歷史PE目前"
            f"{sample_count}筆，"
            f"未達{PE_MIN_HISTORY}筆，"
            "一年平均PE暫不計分"
        )

    else:

        print(
            f"歷史PE已達"
            f"{sample_count}筆，"
            "正式啟用一年平均PE"
        )

    # --------------------------------------------------------
    # 同業
    # --------------------------------------------------------

    industry_pe = None

    peers = (
        get_top_industry_companies(
            industry,
            exclude_code=code,
        )
    )

    peer_values = []

    for peer in peers:

        peer_item = (
            current_pe_data.get(
                peer["code"]
            )
        )

        if not peer_item:
            continue

        peer_pe = peer_item.get(
            "pe"
        )

        if (
            peer_pe is not None
            and 0 < peer_pe <= 200
        ):
            peer_values.append(
                peer_pe
            )

    if peer_values:

        industry_pe = (
            sum(peer_values)
            / len(peer_values)
        )

    # --------------------------------------------------------
    # 技術
    # --------------------------------------------------------

    k, d = calculate_kd(
        symbol
    )

    rsi = calculate_rsi(
        symbol
    )

    # --------------------------------------------------------
    # 法人
    # --------------------------------------------------------

    institutional = (
        calculate_institutional_scores(
            code,
            t86_history,
        )
    )

    inst_5d = institutional[
        "5d"
    ]

    inst_20d = institutional[
        "20d"
    ]

    # --------------------------------------------------------
    # 融資
    # --------------------------------------------------------

    margin_change = (
        calculate_margin_change(
            code,
            margin_history,
        )
    )

    latest_margin = (
        get_latest_margin_item(
            code,
            margin_history,
        )
    )

    short_margin_ratio = (
        calculate_short_margin_ratio(
            latest_margin
        )
    )

    # ========================================================
    # 評分
    # ========================================================

    score = 0
    possible_score = 0

    reasons_good = []

    def add_score(
        available,
        positive,
        reason,
    ):
        nonlocal score
        nonlocal possible_score

        if not available:
            return

        possible_score += 1

        if positive:
            score += 1
            reasons_good.append(
                reason
            )

    # --------------------------------------------------------
    # PE
    # --------------------------------------------------------

    if model["pe"]:

        add_score(
            (
                stock_pe is not None
                and market_pe is not None
            ),

            (
                stock_pe < market_pe
                if (
                    stock_pe is not None
                    and market_pe is not None
                )
                else False
            ),

            "PE低於TAIEX",
        )

        add_score(
            (
                stock_pe is not None
                and industry_pe is not None
            ),

            (
                stock_pe < industry_pe
                if (
                    stock_pe is not None
                    and industry_pe is not None
                )
                else False
            ),

            "PE低於同業",
        )

        # 只有60筆以上才加入
        add_score(
            (
                historical_active
                and stock_pe is not None
                and one_year_pe is not None
            ),

            (
                stock_pe < one_year_pe
                if (
                    stock_pe is not None
                    and one_year_pe is not None
                )
                else False
            ),

            "PE低於60筆歷史平均",
        )

    # --------------------------------------------------------
    # PEG
    # --------------------------------------------------------

    if model["peg"]:

        add_score(
            peg is not None,

            (
                peg < 1
                if peg is not None
                else False
            ),

            "PEG < 1",
        )

    # --------------------------------------------------------
    # PB
    # --------------------------------------------------------

    if model["pb"]:

        add_score(
            (
                stock_pb is not None
                and stock_pb > 0
            ),

            (
                stock_pb < 1
                if stock_pb is not None
                else False
            ),

            "PB < 1",
        )

    # --------------------------------------------------------
    # 殖利率
    # --------------------------------------------------------

    if model["yield"]:

        add_score(
            stock_yield is not None,

            (
                stock_yield >= 4
                if stock_yield is not None
                else False
            ),

            "殖利率 >= 4%",
        )

    # --------------------------------------------------------
    # ROE
    # --------------------------------------------------------

    if model.get("roe"):

        add_score(
            roe is not None,

            (
                roe >= 10
                if roe is not None
                else False
            ),

            "ROE >= 10%",
        )

    # --------------------------------------------------------
    # KD
    # --------------------------------------------------------

    add_score(
        (
            k is not None
            and d is not None
        ),

        (
            k < 30
            and d < 30
            if (
                k is not None
                and d is not None
            )
            else False
        ),

        "KD < 30",
    )

    # --------------------------------------------------------
    # RSI
    # --------------------------------------------------------

    add_score(
        rsi is not None,

        (
            rsi < 35
            if rsi is not None
            else False
        ),

        "RSI < 35",
    )

    # --------------------------------------------------------
    # 法人
    # --------------------------------------------------------

    add_score(
        inst_5d is not None,

        (
            inst_5d > 0
            if inst_5d is not None
            else False
        ),

        "法人5日買超",
    )

    add_score(
        inst_20d is not None,

        (
            inst_20d > 0
            if inst_20d is not None
            else False
        ),

        "法人20日買超",
    )

    # --------------------------------------------------------
    # 融資
    # --------------------------------------------------------

    add_score(
        margin_change is not None,

        (
            margin_change < 0
            if margin_change is not None
            else False
        ),

        "融資5日下降",
    )

    # ========================================================
    # 風險
    # ========================================================

    warnings = []

    if (
        k is not None
        and d is not None
        and k > 70
        and d > 70
    ):
        warnings.append(
            "KD高檔"
        )

    if (
        rsi is not None
        and rsi > 70
    ):
        warnings.append(
            "RSI過熱"
        )

    if (
        inst_20d is not None
        and inst_20d < 0
    ):
        warnings.append(
            "法人20日賣超"
        )

    if (
        short_margin_ratio
        is not None
        and short_margin_ratio < 3
    ):
        warnings.append(
            "券資比偏低"
        )

    # ========================================================
    # 評級
    # ========================================================

    ratio = (
        score / possible_score
        if possible_score > 0
        else 0
    )

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
        level = "⚪ 暫不建議加碼"

    # ========================================================
    # 主控台
    # ========================================================

    print(
        f"PE："
        f"{format_number(stock_pe)}"
    )

    print(
        f"TAIEX PE："
        f"{format_number(market_pe)}"
    )

    print(
        f"同業PE："
        f"{format_number(industry_pe)}"
    )

    print(
        f"一年平均PE："
        f"{format_number(one_year_pe)}"
    )

    print(
        f"PE歷史樣本："
        f"{sample_count}/{PE_MIN_HISTORY}"
    )

    print(
        f"PB："
        f"{format_number(stock_pb)}"
    )

    print(
        f"殖利率："
        f"{format_number(stock_yield)}%"
    )

    print(
        f"EPS成長："
        f"{format_number(earnings_growth)}%"
    )

    print(
        f"PEG："
        f"{format_number(peg)}"
    )

    print(
        f"ROE："
        f"{format_number(roe)}%"
    )

    print(
        f"KD："
        f"K={format_number(k)} / "
        f"D={format_number(d)}"
    )

    print(
        f"RSI："
        f"{format_number(rsi)}"
    )

    print(
        f"法人5日："
        f"{format_number(inst_5d, 0)}"
    )

    print(
        f"法人20日："
        f"{format_number(inst_20d, 0)}"
    )

    print(
        f"融資5日變化："
        f"{format_number(margin_change, 0)}"
    )

    print(
        f"券資比："
        f"{format_number(short_margin_ratio)}%"
    )

    print(
        f"評分："
        f"{score}/{possible_score}"
    )

    # ========================================================
    # LINE訊息
    # ========================================================

    historical_status = (
        f"已啟用（{sample_count}筆）"
        if historical_active
        else
        f"尚未啟用（{sample_count}/"
        f"{PE_MIN_HISTORY}筆）"
    )

    warning_text = (
        "、".join(warnings)
        if warnings
        else "無"
    )

    reasons_text = (
        "、".join(
            reasons_good
        )
        if reasons_good
        else "無"
    )

    message = (
        f"{level}\n\n"

        f"標的："
        f"{code} {name}\n"

        f"市場："
        f"{market}\n"

        f"產業："
        f"{industry}\n\n"

        "【估值】\n"

        f"PE："
        f"{format_number(stock_pe)} 倍\n"

        f"TAIEX PE："
        f"{format_number(market_pe)} 倍\n"

        f"同業平均 PE："
        f"{format_number(industry_pe)} 倍\n"

        f"1年平均 PE："
        f"{format_number(one_year_pe)} 倍\n"

        f"PE歷史："
        f"{sample_count}/{PE_MIN_HISTORY}筆\n"

        f"歷史PE狀態："
        f"{historical_status}\n"

        f"PB："
        f"{format_number(stock_pb)} 倍\n"

        f"殖利率："
        f"{format_number(stock_yield)}%\n"

        f"EPS成長："
        f"{format_number(earnings_growth)}%\n"

        f"PEG："
        f"{format_number(peg)}\n"

        f"ROE："
        f"{format_number(roe)}%\n\n"

        "【技術】\n"

        f"KD："
        f"K {format_number(k)} / "
        f"D {format_number(d)}\n"

        f"RSI："
        f"{format_number(rsi)}\n\n"

        "【籌碼】\n"

        f"法人5日："
        f"{format_number(inst_5d, 0)} 股\n"

        f"法人20日："
        f"{format_number(inst_20d, 0)} 股\n"

        f"融資5日變化："
        f"{format_number(margin_change, 0)} 張\n"

        f"券資比："
        f"{format_number(short_margin_ratio)}%\n\n"

        "━━━━━━━━━━\n"

        f"加碼評分："
        f"{score}/{possible_score} "
        f"({ratio:.0%})\n"

        f"{level}\n"

        "━━━━━━━━━━\n\n"

        "加分項目：\n"

        f"{reasons_text}\n\n"

        "風險提示：\n"

        f"{warning_text}"
    )

    return {
        "message": message,
        "score": score,
        "possible_score": possible_score,
        "level": level,
    }


# ============================================================
# LINE單股查詢
# ============================================================

def handle_stock_query(
    query,
):
    print(
        f"LINE股票查詢：{query}"
    )

    stock = find_stock(
        query
    )

    if not stock:

        return (
            "❌ 找不到這支股票。\n\n"
            "請輸入：\n"
            "2330\n"
            "台積電\n"
            "5347\n"
            "世界"
        )

    stock = enrich_stock_info(
        stock
    )

    # --------------------------------------------------------
    # 目前基本面
    # --------------------------------------------------------

    current_pe_data = (
        get_twse_pe_data()
    )

    market_pe = (
        calculate_taiex_market_pe()
    )

    # --------------------------------------------------------
    # PE歷史
    # --------------------------------------------------------

    pe_history = load_json(
        PE_HISTORY_FILE
    )

    pe_history = update_pe_history(
        [stock["code"]],
        pe_history,
    )

    save_json(
        PE_HISTORY_FILE,
        pe_history,
    )

    # --------------------------------------------------------
    # 融資
    # --------------------------------------------------------

    margin_history = load_json(
        CHIP_HISTORY_FILE
    )

    margin_history = update_margin_history(
        [stock["code"]],
        margin_history,
    )

    save_json(
        CHIP_HISTORY_FILE,
        margin_history,
    )

    # --------------------------------------------------------
    # 法人
    # --------------------------------------------------------

    t86_history = (
        get_recent_t86_history(
            20
        )
    )

    result = analyze_stock(
        stock,
        current_pe_data,
        market_pe,
        pe_history,
        margin_history,
        t86_history,
    )

    return result[
        "message"
    ]


# ============================================================
# LINE Webhook
#
# 若使用 GitHub Actions / cron：
# 可另外透過 Web Server 將 webhook POST到此程式。
#
# 此函式保留給 Flask / FastAPI 使用。
# ============================================================

def process_line_event(
    event,
):
    try:

        if event.get(
            "type"
        ) != "message":

            return

        message = event.get(
            "message",
            {}
        )

        if message.get(
            "type"
        ) != "text":

            return

        text = message.get(
            "text",
            ""
        ).strip()

        source = event.get(
            "source",
            {}
        )

        user_id = source.get(
            "userId"
        )

        if not user_id:
            return

        response = handle_stock_query(
            text
        )

        reply_line(
            user_id,
            response
        )

    except Exception as e:

        print(
            f"LINE事件處理失敗：{e}"
        )


# ============================================================
# 價格
# ============================================================

def get_history(
    symbol,
):
    end = datetime.now(
        TW_TZ
    )

    start = (
        end
        - timedelta(days=14)
    )

    try:

        data = yf.download(
            symbol,
            start=start.strftime(
                "%Y-%m-%d"
            ),
            end=(
                end
                + timedelta(days=1)
            ).strftime(
                "%Y-%m-%d"
            ),
            interval="1d",
            auto_adjust=False,
            progress=False,
            threads=False,
        )

    except Exception as e:

        print(
            f"{symbol} 歷史資料失敗：{e}"
        )

        return None

    if data.empty:
        return None

    try:

        close = data[
            "Close"
        ]

        if hasattr(
            close,
            "columns",
        ):
            close = close.iloc[
                :, 0
            ]

    except Exception:

        close = data.iloc[
            :, 0
        ]

    return close.dropna()


def get_latest_price(
    symbol,
):
    ticker = yf.Ticker(
        symbol
    )

    try:

        intraday = ticker.history(
            period="1d",
            interval="1m",
            prepost=False,
            auto_adjust=False,
        )

        if not intraday.empty:

            prices = (
                intraday[
                    "Close"
                ]
                .dropna()
            )

            if len(prices) > 0:
                return float(
                    prices.iloc[-1]
                )

    except Exception as e:

        print(
            f"{symbol} 1m資料失敗：{e}"
        )

    try:

        daily = ticker.history(
            period="5d",
            interval="1d",
            auto_adjust=False,
        )

        if not daily.empty:

            prices = (
                daily[
                    "Close"
                ]
                .dropna()
            )

            if len(prices) > 0:
                return float(
                    prices.iloc[-1]
                )

    except Exception as e:

        print(
            f"{symbol} 日線資料失敗：{e}"
        )

    return None


def get_previous_close(
    history,
):
    if (
        history is None
        or len(history) < 2
    ):
        return None

    return float(
        history.iloc[-2]
    )


def get_week_high(
    symbol,
):
    end = datetime.now(
        TW_TZ
    )

    start = (
        end
        - timedelta(days=7)
    )

    try:

        data = yf.Ticker(
            symbol
        ).history(
            start=start.strftime(
                "%Y-%m-%d"
            ),
            end=(
                end
                + timedelta(days=1)
            ).strftime(
                "%Y-%m-%d"
            ),
            interval="1d",
            auto_adjust=False,
        )

        if data.empty:
            return None

        highs = (
            data["High"]
            .dropna()
        )

        if len(highs) == 0:
            return None

        return float(
            highs.max()
        )

    except Exception as e:

        print(
            f"{symbol} 7日高點失敗：{e}"
        )

        return None


# ============================================================
# 15分鐘區間最低價
# ============================================================

def get_interval_low(
    symbol,
):
    """
    取得最近15分鐘區間的最低價。

    用1分鐘資料。

    若API只回傳部分資料，
    仍以實際取得資料計算。
    """

    try:

        data = yf.Ticker(
            symbol
        ).history(
            period="1d",
            interval="1m",
            prepost=False,
            auto_adjust=False,
        )

        if data.empty:
            return None

        now = datetime.now(
            TW_TZ
        )

        cutoff = (
            now
            - timedelta(
                minutes=INTERVAL_MINUTES
            )
        )

        # yfinance index可能帶timezone
        try:
            if data.index.tz is None:
                data.index = data.index.tz_localize(
                    TW_TZ
                )
            else:
                data.index = data.index.tz_convert(
                    TW_TZ
                )
        except Exception:
            pass

        data = data[
            data.index >= cutoff
        ]

        if data.empty:
            return None

        lows = (
            data["Low"]
            .dropna()
        )

        if len(lows) == 0:
            return None

        return float(
            lows.min()
        )

    except Exception as e:

        print(
            f"{symbol} 15分鐘最低價失敗：{e}"
        )

        return None


# ============================================================
# 15分鐘區間跌破通知
#
# 邏輯：
#
# 第一次：
# 建立基準
#
# 下一次：
# 取得這15分鐘內最低價
#
# 如果本次最低價 < 上次基準
# → 通知
#
# 然後更新基準
# ============================================================

def check_interval_break(
    name,
    symbol,
    state,
):
    print(
        f"\n---------- "
        f"15分鐘區間：{name}"
        f" ----------"
    )

    current = get_latest_price(
        symbol
    )

    interval_low = get_interval_low(
        symbol
    )

    if (
        current is None
        or interval_low is None
    ):

        print(
            "無法取得15分鐘區間資料"
        )

        return

    state.setdefault(
        "interval_low",
        {}
    )

    item = state[
        "interval_low"
    ].get(
        name
    )

    now_string = datetime.now(
        TW_TZ
    ).strftime(
        "%Y-%m-%d %H:%M:%S"
    )

    if not item:

        state[
            "interval_low"
        ][name] = {
            "low": interval_low,
            "time": now_string,
        }

        print(
            "第一次執行，建立區間基準"
        )

        print(
            f"基準最低價："
            f"{interval_low}"
        )

        return

    previous_low = item.get(
        "low"
    )

    if previous_low is None:

        state[
            "interval_low"
        ][name] = {
            "low": interval_low,
            "time": now_string,
        }

        return

    print(
        f"上次區間最低："
        f"{previous_low}"
    )

    print(
        f"本次區間最低："
        f"{interval_low}"
    )

    print(
        f"目前價格："
        f"{current}"
    )

    # --------------------------------------------------------
    # 跌破上次區間最低
    # --------------------------------------------------------

    if interval_low < previous_low:

        send_line(
            "🔴 15分鐘區間低點跌破\n\n"

            f"標的：{name}\n"

            f"目前價格："
            f"{current:,.2f}\n"

            f"上次區間最低："
            f"{previous_low:,.2f}\n"

            f"本次區間最低："
            f"{interval_low:,.2f}\n\n"

            "⚠️ 本次15分鐘區間最低價"
            "已跌破上一次偵測基準"
        )

        print(
            "🔴 已發送15分鐘區間低點跌破通知"
        )

    else:

        print(
            "未跌破上次區間最低"
        )

    # 更新基準
    state[
        "interval_low"
    ][name] = {
        "low": interval_low,
        "time": now_string,
    }


# ============================================================
# 跌幅通知
# ============================================================

def check_stock(
    name,
    symbol,
    state,
):
    print(
        f"\n========== {name} =========="
    )

    history = get_history(
        symbol
    )

    if history is None:

        print(
            "無法取得歷史資料"
        )

        return

    current = get_latest_price(
        symbol
    )

    if current is None:

        print(
            "無法取得目前價格"
        )

        return

    previous_close = (
        get_previous_close(
            history
        )
    )

    if previous_close is None:

        print(
            "無法取得前一交易日收盤"
        )

        return

    week_high = get_week_high(
        symbol
    )

    if week_high is None:

        print(
            "無法取得7日最高價"
        )

        return

    daily_change = (
        current
        / previous_close
        - 1
    )

    weekly_change = (
        current
        / week_high
        - 1
    )

    print(
        f"目前價格：{current}"
    )

    print(
        f"前一交易日收盤："
        f"{previous_close}"
    )

    print(
        f"單日跌幅："
        f"{daily_change:.2%}"
    )

    print(
        f"過去7日最高價："
        f"{week_high}"
    )

    print(
        f"距7日高點："
        f"{weekly_change:.2%}"
    )

    today = datetime.now(
        TW_TZ
    ).strftime(
        "%Y-%m-%d"
    )

    state.setdefault(
        "daily",
        {}
    )

    state[
        "daily"
    ].setdefault(
        name,
        {
            "daily_alert": False,
            "weekly_alert": False,
            "date": today,
        },
    )

    stock_state = state[
        "daily"
    ][name]

    if (
        stock_state.get(
            "date"
        )
        != today
    ):

        stock_state[
            "daily_alert"
        ] = False

        stock_state[
            "weekly_alert"
        ] = False

        stock_state[
            "date"
        ] = today

    # --------------------------------------------------------
    # 單日 -5%
    # --------------------------------------------------------

    if (
        daily_change
        <= DAILY_THRESHOLD
    ):

        if not stock_state.get(
            "daily_alert",
            False,
        ):

            send_line(
                "🔴 跌幅通知\n\n"

                f"標的：{name}\n"

                f"目前價格："
                f"{current:,.2f}\n"

                f"前一交易日收盤："
                f"{previous_close:,.2f}\n"

                f"單日跌幅："
                f"{daily_change:.2%}\n\n"

                "⚠️ 已達到單日 -5%，"
                "可加碼"
            )

            stock_state[
                "daily_alert"
            ] = True

            print(
                "已發送：單日 -5%"
            )

    else:

        stock_state[
            "daily_alert"
        ] = False

    # --------------------------------------------------------
    # 一週 -10%
    # --------------------------------------------------------

    if (
        weekly_change
        <= WEEK_THRESHOLD
    ):

        if not stock_state.get(
            "weekly_alert",
            False,
        ):

            send_line(
                "🔴 跌幅通知\n\n"

                f"標的：{name}\n"

                f"目前價格："
                f"{current:,.2f}\n"

                f"過去7日最高價："
                f"{week_high:,.2f}\n"

                f"距7日高點跌幅："
                f"{weekly_change:.2%}\n\n"

                "⚠️ 已達到一週 -10%，"
                "可加碼"
            )

            stock_state[
                "weekly_alert"
            ] = True

            print(
                "已發送：一週 -10%"
            )

    else:

        stock_state[
            "weekly_alert"
        ] = False


# ============================================================
# 每日估值
# ============================================================

def run_valuation():
    print(
        "\n========== "
        "每日自動估值分析 "
        "=========="
    )

    current_pe_data = (
        get_twse_pe_data()
    )

    if not current_pe_data:

        print(
            "⚠️ TWSE基本面資料取得失敗"
        )

        return

    market_pe = (
        calculate_taiex_market_pe()
    )

    pe_history = load_json(
        PE_HISTORY_FILE
    )

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

    margin_history = load_json(
        CHIP_HISTORY_FILE
    )

    margin_history = update_margin_history(
        target_codes,
        margin_history,
    )

    save_json(
        CHIP_HISTORY_FILE,
        margin_history,
    )

    print(
        "\n========== "
        "取得三大法人最近20交易日資料 "
        "=========="
    )

    t86_history = (
        get_recent_t86_history(
            20
        )
    )

    print(
        f"法人有效交易日："
        f"{len(t86_history)}"
    )

    for code, stock_info in (
        VALUATION_STOCKS.items()
    ):

        try:

            result = analyze_stock(
                stock_info,
                current_pe_data,
                market_pe,
                pe_history,
                margin_history,
                t86_history,
            )

            # 自動估值每日執行
            # 只有真正達到加碼門檻才通知
            if (
                result["level"]
                in [
                    "🟢 強烈建議加碼",
                    "🟡 建議分批加碼",
                ]
            ):

                state = load_json(
                    STATE_FILE
                )

                state.setdefault(
                    "valuation_v23",
                    {}
                )

                code_state = state[
                    "valuation_v23"
                ].get(
                    code,
                    False,
                )

                if not code_state:

                    send_line(
                        result[
                            "message"
                        ]
                    )

                    state[
                        "valuation_v23"
                    ][code] = True

                    save_json(
                        STATE_FILE,
                        state,
                    )

        except Exception as e:

            print(
                f"{stock_info['name']} "
                f"V2.3分析錯誤：{e}"
            )


# ============================================================
# MAIN
# ============================================================

def main():

    print(
        "================================"
    )

    print(
        "股票跌幅 + "
        "15分鐘區間最低價 + "
        "V2.3自動估值 + "
        "技術 + 籌碼"
    )

    print(
        "================================"
    )

    state = load_json(
        STATE_FILE
    )

    # ========================================================
    # 基本資料
    # ========================================================

    current_pe_data = (
        get_twse_pe_data()
    )

    if current_pe_data:

        print(
            f"取得 "
            f"{len(current_pe_data)} "
            "筆上市/上櫃PE資料"
        )

    else:

        print(
            "⚠️ TWSE基本面資料取得失敗"
        )

    # ========================================================
    # 市場PE
    # ========================================================

    market_pe = (
        calculate_taiex_market_pe()
    )

    # ========================================================
    # PE歷史
    # ========================================================

    pe_history = load_json(
        PE_HISTORY_FILE
    )

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

    # ========================================================
    # 融資
    # ========================================================

    margin_history = load_json(
        CHIP_HISTORY_FILE
    )

    margin_history = update_margin_history(
        target_codes,
        margin_history,
    )

    save_json(
        CHIP_HISTORY_FILE,
        margin_history,
    )

    # ========================================================
    # 法人
    # ========================================================

    print(
        "\n========== "
        "取得三大法人最近20交易日資料 "
        "=========="
    )

    t86_history = (
        get_recent_t86_history(
            20
        )
    )

    print(
        f"法人有效交易日："
        f"{len(t86_history)}"
    )

    # ========================================================
    # 跌幅 + 15分鐘
    # ========================================================

    for name, symbol in (
        STOCKS.items()
    ):

        try:

            # 原本跌幅通知
            check_stock(
                name,
                symbol,
                state,
            )

            # 新15分鐘區間低點
            check_interval_break(
                name,
                symbol,
                state,
            )

        except Exception as e:

            print(
                f"{name} 發生錯誤："
                f"{e}"
            )

    # ========================================================
    # 自動估值
    # ========================================================

    if current_pe_data:

        for code, stock_info in (
            VALUATION_STOCKS.items()
        ):

            try:

                result = analyze_stock(
                    stock_info,
                    current_pe_data,
                    market_pe,
                    pe_history,
                    margin_history,
                    t86_history,
                )

                # ------------------------------------------------
                # 達標才通知
                # ------------------------------------------------

                if result[
                    "level"
                ] in [
                    "🟢 強烈建議加碼",
                    "🟡 建議分批加碼",
                ]:

                    state.setdefault(
                        "valuation_v23",
                        {}
                    )

                    if not state[
                        "valuation_v23"
                    ].get(
                        code,
                        False,
                    ):

                        send_line(
                            result[
                                "message"
                            ]
                        )

                        state[
                            "valuation_v23"
                        ][code] = True

                        print(
                            "🟢 已發送V2.3 "
                            "加碼通知"
                        )

                else:

                    state.setdefault(
                        "valuation_v23",
                        {}
                    )

                    # 離開條件後解除鎖定
                    state[
                        "valuation_v23"
                    ][code] = False

            except Exception as e:

                print(
                    f"{stock_info['name']} "
                    f"V2.3檢查錯誤："
                    f"{e}"
                )

    # ========================================================
    # 儲存狀態
    # ========================================================

    save_json(
        STATE_FILE,
        state,
    )

    print(
        "\n全部檢查完成"
    )


# ============================================================
# 執行
# ============================================================

if __name__ == "__main__":
    main()
