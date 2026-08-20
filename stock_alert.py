# ============================================================
# stock_alert.py V2.5
#
# 股票跌幅 + 15分鐘區間最低價 + 自動產業估值 + 技術 + 籌碼
#
# V2.5 重點：
#
# 1. 不再使用固定 INDUSTRY_POOL
# 2. 自動取得上市 / 上櫃公司基本資料
# 3. 自動判斷市場 TWSE / TPEX
# 4. 自動取得目前產業
# 5. 同業改為該產業「目前市值 TOP 10」
# 6. PE 歷史不足 60 筆，自動向過去交易日回補
# 7. 回補 PE 必須是「該日期官方資料」
# 8. 絕不使用今天 PE 填入歷史日期
# 9. 歷史不足 60 筆時，一年平均 PE 不計分
# 10. 滿 60 筆後才啟用一年平均 PE
# 11. 15分鐘偵測改為「本次～上次執行期間最低價」
# 12. 15分鐘資料失敗不會讓估值程式中斷
# 13. 上櫃股票自動使用 .TWO
# 14. LINE 好友可以輸入股票代號 / 股票名稱
# 15. 不需要手動加入 VALUATION_STOCKS
# 16. 所有主要資料函式都有防呆
#
# ============================================================

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

TPEX_BASE = "https://www.tpex.org.tw/openapi"

STATE_FILE = "alert_state.json"
PE_HISTORY_FILE = "pe_history.json"
CHIP_HISTORY_FILE = "chip_history.json"

# 動態市場資料快取
UNIVERSE_CACHE_FILE = "market_universe_cache.json"

# LINE Webhook 驗證 / 回覆使用
LINE_REPLY_URL = (
    "https://api.line.me/v2/bot/message/reply"
)

# ============================================================
# 跌幅條件
# ============================================================

DAILY_THRESHOLD = -0.05
WEEK_THRESHOLD = -0.10

# ============================================================
# PE
# ============================================================

PE_MIN_HISTORY = 60
PE_MAX_VALID = 200

# ============================================================
# 評分
# ============================================================

STRONG_SCORE = 8
GOOD_SCORE = 6

# ============================================================
# 時區
# ============================================================

TW_TZ = ZoneInfo("Asia/Taipei")

# ============================================================
# API
# ============================================================

TWSE_TIMEOUT = 20
TPEX_TIMEOUT = 20

T86_RETRIES = 2

API_SLEEP = 0.10

# ============================================================
# 動態資料快取
#
# 產業 / 股票基本資料不需要每15分鐘重新抓。
# ============================================================

UNIVERSE_CACHE_HOURS = 24


# ============================================================
# 自動監控標的
# ============================================================

STOCKS = {
    "0050 元大台灣50": "0050.TW",
    "2330 台積電": "2330.TW",
    "3711 日月光投控": "3711.TW",
    "QQQ": "QQQ",
    "台灣加權指數": "^TWII",
}


# ============================================================
# 產業估值模型
#
# 注意：
# 這裡「不是股票池」。
#
# 只是依官方產業名稱判斷使用哪一套估值模型。
# 同業股票完全由市場資料動態產生。
# ============================================================

INDUSTRY_MODEL = {

    # 半導體
    "半導體業": {
        "pe": True,
        "peg": True,
        "pb": True,
        "yield": False,
        "roe": True,
    },

    # 電腦及週邊
    "電腦及週邊設備業": {
        "pe": True,
        "peg": True,
        "pb": True,
        "yield": False,
        "roe": True,
    },

    # 電子零組件
    "電子零組件業": {
        "pe": True,
        "peg": True,
        "pb": True,
        "yield": False,
        "roe": True,
    },

    # 其他電子
    "其他電子業": {
        "pe": True,
        "peg": True,
        "pb": True,
        "yield": False,
        "roe": True,
    },

    # 光電
    "光電業": {
        "pe": True,
        "peg": True,
        "pb": True,
        "yield": False,
        "roe": True,
    },

    # 通信網路
    "通信網路業": {
        "pe": True,
        "peg": True,
        "pb": True,
        "yield": False,
        "roe": True,
    },

    # 資訊服務
    "資訊服務業": {
        "pe": True,
        "peg": True,
        "pb": True,
        "yield": False,
        "roe": True,
    },

    # 金融
    "金融業": {
        "pe": False,
        "peg": False,
        "pb": True,
        "yield": True,
        "roe": True,
    },

    # 銀行
    "銀行業": {
        "pe": False,
        "peg": False,
        "pb": True,
        "yield": True,
        "roe": True,
    },

    # 保險
    "保險業": {
        "pe": False,
        "peg": False,
        "pb": True,
        "yield": True,
        "roe": True,
    },

    # 電信
    "通信網路業": {
        "pe": True,
        "peg": False,
        "pb": False,
        "yield": True,
        "roe": False,
    },

    # 傳產
    "食品工業": {
        "pe": True,
        "peg": False,
        "pb": True,
        "yield": True,
        "roe": True,
    },

    "塑膠工業": {
        "pe": True,
        "peg": False,
        "pb": True,
        "yield": True,
        "roe": True,
    },

    "紡織纖維": {
        "pe": True,
        "peg": False,
        "pb": True,
        "yield": True,
        "roe": True,
    },

    "電機機械": {
        "pe": True,
        "peg": True,
        "pb": True,
        "yield": False,
        "roe": True,
    },

    "鋼鐵工業": {
        "pe": True,
        "peg": False,
        "pb": True,
        "yield": True,
        "roe": True,
    },

    "建材營造": {
        "pe": True,
        "peg": False,
        "pb": True,
        "yield": True,
        "roe": True,
    },

    "航運業": {
        "pe": True,
        "peg": False,
        "pb": True,
        "yield": True,
        "roe": True,
    },

    "觀光餐旅": {
        "pe": True,
        "peg": False,
        "pb": True,
        "yield": True,
        "roe": True,
    },

    "化學工業": {
        "pe": True,
        "peg": False,
        "pb": True,
        "yield": True,
        "roe": True,
    },

    "生技醫療": {
        "pe": True,
        "peg": True,
        "pb": True,
        "yield": False,
        "roe": True,
    },

    "醫療保健": {
        "pe": True,
        "peg": True,
        "pb": True,
        "yield": False,
        "roe": True,
    },

    "其他": {
        "pe": True,
        "peg": False,
        "pb": True,
        "yield": True,
        "roe": True,
    },
}


