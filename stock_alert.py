# stock_alert.py V2.10.21
# 效能修正版：
# 1. 全市場資料批次化
# 2. 單次執行快取
# 3. 限制 Yahoo/API 重試
# 4. 15 分鐘資料僅抓目標股
# 5. 動態市場股票池
# 6. 動態次產業分類，不使用股票代碼硬編碼
# 7. 同次產業 Top 10 依目前市值動態排序
# 8. PE 同業比較改為「同次產業」
# 9. 使用 TPEx/TWSE 產業價值鏈公開資料取得次產業
# 10. 次產業按股票快取 30 天，避免每日大量請求
# 11. 保留原本基本面 / 技術 / 籌碼 / 風險 / LINE 功能
#
# V2.9.8
#
# 次產業資料來源：
# 證券交易所 / 櫃買中心「產業價值鏈資訊平台」公開資料
# https://ic.tpex.org.tw/company_chain.php
#
# 特色：
# - 不再使用 FinMind TaiwanStockIndustryChain
# - 不需要 FINMIND_API_TOKEN
# - 不使用股票代碼硬編碼
# - 次產業資料按股票快取 30 天
# - 只抓本次 STOCKS 目標股所在大產業的候選股票
# - 保留原本基本面 / 技術 / 籌碼 / 風險 / LINE 功能
#
# 股票跌幅 + 15分鐘區間最低價 + 動態估值 + 技術 + 籌碼 + 100分制加碼決策
# V2.10.21：1985檔全市場技術快取版 + LINE 輕量查詢專用分析路徑；保留 V2.10.19 全部分析功能
#          + LINE webhook HMAC-SHA256 簽章驗證 + 群組/聊天室支援
#          + Actions 批次建立全市場技術快取；Render LINE 優先只讀快取，避免 Yahoo/TWSE 即時限流

import os
import json
import time
import math
import traceback
import re
import html
import hmac
import hashlib
import base64
import threading

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import requests
import pandas as pd
import numpy as np
import yfinance as yf


# ============================================================
# 基本設定
# ============================================================

LINE_TOKEN = os.environ.get('LINE_CHANNEL_ACCESS_TOKEN', '')
LINE_CHANNEL_SECRET = os.environ.get('LINE_CHANNEL_SECRET', '')

TWSE_BASE = 'https://openapi.twse.com.tw/v1'
TWSE_WEB_BASE = 'https://www.twse.com.tw/rwd/zh'
TPEX_BASE = 'https://www.tpex.org.tw/openapi/v1'

TW_TZ = ZoneInfo('Asia/Taipei')

STATE_FILE = 'alert_state.json'
PE_HISTORY_FILE = 'pe_history.json'
CHIP_HISTORY_FILE = 'chip_history.json'
LINE_CHIP_CACHE_FILE = 'line_chip_cache.json'
LINE_MARGIN_CACHE_FILE = 'line_margin_cache.json'
LINE_PE_CACHE_FILE = 'line_pe_cache.json'
# V2.10.21：LINE 查詢用的輕量快取；Actions 每日批次建立全市場技術資料，Render 優先讀 GitHub 快取。
LINE_FUND_CACHE_FILE = 'line_fund_cache.json'
LINE_TECH_CACHE_FILE = 'line_technical_cache.json'

UNIVERSE_CACHE_FILE = 'market_universe_cache.json'
TWSE_PROFILE_CACHE_FILE = 'twse_profile_cache.json'
TWSE_QUOTES_CACHE_FILE = 'twse_quotes_cache.json'

# V2.9.8 新增
SUBINDUSTRY_CACHE_FILE = 'subindustry_cache.json'

LINE_REPLY_URL = 'https://api.line.me/v2/bot/message/reply'
LINE_PUSH_URL = 'https://api.line.me/v2/bot/message/push'
LINE_BROADCAST_URL = 'https://api.line.me/v2/bot/message/broadcast'

DAILY_THRESHOLD = -0.05
WEEK_THRESHOLD = -0.10

PE_MIN_HISTORY = 60
PE_MAX_VALID = 200
PE_ONE_YEAR_TRADING_DAYS = 240

UNIVERSE_CACHE_HOURS = 24

TWSE_TIMEOUT = 8
TPEX_TIMEOUT = 10

API_SLEEP = .05
PE_BACKFILL_MAX_DAYS = 370
PE_HISTORY_TIMEOUT = 8
# PE 歷史查詢以日期快取，避免同一次執行 2330/3711 重複打同一天 API
PE_DATE_CACHE = {}

YF_TIMEOUT = 10
MAX_HISTORY_DAYS_PER_RUN = 75

# V2.10.21：全市場技術快取設定。
# Actions 只在快取缺少/過期時更新，避免每天重抓 1985 檔造成不必要的 Yahoo 流量。
TECH_CACHE_MAX_AGE = 36 * 3600
TECH_BATCH_CHUNK = 80
TECH_BATCH_TIMEOUT = 30
TECH_BATCH_PERIOD = '6mo'
TECH_BATCH_INTERVAL = '1d'

# V2.10.21：LINE Free 查詢的硬性網路預算。快取不存在時也必須快速結束，
# 不允許因單一 TWSE/TPEX/Yahoo timeout 把 LINE 卡住數分鐘。
LINE_FAST_TIMEOUT = 3.0
LINE_REMOTE_CACHE_TIMEOUT = 3.0

# 次產業快取時間（實際以天數控制）
SUBINDUSTRY_CACHE_DAYS = 30

RUN_CACHE = {}
INSTITUTIONAL_CACHE = {}
MARGIN_CACHE = {}
SUBINDUSTRY_CACHE = {}

# V2.10.1：LINE 查詢分析鎖，避免多個訊息同時改寫全域快取。
LINE_ANALYSIS_LOCK = threading.Lock()

# V2.10.19：使用非 daemon 的 ThreadPoolExecutor 執行 LINE 背景分析。
# 不再用 daemon=True 的裸 Thread，降低 Render request 結束後背景工作
# 被直接終止的風險。完整分析仍在獨立工作執行緒中，不會讓 replyToken 過期。
from concurrent.futures import ThreadPoolExecutor
LINE_ANALYSIS_EXECUTOR = ThreadPoolExecutor(
    max_workers=1,
    thread_name_prefix='line-analysis'
)

# LINE webhook 可能因網路重試而重送同一事件；避免同一個 event 被分析兩次。
LINE_MODE_ACTIVE = False

LINE_EVENT_LOCK = threading.Lock()
LINE_SEEN_EVENTS = set()
LINE_SEEN_EVENT_MAX = 500


# ============================================================
# 目標標的
# ============================================================

STOCKS = {
    '0050 元大台灣50': '0050.TW',
    '2330 台積電': '2330.TW',
    '3711 日月光投控': '3711.TW',
    'QQQ': 'QQQ',
    '台灣加權指數': '^TWII'
}


# ============================================================
# TWSE 官方產業代碼
# ============================================================

INDUSTRY_CODE_MAP = {
    '01': '水泥工業',
    '02': '食品工業',
    '03': '塑膠工業',
    '04': '紡織纖維',
    '05': '電機機械',
    '06': '電器電纜',
    '08': '玻璃陶瓷',
    '09': '造紙工業',
    '10': '鋼鐵工業',
    '11': '橡膠工業',
    '12': '汽車工業',
    '13': '電子工業',
    '14': '建材營造',
    '15': '航運業',
    '16': '觀光餐旅',
    '17': '金融業',
    '18': '貿易百貨',
    '19': '綜合',
    '20': '其他',
    '21': '化學工業',
    '22': '生技醫療',
    '23': '油電燃氣業',
    '24': '半導體業',
    '25': '電腦及週邊設備業',
    '26': '光電業',
    '27': '通信網路業',
    '28': '電子零組件業',
    '29': '電子通路業',
    '30': '資訊服務業',
    '31': '其他電子業',
    '32': '文化創意業',
    '33': '農業科技',
    '34': '電子商務',
    '35': '數位雲端',
    '36': '運動休閒',
    '37': '居家生活',
    '38': '綠能環保',
    '39': '數位經濟',
    '40': '其他'
}


# ============================================================
# 產業模型
# ============================================================

INDUSTRY_MODEL = {
    '金融業': {
        'pe': False,
        'peg': False,
        'pb': True,
        'yield': True,
        'roe': True
    },
    '銀行業': {
        'pe': False,
        'peg': False,
        'pb': True,
        'yield': True,
        'roe': True
    },
    '保險業': {
        'pe': False,
        'peg': False,
        'pb': True,
        'yield': True,
        'roe': True
    }
}

DEFAULT_MODEL = {
    'pe': True,
    'peg': True,
    'pb': True,
    'yield': True,
    'roe': True
}


# ============================================================
# Helpers
# ============================================================

def to_float(v):
    if v is None:
        return None

    try:
        s = str(v).strip()
        s = s.replace(',', '')
        s = s.replace('%', '')

        if s in {
            '',
            '-',
            '--',
            'N/A',
            'nan',
            'NaN',
            'None',
            'null',
            '－',
            '…'
        }:
            return None

        return float(s)

    except Exception:
        return None


def first_value(row, names):
    if not isinstance(row, dict):
        return None

    for n in names:
        if n in row and row[n] not in (
            None,
            '',
            '-',
            '--',
            '－'
        ):
            return row[n]

    return None


def find_value(row, names):
    return to_float(first_value(row, names))


def clean_code(v):
    s = str(v or '').strip().upper()

    for x in ('.TW', '.TWO'):
        if s.endswith(x):
            s = s[:-len(x)]

    return s.strip()


def normalize_name(v):
    return (
        str(v or '')
        .strip()
        .replace(' ', '')
        .replace('　', '')
        .lower()
    )


def safe_div(a, b):
    try:
        return None if a is None or b in (None, 0) else a / b
    except Exception:
        return None


def fmt(v, d=2):
    return 'N/A' if v is None else f'{float(v):,.{d}f}'


def pct(v):
    return 'N/A' if v is None else f'{v:.2%}'


def canonical_industry(v):
    s = str(v or '').strip()

    if s.isdigit():
        s = INDUSTRY_CODE_MAP.get(s.zfill(2), s)

    aliases = {
        '電子工業': '其他電子業',
        '電信業': '通信網路業',
        '通信網路': '通信網路業',
        '電腦及週邊': '電腦及週邊設備業',
        '電腦及週邊設備': '電腦及週邊設備業',
        '生技醫療業': '生技醫療',
        '醫療保健業': '醫療保健',
        '觀光事業': '觀光餐旅'
    }

    return aliases.get(s, s or '其他')


def symbol_for(code, market=None):
    c = clean_code(code)

    if market == 'TWSE':
        return f'{c}.TW'

    if market == 'TPEX':
        return f'{c}.TWO'

    return c


def _repair_mojibake_text(value):
    """
    修復常見的 UTF-8 -> Latin-1/CP1252 中文亂碼。

    例如：
        æ¶åè£½é  -> 晶圓製造
        ICå°è£æ¸¬è©¦ -> IC封裝測試

    最多連續修復 3 次，並以「亂碼特徵是否下降」判斷是否採用結果，
    避免誤傷正常中文、英文或數字。
    """
    if value is None:
        return ''

    s = str(value)
    if not s:
        return ''

    def badness(x):
        markers = 'ÃÂâðæåçèéêëìíîïòóôõöùúûüýÿ'
        control = sum(1 for ch in x if 0x80 <= ord(ch) <= 0x9F)
        marker = sum(1 for ch in x if ch in markers)
        replacement = x.count('�')
        return marker + control * 2 + replacement * 4

    for _ in range(3):
        before = badness(s)
        if before <= 0:
            break

        candidates = []
        for enc in ('latin1', 'cp1252'):
            try:
                candidates.append(s.encode(enc).decode('utf-8'))
            except (UnicodeEncodeError, UnicodeDecodeError):
                pass

        if not candidates:
            break

        best = min(candidates, key=badness)
        if badness(best) < before:
            s = best
        else:
            break

    return s


def _repair_json_strings(obj):
    """遞迴修復 JSON 快取內所有字串，特別是舊次產業快取。"""
    if isinstance(obj, str):
        return _repair_mojibake_text(obj)
    if isinstance(obj, list):
        return [_repair_json_strings(x) for x in obj]
    if isinstance(obj, dict):
        return {
            _repair_json_strings(k): _repair_json_strings(v)
            for k, v in obj.items()
        }
    return obj


def normalize_subindustry(v):
    """
    統一次產業名稱。

    V2.9.9 三層防護的最後一層：即使 API 或舊快取已經留下
    UTF-8/Latin-1/CP1252 mojibake，最終顯示與比對前仍會修復。
    不建立任何股票代碼 -> 次產業硬編碼。
    """
    s = _repair_mojibake_text(v).strip()
    s = s.replace('　', ' ')
    s = re.sub(r'\s+', '', s)

    if not s:
        return ''

    return s


# ============================================================
# HTTP
# ============================================================

def http_json(
    url,
    params=None,
    timeout=20,
    retries=2,
    headers=None
):
    last = None

    base_headers = {
        'User-Agent': 'Mozilla/5.0 stock-alert/2.10.7'
    }

    if headers:
        base_headers.update(headers)

    for i in range(retries + 1):

        try:
            r = requests.get(
                url,
                params=params,
                timeout=timeout,
                headers=base_headers
            )

            r.raise_for_status()

            # V2.9.9：API 原始 bytes 強制以 UTF-8 解碼，避免
            # requests 自動猜測編碼後產生 mojibake。
            try:
                text = r.content.decode('utf-8-sig')
                return json.loads(text)
            except Exception:
                return r.json()

        except Exception as e:

            last = e

            if i < retries:
                time.sleep(.8 * (i + 1))

    print(f'API失敗：{url} / {last}')

    return None


def http_text(
    url,
    params=None,
    timeout=20,
    retries=2
):
    for i in range(retries + 1):

        try:

            r = requests.get(
                url,
                params=params,
                timeout=timeout,
                headers={
                    'User-Agent':
                        'Mozilla/5.0 stock-alert/2.10.7'
                }
            )

            r.raise_for_status()

            return r.content.decode(
                'utf-8-sig',
                'replace'
            )

        except Exception:

            if i < retries:
                time.sleep(.8 * (i + 1))

    return None


def twse_get(e, p=None):
    return http_json(
        TWSE_BASE + e,
        p,
        TWSE_TIMEOUT,
        retries=1
    )


def twse_web_get(e, p=None):
    return http_json(
        TWSE_WEB_BASE + e,
        p,
        TWSE_TIMEOUT,
        retries=1
    )


def tpex_get(e, p=None):
    return http_json(
        TPEX_BASE + e,
        p,
        TPEX_TIMEOUT,
        retries=1
    )


# ============================================================
# JSON
# ============================================================

def load_json(f):

    try:

        with open(f, encoding='utf-8') as x:

            d = json.load(x)

            # V2.9.9：舊快取讀取時強制修復 UTF-8 -> Latin-1/CP1252
            # 亂碼；即使 V2.9.8 已經把錯誤文字寫進快取，也能自動恢復。
            d = _repair_json_strings(d)

            return d if isinstance(d, dict) else {}

    except Exception:

        return {}


def save_json(f, d):

    t = f + '.tmp'

    d = _repair_json_strings(d)

    with open(
        t,
        'w',
        encoding='utf-8'
    ) as x:

        json.dump(
            d,
            x,
            ensure_ascii=False,
            indent=2
        )

    os.replace(t, f)


# ============================================================
# LINE
# ============================================================


def load_remote_subindustry_cache():
    """V2.10.19：Render Free 的免費遠端次產業快取備援。

    GitHub Actions 可將成功取得的 subindustry_cache.json 提交到公開 repo，
    Render 查詢時直接讀 raw.githubusercontent.com，不需 API token。
    若遠端檔不存在或網路失敗，回傳空 dict，完全不影響原流程。
    """
    url = (
        'https://raw.githubusercontent.com/HSY781106/-stock-line-alert/'
        'main/subindustry_cache.json'
    )
    try:
        r = requests.get(
            url,
            timeout=6,
            headers={'User-Agent': 'stock-alert/2.10.11'}
        )
        r.raise_for_status()
        data = r.json()
        if isinstance(data, dict) and isinstance(data.get('data'), dict):
            return _repair_json_strings(data)
    except Exception as e:
        print(f'遠端次產業快取讀取失敗：{e}')
    return {}

def load_remote_json_cache(filename, timeout=5):
    """V2.10.19：Render Free 遠端 GitHub 快取備援。

    只在本機快取缺失時使用；不需要 GitHub token。
    失敗直接回傳空 dict，不阻塞 LINE 分析。
    """
    url = (
        'https://raw.githubusercontent.com/HSY781106/-stock-line-alert/'
        f'main/{filename}'
    )
    try:
        r = requests.get(
            url,
            timeout=timeout,
            headers={'User-Agent': 'stock-alert/2.10.16'}
        )
        r.raise_for_status()
        data = r.json()
        return _repair_json_strings(data) if isinstance(data, dict) else {}
    except Exception as e:
        print(f'遠端快取讀取失敗 {filename}：{e}', flush=True)
        return {}


def send_line(msg):

    if not LINE_TOKEN:
        return False

    try:

        r = requests.post(
            LINE_BROADCAST_URL,
            headers={
                'Authorization':
                    f'Bearer {LINE_TOKEN}',
                'Content-Type':
                    'application/json'
            },
            json={
                'messages': [{
                    'type': 'text',
                    'text': str(msg)[:5000]
                }]
            },
            timeout=20
        )

        print(f'LINE廣播：{r.status_code}')

        return r.status_code == 200

    except Exception as e:

        print('LINE廣播失敗：', e)

        return False


def reply_line(token, msg):
    """LINE Reply API。回覆失敗時完整印出 HTTP 狀態與 API 訊息。"""
    if not LINE_TOKEN or not token:
        print('LINE Reply略過：缺少 LINE token 或 replyToken')
        return False

    try:
        messages = [
            {'type': 'text', 'text': x}
            for x in _line_text_messages(msg)[:5]
        ]

        r = requests.post(
            LINE_REPLY_URL,
            headers={
                'Authorization': f'Bearer {LINE_TOKEN}',
                'Content-Type': 'application/json'
            },
            json={
                'replyToken': token,
                'messages': messages
            },
            timeout=15
        )

        if r.status_code != 200:
            print(
                f'❌ LINE Reply失敗：HTTP {r.status_code} | '
                f'{r.text[:1000]}'
            )
            return False

        print('✅ LINE Reply成功')
        return True

    except Exception as e:
        print(f'❌ LINE Reply例外：{type(e).__name__}: {e}')
        traceback.print_exc()
        return False


