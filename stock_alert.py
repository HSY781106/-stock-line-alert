# ============================================================
# stock_alert.py V2.4
#
# 股票跌幅 + 15分鐘區間最低價
# + 自動股票搜尋
# + 自動產業判斷
# + 動態產業市值前十大
# + PE歷史60筆回補
# + 技術面
# + 三大法人
# + 融資融券
# + LINE通知
#
# V2.4 修正：
#
# 1. PE歷史不足60筆：
#    - 不使用今天PE硬補歷史
#    - 只接受指定日期真正取得的PE
#    - 往過去交易日回補
#    - 未滿60筆不啟用一年平均PE評分
#    - 滿60筆後正式啟用
#
# 2. 產業同業：
#    - 不再使用固定INDUSTRY_POOL
#    - 自動取得上市/上櫃股票
#    - 依產業分類
#    - 依目前市值排序
#    - 自動取產業市值前10大
#
# 3. LINE單股分析：
#    - 輸入股票代號或股票名稱
#    - 自動搜尋
#    - 自動判斷產業
#    - 自動套用產業模型
#    - 自動尋找市值前十大同業
#
# 4. 15分鐘區間：
#    - 保存上一次偵測時間
#    - 計算兩次偵測間的最低價
#    - 區間最低價跌破門檻才通知
#
# 5. 完整防呆：
#    - Yahoo資料不存在不讓程式中斷
#    - 單一股票失敗不影響其他股票
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

LINE_TOKEN = os.environ.get(
    "LINE_CHANNEL_ACCESS_TOKEN",
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

PE_MIN_HISTORY = 60

TWSE_TIMEOUT = 20
T86_RETRIES = 2
API_SLEEP = 0.20

# 15分鐘區間
INTRADAY_INTERVAL = "1m"

# Yahoo 1m資料通常只能取得近期資料
INTRADAY_LOOKBACK_DAYS = 7

# 同業取目前市值前10
TOP_INDUSTRY_COMPANIES = 10

# 市值快取時間
MARKET_CAP_CACHE_DAYS = 1


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
# 固定需要每日自動估值的股票
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
# 產業估值模型
#
# 注意：
# 這裡只定義「不同產業用哪些指標」。
#
# 同業股票不再固定寫在這裡。
# 同業會由程式自動從市場股票清單中找出，
# 再依市值排序取前10名。
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

    # 預設產業
    "其他": {
        "pe": True,
        "peg": False,
        "pb": True,
        "yield": False,
        "dcf": False,
        "roe": False,
    },
}


# ============================================================
# API
# ============================================================

def twse_get(endpoint, timeout=TWSE_TIMEOUT):
    try:
        response = requests.get(
            TWSE_BASE + endpoint,
            timeout=timeout,
            headers={"User-Agent": "Mozilla/5.0"},
        )

        response.raise_for_status()

        return response.json()

    except Exception as e:
        print(f"TWSE API失敗：{endpoint} / {e}")
        return None


def twse_web_get(
    endpoint,
    params=None,
    timeout=TWSE_TIMEOUT,
):
    try:
        response = requests.get(
            TWSE_WEB_BASE + endpoint,
            params=params,
            timeout=timeout,
            headers={"User-Agent": "Mozilla/5.0"},
        )

        response.raise_for_status()

        return response.json()

    except Exception as e:
        print(
            f"TWSE Web API失敗："
            f"{endpoint} / {e}"
        )

        return None


def tpex_get(endpoint, timeout=TWSE_TIMEOUT):
    try:
        response = requests.get(
            TPEX_BASE + endpoint,
            timeout=timeout,
            headers={"User-Agent": "Mozilla/5.0"},
        )

        response.raise_for_status()

        return response.json()

    except Exception as e:
        print(
            f"TPEX API失敗："
            f"{endpoint} / {e}"
        )

        return None


# ============================================================
# LINE
# ============================================================

def send_line(message):
    if not LINE_TOKEN:
        print("⚠️ LINE_TOKEN不存在，跳過LINE通知")
        return False

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
                        "text": message,
                    }
                ]
            },
            timeout=20,
        )

        if response.status_code != 200:
            print(
                "LINE API error："
                f"{response.status_code} "
                f"{response.text}"
            )

            return False

        return True

    except Exception as e:
        print(f"LINE通知失敗：{e}")
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

        return data if isinstance(data, dict) else {}

    except Exception as e:
        print(
            f"{filename} 讀取失敗：{e}"
        )

        return {}


