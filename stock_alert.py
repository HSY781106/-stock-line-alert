# stock_alert.py V2.9.7.1
# 效能修正版：全市場資料批次化、單次執行快取、限制 Yahoo/API 重試、15分鐘資料僅抓目標股
# 股票跌幅 + 15分鐘區間最低價 + 動態估值 + 技術 + 籌碼 + 100分制加碼決策
import os, json, time, math, traceback, re
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
import requests
import pandas as pd
import numpy as np
import yfinance as yf

LINE_TOKEN=os.environ.get('LINE_CHANNEL_ACCESS_TOKEN','')
TWSE_BASE='https://openapi.twse.com.tw/v1'
TWSE_WEB_BASE='https://www.twse.com.tw/rwd/zh'
TPEX_BASE='https://www.tpex.org.tw/openapi/v1'
TW_TZ=ZoneInfo('Asia/Taipei')
STATE_FILE='alert_state.json'; PE_HISTORY_FILE='pe_history.json'; CHIP_HISTORY_FILE='chip_history.json'
UNIVERSE_CACHE_FILE='market_universe_cache.json'; TWSE_PROFILE_CACHE_FILE='twse_profile_cache.json'; TWSE_QUOTES_CACHE_FILE='twse_quotes_cache.json'
LINE_REPLY_URL='https://api.line.me/v2/bot/message/reply'; LINE_BROADCAST_URL='https://api.line.me/v2/bot/message/broadcast'
DAILY_THRESHOLD=-0.05; WEEK_THRESHOLD=-0.10; PE_MIN_HISTORY=60; PE_MAX_VALID=200; PE_ONE_YEAR_TRADING_DAYS=240
UNIVERSE_CACHE_HOURS=24; TWSE_TIMEOUT=8; TPEX_TIMEOUT=10; API_SLEEP=.05; PE_BACKFILL_MAX_DAYS=370
YF_TIMEOUT=10; MAX_HISTORY_DAYS_PER_RUN=75
RUN_CACHE={}
INSTITUTIONAL_CACHE={}
MARGIN_CACHE={}
STOCKS={'0050 元大台灣50':'0050.TW','2330 台積電':'2330.TW','3711 日月光投控':'3711.TW','QQQ':'QQQ','台灣加權指數':'^TWII'}

INDUSTRY_CODE_MAP={'01':'水泥工業','02':'食品工業','03':'塑膠工業','04':'紡織纖維','05':'電機機械','06':'電器電纜','08':'玻璃陶瓷','09':'造紙工業','10':'鋼鐵工業','11':'橡膠工業','12':'汽車工業','13':'電子工業','14':'建材營造','15':'航運業','16':'觀光餐旅','17':'金融業','18':'貿易百貨','19':'綜合','20':'其他','21':'化學工業','22':'生技醫療','23':'油電燃氣業','24':'半導體業','25':'電腦及週邊設備業','26':'光電業','27':'通信網路業','28':'電子零組件業','29':'電子通路業','30':'資訊服務業','31':'其他電子業','32':'文化創意業','33':'農業科技','34':'電子商務','35':'數位雲端','36':'運動休閒','37':'居家生活','38':'綠能環保','39':'數位經濟','40':'其他'}
INDUSTRY_MODEL={'金融業':{'pe':False,'peg':False,'pb':True,'yield':True,'roe':True},'銀行業':{'pe':False,'peg':False,'pb':True,'yield':True,'roe':True},'保險業':{'pe':False,'peg':False,'pb':True,'yield':True,'roe':True}}
DEFAULT_MODEL={'pe':True,'peg':True,'pb':True,'yield':True,'roe':True}

# ---------- helpers ----------
def to_float(v):
    if v is None:return None
    try:
        s=str(v).strip().replace(',','').replace('%','')
        if s in {'','-','--','N/A','nan','NaN','None','null','－','…'}:return None
        return float(s)
    except:return None

def first_value(row,names):
    if not isinstance(row,dict):return None
    for n in names:
        if n in row and row[n] not in (None,'','-','--','－'):return row[n]
    return None

def find_value(row,names):return to_float(first_value(row,names))
def clean_code(v):
    s=str(v or '').strip().upper()
    for x in ('.TW','.TWO'):
        if s.endswith(x):s=s[:-len(x)]
    return s.strip()
def normalize_name(v):return str(v or '').strip().replace(' ','').replace('　','').lower()
def safe_div(a,b):
    try:return None if a is None or b in (None,0) else a/b
    except:return None
def fmt(v,d=2):return 'N/A' if v is None else f'{float(v):,.{d}f}'
def pct(v):return 'N/A' if v is None else f'{v:.2%}'
def canonical_industry(v):
    s=str(v or '').strip(); s=INDUSTRY_CODE_MAP.get(s.zfill(2),s) if s.isdigit() else s
    aliases={'電子工業':'其他電子業','電信業':'通信網路業','通信網路':'通信網路業','電腦及週邊':'電腦及週邊設備業','電腦及週邊設備':'電腦及週邊設備業','生技醫療業':'生技醫療','醫療保健業':'醫療保健','觀光事業':'觀光餐旅'}
    return aliases.get(s,s or '其他')
def symbol_for(code,market=None):return f'{clean_code(code)}.{"TW" if market=="TWSE" else "TWO"}' if market in ('TWSE','TPEX') else clean_code(code)

# ---------- http / line / json ----------
def http_json(url,params=None,timeout=20,retries=2):
    last=None
    for i in range(retries+1):
        try:
            r=requests.get(url,params=params,timeout=timeout,headers={'User-Agent':'Mozilla/5.0 stock-alert/2.9.7.1'}); r.raise_for_status(); return r.json()
        except Exception as e:
            last=e
            if i<retries:time.sleep(.8*(i+1))
    print(f'API失敗：{url} / {last}'); return None
def http_text(url,params=None,timeout=20,retries=2):
    for i in range(retries+1):
        try:
            r=requests.get(url,params=params,timeout=timeout,headers={'User-Agent':'Mozilla/5.0 stock-alert/2.9.7.1'}); r.raise_for_status(); return r.content.decode('utf-8-sig','replace')
        except Exception as e:
            if i<retries:time.sleep(.8*(i+1))
    return None
