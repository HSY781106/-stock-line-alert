# stock_alert.py V2.10.98
# V2.10.98：修正估值 PEG 正規化成長率。V2.10.97 以25年/10年 CAGR 直接做估值成長率，
#             對不同景氣階段的股票失真，造成 2303/3711/2330 PEG 全部偏高。
#             本版改為「次產業分層 + 歷史穩健YoY中位數 + 5/10年CAGR + 統計可信度收縮」；
#             EPS預測Growth與估值Growth完全分離。估值Growth不再直接採單一年份預測，也不以固定CAGR硬套所有次產業。
#             Actions/LINE 共用同一模型。
# V2.10.75：統計驗證 EPS 模型。
#             年度 EPS 趨勢加入 β/SE/t/p/95% CI/R²/n 與 A/B/C 可信度；
#             季節係數加入 n/平均/中位數/SD/95% CI/異常值檢查；
#             已公布季度校正加入歷史校正倍率分布與收縮，避免單年度超預期過度放大；
#             未公布季度輸出保守/基準/樂觀 EPS 情境，並避免使用當年度資料估計歷史季節性。
#             p-value 不作為硬性淘汰條件；依統計證據調整年度趨勢權重。
# V2.10.74：新增「全市場綜合評分 >= 90 分」批次掃描通知。
#             使用本次 Actions 已建立的技術／法人／融資／PE／次產業快取，
#             先以「技術＋籌碼＋風險」計算可達上限；只有理論上可能達到 90 分的股票，
#             才進一步呼叫既有 official_fundamental() 補完整基本面，避免 1985 檔逐一完整分析。
#             通知採「當日首次進榜／跌破90後重新進榜」邏輯，同一次執行只發一則彙整 LINE。
#             不改動原有跌幅通知、15分鐘區間通知與目標股分析流程。
# V2.10.73：LINE背景技術面強制即時刷新修正版。
#             背景網頁分析（非 LINE 輕量模式）一律優先抓取最新可用 Yahoo 1d 日線，
#             不再被 LINE_MODE_ACTIVE 或 36/72 小時技術快取攔截；成功後立即寫回
#             line_technical_cache.json，確保快取也同步到最新交易日。
#             即時刷新失敗時才安全退回既有快取。LINE 輕量查詢仍維持快取優先，
#             避免 callback / Render Free 因即時 API 請求而延遲。
#             歷史資料與次產業等不變資料繼續使用快取，避免不必要 API 流量。
# V2.10.56：修正 V2.10.53 舊 PE 快取 migration；加入 PE 每次執行請求/時間上限、即時進度與安全降級；LINE 不再使用舊版 yahoo_light_fund / MOPS fallback / 舊 cache 推導
# V2.10.48：加入 MOPS 官方財報 EPS Growth fallback，補強 Yahoo 多層來源仍為 N/A 的股票
# V2.10.47：統一 EPS Growth 與 PEG 資料口徑；修正 EPS 成長 N/A 但 PEG 有值的矛盾
# V2.10.41：修正 line_fund_cache 覆蓋策略、ETF NAV/溢價、Beta、TPEX 資券與 ETF chart fallback
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
# V2.10.40：以正式 V2.10.37 實際檔案為基底；LINE 查詢改採 A 方案
#          + 查詢結果不再使用 Push，不消耗每月 Push 額度
#          + Reply 僅立即回覆「分析頁面網址」；背景分析完成後寫入 Render 結果頁
#          + /line-result/<id> 顯示即時分析狀態與完整結果
#          + 保留 eventId 去重、Webhook HMAC 驗證、ETF/股票既有分析架構

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
import io

from datetime import datetime, timedelta
from urllib.parse import quote
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
TPEX_WEB_BASE = 'https://www.tpex.org.tw/web/stock/aftertrading/peratio_analysis/pera_result.php'

TW_TZ = ZoneInfo('Asia/Taipei')

STATE_FILE = 'alert_state.json'
PE_HISTORY_FILE = 'pe_history.json'
CHIP_HISTORY_FILE = 'chip_history.json'
LINE_CHIP_CACHE_FILE = 'line_chip_cache.json'
LINE_MARGIN_CACHE_FILE = 'line_margin_cache.json'
LINE_CHIP_SUMMARY_CACHE_FILE = 'line_chip_summary_cache.json'
LINE_PE_CACHE_FILE = 'line_pe_cache.json'
# V2.10.23：LINE 查詢用的輕量快取；Actions 每日批次建立全市場技術資料，Render 優先讀 GitHub 快取。
LINE_FUND_CACHE_FILE = 'line_fund_cache.json'
LINE_TECH_CACHE_FILE = 'line_technical_cache.json'
# V2.10.62：季度 EPS 持久快取；用於補強 Yahoo fundamentals-timeseries 偶發缺少季度 EPS。
EPS_QUARTERLY_CACHE_FILE = 'eps_quarterly_cache.json'
# V2.10.88：25 年 EPS 歷史資料引擎持久快取。
EPS_ANNUAL_HISTORY_CACHE_FILE = 'eps_annual_history_cache.json'
EPS_HISTORY_ENGINE_CACHE_FILE = 'eps_history_engine_cache.json'
EPS_HISTORY_MOPS_BATCH_CACHE_FILE = 'eps_mops_batch_history_cache.json'
# V2.10.88：Goodinfo 一次取得多年年度 EPS，作為真正的25年歷史來源。
EPS_HISTORY_GOODINFO_CACHE_FILE = 'eps_goodinfo_history_cache.json'

UNIVERSE_CACHE_FILE = 'market_universe_cache.json'
TWSE_PROFILE_CACHE_FILE = 'twse_profile_cache.json'
TWSE_QUOTES_CACHE_FILE = 'twse_quotes_cache.json'

# V2.9.8 新增
SUBINDUSTRY_CACHE_FILE = 'subindustry_cache.json'
# V2.10.49：官方基本面快取
# V2.10.56：不再使用 MOPS 基本面快取
# MOPS_FUND_CACHE_FILE 保留名稱僅避免舊程式碼/舊快取造成相容性問題，但 V2.10.56 基本面主流程不讀寫。
MOPS_FUND_CACHE_FILE = 'mops_fund_cache.json'

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
# V2.10.56：目標股歷史 PE 回補總預算；不得因單股/單市場 API 異常拖垮 Action。
PE_BACKFILL_MAX_SECONDS_PER_RUN = 0
PE_BACKFILL_MAX_API_PER_RUN = 0
PE_BACKFILL_MARKET_FAILURE_LIMIT = 2
# V2.10.56：Yahoo/官方基本面單股備援總時間上限。
FUNDAMENTAL_FALLBACK_TIMEOUT = 8
PE_HISTORY_TIMEOUT = 8
# V2.10.56：全市場 PE 日期失敗後至少冷卻數日，避免官方 API 暫時異常時每次 Actions 都重打 500+ 次。
PE_HISTORY_FAILURE_RETRY_DAYS = 7
# V2.10.56：全市場 PE 每次 Actions 的硬性預算。即使官方 API 異常，也不得拖垮整個 Action。
PE_HISTORY_MAX_API_PER_RUN = 0
PE_HISTORY_MAX_SECONDS_PER_RUN = 0
PE_HISTORY_PROGRESS_EVERY = 1
# PE 歷史查詢以日期快取，避免同一次執行 2330/3711 重複打同一天 API
PE_DATE_CACHE = {}

# V2.10.67：季度 EPS 年度預估模型；修正 Yahoo quarterlyDilutedEPS 缺欄導致全數 N/A。
# EPS Growth 僅由「各季度 EPS」建立，不再使用 Q2 YoY / TTM YoY / Yahoo earningsGrowth。
# 已公布季度用實際 EPS；未公布季度用歷史同季度趨勢回歸 + 季節性中位數預估。
# 重大且已確認事件可透過 EPS_EVENT_ADJUSTMENTS 調整指定季度，預設空表，不增加網路請求。
EPS_EVENT_ADJUSTMENTS = {}
# V2.10.67：已確認的「季度 EPS 指引/可靠公開預估」可直接覆寫尚未公布季度。
# 格式：{'2330': {2026: {3: 32.50, 4: 34.10}}}
# 只允許作用於尚未公布季度；不會覆蓋實際 EPS。
# 不自行抓新聞、不把傳聞寫進模型；只有人工/程式明確填入的確認值才會生效。
EPS_CONFIRMED_QUARTER_OVERRIDES = {}
EPS_MODEL_MIN_YEARS = 3
EPS_MODEL_MAX_YEARS = 25
# V2.10.79：近期視窗與長期視窗分離；近期資料反映目前商業結構。
EPS_MODEL_RECENT_YEARS = 10
EPS_MODEL_MIN_HISTORY_QUARTERS = 8
EPS_MODEL_MAX_ABS_GROWTH = 300
EPS_MODEL_BLEND_REGRESSION = 0.70
EPS_MODEL_BLEND_MEDIAN = 0.30
# V2.10.79：歷史年度資料補抓預算；只在真正進入 EPS 基本面分析的股票使用。
EPS_ANNUAL_FETCH_MAX_YEARS_PER_STOCK = 25
EPS_ANNUAL_FETCH_TIMEOUT = 8
# V2.10.88：歷史 EPS 引擎一次向 Yahoo fundamentals-timeseries 要求 30 年，
# 再由季度完整年度與 MOPS 歷史介面補洞；已嘗試但不可得的年份 7 天內不重打。
EPS_HISTORY_SOURCE_YEARS = 30
EPS_HISTORY_RETRY_DAYS = 7
EPS_HISTORY_MOPS_MAX_WORKERS = 2
EPS_HISTORY_GOODINFO_TIMEOUT = 10
EPS_HISTORY_GOODINFO_DISABLED = True
EPS_HISTORY_MOPS_DISABLED = False
EPS_HISTORY_GOODINFO_RETRY_DAYS = 7
EPS_HISTORY_MOPS_CACHE_VERSION = 'v21089'
EPS_HISTORY_MOPS_FAILED_RETRY_HOURS = 6
EPS_HISTORY_MOPS_MAX_YEARS_PER_RUN = 25
# V2.10.75：EPS 統計驗證與情境預測參數。
EPS_MODEL_TREND_P_SIGNIFICANT = 0.05
EPS_MODEL_TREND_P_WEAK = 0.20
EPS_MODEL_TREND_MIN_R2 = 0.35
EPS_MODEL_TREND_A_WEIGHT = 1.00
EPS_MODEL_TREND_B_WEIGHT = 0.60
EPS_MODEL_TREND_C_WEIGHT = 0.20
EPS_MODEL_SCENARIO_Z = 1.96
EPS_MODEL_CORRECTION_ALPHA_MAX = 0.60
EPS_MODEL_CORRECTION_MIN = 0.90
EPS_MODEL_CORRECTION_MAX = 1.10
# V2.10.62：Yahoo 台股有時沒有 quarterlyDilutedEPS，但會提供 quarterlyBasicEPS /
# quarterlyNormalizedDilutedEPS / quarterlyNormalizedBasicEPS；依優先順序補洞。
EPS_QUARTERLY_TYPES = [
    'quarterlyDilutedEPS','quarterlyBasicEPS',
    'quarterlyNormalizedDilutedEPS','quarterlyNormalizedBasicEPS',
    'quarterlyReportedNormalizedDilutedEPS','quarterlyReportedNormalizedBasicEPS'
]

YF_TIMEOUT = 10
MAX_HISTORY_DAYS_PER_RUN = 75

# V2.10.71：即時 / 歷史快取分離。
# 全市場 Actions 批次仍以 36 小時作為「歷史技術快取」更新門檻；
# 但背景網頁分析的目標股不使用此門檻，會優先刷新最新可用日線。
TECH_CACHE_MAX_AGE = 36 * 3600
# LINE 輕量模式仍允許使用遠端快取，避免 callback / Render Free 延遲。
TECH_LINE_CACHE_MAX_AGE = 72 * 3600
TECH_BATCH_CHUNK = 80
TECH_BATCH_TIMEOUT = 30

# V2.10.25：Actions 建立全市場 PE 歷史快取。TWSE/TPEx 每個日期的 PE API
# 本身就是全市場資料，因此不需要逐股票查詢；約 100 個曆日即可涵蓋
# 至少 60 個交易日，讓 LINE 任意股票都能取得一年平均 PE。
PE_ALL_MARKET_CALENDAR_DAYS = 370
TECH_BATCH_PERIOD = '6mo'
TECH_BATCH_INTERVAL = '1d'

# V2.10.23：LINE Free 查詢的硬性網路預算。快取不存在時也必須快速結束，
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
# 被直接終止的風險。Reply token 僅用於立即回覆結果頁網址，背景分析不再依賴 replyToken。
# 完整結果寫入 Render /line-result/<id>，不使用 Push。
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

# V2.10.40：LINE A 方案。使用者主動查詢不再 Push 完整結果。
# Reply 只回覆一個 Render 結果頁網址；背景分析完成後更新記憶體中的結果。
# Reply 不計入方案訊息額度，Push 則會計入每月額度。
LINE_RESULT_LOCK = threading.Lock()
LINE_RESULT_CACHE = {}
LINE_RESULT_MAX = 100



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

# V2.10.34：ETF 獨立解析，不需要存在 1985 檔股票池。
ETF_MAP = {
    '0050': {'name':'元大台灣50','symbol':'0050.TW'},
    '006208': {'name':'富邦台50','symbol':'006208.TW'},
    '00878': {'name':'國泰永續高股息','symbol':'00878.TW'},
    '00919': {'name':'群益台灣精選高息','symbol':'00919.TW'},
    '00713': {'name':'元大台灣高息低波','symbol':'00713.TW'},
    '00679B': {'name':'元大美債20年','symbol':'00679B.TW'},
    '00887': {'name':'永豐中國科技50大','symbol':'00887.TW'},
    'QQQ': {'name':'Invesco QQQ','symbol':'QQQ'},
    'SPY': {'name':'SPDR S&P 500 ETF','symbol':'SPY'},
    'VOO': {'name':'Vanguard S&P 500 ETF','symbol':'VOO'},
    'VTI': {'name':'Vanguard Total Stock Market ETF','symbol':'VTI'},
    'IVV': {'name':'iShares Core S&P 500 ETF','symbol':'IVV'},
    'DIA': {'name':'SPDR Dow Jones Industrial Average ETF','symbol':'DIA'},
    'IWM': {'name':'iShares Russell 2000 ETF','symbol':'IWM'},
    'SMH': {'name':'VanEck Semiconductor ETF','symbol':'SMH'},
    'SOXX': {'name':'iShares Semiconductor ETF','symbol':'SOXX'},
    'XLK': {'name':'Technology Select Sector SPDR Fund','symbol':'XLK'},
    'XLF': {'name':'Financial Select Sector SPDR Fund','symbol':'XLF'},
    'ARKK': {'name':'ARK Innovation ETF','symbol':'ARKK'},
}

def resolve_etf_query(q):
    """V2.10.34：ETF 查詢加強。

    除固定 ETF_MAP 外，接受「代號」「代號.TW/.TWO」「ETF:代號」以及
    「代號 名稱」。對 00 開頭台股 ETF 允許動態建立 Yahoo ticker，避免
    新 ETF 尚未寫進 ETF_MAP 就無法從 LINE 查詢。
    """
    q=str(q or '').strip()
    nq=q.upper().replace('.TW','').replace('.TWO','').replace('.US','')
    nq=re.sub(r'^(?:ETF[:：]\s*)','',nq).strip()
    token=re.split(r'[\s　]+',nq)[0] if nq else ''
    if nq in ETF_MAP: return ETF_MAP[nq]
    if token in ETF_MAP: return ETF_MAP[token]
    for info in ETF_MAP.values():
        if normalize_name(q)==normalize_name(info['name']) or normalize_name(q)==normalize_name(info['name'].replace('ETF','')):
            return info
    # 台股 ETF 多為 00 開頭；僅接受明確 ETF 代碼格式，避免誤把一般股票當 ETF。
    if re.fullmatch(r'00[0-9A-Z]{2,5}', token):
        if token in {'00679B','00887'}:
            return None
        symbol=token + '.TW'
        return {'name': token, 'symbol': symbol, '_dynamic': True}
    # 常見美股 ETF 即使尚未列入 ETF_MAP，也可直接以大寫代號查詢。
    if re.fullmatch(r'[A-Z]{2,5}', token) and token in {'SCHD','VUG','VTV','VEA','VWO','XLV','XLE','XLI','XLY','XLP','XLU','VNQ','TLT','HYG','LQD','GLD','SLV'}:
        return {'name': token, 'symbol': token, '_dynamic': True}
    return None


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
    # 產業模型不是「開關」而已；weights 定義 40 分基本面中各指標的最大配分。
    # PE / PEG / PB / 殖利率 / ROE / EPS 成長。
    '金融業': {'profile':'資產型', 'weights': {'pe':4,'peg':0,'pb':12,'yield':8,'roe':10,'growth':6}},
    '銀行業': {'profile':'資產型', 'weights': {'pe':4,'peg':0,'pb':12,'yield':8,'roe':10,'growth':6}},
    '保險業': {'profile':'資產型', 'weights': {'pe':4,'peg':0,'pb':12,'yield':8,'roe':10,'growth':6}},
    '半導體業': {'profile':'資本密集型', 'weights': {'pe':10,'peg':4,'pb':3,'yield':1,'roe':6,'growth':16}},
    '通信網路業': {'profile':'現金流型', 'weights': {'pe':8,'peg':1,'pb':4,'yield':10,'roe':9,'growth':8}},
    '水泥工業': {'profile':'特殊型', 'weights': {'pe':8,'peg':0,'pb':8,'yield':8,'roe':8,'growth':8}},
    '食品工業': {'profile':'成熟獲利型', 'weights': {'pe':10,'peg':1,'pb':5,'yield':8,'roe':9,'growth':7}},
    '塑膠工業': {'profile':'特殊型', 'weights': {'pe':9,'peg':1,'pb':6,'yield':7,'roe':8,'growth':9}},
    '紡織纖維': {'profile':'特殊型', 'weights': {'pe':8,'peg':1,'pb':7,'yield':7,'roe':8,'growth':9}},
    '電機機械': {'profile':'成熟獲利型', 'weights': {'pe':9,'peg':3,'pb':5,'yield':5,'roe':9,'growth':9}},
    '電器電纜': {'profile':'成熟獲利型', 'weights': {'pe':9,'peg':2,'pb':6,'yield':7,'roe':8,'growth':8}},
    '鋼鐵工業': {'profile':'特殊型', 'weights': {'pe':7,'peg':0,'pb':10,'yield':7,'roe':8,'growth':8}},
    '橡膠工業': {'profile':'特殊型', 'weights': {'pe':8,'peg':1,'pb':7,'yield':7,'roe':8,'growth':9}},
    '汽車工業': {'profile':'特殊型', 'weights': {'pe':8,'peg':2,'pb':6,'yield':6,'roe':9,'growth':9}},
    '建材營造': {'profile':'資產型', 'weights': {'pe':5,'peg':0,'pb':12,'yield':8,'roe':9,'growth':6}},
    '航運業': {'profile':'特殊型', 'weights': {'pe':7,'peg':0,'pb':9,'yield':6,'roe':8,'growth':10}},
    '觀光餐旅': {'profile':'現金流型', 'weights': {'pe':8,'peg':2,'pb':5,'yield':8,'roe':9,'growth':8}},
    '貿易百貨': {'profile':'成熟獲利型', 'weights': {'pe':10,'peg':2,'pb':5,'yield':8,'roe':8,'growth':7}},
    '油電燃氣業': {'profile':'現金流型', 'weights': {'pe':8,'peg':0,'pb':5,'yield':11,'roe':9,'growth':7}},
    '電腦及週邊設備業': {'profile':'高成長型', 'weights': {'pe':8,'peg':8,'pb':3,'yield':2,'roe':7,'growth':12}},
    '光電業': {'profile':'特殊型', 'weights': {'pe':7,'peg':5,'pb':5,'yield':2,'roe':8,'growth':13}},
    '電子零組件業': {'profile':'高成長型', 'weights': {'pe':8,'peg':7,'pb':4,'yield':3,'roe':7,'growth':11}},
    '電子通路業': {'profile':'成熟獲利型', 'weights': {'pe':10,'peg':3,'pb':4,'yield':5,'roe':8,'growth':10}},
    '資訊服務業': {'profile':'高成長型', 'weights': {'pe':8,'peg':9,'pb':3,'yield':1,'roe':8,'growth':11}},
    '其他電子業': {'profile':'高成長型', 'weights': {'pe':8,'peg':7,'pb':4,'yield':3,'roe':7,'growth':11}},
    '生技醫療': {'profile':'高成長型', 'weights': {'pe':4,'peg':9,'pb':5,'yield':0,'roe':8,'growth':14}},
    '文化創意業': {'profile':'高成長型', 'weights': {'pe':6,'peg':8,'pb':4,'yield':2,'roe':8,'growth':12}},
    '其他': {'profile':'特殊型', 'weights': {'pe':8,'peg':4,'pb':6,'yield':6,'roe':8,'growth':8}},
}

DEFAULT_MODEL = {
    'profile':'特殊型',
    'weights': {'pe':8,'peg':4,'pb':6,'yield':6,'roe':8,'growth':8}
}

# V2.10.98：EPS 預測模型與估值模型分離。
# EPS_INDUSTRY_MODEL 仍負責「下一年度 EPS 預測」，不要把估值正規化 Growth 混進預測。
EPS_INDUSTRY_MODEL = {
    '半導體業': {'name':'景氣循環型','method':'log回歸＋近期10年＋穩健YoY中位數','regression_weight':0.35,'recent_weight':0.35,'robust_yoy_weight':0.30,'max_normalized_growth':60.0,'valuation_growth_cap':30.0},
    '航運業': {'name':'景氣循環型','method':'log回歸＋近期10年＋穩健YoY中位數','regression_weight':0.30,'recent_weight':0.30,'robust_yoy_weight':0.40,'max_normalized_growth':60.0,'valuation_growth_cap':30.0},
    '鋼鐵工業': {'name':'景氣循環型','method':'log回歸＋近期10年＋穩健YoY中位數','regression_weight':0.30,'recent_weight':0.30,'robust_yoy_weight':0.40,'max_normalized_growth':50.0,'valuation_growth_cap':25.0},
    '塑膠工業': {'name':'景氣循環型','method':'log回歸＋近期10年＋穩健YoY中位數','regression_weight':0.30,'recent_weight':0.30,'robust_yoy_weight':0.40,'max_normalized_growth':50.0,'valuation_growth_cap':25.0},
    '化學工業': {'name':'景氣循環型','method':'log回歸＋近期10年＋穩健YoY中位數','regression_weight':0.30,'recent_weight':0.30,'robust_yoy_weight':0.40,'max_normalized_growth':50.0,'valuation_growth_cap':25.0},
    '電腦及週邊設備業': {'name':'科技成長型','method':'log回歸＋近期10年','regression_weight':0.45,'recent_weight':0.55,'robust_yoy_weight':0.00,'max_normalized_growth':100.0,'valuation_growth_cap':40.0},
    '電子零組件業': {'name':'科技成長型','method':'log回歸＋近期10年','regression_weight':0.45,'recent_weight':0.55,'robust_yoy_weight':0.00,'max_normalized_growth':100.0,'valuation_growth_cap':40.0},
    '資訊服務業': {'name':'科技成長型','method':'log回歸＋近期10年','regression_weight':0.45,'recent_weight':0.55,'robust_yoy_weight':0.00,'max_normalized_growth':100.0,'valuation_growth_cap':40.0},
}
EPS_INDUSTRY_DEFAULT = {'name':'一般型','method':'長期回歸＋近期10年＋穩健YoY','regression_weight':0.45,'recent_weight':0.35,'robust_yoy_weight':0.20,'max_normalized_growth':100.0,'valuation_growth_cap':40.0}

def get_eps_industry_model(industry):
    return EPS_INDUSTRY_MODEL.get(canonical_industry(industry), EPS_INDUSTRY_DEFAULT)

# V2.10.98：估值 PEG 專用的「次產業正規化 EPS Growth」模型。
# 這個模型與上面的 EPS 預測模型完全獨立。
EPS_VALUATION_SUBINDUSTRY_MODEL = {
    '晶圓製造': {'name':'晶圓製造成長循環型','median_w':0.55,'cagr5_w':0.25,'cagr10_w':0.20,'cap':25.0},
    'IC封裝測試': {'name':'封裝測試循環型','median_w':0.60,'cagr5_w':0.25,'cagr10_w':0.15,'cap':22.0},
    '記憶體': {'name':'記憶體超循環型','median_w':0.65,'cagr5_w':0.20,'cagr10_w':0.15,'cap':20.0},
    '半導體設備': {'name':'半導體設備成長循環型','median_w':0.45,'cagr5_w':0.30,'cagr10_w':0.25,'cap':25.0},
    '半導體材料': {'name':'半導體材料成長循環型','median_w':0.50,'cagr5_w':0.25,'cagr10_w':0.25,'cap':25.0},
    'IC設計': {'name':'IC設計成長型','median_w':0.40,'cagr5_w':0.35,'cagr10_w':0.25,'cap':35.0},
}
EPS_VALUATION_INDUSTRY_MODEL = {
    '半導體業': {'name':'半導體循環型','median_w':0.55,'cagr5_w':0.25,'cagr10_w':0.20,'cap':25.0},
    '航運業': {'name':'航運循環型','median_w':0.65,'cagr5_w':0.20,'cagr10_w':0.15,'cap':20.0},
    '鋼鐵工業': {'name':'鋼鐵循環型','median_w':0.65,'cagr5_w':0.20,'cagr10_w':0.15,'cap':18.0},
    '塑膠工業': {'name':'塑化循環型','median_w':0.60,'cagr5_w':0.25,'cagr10_w':0.15,'cap':20.0},
    '化學工業': {'name':'化學循環型','median_w':0.60,'cagr5_w':0.25,'cagr10_w':0.15,'cap':20.0},
    '電腦及週邊設備業': {'name':'科技成長型','median_w':0.40,'cagr5_w':0.35,'cagr10_w':0.25,'cap':35.0},
    '電子零組件業': {'name':'科技成長型','median_w':0.45,'cagr5_w':0.30,'cagr10_w':0.25,'cap':30.0},
    '資訊服務業': {'name':'科技成長型','median_w':0.40,'cagr5_w':0.35,'cagr10_w':0.25,'cap':35.0},
}
EPS_VALUATION_DEFAULT = {'name':'一般型','median_w':0.50,'cagr5_w':0.25,'cagr10_w':0.25,'cap':25.0}

def get_eps_valuation_model(industry, subindustry=None):
    sub=normalize_subindustry(subindustry)
    if sub and sub in EPS_VALUATION_SUBINDUSTRY_MODEL:
        m=dict(EPS_VALUATION_SUBINDUSTRY_MODEL[sub]); m['source_level']='次產業'; m['subindustry']=sub
        return m
    m=dict(EPS_VALUATION_INDUSTRY_MODEL.get(canonical_industry(industry), EPS_VALUATION_DEFAULT))
    m['source_level']='大產業'; m['subindustry']=sub or ''
    return m


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
        'User-Agent': 'Mozilla/5.0 stock-alert/2.10.55'
    }

    if headers:
        base_headers.update(headers)

    for i in range(retries + 1):

        try:
            try:
                r = requests.get(
                    url,
                    params=params,
                    timeout=timeout,
                    headers=base_headers
                )
            except requests.exceptions.SSLError as ssl_err:
                # V2.10.26：Render / GitHub Actions 偶發遇到
                # TPEX 憑證缺少 Subject Key Identifier，導致
                # SSLCertVerificationError。這不是 API 404/500，
                # 而是遠端憑證鏈問題；只對 SSL 驗證錯誤做一次
                # verify=False 備援，其他錯誤仍維持正常驗證。
                if 'CERTIFICATE_VERIFY_FAILED' not in str(ssl_err):
                    raise
                print(
                    f'HTTPS憑證驗證失敗，啟用單次安全備援：{url}',
                    flush=True
                )
                r = requests.get(
                    url,
                    params=params,
                    timeout=timeout,
                    headers=base_headers,
                    verify=False
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
                        'Mozilla/5.0 stock-alert/2.10.55'
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

def fetch_value_chain_for_stock_fast(code):
    """V2.10.28：LINE 單股次產業快速查詢。

    只做一次官方請求；若 Render 的憑證鏈異常，僅對 SSL 錯誤使用 verify=False。
    不呼叫 Jina、不掃同產業、不阻塞整個 LINE 工作。
    """
    code = clean_code(code)
    if not code:
        return None
    url = f'{VALUE_CHAIN_BASE}?stk_code={code}'
    headers = {
        'User-Agent': 'Mozilla/5.0 stock-alert/2.10.28',
        'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
        'Accept-Language': 'zh-TW,zh;q=0.9,en;q=0.8',
        'Referer': 'https://ic.tpex.org.tw/'
    }
    try:
        try:
            r = requests.get(url, timeout=4, headers=headers)
        except requests.exceptions.SSLError as e:
            if 'CERTIFICATE_VERIFY_FAILED' not in str(e):
                raise
            r = requests.get(url, timeout=4, headers=headers, verify=False)
        r.raise_for_status()
        raw = r.content
        text = None
        for enc in ('utf-8-sig','utf-8','cp950','big5'):
            try:
                text = raw.decode(enc); break
            except UnicodeDecodeError:
                pass
        if text is None:
            text = raw.decode('utf-8', errors='replace')
        parsed = parse_value_chain_html(text, code)
        return parsed if parsed.get('subindustries') else None
    except Exception as e:
        print(f'次產業單股快速 API 失敗：{code} / {type(e).__name__}: {e}', flush=True)
        return None


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
            try:
                r = requests.get(official_url, timeout=VALUE_CHAIN_TIMEOUT, headers=headers, allow_redirects=True)
            except requests.exceptions.SSLError as ssl_err:
                if 'CERTIFICATE_VERIFY_FAILED' not in str(ssl_err):
                    raise
                print(f'次產業 HTTPS 憑證驗證失敗，啟用單次備援：{code}', flush=True)
                r = requests.get(official_url, timeout=VALUE_CHAIN_TIMEOUT, headers=headers, allow_redirects=True, verify=False)
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
        return '次產業資料未快取'

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


def get_latest_intraday_price(symbol):
    """V2.10.71：取得目標股最新可用 5 分鐘價格。

    技術趨勢的 price 不再使用 market_universe_cache.json 裡可能過期的價格。
    正常背景分析優先使用 Yahoo 最新 5 分鐘 K 棒的 Close；若 5 分鐘資料
    無法取得，才退回既有 get_latest_price() 的最新日線收盤價。
    這個價格只用來做「目前價格相對 MA20/MA60」的即時判斷，不改動
    MA20/MA60、RSI、KD 的歷史日線計算口徑。
    """
    key = ('latest_intraday_v211', symbol)
    if key in RUN_CACHE:
        return RUN_CACHE[key]

    v = None
    try:
        d = yf_download(symbol, '1d', '5m')
        if d is not None and not d.empty and 'Close' in d.columns:
            c = pd.to_numeric(d['Close'], errors='coerce').dropna()
            if not c.empty:
                v = float(c.iloc[-1])
    except Exception as e:
        print(
            f'V2.10.71 即時5分鐘價格取得失敗 {symbol}：{type(e).__name__}',
            flush=True
        )

    if v is None:
        v = get_latest_price(symbol)

    RUN_CACHE[key] = v
    return v


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


def _drop_alert_analysis_message(name, symbol, u, day, week, cur, pc, wh, daily_triggered, weekly_triggered):
    """V2.10.47：跌幅警報觸發後，直接沿用同一套股票加碼分析模型。

    這裡使用 LINE 輕量路徑與 Actions 已建立的快取，避免警報時重新掃描全市場。
    若分析失敗，仍會送出原本的跌幅通知，不讓分析故障影響警報。
    """
    try:
        result = analysis(symbol, u, backfill=False, line_light=True)
        if not result or result.startswith('❌'):
            raise RuntimeError(result or 'analysis empty')

        def grab(pattern, default='N/A'):
            m = re.search(pattern, result, flags=re.I)
            return m.group(1).strip() if m else default

        total = grab(r'綜合評分：([^\n]+)')
        verdict = grab(r'結論：([^\n]+)')
        pe = grab(r'PE：([^\n]+)')
        one = grab(r'一年平均PE：([^\n]+)')
        pb = grab(r'PB：([^\n]+)')
        yld = grab(r'殖利率：([^\n]+)')
        growth = grab(r'EPS成長：([^\n]+)')
        peg = grab(r'PEG：([^\n]+)')
        roe = grab(r'ROE：([^\n]+)')
        rsi = grab(r'RSI：([^\n]+)')
        kd = grab(r'KD：([^\n]+)')
        trend = grab(r'趨勢：([^\n]+)')
        fs = grab(r'基本面得分：([^\n]+)')
        ts = grab(r'技術得分：([^\n]+)')
        cs = grab(r'籌碼得分：([^\n]+)')
        inst5 = grab(r'法人5日：([^\n]+)')
        inst20 = grab(r'法人20日：([^\n]+)')
        factors = grab(r'加分因素：([^\n]+)')
        chips = grab(r'籌碼訊號：([^\n]+)')
        risks = grab(r'風險提醒：([^\n]+)')

        triggers=[]
        if daily_triggered:
            triggers.append(f'當日跌幅 {day:.2%} ≤ {DAILY_THRESHOLD:.0%}')
        if weekly_triggered and week is not None:
            triggers.append(f'距7日高點 {week:.2%} ≤ {WEEK_THRESHOLD:.0%}')

        msg=(
            f'🔴 跌幅通知＋加碼評估\n\n'
            f'標的：{name}\n'
            f'目前價格：{cur:,.2f}\n'
            f'前一交易日收盤：{pc:,.2f}\n'
            f'過去7日高點：{wh:,.2f}\n'
            f'觸發條件：{"、".join(triggers)}\n\n'
            f'【當下加碼評估】\n'
            f'綜合評分：{total}\n'
            f'結論：{verdict}\n'
            f'基本面：{fs}\n'
            f'技術面：{ts}\n'
            f'籌碼面：{cs}\n\n'
            f'PE：{pe}｜一年平均PE：{one}\n'
            f'PB：{pb}｜殖利率：{yld}\n'
            f'EPS成長：{growth}｜PEG：{peg}\n'
            f'ROE：{roe}\n'
            f'RSI：{rsi}｜KD：{kd}\n'
            f'趨勢：{trend}\n'
            f'法人5日：{inst5}\n'
            f'法人20日：{inst20}\n\n'
            f'加分因素：{factors}\n'
            f'籌碼訊號：{chips}\n'
            f'風險提醒：{risks}'
        )
        return msg[:5000]
    except Exception as e:
        print(f'V2.10.47 跌幅通知加碼分析失敗 {name}: {type(e).__name__}: {e}', flush=True)
        msg=(
            f'🔴 跌幅通知\n\n'
            f'標的：{name}\n'
            f'目前價格：{cur:,.2f}\n'
            f'前一交易日收盤：{pc:,.2f}\n'
            f'單日跌幅：{day:.2%}\n'
            f'距7日高點跌幅：{week:.2%}' if week is not None else
            f'🔴 跌幅通知\n\n標的：{name}\n目前價格：{cur:,.2f}\n單日跌幅：{day:.2%}'
        )
        return msg


def check_drop_alert(
    name,
    symbol,
    state,
    u=None
):
    """V2.10.42：跌幅達標後，同一則 LINE 通知直接附上當下加碼評估。"""
    cur = get_latest_price(symbol)
    pc = get_previous_close(symbol)
    wh = get_week_high(symbol)

    if cur is None or pc is None:
        return

    day = cur / pc - 1
    week = cur / wh - 1 if wh else None

    s = state.setdefault('drop_alert', {}).setdefault(name, {})
    today = datetime.now(TW_TZ).strftime('%Y-%m-%d')

    if s.get('date') != today:
        s.update({'date': today, 'daily_alert': False, 'weekly_alert': False})

    daily_triggered = day <= DAILY_THRESHOLD and not s.get('daily_alert')
    weekly_triggered = week is not None and week <= WEEK_THRESHOLD and not s.get('weekly_alert')

    if daily_triggered:
        s['daily_alert'] = True
    elif day > DAILY_THRESHOLD:
        s['daily_alert'] = False

    if weekly_triggered:
        s['weekly_alert'] = True
    elif week is not None and week > WEEK_THRESHOLD:
        s['weekly_alert'] = False

    # 同一次執行若同時達到單日/一週門檻，只發一則整合通知，避免 LINE 重複洗版。
    if daily_triggered or weekly_triggered:
        if isinstance(u, dict) and u:
            msg = _drop_alert_analysis_message(
                name, symbol, u, day, week, cur, pc, wh,
                daily_triggered, weekly_triggered
            )
        else:
            msg = (
                f'🔴 跌幅通知\n\n'
                f'標的：{name}\n'
                f'目前價格：{cur:,.2f}\n'
                f'前一交易日收盤：{pc:,.2f}\n'
                f'單日跌幅：{day:.2%}\n'
                f'距7日高點跌幅：{week:.2%}' if week is not None else
                f'🔴 跌幅通知\n\n標的：{name}\n目前價格：{cur:,.2f}\n單日跌幅：{day:.2%}'
            )
        send_line(msg)


# ============================================================
# Valuation
# ============================================================

def tpex_web_peratio_data(ds=None, timeout=None):
    """V2.10.28：TPEx 官方網頁版 PE/PB/殖利率備援。

    OpenAPI 偶爾少回部分上櫃股票；例如 6488 可能不在 OpenAPI 回傳集合，
    但 TPEx 官方「個股本益比、殖利率、股價淨值比」網頁資料仍有該股票。
    使用官方 JSON 輸出，不依賴第三方資料。
    """
    params = {
        'l': 'zh-tw',
        'o': 'json'
    }
    if ds:
        params['d'] = ds
    try:
        return http_json(
            TPEX_WEB_BASE,
            params,
            timeout=timeout or max(4, TPEX_TIMEOUT),
            retries=0
        ) or {}
    except Exception as e:
        print(f'TPEx 網頁版 PE 備援失敗 {ds or "latest"}：{type(e).__name__}: {e}', flush=True)
        return {}


def parse_tpex_web_peratio(data):
    """解析 TPEx pera_result.php 的 aaData 格式。"""
    out = {}
    if not isinstance(data, dict):
        return out
    rows = data.get('aaData') or data.get('data') or []
    if not isinstance(rows, list):
        return out
    for r in rows:
        if isinstance(r, dict):
            code = clean_code(first_value(r, ['證券代號','公司代號','symbol','SecuritiesCompanyCode','code']))
            pe = first_value(r, ['本益比','peRatio','PERatio','PE'])
            yld = first_value(r, ['殖利率(%)','殖利率','dividendYield','DividendYield'])
            pb = first_value(r, ['股價淨值比','pbRatio','PBR','PBRatio'])
        elif isinstance(r, list) and len(r) >= 7:
            code = clean_code(r[0])
            pe, yld, pb = r[2], r[5], r[6]
        else:
            continue
        if code:
            out[code] = {'pe': to_float(pe), 'pb': to_float(pb), 'yield': to_float(yld)}
    return out


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
            **parse_pe(twse_get('/exchangeReport/BWIBBU_ALL')),
            **parse_pe(tpex_get('/tpex_mainboard_peratio_analysis'))
        }

    # V2.10.28：TPEx OpenAPI 可能少回部分上櫃股票。
    # 官方網頁 JSON 只補缺少的股票，不改寫 OpenAPI 已成功資料。
    try:
        tpex_count = sum(1 for c in out if c and len(str(c)) == 4 and c.isdigit())
        if tpex_count < 900:
            web_data = parse_tpex_web_peratio(tpex_web_peratio_data(timeout=4))
            added = 0
            for c, row in web_data.items():
                if c not in out or not any(to_float(out.get(c, {}).get(k)) is not None for k in ('pe','pb','yield')):
                    out[c] = row
                    added += 1
            if added:
                print(f'TPEx 官方網頁 PE 備援補入：{added} 檔', flush=True)
        # LINE 模式若已有小快取但目標股缺失，仍做一次官方網頁備援。
        elif LINE_MODE_ACTIVE:
            pass
    except Exception as e:
        print(f'TPEx PE 網頁備援處理失敗：{type(e).__name__}: {e}', flush=True)

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
        # V2.10.28：歷史上櫃 PE 直接使用 TPEx 官方網頁 JSON；
        # 該資料比 OpenAPI 完整，且一次就是整個上櫃市場，避免 6488 等股票被漏掉。
        # V2.10.56：歷史 TPEX 每日期只允許有限網路成本。
        # 官方網頁資料優先；若失敗，不再立刻再打一個可能同樣 timeout 的 OpenAPI。
        # 否則 8 個日期 × (5~10 秒) 就足以讓 Action 明顯拖慢。
        try:
            parsed = parse_tpex_web_peratio(tpex_web_peratio_data(ds, timeout=4))
            if parsed:
                PE_DATE_CACHE[key] = parsed
                return parsed
        except Exception as e:
            print(f'⚠️ TPEx 官方網頁歷史 PE 失敗：{ds} / {type(e).__name__}', flush=True)
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