# ============================================================
# Universe
# ============================================================

def normalize_profile(row, market):

    code = clean_code(
        first_value(
            row,
            [
                '公司代號',
                '證券代號',
                'SecuritiesCompanyCode',
                'Code'
            ]
        )
    )

    name = (
        first_value(
            row,
            [
                '公司簡稱',
                '公司名稱',
                '證券名稱',
                'CompanyAbbreviation',
                'CompanyName'
            ]
        )
        or code
    )

    industry = canonical_industry(
        first_value(
            row,
            [
                '產業類別',
                '產業別',
                'SecuritiesIndustryCode',
                'Industry'
            ]
        )
        or '其他'
    )

    cap = find_value(
        row,
        [
            '實收資本額',
            '實收資本額(元)',
            'PaidinCapital',
            'Capital',
            'Capitals'
        ]
    )

    if not code.isdigit():
        return None

    return {
        'code': code,
        'name': str(name).strip(),
        'industry': industry,
        'market': market,
        'symbol': symbol_for(code, market),
        'capital': cap
    }


def get_twse_universe():

    data = twse_get('/opendata/t187ap03_L')

    out = []

    if isinstance(data, list):

        for r in data:

            x = normalize_profile(
                r,
                'TWSE'
            )

            if x:
                out.append(x)

    if out:

        save_json(
            TWSE_PROFILE_CACHE_FILE,
            {
                'cached_at': time.time(),
                'data': out
            }
        )

        print(
            f'TWSE 基本資料：'
            f'{len(out)}（OpenAPI）'
        )

        return out

    text = http_text(
        'https://mopsfin.twse.com.tw/opendata/t187ap03_L.csv'
    )

    if text:

        try:

            df = pd.read_csv(
                __import__('io').StringIO(text),
                dtype=str
            )

            out = [
                x
                for _, r in df.fillna('').iterrows()
                if (
                    x :=
                    normalize_profile(
                        r.to_dict(),
                        'TWSE'
                    )
                )
            ]

        except Exception:

            out = []

    if out:

        save_json(
            TWSE_PROFILE_CACHE_FILE,
            {
                'cached_at': time.time(),
                'data': out
            }
        )

        print(
            f'TWSE 基本資料：'
            f'{len(out)}（CSV）'
        )

        return out

    c = load_json(
        TWSE_PROFILE_CACHE_FILE
    ).get('data', [])

    print(
        f'⚠️ TWSE 基本資料使用快取：'
        f'{len(c)}'
    )

    return c


def get_tpex_universe():

    data = tpex_get(
        '/mopsfin_t187ap03_O'
    )

    out = []

    if isinstance(data, list):

        for r in data:

            x = normalize_profile(
                r,
                'TPEX'
            )

            if x:
                out.append(x)

    if not out:

        data = tpex_get(
            '/tpex_mainboard_daily_close_quotes'
        )

        if isinstance(data, list):

            for r in data:

                x = normalize_profile(
                    r,
                    'TPEX'
                )

                if x:
                    out.append(x)

    return out


def get_twse_quotes():

    data = twse_get(
        '/exchangeReport/STOCK_DAY_ALL'
    )

    out = {}

    if isinstance(data, list):

        for r in data:

            c = clean_code(
                first_value(
                    r,
                    [
                        'Code',
                        '證券代號'
                    ]
                )
            )

            p = find_value(
                r,
                [
                    'ClosingPrice',
                    '收盤價'
                ]
            )

            if c and p is not None:

                out[c] = {
                    'close': p,
                    'open': find_value(
                        r,
                        [
                            'OpeningPrice',
                            '開盤價'
                        ]
                    ),
                    'high': find_value(
                        r,
                        [
                            'HighestPrice',
                            '最高價'
                        ]
                    ),
                    'low': find_value(
                        r,
                        [
                            'LowestPrice',
                            '最低價'
                        ]
                    ),
                    'volume': find_value(
                        r,
                        [
                            'TradeVolume',
                            '成交股數'
                        ]
                    )
                }

    if out:

        save_json(
            TWSE_QUOTES_CACHE_FILE,
            {
                'cached_at': time.time(),
                'data': out
            }
        )

    else:

        out = load_json(
            TWSE_QUOTES_CACHE_FILE
        ).get('data', {})

    print(
        f'TWSE 當日行情：{len(out)}'
    )

    return out


def get_tpex_quotes():

    data = tpex_get(
        '/tpex_mainboard_daily_close_quotes'
    )

    out = {}

    if isinstance(data, list):

        for r in data:

            c = clean_code(
                first_value(
                    r,
                    [
                        'SecuritiesCompanyCode',
                        'Code',
                        '證券代號'
                    ]
                )
            )

            p = find_value(
                r,
                [
                    'Close',
                    'ClosingPrice'
                ]
            )

            if c and p is not None:

                out[c] = {
                    'close': p,
                    'open': find_value(
                        r,
                        [
                            'Open',
                            'OpeningPrice'
                        ]
                    ),
                    'high': find_value(
                        r,
                        [
                            'High',
                            'HighestPrice'
                        ]
                    ),
                    'low': find_value(
                        r,
                        [
                            'Low',
                            'LowestPrice'
                        ]
                    ),
                    'volume': find_value(
                        r,
                        [
                            'TradingShares',
                            'TradeVolume'
                        ]
                    ),
                    'capital': find_value(
                        r,
                        [
                            'Capitals',
                            'Capital'
                        ]
                    )
                }

    print(
        f'TPEx 當日行情：{len(out)}'
    )

    return out


def get_tpex_market_values():

    data = tpex_get(
        '/tpex_daily_market_value'
    )

    out = {}

    if isinstance(data, list):

        for r in data:

            c = clean_code(
                first_value(
                    r,
                    [
                        'SecuritiesCompanyCode',
                        '證券代號',
                        'Code',
                        '代號'
                    ]
                )
            )

            v = find_value(
                r,
                [
                    'MarketValue',
                    'market_value',
                    '市值',
                    '總市值',
                    'MarketCap'
                ]
            )

            if c and v is not None:
                out[c] = v

    print(
        f'TPEx 官方市值資料：{len(out)}'
    )

    return out


# ============================================================
# V2.9.8
# Dynamic Subindustry
# ============================================================

# 證交所 / 櫃買中心共同的「產業價值鏈資訊平台」
# 例如：
#   2330 -> 半導體 > 晶圓製造
#   3711 -> 半導體 > IC封裝測試
#
# 平台本身同時涵蓋上市、上櫃公司。
# 不使用股票代碼硬編碼。

VALUE_CHAIN_BASE = 'https://ic.tpex.org.tw/company_chain.php'
VALUE_CHAIN_TIMEOUT = 10
VALUE_CHAIN_WORKERS = 2
VALUE_CHAIN_BULK_JINA = False
VALUE_CHAIN_BULK_SLEEP = 0.25
SUBINDUSTRY_CACHE_DAYS = 30


class _TextExtractor(__import__('html.parser', fromlist=['HTMLParser']).HTMLParser):
    """輕量 HTML 文字解析器，不新增第三方套件依賴。"""

    def __init__(self):
        super().__init__()
        self.parts = []

    def handle_data(self, data):
        if data:
            self.parts.append(data)

    def text(self):
        return ' '.join(self.parts)


def parse_value_chain_html(text, code):
    """解析 TPEx/TWSE 產業價值鏈公司頁面。

    V2.10.19：保留 V2.10.9 已驗證可用的解析方式，並兼容
    HTML / Markdown / Reader 純文字格式。只接受「所屬產業鏈」附近
    的「大產業 > 次產業」，避免誤抓導覽列。
    """
    out = {'subindustries': [], 'records': []}
    if not text:
        return out

    raw = html.unescape(str(text))
    parser = _TextExtractor()
    try:
        parser.feed(raw)
        plain = parser.text()
    except Exception:
        plain = raw

    plain = html.unescape(plain)
    plain = plain.replace('\r', '\n')
    plain = re.sub(r'[ \t\u00a0]+', ' ', plain)
    plain = re.sub(r'\n{2,}', '\n', plain)

    pairs = []

    def add_pair(industry, node):
        industry = normalize_subindustry(industry)
        node = normalize_subindustry(node)
        if not industry or not node:
            return
        if len(industry) > 60 or len(node) > 180:
            return
        if 'http' in industry.lower() or 'http' in node.lower():
            return
        if industry in {'個體公司所屬產業鏈如下', '產業鏈簡介'}:
            return
        if node.startswith('使用條款') or node.startswith('隱私權'):
            return
        key = (industry, node)
        if key not in pairs:
            pairs.append(key)

    # 官方頁面 / V2.10.9 已驗證格式：► 半導體 > 晶圓製造
    pattern = r'[►▸▶]\s*([^>\n]{1,80}?)\s*>\s*([^►▸▶\n]{1,160})'
    for m in re.findall(pattern, plain):
        add_pair(m[0], m[1])

    # Jina Reader 有時會移除箭頭，改成單行 Markdown/純文字。
    if not pairs:
        line_pattern = r'^\s*(?:►|▸|▶)?\s*([^>\n]{1,80}?)\s*>\s*([^>\n]{1,160})\s*$'
        for line in plain.split('\n'):
            m = re.search(line_pattern, line)
            if m:
                add_pair(m.group(1), m.group(2))

    # 只在「所屬產業鏈如下」附近做較寬鬆掃描。
    if not pairs:
        marker = plain.find('所屬產業鏈如下')
        if marker >= 0:
            section = plain[marker:marker + 6000]
            for m in re.findall(
                r'(?:►|▸|▶)?\s*([^>\n]{1,80})\s*>\s*([^>\n]{1,160})',
                section
            ):
                add_pair(m[0], m[1])

    # Raw HTML 去 tag 後再試一次。
    if not pairs:
        raw_no_tag = re.sub(r'<[^>]+>', ' ', raw)
        raw_no_tag = html.unescape(raw_no_tag)
        raw_no_tag = re.sub(r'[ \t\u00a0]+', ' ', raw_no_tag)
        for m in re.findall(pattern, raw_no_tag):
            add_pair(m[0], m[1])

    for industry, node in pairs:
        out['records'].append({
            'industry': industry,
            'sub_industry': node,
            'date': datetime.now(TW_TZ).strftime('%Y-%m-%d')
        })
        if node not in out['subindustries']:
            out['subindustries'].append(node)

    return out

def fetch_value_chain_for_stock(code, allow_jina=True):
    """V2.10.19：免費次產業抓取修正版。

    修正：
    1. 補上標準庫 html import；V2.10.19 的 parse_value_chain_html 會呼叫
       html.unescape，但沒有 import html，導致所有股票都報 name 'html' is not defined。
    2. 批次建立次產業快取時，不再讓 8 個 worker 同時轟 Jina Reader，避免 429。
    3. 批次模式只打官方 TPEx 產業價值鏈頁面；Jina 僅留給 LINE 單股查詢的備援。
    4. 不使用付費 API、不使用股票代碼硬編碼。
    """
    code = clean_code(code)
    if not code or not code.isdigit():
        return None

    official_url = f'{VALUE_CHAIN_BASE}?stk_code={code}'
    headers = {
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/126 Safari/537.36',
        'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
        'Accept-Language': 'zh-TW,zh;q=0.9,en;q=0.8',
        'Referer': 'https://ic.tpex.org.tw/'
    }

    last_error = None

    # A. 官方頁面：最多 2 次；批次模式不做 Jina。
    for attempt in range(2):
        try:
            r = requests.get(
                official_url,
                timeout=VALUE_CHAIN_TIMEOUT,
                headers=headers,
                allow_redirects=True
            )
            r.raise_for_status()
            raw = r.content
            page_text = None
            for enc in ('utf-8-sig', 'utf-8', 'cp950', 'big5'):
                try:
                    page_text = raw.decode(enc)
                    break
                except UnicodeDecodeError:
                    pass
            if page_text is None:
                page_text = raw.decode('utf-8', errors='replace')

            parsed = parse_value_chain_html(page_text, code)
            if parsed.get('subindustries'):
                return parsed
            last_error = RuntimeError('官方頁面未解析到次產業')
        except Exception as e:
            last_error = e

        if attempt == 0:
            time.sleep(0.4)

    # B. Jina Reader：只允許 LINE 單股查詢使用。
    # 批次建立 207 檔快取時完全停用，避免 Jina 429。
    if allow_jina:
        proxy_urls = [
            f'https://r.jina.ai/https://ic.tpex.org.tw/company_chain.php?stk_code={code}',
            f'https://r.jina.ai/http://ic.tpex.org.tw/company_chain.php?stk_code={code}'
        ]
        for proxy_url in proxy_urls:
            try:
                r = requests.get(
                    proxy_url,
                    timeout=12,
                    headers={'User-Agent': 'Mozilla/5.0 stock-alert/2.10.12'}
                )
                r.raise_for_status()
                parsed = parse_value_chain_html(r.text, code)
                if parsed.get('subindustries'):
                    print(f'次產業備援成功：{code}（官方頁面 Reader）')
                    return parsed
            except Exception as e:
                last_error = e

    print(f'次產業 API失敗：{code} / {last_error}')
    return None

def _fetch_missing_value_chains(codes):
    """V2.10.19：批次抓取官方次產業資料，避免 Jina 429 與過度併發。

    批次只使用官方 TPEx 產業價值鏈頁面；成功資料會寫入
    subindustry_cache.json，後續 30 天不再重抓。
    """
    from concurrent.futures import ThreadPoolExecutor, as_completed

    codes = [
        clean_code(x)
        for x in codes
        if clean_code(x)
    ]
    codes = list(dict.fromkeys(codes))
    if not codes:
        return {}

    result = {}
    workers = min(VALUE_CHAIN_WORKERS, len(codes))

    def worker(code):
        # 輕微節流，降低 TPEx 被視為大量並發請求的機率。
        time.sleep(VALUE_CHAIN_BULK_SLEEP)
        return code, fetch_value_chain_for_stock(code, allow_jina=VALUE_CHAIN_BULK_JINA)

    with ThreadPoolExecutor(max_workers=workers) as executor:
        futures = {executor.submit(worker, code): code for code in codes}
        for future in as_completed(futures):
            code = futures[future]
            try:
                _code, data = future.result()
            except Exception as e:
                print(f'次產業批次錯誤：{code} / {e}')
                data = None
            if data and data.get('subindustries'):
                result[code] = data

    return result

def get_public_subindustry(u):
    """
    V2.9.8 免費次產業來源：

    證交所 / 櫃買中心「產業價值鏈資訊平台」。

    策略：
    1. 次產業不是每日變動資料，因此快取 30 天。
    2. 不再呼叫 FinMind TaiwanStockIndustryChain。
    3. 只抓本次 STOCKS 需要的「大產業」股票，
       不對全部 1985 檔無差別逐檔請求。
    4. 目標股所在產業的候選股票才會補抓次產業。
    5. 已存在快取的股票完全不請求。
    """

    global SUBINDUSTRY_CACHE

    cache = load_json(
        SUBINDUSTRY_CACHE_FILE
    )

    cached_at = cache.get(
        '_cached_at',
        0
    )

    cached_data = cache.get(
        'data',
        {}
    )

    if not isinstance(cached_data, dict):
        cached_data = {}

    now = time.time()
    cache_fresh = (
        now - cached_at
        < SUBINDUSTRY_CACHE_DAYS * 86400
    )

    # --------------------------------------------------------
    # 需要分析的目標股 -> 對應官方大產業
    # --------------------------------------------------------

    target_codes = []
    for value in STOCKS.values():
        code = clean_code(value)
        if code.isdigit():
            target_codes.append(code)

    target_industries = set()

    for code in target_codes:
        item = u.get(code)
        if item:
            target_industries.add(
                canonical_industry(
                    item.get('industry')
                )
            )

    # 若沒有成功建立股票池，至少抓目標股票本身。
    if not target_industries:
        target_industries = set()

    candidate_codes = []
    for code, item in u.items():
        if target_industries and canonical_industry(
            item.get('industry')
        ) not in target_industries:
            continue
        candidate_codes.append(code)

    # 目標股永遠加入候選。
    candidate_codes.extend(target_codes)
    candidate_codes = list(dict.fromkeys(
        clean_code(x) for x in candidate_codes
        if clean_code(x)
    ))

    # --------------------------------------------------------
    # 30 天快取仍有效：只補缺少的股票。
    # --------------------------------------------------------

    def valid_cached_chain(code):
        info = cached_data.get(code)
        if not isinstance(info, dict):
            return False
        subs = info.get('subindustries', [])
        if not isinstance(subs, list):
            return False
        return any(normalize_subindustry(x) for x in subs)

    if cache_fresh:
        # 空的失敗快取不能視為有效，否則 LINE 查詢會永久顯示 N/A。
        missing = [
            code for code in candidate_codes
            if not valid_cached_chain(code)
        ]
    else:
        # 超過 30 天：重新驗證本次目標產業的全部候選股票。
        missing = list(candidate_codes)

    print(
        '\n========== 更新動態次產業資料 V2.9.9 =========='
    )
    print(
        '次產業來源：TPEx/TWSE 產業價值鏈資訊平台（公開資料）'
    )
    print(
        f'次產業快取：{SUBINDUSTRY_CACHE_DAYS} 天'
    )
    print(
        f'目標大產業：{len(target_industries)} 個'
    )
    print(
        f'候選股票：{len(candidate_codes)} 檔；'
        f'需更新：{len(missing)} 檔'
    )

    if missing:
        fetched = _fetch_missing_value_chains(
            missing
        )

        cached_data.update(fetched)

        print(
            f'本次公開資料取得：'
            f'{len(fetched)}/{len(missing)} 檔'
        )

    else:
        print(
            '本次無需重新抓取次產業資料'
        )

    # --------------------------------------------------------
    # 保存快取
    # --------------------------------------------------------

    if cached_data:
        saved_at = now if missing else cached_at
        save_json(
            SUBINDUSTRY_CACHE_FILE,
            {
                '_cached_at': saved_at,
                'source':
                    'TPEx/TWSE Industry Value Chain',
                'source_url': VALUE_CHAIN_BASE,
                'cache_days': SUBINDUSTRY_CACHE_DAYS,
                'data': cached_data
            }
        )

    SUBINDUSTRY_CACHE = cached_data

    print(
        f'動態次產業覆蓋：'
        f'{len(cached_data)} 檔'
    )

    return cached_data


