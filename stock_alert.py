# stock_alert.py V2.2
# ============================================================
# 股票跌幅 + 區間最低價 + 自動股票分析 + PE歷史 + 技術面 + 籌碼
#
# V2.2 主要功能：
#
# 1. 每15分鐘執行：
#    - 不只看「當下價格」
#    - 改看「上一次偵測～這一次偵測」期間最低價
#    - 如果期間曾跌破門檻，就通知
#
# 2. PE歷史：
#    - 新股票加入後開始累積
#    - 不拿舊資料補
#    - 未滿60筆，不使用一年平均PE評分
#    - 滿60筆才啟用
#
# 3. LINE：
#    - 好友輸入股票代號
#    - 好友輸入股票名稱
#    - 自動找股票
#    - 自動找產業
#    - 自動套用產業模型
#    - 自動找同業
#    - 單獨分析該股票
#
# 4. 支援：
#    - TWSE上市
#    - TPEx上櫃
#
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
# Flask：LINE Webhook
# ============================================================

try:
    from flask import Flask, request
    FLASK_AVAILABLE = True
except ImportError:
    FLASK_AVAILABLE = False


# ============================================================
# 基本設定
# ============================================================

LINE_TOKEN = os.environ["LINE_CHANNEL_ACCESS_TOKEN"]

# 如果有設定 LINE_CHANNEL_SECRET，Webhook 會驗證簽章
LINE_CHANNEL_SECRET = os.environ.get(
    "LINE_CHANNEL_SECRET",
    ""
)

TWSE_BASE = "https://openapi.twse.com.tw/v1"
TWSE_WEB_BASE = "https://www.twse.com.tw/rwd/zh"

TPEX_BASE = "https://www.tpex.org.tw/openapi/v1"

STATE_FILE = "alert_state.json"
PE_HISTORY_FILE = "pe_history.json"
FUNDAMENTAL_HISTORY_FILE = "fundamental_history.json"
CHIP_HISTORY_FILE = "chip_history.json"

DAILY_THRESHOLD = -0.05
WEEK_THRESHOLD = -0.10

TW_TZ = ZoneInfo("Asia/Taipei")

STRONG_SCORE = 8
GOOD_SCORE = 6

# PE歷史至少60個有效交易日
PE_MIN_HISTORY = 60

TWSE_TIMEOUT = 20
TPEX_TIMEOUT = 20

T86_RETRIES = 2
API_SLEEP = 0.20

# ============================================================
# 15分鐘區間偵測
# ============================================================

# 如果程式每15分鐘執行一次：
# state會保存上一次執行時間與價格。
#
# 但「期間最低價」需要盤中資料。
#
# 台股可以利用Yahoo 1m資料抓取最近區間，
# 如果1m資料無法取得，會退回目前價格。
#
# 區間最低價判斷：
#
# current_interval_low <= previous_price * (1 + threshold)
#
# 例如：
#
# 上次偵測 100
# 這15分鐘最低 94
#
# 94 <= 95
#
# => 曾經跌破 -5%
#
# 即使現在回到98，也要通知。


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
# 舊版固定估值股票
#
# 保留只是為了相容舊設定。
#
# 新股票不需要再寫進這裡。
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
#
# 保留既有產業池。
#
# 自動找同業時：
# 1. 優先使用產業池
# 2. 找不到時使用自動產業分類
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
# 自動產業分類
#
# TWSE / TPEx 官方資料會提供產業代碼。
#
# 由於不同市場的產業代碼不完全適合作為估值模型名稱，
# 這裡再轉成我們自己的估值產業。
# ============================================================

INDUSTRY_CODE_MAP = {

    # 電子
    "24": "晶圓代工",
    "25": "晶圓代工",
    "26": "封裝測試",
    "27": "IC設計",
    "28": "IC設計",
    "29": "IC設計",

    # 金融
    "17": "金融",
    "18": "金融",

    # 電信
    "16": "電信",
}


# ============================================================
# API
# ============================================================

def twse_get(endpoint, timeout=TWSE_TIMEOUT):

    response = requests.get(
        TWSE_BASE + endpoint,
        timeout=timeout,
        headers={
            "User-Agent": "Mozilla/5.0"
        },
    )

    response.raise_for_status()

    return response.json()


def twse_web_get(
    endpoint,
    params=None,
    timeout=TWSE_TIMEOUT,
):

    response = requests.get(
        TWSE_WEB_BASE + endpoint,
        params=params,
        timeout=timeout,
        headers={
            "User-Agent": "Mozilla/5.0"
        },
    )

    response.raise_for_status()

    return response.json()


def tpex_get(
    endpoint,
    timeout=TPEX_TIMEOUT,
):

    response = requests.get(
        TPEX_BASE + endpoint,
        timeout=timeout,
        headers={
            "User-Agent": "Mozilla/5.0"
        },
    )

    response.raise_for_status()

    return response.json()


# ============================================================
# LINE Broadcast
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
            ]
        },

        timeout=20,
    )

    if response.status_code != 200:

        raise Exception(
            f"LINE API error: "
            f"{response.status_code} "
            f"{response.text}"
        )


# ============================================================
# LINE Reply
# ============================================================