def twse_get(e,p=None):return http_json(TWSE_BASE+e,p,TWSE_TIMEOUT,retries=1)
def twse_web_get(e,p=None):return http_json(TWSE_WEB_BASE+e,p,TWSE_TIMEOUT,retries=1)
def tpex_get(e,p=None):return http_json(TPEX_BASE+e,p,TPEX_TIMEOUT)
def load_json(f):
    try:
        with open(f,encoding='utf-8') as x:
            d=json.load(x);return d if isinstance(d,dict) else {}
    except:return {}
def save_json(f,d):
    t=f+'.tmp'
    with open(t,'w',encoding='utf-8') as x:json.dump(d,x,ensure_ascii=False,indent=2)
    os.replace(t,f)
def send_line(msg):
    if not LINE_TOKEN:return False
    try:
        r=requests.post(LINE_BROADCAST_URL,headers={'Authorization':f'Bearer {LINE_TOKEN}','Content-Type':'application/json'},json={'messages':[{'type':'text','text':str(msg)[:5000]}]},timeout=20); print(f'LINE廣播：{r.status_code}'); return r.status_code==200
    except Exception as e:print('LINE廣播失敗：',e);return False
def reply_line(token,msg):
    if not LINE_TOKEN or not token:return False
    try:
        r=requests.post(LINE_REPLY_URL,headers={'Authorization':f'Bearer {LINE_TOKEN}','Content-Type':'application/json'},json={'replyToken':token,'messages':[{'type':'text', 'text':str(msg)[:5000]}]},timeout=20);return r.status_code==200
    except:return False

# ---------- universe ----------
def normalize_profile(row,market):
    code=clean_code(first_value(row,['公司代號','證券代號','SecuritiesCompanyCode','Code'])); name=first_value(row,['公司簡稱','公司名稱','證券名稱','CompanyAbbreviation','CompanyName']) or code
    industry=canonical_industry(first_value(row,['產業類別','產業別','SecuritiesIndustryCode','Industry']) or '其他'); cap=find_value(row,['實收資本額','實收資本額(元)','PaidinCapital','Capital','Capitals'])
    if not code.isdigit():return None
    return {'code':code,'name':str(name).strip(),'industry':industry,'market':market,'symbol':symbol_for(code,market),'capital':cap}
def get_twse_universe():
    data=twse_get('/opendata/t187ap03_L');out=[]
    if isinstance(data,list):
        for r in data:
            x=normalize_profile(r,'TWSE')
            if x:out.append(x)
    if out:save_json(TWSE_PROFILE_CACHE_FILE,{'cached_at':time.time(),'data':out});print(f'TWSE 基本資料：{len(out)}（OpenAPI）');return out
    text=http_text('https://mopsfin.twse.com.tw/opendata/t187ap03_L.csv')
    if text:
        try:
            df=pd.read_csv(__import__('io').StringIO(text),dtype=str);out=[x for _,r in df.fillna('').iterrows() if (x:=normalize_profile(r.to_dict(),'TWSE'))]
        except:out=[]
    if out:save_json(TWSE_PROFILE_CACHE_FILE,{'cached_at':time.time(),'data':out});print(f'TWSE 基本資料：{len(out)}（CSV）');return out
    c=load_json(TWSE_PROFILE_CACHE_FILE).get('data',[]);print(f'⚠️ TWSE 基本資料使用快取：{len(c)}');return c
def get_tpex_universe():
    data=tpex_get('/mopsfin_t187ap03_O');out=[]
    if isinstance(data,list):
        for r in data:
            x=normalize_profile(r,'TPEX')
            if x:out.append(x)
    if not out:
        data=tpex_get('/tpex_mainboard_daily_close_quotes')
        if isinstance(data,list):
            for r in data:
                x=normalize_profile(r,'TPEX')
                if x:out.append(x)
    return out
def get_twse_quotes():
    data=twse_get('/exchangeReport/STOCK_DAY_ALL');out={}
    if isinstance(data,list):
        for r in data:
            c=clean_code(first_value(r,['Code','證券代號']));p=find_value(r,['ClosingPrice','收盤價'])
            if c and p is not None:out[c]={'close':p,'open':find_value(r,['OpeningPrice','開盤價']),'high':find_value(r,['HighestPrice','最高價']),'low':find_value(r,['LowestPrice','最低價']),'volume':find_value(r,['TradeVolume','成交股數'])}
    if out:save_json(TWSE_QUOTES_CACHE_FILE,{'cached_at':time.time(),'data':out})
    else:out=load_json(TWSE_QUOTES_CACHE_FILE).get('data',{})
    print(f'TWSE 當日行情：{len(out)}');return out
def get_tpex_quotes():
    data=tpex_get('/tpex_mainboard_daily_close_quotes');out={}
    if isinstance(data,list):
        for r in data:
            c=clean_code(first_value(r,['SecuritiesCompanyCode','Code','證券代號']));p=find_value(r,['Close','ClosingPrice'])
            if c and p is not None:out[c]={'close':p,'open':find_value(r,['Open','OpeningPrice']),'high':find_value(r,['High','HighestPrice']),'low':find_value(r,['Low','LowestPrice']),'volume':find_value(r,['TradingShares','TradeVolume']),'capital':find_value(r,['Capitals','Capital'])}
    print(f'TPEx 當日行情：{len(out)}');return out
def get_tpex_market_values():
    data=tpex_get('/tpex_daily_market_value');out={}
    if isinstance(data,list):
        for r in data:
            c=clean_code(first_value(r,['SecuritiesCompanyCode','證券代號','Code','代號']));v=find_value(r,['MarketValue','market_value','市值','總市值','MarketCap'])
            if c and v is not None:out[c]=v
    print(f'TPEx 官方市值資料：{len(out)}');return out
def build_universe():
    print('\n========== 建立動態市場股票池 V2.9 ==========')
    tw,tx=get_twse_universe(),get_tpex_universe();print(f'TWSE 基本資料：{len(tw)}');print(f'TPEx 基本資料：{len(tx)}')
    tq,xq,xv=get_twse_quotes(),get_tpex_quotes(),get_tpex_market_values();u={}
    for x in tw:
        x=dict(x);q=tq.get(x['code'],{});x['price']=q.get('close');x['market_cap']=safe_div(x.get('capital'),10)*q.get('close') if x.get('capital') and q.get('close') else None;u[x['code']]=x
    for x in tx:
        x=dict(x);q=xq.get(x['code'],{});x['price']=q.get('close');x['market_cap']=xv.get(x['code']) or (safe_div(x.get('capital') or q.get('capital'),10)*q.get('close') if (x.get('capital') or q.get('capital')) and q.get('close') else None);u[x['code']]=x
    print(f'有效動態股票：{len(u)}');return u