def attach_subindustries(u, subindustry_data):

    """
    將次產業資料附加到市場股票池。

    每檔股票：
        subindustries = [...] 
        subindustry = 主要顯示用次產業

    不建立任何股票代碼硬編碼。
    """

    count = 0
    multi_count = 0

    for code, item in u.items():

        info = subindustry_data.get(
            clean_code(code),
            {}
        )

        subs = info.get(
            'subindustries',
            []
        )

        if not isinstance(subs, list):
            subs = []

        subs = [
            normalize_subindustry(x)
            for x in subs
            if normalize_subindustry(x)
        ]

        subs = list(dict.fromkeys(subs))

        item['subindustries'] = subs
        item['subindustry'] = (
            subs[0]
            if subs
            else ''
        )

        if subs:
            count += 1

        if len(subs) > 1:
            multi_count += 1

    print(
        f'次產業掛載：'
        f'{count}/{len(u)} 檔'
    )

    if multi_count:
        print(
            f'多重次產業股票：'
            f'{multi_count} 檔'
        )

    return u


def get_subindustries_for_stock(
    code,
    item=None
):

    c = clean_code(code)

    if item is not None:

        subs = item.get(
            'subindustries',
            []
        )

        if isinstance(subs, list):
            subs = [
                normalize_subindustry(x)
                for x in subs
                if normalize_subindustry(x)
            ]

            if subs:
                return list(
                    dict.fromkeys(subs)
                )

    info = SUBINDUSTRY_CACHE.get(
        c,
        {}
    )

    subs = info.get(
        'subindustries',
        []
    )

    if not isinstance(subs, list):
        return []

    return list(
        dict.fromkeys(
            normalize_subindustry(x)
            for x in subs
            if normalize_subindustry(x)
        )
    )


def same_subindustry(
    target_subindustries,
    peer_subindustries
):

    target = {
        normalize_subindustry(x)
        for x in target_subindustries
        if normalize_subindustry(x)
    }

    peer = {
        normalize_subindustry(x)
        for x in peer_subindustries
        if normalize_subindustry(x)
    }

    if not target or not peer:
        return False

    return bool(
        target.intersection(peer)
    )


def get_dynamic_subindustry_peers(
    code,
    industry,
    subindustry,
    u,
    limit=10
):
    """V2.10.19：動態次產業 Top 10。

    LINE/Render 若啟動時沒有完整次產業快取，查詢時會對
    「同大產業且市值最大的候選股」補抓次產業，直到找到足夠
    的同次產業標的；不使用股票代碼硬編碼。
    """
    code = clean_code(code)
    target = u.get(code)
    if not target:
        return []

    target_industry = canonical_industry(industry)
    target_subs = get_subindustries_for_stock(code, target)
    if not target_subs:
        target_subs = ensure_subindustry_for_query(code, target)
    if not target_subs:
        return []

    candidates = []
    missing = []
    for c, x in u.items():
        if clean_code(c) == code:
            continue
        if canonical_industry(x.get('industry')) != target_industry:
            continue
        market_cap = to_float(x.get('market_cap'))
        if market_cap is None:
            continue
        peer_subs = get_subindustries_for_stock(c, x)
        if peer_subs:
            if same_subindustry(target_subs, peer_subs):
                candidates.append(x)
        else:
            missing.append(x)

    # V2.10.19：只對同大產業中市值最大的候選補抓，避免 LINE 查詢時
    # 對整個市場 1985 檔逐一請求。最多嘗試 60 檔，找到 Top 10 即停止。
    missing.sort(key=lambda x: to_float(x.get('market_cap')) or 0, reverse=True)
    for x in missing[:60]:
        if len(candidates) >= limit:
            break
        c = clean_code(x.get('code'))
        subs = ensure_subindustry_for_query(c, x)
        if subs and same_subindustry(target_subs, subs):
            candidates.append(x)

    candidates = [x for x in candidates if clean_code(x.get('code')) != code]
    candidates.sort(key=lambda x: to_float(x.get('market_cap')) or 0, reverse=True)
    return candidates[:limit]

def get_subindustry_display(
    code,
    item
):

    subs = get_subindustries_for_stock(
        code,
        item
    )

    if not subs:
        return '次產業資料不可用'

    return '、'.join(normalize_subindustry(x) for x in subs)


def ensure_subindustry_for_query(code, item=None):
    """V2.10.5：LINE 查詢時若目標股次產業缺失，立即補抓一次。"""
    c = clean_code(code)
    current = get_subindustries_for_stock(c, item)
    if current:
        return current

    data = fetch_value_chain_for_stock(c)
    if not data:
        return []

    subs = list(dict.fromkeys(
        normalize_subindustry(x)
        for x in data.get('subindustries', [])
        if normalize_subindustry(x)
    ))
    if not subs:
        return []

    SUBINDUSTRY_CACHE[c] = data

    cache = load_json(SUBINDUSTRY_CACHE_FILE)
    cached_data = cache.get('data', {})
    if not isinstance(cached_data, dict):
        cached_data = {}
    cached_data[c] = data

    save_json(
        SUBINDUSTRY_CACHE_FILE,
        {
            '_cached_at': cache.get('_cached_at', time.time()),
            'source': 'TPEx/TWSE Industry Value Chain',
            'source_url': VALUE_CHAIN_BASE,
            'cache_days': SUBINDUSTRY_CACHE_DAYS,
            'data': cached_data
        }
    )

    if isinstance(item, dict):
        item['subindustries'] = subs
        item['subindustry'] = subs[0]

    return subs


# ============================================================
# Build universe
# ============================================================

def build_universe():

    print(
        '\n========== '
        '建立動態市場股票池 V2.10.19 '
        '=========='
    )

    tw = get_twse_universe()
    tx = get_tpex_universe()

    print(
        f'TWSE 基本資料：{len(tw)}'
    )

    print(
        f'TPEx 基本資料：{len(tx)}'
    )

    tq = get_twse_quotes()
    xq = get_tpex_quotes()
    xv = get_tpex_market_values()

    u = {}

    # --------------------------------------------------------
    # TWSE
    # --------------------------------------------------------

    for x in tw:

        x = dict(x)

        q = tq.get(
            x['code'],
            {}
        )

        x['price'] = q.get(
            'close'
        )

        if (
            x.get('capital')
            and q.get('close')
        ):

            x['market_cap'] = (
                safe_div(
                    x.get('capital'),
                    10
                )
                * q.get('close')
            )

        else:

            x['market_cap'] = None

        u[x['code']] = x

    # --------------------------------------------------------
    # TPEX
    # --------------------------------------------------------

    for x in tx:

        x = dict(x)

        q = xq.get(
            x['code'],
            {}
        )

        x['price'] = q.get(
            'close'
        )

        x['market_cap'] = (
            xv.get(x['code'])
            or
            (
                safe_div(
                    x.get('capital')
                    or q.get('capital'),
                    10
                )
                * q.get('close')
                if (
                    x.get('capital')
                    or q.get('capital')
                )
                and q.get('close')
                else None
            )
        )

        u[x['code']] = x

    print(
        f'有效動態股票：{len(u)}'
    )

    return u


def get_market_universe(
    force_refresh=False
):

    c = load_json(
        UNIVERSE_CACHE_FILE
    )

    d = c.get('data')
    t = c.get(
        '_cached_at',
        0
    )

    if (
        not force_refresh
        and isinstance(d, dict)
        and d
        and time.time() - t
        < UNIVERSE_CACHE_HOURS * 3600
    ):

        # 舊版股票池可能沒有次產業
        # 若快取存在但次產業資料不存在，
        # 仍重新補次產業。

        sub_data = get_public_subindustry(d)

        d = attach_subindustries(
            d,
            sub_data
        )

        return d

    u = build_universe()

    if u:

        sub_data = get_public_subindustry(u)

        u = attach_subindustries(
            u,
            sub_data
        )

        save_json(
            UNIVERSE_CACHE_FILE,
            {
                '_cached_at': time.time(),
                'data': u
            }
        )

        return u

    if isinstance(d, dict):

        sub_data = get_public_subindustry(d)

        d = attach_subindustries(
            d,
            sub_data
        )

    return d or {}


# ============================================================
# 股票解析
# ============================================================

def resolve_stock(q, u):

    q = str(q or '').strip()

    m = re.match(
        r'^(?:TWSE:|TPEX:)?'
        r'(\d{4,6})'
        r'(?:\.TW|\.TWO)?'
        r'(?:\s+.*)?$',
        q,
        re.I
    )

    if (
        m
        and m.group(1) in u
    ):
        return u[m.group(1)]

    if clean_code(q) in u:
        return u[clean_code(q)]

    nq = normalize_name(
        re.sub(
            r'^(?:TWSE:|TPEX:)?'
            r'\d{4,6}'
            r'(?:\.TW|\.TWO)?\s*',
            '',
            q,
            flags=re.I
        )
        or q
    )

    hits = [
        x
        for x in u.values()
        if normalize_name(
            x.get('name')
        ) == nq
    ]

    if len(hits) == 1:
        return hits[0]

    hits = [
        x
        for x in u.values()
        if (
            nq
            and nq in normalize_name(
                x.get('name')
            )
        )
    ]

    return (
        hits[0]
        if len(hits) == 1
        else None
    )


# ============================================================
# Yahoo
# ============================================================

def yf_download(
    symbol,
    period='1y',
    interval='1d'
):

    key = (
        symbol,
        period,
        interval
    )

    if key in RUN_CACHE:
        return RUN_CACHE[key]

    try:

        d = yf.download(
            symbol,
            period=period,
            interval=interval,
            progress=False,
            auto_adjust=False,
            threads=False,
            timeout=YF_TIMEOUT
        )

        if d is None or d.empty:

            RUN_CACHE[key] = None

            return None

        if isinstance(
            d.columns,
            pd.MultiIndex
        ):

            d.columns = [
                x[0]
                for x in d.columns
            ]

        RUN_CACHE[key] = d

        return d

    except Exception as e:

        print(
            f'Yahoo download失敗 '
            f'{symbol} '
            f'[{interval}/{period}]: '
            f'{e}'
        )

        RUN_CACHE[key] = None

        return None


def get_latest_price(symbol):

    key = (
        'latest',
        symbol
    )

    if key in RUN_CACHE:
        return RUN_CACHE[key]

    d = yf_download(
        symbol,
        '5d',
        '1d'
    )

    try:

        v = (
            float(
                d['Close']
                .dropna()
                .iloc[-1]
            )
            if (
                d is not None
                and not d.empty
            )
            else None
        )

    except Exception:

        v = None

    RUN_CACHE[key] = v

    return v


def get_previous_close(symbol):

    d = yf_download(
        symbol,
        '10d',
        '1d'
    )

    c = (
        pd.to_numeric(
            d['Close'],
            errors='coerce'
        ).dropna()
        if (
            d is not None
            and 'Close' in d
        )
        else pd.Series(
            dtype=float
        )
    )

    return (
        float(c.iloc[-2])
        if len(c) >= 2
        else None
    )


def get_week_high(symbol):

    d = yf_download(
        symbol,
        '10d',
        '1d'
    )

    h = (
        pd.to_numeric(
            d['High'],
            errors='coerce'
        ).dropna()
        if (
            d is not None
            and 'High' in d
        )
        else pd.Series(
            dtype=float
        )
    )

    return (
        float(h.tail(7).max())
        if not h.empty
        else None
    )


# ============================================================
# 15 分鐘區間
# ============================================================

def parse_time(v):

    try:

        d = datetime.fromisoformat(v)

        return (
            d
            if d.tzinfo
            else d.replace(
                tzinfo=TW_TZ
            )
        )

    except Exception:

        return None


def yahoo_chart_intraday(
    symbol,
    start_dt,
    end_dt,
    interval='5m'
):
    """
    取得 Yahoo 盤中 K 棒。

    V2.9.9：
    Yahoo 台股盤中資料常停在最後一根已完成 K 棒（例如 13:30），
    不再拿 GitHub Actions 的目前時間硬套區間；一律先找 Yahoo
    實際最新 K 棒，再以該時間作為有效 end。
    """
    ranges = {'5m': '5d', '1m': '1d'}
    hosts = ('query1.finance.yahoo.com', 'query2.finance.yahoo.com')

    try:
        if start_dt.tzinfo is None:
            start_dt = start_dt.replace(tzinfo=TW_TZ)
        else:
            start_dt = start_dt.astimezone(TW_TZ)

        if end_dt.tzinfo is None:
            end_dt = end_dt.replace(tzinfo=TW_TZ)
        else:
            end_dt = end_dt.astimezone(TW_TZ)

        for host in hosts:
            try:
                url = f'https://{host}/v8/finance/chart/{symbol}'
                r = requests.get(
                    url,
                    params={
                        'range': ranges[interval],
                        'interval': interval,
                        'events': 'history',
                        'includePrePost': 'false',
                        'includeAdjustedClose': 'true'
                    },
                    headers={
                        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/124.0 Safari/537.36'
                    },
                    timeout=10
                )
                r.raise_for_status()
                payload = r.json()
                chart = payload.get('chart') or {}
                result = (chart.get('result') or [None])[0]

                if not result:
                    err = chart.get('error')
                    print(f'Yahoo Chart無資料 {symbol} [{interval}] {host}: {err or "empty result"}')
                    continue

                ts = result.get('timestamp') or []
                q = ((result.get('indicators') or {}).get('quote') or [{}])[0]
                lows = q.get('low') or []

                all_points = []
                for t, lv in zip(ts, lows):
                    if t is None or lv is None:
                        continue
                    try:
                        dt = datetime.fromtimestamp(float(t), tz=TW_TZ)
                    except Exception:
                        continue
                    v = to_float(lv)
                    if v is not None:
                        all_points.append((dt, v))

                if not all_points:
                    print(f'Yahoo Chart無有效K棒 {symbol} [{interval}] {host}')
                    continue

                latest = max(dt for dt, _ in all_points)

                # 核心修正：Yahoo 最新 K 棒才是資料真正的 end。
                effective_end = min(end_dt, latest)
                effective_start = start_dt

                # 如果 GitHub Actions 執行時間已經晚於 Yahoo 最新K棒，
                # 原本會因 start > latest 而整段 N/A。現在改抓最新K棒往前15分鐘。
                if effective_start > effective_end:
                    effective_end = latest
                    effective_start = latest - timedelta(minutes=15)

                points = [
                    (dt, v) for dt, v in all_points
                    if effective_start <= dt <= effective_end
                ]

                if points:
                    return {
                        'low': min(v for _, v in points),
                        'start': min(dt for dt, _ in points),
                        'end': max(dt for dt, _ in points),
                        'source': f'Yahoo-{interval}',
                        'latest_bar': latest
                    }

                print(
                    f'Yahoo Chart區間無K棒 {symbol} [{interval}] {host}；'
                    f'實際最新K棒：{latest.strftime("%Y-%m-%d %H:%M:%S")}'
                )
            except Exception as e:
                print(f'Yahoo Chart失敗 {symbol} [{interval}] {host}：{e}')

        return None
    except Exception as e:
        print(f'Yahoo Chart盤中資料失敗 {symbol} [{interval}]：{e}')
        return None


def get_interval_stats(
    symbol,
    start_iso
):

    now = datetime.now(
        TW_TZ
    )

    start = (
        parse_time(start_iso)
        if start_iso
        else now - timedelta(
            minutes=15
        )
    )

    if not start or start > now:

        start = now - timedelta(
            minutes=15
        )

    for interval in (
        '5m',
        '1m'
    ):

        z = yahoo_chart_intraday(
            symbol,
            start,
            now,
            interval
        )

        if z:
            return z

    return None


def check_interval_low(
    name,
    symbol,
    state,
    current_price=None
):

    now = datetime.now(
        TW_TZ
    )

    iso = now.isoformat()

    s = (
        state
        .setdefault(
            'interval_low',
            {}
        )
        .setdefault(
            name,
            {}
        )
    )

    prev_t = s.get(
        'last_check'
    )

    prev_p = to_float(
        s.get(
            'last_price'
        )
    )

    # 15 分鐘區間的「目前價格」優先使用 TWSE/TPEx 股票池官方最新價，
    # 避免 Yahoo 日線收盤價或舊 K 棒造成與主分析價格不一致。
    cur = to_float(current_price)
    if cur is None:
        cur = get_latest_price(symbol)

    stats = (
        get_interval_stats(
            symbol,
            prev_t
        )
        if prev_t
        and cur is not None
        else None
    )

    result = None

    if (
        prev_t
        and prev_p
        and stats
    ):

        drop = (
            stats['low']
            / prev_p
            - 1
        )

        result = {
            'previous_price':
                prev_p,
            'interval_low':
                stats['low'],
            'drop':
                drop,
            'start':
                stats['start'].isoformat(),
            'end':
                stats.get('end', now).isoformat(),
            'source':
                stats.get(
                    'source'
                )
        }

        print(
            f'【15分鐘區間】'
            f'上次執行：'
            f'{stats["start"].strftime("%H:%M:%S")} '
            f'本次執行：'
            f'{now.strftime("%H:%M:%S")} '
            f'期間最低：'
            f'{stats["low"]:,.2f} '
            f'目前價格：'
            f'{cur:,.2f} '
            f'區間跌幅：'
            f'{drop:.2%} '
            f'（{stats.get("source","5m")}）'
        )

        if drop <= DAILY_THRESHOLD:

            send_line(
                f'🔴 15分鐘區間低點通知\n\n'
                f'標的：{name}\n'
                f'上次執行：'
                f'{stats["start"].strftime("%H:%M:%S")}\n'
                f'本次執行：'
                f'{now.strftime("%H:%M:%S")}\n'
                f'期間最低：'
                f'{stats["low"]:,.2f}\n'
                f'目前價格：'
                f'{cur:,.2f}\n'
                f'區間跌幅：'
                f'{drop:.2%}'
            )

    elif not prev_t:

        print(
            f'【15分鐘區間】'
            f'首次建立基準：'
            f'{now.strftime("%H:%M:%S")}，'
            f'目前價格：'
            f'{fmt(cur)}'
        )

    elif prev_t and not stats:

        print(
            f'⚠️ 15分鐘資料暫時無法取得；'
            f'保留上次執行基準：'
            f'{prev_t}'
        )

    # 關鍵：
    # 盤中資料失敗不得更新基準
    if not prev_t or stats:

        s.update({
            'last_check':
                iso,
            'last_price':
                cur
        })

    if stats:

        s[
            'last_interval_low'
        ] = stats['low']

        s[
            'last_interval_source'
        ] = stats.get(
            'source'
        )

    return result


