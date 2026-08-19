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

# 評分門檻
STRONG_SCORE = 8
GOOD_SCORE = 6


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
# 估值＋技術＋籌碼股票
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
# 產業分類
#
# 後續新增股票時，只要：
#
# 1. VALUATION_STOCKS 加股票
# 2. INDUSTRY_POOL 加股票
#
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
# 不同產業的估值模型
# ============================================================

INDUSTRY_MODEL = {

    "晶圓代工": {
        "pe": True,
        "peg": True,
        "pb": True,
        "yield": False,
    },

    "封裝測試": {
        "pe": True,
        "peg": True,
        "pb": True,
        "yield": False,
    },

    "IC設計": {
        "pe": True,
        "peg": True,
        "pb": False,
        "yield": False,
    },

    "金融": {
        "pe": False,
        "peg": False,
        "pb": True,
        "yield": True,
    },

    "電信": {
        "pe": True,
        "peg": False,
        "pb": False,
        "yield": True,
    },

    "成熟傳產": {
        "pe": True,
        "peg": False,
        "pb": True,
        "yield": True,
    },
}


# ============================================================
# TWSE GET
# ============================================================

def twse_get(
    endpoint,
    timeout=30
):

    url = TWSE_BASE + endpoint

    response = requests.get(
        url,
        timeout=timeout,
        headers={
            "User-Agent": "Mozilla/5.0"
        }
    )

    response.raise_for_status()

    return response.json()


# ============================================================
# TWSE Web API
# ============================================================

def twse_web_get(
    endpoint,
    params=None,
    timeout=30
):

    url = TWSE_WEB_BASE + endpoint

    response = requests.get(
        url,
        params=params,
        timeout=timeout,
        headers={
            "User-Agent": "Mozilla/5.0"
        }
    )

    response.raise_for_status()

    return response.json()


# ============================================================
# LINE
# ============================================================

def send_line(message):

    url = (
        "https://api.line.me/"
        "v2/bot/message/broadcast"
    )

    headers = {
        "Authorization":
            f"Bearer {LINE_TOKEN}",

        "Content-Type":
            "application/json",
    }

    payload = {
        "messages": [
            {
                "type": "text",
                "text": message
            }
        ]
    }

    response = requests.post(
        url,
        headers=headers,
        json=payload,
        timeout=20
    )

    if response.status_code != 200:

        raise Exception(
            f"LINE API error: "
            f"{response.status_code} "
            f"{response.text}"
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
            encoding="utf-8"
        ) as f:

            return json.load(f)

    except Exception:

        return {}


def save_json(
    filename,
    data
):

    with open(
        filename,
        "w",
        encoding="utf-8"
    ) as f:

        json.dump(
            data,
            f,
            ensure_ascii=False,
            indent=2
        )


# ============================================================
# 數字
# ============================================================

def to_float(value):

    if value is None:
        return None

    try:

        text = str(
            value
        ).strip()

        if text in [
            "",
            "-",
            "--",
            "N/A",
            "nan",
            "None",
            "null"
        ]:

            return None

        text = text.replace(
            ",",
            ""
        )

        text = text.replace(
            "%",
            ""
        )

        return float(text)

    except Exception:

        return None


def find_value(
    row,
    names
):

    if not isinstance(
        row,
        dict
    ):

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
    digits=2
):

    if value is None:
        return "N/A"

    return (
        f"{value:,.{digits}f}"
    )


# ============================================================
# TWSE 個股 PE / 殖利率 / PB
# ============================================================

def get_twse_pe_data():

    try:

        data = twse_get(
            "/exchangeReport/"
            "BWIBBU_ALL"
        )

        result = {}

        if not isinstance(
            data,
            list
        ):

            return result

        for row in data:

            code = str(
                row.get(
                    "Code",
                    ""
                )
            ).strip()

            if not code:
                continue

            pe = find_value(
                row,
                [
                    "PEratio",
                    "PER",
                    "本益比"
                ]
            )

            yield_value = find_value(
                row,
                [
                    "DividendYield",
                    "殖利率",
                    "殖利率(%)"
                ]
            )

            pb = find_value(
                row,
                [
                    "PBratio",
                    "PBR",
                    "股價淨值比"
                ]
            )

            result[code] = {

                "name":
                    row.get(
                        "Name",
                        ""
                    ),

                "pe":
                    pe,

                "yield":
                    yield_value,

                "pb":
                    pb,
            }

        return result

    except Exception as e:

        print(
            f"取得 TWSE "
            f"PE/PB/殖利率失敗：{e}"
        )

        return {}