def get_market_universe(force_refresh=False):
    c=load_json(UNIVERSE_CACHE_FILE);d=c.get('data');t=c.get('_cached_at',0)
    if not force_refresh and isinstance(d,dict) and d and time.time()-t<UNIVERSE_CACHE_HOURS*3600:return d
    u=build_universe()
    if u:save_json(UNIVERSE_CACHE_FILE,{'_cached_at':time.time(),'data':u});return u
    return d or {}
def get_dynamic_industry_peers(code,industry,u,limit=10):
    p=[x for c,x in u.items() if c!=code and canonical_industry(x.get('industry'))==canonical_industry(industry) and to_float(x.get('market_cap')) is not None];p.sort(key=lambda x:x.get('market_cap',0),reverse=True);return p[:limit]
def resolve_stock(q,u):
    q=str(q or '').strip();m=re.match(r'^(?:TWSE:|TPEX:)?(\d{4,6})(?:\.TW|\.TWO)?(?:\s+.*)?$',q,re.I)
    if m and m.group(1) in u:return u[m.group(1)]
    if clean_code(q) in u:return u[clean_code(q)]
    nq=normalize_name(re.sub(r'^(?:TWSE:|TPEX:)?\d{4,6}(?:\.TW|\.TWO)?\s*','',q,flags=re.I) or q)
    hits=[x for x in u.values() if normalize_name(x.get('name'))==nq];
    if len(hits)==1:return hits[0]
    hits=[x for x in u.values() if nq and nq in normalize_name(x.get('name'))];return hits[0] if len(hits)==1 else None

# ---------- Yahoo / interval ----------
def yf_download(symbol,period='1y',interval='1d'):
    key=(symbol,period,interval)
    if key in RUN_CACHE:
        return RUN_CACHE[key]
    try:
        d=yf.download(symbol,period=period,interval=interval,progress=False,auto_adjust=False,threads=False,timeout=YF_TIMEOUT)
        if d is None or d.empty:
            RUN_CACHE[key]=None; return None
        if isinstance(d.columns,pd.MultiIndex):d.columns=[x[0] for x in d.columns]
        RUN_CACHE[key]=d
        return d
    except Exception as e:
        print(f'Yahoo download失敗 {symbol} [{interval}/{period}]: {e}')
        RUN_CACHE[key]=None;return None

def get_latest_price(symbol):
    key=('latest',symbol)
    if key in RUN_CACHE:return RUN_CACHE[key]
    d=yf_download(symbol,'5d','1d')
    try:v=float(d['Close'].dropna().iloc[-1]) if d is not None and not d.empty else None
    except:v=None
    RUN_CACHE[key]=v;return v

def get_previous_close(symbol):
    d=yf_download(symbol,'10d','1d');c=pd.to_numeric(d['Close'],errors='coerce').dropna() if d is not None and 'Close' in d else pd.Series(dtype=float)
    return float(c.iloc[-2]) if len(c)>=2 else None

def get_week_high(symbol):
    d=yf_download(symbol,'10d','1d');h=pd.to_numeric(d['High'],errors='coerce').dropna() if d is not None and 'High' in d else pd.Series(dtype=float)
    return float(h.tail(7).max()) if not h.empty else None

def parse_time(v):
    try:
        d=datetime.fromisoformat(v);return d if d.tzinfo else d.replace(tzinfo=TW_TZ)
    except:return None

def yahoo_chart_intraday(symbol, start_dt, end_dt, interval='5m'):
    """穩定取得盤中 OHLC。
    GitHub Actions 對 period1/period2 的 Yahoo 盤中查詢較容易得到空結果，
    因此改用 range：5m=5d、1m=1d，再在本機精確切出上次成功執行到本次執行的區間。
    query1/query2 雙主機 fallback。
    """
    ranges={'5m':'5d','1m':'1d'}
    hosts=('query1.finance.yahoo.com','query2.finance.yahoo.com')
    try:
        if start_dt.tzinfo is None:start_dt=start_dt.replace(tzinfo=TW_TZ)
        else:start_dt=start_dt.astimezone(TW_TZ)
        if end_dt.tzinfo is None:end_dt=end_dt.replace(tzinfo=TW_TZ)
        else:end_dt=end_dt.astimezone(TW_TZ)
        for host in hosts:
            try:
                url=f'https://{host}/v8/finance/chart/{symbol}'
                r=requests.get(url,params={'range':ranges[interval],'interval':interval,'events':'history','includePrePost':'false','includeAdjustedClose':'true'},headers={'User-Agent':'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/124.0 Safari/537.36'},timeout=8)
                r.raise_for_status(); payload=r.json()
                chart=payload.get('chart') or {}; result=(chart.get('result') or [None])[0]
                if not result:
                    err=chart.get('error')
                    print(f'Yahoo Chart無資料 {symbol} [{interval}] {host}: {err or "empty result"}')
                    continue
                ts=result.get('timestamp') or []
                q=((result.get('indicators') or {}).get('quote') or [{}])[0]
                lows=q.get('low') or []
                points=[]
                for t,lv in zip(ts,lows):
                    if t is None or lv is None:continue
                    dt=datetime.fromtimestamp(float(t),tz=TW_TZ)
                    if start_dt <= dt <= end_dt:
                        v=to_float(lv)
                        if v is not None:points.append((dt,v))
                if points:
                    return {'low':min(v for _,v in points),'start':start_dt,'end':end_dt,'source':f'Yahoo-{interval}'}
                # Yahoo 有資料但沒有落在區間內：把最新一筆時間印出來，方便判斷延遲/時區問題。
                all_times=[datetime.fromtimestamp(float(t),tz=TW_TZ) for t in ts if t is not None]
                latest=max(all_times) if all_times else None
                latest_text = latest.strftime("%Y-%m-%d %H:%M:%S") if latest else "N/A"
                print(f'Yahoo Chart區間無K棒 {symbol} [{interval}] {host}；最新K棒：{latest_text}')
            except Exception as e:
                print(f'Yahoo Chart失敗 {symbol} [{interval}] {host}：{e}')
        return None
    except Exception as e:
        print(f'Yahoo Chart盤中資料失敗 {symbol} [{interval}]：{e}')
        return None