def check_drop_alert(
    name,
    symbol,
    state
):

    cur = get_latest_price(
        symbol
    )

    pc = get_previous_close(
        symbol
    )

    wh = get_week_high(
        symbol
    )

    if cur is None or pc is None:
        return

    day = cur / pc - 1

    week = (
        cur / wh - 1
        if wh
        else None
    )

    s = (
        state
        .setdefault(
            'drop_alert',
            {}
        )
        .setdefault(
            name,
            {}
        )
    )

    today = datetime.now(
        TW_TZ
    ).strftime(
        '%Y-%m-%d'
    )

    if s.get('date') != today:

        s.update({
            'date':
                today,
            'daily_alert':
                False,
            'weekly_alert':
                False
        })

    if (
        day <= DAILY_THRESHOLD
        and not s.get(
            'daily_alert'
        )
    ):

        send_line(
            f'🔴 跌幅通知\n\n'
            f'標的：{name}\n'
            f'目前價格：{cur:,.2f}\n'
            f'前一交易日收盤：{pc:,.2f}\n'
            f'單日跌幅：{day:.2%}'
        )

        s['daily_alert'] = True

    elif day > DAILY_THRESHOLD:

        s['daily_alert'] = False

    if (
        week is not None
        and week <= WEEK_THRESHOLD
        and not s.get(
            'weekly_alert'
        )
    ):

        send_line(
            f'🔴 一週跌幅通知\n\n'
            f'標的：{name}\n'
            f'目前價格：{cur:,.2f}\n'
            f'過去7日高點：{wh:,.2f}\n'
            f'距7日高點跌幅：{week:.2%}'
        )

        s['weekly_alert'] = True

    elif (
        week is not None
        and week > WEEK_THRESHOLD
    ):

        s['weekly_alert'] = False


# ============================================================
# Valuation
# ============================================================

def parse_pe(data):

    out = {}

    if isinstance(data, list):

        rows = data
        fields = None

    elif isinstance(data, dict):

        fields = data.get(
            'fields',
            []
        )

        rows = data.get(
            'data',
            []
        )

    else:

        return out

    for r in (
        rows
        if isinstance(rows, list)
        else []
    ):

        if isinstance(r, list):

            o = (
                dict(
                    zip(
                        fields,
                        r
                    )
                )
                if fields
                else {}
            )

        elif isinstance(r, dict):

            o = r

        else:

            continue

        c = clean_code(
            first_value(
                o,
                [
                    '證券代號',
                    '公司代號',
                    'Code',
                    'SecuritiesCompanyCode'
                ]
            )
        )

        if c:

            out[c] = {
                'pe':
                    find_value(
                        o,
                        [
                            '本益比',
                            'PEratio',
                            'PERatio',
                            'PE'
                        ]
                    ),
                'pb':
                    find_value(
                        o,
                        [
                            '股價淨值比',
                            'PBratio',
                            'PBR',
                            'PBRatio'
                        ]
                    ),
                'yield':
                    find_value(
                        o,
                        [
                            '殖利率(%)',
                            '殖利率',
                            'DividendYield'
                        ]
                    )
            }

    return out


def _load_line_small_cache(filename):
    """LINE 專用小快取：本機 -> GitHub raw；只讀一次。"""
    local = load_json(filename)
    if isinstance(local, dict) and local:
        return local
    remote = load_remote_json_cache(filename, timeout=LINE_REMOTE_CACHE_TIMEOUT)
    return remote if isinstance(remote, dict) else {}


def _save_line_small_cache(filename, data):
    try:
        save_json(filename, data if isinstance(data, dict) else {})
        return True
    except Exception as e:
        print(f'LINE小快取保存失敗 {filename}：{e}', flush=True)
        return False


def _line_current_pe_data():
    cache = _load_line_small_cache(LINE_PE_CACHE_FILE)
    if cache:
        data = cache.get('data', cache) if isinstance(cache, dict) else {}
        if isinstance(data, dict) and data:
            print(f'LINE PE：使用快取 {len(data)} 檔', flush=True)
            return data
    # 最後備援：只允許一次短 timeout、零重試的官方查詢。
    out = {}
    try:
        out.update(parse_pe(http_json(
            TWSE_BASE + '/exchangeReport/BWIBBU_ALL',
            timeout=LINE_FAST_TIMEOUT, retries=0
        )))
    except Exception as e:
        print(f'LINE PE：TWSE 快速取得失敗：{type(e).__name__}', flush=True)
    try:
        out.update(parse_pe(http_json(
            TPEX_BASE + '/tpex_mainboard_peratio_analysis',
            timeout=LINE_FAST_TIMEOUT, retries=0
        )))
    except Exception as e:
        print(f'LINE PE：TPEX 快速取得失敗：{type(e).__name__}', flush=True)
    return out


def get_current_pe_data():

    key = 'current_pe'

    if key in RUN_CACHE:
        return RUN_CACHE[key]

    if LINE_MODE_ACTIVE:
        out = _line_current_pe_data()
    else:
        out = {
            **parse_pe(
                twse_get(
                    '/exchangeReport/BWIBBU_ALL'
                )
            ),
            **parse_pe(
                tpex_get(
                    '/tpex_mainboard_peratio_analysis'
                )
            )
        }

    RUN_CACHE[key] = out

    print(
        f'本次執行 PE 資料：'
        f'{len(out)} 檔（只抓一次）'
    )

    return out


PE_HISTORY_MARKET_BLOCKED = set()


def get_pe_by_date(
    ds,
    market
):
    """
    取得指定交易日 PE。

    V2.10.3 修正：
    - TWSE 歷史日 PE 不再使用 BWIBBU_ALL（該端點在 GitHub Actions
      上容易回 428 Precondition Required）。
    - 改用官方歷史日資料端點 BWIBBU_d + selectType=ALL。
    - 加入瀏覽器 Referer/Accept。
    - 歷史 PE 單次請求不重試，避免 TWSE 回 428 時一個日期卡住數十秒。
    - 同一市場一旦收到 428，本次執行立即停止該市場的歷史 PE 回補，
      不會再浪費數分鐘重複打 API。
    """

    key = (market, ds)

    if key in PE_DATE_CACHE:
        return PE_DATE_CACHE[key]

    if market in PE_HISTORY_MARKET_BLOCKED:
        PE_DATE_CACHE[key] = {}
        return {}

    if market == 'TPEX':
        try:
            data = tpex_get(
                '/tpex_mainboard_peratio_analysis',
                {'date': ds}
            )
            parsed = parse_pe(data)
            PE_DATE_CACHE[key] = parsed
            return parsed
        except Exception as e:
            print(f'歷史 PE 取得失敗：{market} {ds} / {e}')
            PE_DATE_CACHE[key] = {}
            return {}

    # TWSE：使用官方每日本益比/殖利率/PB 歷史端點。
    url = TWSE_WEB_BASE + '/afterTrading/BWIBBU_d'
    params = {
        'date': ds,
        'selectType': 'ALL',
        'response': 'json'
    }
    headers = {
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/131 Safari/537.36',
        'Referer': 'https://www.twse.com.tw/zh/trading/historical/bwibbu-day.html',
        'Accept': 'application/json,text/plain,*/*',
        'Accept-Language': 'zh-TW,zh;q=0.9,en;q=0.8'
    }

    try:
        r = requests.get(
            url,
            params=params,
            timeout=PE_HISTORY_TIMEOUT,
            headers=headers
        )

        if r.status_code == 428:
            print(
                f'歷史 PE API 暫時拒絕（428）：TWSE {ds}；'
                f'本次執行停止 TWSE 歷史 PE 回補，避免長時間重試。'
            )
            PE_HISTORY_MARKET_BLOCKED.add('TWSE')
            PE_DATE_CACHE[key] = {}
            return {}

        r.raise_for_status()

        try:
            data = json.loads(r.content.decode('utf-8-sig'))
        except Exception:
            data = r.json()

        parsed = parse_pe(data)
        PE_DATE_CACHE[key] = parsed
        return parsed

    except Exception as e:
        print(f'歷史 PE 取得失敗：{market} {ds} / {e}')
        PE_DATE_CACHE[key] = {}
        return {}


def backfill_pe(
    code,
    h,
    market
):
    """
    確保指定股票至少累積 PE_MIN_HISTORY 個「有效 PE」。

    重要修正：
    1. 不是只看有幾個日期，而是只計算 0 < PE <= PE_MAX_VALID。
    2. 舊快取中的 NA / 空字串 / 無效 PE 會重新抓，不會永久卡住。
    3. 每個日期的 API 結果在同一次執行快取，避免不同股票重複抓。
    4. 最多往前搜尋 PE_BACKFILL_MAX_DAYS 個曆日。
    """

    h.setdefault(code, {})

    def valid_pe(v):
        x = to_float(v)
        return x is not None and 0 < x <= PE_MAX_VALID

    def count_valid():
        return sum(1 for v in h[code].values() if valid_pe(v))

    n = count_valid()
    d = datetime.now(TW_TZ).date()
    checked = 0

    while n < PE_MIN_HISTORY and checked < PE_BACKFILL_MAX_DAYS:
        if d.weekday() < 5:
            ds = d.strftime('%Y%m%d')
            old = h[code].get(ds)

            # 已有有效值就保留；只有缺少/NA/無效值才重新查。
            if not valid_pe(old):
                data = get_pe_by_date(ds, market)
                pe = data.get(code, {}).get('pe')

                if valid_pe(pe):
                    h[code][ds] = pe
                else:
                    # 保留日期標記，避免本次/後續反覆判斷時無法辨識；
                    # 但下次執行仍會重新查，因為 NA 不被視為有效值。
                    h[code][ds] = old if old is not None else None

            n = count_valid()

        d -= timedelta(days=1)
        checked += 1
        time.sleep(.01)

    print(
        f'PE歷史回補：{code} {n}/{PE_MIN_HISTORY} 個有效PE，'
        f'搜尋 {checked} 天'
    )

    return h


def one_year_pe(
    code,
    h
):
    """計算最近一年有效 PE 的平均；最多採用最近 240 個交易日。"""

    cutoff = (
        datetime.now(TW_TZ).date()
        - timedelta(days=365)
    )

    v = []

    for ds, x in h.get(code, {}).items():
        try:
            d = datetime.strptime(ds, '%Y%m%d').date()
        except Exception:
            continue

        value = to_float(x)
        if (
            d >= cutoff
            and value is not None
            and 0 < value <= PE_MAX_VALID
        ):
            v.append((d, value))

    v.sort(key=lambda x: x[0], reverse=True)
    v = v[:PE_ONE_YEAR_TRADING_DAYS]

    if not v:
        return None, 0

    # 只有至少 60 個有效樣本才產生一年平均 PE。
    if len(v) < PE_MIN_HISTORY:
        return None, len(v)

    return sum(x for _, x in v) / len(v), len(v)


def yahoo_timeseries_fund(symbol):
    """V2.10.19：不依賴 Yahoo quoteSummary/info 的免費基本面備援。

    Render 上 yfinance 的 Ticker.info 偶爾會因 Yahoo quoteSummary/crumb
    限制而拿不到 EPS 成長、ROE、PEG。這裡直接使用 Yahoo 公開的
    fundamentals-timeseries endpoint，不需要 API key，也不需要 crumb。

    - EPS 成長：最近季度 diluted EPS 對四季前同季度 EPS
    - ROE：trailing net income / trailing stockholders equity
    - PEG：trailingPegRatio；若沒有則用 PE / EPS 成長率作估算
    """
    key = ('yf_ts_fund', symbol)
    if key in RUN_CACHE:
        return RUN_CACHE[key]

    out = {
        'eps_growth': None,
        'roe': None,
        'peg': None,
        'pe': None,
        'pb': None,
        'yield': None
    }

    now = datetime.now(TW_TZ)
    period1 = int((now - timedelta(days=900)).timestamp())
    period2 = int((now + timedelta(days=2)).timestamp())
    types = ','.join([
        'quarterlyDilutedEPS',
        'trailingDilutedEPS',
        'trailingNetIncome',
        'trailingStockholdersEquity',
        'trailingPegRatio'
    ])
    url = (
        'https://query1.finance.yahoo.com/ws/fundamentals-timeseries/v1/'
        f'finance/timeseries/{symbol}'
    )
    params = {
        'symbol': symbol,
        'type': types,
        'period1': period1,
        'period2': period2,
        'padTimeSeries': 'true'
    }

    try:
        r = requests.get(
            url,
            params=params,
            timeout=8,
            headers={'User-Agent': 'Mozilla/5.0 stock-alert/2.10.9'}
        )
        r.raise_for_status()
        data = r.json()
        result = (data.get('timeseries') or {}).get('result') or []

        def values_for(name):
            vals = []
            for row in result:
                if name in row:
                    vals.extend(row.get(name) or [])
            vals.sort(key=lambda x: x.get('asOfDate', '') if isinstance(x, dict) else '')
            return [
                to_float(x.get('reportedValue', {}).get('raw'))
                for x in vals
                if isinstance(x, dict)
            ]

        eps_q = values_for('quarterlyDilutedEPS')
        if len(eps_q) >= 5:
            latest = eps_q[-1]
            prior_yoy = eps_q[-5]
            if latest is not None and prior_yoy not in (None, 0):
                out['eps_growth'] = (latest / prior_yoy - 1) * 100

        ni = values_for('trailingNetIncome')
        eq = values_for('trailingStockholdersEquity')
        if ni and eq and ni[-1] is not None and eq[-1] not in (None, 0):
            out['roe'] = ni[-1] / eq[-1] * 100

        peg = values_for('trailingPegRatio')
        if peg:
            out['peg'] = peg[-1]
    except Exception as e:
        print(f'Yahoo timeseries fundamentals失敗 {symbol}: {e}')

    RUN_CACHE[key] = out
    return out


def yahoo_fund(symbol):
    """V2.10.19：Yahoo 基本面多層同步。

    第一層仍使用 Ticker.info（維持 V2.10.19 行為）。
    若 Render 的 Yahoo info 缺少 EPS 成長/ROE/PEG，第二層改讀
    financial statements 計算可取得的指標，避免 LINE 環境全部 N/A。
    """
    key = ('fund', symbol)
    if key in RUN_CACHE:
        return RUN_CACHE[key]

    o = {'eps_growth': None, 'roe': None, 'peg': None, 'pb': None, 'yield': None, 'pe': None}
    try:
        ticker = yf.Ticker(symbol)
        info = ticker.info or {}
        o['pe'] = to_float(info.get('trailingPE')) or to_float(info.get('forwardPE'))
        o['pb'] = to_float(info.get('priceToBook'))
        if info.get('dividendYield') is not None:
            o['yield'] = to_float(info.get('dividendYield')) * 100
        if info.get('earningsGrowth') is not None:
            o['eps_growth'] = to_float(info.get('earningsGrowth')) * 100
        if info.get('returnOnEquity') is not None:
            o['roe'] = to_float(info.get('returnOnEquity')) * 100
        o['peg'] = to_float(info.get('pegRatio'))

        # 財務報表 fallback：Yahoo info 缺欄位時仍可取得。
        if o['eps_growth'] is None or o['roe'] is None:
            try:
                inc = ticker.get_income_stmt(freq='yearly')
                bs = ticker.get_balance_sheet(freq='yearly')
                if isinstance(inc, pd.DataFrame) and not inc.empty:
                    ni = None
                    for row in ['NetIncome', 'Net Income', 'NetIncomeCommonStockholders']:
                        if row in inc.index:
                            ni = pd.to_numeric(inc.loc[row], errors='coerce').dropna()
                            if len(ni):
                                break
                    if ni is not None and len(ni) >= 2 and o['eps_growth'] is None:
                        latest = float(ni.iloc[0]); prev = float(ni.iloc[1])
                        if prev != 0:
                            o['eps_growth'] = (latest / prev - 1) * 100
                    if o['roe'] is None and ni is not None and len(ni) >= 1 and isinstance(bs, pd.DataFrame) and not bs.empty:
                        eq = None
                        for row in ['StockholdersEquity', 'CommonStockholdersEquity', 'TotalEquityGrossMinorityInterest']:
                            if row in bs.index:
                                eq = pd.to_numeric(bs.loc[row], errors='coerce').dropna()
                                if len(eq):
                                    break
                        if eq is not None and len(eq) >= 1:
                            latest_ni = float(ni.iloc[0])
                            latest_eq = float(eq.iloc[0])
                            avg_eq = latest_eq
                            if len(eq) >= 2:
                                avg_eq = (latest_eq + float(eq.iloc[1])) / 2
                            if avg_eq != 0:
                                o['roe'] = latest_ni / avg_eq * 100
            except Exception as e2:
                print(f'Yahoo financial statement fallback失敗 {symbol}: {e2}')

        # PEG 優先使用 Yahoo；若 Yahoo 沒有，嘗試用 forward EPS growth 的合理 fallback。
        # 不用粗暴以目前 PE / 歷史成長率取代 Yahoo PEG，避免改變既有評分口徑。
        if o['peg'] is None:
            for k in ('trailingPegRatio', 'pegRatio5Y', 'pegRatio'):
                v = to_float(info.get(k))
                if v is not None:
                    o['peg'] = v
                    break
    except Exception as e:
        print('Yahoo fundamentals失敗', symbol, e)

    # V2.10.19：直接 Yahoo fundamentals-timeseries 最終備援。
    # 只補缺欄位，不覆蓋原本已成功取得的 Yahoo info 數值。
    try:
        ts = yahoo_timeseries_fund(symbol)
        for k in ('eps_growth', 'roe', 'peg'):
            if o.get(k) is None and ts.get(k) is not None:
                o[k] = ts[k]
    except Exception as e:
        print(f'Yahoo timeseries補值失敗 {symbol}: {e}')

    RUN_CACHE[key] = o
    return o


# ============================================================
# Technical
# ============================================================

def rsi(
    c,
    p=14
):

    c = (
        pd.to_numeric(
            c,
            errors='coerce'
        )
        .dropna()
    )

    d = c.diff()

    g = (
        d.clip(
            lower=0
        )
        .rolling(p)
        .mean()
    )

    l = (
        (-d.clip(
            upper=0
        ))
        .rolling(p)
        .mean()
    )

    x = (
        100
        - 100
        / (
            1
            + g
            / l.replace(
                0,
                np.nan
            )
        )
    ).dropna()

    return (
        float(x.iloc[-1])
        if not x.empty
        else None
    )