def reply_line(reply_token, message):

    response = requests.post(
        "https://api.line.me/v2/bot/message/reply",

        headers={
            "Authorization": f"Bearer {LINE_TOKEN}",
            "Content-Type": "application/json",
        },

        json={
            "replyToken": reply_token,
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
            "LINE reply error:",
            response.status_code,
            response.text,
        )


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

        return (
            data
            if isinstance(data, dict)
            else {}
        )

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

    os.replace(
        tmp,
        filename,
    )


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
            "－",
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

            value = to_float(
                row[name]
            )

            if value is not None:
                return value

    return None


def format_number(
    value,
    digits=2,
):

    if value is None:
        return "N/A"

    return (
        f"{value:,.{digits}f}"
    )


# ============================================================
# 字串清理
# ============================================================

def clean_text(value):

    if value is None:
        return ""

    return (
        str(value)
        .replace("\u3000", "")
        .replace(" ", "")
        .strip()
    )


# ============================================================
# TWSE 上市公司清單
# ============================================================

def get_twse_company_list():

    try:

        data = twse_get(
            "/opendata/t187ap03_L"
        )

        if not isinstance(data, list):
            return []

        result = []

        for row in data:

            code = clean_text(
                row.get(
                    "公司代號",
                    ""
                )
            )

            name = clean_text(
                row.get(
                    "公司簡稱",
                    ""
                )
            )

            industry_code = clean_text(
                row.get(
                    "產業別",
                    ""
                )
            )

            if not code:
                continue

            result.append(
                {
                    "code": code,
                    "name": name,
                    "industry_code":
                        industry_code,
                    "market": "TWSE",
                    "symbol":
                        f"{code}.TW",
                }
            )

        return result

    except Exception as e:

        print(
            "TWSE公司清單失敗：",
            e,
        )

        return []


# ============================================================
# TPEx 上櫃公司清單
# ============================================================

def get_tpex_company_list():

    try:

        data = tpex_get(
            "/mopsfin_t187ap03_O"
        )

        if not isinstance(data, list):
            return []

        result = []

        for row in data:

            code = clean_text(
                row.get(
                    "SecuritiesCompanyCode",
                    ""
                )
            )

            name = clean_text(
                row.get(
                    "CompanyAbbreviation",
                    ""
                )
            )

            industry_code = clean_text(
                row.get(
                    "SecuritiesIndustryCode",
                    ""
                )
            )

            if not code:
                continue

            result.append(
                {
                    "code": code,
                    "name": name,
                    "industry_code":
                        industry_code,
                    "market": "TPEx",
                    "symbol":
                        f"{code}.TWO",
                }
            )

        return result

    except Exception as e:

        print(
            "TPEx公司清單失敗：",
            e,
        )

        return []


# ============================================================
# 公司清單快取
# ============================================================

def get_company_universe():

    cache = load_json(
        "company_universe_cache.json"
    )

    today = datetime.now(
        TW_TZ
    ).strftime("%Y-%m-%d")

    if (
        cache.get("date") == today
        and isinstance(
            cache.get("data"),
            list,
        )
        and cache["data"]
    ):

        return cache["data"]

    twse = get_twse_company_list()

    time.sleep(API_SLEEP)

    tpex = get_tpex_company_list()

    universe = twse + tpex

    if universe:

        save_json(
            "company_universe_cache.json",
            {
                "date": today,
                "data": universe,
            },
        )

    return universe


# ============================================================
# 找股票
# ============================================================

def resolve_stock(query):

    query = clean_text(query)

    if not query:
        return None

    # 去除 .TW / .TWO
    query_upper = query.upper()

    if query_upper.endswith(".TW"):
        query = query[:-3]

    elif query_upper.endswith(".TWO"):
        query = query[:-4]

    query = clean_text(query)

    universe = get_company_universe()

    # 先完全代號
    exact_code = [
        x for x in universe
        if x["code"] == query
    ]

    if len(exact_code) == 1:
        return exact_code[0]

    # 完全名稱
    exact_name = [
        x for x in universe
        if clean_text(x["name"]) == query
    ]

    if len(exact_name) == 1:
        return exact_name[0]

    # 名稱包含
    name_matches = [
        x for x in universe
        if query in clean_text(x["name"])
    ]

    if len(name_matches) == 1:
        return name_matches[0]

    # 代號包含
    code_matches = [
        x for x in universe
        if query in x["code"]
    ]

    if len(code_matches) == 1:
        return code_matches[0]

    # 有多筆，回傳None
    return None


# ============================================================
# 自動產業
# ============================================================

def detect_industry(stock):

    if not stock:
        return "成熟傳產"

    code = clean_text(
        stock.get("industry_code")
    )

    name = clean_text(
        stock.get("name")
    )

    # --------------------------------------------------------
    # 直接產業代碼
    # --------------------------------------------------------

    for prefix, industry in INDUSTRY_CODE_MAP.items():

        if code.startswith(prefix):

            # 電子類再依公司名稱／既有池進一步判斷
            if industry == "晶圓代工":

                if (
                    stock["code"]
                    in INDUSTRY_POOL.get(
                        "晶圓代工",
                        [],
                    )
                ):
                    return "晶圓代工"

                if any(
                    keyword in name
                    for keyword in [
                        "台積",
                        "聯電",
                        "世界",
                        "力積",
                        "晶圓",
                    ]
                ):
                    return "晶圓代工"

            if industry == "封裝測試":

                if (
                    stock["code"]
                    in INDUSTRY_POOL.get(
                        "封裝測試",
                        [],
                    )
                ):
                    return "封裝測試"

                if any(
                    keyword in name
                    for keyword in [
                        "日月光",
                        "矽格",
                        "力成",
                        "京元",
                        "封測",
                        "測試",
                    ]
                ):
                    return "封裝測試"

            if industry == "IC設計":

                if (
                    stock["code"]
                    in INDUSTRY_POOL.get(
                        "IC設計",
                        [],
                    )
                ):
                    return "IC設計"

                if any(
                    keyword in name
                    for keyword in [
                        "聯發科",
                        "瑞昱",
                        "聯詠",
                        "聯陽",
                        "IC",
                    ]
                ):
                    return "IC設計"

            return industry

    # --------------------------------------------------------
    # 既有產業池
    # --------------------------------------------------------

    for industry, codes in INDUSTRY_POOL.items():

        if stock["code"] in codes:
            return industry

    # --------------------------------------------------------
    # 名稱關鍵字 fallback
    # --------------------------------------------------------

    if any(
        keyword in name
        for keyword in [
            "銀行",
            "金控",
            "證券",
            "保險",
        ]
    ):
        return "金融"

    if any(
        keyword in name
        for keyword in [
            "電信",
            "通信",
        ]
    ):
        return "電信"

    return "成熟傳產"


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

            code = clean_text(
                row.get(
                    "Code",
                    ""
                )
            )

            if not code:
                continue

            result[code] = {
                "name":
                    row.get(
                        "Name",
                        "",
                    ),

                "pe":
                    find_value(
                        row,
                        [
                            "PEratio",
                            "PER",
                            "本益比",
                        ],
                    ),

                "yield":
                    find_value(
                        row,
                        [
                            "DividendYield",
                            "殖利率",
                            "殖利率(%)",
                        ],
                    ),

                "pb":
                    find_value(
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
            "取得TWSE PE/PB/殖利率失敗：",
            e,
        )

        return {}


# ============================================================
# TPEx PE / PB / 殖利率
# ============================================================

def get_tpex_pe_data():

    try:

        data = tpex_get(
            "/tpex_mainboard_peratio_analysis"
        )

        if not isinstance(data, list):
            return {}

        result = {}

        for row in data:

            code = clean_text(
                row.get(
                    "SecuritiesCompanyCode",
                    row.get(
                        "股票代號",
                        "",
                    ),
                )
            )

            if not code:
                continue

            result[code] = {
                "name":
                    row.get(
                        "CompanyName",
                        row.get(
                            "名稱",
                            "",
                        ),
                    ),

                "pe":
                    find_value(
                        row,
                        [
                            "PER",
                            "本益比",
                        ],
                    ),

                "yield":
                    find_value(
                        row,
                        [
                            "DividendYield",
                            "殖利率",
                            "殖利率(%)",
                        ],
                    ),

                "pb":
                    find_value(
                        row,
                        [
                            "PBR",
                            "股價淨值比",
                        ],
                    ),
            }

        return result

    except Exception as e:

        print(
            "取得TPEx PE/PB/殖利率失敗：",
            e,
        )

        return {}


# ============================================================
# 合併上市 + 上櫃估值資料
# ============================================================

def get_all_pe_data():

    result = {}

    twse = get_twse_pe_data()

    result.update(twse)

    time.sleep(API_SLEEP)

    tpex = get_tpex_pe_data()

    result.update(tpex)

    return result


# ============================================================
# 指定日期 TWSE PE
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

            code = clean_text(
                row.get(
                    "Code",
                    "",
                )
            )

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
            f"取得 {date_string} TWSE PE失敗：",
            e,
        )

        return {}


# ============================================================
# 指定日期 TPEx PE
#
# TPEx endpoint格式若未來調整：
# get_tpex_pe_data()仍可供當日資料使用。
# ============================================================

def get_tpex_pe_by_date(date_string):

    try:

        # TPEx OpenAPI的日期格式通常為YYYYMMDD
        data = tpex_get(
            "/tpex_mainboard_peratio_analysis",
        )

        result = {}

        if not isinstance(data, list):
            return result

        for row in data:

            code = clean_text(
                row.get(
                    "SecuritiesCompanyCode",
                    row.get(
                        "股票代號",
                        "",
                    ),
                )
            )

            if not code:
                continue

            result[code] = find_value(
                row,
                [
                    "PER",
                    "本益比",
                ],
            )

        return result

    except Exception as e:

        print(
            f"取得 {date_string} TPEx PE失敗：",
            e,
        )

        return {}


# ============================================================
# TAIEX 市場 PE
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
            "TAIEX 官方口徑市場 PE："
            f"{market_pe:.2f}"
        )

        return market_pe

    except Exception as e:

        print(
            "TAIEX 市場 PE失敗：",
            e,
        )

        return None