def get_interval_stats(symbol,start_iso):
    """取得上次成功基準到本次執行的實際盤中最低價。
    只使用 Yahoo Chart；不再呼叫不穩定的 TWSE MIS，避免同一次執行浪費 timeout。
    """
    now=datetime.now(TW_TZ)
    start=parse_time(start_iso) if start_iso else now-timedelta(minutes=15)
    if not start or start>now:start=now-timedelta(minutes=15)
    for interval in ('5m','1m'):
        z=yahoo_chart_intraday(symbol,start,now,interval)
        if z:return z
    return None

def check_interval_low(name,symbol,state):
    now=datetime.now(TW_TZ);iso=now.isoformat()
    s=state.setdefault('interval_low',{}).setdefault(name,{})
    prev_t=s.get('last_check');prev_p=to_float(s.get('last_price'));cur=get_latest_price(symbol)
    stats=get_interval_stats(symbol,prev_t) if prev_t and cur is not None else None
    result=None
    if prev_t and prev_p and stats:
        drop=stats['low']/prev_p-1
        result={'previous_price':prev_p,'interval_low':stats['low'],'drop':drop,'start':stats['start'].isoformat(),'end':iso,'source':stats.get('source')}
        print(f'【15分鐘區間】上次執行：{stats["start"].strftime("%H:%M:%S")} 本次執行：{now.strftime("%H:%M:%S")} 期間最低：{stats["low"]:,.2f} 目前價格：{cur:,.2f} 區間跌幅：{drop:.2%}（{stats.get("source","5m")}）')
        if drop<=DAILY_THRESHOLD:
            send_line(f'🔴 15分鐘區間低點通知\n\n標的：{name}\n上次執行：{stats["start"].strftime("%H:%M:%S")}\n本次執行：{now.strftime("%H:%M:%S")}\n期間最低：{stats["low"]:,.2f}\n目前價格：{cur:,.2f}\n區間跌幅：{drop:.2%}')
    elif not prev_t:
        print(f'【15分鐘區間】首次建立基準：{now.strftime("%H:%M:%S")}，目前價格：{fmt(cur)}')
    elif prev_t and not stats:
        # Yahoo 盤中資料若暫時不可用，不覆蓋基準；下一輪仍會以同一個起點重新嘗試。
        print(f'⚠️ 15分鐘資料暫時無法取得；保留上次執行基準：{prev_t}')
    # 關鍵修正：若盤中資料取得失敗，不得更新基準時間/價格。
    # 否則下一輪會把剛剛失敗的時間當成新起點，永遠無法形成完整區間。
    if not prev_t or stats:
        s.update({'last_check':iso,'last_price':cur})
    if stats:
        s['last_interval_low']=stats['low'];s['last_interval_source']=stats.get('source')
    return result

def check_drop_alert(name,symbol,state):
    cur,pc,wh=get_latest_price(symbol),get_previous_close(symbol),get_week_high(symbol)
    if cur is None or pc is None:return
    day=cur/pc-1;week=cur/wh-1 if wh else None;s=state.setdefault('drop_alert',{}).setdefault(name,{});today=datetime.now(TW_TZ).strftime('%Y-%m-%d')
    if s.get('date')!=today:s.update({'date':today,'daily_alert':False,'weekly_alert':False})
    if day<=DAILY_THRESHOLD and not s.get('daily_alert'):send_line(f'🔴 跌幅通知\n\n標的：{name}\n目前價格：{cur:,.2f}\n前一交易日收盤：{pc:,.2f}\n單日跌幅：{day:.2%}');s['daily_alert']=True
    elif day>DAILY_THRESHOLD:s['daily_alert']=False
    if week is not None and week<=WEEK_THRESHOLD and not s.get('weekly_alert'):send_line(f'🔴 一週跌幅通知\n\n標的：{name}\n目前價格：{cur:,.2f}\n過去7日高點：{wh:,.2f}\n距7日高點跌幅：{week:.2%}');s['weekly_alert']=True
    elif week is not None and week>WEEK_THRESHOLD:s['weekly_alert']=False

# ---------- valuation ----------
def parse_pe(data):
    out={}
    if isinstance(data,list):
        rows=data;fields=None
    elif isinstance(data,dict):
        fields=data.get('fields',[]);rows=data.get('data',[])
    else:return out
    for r in rows if isinstance(rows,list) else []:
        if isinstance(r,list):o=dict(zip(fields,r)) if fields else {}
        elif isinstance(r,dict):o=r
        else:continue
        c=clean_code(first_value(o,['證券代號','公司代號','Code','SecuritiesCompanyCode']));
        if c:out[c]={'pe':find_value(o,['本益比','PEratio','PERatio','PE']),'pb':find_value(o,['股價淨值比','PBratio','PBR','PBRatio']),'yield':find_value(o,['殖利率(%)','殖利率','DividendYield'])}
    return out
def get_current_pe_data():
    key='current_pe'
    if key in RUN_CACHE:return RUN_CACHE[key]
    out={**parse_pe(twse_get('/exchangeReport/BWIBBU_ALL')),**parse_pe(tpex_get('/tpex_mainboard_peratio_analysis'))}
    RUN_CACHE[key]=out
    print(f'本次執行 PE 資料：{len(out)} 檔（只抓一次）')
    return out
def get_pe_by_date(ds,market):return parse_pe(tpex_get('/tpex_mainboard_peratio_analysis',{'date':ds}) if market=='TPEX' else twse_web_get('/afterTrading/BWIBBU_ALL',{'date':ds,'response':'json'}))
def backfill_pe(code,h,market):
    h.setdefault(code,{});n=sum(1 for v in h[code].values() if to_float(v) and 0<to_float(v)<=PE_MAX_VALID)
    d=datetime.now(TW_TZ).date();checked=0
    while n<PE_MIN_HISTORY and checked<PE_BACKFILL_MAX_DAYS:
        if d.weekday()<5:
            ds=d.strftime('%Y%m%d')
            if ds not in h[code]:
                pe=get_pe_by_date(ds,market).get(code,{}).get('pe')
                if pe and 0<pe<=PE_MAX_VALID:h[code][ds]=pe;n+=1
        d-=timedelta(days=1);checked+=1;time.sleep(.03)
    return h