# ============================================================
# 指定日期 PE
# ============================================================

def get_twse_pe_by_date(
    date_string
):

    try:

        data = twse_get(
            f"/exchangeReport/"
            f"BWIBBU_d?date={date_string}"
        )

        result = {}

        if not isinstance(
            data,
            list
        ):

            return result

        for row in data:

            code = str(
                row.get(
                    "Code",
                    ""
                )
            ).strip()

            if not code:
                continue

            pe = find_value(
                row,
                [
                    "PEratio",
                    "PER",
                    "本益比"
                ]
            )

            result[code] = pe

        return result

    except Exception as e:

        print(
            f"取得 {date_string} "
            f"PE 失敗：{e}"
        )

        return {}


# ============================================================
# TAIEX 官方市場 PE
# ============================================================

def calculate_taiex_market_pe():

    print(
        "計算 TAIEX 官方口徑市場 PE..."
    )

    try:

        data = twse_get(
            "/exchangeReport/"
            "BWIBBU_ALL"
        )

        values = []

        if isinstance(
            data,
            list
        ):

            for row in data:

                pe = find_value(
                    row,
                    [
                        "PEratio",
                        "PER",
                        "本益比"
                    ]
                )

                if pe is None:
                    continue

                if pe <= 0:
                    continue

                if pe > 200:
                    continue

                values.append(
                    pe
                )

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
            f"TAIEX 市場 PE 失敗："
            f"{e}"
        )

        return None


# ============================================================
# 市值
# ============================================================

def get_market_cap(
    code
):

    try:

        ticker = yf.Ticker(
            f"{code}.TW"
        )

        info = ticker.fast_info

        value = getattr(
            info,
            "market_cap",
            None
        )

        if value is not None:

            return float(
                value
            )

    except Exception as e:

        print(
            f"{code} 市值取得失敗："
            f"{e}"
        )

    return None


# ============================================================
# 同業前10大
# ============================================================

def get_top_industry_companies(
    industry,
    exclude_code=None
):

    candidates = INDUSTRY_POOL.get(
        industry,
        []
    )

    result = []

    for code in candidates:

        if code == exclude_code:
            continue

        market_cap = get_market_cap(
            code
        )

        if market_cap is None:
            continue

        result.append(
            {
                "code":
                    code,

                "market_cap":
                    market_cap
            }
        )

    result.sort(
        key=lambda x:
            x["market_cap"],
        reverse=True
    )

    return result[:10]


# ============================================================
# KD
# ============================================================