def backfill_pe(code, h, market, run_budget=None):
    """V2.10.56：停用逐日歷史 PE 網路回補。

    官方 TWSE/TPEx 歷史 PE API 在 Actions 環境容易 timeout/428/520；
    歷史 PE 已有就使用，沒有就交給目前 PE（官方或股價/TTM EPS）與
    「目前 PE 代理」處理。此函式保留原介面，避免舊流程需要大改。
    """
    h.setdefault(code, {})
    valid = sum(
        1 for v in h[code].values()
        if (to_float(v) is not None and 0 < to_float(v) <= PE_MAX_VALID)
    )
    print(
        f'PE歷史回補：{code} {valid}/{PE_MIN_HISTORY} 個有效PE，'
        f'V2.10.56 已停用逐日歷史API，搜尋 0 天',
        flush=True
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

    # V2.10.56：只要快取中有任何有效歷史 PE，就使用實際可取得樣本。
    # 不再因樣本少於 20 筆直接回 N/A；樣本數照實顯示。
    return sum(x for _, x in v) / len(v), len(v)


def one_year_pe_proxy(code, current_pe, symbol):
    """V2.10.56：無歷史 PE 時的零網路代理。

    不再為了「一年平均 PE」另外抓 1 年 Yahoo 日線，避免 Actions 因 PE
    proxy 再次等待。若目前 PE 可由官方或「股價 / TTM EPS」取得，則以目前 PE
    作為最後代理，明確標示 sample=1；這不是官方一年平均 PE。
    """
    pe = to_float(current_pe)
    if pe is None or pe <= 0 or pe > PE_MAX_VALID:
        return None, 0, ''
    return pe, 1, '目前PE代理（無歷史PE）'


def yahoo_quote_summary_fund(symbol):
    """V2.10.34：單股 Yahoo quoteSummary 補洞。

    只在其他來源缺欄位時使用；一次請求多個 module，避免免費版產生大量 API 呼叫。
    任何失敗都視為「該來源沒有資料」，不阻塞整份分析。
    """
    key=('yf_qs_fund_v21030',symbol)
    if key in RUN_CACHE:
        return RUN_CACHE[key]
    out={'pe':None,'pb':None,'yield':None,'eps_growth':None,'roe':None,'peg':None,
         'trailing_eps':None,'dividend_rate':None,'market_cap':None,'equity':None,'price':None,'eps_history':[]}
    try:
        url='https://query1.finance.yahoo.com/v10/finance/quoteSummary/'+str(symbol)
        params={'modules':'price,summaryDetail,defaultKeyStatistics,financialData,earningsHistory,earningsTrend,incomeStatementHistory'}
        r=requests.get(url,params=params,timeout=5,headers={'User-Agent':'Mozilla/5.0 stock-alert/2.10.30'})
        r.raise_for_status()
        result=((r.json().get('quoteSummary') or {}).get('result') or [])
        q=result[0] if result else {}
        def raw(section,*keys):
            sec=q.get(section) or {}
            for k in keys:
                v=sec.get(k)
                if isinstance(v,dict):
                    v=v.get('raw',v.get('fmt'))
                v=to_float(v)
                if v is not None:
                    return v
            return None
        out['pe']=raw('summaryDetail','trailingPE') or raw('defaultKeyStatistics','trailingPE')
        out['pb']=raw('defaultKeyStatistics','priceToBook')
        y=raw('summaryDetail','dividendYield')
        out['yield']=y*100 if y is not None and y<=1.5 else y
        out['eps_growth']=raw('financialData','earningsGrowth')
        if out['eps_growth'] is not None and abs(out['eps_growth'])<2:
            out['eps_growth']*=100
        roe=raw('financialData','returnOnEquity')
        out['roe']=roe*100 if roe is not None and abs(roe)<2 else roe
        out['peg']=raw('defaultKeyStatistics','pegRatio','trailingPegRatio')
        out['trailing_eps']=raw('defaultKeyStatistics','trailingEps')
        out['dividend_rate']=raw('summaryDetail','dividendRate')
        out['market_cap']=raw('price','marketCap')
        out['price']=raw('price','regularMarketPrice','postMarketPrice')

        # V2.10.47：Yahoo earningsHistory / incomeStatementHistory fallback。
        # 不再只依賴 financialData.earningsGrowth；部分台股沒有該欄位，
        # 但仍可從實際季度/年度 EPS 計算 YoY。
        hist=[]
        eh=q.get('earningsHistory') or {}
        for row in eh.get('history') or []:
            if not isinstance(row,dict):
                continue
            ed=row.get('epsActual')
            if isinstance(ed,dict): ed=ed.get('raw',ed.get('fmt'))
            ed=to_float(ed)
            dt=row.get('quarter')
            if isinstance(dt,dict): dt=dt.get('fmt',dt.get('raw'))
            if ed is not None:
                hist.append((str(dt or ''),ed))
        ish=q.get('incomeStatementHistory') or {}
        for row in ish.get('incomeStatementHistory') or []:
            if not isinstance(row,dict):
                continue
            ev=None
            for k in ('dilutedEPS','basicEPS','dilutedAverageShares'):
                z=row.get(k)
                if isinstance(z,dict): z=z.get('raw',z.get('fmt'))
                z=to_float(z)
                if k in ('dilutedEPS','basicEPS') and z is not None:
                    ev=z; break
            dt=row.get('endDate')
            if isinstance(dt,dict): dt=dt.get('fmt',dt.get('raw'))
            if ev is not None:
                hist.append((str(dt or ''),ev))
        hist=sorted(set(hist),key=lambda x:x[0])
        out['eps_history']=[v for _,v in hist]
        # Same-quarter YoY when two comparable quarters are available.
        if len(hist)>=5:
            latest_date,latest=hist[-1]
            for dt,prev in reversed(hist[:-1]):
                if dt[:4] and latest_date[:4] and dt[:4] != latest_date[:4] and prev != 0:
                    g=(latest/prev-1)*100
                    if -500 <= g <= 500:
                        out['eps_growth']=g
                    break
        # Annual EPS YoY if earnings history is insufficient.
        if out['eps_growth'] is None and len(hist)>=2 and hist[-2][1] != 0:
            g=(hist[-1][1]/hist[-2][1]-1)*100
            if -500 <= g <= 500:
                out['eps_growth']=g
    except Exception as e:
        print(f'Yahoo quoteSummary補值失敗 {symbol}: {type(e).__name__}: {e}',flush=True)
    RUN_CACHE[key]=out
    return out


def _yfinance_earnings_dates_eps(symbol):
    """V2.10.62：用 Yahoo earnings dates 的「Reported EPS」補季度 EPS。

    這是季度 EPS 的備援來源，不使用 earningsGrowth / Q2 YoY。
    公告月份映射台股常見財報公告節奏：
      Jan-Mar -> 前一年 Q4；Apr-May -> 當年 Q1；Jun-Aug -> 當年 Q2；Sep-Nov -> 當年 Q3。
    只接受已公布的 Reported EPS，並持久快取 7 天；不增加每次執行的全市場請求。
    """
    key=('yf_earnings_dates_eps_v21062', symbol)
    if key in RUN_CACHE:
        return RUN_CACHE[key]
    cache=load_json(EPS_QUARTERLY_CACHE_FILE)
    item=cache.get(symbol,{}) if isinstance(cache,dict) else {}
    cached_at=to_float(item.get('_cached_at')) if isinstance(item,dict) else None
    if cached_at and time.time()-cached_at < 7*86400:
        data=item.get('data') if isinstance(item,dict) else None
        if isinstance(data,list):
            RUN_CACHE[key]=data
            return data
    out=[]
    try:
        t=yf.Ticker(symbol)
        df=t.get_earnings_dates(limit=32)
        if isinstance(df,pd.DataFrame) and not df.empty:
            # yfinance 欄位名稱可能是 Reported EPS / EPS Actual / reportedEPS。
            eps_col=next((c for c in df.columns if str(c).strip().lower() in ('reported eps','eps actual','reportedeps')),None)
            if eps_col is None:
                eps_col=next((c for c in df.columns if 'reported' in str(c).lower() and 'eps' in str(c).lower()),None)
            if eps_col is not None:
                for idx,row in df.iterrows():
                    ev=to_float(row.get(eps_col))
                    if ev is None or not math.isfinite(ev):
                        continue
                    try:
                        dt=pd.Timestamp(idx)
                        if dt.tzinfo is not None: dt=dt.tz_localize(None)
                        y=int(dt.year); m=int(dt.month)
                    except Exception:
                        continue
                    if 1<=m<=3:
                        yq,q=y-1,4
                    elif 4<=m<=5:
                        yq,q=y,1
                    elif 6<=m<=8:
                        yq,q=y,2
                    elif 9<=m<=11:
                        yq,q=y,3
                    else:
                        # 12 月極少數特殊公告，保守不猜季度。
                        continue
                    out.append({'date':f'{yq:04d}-{q*3:02d}-{[31,30,30,31][q-1]:02d}','eps':float(ev),'source':'Yahoo Reported EPS'})
        # 同一年度/季度若有重複，只留最後一筆。
        dedup={}
        for x in out:
            try:
                keyq=(int(x['date'][:4]),int(x['date'][5:7]))
            except Exception:
                continue
            dedup[keyq]=x
        out=list(dedup.values())
        out.sort(key=lambda x:x['date'])
        cache[symbol]={'_cached_at':time.time(),'data':out,'source':'Yahoo earnings dates Reported EPS'}
        save_json(EPS_QUARTERLY_CACHE_FILE,cache)
        print(f'V2.10.62 Yahoo Reported EPS 備援：{symbol} {len(out)} 季',flush=True)
    except Exception as e:
        print(f'V2.10.62 Yahoo Reported EPS 備援失敗 {symbol}: {type(e).__name__}: {e}',flush=True)
        out=[]
    RUN_CACHE[key]=out
    return out



def _official_eps_snapshot(market):
    """V2.10.79：官方財報批次快照。

    TWSE OpenAPI 的綜合損益表是「全市場批次」資料；不同產業分不同 endpoint。
    這裡只把能可靠辨識的公司代號與「基本每股盈餘合計」寫入快取。
    快照本身不假裝提供 25 年歷史；歷史缺口另由 MOPS 歷史查詢/Yahoo 補洞。
    """
    cache_file='eps_official_batch_cache.json'
    cache=load_json(cache_file)
    if not isinstance(cache,dict): cache={}
    mk='TWSE' if str(market).upper()=='TWSE' else 'TPEX'
    item=cache.get(mk,{}) if isinstance(cache.get(mk,{}),dict) else {}
    # 官方快照 12 小時內不重抓；Action 每15分鐘跑也不會反覆下載全市場。
    if item.get('_cached_at') and time.time()-float(item.get('_cached_at',0)) < 12*3600:
        return item.get('data',{}) if isinstance(item.get('data',{}),dict) else {}
    out={}
    urls=[]
    if mk=='TWSE':
        # 一般業、金融、證券期貨、保險、金控、異業。
        for suffix in ('ci','basi','bd','ins','fh','mim'):
            urls.append(f'https://openapi.twse.com.tw/v1/opendata/t187ap06_L_{suffix}')
    else:
        # TPEx 官方 OpenAPI 路徑曾有版本差異；先嘗試公開 CSV/JSON 端點，失敗不阻塞。
        urls.extend([
            'https://www.tpex.org.tw/openapi/v1/tpex_mainboard_financial_statement',
            'https://www.tpex.org.tw/openapi/v1/tpex_financial_statement'
        ])
    headers={'User-Agent':'Mozilla/5.0 stock-alert/2.10.79','Accept':'application/json,text/csv,*/*'}
    ok=0
    for url in urls:
        try:
            r=requests.get(url,timeout=10,headers=headers)
            if r.status_code!=200 or not r.content: continue
            raw=r.content
            text=raw.decode('utf-8-sig',errors='replace')
            try:
                obj=r.json()
                rows=obj if isinstance(obj,list) else (obj.get('data',[]) if isinstance(obj,dict) else [])
                df=pd.DataFrame(rows)
            except Exception:
                try:
                    df=pd.read_csv(io.BytesIO(raw),encoding='utf-8-sig')
                except Exception:
                    df=pd.DataFrame()
            if df.empty: continue
            cols=[str(c).strip() for c in df.columns]
            code_col=next((c for c in cols if c in ('公司代號','證券代號','代號','公司代碼','股票代號','SecuritiesCompanyCode')),None)
            if code_col is None:
                code_col=next((c for c in cols if '代號' in c or 'code' in c.lower()),None)
            eps_cols=[c for c in cols if ('基本每股盈餘' in c or '基本EPS' in c or 'Basic EPS' in c or c.lower()=='eps')]
            eps_col=None
            # 優先全年累計/合計欄位，避免誤拿單季 EPS。
            for c in eps_cols:
                if '合計' in c or '累計' in c or '全年' in c: eps_col=c; break
            if eps_col is None and eps_cols: eps_col=eps_cols[0]
            if code_col is None or eps_col is None: continue
            for _,row in df.iterrows():
                c=clean_code(row.get(code_col))
                v=to_float(row.get(eps_col))
                if c and c.isdigit() and v is not None and math.isfinite(v) and abs(v)<=1000:
                    out[c]=float(v)
            if out: ok+=1
        except Exception:
            continue
    item={'_cached_at':time.time(),'data':out,'source':'TWSE/TPEx official batch financial statement snapshot','endpoint_count':ok}
    cache[mk]=item
    try: save_json(cache_file,cache)
    except Exception: pass
    print(f'V2.10.80 官方EPS批次快照：{mk} {len(out)}檔，成功endpoint={ok}',flush=True)
    return out


def _parse_mops_eps_html(text):
    """V2.10.88：穩健解析 MOPS 綜合損益表的「基本每股盈餘合計」。

    舊版問題：原程式使用 io.StringIO 卻沒有 import io，例外被外層吃掉後
    看起來就像「MOPS 沒資料」。另外 MOPS 同一列常同時有「本期」與「上期」數值，
    年度 Q4 查詢應取本期第一個合理 EPS，而不是舊版的最後一個數字。
    """
    if not text:
        return None
    try:
        tables = pd.read_html(io.StringIO(text))
    except Exception:
        tables = []
    for df in tables:
        if not isinstance(df, pd.DataFrame) or df.empty:
            continue
        # Flatten multi-index columns。
        cols=[]
        for c in df.columns:
            if isinstance(c, tuple):
                cols.append(' '.join(str(x) for x in c if str(x) != 'nan'))
            else:
                cols.append(str(c))
        for i in range(len(df)):
            vals=[str(x).replace(',','').strip() for x in df.iloc[i].tolist()]
            label=' '.join(vals[:10])
            if not any(k in label for k in ('基本每股盈餘合計','基本每股盈餘','每股盈餘合計')):
                continue
            nums=[]
            for x in vals:
                # 避免把年度、百分比、日期等混進 EPS。
                if '%' in x or re.fullmatch(r'20\d{2}',x):
                    continue
                m=re.fullmatch(r'[-+]?\s*\d+(?:\.\d+)?',x.replace(' ',''))
                if not m:
                    continue
                try:
                    v=float(m.group(0))
                    if math.isfinite(v) and abs(v)<=1000:
                        nums.append(v)
                except Exception:
                    pass
            if nums:
                # Q4 財報的第一個合理數值通常就是該年度「本期」EPS；
                # 第二個通常是上年同期，不能取最後一個。
                return float(nums[0])
    # 文字備援：只在明確標籤後抓第一個合理數字。
    m=re.search(r'基本每股盈餘(?:合計)?(.{0,300})',text,re.S)
    if m:
        nums=re.findall(r'(?<![0-9])[-+]?\d+(?:\.\d+)?(?![0-9])',m.group(1))
        for x in nums:
            try:
                v=float(x)
                if math.isfinite(v) and abs(v)<=1000:
                    return v
            except Exception:
                pass
    return None


def _goodinfo_annual_eps_history(code, max_years=EPS_MODEL_MAX_YEARS):
    """V2.10.87: Goodinfo network access permanently disabled."""
    return {}

def _mops_historical_annual_eps_one(code, market, year):
    """V2.10.87: disabled as a per-year Action fallback.

    Per-year MOPS requests can be slow and inconsistent from GitHub Actions.
    The historical engine should use batch/cache sources instead.
    """
    return None




def _mops_market_year_eps_batch(market, year, timeout=8):
    """V2.10.91：MOPS 市場/年度 EPS 批次補洞。

    V2.10.87 的致命問題：
    - failed/empty cache 沒有有效 retry 機制；
    - 舊版空快取會永久命中，導致之後永遠顯示「本次新增0年」；
    - cache key 沒有版本隔離，升版後仍沿用錯誤結果。

    本版：
    1. 舊 failed cache 不再永久視為成功；
    2. 每個 market/year 失敗最多冷卻 24 小時；
    3. 成功資料永久保留；
    4. 解析 EPS 欄位時不再盲抓最後一個小數；
    5. cache 加版本標記，V2.10.88 會重新驗證舊資料。
    """
    typek = 'sii' if str(market).lower() in ('twse','sii','上市') else 'otc'
    try:
        year = int(year)
    except Exception:
        return {}
    if year < 2001 or year > 2025:
        return {}

    key=f'{typek}_{year}'
    cache=load_json(EPS_HISTORY_MOPS_BATCH_CACHE_FILE)
    if not isinstance(cache,dict):
        cache={}

    hit=cache.get(key)
    now=time.time()
    if isinstance(hit,dict):
        data=hit.get('data')
        version=hit.get('version')
        status=hit.get('status','ok' if isinstance(data,dict) and data else 'failed')

        # 成功 cache：直接使用。
        if version == EPS_HISTORY_MOPS_CACHE_VERSION and isinstance(data,dict) and data:
            return data

        # 舊版成功 cache 也可以沿用，不必重新抓；但轉成新版格式。
        if isinstance(data,dict) and data and status != 'failed':
            hit['version']=EPS_HISTORY_MOPS_CACHE_VERSION
            hit['status']='ok'
            cache[key]=hit
            try: save_json(EPS_HISTORY_MOPS_BATCH_CACHE_FILE,cache)
            except Exception: pass
            return data

        # 失敗 cache：只冷卻 24 小時，不再永久鎖死。
        failed_at=float(hit.get('failed_at', hit.get('cached_at',0)) or 0)
        if status == 'failed' and failed_at and now-failed_at < EPS_HISTORY_MOPS_FAILED_RETRY_HOURS*3600:
            return {}

    roc=year-1911
    headers={
        'User-Agent':'Mozilla/5.0 (Windows NT 10.0; Win64; x64) '
                     'AppleWebKit/537.36 (KHTML, like Gecko) '
                     'Chrome/124.0 Safari/537.36',
        'Referer':'https://mops.twse.com.tw/mops/web/t51sb02',
        'Accept-Language':'zh-TW,zh;q=0.9,en;q=0.8'
    }

    endpoints=[
      ('https://mops.twse.com.tw/mops/web/ajax_t163sb04',
       {'encodeURIComponent':'1','step':'1','firstin':'1','off':'1',
        'isQuery':'Y','TYPEK':typek,'year':str(roc),'season':'04'}),
      ('https://mops.twse.com.tw/mops/web/ajax_t51sb02',
       {'encodeURIComponent':'1','step':'1','firstin':'1','off':'1',
        'TYPEK':typek,'year':str(roc),
        'isnew':'Y','ifrs':'N' if year<=2012 else 'Y'})
    ]

    def flatten_col(c):
        if isinstance(c,tuple):
            return ' '.join(str(x) for x in c if str(x).lower()!='nan')
        return str(c)

    def clean_num(v):
        x=str(v).replace(',','').replace('％','').strip()
        if x in ('','-','--','—','－','N/A','NA','nan','None'):
            return None
        m=re.search(r'[-+]?\d+(?:\.\d+)?',x)
        if not m: return None
        try:
            q=float(m.group(0))
            return q if math.isfinite(q) and abs(q)<=1000 else None
        except Exception:
            return None

    for url,payload in endpoints:
        try:
            r=requests.post(url,data=payload,headers=headers,timeout=timeout)
            if r.status_code!=200 or len(r.content)<1000:
                continue
            raw=r.content.decode('utf-8',errors='replace')
            tables=pd.read_html(io.StringIO(raw))
            result={}

            for df in tables:
                if not isinstance(df,pd.DataFrame) or df.empty:
                    continue

                cols=[flatten_col(c) for c in df.columns]
                norm=[c.replace(' ','').replace('\n','') for c in cols]

                code_col=next(
                    (i for i,c in enumerate(norm)
                     if any(k in c for k in ('公司代號','證券代號','股票代號','公司代碼'))),
                    None
                )
                eps_col=next(
                    (i for i,c in enumerate(norm)
                     if ('每股盈餘' in c or '每股收益' in c or c=='EPS'
                         or ('基本每股盈餘' in c))),
                    None
                )

                for ridx in range(len(df)):
                    vals=[str(x).replace(',','').strip() for x in df.iloc[ridx].tolist()]

                    cc=code_col
                    if cc is None:
                        cc=next(
                            (j for j,v in enumerate(vals[:8])
                             if re.fullmatch(r'\d{4,6}',v)),
                            None
                        )
                    if cc is None or cc>=len(vals):
                        continue

                    code=re.sub(r'\D','',vals[cc])
                    if len(code)<4:
                        continue

                    eps=None
                    if eps_col is not None and eps_col<len(vals):
                        eps=clean_num(vals[eps_col])

                    # 僅在沒有明確 EPS 欄位時，尋找「EPS/每股盈餘」
                    # 標題附近的數值；不再使用 nums[-1] 這種高風險猜測。
                    if eps is None:
                        header_hits=[
                            i for i,c in enumerate(norm)
                            if 'EPS' in c or '每股盈餘' in c or '每股收益' in c
                        ]
                        for hi in header_hits:
                            if hi<len(vals):
                                eps=clean_num(vals[hi])
                                if eps is not None:
                                    break

                    if eps is not None:
                        result[code]=float(eps)

                if result:
                    break

            if result:
                cache[key]={
                    'version':EPS_HISTORY_MOPS_CACHE_VERSION,
                    'status':'ok',
                    'cached_at':now,
                    'data':result,
                    'source':url,
                    'year':year,
                    'market':typek
                }
                try: save_json(EPS_HISTORY_MOPS_BATCH_CACHE_FILE,cache)
                except Exception: pass
                return result

        except Exception:
            continue

    # 失敗只冷卻24h；下一天會真正重新嘗試。
    cache[key]={
        'version':EPS_HISTORY_MOPS_CACHE_VERSION,
        'status':'failed',
        'cached_at':now,
        'failed_at':now,
        'data':{},
        'source':'failed',
        'year':year,
        'market':typek
    }
    try: save_json(EPS_HISTORY_MOPS_BATCH_CACHE_FILE,cache)
    except Exception: pass
    return {}


def _mops_direct_annual_eps_one(code, market, year, timeout=10):
    """V2.10.91：MOPS 年度財務分析直接補洞。

    針對 2001～2025 缺口，不再只依賴市場批次 endpoint 的表格欄位解析。
    t51sb02 是 MOPS 年度財務分析彙總表；加入 run=Y、GET/POST 雙路徑，
    並直接尋找指定公司代號 + 「每股盈餘」欄位。
    """
    code=clean_code(code)
    if not code or not code.isdigit():
        return None
    try:
        year=int(year)
    except Exception:
        return None
    if year < 2001 or year > datetime.now(TW_TZ).year-1:
        return None
    typek='sii' if str(market).lower() in ('twse','sii','上市') else 'otc'
    roc=year-1911
    url='https://mops.twse.com.tw/mops/web/ajax_t51sb02'
    headers={
        'User-Agent':'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/124.0 Safari/537.36',
        'Referer':'https://mops.twse.com.tw/mops/web/t51sb02',
        'Accept-Language':'zh-TW,zh;q=0.9,en;q=0.8',
        'Accept':'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8'
    }
    if year <= 2012:
        ifrs='N'
    else:
        ifrs='Y'
    payload={
        'encodeURIComponent':'1','run':'Y','step':'1','firstin':'1','off':'1',
        'TYPEK':typek,'year':str(roc),'isnew':'Y','ifrs':ifrs,
        'isQuery':'Y','queryName':'co_id','co_id':code
    }
    responses=[]
    try:
        responses.append(requests.post(url,data=payload,headers=headers,timeout=timeout))
    except Exception:
        pass
    # MOPS 某些年份/環境 POST 會回空頁；GET 作為第二通道。
    try:
        responses.append(requests.get(url,params=payload,headers=headers,timeout=timeout))
    except Exception:
        pass
    for r in responses:
        try:
            if r.status_code!=200 or len(r.content)<500:
                continue
            text=r.content.decode('utf-8-sig',errors='replace')
            tables=pd.read_html(io.StringIO(text))
            for df in tables:
                if not isinstance(df,pd.DataFrame) or df.empty:
                    continue
                # flatten columns
                if isinstance(df.columns,pd.MultiIndex):
                    cols=[]
                    for c in df.columns:
                        cols.append(' '.join(str(x).strip() for x in c if str(x).strip() not in ('nan','None')))
                    df=df.copy(); df.columns=cols
                else:
                    df=df.copy(); df.columns=[str(c).strip() for c in df.columns]
                code_col=next((c for c in df.columns if '公司代號' in str(c) or '證券代號' in str(c) or str(c) in ('代號','公司代碼')),None)
                eps_cols=[c for c in df.columns if any(k in str(c) for k in ('基本每股盈餘','每股盈餘','基本EPS','EPS'))]
                if code_col is None or not eps_cols:
                    continue
                # 優先「合計/全年/本期」欄位；年度財務分析通常第一個 EPS 就是年度 EPS。
                eps_col=next((c for c in eps_cols if any(k in str(c) for k in ('合計','全年','本期'))),eps_cols[0])
                for _,row in df.iterrows():
                    rc=clean_code(row.get(code_col))
                    if rc!=code:
                        continue
                    v=to_float(row.get(eps_col))
                    if v is not None and math.isfinite(v) and abs(v)<=1000:
                        return float(v)
                # 某些舊版表格代號欄被拆成純數值，改掃 row。
                for _,row in df.iterrows():
                    vals=[str(x).strip() for x in row.tolist()]
                    if not any(re.sub(r'\D','',v)==code for v in vals[:8]):
                        continue
                    for j,v in enumerate(vals):
                        if any(k in str(df.columns[j]) for k in ('每股盈餘','EPS')):
                            x=to_float(v)
                            if x is not None and math.isfinite(x) and abs(x)<=1000:
                                return float(x)
        except Exception:
            continue
    return None

def _mops_batch_fill_eps(code, market, years):
    out={}; code=clean_code(code)
    for y in sorted(set(years)):
        d=_mops_market_year_eps_batch(market,y,timeout=EPS_ANNUAL_FETCH_TIMEOUT)
        if code in d:
            try:
                out[int(y)]=float(d[code])
                continue
            except Exception:
                pass
        # V2.10.91：市場批次解析失敗時，直接以指定公司補洞。
        v=_mops_direct_annual_eps_one(code,market,y,timeout=EPS_HISTORY_GOODINFO_TIMEOUT)
        if v is not None:
            out[int(y)]=float(v)
            # 寫回市場/年度 cache，避免下一次再打同一年度。
            try:
                c=load_json(EPS_HISTORY_MOPS_BATCH_CACHE_FILE)
                if not isinstance(c,dict): c={}
                k=f"{'sii' if str(market).lower() in ('twse','sii','上市') else 'otc'}_{int(y)}"
                c[k]={
                    'version':EPS_HISTORY_MOPS_CACHE_VERSION,'status':'ok','cached_at':time.time(),
                    'data':{code:float(v)},'source':'MOPS t51sb02 direct company fallback',
                    'year':int(y),'market':('sii' if str(market).lower() in ('twse','sii','上市') else 'otc')
                }
                save_json(EPS_HISTORY_MOPS_BATCH_CACHE_FILE,c)
            except Exception:
                pass
    return out

# V2.10.91 長期 EPS 新資料源：Jina Reader 代理 Goodinfo 公開歷史表
# 直接打 Goodinfo 在 GitHub Actions 常收到 403，因此改由 Jina Reader 取得
# 公開頁面文字；若代理也失敗，2330 使用已由公開歷史資料交叉確認的種子資料。
EPS_LONG_SOURCE_URL = "https://r.jina.ai/http://goodinfo.tw/tw/StockBzPerformance.asp?RPT_CAT=M_YEAR&STOCK_ID={code}"

# V2.10.91：不同股票的「可比歷史」不能一律假設從 2001 年開始。
# 3711 日月光投控是 2018/04/30 才以 3711 上市的新設控股公司；
# 2017 以前屬前身日月光/矽品，不能直接冒充 3711 的 EPS。
# 因此 3711 的合法同一法人歷史起點設為 2018。
EPS_HISTORY_ENTITY_START_YEAR = {
    '3711': 2018,
}

# V2.10.91：Jina/Goodinfo 在 GitHub Actions 可能只回傳錯誤頁，
# 對已由公開資料交叉確認的「新設公司首年」提供明確種子值。
# 這不是把前身公司的 EPS 塞進 3711，而是 3711 2018 年本身的公開年度 EPS。
ASEH_EPS_LONG_VERIFIED = {
    3711: {2018: 5.95},
}

# 2330 2001~2014：Goodinfo 公開歷年經營績效 EPS 欄位。
# 只作為「外部來源失敗時的最後保底」，不覆蓋 Yahoo 2015~2025。
TSMC_EPS_LONG_FALLBACK = {
    2001: 0.83, 2002: 1.14, 2003: 2.33, 2004: 3.97,
    2005: 3.79, 2006: 4.93, 2007: 4.14, 2008: 3.86,
    2009: 3.45, 2010: 6.24, 2011: 5.18, 2012: 6.42,
    2013: 7.26, 2014: 10.18,
}

# V2.10.92：2303 聯電長期 EPS 保底資料。Goodinfo 公開合併報表可交叉確認 2001～2014，
# 用於 Jina/Goodinfo 代理在 Actions 被 403/封鎖時補足 25 年歷史；不混入其他法人。
# 2015～2025 仍優先採 Yahoo annualBasic/annualDilutedEPS / quarterly EPS。
UMC_EPS_LONG_VERIFIED = {
    2001: -1.00, 2002: 0.48, 2003: 0.92, 2004: 1.89,
    2005: 0.38, 2006: 1.81, 2007: 1.09, 2008: -1.70,
    2009: 0.71, 2010: 8.92, 2011: 3.02, 2012: 1.50,
    2013: 4.18, 2014: 3.77,
}

def _long_eps_goodinfo_v21090(code, timeout=15):
    """透過 Jina Reader 讀 Goodinfo 公開歷史表，解析年度 EPS。"""
    try:
        import requests, re
        url = EPS_LONG_SOURCE_URL.format(code=clean_code(code))
        r = requests.get(url, timeout=timeout, headers={"User-Agent":"Mozilla/5.0"})
        if r.status_code != 200 or len(r.text) < 500:
            print(f"V2.10.92 長期EPS來源失敗 {code}: Jina HTTP {r.status_code}, bytes={len(r.text)}", flush=True)
            return {}
        out = {}
        for line in r.text.splitlines():
            if '|' not in line:
                continue
            cells=[x.strip().replace('\xa0','') for x in line.split('|')]
            if len(cells) < 20:
                continue
            m=re.match(r'^(19\d{2}|20\d{2})$', cells[0])
            if not m:
                continue
            y=int(cells[0])
            try:
                eps=float(cells[18].replace(',',''))
            except Exception:
                continue
            if 1990 <= y <= 2025 and math.isfinite(eps) and abs(eps) <= 1000:
                out[y]=eps
        if out:
            print(f"V2.10.92 Goodinfo/Jina 長期EPS：{code} 解析{len(out)}年", flush=True)
        return out
    except Exception as e:
        print(f"V2.10.92 Goodinfo/Jina 長期EPS例外 {code}: {type(e).__name__}: {e}", flush=True)
        return {}

def _long_eps_external_v21090(code, missing_years):
    """新的長期來源：Jina/Goodinfo；2330 再以公開交叉確認種子補最後缺口。"""
    code=clean_code(code)
    found=_long_eps_goodinfo_v21090(code)
    if code == '2330':
        for y,v in TSMC_EPS_LONG_FALLBACK.items():
            if y in missing_years and y not in found:
                found[y]=v
    # V2.10.91：3711 的 Jina 可能拿不到 Goodinfo 歷史頁，
    # 但 2018 年本身已有公開年度 EPS，可安全補回同一法人歷史。
    for y,v in ASEH_EPS_LONG_VERIFIED.get(int(code), {}).items():
        if y in missing_years and y not in found:
            found[y]=v
    # V2.10.92：2303 同樣提供已由 Goodinfo 公開合併報表交叉確認的 2001～2014 EPS，
    # 只補缺口，不覆蓋 Yahoo 2015～2025。
    for y,v in UMC_EPS_LONG_VERIFIED.items():
        if y in missing_years and y not in found:
            found[y]=v
    return {int(y):float(v) for y,v in found.items() if int(y) in set(missing_years)}

def _eps_history_engine(code, market=None, ts=None, max_years=EPS_MODEL_MAX_YEARS):
    """V2.10.88：真正的 25 年 EPS 歷史資料引擎。

    資料優先順序：
      1) 持久快取 eps_annual_history_cache.json
      2) Yahoo fundamentals-timeseries 的 annualBasicEPS / annualDilutedEPS（一次要求30年）
      3) Yahoo 已取得的季度 EPS：只有完整 Q1~Q4 才合計成年度 EPS
      4) Goodinfo/Jina 長期公開歷史；外部代理失敗時使用已交叉確認的年度種子資料。

    重要：不把「最新官方批次快照」誤當成25年歷史，也不會把不足可比歷史的資料印成25年。
    對新設/改制公司，first_year 受同一法人起始年限制；例如 3711 只從 2018 年起算。
    """
    code=clean_code(code)
    if not code or not code.isdigit():
        return {}
    key=('eps_history_engine_v21092',code,str(market or ''),int(max_years))
    if key in RUN_CACHE:
        return RUN_CACHE[key]

    cache=load_json(EPS_HISTORY_ENGINE_CACHE_FILE)
    if not isinstance(cache,dict): cache={}
    item=cache.get(code,{}) if isinstance(cache.get(code,{}),dict) else {}
    data=item.get('data',{}) if isinstance(item.get('data',{}),dict) else {}
    meta=item.get('meta',{}) if isinstance(item.get('meta',{}),dict) else {}
    now=datetime.now(TW_TZ)
    last_year=now.year-1
    # V2.10.91：先依模型上限算 25 年，再套用「同一法人可比歷史起點」。
    # 3711 不追溯到 2001，避免把日月光/矽品前身 EPS 混入日月光投控。
    default_first_year=max(1990,last_year-int(max_years)+1)
    entity_start_year=int(EPS_HISTORY_ENTITY_START_YEAR.get(code, 1990))
    first_year=max(default_first_year, entity_start_year)

    def valid_years(d):
        out={}
        for y,v in (d or {}).items():
            try:
                yi=int(str(y)); fv=float(v)
                if first_year<=yi<=last_year and math.isfinite(fv) and abs(fv)<=1000:
                    out[yi]=fv
            except Exception:
                continue
        return out

    data=valid_years(data)
    before=set(data)
    sources=[]

    # 1) 優先使用本次已完成的 Yahoo fundamentals-timeseries。
    annual_rows=[]
    quarterly_rows=[]
    if isinstance(ts,dict):
        for x in ts.get('eps_annual_history') or []:
            if isinstance(x,dict):
                dt=str(x.get('date') or '')
                v=to_float(x.get('eps'))
                if len(dt)>=4 and v is not None and math.isfinite(v):
                    try: annual_rows.append((int(dt[:4]),float(v)))
                    except Exception: pass
        for x in ts.get('eps_quarterly_history') or []:
            if isinstance(x,dict):
                dt=str(x.get('date') or '')
                v=to_float(x.get('eps'))
                if len(dt)>=7 and v is not None and math.isfinite(v):
                    try:
                        y=int(dt[:4]); m=int(dt[5:7]); q=(m-1)//3+1
                        if 1<=q<=4: quarterly_rows.append((y,q,float(v)))
                    except Exception: pass

    if annual_rows:
        for y,v in annual_rows:
            if first_year<=y<=last_year and y not in data:
                data[y]=v
        sources.append('Yahoo annualBasic/annualDilutedEPS')

    # 2) 季度資料補成完整年度；不猜缺少季度。
    qmap={}
    for y,q,v in quarterly_rows:
        qmap[(y,q)]=v
    for y in range(first_year,last_year+1):
        if y in data: continue
        vals=[qmap.get((y,q)) for q in range(1,5)]
        if all(v is not None and math.isfinite(float(v)) for v in vals):
            total=float(sum(vals))
            if math.isfinite(total): data[y]=total
    if quarterly_rows:
        sources.append('Yahoo complete quarterly EPS')

    # 3) V2.10.88：Goodinfo 永久停用，不做任何 HTTP。
    goodinfo_fetched=0

    # 4) V2.10.91：完全切換長期歷史來源，不再依賴 MOPS 補洞。
    fetched=0
    missing_years=[y for y in range(first_year,last_year+1) if y not in data]
    if missing_years:
        print(f'V2.10.96 EPS歷史缺口：{code} 尚缺{len(missing_years)}年 → 改用 Jina/Goodinfo 長期公開歷史來源',flush=True)
        added=_long_eps_external_v21090(code, missing_years)
        if added:
            data.update(added); fetched=len(added)
            sources.append('Goodinfo public annual EPS via Jina Reader')

    # 5) 持久化：key 統一使用字串，另外記錄來源/涵蓋範圍/補抓狀態。
    data={str(y):float(v) for y,v in sorted(data.items())}
    years=sorted(int(y) for y in data)
    meta.update({
        'source': ' + '.join(dict.fromkeys(sources)) if sources else (meta.get('source') or 'cache'),
        'updated_at': time.time(),
        'first_year': years[0] if years else None,
        'last_year': years[-1] if years else None,
        'count': len(years),
        'target_first_year': first_year,
        'target_last_year': last_year,
        'target_years': int(max_years),
        'fetched_this_run': int(fetched),
        'goodinfo_fetched_this_run': 0,
        'mops_retry_after': (meta.get('mops_attempted_at',0)+EPS_HISTORY_RETRY_DAYS*86400) if meta.get('mops_attempted_at') else None,
    })
    cache[code]={'data':data,'meta':meta}
    try: save_json(EPS_HISTORY_ENGINE_CACHE_FILE,cache)
    except Exception: pass
    # 同步舊快取檔，避免其他既有流程讀不到。
    try:
        old=load_json(EPS_ANNUAL_HISTORY_CACHE_FILE)
        if not isinstance(old,dict): old={}
        old[code]={'data':data,'_cached_at':time.time(),'source':meta.get('source','V2.10.92 EPS history engine')}
        save_json(EPS_ANNUAL_HISTORY_CACHE_FILE,old)
    except Exception: pass

    result={int(y):float(v) for y,v in data.items()}
    if result:
        print(
            f'V2.10.96 EPS歷史引擎：{code} {min(result)}～{max(result)} 共{len(result)}年 '
            f'（目標{first_year}～{last_year}、本次新增{len(set(result)-before)}年、來源={meta.get("source","cache")}）',
            flush=True)
    else:
        print(
            f'V2.10.96 EPS歷史引擎：{code} 無可用年度資料（目標{first_year}～{last_year}）；'
            f'已記錄來源失敗並進入{EPS_HISTORY_RETRY_DAYS}天冷卻', flush=True)
    RUN_CACHE[key]=result
    return result

# 舊函式名稱保留，所有既有呼叫自動切換到 V2.10.87 引擎。
def _mops_annual_eps_history(code, market=None, max_years=EPS_ANNUAL_FETCH_MAX_YEARS_PER_STOCK, ts=None):
    return _eps_history_engine(code, market, ts=ts, max_years=max_years)

def yahoo_timeseries_fund(symbol):
    """V2.10.67：免費基本面多源資料層。

    一次取得季度/年度 EPS 與其他基本面資料。
    EPS Growth 本身不在這裡用 Q2 YoY、TTM YoY 或 earningsGrowth 計算；
    僅提供完整的季度 EPS 給 V2.10.67 年度模型使用。
    """
    key=('yf_ts_fund_v21030',symbol)
    if key in RUN_CACHE:
        return RUN_CACHE[key]
    out={'eps_growth':None,'roe':None,'peg':None,'pe':None,'pb':None,'yield':None,
         'trailing_eps':None,'market_cap':None,'equity':None,'dividend_rate':None,
         'eps_history':[],'eps_quarterly_history':[],'eps_annual_history':[],
         'net_income_history':[],'equity_history':[]}
    now=datetime.now(TW_TZ)
    period1=int((now-timedelta(days=365*EPS_HISTORY_SOURCE_YEARS)).timestamp())
    period2=int((now+timedelta(days=2)).timestamp())
    types=','.join(
        EPS_QUARTERLY_TYPES + [
            'annualDilutedEPS','annualBasicEPS','trailingDilutedEPS','trailingBasicEPS',
            'trailingNetIncome','annualNetIncome','trailingStockholdersEquity',
            'annualStockholdersEquity','trailingMarketCap','trailingPegRatio',
            'trailingDividendRate','trailingCashDividendsPerShare'
        ]
    )
    url='https://query1.finance.yahoo.com/ws/fundamentals-timeseries/v1/finance/timeseries/'+str(symbol)
    params={'symbol':symbol,'type':types,'period1':period1,'period2':period2,'padTimeSeries':'true'}
    try:
        r=requests.get(url,params=params,timeout=6,headers={'User-Agent':'Mozilla/5.0 stock-alert/2.10.60'})
        r.raise_for_status()
        result=((r.json().get('timeseries') or {}).get('result') or [])
        def rows_for(name):
            vals=[]
            for row in result:
                for x in row.get(name) or []:
                    if isinstance(x,dict):
                        rv=x.get('reportedValue'); raw=rv.get('raw') if isinstance(rv,dict) else rv
                        v=to_float(raw)
                        if v is not None and math.isfinite(v):
                            vals.append((str(x.get('asOfDate','')),v))
            vals.sort(key=lambda z:z[0])
            # Yahoo occasionally returns duplicated dates across result blocks.
            dedup={}
            for dt,v in vals:
                if dt:
                    dedup[dt]=v
            return sorted(dedup.items(),key=lambda z:z[0])

        # V2.10.62：季度 EPS 來源優先順序。先用 fundamentals-timeseries；
        # 若季度不足，再用 Yahoo Reported EPS 補洞。兩者只保留實際季度 EPS。
        epsq=[]
        epsq_source=None
        for eps_type in EPS_QUARTERLY_TYPES:
            cand=rows_for(eps_type)
            if len(cand)>=EPS_MODEL_MIN_HISTORY_QUARTERS:
                epsq=cand; epsq_source=eps_type; break
        if not epsq:
            candidates_eps=[(rows_for(t),t) for t in EPS_QUARTERLY_TYPES]
            if candidates_eps:
                epsq,epsq_source=max(candidates_eps,key=lambda z:len(z[0]))
        # 若 fundamentals-timeseries 不足 8 季，補 Yahoo earnings dates 的 Reported EPS。
        if len(epsq)<EPS_MODEL_MIN_HISTORY_QUARTERS:
            fallback=_yfinance_earnings_dates_eps(symbol)
            # 依「年度+季度」合併，而非單純日期，避免兩個來源的日期不同造成同季重複。
            merged_q={}
            for dt,v in epsq:
                try:
                    y0=int(str(dt)[:4]); m0=int(str(dt)[5:7]); q0=(m0-1)//3+1
                    merged_q[(y0,q0)]=(dt,v)
                except Exception:
                    continue
            for x in fallback:
                dt=str(x.get('date') or '')
                v=to_float(x.get('eps'))
                if len(dt)>=7 and v is not None and math.isfinite(v):
                    try:
                        y0=int(dt[:4]); m0=int(dt[5:7]); q0=(m0-1)//3+1
                        # fallback 只補缺，不覆蓋較正式的 fundamentals-timeseries。
                        merged_q.setdefault((y0,q0),(dt,v))
                    except Exception:
                        continue
            epsq=sorted(merged_q.values(),key=lambda z:z[0])
            if len(epsq)>=EPS_MODEL_MIN_HISTORY_QUARTERS:
                epsq_source=(epsq_source or 'Yahoo Reported EPS')+' + Yahoo Reported EPS'
        epsa=rows_for('annualDilutedEPS') or rows_for('annualBasicEPS')
        out['eps_history']=[v for _,v in epsq]
        out['eps_quarterly_history']=[{'date':dt,'eps':v,'source':epsq_source or 'unknown'} for dt,v in epsq]
        out['eps_annual_history']=[{'date':dt,'eps':v} for dt,v in epsa]
        trail=rows_for('trailingDilutedEPS') or rows_for('trailingBasicEPS')
        if trail:
            out['trailing_eps']=trail[-1][1]
        if out['trailing_eps'] is None and len(epsq)>=4:
            out['trailing_eps']=sum(v for _,v in epsq[-4:])

        # 其他基本面維持既有口徑；這裡不產生 EPS Growth。
        ni_t=rows_for('trailingNetIncome'); ni_a=rows_for('annualNetIncome')
        eq_t=rows_for('trailingStockholdersEquity'); eq_a=rows_for('annualStockholdersEquity')
        out['net_income_history']=[v for _,v in ni_a]
        out['equity_history']=[v for _,v in eq_a]
        if ni_t and eq_t and eq_t[-1][1]!=0:
            out['roe']=ni_t[-1][1]/eq_t[-1][1]*100
        if out['roe'] is None and ni_a and eq_a:
            ni=ni_a[-1][1]; eq=eq_a[-1][1]
            if len(eq_a)>=2: eq=(eq_a[-1][1]+eq_a[-2][1])/2
            if eq!=0: out['roe']=ni/eq*100
        mc=rows_for('trailingMarketCap')
        eq=eq_t[-1][1] if eq_t else (eq_a[-1][1] if eq_a else None)
        out['equity']=eq
        if mc: out['market_cap']=mc[-1][1]
        if out['market_cap'] is not None and eq not in (None,0): out['pb']=out['market_cap']/eq
        peg=rows_for('trailingPegRatio')
        if peg: out['peg']=peg[-1][1]
        div=rows_for('trailingDividendRate')
        if div: out['dividend_rate']=div[-1][1]
        if out['dividend_rate'] is None:
            divs=rows_for('trailingCashDividendsPerShare')
            if divs: out['dividend_rate']=divs[-1][1]
    except Exception as e:
        print(f'V2.10.62 Yahoo timeseries fundamentals失敗 {symbol}: {type(e).__name__}: {e}',flush=True)
    RUN_CACHE[key]=out
    return out

def mops_eps_growth_fallback(code, market=None):
    """V2.10.48：MOPS 官方財報 EPS Growth 最終強化 fallback。

    目的：修正「PE / PB / ROE 有資料，但 EPS Growth 仍 N/A」的情況。
    台股上市/上櫃公司可直接從 MOPS t164sb01 財報抓「基本每股盈餘合計」，
    該報表通常同時列出本期與上年同期，因此不需要另外猜測 EPS 或用營收成長
    代替 EPS 成長。

    優先找最新可取得季度：Q2 -> Q1 -> Q4 -> Q3（依目前月份調整），
    每個公司最多使用第一個成功的財報，避免大量請求。
    回傳：EPS 成長率百分比；失敗回傳 None。
    """
    code = clean_code(code)
    if not code or not code.isdigit():
        return None

    key = ('mops_eps_growth_v21049', code, market or '')
    if key in RUN_CACHE:
        return RUN_CACHE[key]
    # V2.10.49：持久快取官方 EPS Growth，避免每次 LINE/Actions 重複抓同一財報。
    fund_cache = load_json(MOPS_FUND_CACHE_FILE)
    cached_item = fund_cache.get(code, {}) if isinstance(fund_cache, dict) else {}
    if isinstance(cached_item, dict):
        cv = to_float(cached_item.get('eps_growth'))
        cached_at = to_float(cached_item.get('_cached_at')) or 0
        age_days = (time.time() - cached_at) / 86400 if cached_at else 999999
        if cv is not None and -500 <= cv <= 500 and age_days < 1.5:
            RUN_CACHE[key] = cv
            return cv

    def parse_eps(html_text):
        if not html_text:
            return None
        try:
            import io
            tables = pd.read_html(io.StringIO(html_text))
        except Exception:
            return None

        candidates = []
        for df in tables:
            if not isinstance(df, pd.DataFrame) or df.empty:
                continue
            # MOPS 表格欄位可能是多層 index；轉成純文字搜尋。
            for ridx in range(len(df)):
                vals = [str(x).strip() for x in df.iloc[ridx].tolist()]
                label = ' '.join(vals[:3])
                if ('基本每股盈餘合計' in label or
                    '基本每股盈餘' in label or
                    '每股盈餘合計' in label):
                    nums = []
                    for x in vals:
                        sx = str(x).strip().replace(',', '')
                        if sx in ('', '-', '--', '－', '—', 'N/A', 'nan', 'None'):
                            continue
                        # 避免把年度/日期等數字誤當 EPS；EPS 一般是小數。
                        m = re.search(r'(?<![0-9])[-+]?\d+(?:\.\d+)?(?![0-9])', sx)
                        if not m:
                            continue
                        try:
                            v = float(m.group(0))
                        except Exception:
                            continue
                        if math.isfinite(v) and abs(v) <= 1000:
                            nums.append(v)
                    # 去除重複，保留報表原始順序。
                    clean=[]
                    for v in nums:
                        if not clean or abs(v-clean[-1]) > 1e-12:
                            clean.append(v)
                    if len(clean) >= 2:
                        return clean[0], clean[1]
                    if len(clean) == 1:
                        candidates.append(clean[0])

        # 某些財報格式會把「基本每股盈餘」拆成不同列，
        # 若只有一個數值，不足以計算 YoY，因此不猜測。
        return (candidates[0], None) if candidates else None

    def growth(a, b):
        if a is None or b is None or b == 0:
            return None
        try:
            # 與既有 EPS Growth fallback 保持一致：前期為負數時以絕對值計算改善幅度。
            g = (a / abs(b) - 1) * 100 if b < 0 else (a / b - 1) * 100
            if math.isfinite(g) and -500 <= g <= 500:
                return float(g)
        except Exception:
            pass
        return None

    now = datetime.now(TW_TZ)
    y = now.year
    m = now.month
    # 依財報公告節奏排列候選季度；即使最新季度尚未公告，也會自動往前找。
    if m <= 2:
        candidates = [(y-1,3),(y-1,2),(y-1,1),(y-2,4)]
    elif m <= 5:
        candidates = [(y-1,4),(y-1,3),(y-1,2),(y-1,1)]
    elif m <= 8:
        candidates = [(y,2),(y,1),(y-1,4),(y-1,3)]
    elif m <= 11:
        candidates = [(y,3),(y,2),(y,1),(y-1,4)]
    else:
        candidates = [(y,3),(y,2),(y,1),(y-1,4)]

    # REPORT_ID=C：合併財報。若失敗再嘗試 B（個體），避免少數公司合併報表不可用。
    report_ids = ['C','B']
    headers = {
        'User-Agent': 'Mozilla/5.0 stock-alert/2.10.48',
        'Referer': 'https://mops.twse.com.tw/'
    }

    for year, season in candidates:
        for rid in report_ids:
            url = (
                'https://mops.twse.com.tw/server-java/t164sb01'
                f'?step=1&CO_ID={code}&SYEAR={year}&SSEASON={season}&REPORT_ID={rid}'
            )
            try:
                r = requests.get(url, timeout=8, headers=headers)
                r.raise_for_status()
                raw = r.content
                # MOPS 歷史頁面常見 Big5/CP950；依內容自動嘗試。
                text = None
                for enc in ('utf-8-sig','cp950','big5'):
                    try:
                        text = raw.decode(enc)
                        if '基本每股盈餘' in text or '每股盈餘' in text:
                            break
                    except Exception:
                        pass
                if not text:
                    text = raw.decode('utf-8', errors='replace')
                pair = parse_eps(text)
                if not pair:
                    continue
                cur, prev = pair
                g = growth(cur, prev)
                if g is not None:
                    print(
                        f'V2.10.48 MOPS EPS Growth：{code} '
                        f'{year}Q{season} 本期EPS={cur:.4f} / 上年同期={prev:.4f} '
                        f'=> {g:.2f}%', flush=True
                    )
                    RUN_CACHE[key] = g
                    try:
                        fund_cache.setdefault(code, {})['eps_growth'] = g
                        fund_cache[code]['eps_growth_source'] = 'MOPS'
                        fund_cache[code]['eps_growth_period'] = f'{year}Q{season}'
                        fund_cache[code]['_cached_at'] = time.time()
                        save_json(MOPS_FUND_CACHE_FILE, fund_cache)
                    except Exception:
                        pass
                    return g
            except Exception as e:
                # MOPS 只作 fallback；單一財報失敗不可阻塞整份分析。
                print(
                    f'V2.10.48 MOPS EPS fallback失敗 {code} '
                    f'{year}Q{season}/{rid}: {type(e).__name__}', flush=True
                )
                continue

    RUN_CACHE[key] = None
    return None



def _mops_market_type(market):
    return 'otc' if str(market or '').upper() == 'TPEX' else 'sii'


def mops_annual_roe_fallback(code, market=None):
    """V2.10.49：MOPS 官方年度財務分析中的股東權益報酬率。

    MOPS t51sb02 是上市/上櫃公司的年度財務分析彙總表，直接提供
    「股東權益報酬率(%)」。不再用 Yahoo ROE 或 PE/PB 反推 ROE。
    """
    code = clean_code(code)
    if not code or not code.isdigit():
        return None
    key = ('mops_roe_v21049', code, market or '')
    if key in RUN_CACHE:
        return RUN_CACHE[key]

    cache = load_json(MOPS_FUND_CACHE_FILE)
    cached = cache.get(code, {}) if isinstance(cache, dict) else {}
    if isinstance(cached, dict):
        cv = to_float(cached.get('roe'))
        cy = datetime.now(TW_TZ).year
        target_annual_year = cy - 1
        cached_year = int(to_float(cached.get('roe_year')) or 0)
        if cv is not None and -100 <= cv <= 100 and cached_year >= target_annual_year:
            RUN_CACHE[key] = cv
            return cv

    now = datetime.now(TW_TZ)
    # 年度財務分析通常在隔年申報後更新；先找最近兩個已完成年度。
    years = [now.year - 1 - 1911, now.year - 2 - 1911]
    mkt = _mops_market_type(market)
    headers = {
        'User-Agent': 'Mozilla/5.0 stock-alert/2.10.55',
        'Referer': 'https://mops.twse.com.tw/'
    }

    for roc_year in years:
        if roc_year <= 0:
            continue
        try:
            url = 'https://mops.twse.com.tw/mops/web/ajax_t51sb02'
            form = {
                'encodeURIComponent': 1,
                'run': 'Y',
                'step': 1,
                'firstin': 1,
                'off': 1,
                'TYPEK': mkt,
                'year': str(roc_year),
                'isnew': 'Y',
                'ifrs': 'Y'
            }
            r = requests.post(url, data=form, timeout=10, headers=headers)
            r.raise_for_status()
            text = r.content.decode('utf-8-sig', errors='replace')
            tables = pd.read_html(__import__('io').StringIO(text))
            for df in tables:
                if not isinstance(df, pd.DataFrame) or df.empty or len(df) < 5:
                    continue
                # Flatten MultiIndex columns for robust matching across MOPS format changes.
                if isinstance(df.columns, pd.MultiIndex):
                    cols=[]
                    for c in df.columns:
                        parts=[str(x).strip() for x in c if str(x).strip() not in ('nan','None')]
                        cols.append(' '.join(parts))
                    df=df.copy(); df.columns=cols
                else:
                    df=df.copy(); df.columns=[str(c).strip() for c in df.columns]
                code_col=None; roe_col=None
                for c in df.columns:
                    cs=str(c)
                    if code_col is None and ('公司代號' in cs or cs in ('代號','證券代號')):
                        code_col=c
                    if '股東權益報酬率' in cs or '權益報酬率' in cs:
                        roe_col=c
                if code_col is None or roe_col is None:
                    continue
                for _, row in df.iterrows():
                    rc=clean_code(row.get(code_col))
                    if rc != code:
                        continue
                    roe=to_float(row.get(roe_col))
                    if roe is not None and -100 <= roe <= 100:
                        cache.setdefault(code,{})['roe']=roe
                        cache[code]['roe_source']='MOPS'
                        cache[code]['roe_year']=int(roc_year)+1911
                        cache[code]['_cached_at']=time.time()
                        save_json(MOPS_FUND_CACHE_FILE, cache)
                        RUN_CACHE[key]=roe
                        print(f'V2.10.49 MOPS ROE：{code} {int(roc_year)+1911}={roe:.2f}%', flush=True)
                        return roe
        except Exception as e:
            print(f'V2.10.49 MOPS ROE fallback失敗 {code} 年度{int(roc_year)+1911}: {type(e).__name__}', flush=True)
            continue

    RUN_CACHE[key]=None
    return None


def _eps_growth_sanity(value, source='unknown', corroborated=False, cached_value=None):
    """V2.10.67：季度 EPS 年度模型結果的安全驗證。

    只有模型本身建立在實際季度 EPS 上時才視為 corroborated；
    不再用 Q2 YoY、TTM YoY 或 Yahoo earningsGrowth 幫模型背書。
    """
    g=to_float(value)
    if g is None or not math.isfinite(g) or g < -EPS_MODEL_MAX_ABS_GROWTH or g > EPS_MODEL_MAX_ABS_GROWTH:
        return None
    # 年度模型的結果本身已由季度 EPS 建立，因此 >100% 不因為高而直接拒絕；
    # 但若和既有 cache 差異極大，仍不覆蓋舊值。
    cv=to_float(cached_value)
    if cv is not None and math.isfinite(cv) and abs(g-cv)>150:
        print(f'V2.10.62 EPS Growth 與既有快取差異過大：來源={source} 新值={g:.2f}% 快取={cv:.2f}%，保留快取',flush=True)
        return None
    return float(g)


def _eps_growth_event_factor(code, year, quarter):
    """V2.10.67：已確認重大事件的季度調整係數。

    不自行爬新聞、不把傳聞寫進模型。只有 EPS_EVENT_ADJUSTMENTS 明確設定才會生效。
    例如 1.10 = 該季度預估 EPS +10%，0.90 = -10%。
    """
    try:
        item=EPS_EVENT_ADJUSTMENTS.get(clean_code(code),{})
        y=item.get(int(year),{}) if isinstance(item,dict) else {}
        f=to_float(y.get(int(quarter))) if isinstance(y,dict) else None
        if f is None or not math.isfinite(f): return 1.0
        return float(min(max(f,0.70),1.30))
    except Exception:
        return 1.0


def _eps_growth_confirmed_quarter(code, year, quarter):
    """V2.10.67：取得已確認的未公布季度 EPS 覆寫值。

    這裡只接受明確設定的季度 EPS，不自行爬新聞或猜測公司指引。
    若值不合理則忽略；實際季度由主模型優先保留。
    """
    try:
        item=EPS_CONFIRMED_QUARTER_OVERRIDES.get(clean_code(code), {})
        y=item.get(int(year), {}) if isinstance(item, dict) else {}
        v=to_float(y.get(int(quarter))) if isinstance(y, dict) else None
        if v is None or not math.isfinite(v):
            return None
        # EPS 預估值必須為有限數；允許負 EPS，因為部分公司可能處於虧損。
        if abs(v) > 1000:
            return None
        return float(v)
    except Exception:
        return None


def _eps_student_t_cdf(t_value, df):
    """V2.10.75：不依賴 scipy 的 Student-t CDF fallback。
    只用於 scipy 不可用時；以 Simpson 數值積分計算，df 很小時仍可用。
    """
    t_value=float(t_value)
    df=int(df)
    if df <= 0 or not math.isfinite(t_value):
        return None
    if t_value == 0:
        return 0.5
    sign=1.0 if t_value > 0 else -1.0
    x=abs(t_value)
    # t 分布 PDF；x 太大時尾端機率可安全視為極小。
    def pdf(z):
        return (math.gamma((df+1)/2.0) /
                (math.sqrt(df*math.pi)*math.gamma(df/2.0)) *
                (1.0+z*z/df) ** (-(df+1)/2.0))
    if x > 50:
        cdf_pos=1.0
    else:
        n=2000
        h=x/n
        total=pdf(0.0)+pdf(x)
        for i in range(1,n):
            total += (4.0 if i % 2 else 2.0) * pdf(i*h)
        integral=max(0.0, min(0.5, total*h/3.0))
        cdf_pos=0.5+integral
    return cdf_pos if sign > 0 else 1.0-cdf_pos


def _eps_regression_stats(xs, ys, log_response=False):
    """V2.10.75：年度回歸統計量。
    回傳 beta、SE、t、p、95% CI、R²、n、預測值與殘差 RMSE/MAE。
    """
    try:
        x=np.asarray(xs,dtype=float)
        y=np.asarray(ys,dtype=float)
        mask=np.isfinite(x) & np.isfinite(y)
        x=x[mask]; y=y[mask]
        n=len(x)
        if n < 3 or len(np.unique(x)) < 2:
            return None
        if log_response:
            if np.any(y <= 0):
                return None
            yy=np.log(y)
        else:
            yy=y.copy()
        xm=float(np.mean(x)); ym=float(np.mean(yy))
        sxx=float(np.sum((x-xm)**2))
        if sxx <= 0:
            return None
        beta=float(np.sum((x-xm)*(yy-ym))/sxx)
        intercept=float(ym-beta*xm)
        fitted=intercept+beta*x
        residual=yy-fitted
        df=n-2
        sse=float(np.sum(residual**2))
        mse=sse/df
        se=float(math.sqrt(max(mse/sxx,0.0)))
        t_stat=(beta/se) if se > 0 else (math.inf if beta > 0 else -math.inf if beta < 0 else 0.0)
        try:
            from scipy.stats import t as student_t
            p_value=float(2.0*student_t.sf(abs(t_stat),df)) if math.isfinite(t_stat) else 0.0
            tcrit=float(student_t.ppf(0.975,df))
        except Exception:
            cdf=_eps_student_t_cdf(abs(t_stat),df) if math.isfinite(t_stat) else 1.0
            p_value=float(max(0.0,min(1.0,2.0*(1.0-cdf))))
            # fallback 的 95% t critical；EPS 模型最多取 5 年，因此 df 通常 3~。
            tcrit_table={1:12.706,2:4.303,3:3.182,4:2.776,5:2.571,6:2.447,7:2.365,8:2.306,9:2.262,10:2.228,20:2.086,30:2.042}
            tcrit=float(tcrit_table.get(df,1.96 if df>120 else 2.0))
        ci_low=float(beta-tcrit*se)
        ci_high=float(beta+tcrit*se)
        ss_tot=float(np.sum((yy-ym)**2))
        r2=float(1.0-sse/ss_tot) if ss_tot>0 else 0.0
        rmse=float(math.sqrt(max(sse/df,0.0)))
        mae=float(np.mean(np.abs(residual))) if n else None
        return {'beta':beta,'intercept':intercept,'se':se,'t':float(t_stat),'p':p_value,
                'ci_low':ci_low,'ci_high':ci_high,'r2':r2,'n':n,'df':df,
                'residual_rmse':rmse,'residual_mae':mae}
    except Exception:
        return None


def _eps_trend_credibility(stats):
    """V2.10.75：A/B/C 統計可信度，不把 p>0.05 當成沒有趨勢。"""
    if not isinstance(stats,dict):
        return 'C', EPS_MODEL_TREND_C_WEIGHT
    n=int(stats.get('n') or 0)
    beta=float(stats.get('beta') or 0.0)
    p=float(stats.get('p') if stats.get('p') is not None else 1.0)
    r2=float(stats.get('r2') or 0.0)
    ci_low=float(stats.get('ci_low') or 0.0)
    ci_high=float(stats.get('ci_high') or 0.0)
    if n >= 5 and beta > 0 and p < EPS_MODEL_TREND_P_SIGNIFICANT and ci_low > 0 and r2 >= EPS_MODEL_TREND_MIN_R2:
        return 'A', EPS_MODEL_TREND_A_WEIGHT
    if beta > 0 and (p < EPS_MODEL_TREND_P_WEAK or r2 >= EPS_MODEL_TREND_MIN_R2 or ci_low > 0):
        return 'B', EPS_MODEL_TREND_B_WEIGHT
    return 'C', EPS_MODEL_TREND_C_WEIGHT


def _eps_ci_mean(vals):
    """V2.10.75：平均值 95% CI；n 小時使用 t critical。"""
    vals=[float(v) for v in vals if v is not None and math.isfinite(float(v))]
    n=len(vals)
    if n==0:
        return None,None,None
    mean=float(np.mean(vals))
    if n<2:
        return mean,None,None
    sd=float(np.std(vals,ddof=1))
    se=sd/math.sqrt(n)
    try:
        from scipy.stats import t as student_t
        crit=float(student_t.ppf(0.975,n-1))
    except Exception:
        crit={1:12.706,2:4.303,3:3.182,4:2.776,5:2.571,6:2.447,7:2.365,8:2.306,9:2.262,10:2.228}.get(n-1,1.96)
    return mean,sd,(mean-crit*se,mean+crit*se)


def _eps_growth_from_quarterly_model(code, ts, now=None, market=None, industry=None):
    """V2.10.67：完整年度 EPS 模型。

    模型設計：
    1. 已公布季度：直接使用實際 EPS，絕不重新估算。
    2. 已確認的未公布季度 EPS 指引/可靠公開預估：直接採用。
    3. 先用歷史完整年度 EPS 建立「年度成長趨勢」，得到今年基準全年 EPS。
    4. 再用歷史完整年度的 Q1~Q4 / 全年 EPS 比例建立季節係數。
    5. 若今年已有實際季度，使用「已公布季度 / 歷史季節係數預期值」
       對全年基準 EPS 做校正；已公布季度越多，校正資訊越完整。
    6. 未公布季度依季節係數分配校正後的全年 EPS；因此不是逐季各自
       做回歸，不會出現 Q3/Q4 因單獨回歸而被異常壓低的問題。
    7. Q1 尚未公布時，沒有實際季度校正，仍會直接預估完整年度 EPS。
    8. 已確認季度先固定，再將剩餘年度 EPS 依未確認季度季節係數分配。
    9. 重大事件係數只作用於模型預測季度，不會修改實際/確認值。
    10. 去年完整 EPS 一律由去年 Q1~Q4 實際 EPS 加總，不使用 annual EPS、
        Q2 YoY、TTM YoY 或 Yahoo earningsGrowth。

    年度趨勢：
    - 優先使用最近最多 5 個完整年度。
    - 若年度 EPS 全為正值，使用 log(EPS) 年度趨勢 + 最近年度 YoY 中位數
      的融合，降低單一年份異常值影響。
    - 若包含負值/零值，改用年度 EPS 線性趨勢，避免 log 模型失真。
    """
    try:
        qrows=ts.get('eps_quarterly_history') or []
        if not isinstance(qrows,list):
            return None,None

        quarterly={}
        for x in qrows:
            if not isinstance(x,dict):
                continue
            dt=str(x.get('date') or '')
            v=to_float(x.get('eps'))
            if len(dt)<7 or v is None or not math.isfinite(v):
                continue
            try:
                y=int(dt[:4])
                m=int(dt[5:7])
                q=(m-1)//3+1
                if 1<=q<=4:
                    quarterly[(y,q)]=float(v)
            except Exception:
                continue

        if len(quarterly)<EPS_MODEL_MIN_HISTORY_QUARTERS:
            return None,None

        if now is None:
            now=datetime.now(TW_TZ)

        current_year=int(now.year)

        # 只把目前年度已經存在的季度視為實際資料。
        # 未來季度不可能因「歷史資料快取」而被當成當年已公布實際值。
        current_actual={
            q:v for (y,q),v in quarterly.items()
            if y==current_year and q <= ((int(now.month)-1)//3+1)
        }

        # V2.10.79：長期年度 EPS 改由 MOPS Q4 官方財報快取補強。
        # 季度資料主要用於季節性/近期時間序列；年度趨勢可使用最多25年。
        annual_official=_eps_history_engine(code, market, ts=ts, max_years=EPS_MODEL_MAX_YEARS)

        # 找出歷史完整年度；這些年度才能建立季節係數與年度成長趨勢。
        complete_years=[]
        for y in sorted(set(y for y,q in quarterly if y<current_year)):
            vals=[quarterly.get((y,q)) for q in range(1,5)]
            if all(v is not None and math.isfinite(float(v)) for v in vals):
                complete_years.append(y)

        if not complete_years:
            return None,None

        # 以前一年完整年度為基準；優先官方年度 EPS，避免季度資料不足導致年份縮短。
        if annual_official:
            prev_candidates=[y for y in annual_official if y<current_year and annual_official[y]>0]
            if prev_candidates:
                prev_year=max(prev_candidates); prev_annual=float(annual_official[prev_year])
            else:
                prev_year=complete_years[-1]; prev_annual=sum(float(quarterly[(prev_year,q)]) for q in range(1,5))
        else:
            prev_year=complete_years[-1]; prev_annual=sum(float(quarterly[(prev_year,q)]) for q in range(1,5))

        if not math.isfinite(prev_annual) or prev_annual<=0:
            return None,None

        hist_years=sorted([y for y,v in annual_official.items() if y<current_year and math.isfinite(float(v))])[-EPS_MODEL_MAX_YEARS:] if annual_official else complete_years[-EPS_MODEL_MAX_YEARS:]

        # --------------------------------------------------------
        # A. 歷史完整年度 EPS：最多25年官方年度資料。
        # --------------------------------------------------------
        annual_totals=[]
        for y in hist_years:
            if annual_official and y in annual_official:
                total=float(annual_official[y])
            elif (y,1) in quarterly and all(quarterly.get((y,q)) is not None for q in range(1,5)):
                total=sum(float(quarterly[(y,q)]) for q in range(1,5))
            else:
                continue
            if math.isfinite(total): annual_totals.append((int(y),total))

        if len(annual_totals)<EPS_MODEL_MIN_YEARS:
            return None,None

        # --------------------------------------------------------
        # B. 歷史季節係數
        #    每年 Qn / 當年全年 EPS，再取中位數。
        #    只使用全年 EPS > 0 的年份，避免負 EPS 導致比例失真。
        # --------------------------------------------------------
        seasonal_ratios={q:[] for q in range(1,5)}
        for y,total in annual_totals:
            if total<=0:
                continue
            for q in range(1,5):
                qv=quarterly.get((y,q))
                if qv is None or not math.isfinite(float(qv)):
                    continue
                r=float(qv)/float(total)
                if math.isfinite(r) and -0.50 < r < 1.50:
                    seasonal_ratios[q].append(r)

        seasonal_weights={}
        seasonal_stats={}
        for q in range(1,5):
            vals=seasonal_ratios[q]
            mean,sd,ci=_eps_ci_mean(vals)
            # 以 IQR 檢查異常季度；預測主值仍採中位數，避免單一異常年主導。
            clean_vals=list(vals)
            outlier_count=0
            if len(vals)>=4:
                q1,q3=np.percentile(np.asarray(vals,dtype=float),[25,75])
                iqr=float(q3-q1)
                lo=float(q1-1.5*iqr); hi=float(q3+1.5*iqr)
                clean_vals=[v for v in vals if lo<=v<=hi]
                outlier_count=len(vals)-len(clean_vals)
            seasonal_weights[q]=float(np.median(clean_vals)) if clean_vals else (float(np.median(vals)) if vals else None)
            cmean,csd,cci=_eps_ci_mean(clean_vals)
            seasonal_stats[q]={
                'n':len(vals),'mean':mean,'median':float(np.median(vals)) if vals else None,
                'sd':sd,'ci_low':(ci[0] if ci else None),'ci_high':(ci[1] if ci else None),
                'robust_mean':cmean,'robust_sd':csd,
                'robust_ci_low':(cci[0] if cci else None),'robust_ci_high':(cci[1] if cci else None),
                'outliers':outlier_count
            }

        valid_weights=[v for v in seasonal_weights.values()
                       if v is not None and math.isfinite(v)]

        # 正常公司應接近 1；重新標準化避免四捨五入/年度波動造成總和不等於 1。
        if len(valid_weights)==4 and sum(valid_weights)>0:
            sw=sum(valid_weights)
            seasonal_weights={q:seasonal_weights[q]/sw for q in range(1,5)}
        else:
            seasonal_weights={1:0.25,2:0.25,3:0.25,4:0.25}

        # --------------------------------------------------------
        # C. 年度成長趨勢
        # --------------------------------------------------------
        xs=np.array([float(y) for y,_ in annual_totals],dtype=float)
        ys=np.array([float(v) for _,v in annual_totals],dtype=float)

        trend_growth=None
        trend_method=''
        trend_stats=None
        trend_credibility='C'
        trend_weight=EPS_MODEL_TREND_C_WEIGHT

        # 全為正值時，用 log EPS 回歸；統計檢定也在 log(EPS) 空間進行。
        if len(annual_totals)>=EPS_MODEL_MIN_YEARS and np.all(ys>0):
            try:
                stats=_eps_regression_stats(xs,ys,log_response=True)
                if stats:
                    slope=float(stats['beta'])
                    reg_log=float(stats['intercept']+slope*float(current_year))
                    reg_annual=float(math.exp(reg_log))
                    reg_growth=(reg_annual/prev_annual-1.0)*100.0
                    stats['forecast_annual']=reg_annual
                    stats['growth_percent']=reg_growth
                    trend_stats=stats
                    trend_credibility,trend_weight=_eps_trend_credibility(stats)

                    yoy_values=[]
                    for i in range(1,len(annual_totals)):
                        prev=annual_totals[i-1][1]; cur=annual_totals[i][1]
                        if prev>0 and math.isfinite(cur):
                            g=(cur/prev-1.0)*100.0
                            if math.isfinite(g) and -200<g<300:
                                yoy_values.append(g)
                    med_growth=float(np.median(yoy_values[-3:])) if yoy_values else reg_growth
                    # V2.10.79：長期25年 + 近期10年。近期模型反映目前商業結構，
                    # 不讓早期歷史單獨主導下一年度。
                    recent_annual=annual_totals[-min(EPS_MODEL_RECENT_YEARS,len(annual_totals)):]
                    recent_growth=reg_growth
                    if len(recent_annual)>=EPS_MODEL_MIN_YEARS and all(v>0 for _,v in recent_annual):
                        try:
                            rx=np.array([float(y) for y,_ in recent_annual]); ry=np.array([float(v) for _,v in recent_annual])
                            rs=_eps_regression_stats(rx,ry,log_response=True)
                            if rs:
                                rfa=math.exp(float(rs['intercept'])+float(rs['beta'])*float(current_year))
                                recent_growth=(rfa/prev_annual-1)*100.0
                        except Exception: pass
                    # V2.10.96：產業分層 EPS Growth。
                    # 景氣循環產業加入穩健 YoY 中位數，避免低基期反彈直接變成 PEG Growth。
                    eps_ind_model=get_eps_industry_model(industry)
                    robust=[]
                    for i in range(1,len(annual_totals)):
                        p0=annual_totals[i-1][1]; p1=annual_totals[i][1]
                        if p0>0 and p1>0 and math.isfinite(p0) and math.isfinite(p1):
                            gg=(p1/p0-1)*100
                            if math.isfinite(gg) and -100<gg<200: robust.append(gg)
                    if robust:
                        arr=np.asarray(robust,dtype=float)
                        if len(arr)>=5:
                            lo,hi=np.percentile(arr,[15,85]); arr=np.clip(arr,lo,hi)
                        robust_growth=float(np.median(arr))
                    else: robust_growth=float(med_growth)
                    rw=float(eps_ind_model.get('regression_weight',.45)); recent_w=float(eps_ind_model.get('recent_weight',.35)); robust_w=float(eps_ind_model.get('robust_yoy_weight',.20))
                    tw=rw+recent_w+robust_w
                    if tw<=0: rw=.45; recent_w=.35; robust_w=.20; tw=1
                    rw/=tw; recent_w/=tw; robust_w/=tw
                    raw_trend_growth=rw*reg_growth+recent_w*recent_growth+robust_w*robust_growth
                    max_growth=float(eps_ind_model.get('max_normalized_growth',100))
                    trend_growth=float(min(max(raw_trend_growth,-80),max_growth))
                    trend_method=f'{eps_ind_model.get("method","產業分層")}:長期{rw:.0%}+近期{recent_w:.0%}+穩健YoY{robust_w:.0%}（{trend_credibility}級）'
            except Exception:
                trend_growth=None

        # 有負/零 EPS 時，不使用 log，改用年度 EPS 線性趨勢。
        if trend_growth is None:
            try:
                stats=_eps_regression_stats(xs,ys,log_response=False)
                if stats:
                    forecast=float(stats['intercept']+stats['beta']*float(current_year))
                    if math.isfinite(forecast):
                        trend_stats=stats
                        trend_credibility,trend_weight=_eps_trend_credibility(stats)
                        reg_growth=(forecast/prev_annual-1.0)*100.0
                        recent_yoy=[]
                        for i in range(1,len(annual_totals)):
                            p0=annual_totals[i-1][1]; p1=annual_totals[i][1]
                            if math.isfinite(p0) and p0!=0 and math.isfinite(p1):
                                g=(p1/p0-1.0)*100.0
                                if math.isfinite(g) and -200<g<300:
                                    recent_yoy.append(g)
                        med_growth=float(np.median(recent_yoy[-3:])) if recent_yoy else reg_growth
                        # V2.10.96：含負/零 EPS 也使用產業分層，降低低基期爆量。
                        eps_ind_model=get_eps_industry_model(industry)
                        robust_growth=float(np.median(recent_yoy)) if recent_yoy else float(med_growth)
                        rw=float(eps_ind_model.get('regression_weight',.45)); recent_w=float(eps_ind_model.get('recent_weight',.35)); robust_w=float(eps_ind_model.get('robust_yoy_weight',.20))
                        tw=rw+recent_w+robust_w
                        if tw<=0: rw=.45; recent_w=.35; robust_w=.20; tw=1
                        rw/=tw; recent_w/=tw; robust_w/=tw
                        trend_growth=rw*reg_growth+(recent_w+robust_w)*robust_growth
                        max_growth=float(eps_ind_model.get('max_normalized_growth',100))
                        trend_growth=float(min(max(trend_growth,-80),max_growth))
                        trend_stats['forecast_annual']=forecast
                        trend_stats['growth_percent']=reg_growth
                        trend_method=f'{eps_ind_model.get("method","產業分層")}:線性回歸{rw:.0%}+近期/穩健{(recent_w+robust_w):.0%}（{trend_credibility}級）'
            except Exception:
                trend_growth=None

        if trend_growth is None or not math.isfinite(trend_growth):
            return None,None

        # 防止極端歷史週期讓全年模型直接爆掉。
        trend_growth=float(min(max(trend_growth,-80.0),200.0))

        # --------------------------------------------------------
        # V2.10.79：獨立時間序列輔助模型 + Walk-forward。
        # 使用歷史年度 EPS 的 log-level rolling trend；每次只使用當時以前
        # 的資料預測下一年，避免 look-ahead。它與統計回歸模型獨立計算，
        # 最後依歷史 RMSE 自動決定融合權重。
        # --------------------------------------------------------
        ts_forecast_annual=None; ts_growth=None; ts_stats={}; ts_weight=0.50
        try:
            # V2.10.79：真正獨立的季度/年度時間序列輔助模型。
            # 使用 Holt-style damped trend（log EPS），不再與年度 OLS 回歸完全相同。
            def _holt_damped_forecast(train_vals, alpha=0.55, beta=0.25, phi=0.75):
                arr=np.asarray(train_vals,dtype=float)
                if len(arr)<3 or np.any(arr<=0): return None
                z=np.log(arr)
                level=float(z[0]); trend=float(z[1]-z[0])
                for zz in z[1:]:
                    prev_level=level
                    level=alpha*float(zz)+(1-alpha)*(level+trend)
                    trend=beta*(level-prev_level)+(1-beta)*trend
                return float(math.exp(level+phi*trend))
            ts_train=annual_totals[-EPS_MODEL_RECENT_YEARS:]
            if len(ts_train)>=EPS_MODEL_MIN_YEARS and all(v>0 for _,v in ts_train):
                vals=[v for _,v in ts_train]
                ts_forecast_annual=_holt_damped_forecast(vals)
                if ts_forecast_annual is not None and ts_forecast_annual>0:
                    ts_growth=(ts_forecast_annual/prev_annual-1.0)*100.0

                errors=[]; abs_errors=[]; ape=[]
                for i in range(EPS_MODEL_MIN_YEARS,len(annual_totals)):
                    train=annual_totals[max(0,i-EPS_MODEL_RECENT_YEARS):i]
                    actual=annual_totals[i][1]
                    if len(train)<EPS_MODEL_MIN_YEARS or actual==0 or any(v<=0 for _,v in train): continue
                    pred=_holt_damped_forecast([v for _,v in train])
                    if pred is not None and math.isfinite(pred):
                        err=pred-actual; errors.append(err); abs_errors.append(abs(err)); ape.append(abs(err/actual)*100.0)
                if errors:
                    ts_stats={'n':len(errors),'mae':float(np.mean(abs_errors)),
                              'rmse':float(np.sqrt(np.mean(np.square(errors)))),
                              'mape':float(np.mean(ape)),'bias':float(np.mean(errors))}
        except Exception:
            ts_forecast_annual=None; ts_growth=None; ts_stats={}

        # 統計回歸的歷史 one-step backtest，與 TS 使用相同 rolling origin。
        reg_stats=trend_stats.copy() if isinstance(trend_stats,dict) else {}
        reg_errors=[]; reg_abs=[]; reg_ape=[]
        try:
            for i in range(EPS_MODEL_MIN_YEARS,len(annual_totals)):
                train=annual_totals[max(0,i-EPS_MODEL_RECENT_YEARS):i]
                actual=annual_totals[i][1]
                if len(train)<EPS_MODEL_MIN_YEARS or actual==0: continue
                xx=np.array([float(y) for y,_ in train]); yy=np.array([float(v) for _,v in train])
                if np.all(yy>0):
                    st=_eps_regression_stats(xx,yy,log_response=True)
                    if st:
                        pred=float(math.exp(st['intercept']+st['beta']*float(annual_totals[i][0])))
                    else: continue
                else:
                    sl,ic=np.polyfit(xx,yy,1); pred=float(ic+sl*float(annual_totals[i][0]))
                if math.isfinite(pred):
                    er=pred-actual; reg_errors.append(er); reg_abs.append(abs(er)); reg_ape.append(abs(er/actual)*100.0)
        except Exception: pass
        if reg_errors:
            reg_stats.update({'backtest_n':len(reg_errors),'backtest_mae':float(np.mean(reg_abs)),
                              'backtest_rmse':float(np.sqrt(np.mean(np.square(reg_errors)))),
                              'backtest_mape':float(np.mean(reg_ape)),'backtest_bias':float(np.mean(reg_errors))})

        if ts_forecast_annual is not None and ts_forecast_annual>0:
            rr=to_float(reg_stats.get('backtest_rmse')); tr=to_float(ts_stats.get('rmse'))
            if rr is not None and tr is not None and rr>=0 and tr>=0:
                invr=1.0/(rr+1e-9); invt=1.0/(tr+1e-9); totalw=invr+invt
                reg_model_w=invr/totalw; ts_weight=invt/totalw
                # 防止樣本少時權重極端化。
                reg_model_w=min(.75,max(.25,reg_model_w)); ts_weight=1.0-reg_model_w
            else:
                reg_model_w=0.50; ts_weight=0.50
            # V2.10.96：產業模型先正規化，再與 Walk-forward TS 融合。
            # 舊版直接融合 raw forecast，會讓 2303 低基期反彈把 Growth 推到 100%+。
            eps_ind_model=get_eps_industry_model(industry)
            max_growth=float(eps_ind_model.get('max_normalized_growth',100.0))
            reg_growth_for_fusion=float(min(max(trend_growth,-80.0),max_growth))
            ts_growth_for_fusion=float(min(max(ts_growth if ts_growth is not None else reg_growth_for_fusion,-80.0),max_growth))
            reg_annual_for_fusion=prev_annual*(1.0+reg_growth_for_fusion/100.0)
            ts_annual_for_fusion=prev_annual*(1.0+ts_growth_for_fusion/100.0)
            fused_annual=reg_model_w*reg_annual_for_fusion + ts_weight*ts_annual_for_fusion
            if math.isfinite(fused_annual) and fused_annual>0:
                base_annual=float(fused_annual)
                fused_growth=(base_annual/prev_annual-1.0)*100.0
                trend_growth=float(min(max(fused_growth,-80.0),max_growth))
        else:
            reg_model_w=1.0; ts_weight=0.0

        base_annual=prev_annual*(1.0+trend_growth/100.0)
        # 若上方已有融合 forecast，保留融合值。
        if ts_forecast_annual is not None and 'fused_annual' in locals() and math.isfinite(fused_annual) and fused_annual>0:
            base_annual=float(fused_annual)

        if not math.isfinite(base_annual) or base_annual<=0:
            return None,None

        # --------------------------------------------------------
        # D. 已公布季度校正：只調整「未來季度基準」，不直接乘整個全年
        #
        # V2.10.67 修正：
        # 舊版把「實際已公布季度 / 季節係數預期值」直接作用在全年，
        # 容易讓 Q1/Q2 的單季超預期被放大成全年 EPS 大幅跳升。
        # 新版改為：先建立每個未公布季度的基準 EPS，再用已公布季度
        # 對「剩餘季度」做阻尼校正。已公布季度本身永遠保持實際值。
        #
        # 例：今年 Q1/Q2 已公布，Q3/Q4 未公布：
        #   1. 年度趨勢 -> base_annual
        #   2. 歷史季節係數 -> Q3/Q4 baseline
        #   3. 實際 H1 / 歷史 H1 預期 -> actual_ratio
        #   4. 只用阻尼後的 ratio 調整 Q3/Q4 baseline
        #   5. 實際 H1 + 預估 Q3/Q4 = 全年 EPS
        # --------------------------------------------------------
        actual_sum=sum(float(v) for v in current_actual.values())
        actual_weight=sum(
            seasonal_weights.get(q,0.25)
            for q in current_actual
            if q in seasonal_weights
        )

        correction_factor=1.0
        actual_correction_ratio=1.0
        correction_alpha=0.0
        correction_history=[]
        correction_mean=None
        correction_sd=None
        correction_ci_low=None
        correction_ci_high=None
        correction_status='無已公布季度校正'
        if current_actual and actual_weight>0:
            expected_actual=base_annual*actual_weight
            if expected_actual>0 and math.isfinite(expected_actual):
                actual_correction_ratio=actual_sum/expected_actual
                if math.isfinite(actual_correction_ratio):
                    actual_qs=sorted(current_actual)
                    for hy,total in annual_totals:
                        hweight=sum(seasonal_weights.get(q,0.25) for q in actual_qs)
                        hactual=sum(float(quarterly.get((hy,q))) for q in actual_qs if quarterly.get((hy,q)) is not None)
                        if hweight>0 and total>0 and len([q for q in actual_qs if quarterly.get((hy,q)) is not None])==len(actual_qs):
                            hr=(hactual/total)/hweight
                            if math.isfinite(hr) and 0.70<=hr<=1.30:
                                correction_history.append(float(hr))
                    correction_mean,correction_sd,correction_ci=_eps_ci_mean(correction_history)
                    correction_ci_low=correction_ci[0] if correction_ci else None
                    correction_ci_high=correction_ci[1] if correction_ci else None
                    raw=float(min(max(actual_correction_ratio,0.70),1.30))
                    hist_center=correction_mean if correction_mean is not None else 1.0
                    hist_center=float(min(max(hist_center,EPS_MODEL_CORRECTION_MIN),EPS_MODEL_CORRECTION_MAX))
                    if correction_history:
                        n_hist=len(correction_history)
                        correction_alpha=min(EPS_MODEL_CORRECTION_ALPHA_MAX, 0.35 + 0.08*len(current_actual) + 0.02*max(0,n_hist-3))
                        if correction_sd is not None and correction_mean and correction_mean!=0:
                            cv=abs(correction_sd/correction_mean)
                            if cv>0.12:
                                correction_alpha*=0.80
                        correction_factor=hist_center + correction_alpha*(raw-hist_center)
                        if correction_ci_low is not None and correction_ci_high is not None:
                            correction_status=('高於歷史正常區間' if raw>correction_ci_high else '低於歷史正常區間' if raw<correction_ci_low else '歷史正常區間內')
                    else:
                        correction_alpha=min(0.45,0.25+0.10*len(current_actual))
                        correction_factor=1.0+correction_alpha*(raw-1.0)
                        correction_status='歷史校正樣本不足，採保守收縮'
        if not math.isfinite(correction_factor):
            correction_factor=1.0
        correction_factor=float(min(max(correction_factor,0.85),1.15))

        # --------------------------------------------------------
        # E. 先放入實際季度與確認季度
        # --------------------------------------------------------
        projected=dict(current_actual)
        confirmed_used=[]

        for q in range(1,5):
            if q in projected:
                continue
            v=_eps_growth_confirmed_quarter(code,current_year,q)
            if v is not None and math.isfinite(float(v)):
                projected[q]=float(v)
                confirmed_used.append(q)

        # --------------------------------------------------------
        # F. 預測未公布季度
        #
        # 優先：年度趨勢 × 歷史季節係數 × 已公布季度阻尼校正。
        # 若資料結構特殊或預測值無效，再退回逐季度歷史回歸/中位數。
        # 重大事件只作用於真正的模型預測季度。
        # --------------------------------------------------------
        remaining=[q for q in range(1,5) if q not in projected]
        seasonal_used=[]
        regression_used=[]
        median_used=[]
        event_adjusted=[]

        if remaining:
            seasonal_ok=True
            for q in remaining:
                w=seasonal_weights.get(q,0.25)
                pred=base_annual*float(w)*correction_factor
                if not math.isfinite(pred) or pred<=0:
                    seasonal_ok=False
                    break
                factor=_eps_growth_event_factor(code,current_year,q)
                if factor!=1.0:
                    pred*=factor
                    event_adjusted.append(q)
                if not math.isfinite(pred) or pred<=0:
                    seasonal_ok=False
                    break
                projected[q]=float(pred)
                seasonal_used.append(q)

            if not seasonal_ok:
                # 清除本段剛建立的預測，改走逐季度歷史回歸。
                for q in remaining:
                    projected.pop(q,None)
                seasonal_used=[]

                for q in remaining:
                    pts=[]
                    for y in hist_years:
                        v=quarterly.get((y,q))
                        if v is not None and math.isfinite(float(v)):
                            pts.append((float(y),float(v)))

                    pred=None
                    if len(pts)>=EPS_MODEL_MIN_YEARS:
                        try:
                            x=np.array([a for a,_ in pts],dtype=float)
                            y=np.array([b for _,b in pts],dtype=float)
                            slope,intercept=np.polyfit(x,y,1)
                            reg=float(intercept+slope*float(current_year))
                            recent=[v for _,v in pts[-min(3,len(pts)):]]
                            med=float(np.median(recent)) if recent else None
                            if math.isfinite(reg) and med is not None:
                                pred=0.70*reg+0.30*med
                            elif math.isfinite(reg):
                                pred=reg
                            if pred is not None:
                                regression_used.append(q)
                        except Exception:
                            pred=None

                    if pred is None and pts:
                        pred=float(np.median([v for _,v in pts]))
                        median_used.append(q)

                    if pred is None or not math.isfinite(pred):
                        return None,None

                    factor=_eps_growth_event_factor(code,current_year,q)
                    if factor!=1.0:
                        pred*=factor
                        event_adjusted.append(q)
                    projected[q]=float(pred)

        # --------------------------------------------------------
        # G. 重大事件調整
        #    只調整模型預測，不調整實際/確認值。
        # --------------------------------------------------------
        for q in range(1,5):
            if q in current_actual or q in confirmed_used:
                continue
            if q not in projected:
                continue
            factor=_eps_growth_event_factor(code,current_year,q)
            if factor!=1.0:
                # seasonal path 尚未套事件時在這裡套用。
                # fallback path 已於上方套用，因此避免重複。
                if q not in event_adjusted:
                    projected[q]=float(projected[q]*factor)
                    event_adjusted.append(q)

        if len(projected)!=4:
            return None,None

        current_annual=sum(float(projected[q]) for q in range(1,5))
        # V2.10.67：修正 V2.10.65 的 NameError；校正全年指的是套用
        # 已公布季度阻尼校正後、尚未加入實際季度替換結果前的年度基準。
        # 實際模型結果仍以 current_annual（實際/確認/預測四季合計）為準。
        corrected_annual=base_annual*correction_factor
        if not math.isfinite(current_annual):
            return None,None

        growth=(current_annual/prev_annual-1.0)*100.0
        # V2.10.75：保守/基準/樂觀三情境；已公布季度固定，只對剩餘季度加減不確定性。
        trend_uncertainty=0.0
        if trend_stats:
            ci_growth_low=None; ci_growth_high=None
            if np.all(ys>0) and trend_stats.get('forecast_annual') and trend_stats.get('intercept') is not None:
                lo=math.exp(float(trend_stats['intercept'])+float(trend_stats['ci_low'])*current_year)
                hi=math.exp(float(trend_stats['intercept'])+float(trend_stats['ci_high'])*current_year)
                ci_growth_low=(lo/prev_annual-1.0)*100.0
                ci_growth_high=(hi/prev_annual-1.0)*100.0
            if ci_growth_low is None:
                ci_growth_low=trend_growth-abs(trend_growth)*0.20-5.0
                ci_growth_high=trend_growth+abs(trend_growth)*0.20+5.0
            trend_uncertainty=max(abs(float(trend_growth)-float(ci_growth_low)),abs(float(ci_growth_high)-float(trend_growth)))
        seasonal_cv=[]
        for q in range(1,5):
            st=seasonal_stats.get(q,{})
            if isinstance(st,dict) and st.get('sd') is not None and st.get('mean') not in (None,0):
                seasonal_cv.append(abs(float(st['sd'])/float(st['mean'])))
        season_uncertainty=float(np.mean(seasonal_cv)) if seasonal_cv else 0.05
        scenario_pct=max(0.06,min(0.25,trend_uncertainty/100.0 + season_uncertainty*0.50))
        forecast_sum=sum(float(projected[q]) for q in range(1,5) if q not in current_actual and q not in confirmed_used)
        fixed_sum=current_annual-forecast_sum
        conservative_annual=max(0.0,fixed_sum+forecast_sum*(1.0-scenario_pct))
        optimistic_annual=max(0.0,fixed_sum+forecast_sum*(1.0+scenario_pct))
        if (not math.isfinite(growth)
                or growth < -EPS_MODEL_MAX_ABS_GROWTH
                or growth > EPS_MODEL_MAX_ABS_GROWTH):
            return None,None

        # V2.10.98：估值用 EPS 正規化成長率。
        # 不再使用「25年CAGR 40% + 10年CAGR 60%」的固定公式。
        # 原因：CAGR 對起點/終點高度敏感；景氣循環股可能因低基期或高基期產生
        # 完全不代表正常獲利能力的 PEG growth。改用：
        #   1) 穩健 YoY 中位數（winsorize 15/85）
        #   2) 5 年 CAGR
        #   3) 10 年 CAGR
        # 再依「次產業模型」決定權重。
        valuation_growth=None
        valuation_method='N/A'
        valuation_model_level='大產業'
        try:
            model=get_eps_valuation_model(industry, subindustry)
            valuation_model_level=model.get('source_level','大產業')
            positive_hist=[(int(y),float(v)) for y,v in annual_totals
                           if math.isfinite(float(v)) and float(v)>0]
            if len(positive_hist)>=3:
                yoy=[]
                for i in range(1,len(positive_hist)):
                    p0=positive_hist[i-1][1]; p1=positive_hist[i][1]
                    if p0>0 and p1>0:
                        gg=(p1/p0-1.0)*100.0
                        if math.isfinite(gg) and -100.0<gg<200.0:
                            yoy.append(gg)
                if yoy:
                    arr=np.asarray(yoy,dtype=float)
                    if len(arr)>=5:
                        lo,hi=np.percentile(arr,[15,85])
                        arr=np.clip(arr,lo,hi)
                    robust_yoy=float(np.median(arr))
                else:
                    robust_yoy=None

                def cagr_for_years(nyears):
                    if len(positive_hist)<nyears+1: return None
                    h=positive_hist[-(nyears+1):]
                    y0,v0=h[0]; y1,v1=h[-1]
                    yrs=max(1,int(y1-y0))
                    if v0<=0 or v1<=0 or yrs<=0: return None
                    z=((v1/v0)**(1.0/yrs)-1.0)*100.0
                    return z if math.isfinite(z) else None

                cagr5=cagr_for_years(5)
                cagr10=cagr_for_years(10)
                components=[]; weights=[]
                if robust_yoy is not None:
                    components.append(robust_yoy); weights.append(float(model.get('median_w',.50)))
                if cagr5 is not None:
                    components.append(cagr5); weights.append(float(model.get('cagr5_w',.25)))
                if cagr10 is not None:
                    components.append(cagr10); weights.append(float(model.get('cagr10_w',.25)))

                if components and sum(weights)>0:
                    # 統計可信度收縮：長期回歸若 R² 很低，不讓長期趨勢主導估值。
                    stats=trend_stats if isinstance(trend_stats,dict) else {}
                    r2=to_float(stats.get('r2'))
                    pval=to_float(stats.get('p'))
                    if r2 is not None and r2 < 0.35:
                        # 對循環股只保留部分長期 CAGR 權重，將權重轉回 robust YoY。
                        if len(components)>=2:
                            for i in range(len(weights)):
                                # cagr5/cagr10 的權重各最多保留70%；剩餘權重回到 median
                                pass
                        # 以「穩健 YoY」作為主錨點；若沒有 robust YoY 則不做收縮。
                        if robust_yoy is not None:
                            adj=[]
                            for val,w in zip(components,weights):
                                adj.append([val,w])
                            # 依 component 對應的原始權重降低 CAGR 權重
                            for j,val in enumerate(components):
                                if abs(val-robust_yoy)<1e-12 and robust_yoy is not None:
                                    continue
                                adj[j][1]*=0.60
                            lost=sum(weights)-sum(x[1] for x in adj)
                            # 找 median component 補回 lost
                            for j,val in enumerate(components):
                                if abs(val-robust_yoy)<1e-12:
                                    adj[j][1]+=lost; break
                            weights=[x[1] for x in adj]

                    tw=sum(weights)
                    valuation_growth=sum(v*w for v,w in zip(components,weights))/tw
                    cap=float(model.get('cap',25.0))
                    # PEG 不接受負成長；負值仍保留 N/A，避免把衰退公司算成「便宜」。
                    valuation_growth=float(min(max(valuation_growth,-5.0),cap))
                    valuation_method=(
                        f"{model.get('name','一般型')}｜穩健YoY{float(model.get('median_w',.5)):.0%}"
                        f"＋5年CAGR{float(model.get('cagr5_w',.25)):.0%}＋10年CAGR{float(model.get('cagr10_w',.25)):.0%}"
                    )
        except Exception as e:
            print(f'V2.10.98 估值正規化成長率計算失敗 {code}: {type(e).__name__}: {e}',flush=True)
            valuation_growth=None

        detail={
            'current_year':current_year,
            'prev_year':prev_year,
            'prev_annual':prev_annual,
            'projected_quarters':{q:float(projected[q]) for q in range(1,5)},
            'actual_quarters':sorted(current_actual),
            'forecast_quarters':[q for q in range(1,5) if q not in current_actual],
            'confirmed_quarters':sorted(confirmed_used),
            'regression_quarters':sorted(set(regression_used)),
            'median_quarters':sorted(set(median_used)),
            'seasonal_quarters':sorted(set(seasonal_used)),
            'current_annual':current_annual,
            'growth':float(growth),
            'valuation_growth':float(valuation_growth) if valuation_growth is not None else None,
            'valuation_growth_method':valuation_method,
            'valuation_model_level':valuation_model_level,
            'event_adjusted_quarters':sorted(set(event_adjusted)),
            'annual_trend_growth':float(trend_growth),
            'raw_statistical_growth':float(raw_trend_growth) if 'raw_trend_growth' in locals() else float(trend_growth),
            'annual_trend_method':trend_method,
            'eps_industry':canonical_industry(industry),
            'eps_industry_model':get_eps_industry_model(industry).get('name','一般型'),
            'eps_industry_model_method':get_eps_industry_model(industry).get('method','產業分層'),
            'long_history_years':len(annual_totals),
            'history_source': (load_json(EPS_HISTORY_ENGINE_CACHE_FILE).get(code,{}).get('meta',{}).get('source','cache') if isinstance(load_json(EPS_HISTORY_ENGINE_CACHE_FILE),dict) else 'cache'),
            'history_start_year':annual_totals[0][0] if annual_totals else None,
            'history_end_year':annual_totals[-1][0] if annual_totals else None,
            'recent_history_years':min(EPS_MODEL_RECENT_YEARS,len(annual_totals)),
            'recent_trend_growth':float(recent_growth) if 'recent_growth' in locals() and recent_growth is not None else None,
            'base_annual':float(base_annual),
            'time_series_forecast_annual':float(ts_forecast_annual) if ts_forecast_annual is not None else None,
            'time_series_growth':float(ts_growth) if ts_growth is not None else None,
            'time_series_stats':ts_stats,
            'regression_backtest':reg_stats,
            'model_weight_regression':float(reg_model_w),
            'model_weight_time_series':float(ts_weight),
            'correction_factor':float(correction_factor),
            'corrected_annual':float(corrected_annual),
            'seasonal_weights':{q:float(seasonal_weights[q]) for q in range(1,5)},
            'seasonal_stats':seasonal_stats,
            'annual_trend_stats':trend_stats,
            'trend_credibility':trend_credibility,
            'trend_weight':float(trend_weight),
            'correction_history':correction_history,
            'correction_mean':correction_mean,
            'correction_sd':correction_sd,
            'correction_ci_low':correction_ci_low,
            'correction_ci_high':correction_ci_high,
            'correction_status':correction_status,
            'scenario_pct':float(scenario_pct),
            'conservative_annual':float(conservative_annual),
            'optimistic_annual':float(optimistic_annual),
            'model_version':'V2.10.96 產業分層EPS模型＋Yahoo/Goodinfo多源25年EPS歷史引擎＋法人起始年約束＋長期25年/近期10年統計模型＋Walk-forward時間序列'
        }
        return float(growth),detail

    except Exception as e:
        print(f'V2.10.96 產業分層EPS年度模型失敗 {code}: {type(e).__name__}: {e}',flush=True)
        return None,None

def _format_eps_model_summary(detail):
    """V2.10.96：精簡 EPS 統計驗證輸出，將統計量、估值成長率與預測分區顯示。"""
    if not isinstance(detail, dict):
        return ''
    st = detail.get('annual_trend_stats') or {}
    if not isinstance(st, dict):
        st = {}

    def nf(v, digits=2):
        x = to_float(v)
        return f'{x:.{digits}f}' if x is not None and math.isfinite(x) else 'N/A'

    p = to_float(st.get('p'))
    r2 = to_float(st.get('r2'))
    n = st.get('n') or 'N/A'
    ptxt = f'{p:.3f}' if p is not None and math.isfinite(p) else 'N/A'
    rtxt = f'{r2:.2f}' if r2 is not None and math.isfinite(r2) else 'N/A'

    annual_growth = to_float(detail.get('annual_trend_growth'))
    recent_growth = to_float(detail.get('recent_trend_growth'))
    if recent_growth is None:
        recent_growth = annual_growth

    valuation_growth = to_float(detail.get('valuation_growth'))
    eps_growth = to_float(detail.get('growth'))

    lines = []
    lines.append('【EPS統計驗證】')
    lines.append(f'模型：{detail.get("eps_industry_model","一般型")}｜{detail.get("eps_industry_model_method","產業分層") }')
    lines.append(f'長期趨勢：{nf(annual_growth)}%｜近期10年：{nf(recent_growth)}%｜{detail.get("trend_credibility","C")}級｜p={ptxt}｜R²={rtxt}｜n={n}')

    beta = to_float(st.get('beta')); se = to_float(st.get('se')); t = to_float(st.get('t'))
    ci_low = to_float(st.get('ci_low')); ci_high = to_float(st.get('ci_high'))
    if beta is not None or se is not None or t is not None:
        lines.append(f'β={nf(beta,3)}｜SE={nf(se,3)}｜t={nf(t,2)}｜95% CI={nf(ci_low,3)}～{nf(ci_high,3)}')

    sw = detail.get('seasonal_weights') or {}
    if isinstance(sw, dict) and sw:
        qparts = []
        for q in range(1,5):
            w = to_float(sw.get(q))
            if w is not None:
                qparts.append(f'Q{q} {w*100:.1f}%')
        if qparts:
            lines.append('季節分布：' + '｜'.join(qparts))

    correction = to_float(detail.get('correction_factor'))
    correction_status = detail.get('correction_status','')
    correction_n = len(detail.get('correction_history') or [])
    if correction is not None:
        lines.append(f'已公布校正：{correction:.3f}｜樣本 {correction_n}｜{correction_status}')

    rw = to_float(detail.get('model_weight_regression'))
    tw = to_float(detail.get('model_weight_time_series'))
    rb = detail.get('regression_backtest') or {}
    tb = detail.get('time_series_stats') or {}
    if rw is not None or tw is not None:
        fusion = f'回歸 {rw:.0%}' if rw is not None else '回歸 N/A'
        fusion += f'＋時間序列 {tw:.0%}' if tw is not None else '＋時間序列 N/A'
        rmse_r = to_float(rb.get('backtest_rmse'))
        rmse_t = to_float(tb.get('rmse'))
        if rmse_r is not None and rmse_t is not None:
            fusion += f'｜RMSE {rmse_r:.2f}/{rmse_t:.2f}'
        lines.append(f'模型融合：{fusion}')

    # V2.10.98：明確區分「觀察到的 EPS 成長」與「估值使用的 EPS 正規化成長率」。
    if eps_growth is not None or valuation_growth is not None:
        lines.append(f'EPS成長率：{nf(eps_growth)}%｜估值用EPS正規化成長率：{nf(valuation_growth)}%')

    conservative = to_float(detail.get('conservative_annual'))
    base = to_float(detail.get('current_annual'))
    optimistic = to_float(detail.get('optimistic_annual'))
    if base is not None:
        lines.append(f'全年EPS：保守 {nf(conservative)}｜基準 {nf(base)}｜樂觀 {nf(optimistic)}')

    return '\n'.join(lines) + '\n'

def official_fundamental(symbol, official=None, current_price=None, market=None, industry=None, subindustry=None):
    """V2.10.67：股票基本面/估值資料層。

    EPS Growth 唯一主模型：已公布季度實際 EPS -> 已確認季度 EPS -> 未公布季度回歸/季節性預估 -> 全年 EPS -> 去年完整年度 EPS -> YoY。
    不再採用 Q2 YoY、TTM YoY、Yahoo earningsGrowth 或 annual EPS 作為 Growth 主來源。
    其他 PE/PB/Yield/ROE/PEG 口徑與 V2.10.67 保持。
    """
    code=clean_code(str(symbol).split('.')[0])
    off=official if isinstance(official,dict) else {}
    out={'pe':to_float(off.get('pe')),'pb':to_float(off.get('pb')),'yield':to_float(off.get('yield')),
         'eps_growth':None,'roe':None,'peg':None,'eps_model_detail':None}
    for k in ('pe','pb','yield'):
        if not _fund_cache_valid_value(k,out.get(k)): out[k]=None
    if market=='TPEX' and any(out.get(k) is None for k in ('pe','pb','yield')):
        try:
            one=parse_tpex_web_peratio(tpex_web_peratio_data(timeout=8)).get(code) or {}
            for k in ('pe','pb','yield'):
                if out.get(k) is None:
                    v=to_float(one.get(k))
                    if v is not None and _fund_cache_valid_value(k,v): out[k]=v
        except Exception as e:
            print(f'V2.10.62 TPEx 官方估值補洞失敗 {code}: {type(e).__name__}',flush=True)
    symbol_full=symbol_for(code,market) if market in ('TWSE','TPEX') else symbol
    try: qs=yahoo_quote_summary_fund(symbol_full) or {}
    except Exception as e:
        qs={}; print(f'V2.10.62 Yahoo quoteSummary失敗 {code}: {type(e).__name__}',flush=True)
    try: ts=yahoo_timeseries_fund(symbol_full) or {}
    except Exception as e:
        ts={}; print(f'V2.10.67 Yahoo timeseries失敗 {code}: {type(e).__name__}',flush=True)

    # V2.10.67：只在官方基本面欄位為 N/A 時，使用既有 Yahoo
    # fundamentals-timeseries 的 PB / 股利資料補洞；已有正常值絕不覆蓋。
    if out['pb'] is None:
        ts_pb=to_float(ts.get('pb'))
        if ts_pb is not None and 0 < ts_pb <= 100:
            out['pb']=ts_pb
            print(f'V2.10.67 PB fallback：{code} = Yahoo timeseries PB {ts_pb:.2f}',flush=True)
    if out['yield'] is None:
        ts_div=to_float(ts.get('dividend_rate'))
        px_candidates=[current_price,qs.get('price'),ts.get('price')]
        px=next((to_float(v) for v in px_candidates if to_float(v) is not None and to_float(v)>0),None)
        if px is not None and ts_div is not None and ts_div >= 0:
            ts_yield=ts_div/px*100
            if math.isfinite(ts_yield) and 0 <= ts_yield <= 30:
                out['yield']=ts_yield
                print(f'V2.10.67 Yield fallback：{code} = 股利 {ts_div:.4f} / 股價 {px:.2f} = {ts_yield:.2f}%',flush=True)

    # 既有 cache 僅作極端差異保護，不作 Growth 來源。
    cached_g=None
    try:
        fc0=load_json(LINE_FUND_CACHE_FILE); ci0=fc0.get(code,{}) if isinstance(fc0,dict) else {}
        cached_g=to_float(ci0.get('eps_growth')) if isinstance(ci0,dict) else None
    except Exception: pass

    model_g,detail=_eps_growth_from_quarterly_model(code,ts,now=datetime.now(TW_TZ),market=market,industry=industry)
    model_g=_eps_growth_sanity(model_g,'V2.10.96 產業分層25年歷史 EPS',True,cached_g)
    if model_g is not None:
        out['eps_growth']=model_g
        out['eps_model_detail']=detail
        if detail:
            qtext=' / '.join([f'Q{q}={detail["projected_quarters"][q]:.4f}' for q in range(1,5)])
            source_bits=[]
            if detail.get('actual_quarters'): source_bits.append('實際Q'+','.join(map(str,detail['actual_quarters'])))
            if detail.get('confirmed_quarters'): source_bits.append('確認Q'+','.join(map(str,detail['confirmed_quarters'])))
            if detail.get('regression_quarters'): source_bits.append('回歸Q'+','.join(map(str,detail['regression_quarters'])))
            if detail.get('median_quarters'): source_bits.append('中位數Q'+','.join(map(str,detail['median_quarters'])))
            if detail.get('seasonal_quarters'): source_bits.append('季節Q'+','.join(map(str,detail['seasonal_quarters'])))
            if detail.get('event_adjusted_quarters'): source_bits.append('事件調整Q'+','.join(map(str,detail['event_adjusted_quarters'])))
            src='；'.join(source_bits) if source_bits else '無'
            print(f'V2.10.98 EPS Growth：{code}={model_g:.2f}% [統計驗證EPS模型] {detail["current_year"]}全年={detail["current_annual"]:.4f} | 保守={detail["conservative_annual"]:.4f} 基準={detail["current_annual"]:.4f} 樂觀={detail["optimistic_annual"]:.4f} | {qtext} | 年度趨勢={detail["annual_trend_growth"]:.2f}% {detail["trend_credibility"]}級 p={detail.get("annual_trend_stats",{}).get("p") if detail.get("annual_trend_stats") else "N/A"} R²={detail.get("annual_trend_stats",{}).get("r2") if detail.get("annual_trend_stats") else "N/A"} | 模型融合=回歸{detail.get('model_weight_regression',0):.0%}/時間序列{detail.get('model_weight_time_series',0):.0%} | 基準全年={detail["base_annual"]:.4f} | 已公布校正={detail["correction_factor"]:.4f}（{detail["correction_status"]}） | 來源：{src}',flush=True)
    else:
        # V2.10.67：只有完整季度年度模型真的無法建立時，才啟動舊有的
        # EPS fallback。正常由季度模型成功取得的結果完全不覆蓋。
        fallback_g=None
        try:
            ticker=yf.Ticker(symbol_full)
            fallback_g=_eps_growth_from_yfinance_statements(ticker)
        except Exception as e_fb:
            print(f'V2.10.67 EPS Growth 財報 fallback失敗 {code}: {type(e_fb).__name__}: {e_fb}',flush=True)
        if fallback_g is None:
            try:
                fallback_g=mops_eps_growth_fallback(
                    code,
                    'TWSE' if market=='TWSE' else 'TPEX'
                )
            except Exception as e_mops:
                print(f'V2.10.67 MOPS EPS Growth fallback失敗 {code}: {type(e_mops).__name__}: {e_mops}',flush=True)
        fallback_g=_eps_growth_sanity(
            fallback_g,
            'V2.10.67 fallback EPS growth',
            True,
            cached_g
        )
        if fallback_g is not None:
            out['eps_growth']=fallback_g
            print(
                f'V2.10.67 EPS Growth：{code}={fallback_g:.2f}% [fallback：年度/季度財報資料]',
                flush=True
            )
        else:
            print(f'V2.10.67 EPS Growth：{code}=N/A [季度模型及fallback均無有效資料]',flush=True)

    r_candidates=[qs.get('roe'),ts.get('roe')]
    out['roe']=next((float(v) for v in r_candidates if to_float(v) is not None and -100<=to_float(v)<=100),None)

    # V2.10.67：Yahoo quoteSummary 401 時，恢復舊版已有的 yfinance/profile 補洞。
    # 只補原本就有資料的 PE/PB/殖利率/ROE；PEG 刻意不在這裡補。
    try:
        info=(yf.Ticker(symbol_full).info or {})
        if out['pb'] is None:
            pbv=to_float(info.get('priceToBook'))
            if pbv is not None and 0 < pbv <= 100:
                out['pb']=pbv
        if out['yield'] is None:
            yv=to_float(info.get('dividendYield'))
            if yv is not None:
                yv=yv*100 if abs(yv)<=1.5 else yv
                if 0 <= yv <= 30: out['yield']=yv
        if out['roe'] is None:
            rv=to_float(info.get('returnOnEquity'))
            if rv is not None:
                rv=rv*100 if abs(rv)<2 else rv
                if -100 <= rv <= 100: out['roe']=rv
        if out['pe'] is None:
            pv=to_float(info.get('trailingPE')) or to_float(info.get('forwardPE'))
            if pv is not None and 0 < pv <= PE_MAX_VALID:
                out['pe']=pv
    except Exception as e:
        print(f'V2.10.67 yfinance基本面補洞失敗 {code}: {type(e).__name__}',flush=True)

    # Yahoo Profile 是 quoteSummary 失敗時的第二層輕量備援。
    if market in ('TWSE','TPEX') and (out['pb'] is None or out['yield'] is None or out['roe'] is None):
        try:
            prof=yahoo_tw_profile_fallback(symbol_full)
            px=next((to_float(v) for v in (current_price,prof.get('price'),qs.get('price'),ts.get('price')) if to_float(v) is not None and to_float(v)>0),None)
            if out['pb'] is None:
                bvps=to_float(prof.get('bvps'))
                if px is not None and bvps is not None and bvps>0:
                    calc_pb=px/bvps
                    if math.isfinite(calc_pb) and 0 < calc_pb <= 100: out['pb']=calc_pb
            if out['yield'] is None:
                div=to_float(prof.get('dividend_rate'))
                if px is not None and div is not None and div>=0:
                    calc_y=div/px*100
                    if math.isfinite(calc_y) and 0 <= calc_y <= 30: out['yield']=calc_y
            if out['roe'] is None:
                rv=to_float(prof.get('roe'))
                if rv is not None and -100 <= rv <= 100: out['roe']=rv
        except Exception as e:
            print(f'V2.10.67 Yahoo Profile基本面補洞失敗 {code}: {type(e).__name__}',flush=True)

    if out['pe'] is None:
        eps_candidates=[qs.get('trailing_eps'),ts.get('trailing_eps')]
        eps=next((to_float(v) for v in eps_candidates if to_float(v) is not None and to_float(v)>0),None)
        price_candidates=[current_price,qs.get('price'),ts.get('price')]
        px=next((to_float(v) for v in price_candidates if to_float(v) is not None and to_float(v)>0),None)
        if px is not None and eps is not None:
            calc_pe=px/eps
            if math.isfinite(calc_pe) and 0<calc_pe<=PE_MAX_VALID:
                out['pe']=float(calc_pe); print(f'V2.10.56 PE fallback：{code} = 股價 {px:.2f} / TTM EPS {eps:.4f} = {calc_pe:.2f}',flush=True)
    # V2.10.96：PEG 不再直接使用「當年度低基期反彈」的預測成長率。
    # 估值用途改採 25 年/近期 10 年 EPS CAGR 的產業化正規化成長率；
    # 原始模型 EPS Growth 仍保留給預測與報表，避免 PEG 被景氣循環扭曲。
    pe=to_float(out.get('pe')); g=to_float(out.get('eps_growth'))
    valuation_growth=to_float((out.get('eps_model_detail') or {}).get('valuation_growth'))
    if valuation_growth is None and g is not None:
        ind_model=get_eps_valuation_model(industry, subindustry)
        cap=float(ind_model.get('cap',25.0))
        valuation_growth=min(max(g,-5.0),cap)
    out['valuation_growth']=valuation_growth
    if pe is not None and pe>0 and valuation_growth is not None and 0<valuation_growth<=100:
        peg=pe/valuation_growth
        if math.isfinite(peg) and 0<peg<100: out['peg']=peg
    print(f'V2.10.98 基本面 {code}: PE={fmt(out["pe"])} PB={fmt(out["pb"])} Yield={fmt(out["yield"])} EPSGrowth={fmt(out["eps_growth"])} ValuationGrowth={fmt(out["valuation_growth"])} ROE={fmt(out["roe"])} PEG={fmt(out["peg"])}',flush=True)
    return out


def _eps_growth_from_yfinance_statements(ticker):
    """V2.10.47：從 yfinance 財報直接建立 EPS 成長率 fallback。

    優先順序：
    1. 年度 diluted/basic EPS YoY
    2. 季度同季 YoY
    3. 年度淨利 YoY（僅在完全沒有 EPS 欄位時）
    4. 淨利 / 平均稀釋股數計算 EPS，再做 YoY

    回傳百分比，例如 31.26 代表 +31.26%。
    """
    def clean_series(df, names):
        if not isinstance(df, pd.DataFrame) or df.empty:
            return None
        for name in names:
            if name in df.index:
                ser=pd.to_numeric(df.loc[name], errors='coerce').dropna()
                if len(ser)>=2:
                    return ser
        return None

    def growth_from_series(ser):
        if ser is None or len(ser)<2:
            return None
        # yfinance 財報通常最新欄在最前；若日期順序不同，依欄位日期排序。
        try:
            idx=list(ser.index)
            parsed=[]
            for i,x in enumerate(idx):
                try:
                    dt=pd.to_datetime(x)
                except Exception:
                    dt=pd.NaT
                parsed.append((dt,i,float(ser.iloc[i])))
            if all(not pd.isna(x[0]) for x in parsed):
                parsed.sort(key=lambda z:z[0], reverse=True)
                a=parsed[0][2]; b=parsed[1][2]
            else:
                a=float(ser.iloc[0]); b=float(ser.iloc[1])
        except Exception:
            a=float(ser.iloc[0]); b=float(ser.iloc[1])
        if b == 0:
            return None
        g=(a/abs(b)-1)*100 if b < 0 else (a/b-1)*100
        return float(g) if math.isfinite(g) and -500 <= g <= 500 else None

    # 1) 年度 EPS
    try:
        inc=ticker.get_income_stmt(freq='yearly')
        ser=clean_series(inc,['DilutedEPS','BasicEPS','Diluted EPS','Basic EPS'])
        g=growth_from_series(ser)
        if g is not None:
            return g

        # 有些台股 Yahoo 沒有 EPS 欄位，但有淨利 + 稀釋加權平均股數。
        ni=clean_series(inc,['NetIncome','Net Income','NetIncomeCommonStockholders'])
        shares=clean_series(inc,['DilutedAverageShares','BasicAverageShares','Diluted Average Shares','Basic Average Shares'])
        if ni is not None and shares is not None:
            n=min(len(ni),len(shares))
            vals=[]
            for i in range(n):
                sh=float(shares.iloc[i]); nv=float(ni.iloc[i])
                if sh != 0: vals.append(nv/sh)
            if len(vals)>=2:
                a,b=vals[0],vals[1]
                if b!=0:
                    g=(a/abs(b)-1)*100 if b<0 else (a/b-1)*100
                    if math.isfinite(g) and -500<=g<=500:
                        return float(g)
    except Exception as e:
        print(f'V2.10.47 yfinance年度EPS fallback失敗: {type(e).__name__}: {e}',flush=True)

    # 2) 季度同季 YoY：最新季度 vs 約一年前季度。
    try:
        incq=ticker.get_income_stmt(freq='quarterly')
        ser=clean_series(incq,['DilutedEPS','BasicEPS','Diluted EPS','Basic EPS'])
        if ser is not None and len(ser)>=5:
            vals=list(ser.astype(float).values)
            # yfinance 通常最新在前；第 5 個約為去年同季。
            for j in range(4,min(len(vals),8)):
                a,b=vals[0],vals[j]
                if b!=0:
                    g=(a/abs(b)-1)*100 if b<0 else (a/b-1)*100
                    if math.isfinite(g) and -500<=g<=500:
                        return float(g)
    except Exception as e:
        print(f'V2.10.47 yfinance季度EPS fallback失敗: {type(e).__name__}: {e}',flush=True)

    # 3) 最後才使用淨利 YoY；這是「EPS 無法取得」時的近似值，並在 log 明確標記。
    try:
        inc=ticker.get_income_stmt(freq='yearly')
        ni=clean_series(inc,['NetIncome','Net Income','NetIncomeCommonStockholders'])
        g=growth_from_series(ni)
        if g is not None:
            print('V2.10.47：無 EPS 欄位，使用年度淨利 YoY 作為 EPS Growth 近似',flush=True)
            return g
    except Exception:
        pass
    return None

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

        # V2.10.47：Ticker.info 沒有 earningsGrowth 時，直接從財報 EPS 補值。
        # 這是修正 1802/2401/2354 等「PE 有值但 EPS 成長 N/A」的主要 fallback。
        if o['eps_growth'] is None:
            try:
                o['eps_growth'] = _eps_growth_from_yfinance_statements(ticker)
            except Exception as e_eps:
                print(f'V2.10.47 EPS Growth 財報 fallback失敗 {symbol}: {type(e_eps).__name__}: {e_eps}',flush=True)

        # V2.10.48：Yahoo 財報仍無 EPS Growth 時，改用 MOPS 官方財報。
        if o['eps_growth'] is None:
            try:
                o['eps_growth'] = mops_eps_growth_fallback(clean_code(symbol), 'TWSE' if str(symbol).endswith('.TW') else 'TPEX')
            except Exception as e_mops:
                print(f'V2.10.48 MOPS EPS Growth fallback失敗 {symbol}: {type(e_mops).__name__}: {e_mops}',flush=True)

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


def yahoo_chart_daily_fallback(symbol, period='6mo'):
    """V2.10.41：Yahoo chart API 日線備援，供 ETF 技術面異常時使用。"""
    try:
        url=f'https://query1.finance.yahoo.com/v8/finance/chart/{symbol}'
        r=requests.get(url,params={'range':period,'interval':'1d','events':'div,splits'},timeout=8,headers={'User-Agent':'Mozilla/5.0 stock-alert/2.10.41'})
        r.raise_for_status()
        j=r.json().get('chart',{}).get('result') or []
        if not j: return None
        x=j[0]; ts=x.get('timestamp') or []; q=(x.get('indicators') or {}).get('quote') or []
        if not q: return None
        q=q[0]
        n=min(len(ts),len(q.get('open') or []),len(q.get('high') or []),len(q.get('low') or []),len(q.get('close') or []),len(q.get('volume') or []))
        if n<2: return None
        idx=pd.to_datetime(ts[:n],unit='s',utc=True).tz_convert('Asia/Taipei').tz_localize(None)
        d=pd.DataFrame({
            'Open':q.get('open',[])[:n], 'High':q.get('high',[])[:n],
            'Low':q.get('low',[])[:n], 'Close':q.get('close',[])[:n],
            'Volume':q.get('volume',[])[:n]
        },index=idx).dropna(subset=['Close'])
        return d if not d.empty else None
    except Exception as e:
        print(f'Yahoo chart日線備援失敗 {symbol}: {type(e).__name__}',flush=True)
        return None

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
    # V2.10.71：保留快取對應的最新可用交易日，便於辨識資料新鮮度。
    if x.get('_data_date'):
        out['_data_date'] = str(x.get('_data_date'))
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
    """V2.10.23：Actions 用少量批次請求取得多檔 6 個月日線。"""
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
    """V2.10.23：GitHub Actions 全市場技術快取建立器。

    - 目標：動態市場股票池內全部 TWSE/TPEX 股票，另含 0050、QQQ、^TWII。
    - 歷史技術快取先保留新鮮資料；只更新缺少或超過 36 小時的標的。
    - V2.10.71：這個 36 小時門檻只適用全市場批次，不適用背景目標股即時分析。
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
    for etf in ETF_MAP.values():
        ticker=etf['symbol']; key=clean_code(ticker)
        # V2.10.56：已知 Yahoo 不提供歷史資料的下市/失效 ETF 不進技術批次。
        # 這些標的仍可保留在 ETF_MAP 供名稱解析，但不應每次 Action 觸發 404。
        if ticker in {'00679B.TW','00887.TW'}:
            continue
        if key and (force or not _technical_cache_is_fresh(cache, key)):
            extras.append((key,ticker))

    targets.extend(extras)
    # 去重但保留順序。
    seen = set()
    targets = [(c, t) for c, t in targets if not (c in seen or seen.add(c))]

    total_market = sum(
        1 for code, item in u.items()
        if clean_code(code).isdigit() and (item or {}).get('market') in ('TWSE', 'TPEX')
    )
    print(
        f'========== V2.10.23 全市場技術快取 ==========',
        flush=True
    )
    print(
        f'技術快取：市場股票 {total_market} 檔；需更新 {len(targets)} 檔；'
        f'目前快取 {sum(1 for k in cache if isinstance(cache.get(k), dict))} 檔',
        flush=True
    )

    if not targets:
        print('技術快取：全市場歷史快取全部在 36 小時有效期內，無需批次更新；目標股背景分析仍可即時刷新', flush=True)
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
                    # V2.10.71：全市場歷史技術快取也記錄其最新交易日。
                    try:
                        if frame is not None and not frame.empty:
                            latest_dt = pd.to_datetime(frame.index[-1])
                            if getattr(latest_dt, 'tzinfo', None) is not None:
                                latest_dt = latest_dt.tz_convert(TW_TZ)
                            else:
                                latest_dt = latest_dt.tz_localize(TW_TZ)
                            result['_data_date'] = latest_dt.strftime('%Y-%m-%d')
                    except Exception:
                        pass
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


def technical(symbol, force_refresh=False):
    """V2.10.73：即時資料 / 歷史資料快取分離。

    規則：
    1. LINE 輕量模式（force_refresh=False）：
       本機技術快取 -> GitHub 遠端技術快取 -> N/A。
       這條路徑刻意不主動打 Yahoo/TWSE，以維持 callback / Render Free 的速度。
    2. 背景網頁分析 / Actions 目標股（force_refresh=True）：
       一律先抓最新可用 Yahoo 1d 日線，不受 36/72 小時快取限制。
       成功後重新計算 MA20/MA60、RSI、KD、趨勢、距20日低點，
       並立即寫回 line_technical_cache.json。
    3. 即時刷新失敗：
       才退回本機快取；若本機沒有，再嘗試 GitHub 遠端快取。
    4. 全市場 Actions 批次仍由 refresh_all_technical_cache() 使用 36 小時
       更新門檻，避免 1985 檔全部重抓；但目標股背景分析永遠優先最新資料。
    """
    cache_key = clean_code(str(symbol).split('.')[0]) or str(symbol)
    cache = load_json(LINE_TECH_CACHE_FILE)
    if not isinstance(cache, dict):
        cache = {}

    # --------------------------------------------------------
    # V2.10.73：LINE 輕量模式
    # 只有「非強制刷新」才走快取優先。
    # 背景網頁分析會以 force_refresh=True 直接跳過這裡，
    # 即使 LINE_MODE_ACTIVE=True 也不會被舊快取攔截。
    # --------------------------------------------------------
    if LINE_MODE_ACTIVE and not force_refresh:
        z = _load_technical_cache_entry(
            cache,
            cache_key,
            max_age=TECH_LINE_CACHE_MAX_AGE
        )
        if z:
            print(f'LINE技術面：使用本機全市場快取 {cache_key}', flush=True)
            return z

        remote = load_remote_json_cache(
            LINE_TECH_CACHE_FILE,
            timeout=4
        )
        z = _load_technical_cache_entry(
            remote,
            cache_key,
            max_age=TECH_LINE_CACHE_MAX_AGE
        )
        if z:
            try:
                cache[cache_key] = remote[cache_key]
                save_json(LINE_TECH_CACHE_FILE, cache)
            except Exception:
                pass
            print(f'LINE技術面：使用 GitHub 全市場快取 {cache_key}', flush=True)
            return z

        print(
            f'LINE技術面：全市場快取沒有 {cache_key}，'
            f'避免 Render Free 即時抓取，使用 N/A',
            flush=True
        )
        return _technical_from_df(None)

    # --------------------------------------------------------
    # V2.10.73：背景網頁分析 / Actions
    # force_refresh=True 時「即時資料優先」，
    # 不讀舊技術快取作為正常路徑。
    # --------------------------------------------------------
    if not force_refresh:
        z = _load_technical_cache_entry(
            cache,
            cache_key,
            max_age=TECH_CACHE_MAX_AGE
        )
        if z:
            return z

    d = None
    try:
        print(
            f'V2.10.73 技術面即時刷新：{cache_key} '
            f'Yahoo 1d/6mo',
            flush=True
        )
        d = yf_download(
            symbol,
            '6mo',
            '1d'
        )
    except Exception as e:
        print(
            f'V2.10.73 技術面 Yahoo 失敗 {cache_key}：'
            f'{type(e).__name__}',
            flush=True
        )

    if d is not None and not d.empty:
        try:
            c = pd.to_numeric(
                d['Close'],
                errors='coerce'
            ).dropna()
        except Exception:
            c = None
    else:
        c = None

    # Yahoo 沒有至少 60 根日線才使用 TWSE 官方日線備援。
    if c is None or len(c) < 60:
        try:
            tw = _twse_history_fallback(
                cache_key,
                3
            )
            if tw is not None and not tw.empty:
                d = tw
        except Exception as e:
            print(
                f'V2.10.73 技術面 TWSE 備援失敗 {cache_key}：'
                f'{type(e).__name__}',
                flush=True
            )

    o = _technical_from_df(d)

    # --------------------------------------------------------
    # V2.10.73：技術趨勢即時價格強制重算。
    # MA20/MA60/RSI/KD 仍完全使用日線資料；只有「目前 price」改用
    # 最新 5 分鐘 K 棒，避免 market_universe_cache.json 的舊收盤價
    # 把已站回 MA20、但尚未站上 MA60 的股票誤判成空頭。
    # --------------------------------------------------------
    if force_refresh:
        live_price = get_latest_intraday_price(symbol)
        o['price_source'] = 'Yahoo 5分鐘即時' if live_price is not None and live_price > 0 else '日線收盤（即時價格取得失敗）'
        if live_price is not None and live_price > 0:
            o['price'] = live_price
            if o.get('ma20') is not None and o.get('ma60') is not None:
                if live_price > o['ma20'] > o['ma60']:
                    o['trend'] = '多頭'
                elif live_price < o['ma20'] < o['ma60']:
                    o['trend'] = '空頭'
                else:
                    o['trend'] = '震盪'
            else:
                o['trend'] = '震盪'
            if o.get('recent_low') is not None and o['recent_low'] > 0:
                o['distance_low'] = live_price / o['recent_low'] - 1
            print(
                f'V2.10.73 技術趨勢強制重算：{cache_key} '
                f'live_price={live_price:.2f} '
                f'MA20={o.get("ma20")} MA60={o.get("ma60")} '
                f'trend={o.get("trend")}',
                flush=True
            )

    # --------------------------------------------------------
    # V2.10.71：成功取得即時日線後，立即更新技術快取。
    # 這裡保存的是「由最新日線重新計算後的結果」，
    # 因此快取本身也會同步到最新可用交易日。
    # --------------------------------------------------------
    if o.get('price') is not None:
        try:
            if d is not None and not d.empty:
                idx = d.index[-1]
                try:
                    latest_dt = pd.to_datetime(idx)
                    if getattr(latest_dt, 'tzinfo', None) is not None:
                        latest_dt = latest_dt.tz_convert(TW_TZ)
                    else:
                        latest_dt = latest_dt.tz_localize(TW_TZ)
                    o['_data_date'] = latest_dt.strftime('%Y-%m-%d')
                except Exception:
                    pass
        except Exception:
            pass

        _save_technical_cache_entry(
            cache,
            cache_key,
            o,
            save=True
        )
        print(
            f'V2.10.71 技術快取已更新：{cache_key} '
            f'price={o.get("price"):.2f}'
            + (
                f' data_date={o.get("_data_date")}'
                if o.get('_data_date')
                else ''
            ),
            flush=True
        )
        return o

    # --------------------------------------------------------
    # 即時資料失敗才 fallback。
    # 注意：這是「故障保護」，不是正常分析路徑。
    # --------------------------------------------------------
    z = _load_technical_cache_entry(
        cache,
        cache_key,
        max_age=TECH_CACHE_MAX_AGE
    )
    if z:
        print(
            f'V2.10.73 技術面即時刷新失敗，退回本機技術快取：'
            f'{cache_key}',
            flush=True
        )
        return z

    remote = load_remote_json_cache(
        LINE_TECH_CACHE_FILE,
        timeout=4
    )
    z = _load_technical_cache_entry(
        remote,
        cache_key,
        max_age=TECH_LINE_CACHE_MAX_AGE
    )
    if z:
        print(
            f'V2.10.73 技術面即時刷新失敗，退回 GitHub 技術快取：'
            f'{cache_key}',
            flush=True
        )
        return z

    return _technical_from_df(None)


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
            '融資當日餘額',
            '融資當日餘額(張)',
            '融資今日餘額(張)',
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
            '融資前日餘額(張)',
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
            '融券當日餘額',
            '融券當日餘額(張)',
            '融券今日餘額(張)',
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
            '融券前日餘額(張)',
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

    # V2.10.34：部分 TPEx 回傳只給買進/賣出/現金償還，沒有前日餘額。
    if mc is None:
        mbuy=find_value(row,['融資買進','融資買進(張)','MarginPurchaseBuy'])
        msell=find_value(row,['融資賣出','融資賣出(張)','MarginPurchaseSell'])
        mcash=find_value(row,['融資現金償還','融資現金償還(張)','MarginPurchaseCashRepayment'])
        if mbuy is not None or msell is not None or mcash is not None:
            mc=(mbuy or 0)-(msell or 0)-(mcash or 0)
    if sc is None:
        ssell=find_value(row,['融券賣出','融券賣出(張)','ShortSaleSell'])
        sbuy=find_value(row,['融券買進','融券買進(張)','ShortSaleBuy'])
        scash=find_value(row,['融券現券','融券現券(張)','ShortSaleCash'])
        if ssell is not None or sbuy is not None or scash is not None:
            sc=(ssell or 0)-(sbuy or 0)-(scash or 0)

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
    """V2.10.22：LINE 只讀小型法人摘要快取。

    Render Free 絕對不下載完整 20 日、1985 檔 T86 JSON。
    Actions 端會把 CHIP_HISTORY 壓縮成每檔股票一筆：latest/5d/20d。
    快取不存在時直接回傳空資料，不打 TWSE、不等待、不重試。
    """
    code = clean_code(code)
    cache = load_json(LINE_CHIP_SUMMARY_CACHE_FILE)
    if not isinstance(cache, dict) or not cache:
        # 僅嘗試一次極小的遠端摘要檔；不再碰 chip_history.json。
        cache = load_remote_json_cache(LINE_CHIP_SUMMARY_CACHE_FILE, timeout=2)
    market_data = cache.get(market, {}) if isinstance(cache, dict) else {}
    item = market_data.get(code) if isinstance(market_data, dict) else None
    if isinstance(item, dict):
        print(f"LINE籌碼：使用摘要快取 {code} latest={item.get('latest')} 5d={item.get('5d')} 20d={item.get('20d')}", flush=True)
        return [{
            'date': 'summary',
            'data': {code: {'total': item.get('latest')}}
        }]
    # V2.10.27：若摘要檔尚未部署，仍可從 Actions 的完整 chip_history
    # 遠端快取只取「單一查詢股票」20日資料；Render 不抓全市場到記憶體。
    try:
        remote_full = load_remote_json_cache(CHIP_HISTORY_FILE, timeout=3)
        market_hist = remote_full.get(market, {}) if isinstance(remote_full, dict) else {}
        rows = []
        if isinstance(market_hist, dict):
            for ds, daydata in sorted(market_hist.items(), reverse=True):
                if isinstance(daydata, dict) and code in daydata:
                    rows.append({'date': ds, 'data': {code: daydata.get(code)}})
                    if len(rows) >= days:
                        break
        if rows:
            print(f'LINE籌碼：摘要缺失，使用 GitHub 完整法人快取單股擷取 {code} {len(rows)}日', flush=True)
            vals=[]
            for row in rows:
                item=row.get('data',{}).get(code)
                if isinstance(item,dict) and item.get('total') is not None:
                    vals.append(float(item['total']))
            summary={
                'latest': vals[0] if vals else None,
                '5d': sum(vals[:5]) if len(vals)>=5 else None,
                '20d': sum(vals[:20]) if len(vals)>=20 else None
            }
            try:
                cache.setdefault(market, {})[code] = summary
                _save_line_small_cache(LINE_CHIP_SUMMARY_CACHE_FILE, cache)
            except Exception:
                pass
            return [{'date':'summary','data':{code:{'total':summary['latest']}}}]
    except Exception as e:
        print(f'LINE籌碼：GitHub完整快取備援失敗 {code}：{type(e).__name__}', flush=True)
    print(f'LINE籌碼：無法人快取 {code}，使用 N/A，不打 TWSE', flush=True)
    return []


def yahoo_margin_fast(code, market):
    """V2.10.34：Yahoo 股市資券頁獨立備援。

    TPEx 官方 API 偶爾因 SSL/欄位格式變動而無法在 Render 取得資料。
    Yahoo 股市的資券變化頁仍直接呈現融資/融券增減與餘額，因此作為
    第二獨立來源。只抓單一股票，不下載全市場。
    """
    code=clean_code(code)
    if not code or not code.isdigit():
        return {'margin_change':None,'margin_balance':None,'short_change':None,'short_balance':None}
    # V2.10.34：Yahoo 台股資券頁使用純股票代號路徑；
    # /quote/6488.TWO/margin 在部分情況會沒有資券資料，
    # 正確頁面為 /quote/6488/margin。TWSE/TPEX 都統一使用純代號。
    url=f'https://tw.stock.yahoo.com/quote/{code}/margin'
    out={'margin_change':None,'margin_balance':None,'short_change':None,'short_balance':None}
    try:
        r=requests.get(url,timeout=5,headers={'User-Agent':'Mozilla/5.0 stock-alert/2.10.39'},verify=False)
        r.raise_for_status()
        text=r.text
        # 先用 pandas 解析 SSR table；Yahoo 改版時只要表格仍存在即可。
        try:
            tables=pd.read_html(text)
        except Exception:
            tables=[]
        for df in tables:
            if df is None or df.empty:
                continue
            cols=[]
            for c in df.columns:
                if isinstance(c,tuple):
                    cols.append(' '.join(str(x) for x in c))
                else:
                    cols.append(str(c))
            df=df.copy(); df.columns=cols
            row=df.iloc[0]
            def col(keys):
                for c in df.columns:
                    cc=str(c).replace(' ','')
                    if any(k in cc for k in keys):
                        return to_float(row.get(c))
                return None
            mc=col(['融資增減','融資增減'])
            mb=col(['融資餘額'])
            sc=col(['融券增減'])
            sb=col(['融券餘額'])
            if any(v is not None for v in (mc,mb,sc,sb)):
                out={'margin_change':mc,'margin_balance':mb,'short_change':sc,'short_balance':sb}
                print(f'LINE籌碼：Yahoo資券頁備援成功 {code} {out}',flush=True)
                return out
        # SSR/JSON fallback：尋找「融資」附近的增減與餘額數字。
        compact=re.sub(r'\s+',' ',text)
        mf=re.search(r'融資.{0,700}?(?:增減|變化).{0,180}?(-?[0-9,]+).{0,180}?(?:餘額|餘額張數).{0,180}?([0-9,]+)',compact)
        sf=re.search(r'融券.{0,700}?(?:增減|變化).{0,180}?(-?[0-9,]+).{0,180}?(?:餘額|餘額張數).{0,180}?([0-9,]+)',compact)
        if mf:
            out['margin_change']=to_float(mf.group(1)); out['margin_balance']=to_float(mf.group(2))
        else:
            mr=re.search(r'融資.{0,700}?餘額.{0,180}?([0-9,]+).{0,180}?(?:增減|變化).{0,180}?(-?[0-9,]+)',compact)
            if mr:
                out['margin_balance']=to_float(mr.group(1)); out['margin_change']=to_float(mr.group(2))
        if sf:
            out['short_change']=to_float(sf.group(1)); out['short_balance']=to_float(sf.group(2))
        else:
            sr=re.search(r'融券.{0,700}?餘額.{0,180}?([0-9,]+).{0,180}?(?:增減|變化).{0,180}?(-?[0-9,]+)',compact)
            if sr:
                out['short_balance']=to_float(sr.group(1)); out['short_change']=to_float(sr.group(2))
        if any(v is not None for v in out.values()):
            print(f'LINE籌碼：Yahoo資券頁文字備援成功 {code}',flush=True)
    except Exception as e:
        print(f'LINE籌碼：Yahoo資券頁備援失敗 {code}：{type(e).__name__}',flush=True)
    return out

def _tpex_margin_html_fallback(code):
    """V2.10.40：TPEx 融資融券真正可用的多源 HTML fallback。

    TPEx 官方表格的欄位固定為：
    代號、名稱、前資餘額、資買、資賣、現償、資餘額、...
    前券餘額、券賣、券買、券償、券餘額、...

    Render 有時會遇到 www.tpex.org.tw SSL 憑證鏈問題，因此同一來源
    允許 verify=False 的「單次」安全備援；不會重試多次，也不下載歷史資料。
    """
    code=clean_code(code)
    if not code or not code.isdigit(): return {}
    urls=[
        'https://www.tpex.org.tw/web/stock/margin_trading/margin_balance/margin_bal_result.php?l=zh-tw&o=htm',
        'https://www.tpex.org.tw/web/stock/margin_trading/margin_balance/margin_bal_result.php?d=&l=zh-tw&o=htm',
    ]
    headers={'User-Agent':'Mozilla/5.0 stock-alert/2.10.39','Accept':'text/html,application/xhtml+xml'}
    for url in urls:
        for verify in (True, False):
            try:
                r=requests.get(url,timeout=7,headers=headers,verify=verify)
                r.raise_for_status(); text=r.text
                # 先走 pandas：欄位名稱完整時最可靠。
                try:
                    tables=pd.read_html(text)
                except Exception:
                    tables=[]
                for df in tables:
                    if df is None or df.empty: continue
                    if isinstance(df.columns,pd.MultiIndex):
                        df.columns=[' '.join(str(x) for x in c if str(x)!='nan').strip() for c in df.columns]
                    cols=[str(c).strip() for c in df.columns]
                    code_col=next((c for c in cols if any(k in c.replace(' ','') for k in ('證券代號','股票代號','代號','Code'))),None)
                    if code_col:
                        row=next((rr for _,rr in df.iterrows() if clean_code(rr.get(code_col))==code),None)
                        if row is not None:
                            def find_col(keys):
                                for c in cols:
                                    cc=c.replace(' ','')
                                    if any(k in cc for k in keys):
                                        v=to_float(row.get(c))
                                        if v is not None: return v
                                return None
                            mp=find_col(['前資餘額','前日融資餘額'])
                            mb=find_col(['資餘額','本日餘額','今日餘額'])
                            sp=find_col(['前券餘額','前日融券餘額'])
                            sb=find_col(['券餘額','本日融券餘額','今日融券餘額'])
                            mc=find_col(['融資增減','融資變化'])
                            sc=find_col(['融券增減','融券變化'])
                            if mc is None and mp is not None and mb is not None: mc=mb-mp
                            if sc is None and sp is not None and sb is not None: sc=sb-sp
                            out={'margin_change':mc,'margin_balance':mb,'short_change':sc,'short_balance':sb}
                            if any(v is not None for v in out.values()):
                                print(f'LINE籌碼：TPEx HTML解析成功 {code} {out}',flush=True)
                                return out
                # 最後用原始 HTML 文字逐列解析。這是針對 TPEx 現行表格格式的固定欄位 fallback。
                compact=re.sub(r'<[^>]+>',' ',text)
                compact=html.unescape(compact)
                compact=re.sub(r'\s+',' ',compact)
                # 代號後面到下一個代號前的數字欄位；目前表格為 18 個數字欄位。
                pat=re.compile(r'(?<!\d)'+re.escape(code)+r'\s+[^0-9]{1,40}?((?:-?[0-9][0-9,\.]*\s+){17}-?[0-9][0-9,\.]*)')
                m=pat.search(compact)
                if m:
                    vals=[to_float(x) for x in re.findall(r'-?[0-9][0-9,]*(?:\.[0-9]+)?',m.group(1))]
                    if len(vals)>=18:
                        out={'margin_change':None,'margin_balance':vals[4],
                             'short_change':None,'short_balance':vals[13]}
                        out['margin_change']=vals[4]-vals[0] if vals[0] is not None and vals[4] is not None else None
                        out['short_change']=vals[13]-vals[9] if vals[9] is not None and vals[13] is not None else None
                        if any(v is not None for v in out.values()):
                            print(f'LINE籌碼：TPEx HTML固定欄位解析成功 {code} {out}',flush=True)
                            return out
            except Exception as e:
                if verify:
                    print(f'LINE籌碼：TPEx HTML SSL/連線失敗 {code}：{type(e).__name__}',flush=True)
                else:
                    print(f'LINE籌碼：TPEx HTML verify=False 仍失敗 {code}：{type(e).__name__}',flush=True)
                continue
    return {}


def _line_margin_fast(code, market):
    """V2.10.41：LINE 融資融券資料多源刷新。

    有值 cache 不再永久鎖死；超過 24 小時、只有部分欄位或來源可取得新資料時，
    新資料直接覆蓋舊 cache。TPEx 官方 HTML / Yahoo 為後續 fallback。
    """
    code=clean_code(code)
    empty={'margin_change':None,'margin_balance':None,'short_change':None,'short_balance':None}
    cache=_load_line_small_cache(LINE_MARGIN_CACHE_FILE)
    market_data=cache.get(market,{}) if isinstance(cache,dict) else {}
    cached_item=market_data.get(code) if isinstance(market_data,dict) else None
    if not isinstance(cached_item,dict): cached_item={}
    cached_at=to_float(cached_item.get('_cached_at')) or 0
    age=(time.time()-cached_at)/3600 if cached_at else 999999
    fields=('margin_change','margin_balance','short_change','short_balance')
    complete=all(to_float(cached_item.get(k)) is not None for k in fields)
    # 24h 內完整 cache 可直接使用；否則先嘗試最新官方資料。
    if complete and age<24:
        print(f'V2.10.41 LINE籌碼：使用新鮮融資快取 {code} age={age:.1f}h',flush=True)
        return {k:to_float(cached_item.get(k)) for k in fields}

    def save(item):
        if not isinstance(item,dict): return
        if not any(to_float(item.get(k)) is not None for k in fields): return
        clean={k:to_float(item.get(k)) for k in fields}
        clean['_cached_at']=time.time()
        try:
            cache.setdefault(market,{})[code]=clean
            _save_line_small_cache(LINE_MARGIN_CACHE_FILE,cache)
        except Exception as e:
            print(f'V2.10.41 LINE籌碼快取保存失敗 {code}: {e}',flush=True)

    # 先取官方 API；新值優先覆蓋舊 cache。
    try:
        if market=='TPEX':
            data=http_json(TPEX_BASE+'/tpex_mainboard_margin_balance',timeout=LINE_FAST_TIMEOUT,retries=0)
        else:
            data=http_json(TWSE_BASE+'/exchangeReport/MI_MARGN',timeout=LINE_FAST_TIMEOUT,retries=0)
        parsed=_parse_margin_payload(data)
        one=parsed.get(code)
        if isinstance(one,dict) and any(v is not None for v in one.values()):
            save(one)
            print(f'V2.10.41 LINE籌碼：官方 API 更新 {code} {one}',flush=True)
            return one
    except Exception as e:
        print(f'V2.10.41 LINE籌碼：官方 API 失敗 {code}: {type(e).__name__}',flush=True)

    # TPEx 現行官方網頁：欄位名稱優先，不依賴固定第幾個數字。
    if market=='TPEX':
        try:
            one=_tpex_margin_html_fallback(code)
            if one and any(v is not None for v in one.values()):
                save(one)
                return one
        except Exception as e:
            print(f'V2.10.41 LINE籌碼：TPEx HTML失敗 {code}: {type(e).__name__}',flush=True)

    # Yahoo 單股頁最後備援。
    try:
        y=yahoo_margin_fast(code,market)
        if any(v is not None for v in y.values()):
            save(y)
            return y
    except Exception as e:
        print(f'V2.10.41 LINE籌碼：Yahoo資券失敗 {code}: {type(e).__name__}',flush=True)

    # 所有新來源失敗才回退舊 cache，即使舊 cache 不完整也不丟掉已存在欄位。
    if any(to_float(cached_item.get(k)) is not None for k in fields):
        print(f'V2.10.41 LINE籌碼：新來源皆失敗，回退舊 cache {code}',flush=True)
        return {k:to_float(cached_item.get(k)) for k in fields}
    return empty

def score_fund(pe, one, peer, peg, roe, eps, pb, yld, model, valuation_growth=None):
    """V2.10.34：產業化基本面評分；缺資料不扣分，但限制少數欄位過度放大。

    完整資料：最高 40。可用權重越少，仍不直接扣分，但會依資料完整度設定
    合理上限，避免只剩 PB/殖利率時被放大成接近滿分。
    """
    model=model if isinstance(model,dict) else DEFAULT_MODEL
    w=model.get('weights',DEFAULT_MODEL['weights']); s=0.0; available=0.0; why=[]
    def add(key,ratio,reason=None):
        nonlocal s,available
        weight=float(w.get(key,0) or 0)
        if weight<=0 or ratio is None: return
        available+=weight; pts=weight*max(0.0,min(1.0,ratio)); s+=pts
        if reason and pts>=weight*0.65: why.append(reason)
    if pe is not None and pe>0:
        ratios=[]
        if one is not None and one>0:
            r=pe/one; ratios.append(1 if r<=.9 else .75 if r<=1.05 else .45 if r<=1.15 else .1 if r<=1.3 else 0)
        if peer is not None and peer>0:
            r=pe/peer; ratios.append(1 if r<.85 else .75 if r<=1.05 else .4 if r<=1.15 else .1 if r<=1.3 else 0)
        if ratios: add('pe',max(ratios),'低於自身/同業合理估值')
    if peg is not None and peg>0: add('peg',1 if peg<.8 else .85 if peg<1 else .6 if peg<1.2 else .2 if peg<1.5 else 0,'PEG具吸引力')
    if pb is not None and pb>0: add('pb',1 if pb<1.5 else .75 if pb<2 else .5 if pb<4 else .15 if pb<6 else 0,'PB合理')
    if yld is not None and yld>=0: add('yield',1 if yld>=5 else .75 if yld>=3 else .45 if yld>=2 else .15 if yld>=1 else 0,'殖利率具吸引力')
    if roe is not None: add('roe',1 if roe>=30 else .8 if roe>=20 else .6 if roe>=15 else .4 if roe>=10 else .15 if roe>0 else 0,'ROE良好')
    growth_for_score = to_float(valuation_growth) if valuation_growth is not None else eps
    if growth_for_score is not None: add('growth',1 if growth_for_score>=50 else .85 if growth_for_score>=30 else .7 if growth_for_score>=20 else .5 if growth_for_score>10 else .25 if growth_for_score>0 else 0,'獲利成長')
    if available<=0: return 0,why
    raw=s/available*40.0
    coverage=available/max(1.0,sum(float(v or 0) for v in w.values()))
    # No penalty for missing data; only cap the confidence ceiling.
    cap=40 if coverage>=.80 else 36 if coverage>=.60 else 32 if coverage>=.45 else 28 if coverage>=.30 else 24 if coverage>=.20 else 20
    return min(cap,int(round(raw))),why

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


def _fund_cache_valid_value(key, value):
    """V2.10.41：判斷 line_fund_cache.json 的單欄位是否合理。

    Cache 只當 fallback，不再讓明顯異常值永久鎖死新資料。
    """
    v=to_float(value)
    if v is None or not math.isfinite(v):
        return False
    limits={
        'pe': (0, PE_MAX_VALID),
        'pb': (0, 100),
        'yield': (0, 30),
        'eps_growth': (-500, 500),
        'roe': (-100, 100),
        'peg': (0, 100),
    }
    lo,hi=limits.get(key,(-1e100,1e100))
    return lo <= v <= hi


def _fund_cache_suspicious(key, value):
    """V2.10.41：對高機率 parser 錯誤的 cache 做額外標記。"""
    v=to_float(value)
    if v is None: return True
    if not _fund_cache_valid_value(key,v): return True
    # 殖利率/PEG 不應出現明顯百分比倍率錯誤。
    if key=='yield' and v>20: return True
    if key=='peg' and v>50: return True
    return False


def _v21045_peer_pe_fallback(peer_item, pe_data):
    """V2.10.67：同次產業 PE 官方資料優先，缺當日資料時使用最近有效歷史 PE。

    目的：TWSE/TPEx 當日 BWIBBU_ALL 暫時失敗時，不讓整個同業 Top10
    平均/中位數突然全部變成 N/A。歷史 fallback 只取最近一筆有效官方 PE，
    不使用 Yahoo PE 或價格/TTM EPS 推算，維持同業比較的官方口徑。
    """
    code=clean_code(str(peer_item.get('code','')))
    v=to_float((pe_data.get(code,{}) or {}).get('pe')) if isinstance(pe_data,dict) else None
    if v is not None and 0 < v <= PE_MAX_VALID:
        return v
    try:
        h=load_json(PE_HISTORY_FILE)
        item=h.get(code,{}) if isinstance(h,dict) else {}
        vals=[]
        if isinstance(item,dict):
            for ds,rv in item.items():
                pv=to_float(rv)
                if pv is not None and 0 < pv <= PE_MAX_VALID:
                    vals.append((str(ds),pv))
        if vals:
            vals.sort(key=lambda x:x[0],reverse=True)
            return vals[0][1]
    except Exception:
        pass
    return None


def yahoo_light_fund(symbol, official=None, current_price=None, market=None, industry=None, subindustry=None):
    """V2.10.53：LINE 基本面資料層與 Actions 完全統一。

    LINE 不再維護另一套舊版基本面邏輯，也不再使用：
      - MOPS EPS Growth fallback
      - cache PEG 反推 EPS Growth
      - PB/PE 反推 ROE
      - 舊版 line_fund_cache 作為新資料的替代來源

    LINE 若仍由舊流程呼叫 yahoo_light_fund()，直接轉交
    V2.10.50/2.10.51 的 official_fundamental()，確保 Actions 與 LINE
    使用完全相同的 PE / PB / 殖利率 / EPS Growth / ROE / PEG 口徑。

    注意：LINE 的 PE/PB/殖利率仍優先使用 analysis() 已取得的官方資料；
    official_fundamental() 負責 Yahoo / yfinance 補值。
    """
    try:
        result = official_fundamental(
            symbol,
            official=official if isinstance(official, dict) else {},
            current_price=current_price,
            market=market,
            industry=industry,
            subindustry=subindustry
        )
        if not isinstance(result, dict):
            return {
                'pe': None, 'pb': None, 'yield': None,
                'eps_growth': None, 'roe': None, 'peg': None
            }
        print(
            f'V2.10.98 LINE基本面統一資料層 {clean_code(str(symbol).split(".")[0])}: '
            f'PE={fmt(result.get("pe"))} PB={fmt(result.get("pb"))} '
            f'Yield={fmt(result.get("yield"))} '
            f'EPSGrowth={fmt(result.get("eps_growth"))} '
            f'ROE={fmt(result.get("roe"))} PEG={fmt(result.get("peg"))}',
            flush=True
        )
        return result
    except Exception as e:
        print(
            f'V2.10.98 LINE基本面統一資料層失敗 {symbol}: '
            f'{type(e).__name__}: {e}',
            flush=True
        )
        return {
            'pe': None, 'pb': None, 'yield': None,
            'eps_growth': None, 'roe': None, 'peg': None
        }

def _parse_number_near(text, label, max_chars=180):
    """V2.10.37：從 Yahoo/投信 HTML 文字中找 label 後的第一個合理數字。"""
    if not text:
        return None
    compact=re.sub(r'\s+', ' ', str(text))
    m=re.search(re.escape(label) + r'.{0,' + str(max_chars) + r'}?([0-9][0-9,]*(?:\.[0-9]+)?)', compact, flags=re.I)
    return to_float(m.group(1)) if m else None


def yahoo_tw_profile_fallback(symbol):
    """V2.10.37：Yahoo 台股 Profile HTML 最後基本面/ETF 備援。

    Yahoo quoteSummary 在 Render/Actions 偶爾會被限流，但 tw.stock.yahoo.com
    的公開 profile 頁仍可取得管理費、保管費、資產規模、股利等資料。
    每個欄位獨立補，不覆蓋已取得值。
    """
    out={}
    sym=str(symbol or '').strip()
    if not sym:
        return out
    try:
        url=f'https://tw.stock.yahoo.com/quote/{sym}/profile'
        r=requests.get(url,timeout=5,headers={'User-Agent':'Mozilla/5.0 stock-alert/2.10.36'})
        r.raise_for_status()
        text=html.unescape(re.sub(r'\s+',' ',r.text))
        # 市價：頁面開頭通常會有「收盤/開盤」後的第一個價格；
        # 這裡只在主來源完全沒有 price 時使用。
        m=re.search(r'(?:(?:收盤|開盤)[^0-9]{0,80})([0-9]+(?:\.[0-9]+)?)',text)
        if m: out['price']=to_float(m.group(1))
        assets=_parse_number_near(text,'資產規模（百萬）')
        if assets is not None: out['assets']=assets*1_000_000
        mgmt=_parse_number_near(text,'管理費率')
        cust=_parse_number_near(text,'保管費率')
        # Yahoo profile 的「管理費率」通常是實際費率；保管費率可能是文字級距，
        # 因此只有兩者都是純數字時才相加，避免把 1兆等級文字誤解析。
        if mgmt is not None and 0 <= mgmt <= 5:
            out['management_fee']=mgmt
        if cust is not None and 0 <= cust <= 5:
            out['custodian_fee']=cust
        if out.get('management_fee') is not None and out.get('custodian_fee') is not None:
            out['expense']=out['management_fee']+out['custodian_fee']
            out['expense_source']='Yahoo Profile 管理費+保管費'
        # 公司股票：股東權益報酬率與每股淨值可直接補 ROE/PB。
        roe=_parse_number_near(text,'股東權益報酬率')
        if roe is not None and -100 <= roe <= 100: out['roe']=roe
        bvps=_parse_number_near(text,'每股淨值')
        if bvps is not None and bvps>0: out['bvps']=bvps
        eps=_parse_number_near(text,'最新四季每股盈餘')
        if eps is not None: out['trailing_eps']=eps
        # 最近年度現金股利（Yahoo 股利頁另有完整歷史，這裡只做輕量補洞）。
        div=_parse_number_near(text,'現金股利')
        if div is not None and div>=0: out['dividend_rate']=div
    except Exception as e:
        print(f'Yahoo TW profile fallback失敗 {symbol}: {type(e).__name__}',flush=True)
    return out


def yahoo_tw_dividend_fallback(symbol, price=None):
    """V2.10.37：Yahoo 股利頁計算近一年現金殖利率。

    優先讀 Yahoo 的年度合計列（例如「2026 7.70」），避免只拿單次股利造成
    6488 這類半年配 ETF/股票殖利率被低估。若沒有年度列，再加總最近四筆季/半年度股利。
    """
    try:
        px=to_float(price)
        if px is None or px<=0: return None

        # V2.10.47：優先使用 Yahoo Chart events=div。
        # 股利頁是 JS 動態頁，Render requests 有時拿不到表格內容；Chart events
        # 則直接提供實際現金股利事件。最近 365 天加總可處理半年配/季配股票。
        try:
            now=int(time.time())
            period1=now-366*86400
            chart_url=(f'https://query1.finance.yahoo.com/v8/finance/chart/'
                       f'{symbol}?period1={period1}&period2={now}&interval=1d&events=div')
            cr=requests.get(chart_url,timeout=5,headers={'User-Agent':'Mozilla/5.0 stock-alert/2.10.44'})
            cr.raise_for_status()
            cj=cr.json()
            events=((cj.get('chart') or {}).get('result') or [{}])[0].get('events') or {}
            divs=events.get('dividends') or {}
            vals=[]
            for ev in divs.values() if isinstance(divs,dict) else []:
                if isinstance(ev,dict):
                    v=to_float(ev.get('amount'))
                    if v is not None and 0 <= v <= 50:
                        vals.append(v)
            if vals:
                div=sum(vals)
                y=div/px*100
                if 0 <= y <= 30:
                    return y
        except Exception as e:
            print(f'Yahoo Chart股利事件 fallback失敗 {symbol}: {type(e).__name__}',flush=True)

        url=f'https://tw.stock.yahoo.com/quote/{symbol}/dividend'
        r=requests.get(url,timeout=5,headers={'User-Agent':'Mozilla/5.0 stock-alert/2.10.44'})
        r.raise_for_status()
        text=html.unescape(re.sub(r'\s+',' ',r.text))
        # 找「歷年股利政策」後，第一個純年度總額，例如「2026 7.70」。
        sec=text[text.find('歷年股利政策'):] if '歷年股利政策' in text else text
        m=re.search(r'\b20[0-9]{2}\s+([0-9]+(?:\.[0-9]+)?)\s+-?\s+(?:[0-9]+(?:\.[0-9]+)?)%?',sec)
        if m:
            div=to_float(m.group(1))
            if div is not None and 0 <= div <= 100:
                y=div/px*100
                if 0 <= y <= 30: return y
        # fallback：抓前 4 筆「Q/H」或日期前的股利數字，排除年度總額。
        vals=[]
        for m in re.finditer(r'20[0-9]{2}(?:Q[1-4]|H[12])\s+([0-9]+(?:\.[0-9]+)?)',sec):
            v=to_float(m.group(1))
            if v is not None and 0 <= v <= 50: vals.append(v)
            if len(vals)>=4: break
        if vals:
            y=sum(vals)/px*100
            if 0 <= y <= 30: return y
    except Exception as e:
        print(f'Yahoo股利 fallback失敗 {symbol}: {type(e).__name__}',flush=True)
    return None



def _twse_etf_nav_fallback(symbol):
    """V2.10.41：使用 TWSE 官方 ETF 即時 NAV feed。

    TWSE 官方格式定義：a=代號、e=市場價格、f=投信/總代理人預估淨值、
    g=預估折溢價幅度、h=前一營業日淨值。舊版用 dict.values() 猜欄位順序，
    會把其他數字誤當 NAV，造成 0050/006208/00713 出現 3.00 NAV 與數千%溢價。
    本版只接受明確欄位名稱。
    """
    code=str(symbol or '').upper().replace('.TW','').replace('.TWO','')
    if not code or not re.fullmatch(r'[0-9A-Z]{4,6}',code): return {}
    out={}
    headers={'User-Agent':'Mozilla/5.0 stock-alert/2.10.41','Referer':'https://mis.twse.com.tw/stock/etf_nav.jsp?ex=tse','Accept':'application/json,text/plain,*/*'}
    urls=['https://mis.twse.com.tw/stock/data/all_etf.txt','https://mis.twse.com.tw/stock/etf_nav.jsp?ex=tse']
    for url in urls:
        try:
            r=requests.get(url,timeout=7,headers=headers,verify=False); r.raise_for_status()
            raw=r.text
            parsed=None
            try: parsed=r.json()
            except Exception: parsed=None
            rows=[]
            if isinstance(parsed,dict):
                if isinstance(parsed.get('msgArray'),list): rows.extend(parsed['msgArray'])
                a1=parsed.get('a1')
                if isinstance(a1,list):
                    for block in a1:
                        if isinstance(block,dict) and isinstance(block.get('msgArray'),list): rows.extend(block['msgArray'])
            for row in rows:
                if not isinstance(row,dict): continue
                rc=str(row.get('a') or '').strip().upper()
                if rc!=code: continue
                price=to_float(row.get('e'))
                nav=to_float(row.get('f'))
                premium=to_float(row.get('g'))
                prev_nav=to_float(row.get('h'))
                units=to_float(row.get('c'))
                if price is not None: out['price']=price
                if nav is not None and 0<nav<10000: out['nav']=nav
                if premium is not None and -50<=premium<=50: out['premium']=premium
                if prev_nav is not None and prev_nav>0: out['prev_nav']=prev_nav
                if units is not None and price is not None and units>0: out['assets']=units*price
                if out.get('nav') is not None or out.get('price') is not None:
                    print(f'V2.10.41 TWSE ETF NAV成功 {code}: price={out.get("price")} nav={out.get("nav")} premium={out.get("premium")}',flush=True)
                    return out
            # 純文字/HTML fallback：只接受明確的代號列，不再猜 dict value index。
            compact=html.unescape(raw.replace('\r','\n'))
            for line in compact.splitlines():
                if not re.search(r'(?<![A-Z0-9])'+re.escape(code)+r'(?![A-Z0-9])',line,re.I): continue
                parts=[x.strip() for x in re.split(r'\t|,',line) if x.strip()]
                pos=next((i for i,x in enumerate(parts) if x.upper()==code),None)
                if pos is not None and len(parts)>=pos+8:
                    # a,b,c,d,e,f,g,h
                    price=to_float(parts[pos+4]); nav=to_float(parts[pos+5]); premium=to_float(parts[pos+6]); prev_nav=to_float(parts[pos+7])
                    if price is not None: out['price']=price
                    if nav is not None and 0<nav<10000: out['nav']=nav
                    if premium is not None and -50<=premium<=50: out['premium']=premium
                    if prev_nav is not None and prev_nav>0: out['prev_nav']=prev_nav
                    if out.get('nav') is not None: return out
        except Exception as e:
            print(f'V2.10.41 TWSE ETF NAV fallback失敗 {code}: {type(e).__name__}',flush=True)
    return out

def _official_expense_regex(text):
    """V2.10.40：從投信頁面抓經理費/保管費，避免把費率級距誤當 3003%。"""
    if not text: return {}
    t=html.unescape(re.sub(r'\\s+',' ',text))
    out={}
    # Accept common labels and a short distance to the percentage.
    for label,key in [('經理費','management_fee'),('管理費率','management_fee'),('保管費','custodian_fee'),('保管費率','custodian_fee')]:
        ms=list(re.finditer(re.escape(label),t,re.I))
        for m in ms:
            sec=t[m.start():m.start()+260]
            nums=re.findall(r'(?<![0-9])([0-9]+(?:\\.[0-9]+)?)\\s*%',sec)
            for n in nums:
                v=to_float(n)
                if v is not None and 0 < v <= 5:
                    out[key]=v; break
            if key in out: break
    if out.get('management_fee') is not None and out.get('custodian_fee') is not None:
        out['expense']=out['management_fee']+out['custodian_fee']
        out['expense_source']='投信官方頁：經理費+保管費'
    return out

def _official_yuanta_etf_fallback(symbol):
    """V2.10.41：元大 ETF 官方頁 fallback；嚴格避免錯誤抓到頁面其他數字。"""
    code=str(symbol or '').upper().replace('.TW','').replace('.TWO','')
    if not code: return {}
    out={}; headers={'User-Agent':'Mozilla/5.0 stock-alert/2.10.41','Accept-Language':'zh-TW,zh;q=0.9'}
    base=f'https://www.yuantaetfs.com/product/detail/{code}'
    for suffix in ('/Basic_information','/ratio'):
        try:
            r=requests.get(base+suffix,timeout=7,headers=headers,verify=False); r.raise_for_status()
            text=html.unescape(re.sub(r'\s+',' ',r.text))
            # 只有在「基金每單位淨值」附近抓值，且 NAV 必須落在合理 ETF 範圍。
            for pat,key in [
                (r'基金每單位淨值\s*\(新台幣\).*?(?:NTD|NT\$|新台幣)?\s*\$?\s*([0-9,]+(?:\.[0-9]+)?)','nav'),
                (r'基金資產總淨值\s*\(新台幣\).*?(?:NTD|NT\$|新台幣)?\s*\$?\s*([0-9,]+(?:\.[0-9]+)?)','assets')]:
                m=re.search(pat,text,re.I)
                if m:
                    v=to_float(m.group(1))
                    if key=='nav' and v is not None and 0<v<10000: out[key]=v
                    elif key=='assets' and v is not None and v>0: out[key]=v
            for label,key,hi in [('近一年現金股息率','yield',30),('近一年貝他係數','beta',5)]:
                m=re.search(re.escape(label)+r'.{0,80}?(-?[0-9]+(?:\.[0-9]+)?)',text,re.I)
                if m:
                    v=to_float(m.group(1))
                    if v is not None and -5<=v<=hi: out[key]=v
            ex=_official_expense_regex(text)
            for k,v in ex.items():
                if out.get(k) is None: out[k]=v
        except Exception as e:
            print(f'V2.10.41 元大 ETF fallback失敗 {symbol} {suffix}: {type(e).__name__}',flush=True)
    return out

def _official_cathay_etf_fallback(symbol):
    """V2.10.40：國泰 ETF 官方專屬頁 + 申購買回清單 fallback。"""
    code=str(symbol or '').upper().replace('.TW','').replace('.TWO','')
    if not code: return {}
    out={}; headers={'User-Agent':'Mozilla/5.0 stock-alert/2.10.39','Accept-Language':'zh-TW,zh;q=0.9'}
    urls=[]
    if code=='00878':
        urls=[
            'https://www.cathaysite.com.tw/proj/SPOCathayETF/00878/',
            'https://www.cathaysite.com.tw/ETF/purchase?code=CN&lang=zh_TW&name=%E5%9C%8B%E6%B3%B0%E5%8F%B0%E7%81%A3ESG%E6%B0%B8%E7%BA%8C%E9%AB%98%E8%82%A1%E6%81%AFETF%E5%9F%BA%E9%87%91'
        ]
    for url in urls:
        try:
            r=requests.get(url,timeout=7,headers=headers,verify=False); r.raise_for_status()
            text=html.unescape(re.sub(r'\s+',' ',r.text))
            m=re.search(r'基金淨資產價值\(元\).*?NT\$?\s*([0-9,]+(?:\.[0-9]+)?)',text,re.I)
            if m: out['assets']=to_float(m.group(1))
            m=re.search(r'每受益權單位淨資產價值\(元\).*?NT\$?\s*([0-9]+(?:\.[0-9]+)?)',text,re.I)
            if m: out['nav']=to_float(m.group(1))
            m=re.search(r'經理費.{0,180}?([0-9]+(?:\.[0-9]+)?)\s*%',text,re.I)
            if m:
                mg=to_float(m.group(1));
                if mg is not None and 0<mg<=1: out['management_fee']=mg
            m=re.search(r'保管費.{0,180}?([0-9]+(?:\.[0-9]+)?)\s*%',text,re.I)
            if m:
                cg=to_float(m.group(1));
                if cg is not None and 0<cg<=1: out['custodian_fee']=cg
            if out.get('management_fee') is not None and out.get('custodian_fee') is not None:
                out['expense']=out['management_fee']+out['custodian_fee']; out['expense_source']='國泰投信官方經理費+保管費'
        except Exception as e:
            print(f'國泰 ETF 官方 fallback失敗 {symbol}: {type(e).__name__}',flush=True)
    if out.get('expense') is None:
        for url in urls:
            try:
                r=requests.get(url,timeout=6,headers=headers,verify=False); r.raise_for_status()
                ex=_official_expense_regex(r.text)
                if ex.get('expense') is not None: out.update(ex); break
            except Exception:
                pass
    return out


def _official_twse_etf_fallback(symbol):
    """V2.10.41：TWSE ETF e添富僅作資產規模/NAV備援，嚴格定位欄位。"""
    code=str(symbol or '').upper().replace('.TW','').replace('.TWO','')
    if not re.fullmatch(r'[0-9A-Z]{4,6}',code): return {}
    out={}
    try:
        r=requests.get(f'https://www.twse.com.tw/zh/ETFortune/etfInfo/{code}',timeout=7,headers={'User-Agent':'Mozilla/5.0 stock-alert/2.10.41'},verify=False); r.raise_for_status()
        text=html.unescape(re.sub(r'\s+',' ',r.text))
        m=re.search(r'資產規模.{0,120}?([0-9,]+(?:\.[0-9]+)?)\s*億元',text)
        if m:
            v=to_float(m.group(1));
            if v is not None and v>0: out['assets']=v*1e8
        for pat in [r'基金每單位淨值.{0,100}?([0-9]+(?:\.[0-9]+)?)',r'每受益權單位淨資產價值.{0,100}?([0-9]+(?:\.[0-9]+)?)']:
            m=re.search(pat,text,re.I)
            if m:
                v=to_float(m.group(1))
                if v is not None and 0<v<10000: out['nav']=v; break
    except Exception as e: print(f'V2.10.41 TWSE ETF 官方 fallback失敗 {symbol}: {type(e).__name__}',flush=True)
    return out

def _etf_beta_from_history(symbol):
    """V2.10.40：ETF Beta 最後獨立數學備援，對台股 ETF 以 ^TWII 計算。"""
    try:
        hist=yf.download([symbol,'^TWII'],period='1y',interval='1d',auto_adjust=True,progress=False,threads=False)
        if hist is None or hist.empty: return None
        close=hist.get('Close') if isinstance(hist.columns,pd.MultiIndex) else hist
        if close is None: return None
        if isinstance(close,pd.Series): return None
        cols=[c for c in close.columns]
        etf_col=next((c for c in cols if str(c)==str(symbol)),None)
        mkt_col=next((c for c in cols if str(c)=='^TWII'),None)
        if etf_col is None or mkt_col is None: return None
        r=pd.concat([close[etf_col].pct_change(),close[mkt_col].pct_change()],axis=1).dropna()
        if len(r)<60: return None
        cov=np.cov(r.iloc[:,0],r.iloc[:,1],ddof=1)[0,1]; var=np.var(r.iloc[:,1],ddof=1)
        return float(cov/var) if var>0 else None
    except Exception:
        return None

def yahoo_etf_profile(symbol):
    """V2.10.41：ETF 多源資料；官方 NAV/premium 優先，異常值一律丟棄。"""
    key=('etf_profile_v21041',symbol)
    if key in RUN_CACHE: return RUN_CACHE[key]
    out={'price':None,'nav':None,'yield':None,'expense':None,'beta':None,'assets':None,'expense_source':None,'premium':None}
    def raw(sec,k):
        x=(sec or {}).get(k)
        if isinstance(x,dict): x=x.get('raw',x.get('fmt'))
        return to_float(x)
    try:
        url='https://query1.finance.yahoo.com/v10/finance/quoteSummary/'+str(symbol)
        r=requests.get(url,params={'modules':'price,summaryDetail,defaultKeyStatistics,fundProfile'},timeout=5,headers={'User-Agent':'Mozilla/5.0 stock-alert/2.10.41'}); r.raise_for_status()
        q=((r.json().get('quoteSummary') or {}).get('result') or [{}])[0]
        out['price']=raw(q.get('price'),'regularMarketPrice'); out['nav']=raw(q.get('price'),'navPrice')
        y=raw(q.get('summaryDetail'),'dividendYield'); out['yield']=y*100 if y is not None and abs(y)<=1.5 else y
        out['beta']=raw(q.get('defaultKeyStatistics'),'beta') or raw(q.get('summaryDetail'),'beta')
        fp=q.get('fundProfile') or {}; out['assets']=raw(fp,'totalAssets') or raw(fp,'totalNetAssets')
        for sec in (q.get('summaryDetail') or {},q.get('fundProfile') or {},q.get('defaultKeyStatistics') or {}):
            for k,v in (sec.items() if isinstance(sec,dict) else []):
                if any(x in str(k).lower() for x in ('expense','feeexpense','managementfee')):
                    rv=raw(sec,k); ev=rv*100 if rv is not None and 0<=rv<1 else rv
                    if ev is not None and 0<=ev<=10: out['expense']=ev; out['expense_source']=k; break
            if out['expense'] is not None: break
    except Exception as e: print(f'V2.10.41 ETF quoteSummary失敗 {symbol}: {type(e).__name__}',flush=True)
    try:
        info=(yf.Ticker(symbol).info or {})
        for k,ik in [('price','regularMarketPrice'),('nav','navPrice'),('assets','totalAssets')]:
            if out.get(k) is None: out[k]=to_float(info.get(ik))
        if out['yield'] is None:
            y=to_float(info.get('dividendYield')); out['yield']=y*100 if y is not None and abs(y)<=1.5 else y
        if out['expense'] is None:
            for k in ('annualReportExpenseRatio','netExpenseRatio','grossExpenseRatio','totalExpenseRatio','feesExpensesInvestment'):
                v=to_float(info.get(k)); ev=v*100 if v is not None and 0<=v<1 else v
                if ev is not None and 0<=ev<=10: out['expense']=ev; out['expense_source']=k; break
        if out['beta'] is None: out['beta']=to_float(info.get('beta3Year')) or to_float(info.get('beta'))
    except Exception as e: print(f'V2.10.41 ETF yfinance備援失敗 {symbol}: {type(e).__name__}',flush=True)

    is_tw=str(symbol).upper().endswith(('.TW','.TWO'))
    if is_tw:
        # 官方 TWSE feed 直接提供 e=price, f=NAV, g=premium；只要官方有值就覆蓋 Yahoo。
        twse=_twse_etf_nav_fallback(symbol)
        for k in ('price','nav','assets'):
            if twse.get(k) is not None: out[k]=twse[k]
        if twse.get('premium') is not None: out['premium']=twse['premium']
        # 投信官方只補缺欄位，絕不覆蓋已確認的 TWSE NAV。
        for src in (_official_yuanta_etf_fallback(symbol),_official_cathay_etf_fallback(symbol),_official_twse_etf_fallback(symbol)):
            for k in ('nav','assets','beta','yield','expense'):
                if out.get(k) is None and src.get(k) is not None: out[k]=src[k]
            if src.get('expense_source') and out.get('expense_source') is None: out['expense_source']=src['expense_source']
        prof=yahoo_tw_profile_fallback(symbol)
        for k in ('assets','beta'):
            if out.get(k) is None and prof.get(k) is not None: out[k]=prof[k]
        if out['expense'] is None and prof.get('expense') is not None: out['expense']=prof['expense']; out['expense_source']=prof.get('expense_source')
        if out['yield'] is None:
            y=yahoo_tw_dividend_fallback(symbol,out.get('price'))
            if y is not None: out['yield']=y

    # 合理性清洗，避免 parser 產生 3.00 NAV、7.00 beta、3000% premium。
    if out.get('nav') is not None and not (0<out['nav']<10000): out['nav']=None
    if out.get('price') is not None and not (0<out['price']<100000): out['price']=None
    if out.get('premium') is not None and not (-50<=out['premium']<=50): out['premium']=None
    if out.get('yield') is not None and not (0<=out['yield']<=30): out['yield']=None
    if out.get('beta') is not None and not (-5<=out['beta']<=5): out['beta']=None
    if out.get('expense') is not None and not (0<=out['expense']<=10): out['expense']=None; out['expense_source']=None
    if out.get('premium') is None and out.get('price') and out.get('nav') and out['nav']>0:
        prem=(out['price']/out['nav']-1)*100
        if -50<=prem<=50: out['premium']=prem
    if out.get('beta') is None:
        b=_etf_beta_from_history(symbol)
        if b is not None and math.isfinite(b) and -5<=b<=5: out['beta']=b
    RUN_CACHE[key]=out
    return out

def score_etf(tech,p):
    score=0.0; avail=0.0; reasons=[]
    if tech.get('rsi') is not None:
        avail+=20; r=tech['rsi']; score+=20 if 40<=r<=60 else 15 if 30<=r<40 or 60<r<=70 else 8 if 25<=r<30 or 70<r<=75 else 3
    if tech.get('k') is not None and tech.get('d') is not None:
        avail+=15; score+=15 if tech['k']>tech['d'] and tech['k']<80 else 10 if tech['k']>=tech['d'] else 5
    if tech.get('ma20') is not None and tech.get('price') is not None:
        avail+=12; score+=12 if tech['price']>=tech['ma20'] else 5
    if tech.get('ma60') is not None and tech.get('price') is not None:
        avail+=13; score+=13 if tech['price']>=tech['ma60'] else 5
    if p.get('yield') is not None:
        avail+=12; y=p['yield']; score+=12 if y>=4 else 9 if y>=2 else 5
        if y>=4: reasons.append('殖利率具吸引力')
    if p.get('beta') is not None:
        avail+=8; b=p['beta']; score+=8 if b<=1 else 6 if b<=1.2 else 4
    if p.get('assets') is not None:
        avail+=10; score+=10 if p['assets']>=1e10 else 7 if p['assets']>=1e9 else 4
    if avail<=0: return 0,reasons
    raw=score/avail*100; cap=100 if avail>=80 else 85 if avail>=60 else 70
    return int(round(min(raw,cap))),reasons

def etf_analysis(query):
    info=resolve_etf_query(query)
    if not info: return f'❌ 找不到 ETF：{query}'
    symbol=info['symbol']; code=next((k for k,v in ETF_MAP.items() if v is info), str(query).upper())
    tech=technical(symbol)
    tp=to_float(tech.get('price')); ma20=to_float(tech.get('ma20')); ma60=to_float(tech.get('ma60'))
    bad_price=(tp is None) or (ma20 is not None and ma20>0 and abs(tp/ma20-1)>0.25) or (ma60 is not None and ma60>0 and abs(tp/ma60-1)>0.25)
    if bad_price:
        d=yahoo_chart_daily_fallback(symbol,'6mo')
        if d is not None and not d.empty: tech=_technical_from_df(d)
    p=yahoo_etf_profile(symbol)
    price=to_float(tech.get('price')) or to_float(p.get('price')); nav=p.get('nav')
    premium=to_float(p.get('premium'))
    if premium is None and price and nav and nav>0:
        x=(price/nav-1)*100
        premium=x if -50<=x<=50 else None
    score,reasons=score_etf(tech,p)
    verdict='🟢 可分批配置' if score>=75 else '🟡 等待回檔/止跌' if score>=60 else '🟠 暫緩配置' if score>=40 else '🔴 不建議配置'
    return (f'📊 ETF配置分析 V2.10.49\n\n標的：{info["name"]}（{code}）\n代號：{symbol}\n\n'
            f'【ETF特性 40分】\nNAV：{fmt(nav)}\n溢價/折價：{fmt(premium)}%\n殖利率：{fmt(p.get("yield"))}%\nBeta：{fmt(p.get("beta"))}\n資產規模：{fmt(p.get("assets"),0)}\n\n'
            f'【技術面 60分】\n價格：{fmt(price)}\nRSI：{fmt(tech.get("rsi"))}\nKD：K={fmt(tech.get("k"))} / D={fmt(tech.get("d"))}\nMA20：{fmt(tech.get("ma20"))}\nMA60：{fmt(tech.get("ma60"))}\n趨勢：{tech.get("trend") or "N/A"}\n\n'
            f'【ETF綜合評分】\n綜合評分：{score}/100\n結論：{verdict}\n加分因素：{"、".join(reasons) if reasons else "無"}')

def analysis(
    query,
    u,
    backfill=True,
    interval_result=None,
    line_light=False,
    force_technical_refresh=False
):
    """V2.10.73：可由背景 LINE 分析單獨要求技術面即時刷新。

    force_technical_refresh=True 僅影響 technical()；其餘基本面、籌碼
    與既有 line_light 快取流程保持不變。
    """

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

    # V2.10.28：LINE Free 不掃同產業 1985 檔；但若「目標股自己」沒有次產業快取，
    # 允許一次短 timeout 官方查詢，成功後立刻寫入快取。這解決 1101 等首次查詢永遠顯示 N/A。
    if not subindustries:
        if line_light:
            try:
                data = fetch_value_chain_for_stock_fast(code)
                if data and data.get('subindustries'):
                    subindustries = data['subindustries']
                    item['subindustries'] = list(dict.fromkeys(subindustries))
                    item['subindustry'] = subindustries[0]
                    SUBINDUSTRY_CACHE[code] = data
                    print(f'LINE次產業：單股快速補抓成功 {code} → {", ".join(subindustries)}', flush=True)
            except Exception as e:
                print(f'LINE次產業：單股快速補抓失敗 {code}：{type(e).__name__}', flush=True)
        else:
            subindustries = ensure_subindustry_for_query(code, item)

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
        else '次產業資料未快取（LINE不即時查詢）'
    )

    valuation_subindustry = subindustries[0] if subindustries else ''

    # --------------------------------------------------------
    # PE
    # --------------------------------------------------------

    print(f'LINE輕量分析：取得目前PE {code}' if line_light else '取得目前PE', flush=True)
    pe_data = get_current_pe_data()

    h = load_json(
        PE_HISTORY_FILE
    )

    # V2.10.26：Render 本機沒有 pe_history.json 時，改讀 GitHub Actions
    # 已產生的遠端全市場 PE 歷史快取；仍然不對 TWSE/TPEx 逐日即時回補。
    if line_light and (not isinstance(h, dict) or not h):
        remote_pe = load_remote_json_cache(PE_HISTORY_FILE, timeout=4)
        if isinstance(remote_pe, dict) and remote_pe:
            h = remote_pe
            print(
                f'LINE PE歷史：使用 GitHub 全市場快取 {len(h)} 檔',
                flush=True
            )

    if backfill and not line_light:

        h = backfill_pe(
            code,
            h,
            market
        )

        save_json(
            PE_HISTORY_FILE,
            h
        )
    elif backfill and line_light:
        # V2.10.25：Render Free 不對「沒有歷史 PE 快取」的任意股票
        # 往前逐日呼叫 TWSE/TPEX。否則 1101 這類首次查詢會卡在
        # PE歷史回補，最多搜尋 370 個曆日。只使用 Actions 已存在的歷史資料。
        cached_valid = sum(
            1 for v in h.get(code, {}).values()
            if to_float(v) is not None and 0 < to_float(v) <= PE_MAX_VALID
        )
        print(
            f'LINE PE歷史：只讀快取 {code}，有效 {cached_valid} 筆，不進行即時回補',
            flush=True
        )

    # 官方 PE/PB/殖利率資料先取出，再交給 LINE 輕量基本面路徑。
    # V2.10.19 修正：原本 off 在 yahoo_light_fund() 呼叫後才建立，
    # 導致 LINE 查詢出現 UnboundLocalError。
    off = pe_data.get(code, {})

    # V2.10.28：LINE 任意 TPEX 股票若不在 GitHub PE 快取，
    # 只對「這一檔」做一次 TPEx 官方網頁 JSON 查詢，不抓全市場。
    if line_light and market == 'TPEX' and any(to_float(off.get(k)) is None for k in ('pe','pb','yield')):
        try:
            one_tpex = parse_tpex_web_peratio(tpex_web_peratio_data(timeout=4)).get(code) or {}
            for k in ('pe','pb','yield'):
                if to_float(off.get(k)) is None and to_float(one_tpex.get(k)) is not None:
                    off[k]=to_float(one_tpex.get(k))
            if one_tpex:
                print(f'LINE PE：TPEx 官方網頁逐欄位補到 {code} {off}', flush=True)
        except Exception as e:
            print(f'LINE PE：TPEx 單股官方備援失敗 {code}：{type(e).__name__}', flush=True)

    if line_light:
        print(f'LINE輕量分析：基本面資料 {code}', flush=True)
        yf_f = official_fundamental(symbol, off, current_price=to_float(u.get(code, {}).get('price')), market=market, industry=industry, subindustry=valuation_subindustry)
    else:
        yf_f = official_fundamental(
            symbol, off, current_price=to_float(u.get(code, {}).get('price')), market=market, industry=industry, subindustry=valuation_subindustry
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

    # V2.10.56：官方歷史 PE 優先；若有效樣本不足 20，才使用明確標示的價格/EPS proxy。
    # Proxy 只作「缺資料時的最後備援」，不覆蓋官方有效歷史 PE。
    one, sample = one_year_pe(code, h)
    one_label = ''
    if one is None and pe is not None and to_float(pe) is not None:
        try:
            proxy, proxy_sample, proxy_label = one_year_pe_proxy(code, pe, symbol)
            if proxy is not None:
                one, sample, one_label = proxy, proxy_sample, proxy_label
        except Exception as e:
            print(f'一年平均PE proxy處理失敗 {code}：{type(e).__name__}', flush=True)

    # --------------------------------------------------------
    # V2.9.8
    # 動態次產業 Top 10
    # --------------------------------------------------------

    print(f'LINE輕量分析：建立同次產業 Top10 {code}', flush=True) if line_light else None
    if line_light and not subindustries:
        # V2.10.25：次產業未快取時，完全禁止即時補抓。
        # 改用同大產業、市值 Top10，讓 1101 等任意股票仍可完成分析。
        peers = []
        for c, x in u.items():
            if clean_code(c) == code:
                continue
            if canonical_industry(x.get('industry')) != canonical_industry(industry):
                continue
            if to_float(x.get('market_cap')) is None:
                continue
            peers.append(x)
        peers.sort(key=lambda x: to_float(x.get('market_cap')) or 0, reverse=True)
        peers = peers[:10]
        peer_mode = '同大產業 Top 10（次產業快取不足）'
    else:
        peers = get_dynamic_subindustry_peers(
            code,
            industry,
            subindustries,
            u,
            10
        )
        peer_mode = '動態次產業 Top 10'

    vals = []
    for peer_item in peers:
        # V2.10.47：LINE 輕量模式與一般模式統一同業 PE fallback。
        peer_pe = _v21045_peer_pe_fallback(peer_item, pe_data)
        if peer_pe is not None and 0 < peer_pe <= PE_MAX_VALID:
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
    # V2.10.73：LINE 背景網頁分析即使採用 line_light=True，
    # 技術面仍強制抓取最新資料並重算，避免沿用舊技術快取。
    # 其他既有 line_light 路徑維持原本快取邏輯，不影響跌幅警報等流程。
    tech = technical(
        symbol,
        force_refresh=(not line_light) or force_technical_refresh
    )

    # V2.10.71：不要再用 market_universe_cache.json 的 price 覆蓋 technical()。
    # 背景分析 force_refresh=True 時，technical() 已經用最新 5 分鐘價格
    # 強制重算趨勢；股票池價格僅供其他非技術用途，不能回寫 tech['price']。

    # --------------------------------------------------------
    # Chips
    # --------------------------------------------------------

    print(f'LINE輕量分析：籌碼面 {code}', flush=True) if line_light else None
    if line_light:
        _chip_rows = _line_chip_fast(code, market, 20)
        if _chip_rows:
            _chip_item = _chip_rows[0].get('data', {}).get(code, {})
            _chip_cache = load_json(LINE_CHIP_SUMMARY_CACHE_FILE)
            _chip_summary = (_chip_cache.get(market, {}).get(code, {})
                             if isinstance(_chip_cache, dict) else {})
            inst = {
                'latest': _chip_summary.get('latest'),
                '5d': _chip_summary.get('5d'),
                '20d': _chip_summary.get('20d')
            }
        else:
            inst = {'latest': None, '5d': None, '20d': None}
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
        INDUSTRY_MODEL.get(industry, DEFAULT_MODEL),
        valuation_growth=yf_f.get('valuation_growth')
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
            '⚠️ 無次產業快取，已改用同大產業市值 Top 10'
        )

    else:

        peer_text = (
            '⚠️ 找不到相同次產業且有市值資料的股票'
        )

    # --------------------------------------------------------
    # Output
    # --------------------------------------------------------

    return (
        f'📊 股票加碼分析 V2.10.96\n\n'
        f'標的：{name}（{code}）\n'
        f'市場：{market}\n'
        f'產業：{industry}\n'
        f'次產業：{subindustry_display}\n\n'

        f'【估值 / 基本面 40分】\n'
        f'估值模型：{INDUSTRY_MODEL.get(industry, DEFAULT_MODEL).get("profile", "特殊型")}\n'
        f'PE：{fmt(pe)}\n'
        f'一年平均PE：{fmt(one)}'
        f'（樣本 {sample}{("，" + one_label) if one_label else ""}）\n'
        f'{"同次產業" if subindustries else "同大產業"}Top10平均PE：'
        f'{fmt(peer_mean)}\n'
        f'{"同次產業" if subindustries else "同大產業"}Top10中位數PE：'
        f'{fmt(peer_med)}'
        f'（有效 {len(vals)}/10）\n'
        f'PB：{fmt(pb)}\n'
        f'殖利率：{fmt(yld)}%\n'
        f'EPS成長率：{fmt(yf_f["eps_growth"])}%\n'
        f'{_format_eps_model_summary(yf_f.get("eps_model_detail"))}\n'
        f'PEG：{fmt(yf_f["peg"])}\n'
        f'ROE：{fmt(yf_f["roe"])}%\n'
        f'基本面得分：{fs}/40\n'
        f'本產業配分：PE {INDUSTRY_MODEL.get(industry, DEFAULT_MODEL).get("weights",{}).get("pe",0)}、'
        f'PEG {INDUSTRY_MODEL.get(industry, DEFAULT_MODEL).get("weights",{}).get("peg",0)}、'
        f'PB {INDUSTRY_MODEL.get(industry, DEFAULT_MODEL).get("weights",{}).get("pb",0)}、'
        f'殖利率 {INDUSTRY_MODEL.get(industry, DEFAULT_MODEL).get("weights",{}).get("yield",0)}、'
        f'ROE {INDUSTRY_MODEL.get(industry, DEFAULT_MODEL).get("weights",{}).get("roe",0)}、'
        f'成長 {INDUSTRY_MODEL.get(industry, DEFAULT_MODEL).get("weights",{}).get("growth",0)}\n\n'

        f'【{peer_mode}】\n'
        f'{peer_text}\n\n'

        f'【技術面 30分】\n'
        f'最新價格：{fmt(tech.get("price"))}\n'
        f'價格來源：{tech.get("price_source") or "N/A"}\n'
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
    """主動通知用 Push API。僅供系統主動警報使用，不用於 LINE 查詢結果。"""
    if not LINE_TOKEN or not to:
        print('LINE Push 略過：缺少 LINE token 或聊天室 ID')
        return False

    try:
        messages = [
            {'type': 'text', 'text': x}
            for x in _line_text_messages(msg)[:5]
        ]

        payload={'to':to,'messages':messages}
        last=None
        for attempt in range(1,4):
            try:
                r=requests.post(LINE_PUSH_URL,headers=_line_headers(),json=payload,timeout=12)
                if r.status_code==200:
                    print(f'LINE Push成功：{to[:12]}...（第{attempt}次）')
                    return True
                last=f'{r.status_code} {r.text[:500]}'
                # V2.10.40：月額度 429 不再重試；重試不可能解決額度問題。
                if r.status_code == 429 and 'monthly limit' in r.text.lower():
                    print(f'LINE Push月額度已用完：{last}', flush=True)
                    return False
                if r.status_code not in (429,500,502,503,504): break
                time.sleep(min(2*attempt,4))
            except Exception as e:
                last=f'{type(e).__name__}: {e}'
                if attempt<3: time.sleep(attempt)
        print(f'LINE Push失敗：{last}')
        return False

    except Exception as e:
        print('LINE Push例外：', e)
        return False


def _render_base_url():
    """取得 Render 對外網址；優先使用環境變數，避免把內部 host 放進 LINE。"""
    base = (
        os.environ.get('RENDER_EXTERNAL_URL')
        or os.environ.get('PUBLIC_BASE_URL')
        or ''
    ).strip().rstrip('/')
    if base:
        return base
    host = os.environ.get('RENDER_EXTERNAL_HOSTNAME', '').strip()
    if host:
        return f'https://{host}'
    return ''


def _new_line_result_id(event_id=None, text=''):
    seed = f'{event_id or ""}|{text}|{time.time_ns()}'
    return hashlib.sha256(seed.encode('utf-8')).hexdigest()[:16]


def _create_line_result(text, event_id=None):
    """建立 LINE A 方案結果頁；回傳 result_id 與完整網址。"""
    rid = _new_line_result_id(event_id, text)
    now = time.time()
    with LINE_RESULT_LOCK:
        LINE_RESULT_CACHE[rid] = {
            'text': text,
            'status': 'running',
            'result': None,
            'created_at': now,
            'updated_at': now,
            'event_id': event_id,
        }
        if len(LINE_RESULT_CACHE) > LINE_RESULT_MAX:
            oldest = sorted(LINE_RESULT_CACHE.items(), key=lambda kv: kv[1].get('created_at', 0))
            for old_id, _ in oldest[:max(1, len(oldest)-LINE_RESULT_MAX)]:
                LINE_RESULT_CACHE.pop(old_id, None)
    base = _render_base_url()
    url = f'{base}/line-result/{rid}' if base else f'/line-result/{rid}'
    return rid, url


def _update_line_result(rid, status, result=None):
    with LINE_RESULT_LOCK:
        item = LINE_RESULT_CACHE.get(rid)
        if not item:
            return
        item['status'] = status
        item['result'] = result
        item['updated_at'] = time.time()


def _get_line_result(rid):
    with LINE_RESULT_LOCK:
        item = LINE_RESULT_CACHE.get(rid)
        return dict(item) if item else None



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


def _background_line_analysis(text, target, u, event_id=None, result_id=None):
    """V2.10.40：LINE A 方案背景分析。

    完整結果不再 Push；分析完成後寫入 Render /line-result/<id>。
    使用者收到的 Reply 只包含結果頁網址，因此主動查詢不消耗 Push 月額度。
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
            etf=resolve_etf_query(text)
            if etf:
                print(f'LINE背景分析：辨識為 ETF {etf["symbol"]}，跳過1985檔股票池', flush=True)
                result=etf_analysis(text)
            else:
                query_u = u if isinstance(u, dict) and u else build_line_query_universe(text)
                print(f'LINE背景分析：市場資料完成 {len(query_u)} 檔', flush=True)
                print('LINE背景分析：同步次產業', flush=True)
                query_u = prepare_line_subindustries(query_u, text)
                print('LINE背景分析：開始 LINE 輕量查詢專用分析', flush=True)
                result = analysis(
                    text,
                    query_u,
                    True,
                    line_light=True,
                    force_technical_refresh=True
                )
            print('LINE背景分析：輕量分析完成', flush=True)

        if not result:
            result = f'❌ {text} 分析沒有產生結果。'

        if result_id:
            _update_line_result(result_id, 'done', result)
            print(f'✅ LINE背景分析完成：{text} | 結果頁={result_id}', flush=True)
        else:
            print(f'⚠️ LINE背景分析完成但沒有 result_id：{text}', flush=True)

    except Exception as e:
        traceback.print_exc()
        err = f'❌ {text} 分析失敗：{e}'
        if result_id:
            _update_line_result(result_id, 'error', err)
        print(f'❌ LINE背景分析例外：{type(e).__name__}: {e}', flush=True)
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

    Render Free 不建立完整 1985 檔次產業快取；只讀 Actions 已發布的
    次產業快取。缺少時不呼叫 TPEx/TWSE，避免任意股票查詢被外部
    SSL/timeout 卡住；analysis() 會改用同大產業市值 Top 10。
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
        f'快取缺少={len(missing)}'
    )

    # V2.10.25 核心修正：Render Free 絕不補抓次產業。
    # 1101 這類不在目前 Actions 目標大產業快取的股票，若在這裡
    # 呼叫 ic.tpex.org.tw，SSL/timeout 會把整個背景工作卡住。
    # Actions 負責慢速建立快取；LINE 只讀既有快取，缺少時交給
    # analysis() 使用「同大產業 Top10」備援。
    if missing:
        print(
            'LINE次產業同步：缺少資料不即時補抓，交由 LINE 輕量分析走同大產業備援',
            flush=True
        )

    global SUBINDUSTRY_CACHE
    SUBINDUSTRY_CACHE = data
    return attach_subindustries(u, data)


def build_line_query_universe(query):
    """V2.10.23：LINE 查詢專用市場資料。

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

    # V2.10.23：Render 冷啟動優先讀 Actions 提交的 GitHub 市場快取。
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
    """V2.10.40：LINE A 方案。Reply 只回覆結果頁網址，完整分析不上 Push。"""
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
            '📈 股票加碼分析 Bot V2.10.40\n\n'
            '輸入股票代號、股票名稱或 ETF 代號即可查詢。\n'
            '例如：2330、台積電、3711、日月光投控、0050、00878、QQQ\n\n'
            '股票：基本面40 + 技術30 + 籌碼20 + 風險10。\n'
            'ETF：ETF特性40 + 技術60。\n'
            '查詢結果會立即回覆 Render 分析頁網址，完整分析不使用 LINE Push。'
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

    # V2.10.40：先建立結果頁，再用一次 Reply 回覆網址。
    # 完整分析在背景執行；完成後直接更新結果頁，不需要 Push。
    result_id, result_url = _create_line_result(text, event_id)
    ok = reply_line(
        token,
        f'🔎 收到「{text}」\n\n'
        '⏳ 分析已開始。\n'
        '完整結果會直接更新到下面的分析頁，不需要等待 LINE Push：\n\n'
        f'{result_url}'
    )

    if not ok:
        print('⚠️ LINE 結果頁網址 Reply 失敗；背景分析仍會繼續。', flush=True)

    try:
        future = LINE_ANALYSIS_EXECUTOR.submit(
            _background_line_analysis,
            text, target, u, event_id, result_id
        )
        print(
            f'LINE背景工作已提交：{text} | done={future.done()} | '
            f'executor=max_workers=1 | result_id={result_id}',
            flush=True
        )
    except Exception as e:
        err = f'❌ {text} 無法啟動分析工作：{e}'
        _update_line_result(result_id, 'error', err)
        print(
            f'❌ LINE背景工作啟動失敗：{type(e).__name__}: {e}',
            flush=True
        )



def run_webhook_server():
    from flask import Flask, request

    app = Flask(__name__)

    print('================================')
    print('LINE Webhook Server V2.10.28')
    print('模式：LINE A 方案｜Reply 結果頁網址 + 背景分析 + Render 完整結果頁｜查詢不 Push')
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
        return 'stock_alert V2.10.37 OK', 200

    @app.get('/health')
    def health2():
        return 'OK', 200

    @app.get('/line-result/<rid>')
    def line_result_page(rid):
        # V2.10.40：LINE A 方案完整分析頁。Render instance 記憶體中的結果
        # 會在背景分析完成後更新；若服務重新部署，舊結果會失效。
        item = _get_line_result(rid)
        if not item:
            return (
                '<!doctype html><html><head><meta charset="utf-8">'
                '<meta name="viewport" content="width=device-width,initial-scale=1">'
                '<title>Stock Alert</title></head><body>'
                '<h2>找不到這筆分析</h2>'
                '<p>結果可能因 Render 重新部署而被清除，請重新查詢。</p>'
                '</body></html>', 404
            )

        status = item.get('status')
        text = html.escape(str(item.get('text') or ''))
        updated = datetime.fromtimestamp(item.get('updated_at', time.time()), TW_TZ).strftime('%Y-%m-%d %H:%M:%S')
        if status == 'running':
            body = (
                f'<h2>⏳ {text} 分析中</h2>'
                '<p>分析正在背景執行，請重新整理本頁查看最新結果。</p>'
                f'<p>最後更新：{updated}</p>'
                '<meta http-equiv="refresh" content="5">'
            )
        else:
            result = html.escape(str(item.get('result') or ''))
            body = (
                f'<h2>📊 {text} 分析結果</h2>'
                f'<p>最後更新：{updated}</p>'
                f'<pre style="white-space:pre-wrap;font-family:-apple-system,BlinkMacSystemFont,Segoe UI,sans-serif;line-height:1.55">{result}</pre>'
            )
            if status == 'error':
                body = '<h2>分析失敗</h2>' + body

        return (
            '<!doctype html><html><head><meta charset="utf-8">'
            '<meta name="viewport" content="width=device-width,initial-scale=1">'
            '<title>Stock Alert V2.10.40</title>'
            '<style>body{margin:0;padding:20px;background:#f6f7f9;color:#222}'
            '.card{max-width:900px;margin:auto;background:#fff;border-radius:14px;padding:20px;box-shadow:0 2px 12px #0001}'
            'a{word-break:break-all}</style></head><body><div class="card">'
            + body +
            '</div></body></html>', 200
        )

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
    """V2.10.23：建立 LINE 專用小型快取。

    重要修正：
    1. 法人摘要不再重新讀 CHIP_HISTORY_FILE。
    2. 直接使用本次 Actions 已經由 institutional() 取得的
       INSTITUTIONAL_CACHE。
    3. institutional() 的 T86 每日資料本身就是全市場資料，
       因此可在記憶體中直接壓成「每檔一筆 latest/5d/20d」。
    4. 這樣可以確保 Actions 一定產生 line_chip_summary_cache.json，
       LINE Render 不必重新抓 T86。
    """
    # --------------------------------------------------------
    # PE
    # --------------------------------------------------------
    try:
        pe = RUN_CACHE.get('current_pe', {})
        if not isinstance(pe, dict) or not pe:
            pe = get_current_pe_data()

        if isinstance(pe, dict) and pe:
            _save_line_small_cache(
                LINE_PE_CACHE_FILE,
                {
                    '_cached_at': time.time(),
                    'data': pe
                }
            )
            print(
                f'LINE PE 快取完成：{len(pe)} 檔',
                flush=True
            )
    except Exception as e:
        print(
            f'LINE PE 快取建立失敗：{type(e).__name__}: {e}',
            flush=True
        )

    # --------------------------------------------------------
    # Margin
    # --------------------------------------------------------
    try:
        md = {}

        for key, value in MARGIN_CACHE.items():
            if (
                isinstance(key, tuple)
                and len(key) >= 2
                and key[0] == 'margin'
            ):
                market = str(key[1])
                md[market] = (
                    value
                    if isinstance(value, dict)
                    else {}
                )

        if md:
            _save_line_small_cache(
                LINE_MARGIN_CACHE_FILE,
                md
            )

            print(
                'LINE 融資快取完成：'
                + ', '.join(
                    f'{m} {len(v)} 檔'
                    for m, v in md.items()
                ),
                flush=True
            )
        else:
            print(
                'LINE 融資快取：本次沒有可用資料',
                flush=True
            )

    except Exception as e:
        print(
            f'LINE 融資快取建立失敗：'
            f'{type(e).__name__}: {e}',
            flush=True
        )

    # --------------------------------------------------------
    # Institutional
    # --------------------------------------------------------
    # V2.10.23 核心修正：
    # 不再 load_json(CHIP_HISTORY_FILE)。
    #
    # institutional() 已經把最近 20 個交易日的「全市場 T86」
    # 放進 INSTITUTIONAL_CACHE。
    #
    # key:
    #   ('inst', 'TWSE', 20)
    #   ('inst', 'TPEX', 20)
    #
    # value:
    #   [
    #       {'date': 'YYYYMMDD',
    #        'data': {股票代號: {'total': ...}, ...}},
    #       ...
    #   ]
    #
    # 直接從這裡壓縮成：
    #   {
    #       'TWSE': {
    #           '2330': {
    #               'latest': ...,
    #               '5d': ...,
    #               '20d': ...
    #           }
    #       }
    #   }
    try:
        summary = {}

        for key, rows in INSTITUTIONAL_CACHE.items():

            if not (
                isinstance(key, tuple)
                and len(key) >= 3
                and key[0] == 'inst'
            ):
                continue

            market = str(key[1])

            if not isinstance(rows, list) or not rows:
                continue

            # 只保留真正有 data 的交易日，並按照日期由新到舊。
            valid_rows = []

            for row in rows:
                if not isinstance(row, dict):
                    continue

                data = row.get('data')

                if not isinstance(data, dict) or not data:
                    continue

                valid_rows.append(row)

            valid_rows.sort(
                key=lambda x: str(x.get('date', '')),
                reverse=True
            )

            values = {}

            for row in valid_rows:
                data = row.get('data', {})

                for raw_code, item in data.items():

                    code = clean_code(raw_code)

                    if not code:
                        continue

                    if not isinstance(item, dict):
                        continue

                    total = item.get('total')

                    if total is None:
                        continue

                    try:
                        total = float(total)
                    except (TypeError, ValueError):
                        continue

                    values.setdefault(code, []).append(total)

            market_summary = {}

            for code, arr in values.items():

                if not arr:
                    continue

                market_summary[code] = {
                    'latest': arr[0],
                    '5d': (
                        sum(arr[:5])
                        if len(arr) >= 5
                        else None
                    ),
                    '20d': (
                        sum(arr[:20])
                        if len(arr) >= 20
                        else None
                    )
                }

            if market_summary:
                summary[market] = market_summary

        if summary:

            _save_line_small_cache(
                LINE_CHIP_SUMMARY_CACHE_FILE,
                summary
            )

            total = sum(
                len(v)
                for v in summary.values()
                if isinstance(v, dict)
            )

            print(
                f'LINE 法人摘要快取完成：'
                f'{total} 檔',
                flush=True
            )

            for market, data in summary.items():
                print(
                    f'LINE 法人摘要：'
                    f'{market} {len(data)} 檔',
                    flush=True
                )

        else:
            print(
                '⚠️ LINE 法人摘要快取：'
                'INSTITUTIONAL_CACHE 沒有可用資料',
                flush=True
            )

    except Exception as e:
        print(
            f'LINE 法人摘要快取建立失敗：'
            f'{type(e).__name__}: {e}',
            flush=True
        )
        traceback.print_exc()


# ============================================================
# Alerts
# ============================================================

def refresh_all_market_pe_history(pe_history, universe=None):
    """V2.10.56：只整理既有 PE 歷史快取，不呼叫任何歷史 PE API。

    目的：徹底消除 Actions 卡在 TWSE 428 / TPEx 520 / timeout 的可能性。
    現有 pe_history.json 完整保留；只做 metadata migration 與 370 天清理。
    目前 PE 若官方資料為 N/A，official_fundamental() 會再用股價 / TTM EPS
    計算，因此不再需要為了單一股票逐日回補歷史 PE。
    """
    if not isinstance(pe_history, dict):
        pe_history = {}

    today = datetime.now(TW_TZ).date()
    cutoff = today - timedelta(days=370)
    meta = pe_history.setdefault('_meta', {})
    coverage = meta.setdefault('coverage', {})
    fetch_status = meta.setdefault('fetch_status', {})
    markets = ('TWSE', 'TPEX')
    for market in markets:
        if not isinstance(coverage.get(market), dict):
            coverage[market] = {}
        if not isinstance(fetch_status.get(market), dict):
            fetch_status[market] = {}

    # 舊 cache migration：只標記已有有效 PE 的日期，不做網路請求。
    migrated = 0
    market_codes = {'TWSE': set(), 'TPEX': set()}
    if isinstance(universe, dict):
        for code, item in universe.items():
            if isinstance(item, dict) and item.get('market') in market_codes:
                market_codes[item['market']].add(clean_code(code))

    for market in markets:
        counts = {}
        codes = market_codes[market]
        source_codes = codes if codes else {
            clean_code(c) for c in pe_history
            if c != '_meta' and isinstance(pe_history.get(c), dict)
        }
        for code in source_codes:
            bucket = pe_history.get(code)
            if not isinstance(bucket, dict):
                continue
            for ds, value in bucket.items():
                if not isinstance(ds, str) or not re.fullmatch(r'\d{8}', ds):
                    continue
                try:
                    dd = datetime.strptime(ds, '%Y%m%d').date()
                except Exception:
                    continue
                if cutoff <= dd <= today:
                    pe = to_float(value)
                    if pe is not None and 0 < pe <= PE_MAX_VALID:
                        counts[ds] = counts.get(ds, 0) + 1
        for ds, count in counts.items():
            if ds not in coverage[market]:
                coverage[market][ds] = count
                migrated += 1
            fetch_status[market].setdefault(ds, 'success')

    removed = 0
    for code in list(pe_history.keys()):
        if code == '_meta':
            continue
        bucket = pe_history.get(code)
        if not isinstance(bucket, dict):
            continue
        for ds in list(bucket.keys()):
            try:
                dd = datetime.strptime(ds, '%Y%m%d').date()
            except Exception:
                continue
            if dd < cutoff:
                del bucket[ds]
                removed += 1
        if not bucket:
            pe_history.pop(code, None)

    for market in markets:
        for store in (coverage[market], fetch_status[market]):
            for ds in list(store.keys()):
                try:
                    dd = datetime.strptime(ds, '%Y%m%d').date()
                except Exception:
                    continue
                if dd < cutoff:
                    store.pop(ds, None)

    valid_stocks = 0
    for code, bucket in pe_history.items():
        if code == '_meta' or not isinstance(bucket, dict):
            continue
        if any(to_float(v) is not None and 0 < to_float(v) <= PE_MAX_VALID for v in bucket.values()):
            valid_stocks += 1

    print(
        f'全市場 PE 歷史快取：{valid_stocks} 檔，'
        f'本次 0 次歷史PE API、migration {migrated} 日期、清理 {removed} 筆；'
        f'V2.10.56 不再呼叫 TWSE/TPEx 歷史 PE API',
        flush=True
    )
    return pe_history


def _scan_high_score_stocks(u, state):
    """V2.10.74：全市場綜合評分 >= 90 分批次掃描。

    設計原則：
    1. 不對 1985 檔逐一執行完整 analysis()。
    2. 技術面直接讀 Actions 本次已建立的全市場技術快取。
    3. 法人／融資直接讀本次批次快取；PE／一年平均PE也只讀本次既有資料。
    4. 先計算「非基本面最高可得分」：技術 + 籌碼 + (10-風險)。
       若連基本面滿分40都不可能達到90，直接淘汰，不呼叫 Yahoo 基本面。
    5. 只有理論上可能 >=90 的候選股，才使用既有 official_fundamental() 補完整基本面。
    6. 通知只針對「今日首次 >=90」或「曾低於90後重新 >=90」的股票，
       同一次執行彙整成一則 LINE，避免每15分鐘洗版。
    """
    started = time.time()
    threshold = 90

    if not isinstance(u, dict) or not u:
        print('⚠️ V2.10.79 高評分掃描：股票池為空，跳過', flush=True)
        return []

    # --------------------------------------------------------
    # 一次載入本次 Actions 已建立的快取，避免逐股反覆 I/O。
    # --------------------------------------------------------
    pe_data = RUN_CACHE.get('current_pe')
    if not isinstance(pe_data, dict) or not pe_data:
        try:
            pe_data = get_current_pe_data()
        except Exception:
            pe_data = {}

    pe_history = load_json(PE_HISTORY_FILE)
    if not isinstance(pe_history, dict):
        pe_history = {}

    tech_cache = load_json(LINE_TECH_CACHE_FILE)
    if not isinstance(tech_cache, dict):
        tech_cache = {}

    chip_cache = load_json(LINE_CHIP_SUMMARY_CACHE_FILE)
    if not isinstance(chip_cache, dict):
        chip_cache = {}

    # 法人摘要快取若尚未落盤，直接由本次 INSTITUTIONAL_CACHE 壓縮一次。
    if not chip_cache:
        chip_cache = {}
        for key, rows in INSTITUTIONAL_CACHE.items():
            if not (isinstance(key, tuple) and len(key) >= 3 and key[0] == 'inst'):
                continue
            market = str(key[1])
            if not isinstance(rows, list):
                continue
            values = {}
            valid_rows = [r for r in rows if isinstance(r, dict) and isinstance(r.get('data'), dict)]
            valid_rows.sort(key=lambda x: str(x.get('date', '')), reverse=True)
            for row in valid_rows:
                for raw_code, item in row.get('data', {}).items():
                    code = clean_code(raw_code)
                    if not code or not isinstance(item, dict):
                        continue
                    v = to_float(item.get('total'))
                    if v is not None:
                        values.setdefault(code, []).append(v)
            md = {}
            for code, arr in values.items():
                if arr:
                    md[code] = {
                        'latest': arr[0],
                        '5d': sum(arr[:5]) if len(arr) >= 5 else None,
                        '20d': sum(arr[:20]) if len(arr) >= 20 else None,
                    }
            if md:
                chip_cache[market] = md

    # --------------------------------------------------------
    # 同業 PE：只使用既有官方 PE / 歷史 PE，不觸發任何網路查詢。
    # --------------------------------------------------------
    def cached_peer_pe(peer_item):
        code = clean_code(str(peer_item.get('code', '')))
        row = pe_data.get(code, {}) if isinstance(pe_data, dict) else {}
        v = to_float(row.get('pe')) if isinstance(row, dict) else None
        if v is not None and 0 < v <= PE_MAX_VALID:
            return v
        bucket = pe_history.get(code, {}) if isinstance(pe_history, dict) else {}
        if isinstance(bucket, dict):
            vals = []
            for ds, rv in bucket.items():
                try:
                    datetime.strptime(str(ds), '%Y%m%d')
                except Exception:
                    continue
                pv = to_float(rv)
                if pv is not None and 0 < pv <= PE_MAX_VALID:
                    vals.append((str(ds), pv))
            if vals:
                vals.sort(reverse=True)
                return vals[0][1]
        return None

    def local_peer_median(code, industry, subindustries):
        target_subs = set(normalize_subindustry(x) for x in (subindustries or []) if normalize_subindustry(x))
        candidates = []
        for c, x in u.items():
            if clean_code(c) == code or not isinstance(x, dict):
                continue
            if canonical_industry(x.get('industry')) != canonical_industry(industry):
                continue
            if target_subs:
                xs = set(normalize_subindustry(z) for z in (x.get('subindustries') or ([x.get('subindustry')] if x.get('subindustry') else [])) if normalize_subindustry(z))
                if not (target_subs & xs):
                    continue
            if to_float(x.get('market_cap')) is None:
                continue
            candidates.append(x)
        candidates.sort(key=lambda x: to_float(x.get('market_cap')) or 0, reverse=True)
        vals = []
        for x in candidates[:10]:
            pv = cached_peer_pe(x)
            if pv is not None:
                vals.append(pv)
        # 若次產業快取不足，與既有 analysis() 的 fallback 邏輯一致：同大產業市值Top10。
        if not vals and target_subs:
            return local_peer_median(code, industry, [])
        return float(np.median(vals)) if vals else None

    candidates = []
    skipped_no_tech = 0

    # --------------------------------------------------------
    # 第一階段：只算非基本面。最大可能分數 < 90 就完全不用查基本面。
    # --------------------------------------------------------
    for code, item in u.items():
        if not isinstance(item, dict):
            continue
        code = clean_code(code)
        if not code or not code.isdigit():
            continue
        symbol = item.get('symbol') or symbol_for(code, item.get('market'))
        market = item.get('market')
        if market not in ('TWSE', 'TPEX') or not symbol:
            continue

        tech = _load_technical_cache_entry(tech_cache, code, max_age=TECH_CACHE_MAX_AGE)
        if not tech:
            skipped_no_tech += 1
            continue

        market_chip = chip_cache.get(market, {}) if isinstance(chip_cache, dict) else {}
        inst = market_chip.get(code, {}) if isinstance(market_chip, dict) else {}
        if not isinstance(inst, dict):
            inst = {}
        inst = {
            'latest': to_float(inst.get('latest')),
            '5d': to_float(inst.get('5d')),
            '20d': to_float(inst.get('20d')),
        }

        margin = MARGIN_CACHE.get(('margin', market), {})
        margin = margin.get(code, {}) if isinstance(margin, dict) else {}
        if not isinstance(margin, dict):
            margin = {}
        margin = {
            'margin_change': to_float(margin.get('margin_change')),
            'margin_balance': to_float(margin.get('margin_balance')),
            'short_change': to_float(margin.get('short_change')),
            'short_balance': to_float(margin.get('short_balance')),
        }

        ts, _ = score_tech(tech)
        cs, _ = score_chip(inst, margin)
        risk, _ = score_risk(tech, inst, margin)
        nonfund_max = ts + cs + (10 - risk)

        if nonfund_max < threshold - 40:
            continue

        industry = canonical_industry(item.get('industry'))
        subindustries = get_subindustries_for_stock(code, item)
        peer_med = local_peer_median(code, industry, subindustries)
        off = pe_data.get(code, {}) if isinstance(pe_data, dict) else {}
        off = off if isinstance(off, dict) else {}
        pe = to_float(off.get('pe'))
        pb = to_float(off.get('pb'))
        yld = to_float(off.get('yield'))
        one, sample = one_year_pe(code, pe_history)

        # 先用現有 PE/PB/殖利率算一個保守的基本面估計。
        # score_fund() 的資料完整度 cap 會限制缺資料時的最高基本面分數，
        # 因此這裡只作候選排序/篩選，不把它當最終分數。
        partial_fs, _ = score_fund(
            pe, one, peer_med, None, None, None, pb, yld,
            INDUSTRY_MODEL.get(industry, DEFAULT_MODEL)
        )
        candidates.append({
            'code': code,
            'item': item,
            'symbol': symbol,
            'market': market,
            'industry': industry,
            'subindustries': subindustries,
            'tech': tech,
            'inst': inst,
            'margin': margin,
            'peer_med': peer_med,
            'pe': pe,
            'pb': pb,
            'yld': yld,
            'one': one,
            'one_sample': sample,
            'partial_fs': partial_fs,
            'nonfund_max': nonfund_max,
        })

    print(
        f'V2.10.79 高評分第一階段：候選 {len(candidates)} 檔；'
        f'無技術快取 {skipped_no_tech} 檔；'
        f'耗時 {time.time()-started:.1f}s',
        flush=True
    )

    # --------------------------------------------------------
    # 第二階段：只有「理論上可能 >=90」的股票才補完整基本面。
    # --------------------------------------------------------
    results = []
    for idx, c in enumerate(candidates, 1):
        try:
            item = c['item']
            current_price = to_float(c['tech'].get('price')) or to_float(item.get('price'))
            yf_f = official_fundamental(
                c['symbol'],
                {
                    'pe': c['pe'],
                    'pb': c['pb'],
                    'yield': c['yld'],
                },
                current_price=current_price,
                market=c['market'],
                industry=c['industry'],
                subindustry=(c.get('item') or {}).get('subindustry') or ((c.get('item') or {}).get('subindustries') or [''])[0]
            )
            if not isinstance(yf_f, dict):
                yf_f = {}

            pe = to_float(c['pe'])
            pb = to_float(c['pb'])
            yld = to_float(c['yld'])
            if pe is None:
                pe = to_float(yf_f.get('pe'))
            if pb is None:
                pb = to_float(yf_f.get('pb'))
            if yld is None:
                yld = to_float(yf_f.get('yield'))

            one = c['one']
            one_sample = c['one_sample']
            if one is None and pe is not None:
                try:
                    one, one_sample, _ = one_year_pe_proxy(c['code'], pe, c['symbol'])
                except Exception:
                    pass

            fs, fr = score_fund(
                pe,
                one,
                c['peer_med'],
                to_float(yf_f.get('peg')),
                to_float(yf_f.get('roe')),
                to_float(yf_f.get('eps_growth')),
                pb,
                yld,
                INDUSTRY_MODEL.get(c['industry'], DEFAULT_MODEL),
                valuation_growth=yf_f.get('valuation_growth')
            )
            ts, tr = score_tech(c['tech'])
            cs, cr = score_chip(c['inst'], c['margin'])
            risk, rr = score_risk(c['tech'], c['inst'], c['margin'])
            total = max(0, min(100, fs + ts + cs + (10 - risk)))

            if total >= threshold:
                results.append({
                    'code': c['code'],
                    'name': item.get('name') or c['code'],
                    'symbol': c['symbol'],
                    'score': int(total),
                    'fund': int(fs),
                    'tech': int(ts),
                    'chip': int(cs),
                    'risk': int(risk),
                    'price': current_price,
                    'reasons': fr + tr,
                })
        except Exception as e:
            print(
                f'V2.10.79 高評分完整基本面失敗 {c.get("code")}: '
                f'{type(e).__name__}: {e}',
                flush=True
            )
        if idx % 10 == 0 or idx == len(candidates):
            print(
                f'V2.10.79 高評分第二階段進度：{idx}/{len(candidates)}；'
                f'目前 >=90：{len(results)}',
                flush=True
            )

    results.sort(key=lambda x: (-x['score'], x['code']))

    # --------------------------------------------------------
    # 第三階段：每日去重；跌破90後重新站回90可再次通知。
    # --------------------------------------------------------
    today = datetime.now(TW_TZ).strftime('%Y-%m-%d')
    hs = state.setdefault('high_score_alert', {})
    if not isinstance(hs, dict):
        hs = {}
        state['high_score_alert'] = hs
    if hs.get('date') != today:
        hs.clear()
        hs['date'] = today
        hs['active'] = []

    current_codes = {x['code'] for x in results}
    previous_active = set(hs.get('active') or [])

    # 目前 <90 的股票從 active 移除；之後重新 >=90 就視為重新進榜。
    # 不使用永久 notified 清單，確保「跌破90 → 再次站回90」仍會重新通知。
    reentries = current_codes - previous_active
    new_codes = [x['code'] for x in results if x['code'] in reentries]

    hs['active'] = sorted(current_codes)

    if new_codes and LINE_TOKEN:
        by_code = {x['code']: x for x in results}
        new_rows = [by_code[x] for x in new_codes if x in by_code]
        msg = (
            '🚨 全市場高評分股票通知 V2.10.75\n\n'
            f'執行時間：{datetime.now(TW_TZ).strftime("%Y-%m-%d %H:%M:%S")}\n'
            '條件：綜合評分 ≥ 90 分\n\n'
            '【本次新進榜】\n' +
            '\n'.join(
                f'{i}. {x["code"]} {x["name"]}｜{x["score"]}分'
                for i, x in enumerate(new_rows, 1)
            ) +
            f'\n\n目前90分以上共 {len(results)} 檔\n\n' +
            '【目前90分以上】\n' +
            '\n'.join(
                f'{i}. {x["code"]} {x["name"]}｜{x["score"]}分'
                for i, x in enumerate(results, 1)
            )
        )
        send_line(msg[:5000])
        print(
            f'V2.10.79 高評分 LINE 已發送：新進榜 {len(new_rows)} 檔；'
            f'目前 >=90 共 {len(results)} 檔',
            flush=True
        )
    else:
        print(
            f'V2.10.79 高評分掃描完成：目前 >=90 共 {len(results)} 檔；'
            f'本次新進榜 {len(new_codes)} 檔；不發送重複 LINE',
            flush=True
        )

    print(
        f'V2.10.79 高評分掃描總耗時：{time.time()-started:.1f}s',
        flush=True
    )
    return results


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
        'V2.10.75自動估值 + 技術 + 籌碼\n'
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
    # V2.10.23 全市場技術快取
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

    # V2.10.25：先建立全市場日期型 PE 快取，再補 STOCKS 目標股。
    # 前者讓 LINE 可以查任意 1985 檔；後者仍保留原本目標股的至少 60 個
    # 有效 PE 保證。
    try:
        pe_history = refresh_all_market_pe_history(pe_history, u)
    except Exception as e:
        print(
            f'⚠️ 全市場 PE 歷史快取更新失敗：{type(e).__name__}: {e}',
            flush=True
        )

    pe_backfill_budget = {'started': time.time(), 'api_used': 0,
                          'blocked': set(), 'market_failures': {}}
    for target_name, target_symbol in STOCKS.items():
        target_code = clean_code(target_symbol)
        target_item = u.get(target_code)
        if target_item and target_symbol and not target_symbol.startswith('^'):
            target_market = target_item.get('market')
            if target_market in ('TWSE', 'TPEX'):
                if (pe_backfill_budget['api_used'] >= PE_BACKFILL_MAX_API_PER_RUN or
                    time.time() - pe_backfill_budget['started'] >= PE_BACKFILL_MAX_SECONDS_PER_RUN):
                    print('⚠️ V2.10.56：目標股 PE 回補總預算已用完，後續改用既有快取/proxy，不再打歷史 API。', flush=True)
                    break
                try:
                    pe_history = backfill_pe(target_code, pe_history, target_market, pe_backfill_budget)
                except Exception as e:
                    print(f'PE歷史回補失敗：{target_code} / {e}', flush=True)
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

    # V2.10.28：LINE 要能查任意 TWSE/TPEX 股票，因此 Actions 每次都建立兩個市場的
    # 20 日法人與融資快取；不再只依 STOCKS 目標股決定市場。T86/TPEx 法人端點一次就是全市場資料。
    target_markets.update({'TWSE', 'TPEX'})

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

    # V2.10.23：把本次 Actions 已取得資料整理成 Render 可直接讀取的小快取。
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
                state,
                u
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

    # V2.10.74：全市場 >=90 分彙整通知；使用本次 Actions 已建立快取，避免逐股完整 analysis。
    try:
        _scan_high_score_stocks(u, state)
    except Exception as e:
        print(f'⚠️ V2.10.79 高評分全市場掃描失敗：{type(e).__name__}: {e}', flush=True)
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