def one_year_pe(code,h):
    cutoff=datetime.now(TW_TZ).date()-timedelta(days=365);v=[]
    for ds,x in h.get(code,{}).items():
        try:d=datetime.strptime(ds,'%Y%m%d').date()
        except:continue
        if d>=cutoff and to_float(x) and 0<to_float(x)<=PE_MAX_VALID:v.append((d,float(x)))
    v=sorted(v,reverse=True)[:PE_ONE_YEAR_TRADING_DAYS];return (sum(x for _,x in v)/len(v),len(v)) if len(v)>=PE_MIN_HISTORY else (None,len(v))
def yahoo_fund(symbol):
    key=('fund',symbol)
    if key in RUN_CACHE:return RUN_CACHE[key]
    o={'eps_growth':None,'roe':None,'peg':None,'pb':None,'yield':None,'pe':None}
    try:
        i=yf.Ticker(symbol).info
        o['pe']=to_float(i.get('trailingPE')) or to_float(i.get('forwardPE'));o['pb']=to_float(i.get('priceToBook'));o['yield']=to_float(i.get('dividendYield'))*100 if i.get('dividendYield') is not None else None;o['eps_growth']=to_float(i.get('earningsGrowth'))*100 if i.get('earningsGrowth') is not None else None;o['roe']=to_float(i.get('returnOnEquity'))*100 if i.get('returnOnEquity') is not None else None;o['peg']=to_float(i.get('pegRatio'))
    except Exception as e:print('Yahoo fundamentals失敗',symbol,e)
    RUN_CACHE[key]=o
    return o

# ---------- technical ----------
def rsi(c,p=14):
    c=pd.to_numeric(c,errors='coerce').dropna();d=c.diff();g=d.clip(lower=0).rolling(p).mean();l=(-d.clip(upper=0)).rolling(p).mean();x=(100-100/(1+g/l.replace(0,np.nan))).dropna();return float(x.iloc[-1]) if not x.empty else None
def kd(d):
    if d is None or len(d)<20:return None,None
    h,l,c=d['High'],d['Low'],d['Close'];lo=l.rolling(9).min();hi=h.rolling(9).max();r=(c-lo)/(hi-lo).replace(0,np.nan)*100;k=r.ewm(com=2,adjust=False).mean();dd=k.ewm(com=2,adjust=False).mean();return (float(k.dropna().iloc[-1]),float(dd.dropna().iloc[-1])) if not k.dropna().empty and not dd.dropna().empty else (None,None)
def technical(symbol):
    d=yf_download(symbol,'6mo','1d');o={'k':None,'d':None,'rsi':None,'ma20':None,'ma60':None,'trend':None,'distance_low':None,'price':None,'recent_low':None}
    if d is None or d.empty:return o
    c=pd.to_numeric(d['Close'],errors='coerce').dropna();k,dd=kd(d);o.update({'k':k,'d':dd,'rsi':rsi(c),'ma20':float(c.tail(20).mean()) if len(c)>=20 else None,'ma60':float(c.tail(60).mean()) if len(c)>=60 else None,'price':float(c.iloc[-1])})
    o['trend']='多頭' if o['ma20'] and o['ma60'] and o['price']>o['ma20']>o['ma60'] else ('空頭' if o['ma20'] and o['ma60'] and o['price']<o['ma20']<o['ma60'] else '震盪')
    lo=c.tail(20).min() if len(c)>=20 else c.min();o['recent_low']=float(lo);o['distance_low']=o['price']/lo-1 if lo else None;return o

# ---------- chips / margin ----------
def parse_t86(data):
    out={}
    if not isinstance(data,dict):return out
    fields=data.get('fields',[])
    for r in data.get('data',[]):
        if not isinstance(r,list):continue
        o=dict(zip(fields,r));c=clean_code(first_value(o,['證券代號','公司代號']));f=find_value(o,['外陸資買賣超股數(不含外資自營商)','外資及陸資買賣超股數']);t=find_value(o,['投信買賣超股數']);d=find_value(o,['自營商買賣超股數']);
        if c:out[c]={'foreign':f,'trust':t,'dealer':d,'total':sum(x for x in (f,t,d) if x is not None)}
    return out
def institutional(code,market,days=20):
    """批次取得法人資料，並持久化到 CHIP_HISTORY_FILE。
    不再對同一日期反覆重試；成功過的日期直接使用快取。
    """
    key=('inst',market,days)
    if key in INSTITUTIONAL_CACHE:return INSTITUTIONAL_CACHE[key]
    history=load_json(CHIP_HISTORY_FILE)
    market_hist=history.setdefault(market,{})
    # T86 通常在收盤後才完整；不要把今天尚未公布的資料算進20日。
    today=datetime.now(TW_TZ).date();dates=[];d=today-timedelta(days=1)
    while len(dates)<days:
        if d.weekday()<5:dates.append(d)
        d-=timedelta(days=1)
    missing=[x for x in dates if x.strftime('%Y%m%d') not in market_hist]
    print(f'法人資料：{market} 已有 {len(dates)-len(missing)}/{days} 日快取，需補 {len(missing)} 日')
    from concurrent.futures import ThreadPoolExecutor,as_completed
    def fetch(dt):
        ds=dt.strftime('%Y%m%d')
        if market=='TPEX':
            x=tpex_get('/tpex_3insti_daily_trading')
            return ds,parse_tpex_inst(x) if x else {}
        # TWSE 網頁 T86 對日期查詢較穩定；單次 timeout，不做多次重試。
        x=http_json(TWSE_WEB_BASE+'/fund/T86',{'date':ds,'selectType':'ALL','response':'json'},timeout=4,retries=0)
        return ds,parse_t86(x) if x else {}
    if missing:
        with ThreadPoolExecutor(max_workers=min(5,len(missing))) as ex:
            futs=[ex.submit(fetch,x) for x in missing]
            for f in as_completed(futs):
                try:
                    ds,data=f.result()
                    if data:market_hist[ds]=data
                except Exception as e:print('法人批次失敗：',e)
        save_json(CHIP_HISTORY_FILE,history)
    result=[{'date':dt.strftime('%Y%m%d'),'data':market_hist[dt.strftime('%Y%m%d')]} for dt in dates if dt.strftime('%Y%m%d') in market_hist]
    INSTITUTIONAL_CACHE[key]=result
    print(f'法人資料完成：{len(result)} 個交易日')
    return result