# ============================================================
# 市值
# ============================================================

def get_market_cap(code, market="TWSE"):

    try:

        suffix = (
            ".TW"
            if market == "TWSE"
            else ".TWO"
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
            f"{code} 市值取得失敗：",
            e,
        )

    return None


# ============================================================
# 自動找同業
# ============================================================

def get_top_industry_companies(
    industry,
    exclude_code=None,
):

    result = []

    # 先使用我們已有的高品質同業池
    candidate_codes = INDUSTRY_POOL.get(
        industry,
        [],
    )

    for code in candidate_codes:

        if code == exclude_code:
            continue

        market_cap = get_market_cap(
            code,
            "TWSE",
        )

        if market_cap is None:

            market_cap = get_market_cap(
                code,
                "TPEx",
            )

        if market_cap is not None:

            result.append(
                {
                    "code": code,
                    "market_cap":
                        market_cap,
                }
            )

        time.sleep(API_SLEEP)

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
            f"{symbol} KD失敗：",
            e,
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

        close = (
            data["Close"]
            .dropna()
        )

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
            f"{symbol} RSI失敗：",
            e,
        )

        return None


# ============================================================
# 基本面
#
# Yahoo info失敗時，不會讓整支分析失敗。
# ============================================================

def get_company_fundamentals(symbol):

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
            f"{symbol} 基本面資料失敗：",
            e,
        )

    return result


# ============================================================
# PEG
# ============================================================

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
# PE歷史
#
# 核心規則：
#
# 新股票：
#   第1天 → 1筆
#   第20天 → 20筆
#   第59天 → 59筆
#   第60天 → 開始啟用一年平均PE評分
#
# 不補歷史。
# 不拿別的股票資料。
# 不把昨天PE寫成今天。
# ============================================================

