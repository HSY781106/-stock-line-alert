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

STATE_FILE = "alert_state.json"
PE_HISTORY_FILE = "pe_history.json"

DAILY_THRESHOLD = -0.05
WEEK_THRESHOLD = -0.10

TW_TZ = ZoneInfo("Asia/Taipei")


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
# 需要做「估值＋KD」判斷的個股
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
# 細分產業候選池
#
# 注意：
# 這不是固定「前10大」。
# 程式會先從這些候選公司取得市值，
# 再選出當下市值最大的10家。
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
}


# ============================================================
# TWSE API
# ============================================================

def twse_get(endpoint, timeout=20):

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
# LINE
# ============================================================

def send_line(message):

    url = "https://api.line.me/v2/bot/message/broadcast"

    headers = {
        "Authorization": f"Bearer {LINE_TOKEN}",
        "Content-Type": "application/json",
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
            f"LINE API error: {response.status_code} {response.text}"
        )


# ============================================================
# JSON state
# ============================================================

def load_json(filename):

    if not os.path.exists(filename):
        return {}

    try:
        with open(filename, "r", encoding="utf-8") as f:
            return json.load(f)

    except Exception:
        return {}


def save_json(filename, data):

    with open(filename, "w", encoding="utf-8") as f:
        json.dump(
            data,
            f,
            ensure_ascii=False,
            indent=2
        )


# ============================================================
# 數字處理
# ============================================================

def to_float(value):

    if value is None:
        return None

    try:

        text = str(value).strip()

        if text in ["", "-", "--", "N/A", "nan", "None"]:
            return None

        text = text.replace(",", "")

        return float(text)

    except Exception:
        return None


def find_value(row, possible_names):

    for name in possible_names:

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
# TWSE：當日個股 PE
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

            pe = find_value(
                row,
                [
                    "PEratio",
                    "PER",
                    "本益比"
                ]
            )

            name = row.get(
                "Name",
                ""
            )

            result[code] = {
                "name": name,
                "pe": pe,
            }

        return result

    except Exception as e:

        print(
            f"取得 TWSE 當日 PE 失敗：{e}"
        )

        return {}


# ============================================================
# TWSE：指定日期 PE
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

            code = str(
                row.get("Code", "")
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
            f"取得 {date_string} PE 失敗：{e}"
        )

        return {}


# ============================================================
# 取得台股大盤 PE
#
# 使用全市場有效個股 PE 的簡單平均，
# 避免把沒有 PE / 負 PE 公司納入。
# ============================================================

def calculate_market_pe(pe_data):

    values = []

    for item in pe_data.values():

        pe = item.get("pe")

        if pe is None:
            continue

        if pe <= 0:
            continue

        if pe > 200:
            continue

        values.append(pe)

    if len(values) == 0:
        return None

    return sum(values) / len(values)


# ============================================================
# 取得市值
#
# yfinance 用於同業排序。
# ============================================================

def get_market_cap(code):

    try:

        ticker = yf.Ticker(
            f"{code}.TW"
        )

        info = ticker.fast_info

        market_cap = getattr(
            info,
            "market_cap",
            None
        )

        if market_cap is not None:
            return float(market_cap)

    except Exception as e:

        print(
            f"{code} 市值取得失敗：{e}"
        )

    return None


# ============================================================
# 找出同業市值前10大
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
                "code": code,
                "market_cap": market_cap,
            }
        )

    result.sort(
        key=lambda x: x["market_cap"],
        reverse=True
    )

    return result[:10]


# ============================================================
# KD
# ============================================================

