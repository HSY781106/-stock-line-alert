# ============================================================
# stock_alert.py V2.6
# 股票跌幅 + 15分鐘區間最低價 + 動態產業估值 + 技術 + 籌碼
#
# V2.6 重點
# 1. TWSE + TPEx 動態股票池
# 2. TPEx API 改用目前官方 OpenAPI endpoint，並提供多層 fallback
# 3. API 失敗時優先使用快取；不因 TPEx 失敗而讓整體股票池變 0
# 4. 各產業依即時市值排序，動態取 Top 10，同業不寫死
# 5. PE 歷史只寫入「該日期官方 PE」，絕不拿今天 PE 回填過去
# 6. PE 歷史不足 60 筆不啟用一年平均 PE 評分
# 7. 15 分鐘改為「上次執行 → 本次執行」區間最低價
# 8. LINE 可輸入股票代號、名稱、Yahoo symbol
# 9. TWSE / TPEx 法人與融資資料分市場取得
# 10. 單一股票 Yahoo 404 不影響整體程式
# ============================================================

import os
import json
import time
import math
import traceback
from datetime import datetime, timedelta, date
from zoneinfo import ZoneInfo

import requests
import pandas as pd
import numpy as np
import yfinance as yf

# ============================================================
# 基本設定
# ============================================================

LINE_TOKEN = os.environ.get("LINE_CHANNEL_ACCESS_TOKEN", "")

TWSE_BASE = "https://openapi.twse.com.tw/v1"
TWSE_WEB_BASE = "https://www.twse.com.tw/rwd/zh"
TPEX_BASE = "https://www.tpex.org.tw/openapi/v1"

STATE_FILE = "alert_state.json"
PE_HISTORY_FILE = "pe_history.json"
CHIP_HISTORY_FILE = "chip_history.json"
UNIVERSE_CACHE_FILE = "market_universe_cache.json"

LINE_REPLY_URL = "https://api.line.me/v2/bot/message/reply"
LINE_BROADCAST_URL = "https://api.line.me/v2/bot/message/broadcast"

TW_TZ = ZoneInfo("Asia/Taipei")

DAILY_THRESHOLD = -0.05
WEEK_THRESHOLD = -0.10
PE_MIN_HISTORY = 60
PE_MAX_VALID = 200
STRONG_SCORE = 8
GOOD_SCORE = 6
UNIVERSE_CACHE_HOURS = 24
TPEX_CACHE_HOURS = 24
TWSE_TIMEOUT = 20
TPEX_TIMEOUT = 25
YF_TIMEOUT = 20
T86_RETRIES = 2
API_SLEEP = 0.12
PE_BACKFILL_MAX_DAYS = 500

# 自動跌幅監控標的；LINE 單股查詢不受此限制
STOCKS = {
    "0050 元大台灣50": "0050.TW",
    "2330 台積電": "2330.TW",
    "3711 日月光投控": "3711.TW",
    "QQQ": "QQQ",
    "台灣加權指數": "^TWII",
}

# ============================================================
# 產業模型
# ============================================================

INDUSTRY_MODEL = {
    "半導體業": {"pe": True, "peg": True, "pb": True, "yield": False, "roe": True},
    "電腦及週邊設備業": {"pe": True, "peg": True, "pb": True, "yield": False, "roe": True},
    "電子零組件業": {"pe": True, "peg": True, "pb": True, "yield": False, "roe": True},
    "電子通路業": {"pe": True, "peg": True, "pb": True, "yield": False, "roe": True},
    "其他電子業": {"pe": True, "peg": True, "pb": True, "yield": False, "roe": True},
    "光電業": {"pe": True, "peg": True, "pb": True, "yield": False, "roe": True},
    "通信網路業": {"pe": True, "peg": False, "pb": True, "yield": True, "roe": True},
    "資訊服務業": {"pe": True, "peg": True, "pb": True, "yield": False, "roe": True},
    "金融業": {"pe": False, "peg": False, "pb": True, "yield": True, "roe": True},
    "銀行業": {"pe": False, "peg": False, "pb": True, "yield": True, "roe": True},
    "保險業": {"pe": False, "peg": False, "pb": True, "yield": True, "roe": True},
    "食品工業": {"pe": True, "peg": False, "pb": True, "yield": True, "roe": True},
    "塑膠工業": {"pe": True, "peg": False, "pb": True, "yield": True, "roe": True},
    "紡織纖維": {"pe": True, "peg": False, "pb": True, "yield": True, "roe": True},
    "電機機械": {"pe": True, "peg": True, "pb": True, "yield": False, "roe": True},
    "鋼鐵工業": {"pe": True, "peg": False, "pb": True, "yield": True, "roe": True},
    "建材營造": {"pe": True, "peg": False, "pb": True, "yield": True, "roe": True},
    "航運業": {"pe": True, "peg": False, "pb": True, "yield": True, "roe": True},
    "觀光餐旅": {"pe": True, "peg": False, "pb": True, "yield": True, "roe": True},
    "化學工業": {"pe": True, "peg": False, "pb": True, "yield": True, "roe": True},
    "生技醫療": {"pe": True, "peg": True, "pb": True, "yield": False, "roe": True},
    "醫療保健": {"pe": True, "peg": True, "pb": True, "yield": False, "roe": True},
    "汽車工業": {"pe": True, "peg": False, "pb": True, "yield": True, "roe": True},
    "橡膠工業": {"pe": True, "peg": False, "pb": True, "yield": True, "roe": True},
    "其他": {"pe": True, "peg": False, "pb": True, "yield": True, "roe": True},
}

DEFAULT_MODEL = {"pe": True, "peg": False, "pb": True, "yield": False, "roe": True}

# 台灣證券交易所常見產業代碼；不同 API 版本可能直接給中文名稱，因此兩種都支援。
INDUSTRY_CODE_MAP = {
    "01": "水泥工業", "02": "食品工業", "03": "塑膠工業", "04": "紡織纖維",
    "05": "電機機械", "06": "電器電纜", "08": "玻璃陶瓷", "09": "造紙工業",
    "10": "鋼鐵工業", "11": "橡膠工業", "12": "汽車工業", "13": "電子工業",
    "14": "建材營造", "15": "航運業", "16": "觀光餐旅", "17": "金融業",
    "18": "貿易百貨", "19": "綜合", "20": "其他", "21": "化學工業",
    "22": "生技醫療", "23": "油電燃氣業", "24": "半導體業", "25": "電腦及週邊設備業",
    "26": "光電業", "27": "通信網路業", "28": "電子零組件業", "29": "電子通路業",
    "30": "資訊服務業", "31": "其他電子業", "32": "文化創意業", "33": "農業科技",
    "34": "電子商務", "35": "數位雲端", "36": "運動休閒", "37": "居家生活",
    "38": "綠能環保", "39": "數位經濟", "40": "其他",
}