def calculate_kd(
    symbol
):

    try:

        ticker = yf.Ticker(
            symbol
        )

        data = ticker.history(
            period="6mo",
            interval="1d",
            auto_adjust=False
        )

        if data.empty:

            return None, None

        data = data.dropna(
            subset=[
                "High",
                "Low",
                "Close"
            ]
        )

        if len(data) < 14:

            return None, None

        low14 = (
            data["Low"]
            .rolling(
                window=14
            )
            .min()
        )

        high14 = (
            data["High"]
            .rolling(
                window=14
            )
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
                float("-inf")
            ],
            None
        )

        k = []
        d = []

        previous_k = 50.0
        previous_d = 50.0

        for value in (
            rsv.dropna()
        ):

            current_rsv = float(
                value
            )

            current_k = (
                previous_k * 2 / 3
                + current_rsv / 3
            )

            current_d = (
                previous_d * 2 / 3
                + current_k / 3
            )

            k.append(
                current_k
            )

            d.append(
                current_d
            )

            previous_k = current_k
            previous_d = current_d

        if not k or not d:

            return None, None

        return (
            float(k[-1]),
            float(d[-1])
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
    period=14
):

    try:

        ticker = yf.Ticker(
            symbol
        )

        data = ticker.history(
            period="6mo",
            interval="1d",
            auto_adjust=False
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

        gain = (
            delta.clip(
                lower=0
            )
        )

        loss = (
            -delta.clip(
                upper=0
            )
        )

        avg_gain = (
            gain
            .ewm(
                alpha=1 / period,
                adjust=False
            )
            .mean()
        )

        avg_loss = (
            loss
            .ewm(
                alpha=1 / period,
                adjust=False
            )
            .mean()
        )

        last_gain = (
            avg_gain.iloc[-1]
        )

        last_loss = (
            avg_loss.iloc[-1]
        )

        if last_loss == 0:

            return 100.0

        rs = (
            last_gain
            / last_loss
        )

        rsi = (
            100
            - (
                100
                / (1 + rs)
            )
        )

        return float(
            rsi
        )

    except Exception as e:

        print(
            f"{symbol} RSI失敗：{e}"
        )

        return None


# ============================================================
# 公司基本面
#
# yfinance：
# - earnings growth
# - ROE
#
# 缺資料就跳過。
# ============================================================

def get_company_fundamentals(
    symbol
):

    result = {
        "earnings_growth":
            None,

        "roe":
            None,
    }

    try:

        ticker = yf.Ticker(
            symbol
        )

        info = ticker.info

        growth = (
            info.get(
                "earningsGrowth"
            )
        )

        if growth is not None:

            growth = float(
                growth
            )

            # yfinance 通常為小數
            if abs(growth) < 5:

                growth = (
                    growth * 100
                )

            result[
                "earnings_growth"
            ] = growth

        roe = (
            info.get(
                "returnOnEquity"
            )
        )

        if roe is not None:

            roe = float(
                roe
            )

            if abs(roe) < 5:

                roe = (
                    roe * 100
                )

            result[
                "roe"
            ] = roe

    except Exception as e:

        print(
            f"{symbol} "
            f"基本面資料失敗：{e}"
        )

    return result


# ============================================================
# PEG
#
# PEG = PE / EPS成長率
#
# 例如：
# PE 25
# EPS成長 30%
# PEG = 0.83
# ============================================================

def calculate_peg(
    pe,
    growth
):

    if pe is None:
        return None

    if growth is None:
        return None

    if growth <= 0:
        return None

    if pe <= 0:
        return None

    return (
        pe / growth
    )


# ============================================================
# 歷史 PE
# ============================================================

def update_pe_history(
    target_codes,
    history
):

    today = datetime.now(
        TW_TZ
    ).date()

    today_string = (
        today.strftime(
            "%Y%m%d"
        )
    )

    # 今天已經存在
    for code in target_codes:

        if code not in history:
            continue

        if (
            today_string
            in history[code]
        ):

            print(
                f"{today_string} PE "
                "已存在，略過"
            )

            return history

    print(
        f"確認 {today_string} "
        "是否已有當日 PE..."
    )

    pe_data = get_twse_pe_by_date(
        today_string
    )

    if not pe_data:

        print(
            f"{today_string} "
            "尚未有當日 PE，"
            "不寫入"
        )

        return history

    valid_count = 0

    for code in target_codes:

        pe = pe_data.get(
            code
        )

        if pe is None:
            continue

        if pe <= 0:
            continue

        if pe > 200:
            continue

        valid_count += 1

    if valid_count == 0:

        print(
            "沒有有效目標股票 PE，"
            "不寫入"
        )

        return history

    # --------------------------------------------------------
    # 確認當天確實是交易日
    # --------------------------------------------------------

    try:

        market_data = twse_get(
            "/exchangeReport/"
            "STOCK_DAY_ALL"
        )

        if not isinstance(
            market_data,
            list
        ):

            print(
                "市場交易資料異常，"
                "不寫入歷史 PE"
            )

            return history

        valid_rows = 0

        for row in market_data:

            if not isinstance(
                row,
                dict
            ):
                continue

            close = find_value(
                row,
                [
                    "ClosingPrice",
                    "收盤價",
                    "Close"
                ]
            )

            if close is None:
                continue

            if close <= 0:
                continue

            valid_rows += 1

            if valid_rows >= 10:
                break

        if valid_rows < 10:

            print(
                "尚未確認當日交易，"
                "不寫入歷史 PE"
            )

            return history

    except Exception as e:

        print(
            f"確認交易日失敗：{e}"
        )

        return history

    # --------------------------------------------------------
    # 寫入
    # --------------------------------------------------------

    for code in target_codes:

        pe = pe_data.get(
            code
        )

        if pe is None:
            continue

        if pe <= 0:
            continue

        if pe > 200:
            continue

        history.setdefault(
            code,
            {}
        )

        history[
            code
        ][
            today_string
        ] = pe

        print(
            f"{code} PE："
            f"{pe:.2f}"
        )

    return history


# ============================================================
# 一年平均 PE
# ============================================================

def calculate_one_year_average_pe(
    code,
    history
):

    stock_history = history.get(
        code,
        {}
    )

    if not stock_history:

        return None, 0

    today = datetime.now(
        TW_TZ
    ).date()

    cutoff = (
        today
        - timedelta(
            days=365
        )
    )

    values = []

    for date_string, pe in (
        stock_history.items()
    ):

        try:

            date_obj = (
                datetime.strptime(
                    date_string,
                    "%Y%m%d"
                ).date()
            )

        except Exception:

            continue

        if date_obj < cutoff:
            continue

        if pe is None:
            continue

        if pe <= 0:
            continue

        if pe > 200:
            continue

        values.append(
            float(pe)
        )

    if not values:

        return None, 0

    return (
        sum(values)
        / len(values),
        len(values)
    )


# ============================================================
# T86 三大法人
#
# 使用 TWSE 官方 T86。
# 這是盤後資料，所以凌晨尚未產生時會跳過。
# ============================================================

def get_t86_data(
    date_string
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
                    "json"
            }
        )

        if not isinstance(
            payload,
            dict
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
                    raw_row
                )
            )

            code = str(
                row.get(
                    "證券代號",
                    row.get(
                        "代號",
                        ""
                    )
                )
            ).strip()

            if not code:
                continue

            net = find_value(
                row,
                [
                    "三大法人買賣超股數",
                    "三大法人買賣超",
                ]
            )

            foreign = find_value(
                row,
                [
                    "外陸資買賣超股數"
                ]
            )

            trust = find_value(
                row,
                [
                    "投信買賣超股數"
                ]
            )

            dealer = find_value(
                row,
                [
                    "自營商買賣超股數"
                ]
            )

            result[code] = {

                "total":
                    net,

                "foreign":
                    foreign,

                "trust":
                    trust,

                "dealer":
                    dealer,
            }

        return result

    except Exception as e:

        print(
            f"T86 {date_string} "
            f"取得失敗：{e}"
        )

        return {}