DEFAULT_MODEL = {
    "pe": True,
    "peg": False,
    "pb": True,
    "yield": False,
    "roe": True,
}


# ============================================================
# 工具
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


def clean_code(value):

    if value is None:
        return ""

    text = str(value).strip()

    if text.endswith(".TW"):
        text = text[:-3]

    if text.endswith(".TWO"):
        text = text[:-4]

    return text


def normalize_name(value):

    if value is None:
        return ""

    return (
        str(value)
        .strip()
        .replace(" ", "")
        .replace("　", "")
        .lower()
    )


# ============================================================
# HTTP
# ============================================================

def twse_get(endpoint, params=None):

    try:

        response = requests.get(
            TWSE_BASE + endpoint,
            params=params,
            timeout=TWSE_TIMEOUT,
            headers={
                "User-Agent": "Mozilla/5.0"
            },
        )

        response.raise_for_status()

        return response.json()

    except Exception as e:

        print(
            f"TWSE API失敗：{endpoint} / {e}"
        )

        return None


def twse_web_get(endpoint, params=None):

    try:

        response = requests.get(
            TWSE_WEB_BASE + endpoint,
            params=params,
            timeout=TWSE_TIMEOUT,
            headers={
                "User-Agent": "Mozilla/5.0"
            },
        )

        response.raise_for_status()

        return response.json()

    except Exception as e:

        print(
            f"TWSE Web API失敗：{endpoint} / {e}"
        )

        return None


def tpex_get(endpoint, params=None):

    try:

        response = requests.get(
            TPEX_BASE + endpoint,
            params=params,
            timeout=TPEX_TIMEOUT,
            headers={
                "User-Agent": "Mozilla/5.0"
            },
        )

        response.raise_for_status()

        return response.json()

    except Exception as e:

        print(
            f"TPEX API失敗：{endpoint} / {e}"
        )

        return None


# ============================================================
# LINE
# ============================================================