def calculate_kd(symbol):

    try:

        ticker = yf.Ticker(symbol)

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

        low14 = data["Low"].rolling(
            window=14
        ).min()

        high14 = data["High"].rolling(
            window=14
        ).max()

        denominator = (
            high14 - low14
        )

        rsv = (
            (data["Close"] - low14)
            / denominator
            * 100
        )

        rsv = rsv.replace(
            [float("inf"), float("-inf")],
            None
        )

        k = []
        d = []

        previous_k = 50.0
        previous_d = 50.0

        for value in rsv.dropna():

            current_rsv = float(value)

            current_k = (
                previous_k * 2 / 3
                + current_rsv / 3
            )

            current_d = (
                previous_d * 2 / 3
                + current_k / 3
            )

            k.append(current_k)
            d.append(current_d)

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
            f"{symbol} KD 計算失敗：{e}"
        )

        return None, None


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

    today_string = today.strftime(
        "%Y%m%d"
    )

    if today_string in history:
        return history

    print(
        f"更新 {today_string} PE 歷史資料"
    )

    pe_data = get_twse_pe_by_date(
        today_string
    )

    if not pe_data:
        return history

    for code in target_codes:

        pe = pe_data.get(code)

        if pe is None:
            continue

        if pe <= 0:
            continue

        if pe > 200:
            continue

        if code not in history:
            history[code] = {}

        history[code][today_string] = pe

    return history


# ============================================================
# 計算個股過去一年平均 PE
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

    cutoff = today - timedelta(
        days=365
    )

    values = []

    for date_string, pe in stock_history.items():

        try:

            date_obj = datetime.strptime(
                date_string,
                "%Y%m%d"
            ).date()

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

        values.append(float(pe))

    if len(values) == 0:
        return None, 0

    return (
        sum(values) / len(values),
        len(values)
    )


# ============================================================
# 估值條件
# ============================================================

def check_valuation(
    code,
    stock_info,
    current_pe_data,
    market_pe,
    history,
    state
):

    name = stock_info["name"]
    symbol = stock_info["symbol"]
    industry = stock_info["industry"]

    print(
        f"\n---------- 估值檢查：{name} ----------"
    )

    # --------------------------------------------------------
    # 1. 個股 PE
    # --------------------------------------------------------

    item = current_pe_data.get(code)

    if not item:
        print("無當日 PE，跳過")
        return

    stock_pe = item.get("pe")

    if stock_pe is None or stock_pe <= 0:
        print("個股 PE 無效，跳過")
        return

    # --------------------------------------------------------
    # 2. 大盤 PE
    # --------------------------------------------------------

    if market_pe is None:
        print("無法取得大盤 PE，跳過")
        return

    # --------------------------------------------------------
    # 3. 個股一年平均 PE
    # --------------------------------------------------------

    one_year_pe, sample_count = (
        calculate_one_year_average_pe(
            code,
            history
        )
    )

    if one_year_pe is None:
        print("沒有一年歷史 PE，跳過")
        return

    # 至少累積60個交易日
    if sample_count < 60:
        print(
            f"歷史 PE 僅 {sample_count} 筆，"
            "未達60筆，跳過"
        )
        return

    # --------------------------------------------------------
    # 4. 同業前10大
    # --------------------------------------------------------

    peers = get_top_industry_companies(
        industry,
        exclude_code=code
    )

    peer_values = []

    for peer in peers:

        peer_code = peer["code"]

        peer_item = current_pe_data.get(
            peer_code
        )

        if not peer_item:
            continue

        peer_pe = peer_item.get("pe")

        if peer_pe is None:
            continue

        if peer_pe <= 0:
            continue

        if peer_pe > 200:
            continue

        peer_values.append(
            peer_pe
        )

    # 至少3家有效同業
    if len(peer_values) < 3:

        print(
            f"有效同業只有 {len(peer_values)} 家，"
            "少於3家，跳過"
        )

        return

    industry_pe = (
        sum(peer_values)
        / len(peer_values)
    )

    # --------------------------------------------------------
    # 5. KD
    # --------------------------------------------------------

    k, d = calculate_kd(
        symbol
    )

    if k is None or d is None:

        print(
            "KD 無法取得，跳過"
        )

        return

    # --------------------------------------------------------
    # 條件
    # --------------------------------------------------------

    condition_market = (
        stock_pe < market_pe
    )

    condition_industry = (
        stock_pe < industry_pe
    )

    condition_history = (
        stock_pe < one_year_pe
    )

    condition_kd = (
        k < 30 and d < 30
    )

    print(
        f"個股 PE：{stock_pe:.2f}"
    )

    print(
        f"台股平均 PE：{market_pe:.2f}"
    )

    print(
        f"同業平均 PE：{industry_pe:.2f}"
    )

    print(
        f"一年平均 PE：{one_year_pe:.2f}"
    )

    print(
        f"KD：K={k:.2f} / D={d:.2f}"
    )

    print(
        f"大盤條件：{condition_market}"
    )

    print(
        f"同業條件：{condition_industry}"
    )

    print(
        f"一年平均條件：{condition_history}"
    )

    print(
        f"KD條件：{condition_kd}"
    )

    # --------------------------------------------------------
    # 全部成立
    # --------------------------------------------------------

    all_conditions = (
        condition_market
        and condition_industry
        and condition_history
        and condition_kd
    )

    if not all_conditions:

        # 如果離開條件區，解除通知鎖定
        state.setdefault(
            "valuation",
            {}
        )

        state["valuation"].setdefault(
            code,
            False
        )

        state["valuation"][code] = False

        return

    # --------------------------------------------------------
    # 防止重複通知
    # --------------------------------------------------------

    state.setdefault(
        "valuation",
        {}
    )

    already_alerted = state[
        "valuation"
    ].get(
        code,
        False
    )

    if already_alerted:

        print(
            "估值條件已通知，略過重複通知"
        )

        return

    # --------------------------------------------------------
    # LINE
    # --------------------------------------------------------

    message = (
        "🟢 估值加碼通知\n\n"
        f"標的：{name}\n"
        f"目前 PE：{stock_pe:.2f} 倍\n"
        f"台股平均 PE：{market_pe:.2f} 倍\n"
        f"同業前10大平均 PE：{industry_pe:.2f} 倍\n"
        f"個股1年平均 PE：{one_year_pe:.2f} 倍\n\n"
        f"KD：K {k:.2f} / D {d:.2f}\n\n"
        "⚠️ 估值低於大盤、同業及自身一年平均，"
        "且 KD < 30，可加碼"
    )

    send_line(message)

    state["valuation"][code] = True

    print(
        "🟢 已發送估值加碼通知"
    )