# ============================================================
# 工具
# ============================================================

def to_float(value):
    if value is None:
        return None
    try:
        s = str(value).strip().replace(",", "").replace("%", "")
        if s in {"", "-", "--", "N/A", "nan", "NaN", "None", "null", "－", "…"}:
            return None
        return float(s)
    except Exception:
        return None


def first_value(row, names):
    if not isinstance(row, dict):
        return None
    for name in names:
        if name in row:
            value = row[name]
            if value not in (None, "", "-", "--", "－"):
                return value
    return None


def find_value(row, names):
    return to_float(first_value(row, names))


def clean_code(value):
    if value is None:
        return ""
    text = str(value).strip().upper()
    for suffix in (".TW", ".TWO"):
        if text.endswith(suffix):
            text = text[:-len(suffix)]
    return text.strip()


def normalize_name(value):
    return str(value or "").strip().replace(" ", "").replace("　", "").lower()


def format_number(value, digits=2):
    if value is None or (isinstance(value, float) and math.isnan(value)):
        return "N/A"
    return f"{float(value):,.{digits}f}"


def safe_div(a, b):
    try:
        if a is None or b in (None, 0):
            return None
        return a / b
    except Exception:
        return None


def canonical_industry(value):
    s = str(value or "").strip()
    if not s:
        return "其他"
    code = s.zfill(2) if s.isdigit() else s
    if code in INDUSTRY_CODE_MAP:
        s = INDUSTRY_CODE_MAP[code]
    aliases = {
        "電子工業": "其他電子業",
        "電信業": "通信網路業",
        "通信網路": "通信網路業",
        "電腦及週邊": "電腦及週邊設備業",
        "電腦及週邊設備": "電腦及週邊設備業",
        "生技醫療業": "生技醫療",
        "醫療保健業": "醫療保健",
        "觀光事業": "觀光餐旅",
    }
    return aliases.get(s, s)


def symbol_for(code, market=None):
    code = clean_code(code)
    if market == "TWSE":
        return f"{code}.TW"
    if market in {"TPEX", "TPEx"}:
        return f"{code}.TWO"
    return code

# ============================================================
# HTTP
# ============================================================

def http_json(url, params=None, timeout=20, retries=2):
    last_error = None
    headers = {"User-Agent": "Mozilla/5.0 stock-alert/2.6"}
    for attempt in range(retries + 1):
        try:
            r = requests.get(url, params=params, timeout=timeout, headers=headers)
            r.raise_for_status()
            data = r.json()
            if data is not None:
                return data
        except Exception as e:
            last_error = e
            if attempt < retries:
                time.sleep(0.8 * (attempt + 1))
    print(f"API失敗：{url} / {last_error}")
    return None


def twse_get(endpoint, params=None):
    return http_json(TWSE_BASE + endpoint, params, TWSE_TIMEOUT, 2)


def twse_web_get(endpoint, params=None):
    return http_json(TWSE_WEB_BASE + endpoint, params, TWSE_TIMEOUT, 2)


def tpex_get(endpoint, params=None):
    return http_json(TPEX_BASE + endpoint, params, TPEX_TIMEOUT, 2)

# ============================================================
# LINE
# ============================================================

def send_line(message):
    if not LINE_TOKEN:
        print("LINE_TOKEN 未設定，略過 LINE 廣播")
        return False
    try:
        r = requests.post(
            LINE_BROADCAST_URL,
            headers={"Authorization": f"Bearer {LINE_TOKEN}", "Content-Type": "application/json"},
            json={"messages": [{"type": "text", "text": str(message)[:5000]}]},
            timeout=20,
        )
        if r.status_code != 200:
            print("LINE廣播失敗：", r.status_code, r.text)
            return False
        return True
    except Exception as e:
        print("LINE廣播失敗：", e)
        return False


def reply_line(reply_token, message):
    if not LINE_TOKEN or not reply_token:
        return False
    try:
        r = requests.post(
            LINE_REPLY_URL,
            headers={"Authorization": f"Bearer {LINE_TOKEN}", "Content-Type": "application/json"},
            json={"replyToken": reply_token, "messages": [{"type": "text", "text": str(message)[:5000]}]},
            timeout=20,
        )
        if r.status_code != 200:
            print("LINE回覆失敗：", r.status_code, r.text)
            return False
        return True
    except Exception as e:
        print("LINE reply失敗：", e)
        return False

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
# TWSE Universe
# ============================================================

def normalize_twse_profile(row):
    code = clean_code(first_value(row, ["公司代號", "證券代號", "Code", "SecuritiesCompanyCode"]))
    name = first_value(row, ["公司簡稱", "公司名稱", "證券名稱", "CompanyAbbreviation", "CompanyName"]) or ""
    industry = first_value(row, ["產業類別", "產業別", "Industry", "SecuritiesIndustryCode"]) or ""
    capital = find_value(row, ["實收資本額", "實收資本額(元)", "PaidinCapital", "Capital", "Capitals"])
    if not code or not code.isdigit():
        return None
    return {
        "code": code,
        "name": str(name).strip(),
        "industry": canonical_industry(industry),
        "market": "TWSE",
        "symbol": symbol_for(code, "TWSE"),
        "capital": capital,
    }


def get_twse_universe():
    data = twse_get("/opendata/t187ap03_L")
    result = []
    if isinstance(data, list):
        for row in data:
            item = normalize_twse_profile(row)
            if item:
                result.append(item)
    print(f"TWSE 基本資料：{len(result)}")
    return result