def parse_tpex_inst(data):
    out={}
    if not isinstance(data,list):return out
    for r in data:
        c=clean_code(first_value(r,['SecuritiesCompanyCode','Code','證券代號']))
        if not c:continue
        def net(b,s):
            a=find_value(r,b);z=find_value(r,s);return a-z if a is not None and z is not None else None
        f=net(['Foreign Investors include Mainland Area Investors (Foreign Dealers excluded)-Total Buy','ForeignInvestors-TotalBuy','Foreign Buy'],['Foreign Investors include Mainland Area Investors (Foreign Dealers excluded)-Total Sell','ForeignInvestors-TotalSell','Foreign Sell'])
        t=net(['SecuritiesInvestmentTrustCompanies-TotalBuy','InvestmentTrust-TotalBuy'],['SecuritiesInvestmentTrustCompanies-TotalSell','InvestmentTrust-TotalSell'])
        d=net(['Dealers-TotalBuy','Dealer-TotalBuy'],['Dealers-TotalSell','Dealer-TotalSell'])
        out[c]={'foreign':f,'trust':t,'dealer':d,'total':sum(x for x in (f,t,d) if x is not None)}
    return out

def chip_sums(code,h):
    v=[x.get('data',{}).get(code,{}).get('total') for x in h]
    v=[x for x in v if x is not None]
    return {'latest':v[0] if v else None,'5d':sum(v[:5]) if len(v)>=5 else None,'20d':sum(v[:20]) if len(v)>=20 else None}

def parse_margin_row(row):
    """解析 TWSE/TPEX 融資融券個股資料；同時支援中英文欄位。"""
    if not isinstance(row,dict):
        return {'margin_change':None,'margin_balance':None,'short_change':None,'short_balance':None}
    margin_today=find_value(row,['融資今日餘額','今日融資餘額','MarginTodayBalance','MarginBalance','margin_balance','融資餘額','MarginPurchaseTodayBalance'])
    margin_prev=find_value(row,['融資前日餘額','前日融資餘額','MarginPreviousBalance','PreviousMarginBalance','融資昨日餘額','MarginPurchasePreviousBalance'])
    short_today=find_value(row,['融券今日餘額','今日融券餘額','ShortTodayBalance','ShortBalance','short_balance','融券餘額','ShortSaleTodayBalance'])
    short_prev=find_value(row,['融券前日餘額','前日融券餘額','ShortPreviousBalance','PreviousShortBalance','融券昨日餘額','ShortSalePreviousBalance'])
    # 某些 API 只提供今日/前日餘額，變化直接自行計算。
    mc=(margin_today-margin_prev) if margin_today is not None and margin_prev is not None else find_value(row,['融資增減','融資變化','MarginChange','margin_change'])
    sc=(short_today-short_prev) if short_today is not None and short_prev is not None else find_value(row,['融券增減','融券變化','ShortChange','short_change'])
    return {'margin_change':mc,'margin_balance':margin_today,'short_change':sc,'short_balance':short_today}

def _parse_margin_payload(data):
    """把 TWSE MI_MARGN 可能出現的 list/dict/table 格式統一成 {code: row}。"""
    out={}
    def consume_rows(rows,fields=None):
        if not isinstance(rows,list): return
        for r in rows:
            if isinstance(r,dict):
                o=r
                c=clean_code(first_value(o,['股票代號','證券代號','公司代號','Code','SecuritiesCompanyCode']))
                if c and c.isdigit(): out[c]=parse_margin_row(o)
            elif isinstance(r,list) and fields:
                o=dict(zip(fields,r))
                c=clean_code(first_value(o,['股票代號','證券代號','公司代號','Code','SecuritiesCompanyCode']))
                if c and c.isdigit(): out[c]=parse_margin_row(o)
    if isinstance(data,list):
        consume_rows(data)
        # 部分報表格式第一列是欄位名稱
        if data and isinstance(data[0],list):
            fields=[str(x) for x in data[0]]
            consume_rows(data[1:],fields)
    elif isinstance(data,dict):
        fields=data.get('fields',[])
        rows=data.get('data',[])
        consume_rows(rows,fields)
        # MI_MARGN 舊格式可能 data 裡還包一層 table/header
        if isinstance(rows,list):
            for table in rows:
                if isinstance(table,list) and table and isinstance(table[0],list):
                    header=[str(x) for x in table[0]]
                    consume_rows(table[1:],header)
    return out

def margin_data(code,market):
    key=('margin',market)
    if key not in MARGIN_CACHE:
        out={}
        try:
            if market=='TPEX':
                data=tpex_get('/tpex_mainboard_margin_balance')
                out=_parse_margin_payload(data)
            else:
                # TWSE OpenAPI 官方 MI_MARGN：全市場一次取得，不逐股查詢。
                data=twse_get('/exchangeReport/MI_MARGN')
                out=_parse_margin_payload(data)
                # OpenAPI 偶發回傳空資料時，使用官方網頁 JSON 作為 fallback。
                if not out:
                    data=twse_web_get('/exchangeReport/MI_MARGN',{'response':'json','selectType':'ALL'})
                    out=_parse_margin_payload(data)
        except Exception as e:
            print('融資融券批次失敗：',e)
        MARGIN_CACHE[key]=out
        print(f'融資融券資料完成：{market} {len(out)} 檔（只抓一次）')
    return MARGIN_CACHE[key].get(code,{'margin_change':None,'margin_balance':None,'short_change':None,'short_balance':None})