def save_json(filename, data):
    try:
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

    except Exception as e:
        print(
            f"{filename} 儲存失敗：{e}"
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

        if name not in row:
            continue

        value = to_float(row[name])

        if value is not None:
            return value

    return None


def format_number(value, digits=2):
    if value is None:
        return "N/A"

    return f"{value:,.{digits}f}"


# ============================================================
# 日期
# ============================================================

def get_today():
    return datetime.now(TW_TZ).date()


def date_to_string(date_obj):
    return date_obj.strftime("%Y%m%d")


def is_weekday(date_obj):
    return date_obj.weekday() < 5


# ============================================================
# TWSE / TPEX 股票清單
# ============================================================

def get_twse_stock_list():
    """
    取得上市股票基本資料。
    """

    result = []

    data = twse_get(
        "/exchangeReport/STOCK_DAY_ALL"
    )

    if isinstance(data, list):

        for row in data:

            if not isinstance(row, dict):
                continue

            code = str(
                row.get("Code", "")
            ).strip()

            name = str(
                row.get("Name", "")
            ).strip()

            if (
                not code
                or not name
                or not code.isdigit()
            ):
                continue

            result.append({
                "code": code,
                "name": name,
                "market": "TWSE",
                "symbol": f"{code}.TW",
            })

    return result


def get_tpex_stock_list():
    """
    取得上櫃股票基本資料。
    """

    result = []

    data = tpex_get(
        "/tpex_mainboard_peratio_analysis"
    )

    if isinstance(data, list):

        for row in data:

            if not isinstance(row, dict):
                continue

            code = str(
                row.get(
                    "SecuritiesCompanyCode",
                    row.get(
                        "Code",
                        ""
                    ),
                )
            ).strip()

            name = str(
                row.get(
                    "CompanyName",
                    row.get(
                        "Name",
                        ""
                    ),
                )
            ).strip()

            if (
                not code
                or not name
                or not code.isdigit()
            ):
                continue

            result.append({
                "code": code,
                "name": name,
                "market": "TPEX",
                "symbol": f"{code}.TWO",
            })

    return result


def get_all_market_stocks():
    """
    上市 + 上櫃股票。

    如果某一邊API失敗，
    仍然保留另一邊資料。
    """

    stocks = []

    twse = get_twse_stock_list()
    tpex = get_tpex_stock_list()

    stocks.extend(twse)
    stocks.extend(tpex)

    unique = {}

    for item in stocks:
        code = item["code"]

        if code not in unique:
            unique[code] = item

    result = list(unique.values())

    print(
        f"市場股票清單："
        f"{len(result)} 檔"
    )

    return result


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
            f"取得TWSE PE/PB失敗：{e}"
        )

        return {}


# ============================================================
# 指定日期 PE
# ============================================================

def get_twse_pe_by_date(date_string):
    """
    只取得指定日期真正存在的PE。

    絕對不使用目前PE代替歷史PE。
    """

    try:

        data = twse_get(
            "/exchangeReport/BWIBBU_d"
            f"?date={date_string}"
        )

        result = {}

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

            if not code:
                continue

            pe = find_value(
                row,
                [
                    "PEratio",
                    "PER",
                    "本益比",
                ],
            )

            if (
                pe is not None
                and 0 < pe <= 200
            ):
                result[code] = pe

        return result

    except Exception as e:
        print(
            f"{date_string} PE取得失敗：{e}"
        )

        return {}


# ============================================================
# TAIEX市場PE
# ============================================================

def calculate_taiex_market_pe(
    current_pe_data=None
):
    print(
        "計算 TAIEX 官方口徑市場 PE..."
    )

    try:

        if not current_pe_data:
            current_pe_data = (
                get_twse_pe_data()
            )

        values = []

        for row in current_pe_data.values():

            pe = row.get("pe")

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
            f"TAIEX市場PE失敗：{e}"
        )

        return None


# ============================================================
# PE歷史
# ============================================================

def get_existing_pe_count(
    code,
    history,
):
    stock_history = history.get(
        code,
        {},
    )

    count = 0

    for date_string, pe in stock_history.items():

        if not isinstance(
            date_string,
            str,
        ):
            continue

        if not date_string.isdigit():
            continue

        if (
            pe is not None
            and 0 < pe <= 200
        ):
            count += 1

    return count