def get_twse_daily_quotes():
    result = {}
    data = twse_get("/exchangeReport/STOCK_DAY_ALL")
    if isinstance(data, list):
        for row in data:
            code = clean_code(first_value(row, ["Code", "證券代號"]))
            close = find_value(row, ["ClosingPrice", "收盤價"])
            if code and close is not None:
                result[code] = {
                    "close": close,
                    "open": find_value(row, ["OpeningPrice", "開盤價"]),
                    "high": find_value(row, ["HighestPrice", "最高價"]),
                    "low": find_value(row, ["LowestPrice", "最低價"]),
                    "change": find_value(row, ["Change", "漲跌價差"]),
                    "volume": find_value(row, ["TradeVolume", "成交股數"]),
                }
    print(f"TWSE 當日行情：{len(result)}")
    return result

# ============================================================
# TPEx Universe — V2.6 核心修正
# ============================================================

def normalize_tpex_profile(row):
    # V2.5 的 bug：只解析中文欄位；目前 TPEx company profile 主要回傳英文欄位。
    code = clean_code(first_value(row, [
        "SecuritiesCompanyCode", "證券代號", "公司代號", "Code"
    ]))
    name = first_value(row, [
        "CompanyAbbreviation", "CompanyName", "證券名稱", "公司簡稱", "公司名稱"
    ]) or ""
    industry = first_value(row, [
        "SecuritiesIndustryCode", "產業類別", "產業別", "Industry"
    ]) or ""
    capital = find_value(row, [
        "PaidinCapital", "實收資本額", "實收資本額(元)", "Capital", "Capitals"
    ])
    if not code or not code.isdigit():
        return None
    return {
        "code": code,
        "name": str(name).strip(),
        "industry": canonical_industry(industry),
        "market": "TPEX",
        "symbol": symbol_for(code, "TPEX"),
        "capital": capital,
    }


def get_tpex_universe_primary():
    data = tpex_get("/mopsfin_t187ap03_O")
    result = []
    if isinstance(data, list):
        for row in data:
            item = normalize_tpex_profile(row)
            if item:
                result.append(item)
    return result


def get_tpex_universe_fallback():
    # 第二層 fallback：TPEx 現行每日收盤 endpoint 本身含代號、名稱、資本額等欄位。
    data = tpex_get("/tpex_mainboard_daily_close_quotes")
    result = []
    if not isinstance(data, list):
        return result
    for row in data:
        code = clean_code(first_value(row, ["SecuritiesCompanyCode", "證券代號", "Code"]))
        name = first_value(row, ["CompanyName", "CompanyAbbreviation", "證券名稱"]) or ""
        capital = find_value(row, ["Capitals", "Capital", "PaidinCapital"])
        if code and code.isdigit():
            result.append({
                "code": code,
                "name": str(name).strip(),
                "industry": "其他",
                "market": "TPEX",
                "symbol": symbol_for(code, "TPEX"),
                "capital": capital,
            })
    return result


def get_tpex_daily_quotes():
    result = {}
    data = tpex_get("/tpex_mainboard_daily_close_quotes")
    if isinstance(data, list):
        for row in data:
            code = clean_code(first_value(row, ["SecuritiesCompanyCode", "Code"]))
            close = find_value(row, ["Close", "ClosingPrice"])
            if code and close is not None:
                result[code] = {
                    "close": close,
                    "open": find_value(row, ["Open", "OpeningPrice"]),
                    "high": find_value(row, ["High", "HighestPrice"]),
                    "low": find_value(row, ["Low", "LowestPrice"]),
                    "change": find_value(row, ["Change", "PriceChange"]),
                    "volume": find_value(row, ["TradingShares", "TradeVolume"]),
                    "capital": find_value(row, ["Capitals", "Capital"]),
                }
    print(f"TPEx 當日行情：{len(result)}")
    return result


def get_tpex_market_values():
    # V2.6：tpex_daily_market_value 是歷史排行 endpoint，直接取最新資料可能受日期參數/回傳格式影響。
    # 因此先嘗試官方排行，再以每日行情的資本額 × 收盤價作 fallback。
    result = {}
    data = tpex_get("/tpex_daily_market_value")
    if isinstance(data, list):
        for row in data:
            code = clean_code(first_value(row, ["SecuritiesCompanyCode", "證券代號", "Code", "代號"]))
            cap = find_value(row, ["MarketValue", "market_value", "市值", "總市值", "MarketCap"])
            if code and cap is not None:
                result[code] = cap
    print(f"TPEx 官方市值資料：{len(result)}")
    return result

# ============================================================
# Universe 建立與快取
# ============================================================

def estimate_market_cap(capital, price):
    if capital is None or price is None or price <= 0:
        return None
    # TWSE/TPEx 公開資料常見實收資本額以元表示；面額 10 元時股數 = capital / 10。
    # 若 API 已給市場值，優先使用市場值；此函式只做 fallback。
    shares = capital / 10.0
    return shares * price


def build_market_universe():
    print("\n========== 建立動態市場股票池 V2.6 ==========")

    twse = get_twse_universe()
    tpex = get_tpex_universe_primary()

    if not tpex:
        print("⚠️ TPEx 基本資料 endpoint 無有效資料，啟用每日行情 fallback")
        tpex = get_tpex_universe_fallback()

    print(f"TPEx 基本資料：{len(tpex)}")

    twse_quotes = get_twse_daily_quotes()
    tpex_quotes = get_tpex_daily_quotes()
    tpex_values = get_tpex_market_values()

    universe = {}

    for item in twse:
        code = item["code"]
        q = twse_quotes.get(code, {})
        price = q.get("close")
        item = dict(item)
        item["price"] = price
        item["market_cap"] = estimate_market_cap(item.get("capital"), price)
        item["industry"] = canonical_industry(item.get("industry"))
        universe[code] = item

    for item in tpex:
        code = item["code"]
        q = tpex_quotes.get(code, {})
        price = q.get("close")
        item = dict(item)
        item["price"] = price
        # 官方市值 > 由資本額估算 > None
        item["market_cap"] = tpex_values.get(code)
        if item["market_cap"] is None:
            item["market_cap"] = estimate_market_cap(item.get("capital") or q.get("capital"), price)
        item["industry"] = canonical_industry(item.get("industry"))
        universe[code] = item

    # 不再因為某個市場 API 失敗就整個 universe 歸零。
    valid = {}
    for code, item in universe.items():
        if not item.get("name"):
            continue
        if not item.get("industry"):
            item["industry"] = "其他"
        # 市值可以暫缺；LINE 單股分析仍可工作。
        valid[code] = item

    print(f"有效動態股票：{len(valid)}")
    print(f"其中 TWSE：{sum(1 for x in valid.values() if x.get('market') == 'TWSE')}")
    print(f"其中 TPEx：{sum(1 for x in valid.values() if x.get('market') == 'TPEX')}")
    return valid