def update_pe_history(
    target_codes,
    history,
):

    today = datetime.now(
        TW_TZ
    ).date()

    today_string = (
        today.strftime("%Y%m%d")
    )

    if (
        today.weekday()
        >= 5
    ):

        print(
            f"{today_string} "
            "為週末，不寫入PE歷史"
        )

        return history

    pe_data = {}

    # 上市
    try:

        pe_data.update(
            get_twse_pe_by_date(
                today_string
            )
        )

    except Exception:
        pass

    # 上櫃
    try:

        tpex_data = (
            get_tpex_pe_by_date(
                today_string
            )
        )

        for code, pe in tpex_data.items():

            if code not in pe_data:
                pe_data[code] = pe

    except Exception:
        pass

    if not pe_data:

        print(
            f"{today_string} "
            "尚未有PE資料，不寫入"
        )

        return history

    valid_count = 0

    for code in target_codes:

        pe = pe_data.get(code)

        if (
            pe is not None
            and 0 < pe <= 200
        ):
            valid_count += 1

    if valid_count == 0:

        print(
            "沒有有效目標股票PE，不寫入"
        )

        return history

    for code in target_codes:

        pe = pe_data.get(code)

        if (
            pe is None
            or pe <= 0
            or pe > 200
        ):
            continue

        history.setdefault(
            code,
            {},
        )

        # 防止重複
        history[code][
            today_string
        ] = pe

        print(
            f"{code} PE：{pe:.2f}"
        )

    return history


# ============================================================
# 一年平均PE
# ============================================================

def calculate_one_year_average_pe(
    code,
    history,
):

    stock_history = (
        history.get(
            code,
            {},
        )
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

    for (
        date_string,
        pe,
    ) in stock_history.items():

        try:

            date_obj = (
                datetime.strptime(
                    date_string,
                    "%Y%m%d",
                ).date()
            )

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
        sum(values)
        / len(values),
        len(values),
    )


# ============================================================
# T86 三大法人
# ============================================================