def kd(d):

    if d is None or len(d) < 20:
        return None, None

    h = d['High']
    l = d['Low']
    c = d['Close']

    lo = l.rolling(9).min()
    hi = h.rolling(9).max()

    r = (
        (c - lo)
        / (hi - lo).replace(
            0,
            np.nan
        )
        * 100
    )

    k = r.ewm(
        com=2,
        adjust=False
    ).mean()

    dd = k.ewm(
        com=2,
        adjust=False
    ).mean()

    return (
        (
            float(
                k.dropna().iloc[-1]
            ),
            float(
                dd.dropna().iloc[-1]
            )
        )
        if (
            not k.dropna().empty
            and not dd.dropna().empty
        )
        else (
            None,
            None
        )
    )


def _twse_history_fallback(code, months=3):
    """V2.10.19：TWSE 官方日線最後備援。

    Render Free 不應把多個 TWSE 月份請求串行等待；因此改成最多 3 個月份
    平行抓取、短 timeout。正常情況 LINE 不會走到這裡，因為 GitHub Actions
    會在批次執行時建立 line_technical_cache.json。
    """
    from concurrent.futures import ThreadPoolExecutor, as_completed

    code = clean_code(code)
    if not code or not code.isdigit():
        return None

    today = datetime.now(TW_TZ).date()
    first_month = today.replace(day=1)
    headers = {
        'User-Agent': 'Mozilla/5.0 stock-alert/2.10.19',
        'Accept': 'application/json,text/plain,*/*'
    }

    dates = []
    for i in range(max(1, min(int(months), 3))):
        y = first_month.year
        m = first_month.month - i
        while m <= 0:
            y -= 1
            m += 12
        dates.append(f'{y:04d}{m:02d}01')

    def fetch(ds):
        url = f'{TWSE_WEB_BASE}/afterTrading/STOCK_DAY'
        try:
            r = requests.get(
                url,
                params={
                    'response': 'json',
                    'date': ds,
                    'stockNo': code
                },
                headers=headers,
                timeout=2.5
            )
            r.raise_for_status()
            data = r.json()
            return ds, data
        except Exception as e:
            print(
                f'LINE技術面：TWSE {ds} 取得失敗 {code}：'
                f'{type(e).__name__}',
                flush=True
            )
            return ds, None

    frames = []
    workers = min(3, len(dates))
    with ThreadPoolExecutor(max_workers=workers) as ex:
        futures = [ex.submit(fetch, ds) for ds in dates]
        for fut in as_completed(futures):
            ds, data = fut.result()
            rows = data.get('data', []) if isinstance(data, dict) else []
            for row in rows:
                if not isinstance(row, list) or len(row) < 7:
                    continue
                try:
                    def num(v):
                        return to_float(str(v).replace(',', ''))

                    close = num(row[6])
                    if close is None:
                        continue
                    frames.append({
                        'date': str(row[0]),
                        'Open': num(row[3]),
                        'High': num(row[4]),
                        'Low': num(row[5]),
                        'Close': close
                    })
                except Exception:
                    continue

    if not frames:
        return None

    df = (
        pd.DataFrame(frames)
        .drop_duplicates('date')
        .sort_values('date')
    )
    for col in ['Open', 'High', 'Low', 'Close']:
        df[col] = pd.to_numeric(df[col], errors='coerce')
    df = df.dropna(subset=['Close'])

    return df.reset_index(drop=True) if len(df) >= 20 else None


def _technical_from_df(d):
    """統一技術指標計算，避免 Yahoo/TWSE/快取三條路徑口徑不同。"""
    o = {
        'k': None,
        'd': None,
        'rsi': None,
        'ma20': None,
        'ma60': None,
        'trend': None,
        'distance_low': None,
        'price': None,
        'recent_low': None
    }

    if d is None or d.empty or 'Close' not in d.columns:
        return o

    c = pd.to_numeric(d['Close'], errors='coerce').dropna()
    if c.empty:
        return o

    try:
        if all(x in d.columns for x in ['High', 'Low', 'Close']):
            k, dd = kd(d)
        else:
            k, dd = None, None
    except Exception:
        k, dd = None, None

    o.update({
        'k': k,
        'd': dd,
        'rsi': rsi(c),
        'ma20': float(c.tail(20).mean()) if len(c) >= 20 else None,
        'ma60': float(c.tail(60).mean()) if len(c) >= 60 else None,
        'price': float(c.iloc[-1])
    })

    if o['ma20'] is not None and o['ma60'] is not None:
        if o['price'] > o['ma20'] > o['ma60']:
            o['trend'] = '多頭'
        elif o['price'] < o['ma20'] < o['ma60']:
            o['trend'] = '空頭'
        else:
            o['trend'] = '震盪'
    else:
        o['trend'] = '震盪'

    lo = c.tail(20).min() if len(c) >= 20 else c.min()
    if lo is not None and not pd.isna(lo):
        o['recent_low'] = float(lo)
        o['distance_low'] = o['price'] / float(lo) - 1 if float(lo) else None

    return o


def _load_technical_cache_entry(cache, key, max_age=72 * 3600):
    """讀取單一技術快取；Render 冷啟動允許週末最多 72 小時。"""
    if not isinstance(cache, dict):
        return None
    x = cache.get(key)
    if not isinstance(x, dict) or not x.get('_cached_at'):
        return None
    try:
        age = time.time() - float(x.get('_cached_at', 0))
        if age < 0 or age >= max_age:
            return None
    except Exception:
        return None

    out = {}
    for k in ['k', 'd', 'rsi', 'ma20', 'ma60', 'trend', 'distance_low', 'price', 'recent_low']:
        if k == 'trend':
            out[k] = x.get(k)
        else:
            out[k] = to_float(x.get(k))
    return out


def _technical_cache_is_fresh(cache, key, max_age=TECH_CACHE_MAX_AGE):
    """Actions 用較嚴格的新鮮度；Render 仍可使用較寬鬆的 72 小時快取。"""
    if not isinstance(cache, dict):
        return False
    x = cache.get(key)
    if not isinstance(x, dict):
        return False
    try:
        age = time.time() - float(x.get('_cached_at', 0))
        return 0 <= age < max_age and to_float(x.get('price')) is not None
    except Exception:
        return False


def _save_technical_cache_entry(cache, key, value, save=True):
    try:
        cache[key] = dict(value)
        cache[key]['_cached_at'] = time.time()
        if save:
            save_json(LINE_TECH_CACHE_FILE, cache)
        return True
    except Exception as e:
        print(f'技術快取保存失敗 {key}：{e}', flush=True)
        return False


def _technical_from_yahoo_batch_frame(d):
    """把 Yahoo 批次 DataFrame 統一交給既有技術指標計算。"""
    if d is None or d.empty:
        return None
    try:
        d = d.copy()
        if isinstance(d.columns, pd.MultiIndex):
            # 已由批次 extractor 取出單一 ticker 時，理論上不會進來；
            # 若仍有 MultiIndex，取第一層價格欄位。
            if len(d.columns.levels) == 2:
                cols = []
                for col in d.columns:
                    if isinstance(col, tuple):
                        cols.append(col[0] if col[0] in {'Open','High','Low','Close','Adj Close','Volume'} else col[-1])
                    else:
                        cols.append(col)
                d.columns = cols
        keep = [x for x in ['Open','High','Low','Close','Volume'] if x in d.columns]
        if 'Close' not in keep:
            return None
        d = d[keep].copy()
        for col in keep:
            d[col] = pd.to_numeric(d[col], errors='coerce')
        d = d.dropna(subset=['Close'])
        return d if len(d) >= 20 else None
    except Exception:
        return None


def _extract_batch_ticker_frame(batch, ticker):
    """兼容 yfinance 不同版本的 MultiIndex 欄位排列。"""
    if batch is None or batch.empty:
        return None
    try:
        if not isinstance(batch.columns, pd.MultiIndex):
            return batch

        levels = [list(batch.columns.get_level_values(i)) for i in range(batch.columns.nlevels)]
        price_names = {'Open', 'High', 'Low', 'Close', 'Adj Close', 'Volume'}

        # group_by='ticker'：第一層通常是 ticker。
        for level_idx in range(batch.columns.nlevels):
            if ticker in levels[level_idx]:
                try:
                    sub = batch.xs(ticker, axis=1, level=level_idx, drop_level=True)
                    if isinstance(sub, pd.DataFrame):
                        # 若仍有一層 ticker/價格，嘗試再降一層。
                        if isinstance(sub.columns, pd.MultiIndex):
                            for j in range(sub.columns.nlevels):
                                vals = set(sub.columns.get_level_values(j))
                                if vals & price_names:
                                    sub = sub.droplevel(j, axis=1)
                                    break
                        return sub
                except Exception:
                    pass
        return None
    except Exception:
        return None


def _yahoo_batch_download(tickers):
    """V2.10.21：Actions 用少量批次請求取得多檔 6 個月日線。"""
    if not tickers:
        return None
    try:
        print(
            f'技術批次：Yahoo 一次請求 {len(tickers)} 檔 '
            f'[{TECH_BATCH_PERIOD}/{TECH_BATCH_INTERVAL}]',
            flush=True
        )
        return yf.download(
            tickers=tickers,
            period=TECH_BATCH_PERIOD,
            interval=TECH_BATCH_INTERVAL,
            group_by='ticker',
            auto_adjust=False,
            progress=False,
            threads=False,
            timeout=TECH_BATCH_TIMEOUT
        )
    except Exception as e:
        print(f'技術批次：Yahoo 取得失敗 {len(tickers)} 檔：{type(e).__name__}: {e}', flush=True)
        return None


def refresh_all_technical_cache(u, force=False):
    """V2.10.21：GitHub Actions 全市場技術快取建立器。

    - 目標：動態市場股票池內全部 TWSE/TPEX 股票，另含 0050、QQQ、^TWII。
    - 先保留新鮮快取；只更新缺少或超過 36 小時的標的。
    - Yahoo 以 80 檔/批次下載 6 個月日線，避免 1985 次單檔 API 呼叫。
    - 每批成功後統一寫回一次 JSON，最後由既有 Actions git add -A 提交。
    - 批次失敗不清空舊快取；因此不會因單次 Yahoo 限流把 Render 可用資料變成 N/A。
    """
    if not isinstance(u, dict) or not u:
        print('技術批次：股票池為空，略過', flush=True)
        return 0

    cache = load_json(LINE_TECH_CACHE_FILE)
    if not isinstance(cache, dict):
        cache = {}

    # 不把完整市場 cache 放進 Render 的 LINE 即時路徑；只有 Actions 呼叫此函式。
    targets = []
    for code, item in u.items():
        code = clean_code(code)
        if not code or not str(code).isdigit():
            continue
        market = (item or {}).get('market')
        if market not in ('TWSE', 'TPEX'):
            continue
        ticker = (item or {}).get('symbol') or symbol_for(code, market)
        if force or not _technical_cache_is_fresh(cache, code):
            targets.append((code, ticker))

    # ETF / index 也維持在同一份 cache；不影響 1985 檔股票計數。
    extras = []
    for name, ticker in STOCKS.items():
        key = clean_code(ticker)
        if key and (force or not _technical_cache_is_fresh(cache, key)):
            extras.append((key, ticker))

    targets.extend(extras)
    # 去重但保留順序。
    seen = set()
    targets = [(c, t) for c, t in targets if not (c in seen or seen.add(c))]

    total_market = sum(
        1 for code, item in u.items()
        if clean_code(code).isdigit() and (item or {}).get('market') in ('TWSE', 'TPEX')
    )
    print(
        f'========== V2.10.21 全市場技術快取 ==========',
        flush=True
    )
    print(
        f'技術快取：市場股票 {total_market} 檔；需更新 {len(targets)} 檔；'
        f'目前快取 {sum(1 for k in cache if isinstance(cache.get(k), dict))} 檔',
        flush=True
    )

    if not targets:
        print('技術快取：全部在 36 小時有效期內，無需更新', flush=True)
        return sum(1 for k in cache if isinstance(cache.get(k), dict))

    success = 0
    failed = 0
    for offset in range(0, len(targets), TECH_BATCH_CHUNK):
        chunk = targets[offset:offset + TECH_BATCH_CHUNK]
        tickers = [t for _, t in chunk]
        batch = _yahoo_batch_download(tickers)
        chunk_success = 0

        if batch is not None and not batch.empty:
            for code, ticker in chunk:
                frame = _extract_batch_ticker_frame(batch, ticker)
                frame = _technical_from_yahoo_batch_frame(frame)
                result = _technical_from_df(frame)
                if result.get('price') is not None:
                    if _save_technical_cache_entry(cache, code, result, save=False):
                        chunk_success += 1
                else:
                    failed += 1

        # 批次完成後才寫一次，避免 80 次 I/O。
        try:
            save_json(LINE_TECH_CACHE_FILE, cache)
        except Exception as e:
            print(f'技術批次：快取寫入失敗：{e}', flush=True)

        success += chunk_success
        if chunk_success < len(chunk):
            failed += max(0, len(chunk) - chunk_success)
        print(
            f'技術批次進度：{min(offset + len(chunk), len(targets))}/{len(targets)} '
            f'（本批成功 {chunk_success}/{len(chunk)}）',
            flush=True
        )

    print(
        f'技術快取完成：本次成功更新 {success} 檔；失敗/未更新 {failed} 檔；'
        f'快取總數 {sum(1 for k in cache if isinstance(cache.get(k), dict))} 檔',
        flush=True
    )
    return sum(1 for k in cache if isinstance(cache.get(k), dict))


def technical(symbol):
    """V2.10.21 技術面。

    LINE 路徑：本機快取 -> GitHub 全市場快取 -> 不再主動打 Yahoo/TWSE。
    Actions 批次：若快取缺少/過期，先由 refresh_all_technical_cache() 建立；
    單檔 fallback 僅保留給非 LINE 的特殊情況。
    """
    cache_key = clean_code(str(symbol).split('.')[0]) or str(symbol)
    cache = load_json(LINE_TECH_CACHE_FILE)
    if not isinstance(cache, dict):
        cache = {}

    if LINE_MODE_ACTIVE:
        z = _load_technical_cache_entry(cache, cache_key, max_age=72 * 3600)
        if z:
            print(f'LINE技術面：使用本機全市場快取 {cache_key}', flush=True)
            return z

        # Render Free 只下載一次整份 GitHub JSON；若有目標股就直接取。
        remote = load_remote_json_cache(LINE_TECH_CACHE_FILE, timeout=4)
        z = _load_technical_cache_entry(remote, cache_key, max_age=72 * 3600)
        if z:
            try:
                cache[cache_key] = remote[cache_key]
                save_json(LINE_TECH_CACHE_FILE, cache)
            except Exception:
                pass
            print(f'LINE技術面：使用 GitHub 全市場快取 {cache_key}', flush=True)
            return z

        # 重要：Render Free 不再因為快取缺一檔就連續打 Yahoo + 3 個 TWSE 月份。
        print(
            f'LINE技術面：全市場快取沒有 {cache_key}，避免 Render Free 即時抓取，使用 N/A',
            flush=True
        )
        return _technical_from_df(None)

    # 非 LINE（Actions / analyze）單檔路徑：先使用快取，沒有才使用既有 Yahoo/TWSE 備援。
    z = _load_technical_cache_entry(cache, cache_key, max_age=TECH_CACHE_MAX_AGE)
    if z:
        return z

    d = None
    try:
        d = yf_download(symbol, '6mo', '1d')
    except Exception as e:
        print(f'技術面 Yahoo 失敗 {cache_key}：{type(e).__name__}', flush=True)

    if d is not None and not d.empty:
        try:
            c = pd.to_numeric(d['Close'], errors='coerce').dropna()
        except Exception:
            c = None
    else:
        c = None

    if c is None or len(c) < 60:
        try:
            tw = _twse_history_fallback(cache_key, 3)
            if tw is not None and not tw.empty:
                d = tw
        except Exception as e:
            print(f'技術面 TWSE 備援失敗 {cache_key}：{type(e).__name__}', flush=True)

    o = _technical_from_df(d)
    if o.get('price') is not None:
        _save_technical_cache_entry(cache, cache_key, o, save=True)
    return o


# ============================================================
# Chips
# ============================================================

def parse_t86(data):

    out = {}

    if not isinstance(data, dict):
        return out

    fields = data.get(
        'fields',
        []
    )

    for r in data.get(
        'data',
        []
    ):

        if not isinstance(
            r,
            list
        ):
            continue

        o = dict(
            zip(
                fields,
                r
            )
        )

        c = clean_code(
            first_value(
                o,
                [
                    '證券代號',
                    '公司代號'
                ]
            )
        )

        f = find_value(
            o,
            [
                '外陸資買賣超股數(不含外資自營商)',
                '外資及陸資買賣超股數'
            ]
        )

        t = find_value(
            o,
            [
                '投信買賣超股數'
            ]
        )

        d = find_value(
            o,
            [
                '自營商買賣超股數'
            ]
        )

        if c:

            out[c] = {
                'foreign':
                    f,
                'trust':
                    t,
                'dealer':
                    d,
                'total':
                    sum(
                        x
                        for x in (
                            f,
                            t,
                            d
                        )
                        if x is not None
                    )
            }

    return out