def get_market_universe(force_refresh=False):
    cache = load_json(UNIVERSE_CACHE_FILE)
    cached_at = cache.get("_cached_at")
    cached_data = cache.get("data")
    now = time.time()

    if not force_refresh and cached_at and isinstance(cached_data, dict):
        if now - float(cached_at) < UNIVERSE_CACHE_HOURS * 3600:
            print(f"使用市場股票池快取：{len(cached_data)}")
            return cached_data

    universe = build_market_universe()
    if universe:
        save_json(UNIVERSE_CACHE_FILE, {"_cached_at": now, "data": universe})
        return universe

    if isinstance(cached_data, dict) and cached_data:
        print(f"⚠️ 市場資料更新失敗，使用舊快取：{len(cached_data)}")
        return cached_data

    print("⚠️ 沒有新資料，也沒有舊快取；建立最小 fallback universe")
    fallback = {}
    for label, symbol in STOCKS.items():
        code = clean_code(symbol)
        if code.isdigit():
            fallback[code] = {
                "code": code,
                "name": label,
                "industry": "其他",
                "market": "TWSE",
                "symbol": symbol,
                "capital": None,
                "price": None,
                "market_cap": None,
            }
    return fallback

# ============================================================
# 動態同業 Top 10
# ============================================================

def get_dynamic_industry_peers(code, industry, universe, limit=10):
    target = canonical_industry(industry)
    peers = []
    for c, item in universe.items():
        if c == code:
            continue
        if canonical_industry(item.get("industry")) != target:
            continue
        cap = to_float(item.get("market_cap"))
        if cap is None:
            continue
        peers.append(item)
    peers.sort(key=lambda x: x.get("market_cap", 0), reverse=True)
    return peers[:limit]

# ============================================================
# 股票搜尋
# ============================================================

def resolve_stock(query, universe):
    q = str(query or "").strip()
    if not q:
        return None

    code = clean_code(q)
    if code in universe:
        return universe[code]

    nq = normalize_name(q)
    exact = [item for item in universe.values() if normalize_name(item.get("name")) == nq]
    if len(exact) == 1:
        return exact[0]

    partial = [item for item in universe.values() if nq and nq in normalize_name(item.get("name"))]
    if len(partial) == 1:
        return partial[0]

    uq = q.upper()
    for item in universe.values():
        if str(item.get("symbol", "")).upper() == uq:
            return item

    # ETF / 指數等不在公司 universe 的查詢，直接交給 Yahoo。
    return None

# ============================================================
# Yahoo / 價格
# ============================================================

def yf_download(symbol, period="1y", interval="1d"):
    try:
        df = yf.download(symbol, period=period, interval=interval, progress=False, auto_adjust=False, threads=False)
        if df is None or df.empty:
            return None
        if isinstance(df.columns, pd.MultiIndex):
            df.columns = [c[0] for c in df.columns]
        return df
    except Exception as e:
        print(f"Yahoo download失敗 {symbol}: {e}")
        return None


def get_latest_price(symbol):
    try:
        ticker = yf.Ticker(symbol)
        hist = ticker.history(period="2d", interval="1d", auto_adjust=False)
        if hist is not None and not hist.empty:
            return float(hist["Close"].dropna().iloc[-1])
    except Exception as e:
        print(f"Yahoo latest price失敗 {symbol}: {e}")
    return None


def get_previous_close(symbol):
    df = yf_download(symbol, period="10d", interval="1d")
    if df is None or "Close" not in df.columns:
        return None
    close = pd.to_numeric(df["Close"], errors="coerce").dropna()
    if len(close) < 2:
        return None
    return float(close.iloc[-2])


def get_week_high(symbol):
    df = yf_download(symbol, period="10d", interval="1d")
    if df is None or "High" not in df.columns:
        return None
    high = pd.to_numeric(df["High"], errors="coerce").dropna()
    if high.empty:
        return None
    return float(high.tail(7).max())

# ============================================================
# 15分鐘區間最低價
# ============================================================

def parse_time(value):
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(value)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=TW_TZ)
        return dt
    except Exception:
        return None


def get_interval_low(symbol, start_time):
    try:
        now = datetime.now(TW_TZ)
        if start_time is None:
            start = now - timedelta(minutes=15)
        else:
            start = parse_time(start_time)
            if start is None:
                start = now - timedelta(minutes=15)
        if start > now:
            start = now - timedelta(minutes=15)
        minutes = max(15, int((now - start).total_seconds() / 60) + 5)
        # Yahoo intraday通常只提供有限歷史；對長時間間隔自動擴大到 1d。
        period = "1d" if minutes <= 1440 else "5d"
        df = yf_download(symbol, period=period, interval="1m")
        if df is None or df.empty or "Low" not in df.columns:
            return None
        idx = pd.to_datetime(df.index)
        if idx.tz is None:
            idx = idx.tz_localize("UTC").tz_convert(TW_TZ)
        else:
            idx = idx.tz_convert(TW_TZ)
        df = df.copy()
        df.index = idx
        lows = pd.to_numeric(df["Low"], errors="coerce")
        mask = (df.index >= start) & (df.index <= now)
        lows = lows.loc[mask].dropna()
        if lows.empty:
            return None
        return float(lows.min())
    except Exception as e:
        print(f"{symbol} 15分鐘區間資料失敗：{e}")
        return None