def send_line(message):

    try:

        response = requests.post(
            "https://api.line.me/v2/bot/message/broadcast",
            headers={
                "Authorization":
                    f"Bearer {LINE_TOKEN}",
                "Content-Type":
                    "application/json",
            },
            json={
                "messages": [
                    {
                        "type": "text",
                        "text": message[:5000],
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

    except Exception as e:

        print(f"LINE廣播失敗：{e}")


def reply_line(reply_token, message):

    if not reply_token:
        return False

    try:

        response = requests.post(
            LINE_REPLY_URL,
            headers={
                "Authorization":
                    f"Bearer {LINE_TOKEN}",
                "Content-Type":
                    "application/json",
            },
            json={
                "replyToken": reply_token,
                "messages": [
                    {
                        "type": "text",
                        "text": message[:5000],
                    }
                ],
            },
            timeout=20,
        )

        if response.status_code != 200:

            print(
                "LINE回覆失敗：",
                response.status_code,
                response.text,
            )

            return False

        return True

    except Exception as e:

        print(f"LINE reply失敗：{e}")

        return False


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
# TWSE 股票基本資料
# ============================================================

def get_twse_universe():

    data = twse_get(
        "/opendata/t187ap03_L"
    )

    result = []

    if not isinstance(data, list):
        return result

    for row in data:

        code = clean_code(
            row.get(
                "公司代號",
                row.get("Code", ""),
            )
        )

        name = (
            row.get("公司名稱")
            or row.get("CompanyName")
            or row.get("公司簡稱")
            or ""
        )

        industry = (
            row.get("產業類別")
            or row.get("Industry")
            or ""
        )

        capital = find_value(
            row,
            [
                "實收資本額",
                "實收資本額(元)",
                "PaidinCapital",
            ],
        )

        if not code:
            continue

        if not code.isdigit():
            continue

        result.append(
            {
                "code": code,
                "name": str(name).strip(),
                "industry": str(industry).strip(),
                "market": "TWSE",
                "symbol": f"{code}.TW",
                "capital": capital,
            }
        )

    return result


# ============================================================
# TPEX 股票基本資料
# ============================================================

def get_tpex_universe():

    data = tpex_get(
        "/mopsfin_t187ap03_O"
    )

    result = []

    if not isinstance(data, list):
        return result

    for row in data:

        code = clean_code(
            row.get(
                "證券代號",
                row.get(
                    "公司代號",
                    row.get("Code", ""),
                ),
            )
        )

        name = (
            row.get("公司名稱")
            or row.get("證券名稱")
            or row.get("公司簡稱")
            or ""
        )

        industry = (
            row.get("產業類別")
            or row.get("產業別")
            or row.get("Industry")
            or ""
        )

        capital = find_value(
            row,
            [
                "實收資本額",
                "實收資本額(元)",
                "PaidinCapital",
            ],
        )

        if not code:
            continue

        if not code.isdigit():
            continue

        result.append(
            {
                "code": code,
                "name": str(name).strip(),
                "industry": str(industry).strip(),
                "market": "TPEX",
                "symbol": f"{code}.TWO",
                "capital": capital,
            }
        )

    return result


# ============================================================
# TPEX 市值排行
#
# 官方提供「上櫃歷史個股市值排行」資料。
#
# 若 API 可取得目前資料，優先使用官方市值。
# ============================================================

def get_tpex_market_values():

    result = {}

    data = tpex_get(
        "/tpex_daily_market_value"
    )

    if not isinstance(data, list):
        return result

    for row in data:

        code = clean_code(
            row.get(
                "證券代號",
                row.get(
                    "Code",
                    row.get("代號", ""),
                ),
            )
        )

        market_cap = find_value(
            row,
            [
                "市值",
                "總市值",
                "MarketValue",
                "market_value",
            ],
        )

        if code and market_cap is not None:

            result[code] = market_cap

    return result


# ============================================================
# TWSE 當日行情
# ============================================================

def get_twse_daily_quotes():

    data = twse_get(
        "/exchangeReport/STOCK_DAY_ALL"
    )

    result = {}

    if not isinstance(data, list):
        return result

    for row in data:

        code = clean_code(
            row.get(
                "Code",
                row.get(
                    "證券代號",
                    "",
                ),
            )
        )

        close = find_value(
            row,
            [
                "ClosingPrice",
                "收盤價",
            ],
        )

        if code and close is not None:

            result[code] = close

    return result


# ============================================================
# 建立市場 Universe
# ============================================================

def build_market_universe():

    print(
        "\n========== 建立動態市場股票池 =========="
    )

    twse = get_twse_universe()

    print(
        f"TWSE 基本資料：{len(twse)}"
    )

    tpex = get_tpex_universe()

    print(
        f"TPEX 基本資料：{len(tpex)}"
    )

    if not twse and not tpex:

        print(
            "⚠️ 無法取得市場基本資料"
        )

        return {}

    twse_quotes = get_twse_daily_quotes()

    print(
        f"TWSE 當日行情："
        f"{len(twse_quotes)}"
    )

    tpex_values = get_tpex_market_values()

    print(
        f"TPEX 市值資料："
        f"{len(tpex_values)}"
    )

    universe = {}

    # --------------------------------------------------------
    # TWSE
    # --------------------------------------------------------

    for item in twse:

        code = item["code"]

        price = twse_quotes.get(code)

        market_cap = None

        capital = item.get("capital")

        if (
            capital is not None
            and price is not None
        ):

            # 實收資本額 / 10 = 約略股數
            shares = capital / 10

            market_cap = (
                shares * price
            )

        item["price"] = price
        item["market_cap"] = market_cap

        universe[code] = item

    # --------------------------------------------------------
    # TPEX
    # --------------------------------------------------------

    for item in tpex:

        code = item["code"]

        market_cap = tpex_values.get(
            code
        )

        item["market_cap"] = market_cap

        universe[code] = item

    # --------------------------------------------------------
    # 移除無產業資料
    # --------------------------------------------------------

    valid = {}

    for code, item in universe.items():

        if not item.get("industry"):
            continue

        valid[code] = item

    print(
        f"有效動態股票：{len(valid)}"
    )

    return valid


# ============================================================
# Universe 快取
# ============================================================

def get_market_universe():

    cache = load_json(
        UNIVERSE_CACHE_FILE
    )

    now = time.time()

    cached_at = cache.get(
        "_cached_at"
    )

    cached_data = cache.get(
        "data"
    )

    if (
        cached_at
        and cached_data
        and now - cached_at
        < UNIVERSE_CACHE_HOURS * 3600
    ):

        print(
            "使用市場股票池快取"
        )

        return cached_data

    universe = build_market_universe()

    if universe:

        save_json(
            UNIVERSE_CACHE_FILE,
            {
                "_cached_at": now,
                "data": universe,
            },
        )

        return universe

    # API失敗時使用舊快取
    if cached_data:

        print(
            "⚠️ 市場資料更新失敗，"
            "使用舊快取"
        )

        return cached_data

    return {}


# ============================================================
# 股票搜尋
# ============================================================

def resolve_stock(query, universe):

    query = str(query).strip()

    if not query:
        return None

    code_query = clean_code(query)

    # --------------------------------------------------------
    # 直接代號
    # --------------------------------------------------------

    if code_query in universe:

        return universe[
            code_query
        ]

    normalized_query = normalize_name(
        query
    )

    # --------------------------------------------------------
    # 股票名稱
    # --------------------------------------------------------

    exact_matches = []

    for code, item in universe.items():

        name = normalize_name(
            item.get("name", "")
        )

        if (
            name == normalized_query
        ):

            exact_matches.append(
                item
            )

    if len(exact_matches) == 1:
        return exact_matches[0]

    # --------------------------------------------------------
    # 名稱包含
    # --------------------------------------------------------

    partial = []

    for code, item in universe.items():

        name = normalize_name(
            item.get("name", "")
        )

        if (
            normalized_query
            and normalized_query in name
        ):

            partial.append(item)

    if len(partial) == 1:
        return partial[0]

    # --------------------------------------------------------
    # Yahoo symbol
    # --------------------------------------------------------

    upper_query = query.upper()

    for item in universe.values():

        if (
            item.get("symbol", "")
            .upper()
            == upper_query
        ):

            return item

    return None


# ============================================================
# 動態同業 TOP 10
# ============================================================

def get_dynamic_industry_peers(
    target_code,
    industry,
    universe,
    limit=10,
):

    peers = []

    for code, item in universe.items():

        if code == target_code:
            continue

        if (
            item.get("industry")
            != industry
        ):
            continue

        market_cap = item.get(
            "market_cap"
        )

        if market_cap is None:
            continue

        peers.append(
            {
                "code": code,
                "name": item.get(
                    "name",
                    "",
                ),
                "symbol": item.get(
                    "symbol",
                    "",
                ),
                "market_cap": market_cap,
                "market": item.get(
                    "market",
                    "",
                ),
            }
        )

    peers.sort(
        key=lambda x: x[
            "market_cap"
        ],
        reverse=True,
    )

    return peers[:limit]


# ============================================================
# PE / PB / 殖利率
# ============================================================

def parse_twse_pe_rows(data):

    result = {}

    if not isinstance(data, list):
        return result

    for row in data:

        code = clean_code(
            row.get(
                "Code",
                row.get(
                    "證券代號",
                    "",
                ),
            )
        )

        if not code:
            continue

        result[code] = {
            "name":
                row.get(
                    "Name",
                    row.get(
                        "證券名稱",
                        "",
                    ),
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


def get_twse_pe_data():

    data = twse_get(
        "/exchangeReport/BWIBBU_ALL"
    )

    result = parse_twse_pe_rows(
        data
    )

    print(
        f"TWSE PE資料：{len(result)}"
    )

    return result


# ============================================================
# 指定日期 TWSE PE
# ============================================================

def get_twse_pe_by_date(
    date_string
):

    data = twse_get(
        "/exchangeReport/BWIBBU_d",
        params={
            "date": date_string,
            "selectType": "ALL",
            "response": "json",
        },
    )

    if not isinstance(data, dict):
        return {}

    if data.get("stat") != "OK":
        return {}

    rows = data.get(
        "data",
        []
    )

    result = {}

    for row in rows:

        if not isinstance(row, list):
            continue

        if len(row) < 5:
            continue

        code = clean_code(
            row[0]
        )

        pe = to_float(
            row[4]
        )

        if (
            code
            and pe is not None
            and 0 < pe <= PE_MAX_VALID
        ):

            result[code] = pe

    return result


# ============================================================
# PE 歷史回補
#
# 重要：
#
# 絕對不把今天 PE 複製到過去。
#
# 每一個日期都重新向官方 API 查詢。
# ============================================================

def update_pe_history(
    target_codes,
    history,
):

    if not isinstance(
        history,
        dict,
    ):
        history = {}

    today = datetime.now(
        TW_TZ
    ).date()

    # --------------------------------------------------------
    # 先抓今天
    # --------------------------------------------------------

    today_string = today.strftime(
        "%Y%m%d"
    )

    today_data = get_twse_pe_by_date(
        today_string
    )

    for code in target_codes:

        pe = today_data.get(code)

        if (
            pe is not None
            and 0 < pe <= PE_MAX_VALID
        ):

            history.setdefault(
                code,
                {},
            )

            history[code][
                today_string
            ] = pe

    # --------------------------------------------------------
    # 每支股票確認歷史筆數
    # --------------------------------------------------------

    for code in target_codes:

        stock_history = history.get(
            code,
            {},
        )

        valid_dates = []

        for date_string, pe in (
            stock_history.items()
        ):

            try:

                datetime.strptime(
                    date_string,
                    "%Y%m%d",
                )

            except Exception:
                continue

            if (
                pe is not None
                and 0 < float(pe)
                <= PE_MAX_VALID
            ):

                valid_dates.append(
                    date_string
                )

        current_count = len(
            set(valid_dates)
        )

        print(
            f"{code} 目前PE歷史："
            f"{current_count}筆"
        )

        if current_count >= PE_MIN_HISTORY:
            continue

        print(
            f"{code} PE不足"
            f"{PE_MIN_HISTORY}筆，"
            "開始向過去交易日回補..."
        )

        # ----------------------------------------------------
        # 從昨天往回找
        #
        # 每個日期真的呼叫官方 API
        # ----------------------------------------------------

        cursor = today - timedelta(
            days=1
        )

        checked_days = 0

        max_days = 150

        while (
            current_count
            < PE_MIN_HISTORY
            and checked_days
            < max_days
        ):

            date_string = cursor.strftime(
                "%Y%m%d"
            )

            # 已有就不要重新抓
            if date_string in (
                history.get(
                    code,
                    {},
                )
            ):

                cursor -= timedelta(
                    days=1
                )

                checked_days += 1

                continue

            pe_data = (
                get_twse_pe_by_date(
                    date_string
                )
            )

            pe = pe_data.get(code)

            if (
                pe is not None
                and 0 < pe <= PE_MAX_VALID
            ):

                history.setdefault(
                    code,
                    {},
                )

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

            cursor -= timedelta(
                days=1
            )

            checked_days += 1

            # API稍微休息
            time.sleep(
                API_SLEEP
            )

        print(
            f"{code} PE回補完成："
            f"{current_count}筆"
        )

    return history


# ============================================================
# 一年平均 PE
#
# 注意：
# 「60筆」是啟用門檻。
# 不代表一定要完整一年。
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
        - timedelta(days=365)
    )

    values = []

    for date_string, pe in (
        stock_history.items()
    ):

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
            or pe > PE_MAX_VALID
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

        if isinstance(
            data,
            list,
        ):

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

        if not values:
            return None

        market_pe = (
            sum(values)
            / len(values)
        )

        print(
            f"有效市場股票："
            f"{len(values)} 家"
        )

        print(
            f"TAIEX 官方口徑市場 PE："
            f"{market_pe:.2f}"
        )

        return market_pe

    except Exception as e:

        print(
            f"TAIEX市場PE失敗：{e}"
        )

        return None


# ============================================================
# 基本面
# ============================================================

def get_company_fundamentals(
    symbol
):

    result = {
        "earnings_growth": None,
        "roe": None,
    }

    try:

        ticker = yf.Ticker(
            symbol
        )

        info = ticker.info

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

            roe = float(
                roe
            )

            if abs(roe) < 5:

                roe *= 100

            result[
                "roe"
            ] = roe

    except Exception as e:

        print(
            f"{symbol} 基本面資料失敗："
            f"{e}"
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
# KD
# ============================================================

def calculate_kd(
    symbol
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

            value = float(
                value
            )

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

        close = (
            data["Close"]
            .dropna()
        )

        if len(close) < (
            period + 2
        ):
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
# 三大法人
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

                code = clean_code(
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

        except Exception as e:

            print(
                f"T86 {date_string} "
                f"失敗：{e}"
            )

            if attempt < (
                T86_RETRIES
            ):

                time.sleep(1)

    return {}


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

        date_string = day.strftime(
            "%Y%m%d"
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

            code = clean_code(
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
            f"融資融券失敗：{e}"
        )

        return {}


def update_margin_history(
    codes,
    history,
):

    today_string = datetime.now(
        TW_TZ
    ).strftime("%Y%m%d")

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

    previous_index = min(
        5,
        len(dates) - 1
    )

    previous = stock[
        dates[previous_index]
    ].get("margin")

    if (
        latest is None
        or previous is None
    ):
        return None

    return latest - previous


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
# 15分鐘區間
#
# 本次執行 vs 上次執行
#
# 不再只看「當下股價」。
#
# ============================================================

def get_interval_low(
    symbol,
    previous_time,
):

    print(
        f"\n---------- "
        f"15分鐘區間：{symbol} "
        f"----------"
    )

    try:

        ticker = yf.Ticker(
            symbol
        )

        data = ticker.history(
            period="1d",
            interval="1m",
            prepost=False,
            auto_adjust=False,
        )

        if data.empty:

            print(
                "無法取得盤中資料"
            )

            return None

        lows = data["Low"].dropna()

        if lows.empty:

            print(
                "無有效最低價"
            )

            return None

        # ----------------------------------------------------
        # 時間處理
        # ----------------------------------------------------

        if previous_time:

            try:

                previous_dt = (
                    datetime.fromisoformat(
                        previous_time
                    )
                )

                if (
                    previous_dt.tzinfo
                    is None
                ):

                    previous_dt = (
                        previous_dt.replace(
                            tzinfo=TW_TZ
                        )
                    )

                index = data.index

                # Yahoo通常帶時區
                if index.tz is None:

                    previous_dt = (
                        previous_dt.replace(
                            tzinfo=None
                        )
                    )

                mask = (
                    index
                    >= previous_dt
                )

                interval_data = data.loc[
                    mask
                ]

                if (
                    not interval_data.empty
                ):

                    lows = (
                        interval_data[
                            "Low"
                        ]
                        .dropna()
                    )

            except Exception as e:

                print(
                    "區間時間解析失敗："
                    f"{e}"
                )

        if lows.empty:

            print(
                "區間內無資料"
            )

            return None

        interval_low = float(
            lows.min()
        )

        print(
            f"區間最低價："
            f"{interval_low}"
        )

        return interval_low

    except Exception as e:

        print(
            f"{symbol} "
            f"15分鐘資料失敗：{e}"
        )

        return None


def check_interval_low(
    name,
    symbol,
    state,
):

    now = datetime.now(
        TW_TZ
    )

    now_iso = now.isoformat()

    state.setdefault(
        "interval_low",
        {}
    )

    stock_state = (
        state[
            "interval_low"
        ].setdefault(
            name,
            {}
        )
    )

    previous_time = (
        stock_state.get(
            "last_check"
        )
    )

    current_price = (
        get_latest_price(
            symbol
        )
    )

    if current_price is None:

        print(
            "無法取得目前價格"
        )

        stock_state[
            "last_check"
        ] = now_iso

        return

    interval_low = (
        get_interval_low(
            symbol,
            previous_time,
        )
    )

    if interval_low is None:

        print(
            "無法取得15分鐘區間資料"
        )

        stock_state[
            "last_check"
        ] = now_iso

        stock_state[
            "last_price"
        ] = current_price

        return

    # --------------------------------------------------------
    # 第一輪
    # --------------------------------------------------------

    if previous_time is None:

        print(
            "第一次執行，建立區間基準"
        )

        stock_state[
            "last_check"
        ] = now_iso

        stock_state[
            "last_price"
        ] = current_price

        stock_state[
            "last_interval_low"
        ] = interval_low

        return

    # --------------------------------------------------------
    # 判斷：
    #
    # 本次～上次期間最低價
    # 是否比上一次記錄價格下跌
    #
    # --------------------------------------------------------

    previous_price = (
        stock_state.get(
            "last_price"
        )
    )

    if previous_price is None:

        previous_price = (
            current_price
        )

    if interval_low < (
        float(previous_price)
    ):

        drop = (
            interval_low
            / float(previous_price)
            - 1
        )

        message = (
            "🔴 15分鐘區間低點通知\n\n"
            f"標的：{name}\n"
            f"上次偵測價格："
            f"{float(previous_price):,.2f}\n"
            f"區間最低價："
            f"{interval_low:,.2f}\n"
            f"最低跌幅："
            f"{drop:.2%}\n"
            f"目前價格："
            f"{current_price:,.2f}\n\n"
            "⚠️ 本次偵測期間曾跌破上次偵測價格"
        )

        send_line(
            message
        )

        print(
            "🔴 已發送15分鐘區間低點通知"
        )

    else:

        print(
            "本次期間最低價未跌破上次價格"
        )

    stock_state[
        "last_check"
    ] = now_iso

    stock_state[
        "last_price"
    ] = current_price

    stock_state[
        "last_interval_low"
    ] = interval_low


# ============================================================
# 最新價格
# ============================================================

def get_latest_price(
    symbol
):

    try:

        ticker = yf.Ticker(
            symbol
        )

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

            if not prices.empty:

                return float(
                    prices.iloc[-1]
                )

    except Exception as e:

        print(
            f"{symbol} 1m價格失敗："
            f"{e}"
        )

    try:

        daily = yf.Ticker(
            symbol
        ).history(
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

            if not prices.empty:

                return float(
                    prices.iloc[-1]
                )

    except Exception as e:

        print(
            f"{symbol} 日線價格失敗："
            f"{e}"
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

        if data.empty:
            return None

        close = data["Close"]

        if hasattr(
            close,
            "columns",
        ):

            close = close.iloc[
                :, 0
            ]

        return close.dropna()

    except Exception as e:

        print(
            f"{symbol}歷史資料失敗："
            f"{e}"
        )

        return None


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

        if highs.empty:
            return None

        return float(
            highs.max()
        )

    except Exception as e:

        print(
            f"{symbol} 7日高點失敗："
            f"{e}"
        )

        return None


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
            "daily_alert":
                False,
            "weekly_alert":
                False,
            "date":
                today,
        },
    )

    stock_state = (
        state[
            "daily"
        ][name]
    )

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
                "⚠️ 已達到單日 -5%，可進一步評估加碼"
            )

            stock_state[
                "daily_alert"
            ] = True

    else:

        stock_state[
            "daily_alert"
        ] = False

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
                "⚠️ 已達到一週 -10%，可進一步評估加碼"
            )

            stock_state[
                "weekly_alert"
            ] = True

    else:

        stock_state[
            "weekly_alert"
        ] = False


# ============================================================
# 估值分析
# ============================================================

def calculate_industry_pe(
    code,
    industry,
    universe,
    current_pe_data,
):

    peers = (
        get_dynamic_industry_peers(
            code,
            industry,
            universe,
            limit=10,
        )
    )

    values = []

    for peer in peers:

        item = current_pe_data.get(
            peer["code"]
        )

        if not item:
            continue

        pe = item.get(
            "pe"
        )

        if (
            pe is not None
            and 0 < pe <= PE_MAX_VALID
        ):

            values.append(
                pe
            )

    if not values:
        return None, peers

    return (
        sum(values)
        / len(values),
        peers,
    )


def analyze_stock(
    stock_info,
    current_pe_data,
    market_pe,
    pe_history,
    margin_history,
    t86_history,
    universe,
):

    code = stock_info["code"]
    name = stock_info["name"]
    symbol = stock_info["symbol"]
    industry = stock_info["industry"]

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
        f"市場："
        f"{stock_info['market']}"
    )

    print(
        f"Yahoo：{symbol}"
    )

    print(
        "================================"
    )

    model = INDUSTRY_MODEL.get(
        industry,
        DEFAULT_MODEL,
    )

    current = current_pe_data.get(
        code
    )

    if current is None:

        print(
            "目前無 PE / PB / 殖利率資料"
        )

        return {
            "level": None,
            "score": 0,
            "possible": 0,
            "message": (
                f"目前無法取得 {name} "
                "官方估值資料。"
            ),
        }

    stock_pe = current.get(
        "pe"
    )

    stock_yield = current.get(
        "yield"
    )

    stock_pb = current.get(
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

    historical_active = (
        sample_count
        >= PE_MIN_HISTORY
        and one_year_pe is not None
    )

    if not historical_active:

        print(
            f"歷史PE目前"
            f"{sample_count}筆，"
            f"未達{PE_MIN_HISTORY}筆，"
            "一年平均PE暫不計分"
        )

    # --------------------------------------------------------
    # 動態同業
    # --------------------------------------------------------

    industry_pe, peers = (
        calculate_industry_pe(
            code,
            industry,
            universe,
            current_pe_data,
        )
    )

    print(
        "動態同業 TOP10："
    )

    for peer in peers:

        print(
            f"  {peer['code']} "
            f"{peer['name']} "
            f"市值="
            f"{format_number(peer['market_cap'], 0)}"
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

    if model.get("pe"):

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
            "PE低於動態同業",
        )

        add_score(
            historical_active,
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

    if model.get("peg"):

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

    if model.get("pb"):

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

    if model.get("yield"):

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
    # 顯示
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
        f"動態同業 PE："
        f"{format_number(industry_pe)}"
    )

    print(
        f"一年平均 PE："
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
        f"K={format_number(k)} / "
        f"D={format_number(d)}"
    )

    print(
        f"RSI："
        f"{format_number(rsi)}"
    )

    print(
        f"法人5日："
        f"{format_number(inst_5d, 0)} 股"
    )

    print(
        f"法人20日："
        f"{format_number(inst_20d, 0)} 股"
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
        f"評分："
        f"{score}/{possible_score}"
    )

    ratio = (
        score / possible_score
        if possible_score > 0
        else 0
    )

    # --------------------------------------------------------
    # 加碼等級
    # --------------------------------------------------------

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
    # LINE 單股分析訊息
    # --------------------------------------------------------

    peer_text = []

    for peer in peers:

        peer_text.append(
            f"{peer['code']} "
            f"{peer['name']}"
        )

    peer_text_string = (
        "、".join(peer_text)
        if peer_text
        else "無"
    )

    history_status = (
        "已啟用"
        if historical_active
        else
        f"未啟用（{sample_count}/"
        f"{PE_MIN_HISTORY}筆）"
    )

    warning_text = (
        "、".join(warnings)
        if warnings
        else "無"
    )

    message = (

        f"{level}\n\n"

        f"標的："
        f"{code} {name}\n"

        f"產業："
        f"{industry}\n"

        f"市場："
        f"{stock_info['market']}\n\n"

        "【估值】\n"

        f"PE："
        f"{format_number(stock_pe)} 倍\n"

        f"TAIEX PE："
        f"{format_number(market_pe)} 倍\n"

        f"動態同業平均 PE："
        f"{format_number(industry_pe)} 倍\n"

        f"歷史平均 PE："
        f"{format_number(one_year_pe)} 倍\n"

        f"PE歷史資料："
        f"{sample_count} 筆\n"

        f"歷史PE評分："
        f"{history_status}\n"

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

        "【動態同業 TOP 10】\n"

        f"{peer_text_string}\n\n"

        "━━━━━━━━━━\n"

        f"加碼評分："
        f"{score}/{possible_score} "
        f"({ratio:.0%})\n"

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

        + warning_text
    )

    return {
        "level": level,
        "score": score,
        "possible": possible_score,
        "ratio": ratio,
        "message": message,
        "stock": stock_info,
    }


# ============================================================
# LINE 單股分析
# ============================================================

def line_single_stock_analysis(
    query,
    universe,
):

    stock_info = resolve_stock(
        query,
        universe,
    )

    if stock_info is None:

        return (
            "❌ 找不到這支股票。\n\n"
            "請輸入：\n"
            "2330\n"
            "台積電\n"
            "5347\n"
            "世界\n\n"
            "目前僅支援上市 / 上櫃股票。"
        )

    print(
        "\n================================"
    )

    print(
        f"LINE單股查詢："
        f"{stock_info['code']} "
        f"{stock_info['name']}"
    )

    print(
        "================================"
    )

    current_pe_data = (
        get_twse_pe_data()
    )

    market_pe = (
        calculate_taiex_market_pe()
    )

    pe_history = load_json(
        PE_HISTORY_FILE
    )

    code = stock_info[
        "code"
    ]

    pe_history = update_pe_history(
        [code],
        pe_history,
    )

    save_json(
        PE_HISTORY_FILE,
        pe_history,
    )

    margin_history = load_json(
        CHIP_HISTORY_FILE
    )

    margin_history = (
        update_margin_history(
            [code],
            margin_history,
        )
    )

    save_json(
        CHIP_HISTORY_FILE,
        margin_history,
    )

    print(
        "取得法人最近20交易日..."
    )

    t86_history = (
        get_recent_t86_history(
            20
        )
    )

    try:

        result = analyze_stock(
            stock_info,
            current_pe_data,
            market_pe,
            pe_history,
            margin_history,
            t86_history,
            universe,
        )

        return result[
            "message"
        ]

    except Exception as e:

        print(
            f"LINE單股分析失敗："
            f"{e}"
        )

        return (
            f"❌ {stock_info['name']} "
            "目前分析失敗。\n\n"
            f"錯誤：{e}"
        )


# ============================================================
# LINE Webhook
#
# 注意：
# GitHub Actions 不會持續監聽 LINE Webhook。
#
# 這個函式是給 Flask / Render / Railway /
# Cloud Run 等長時間運行服務使用。
# ============================================================

def handle_line_webhook_event(
    event,
    universe,
):

    if (
        event.get("type")
        != "message"
    ):
        return

    message = event.get(
        "message",
        {}
    )

    if (
        message.get("type")
        != "text"
    ):
        return

    text = (
        message.get(
            "text",
            ""
        )
        .strip()
    )

    reply_token = event.get(
        "replyToken"
    )

    if not text:
        return

    # --------------------------------------------------------
    # 指令
    # --------------------------------------------------------

    if text in [
        "說明",
        "help",
        "HELP",
    ]:

        reply_line(
            reply_token,
            (
                "📈 股票加碼分析 Bot\n\n"
                "直接輸入股票代號或名稱即可。\n\n"
                "例如：\n"
                "2330\n"
                "台積電\n"
                "5347\n"
                "世界\n\n"
                "Bot會自動：\n"
                "① 找股票\n"
                "② 判斷產業\n"
                "③ 找目前市值前十大同業\n"
                "④ 套用產業估值模型\n"
                "⑤ 分析技術面\n"
                "⑥ 分析法人與融資\n"
                "⑦ 回答目前是否適合加碼"
            ),
        )

        return

    result = line_single_stock_analysis(
        text,
        universe,
    )

    reply_line(
        reply_token,
        result,
    )


# ============================================================
# Flask Webhook Server
#
# 如果環境有 Flask：
#
# python stock_alert.py webhook
#
# 就會啟動 LINE Webhook。
# ============================================================

def run_webhook_server():

    try:

        from flask import (
            Flask,
            request,
        )

    except ImportError:

        print(
            "❌ 尚未安裝 Flask"
        )

        print(
            "請安裝："
            "pip install flask"
        )

        return

    app = Flask(
        __name__
    )

    universe = (
        get_market_universe()
    )

    @app.route(
        "/callback",
        methods=["POST"],
    )
    def callback():

        body = request.get_json(
            silent=True
        )

        if not body:

            return (
                "OK",
                200,
            )

        events = body.get(
            "events",
            []
        )

        for event in events:

            try:

                handle_line_webhook_event(
                    event,
                    universe,
                )

            except Exception as e:

                print(
                    f"Webhook錯誤："
                    f"{e}"
                )

        return (
            "OK",
            200,
        )

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
        "LINE Webhook Server V2.5"
    )

    print(
        f"Port：{port}"
    )

    print(
        "================================"
    )

    app.run(
        host="0.0.0.0",
        port=port,
    )


# ============================================================
# 自動估值分析
# ============================================================

def run_daily_valuation(
    universe,
    current_pe_data,
    market_pe,
    pe_history,
    margin_history,
    t86_history,
    state,
):

    print(
        "\n========== "
        "每日自動估值分析 "
        "=========="
    )

    # --------------------------------------------------------
    # 自動分析目前監控的股票
    #
    # 不需要 VALUATION_STOCKS
    # --------------------------------------------------------

    target_codes = [
        "2330",
        "3711",
    ]

    for code in target_codes:

        stock_info = universe.get(
            code
        )

        if not stock_info:

            print(
                f"{code} "
                "市場資料不存在"
            )

            continue

        try:

            result = analyze_stock(
                stock_info,
                current_pe_data,
                market_pe,
                pe_history,
                margin_history,
                t86_history,
                universe,
            )

            # ------------------------------------------------
            # 只有真正達到加碼等級才廣播
            # ------------------------------------------------

            level = result.get(
                "level"
            )

            if level in [
                "🟢 強烈建議加碼",
                "🟡 建議分批加碼",
            ]:

                state.setdefault(
                    "valuation_v25",
                    {}
                )

                already_sent = (
                    state[
                        "valuation_v25"
                    ].get(
                        code,
                        False,
                    )
                )

                if not already_sent:

                    send_line(
                        result[
                            "message"
                        ]
                    )

                    state[
                        "valuation_v25"
                    ][code] = True

                    print(
                        f"{code} "
                        "已發送V2.5估值通知"
                    )

                else:

                    print(
                        f"{code} "
                        "估值通知已發送，略過"
                    )

            else:

                state.setdefault(
                    "valuation_v25",
                    {}
                )

                # 條件失效後解除鎖定
                state[
                    "valuation_v25"
                ][code] = False

        except Exception as e:

            print(
                f"{code} "
                f"V2.5估值錯誤："
                f"{e}"
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
        "V2.5自動估值 + "
        "技術 + 籌碼"
    )

    print(
        "================================"
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

    # ========================================================
    # 市場股票池
    # ========================================================

    universe = (
        get_market_universe()
    )

    if not universe:

        print(
            "❌ 無法建立市場股票池"
        )

        return

    # ========================================================
    # 當日 PE
    # ========================================================

    current_pe_data = (
        get_twse_pe_data()
    )

    if current_pe_data:

        print(
            f"取得 "
            f"{len(current_pe_data)} "
            "筆上市PE資料"
        )

    else:

        print(
            "⚠️ TWSE PE資料取得失敗"
        )

    # ========================================================
    # TAIEX PE
    # ========================================================

    market_pe = (
        calculate_taiex_market_pe()
    )

    # ========================================================
    # PE歷史
    #
    # 自動監控標的
    # ========================================================

    target_codes = []

    for code in [
        "2330",
        "3711",
    ]:

        if code in universe:

            target_codes.append(
                code
            )

    if target_codes:

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

    # ========================================================
    # 融資融券
    # ========================================================

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

    # ========================================================
    # 三大法人
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

            check_stock(
                name,
                symbol,
                state,
            )

        except Exception as e:

            print(
                f"{name} 跌幅檢查錯誤："
                f"{e}"
            )

        try:

            check_interval_low(
                name,
                symbol,
                state,
            )

        except Exception as e:

            print(
                f"{name} "
                f"15分鐘檢查錯誤："
                f"{e}"
            )

    # ========================================================
    # 自動估值
    # ========================================================

    if current_pe_data:

        run_daily_valuation(
            universe,
            current_pe_data,
            market_pe,
            pe_history,
            margin_history,
            t86_history,
            state,
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
# ENTRY
# ============================================================

if __name__ == "__main__":

    import sys

    if (
        len(sys.argv) > 1
        and sys.argv[1]
        == "webhook"
    ):

        run_webhook_server()

    else:

        main()