# ============================================================
# 找最近交易日
# ============================================================

def get_recent_trading_dates(
    count=20
):

    dates = []

    current = datetime.now(
        TW_TZ
    ).date()

    # 往前最多找45天
    for i in range(45):

        day = (
            current
            - timedelta(
                days=i
            )
        )

        date_string = (
            day.strftime(
                "%Y%m%d"
            )
        )

        # 用 T86 判斷是否為真正有法人資料的交易日
        data = get_t86_data(
            date_string
        )

        if data:

            dates.append(
                date_string
            )

            if len(dates) >= count:

                break

        # 避免 API 打太快
        time.sleep(
            0.15
        )

    return dates


# ============================================================
# 三大法人 5 / 20 日
# ============================================================

def calculate_institutional_scores(
    code
):

    dates = get_recent_trading_dates(
        20
    )

    if not dates:

        return {
            "5d":
                None,

            "20d":
                None,

            "latest":
                None
        }

    values = []

    for date_string in dates:

        data = get_t86_data(
            date_string
        )

        item = data.get(
            code
        )

        if not item:
            continue

        total = item.get(
            "total"
        )

        if total is None:
            continue

        values.append(
            total
        )

    if not values:

        return {
            "5d":
                None,

            "20d":
                None,

            "latest":
                None
        }

    latest = values[0]

    five = (
        sum(
            values[:5]
        )
        if len(values) >= 5
        else None
    )

    twenty = (
        sum(
            values[:20]
        )
        if len(values) >= 20
        else None
    )

    return {
        "5d":
            five,

        "20d":
            twenty,

        "latest":
            latest
    }


# ============================================================
# 融資融券
#
# MI_MARGN 為 TWSE 官方集中市場融資融券資料。
# ============================================================

def get_margin_data():

    try:

        data = twse_get(
            "/exchangeReport/"
            "MI_MARGN"
        )

        if not isinstance(
            data,
            list
        ):

            return {}

        result = {}

        for row in data:

            if not isinstance(
                row,
                dict
            ):

                continue

            code = str(
                row.get(
                    "股票代號",
                    row.get(
                        "Code",
                        ""
                    )
                )
            ).strip()

            if not code:
                continue

            margin = find_value(
                row,
                [
                    "融資餘額",
                    "MarginBalance",
                    "融資餘額(張)"
                ]
            )

            short = find_value(
                row,
                [
                    "融券餘額",
                    "ShortBalance",
                    "融券餘額(張)"
                ]
            )

            result[code] = {

                "margin":
                    margin,

                "short":
                    short,
            }

        return result

    except Exception as e:

        print(
            f"融資融券取得失敗：{e}"
        )

        return {}


