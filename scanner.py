#!/usr/bin/env python3
"""Cautious eMAG public sitemap scanner; no login, proxies or bot bypass."""
import os,re,json,time,random,csv,io,datetime,urllib.parse,urllib.robotparser,xml.etree.ElementTree as ET
from collections import deque
import requests
from bs4 import BeautifulSoup

BASE='https://www.emag.bg/'
UA='EmagPublicPriceResearch/1.0 (respectful sitemap reader)'
MAX_PRODUCTS=int(os.getenv('MAX_PRODUCTS','150'))
MAX_SITEMAPS=int(os.getenv('MAX_SITEMAPS','40'))
DELAY=float(os.getenv('REQUEST_DELAY','5'))
TIMEOUT=18
MIN_DIFF=50
DATA='docs/results.json'
SESSION=requests.Session(); SESSION.headers.update({'User-Agent':UA,'Accept-Language':'bg-BG,bg;q=0.9'})
state={'status':'starting','checked':0,'discovered_urls':0,'errors':[],'updated_utc':None,'items':[]}

def request(url):
    time.sleep(DELAY)
    r=SESSION.get(url,timeout=TIMEOUT,allow_redirects=True)
    if r.status_code in (401,403,429,503,511): raise PermissionError(f'HTTP {r.status_code} за {url}')
    r.raise_for_status()
    if urllib.parse.urlparse(r.url).hostname not in ('emag.bg','www.emag.bg'): raise ValueError('Извън emag.bg')
    return r

def amount(s):
    s=re.sub(r'[^\d,.]','',str(s))
    if not s: return None
    if ',' in s and '.' in s:
        s=s.replace('.','').replace(',','.') if s.rfind(',')>s.rfind('.') else s.replace(',','')
    elif ',' in s: s=s.replace(',','.')
    try:return float(s)
    except ValueError:return None

def eur(value,unit):
    if value is None:return None
    unit=unit.lower()
    return round(value/1.95583,2) if ('лв' in unit or 'bgn' in unit) else round(value,2)

def extract(html,url):
    soup=BeautifulSoup(html,'html.parser')
    for tag in soup(['script','style','noscript']):
        if tag.name!='script' or tag.get('type','').lower()!='application/ld+json':tag.decompose()
    txt=soup.get_text(' ',strip=True)
    # Require explicit PCD shown on the rendered HTML; never substitute crossed-out/list prices.
    m=re.search(r'ПЦД\s*:?\s*([\d\s\u00a0.,]+?)\s*(€|EUR|лв\.?|BGN)',txt,re.I)
    if not m:return None
    pcd=eur(amount(m.group(1)),m.group(2))
    if not pcd or pcd<=0:return None
    current=None
    # Prefer structured offer price, but only if currency identified.
    for script in soup.find_all('script',type='application/ld+json'):
        try: data=json.loads(script.string or script.get_text())
        except (ValueError,TypeError):continue
        stack=[data]
        while stack:
            obj=stack.pop()
            if isinstance(obj,list):stack.extend(obj);continue
            if not isinstance(obj,dict):continue
            stack.extend(v for v in obj.values() if isinstance(v,(dict,list)))
            if 'price' in obj and obj.get('priceCurrency') in ('EUR','BGN'):
                value=eur(amount(obj['price']),obj['priceCurrency'])
                if value is not None and value>=0 and (current is None or value<current):current=value
    if current is None:
        # visible selling price cues; skip if none (do not guess from other numbers)
        for pat in (r'(?:Цена|Текуща цена|Специална цена)\s*:?\s*([\d\s\u00a0.,]+?)\s*(€|EUR|лв\.?|BGN)',):
            z=re.search(pat,txt,re.I)
            if z: current=eur(amount(z.group(1)),z.group(2));break
    if current is None or current<0 or current>=pcd:return None
    pct=round(100*(pcd-current)/pcd,2)
    if pct<MIN_DIFF:return None
    h=soup.select_one('h1')
    title=(h.get_text(' ',strip=True) if h else (soup.title.get_text(' ',strip=True) if soup.title else url))
    return {'name':title[:240],'url':url,'pcd_eur':pcd,'price_eur':current,'difference_eur':round(pcd-current,2),'difference_percent':pct}