# ---------- scoring ----------
def score_fund(pe,one,peer,peg,roe,eps,pb,yld,model):
    s=0;why=[]
    if model.get('pe'):
        if pe is not None and one is not None:
            r=pe/one
            if r<=.9:s+=10;why.append('低於自身歷史PE')
            elif r<=1.05:s+=7;why.append('接近自身歷史PE')
            elif r<=1.15:s+=4
        if pe is not None and peer is not None:
            r=pe/peer
            if r<.85:s+=10;why.append('低於同業中位數')
            elif r<=1.05:s+=7;why.append('接近同業中位數')
            elif r<=1.15:s+=3
    if peg is not None:
        if peg<.8:s+=6
        elif peg<1:s+=5
        elif peg<1.2:s+=3
    if roe is not None:
        if roe>=30:s+=6
        elif roe>=20:s+=5
        elif roe>=10:s+=3
    if eps is not None:
        if eps>=50:s+=6
        elif eps>=20:s+=5
        elif eps>0:s+=3
    if pb is not None and model.get('pb'):
        if pb<2:s+=4
        elif pb<4:s+=2
    if yld is not None and model.get('yield'):
        if yld>=5:s+=3
        elif yld>=3:s+=2
    return min(40,s),why
def score_tech(t):
    s=0;reasons=[]
    r=t['rsi'];k,d=t['k'],t['d'];p=t['price'];m20=t['ma20'];m60=t['ma60'];dist=t['distance_low']
    if r is not None:
        if 30<=r<=45:s+=7;reasons.append('RSI偏低')
        elif 45<r<=60:s+=6
        elif r<70:s+=4
    if k is not None and d is not None:
        if k<30 and k>d:s+=7;reasons.append('KD低檔轉強')
        elif k<40:s+=5
        elif k>d:s+=4
    if p and m20:
        if p>=m20:s+=5
        elif p>=m20*.97:s+=3
    if p and m60:
        if p>=m60:s+=5
        elif p>=m60*.95:s+=3
    if t['trend']=='多頭':s+=3
    elif t['trend']=='震盪':s+=2
    if dist is not None:
        if dist<=.03:s+=3;reasons.append('接近20日低點')
        elif dist<=.08:s+=2
    return min(30,s),reasons
def score_chip(c,m):
    s=0;r=[]
    for key,w in [('latest',5),('5d',6),('20d',7)]:
        v=c.get(key)
        if v is not None:
            if v>0:s+=w
            elif v<0 and key=='20d':r.append('法人20日賣超')
    mc=m.get('margin_change');sc=m.get('short_change')
    if mc is not None:
        if mc<0:s+=1
        elif mc>0:r.append('融資增加')
    if sc is not None and sc>0:s+=1
    return min(20,s),r
def score_risk(t,c,m):
    risk=0;r=[]
    if t.get('rsi') is not None and t['rsi']>70:risk+=3;r.append('RSI過熱')
    if t.get('k') is not None and t.get('d') is not None and t['k']>80 and t['d']>80:risk+=2;r.append('KD高檔')
    if c.get('20d') is not None and c['20d']<0:risk+=2;r.append('法人連續賣超')
    if m.get('margin_change') is not None and m['margin_change']>0:risk+=1;r.append('融資增加')
    if t.get('price') and t.get('ma20') and t['price']<t['ma20']:risk+=1;r.append('跌破MA20')
    if t.get('price') and t.get('ma60') and t['price']<t['ma60']:risk+=1;r.append('跌破MA60')
    return min(10,risk),r

def analysis(query,u,backfill=True,interval_result=None):
    item=resolve_stock(query,u)
    if not item:return f'❌ 找不到股票：{query}'
    code=item['code'];name=item['name'];market=item['market'];industry=canonical_industry(item['industry']);symbol=item['symbol']
    pe_data=get_current_pe_data();h=load_json(PE_HISTORY_FILE)
    if backfill:h=backfill_pe(code,h,market);save_json(PE_HISTORY_FILE,h)
    yf_f=yahoo_fund(symbol);off=pe_data.get(code,{})
    pe=off.get('pe') or yf_f.get('pe');pb=off.get('pb') or yf_f.get('pb');yld=off.get('yield') or yf_f.get('yield');one,sample=one_year_pe(code,h)
    peers=get_dynamic_industry_peers(code,industry,u,10);vals=[pe_data.get(x['code'],{}).get('pe') for x in peers];vals=[x for x in vals if x and 0<x<=PE_MAX_VALID];peer_mean=sum(vals)/len(vals) if vals else None;peer_med=float(np.median(vals)) if vals else None
    tech=technical(symbol);inst=chip_sums(code,institutional(code,market,20));margin=margin_data(code,market);fs,fr=score_fund(pe,one,peer_med,yf_f['peg'],yf_f['roe'],yf_f['eps_growth'],pb,yld,INDUSTRY_MODEL.get(industry,DEFAULT_MODEL));ts,tr=score_tech(tech);cs,cr=score_chip(inst,margin);risk,rr=score_risk(tech,inst,margin);total=max(0,min(100,fs+ts+cs+(10-risk)))
    if total>=90:verdict='🟢 強力加碼'
    elif total>=75:verdict='🟢 可分批加碼'
    elif total>=60:verdict='🟡 等待回檔/止跌'
    elif total>=40:verdict='🟠 暫緩加碼'
    else:verdict='🔴 不建議加碼'
    interval=u.get(code,{}).get('price')
    # run_alerts 已經先嘗試過區間資料；即使失敗也不要在 analysis 再打一次 API。
    # webhook/analyze 模式沒有傳入 interval_result 時，才自行讀取 state 並嘗試一次。
    if interval_result is None and not RUN_CACHE.get(('interval_attempted',symbol),False):
        st=load_json(STATE_FILE).get('interval_low',{}).get(next((k for k,v in STOCKS.items() if clean_code(v)==code),''),{})
        if st.get('last_check') and st.get('last_price'):
            z=get_interval_stats(symbol,st.get('last_check'))
            RUN_CACHE[('interval_attempted',symbol)]=True
            if z: interval_result={'previous_price':st.get('last_price'),'interval_low':z['low'],'drop':z['low']/st.get('last_price')-1,'start':z['start'].isoformat(),'end':z['end'].isoformat()}
    peer_text='、'.join(f"{x['code']} {x['name']}" for x in peers) or '無法取得市值資料'
    return (f'📊 股票加碼分析 V2.9.7.1\n\n標的：{name}（{code}）\n市場：{market}\n產業：{industry}\n\n'
            f'【估值 / 基本面 40分】\nPE：{fmt(pe)}\n一年平均PE：{fmt(one)}（樣本 {sample}）\n同業Top10平均PE：{fmt(peer_mean)}\n同業Top10中位數PE：{fmt(peer_med)}（有效 {len(vals)}/10）\nPB：{fmt(pb)}\n殖利率：{fmt(yld)}%\nEPS成長：{fmt(yf_f["eps_growth"])}%\nPEG：{fmt(yf_f["peg"])}\nROE：{fmt(yf_f["roe"])}%\n基本面得分：{fs}/40\n\n'
            f'【動態同業 Top 10】\n{peer_text}\n\n'
            f'【技術面 30分】\nRSI：{fmt(tech["rsi"])}\nKD：K={fmt(tech["k"])} / D={fmt(tech["d"])}\nMA20：{fmt(tech["ma20"])}\nMA60：{fmt(tech["ma60"])}\n趨勢：{tech["trend"] or "N/A"}\n距20日低點：{pct(tech["distance_low"])}\n技術得分：{ts}/30\n\n'
            f'【籌碼面 20分】\n法人最新：{fmt(inst["latest"],0)} 股\n法人5日：{fmt(inst["5d"],0)} 股\n法人20日：{fmt(inst["20d"],0)} 股\n融資變化：{fmt(margin["margin_change"],0)} 張\n融資餘額：{fmt(margin["margin_balance"],0)} 張\n融券變化：{fmt(margin["short_change"],0)} 張\n融券餘額：{fmt(margin["short_balance"],0)} 張\n籌碼得分：{cs}/20\n\n'
            f'【風險 10分】\n風險扣分：-{risk}\n{("、".join(rr) if rr else "目前無主要風險警訊")}\n\n'
            f'【15分鐘區間】\n上次執行：{datetime.fromisoformat(interval_result["start"]).strftime("%H:%M:%S") if interval_result else "N/A"}\n本次執行：{datetime.fromisoformat(interval_result["end"]).strftime("%H:%M:%S") if interval_result else "N/A"}\n期間最低：{fmt(interval_result["interval_low"]) if interval_result else "N/A"}\n目前價格：{fmt(interval)}\n區間跌幅：{pct(interval_result["drop"]) if interval_result else "N/A"}\n\n'
            f'【加碼決策】\n綜合評分：{total}/100\n結論：{verdict}\n\n'
            f'加分因素：{"、".join(fr+tr) if fr+tr else "無"}\n籌碼訊號：{"、".join(cr) if cr else "無"}\n風險提醒：{"、".join(rr) if rr else "目前無主要風險警訊"}')