def institutional(
    code,
    market,
    days=20
):
    """
    V2.9.9 法人資料：
    - TWSE T86 timeout 由 4 秒提高至 10 秒
    - 暫時性失敗允許 1 次重試
    - 先抓最近 20 個交易日，若不足 20 日，再自動往前補抓 10 日
    - 已成功資料立即寫入 chip_history.json，避免單日 timeout 造成整批失敗
    """
    key = ('inst', market, days)

    if key in INSTITUTIONAL_CACHE:
        return INSTITUTIONAL_CACHE[key]

    # V2.10.19：LINE 查詢絕不載入完整 chip_history.json。
    # T86 每日回傳全市場資料，若把 20 天全部留在 Render 記憶體會很容易
    # 超過 512MB。LINE 模式改用只保存「查詢股票」的精簡快取。
    if LINE_MODE_ACTIVE:
        line_history = load_json(LINE_CHIP_CACHE_FILE)
        if not isinstance(line_history, dict) or not line_history:
            remote_line = load_remote_json_cache(LINE_CHIP_CACHE_FILE, timeout=4)
            line_history = remote_line if isinstance(remote_line, dict) else {}
        history = {'LINE': line_history}
        market_hist = history['LINE'].setdefault(market, {})

        # 若 LINE 專用快取尚未建立，直接從 GitHub 的 chip_history.json
        # 讀取目標股資料；只保留這一支股票，避免把全市場 20 日資料留在 Render。
        existing_days = sum(1 for x in market_hist.values() if isinstance(x, dict) and code in x)
        if existing_days < days:
            remote_full = load_remote_json_cache(CHIP_HISTORY_FILE, timeout=6)
            remote_market = remote_full.get(market, {}) if isinstance(remote_full, dict) else {}
            if isinstance(remote_market, dict):
                for ds, daydata in remote_market.items():
                    if isinstance(daydata, dict) and code in daydata:
                        market_hist[ds] = {code: daydata.get(code)}

    else:
        history = load_json(CHIP_HISTORY_FILE)
        market_hist = history.setdefault(market, {})
    today = datetime.now(TW_TZ).date()

    def weekday_dates(start_date, count):
        out = []
        d = start_date
        while len(out) < count:
            if d.weekday() < 5:
                out.append(d)
            d -= timedelta(days=1)
        return out

    # V2.9.9：把今天也納入候選；若 T86 尚未發布，該日會自然失敗，
    # 程式會繼續使用前一交易日資料。
    dates = weekday_dates(today, days)

    def fetch(dt):
        ds = dt.strftime('%Y%m%d')

        if market == 'TPEX':
            x = tpex_get(
                '/tpex_3insti_daily_trading',
                {'date': ds}
            )
            parsed = parse_tpex_inst(x) if x else {}
        else:
            x = http_json(
                TWSE_WEB_BASE + '/fund/T86',
                {
                    'date': ds,
                    'selectType': 'ALL',
                    'response': 'json'
                },
                timeout=10,
                retries=1
            )
            parsed = parse_t86(x) if x else {}

        # LINE 模式：解析後立刻只留下目標股票，不能把整個市場資料留在 memory。
        if LINE_MODE_ACTIVE:
            one = parsed.get(code) if isinstance(parsed, dict) else None
            return ds, ({code: one} if one else {})

        return ds, parsed

    def fetch_missing(target_dates):
        missing = [
            x for x in target_dates
            if x.strftime('%Y%m%d') not in market_hist
        ]

        print(
            f'法人資料：{market} 已有 '
            f'{len(target_dates)-len(missing)}/{len(target_dates)} 日快取，'
            f'需補 {len(missing)} 日'
        )

        if not missing:
            return

        from concurrent.futures import ThreadPoolExecutor, as_completed

        with ThreadPoolExecutor(max_workers=min(5, len(missing))) as ex:
            futs = [ex.submit(fetch, x) for x in missing]
            for f in as_completed(futs):
                try:
                    ds, data = f.result()
                    if data:
                        market_hist[ds] = data
                except Exception as e:
                    print('法人批次失敗：', e)

        if LINE_MODE_ACTIVE:
            save_json(
                LINE_CHIP_CACHE_FILE,
                history.get('LINE', {})
            )
        else:
            save_json(CHIP_HISTORY_FILE, history)

    fetch_missing(dates)

    # 若最近 20 個交易日仍不足 20 日，向前再補 10 個交易日。
    available = sum(
        1 for dt in dates
        if dt.strftime('%Y%m%d') in market_hist
    )

    if available < days:
        extended = weekday_dates(
            today - timedelta(days=1),
            days + 10
        )
        extra = [
            dt for dt in extended
            if dt.strftime('%Y%m%d') not in {
                x.strftime('%Y%m%d') for x in dates
            }
        ][:10]

        if extra:
            print(
                f'法人資料不足 {days} 日，追加往前補抓：{len(extra)} 日'
            )
            fetch_missing(extra)

    # 重新建立最近可用交易日清單，最多取 days 日。
    all_dates = weekday_dates(today, days + 10)
    usable = [
        dt for dt in all_dates
        if dt.strftime('%Y%m%d') in market_hist
    ][:days]

    result = [
        {
            'date': dt.strftime('%Y%m%d'),
            'data': market_hist[dt.strftime('%Y%m%d')]
        }
        for dt in usable
    ]

    INSTITUTIONAL_CACHE[key] = result

    print(
        f'法人資料完成：{len(result)} 個交易日'
        + ('（完整20日）' if len(result) >= days else '（目前不足20日）')
    )

    return result


def parse_tpex_inst(data):

    out = {}

    if not isinstance(
        data,
        list
    ):
        return out

    for r in data:

        c = clean_code(
            first_value(
                r,
                [
                    'SecuritiesCompanyCode',
                    'Code',
                    '證券代號'
                ]
            )
        )

        if not c:
            continue

        def net(b, s):

            a = find_value(
                r,
                b
            )

            z = find_value(
                r,
                s
            )

            return (
                a - z
                if (
                    a is not None
                    and z is not None
                )
                else None
            )

        f = net(
            [
                'Foreign Investors include Mainland Area Investors (Foreign Dealers excluded)-Total Buy',
                'ForeignInvestors-TotalBuy',
                'Foreign Buy'
            ],
            [
                'Foreign Investors include Mainland Area Investors (Foreign Dealers excluded)-Total Sell',
                'ForeignInvestors-TotalSell',
                'Foreign Sell'
            ]
        )

        t = net(
            [
                'SecuritiesInvestmentTrustCompanies-TotalBuy',
                'InvestmentTrust-TotalBuy'
            ],
            [
                'SecuritiesInvestmentTrustCompanies-TotalSell',
                'InvestmentTrust-TotalSell'
            ]
        )

        d = net(
            [
                'Dealers-TotalBuy',
                'Dealer-TotalBuy'
            ],
            [
                'Dealers-TotalSell',
                'Dealer-TotalSell'
            ]
        )

        out[c] = {
            'foreign':
                f,
            'trust':
                t,
            'dealer':
                d,
            'total':
                sum(
                    x
                    for x in (
                        f,
                        t,
                        d
                    )
                    if x is not None
                )
        }

    return out


def chip_sums(
    code,
    h
):

    v = [
        x.get(
            'data',
            {}
        )
        .get(
            code,
            {}
        )
        .get(
            'total'
        )
        for x in h
    ]

    v = [
        x
        for x in v
        if x is not None
    ]

    return {
        'latest':
            v[0]
            if v
            else None,
        '5d':
            sum(v[:5])
            if len(v) >= 5
            else None,
        '20d':
            sum(v[:20])
            if len(v) >= 20
            else None
    }


# ============================================================
# Margin
# ============================================================

def parse_margin_row(row):

    if not isinstance(
        row,
        dict
    ):

        return {
            'margin_change':
                None,
            'margin_balance':
                None,
            'short_change':
                None,
            'short_balance':
                None
        }

    margin_today = find_value(
        row,
        [
            '融資今日餘額',
            '今日融資餘額',
            'MarginTodayBalance',
            'MarginBalance',
            'margin_balance',
            '融資餘額',
            'MarginPurchaseTodayBalance'
        ]
    )

    margin_prev = find_value(
        row,
        [
            '融資前日餘額',
            '前日融資餘額',
            'MarginPreviousBalance',
            'PreviousMarginBalance',
            '融資昨日餘額',
            'MarginPurchasePreviousBalance'
        ]
    )

    short_today = find_value(
        row,
        [
            '融券今日餘額',
            '今日融券餘額',
            'ShortTodayBalance',
            'ShortBalance',
            'short_balance',
            '融券餘額',
            'ShortSaleTodayBalance'
        ]
    )

    short_prev = find_value(
        row,
        [
            '融券前日餘額',
            '前日融券餘額',
            'ShortPreviousBalance',
            'PreviousShortBalance',
            '融券昨日餘額',
            'ShortSalePreviousBalance'
        ]
    )

    mc = (
        margin_today
        - margin_prev
        if (
            margin_today is not None
            and margin_prev is not None
        )
        else find_value(
            row,
            [
                '融資增減',
                '融資變化',
                'MarginChange',
                'margin_change'
            ]
        )
    )

    sc = (
        short_today
        - short_prev
        if (
            short_today is not None
            and short_prev is not None
        )
        else find_value(
            row,
            [
                '融券增減',
                '融券變化',
                'ShortChange',
                'short_change'
            ]
        )
    )

    return {
        'margin_change':
            mc,
        'margin_balance':
            margin_today,
        'short_change':
            sc,
        'short_balance':
            short_today
    }


def _parse_margin_payload(
    data
):

    out = {}

    def consume_rows(
        rows,
        fields=None
    ):

        if not isinstance(
            rows,
            list
        ):
            return

        for r in rows:

            if isinstance(
                r,
                dict
            ):

                o = r

                c = clean_code(
                    first_value(
                        o,
                        [
                            '股票代號',
                            '證券代號',
                            '公司代號',
                            'Code',
                            'SecuritiesCompanyCode'
                        ]
                    )
                )

                if (
                    c
                    and c.isdigit()
                ):

                    out[c] = (
                        parse_margin_row(
                            o
                        )
                    )

            elif (
                isinstance(r, list)
                and fields
            ):

                o = dict(
                    zip(
                        fields,
                        r
                    )
                )

                c = clean_code(
                    first_value(
                        o,
                        [
                            '股票代號',
                            '證券代號',
                            '公司代號',
                            'Code',
                            'SecuritiesCompanyCode'
                        ]
                    )
                )

                if (
                    c
                    and c.isdigit()
                ):

                    out[c] = (
                        parse_margin_row(
                            o
                        )
                    )

    if isinstance(
        data,
        list
    ):

        consume_rows(data)

        if (
            data
            and isinstance(
                data[0],
                list
            )
        ):

            fields = [
                str(x)
                for x in data[0]
            ]

            consume_rows(
                data[1:],
                fields
            )

    elif isinstance(
        data,
        dict
    ):

        fields = data.get(
            'fields',
            []
        )

        rows = data.get(
            'data',
            []
        )

        consume_rows(
            rows,
            fields
        )

        if isinstance(
            rows,
            list
        ):

            for table in rows:

                if (
                    isinstance(
                        table,
                        list
                    )
                    and table
                    and isinstance(
                        table[0],
                        list
                    )
                ):

                    header = [
                        str(x)
                        for x in table[0]
                    ]

                    consume_rows(
                        table[1:],
                        header
                    )

    return out


def margin_data(
    code,
    market
):

    key = (
        'margin',
        market
    )

    if key not in MARGIN_CACHE:

        out = {}

        try:

            if market == 'TPEX':

                data = tpex_get(
                    '/tpex_mainboard_margin_balance'
                )

                out = (
                    _parse_margin_payload(
                        data
                    )
                )

            else:

                data = twse_get(
                    '/exchangeReport/MI_MARGN'
                )

                out = (
                    _parse_margin_payload(
                        data
                    )
                )

                if not out:

                    data = twse_web_get(
                        '/exchangeReport/MI_MARGN',
                        {
                            'response':
                                'json',
                            'selectType':
                                'ALL'
                        }
                    )

                    out = (
                        _parse_margin_payload(
                            data
                        )
                    )

        except Exception as e:

            print(
                '融資融券批次失敗：',
                e
            )

        MARGIN_CACHE[key] = out

        print(
            f'融資融券資料完成：'
            f'{market} '
            f'{len(out)} 檔（只抓一次）'
        )

    return MARGIN_CACHE[key].get(
        code,
        {
            'margin_change':
                None,
            'margin_balance':
                None,
            'short_change':
                None,
            'short_balance':
                None
        }
    )


def _line_chip_fast(code, market, days=20):
    """V2.10.21：LINE 完全快取優先的法人資料。

    優先讀 Actions 產生的 line_chip_cache.json。快取不足時不再自動補抓
    20 個 T86 日期；最多只打一個 3 秒、零重試的最新日請求，避免 Render Free
    被 TWSE timeout 卡住。
    """
    code = clean_code(code)
    cache = _load_line_small_cache(LINE_CHIP_CACHE_FILE)
    market_hist = cache.get(market, {}) if isinstance(cache, dict) else {}
    if not isinstance(market_hist, dict):
        market_hist = {}

    dates = sorted(
        [ds for ds, data in market_hist.items()
         if isinstance(data, dict) and code in data],
        reverse=True
    )[:days]
    if dates:
        result = [
            {'date': ds, 'data': {code: market_hist[ds].get(code)}}
            for ds in dates
        ]
        print(f'LINE籌碼：使用快取 {code} {len(result)}/{days} 日', flush=True)
        return result

    # 快取真的不存在時，只取最新一天；成功就寫入本機，下一次直接使用。
    today = datetime.now(TW_TZ).date()
    for back in range(0, 4):
        dt = today - timedelta(days=back)
        if dt.weekday() >= 5:
            continue
        ds = dt.strftime('%Y%m%d')
        try:
            if market == 'TPEX':
                data = http_json(
                    TPEX_BASE + '/tpex_3insti_daily_trading',
                    {'date': ds}, timeout=LINE_FAST_TIMEOUT, retries=0
                )
                parsed = parse_tpex_inst(data) if data else {}
            else:
                data = http_json(
                    TWSE_WEB_BASE + '/fund/T86',
                    {'date': ds, 'selectType': 'ALL', 'response': 'json'},
                    timeout=LINE_FAST_TIMEOUT, retries=0
                )
                parsed = parse_t86(data) if data else {}
            one = parsed.get(code) if isinstance(parsed, dict) else None
            if one:
                market_hist[ds] = {code: one}
                cache[market] = market_hist
                _save_line_small_cache(LINE_CHIP_CACHE_FILE, cache)
                print(f'LINE籌碼：快速取得最新日 {code}', flush=True)
                return [{'date': ds, 'data': {code: one}}]
        except Exception as e:
            print(f'LINE籌碼：快速取得失敗 {code}/{ds}：{type(e).__name__}', flush=True)
            break
    print(f'LINE籌碼：無快取且快速 API 無資料 {code}，不再等待', flush=True)
    return []


def _line_margin_fast(code, market):
    """V2.10.21：LINE 融資融券快取優先，避免 Render 即時打全市場 API。"""
    code = clean_code(code)
    cache = _load_line_small_cache(LINE_MARGIN_CACHE_FILE)
    market_data = cache.get(market, {}) if isinstance(cache, dict) else {}
    if isinstance(market_data, dict) and isinstance(market_data.get(code), dict):
        print(f'LINE籌碼：使用融資快取 {code}', flush=True)
        return market_data[code]

    # 快取缺失時，只做一次短 timeout 的官方全市場查詢。
    try:
        if market == 'TPEX':
            data = http_json(TPEX_BASE + '/tpex_mainboard_margin_balance',
                             timeout=LINE_FAST_TIMEOUT, retries=0)
        else:
            data = http_json(TWSE_BASE + '/exchangeReport/MI_MARGN',
                             timeout=LINE_FAST_TIMEOUT, retries=0)
        parsed = _parse_margin_payload(data)
        one = parsed.get(code)
        if one:
            cache.setdefault(market, {})[code] = one
            _save_line_small_cache(LINE_MARGIN_CACHE_FILE, cache)
            print(f'LINE籌碼：快速取得融資資料 {code}', flush=True)
            return one
    except Exception as e:
        print(f'LINE籌碼：快速融資取得失敗 {code}：{type(e).__name__}', flush=True)
    return {'margin_change': None, 'margin_balance': None,
            'short_change': None, 'short_balance': None}


# ============================================================
# Scoring
# ============================================================

def score_fund(
    pe,
    one,
    peer,
    peg,
    roe,
    eps,
    pb,
    yld,
    model
):

    s = 0
    why = []

    if model.get('pe'):

        if (
            pe is not None
            and one is not None
        ):

            r = pe / one

            if r <= .9:

                s += 10

                why.append(
                    '低於自身歷史PE'
                )

            elif r <= 1.05:

                s += 7

                why.append(
                    '接近自身歷史PE'
                )

            elif r <= 1.15:

                s += 4

        if (
            pe is not None
            and peer is not None
        ):

            r = pe / peer

            if r < .85:

                s += 10

                why.append(
                    '低於同次產業中位數'
                )

            elif r <= 1.05:

                s += 7

                why.append(
                    '接近同次產業中位數'
                )

            elif r <= 1.15:

                s += 3

    if peg is not None:

        if peg < .8:

            s += 6

        elif peg < 1:

            s += 5

        elif peg < 1.2:

            s += 3

    if roe is not None:

        if roe >= 30:

            s += 6

        elif roe >= 20:

            s += 5

        elif roe >= 10:

            s += 3

    if eps is not None:

        if eps >= 50:

            s += 6

        elif eps >= 20:

            s += 5

        elif eps > 0:

            s += 3

    if (
        pb is not None
        and model.get('pb')
    ):

        if pb < 2:

            s += 4

        elif pb < 4:

            s += 2

    if (
        yld is not None
        and model.get('yield')
    ):

        if yld >= 5:

            s += 3

        elif yld >= 3:

            s += 2

    return (
        min(40, s),
        why
    )


def score_tech(t):

    s = 0
    reasons = []

    r = t['rsi']
    k = t['k']
    d = t['d']
    p = t['price']
    m20 = t['ma20']
    m60 = t['ma60']
    dist = t['distance_low']

    if r is not None:

        if 30 <= r <= 45:

            s += 7
            reasons.append(
                'RSI偏低'
            )

        elif 45 < r <= 60:

            s += 6

        elif r < 70:

            s += 4

    if (
        k is not None
        and d is not None
    ):

        if (
            k < 30
            and k > d
        ):

            s += 7
            reasons.append(
                'KD低檔轉強'
            )

        elif k < 40:

            s += 5

        elif k > d:

            s += 4

    if p and m20:

        if p >= m20:

            s += 5

        elif p >= m20 * .97:

            s += 3

    if p and m60:

        if p >= m60:

            s += 5

        elif p >= m60 * .95:

            s += 3

    if t['trend'] == '多頭':

        s += 3

    elif t['trend'] == '震盪':

        s += 2

    if dist is not None:

        if dist <= .03:

            s += 3

            reasons.append(
                '接近20日低點'
            )

        elif dist <= .08:

            s += 2

    return (
        min(30, s),
        reasons
    )


def score_chip(c, m):

    s = 0
    r = []

    for key, w in [
        ('latest', 5),
        ('5d', 6),
        ('20d', 7)
    ]:

        v = c.get(key)

        if v is not None:

            if v > 0:

                s += w

            elif (
                v < 0
                and key == '20d'
            ):

                r.append(
                    '法人20日賣超'
                )

    mc = m.get(
        'margin_change'
    )

    sc = m.get(
        'short_change'
    )

    if mc is not None:

        if mc < 0:

            s += 1

        elif mc > 0:

            r.append(
                '融資增加'
            )

    if (
        sc is not None
        and sc > 0
    ):

        s += 1

    return (
        min(20, s),
        r
    )