def save():
    state['updated_utc']=datetime.datetime.now(datetime.timezone.utc).isoformat(timespec='seconds')
    os.makedirs('docs',exist_ok=True)
    with open(DATA,'w',encoding='utf8') as f:json.dump(state,f,ensure_ascii=False,indent=2)
    with open('docs/results.csv','w',encoding='utf-8-sig',newline='') as f:
        w=csv.writer(f);w.writerow(['Продукт','Линк','ПЦД €','Цена €','Разлика €','Разлика %'])
        for p in state['items']:w.writerow([p['name'],p['url'],p['pcd_eur'],p['price_eur'],p['difference_eur'],p['difference_percent']])

def main():
    try:
        robots_url=urllib.parse.urljoin(BASE,'robots.txt')
        response=request(robots_url)
        rules=urllib.robotparser.RobotFileParser(); rules.parse(response.text.splitlines())
        if not rules.can_fetch(UA,BASE):
            state['status']='blocked_by_robots';return
        sitemaps=re.findall(r'^Sitemap:\s*(\S+)',response.text,re.I|re.M)
        # Defaults are probes only if robots allows them; not evidence sitemap actually exists.
        if not sitemaps:sitemaps=[urllib.parse.urljoin(BASE,'sitemap.xml')]
        queue=deque(sitemaps); visited=set();product_urls=[];seen_urls=set();count=0
        while queue and count<MAX_SITEMAPS and len(product_urls)<MAX_PRODUCTS*5:
            sm=queue.popleft()
            if sm in visited:continue
            visited.add(sm);count+=1
            if urllib.parse.urlparse(sm).hostname not in ('emag.bg','www.emag.bg'):continue
            if not rules.can_fetch(UA,sm):continue
            try:
                resp=request(sm)
                if len(resp.content)>15_000_000:continue
                root=ET.fromstring(resp.content)
            except PermissionError:raise
            except Exception as e:
                state['errors'].append(f'Sitemap {sm}: {str(e)[:110]}');continue
            for loc in root.iter():
                if loc.tag.split('}')[-1]!='loc' or not loc.text:continue
                link=loc.text.strip(); host=urllib.parse.urlparse(link).hostname
                if host not in ('emag.bg','www.emag.bg'):continue
                if link.lower().endswith(('.xml','.xml.gz')):
                    if link not in visited:queue.append(link)
                elif '/pd/' in link and link not in seen_urls:
                    product_urls.append(link);seen_urls.add(link)
        state['discovered_urls']=len(product_urls)
        if not product_urls:
            state['status']='no_product_urls_found';return
        old={p['url']:p for p in state['items']}
        for url in product_urls[:MAX_PRODUCTS]:
            if not rules.can_fetch(UA,url):continue
            try:
                resp=request(url)
                item=extract(resp.text,url)
                if item:old[url]=item
                elif url in old:del old[url]
                state['checked']+=1
            except PermissionError:raise
            except Exception as e:state['errors'].append(f'Продукт {url}: {str(e)[:95]}')
        state['items']=sorted(old.values(),key=lambda p:p['difference_percent'],reverse=True)
        state['status']='completed'
    except PermissionError as e:
        state['status']='access_blocked';state['errors'].append(str(e))
    except requests.RequestException as e:
        state['status']='network_error';state['errors'].append(str(e))
    except Exception as e:
        state['status']='error';state['errors'].append(str(e))
    finally:save();print(json.dumps({k:v for k,v in state.items() if k!='items'},ensure_ascii=False))

if __name__=='__main__':main()