def check_interval_low(name, symbol, state):
    now = datetime.now(TW_TZ)
    now_iso = now.isoformat()
    block = state.setdefault("interval_low", {})
    stock = block.setdefault(name, {})
    previous_time = stock.get("last_check")
    current_price = get_latest_price(symbol)

    if current_price is None:
        stock["last_check"] = now_iso
        return None

    interval_low = get_interval_low(symbol, previous_time)
    previous_price = to_float(stock.get("last_price"))

    result = None
    if previous_time is not None and interval_low is not None and previous_price:
        if interval_low < previous_price:
            drop = (interval_low / previous_price) - 1
            result = {
                "previous_price": previous_price,
                "interval_low": interval_low,
                "drop": drop,
                "start": previous_time,
                "end": now_iso,
            }
            # 避免同一輪重複通知；僅在區間最低價真的跌破上次價格時通知。
            if drop <= DAILY_THRESHOLD:
                send_line(
                    "🔴 15分鐘區間低點通知\n\n"
                    f"標的：{name}\n"
                    f"上次執行價格：{previous_price:,.2f}\n"
                    f"本次區間最低：{interval_low:,.2f}\n"
                    f"區間跌幅：{drop:.2%}\n\n"
                    "⚠️ 此次比較的是「上次執行 → 本次執行」期間最低價。"
                )

    stock["last_check"] = now_iso
    stock["last_price"] = current_price
    stock["last_interval_low"] = interval_low
    return result

# ============================================================
# 跌幅通知
# ============================================================

def check_drop_alert(name, symbol, state):
    current = get_latest_price(symbol)
    previous_close = get_previous_close(symbol)
    week_high = get_week_high(symbol)
    if current is None or previous_close is None:
        return

    daily_change = current / previous_close - 1
    weekly_change = current / week_high - 1 if week_high else None
    today = datetime.now(TW_TZ).strftime("%Y-%m-%d")
    stock_state = state.setdefault("drop_alert", {}).setdefault(name, {})
    if stock_state.get("date") != today:
        stock_state.update({"date": today, "daily_alert": False, "weekly_alert": False})

    if daily_change <= DAILY_THRESHOLD and not stock_state.get("daily_alert"):
        send_line(
            "🔴 跌幅通知\n\n"
            f"標的：{name}\n"
            f"目前價格：{current:,.2f}\n"
            f"前一交易日收盤：{previous_close:,.2f}\n"
            f"單日跌幅：{daily_change:.2%}\n\n"
            "⚠️ 已達到單日 -5%，可進一步評估。"
        )
        stock_state["daily_alert"] = True
    elif daily_change > DAILY_THRESHOLD:
        stock_state["daily_alert"] = False

    if weekly_change is not None and weekly_change <= WEEK_THRESHOLD and not stock_state.get("weekly_alert"):
        send_line(
            "🔴 一週跌幅通知\n\n"
            f"標的：{name}\n"
            f"目前價格：{current:,.2f}\n"
            f"過去7日高點：{week_high:,.2f}\n"
            f"距7日高點跌幅：{weekly_change:.2%}\n\n"
            "⚠️ 已達到一週 -10%，可進一步評估。"
        )
        stock_state["weekly_alert"] = True
    elif weekly_change is not None and weekly_change > WEEK_THRESHOLD:
        stock_state["weekly_alert"] = False

# ============================================================
# PE 歷史
# ============================================================

def parse_twse_pe_response(data):
    result = {}
    if not isinstance(data, dict):
        return result
    fields = data.get("fields", [])
    rows = data.get("data", [])
    if not isinstance(rows, list):
        return result
    for row in rows:
        if not isinstance(row, list):
            continue
        obj = dict(zip(fields, row)) if fields else {}
        code = clean_code(first_value(obj, ["證券代號", "公司代號"]))
        pe = find_value(obj, ["本益比", "本益比(益) ", "PERatio", "PE"])
        pb = find_value(obj, ["股價淨值比", "PBR", "PriceBookRatio"])
        yld = find_value(obj, ["殖利率(%)", "殖利率", "DividendYield"])
        if code:
            result[code] = {"pe": pe, "pb": pb, "yield": yld}
    return result


def get_twse_pe_by_date(date_string):
    data = twse_web_get(
        "/afterTrading/BWIBBU_ALL",
        {"date": date_string, "response": "json"},
    )
    # 某些版本回傳格式為 table 陣列；兼容。
    result = parse_twse_pe_response(data)
    if result:
        return result
    if isinstance(data, dict):
        for table in data.get("data", []):
            if isinstance(table, list) and table and isinstance(table[0], list):
                fields = table[0]
                for row in table[1:]:
                    if isinstance(row, list):
                        obj = dict(zip(fields, row))
                        code = clean_code(first_value(obj, ["證券代號", "公司代號"]))
                        pe = find_value(obj, ["本益比"])
                        pb = find_value(obj, ["股價淨值比"])
                        yld = find_value(obj, ["殖利率(%)", "殖利率"])
                        if code:
                            result[code] = {"pe": pe, "pb": pb, "yield": yld}
    return result


def get_current_pe_data(universe):
    result = {}
    # TWSE：官方 BWIBBU_ALL
    data = twse_get("/exchangeReport/BWIBBU_ALL")
    if isinstance(data, list):
        for row in data:
            code = clean_code(first_value(row, ["Code", "證券代號"]))
            if not code:
                continue
            result[code] = {
                "pe": find_value(row, ["PEratio", "PERatio", "本益比"]),
                "pb": find_value(row, ["PBratio", "PBR", "股價淨值比"]),
                "yield": find_value(row, ["DividendYield", "殖利率(%)", "殖利率"]),
            }

    # TPEx：官方個股本益比/殖利率/PBR
    tpex = tpex_get("/tpex_mainboard_peratio_analysis")
    if isinstance(tpex, list):
        for row in tpex:
            code = clean_code(first_value(row, ["SecuritiesCompanyCode", "Code"]))
            if not code:
                continue
            result[code] = {
                "pe": find_value(row, ["PERatio", "PE", "本益比"]),
                "pb": find_value(row, ["PBRatio", "PBR", "股價淨值比"]),
                "yield": find_value(row, ["DividendYield", "殖利率"]),
            }
    return result


def backfill_pe_history(code, history):
    history.setdefault(code, {})
    current_count = sum(
        1 for v in history[code].values()
        if to_float(v) is not None and 0 < float(v) <= PE_MAX_VALID
    )
    if current_count >= PE_MIN_HISTORY:
        return history

    cursor = datetime.now(TW_TZ).date()
    checked = 0
    print(f"{code} PE歷史回補開始，目前 {current_count}/{PE_MIN_HISTORY}")

    while current_count < PE_MIN_HISTORY and checked < PE_BACKFILL_MAX_DAYS:
        if cursor.weekday() >= 5:
            cursor -= timedelta(days=1)
            checked += 1
            continue
        ds = cursor.strftime("%Y%m%d")
        if ds in history[code]:
            cursor -= timedelta(days=1)
            checked += 1
            continue

        pe_data = get_twse_pe_by_date(ds)
        pe = pe_data.get(code, {}).get("pe")
        if pe is not None and 0 < pe <= PE_MAX_VALID:
            history[code][ds] = float(pe)
            current_count += 1
            print(f"{code} 回補 {ds} PE={pe:.2f} ({current_count}/{PE_MIN_HISTORY})")
        cursor -= timedelta(days=1)
        checked += 1
        time.sleep(API_SLEEP)

    print(f"{code} PE回補完成：{current_count}筆")
    return history