def update_pe_history(
    target_codes,
    history,
):
    """
    V2.4 PE核心。

    例如：

    現有2筆
        ↓
    往過去交易日查
        ↓
    取得指定日期PE
        ↓
    寫入真正歷史值
        ↓
    到60筆停止

    不會把今天PE複製成60天。
    """

    today = get_today()

    for code in target_codes:

        current_count = (
            get_existing_pe_count(
                code,
                history,
            )
        )

        print(
            f"{code} 目前PE歷史："
            f"{current_count}筆"
        )

        if current_count >= PE_MIN_HISTORY:
            print(
                f"{code} PE歷史已達"
                f"{PE_MIN_HISTORY}筆"
            )

            continue

        print(
            f"{code} PE不足"
            f"{PE_MIN_HISTORY}筆，"
            "開始往過去日期回補..."
        )

        current_date = today - timedelta(
            days=1
        )

        attempts = 0

        while (
            current_count
            < PE_MIN_HISTORY
            and attempts < 450
        ):

            if not is_weekday(
                current_date
            ):
                current_date -= timedelta(
                    days=1
                )

                attempts += 1
                continue

            date_string = date_to_string(
                current_date
            )

            # 已經有資料就跳過
            if date_string in history.get(
                code,
                {},
            ):
                current_date -= timedelta(
                    days=1
                )

                attempts += 1
                continue

            pe_data = (
                get_twse_pe_by_date(
                    date_string
                )
            )

            pe = pe_data.get(code)

            if (
                pe is not None
                and 0 < pe <= 200
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

            else:

                print(
                    f"{code} "
                    f"{date_string} "
                    "沒有有效PE，跳過"
                )

            current_date -= timedelta(
                days=1
            )

            attempts += 1

            time.sleep(API_SLEEP)

        print(
            f"{code} PE回補完成："
            f"{current_count}筆"
        )

    return history


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
        get_today()
        - timedelta(days=365)
    )

    values = []

    for date_string, pe in stock_history.items():

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
# 股票名稱搜尋
# ============================================================

def normalize_stock_query(text):
    if text is None:
        return ""

    return (
        str(text)
        .strip()
        .replace(
            "　",
            " ",
        )
    )


def find_stock(
    query,
    stock_list,
):
    """
    支援：

    2330
    台積電
    台積
    5347
    世界
    """

    query = normalize_stock_query(
        query
    )

    if not query:
        return None

    # 完整代號
    for stock in stock_list:

        if stock["code"] == query:
            return stock

    # 完整名稱
    for stock in stock_list:

        if stock["name"] == query:
            return stock

    # 名稱包含
    matches = []

    for stock in stock_list:

        if query in stock["name"]:
            matches.append(stock)

    if len(matches) == 1:
        return matches[0]

    # 代號包含
    matches = []

    for stock in stock_list:

        if query in stock["code"]:
            matches.append(stock)

    if len(matches) == 1:
        return matches[0]

    return None


# ============================================================
# 股票產業
# ============================================================

def get_stock_industry(
    code,
    stock_list=None,
):
    """
    優先使用TWSE/TPEX分類。

    若API資料無法取得，
    再用Yahoo industry作為備援。
    """

    # --------------------------------------------------------
    # TWSE產業
    # --------------------------------------------------------

    try:

        data = twse_get(
            "/exchangeReport/STOCK_DAY_ALL"
        )

        if isinstance(data, list):

            for row in data:

                if str(
                    row.get(
                        "Code",
                        "",
                    )
                ).strip() != code:
                    continue

                industry = (
                    row.get(
                        "Industry",
                        row.get(
                            "產業別",
                            "",
                        ),
                    )
                )

                if industry:
                    return normalize_industry(
                        industry
                    )

    except Exception:
        pass

    # --------------------------------------------------------
    # Yahoo備援
    # --------------------------------------------------------

    try:

        ticker = yf.Ticker(
            f"{code}.TW"
        )

        info = ticker.info

        industry = info.get(
            "industry"
        )

        sector = info.get(
            "sector"
        )

        if industry:
            return normalize_industry(
                industry
            )

        if sector:
            return normalize_industry(
                sector
            )

    except Exception as e:

        print(
            f"{code} Yahoo產業取得失敗："
            f"{e}"
        )

    return "其他"


def normalize_industry(
    industry
):
    """
    把市場實際產業分類映射到
    我們的估值模型分類。
    """

    if not industry:
        return "其他"

    text = str(
        industry
    ).strip()

    # 晶圓代工
    if any(
        keyword in text
        for keyword in [
            "晶圓代工",
            "半導體業",
            "半導體",
        ]
    ):

        # 如果明確是封裝測試，
        # 後面會優先處理
        if any(
            keyword in text
            for keyword in [
                "封裝",
                "測試",
                "IC封測",
            ]
        ):
            return "封裝測試"

        return "晶圓代工"

    # 封裝測試
    if any(
        keyword in text
        for keyword in [
            "封裝",
            "測試",
            "IC封測",
        ]
    ):
        return "封裝測試"

    # IC設計
    if any(
        keyword in text
        for keyword in [
            "IC設計",
            "設計",
            "半導體設計",
        ]
    ):
        return "IC設計"

    # 金融
    if any(
        keyword in text
        for keyword in [
            "金融",
            "銀行",
            "保險",
            "證券",
        ]
    ):
        return "金融"

    # 電信
    if any(
        keyword in text
        for keyword in [
            "通信",
            "電信",
            "通訊",
        ]
    ):
        return "電信"

    # 傳產
    if any(
        keyword in text
        for keyword in [
            "水泥",
            "食品",
            "塑膠",
            "鋼鐵",
            "汽車",
            "航運",
            "電機",
            "化學",
            "建材",
        ]
    ):
        return "成熟傳產"

    return "其他"