def get_t86_data(
    date_string
):

    for attempt in range(
        T86_RETRIES + 1
    ):

        try:

            payload = twse_web_get(
                "/fund/T86",
                params={
                    "date":
                        date_string,
                    "selectType":
                        "ALL",
                    "response":
                        "json",
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
                [],
            )

            rows = payload.get(
                "data",
                [],
            )

            result = {}

            for raw_row in rows:

                if len(
                    raw_row
                ) != len(fields):

                    continue

                row = dict(
                    zip(
                        fields,
                        raw_row,
                    )
                )

                code = clean_text(
                    row.get(
                        "證券代號",
                        row.get(
                            "代號",
                            "",
                        ),
                    )
                )

                if not code:
                    continue

                result[code] = {

                    "total":
                        find_value(
                            row,
                            [
                                "三大法人買賣超股數",
                                "三大法人買賣超",
                            ],
                        ),

                    "foreign":
                        find_value(
                            row,
                            [
                                "外陸資買賣超股數",
                            ],
                        ),

                    "trust":
                        find_value(
                            row,
                            [
                                "投信買賣超股數",
                            ],
                        ),

                    "dealer":
                        find_value(
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


# ============================================================
# 最近20交易日法人
# ============================================================

def get_recent_t86_history(
    count=20
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

        date_string = (
            day.strftime(
                "%Y%m%d"
            )
        )

        data = get_t86_data(
            date_string
        )

        if data:

            result.append(
                {
                    "date":
                        date_string,
                    "data":
                        data,
                }
            )

            if len(
                result
            ) >= count:

                break

        time.sleep(
            API_SLEEP
        )

    return result


# ============================================================
# 法人評分
# ============================================================

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

        "5d":
            sum(values[:5])
            if len(values) >= 5
            else None,

        "20d":
            sum(values[:20])
            if len(values) >= 20
            else None,

        "latest":
            values[0],
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

            code = clean_text(
                row.get(
                    "股票代號",
                    row.get(
                        "Code",
                        "",
                    ),
                )
            )

            if not code:
                continue

            result[code] = {

                "margin":
                    find_value(
                        row,
                        [
                            "融資餘額",
                            "MarginBalance",
                            "融資餘額(張)",
                        ],
                    ),

                "short":
                    find_value(
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
            "融資融券取得失敗：",
            e,
        )

        return {}


def update_margin_history(
    codes,
    history,
):

    today_string = (
        datetime.now(
            TW_TZ
        ).strftime("%Y%m%d")
    )

    if today_string in (
        history.get(
            "_dates",
            [],
        )
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
                {},
            )

            history[
                code
            ][
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
        {},
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
        dates[
            min(
                5,
                len(dates) - 1
            )
        ]
    ].get("margin")

    if (
        latest is None
        or previous is None
    ):
        return None

    return (
        latest - previous
    )


def get_latest_margin_item(
    code,
    history,
):

    stock = history.get(
        code,
        {},
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
    item
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
# 15分鐘區間資料
# ============================================================

def get_intraday_low(
    symbol,
    start_time,
    end_time,
):

    try:

        # Yahoo 1m資料通常只能取得近期資料。
        # 因此我們最多抓最近7天。
        now = datetime.now(
            TW_TZ
        )

        if (
            now - start_time
        ).total_seconds() > (
            7 * 24 * 3600
        ):
            return None

        data = yf.Ticker(
            symbol
        ).history(
            start=(
                start_time
                - timedelta(
                    minutes=2
                )
            ),
            end=(
                end_time
                + timedelta(
                    minutes=2
                )
            ),
            interval="1m",
            prepost=False,
            auto_adjust=False,
        )

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
            f"{symbol} "
            "區間最低價取得失敗：",
            e,
        )

        return None


# ============================================================
# 取得目前價格
# ============================================================

def get_latest_price(
    symbol
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
                intraday["Close"]
                .dropna()
            )

            if len(prices) > 0:

                return float(
                    prices.iloc[-1]
                )

    except Exception as e:

        print(
            f"{symbol} "
            f"1m資料失敗：{e}"
        )

    try:

        daily = ticker.history(
            period="5d",
            interval="1d",
            auto_adjust=False,
        )

        if not daily.empty:

            prices = (
                daily["Close"]
                .dropna()
            )

            if len(prices) > 0:

                return float(
                    prices.iloc[-1]
                )

    except Exception as e:

        print(
            f"{symbol} "
            f"日線資料失敗：{e}"
        )

    return None


# ============================================================
# 歷史價格
# ============================================================

def get_history(
    symbol
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
            f"{symbol} "
            f"歷史資料失敗：{e}"
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


def get_previous_close(
    history
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
    symbol
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
            f"{symbol} "
            f"7日高點失敗：{e}"
        )

        return None


# ============================================================
# 跌幅通知
#
# V2.2：
#
# 不再單純：
#
#     current <= threshold
#
# 而是：
#
#     interval_low <= previous_price * threshold
#
# ============================================================

def check_stock(
    name,
    symbol,
    state,
):

    print(
        f"\n========== {name} =========="
    )

    now = datetime.now(
        TW_TZ
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

    week_high = (
        get_week_high(
            symbol
        )
    )

    # --------------------------------------------------------
    # 建立狀態
    # --------------------------------------------------------

    state.setdefault(
        "interval_alert",
        {},
    )

    stock_state = (
        state[
            "interval_alert"
        ].setdefault(
            name,
            {},
        )
    )

    previous_check_time_text = (
        stock_state.get(
            "last_check_time"
        )
    )

    previous_check_price = (
        stock_state.get(
            "last_check_price"
        )
    )

    # --------------------------------------------------------
    # 如果沒有上次資料
    # --------------------------------------------------------

    if (
        not previous_check_time_text
        or previous_check_price is None
    ):

        print(
            "第一次執行，建立區間基準"
        )

        stock_state[
            "last_check_time"
        ] = now.isoformat()

        stock_state[
            "last_check_price"
        ] = current

        save_json(
            STATE_FILE,
            state,
        )

        return

    # --------------------------------------------------------
    # 讀取上次執行時間
    # --------------------------------------------------------

    try:

        previous_check_time = (
            datetime.fromisoformat(
                previous_check_time_text
            )
        )

        if (
            previous_check_time.tzinfo
            is None
        ):

            previous_check_time = (
                previous_check_time.replace(
                    tzinfo=TW_TZ
                )
            )

    except Exception:

        previous_check_time = (
            now
            - timedelta(
                minutes=15
            )
        )

    previous_check_price = float(
        previous_check_price
    )

    # --------------------------------------------------------
    # 取得本區間最低價
    # --------------------------------------------------------

    interval_low = (
        get_intraday_low(
            symbol,
            previous_check_time,
            now,
        )
    )

    # 如果Yahoo 1m沒有資料
    # 至少使用目前價格
    if interval_low is None:

        interval_low = current

        print(
            "⚠️ 無法取得完整1m區間，"
            "使用目前價格作為區間最低價"
        )

    # --------------------------------------------------------
    # 單日跌幅
    # --------------------------------------------------------

    daily_change = (
        current
        / previous_close
        - 1
    )

    # --------------------------------------------------------
    # 7日高點
    # --------------------------------------------------------

    if week_high is not None:

        weekly_change = (
            current
            / week_high
            - 1
        )

    else:

        weekly_change = None

    # --------------------------------------------------------
    # 區間跌幅
    # --------------------------------------------------------

    interval_change = (
        interval_low
        / previous_check_price
        - 1
    )

    print(
        f"上次偵測時間："
        f"{previous_check_time}"
    )

    print(
        f"上次偵測價格："
        f"{previous_check_price}"
    )

    print(
        f"本次目前價格："
        f"{current}"
    )

    print(
        f"本次區間最低："
        f"{interval_low}"
    )

    print(
        f"區間最低跌幅："
        f"{interval_change:.2%}"
    )

    print(
        f"單日跌幅："
        f"{daily_change:.2%}"
    )

    if weekly_change is not None:

        print(
            f"距7日高點："
            f"{weekly_change:.2%}"
        )

    # --------------------------------------------------------
    # 日期
    # --------------------------------------------------------

    today = datetime.now(
        TW_TZ
    ).strftime(
        "%Y-%m-%d"
    )

    # --------------------------------------------------------
    # 區間 -5%
    # --------------------------------------------------------

    if (
        interval_change
        <= DAILY_THRESHOLD
    ):

        last_alert_date = (
            stock_state.get(
                "interval_daily_alert_date"
            )
        )

        # 同一天只通知一次
        if (
            last_alert_date
            != today
        ):

            send_line(
                "🔴 區間跌幅通知\n\n"

                f"標的：{name}\n"

                f"上次偵測價格："
                f"{previous_check_price:,.2f}\n"

                f"本次區間最低："
                f"{interval_low:,.2f}\n"

                f"區間跌幅："
                f"{interval_change:.2%}\n"

                f"目前價格："
                f"{current:,.2f}\n\n"

                "⚠️ "
                "上一次偵測至本次偵測期間，"
                "曾跌破 -5%"
            )

            stock_state[
                "interval_daily_alert_date"
            ] = today

            print(
                "已發送：15分鐘區間 -5%"
            )

    # --------------------------------------------------------
    # 7日高點 -10%
    # --------------------------------------------------------

    if (
        weekly_change is not None
        and weekly_change
        <= WEEK_THRESHOLD
    ):

        last_week_alert_date = (
            stock_state.get(
                "weekly_alert_date"
            )
        )

        if (
            last_week_alert_date
            != today
        ):

            send_line(
                "🔴 7日跌幅通知\n\n"

                f"標的：{name}\n"

                f"目前價格："
                f"{current:,.2f}\n"

                f"過去7日最高價："
                f"{week_high:,.2f}\n"

                f"距7日高點跌幅："
                f"{weekly_change:.2%}\n\n"

                "⚠️ "
                "已達到一週 -10%，"
                "可加碼"
            )

            stock_state[
                "weekly_alert_date"
            ] = today

            print(
                "已發送：一週 -10%"
            )

    # --------------------------------------------------------
    # 更新下一次區間基準
    # --------------------------------------------------------

    stock_state[
        "last_check_time"
    ] = now.isoformat()

    stock_state[
        "last_check_price"
    ] = current

    stock_state[
        "last_interval_low"
    ] = interval_low


# ============================================================
# 單股估值分析
# ============================================================

def analyze_stock(
    stock,
    current_pe_data,
    market_pe,
    pe_history,
    margin_history,
    t86_history,
):

    code = stock["code"]

    name = (
        f"{code} "
        f"{stock['name']}"
    )

    symbol = stock[
        "symbol"
    ]

    industry = detect_industry(
        stock
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

    print(
        "\n================================"
    )

    print(
        f"單股分析：{name}"
    )

    print(
        f"產業：{industry}"
    )

    print(
        f"市場：{stock['market']}"
    )

    print(
        f"Yahoo：{symbol}"
    )

    print(
        "================================"
    )

    item = current_pe_data.get(
        code
    )

    if not item:

        return {
            "success": False,
            "message":
                "目前無法取得 "
                "官方PE/PB資料。",
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

    one_year_pe, sample_count = (
        calculate_one_year_average_pe(
            code,
            pe_history,
        )
    )

    # --------------------------------------------------------
    # 同業
    # --------------------------------------------------------

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

    industry_pe = None

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

    # --------------------------------------------------------
    # 評分
    # --------------------------------------------------------

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

            stock_pe is not None
            and market_pe is not None,

            (
                stock_pe
                < market_pe
                if (
                    stock_pe is not None
                    and market_pe is not None
                )
                else False
            ),

            "PE低於TAIEX",
        )

        add_score(

            stock_pe is not None
            and industry_pe is not None,

            (
                stock_pe
                < industry_pe
                if (
                    stock_pe is not None
                    and industry_pe is not None
                )
                else False
            ),

            "PE低於同業",
        )

        historical_active = (
            one_year_pe is not None
            and sample_count
            >= PE_MIN_HISTORY
        )

        add_score(

            historical_active,

            (
                stock_pe
                < one_year_pe
                if (
                    stock_pe is not None
                    and one_year_pe is not None
                )
                else False
            ),

            "PE低於一年平均",
        )

        if not historical_active:

            print(
                f"歷史PE目前"
                f"{sample_count}筆，"
                f"未達{PE_MIN_HISTORY}筆，"
                "一年平均PE暫不計分"
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

    # --------------------------------------------------------
    # 風險
    # --------------------------------------------------------

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
        short_margin_ratio is not None
        and short_margin_ratio < 3
    ):

        warnings.append(
            "券資比偏低"
        )

    # --------------------------------------------------------
    # 建議
    # --------------------------------------------------------

    if possible_score <= 0:

        level = (
            "⚪ 資料不足，"
            "暫不判斷"
        )

        ratio = 0

    else:

        ratio = (
            score
            / possible_score
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

            level = (
                "🟢 強烈建議加碼"
            )

        elif good:

            level = (
                "🟡 建議分批加碼"
            )

        else:

            level = (
                "⚪ 目前不建議加碼"
            )

    # --------------------------------------------------------
    # 輸出
    # --------------------------------------------------------

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
        f"{sample_count}"
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
        f"K={format_number(k)} "
        f"/ D={format_number(d)}"
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

    # --------------------------------------------------------
    # LINE訊息
    # --------------------------------------------------------

    peer_text = (
        format_number(
            industry_pe
        )
        if industry_pe is not None
        else "N/A"
    )

    message = (

        f"{level}\n\n"

        f"標的：{name}\n"

        f"市場："
        f"{stock['market']}\n"

        f"產業："
        f"{industry}\n\n"

        "【估值】\n"

        f"PE："
        f"{format_number(stock_pe)} 倍\n"

        f"TAIEX PE："
        f"{format_number(market_pe)} 倍\n"

        f"同業平均 PE："
        f"{peer_text} 倍\n"

        f"1年平均 PE："
        f"{format_number(one_year_pe)} 倍\n"

        f"歷史樣本："
        f"{sample_count} 筆\n"

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
        f"K {format_number(k)} "
        f"/ D {format_number(d)}\n"

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
        f"{score}/{possible_score}"

        if possible_score > 0
        else
        "加碼評分：資料不足"

    )

    message += (

        "\n"

        f"目前判斷："
        f"{level}\n"

        "━━━━━━━━━━\n\n"

        "加分項目：\n"

        + (
            "、".join(
                reasons_good
            )
            if reasons_good
            else "無"
        )

        + "\n\n"

        "風險提示：\n"

        + (
            "、".join(
                warnings
            )
            if warnings
            else "無"
        )
    )

    return {
        "success": True,
        "message": message,
        "level": level,
        "score": score,
        "possible_score":
            possible_score,
        "ratio": ratio,
    }


# ============================================================
# LINE 好友單股分析
# ============================================================

def analyze_stock_from_line(
    query
):

    stock = resolve_stock(
        query
    )

    if stock is None:

        return (
            "❌ 找不到這支股票。\n\n"
            "請輸入：\n"
            "2330\n"
            "台積電\n"
            "5347\n"
            "世界\n\n"
            "目前支援上市／上櫃股票。"
        )

    # --------------------------------------------------------
    # 讀取資料
    # --------------------------------------------------------

    current_pe_data = (
        get_all_pe_data()
    )

    market_pe = (
        calculate_taiex_market_pe()
    )

    state = load_json(
        STATE_FILE
    )

    pe_history = load_json(
        PE_HISTORY_FILE
    )

    margin_history = load_json(
        CHIP_HISTORY_FILE
    )

    # --------------------------------------------------------
    # 新股票加入PE歷史
    # --------------------------------------------------------

    pe_history = (
        update_pe_history(
            [stock["code"]],
            pe_history,
        )
    )

    save_json(
        PE_HISTORY_FILE,
        pe_history,
    )

    # --------------------------------------------------------
    # 融資
    # --------------------------------------------------------

    margin_history = (
        update_margin_history(
            [stock["code"]],
            margin_history,
        )
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

    # --------------------------------------------------------
    # 分析
    # --------------------------------------------------------

    result = analyze_stock(
        stock,
        current_pe_data,
        market_pe,
        pe_history,
        margin_history,
        t86_history,
    )

    if not result["success"]:

        return (
            f"⚠️ {stock['code']} "
            f"{stock['name']}\n\n"
            f"{result['message']}"
        )

    return result["message"]


# ============================================================
# LINE Webhook簽章驗證
# ============================================================

def verify_line_signature(
    body,
    signature,
):

    if not LINE_CHANNEL_SECRET:

        # 沒設定Secret時不驗證
        # 方便舊環境先跑
        return True

    import hmac
    import hashlib
    import base64

    digest = hmac.new(
        LINE_CHANNEL_SECRET.encode(
            "utf-8"
        ),
        body,
        hashlib.sha256,
    ).digest()

    expected = (
        base64.b64encode(
            digest
        ).decode(
            "utf-8"
        )
    )

    return hmac.compare_digest(
        expected,
        signature or "",
    )


# ============================================================
# LINE Webhook
# ============================================================

def create_flask_app():

    if not FLASK_AVAILABLE:

        raise RuntimeError(
            "缺少 Flask。\n"
            "請安裝：pip install flask"
        )

    app = Flask(
        __name__
    )

    @app.route(
        "/",
        methods=["GET"],
    )
    def index():

        return (
            "Stock Alert Bot V2.2 OK"
        )

    @app.route(
        "/callback",
        methods=["POST"],
    )
    def callback():

        body = request.get_data()

        signature = request.headers.get(
            "X-Line-Signature",
            "",
        )

        if not verify_line_signature(
            body,
            signature,
        ):

            return (
                "Invalid signature",
                400,
            )

        payload = request.get_json(
            silent=True
        )

        if not payload:

            return (
                "OK",
                200,
            )

        events = payload.get(
            "events",
            [],
        )

        for event in events:

            if event.get(
                "type"
            ) != "message":

                continue

            message = event.get(
                "message",
                {},
            )

            if message.get(
                "type"
            ) != "text":

                continue

            text = (
                message.get(
                    "text",
                    "",
                )
                .strip()
            )

            reply_token = (
                event.get(
                    "replyToken"
                )
            )

            if not text:

                continue

            # ------------------------------------------------
            # 指令
            # ------------------------------------------------

            if text in [
                "help",
                "幫助",
                "說明",
                "選單",
            ]:

                reply_line(

                    reply_token,

                    "📈 股票分析 Bot\n\n"

                    "直接輸入股票代號或名稱即可。\n\n"

                    "例如：\n"

                    "2330\n"
                    "台積電\n"
                    "5347\n"
                    "世界\n\n"

                    "Bot會自動：\n"

                    "✓ 找股票\n"
                    "✓ 找產業\n"
                    "✓ 找同業\n"
                    "✓ 套用估值模型\n"
                    "✓ 分析技術面\n"
                    "✓ 分析法人籌碼\n"
                    "✓ 判斷是否適合加碼"
                )

                continue

            # ------------------------------------------------
            # 背景執行
            #
            # LINE reply token不能等太久。
            # 分析可能需要數十秒，
            # 因此這裡先用背景thread。
            # ------------------------------------------------

            def worker(
                token,
                query,
            ):

                try:

                    reply = (
                        analyze_stock_from_line(
                            query
                        )
                    )

                    reply_line(
                        token,
                        reply,
                    )

                except Exception as e:

                    print(
                        "LINE單股分析錯誤：",
                        e,
                    )

                    reply_line(

                        token,

                        "⚠️ 分析時發生錯誤。\n\n"
                        f"{str(e)[:500]}"
                    )

            threading.Thread(
                target=worker,
                args=(
                    reply_token,
                    text,
                ),
                daemon=True,
            ).start()

        return (
            "OK",
            200,
        )

    return app


# ============================================================
# 自動取得所有需要PE歷史的股票
#
# 這裡包含：
# 1. 舊VALUTION_STOCKS
# 2. LINE曾經分析過的股票
# ============================================================

def get_tracked_valuation_codes(
    pe_history,
):

    codes = set(
        VALUATION_STOCKS.keys()
    )

    for code in pe_history.keys():

        if code.startswith("_"):
            continue

        if (
            code.isdigit()
            and len(code) == 4
        ):

            codes.add(code)

    return sorted(
        codes
    )


# ============================================================
# 自動分析舊版固定股票
#
# 保留原本每日自動分析2330 / 3711。
# ============================================================

def run_scheduled_valuation():

    print(
        "\n========== "
        "每日自動估值分析 "
        "=========="
    )

    current_pe_data = (
        get_all_pe_data()
    )

    if not current_pe_data:

        print(
            "⚠️ 無法取得PE資料"
        )

        return

    market_pe = (
        calculate_taiex_market_pe()
    )

    state = load_json(
        STATE_FILE
    )

    pe_history = load_json(
        PE_HISTORY_FILE
    )

    margin_history = load_json(
        CHIP_HISTORY_FILE
    )

    # --------------------------------------------------------
    # 自動取得追蹤股票
    # --------------------------------------------------------

    target_codes = (
        get_tracked_valuation_codes(
            pe_history
        )
    )

    # 舊版固定股票也一定加入
    target_codes = sorted(
        set(
            target_codes
            + list(
                VALUATION_STOCKS.keys()
            )
        )
    )

    # --------------------------------------------------------
    # PE歷史
    # --------------------------------------------------------

    pe_history = (
        update_pe_history(
            target_codes,
            pe_history,
        )
    )

    save_json(
        PE_HISTORY_FILE,
        pe_history,
    )

    # --------------------------------------------------------
    # 融資
    # --------------------------------------------------------

    margin_history = (
        update_margin_history(
            target_codes,
            margin_history,
        )
    )

    save_json(
        CHIP_HISTORY_FILE,
        margin_history,
    )

    # --------------------------------------------------------
    # 法人
    # --------------------------------------------------------

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
        "法人有效交易日："
        f"{len(t86_history)}"
    )

    # --------------------------------------------------------
    # 分析舊版固定股票
    # --------------------------------------------------------

    for code, stock_info in (
        VALUATION_STOCKS.items()
    ):

        try:

            stock = {
                "code": code,
                "name":
                    stock_info["name"]
                    .replace(
                        f"{code} ",
                        "",
                    ),
                "symbol":
                    stock_info["symbol"],
                "industry":
                    stock_info["industry"],
                "market":
                    "TWSE"
                    if stock_info[
                        "symbol"
                    ].endswith(".TW")
                    else "TPEx",
                "industry_code":
                    "",
            }

            result = analyze_stock(
                stock,
                current_pe_data,
                market_pe,
                pe_history,
                margin_history,
                t86_history,
            )

            # 自動通知只有達到加碼門檻
            if result["success"]:

                if result[
                    "level"
                ] in [
                    "🟢 強烈建議加碼",
                    "🟡 建議分批加碼",
                ]:

                    state.setdefault(
                        "valuation_v22",
                        {},
                    )

                    notified = (
                        state[
                            "valuation_v22"
                        ].get(
                            code,
                            False,
                        )
                    )

                    if not notified:

                        send_line(
                            result[
                                "message"
                            ]
                        )

                        state[
                            "valuation_v22"
                        ][code] = True

                        print(
                            f"{code} "
                            "已發送估值通知"
                        )

                    else:

                        print(
                            f"{code} "
                            "估值條件已通知，"
                            "跳過"
                        )

                else:

                    state.setdefault(
                        "valuation_v22",
                        {},
                    )

                    state[
                        "valuation_v22"
                    ][code] = False

        except Exception as e:

            print(
                f"{code} "
                f"自動估值分析錯誤：",
                e,
            )

    save_json(
        STATE_FILE,
        state,
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
        "V2.2自動估值 + "
        "技術 + 籌碼"
    )

    print(
        "================================"
    )

    state = load_json(
        STATE_FILE
    )

    # --------------------------------------------------------
    # 1. 先取得基本資料
    # --------------------------------------------------------

    current_pe_data = {}

    try:

        current_pe_data = (
            get_all_pe_data()
        )

        print(
            "取得 "
            f"{len(current_pe_data)} "
            "筆上市/上櫃PE資料"
        )

    except Exception as e:

        print(
            "⚠️ PE資料取得失敗：",
            e,
        )

    # --------------------------------------------------------
    # 2. 市場PE
    # --------------------------------------------------------

    try:

        market_pe = (
            calculate_taiex_market_pe()
        )

    except Exception as e:

        print(
            "市場PE錯誤：",
            e,
        )

        market_pe = None

    # --------------------------------------------------------
    # 3. PE歷史
    #
    # 自動追蹤：
    # - 舊股票
    # - 曾經LINE分析過的股票
    # --------------------------------------------------------

    pe_history = load_json(
        PE_HISTORY_FILE
    )

    target_codes = (
        get_tracked_valuation_codes(
            pe_history
        )
    )

    pe_history = (
        update_pe_history(
            target_codes,
            pe_history,
        )
    )

    save_json(
        PE_HISTORY_FILE,
        pe_history,
    )

    # --------------------------------------------------------
    # 4. 融資
    # --------------------------------------------------------

    margin_history = load_json(
        CHIP_HISTORY_FILE
    )

    margin_history = (
        update_margin_history(
            target_codes,
            margin_history,
        )
    )

    save_json(
        CHIP_HISTORY_FILE,
        margin_history,
    )

    # --------------------------------------------------------
    # 5. 法人
    # --------------------------------------------------------

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
        "法人有效交易日："
        f"{len(t86_history)}"
    )

    # --------------------------------------------------------
    # 6. 跌幅監控
    # --------------------------------------------------------

    for name, symbol in (
        STOCKS.items()
    ):

        try:

            check_stock(
                name,
                symbol,
                state,
            )

        except Exception as e:

            print(
                f"{name} "
                f"發生錯誤：{e}"
            )

    # --------------------------------------------------------
    # 7. 自動估值
    # --------------------------------------------------------

    try:

        run_scheduled_valuation()

    except Exception as e:

        print(
            "自動估值錯誤：",
            e,
        )

    # --------------------------------------------------------
    # 8. 儲存狀態
    # --------------------------------------------------------

    save_json(
        STATE_FILE,
        state,
    )

    print(
        "\n全部檢查完成"
    )


# ============================================================
# Web Server
# ============================================================

def run_webhook_server():

    app = create_flask_app()

    port = int(
        os.environ.get(
            "PORT",
            "8080",
        )
    )

    print(
        "================================"
    )

    print(
        "LINE Webhook Server V2.2"
    )

    print(
        f"Port：{port}"
    )

    print(
        "Callback：/callback"
    )

    print(
        "================================"
    )

    app.run(
        host="0.0.0.0",
        port=port,
    )


# ============================================================
# 啟動模式
#
# MODE=webhook
#     啟動LINE Webhook
#
# MODE=once
#     執行一次股票偵測
#
# MODE未設定
#     預設執行一次
# ============================================================

if __name__ == "__main__":

    mode = os.environ.get(
        "MODE",
        "once",
    ).lower()

    if mode == "webhook":

        if not FLASK_AVAILABLE:

            raise RuntimeError(
                "目前環境沒有Flask。\n"
                "請安裝：pip install flask"
            )

        run_webhook_server()

    else:

        main()