def score_risk(
    t,
    c,
    m
):

    risk = 0
    r = []

    if (
        t.get('rsi') is not None
        and t['rsi'] > 70
    ):

        risk += 3

        r.append(
            'RSI過熱'
        )

    if (
        t.get('k') is not None
        and t.get('d') is not None
        and t['k'] > 80
        and t['d'] > 80
    ):

        risk += 2

        r.append(
            'KD高檔'
        )

    if (
        c.get('20d') is not None
        and c['20d'] < 0
    ):

        risk += 2

        r.append(
            '法人連續賣超'
        )

    if (
        m.get('margin_change')
        is not None
        and m['margin_change'] > 0
    ):

        risk += 1

        r.append(
            '融資增加'
        )

    if (
        t.get('price')
        and t.get('ma20')
        and t['price']
        < t['ma20']
    ):

        risk += 1

        r.append(
            '跌破MA20'
        )

    if (
        t.get('price')
        and t.get('ma60')
        and t['price']
        < t['ma60']
    ):

        risk += 1

        r.append(
            '跌破MA60'
        )

    return (
        min(10, risk),
        r
    )


def yahoo_light_fund(symbol, official=None):
    """V2.10.19 Render Free 輕量基本面。

    優先使用本機 LINE 基本面快取；只有快取缺欄位才詢問 Yahoo。
    Yahoo 被限流/SSL/網路失敗時，直接保留既有值，不讓整個 LINE 分析失敗。
    """
    o = {
        'eps_growth': None,
        'roe': None,
        'peg': None,
        'pb': None,
        'yield': None,
        'pe': None
    }

    if isinstance(official, dict):
        o['pe'] = to_float(official.get('pe'))
        o['pb'] = to_float(official.get('pb'))
        o['yield'] = to_float(official.get('yield'))

    code = clean_code(symbol)
    try:
        # symbol 通常是 2330.TW / 3711.TW；快取鍵統一去掉市場尾碼。
        code = clean_code(str(symbol).split('.')[0]) or code
    except Exception:
        pass

    cache = load_json(LINE_FUND_CACHE_FILE)
    if not isinstance(cache, dict):
        cache = {}
    if not cache:
        remote = load_remote_json_cache(LINE_FUND_CACHE_FILE, timeout=4)
        if isinstance(remote, dict):
            cache = remote

    cached = cache.get(code, {})
    if isinstance(cached, dict):
        for k in ('eps_growth', 'roe', 'peg'):
            v = to_float(cached.get(k))
            if v is not None:
                o[k] = v

    missing = [
        k for k in ('eps_growth', 'roe', 'peg')
        if o.get(k) is None
    ]

    # LINE 查詢只允許一次輕量 Yahoo fundamentals-timeseries。
    if missing:
        try:
            ts = yahoo_timeseries_fund(symbol)
            for k in missing:
                if ts.get(k) is not None:
                    o[k] = ts[k]
        except Exception as e:
            print(
                f'LINE輕量基本面 Yahoo timeseries失敗 {symbol}: '
                f'{type(e).__name__}: {e}',
                flush=True
            )

    # V2.10.19：免費 PE/PB fallback。ROE 缺失時，用同口徑 PB/PE 推導；
    # 若 Yahoo 正式 ROE 已存在，絕不覆蓋。
    if o.get('roe') is None:
        pe_for_roe=to_float(o.get('pe')); pb_for_roe=to_float(o.get('pb'))
        if pe_for_roe and pb_for_roe and pe_for_roe>0 and pb_for_roe>0:
            o['roe']=pb_for_roe/pe_for_roe*100
            print(f'LINE基本面：ROE 使用 PE/PB 推導 {code} = {o["roe"]:.2f}%',flush=True)

    # 只保存單一標的的少量數字，不保存 Yahoo DataFrame / info。
    try:
        cache[code] = {
            'eps_growth': o.get('eps_growth'),
            'roe': o.get('roe'),
            'peg': o.get('peg'),
            '_cached_at': time.time()
        }
        save_json(LINE_FUND_CACHE_FILE, cache)
    except Exception as e:
        print(f'LINE基本面快取保存失敗 {code}: {e}', flush=True)

    return o


# ============================================================
# Analysis
# ============================================================

def analysis(
    query,
    u,
    backfill=True,
    interval_result=None,
    line_light=False
):

    item = resolve_stock(
        query,
        u
    )

    if not item:

        return (
            f'❌ 找不到股票：{query}'
        )

    code = item['code']
    name = item['name']
    market = item['market']

    industry = canonical_industry(
        item['industry']
    )

    symbol = item['symbol']

    # --------------------------------------------------------
    # 次產業
    # --------------------------------------------------------

    subindustries = (
        get_subindustries_for_stock(
            code,
            item
        )
    )

    # V2.10.5：LINE/Render 不依賴既有次產業快取。
    if not subindustries:
        subindustries = ensure_subindustry_for_query(
            code,
            item
        )

    # V2.10.5 修正：ensure_subindustry_for_query() 成功後，
    # 立即把最新次產業同步回 LINE 查詢使用的市場股票池。
    if subindustries:
        item['subindustries'] = list(dict.fromkeys(
            normalize_subindustry(x)
            for x in subindustries
            if normalize_subindustry(x)
        ))
        item['subindustry'] = item['subindustries'][0]

    subindustry_display = (
        '、'.join(
            normalize_subindustry(x)
            for x in subindustries
            if normalize_subindustry(x)
        )
        if subindustries
        else '次產業資料不可用'
    )

    # --------------------------------------------------------
    # PE
    # --------------------------------------------------------

    print(f'LINE輕量分析：取得目前PE {code}' if line_light else '取得目前PE', flush=True)
    pe_data = get_current_pe_data()

    h = load_json(
        PE_HISTORY_FILE
    )

    if backfill:

        print(f'LINE輕量分析：PE歷史 {code}', flush=True) if line_light else None
        h = backfill_pe(
            code,
            h,
            market
        )

        save_json(
            PE_HISTORY_FILE,
            h
        )

    # 官方 PE/PB/殖利率資料先取出，再交給 LINE 輕量基本面路徑。
    # V2.10.19 修正：原本 off 在 yahoo_light_fund() 呼叫後才建立，
    # 導致 LINE 查詢出現 UnboundLocalError。
    off = pe_data.get(
        code,
        {}
    )

    if line_light:
        print(f'LINE輕量分析：基本面資料 {code}', flush=True)
        yf_f = yahoo_light_fund(symbol, off)
    else:
        yf_f = yahoo_fund(
            symbol
        )

    pe = (
        off.get('pe')
        or yf_f.get('pe')
    )

    pb = (
        off.get('pb')
        or yf_f.get('pb')
    )

    yld = (
        off.get('yield')
        or yf_f.get('yield')
    )

    one, sample = one_year_pe(
        code,
        h
    )

    # --------------------------------------------------------
    # V2.9.8
    # 動態次產業 Top 10
    # --------------------------------------------------------

    print(f'LINE輕量分析：建立同次產業 Top10 {code}', flush=True) if line_light else None
    peers = get_dynamic_subindustry_peers(
        code,
        industry,
        subindustries,
        u,
        10
    )

    vals = []
    for peer_item in peers:
        peer_pe = (
            pe_data
            .get(peer_item['code'], {})
            .get('pe')
        )

        if (
            not peer_pe
            and not line_light
        ):
            # 一般批次模式才對同業使用 Yahoo 備援。
            peer_f = yahoo_fund(peer_item['symbol'])
            peer_pe = peer_f.get('pe')

        if (
            peer_pe is not None
            and 0 < peer_pe <= PE_MAX_VALID
        ):
            vals.append(peer_pe)

    peer_mean = (
        sum(vals) / len(vals)
        if vals
        else None
    )

    peer_med = (
        float(
            np.median(vals)
        )
        if vals
        else None
    )

    # --------------------------------------------------------
    # Technical
    # --------------------------------------------------------

    print(f'LINE輕量分析：技術面 {code}', flush=True) if line_light else None
    tech = technical(
        symbol
    )

    # --------------------------------------------------------
    # Chips
    # --------------------------------------------------------

    print(f'LINE輕量分析：籌碼面 {code}', flush=True) if line_light else None
    if line_light:
        inst = chip_sums(code, _line_chip_fast(code, market, 20))
        margin = _line_margin_fast(code, market)
    else:
        inst = chip_sums(code, institutional(code, market, 20))
        margin = margin_data(code, market)

    # --------------------------------------------------------
    # Score
    # --------------------------------------------------------

    fs, fr = score_fund(
        pe,
        one,
        peer_med,
        yf_f['peg'],
        yf_f['roe'],
        yf_f['eps_growth'],
        pb,
        yld,
        INDUSTRY_MODEL.get(
            industry,
            DEFAULT_MODEL
        )
    )

    ts, tr = score_tech(
        tech
    )

    cs, cr = score_chip(
        inst,
        margin
    )

    risk, rr = score_risk(
        tech,
        inst,
        margin
    )

    total = max(
        0,
        min(
            100,
            fs
            + ts
            + cs
            + (10 - risk)
        )
    )

    if total >= 90:

        verdict = (
            '🟢 強力加碼'
        )

    elif total >= 75:

        verdict = (
            '🟢 可分批加碼'
        )

    elif total >= 60:

        verdict = (
            '🟡 等待回檔/止跌'
        )

    elif total >= 40:

        verdict = (
            '🟠 暫緩加碼'
        )

    else:

        verdict = (
            '🔴 不建議加碼'
        )

    interval = (
        u.get(
            code,
            {}
        ).get(
            'price'
        )
    )

    # --------------------------------------------------------
    # Webhook / analyze 模式
    # --------------------------------------------------------

    if (
        interval_result is None
        and not RUN_CACHE.get(
            (
                'interval_attempted',
                symbol
            ),
            False
        )
    ):

        st = (
            load_json(
                STATE_FILE
            )
            .get(
                'interval_low',
                {}
            )
            .get(
                next(
                    (
                        k
                        for k, v
                        in STOCKS.items()
                        if clean_code(v)
                        == code
                    ),
                    ''
                ),
                {}
            )
        )

        if (
            st.get(
                'last_check'
            )
            and st.get(
                'last_price'
            )
        ):

            z = get_interval_stats(
                symbol,
                st.get(
                    'last_check'
                )
            )

            RUN_CACHE[
                (
                    'interval_attempted',
                    symbol
                )
            ] = True

            if z:

                interval_result = {
                    'previous_price':
                        st.get(
                            'last_price'
                        ),
                    'interval_low':
                        z['low'],
                    'drop':
                        z['low']
                        / st.get(
                            'last_price'
                        )
                        - 1,
                    'start':
                        z['start'].isoformat(),
                    'end':
                        z['end'].isoformat()
                }

    # --------------------------------------------------------
    # 同次產業 Top 10
    # --------------------------------------------------------

    if peers:

        peer_text = '、'.join(
            f"{x['code']} {x['name']}"
            for x in peers
        )

    elif not subindustries:

        peer_text = (
            '⚠️ 無次產業資料，'
            '本次不進行同次產業排名'
        )

    else:

        peer_text = (
            '⚠️ 找不到相同次產業且有市值資料的股票'
        )

    # --------------------------------------------------------
    # Output
    # --------------------------------------------------------

    return (
        f'📊 股票加碼分析 V2.10.19\n\n'
        f'標的：{name}（{code}）\n'
        f'市場：{market}\n'
        f'產業：{industry}\n'
        f'次產業：{subindustry_display}\n\n'

        f'【估值 / 基本面 40分】\n'
        f'PE：{fmt(pe)}\n'
        f'一年平均PE：{fmt(one)}'
        f'（樣本 {sample}）\n'
        f'同次產業Top10平均PE：'
        f'{fmt(peer_mean)}\n'
        f'同次產業Top10中位數PE：'
        f'{fmt(peer_med)}'
        f'（有效 {len(vals)}/10）\n'
        f'PB：{fmt(pb)}\n'
        f'殖利率：{fmt(yld)}%\n'
        f'EPS成長：'
        f'{fmt(yf_f["eps_growth"])}%\n'
        f'PEG：{fmt(yf_f["peg"])}\n'
        f'ROE：{fmt(yf_f["roe"])}%\n'
        f'基本面得分：{fs}/40\n\n'

        f'【動態次產業 Top 10】\n'
        f'{peer_text}\n\n'

        f'【技術面 30分】\n'
        f'RSI：{fmt(tech["rsi"])}\n'
        f'KD：'
        f'K={fmt(tech["k"])} / '
        f'D={fmt(tech["d"])}\n'
        f'MA20：{fmt(tech["ma20"])}\n'
        f'MA60：{fmt(tech["ma60"])}\n'
        f'趨勢：'
        f'{tech["trend"] or "N/A"}\n'
        f'距20日低點：'
        f'{pct(tech["distance_low"])}\n'
        f'技術得分：{ts}/30\n\n'

        f'【籌碼面 20分】\n'
        f'法人最新：'
        f'{fmt(inst["latest"],0)} 股\n'
        f'法人5日：'
        f'{fmt(inst["5d"],0)} 股\n'
        f'法人20日：'
        f'{fmt(inst["20d"],0)} 股\n'
        f'融資變化：'
        f'{fmt(margin["margin_change"],0)} 張\n'
        f'融資餘額：'
        f'{fmt(margin["margin_balance"],0)} 張\n'
        f'融券變化：'
        f'{fmt(margin["short_change"],0)} 張\n'
        f'融券餘額：'
        f'{fmt(margin["short_balance"],0)} 張\n'
        f'籌碼得分：{cs}/20\n\n'

        f'【風險 10分】\n'
        f'風險扣分：-{risk}\n'
        f'{("、".join(rr) if rr else "目前無主要風險警訊")}\n\n'

        f'【加碼決策】\n'
        f'綜合評分：{total}/100\n'
        f'結論：{verdict}\n\n'

        f'加分因素：'
        f'{"、".join(fr + tr) if fr + tr else "無"}\n'
        f'籌碼訊號：'
        f'{"、".join(cr) if cr else "無"}\n'
        f'風險提醒：'
        f'{"、".join(rr) if rr else "目前無主要風險警訊"}'
    )


# ============================================================
# Webhook / LINE 即時查詢 V2.10.5
# ============================================================


def verify_line_signature(body, signature):
    """使用 LINE Channel Secret 驗證原始 webhook body。"""
    if not LINE_CHANNEL_SECRET or not signature:
        return False

    digest = hmac.new(
        LINE_CHANNEL_SECRET.encode('utf-8'),
        body,
        hashlib.sha256
    ).digest()

    expected = base64.b64encode(digest).decode('ascii')
    return hmac.compare_digest(expected, signature)


def _line_text_messages(msg):
    """LINE 單則文字最多 5000 字；超過時切成多則。"""
    text = str(msg or '')
    if not text:
        return ['']
    return [text[i:i + 5000] for i in range(0, len(text), 5000)]


def _line_headers():
    return {
        'Authorization': f'Bearer {LINE_TOKEN}',
        'Content-Type': 'application/json'
    }


def push_line(to, msg):
    """背景分析完成後，使用 Push API 回傳給原本的聊天室。"""
    if not LINE_TOKEN or not to:
        print('LINE Push 略過：缺少 LINE token 或聊天室 ID')
        return False

    try:
        messages = [
            {'type': 'text', 'text': x}
            for x in _line_text_messages(msg)[:5]
        ]

        r = requests.post(
            LINE_PUSH_URL,
            headers=_line_headers(),
            json={
                'to': to,
                'messages': messages
            },
            timeout=20
        )

        if r.status_code != 200:
            print(
                f'LINE Push失敗：{r.status_code} '
                f'{r.text[:500]}'
            )
            return False

        print(f'LINE Push成功：{to[:12]}...')
        return True

    except Exception as e:
        print('LINE Push例外：', e)
        return False


def line_target_from_event(e):
    """取得 Push API 的聊天室目標：userId / groupId / roomId。"""
    source = e.get('source') or {}
    source_type = source.get('type')

    if source_type == 'user':
        return source.get('userId')
    if source_type == 'group':
        return source.get('groupId')
    if source_type == 'room':
        return source.get('roomId')

    return None


def _background_line_analysis(text, target, u, event_id=None):
    """V2.10.21：低記憶體背景完整分析；技術面改用全市場 GitHub 快取。

    使用 ThreadPoolExecutor（非 daemon）而非裸 daemon Thread，並在分析前後
    明確記錄狀態；完成後用 Push API 回原聊天室。
    """
    global LINE_MODE_ACTIVE
    LINE_MODE_ACTIVE = True
    try:
        print(
            f'LINE背景分析開始：{text} -> '
            f'{str(target)[:12]}...'
        )
        print('LINE背景分析：進入查詢資料準備', flush=True)

        with LINE_ANALYSIS_LOCK:
            RUN_CACHE['line_mode'] = True
            print('LINE背景分析：建立/載入市場資料', flush=True)
            query_u = u if isinstance(u, dict) and u else build_line_query_universe(text)
            print(f'LINE背景分析：市場資料完成 {len(query_u)} 檔', flush=True)
            print('LINE背景分析：同步次產業', flush=True)
            query_u = prepare_line_subindustries(query_u, text)
            print('LINE背景分析：開始 LINE 輕量查詢專用分析', flush=True)
            result = analysis(text, query_u, True, line_light=True)
            print('LINE背景分析：輕量分析完成', flush=True)

        if not result:
            result = f'❌ {text} 分析沒有產生結果。'

        if not push_line(target, result):
            print(f'❌ LINE背景分析完成，但 Push 失敗：{text}', flush=True)
        else:
            print(f'✅ LINE背景分析完成：{text}', flush=True)

    except Exception as e:
        traceback.print_exc()
        print(f'❌ LINE背景分析例外：{type(e).__name__}: {e}', flush=True)
        push_line(
            target,
            f'❌ {text} 分析失敗：{e}'
        )
    finally:
        LINE_MODE_ACTIVE = False
        release_line_memory()


def _mark_line_event_seen(event_id):
    """避免 LINE webhook 重試造成同一事件重複分析。"""
    if not event_id:
        return True

    with LINE_EVENT_LOCK:
        if event_id in LINE_SEEN_EVENTS:
            return False
        LINE_SEEN_EVENTS.add(event_id)
        if len(LINE_SEEN_EVENTS) > LINE_SEEN_EVENT_MAX:
            # set 沒有順序；超過上限時清空即可，目的只是短期去重。
            LINE_SEEN_EVENTS.clear()
            LINE_SEEN_EVENTS.add(event_id)
        return True


