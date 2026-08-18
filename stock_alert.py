import os
import json
import requests
import yfinance as yf
from datetime import datetime, timedelta

# =========================
# 設定
# =========================

LINE_TOKEN = os.environ["LINE_CHANNEL_ACCESS_TOKEN"]

STOCKS = {
    "0050": "0050.TW",
    "2330": "2330.TW",
    "3711": "3711.TW",
    "QQQ": "QQQ",
    "台灣加權指數": "^TWII",
}

DAILY_DROP = -0.05
WEEK_DROP = -0.10

STATE_FILE = "alert_state.json"


# =========================
# LINE
# =========================

def send_line(message):
    url = "https://api.line.me/v2/bot/message/broadcast"

    headers = {
        "Authorization": f"Bearer {LINE_TOKEN}",
        "Content-Type": "application/json",
    }

    data = {
        "messages": [
            {
                "type": "text",
                "text": message
            }
        ]
    }

    response = requests.post(url, headers=headers, json=data)

    if response.status_code != 200:
        print("LINE error:", response.text)


# =========================
# 狀態
# =========================

def load_state():
    if not os.path.exists(STATE_FILE):
        return {}

    with open(STATE_FILE, "r", encoding="utf-8") as f:
        return json.load(f)


def save_state(state):
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)


# =========================
# 股價
# =========================

def get_data(symbol):

    ticker = yf.Ticker(symbol)

    end = datetime.now()
    start = end - timedelta(days=10)

    data = ticker.history(
        start=start,
        end=end,
        interval="1d",
        auto_adjust=False
    )

    if data.empty:
        return None

    return data


# =========================
# 判斷
# =========================

def check_stock(name, symbol, state):

    data = get_data(symbol)

    if data is None or len(data) < 2:
        print(f"{name}: 無法取得資料")
        return

    closes = data["Close"].dropna()

    current = float(closes.iloc[-1])
    previous = float(closes.iloc[-2])

    daily_drop = current / previous - 1

    # 過去7個曆日內可取得的最高收盤價
    week_data = closes.tail(7)

    week_high = float(week_data.max())

    week_drop = current / week_high - 1

    print(
        f"{name}: "
        f"現價={current:.2f}, "
        f"今日={daily_drop:.2%}, "
        f"7日高點={week_high:.2f}, "
        f"距高點={week_drop:.2%}"
    )

    today = datetime.now().strftime("%Y-%m-%d")

    # 初始化
    if name not in state:
        state[name] = {
            "daily": False,
            "weekly": False,
            "last_date": today
        }

    # 新的一天，重置單日警報
    if state[name].get("last_date") != today:
        state[name]["daily"] = False
        state[name]["last_date"] = today

    # -------------------------
    # 單日跌5%
    # -------------------------

    if daily_drop <= DAILY_DROP:

        if not state[name]["daily"]:

            message = (
                f"🔴 跌幅警報\n\n"
                f"{name}\n"
                f"現價：{current:.2f}\n"
                f"單日跌幅：{daily_drop:.2%}\n\n"
                f"⚠️ 已跌超過 5%"
            )

            send_line(message)

            state[name]["daily"] = True

    # -------------------------
    # 一週跌10%
    # -------------------------

    if week_drop <= WEEK_DROP:

        if not state[name]["weekly"]:

            message = (
                f"🔴 一週跌幅警報\n\n"
                f"{name}\n"
                f"現價：{current:.2f}\n"
                f"過去7日最高收盤：{week_high:.2f}\n"
                f"距高點跌幅：{week_drop:.2%}\n\n"
                f"⚠️ 已跌超過 10%"
            )

            send_line(message)

            state[name]["weekly"] = True

    # 如果價格回到警戒線以上，允許下一次重新觸發
    if daily_drop > DAILY_DROP:
        state[name]["daily"] = False

    if week_drop > WEEK_DROP:
        state[name]["weekly"] = False


# =========================
# 主程式
# =========================

def main():

    state = load_state()

    for name, symbol in STOCKS.items():

        try:
            check_stock(name, symbol, state)

        except Exception as e:

            print(f"{name} 發生錯誤：{e}")

    save_state(state)


if __name__ == "__main__":
    main()