# ---------- webhook / main ----------
def handle_event(e,u):
    if e.get('type')!='message' or e.get('message',{}).get('type')!='text':return
    text=e['message']['text'].strip();token=e.get('replyToken')
    if text.lower() in {'help','說明','功能','股票'}:
        reply_line(token,'📈 股票加碼分析 Bot V2.9\n\n輸入股票代號或名稱即可。\n例如：2330、台積電、3711、日月光投控\n\n模型：基本面40 + 技術30 + 籌碼20 + 風險10。');return
    try:r=analysis(text,u,True)
    except Exception as e:traceback.print_exc();r=f'❌ 分析失敗：{e}'
    reply_line(token,r)
def run_webhook_server():
    from flask import Flask,request
    app=Flask(__name__);u=get_market_universe()
    @app.route('/callback',methods=['POST'])
    def cb():
        body=request.get_json(silent=True) or {}
        for e in body.get('events',[]):handle_event(e,u)
        return 'OK',200
    app.run(host='0.0.0.0',port=int(os.environ.get('PORT','8080')))
def run_alerts():
    global RUN_CACHE,INSTITUTIONAL_CACHE,MARGIN_CACHE
    RUN_CACHE={};INSTITUTIONAL_CACHE={};MARGIN_CACHE={}
    started=time.time()
    print('================================\n股票跌幅 + 15分鐘區間最低價 + V2.9.7.1自動估值 + 技術 + 籌碼\n================================')
    state=load_json(STATE_FILE);u=get_market_universe()
    print(f'[耗時 {time.time()-started:.1f}s] 股票池完成：{len(u)}')
    # 只預抓真正會分析的 TWSE/TPEx 個股法人與融資資料；每種市場各抓一次。
    target_markets=set()
    for name,symbol in STOCKS.items():
        c=clean_code(symbol)
        if c in u:target_markets.add(u[c].get('market'))
    for m in target_markets:
        if m in ('TWSE','TPEX'):
            institutional('2330' if m=='TWSE' else next((x['code'] for x in u.values() if x.get('market')==m),''),m,20)
            margin_data('2330' if m=='TWSE' else next((x['code'] for x in u.values() if x.get('market')==m),''),m)
    print(f'[耗時 {time.time()-started:.1f}s] 法人/融資批次資料完成')
    for name,symbol in STOCKS.items():
        print(f'\n========== {name} ==========')
        item=u.get(clean_code(symbol))
        try:
            check_drop_alert(name,symbol,state); RUN_CACHE[('interval_attempted',symbol)]=True; interval_result=check_interval_low(name,symbol,state)
        except Exception as e: print('價格/通知失敗：',e); interval_result=None
        try:
            if symbol.startswith('^'):
                t=technical(symbol);print(f'\n📈 指數分析\n標的：{name}\n目前價格：{fmt(get_latest_price(symbol))}\nKD：K={fmt(t["k"])} / D={fmt(t["d"])}\nRSI：{fmt(t["rsi"])}')
            elif name in ('0050 元大台灣50','QQQ'):
                t=technical(symbol);print(f'\n📊 ETF分析\n標的：{name}\n目前價格：{fmt(get_latest_price(symbol))}\nRSI：{fmt(t["rsi"])}')
            elif item:
                print('\n'+analysis(name,u,False,interval_result))
            else:
                print(f'⚠️ {name} 不在動態股票池，跳過詳細估值')
        except Exception as e:print(f'{name} 分析失敗：{e}');traceback.print_exc()
    save_json(STATE_FILE,state)
    print(f'\n========== 完成｜總耗時 {time.time()-started:.1f} 秒 ==========' )
def main():
    import sys
    if len(sys.argv)>1 and sys.argv[1].lower()=='webhook':run_webhook_server()
    elif len(sys.argv)>1 and sys.argv[1].lower()=='refresh':get_market_universe(True)
    elif len(sys.argv)>1 and sys.argv[1].lower()=='analyze':print(analysis(' '.join(sys.argv[2:]),get_market_universe(),True))
    else:run_alerts()
if __name__=='__main__':main()