# ============================================================
# 市值
# ============================================================

def get_market_cap(
    code,
    symbol=None,
):
    """
    取得目前市值。

    Yahoo失敗就回傳None。
    不讓單一股票阻斷整個程式。
    """

    if symbol is None:
        symbol = f"{code}.TW"

    try:

        ticker = yf.Ticker(
            symbol
        )

        fast_info = ticker.fast_info

        market_cap = getattr(
            fast_info,
            "market_cap",
            None,
        )

        if market_cap is not None:
            return float(
                market_cap
            )

    except Exception as e:

        print(
            f"{code} 市值取得失敗："
            f"{e}"
        )

    # Yahoo fast_info失敗，
    # 再嘗試info
    try:

        ticker = yf.Ticker(
            symbol
        )

        info = ticker.info

        market_cap = info.get(
            "marketCap"
        )

        if market_cap is not None:
            return float(
                market_cap
            )

    except Exception:
        pass

    return None


# ============================================================
# 動態產業市值前十大
# ============================================================

def get_top_industry_companies(
    industry,
    exclude_code,
    stock_list,
    current_pe_data,
):
    """
    V2.4核心：

    不再：

    INDUSTRY_POOL = {
        "晶圓代工": [
            "2330",
            "2303",
            ...
        ]
    }

    而是：

    1. 全市場股票
    2. 找相同產業
    3. 取得目前市值
    4. 排序
    5. 取前10

    最後排除自己。
    """

    candidates = []

    for stock in stock_list:

        code = stock["code"]

        if code == exclude_code:
            continue

        stock_industry = get_cached_industry(
            code,
            stock,
        )

        if stock_industry != industry:
            continue

        # 必須有PE才有資格參與同業PE
        pe_item = current_pe_data.get(
            code
        )

        if not pe_item:
            continue

        pe = pe_item.get(
            "pe"
        )

        if (
            pe is None
            or pe <= 0
            or pe > 200
        ):
            continue

        market_cap = get_market_cap(
            code,
            stock.get(
                "symbol"
            ),
        )

        if market_cap is None:
            continue

        candidates.append({
            "code": code,
            "name": stock["name"],
            "industry": industry,
            "market_cap": market_cap,
            "pe": pe,
        })

        time.sleep(
            API_SLEEP
        )

    candidates.sort(
        key=lambda x:
        x["market_cap"],
        reverse=True,
    )

    result = candidates[
        :TOP_INDUSTRY_COMPANIES
    ]

    print(
        f"{industry} "
        "目前市值前十大同業："
    )

    for index, item in enumerate(
        result,
        start=1,
    ):
        print(
            f"{index}. "
            f"{item['code']} "
            f"{item['name']} "
            f"市值="
            f"{item['market_cap']:,.0f} "
            f"PE="
            f"{item['pe']:.2f}"
        )

    return result


# ============================================================
# 產業快取
# ============================================================

INDUSTRY_CACHE = {}


def get_cached_industry(
    code,
    stock,
):
    if code in INDUSTRY_CACHE:
        return INDUSTRY_CACHE[code]

    industry = get_stock_industry(
        code
    )

    INDUSTRY_CACHE[
        code
    ] = industry

    return industry


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
            f"{symbol} KD失敗："
            f"{e}"
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
            f"{symbol} RSI失敗："
            f"{e}"
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
            f"{symbol} 基本面失敗："
            f"{e}"
        )

    return result


def calculate_peg(
    pe,
    growth,
):
    if pe is None:
        return None

    if growth is None:
        return None

    if pe <= 0:
        return None

    if growth <= 0:
        return None

    return pe / growth


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
                    f"T86 "
                    f"{date_string} "
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

                if (
                    len(raw_row)
                    != len(fields)
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
                f"T86 "
                f"{date_string} "
                f"取得失敗：{e}"
            )

            if attempt < T86_RETRIES:
                time.sleep(1)

        except Exception as e:

            print(
                f"T86 "
                f"{date_string} "
                f"解析失敗：{e}"
            )

            return {}

    return {}


def get_recent_t86_history(
    count=20
):
    result = []

    current = get_today()

    for i in range(45):

        day = (
            current
            - timedelta(days=i)
        )

        if not is_weekday(day):
            continue

        date_string = date_to_string(
            day
        )

        data = get_t86_data(
            date_string
        )

        if data:

            result.append({
                "date":
                    date_string,
                "data":
                    data,
            })

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

        stock = item.get(
            "data",
            {},
        ).get(
            code
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
    today_string = (
        datetime.now(
            TW_TZ
        ).strftime("%Y%m%d")
    )

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

        item = data.get(
            code
        )

        if item:

            history.setdefault(
                code,
                {},
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
        {},
    )

    dates = []

    for key in stock:

        try:
            datetime.strptime(
                key,
                "%Y%m%d",
            )

            dates.append(
                key
            )

        except Exception:
            pass

    dates.sort(
        reverse=True
    )

    if len(dates) < 2:
        return None

    latest = stock[
        dates[0]
    ].get(
        "margin"
    )

    previous_index = min(
        5,
        len(dates) - 1,
    )

    previous = stock[
        dates[previous_index]
    ].get(
        "margin"
    )

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

            dates.append(
                key
            )

        except Exception:
            pass

    if not dates:
        return None

    latest = max(
        dates
    )

    return stock.get(
        latest
    )


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
        short / margin * 100
    )