def calculate_one_year_average_pe(code, history):
    values = []
    cutoff = datetime.now(TW_TZ).date() - timedelta(days=365)
    for ds, pe in history.get(code, {}).items():
        try:
            d = datetime.strptime(ds, "%Y%m%d").date()
        except Exception:
            continue
        if d < cutoff:
            continue
        pe = to_float(pe)
        if pe is not None and 0 < pe <= PE_MAX_VALID:
            values.append(pe)
    if len(values) < PE_MIN_HISTORY:
        return None, len(values)
    return sum(values) / len(values), len(values)


def calculate_taiex_market_pe():
    data = twse_get("/exchangeReport/BWIBBU_ALL")
    values = []
    if isinstance(data, list):
        for row in data:
            pe = find_value(row, ["PEratio", "PERatio", "本益比"])
            if pe is not None and 0 < pe <= PE_MAX_VALID:
                values.append(pe)
    if not values:
        return None
    return sum(values) / len(values)

# ============================================================
# 財務 / 估值
# ============================================================

def get_yahoo_fundamentals(symbol):
    out = {"pe": None, "pb": None, "yield": None, "eps_growth": None, "roe": None, "peg": None}
    try:
        t = yf.Ticker(symbol)
        info = t.info
        out["pe"] = to_float(info.get("trailingPE")) or to_float(info.get("forwardPE"))
        out["pb"] = to_float(info.get("priceToBook"))
        out["yield"] = (to_float(info.get("dividendYield")) or 0) * 100 if info.get("dividendYield") is not None else None
        out["eps_growth"] = (to_float(info.get("earningsGrowth")) or 0) * 100 if info.get("earningsGrowth") is not None else None
        out["roe"] = (to_float(info.get("returnOnEquity")) or 0) * 100 if info.get("returnOnEquity") is not None else None
        out["peg"] = to_float(info.get("pegRatio"))
    except Exception as e:
        print(f"Yahoo fundamentals 失敗 {symbol}: {e}")
    return out


def get_dynamic_industry_pe(code, industry, universe, current_pe_data):
    peers = get_dynamic_industry_peers(code, industry, universe, 10)
    vals = []
    for peer in peers:
        item = current_pe_data.get(peer["code"])
        pe = item.get("pe") if item else None
        if pe is not None and 0 < pe <= PE_MAX_VALID:
            vals.append(pe)
    if not vals:
        return None, peers
    return sum(vals) / len(vals), peers


def valuation_score(stock_pe, industry_pe, one_year_pe, stock_pb, stock_yield, earnings_growth, peg, roe, model):
    score = 0
    reasons = []

    def add(condition, positive, text):
        nonlocal score
        if condition:
            score += 1 if positive else 0
            reasons.append((text, positive))

    if model.get("pe"):
        add(stock_pe is not None and industry_pe is not None, stock_pe < industry_pe if stock_pe and industry_pe else False, "低於同業PE")
        add(stock_pe is not None and one_year_pe is not None, stock_pe < one_year_pe if stock_pe and one_year_pe else False, "低於一年平均PE")
    if model.get("pb"):
        add(stock_pb is not None, stock_pb < 2 if stock_pb is not None else False, "PB合理")
    if model.get("yield"):
        add(stock_yield is not None, stock_yield >= 3 if stock_yield is not None else False, "殖利率達3%")
    if model.get("peg"):
        add(peg is not None, peg < 1.5 if peg is not None else False, "PEG合理")
    if model.get("roe"):
        add(roe is not None, roe >= 10 if roe is not None else False, "ROE達10%")
    add(earnings_growth is not None, earnings_growth > 0 if earnings_growth is not None else False, "獲利成長")
    return score, reasons

# ============================================================
# 技術面
# ============================================================

def calculate_rsi(close, period=14):
    close = pd.to_numeric(close, errors="coerce").dropna()
    if len(close) < period + 1:
        return None
    delta = close.diff()
    gain = delta.clip(lower=0).rolling(period).mean()
    loss = (-delta.clip(upper=0)).rolling(period).mean()
    rs = gain / loss.replace(0, np.nan)
    rsi = 100 - (100 / (1 + rs))
    value = rsi.dropna()
    return float(value.iloc[-1]) if not value.empty else None


def calculate_kd(df):
    if df is None or len(df) < 20:
        return None, None
    high = pd.to_numeric(df["High"], errors="coerce")
    low = pd.to_numeric(df["Low"], errors="coerce")
    close = pd.to_numeric(df["Close"], errors="coerce")
    lowest = low.rolling(9).min()
    highest = high.rolling(9).max()
    rsv = (close - lowest) / (highest - lowest).replace(0, np.nan) * 100
    k = rsv.ewm(com=2, adjust=False).mean()
    d = k.ewm(com=2, adjust=False).mean()
    if k.dropna().empty or d.dropna().empty:
        return None, None
    return float(k.dropna().iloc[-1]), float(d.dropna().iloc[-1])


def get_technical(symbol):
    df = yf_download(symbol, period="6mo", interval="1d")
    if df is None or df.empty or not {"Close", "High", "Low"}.issubset(df.columns):
        return {"k": None, "d": None, "rsi": None}
    k, d = calculate_kd(df)
    return {"k": k, "d": d, "rsi": calculate_rsi(df["Close"])}

# ============================================================
# 籌碼：TWSE + TPEx
# ============================================================