def prepare_line_subindustries(u, query):
    """V2.10.19：LINE 查詢前只同步「目標大產業」的必要次產業。

    Render Free 不建立完整 1985 檔次產業快取；收到 2330/3711 後，
    只找出該股票的大產業，先補目標股，再補同大產業市值前 80 檔。
    這樣可以保留動態 Top 10，又避免啟動時 OOM。
    """
    if not isinstance(u, dict) or not u:
        return u

    item = resolve_stock(query, u)
    if not item:
        return u

    target_code = clean_code(item.get('code'))
    target_industry = canonical_industry(item.get('industry'))

    targets = [target_code]
    candidates = []
    for code, x in u.items():
        if clean_code(code) == target_code:
            continue
        if canonical_industry(x.get('industry')) != target_industry:
            continue
        cap = to_float(x.get('market_cap'))
        if cap is not None:
            candidates.append((cap, clean_code(code)))
    candidates.sort(reverse=True)
    targets.extend(c for _, c in candidates[:80])
    targets = list(dict.fromkeys(targets))

    cache = load_json(SUBINDUSTRY_CACHE_FILE)
    data = cache.get('data', {}) if isinstance(cache, dict) else {}
    if not isinstance(data, dict):
        data = {}

    # Render Free 本機檔案在重新部署後可能不存在；優先合併 GitHub Actions
    # 已成功取得的公開次產業快取，避免 LINE 再次依賴 TPEx 網路。
    if not data:
        remote_cache = load_remote_subindustry_cache()
        remote_data = remote_cache.get('data', {}) if isinstance(remote_cache, dict) else {}
        if isinstance(remote_data, dict):
            data.update(remote_data)

    missing = []
    for code in targets:
        info = data.get(code)
        subs = info.get('subindustries', []) if isinstance(info, dict) else []
        if not any(normalize_subindustry(x) for x in subs if normalize_subindustry(x)):
            missing.append(code)

    print(
        f'LINE次產業同步：目標={target_code}、同大產業候選={len(candidates)}、'
        f'需補={len(missing)}'
    )

    if missing:
        # Render Free / LINE 查詢不允許一次補數百檔；只補目標與少量最相關股票。
        fetch_targets = missing[:20]
        if len(missing) > len(fetch_targets):
            print(
                f'LINE次產業同步：缺少 {len(missing)} 檔，只補前 {len(fetch_targets)} 檔，'
                '其餘等待 GitHub Actions 公開快取',
                flush=True
            )
        fetched = _fetch_missing_value_chains(fetch_targets)
        data.update(fetched)
        if fetched:
            save_json(
                SUBINDUSTRY_CACHE_FILE,
                {
                    '_cached_at': cache.get('_cached_at', time.time()),
                    'source': 'TPEx/TWSE Industry Value Chain',
                    'source_url': VALUE_CHAIN_BASE,
                    'cache_days': SUBINDUSTRY_CACHE_DAYS,
                    'data': data
                }
            )

    global SUBINDUSTRY_CACHE
    SUBINDUSTRY_CACHE = data
    return attach_subindustries(u, data)


def build_line_query_universe(query):
    """V2.10.21：LINE 查詢專用市場資料。

    不在 Render 啟動時建立完整股票池；只有真正收到股票查詢時才建立一次
    市場 metadata。這保留動態次產業/Top10 所需的 code、industry、market_cap，
    但避免 Web Service 啟動時同時載入次產業與大量快取。
    """
    # 若本機已有近期股票池快取，直接使用；不強制刷新。
    try:
        c = load_json(UNIVERSE_CACHE_FILE)
        d = c.get('data') if isinstance(c, dict) else None
        t = c.get('_cached_at', 0) if isinstance(c, dict) else 0
        if isinstance(d, dict) and d and time.time() - t < UNIVERSE_CACHE_HOURS * 3600:
            return d
    except Exception as e:
        print(f'LINE股票池快取讀取失敗：{e}')

    # V2.10.21：Render 冷啟動優先讀 Actions 提交的 GitHub 市場快取。
    # 只有遠端快取也不存在時，才建立 1985 檔 metadata。
    remote = load_remote_json_cache(UNIVERSE_CACHE_FILE, timeout=LINE_REMOTE_CACHE_TIMEOUT)
    rd = remote.get('data') if isinstance(remote, dict) else None
    rt = remote.get('_cached_at', 0) if isinstance(remote, dict) else 0
    if isinstance(rd, dict) and rd:
        if not rt or time.time() - float(rt) < (UNIVERSE_CACHE_HOURS + 24) * 3600:
            print(f'LINE查詢：使用 GitHub 市場快取 {len(rd)} 檔', flush=True)
            return rd

    print('LINE查詢：GitHub 市場快取不可用，建立市場 metadata', flush=True)
    u = build_universe()
    return u or {}


def release_line_memory():
    """V2.10.19：清除 LINE 查詢期間的大型一次性快取。"""
    # 分析完成後整個 RUN_CACHE 都不再需要；尤其 Yahoo DataFrame / info
    # 若留在全域 dict，Render 長時間運作後會逐次累積。
    RUN_CACHE.clear()
    PE_DATE_CACHE.clear()
    INSTITUTIONAL_CACHE.clear()
    MARGIN_CACHE.clear()
    SUBINDUSTRY_CACHE.clear()
    try:
        import gc
        gc.collect()
    except Exception:
        pass


def handle_event(e, u):
    """V2.10.21：立即 Reply 確認，再用低記憶體背景分析並 Push。"""
    if (
        e.get('type') != 'message'
        or e.get('message', {}).get('type') != 'text'
    ):
        return

    text = e.get('message', {}).get('text', '').strip()
    token = e.get('replyToken')
    target = line_target_from_event(e)
    event_id = e.get('webhookEventId') or e.get('eventId')

    if not text:
        return

    if not _mark_line_event_seen(event_id):
        print(f'LINE事件重複，略過：{event_id}')
        return

    if text.lower() in {'help', '說明', '功能', '股票'}:
        ok = reply_line(
            token,
            '📈 股票加碼分析 Bot V2.10.21\n\n'
            '輸入股票代號或名稱即可查詢。\n'
            '例如：2330、台積電、3711、日月光投控\n\n'
            '模型：基本面40 + 技術30 + 籌碼20 + 風險10。\n'
            '同業估值：動態次產業 Top 10。\n\n'
            '查詢後會先回覆「分析中」，完成後再把完整結果推送回本聊天室。'
        )
        if not ok:
            print('❌ LINE Help Reply失敗')
        return

    if not target:
        reply_line(
            token,
            '❌ 無法取得 LINE 聊天室 ID，請確認 webhook source。'
        )
        return

    # 只做極短的立即回覆，避免 replyToken 因完整分析耗時而失效。
    ok = reply_line(
        token,
        f'🔎 收到「{text}」\n\n'
        '⏳ 正在進行完整分析……\n'
        '會計算基本面、次產業估值、技術、籌碼、風險與綜合評分。\n\n'
        '分析完成後會自動回傳結果。'
    )

    if not ok:
        print('⚠️ LINE 即時確認回覆失敗；仍會嘗試背景 Push。')

    # V2.10.19：使用單一非 daemon Executor，避免同一 Render instance
    # 同時跑多個查詢造成記憶體暴增；/callback 仍立即 HTTP 200。
    try:
        future = LINE_ANALYSIS_EXECUTOR.submit(
            _background_line_analysis,
            text, target, u, event_id
        )
        print(
            f'LINE背景工作已提交：{text} | done={future.done()} | '
            'executor=max_workers=1',
            flush=True
        )
    except Exception as e:
        print(
            f'❌ LINE背景工作啟動失敗：{type(e).__name__}: {e}',
            flush=True
        )
        push_line(target, f'❌ {text} 無法啟動分析工作：{e}')

def run_webhook_server():
    from flask import Flask, request

    app = Flask(__name__)

    print('================================')
    print('LINE Webhook Server V2.10.21')
    print('模式：立即 Reply + Render Free 穩定背景 Thread + Push')
    print('================================')

    if not LINE_TOKEN:
        print('⚠️ 未設定 LINE_CHANNEL_ACCESS_TOKEN')
    if not LINE_CHANNEL_SECRET:
        print('⚠️ 未設定 LINE_CHANNEL_SECRET')

    # V2.10.19：LINE/Render 啟動時不建立 1985 檔完整市場股票池。
    # V2.10.19 原本在 Web Service 啟動時 force_refresh=True，會同時抓
    # TWSE/TPEx 股票池、次產業公開資料並保留大量快取，Render Free 512MB
    # 容易 OOM。LINE 查詢改為「收到查詢後才建立必要資料」，並在分析完成
    # 後釋放大型物件。
    u = {}
    print('LINE 啟動：跳過完整 1985 檔市場股票池，採查詢時載入模式')

    @app.get('/')
    def health():
        return 'stock_alert V2.10.19 OK', 200

    @app.get('/health')
    def health2():
        return 'OK', 200

    @app.post('/callback')
    def cb():
        # LINE 官方要求：必須用「未解析、未修改」的原始 body 驗證簽章。
        raw_body = request.get_data(cache=True)
        signature = request.headers.get('X-Line-Signature', '')

        if not verify_line_signature(raw_body, signature):
            print('❌ LINE webhook signature 驗證失敗')
            return 'Invalid signature', 400

        try:
            body = json.loads(raw_body.decode('utf-8'))
        except Exception as e:
            print('❌ LINE webhook JSON 解析失敗：', e)
            return 'Bad Request', 400

        events = body.get('events', [])
        print(f'LINE Webhook收到事件：{len(events)}', flush=True)

        for e in events:
            try:
                print(
                    'LINE事件：'
                    f"type={e.get('type')} "
                    f"eventId={e.get('webhookEventId', e.get('eventId', 'N/A'))} "
                    f"messageType={(e.get('message') or {}).get('type', 'N/A')}"
                , flush=True)
                handle_event(e, u)
            except Exception as e2:
                traceback.print_exc()
                print('❌ Webhook事件處理錯誤：', e2)

        print('LINE Webhook處理完成：HTTP 200', flush=True)
        return 'OK', 200

    port = int(os.environ.get('PORT', '8080'))
    app.run(
        host='0.0.0.0',
        port=port,
        threaded=True
    )


def build_line_caches_for_actions():
    """V2.10.21：Actions 將已取得的 PE/法人/融資資料轉成 LINE 小快取。

    不新增外部 API 請求；直接利用 run_alerts 已經抓到的資料。Render 因此不需要
    在查詢時再補 20 天 T86 或全市場融資資料。
    """
    # PE：只保存本次執行已取得的完整市場資料。
    try:
        pe = RUN_CACHE.get('current_pe', {})
        if isinstance(pe, dict) and pe:
            _save_line_small_cache(LINE_PE_CACHE_FILE, {
                '_cached_at': time.time(), 'data': pe
            })
            print(f'LINE PE 快取完成：{len(pe)} 檔', flush=True)
    except Exception as e:
        print(f'LINE PE 快取建立失敗：{e}', flush=True)

    # Margin：RUN_CACHE 以 ('margin', market) 為 key。
    try:
        md = {}
        for key, value in MARGIN_CACHE.items():
            if isinstance(key, tuple) and len(key) >= 2 and key[0] == 'margin':
                md[str(key[1])] = value if isinstance(value, dict) else {}
        if md:
            _save_line_small_cache(LINE_MARGIN_CACHE_FILE, md)
            print('LINE 融資快取完成：' + ', '.join(
                f'{m} {len(v)} 檔' for m, v in md.items()
            ), flush=True)
    except Exception as e:
        print(f'LINE 融資快取建立失敗：{e}', flush=True)

    # Institutional：CHIP_HISTORY 已在 Actions 取得 20 日資料；抽取所有股票。
    try:
        full = load_json(CHIP_HISTORY_FILE)
        light = {}
        for market, market_hist in full.items():
            if not isinstance(market_hist, dict):
                continue
            out = {}
            # 最多保留最近 20 個有資料日期。
            dates = sorted(market_hist.keys(), reverse=True)[:20]
            for ds in dates:
                day = market_hist.get(ds)
                if isinstance(day, dict):
                    out[ds] = day
            if out:
                light[market] = out
        if light:
            _save_line_small_cache(LINE_CHIP_CACHE_FILE, light)
            total = sum(len(v) for v in light.values())
            print(f'LINE 法人快取完成：{total} 日資料', flush=True)
    except Exception as e:
        print(f'LINE 法人快取建立失敗：{e}', flush=True)


# ============================================================
# Alerts
# ============================================================

def run_alerts():

    global RUN_CACHE
    global INSTITUTIONAL_CACHE
    global MARGIN_CACHE
    global SUBINDUSTRY_CACHE

    RUN_CACHE = {}
    INSTITUTIONAL_CACHE = {}
    MARGIN_CACHE = {}

    started = time.time()

    print(
        '================================\n'
        '股票跌幅 + 15分鐘區間最低價 + '
        'V2.10.5自動估值 + 技術 + 籌碼\n'
        '================================'
    )

    state = load_json(
        STATE_FILE
    )

    u = get_market_universe()

    print(
        f'[耗時 '
        f'{time.time()-started:.1f}s] '
        f'股票池完成：{len(u)}'
    )

    # --------------------------------------------------------
    # V2.10.21 全市場技術快取
    # GitHub Actions 負責重工作；Render LINE 不再即時碰 Yahoo/TWSE。
    # --------------------------------------------------------
    try:
        refresh_all_technical_cache(u)
    except Exception as e:
        print(f'⚠️ 全市場技術快取更新失敗：{type(e).__name__}: {e}', flush=True)
        traceback.print_exc()

    # --------------------------------------------------------
    # 顯示 V2.9.9 次產業狀態
    # --------------------------------------------------------

    sub_count = sum(
        1
        for x in u.values()
        if x.get(
            'subindustries'
        )
    )

    print(
        f'動態次產業覆蓋：'
        f'{sub_count}/{len(u)}'
    )

    for name, symbol in STOCKS.items():

        c = clean_code(
            symbol
        )

        if c in u:

            subs = get_subindustries_for_stock(
                c,
                u[c]
            )

            print(
                f'次產業：'
                f'{name} → '
                f'{", ".join(subs) if subs else "N/A"}'
            )

    # --------------------------------------------------------
    # PE 歷史：先為本次目標股補足最近一年至少 60 個有效 PE
    # 再進入逐股 analysis，避免 analysis(..., backfill=False) 時
    # 只讀到 pe_history.json 裡少數舊資料。
    # --------------------------------------------------------
    pe_history = load_json(PE_HISTORY_FILE)
    for target_name, target_symbol in STOCKS.items():
        target_code = clean_code(target_symbol)
        target_item = u.get(target_code)
        if target_item and target_symbol and not target_symbol.startswith('^'):
            target_market = target_item.get('market')
            if target_market in ('TWSE', 'TPEX'):
                try:
                    pe_history = backfill_pe(
                        target_code,
                        pe_history,
                        target_market
                    )
                except Exception as e:
                    print(f'PE歷史回補失敗：{target_code} / {e}')
    save_json(PE_HISTORY_FILE, pe_history)

    # --------------------------------------------------------
    # 法人 / 融資
    # --------------------------------------------------------

    target_markets = set()

    for name, symbol in STOCKS.items():

        c = clean_code(
            symbol
        )

        if c in u:

            target_markets.add(
                u[c].get(
                    'market'
                )
            )

    for m in target_markets:

        if m in (
            'TWSE',
            'TPEX'
        ):

            dummy = (
                '2330'
                if m == 'TWSE'
                else next(
                    (
                        x['code']
                        for x in u.values()
                        if x.get(
                            'market'
                        ) == m
                    ),
                    ''
                )
            )

            institutional(
                dummy,
                m,
                20
            )

            margin_data(
                dummy,
                m
            )

    # V2.10.21：把本次 Actions 已取得資料整理成 Render 可直接讀取的小快取。
    try:
        get_current_pe_data()
    except Exception as e:
        print(f'LINE PE 預建失敗：{e}', flush=True)
    build_line_caches_for_actions()

    print(
        f'[耗時 '
        f'{time.time()-started:.1f}s] '
        f'法人/融資批次資料完成'
    )

    # --------------------------------------------------------
    # 逐目標分析
    # --------------------------------------------------------

    for name, symbol in STOCKS.items():

        print(
            f'\n========== {name} =========='
        )

        item = u.get(
            clean_code(symbol)
        )

        try:

            check_drop_alert(
                name,
                symbol,
                state
            )

            RUN_CACHE[
                (
                    'interval_attempted',
                    symbol
                )
            ] = True

            interval_result = (
                check_interval_low(
                    name,
                    symbol,
                    state,
                    item.get('price') if item else None
                )
            )

        except Exception as e:

            print(
                '價格/通知失敗：',
                e
            )

            interval_result = None

        try:

            if symbol.startswith('^'):

                t = technical(
                    symbol
                )

                print(
                    f'\n📈 指數分析\n'
                    f'標的：{name}\n'
                    f'目前價格：'
                    f'{fmt(get_latest_price(symbol))}\n'
                    f'KD：'
                    f'K={fmt(t["k"])} / '
                    f'D={fmt(t["d"])}\n'
                    f'RSI：'
                    f'{fmt(t["rsi"])}'
                )

            elif name in (
                '0050 元大台灣50',
                'QQQ'
            ):

                t = technical(
                    symbol
                )

                print(
                    f'\n📊 ETF分析\n'
                    f'標的：{name}\n'
                    f'目前價格：'
                    f'{fmt(get_latest_price(symbol))}\n'
                    f'RSI：'
                    f'{fmt(t["rsi"])}'
                )

            elif item:

                print(
                    '\n'
                    + analysis(
                        name,
                        u,
                        False,
                        interval_result
                    )
                )

            else:

                print(
                    f'⚠️ {name} '
                    f'不在動態股票池，'
                    f'跳過詳細估值'
                )

        except Exception as e:

            print(
                f'{name} 分析失敗：'
                f'{e}'
            )

            traceback.print_exc()

    save_json(
        STATE_FILE,
        state
    )

    print(
        f'\n========== '
        f'完成｜總耗時 '
        f'{time.time()-started:.1f} 秒 '
        f'=========='
    )


# ============================================================
# Main
# ============================================================

def main():

    import sys

    if (
        len(sys.argv) > 1
        and sys.argv[1].lower()
        == 'webhook'
    ):

        run_webhook_server()

    elif (
        len(sys.argv) > 1
        and sys.argv[1].lower()
        == 'refresh'
    ):

        get_market_universe(
            True
        )

    elif (
        len(sys.argv) > 1
        and sys.argv[1].lower()
        == 'analyze'
    ):

        print(
            analysis(
                ' '.join(
                    sys.argv[2:]
                ),
                get_market_universe(),
                True
            )
        )

    else:

        run_alerts()


if __name__ == '__main__':
    main()