# ============================================================
# 評分
# ============================================================

def analyze_stock(
    code,
    stock_info,
    current_pe_data,
    market_pe,
    pe_history,
    margin_history,
    t86_history,
    stock_list,
):
    """
    回傳完整分析結果。

    不直接發LINE。
    方便：

    1. 每日自動分析
    2. LINE好友查詢

    共用同一套分析邏輯。
    """

    name = stock_info["name"]
    symbol = stock_info["symbol"]

    industry = stock_info.get(
        "industry"
    )

    if not industry:
        industry = get_stock_industry(
            code,
            stock_list,
        )

    model = INDUSTRY_MODEL.get(
        industry,
        INDUSTRY_MODEL["其他"],
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
        f"市場："
        f"{stock_info.get('market', 'N/A')}"
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
        return None

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

    # --------------------------------------------------------
    # 動態同業
    # --------------------------------------------------------

    peers = (
        get_top_industry_companies(
            industry,
            code,
            stock_list,
            current_pe_data,
        )
    )

    peer_values = [
        x["pe"]
        for x in peers
        if (
            x.get("pe")
            is not None
        )
    ]

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

    # PE
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
            "PE低於同業",
        )

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
            "PE低於60筆以上歷史平均",
        )

    # PEG
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

    # PB
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

    # 殖利率
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

    # ROE
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

    # KD
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

    # RSI
    add_score(
        rsi is not None,
        (
            rsi < 35
            if rsi is not None
            else False
        ),
        "RSI < 35",
    )

    # 法人
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

    # 融資
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
        level = "🔴 目前不建議加碼"

    result = {
        "code": code,
        "name": name,
        "symbol": symbol,
        "industry": industry,
        "model": model,

        "stock_pe": stock_pe,
        "market_pe": market_pe,
        "industry_pe": industry_pe,

        "one_year_pe":
            one_year_pe,

        "sample_count":
            sample_count,

        "stock_pb":
            stock_pb,

        "stock_yield":
            stock_yield,

        "earnings_growth":
            earnings_growth,

        "peg":
            peg,

        "roe":
            roe,

        "k":
            k,

        "d":
            d,

        "rsi":
            rsi,

        "inst_5d":
            inst_5d,

        "inst_20d":
            inst_20d,

        "margin_change":
            margin_change,

        "short_margin_ratio":
            short_margin_ratio,

        "score":
            score,

        "possible_score":
            possible_score,

        "ratio":
            ratio,

        "level":
            level,

        "strong":
            strong,

        "good":
            good,

        "reasons_good":
            reasons_good,

        "warnings":
            warnings,

        "pe_history_active":
            historical_active,

        "pe_history":
            sample_count,

        "pe_history_required":
            PE_MIN_HISTORY,

        "peers":
            peers,
    }

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

    return result


# ============================================================
# LINE單股分析訊息
# ============================================================