def parse_t86_data(data):
    result = {}
    if not isinstance(data, dict):
        return result
    fields = data.get("fields", [])
    rows = data.get("data", [])
    for row in rows if isinstance(rows, list) else []:
        if not isinstance(row, list):
            continue
        obj = dict(zip(fields, row))
        code = clean_code(first_value(obj, ["證券代號", "公司代號"]))
        foreign = find_value(obj, ["外陸資買賣超股數(不含外資自營商)", "外資及陸資買賣超股數"])
        trust = find_value(obj, ["投信買賣超股數"])
        dealer = find_value(obj, ["自營商買賣超股數"])
        if code:
            result[code] = {"foreign": foreign, "trust": trust, "dealer": dealer,
                            "total": sum(x for x in [foreign, trust, dealer] if x is not None)}
    return result


def get_t86_data(date_string):
    for attempt in range(T86_RETRIES + 1):
        try:
            data = twse_web_get("/fund/T86", {"date": date_string, "selectType": "ALL", "response": "json"})
            result = parse_t86_data(data)
            if result:
                return result
        except Exception as e:
            print(f"T86 {date_string} 失敗：{e}")
        if attempt < T86_RETRIES:
            time.sleep(1)
    return {}


def parse_tpex_institutional(data):
    result = {}
    if not isinstance(data, list):
        return result
    for row in data:
        code = clean_code(first_value(row, ["SecuritiesCompanyCode", "Code"]))
        if not code:
            continue
        foreign_buy = find_value(row, [
            "Foreign Investors include Mainland Area Investors (Foreign Dealers excluded)-Total Buy",
            "ForeignInvestors-TotalBuy", "Foreign Buy"
        ])
        foreign_sell = find_value(row, [
            "Foreign Investors include Mainland Area Investors (Foreign Dealers excluded)-Total Sell",
            "ForeignInvestors-TotalSell", "Foreign Sell"
        ])
        trust_buy = find_value(row, ["SecuritiesInvestmentTrustCompanies-TotalBuy", "InvestmentTrust-TotalBuy"])
        trust_sell = find_value(row, ["SecuritiesInvestmentTrustCompanies-TotalSell", "InvestmentTrust-TotalSell"])
        dealer_buy = find_value(row, ["Dealers-TotalBuy", "Dealer-TotalBuy"])
        dealer_sell = find_value(row, ["Dealers-TotalSell", "Dealer-TotalSell"])
        vals = [
            safe_div(0, 1) if False else None,
            (foreign_buy - foreign_sell) if foreign_buy is not None and foreign_sell is not None else None,
            (trust_buy - trust_sell) if trust_buy is not None and trust_sell is not None else None,
            (dealer_buy - dealer_sell) if dealer_buy is not None and dealer_sell is not None else None,
        ]
        foreign, trust, dealer = vals[1], vals[2], vals[3]
        total = sum(x for x in [foreign, trust, dealer] if x is not None)
        result[code] = {"foreign": foreign, "trust": trust, "dealer": dealer, "total": total}
    return result


def get_institutional_data(code, market, days=20):
    result = []
    if market == "TPEX":
        data = tpex_get("/tpex_3insti_daily_trading")
        parsed = parse_tpex_institutional(data)
        if parsed:
            result.append({"date": datetime.now(TW_TZ).strftime("%Y%m%d"), "data": parsed})
        return result

    current = datetime.now(TW_TZ).date()
    for i in range(max(45, days * 2)):
        day = current - timedelta(days=i)
        if day.weekday() >= 5:
            continue
        ds = day.strftime("%Y%m%d")
        data = get_t86_data(ds)
        if data:
            result.append({"date": ds, "data": data})
        if len(result) >= days:
            break
        time.sleep(API_SLEEP)
    return result


def calculate_institutional_scores(code, history):
    vals = []
    for item in history:
        stock = item.get("data", {}).get(code)
        if stock and stock.get("total") is not None:
            vals.append(stock["total"])
    return {
        "5d": sum(vals[:5]) if len(vals) >= 5 else None,
        "20d": sum(vals[:20]) if len(vals) >= 20 else None,
        "latest": vals[0] if vals else None,
    }


def get_margin_change(symbol):
    # Yahoo沒有可靠的台股融資融券歷史；此處優先使用 TWSE/TPEx 官方 endpoint。
    code = clean_code(symbol)
    try:
        if code.isdigit():
            data = twse_get("/exchangeReport/MI_MARGN")
            if isinstance(data, dict):
                fields = data.get("fields", [])
                for table in data.get("data", []):
                    if isinstance(table, list):
                        for row in table:
                            if isinstance(row, list):
                                obj = dict(zip(fields, row))
                                if clean_code(first_value(obj, ["股票代號", "證券代號"])) == code:
                                    return find_value(obj, ["融資增減"])
    except Exception as e:
        print(f"融資資料失敗 {symbol}: {e}")
    return None

# ============================================================
# LINE 單股分析
# ============================================================