# ============================================================
# 原本的價格歷史
# ============================================================

def get_history(symbol):

    end = datetime.now(
        TW_TZ
    )

    start = end - timedelta(
        days=14
    )

    data = yf.download(
        symbol,
        start=start.strftime("%Y-%m-%d"),
        end=(
            end + timedelta(days=1)
        ).strftime("%Y-%m-%d"),
        interval="1d",
        auto_adjust=False,
        progress=False
    )

    if data.empty:
        return None

    try:

        close = data["Close"]

        if hasattr(
            close,
            "columns"
        ):

            close = close.iloc[:, 0]

    except Exception:

        close = data.iloc[:, 0]

    return close.dropna()


# ============================================================
# 原本的即時價格
# ============================================================

def get_latest_price(symbol):

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

            price = (
                intraday["Close"]
                .dropna()
            )

            if len(price) > 0:

                return float(
                    price.iloc[-1]
                )

    except Exception as e:

        print(
            f"{symbol} 1m資料失敗：{e}"
        )

    try:

        daily = ticker.history(
            period="5d",
            interval="1d",
            auto_adjust=False
        )

        if not daily.empty:

            price = (
                daily["Close"]
                .dropna()
            )

            if len(price) > 0:

                return float(
                    price.iloc[-1]
                )

    except Exception as e:

        print(
            f"{symbol} 日線資料失敗：{e}"
        )

    return None


# ============================================================
# 原本的前一交易日收盤
# ============================================================

def get_previous_close(history):

    if history is None:
        return None

    if len(history) < 2:
        return None

    return float(
        history.iloc[-2]
    )