def format_stock_analysis_message(
    result
):
    if not result:
        return None

    name = result["name"]
    code = result["code"]
    industry = result[
        "industry"
    ]

    score = result[
        "score"
    ]

    possible = result[
        "possible_score"
    ]

    ratio = result[
        "ratio"
    ]

    level = result[
        "level"
    ]

    peers = result.get(
        "peers",
        [],
    )

    peer_text = "無"

    if peers:

        peer_text = "、".join(
            [
                (
                    f"{x['code']} "
                    f"{x['name']}"
                )
                for x in peers
            ]
        )

    reasons = (
        "、".join(
            result[
                "reasons_good"
            ]
        )
        if result[
            "reasons_good"
        ]
        else "無"
    )

    warnings = (
        "、".join(
            result[
                "warnings"
            ]
        )
        if result[
            "warnings"
        ]
        else "無"
    )

    if (
        result[
            "pe_history_active"
        ]
    ):

        history_text = (
            f"{result['pe_history']}筆"
        )

        average_text = (
            f"{format_number(result['one_year_pe'])}"
        )

    else:

        history_text = (
            f"{result['pe_history']}/"
            f"{result['pe_history_required']}筆"
        )

        average_text = (
            "暫不納入評分"
        )

    message = (
        f"{level}\n\n"

        f"【股票】\n"
        f"{code} {name}\n"
        f"產業：{industry}\n\n"

        f"【估值】\n"
        f"PE："
        f"{format_number(result['stock_pe'])} 倍\n"
        f"TAIEX PE："
        f"{format_number(result['market_pe'])} 倍\n"
        f"同業平均 PE："
        f"{format_number(result['industry_pe'])} 倍\n"
        f"1年平均 PE："
        f"{average_text}\n"
        f"PE歷史資料："
        f"{history_text}\n"
        f"PB："
        f"{format_number(result['stock_pb'])} 倍\n"
        f"殖利率："
        f"{format_number(result['stock_yield'])}%\n"
        f"EPS成長："
        f"{format_number(result['earnings_growth'])}%\n"
        f"PEG："
        f"{format_number(result['peg'])}\n"
        f"ROE："
        f"{format_number(result['roe'])}%\n\n"

        f"【動態同業】\n"
        f"{peer_text}\n\n"

        f"【技術】\n"
        f"KD："
        f"K {format_number(result['k'])} "
        f"/ D {format_number(result['d'])}\n"
        f"RSI："
        f"{format_number(result['rsi'])}\n\n"

        f"【籌碼】\n"
        f"法人5日："
        f"{format_number(result['inst_5d'], 0)} 股\n"
        f"法人20日："
        f"{format_number(result['inst_20d'], 0)} 股\n"
        f"融資5日變化："
        f"{format_number(result['margin_change'], 0)} 張\n"
        f"券資比："
        f"{format_number(result['short_margin_ratio'])}%\n\n"

        f"━━━━━━━━━━\n"
        f"加碼評分："
        f"{score}/{possible}"
        f" ({ratio:.0%})\n"
        f"{level}\n"
        f"━━━━━━━━━━\n\n"

        f"加分項目：\n"
        f"{reasons}\n\n"

        f"風險提示：\n"
        f"{warnings}"
    )

    return message


# ============================================================
# 價格歷史
# ============================================================

def get_history(symbol):
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

    if data is None or data.empty:
        return None

    try:

        close = data["Close"]

        if hasattr(
            close,
            "columns",
        ):
            close = close.iloc[:, 0]

    except Exception:

        close = data.iloc[:, 0]

    return close.dropna()


def get_latest_price(symbol):
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

        if (
            intraday is not None
            and not intraday.empty
        ):

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
            f"{symbol} 1m資料失敗："
            f"{e}"
        )

    try:

        daily = ticker.history(
            period="5d",
            interval="1d",
            auto_adjust=False,
        )

        if (
            daily is not None
            and not daily.empty
        ):

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
            f"{symbol} 日線資料失敗："
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


def get_week_high(symbol):
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

        if (
            data is None
            or data.empty
        ):
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
# 15分鐘區間資料
# ============================================================

def get_intraday_low_between(
    symbol,
    start_time,
    end_time,
):
    """
    取得：

    上一次偵測時間
          ↓
    本次偵測時間

    期間最低價。

    使用1分鐘資料，
    因此不是只拿「當下價格」。
    """

    if (
        start_time is None
        or end_time is None
    ):
        return None, None

    try:

        ticker = yf.Ticker(
            symbol
        )

        data = ticker.history(
            start=(
                start_time
                - timedelta(
                    minutes=2
                )
            ).strftime(
                "%Y-%m-%d %H:%M:%S"
            ),
            end=(
                end_time
                + timedelta(
                    minutes=1
                )
            ).strftime(
                "%Y-%m-%d %H:%M:%S"
            ),
            interval=INTRADAY_INTERVAL,
            prepost=False,
            auto_adjust=False,
        )

        if (
            data is None
            or data.empty
        ):
            return None, None

        lows = (
            data["Low"]
            .dropna()
        )

        if len(lows) == 0:
            return None, None

        minimum = float(
            lows.min()
        )

        minimum_time = (
            lows.idxmin()
        )

        return (
            minimum,
            minimum_time,
        )

    except Exception as e:

        print(
            f"{symbol} "
            "15分鐘區間資料失敗："
            f"{e}"
        )

        return None, None