# ============================================================
# 歷史融資
# ============================================================

def update_margin_history(
    codes,
    history
):

    today = datetime.now(
        TW_TZ
    ).date()

    today_string = (
        today.strftime(
            "%Y%m%d"
        )
    )

    if (
        today_string
        in history.get(
            "_dates",
            []
        )
    ):

        return history

    margin_data = get_margin_data()

    if not margin_data:

        print(
            "今日融資融券尚未更新"
        )

        return history

    for code in codes:

        item = margin_data.get(
            code
        )

        if not item:
            continue

        history.setdefault(
            code,
            {}
        )

        history[
            code
        ][
            today_string
        ] = item

    history.setdefault(
        "_dates",
        []
    )

    history[
        "_dates"
    ].append(
        today_string
    )

    # 最多保留100個交易日日期
    history[
        "_dates"
    ] = history[
        "_dates"
    ][-100:]

    return history


# ============================================================
# 融資5日變化
# ============================================================

def calculate_margin_change(
    code,
    history
):

    stock = history.get(
        code,
        {}
    )

    dates = []

    for key in stock.keys():

        if key == "_dates":
            continue

        try:

            datetime.strptime(
                key,
                "%Y%m%d"
            )

            dates.append(
                key
            )

        except Exception:

            continue

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

    previous = stock[
        dates[
            min(
                5,
                len(dates) - 1
            )
        ]
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


# ============================================================
# 券資比
#
# 融券 / 融資 × 100
# ============================================================

def calculate_short_margin_ratio(
    margin_item
):

    if not margin_item:

        return None

    margin = margin_item.get(
        "margin"
    )

    short = margin_item.get(
        "short"
    )

    if (
        margin is None
        or short is None
    ):

        return None

    if margin <= 0:

        return None

    return (
        short
        / margin
        * 100
    )


# ============================================================
# 技術＋估值評分
# ============================================================

def check_valuation_v2(
    code,
    stock_info,
    current_pe_data,
    market_pe,
    pe_history,
    margin_history,
    state
):

    name = stock_info[
        "name"
    ]

    symbol = stock_info[
        "symbol"
    ]

    industry = stock_info[
        "industry"
    ]

    print(
        f"\n========== "
        f"V2估值檢查：{name} "
        f"=========="
    )

    # --------------------------------------------------------
    # 模型
    # --------------------------------------------------------

    model = INDUSTRY_MODEL.get(
        industry,
        {
            "pe": True,
            "peg": False,
            "pb": True,
            "yield": False,
        }
    )

    item = current_pe_data.get(
        code
    )

    if not item:

        print(
            "無 TWSE 基本面資料"
        )

        return

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

    roe = (
        fundamentals[
            "roe"
        ]
    )

    peg = calculate_peg(
        stock_pe,
        earnings_growth
    )

    # --------------------------------------------------------
    # 一年平均 PE
    # --------------------------------------------------------

    one_year_pe, sample_count = (
        calculate_one_year_average_pe(
            code,
            pe_history
        )
    )

    # --------------------------------------------------------
    # 同業 PE
    # --------------------------------------------------------

    industry_pe = None

    peers = get_top_industry_companies(
        industry,
        exclude_code=code
    )

    peer_values = []

    for peer in peers:

        peer_item = current_pe_data.get(
            peer["code"]
        )

        if not peer_item:
            continue

        peer_pe = peer_item.get(
            "pe"
        )

        if peer_pe is None:
            continue

        if peer_pe <= 0:
            continue

        if peer_pe > 200:
            continue

        peer_values.append(
            peer_pe
        )

    if peer_values:

        industry_pe = (
            sum(peer_values)
            / len(peer_values)
        )

    # --------------------------------------------------------
    # KD / RSI
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
            code
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
            margin_history
        )
    )

    margin_item = (
        margin_history
        .get(
            code,
            {}
        )
    )

    # 找最新融資資料
    latest_margin = None

    margin_dates = []

    for date_string in (
        margin_item.keys()
    ):

        try:

            datetime.strptime(
                date_string,
                "%Y%m%d"
            )

            margin_dates.append(
                date_string
            )

        except Exception:

            continue

    if margin_dates:

        latest_date = max(
            margin_dates
        )

        latest_margin = (
            margin_item[
                latest_date
            ]
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

    reasons_good = []
    reasons_neutral = []

    # --------------------------------------------------------
    # PE
    # --------------------------------------------------------

    if model["pe"]:

        if (
            stock_pe is not None
            and market_pe is not None
            and stock_pe < market_pe
        ):

            score += 1

            reasons_good.append(
                "PE低於TAIEX"
            )

        else:

            reasons_neutral.append(
                "PE未低於TAIEX"
            )

        if (
            stock_pe is not None
            and industry_pe is not None
            and stock_pe < industry_pe
        ):

            score += 1

            reasons_good.append(
                "PE低於同業"
            )

        if (
            stock_pe is not None
            and one_year_pe is not None
            and sample_count >= 60
            and stock_pe < one_year_pe
        ):

            score += 1

            reasons_good.append(
                "PE低於一年平均"
            )

    # --------------------------------------------------------
    # PEG
    # --------------------------------------------------------

    if model["peg"]:

        if (
            peg is not None
            and peg < 1
        ):

            score += 1

            reasons_good.append(
                "PEG < 1"
            )

    # --------------------------------------------------------
    # PB
    # --------------------------------------------------------

    if model["pb"]:

        if (
            stock_pb is not None
            and stock_pb > 0
            and stock_pb < 1
        ):

            score += 1

            reasons_good.append(
                "PB < 1"
            )

    # --------------------------------------------------------
    # 殖利率
    # --------------------------------------------------------

    if model["yield"]:

        if (
            stock_yield is not None
            and stock_yield >= 4
        ):

            score += 1

            reasons_good.append(
                "殖利率 >= 4%"
            )

    # --------------------------------------------------------
    # ROE
    # --------------------------------------------------------

    if industry == "金融":

        if (
            roe is not None
            and roe >= 10
        ):

            score += 1

            reasons_good.append(
                "ROE >= 10%"
            )

    # --------------------------------------------------------
    # KD
    # --------------------------------------------------------

    if (
        k is not None
        and d is not None
        and k < 30
        and d < 30
    ):

        score += 1

        reasons_good.append(
            "KD < 30"
        )

    # --------------------------------------------------------
    # RSI
    # --------------------------------------------------------

    if (
        rsi is not None
        and rsi < 35
    ):

        score += 1

        reasons_good.append(
            "RSI < 35"
        )

    # --------------------------------------------------------
    # 法人5日
    # --------------------------------------------------------

    if (
        inst_5d is not None
        and inst_5d > 0
    ):

        score += 1

        reasons_good.append(
            "法人5日買超"
        )

    # --------------------------------------------------------
    # 法人20日
    # --------------------------------------------------------

    if (
        inst_20d is not None
        and inst_20d > 0
    ):

        score += 1

        reasons_good.append(
            "法人20日買超"
        )

    # --------------------------------------------------------
    # 融資下降
    # --------------------------------------------------------

    if (
        margin_change is not None
        and margin_change < 0
    ):

        score += 1

        reasons_good.append(
            "融資5日下降"
        )

    # --------------------------------------------------------
    # 券資比
    #
    # 不直接加分。
    # 只作為輔助資訊。
    # --------------------------------------------------------

    # ========================================================
    # 最大分數
    #
    # 不同產業模型會導致不同最大分數。
    # 所以這裡直接使用實際可取得項目。
    # ========================================================

    possible_score = 0

    # PE相關
    if model["pe"]:
        possible_score += 3

    # PEG
    if model["peg"]:
        possible_score += 1

    # PB
    if model["pb"]:
        possible_score += 1

    # 殖利率
    if model["yield"]:
        possible_score += 1

    # 金融 ROE
    if industry == "金融":
        possible_score += 1

    # KD
    possible_score += 1

    # RSI
    possible_score += 1

    # 法人
    possible_score += 2

    # 融資
    possible_score += 1

    # --------------------------------------------------------
    # 重要：
    # 如果資料尚未更新，不要讓分母把分數拉低。
    # --------------------------------------------------------

    # --------------------------------------------------------
    # 顯示
    # --------------------------------------------------------

    print(
        f"產業：{industry}"
    )

    print(
        f"估值模型：{model}"
    )

    print(
        f"PE："
        f"{format_number(stock_pe)}"
    )

    print(
        f"TAIEX PE："
        f"{format_number(market_pe)}"
    )

    print(
        f"同業 PE："
        f"{format_number(industry_pe)}"
    )

    print(
        f"一年平均 PE："
        f"{format_number(one_year_pe)} "
        f"({sample_count}筆)"
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
        f"目前評分："
        f"{score}/{possible_score}"
    )

    print(
        "加分項目："
        + (
            "、".join(
                reasons_good
            )
            if reasons_good
            else "無"
        )
    )

    # ========================================================
    # 標準化評分
    # ========================================================

    if possible_score <= 0:

        print(
            "沒有足夠資料，跳過通知"
        )

        return

    score_ratio = (
        score
        / possible_score
    )

    # --------------------------------------------------------
    # 通知門檻
    #
    # 強力加碼：
    # 8分以上且至少80%
    #
    # 可以加碼：
    # 6分以上且至少65%
    #
    # 這樣避免因資料尚未更新造成假高分。
    # --------------------------------------------------------

    strong = (
        score >= STRONG_SCORE
        and score_ratio >= 0.80
    )

    good = (
        score >= GOOD_SCORE
        and score_ratio >= 0.65
    )

    if strong:

        level = (
            "🟢 強力加碼"
        )

    elif good:

        level = (
            "🟡 可以分批加碼"
        )

    else:

        level = None

    if level is None:

        state.setdefault(
            "valuation_v2",
            {}
        )

        state[
            "valuation_v2"
        ][code] = False

        print(
            "評分未達通知門檻"
        )

        return

    # ========================================================
    # 防止重複通知
    # ========================================================

    state.setdefault(
        "valuation_v2",
        {}
    )

    already_alerted = (
        state[
            "valuation_v2"
        ].get(
            code,
            False
        )
    )

    if already_alerted:

        print(
            "V2估值條件已通知，"
            "略過重複通知"
        )

        return

    # ========================================================
    # LINE
    # ========================================================

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

    message = (
        f"{level}\n\n"
        f"標的：{name}\n"
        f"產業：{industry}\n\n"

        "【估值】\n"
        f"PE："
        f"{format_number(stock_pe)} 倍\n"
        f"TAIEX PE："
        f"{format_number(market_pe)} 倍\n"
        f"同業平均 PE："
        f"{format_number(industry_pe)} 倍\n"
        f"1年平均 PE："
        f"{format_number(one_year_pe)} 倍\n"
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
        f"{inst5_text}\n"
        f"法人20日："
        f"{inst20_text}\n"
        f"融資5日變化："
        f"{margin_text}\n"
        f"券資比："
        f"{format_number(short_margin_ratio)}%\n\n"

        "━━━━━━━━━━\n"
        f"加碼評分："
        f"{score}/{possible_score}\n"
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
    )

    send_line(
        message
    )

    state[
        "valuation_v2"
    ][code] = True

    print(
        "🟢 已發送 V2 加碼通知"
    )


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
        - timedelta(
            days=14
        )
    )

    data = yf.download(
        symbol,
        start=start.strftime(
            "%Y-%m-%d"
        ),
        end=(
            end
            + timedelta(
                days=1
            )
        ).strftime(
            "%Y-%m-%d"
        ),
        interval="1d",
        auto_adjust=False,
        progress=False
    )

    if data.empty:

        return None

    try:

        close = data[
            "Close"
        ]

        if hasattr(
            close,
            "columns"
        ):

            close = close.iloc[
                :,
                0
            ]

    except Exception:

        close = data.iloc[
            :,
            0
        ]

    return close.dropna()


