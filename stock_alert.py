import os
import json
import requests
import yfinance as yf
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

LINE_TOKEN = os.environ["LINE_CHANNEL_ACCESS_TOKEN"]

STOCKS = {
    "0050 元大台灣50": "0050.TW",
    "2330 台積電": "2330.TW",
    "3711 日月光投控": "3711.TW",
    "QQQ": "QQQ",
    "台灣加權指數": "^TWII",
}

DAILY_THRESHOLD = -0.05
WEEK_THRESHOLD = -0.10

STATE_FILE = "alert_state.json"

TW_TZ = ZoneInfo("Asia/Taipei")


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


def load_state():
    if not os.path.exists(STATE_FILE):
        return {}

    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def save_state(state):
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(
            state,
            f,
            ensure_ascii=False,
            indent=2
        )


def get_history(symbol):
    """
    抓取最近約兩週的日線資料。
    用於：
    1. 前一交易日收盤價
    2. 過去7個曆日內的最高價
    """

    end = datetime.now(TW_TZ)
    start = end - timedelta(days=14)

    data = yf.download(
        symbol,
        start=start.strftime("%Y-%m-%d"),
        end=(end + timedelta(days=1)).strftime("%Y-%m-%d"),
        interval="1d",
        auto_adjust=False,
        progress=False
    )

    if data.empty:
        return None

    # yfinance 新版有時會回傳 MultiIndex
    if hasattr(data.columns, "levels"):
        try:
            close = data["Close"]

            if hasattr(close, "columns"):
                close = close.iloc[:, 0]

        except Exception:
            close = data.iloc[:, 0]
    else:
        close = data["Close"]

    close = close.dropna()

    if len(close) < 2:
        return None

    return close


def get_latest_price(symbol):
    """
    嘗試取得最新價格。
    若盤中資料不可用，退回最近可取得價格。
    """

    ticker = yf.Ticker(symbol)

    try:
        intraday = ticker.history(
            period="1d",
            interval="1m",
            prepost=False,
            auto_adjust=False
        )

        if not intraday.empty:
            price = intraday["Close"].dropna()

            if len(price) > 0:
                return float(price.iloc[-1])

    except Exception as e:
        print(f"{symbol} 1m資料失敗: {e}")

    # 退回最近日線價格
    try:
        daily = ticker.history(
            period="5d",
            interval="1d",
            auto_adjust=False
        )

        if not daily.empty:
            price = daily["Close"].dropna()

            if len(price) > 0:
                return float(price.iloc[-1])

    except Exception as e:
        print(f"{symbol} 日線資料失敗: {e}")

    return None


def get_previous_close(history):
    """
    最後一筆日線可能就是今天已經形成的資料，
    所以取倒數第二筆作為前一交易日收盤。
    """

    closes = history.dropna()

    if len(closes) < 2:
        return None

    return float(closes.iloc[-2])


def get_week_high(symbol, history):
    """
    使用過去7個曆日內的日線最高價。

    注意：
    這裡使用的是「最高價 High」，
    因為你的原始需求是：
    今天價格與一週內曾經出現的價格比較。
    """

    end = datetime.now(TW_TZ)
    start = end - timedelta(days=7)

    ticker = yf.Ticker(symbol)

    try:
        data = ticker.history(
            start=start.strftime("%Y-%m-%d"),
            end=(end + timedelta(days=1)).strftime("%Y-%m-%d"),
            interval="1d",
            auto_adjust=False
        )

        if not data.empty:
            highs = data["High"].dropna()

            if len(highs) > 0:
                return float(highs.max())

    except Exception as e:
        print(f"{symbol} 7日最高價取得失敗: {e}")

    return None


def format_price(price):
    if price >= 1000:
        return f"{price:,.0f}"

    if price >= 100:
        return f"{price:,.2f}"

    return f"{price:,.2f}"


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
        print("無法取得前一交易日收盤價")
        return

    week_high = get_week_high(symbol, history)

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

    now = datetime.now(TW_TZ)
    today = now.strftime("%Y-%m-%d")

    if name not in state:
        state[name] = {
            "daily_alert": False,
            "weekly_alert": False,
            "date": today
        }

    # 新的一天
    if state[name].get("date") != today:

        state[name]["daily_alert"] = False
        state[name]["date"] = today

    # ==========================
    # 單日跌5%
    # ==========================

    if daily_change <= DAILY_THRESHOLD:

        if not state[name]["daily_alert"]:

            message = (
                "🔴 股票跌幅警報\n\n"
                f"標的：{name}\n"
                f"目前價格：{format_price(current)}\n"
                f"前一交易日收盤：{format_price(previous_close)}\n"
                f"單日跌幅：{daily_change:.2%}\n\n"
                "⚠️ 已達到單日 -5% 警戒"
            )

            send_line(message)

            state[name]["daily_alert"] = True

            print("已發送：單日 -5%")

    # ==========================
    # 一週跌10%
    # ==========================

    if weekly_change <= WEEK_THRESHOLD:

        if not state[name]["weekly_alert"]:

            message = (
                "🔴 一週跌幅警報\n\n"
                f"標的：{name}\n"
                f"目前價格：{format_price(current)}\n"
                f"過去7日最高價：{format_price(week_high)}\n"
                f"距7日高點：{weekly_change:.2%}\n\n"
                "⚠️ 已達到一週 -10% 警戒"
            )

            send_line(message)

            state[name]["weekly_alert"] = True

            print("已發送：一週 -10%")

    # ==========================
    # 離開警戒區後解除鎖定
    # ==========================

    if daily_change > DAILY_THRESHOLD:
        state[name]["daily_alert"] = False

    if weekly_change > WEEK_THRESHOLD:
        state[name]["weekly_alert"] = False


def main():

    print("================================")
    print("股票跌幅 LINE 警報")
    print("================================")

    state = load_state()

    for name, symbol in STOCKS.items():

        try:
            check_stock(name, symbol, state)

        except Exception as e:

            print(f"{name} 發生錯誤：{e}")

    save_state(state)

    print("\n全部檢查完成")


if __name__ == "__main__":
    main()