def check_intraday_interval(
    name,
    symbol,
    state,
):
    """
    V2.4：

    第一次執行：
        建立基準，不通知

    第二次以後：
        計算上次偵測→這次偵測
        的最低價

    若區間最低價跌破門檻，
    就發送通知。
    """

    print(
        f"\n---------- "
        f"15分鐘區間：{name}"
        f" ----------"
    )

    now = datetime.now(
        TW_TZ
    )

    state.setdefault(
        "intraday_15m",
        {},
    )

    stock_state = state[
        "intraday_15m"
    ].setdefault(
        symbol,
        {},
    )

    previous_string = (
        stock_state.get(
            "last_check"
        )
    )

    # --------------------------------------------------------
    # 第一次執行
    # --------------------------------------------------------

    if not previous_string:

        stock_state[
            "last_check"
        ] = now.isoformat()

        print(
            "第一次執行，"
            "建立區間基準"
        )

        return

    # --------------------------------------------------------
    # 解析上一時間
    # --------------------------------------------------------

    try:

        previous_time = (
            datetime.fromisoformat(
                previous_string
            )
        )

        if (
            previous_time.tzinfo
            is None
        ):
            previous_time = (
                previous_time.replace(
                    tzinfo=TW_TZ
                )
            )

    except Exception:

        stock_state[
            "last_check"
        ] = now.isoformat()

        print(
            "上次時間格式錯誤，"
            "重新建立基準"
        )

        return

    # --------------------------------------------------------
    # 防止時間異常
    # --------------------------------------------------------

    if now <= previous_time:

        stock_state[
            "last_check"
        ] = now.isoformat()

        return

    # 如果兩次執行距離太久，
    # 超過7天就不要查
    if (
        now - previous_time
    ).days > INTRADAY_LOOKBACK_DAYS:

        stock_state[
            "last_check"
        ] = now.isoformat()

        print(
            "距離上次執行超過"
            f"{INTRADAY_LOOKBACK_DAYS}天，"
            "重新建立基準"
        )

        return

    minimum, minimum_time = (
        get_intraday_low_between(
            symbol,
            previous_time,
            now,
        )
    )

    current = get_latest_price(
        symbol
    )

    if minimum is None:

        print(
            "無法取得15分鐘區間資料"
        )

        # 注意：
        # 即使查不到資料，
        # 也更新時間，避免下次查超大區間
        stock_state[
            "last_check"
        ] = now.isoformat()

        return

    print(
        f"上次偵測："
        f"{previous_time}"
    )

    print(
        f"本次偵測："
        f"{now}"
    )

    print(
        f"區間最低價："
        f"{minimum}"
    )

    if minimum_time is not None:

        try:

            if hasattr(
                minimum_time,
                "to_pydatetime",
            ):
                minimum_time = (
                    minimum_time
                    .to_pydatetime()
                )

            if minimum_time.tzinfo is None:
                minimum_time = (
                    minimum_time.replace(
                        tzinfo=TW_TZ
                    )
                )

            print(
                f"最低價時間："
                f"{minimum_time}"
            )

        except Exception:
            pass

    # --------------------------------------------------------
    # 目前價格
    # --------------------------------------------------------

    if current is None:

        print(
            "無法取得目前價格"
        )

        stock_state[
            "last_check"
        ] = now.isoformat()

        return

    # --------------------------------------------------------
    # 跌幅判斷
    #
    # 這裡的「區間最低價」
    # 與上次偵測時價格比較。
    # --------------------------------------------------------

    previous_price = (
        stock_state.get(
            "last_price"
        )
    )

    if previous_price is None:

        previous_price = current

    previous_price = to_float(
        previous_price
    )

    if previous_price is None:

        previous_price = current

    interval_change = (
        minimum
        / previous_price
        - 1
    )

    print(
        f"上次偵測價格："
        f"{previous_price}"
    )

    print(
        f"區間最低跌幅："
        f"{interval_change:.2%}"
    )

    # --------------------------------------------------------
    # 通知
    # --------------------------------------------------------

    alerted = stock_state.get(
        "alerted",
        False,
    )

    if (
        interval_change
        <= DAILY_THRESHOLD
    ):

        if not alerted:

            message = (
                "🔴 15分鐘區間跌幅通知\n\n"

                f"標的：{name}\n"
                f"目前價格："
                f"{current:,.2f}\n"

                f"上次偵測價格："
                f"{previous_price:,.2f}\n"

                f"區間最低價："
                f"{minimum:,.2f}\n"

                f"區間最低跌幅："
                f"{interval_change:.2%}\n\n"

                "⚠️ 上次偵測至本次偵測期間，"
                "最低價已達 -5%"
            )

            if send_line(message):

                stock_state[
                    "alerted"
                ] = True

                print(
                    "已發送："
                    "15分鐘區間 -5%"
                )

    else:

        stock_state[
            "alerted"
        ] = False

    # --------------------------------------------------------
    # 更新基準
    # --------------------------------------------------------

    stock_state[
        "last_check"
    ] = now.isoformat()

    stock_state[
        "last_price"
    ] = current


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
        f"目前價格："
        f"{current}"
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
        {},
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
    # 單日跌幅
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

                "⚠️ 已達到單日 -5%"
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
    # 一週跌幅
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

                "⚠️ 已達到一週 -10%"
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

    # --------------------------------------------------------
    # 15分鐘區間
    # --------------------------------------------------------

    check_intraday_interval(
        name,
        symbol,
        state,
    )


# ============================================================
# 每日自動估值
# ============================================================