# ============================================================
# 最新價格
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
            auto_adjust=False
        )

        if not intraday.empty:

            prices = (
                intraday[
                    "Close"
                ].dropna()
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
            auto_adjust=False
        )

        if not daily.empty:

            prices = (
                daily[
                    "Close"
                ].dropna()
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
# 前一交易日
# ============================================================

def get_previous_close(
    history
):

    if history is None:

        return None

    if len(history) < 2:

        return None

    return float(
        history.iloc[-2]
    )


# ============================================================
# 7日高點
# ============================================================

def get_week_high(
    symbol
):

    end = datetime.now(
        TW_TZ
    )

    start = (
        end
        - timedelta(
            days=7
        )
    )

    ticker = yf.Ticker(
        symbol
    )

    try:

        data = ticker.history(
            start=start.strftime(
                "%Y-%m-%d"
            ),
            end=(
                end
                + timedelta(
                    days=1
                )
            ).strftime(
                "%Y-%m-%d"
            ),
            interval="1d",
            auto_adjust=False
        )

        if data.empty:

            return None

        highs = (
            data[
                "High"
            ].dropna()
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
# 原本跌幅通知
# ============================================================

def check_stock(
    name,
    symbol,
    state
):

    print(
        f"\n========== "
        f"{name} "
        f"=========="
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
                today
        }
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

        if not stock_state[
            "daily_alert"
        ]:

            message = (
                "🔴 跌幅通知\n\n"
                f"標的：{name}\n"
                f"目前價格："
                f"{current:,.2f}\n"
                f"前一交易日收盤："
                f"{previous_close:,.2f}\n"
                f"單日跌幅："
                f"{daily_change:.2%}\n\n"
                "⚠️ 已達到單日 -5%，可加碼"
            )

            send_line(
                message
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

        if not stock_state[
            "weekly_alert"
        ]:

            message = (
                "🔴 跌幅通知\n\n"
                f"標的：{name}\n"
                f"目前價格："
                f"{current:,.2f}\n"
                f"過去7日最高價："
                f"{week_high:,.2f}\n"
                f"距7日高點跌幅："
                f"{weekly_change:.2%}\n\n"
                "⚠️ 已達到一週 -10%，可加碼"
            )

            send_line(
                message
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
# MAIN
# ============================================================

def main():

    print(
        "================================"
    )

    print(
        "股票跌幅 + V2估值 + "
        "技術 + 籌碼 LINE 通知"
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

    # --------------------------------------------------------
    # 當日 PE / PB / 殖利率
    # --------------------------------------------------------

    current_pe_data = (
        get_twse_pe_data()
    )

    if current_pe_data:

        print(
            f"取得 "
            f"{len(current_pe_data)} "
            "筆上市 PE/PB/殖利率"
        )

    else:

        print(
            "⚠️ TWSE 基本面資料取得失敗"
        )

    # --------------------------------------------------------
    # TAIEX PE
    # --------------------------------------------------------

    market_pe = (
        calculate_taiex_market_pe()
    )

    if market_pe is not None:

        print(
            f"TAIEX 官方市場 PE："
            f"{market_pe:.2f}"
        )

    else:

        print(
            "⚠️ 無法取得 TAIEX 市場 PE"
        )

    # --------------------------------------------------------
    # PE歷史
    # --------------------------------------------------------

    target_codes = list(
        VALUATION_STOCKS.keys()
    )

    pe_history = update_pe_history(
        target_codes,
        pe_history
    )

    save_json(
        PE_HISTORY_FILE,
        pe_history
    )

    # --------------------------------------------------------
    # 融資融券歷史
    # --------------------------------------------------------

    margin_history = (
        update_margin_history(
            target_codes,
            margin_history
        )
    )

    save_json(
        CHIP_HISTORY_FILE,
        margin_history
    )

    # --------------------------------------------------------
    # 原本跌幅監控
    # --------------------------------------------------------

    for name, symbol in (
        STOCKS.items()
    ):

        try:

            check_stock(
                name,
                symbol,
                state
            )

        except Exception as e:

            print(
                f"{name} 發生錯誤："
                f"{e}"
            )

    # --------------------------------------------------------
    # V2
    # --------------------------------------------------------

    if current_pe_data:

        for code, stock_info in (
            VALUATION_STOCKS.items()
        ):

            try:

                check_valuation_v2(
                    code,
                    stock_info,
                    current_pe_data,
                    market_pe,
                    pe_history,
                    margin_history,
                    state
                )

            except Exception as e:

                print(
                    f"{stock_info['name']} "
                    f"V2檢查錯誤："
                    f"{e}"
                )

    # --------------------------------------------------------
    # 儲存狀態
    # --------------------------------------------------------

    save_json(
        STATE_FILE,
        state
    )

    print(
        "\n全部檢查完成"
    )


if __name__ == "__main__":

    main()