# ============================================================
# 原本的7日高點
# ============================================================

def get_week_high(symbol):

    end = datetime.now(
        TW_TZ
    )

    start = end - timedelta(
        days=7
    )

    ticker = yf.Ticker(
        symbol
    )

    try:

        data = ticker.history(
            start=start.strftime("%Y-%m-%d"),
            end=(
                end + timedelta(days=1)
            ).strftime("%Y-%m-%d"),
            interval="1d",
            auto_adjust=False
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
# 原本跌幅檢查
# ============================================================

def check_stock(
    name,
    symbol,
    state
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
        current / previous_close
        - 1
    )

    weekly_change = (
        current / week_high
        - 1
    )

    print(
        f"目前價格：{current}"
    )

    print(
        f"前一交易日收盤：{previous_close}"
    )

    print(
        f"單日跌幅：{daily_change:.2%}"
    )

    print(
        f"過去7日最高價：{week_high}"
    )

    print(
        f"距7日高點：{weekly_change:.2%}"
    )

    now = datetime.now(
        TW_TZ
    )

    today = now.strftime(
        "%Y-%m-%d"
    )

    state.setdefault(
        "daily",
        {}
    )

    state["daily"].setdefault(
        name,
        {
            "daily_alert": False,
            "weekly_alert": False,
            "date": today
        }
    )

    stock_state = state[
        "daily"
    ][name]

    # 新的一天
    if stock_state.get(
        "date"
    ) != today:

        stock_state[
            "daily_alert"
        ] = False

        stock_state[
            "date"
        ] = today

    # ========================================================
    # 單日 -5%
    # ========================================================

    if daily_change <= DAILY_THRESHOLD:

        if not stock_state[
            "daily_alert"
        ]:

            message = (
                "🔴 跌幅通知\n\n"
                f"標的：{name}\n"
                f"目前價格：{current:,.2f}\n"
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

    # ========================================================
    # 一週 -10%
    # ========================================================

    if weekly_change <= WEEK_THRESHOLD:

        if not stock_state[
            "weekly_alert"
        ]:

            message = (
                "🔴 跌幅通知\n\n"
                f"標的：{name}\n"
                f"目前價格：{current:,.2f}\n"
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
# 主程式
# ============================================================

def main():

    print(
        "================================"
    )

    print(
        "股票跌幅 + 估值 + KD LINE 通知"
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

    # --------------------------------------------------------
    # 取得今日 PE
    # --------------------------------------------------------

    current_pe_data = (
        get_twse_pe_data()
    )

    if current_pe_data:

        print(
            f"取得 {len(current_pe_data)} "
            "筆上市個股 PE"
        )

    else:

        print(
            "⚠️ 今日 PE 資料取得失敗"
        )

    # --------------------------------------------------------
    # 大盤 PE
    # --------------------------------------------------------

    market_pe = calculate_market_pe(
        current_pe_data
    )

    if market_pe is not None:

        print(
            f"台股平均 PE："
            f"{market_pe:.2f}"
        )

    else:

        print(
            "⚠️ 無法計算台股 PE"
        )

    # --------------------------------------------------------
    # 更新估值股票歷史 PE
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
    # 原本的5個標的
    # --------------------------------------------------------

    for name, symbol in STOCKS.items():

        try:

            check_stock(
                name,
                symbol,
                state
            )

        except Exception as e:

            print(
                f"{name} 發生錯誤：{e}"
            )

    # --------------------------------------------------------
    # 新增：估值＋KD
    # --------------------------------------------------------

    if current_pe_data:

        for code, stock_info in (
            VALUATION_STOCKS.items()
        ):

            try:

                check_valuation(
                    code,
                    stock_info,
                    current_pe_data,
                    market_pe,
                    pe_history,
                    state
                )

            except Exception as e:

                print(
                    f"{stock_info['name']} "
                    f"估值檢查錯誤：{e}"
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