def run_daily_valuation(
    current_pe_data,
    market_pe,
    pe_history,
    margin_history,
    t86_history,
    stock_list,
    state,
):
    print(
        "\n========== "
        "每日自動估值分析 "
        "=========="
    )

    for code, original_info in (
        VALUATION_STOCKS.items()
    ):

        try:

            # ------------------------------------------------
            # 重新從市場股票清單取得資料
            # ------------------------------------------------

            market_stock = None

            for stock in stock_list:

                if stock[
                    "code"
                ] == code:

                    market_stock = stock
                    break

            if market_stock:

                stock_info = {
                    **market_stock,
                    "industry":
                        original_info.get(
                            "industry"
                        ),
                }

                if not stock_info.get(
                    "industry"
                ):

                    stock_info[
                        "industry"
                    ] = get_stock_industry(
                        code,
                        stock_list,
                    )

            else:

                stock_info = original_info

            result = analyze_stock(
                code,
                stock_info,
                current_pe_data,
                market_pe,
                pe_history,
                margin_history,
                t86_history,
                stock_list,
            )

            if not result:
                continue

            # ------------------------------------------------
            # 每日自動估值：
            # 只有達到加碼門檻才LINE通知
            # ------------------------------------------------

            state.setdefault(
                "valuation_v24",
                {},
            )

            if (
                result["strong"]
                or result["good"]
            ):

                already = state[
                    "valuation_v24"
                ].get(
                    code,
                    False,
                )

                if not already:

                    message = (
                        format_stock_analysis_message(
                            result
                        )
                    )

                    if message:

                        if send_line(
                            message
                        ):

                            state[
                                "valuation_v24"
                            ][code] = True

                            print(
                                "已發送V2.4估值通知"
                            )

            else:

                state[
                    "valuation_v24"
                ][code] = False

        except Exception as e:

            print(
                f"{code} "
                f"V2.4分析錯誤："
                f"{e}"
            )


# ============================================================
# LINE Webhook
#
# 注意：
# GitHub Actions本身不能直接接LINE webhook。
#
# 這部分保留給你後續部署Webhook服務。
# 分析核心已經完成。
# ============================================================

def analyze_line_query(
    query,
    stock_list,
    current_pe_data,
    market_pe,
    pe_history,
    margin_history,
    t86_history,
):
    """
    給LINE webhook呼叫。

    輸入：
        2330
        台積電
        5347
        世界

    回傳：
        單股分析文字
    """

    stock = find_stock(
        query,
        stock_list,
    )

    if stock is None:

        return (
            "❌ 找不到股票\n\n"
            f"你輸入：{query}\n\n"
            "請輸入：\n"
            "• 股票代號，例如 2330\n"
            "• 股票名稱，例如 台積電"
        )

    code = stock["code"]

    industry = get_stock_industry(
        code,
        stock_list,
    )

    stock_info = {
        **stock,
        "industry":
            industry,
    }

    try:

        result = analyze_stock(
            code,
            stock_info,
            current_pe_data,
            market_pe,
            pe_history,
            margin_history,
            t86_history,
            stock_list,
        )

        if not result:

            return (
                f"❌ {code} "
                f"{stock['name']} "
                "目前資料不足，"
                "無法完成分析。"
            )

        return (
            format_stock_analysis_message(
                result
            )
        )

    except Exception as e:

        print(
            f"LINE單股分析錯誤："
            f"{e}"
        )

        return (
            "❌ 分析時發生錯誤\n\n"
            f"標的："
            f"{code} "
            f"{stock['name']}\n"
            f"錯誤：{e}"
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
        "V2.4自動估值 + "
        "技術 + 籌碼"
    )

    print(
        "================================"
    )

    # --------------------------------------------------------
    # State
    # --------------------------------------------------------

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
    # 股票清單
    # --------------------------------------------------------

    stock_list = (
        get_all_market_stocks()
    )

    # --------------------------------------------------------
    # 當日PE/PB/殖利率
    # --------------------------------------------------------

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
            "⚠️ "
            "TWSE基本面資料取得失敗"
        )

    # --------------------------------------------------------
    # 市場PE
    # --------------------------------------------------------

    market_pe = (
        calculate_taiex_market_pe(
            current_pe_data
        )
    )

    # --------------------------------------------------------
    # PE歷史回補
    # --------------------------------------------------------

    target_codes = list(
        VALUATION_STOCKS.keys()
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
    # 融資融券
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
    # 三大法人
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
        f"法人有效交易日："
        f"{len(t86_history)}"
    )

    # --------------------------------------------------------
    # 跌幅監控
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
    # 每日估值
    # --------------------------------------------------------

    if current_pe_data:

        run_daily_valuation(
            current_pe_data,
            market_pe,
            pe_history,
            margin_history,
            t86_history,
            stock_list,
            state,
        )

    # --------------------------------------------------------
    # 儲存
    # --------------------------------------------------------

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
    main()