def line_single_stock_analysis(query, universe):
    item = resolve_stock(query, universe)
    if item is None:
        # 直接接受 Yahoo symbol，例如 QQQ、^TWII、2330.TW。
        q = str(query).strip()
        symbol = q.upper() if "." in q or q.startswith("^") else None
        if not symbol:
            return f"❌ 找不到股票：{query}\n\n請輸入股票代號或名稱，例如：2330、台積電、5347、世界。"
        item = {"code": clean_code(q), "name": q, "industry": "其他", "market": "TWSE", "symbol": symbol, "market_cap": None}

    code = item.get("code")
    name = item.get("name") or code
    industry = canonical_industry(item.get("industry"))
    symbol = item.get("symbol") or symbol_for(code, item.get("market"))
    market = item.get("market")

    print("\n================================")
    print(f"LINE 單股分析：{name} {symbol}")
    print("================================")

    current_pe_data = get_current_pe_data(universe)
    history = load_json(PE_HISTORY_FILE)
    if not isinstance(history, dict):
        history = {}
    history = backfill_pe_history(code, history)
    save_json(PE_HISTORY_FILE, history)

    yf_fund = get_yahoo_fundamentals(symbol)
    official = current_pe_data.get(code, {})
    stock_pe = official.get("pe") or yf_fund.get("pe")
    stock_pb = official.get("pb") or yf_fund.get("pb")
    stock_yield = official.get("yield") or yf_fund.get("yield")

    industry_pe, peers = get_dynamic_industry_pe(code, industry, universe, current_pe_data)
    one_year_pe, sample_count = calculate_one_year_average_pe(code, history)
    market_pe = calculate_taiex_market_pe() if market == "TWSE" else None

    tech = get_technical(symbol)
    inst_history = get_institutional_data(code, market, 20)
    inst = calculate_institutional_scores(code, inst_history)
    margin_change = get_margin_change(symbol)

    model = INDUSTRY_MODEL.get(industry, DEFAULT_MODEL)
    score, reasons = valuation_score(
        stock_pe, industry_pe, one_year_pe, stock_pb, stock_yield,
        yf_fund.get("eps_growth"), yf_fund.get("peg"), yf_fund.get("roe"), model
    )

    if score >= STRONG_SCORE:
        verdict = "🟢 偏適合加碼"
    elif score >= GOOD_SCORE:
        verdict = "🟡 可分批觀察"
    else:
        verdict = "🔴 暫不建議急著加碼"

    peer_text = "、".join(f"{p.get('code')} {p.get('name')}" for p in peers) if peers else "無法取得市值資料"
    warning = []
    k, d, rsi = tech.get("k"), tech.get("d"), tech.get("rsi")
    if k is not None and d is not None and k > 70 and d > 70:
        warning.append("KD高檔")
    if rsi is not None and rsi > 70:
        warning.append("RSI過熱")
    if inst.get("20d") is not None and inst["20d"] < 0:
        warning.append("法人20日賣超")
    if margin_change is not None and margin_change < 0:
        warning.append("融資下降")

    return (
        f"📊 股票加碼分析 V2.6\n\n"
        f"標的：{name}（{code}）\n"
        f"市場：{market}\n"
        f"產業：{industry}\n\n"
        f"【估值】\n"
        f"PE：{format_number(stock_pe)}\n"
        f"同業市值Top10平均PE：{format_number(industry_pe)}\n"
        f"一年平均PE：{format_number(one_year_pe)}（樣本 {sample_count}/{PE_MIN_HISTORY}）\n"
        f"TAIEX PE：{format_number(market_pe)}\n"
        f"PB：{format_number(stock_pb)}\n"
        f"殖利率：{format_number(stock_yield)}%\n"
        f"EPS成長：{format_number(yf_fund.get('eps_growth'))}%\n"
        f"PEG：{format_number(yf_fund.get('peg'))}\n"
        f"ROE：{format_number(yf_fund.get('roe'))}%\n\n"
        f"【動態同業 Top 10】\n{peer_text}\n\n"
        f"【技術】\n"
        f"KD：K={format_number(k)} / D={format_number(d)}\n"
        f"RSI：{format_number(rsi)}\n\n"
        f"【籌碼】\n"
        f"法人最新：{format_number(inst.get('latest'), 0)} 股\n"
        f"法人5日：{format_number(inst.get('5d'), 0)} 股\n"
        f"法人20日：{format_number(inst.get('20d'), 0)} 股\n"
        f"融資變化：{format_number(margin_change, 0)} 張\n\n"
        f"【評分】\n"
        f"估值/基本面：{score} 分\n"
        f"結論：{verdict}\n"
        f"風險提醒：{'、'.join(warning) if warning else '目前無主要技術/籌碼警訊'}\n\n"
        "※ V2.6 的 PE 歷史只採該交易日官方資料；不足60筆時，一年平均PE不進入評分。"
    )

# ============================================================
# Webhook
# ============================================================

def handle_line_webhook_event(event, universe):
    if event.get("type") != "message":
        return
    message = event.get("message", {})
    if message.get("type") != "text":
        return
    text = str(message.get("text", "")).strip()
    token = event.get("replyToken")
    if not text:
        return
    if text.lower() in {"help", "說明", "功能", "股票"}:
        reply_line(token,
            "📈 股票加碼分析 Bot V2.6\n\n"
            "直接輸入股票代號或名稱即可。\n\n"
            "例如：\n2330\n台積電\n5347\n世界\n\n"
            "Bot會自動：\n"
            "① 找股票\n② 判斷上市/上櫃\n③ 判斷產業\n"
            "④ 找該產業目前市值Top10同業\n⑤ 估值\n"
            "⑥ KD / RSI\n⑦ 三大法人\n⑧ 融資\n⑨ 回答是否適合加碼"
        )
        return
    try:
        result = line_single_stock_analysis(text, universe)
    except Exception as e:
        print("LINE分析錯誤：", e)
        traceback.print_exc()
        result = f"❌ 分析失敗：{e}"
    reply_line(token, result)


def run_webhook_server():
    try:
        from flask import Flask, request
    except ImportError:
        print("❌ 尚未安裝 Flask：pip install flask")
        return

    app = Flask(__name__)
    universe = get_market_universe()

    @app.route("/callback", methods=["POST"])
    def callback():
        body = request.get_json(silent=True) or {}
        for event in body.get("events", []):
            try:
                handle_line_webhook_event(event, universe)
            except Exception as e:
                print("Webhook錯誤：", e)
        return "OK", 200

    port = int(os.environ.get("PORT", "8080"))
    print("================================")
    print("LINE Webhook Server V2.6")
    print(f"Port：{port}")
    print("================================")
    app.run(host="0.0.0.0", port=port)

# ============================================================
# 主程式
# ============================================================

def run_alerts():
    print("================================")
    print("股票跌幅 + 15分鐘區間最低價 + V2.6自動估值 + 技術 + 籌碼")
    print("================================")

    state = load_json(STATE_FILE)
    universe = get_market_universe()

    if not universe:
        print("❌ 無法建立任何股票資料，程式結束")
        return

    for name, symbol in STOCKS.items():
        try:
            print(f"\n========== {name} ==========")
            check_drop_alert(name, symbol, state)
            check_interval_low(name, symbol, state)
        except Exception as e:
            print(f"{name} 分析失敗：{e}")
            traceback.print_exc()

    save_json(STATE_FILE, state)
    print("\n========== 完成 ==========")


def main():
    import sys
    if len(sys.argv) > 1 and sys.argv[1].lower() == "webhook":
        run_webhook_server()
    elif len(sys.argv) > 1 and sys.argv[1].lower() == "refresh":
        get_market_universe(force_refresh=True)
    elif len(sys.argv) > 1 and sys.argv[1].lower() == "analyze":
        universe = get_market_universe()
        query = " ".join(sys.argv[2:]).strip()
        if not query:
            print("用法：python stock_alert.py analyze 2330")
            return
        print(line_single_stock_analysis(query, universe))
    else:
        run_alerts()


if __name__ == "__main__":
    main()
