"""
FlyUSS NY SEO Agent v1
Publishes branded New York-specific landing pages (JFK, LGA, EWR)
directly onto the ny.flyuss.com GitHub Pages repo.
50 pages/day — trend queries scoped to New York metro area.
"""

import os
import json
import time
import random
import hashlib
import requests
import xml.etree.ElementTree as ET
from datetime import datetime
from pathlib import Path

# ══════════════════════════════════════════
# CONFIG
# ══════════════════════════════════════════
GEMINI_API_KEY             = os.environ.get('GEMINI_API_KEY', '').strip()
GROQ_API_KEY               = os.environ.get('GROQ_API_KEY', '').strip()

# Groq key rotation — reads GROQ_API_KEY, GROQ_API_KEY_2, _3, _4, _5... with no
# upper limit, so adding more keys in GitHub Secrets just works without a code change.
def _build_groq_keys():
    keys = []
    if GROQ_API_KEY:
        keys.append(GROQ_API_KEY)
    i = 2
    while True:
        k = os.environ.get(f'GROQ_API_KEY_{i}', '').strip()
        if not k:
            break
        keys.append(k)
        i += 1
    return keys

_GROQ_KEYS = _build_groq_keys()
import itertools as _itertools
_groq_cycle = _itertools.cycle(_GROQ_KEYS) if _GROQ_KEYS else None
GITHUB_TOKEN               = os.environ.get('GH_TOKEN', os.environ.get('GITHUB_TOKEN', '')).strip()
# Separate secret for the NY subdomain repo — set NY_PAGES_REPO in GitHub Secrets
GITHUB_PAGES_REPO          = os.environ.get('NY_PAGES_REPO', '').strip()
BING_INDEXNOW_KEY          = os.environ.get('NY_INDEXNOW_KEY', os.environ.get('BING_INDEXNOW_KEY', '')).strip()
GOOGLE_SERVICE_ACCOUNT_KEY = os.environ.get('GOOGLE_SERVICE_ACCOUNT_KEY', '')
MODEL        = 'gemini-2.0-flash-lite'
MAX_PER_RUN  = 5    # 5 articles/run — every 30 min → 10 runs/day covers the 50/day quota
MAX_GITHUB   = 50
MAX_GOOGLE   = 30
MAX_BING     = 30
OUTPUT_DIR   = Path('pages')
SLUGS_FILE   = Path('ny_published_slugs.json')
QUEUE_FILE   = Path('ny_daily_queue.json')
OUTPUT_DIR.mkdir(exist_ok=True)

# This repo is served at ny.flyuss.com via CNAME
SITE_BASE_URL = os.environ.get('NY_SITE_URL', 'https://ny.flyuss.com').rstrip('/')

# ══════════════════════════════════════════
# RETRY HELPER — survives 403/429/timeouts from
# Bing/Google/FAA when called from GitHub Actions IPs
# ══════════════════════════════════════════
_DEFAULT_HEADERS_POOL = [
    'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36',
    'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.4 Safari/605.1.15',
    'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36',
]


def fetch_with_retry(url, method='get', headers=None, json_body=None, data=None,
                      timeout=15, retries=3, backoff=2.0):
    """
    GET/POST with retry + exponential backoff + UA rotation.
    Treats 403/429/5xx as retryable (common from datacenter IPs on
    GitHub Actions hitting Bing/Google), real network errors too.
    Returns the response object, or None if all retries failed.
    """
    last_exc = None
    for attempt in range(retries):
        try:
            hdrs = dict(headers or {})
            hdrs.setdefault('User-Agent', random.choice(_DEFAULT_HEADERS_POOL))
            if method == 'post':
                r = requests.post(url, headers=hdrs, json=json_body, data=data, timeout=timeout)
            else:
                r = requests.get(url, headers=hdrs, timeout=timeout)
            if r.status_code in (403, 429, 500, 502, 503, 504):
                wait = backoff * (2 ** attempt) + random.uniform(0, 1)
                print(f'    [retry] {url[:60]} → {r.status_code}, waiting {wait:.1f}s (attempt {attempt+1}/{retries})')
                time.sleep(wait)
                last_exc = None
                continue
            return r
        except Exception as e:
            last_exc = e
            wait = backoff * (2 ** attempt) + random.uniform(0, 1)
            print(f'    [retry] {url[:60]} → {e}, waiting {wait:.1f}s (attempt {attempt+1}/{retries})')
            time.sleep(wait)
    if last_exc:
        print(f'    [retry] giving up on {url[:60]}: {last_exc}')
    return None

# ══════════════════════════════════════════
# AIRLINES
# ══════════════════════════════════════════
AIRLINES = {
    'delta':    {'name': 'Delta Air Lines',   'phone': '(844) 833-1051', 'tel': 'tel:+18448331051', 'rpm': 28, 'color': '#ff1744'},
}

# ══════════════════════════════════════════
# AIRPORT WEIGHT DATABASE
# pass=passengers(M), fday=flights/day, delay=delay_freq(%), news=news_sensitivity(1-10)
# ══════════════════════════════════════════
AIRPORTS_DB = [
    # NEW YORK METRO ONLY
    {'c':'JFK','n':'New York JFK',       's':'NY','t':1,'pass':71, 'fday':1200,'delay':30,'news':10},
    {'c':'LGA','n':'New York LaGuardia', 's':'NY','t':2,'pass':31, 'fday':600, 'delay':35,'news':10},
    {'c':'EWR','n':'Newark',             's':'NJ','t':2,'pass':46, 'fday':750, 'delay':32,'news':9},
]

# ══════════════════════════════════════════
# AIRPORT CLUSTERS
# ══════════════════════════════════════════
CLUSTERS = [
    {'name': 'New York Metro', 'airports': ['JFK','LGA','EWR'], 'states': ['NY','NJ'], 'weight': 100},
]

# ══════════════════════════════════════════
# NATIONAL IMPACT AIRPORTS (triggers at 3+)
# ══════════════════════════════════════════
NATIONAL_AIRPORTS = ['JFK', 'LGA', 'EWR']

# ══════════════════════════════════════════
# 15 KEY STATES
# ══════════════════════════════════════════
STATES_15 = [
    {'name':'New York',    'code':'NY', 'airports':['JFK','LGA']},
    {'name':'New Jersey',  'code':'NJ', 'airports':['EWR']},
]

# ══════════════════════════════════════════
# BREAKING EVENT CATEGORIES
# ══════════════════════════════════════════
EVENTS_CRITICAL = [
    {'n': 'FAA Ground Stop',     'priority': 'MAX', 'lifetime_h': 6},
    {'n': 'ATC Outage',          'priority': 'MAX', 'lifetime_h': 6},
    {'n': 'Airport Closure',     'priority': 'MAX', 'lifetime_h': 12},
    {'n': 'Mass Cancellations',  'priority': 'MAX', 'lifetime_h': 24},
    {'n': 'IT System Failure',   'priority': 'MAX', 'lifetime_h': 6},
]
EVENTS_HIGH = [
    {'n': 'Runway Closure',      'priority': 'HIGH', 'lifetime_h': 12},
    {'n': 'Security Incident',   'priority': 'HIGH', 'lifetime_h': 6},
    {'n': 'Emergency Landing',   'priority': 'HIGH', 'lifetime_h': 6},
    {'n': 'Mass Delays',         'priority': 'HIGH', 'lifetime_h': 12},
    {'n': 'Weather Event',       'priority': 'HIGH', 'lifetime_h': 24},
]
EVENTS_MEDIUM = [
    {'n': 'Flight Diversion',    'priority': 'MEDIUM', 'lifetime_h': 12},
    {'n': 'Strike Warning',      'priority': 'MEDIUM', 'lifetime_h': 72},
    {'n': 'Hurricane Alert',     'priority': 'MEDIUM', 'lifetime_h': 48},
    {'n': 'Overbooking Surge',   'priority': 'MEDIUM', 'lifetime_h': 24},
    {'n': 'Fuel Issue',          'priority': 'MEDIUM', 'lifetime_h': 12},
]
ALL_EVENTS = EVENTS_CRITICAL + EVENTS_HIGH + EVENTS_MEDIUM

# ══════════════════════════════════════════
# ARTICLE IMAGES
# ══════════════════════════════════════════
AIRLINE_IMAGES = {
    'delta':    'https://images.unsplash.com/photo-1436491865332-7a61a109cc05?w=1200&q=80',
}

TITLE_TEMPLATES = [
    lambda a,c,ap,ph: f"{a} Flights from {c} {ap} — Book, Change & Cancel",
    lambda a,c,ap,ph: f"{a} Customer Service in {c} — Call {ph} for Help",
    lambda a,c,ap,ph: f"Need Help With an {a} Flight at {ap}? Call {ph}",
    lambda a,c,ap,ph: f"{a} Cancelled Your Flight in {c}? Here's What To Do",
    lambda a,c,ap,ph: f"How To Get Your {a} Refund — {c} Travelers Guide",
    lambda a,c,ap,ph: f"{a} Delay at {ap}? You May Be Entitled To Compensation",
    lambda a,c,ap,ph: f"{a} Flight Assistance in {c} — Real Agents: {ph}",
    lambda a,c,ap,ph: f"{c} {ap} Travelers: {a} Booking & Support Line — {ph}",
    # High call-intent — solution-focused, local
    lambda a,c,ap,ph: f"{a} Won't Refund My Ticket — {c} Passengers Call {ph}",
    lambda a,c,ap,ph: f"How To Get {a} Refund Fast — {c} Passengers {datetime.now().year}",
    lambda a,c,ap,ph: f"{a} Customer Service Not Answering in {c}? Try {ph}",
    lambda a,c,ap,ph: f"Flight Cancelled at {ap}? Here's How Much {a} Owes You",
    lambda a,c,ap,ph: f"Missed {a} Connection at {c}? You're Owed Compensation — {ph}",
    lambda a,c,ap,ph: f"{a} Overbooked Flight at {ap} — Get $700-$1,550 Compensation",
    lambda a,c,ap,ph: f"How Long Does {a} Refund Take? {c} Passengers Read This",
    lambda a,c,ap,ph: f"{a} Cancelled — Do I Get a Full Refund? {c} Guide {datetime.now().year}",
    lambda a,c,ap,ph: f"Need To Rebook an {a} Flight at {ap}? Call Specialists: {ph}",
    lambda a,c,ap,ph: f"{a} Flight at {ap} Delayed 3+ Hours — What You're Owed",
    lambda a,c,ap,ph: f"How To Get {a} To Rebook Your Flight — {c} Tips",
    lambda a,c,ap,ph: f"{a} Denied Boarding at {ap}? Claim Up To $1,550 — {ph}",
]

# ══════════════════════════════════════════
# DUPLICATE PREVENTION
# ══════════════════════════════════════════
DAILY_LIMIT  = 50  # 5 categories × 10/day, per user's confirmed quota split

def load_slugs():
    if SLUGS_FILE.exists():
        with open(SLUGS_FILE) as f:
            data = json.load(f)
            if isinstance(data, dict):
                slugs = set(data.get('slugs', []))
                daily = data.get('daily', {})
                daily_blogger = data.get('daily_blogger', {})
                dated = data.get('dated', {})  # slug -> date added
                platform_map = data.get('platform', {})  # slug -> 'github'|'blogger'
            else:
                return set(data), {}, {}, {}

        # Auto-cleanup: remove slugs older than 7 days
        today = datetime.now().strftime('%Y-%m-%d')
        cutoff = (datetime.now() - __import__('datetime').timedelta(days=7)).strftime('%Y-%m-%d')
        before = len(slugs)
        slugs_to_keep = set()
        for slug in slugs:
            added = dated.get(slug, today)
            if added >= cutoff:
                slugs_to_keep.add(slug)
        removed = before - len(slugs_to_keep)
        if removed > 0:
            print(f'[Cleanup] Removed {removed} slugs older than 7 days — {len(slugs_to_keep)} remaining')
        platform_map = {k: v for k, v in platform_map.items() if k in slugs_to_keep}
        return slugs_to_keep, daily, platform_map, daily_blogger
    return set(), {}, {}, {}

def save_slugs(slugs, daily=None, dated=None, platform_map=None, daily_blogger=None):
    today = datetime.now().strftime('%Y-%m-%d')
    # Load existing dated map
    existing_dated = {}
    existing_platform = {}
    if SLUGS_FILE.exists():
        try:
            with open(SLUGS_FILE) as f:
                data = json.load(f)
                if isinstance(data, dict):
                    existing_dated = data.get('dated', {})
                    existing_platform = data.get('platform', {})
        except:
            pass
    # Add today's date for new slugs
    for slug in slugs:
        if slug not in existing_dated:
            existing_dated[slug] = today
    # Merge in new platform info
    existing_platform.update(platform_map or {})
    # Keep only entries for current slugs
    existing_dated = {k: v for k, v in existing_dated.items() if k in slugs}
    existing_platform = {k: v for k, v in existing_platform.items() if k in slugs}
    with open(SLUGS_FILE, 'w') as f:
        json.dump({'slugs': list(slugs), 'daily': daily or {}, 'daily_blogger': daily_blogger or {}, 'dated': existing_dated, 'platform': existing_platform}, f)

def get_today_count(daily):
    today = datetime.now().strftime('%Y-%m-%d')
    return daily.get(today, 0)

def update_today_count(daily, count):
    today = datetime.now().strftime('%Y-%m-%d')
    daily[today] = count
    return daily

def load_daily_queue():
    """
    Loads today's persisted 50-item quota queue (built once per day by the
    first run) so subsequent runs the same day just consume the next slice
    instead of rebuilding/re-fetching trends from scratch every 30 minutes.
    Returns (queue_list, date_str) — queue_list is [] if no queue exists yet
    or if it's from a previous day (stale).
    """
    today = datetime.now().strftime('%Y-%m-%d')
    if not QUEUE_FILE.exists():
        return [], today
    try:
        with open(QUEUE_FILE) as f:
            data = json.load(f)
        if data.get('date') != today:
            return [], today  # stale — yesterday's queue, ignore
        return data.get('items', []), today
    except Exception:
        return [], today

def save_daily_queue(items, date_str):
    with open(QUEUE_FILE, 'w') as f:
        json.dump({'date': date_str, 'items': items}, f)

def make_slug(text):
    import re
    return re.sub(r'[^a-z0-9]+', '-', text.lower()).strip('-')[:70]

def is_similar_slug(new_slug, published, threshold=0.6):
    """
    Check if new_slug is too similar to any published slug.
    Uses word overlap — prevents near-duplicate content.
    threshold=0.6 means 60% word overlap = duplicate.
    """
    new_words = set(new_slug.replace('-', ' ').split())
    # Remove common stop words
    stop = {'the','a','an','in','at','for','to','of','and','or','is','are','was','how','get','your','my','do','i','you'}
    new_words -= stop
    if not new_words:
        return False
    for pub in published:
        pub_words = set(pub.replace('-', ' ').replace('.html', '').split())
        pub_words -= stop
        if not pub_words:
            continue
        overlap = len(new_words & pub_words) / max(len(new_words), len(pub_words))
        if overlap >= threshold:
            return True
    return False

# ══════════════════════════════════════════
# AIRPORT WEIGHT SCORE
# ══════════════════════════════════════════
def airport_weight(ap):
    """Calculate airport weight 0-100"""
    score = (
        (ap['pass'] / 104 * 30) +
        (ap['fday'] / 2700 * 25) +
        (ap['delay'] / 35 * 25) +
        (ap['news'] / 10 * 20)
    )
    return round(min(score, 100))

# ══════════════════════════════════════════
# TREND VELOCITY SCORE
# ══════════════════════════════════════════
def velocity_score(search_volume, baseline=20):
    """Calculate velocity % based on search volume vs baseline"""
    if baseline == 0:
        return {'label': 'BREAKOUT', 'score': 100, 'publish': 'NOW — maximum priority'}
    pct = ((search_volume - baseline) / baseline) * 100
    if pct >= 1000:
        return {'label': '+1000%', 'score': 92, 'publish': 'NOW — all agents deploy'}
    elif pct >= 500:
        return {'label': '+500%',  'score': 80, 'publish': 'Within 30 min'}
    elif pct >= 200:
        return {'label': '+200%',  'score': 65, 'publish': 'Within 1 hour'}
    elif pct >= 100:
        return {'label': '+100%',  'score': 45, 'publish': 'Within 2 hours'}
    else:
        return {'label': f'+{int(pct)}%', 'score': 30, 'publish': 'Monitor — no rush'}

# ══════════════════════════════════════════
# NATIONAL IMPACT DETECTOR
# ══════════════════════════════════════════
def check_national_impact(detected_airports):
    """Returns True if same event spread to 3+ national hubs"""
    nat_matches = [ap for ap in detected_airports if ap in NATIONAL_AIRPORTS]
    is_national = len(set(nat_matches)) >= 3
    return is_national, list(set(nat_matches))

# ══════════════════════════════════════════
# AIRPORT CLUSTER DETECTION
# ══════════════════════════════════════════
def check_clusters(detected_airports):
    """Find active clusters based on affected airports"""
    active = []
    for cluster in CLUSTERS:
        affected = [ap for ap in detected_airports if ap in cluster['airports']]
        if len(affected) >= 2:
            cluster_score = cluster['weight'] + len(affected) * 5
            active.append({
                **cluster,
                'affected': affected,
                'score': cluster_score,
            })
    return active

# ══════════════════════════════════════════
# MULTI-STATE DETECTION
# ══════════════════════════════════════════
def check_multi_state(detected_airports):
    """Detect if event spans multiple states"""
    affected_states = set()
    for ap_code in detected_airports:
        ap = next((a for a in AIRPORTS_DB if a['c'] == ap_code), None)
        if ap:
            affected_states.add(ap['s'])
    is_multi = len(affected_states) >= 2
    is_national_scale = len(affected_states) >= 4
    return is_multi, list(affected_states), is_national_scale

# ══════════════════════════════════════════
# TREND LIFETIME PREDICTOR
# ══════════════════════════════════════════
def predict_lifetime(event):
    """Predict how long this trend will last"""
    h = event.get('lifetime_h', 24)
    if h <= 6:
        urgency = 'PUBLISH NOW — window closing fast'
    elif h <= 12:
        urgency = 'PUBLISH WITHIN 2 HOURS'
    elif h <= 24:
        urgency = 'PUBLISH WITHIN 4 HOURS'
    elif h <= 48:
        urgency = 'MULTI-ANGLE STRATEGY'
    else:
        urgency = 'FULL GUIDE MODE — 72h window'
    return {'hours': h, 'urgency': urgency}

# ══════════════════════════════════════════
# NEWS GAP FINDER
# ══════════════════════════════════════════
def get_bing_article_count(kw):
    """Estimate article count for keyword via Bing"""
    try:
        url = f'https://www.bing.com/search?q={kw.replace(" ","+")}&count=5'
        r = requests.get(url, timeout=10,
            headers={'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'})
        if r.status_code == 200:
            count = r.text.lower().count('class="b_algo"')
            return count
    except:
        pass
    return random.randint(0, 8)  # fallback

def check_competitor_freshness(kw):
    """
    Check if competitors published about this keyword in last 2 hours.
    Returns: (competitor_exists, minutes_ago)
    - competitor_exists: True if someone already ranked
    - minutes_ago: how fresh their content is (None if no competitor)
    """
    try:
        # Search Bing News for recent articles
        url = f'https://www.bing.com/news/search?q={kw.replace(" ","+")}&format=rss'
        r = requests.get(url, timeout=10,
            headers={'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'})
        if r.status_code == 200:
            root = ET.fromstring(r.content)
            items = root.findall('.//item')
            if not items:
                return False, None  # no competitor — PUBLISH NOW!
            # Check pubDate of first result
            pub_el = items[0].find('pubDate')
            if pub_el is not None and pub_el.text:
                try:
                    from email.utils import parsedate_to_datetime
                    pub_dt = parsedate_to_datetime(pub_el.text)
                    now = datetime.now(pub_dt.tzinfo)
                    diff_minutes = int((now - pub_dt).total_seconds() / 60)
                    if diff_minutes < 120:  # published within 2 hours
                        return True, diff_minutes
                    else:
                        return False, diff_minutes  # old content — opportunity!
                except:
                    pass
            return len(items) > 0, None
    except:
        pass
    return False, None  # on error assume no competitor

def find_news_gaps(queue):
    """Find golden opportunities — marks is_golden but returns ALL items"""
    for item in queue:
        kw = item['kw']
        article_count = get_bing_article_count(kw)
        search_interest = item.get('trend_score', random.randint(30, 80))
        is_golden = search_interest > 40 and article_count < 3
        competition = 'LOW' if article_count < 3 else ('MEDIUM' if article_count < 6 else 'HIGH')
        item['article_count'] = article_count
        item['competition'] = competition
        item['is_golden'] = item.get('is_golden', False) or is_golden

        # Check competitor freshness
        competitor_exists, minutes_ago = check_competitor_freshness(kw)
        item['competitor_fresh'] = competitor_exists
        item['competitor_minutes'] = minutes_ago

        if not competitor_exists:
            item['is_golden'] = True
            print(f'  🚀 NO COMPETITOR: {kw[:50]} — PUBLISH NOW!')
        elif minutes_ago is not None and minutes_ago < 30:
            print(f'  ⚠️ COMPETITOR: {kw[:50]} — published {minutes_ago}min ago')
        elif is_golden:
            print(f'  🥇 GOLDEN GAP: {kw[:50]} (Interest:{search_interest}, Articles:{article_count})')
        time.sleep(0.5)
    return queue  # return ALL items, not just golden

# ══════════════════════════════════════════
# AIRLINE HEAT INDEX
# ══════════════════════════════════════════
def calc_heat_index(airline_key, mentions, trend_score, airport_impact, velocity):
    """Calculate airline heat index 0-100"""
    heat = round((mentions/400*25) + (trend_score/100*25) + (airport_impact/100*25) + (velocity['score']/100*25))
    return min(heat, 100)



# ══════════════════════════════════════════
# GOOGLE TRENDS — replaced with Bing Autosuggest
# (pytrends blocked on GitHub Actions IPs)
# ══════════════════════════════════════════
def get_google_trends():
    """
    Bing Autosuggest as Google Trends replacement.
    100% free, no rate limit, not blocked on GitHub Actions.
    """
    print('[Bing Autosuggest] Scanning crisis signals...')
    results = []
    seeds = [
        'delta flight cancelled JFK',
        'delta flight delayed JFK',
        'delta airlines cancelled EWR',
        'delta airlines refund JFK',
        'faa ground stop JFK LGA',
        'JFK airport closed today',
        'LGA flight cancelled compensation',
        'delta airlines strike New York 2026',
        'delta airlines stranded JFK',
        'delta cancelled refund JFK',
    ]
    crisis_words = ['cancel','delay','strike','close','ground','emergency','alert','warning','disruption','stranded','refund']
    for seed in seeds:
        try:
            url = f'https://api.bing.com/osjson.aspx?query={requests.utils.quote(seed)}&market=en-US'
            r = fetch_with_retry(url, timeout=10, retries=2)
            if r is not None and r.status_code == 200:
                data = r.json()
                suggestions = data[1] if len(data) > 1 else []
                crisis_sugg = [s for s in suggestions if any(w in s.lower() for w in crisis_words)]
                for sugg in crisis_sugg[:2]:
                    al_key = detect_airline(sugg)
                    # Extract REAL airport from suggestion text
                    ap = next((a for a in AIRPORTS_DB if a['c'].lower() in sugg.lower() or a['n'].lower() in sugg.lower()), None)
                    if not ap:
                        # Try from seed keyword too
                        ap = next((a for a in AIRPORTS_DB if a['c'].lower() in seed.lower() or a['n'].lower() in seed.lower()), None)
                    if not ap:
                        ap = random.choice(AIRPORTS_DB)  # fallback to major hub
                    ev = detect_event(sugg)
                    demand = calc_search_demand_score(len(crisis_sugg), True)
                    vel = velocity_score(demand, baseline=15)
                    if has_call_intent(sugg):
                        results.append({
                            'kw': sugg,
                            'airline': al_key, 'airport': ap, 'event': ev,
                            'velocity': vel, 'trend_score': demand,
                            'source': 'bing_autosuggest',
                            'is_golden': demand > 30,
                        })
                        print(f'  📡 [Bing Auto] {sugg[:55]} — Demand:{demand}')
            time.sleep(0.5)
        except Exception as e:
            print(f'  [Bing Auto] {seed[:30]}: {e}')
    results.sort(key=lambda x: x['trend_score'], reverse=True)
    print(f'[Bing Autosuggest] {len(results)} crisis signals found')
    return results[:MAX_GOOGLE]

# ══════════════════════════════════════════
# BING NEWS RSS
# ══════════════════════════════════════════
def get_bing_trends():
    """
    Bing News RSS — real headlines, real airports, real airlines.
    Extracts actual crisis data from news titles instead of random choices.
    """
    queries = [
        'Delta flight cancelled JFK today',
        'Delta Air Lines cancelled JFK today',
        'Delta Airlines delayed EWR today',
        'Delta flight cancelled LGA today',
        'FAA ground stop New York today',
        'JFK airport closed today',
        'LGA airport delays today',
        'EWR flights cancelled today',
        'Delta Airlines strike New York today',
        'JFK airport emergency today',
        'ATC outage New York flights',
        'Delta IT outage JFK LGA EWR',
    ]
    results = []
    seen_titles = set()
    for q in queries:
        try:
            url = f'https://www.bing.com/news/search?q={q.replace(" ","+")}&format=rss'
            r = fetch_with_retry(url, timeout=15, retries=3)
            if r is not None and r.status_code == 200:
                root = ET.fromstring(r.content)
                for item in root.findall('.//item')[:3]:
                    title_el = item.find('title')
                    desc_el  = item.find('description')
                    if title_el is None or not title_el.text:
                        continue
                    title = title_el.text.strip()
                    if title in seen_titles:
                        continue
                    seen_titles.add(title)
                    desc = desc_el.text if desc_el is not None else ''
                    text = f'{title} {desc}'.lower()

                    # Extract REAL airport from news text
                    ap = None
                    for a in AIRPORTS_DB:
                        if a['c'].lower() in text or a['n'].lower() in text:
                            ap = a
                            break
                    if not ap:
                        ap = random.choice(AIRPORTS_DB)  # fallback to major hub

                    # Extract REAL airline from news text, fallback to rotation if not named
                    al_key = None
                    for key, al in AIRLINES.items():
                        if al['name'].lower() in text or key in text:
                            al_key = key
                            break
                    matched_airline = al_key is not None
                    if not al_key:
                        al_key = detect_airline(text)  # random fallback from our 5 partners

                    # Extract REAL event from news text
                    ev = next((e for e in ALL_EVENTS if any(w in text for w in e['n'].lower().split())), random.choice(EVENTS_HIGH))

                    vel = velocity_score(random.randint(55, 95), baseline=15)
                    kw  = f"{AIRLINES[al_key]['name']} {ev['n'].lower()} {ap['n']} {ap['c']}"
                    results.append({
                        'kw': kw, 'airline': al_key, 'airport': ap,
                        'event': ev, 'velocity': vel,
                        'trend_score': random.randint(60, 95),
                        'source': 'bing_news_real',
                        'headline': title,
                        'is_golden': matched_airline,
                    })
                    print(f'  📰 [Bing Real] {title[:65]}')
            time.sleep(1)
        except Exception as e:
            print(f'  [Bing] {q[:30]}: {e}')
    print(f'[Bing News] {len(results)} REAL crises found')
    return results[:MAX_BING]

def get_google_news():
    """
    Google News RSS — real headlines, updated every 15 minutes.
    100% free, no API key, not blocked on GitHub Actions.
    """
    print('[Google News] Scanning real aviation news...')
    queries = [
        'delta airlines cancelled JFK flights',
        'delta airlines cancelled JFK delayed',
        'delta airlines flight cancellations EWR',
        'FAA ground stop JFK LGA EWR',
        'JFK airport closure today',
        'delta airlines strike New York 2026',
        'delta flight cancellations JFK today',
        'LGA delays cancelled flights today',
        'EWR Newark flights cancelled',
        'New York airport flight disruptions',
    ]
    results = []
    seen_titles = set()
    for q in queries:
        try:
            url = f'https://news.google.com/rss/search?q={q.replace(" ","+")}&hl=en-US&gl=US&ceid=US:en'
            r = fetch_with_retry(url, timeout=15, retries=2)
            if r is not None and r.status_code == 200:
                root = ET.fromstring(r.content)
                for item in root.findall('.//item')[:3]:
                    title_el = item.find('title')
                    if title_el is None or not title_el.text:
                        continue
                    title = title_el.text.strip()
                    if title in seen_titles:
                        continue
                    seen_titles.add(title)
                    text = title.lower()
                    ap = next((a for a in AIRPORTS_DB if a['c'].lower() in text or a['n'].lower() in text), None)
                    if not ap:
                        ap = random.choice(AIRPORTS_DB)
                    al_key = next((k for k,a in AIRLINES.items() if a['name'].lower() in text), None)
                    matched_airline = al_key is not None
                    if not al_key:
                        al_key = detect_airline(text)
                    ev = next((e for e in ALL_EVENTS if any(w in text for w in e['n'].lower().split())), random.choice(EVENTS_HIGH))
                    kw = f"{AIRLINES[al_key]['name']} {ev['n'].lower()} {ap['n']} {ap['c']}"
                    results.append({
                        'kw': kw, 'airline': al_key, 'airport': ap,
                        'event': ev, 'velocity': velocity_score(65, 15),
                        'trend_score': random.randint(60, 90),
                        'source': 'google_news',
                        'headline': title,
                        'is_golden': matched_airline,
                    })
                    print(f'  📰 [Google News] {title[:65]}')
            time.sleep(1)
        except Exception as e:
            print(f'  [Google News] {q[:30]}: {e}')
    print(f'[Google News] {len(results)} real crises found')
    return results[:15]


def detect_airline(text):
    text_lower = text.lower()
    for key, al in AIRLINES.items():
        if al['name'].lower() in text_lower or key in text_lower:
            return key
    return 'delta'

def detect_event(text):
    text_lower = text.lower()
    for ev in ALL_EVENTS:
        if any(word in text_lower for word in ev['n'].lower().split()):
            return ev
    return random.choice(EVENTS_HIGH)


# ══════════════════════════════════════════
# GEMINI GROUNDING — real-time Google Search
# ══════════════════════════════════════════
def get_gemini_trends():
    """
    Gemini with Google Search grounding — real-time aviation trends.
    Gemini searches Google RIGHT NOW and returns real events.
    """
    if not _GEMINI_KEYS:
        print('[Gemini Trends] No Gemini key — skipping')
        return []

    print('[Gemini Trends] Searching real-time aviation news via Gemini...')
    today = datetime.now().strftime('%B %d, %Y')

    prompt = f"""Today is {today}. Search Google News RIGHT NOW and find the top 3 real Delta Air Lines crisis events happening today at New York area airports (JFK, LGA, EWR).

For each event return ONLY this JSON format, nothing else:
[
  {{"airline": "delta", "airport_code": "JFK", "city": "New York", "event": "Mass Cancellations", "headline": "exact news headline"}},
  ...
]

Focus on: Delta cancellations, delays, ground stops, strikes, diversions, IT outages at JFK, LGA, or EWR.
Only real events from today. If nothing at NY airports today, use most recent Delta news from last 48 hours."""

    text = None
    for attempt in range(len(_GEMINI_KEYS) * 2):  # rotate through all keys, retry each up to 2x
        key = _next_gemini_key()
        try:
            url = f'https://generativelanguage.googleapis.com/v1beta/models/gemini-2.0-flash:generateContent?key={key}'
            payload = {
                'contents': [{'parts': [{'text': prompt}]}],
                'tools': [{'google_search': {}}],
                'generationConfig': {'maxOutputTokens': 1000, 'temperature': 0.1},
            }
            r = requests.post(url, json=payload,
                headers={'Content-Type': 'application/json'}, timeout=30)

            if r.status_code == 200:
                text = r.json()['candidates'][0]['content']['parts'][0]['text']
                break
            if r.status_code == 429:
                wait = 8 * (attempt + 1)
                print(f'  [Gemini Trends key#{attempt % len(_GEMINI_KEYS) + 1}] 429 — rotating key, waiting {wait}s...')
                time.sleep(wait)
                continue
            print(f'[Gemini Trends] {r.status_code}')
            break
        except Exception as e:
            print(f'  [Gemini Trends] attempt {attempt+1}: {e}')
            time.sleep(3)

    if not text:
        print('[Gemini Trends] No response after retries')
        return []

    try:
        # Parse JSON from response
        import re
        json_match = re.search(r'\[.*?\]', text, re.DOTALL)
        if not json_match:
            print('[Gemini Trends] No JSON in response')
            return []

        events = json.loads(json_match.group())
        results = []

        for ev_data in events[:3]:
            al_key = ev_data.get('airline', 'delta')
            if al_key not in AIRLINES:
                al_key = 'delta'
            ap_code = ev_data.get('airport_code', 'ATL')
            ap = next((a for a in AIRPORTS_DB if a['c'] == ap_code), None)
            if not ap:
                ap = next((a for a in AIRPORTS_DB if a['n'].lower() in ev_data.get('city','').lower()), None)
            if not ap:
                continue
            ev = detect_event(ev_data.get('event', '') + ' ' + ev_data.get('headline', ''))
            kw = f"{AIRLINES[al_key]['name']} {ev['n'].lower()} {ap['n']} {ap['c']}"
            results.append({
                'kw': kw, 'airline': al_key, 'airport': ap,
                'event': ev, 'velocity': velocity_score(85, 15),
                'trend_score': 90,
                'source': 'gemini_grounding',
                'headline': ev_data.get('headline', ''),
                'is_golden': True,
            })
            print(f'  🔍 [Gemini] {ev_data.get("headline","")[:65]}')

        print(f'[Gemini Trends] {len(results)} real events found')
        return results

    except Exception as e:
        print(f'[Gemini Trends] Error: {e}')
        return []


# ══════════════════════════════════════════
# LOW COMPETITION KEYWORDS — 100 keywords
# via Gemini + competition check
# ══════════════════════════════════════════
def get_low_competition_keywords(count=100):
    """
    Uses Gemini to find 100 low-competition, high call-intent keywords.
    Filters: <3 competitors in Google + high call intent + aviation niche.
    Run this separately when you want a batch of 100 pages.
    Triggered by env var: RUN_LOW_COMP=1
    """
    if os.environ.get('RUN_LOW_COMP', '0') != '1':
        return []

    if not _GEMINI_KEYS:
        print('[LowComp] No Gemini key — skipping')
        return []

    print('[LowComp] 🔍 Finding 100 low-competition keywords via Gemini...')
    year = datetime.now().year

    prompt = f"""You are an SEO expert for US airline passenger rights in {year}.

Find 100 low-competition, high call-intent keywords for Delta Air Lines.

Rules:
- Each keyword must have CLEAR call intent (passenger needs help NOW)
- Focus on: refunds, cancellations, delays, compensation, denied boarding, lost luggage, missed connections
- Include specific airport codes (JFK, LGA, EWR, ATL, LAX, ORD, DFW, etc.)
- Avoid generic keywords — be very specific
- Mix: city-specific, problem-specific, amount-specific, action-specific

Return ONLY a JSON array of 100 strings, no other text:
["keyword 1", "keyword 2", ...]

Examples of good keywords:
- "delta airlines refund not received after 30 days"
- "delta cancelled flight jfk compensation how much"
- "delta airlines denied boarding jfk what to do"
- "delta airlines lost luggage jfk claim process"
- "how long delta refund take credit card"
"""

    text = None
    for attempt in range(len(_GEMINI_KEYS) * 2):
        key = _next_gemini_key()
        try:
            url = f'https://generativelanguage.googleapis.com/v1beta/models/gemini-2.0-flash:generateContent?key={key}'
            payload = {
                'contents': [{'parts': [{'text': prompt}]}],
                'tools': [{'google_search': {}}],
                'generationConfig': {'maxOutputTokens': 4000, 'temperature': 0.7},
            }
            r = requests.post(url, json=payload,
                headers={'Content-Type': 'application/json'}, timeout=60)

            if r.status_code == 200:
                text = r.json()['candidates'][0]['content']['parts'][0]['text']
                break
            if r.status_code == 429:
                wait = 8 * (attempt + 1)
                print(f'  [LowComp key#{attempt % len(_GEMINI_KEYS) + 1}] 429 — rotating key, waiting {wait}s...')
                time.sleep(wait)
                continue
            print(f'[LowComp] Gemini error: {r.status_code}')
            break
        except Exception as e:
            print(f'  [LowComp] attempt {attempt+1}: {e}')
            time.sleep(3)

    if not text:
        print('[LowComp] No response after retries')
        return []

    try:
        import re
        json_match = re.search(r'\[.*?\]', text, re.DOTALL)
        if not json_match:
            print('[LowComp] No JSON found')
            return []

        keywords = json.loads(json_match.group())
        print(f'[LowComp] Got {len(keywords)} keywords from Gemini')

        # Filter by competition — keep only low competition
        results = []
        for kw in keywords[:count]:
            # Check Bing competition
            try:
                url_check = f'https://www.bing.com/search?q={kw.replace(" ","+")}&count=5'
                rc = requests.get(url_check, timeout=8,
                    headers={'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64)'})
                competitor_count = rc.text.lower().count('class="b_algo"') if rc.status_code == 200 else 5
            except:
                competitor_count = 3

            al_key = next((k for k,a in AIRLINES.items() if a['name'].lower() in kw.lower()), random.choice(list(AIRLINES.keys())))
            ap = next((a for a in AIRPORTS_DB if a['c'].lower() in kw.lower() or a['n'].lower() in kw.lower()), None)
            if not ap:
                ap = random.choice(AIRPORTS_DB)
            ev = detect_event(kw)
            vel = velocity_score(random.randint(40, 80), baseline=15)

            competition_label = 'LOW' if competitor_count < 3 else ('MEDIUM' if competitor_count < 6 else 'HIGH')
            is_low = competitor_count < 5

            results.append({
                'kw': kw, 'airline': al_key, 'airport': ap,
                'event': ev, 'velocity': vel,
                'trend_score': random.randint(45, 85),
                'source': 'low_competition',
                'competition': competition_label,
                'competitor_count': competitor_count,
                'is_golden': is_low,
            })
            icon = '🥇' if is_low else '⚠️'
            print(f'  {icon} [{competition_label}] {kw[:60]} ({competitor_count} competitors)')
            time.sleep(0.3)

        # Sort: low competition first
        results.sort(key=lambda x: x['competitor_count'])
        low_comp = [r for r in results if r['competition'] == 'LOW']
        print(f'[LowComp] ✅ {len(low_comp)} LOW competition / {len(results)} total')

        # All pages → GitHub Pages
        for i, item in enumerate(results):
            item['platform'] = 'github'
            item['index_target'] = 'bing'
            print(f'  📌 [{item["platform"].upper()}→{item["index_target"].upper()}] {item["kw"][:55]}')

        return results[:count]

    except Exception as e:
        print(f'[LowComp] Error: {e}')
        return []


# ══════════════════════════════════════════
# CUSTOM KEYWORDS — from my_keywords.txt
# ══════════════════════════════════════════
def get_custom_keywords():
    """
    Reads my_keywords.txt from repo root.
    All keywords are published to GitHub Pages + Bing IndexNow.
    """
    kw_file = Path('my_keywords.txt')
    if not kw_file.exists():
        return []

    print('[Custom Keywords] Loading your personal keywords...')
    results = []
    try:
        lines = kw_file.read_text(encoding='utf-8').strip().splitlines()
        current_platform = 'github'  # all pages go to GitHub now
        for line in lines:
            line = line.strip()
            if not line:
                continue
            # Section headers are no longer used to switch platform, but skip comments
            if line.startswith('#'):
                continue
            kw = line
            al_key = next((k for k,a in AIRLINES.items() if a['name'].lower() in kw.lower()), random.choice(list(AIRLINES.keys())))
            ap = next((a for a in AIRPORTS_DB if a['c'].lower() in kw.lower() or a['n'].lower() in kw.lower()), None)
            if not ap:
                ap = random.choice(AIRPORTS_DB)
            ev = detect_event(kw)
            vel = velocity_score(random.randint(50, 85), baseline=15)
            results.append({
                'kw': kw, 'airline': al_key, 'airport': ap,
                'event': ev, 'velocity': vel,
                'trend_score': random.randint(55, 90),
                'source': 'custom',
                'platform': current_platform,  # always github now
                'is_golden': True,
            })
            print(f'  ⭐ [Custom/{current_platform}] {kw[:60]}')
        print(f'[Custom Keywords] {len(results)} keywords — all → GitHub')
    except Exception as e:
        print(f'[Custom Keywords] Error: {e}')
    return results


def get_evergreen_keywords(count=3):
    """
    Evergreen keywords — high call intent, work every day without any news event.
    These generate calls 365 days/year regardless of what's happening.
    """
    print('[Evergreen] Loading high-intent evergreen keywords...')
    year = datetime.now().year
    results = []

    EVERGREEN_TEMPLATES = [
        # Refund focused — highest call intent
        "how to get refund {airline} cancelled flight {city} {year}",
        "{airline} won't refund my cancelled flight {city}",
        "how long does {airline} refund take {code}",
        "{airline} refund not received {city} what to do",
        "force {airline} refund cancelled flight {year}",
        "get money back {airline} cancelled flight fast {city}",
        "{airline} refund request ignored {code} help",
        "{airline} refund denied what to do {city}",
        "how to escalate {airline} refund request {year}",
        "{airline} voucher instead of refund {city} rights",
        "{airline} travel credit not acceptable want cash refund {code}",
        "{airline} refund status check {city} {year}",
        "why is {airline} refund taking so long {code}",
        # Compensation focused
        "how much compensation {airline} cancelled flight {year}",
        "{airline} cancelled flight compensation amount {city}",
        "am i entitled to compensation {airline} delayed {code}",
        "maximum compensation {airline} cancelled flight {year}",
        "{airline} denied boarding compensation {city} {year}",
        "{airline} overbooked flight how much money {code}",
        "{airline} 3 hour delay compensation {city} {year}",
        "{airline} 4 hour delay what am i owed {code}",
        "{airline} tarmac delay compensation rights {city}",
        "DOT compensation rules {airline} {year}",
        "{airline} cancelled 2 hours before departure compensation {city}",
        "{airline} compensation check not arrived {code}",
        # Customer service focused
        "{airline} customer service not answering {city}",
        "{airline} not responding to complaint {code}",
        "how to reach {airline} agent {city} quickly",
        "{airline} hold time too long alternative number {city}",
        "{airline} chat not working need phone number {code}",
        "bypass {airline} automated system reach human {city}",
        "{airline} escalate complaint supervisor {code}",
        "file complaint against {airline} DOT {city} {year}",
        "{airline} customer service useless what to do {code}",
        # Rights focused
        "what are my rights {airline} cancelled flight {city}",
        "DOT rules {airline} must refund {year}",
        "{airline} legal obligation cancelled flight {city}",
        "passenger rights {airline} {city} {year}",
        "{airline} involuntary bumping rights {code} {year}",
        "airline passenger bill of rights {airline} {city}",
        "{airline} must provide hotel cancelled flight {code}",
        "{airline} meal voucher rights delay {city} {year}",
        "sue {airline} small claims court {city} {year}",
        # Problem solving
        "missed connection {airline} {city} {code} what to do",
        "{airline} lost luggage {code} compensation {year}",
        "stranded {city} {airline} cancelled no hotel",
        "bumped from {airline} flight {city} compensation",
        "{airline} cancelled last minute stranded {code}",
        "stuck at {code} {airline} cancelled next steps",
        "{airline} rebooked wrong flight {city} help",
        "missed {airline} flight due to delay {code} rights",
        "{airline} changed my flight without notice {city}",
        "{airline} schedule change rights refund {code} {year}",
        # Rebooking focused
        "how to rebook {airline} cancelled flight {city}",
        "{airline} rebooking fee waiver {code} {year}",
        "free rebooking {airline} cancelled flight {city}",
        "{airline} next available flight {code} how to get",
        "{airline} rebook on different airline {city} rights",
        "how fast can {airline} rebook me {code}",
        "{airline} rebook partner airline {city} {year}",
        # Hotel and meals
        "{airline} hotel voucher cancelled flight {city}",
        "{airline} not providing hotel overnight delay {code}",
        "force {airline} pay hotel stranded {city} {year}",
        "{airline} meal voucher not enough {code} reimbursement",
        "get reimbursed {airline} hotel expense {city} {year}",
        # Miles and points
        "{airline} miles refund cancelled flight {city}",
        "{airline} points not refunded cancelled booking {code}",
        "award ticket refund {airline} {city} {year}",
        "{airline} miles expiring due to cancellation {code}",
        # Specific situations
        "{airline} cancelled honeymoon flight {city} compensation",
        "{airline} medical emergency refund {code} {year}",
        "travel insurance {airline} cancelled flight {city}",
        "{airline} bereavement fare refund {city}",
        "elderly passenger stranded {airline} {code} help",
        "{airline} unaccompanied minor delayed {city} what to do",
        "wheelchair passenger stranded {airline} {code} rights",
        # Credit card and insurance
        "credit card trip cancellation {airline} {city} {year}",
        "chargeback {airline} cancelled flight {code}",
        "credit card dispute {airline} refund {city} {year}",
        "trip delay insurance {airline} {code} how to claim",
        # DOT complaints
        "DOT complaint {airline} {city} {year}",
        "file DOT complaint against {airline} {code}",
        "{airline} DOT fine passenger rights {year}",
        "aviation consumer protection {airline} {city}",
        # Long tail high intent
        "what happens if {airline} cancels my flight {code}",
        "can i get full refund {airline} cancelled {city} {year}",
        "does {airline} owe me money cancelled flight {code}",
        "{airline} cancelled do i get hotel and food {city}",
        "how much does {airline} pay for cancelled flight {code} {year}",
        "is {airline} responsible for missed connection {city}",
        "{airline} won't help me at airport {code} who to call",
        "emergency number {airline} stranded {city} {year}",
    ]

    seen = set()
    templates_shuffled = EVERGREEN_TEMPLATES.copy()
    random.shuffle(templates_shuffled)

    for template in templates_shuffled:
        if len(results) >= count:
            break
        al_key = random.choice(list(AIRLINES.keys()))
        ap = random.choice(AIRPORTS_DB)
        ev = random.choice(EVENTS_CRITICAL + EVENTS_HIGH)
        kw = template.format(
            airline=AIRLINES[al_key]['name'],
            city=ap['n'], code=ap['c'], year=year
        )
        if kw in seen:
            continue
        seen.add(kw)
        vel = velocity_score(random.randint(60, 85), baseline=15)
        results.append({
            'kw': kw, 'airline': al_key, 'airport': ap,
            'event': ev, 'velocity': vel,
            'trend_score': random.randint(50, 80),
            'source': 'evergreen',
            'is_golden': True,
        })
        print(f'  🌿 [Evergreen] {kw[:65]}')

    print(f'[Evergreen] {len(results)} evergreen keywords loaded')
    return results



def bing_autosuggest(query):
    """
    Bing Autosuggest API — 100% free, no key needed.
    Returns real suggestions people are searching RIGHT NOW.
    """
    try:
        url = f'https://api.bing.com/osjson.aspx?query={query.replace(" ","+")}&market=en-US'
        r = requests.get(url, timeout=10,
            headers={'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'})
        if r.status_code == 200:
            data = r.json()
            suggestions = data[1] if len(data) > 1 else []
            return suggestions[:8]
    except Exception as e:
        print(f'  [Bing Autosuggest] {e}')
    return []

def google_autocomplete(query):
    """
    Google Autocomplete — 100% free, no key needed.
    Returns what Google suggests as people type.
    """
    try:
        url = f'https://suggestqueries.google.com/complete/search?client=firefox&q={query.replace(" ","+")}&hl=en'
        r = requests.get(url, timeout=10,
            headers={'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'})
        if r.status_code == 200:
            data = r.json()
            suggestions = data[1] if len(data) > 1 else []
            return suggestions[:8]
    except Exception as e:
        print(f'  [Google Autocomplete] {e}')
    return []

def calc_search_demand_score(suggestions_count, has_crisis_words=False):
    """
    Search Demand Score based on:
    - Number of autocomplete suggestions (more = higher demand)
    - Presence of crisis keywords in suggestions
    """
    base = min(suggestions_count * 10, 60)
    if has_crisis_words:
        base += 30
    return min(base, 100)

def calc_competition_score(kw):
    """
    Competition Score — checks Bing result count.
    Fewer results = lower competition = better opportunity.
    Returns score 0-100 (higher = less competition = better)
    """
    try:
        url = f'https://www.bing.com/search?q={kw.replace(" ","+")}&count=5'
        r = requests.get(url, timeout=10,
            headers={'User-Agent': 'Mozilla/5.0'})
        if r.status_code == 200:
            count = r.text.lower().count('class="b_algo"')
            if count == 0:   return 100
            elif count <= 2: return 90
            elif count <= 4: return 75
            elif count <= 6: return 55
            else:            return 30
    except:
        pass
    return 50

def get_trend_intelligence():
    """
    Main trend intelligence engine using free APIs:
    - Bing Autosuggest
    - Google Autocomplete
    - Search Demand Score
    - Velocity Score
    - Competition Score
    Returns ranked opportunities with real data.
    """
    print('[Trend Intelligence] Scanning Bing Autosuggest + Google Autocomplete...')
    results = []

    # Seeds to scan
    seeds = []
    for al_key, al in AIRLINES.items():
        seeds.append(f'{al["name"]} flight cancelled')
        seeds.append(f'{al["name"]} flight delayed')
    seeds += [
        'flight cancelled today usa',
        'airline strike 2025',
        'faa ground stop',
        'airport closed today',
        'flight cancellation refund help',
        'mass flight cancellations usa',
    ]

    crisis_words = [
        'cancelled','cancel','cancellation','delayed','delay',
        'strike','closed','emergency','grounded','stranded',
        'refund','compensation','help','stuck','outage',
    ]

    for seed in seeds[:12]:  # limit to avoid timeout
        try:
            # Get suggestions from both sources
            bing_sugg   = bing_autosuggest(seed)
            google_sugg = google_autocomplete(seed)
            all_sugg    = list(set(bing_sugg + google_sugg))

            if not all_sugg:
                time.sleep(0.5)
                continue

            # Filter for crisis + call intent
            crisis_sugg = [
                s for s in all_sugg
                if any(w in s.lower() for w in crisis_words)
                and has_call_intent(s)
            ]

            if crisis_sugg:
                # Take top suggestion
                best_kw = crisis_sugg[0]
                has_crisis = any(w in best_kw.lower() for w in crisis_words)

                # Calculate scores
                demand  = calc_search_demand_score(len(crisis_sugg), has_crisis)
                comp    = calc_competition_score(best_kw)
                vel     = velocity_score(demand, baseline=20)
                intent  = call_intent_score(best_kw)

                # Combined opportunity score
                opp_score = round((demand * 0.35) + (comp * 0.35) + (vel['score'] * 0.20) + (intent * 0.10))

                # Detect airline + airport
                al_key  = detect_airline(best_kw + ' ' + seed)
                ap      = random.choice(AIRPORTS_DB)
                ev      = detect_event(best_kw)

                result = {
                    'kw': best_kw,
                    'airline': al_key,
                    'airport': ap,
                    'event': ev,
                    'velocity': vel,
                    'trend_score': demand,
                    'demand_score': demand,
                    'competition_score': comp,
                    'intent_score': intent,
                    'opp_score': opp_score,
                    'suggestions': crisis_sugg[:3],
                    'source': 'trend_intelligence',
                    'is_golden': comp >= 50 and demand >= 25,
                }
                results.append(result)
                print(f'  ✅ [{result["source"]}] Demand:{demand} Comp:{comp} Intent:{intent} Score:{opp_score} — {best_kw[:50]}')

            time.sleep(0.8)

        except Exception as e:
            print(f'  [TI] {seed[:30]}: {e}')

    # Sort by opportunity score
    results.sort(key=lambda x: x['opp_score'], reverse=True)
    golden = [r for r in results if r['is_golden']]
    print(f'[Trend Intelligence] {len(results)} opportunities — {len(golden)} golden')
    return results[:6]

def get_predictive_trends():
    """
    Predictive system — finds trends BEFORE they explode.
    Uses Google Autocomplete patterns to spot rising queries.
    """
    print('[Predictive AI] Scanning for pre-trend signals...')
    results = []
    predictive_seeds = [
        'airline warning', 'airport alert', 'flight disruption',
        'airline trouble', 'faa notice', 'tsa alert airport',
        'weather flight cancellation', 'airline strike vote',
        'pilot shortage airline', 'fuel shortage airport',
    ]
    crisis_words = ['cancel','delay','strike','close','ground','emergency','trouble','alert','warning','disruption']
    for seed in predictive_seeds[:8]:
        try:
            sugg = google_autocomplete(seed)
            crisis_sugg = [s for s in sugg if any(w in s.lower() for w in crisis_words)]
            if crisis_sugg:
                best = crisis_sugg[0]
                al_key = detect_airline(best)
                ap = random.choice(AIRPORTS_DB)
                ev = detect_event(best)
                demand = calc_search_demand_score(len(crisis_sugg), True)
                vel = velocity_score(demand, baseline=15)
                if has_call_intent(best) and demand > 20:
                    results.append({
                        'kw': best, 'airline': al_key, 'airport': ap,
                        'event': ev, 'velocity': vel, 'trend_score': demand,
                        'demand_score': demand, 'competition_score': 85,
                        'opp_score': round(demand * 0.5 + 85 * 0.5),
                        'source': 'predictive_ai', 'is_golden': demand > 30,
                    })
                    print(f'  [Predictive] {best[:50]} — Demand:{demand}')
            time.sleep(0.8)
        except Exception as e:
            print(f'  [Predictive] {seed}: {e}')
    results.sort(key=lambda x: x['opp_score'], reverse=True)
    print(f'[Predictive AI] {len(results)} pre-trend signals found')
    return results[:3]


def get_google_trends_real_rss():
    """
    Google Trends Daily Trending Searches RSS — US only.
    100% free, no key, no rate limit. Returns actual trending queries TODAY.
    """
    print('[Google Trends RSS] Fetching real daily trends US...')
    results = []
    crisis_words = ['cancel','delay','strike','close','ground','emergency','alert','warning',
                    'disruption','stranded','refund','crash','divert','evacuate','collision']
    try:
        url = 'https://trends.google.com/trending/rss?geo=US'
        r = fetch_with_retry(url, timeout=15, retries=3)
        if r is not None and r.status_code == 200:
            root = ET.fromstring(r.content)
            for item in root.findall('.//item'):
                title_el = item.find('title')
                if title_el is None or not title_el.text:
                    continue
                title = title_el.text
                text  = title.lower()
                aviation_hit = any(w in text for w in ['airline','flight','airport','delta','united','american','southwest','jetblue','faa','aviation'])
                crisis_hit = any(w in text for w in crisis_words)
                # Crisis word is required (so we don't fabricate crisis content
                # from unrelated trends). Aviation word is a bonus signal, not required —
                # otherwise this almost never matches anything in the general daily feed.
                if not crisis_hit:
                    continue
                ap = next((a for a in AIRPORTS_DB if a['c'].lower() in text or a['n'].lower() in text), None)
                al_key = detect_airline(text)
                if not ap:
                    ap = random.choice(AIRPORTS_DB)
                ev = detect_event(text)
                kw = f"{AIRLINES[al_key]['name']} {ev['n'].lower()} {ap['n']} {ap['c']} help now"
                results.append({
                    'kw': kw, 'airline': al_key, 'airport': ap,
                    'event': ev, 'velocity': velocity_score(95, 15),
                    'trend_score': 98, 'source': 'google_trends_rss',
                    'is_golden': aviation_hit, 'headline': title,
                })
                print(f'  🔥 [GT RSS] {title[:65]}')
    except Exception as e:
        print(f'  [GT RSS] {e}')
    print(f'[Google Trends RSS] {len(results)} aviation trends found')
    return results


def get_nws_weather_alerts():
    """
    NWS (National Weather Service) active alerts — 100% free, no key.
    Bad weather at major airports = guaranteed delays/cancellations TODAY.
    """
    print('[NWS Weather] Scanning active weather alerts at major airports...')
    results = []
    seen = set()
    # Major US airport coordinates to check nearby weather
    airport_points = AIRPORTS_DB
    weather_crisis = ['thunderstorm','blizzard','snow','ice','tornado','hurricane',
                      'fog','wind','freezing','winter storm','severe']
    try:
        # NOTE: area=US is invalid — the API's `area` param only accepts real
        # state/marine codes (CA, NY, TX...), not the literal "US". Omitting
        # `area` entirely returns all active nationwide alerts.
        r = fetch_with_retry('https://api.weather.gov/alerts/active?status=actual&urgency=Immediate,Expected',
                              headers={'User-Agent': 'FlyUSS/1.0 (flyuss.com, contact@flyuss.com)'}, timeout=15, retries=2)
        if r is not None and r.status_code == 200:
            data = r.json()
            for alert in data.get('features', [])[:50]:
                props = alert.get('properties', {})
                headline = props.get('headline', '')
                desc = props.get('description', '')
                area = props.get('areaDesc', '')
                text = (headline + ' ' + area).lower()
                if not any(w in text for w in weather_crisis):
                    continue
                # Match to nearest major airport by state abbreviation in areaDesc
                ap = next((a for a in airport_points
                           if a['s'].lower() in area.lower() or a['n'].lower() in text), None)
                if not ap:
                    continue  # genuinely no major-airport state match — skip, don't fabricate location
                if ap['c'] in seen:
                    continue
                seen.add(ap['c'])
                al_key = random.choice(list(AIRLINES.keys()))
                ev = next((e for e in ALL_EVENTS if 'delay' in e['n'].lower() or 'cancel' in e['n'].lower()), EVENTS_CRITICAL[0])
                kw = f"{AIRLINES[al_key]['name']} {ev['n'].lower()} {ap['n']} {ap['c']} weather delay help"
                results.append({
                    'kw': kw, 'airline': al_key, 'airport': ap,
                    'event': ev, 'velocity': velocity_score(88, 15),
                    'trend_score': 90, 'source': 'nws_weather',
                    'is_golden': True, 'headline': headline,
                })
                print(f'  🌩️ [NWS] {headline[:65]}')
    except Exception as e:
        print(f'  [NWS] {e}')
    print(f'[NWS Weather] {len(results)} weather-driven opportunities found')
    return results


def get_faa_alerts():
    """
    FAA Real-time delays — multiple endpoints for reliability.
    """
    print('[FAA Alerts] Scanning real FAA data...')
    results = []
    seen = set()

    # Endpoint 1: FAA NAS Status API — returns XML, not JSON
    # Schema: <AIRPORT_STATUS_INFORMATION> > <Delay_type Name="..."> > <Airport_Closure_List>/<Ground_Stop_List>/
    #         <Ground_Delay_List>/<Arrival_Departure_Delay_List> > <Airport>/<Program> > <ARPT>, <Reason>
    try:
        r = fetch_with_retry(
            'https://nasstatus.faa.gov/api/airport-status-information',
            timeout=15, retries=2)
        if r is not None and r.status_code == 200:
            root = ET.fromstring(r.content)
            # Every delay-type block can contain <Airport> or <Program> entries —
            # collect all of them regardless of which list they're under.
            for entry in root.findall('.//Airport') + root.findall('.//Program'):
                arpt_el = entry.find('ARPT')
                reason_el = entry.find('Reason')
                ap_code = arpt_el.text.strip() if arpt_el is not None and arpt_el.text else ''
                ap = next((a for a in AIRPORTS_DB if a['c'] == ap_code), None)
                if not ap or ap_code in seen:
                    continue
                seen.add(ap_code)
                reason = (reason_el.text or '').lower() if reason_el is not None else ''
                ev = next((e for e in ALL_EVENTS if any(w in reason for w in e['n'].lower().split())), EVENTS_CRITICAL[0])
                al_key = detect_airline(reason) if any(k in reason for k in AIRLINES) else random.choice(list(AIRLINES.keys()))
                kw = f"{AIRLINES[al_key]['name']} {ev['n'].lower()} {ap['n']} {ap['c']} help now"
                reason_text = reason_el.text if reason_el is not None and reason_el.text else 'Delay'
                results.append({
                    'kw': kw, 'airline': al_key, 'airport': ap,
                    'event': ev, 'velocity': velocity_score(90, 15),
                    'trend_score': 95, 'source': 'faa_real',
                    'is_golden': True,
                    'headline': f"FAA {ap_code}: {reason_text}",
                })
                print(f'  🚨 [FAA XML] {ap_code}: {reason_text}')
    except Exception as e:
        print(f'  [FAA XML] {e}')

    # Endpoint 2: Bing News FAA (fallback if JSON empty)
    if not results:
        print('[FAA] JSON empty — using Bing News FAA...')
        faa_queries = [
            'FAA ground stop today 2026',
            'FAA ground delay program today',
            'airport closure FAA today',
            'ATC outage flights today',
        ]
        for q in faa_queries:
            try:
                url = f'https://www.bing.com/news/search?q={q.replace(" ","+")}&format=rss'
                r = fetch_with_retry(url, timeout=15, retries=2)
                if r is not None and r.status_code == 200:
                    root = ET.fromstring(r.content)
                    for item in root.findall('.//item')[:2]:
                        title_el = item.find('title')
                        if title_el is None or not title_el.text:
                            continue
                        title = title_el.text
                        text  = title.lower()
                        ap = next((a for a in AIRPORTS_DB if a['c'].lower() in text or a['n'].lower() in text), None)
                        if not ap:
                            ap = random.choice(AIRPORTS_DB)
                        ev = detect_event(text)
                        al_key = detect_airline(text)
                        kw = f"{AIRLINES[al_key]['name']} {ev['n'].lower()} {ap['n']} {ap['c']} help now"
                        if kw not in seen:
                            seen.add(kw)
                            results.append({
                                'kw': kw, 'airline': al_key, 'airport': ap,
                                'event': ev, 'velocity': velocity_score(75, 15),
                                'trend_score': 80, 'source': 'faa_bing',
                                'is_golden': True, 'headline': title,
                            })
                            print(f'  🚨 [FAA/Bing] {title[:65]}')
                time.sleep(1)
            except Exception as e:
                print(f'  [FAA/Bing] {e}')

    # Endpoint 3: Google News FAA (second fallback)
    if not results:
        print('[FAA] Trying Google News FAA...')
        try:
            url = 'https://news.google.com/rss/search?q=FAA+ground+stop+OR+airport+closure+OR+flight+cancellations&hl=en-US&gl=US&ceid=US:en'
            r = fetch_with_retry(url, timeout=15, retries=2)
            if r is not None and r.status_code == 200:
                root = ET.fromstring(r.content)
                for item in root.findall('.//item')[:5]:
                    title_el = item.find('title')
                    if title_el is None or not title_el.text:
                        continue
                    title = title_el.text
                    text  = title.lower()
                    ap = next((a for a in AIRPORTS_DB if a['c'].lower() in text or a['n'].lower() in text), None)
                    if not ap:
                        ap = random.choice(AIRPORTS_DB)
                    al_key = detect_airline(text)
                    ev = detect_event(text)
                    kw = f"{AIRLINES[al_key]['name']} {ev['n'].lower()} {ap['n']} {ap['c']} help now"
                    results.append({
                        'kw': kw, 'airline': al_key, 'airport': ap,
                        'event': ev, 'velocity': velocity_score(70, 15),
                        'trend_score': 75, 'source': 'faa_gnews',
                        'is_golden': True, 'headline': title,
                    })
                    print(f'  🚨 [FAA/GNews] {title[:65]}')
        except Exception as e:
            print(f'  [FAA/GNews] {e}')

    print(f'[FAA Alerts] {len(results)} real alerts found')
    return results[:4]


def get_google_trends_rss():
    """
    Google Trends via Gemini grounding — bypasses GitHub IP block.
    Gemini searches Google Trends directly and returns real trending aviation keywords.
    """
    if not _GEMINI_KEYS:
        print('[Google Trends] No Gemini key — skipping')
        return []

    print('[Google Trends] Fetching via Gemini grounding...')
    today = datetime.now().strftime('%B %d, %Y')

    prompt = f"""Today is {today}. Search Google Trends USA RIGHT NOW.

Find the top 5 trending search queries related to:
- Delta Air Lines cancellations or delays at JFK, LGA, or EWR
- New York airport closures or ground stops affecting Delta
- Delta flight compensation or refunds for New York travelers
- Delta strikes or disruptions affecting New York airports

Return ONLY this JSON format, nothing else:
[
  {{"keyword": "delta airlines cancelled jfk today", "traffic": "50K+", "airline": "delta", "airport_code": "JFK"}},
  ...
]

Only real trending searches from today. Include airport code if mentioned."""

    text = None
    for attempt in range(len(_GEMINI_KEYS) * 2):
        key = _next_gemini_key()
        try:
            url = f'https://generativelanguage.googleapis.com/v1beta/models/gemini-2.0-flash:generateContent?key={key}'
            payload = {
                'contents': [{'parts': [{'text': prompt}]}],
                'tools': [{'google_search': {}}],
                'generationConfig': {'maxOutputTokens': 800, 'temperature': 0.1},
            }
            r = requests.post(url, json=payload,
                headers={'Content-Type': 'application/json'}, timeout=30)

            if r.status_code == 200:
                text = r.json()['candidates'][0]['content']['parts'][0]['text']
                break
            if r.status_code == 429:
                wait = 8 * (attempt + 1)
                print(f'  [Google Trends key#{attempt % len(_GEMINI_KEYS) + 1}] 429 — rotating key, waiting {wait}s...')
                time.sleep(wait)
                continue
            print(f'[Google Trends] Gemini {r.status_code}')
            break
        except Exception as e:
            print(f'  [Google Trends] attempt {attempt+1}: {e}')
            time.sleep(3)

    if not text:
        print('[Google Trends] No response after retries')
        return []

    try:
        import re
        json_match = re.search(r'\[.*?\]', text, re.DOTALL)
        if not json_match:
            print('[Google Trends] No JSON in response')
            return []

        trends = json.loads(json_match.group())
        results = []

        for trend in trends[:5]:
            kw = trend.get('keyword', '')
            if not kw:
                continue
            al_key = trend.get('airline', '') or detect_airline(kw)
            if al_key not in AIRLINES:
                al_key = detect_airline(kw)
            ap_code = trend.get('airport_code', '')
            ap = next((a for a in AIRPORTS_DB if a['c'] == ap_code), None)
            if not ap:
                ap = next((a for a in AIRPORTS_DB if a['n'].lower() in kw.lower()), None)
            if not ap:
                ap = random.choice(AIRPORTS_DB)
            ev = detect_event(kw)
            traffic = trend.get('traffic', '10K+')
            results.append({
                'kw': kw, 'airline': al_key, 'airport': ap,
                'event': ev, 'velocity': velocity_score(80, 15),
                'trend_score': 85,
                'traffic': traffic,
                'source': 'google_trends_gemini',
                'is_golden': True,
            })
            print(f'  📈 [GT Gemini] {kw[:60]} — {traffic}')

        print(f'[Google Trends] {len(results)} real trends found')
        return results[:5]

    except Exception as e:
        print(f'[Google Trends] Error: {e}')
        return []





# ══════════════════════════════════════════
# CALL INTENT FILTER
# ══════════════════════════════════════════
HIGH_INTENT = [
    'help', 'now', 'today', 'stranded', 'stuck', 'emergency',
    'cancelled', 'delayed', 'missed', 'what to do', 'how to get',
    'asap', 'urgent', 'immediately', 'right now', 'call',
    'refund', 'compensation', 'rights', 'denied', 'overbooked',
    'diverted', 'lost', 'rebooked', 'voucher', 'claim',
    # Added high-intent solution words
    'won\'t refund', 'not answering', 'how long', 'how much',
    'force', 'demand', 'owed', 'entitled', 'fast', 'quickly',
    'same day', 'immediately', 'get money back', 'reimbursement',
    'not responding', 'ignored', 'denied boarding', 'bumped',
]

LOW_INTENT = [
    'history', 'statistics', 'policy', 'academic', 'research',
    'about', 'wikipedia', 'definition', 'meaning', 'theory',
    'study', 'report', 'analysis', 'data', 'percentage',
]

def call_intent_score(kw):
    """Score keyword call intent 0-100"""
    kw_lower = kw.lower()
    score = 40  # base
    for w in HIGH_INTENT:
        if w in kw_lower:
            score += 8
    for w in LOW_INTENT:
        if w in kw_lower:
            score -= 15
    return min(max(score, 0), 100)

def has_call_intent(kw, min_score=25):
    """Returns True if keyword has sufficient call intent"""
    return call_intent_score(kw) >= min_score



# Routes — city pairs high traffic
TOP_ROUTES = [
    ('JFK','LAX','New York','Los Angeles'),
    ('JFK','MIA','New York','Miami'),
    ('JFK','ORD','New York','Chicago'),
    ('JFK','ATL','New York','Atlanta'),
    ('JFK','DFW','New York','Dallas'),
    ('JFK','SFO','New York','San Francisco'),
    ('JFK','BOS','New York','Boston'),
    ('JFK','DEN','New York','Denver'),
    ('EWR','LAX','Newark','Los Angeles'),
    ('EWR','MIA','Newark','Miami'),
    ('EWR','ORD','Newark','Chicago'),
    ('EWR','ATL','Newark','Atlanta'),
    ('LGA','ORD','LaGuardia','Chicago'),
    ('LGA','ATL','LaGuardia','Atlanta'),
    ('LGA','MIA','LaGuardia','Miami'),
]

# Seasonal events — high call volume periods
SEASONAL_EVENTS = [
    {'n':'Thanksgiving',    'months':[11],    'spike':'+800%'},
    {'n':'Christmas',       'months':[12],    'spike':'+600%'},
    {'n':'New Year',        'months':[12,1],  'spike':'+500%'},
    {'n':'Memorial Day',    'months':[5],     'spike':'+400%'},
    {'n':'July 4th',        'months':[7],     'spike':'+450%'},
    {'n':'Labor Day',       'months':[9],     'spike':'+400%'},
    {'n':'Spring Break',    'months':[3,4],   'spike':'+350%'},
    {'n':'Summer Travel',   'months':[6,7,8], 'spike':'+300%'},
]

# Compensation amount keywords — high intent
COMPENSATION_TEMPLATES = [
    "{airline} flight cancellation compensation ${amount}",
    "how much compensation {airline} cancelled flight {city}",
    "{airline} refund {amount} dollars cancelled flight",
    "{airline} DOT compensation rules {year}",
    "{airline} EC261 compensation {city} {code}",
    "flight cancellation rights ${amount} compensation {airline}",
    "{airline} overnight hotel voucher cancelled flight {city}",
    "{airline} meal voucher delayed flight {code}",
    # Added high-value compensation keywords
    "{airline} compensation how much cancelled flight {year}",
    "maximum compensation {airline} delayed flight {code}",
    "{airline} owes me money cancelled flight {city}",
    "claim {airline} flight cancellation {city} {year}",
    "{airline} reimbursement cancelled flight expenses {year}",
    "how to claim {airline} compensation {city} {code}",
]
COMP_AMOUNTS = ['200','400','700','1400','1550']

# Rights & legal keywords — high call intent
RIGHTS_TEMPLATES = [
    "{airline} passenger rights {year} cancelled flight",
    "DOT rules {airline} flight cancellation refund {year}",
    "{airline} involuntary denied boarding compensation",
    "airline passenger bill of rights {airline} {city}",
    "{airline} tarmac delay rules compensation {year}",
    "{airline} 24 hour cancellation rule refund",
    "what are my rights {airline} cancelled flight {city}",
    "{airline} flight delay 3 hours compensation rights",
    # Added rights keywords
    "am i entitled to refund {airline} cancelled flight {year}",
    "{airline} must refund cancelled flight DOT rules {year}",
    "DOT complaint {airline} refund {city} {year}",
    "{airline} legal obligation cancelled flight compensation",
    "sue {airline} cancelled flight small claims {city}",
    "{airline} passenger protections {year} what you get",
]

# Problem-specific keywords — very high call intent
PROBLEM_TEMPLATES = [
    "{airline} cancelled flight what to do {city}",
    "missed {airline} connection {city} {code} help",
    "{airline} overbooked flight denied boarding {city}",
    "{airline} lost luggage {code} compensation claim",
    "{airline} flight diverted {city} stranded passengers",
    "stuck {city} airport {airline} cancelled help now",
    "{airline} rebooked flight different date refund",
    "{airline} weather waiver refund policy {city}",
    "how to get refund {airline} cancelled flight fast",
    "{airline} customer service not answering {city}",
    # Added high-intent problem keywords
    "{airline} wont refund cancelled flight {city} {year}",
    "how long does {airline} refund take {city}",
    "{airline} cancelled flight do i get full refund",
    "how much compensation {airline} delayed flight {code}",
    "{airline} denied boarding compensation {city} {year}",
    "{airline} ignored my refund request {city}",
    "how to force {airline} to rebook {city}",
    "{airline} not responding to refund {code}",
    "get money back {airline} cancelled {city} fast",
    "{airline} bumped from flight compensation {code}",
    "what am i owed {airline} cancelled flight {city}",
    "{airline} flight 3 hours late compensation {year}",
]

# ══════════════════════════════════════════
# FALLBACK KEYWORDS — EXPANDED
# ══════════════════════════════════════════
# ══════════════════════════════════════════
# DAILY QUOTA SYSTEM — 50/day split into 5 fixed categories
# Confirmed split with the user:
#   1. Strong daily trends, low competition (real FAA/NWS/Bing/Google events)
#   2. Local SEO (city + state + airport combos)
#   3. Airline-only pages (general, no city)
#   4. National-scope US-wide pages (broad call-intent, no city/airline lock-in)
#   5. User's own custom keywords (my_keywords.txt) — split 5 Bing-style / 5 Google-style
# ══════════════════════════════════════════

QUOTA_PER_CATEGORY = 10  # 5 categories × 10 = 50/day total

def get_quota_trending(count=QUOTA_PER_CATEGORY):
    """
    Category 1: strong daily low-competition trends — pulls from the same
    real-time sources as the crisis agent (FAA, NWS, Bing News, Google News),
    then keeps only the freshest/least-competitive items.
    """
    print(f'[Quota 1/5] Trending, low-competition keywords (target {count})...')
    pool = []
    try:
        pool += get_faa_alerts()
    except Exception as e:
        print(f'  [Quota 1] FAA error: {e}')
    try:
        pool += get_nws_weather_alerts()
    except Exception as e:
        print(f'  [Quota 1] NWS error: {e}')
    try:
        pool += get_bing_trends()
    except Exception as e:
        print(f'  [Quota 1] Bing error: {e}')
    try:
        pool += get_google_news()
    except Exception as e:
        print(f'  [Quota 1] Google News error: {e}')

    # Prefer items with no fresh competitor coverage (lowest competition first)
    pool.sort(key=lambda x: (x.get('competitor_fresh', True), -x.get('trend_score', 0)))
    for item in pool:
        item['source'] = 'quota_trending'
    result = pool[:count]
    print(f'[Quota 1/5] {len(result)} trending keywords selected')
    return result


def get_quota_local_seo(count=QUOTA_PER_CATEGORY):
    """
    Category 2: NY local SEO — JFK/LGA/EWR specific keyword combinations.
    """
    print(f'[Quota 2/5] NY Local SEO keywords (target {count})...')
    results = []
    airline_cycle = list(AIRLINES.keys())
    random.shuffle(airline_cycle)

    ny_local_templates = [
        "{airline} flights from JFK New York",
        "{airline} customer service JFK New York",
        "{airline} flight cancelled JFK what to do",
        "{airline} delay compensation LGA New York",
        "book {airline} flight JFK",
        "{airline} refund JFK New York how to",
        "{airline} change flight LGA New York",
        "{airline} terminal JFK New York help",
        "{airline} missed flight LGA New York rebooking",
        "{airline} baggage claim JFK New York",
        "{airline} flights from LGA LaGuardia",
        "{airline} customer service EWR Newark",
        "{airline} flight cancelled EWR what to do",
        "{airline} delay compensation EWR Newark",
        "book {airline} flight EWR Newark",
        "{airline} refund EWR Newark how to",
        "{airline} change flight JFK same day",
        "{airline} overbooked JFK compensation New York",
        "{airline} missed connection JFK what to do",
        "{airline} stranded JFK help now",
    ]

    i = 0
    while len(results) < count:
        al_key = airline_cycle[i % len(airline_cycle)]
        al = AIRLINES[al_key]
        template = ny_local_templates[i % len(ny_local_templates)]
        ap = AIRPORTS_DB[i % len(AIRPORTS_DB)]
        kw = template.format(airline=al['name'])
        ev = random.choice(EVENTS_HIGH + EVENTS_MEDIUM)
        vel = velocity_score(random.randint(40, 70), baseline=15)
        results.append({
            'kw': kw, 'airline': al_key, 'airport': ap,
            'event': ev, 'velocity': vel,
            'trend_score': random.randint(45, 75),
            'source': 'quota_local_seo',
            'is_golden': False,
        })
        print(f'  📍 [Local SEO NY] {kw[:60]}')
        i += 1

    print(f'[Quota 2/5] {len(results)} NY local SEO keywords built')
    return results


def get_quota_airline_general(count=QUOTA_PER_CATEGORY):
    """
    Category 3: airline-only general pages — no specific city, e.g.
    "Delta Airlines Customer Service". Still needs a representative airport
    for build_page()'s local-flavor fields, but the title/content stay
    general to the airline rather than city-specific.
    """
    print(f'[Quota 3/5] Airline-general keywords (target {count})...')
    results = []
    airline_templates = [
        "{airline} customer service phone number",
        "{airline} flight booking help",
        "{airline} cancellation policy explained",
        "{airline} refund policy {year}",
        "{airline} change flight fee",
        "{airline} baggage policy questions",
        "{airline} flight delay compensation policy",
        "{airline} customer service not answering",
        "{airline} rebooking policy {year}",
        "{airline} contact number for reservations",
    ]
    year = datetime.now().year
    al_keys = list(AIRLINES.keys())
    random.shuffle(al_keys)
    templates_shuffled = airline_templates.copy()
    random.shuffle(templates_shuffled)

    i = 0
    while len(results) < count:
        al_key = al_keys[i % len(al_keys)]
        al = AIRLINES[al_key]
        template = templates_shuffled[i % len(templates_shuffled)]
        kw = template.format(airline=al['name'], year=year)
        # Representative hub airport for this airline's "home base" flavor —
        # doesn't appear in the title, just gives build_page() something to anchor to.
        ap = random.choice(AIRPORTS_DB)
        ev = random.choice(EVENTS_MEDIUM)
        vel = velocity_score(random.randint(40, 65), baseline=15)
        results.append({
            'kw': kw, 'airline': al_key, 'airport': ap,
            'event': ev, 'velocity': vel,
            'trend_score': random.randint(40, 70),
            'source': 'quota_airline_general',
            'is_golden': False,
        })
        print(f'  ✈️ [Airline General] {kw[:60]}')
        i += 1

    print(f'[Quota 3/5] {len(results)} airline-general keywords built')
    return results


def get_quota_national_seo(count=QUOTA_PER_CATEGORY):
    """
    Category 4: New York-scoped SEO — broad call-intent keywords anchored to
    New York / JFK / LGA / EWR but without airline lock-in.
    """
    print(f'[Quota 4/5] New York-wide SEO keywords (target {count})...')
    results = []
    year = datetime.now().year
    ny_national_templates = [
        "flight cancelled JFK what to do {year}",
        "airline customer service JFK phone numbers {year}",
        "how to get flight refund fast JFK {year}",
        "flight delay compensation JFK LGA EWR {year}",
        "passenger rights when flight cancelled New York {year}",
        "DOT airline refund rules JFK passengers {year}",
        "what to do if airline loses luggage JFK",
        "how to rebook a cancelled flight JFK quickly",
        "airline denied boarding compensation JFK {year}",
        "flight delayed 3 hours JFK what are you owed",
        "best airline for JFK flights {year}",
        "JFK airport flight cancellation rights {year}",
        "LGA delayed flight compensation {year}",
        "EWR Newark flight cancelled refund {year}",
        "New York airport traveler rights {year}",
    ]
    templates_shuffled = ny_national_templates.copy()
    random.shuffle(templates_shuffled)

    for i in range(count):
        template = templates_shuffled[i % len(templates_shuffled)]
        kw = template.format(year=year)
        al_key = random.choice(list(AIRLINES.keys()))
        ap = random.choice(AIRPORTS_DB)
        ev = random.choice(EVENTS_HIGH + EVENTS_MEDIUM)
        vel = velocity_score(random.randint(45, 75), baseline=15)
        results.append({
            'kw': kw, 'airline': al_key, 'airport': ap,
            'event': ev, 'velocity': vel,
            'trend_score': random.randint(50, 80),
            'source': 'quota_national_seo',
            'is_golden': False,
            'force_national': True,
        })
        print(f'  🗽 [NY SEO] {kw[:60]}')

    print(f'[Quota 4/5] {len(results)} NY-wide SEO keywords built')
    return results


def get_quota_custom_keywords(count=QUOTA_PER_CATEGORY):
    """
    Category 5: the user's own keywords from my_keywords.txt — split into
    the first half tagged for Bing-style submission and the second half for
    Google-style, per the user's confirmed split (5/5).
    """
    print(f'[Quota 5/5] Custom keywords from my_keywords.txt (target {count})...')
    all_custom = get_custom_keywords()
    if not all_custom:
        print('[Quota 5/5] my_keywords.txt empty or missing — skipping category')
        return []
    selected = all_custom[:count]
    half = len(selected) // 2 or 1
    for idx, item in enumerate(selected):
        item['source'] = 'quota_custom'
        item['search_engine_focus'] = 'bing' if idx < half else 'google'
    print(f'[Quota 5/5] {len(selected)} custom keywords selected '
          f'({sum(1 for i in selected if i["search_engine_focus"]=="bing")} Bing-focused, '
          f'{sum(1 for i in selected if i["search_engine_focus"]=="google")} Google-focused)')
    return selected


def get_daily_quota_keywords():
    """
    Builds the full daily queue from the 5 fixed categories, 10 each = 50/day.
    If any single category falls short (e.g. trends dry up, custom keywords
    file is short), the shortfall is backfilled from quota_national_seo so
    the daily total still reaches 50 wherever possible.
    """
    queue = []
    queue += get_quota_trending(QUOTA_PER_CATEGORY)
    queue += get_quota_local_seo(QUOTA_PER_CATEGORY)
    queue += get_quota_airline_general(QUOTA_PER_CATEGORY)
    queue += get_quota_national_seo(QUOTA_PER_CATEGORY)
    queue += get_quota_custom_keywords(QUOTA_PER_CATEGORY)

    target_total = QUOTA_PER_CATEGORY * 5
    shortfall = target_total - len(queue)
    if shortfall > 0:
        print(f'[Quota] Shortfall of {shortfall} — backfilling with extra national SEO keywords')
        queue += get_quota_national_seo(shortfall)

    print(f'[Quota] Daily queue built: {len(queue)}/{target_total} total')
    return queue


def get_fallback_keywords(count=6):
    year = datetime.now().year
    month = datetime.now().month
    results = []

    # 1. Basic event + airport templates
    basic_templates = [
        "{airline} {event} {city} {code} help today",
        "{airline} {event} {city} what to do now",
        "{airline} cancelled {city} {code} refund guide",
        "{airline} {event} {code} passengers compensation",
        "stranded {city} {code} {airline} emergency help",
        "{airline} delays {city} {code} rights {year}",
    ]

    # 2. Route keywords
    def get_route_kws(n=2):
        route_kws = []
        for _ in range(n):
            al_key = random.choice(list(AIRLINES.keys()))
            route = random.choice(TOP_ROUTES)
            ev = random.choice(EVENTS_CRITICAL + EVENTS_HIGH)
            ap = next((a for a in AIRPORTS_DB if a['c'] == route[0]), AIRPORTS_DB[0])
            kw = f"{AIRLINES[al_key]['name']} {route[0]} to {route[1]} {ev['n'].lower()} {route[2]}"
            route_kws.append({
                'kw': kw, 'airline': al_key, 'airport': ap,
                'event': ev, 'velocity': velocity_score(random.randint(35, 75), 15),
                'trend_score': random.randint(35, 70), 'source': 'route',
            })
        return route_kws

    # 3. Seasonal keywords
    def get_seasonal_kws(n=1):
        seasonal_kws = []
        current_season = next((s for s in SEASONAL_EVENTS if month in s['months']), None)
        if current_season:
            for _ in range(n):
                al_key = random.choice(list(AIRLINES.keys()))
                ap = random.choice(AIRPORTS_DB)
                ev = random.choice(EVENTS_CRITICAL)
                kw = f"{AIRLINES[al_key]['name']} {current_season['n']} flights cancelled {ap['n']} {ap['c']}"
                seasonal_kws.append({
                    'kw': kw, 'airline': al_key, 'airport': ap,
                    'event': ev, 'velocity': velocity_score(random.randint(50, 90), 15),
                    'trend_score': random.randint(50, 85), 'source': 'seasonal',
                    'is_golden': True,
                })
        return seasonal_kws

    # 4. Compensation keywords
    def get_comp_kws(n=1):
        comp_kws = []
        for _ in range(n):
            al_key = random.choice(list(AIRLINES.keys()))
            ap = random.choice(AIRPORTS_DB)
            ev = random.choice(EVENTS_CRITICAL + EVENTS_HIGH)
            template = random.choice(COMPENSATION_TEMPLATES)
            kw = template.format(
                airline=AIRLINES[al_key]['name'],
                amount=random.choice(COMP_AMOUNTS),
                city=ap['n'], code=ap['c'], year=year
            )
            comp_kws.append({
                'kw': kw, 'airline': al_key, 'airport': ap,
                'event': ev, 'velocity': velocity_score(random.randint(30, 70), 15),
                'trend_score': random.randint(40, 75), 'source': 'compensation',
            })
        return comp_kws

    # 5. Rights keywords
    def get_rights_kws(n=1):
        rights_kws = []
        for _ in range(n):
            al_key = random.choice(list(AIRLINES.keys()))
            ap = random.choice(AIRPORTS_DB)
            ev = random.choice(EVENTS_HIGH)
            template = random.choice(RIGHTS_TEMPLATES)
            kw = template.format(
                airline=AIRLINES[al_key]['name'],
                city=ap['n'], code=ap['c'], year=year
            )
            rights_kws.append({
                'kw': kw, 'airline': al_key, 'airport': ap,
                'event': ev, 'velocity': velocity_score(random.randint(25, 65), 15),
                'trend_score': random.randint(35, 70), 'source': 'rights',
            })
        return rights_kws

    # 6. Problem-specific keywords
    def get_problem_kws(n=1):
        prob_kws = []
        for _ in range(n):
            al_key = random.choice(list(AIRLINES.keys()))
            ap = random.choice(AIRPORTS_DB)
            ev = random.choice(EVENTS_CRITICAL + EVENTS_HIGH)
            template = random.choice(PROBLEM_TEMPLATES)
            kw = template.format(
                airline=AIRLINES[al_key]['name'],
                city=ap['n'], code=ap['c'], year=year
            )
            prob_kws.append({
                'kw': kw, 'airline': al_key, 'airport': ap,
                'event': ev, 'velocity': velocity_score(random.randint(30, 75), 15),
                'trend_score': random.randint(40, 78), 'source': 'problem',
            })
        return prob_kws

    # Mix all categories — scaled for 40 articles
    results += get_seasonal_kws(4)    # seasonal first — highest priority
    results += get_route_kws(6)       # route keywords
    results += get_comp_kws(8)        # compensation
    results += get_rights_kws(6)      # rights
    results += get_problem_kws(6)     # problem-specific

    # Fill rest with basic
    remaining = count - len(results)
    for _ in range(remaining):
        al_key = random.choice(list(AIRLINES.keys()))
        ap = random.choice(AIRPORTS_DB)
        ev = random.choice(EVENTS_CRITICAL + EVENTS_HIGH)
        vel = velocity_score(random.randint(20, 70), baseline=15)
        template = random.choice(basic_templates)
        kw = template.format(
            airline=AIRLINES[al_key]['name'],
            event=ev['n'].lower(),
            city=ap['n'], code=ap['c'], year=year
        )
        results.append({
            'kw': kw, 'airline': al_key, 'airport': ap,
            'event': ev, 'velocity': vel,
            'trend_score': random.randint(25, 65),
            'source': 'fallback',
        })

    results.sort(key=lambda x: (x.get('is_golden', False), x['velocity']['score']), reverse=True)

    # Apply call intent filter — remove low intent keywords
    filtered = [r for r in results if has_call_intent(r['kw'])]
    removed = len(results) - len(filtered)
    if removed > 0:
        print(f'[Call Intent Filter] Removed {removed} low-intent keywords')

    print(f'[Fallback Keywords] {len(filtered)} keywords — categories: seasonal/route/compensation/rights/problem/basic')
    return filtered[:count]

# ══════════════════════════════════════════
# ANTI-SPAM CHECK
# ══════════════════════════════════════════
def anti_spam_check(content, keyword):
    words = content.lower().split()
    total = len(words)
    if total < 700:
        return False, f'Too short: {total}w'
    kw_words = keyword.lower().split()
    kw_count = sum(1 for w in words if w in kw_words)
    density = (kw_count / total) * 100 if total > 0 else 0
    if density > 8.0:
        return False, f'KW density too high: {density:.1f}%'
    for phrase in ['as an AI', 'I cannot', 'language model', 'I apologize', 'as an assistant']:
        if phrase.lower() in content.lower():
            return False, f'AI fingerprint: {phrase}'
    return True, f'PASS ({total}w, density {density:.1f}%)'

# ══════════════════════════════════════════
# GEMINI KEY ROTATION
# ══════════════════════════════════════════
import itertools as _itertools

def _build_gemini_keys():
    keys = []
    # Primary key
    if GEMINI_API_KEY:
        keys.append(GEMINI_API_KEY)
    # Extra keys: GEMINI_API_KEY_2, GEMINI_API_KEY_3, ...
    for i in range(2, 6):
        k = os.environ.get(f'GEMINI_API_KEY_{i}', '').strip()
        if k:
            keys.append(k)
    return keys

_GEMINI_KEYS = _build_gemini_keys()
_gemini_cycle = _itertools.cycle(_GEMINI_KEYS) if _GEMINI_KEYS else None

def _next_gemini_key():
    if _gemini_cycle:
        return next(_gemini_cycle)
    return None

# ══════════════════════════════════════════
# LLM API CALL — Groq primary, Gemini fallback
# ══════════════════════════════════════════
def call_api(prompt):
    # ── 1. Groq with key rotation ──
    # Free tier on llama-3.3-70b-versatile is 12,000 TPM per key. A second
    # full retry cycle with short waits just re-hits the same exhausted
    # window, so: try each key once quickly (catches transient/non-rate
    # issues), then fall to Gemini immediately rather than burning minutes
    # waiting for Groq's per-minute window to reset mid-run.
    if _GROQ_KEYS:
        for attempt in range(len(_GROQ_KEYS)):
            key = next(_groq_cycle)
            try:
                r = requests.post(
                    'https://api.groq.com/openai/v1/chat/completions',
                    headers={
                        'Authorization': f'Bearer {key}',
                        'Content-Type': 'application/json',
                    },
                    json={
                        'model': 'llama-3.3-70b-versatile',
                        'messages': [{'role': 'user', 'content': prompt}],
                        'max_tokens': 3500,
                        'temperature': 0.85,
                    },
                    timeout=90,
                )
                if r.status_code == 200:
                    return r.json()['choices'][0]['message']['content']
                if r.status_code == 429:
                    print(f'  [Groq key#{attempt+1}] 429 — trying next key...')
                    import time; time.sleep(3)
                else:
                    print(f'  [Groq] {r.status_code} — falling back to Gemini...')
                    break
            except Exception as e:
                print(f'  [Groq] Error: {e} — falling back to Gemini...')
                break
        else:
            print('  [Groq] All keys rate-limited this minute — falling back to Gemini...')

    # ── 2. Gemini fallback with key rotation ──
    if not _GEMINI_KEYS:
        raise Exception('No API keys available (GROQ_API_KEY and GEMINI_API_KEY both missing)')

    for attempt in range(len(_GEMINI_KEYS) * 2):  # try each key twice max
        key = _next_gemini_key()
        url = f'https://generativelanguage.googleapis.com/v1beta/models/{MODEL}:generateContent?key={key}'
        data = {
            'contents': [{'parts': [{'text': prompt}]}],
            'generationConfig': {'maxOutputTokens': 3500, 'temperature': 0.85},
        }
        try:
            r = requests.post(
                url,
                headers={'Content-Type': 'application/json'},
                json=data,
                timeout=90,
            )
            if r.status_code == 200:
                return r.json()['candidates'][0]['content']['parts'][0]['text']
            if r.status_code == 429:
                wait = 65  # fixed 65s — ensures we exit the 1-min rate-limit window
                print(f'  [Gemini key#{attempt % len(_GEMINI_KEYS) + 1}] 429 — rotating key, waiting {wait}s...')
                time.sleep(wait)
                continue
            if r.status_code == 404:
                raise Exception(f'Gemini 404 — model "{MODEL}" not found. Check MODEL name.')
            raise Exception(f'Gemini API {r.status_code}: {r.text[:200]}')
        except Exception as e:
            if '404' in str(e) or 'model' in str(e).lower():
                raise  # don't retry on model errors
            print(f'  [Gemini] attempt {attempt+1}: {e}')
            time.sleep(5)

    raise Exception('All API keys exhausted — 429 max retries exceeded')

# ══════════════════════════════════════════
# GENERATE ARTICLE
# ══════════════════════════════════════════
def generate_article(item, published, is_national=False, active_clusters=None, multi_states=None, platform_map=None):
    kw        = item['kw']
    al_key    = item['airline']
    ap        = item['airport']
    event     = item['event']
    velocity  = item['velocity']
    al        = AIRLINES[al_key]
    phone     = al['phone']
    tel       = al['tel']
    year      = datetime.now().year

    slug = make_slug(kw)
    if slug in published:
        print(f'  ⏭ SKIP exact duplicate: {slug[:50]}')
        return None
    if is_similar_slug(slug, published, threshold=0.85):
        print(f'  ⏭ SKIP similar content: {slug[:50]}')
        return None

    # Weight score
    ap_weight     = airport_weight(ap)
    article_count = item.get('article_count', random.randint(1, 5))
    lifetime      = predict_lifetime(event)

    print(f'  📊 AP Weight:{ap_weight} | Velocity:{velocity["label"]} | Lifetime:{lifetime["hours"]}h')
    if is_national:
        print(f'  🇺🇸 NATIONAL IMPACT — Priority MAX')
    if active_clusters:
        print(f'  🔗 Active clusters: {[c["name"] for c in active_clusters]}')

    # Build prompt
    t_idx  = hash(kw) % len(TITLE_TEMPLATES)
    s_idx  = hash(kw + 'struct') % 4
    b_idx  = hash(kw + 'box') % 4
    seed   = hashlib.md5(kw.encode()).hexdigest()[:8]
    title  = TITLE_TEMPLATES[t_idx](al['name'], ap['n'], ap['c'], phone)

    national_context = ''
    if is_national:
        national_context = f'\nNATIONAL CONTEXT: This airline is experiencing disruptions at multiple major US hubs right now — mention this is affecting travelers nationwide, not just {ap["n"]}.'

    cluster_context = ''
    if active_clusters:
        names = ', '.join(c['name'] for c in active_clusters)
        cluster_context = f'\nREGIONAL CONTEXT: {names} — multiple airports in the region are seeing related disruptions. Mention neighboring airports where relevant.'

    structures = [
        f"Local landing page for {ap['c']} ({ap['n']}, {ap['s']}). 1.Intro (150w): why travelers from {ap['n']} contact FlyUSS for {al['name']} 2.Call script section [BOX] 3.Step-by-step numbered list: how FlyUSS agents help (booking/changes/refunds) — at least 5 steps with detail 4.What to expect / typical compensation ranges, explained 5.Common scenarios travelers face at {ap['c']} (3-4 specific situations) 6.Local FAQ (5-6 Q&A, mention {ap['c']} and {ap['s']}) 7.Closing CTA",
        f"YOUR RIGHTS guide for {al['name']} passengers at {ap['n']} {ap['c']}. 1.Intro (150w) framing the rights gap travelers face 2.What {al['name']} owes under DOT rules {year} — detailed breakdown by scenario 3.Call FlyUSS for help [BOX] 4.Compensation checklist (numbered, 5+ items) 5.Common excuses airlines give vs actual passenger rights (3-4 examples) 6.How to document your case before calling 7.FAQ (5-6 Q&A) 8.CTA",
        f"Practical help guide, {ap['c']} focus. 1.Intro (150w) setting the scene for {ap['n']} travelers 2.Common situations travelers from {ap['n']} face with {al['name']} (4-5 detailed scenarios) 3.Call script [BOX] 4.Scenario-based solutions (cancellation/delay/refund/rebooking — each with its own short section) 5.What compensation may apply, with reasoning 6.What to have ready before you call (numbered list) 7.FAQ (5-6 Q&A) 8.Final CTA",
        f"Insider tips for {al['name']} travelers flying through {ap['c']}. 1.Intro (150w) hook on what most passengers don't know 2.What most passengers don't know about their rights (detailed, 3-4 points) 3.Why calling FlyUSS at {phone} saves time vs the airline line — be specific 4.Local tips for {ap['n']} {year} [BOX] 5.Entitled vs offered compensation — detailed comparison 6.Step-by-step: what to do right now (numbered list) 7.FAQ (5-6 Q&A) 8.CTA",
    ]

    help_boxes = [
        f'<div style="background:linear-gradient(135deg,#eff6ff,#dbeafe);border:2px solid var(--blue);border-radius:12px;padding:26px;margin:30px 0;text-align:center"><div style="font-size:11px;color:#0e4b9b;font-weight:800;letter-spacing:3px;margin-bottom:8px">📞 TALK TO AN AGENT</div><div style="font-size:36px;font-weight:900;color:var(--blue)">{phone}</div><div style="font-size:12px;color:#64748b;margin:8px 0 16px">Independent agents available 24/7</div><a href="{tel}" style="display:inline-block;padding:13px 40px;background:var(--blue);color:#fff;border-radius:8px;font-weight:900;font-size:16px;text-decoration:none">📞 Call {phone}</a></div>',
        f'<div style="background:#fff;border-left:6px solid var(--blue);border-right:6px solid var(--blue);padding:24px;margin:30px 0;border-radius:8px"><div style="display:flex;align-items:center;justify-content:space-between;flex-wrap:wrap;gap:12px"><div><div style="font-size:30px;font-weight:900;color:var(--dark)">{phone}</div><div style="font-size:11px;color:#64748b;margin-top:3px">Independent agents • 24/7 • All 50 states</div></div><a href="{tel}" style="padding:13px 26px;background:var(--blue);color:#fff;border-radius:8px;font-weight:900;text-decoration:none">📞 Call Now</a></div></div>',
        f'<div style="background:linear-gradient(135deg,var(--blue),#1e3a8a);border-radius:12px;padding:24px;margin:30px 0;text-align:center"><div style="font-size:32px;font-weight:900;color:#fff;margin:7px 0">{phone}</div><a href="{tel}" style="display:inline-block;padding:12px 36px;background:#fff;color:var(--blue);border-radius:8px;font-weight:900;font-size:15px;text-decoration:none">📞 Get Help Now</a></div>',
        f'<div style="border:1px solid var(--blue);border-radius:9px;padding:20px;margin:30px 0;background:#f8fafc;position:relative"><div style="position:absolute;top:-10px;left:18px;background:var(--blue);padding:2px 10px;border-radius:4px;font-size:9px;font-weight:800;color:#fff">SUPPORT LINE</div><div style="display:grid;grid-template-columns:1fr auto;gap:12px;align-items:center"><div style="font-size:26px;font-weight:900;color:var(--blue)">{phone}</div><a href="{tel}" style="display:block;padding:11px 20px;background:var(--blue);color:#fff;border-radius:8px;font-weight:900;text-decoration:none;text-align:center">📞 Call<br>Now</a></div></div>',
    ]
    box = help_boxes[b_idx].format(phone=phone, tel=tel)

    prompt = f"""Write 100% UNIQUE HTML body (no html/head/body tags). Seed:{seed}
Keyword:"{kw}"
Title:"{title}"
Airline:{al['name']} | Airport:{ap['n']} ({ap['c']}) | State:{ap['s']} | Phone:{phone} | Year:{year}
Tone: helpful, professional, calm — like a local travel-assistance landing page, NOT an alarmist crisis post.
{national_context}{cluster_context}

STRUCTURE: {structures[s_idx]}
REPLACE [BOX] WITH: {box}

RULES:
✅ Phone {phone} minimum 6 times
✅ 1200+ words minimum — this must be a genuinely substantial, in-depth guide, not padded filler. Go deep on each section: real specifics, real scenarios, real numbers.
✅ HTML inline CSS only — use var(--blue), var(--dark), var(--gray), var(--red) for colors (already defined on the page)
✅ NO AI phrases (as an AI, I cannot, language model)
✅ NO filler or repetition — every paragraph must add new, concrete information (not restating the previous paragraph in different words)
✅ Each H2 unique and specific to {ap['n']} {ap['c']}
✅ Keyword density MAX 2.5% — vary synonyms and related phrases
✅ p style: color:#475569;font-size:15px;line-height:1.9;margin-bottom:14px
✅ H2 style: color:var(--blue);font-size:26px;font-weight:800;margin:32px 0 14px;border-bottom:2px solid #e5e7eb;padding-bottom:9px
✅ Mention realistic dollar ranges where relevant: $200-$1,550 cancellations, $100-$700 delays — explain WHY those ranges apply (fare class, distance, DOT rules), not just the numbers
✅ Mention {ap['n']} and {ap['c']} details to feel local — nearby neighborhoods, terminal names, or common routes if known
✅ Disclose clearly that FlyUSS is an independent service, not the airline itself (at least once in the body)
✅ Include at least one realistic step-by-step list (numbered or bulleted) with concrete actions, not vague advice
✅ Include a FAQ section with 4-6 specific questions and complete, useful answers (not one-liners)

TRUST (mandatory, once near the top):
✅ Trust bar: <div style="display:flex;gap:20px;flex-wrap:wrap;margin:20px 0;padding:14px;background:#f8fafc;border-radius:8px;font-size:12px;color:#64748b">⭐ 4.9/5 Rating &nbsp;|&nbsp; 🔒 100% Free Consultation &nbsp;|&nbsp; ✈️ All US Airlines &nbsp;|&nbsp; 📞 24/7 Live Agents</div>

CTA BUTTON (use exactly this HTML — place 3 times at key moments, spaced through the article):
<div style="text-align:center;margin:30px 0">
<a href="{tel}" style="display:inline-block;padding:16px 44px;background:var(--blue);color:#fff;border-radius:8px;font-weight:800;font-size:18px;text-decoration:none;letter-spacing:.3px">📞 Call {phone}</a>
<p style="color:#94a3b8;font-size:12px;margin-top:8px">Agents available now</p>
</div>"""

    print(f'  ✍ Writing: "{title[:55]}"')
    body = call_api(prompt)

    passed, msg = anti_spam_check(body, kw)
    print(f'  Anti-spam: {msg}')
    if not passed:
        return None

    # Get related pages for internal linking
    related = get_related_pages(published, slug, al_key, ap['c'], platform_map)

    html = build_page(al_key, title, kw, body, ap, is_national, related)
    filename = slug + '.html'
    (OUTPUT_DIR / filename).write_text(html, encoding='utf-8')

    return {
        'slug': filename, 'title': title, 'kw': kw,
        'airline': al_key, 'airport': ap['c'], 'city': ap['n'],
        'words': len(body.split()), 'html': html,
        'national': is_national, 'velocity': velocity['label'],
        'lifetime': lifetime['hours'],
        'platform': item.get('platform', ''),
    }


def get_related_pages(published, current_slug, al_key, ap_code, platform_map=None):
    """
    Get 3-5 related published slugs for internal linking.
    Prefers same airline or same airport — falls back to random.
    Only links to pages confirmed published on GitHub Pages (internal links use GH Pages URLs).
    """
    if not published:
        return []
    platform_map = platform_map or {}
    # Remove current slug and .html extension for comparison
    others = [s for s in published if s != current_slug and s != current_slug.replace('.html','')]
    # Only keep slugs known to be on GitHub Pages (avoid broken/Blogger-only links)
    others = [s for s in others if platform_map.get(s) == 'github']
    if not others:
        return []
    # Prefer same airline
    airline_name = AIRLINES[al_key]['name'].lower().split()[0]
    same_airline = [s for s in others if airline_name in s.lower()]
    # Prefer same airport
    same_airport = [s for s in others if ap_code.lower() in s.lower()]
    # Build related list
    related = list(set(same_airline[:2] + same_airport[:2]))
    # Fill with random if needed
    remaining = [s for s in others if s not in related]
    random.shuffle(remaining)
    related += remaining[:max(0, 4 - len(related))]
    return related[:4]

# ══════════════════════════════════════════
# BUILD PAGE
# ══════════════════════════════════════════
def build_page(al_key, title, kw, body, ap, is_national, related=None):
    """
    Builds a full standalone HTML page matching flyuss.com's real homepage
    template exactly: same CSS variables, ticker, logo, nav with Airlines
    dropdown, hero, services-style trust bar, FAB call button, legal
    disclaimer block, and footer. Only the hero content, body article,
    phone number, and meta tags change per page.
    """
    al        = AIRLINES[al_key]
    phone     = al['phone']
    tel       = al['tel']
    img       = AIRLINE_IMAGES[al_key]
    pub_human = datetime.now().strftime('%B %d, %Y')
    year      = datetime.now().year
    city, code, state = ap['n'], ap['c'], ap['s']

    meta_desc = f"Need help with an {al['name']} flight at {city} ({code})? FlyUSS independent agents assist with bookings, changes, cancellations and refunds. Call {phone}."

    # Internal links to other published local pages (same flyuss.com site)
    internal_links_html = ''
    if related:
        links_html = ''
        for slug_item in related:
            clean = slug_item.replace('.html', '').replace('-', ' ').title()
            links_html += (f'<a href="{SITE_BASE_URL}/pages/{slug_item}.html" '
                            f'style="display:block;background:#f8fafc;border:1px solid #e5e7eb;border-radius:8px;'
                            f'padding:10px 14px;color:#0f172a;text-decoration:none;font-size:13px;font-weight:600;'
                            f'margin-bottom:8px" onmouseover="this.style.borderColor=\'var(--blue)\'" '
                            f'onmouseout="this.style.borderColor=\'#e5e7eb\'">→ {clean[:60]}</a>')
        internal_links_html = f'''<div style="background:#fff;border:1px solid #e5e7eb;border-radius:12px;padding:20px;margin:28px 0">
<h3 style="font-size:14px;font-weight:800;color:var(--blue);letter-spacing:.3px;margin-bottom:14px">🔗 More Local Guides on FlyUSS</h3>
{links_html}
</div>'''

    national_note = ''
    if is_national:
        national_note = (f'<div style="background:#fffbeb;border:1px solid #fcd34d;border-radius:8px;padding:14px 18px;'
                          f'margin-bottom:16px;font-size:13px;color:#92400e"><strong>Heads up:</strong> {al["name"]} is currently '
                          f'experiencing disruptions at several major US airports — this is affecting travelers nationwide, not just {city}.</div>')

    return f'''<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{title} | FlyUSS</title>
<meta name="description" content="{meta_desc}">
<meta name="theme-color" content="#0e4b9b">
<link rel="canonical" href="{SITE_BASE_URL}/pages/{make_slug(kw)}.html">
<style>
:root{{--blue:#0e4b9b;--dark:#0f172a;--red:#c8102e;--gray:#64748b;--light:#f8fafc}}
*{{margin:0;padding:0;box-sizing:border-box}}
body{{font-family:-apple-system,BlinkMacSystemFont,Segoe UI,Arial,sans-serif;color:var(--dark);line-height:1.6}}
.wrap{{max-width:1200px;margin:0 auto;padding:0 20px}}

.ticker{{background:var(--blue);color:#fff;padding:12px 0;overflow:hidden;position:sticky;top:0;z-index:100;box-shadow:0 2px 8px rgba(0,0,0,.1)}}
.ticker-wrap{{display:flex;overflow:hidden}}
.ticker-move{{display:flex;gap:40px;animation:scroll 20s linear infinite;white-space:nowrap}}
.ticker-move span{{display:inline-block;font-size:14px;font-weight:600}}
@keyframes scroll{{0%{{transform:translateX(0)}}100%{{transform:translateX(-50%)}}}}

.nav{{background:#fff;padding:16px 0;border-bottom:1px solid #e5e7eb;position:sticky;top:48px;z-index:99;backdrop-filter:blur(10px)}}
.nav-wrap{{display:flex;justify-content:space-between;align-items:center}}

.logo{{display:flex;align-items:center;gap:10px;padding:8px 16px;background:#fff;border:2px solid var(--blue);border-radius:12px;box-shadow:0 4px 12px rgba(14,75,155,.15);text-decoration:none}}
.logo-diamond{{width:32px;height:32px;background:var(--blue);transform:rotate(45deg);display:flex;align-items:center;justify-content:center;position:relative}}
.logo-diamond::after{{content:'✈️';position:absolute;transform:rotate(-45deg);font-size:16px}}
.logo-text{{font-size:28px;font-weight:900;color:var(--blue);letter-spacing:-1px}}
.logo-text span{{color:var(--red)}}

.menu{{display:flex;gap:8px;align-items:center}}
.menu a{{color:var(--dark);text-decoration:none;font-weight:600;transition:.2s;font-size:15px;padding:6px 10px;border-radius:6px}}
.menu a:hover{{color:var(--blue);background:#f1f5f9}}

.dropdown{{position:relative}}
.dropdown-toggle{{color:var(--dark);text-decoration:none;font-weight:600;font-size:15px;padding:6px 10px;border-radius:6px;cursor:pointer;display:flex;align-items:center;gap:4px;background:none;border:none;font-family:inherit;transition:.2s}}
.dropdown-toggle:hover{{color:var(--blue);background:#f1f5f9}}
.dropdown-toggle::after{{content:'▾';font-size:11px;opacity:.6}}
.dropdown-menu{{display:none;position:absolute;top:calc(100% + 8px);left:0;background:#fff;border:1px solid #e5e7eb;border-radius:10px;box-shadow:0 8px 24px rgba(0,0,0,.12);min-width:180px;z-index:200;overflow:hidden}}
.dropdown-menu a{{display:block;padding:10px 16px;color:var(--dark);text-decoration:none;font-weight:600;font-size:14px;transition:.15s}}
.dropdown-menu a:hover{{background:#f1f5f9;color:var(--blue)}}
.dropdown:hover .dropdown-menu{{display:block}}
.btn{{display:inline-block;padding:12px 24px;background:var(--blue);color:#fff;border-radius:8px;text-decoration:none;font-weight:700;transition:.2s;border:none;cursor:pointer}}
.btn:hover{{background:#0b3c7c;transform:translateY(-2px)}}
.btn-red{{background:var(--red)}}
.btn-red:hover{{background:#b91c1c}}
.btn-outline{{background:transparent;border:3px solid #fff;color:#fff}}
.btn-outline:hover{{background:rgba(255,255,255,.2);border-color:#fff}}

.hero{{position:relative;min-height:420px;display:flex;align-items:center;overflow:hidden;padding:40px 0}}
.hero-bg{{position:absolute;inset:0;z-index:0}}
.hero-bg img{{width:100%;height:100%;object-fit:cover;object-position:center}}
.hero-overlay{{position:absolute;inset:0;background:linear-gradient(135deg,rgba(14,75,155,.88) 0%,rgba(14,75,155,.68) 50%,rgba(59,130,246,.55) 100%);z-index:1}}
.hero-content{{position:relative;z-index:2;color:#fff}}
.hero .breadcrumb{{font-size:12px;color:rgba(255,255,255,.75);margin-bottom:14px;font-weight:600}}
.hero .breadcrumb a{{color:#fff;text-decoration:none}}
.hero h1{{font-size:38px;margin-bottom:14px;font-weight:900;line-height:1.15;text-shadow:0 2px 20px rgba(0,0,0,.3)}}
.hero p{{font-size:17px;margin-bottom:24px;opacity:.95;max-width:680px;text-shadow:0 1px 10px rgba(0,0,0,.2)}}
.hero-subtitle{{font-size:13px;margin-top:14px;opacity:.85}}

.flex{{display:flex;gap:16px;flex-wrap:wrap}}
.sect{{padding:50px 0}}
.sect-title{{font-size:30px;text-align:center;margin-bottom:30px;font-weight:800}}
.trust{{background:#f8fafc;border-top:1px solid #e5e7eb;border-bottom:1px solid #e5e7eb;padding:14px 0}}
.trust-inner{{max-width:1200px;margin:0 auto;padding:0 20px;display:flex;gap:24px;justify-content:center;flex-wrap:wrap}}
.trust-item{{font-size:12px;font-weight:700;color:var(--gray)}}

.main-layout{{max-width:1200px;margin:0 auto;padding:40px 20px;display:grid;grid-template-columns:1fr 300px;gap:32px}}
.article h2{{font-size:24px;font-weight:800;color:var(--blue);margin:32px 0 12px;border-bottom:2px solid #e5e7eb;padding-bottom:8px}}
.article h3{{font-size:17px;font-weight:700;color:var(--dark);margin:18px 0 8px}}
.article p{{color:#475569;font-size:15px;line-height:1.9;margin-bottom:14px}}
.article ul,.article ol{{padding-left:22px;color:#475569;font-size:15px;line-height:1.9;margin-bottom:14px}}

.sidebar{{display:flex;flex-direction:column;gap:16px}}
.side-card{{background:#fff;border:2px solid var(--blue);border-radius:12px;padding:20px;text-align:center;box-shadow:0 4px 12px rgba(14,75,155,.1)}}
.side-card h3{{font-size:12px;font-weight:800;color:var(--blue);letter-spacing:1px;margin-bottom:6px}}
.side-card .sp{{font-size:24px;font-weight:900;color:var(--dark);margin:8px 0}}
.side-card a.btn{{display:block;margin-top:10px}}
.side-links{{background:#fff;border:1px solid #e5e7eb;border-radius:12px;padding:16px}}
.side-links h4{{font-size:11px;font-weight:800;color:var(--gray);letter-spacing:1px;margin-bottom:10px;text-transform:uppercase}}
.side-links a{{display:block;color:#475569;text-decoration:none;font-size:13px;padding:6px 0;border-bottom:1px solid #f1f5f9}}
.side-links a:last-child{{border-bottom:none}}

.cta{{background:linear-gradient(135deg,#c8102e,#ef4444);color:#fff;padding:50px 0;text-align:center}}
.cta h2{{font-size:30px;margin-bottom:14px}}
.cta p{{font-size:16px;margin-bottom:24px;opacity:.95}}

.disclosure{{background:#f1f5f9;border-top:3px solid #e2e8f0;padding:40px 0}}
.disclosure-box{{background:#fff;border-radius:12px;padding:24px;border:1px solid #e2e8f0;margin-bottom:14px}}
.disclosure-box h3{{font-size:14px;font-weight:700;color:var(--dark);margin-bottom:8px}}
.disclosure-box p{{font-size:12px;color:var(--gray);line-height:1.85}}
.disclosure-final{{background:#fffbeb;border-radius:12px;padding:18px 24px;border:1px solid #fcd34d}}
.disclosure-final p{{font-size:11px;color:#92400e;line-height:1.85}}

.footer{{background:var(--dark);color:#cbd5e1;padding:36px 0}}
.footer-content{{display:grid;grid-template-columns:repeat(auto-fit,minmax(200px,1fr));gap:28px;margin-bottom:28px}}
.footer-section h4{{color:#fff;margin-bottom:10px;font-size:15px}}
.footer-section ul{{list-style:none;padding:0}}
.footer-section ul li{{margin-bottom:7px}}
.footer a{{color:#cbd5e1;text-decoration:none;font-size:13px}}
.footer a:hover{{color:#fff}}
.footer-bottom{{text-align:center;padding-top:20px;border-top:1px solid #334155;font-size:11px;opacity:.6}}
.footer .logo{{border-color:#3b82f6;box-shadow:0 4px 12px rgba(59,130,246,.2)}}

.fab{{position:fixed;bottom:28px;right:28px;width:120px;height:120px;background:linear-gradient(135deg,var(--red),#ef4444);color:#fff;border-radius:50%;display:flex;align-items:center;justify-content:center;font-size:54px;box-shadow:0 12px 48px rgba(200,16,46,.5);cursor:pointer;z-index:999;text-decoration:none;animation:pulse 2s infinite;border:4px solid rgba(255,255,255,.4)}}
@keyframes pulse{{0%,100%{{transform:scale(1);box-shadow:0 12px 48px rgba(200,16,46,.5)}}50%{{transform:scale(1.15);box-shadow:0 16px 60px rgba(200,16,46,.7)}}}}

.ham{{display:none;flex-direction:column;gap:4px;cursor:pointer;padding:8px}}
.ham span{{width:24px;height:3px;background:var(--dark);border-radius:2px;transition:.3s}}
@media(max-width:768px){{
.menu{{display:none;position:absolute;top:60px;left:0;right:0;background:#fff;flex-direction:column;padding:20px;box-shadow:0 4px 12px rgba(0,0,0,.1)}}
.menu.show{{display:flex}}
.ham{{display:flex}}
.dropdown-menu{{display:flex!important;position:static;border:none;box-shadow:none;background:#f8fafc;border-radius:8px;margin:4px 0;min-width:auto}}
.dropdown-menu a{{padding:8px 16px;font-size:14px}}
.dropdown-toggle{{width:100%;justify-content:space-between}}
.hero{{min-height:340px;padding:36px 0 90px}}
.hero h1{{font-size:24px;line-height:1.25}}
.hero p{{font-size:14px}}
.hero .flex{{margin-bottom:8px}}
.sect-title{{font-size:24px}}
.main-layout{{grid-template-columns:1fr}}
.sidebar{{display:none}}
.fab{{width:105px;height:105px;font-size:48px;bottom:20px;right:20px}}
.logo{{padding:6px 12px}}
.logo-text{{font-size:20px}}
.logo-diamond{{width:24px;height:24px}}
.logo-diamond::after{{font-size:12px}}
.footer-content{{grid-template-columns:1fr}}
.footer .logo{{justify-content:center;margin:0 auto}}
}}
</style>
</head>
<body>

<div class="ticker">
  <div class="ticker-wrap">
    <div class="ticker-move">
      <span>✈️ Book flights instantly</span>
      <span>📞 24/7 Customer Support</span>
      <span>🌍 All major airlines covered</span>
      <span>💳 Secure payments</span>
      <span>⚡ Same-day changes available</span>
      <span>🎫 Best price guarantee</span>
      <span>✈️ Book flights instantly</span>
      <span>📞 24/7 Customer Support</span>
      <span>🌍 All major airlines covered</span>
      <span>💳 Secure payments</span>
      <span>⚡ Same-day changes available</span>
      <span>🎫 Best price guarantee</span>
    </div>
  </div>
</div>

<nav class="nav">
  <div class="wrap nav-wrap">
    <a href="{SITE_BASE_URL}/" class="logo">
      <div class="logo-diamond"></div>
      <div class="logo-text">Fly<span>USS</span></div>
    </a>
    <button class="ham" id="ham" onclick="toggleMenu()">
      <span></span><span></span><span></span>
    </button>
    <div class="menu" id="menu">
      <a href="{SITE_BASE_URL}/#services">Services</a>
      <div class="dropdown">
        <button class="dropdown-toggle">Airlines</button>
        <div class="dropdown-menu">
          <a href="{SITE_BASE_URL}/delta-airlines.html">✈️ Delta Air Lines</a>
        </div>
      </div>
      <a href="{SITE_BASE_URL}/#why">Why Us</a>
      <a href="{SITE_BASE_URL}/#faq">FAQ</a>
      <a href="{SITE_BASE_URL}/#contact">Contact</a>
      <a href="{tel}" class="btn">📞 Call Now</a>
    </div>
  </div>
</nav>

<section class="hero">
  <div class="hero-bg">
    <img src="{img}" alt="{al['name']} flight assistance {city} {code}" loading="eager">
  </div>
  <div class="hero-overlay"></div>
  <div class="wrap hero-content">
    <div class="breadcrumb"><a href="{SITE_BASE_URL}/">FlyUSS</a> &nbsp;›&nbsp; {al['name']} &nbsp;›&nbsp; {city}, {state}</div>
    <h1>{title}</h1>
    <p>Independent travel agents helping {city} travelers with {al['name']} bookings, same-day changes, cancellations and refunds — serving {code} and the surrounding area.</p>
    <div class="flex">
      <a href="{tel}" class="btn btn-red">📞 {phone}</a>
      <a href="#guide" class="btn btn-outline">Read Guide</a>
    </div>
    <p class="hero-subtitle">Independent phone assistance — not affiliated with {al['name']}.</p>
  </div>
</section>

<div class="trust"><div class="trust-inner">
<div class="trust-item">⏱ Fast Response</div>
<div class="trust-item">🔒 100% Confidential</div>
<div class="trust-item">🌎 All 50 US States</div>
<div class="trust-item">📞 Agents Available 24/7</div>
<div class="trust-item">✅ Fees Disclosed Upfront</div>
</div></div>

<div class="main-layout">
<article class="article" id="guide">
{national_note}
{body}
{internal_links_html}
</article>
<aside class="sidebar">
<div class="side-card">
<h3>NEED HELP NOW?</h3>
<p style="font-size:11px;color:var(--gray)">{al['name']} — {city}, {state}</p>
<div class="sp">{phone}</div>
<a href="{tel}" class="btn btn-red">📞 Call 24/7</a>
<p style="font-size:11px;color:var(--gray);margin-top:8px">Independent agents — not the airline</p>
</div>
<div class="side-links">
<h4>Quick Links</h4>
<a href="{SITE_BASE_URL}/#services">Services</a>
<a href="{SITE_BASE_URL}/#faq">FAQ</a>
<a href="{SITE_BASE_URL}/#contact">Contact</a>
<a href="{SITE_BASE_URL}/#privacy">Privacy Policy</a>
</div>
</aside>
</div>

<section class="cta" id="contact">
  <div class="wrap">
    <h2>Need Help With Your {al['name']} Flight in {city}?</h2>
    <p>Our independent agents are standing by to help — 24/7</p>
    <div class="flex" style="justify-content:center">
      <a href="{tel}" class="btn" style="background:#fff;color:var(--red);font-size:18px;padding:16px 32px">
        📞 Call {phone}
      </a>
    </div>
    <p style="margin-top:18px;font-size:13px;opacity:.9">
      Available 24/7 | All Major Airlines | Serving {city}, {state} and All 50 States
    </p>
  </div>
</section>

<section class="disclosure" id="disclaimer">
  <div class="wrap">
    <div style="max-width:860px;margin:0 auto">
      <h2 style="font-size:20px;font-weight:800;color:var(--dark);margin-bottom:6px">⚖️ Legal Disclaimer &amp; Important Disclosure</h2>
      <p style="font-size:12px;color:var(--gray);margin-bottom:20px">Please read this disclosure carefully before using our services.</p>

      <div class="disclosure-box">
        <h3>📌 Independent Service Provider</h3>
        <p>FlyUSS is an <strong>independent, third-party travel assistance service</strong> and is <strong>not affiliated with, endorsed by, sponsored by, or officially connected to</strong> {al['name']} or any other airline. FlyUSS does not represent, act on behalf of, or hold any agency relationship with any airline.</p>
      </div>

      <div class="disclosure-box">
        <h3>®️ Trademark Notice</h3>
        <p>{al['name']}® is a registered trademark of its respective owner. All airline names, logos, and trademarks referenced on this page are the exclusive property of their respective owners and are used solely for descriptive and informational purposes.</p>
      </div>

      <div class="disclosure-box">
        <h3>💲 Pricing &amp; Fees</h3>
        <p>Flight availability, pricing, fare rules, refund eligibility, and cancellation policies are set entirely by {al['name']} and are subject to change without notice. <strong>Service fees charged by FlyUSS for agent assistance are separate from and in addition to any fees imposed by the airline.</strong> All applicable fees will be clearly disclosed before any transaction is processed.</p>
      </div>

      <div class="disclosure-box">
        <h3>🗺️ Local Coverage — {city}, {state}</h3>
        <p>FlyUSS provides phone-based travel assistance to travelers near {city} ({code}) and across <strong>all 50 United States</strong>. Our service is available to any traveler calling from within the United States regardless of their state of residence.</p>
      </div>

      <div class="disclosure-final">
        <p><strong>By contacting FlyUSS, you acknowledge that:</strong> (1) you are engaging an independent service provider, not the airline directly; (2) FlyUSS acts solely as an intermediary between you and the airline; (3) the airline itself remains solely responsible for flight operations, schedules, and policies; and (4) service fees may apply for FlyUSS agent assistance and will be disclosed prior to any transaction.</p>
      </div>
    </div>
  </div>
</section>

<footer class="footer">
  <div class="wrap">
    <div class="footer-content">
      <div class="footer-section">
        <a href="{SITE_BASE_URL}/" class="logo" style="margin-bottom:16px;display:inline-flex">
          <div class="logo-diamond"></div>
          <div class="logo-text" style="color:#cbd5e1">Fly<span style="color:var(--red)">USS</span></div>
        </a>
        <p style="font-size:14px;margin-top:12px">Independent phone assistance for major US airlines</p>
      </div>
      <div class="footer-section">
        <h4>Services</h4>
        <ul>
          <li><a href="{SITE_BASE_URL}/#services">Flight Booking</a></li>
          <li><a href="{SITE_BASE_URL}/#services">Changes &amp; Modifications</a></li>
          <li><a href="{SITE_BASE_URL}/#services">Cancellations</a></li>
          <li><a href="{SITE_BASE_URL}/#services">Upgrades</a></li>
        </ul>
      </div>
      <div class="footer-section">
        <h4>Company</h4>
        <ul>
          <li><a href="{SITE_BASE_URL}/#why">About Us</a></li>
          <li><a href="{SITE_BASE_URL}/#faq">FAQ</a></li>
          <li><a href="{SITE_BASE_URL}/#contact">Contact</a></li>
        </ul>
      </div>
      <div class="footer-section">
        <h4>Airlines</h4>
        <ul>
          <li><a href="{SITE_BASE_URL}/delta-airlines.html">Delta Air Lines</a></li>
        </ul>
      </div>
      <div class="footer-section">
        <h4>Legal</h4>
        <ul>
          <li><a href="{SITE_BASE_URL}/#privacy">Privacy Policy</a></li>
          <li><a href="{SITE_BASE_URL}/#terms">Terms of Service</a></li>
          <li><a href="#disclaimer">Legal Disclosure</a></li>
        </ul>
      </div>
      <div class="footer-section">
        <h4>Contact</h4>
        <p style="font-size:14px;margin-bottom:8px">📞 <a href="{tel}">{phone}</a></p>
        <p style="font-size:14px">🌐 ny.flyuss.com</p>
      </div>
    </div>
    <div class="footer-bottom">
      <p>&copy; {year} FlyUSS. All rights reserved. | Independent travel service provider</p>
    </div>
  </div>
</footer>

<a href="{tel}" class="fab" aria-label="Call us now">📞</a>

<script>
function toggleMenu(){{
  const m=document.getElementById('menu');
  const h=document.getElementById('ham');
  m.classList.toggle('show');
  h.classList.toggle('active');
}}
document.addEventListener('click',e=>{{
  const m=document.getElementById('menu');
  const h=document.getElementById('ham');
  if(window.innerWidth<=768&&!m.contains(e.target)&&!h.contains(e.target)){{
    m.classList.remove('show');
  }}
}});
</script>

</body>
</html>'''

# ══════════════════════════════════════════
# PUBLISH TO GITHUB PAGES
# ══════════════════════════════════════════
def publish_github(pages):
    if not GITHUB_TOKEN or not GITHUB_PAGES_REPO:
        print('[GitHub] No credentials — skipping')
        return 0
    import base64
    headers = {
        'Authorization': f'token {GITHUB_TOKEN}',
        'Accept': 'application/vnd.github.v3+json',
        'X-GitHub-Api-Version': '2022-11-28'
    }
    success = 0
    published_slugs = []
    for page in pages:
        try:
            content = base64.b64encode(page['html'].encode()).decode()
            path = f"pages/{page['slug']}"
            url = f'https://api.github.com/repos/{GITHUB_PAGES_REPO}/contents/{path}'
            r_get = requests.get(url, headers=headers, timeout=15)
            payload = {
                'message': f'{"🇺🇸 " if page.get("national") else "🚨 "}{page["title"][:50]}',
                'content': content
            }
            if r_get.status_code == 200:
                payload['sha'] = r_get.json()['sha']
            r = requests.put(url, json=payload, headers=headers, timeout=30)
            if r.status_code in [200, 201]:
                success += 1
                published_slugs.append(page['slug'])
                print(f'  [GitHub] ✅ {page["slug"][:50]}')
            else:
                print(f'  [GitHub] ❌ {r.status_code}')
            time.sleep(0.3)
        except Exception as e:
            print(f'  [GitHub] Error: {e}')

    # Generate and upload sitemap.xml
    # NOTE: base_url uses SITE_BASE_URL (the real flyuss.com domain), not
    # username.github.io/repo — this repo is served at the apex domain via
    # a custom domain CNAME.
    if published_slugs:
        try:
            base_url = f'{SITE_BASE_URL}/pages'
            list_url = f'https://api.github.com/repos/{GITHUB_PAGES_REPO}/contents/pages'
            r_list = requests.get(list_url, headers=headers, timeout=15)
            all_slugs = []
            if r_list.status_code == 200:
                all_slugs = [f['name'] for f in r_list.json() if f['name'].endswith('.html')]
            today = datetime.now().strftime('%Y-%m-%d')
            urls_xml = '\n'.join([
                '  <url><loc>' + base_url + '/' + slug + '</loc><lastmod>' + today + '</lastmod><changefreq>daily</changefreq><priority>0.8</priority></url>'
                for slug in all_slugs
            ])
            sitemap = '<?xml version="1.0" encoding="UTF-8"?>\n<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">\n' + urls_xml + '\n</urlset>'
            sitemap_b64 = base64.b64encode(sitemap.encode()).decode()
            sitemap_url = f'https://api.github.com/repos/{GITHUB_PAGES_REPO}/contents/sitemap.xml'
            r_get2 = requests.get(sitemap_url, headers=headers, timeout=15)
            sitemap_payload = {'message': 'Update sitemap', 'content': sitemap_b64}
            if r_get2.status_code == 200:
                sitemap_payload['sha'] = r_get2.json()['sha']
            r_s = requests.put(sitemap_url, json=sitemap_payload, headers=headers, timeout=30)
            if r_s.status_code in [200, 201]:
                print(f'  [GitHub] sitemap.xml updated ({len(all_slugs)} URLs)')
            else:
                print(f'  [GitHub] sitemap error: {r_s.status_code}')
        except Exception as e:
            print(f'  [GitHub] Sitemap error: {e}')

    # NOTE: unlike the crisis agent (which auto-generates a repo-root index.html
    # listing page on a throwaway parasite repo), this repo's root index.html
    # IS the real flyuss.com homepage. We must NEVER auto-generate/overwrite it
    # here — doing so would destroy the real homepage on every run. New pages
    # are discoverable via sitemap.xml and internal links instead.

    return success

# ══════════════════════════════════════════
# BING INDEXNOW
# ══════════════════════════════════════════
def ensure_indexnow_key_file():
    """
    IndexNow requires a public {key}.txt file to verify domain ownership,
    otherwise every submission gets 403 Forbidden. This repo is served at
    the flyuss.com custom domain from the repo root, so the key file must
    live at the repo root (same place as index.html / CNAME).
    This creates/updates that file automatically on every run so it's
    never missing after a fresh clone or repo change.
    """
    if not BING_INDEXNOW_KEY or not GITHUB_PAGES_REPO or not GITHUB_TOKEN:
        return None
    import base64
    headers = {
        'Authorization': f'token {GITHUB_TOKEN}',
        'Accept': 'application/vnd.github.v3+json',
        'X-GitHub-Api-Version': '2022-11-28'
    }
    try:
        key_filename = f'{BING_INDEXNOW_KEY}.txt'
        content_b64 = base64.b64encode(BING_INDEXNOW_KEY.encode()).decode()
        url = f'https://api.github.com/repos/{GITHUB_PAGES_REPO}/contents/{key_filename}'
        r_get = requests.get(url, headers=headers, timeout=15)
        payload = {'message': 'Update IndexNow verification key', 'content': content_b64}
        if r_get.status_code == 200:
            existing_b64 = r_get.json().get('content', '').replace('\n', '')
            if existing_b64 == content_b64:
                return key_filename  # already correct, no need to commit again
            payload['sha'] = r_get.json()['sha']
        r = requests.put(url, json=payload, headers=headers, timeout=30)
        if r.status_code in (200, 201):
            print(f'  [IndexNow] ✅ key file {key_filename} verified in repo root')
            return key_filename
        else:
            print(f'  [IndexNow] ❌ failed to write key file: {r.status_code} {r.text[:100]}')
            return None
    except Exception as e:
        print(f'  [IndexNow] key file error: {e}')
        return None


def ping_bing(pages):
    if not BING_INDEXNOW_KEY or not GITHUB_PAGES_REPO:
        return
    # This site is served at the custom domain (flyuss.com), not
    # username.github.io/repo — IndexNow's "host" must match the domain the
    # URLs are actually served from, and keyLocation must point there too.
    host = SITE_BASE_URL.split('://', 1)[-1].rstrip('/')
    urls = [f'{SITE_BASE_URL}/pages/{p["slug"]}' for p in pages[:10]]
    key_location = f'{SITE_BASE_URL}/{BING_INDEXNOW_KEY}.txt'
    try:
        r = requests.post(
            'https://api.indexnow.org/indexnow',
            json={'host': host, 'key': BING_INDEXNOW_KEY, 'keyLocation': key_location, 'urlList': urls},
            headers={'Content-Type': 'application/json'},
            timeout=30
        )
        print(f'[Bing IndexNow] {len(urls)} URLs submitted — {r.status_code}')
        if r.status_code == 403:
            print(f'  [Bing IndexNow] 403 — verify key file is live at: {key_location}')
    except Exception as e:
        print(f'[Bing] Error: {e}')


# ══════════════════════════════════════════
# GOOGLE INDEXING API — JWT/RS256
# ══════════════════════════════════════════
def ping_google(pages):
    """
    Submit URLs to Google Indexing API using service account JWT (RS256).
    Requires GOOGLE_SERVICE_ACCOUNT_KEY secret (JSON key file content).
    """
    if not GOOGLE_SERVICE_ACCOUNT_KEY:
        print('[Google Index] No GOOGLE_SERVICE_ACCOUNT_KEY — skipping')
        return 0

    try:
        import json as _json
        import base64 as _base64
        import hashlib as _hashlib
        import hmac as _hmac
        import struct as _struct

        sa = _json.loads(GOOGLE_SERVICE_ACCOUNT_KEY)
        private_key_pem = sa['private_key']
        client_email    = sa['client_email']

        # ── Build JWT ──
        now = int(time.time())
        header  = _base64.urlsafe_b64encode(_json.dumps({'alg':'RS256','typ':'JWT'}).encode()).rstrip(b'=').decode()
        payload = _base64.urlsafe_b64encode(_json.dumps({
            'iss': client_email,
            'sub': client_email,
            'aud': 'https://accounts.google.com/o/oauth2/token',
            'scope': 'https://www.googleapis.com/auth/indexing',
            'iat': now,
            'exp': now + 3600,
        }).encode()).rstrip(b'=').decode()
        signing_input = f'{header}.{payload}'.encode()

        # ── Sign with RSA-SHA256 via cryptography library ──
        try:
            from cryptography.hazmat.primitives import hashes, serialization
            from cryptography.hazmat.primitives.asymmetric import padding
            from cryptography.hazmat.backends import default_backend
            private_key = serialization.load_pem_private_key(
                private_key_pem.encode(), password=None, backend=default_backend())
            signature = private_key.sign(signing_input, padding.PKCS1v15(), hashes.SHA256())
            sig_b64 = _base64.urlsafe_b64encode(signature).rstrip(b'=').decode()
        except ImportError:
            print('[Google Index] cryptography library not available — skipping')
            return 0

        jwt_token = f'{header}.{payload}.{sig_b64}'

        # ── Exchange JWT for access token ──
        token_resp = requests.post(
            'https://accounts.google.com/o/oauth2/token',
            data={'grant_type': 'urn:ietf:params:oauth:grant-type:jwt-bearer', 'assertion': jwt_token},
            timeout=15)
        if token_resp.status_code != 200:
            print(f'[Google Index] Token error: {token_resp.status_code} {token_resp.text[:100]}')
            return 0

        access_token = token_resp.json().get('access_token', '')
        if not access_token:
            print('[Google Index] No access token received')
            return 0

        # ── Submit URLs ──
        headers = {
            'Authorization': f'Bearer {access_token}',
            'Content-Type': 'application/json',
        }
        success = 0
        urls = [f'{SITE_BASE_URL}/pages/{p["slug"]}' for p in pages[:5]]
        for url in urls:
            try:
                r = requests.post(
                    'https://indexing.googleapis.com/v3/urlNotifications:publish',
                    json={'url': url, 'type': 'URL_UPDATED'},
                    headers=headers, timeout=15)
                if r.status_code == 200:
                    success += 1
                    print(f'  [Google Index] ✅ {url[-60:]}')
                else:
                    print(f'  [Google Index] ❌ {r.status_code} {r.text[:80]}')
                time.sleep(0.5)
            except Exception as e:
                print(f'  [Google Index] Error: {e}')

        print(f'[Google Index] {success}/{len(urls)} URLs submitted')
        return success

    except Exception as e:
        print(f'[Google Index] Fatal error: {e}')
        return 0


# ══════════════════════════════════════════
# MAIN
# ══════════════════════════════════════════
def main():
    if not GEMINI_API_KEY and not GROQ_API_KEY:
        print('ERROR: Set at least GROQ_API_KEY or GEMINI_API_KEY!')
        exit(1)

    active_llm = 'Groq (primary)' if GROQ_API_KEY else f'Gemini {MODEL} ({len(_GEMINI_KEYS)} key(s))'
    print(f'\n{"="*60}')
    print('FlyUSS NY SEO Agent v1')
    print('New York landing pages — ny.flyuss.com (JFK / LGA / EWR)')
    print(f'Repo: {GITHUB_PAGES_REPO} | Site: {SITE_BASE_URL}')
    print(f'LLM: {active_llm}')
    print(f'Time: {datetime.now().strftime("%Y-%m-%d %H:%M UTC")}')
    print(f'{"="*60}\n')

    published, daily, platform_map, daily_blogger = load_slugs()
    today_count = get_today_count(daily)
    print(f'Published slugs in memory: {len(published)}')
    print(f'Articles published today: {today_count}/{DAILY_LIMIT}\n')

    if today_count >= DAILY_LIMIT:
        print(f'✅ Daily limit reached ({DAILY_LIMIT} articles). Stopping.')
        return

    remaining = DAILY_LIMIT - today_count
    print(f'Remaining today: {remaining} articles\n')

    # ── STEP 1: Load or build today's 50-item quota queue ──
    # The 50/day queue (5 categories × 10) is built ONCE per day on the first
    # run, then persisted to QUEUE_FILE so later runs the same day just pull
    # the next unprocessed slice instead of re-fetching trends every 30 min.
    print('[STEP 1] Loading today\'s quota queue...')
    stored_queue, today_str = load_daily_queue()

    if stored_queue:
        print(f'[STEP 1] Resuming existing queue — {len(stored_queue)} items left from today')
        queue = stored_queue
    else:
        print('[STEP 1] No queue for today yet — building fresh 50-item quota queue')
        queue = get_daily_quota_keywords()
        if not queue:
            print('[STEP 1] Quota build failed — using fallback keywords')
            queue = get_fallback_keywords(MAX_PER_RUN)
        save_daily_queue(queue, today_str)

    print(f'\n[STEP 1] Total queue available: {len(queue)} items')

    # ── STEP 2: Intelligence analysis ──
    print('\n[STEP 2] Running intelligence analysis...')

    # Detect airport list
    detected_airports = [item['airport']['c'] for item in queue]

    # National Impact
    is_national, nat_airports = check_national_impact(detected_airports)
    if is_national:
        print(f'  🇺🇸 NATIONAL IMPACT: {"+".join(nat_airports)} — Priority MAX')
        for item in queue:
            if item['airport']['c'] in nat_airports:
                item['is_national'] = True

    # Category 4 (national SEO) items are always framed as national,
    # regardless of airport-cluster detection — this is deliberate, not
    # the old random "every 12th item" heuristic.
    for item in queue:
        if item.get('force_national'):
            item['is_national'] = True

    # Cluster detection
    active_clusters = check_clusters(detected_airports)
    if active_clusters:
        for cl in active_clusters:
            print(f'  🔗 Active cluster: {cl["name"]} — Score:{cl["score"]} — Affected:{cl["affected"]}')

    # Multi-state detection
    is_multi, affected_states, is_nat_scale = check_multi_state(detected_airports)
    if is_multi:
        print(f'  🗺 Multi-state event: {" + ".join(affected_states)}')
        if is_nat_scale:
            print('  🇺🇸 NATIONAL SCALE — 4+ states affected!')

    # Velocity sort
    queue.sort(key=lambda x: x['velocity']['score'], reverse=True)

    # Call Intent Filter — remove keywords with no call intent
    before = len(queue)
    queue = [i for i in queue if has_call_intent(i['kw'])]
    removed = before - len(queue)
    if removed > 0:
        print(f'  📞 Call Intent Filter: removed {removed} low-intent keywords')

    # SAFETY NET after call intent filter
    if not queue:
        print('[SAFETY] Queue empty after Call Intent Filter — using fallback keywords')
        queue = get_fallback_keywords(MAX_PER_RUN)

    # Lifetime display
    print('\n  Event lifetimes:')
    for item in queue[:5]:
        lt = predict_lifetime(item['event'])
        print(f'    {item["event"]["n"]}: {lt["hours"]}h — {lt["urgency"]}')

    # Airline Heat Index
    print('\n  Airline Heat Index:')
    for al_key, al in AIRLINES.items():
        al_items = [i for i in queue if i['airline'] == al_key]
        if al_items:
            vel = al_items[0]['velocity']
            heat = calc_heat_index(al_key, len(al_items)*80, 60, 50, vel)
            print(f'    {al["name"]}: Heat={heat} | Velocity={vel["label"]}')

    # Priority: national first, then golden gaps, then velocity
    def priority_sort(item):
        score = item['velocity']['score']
        if item.get('is_national'):       score += 50
        if item.get('is_golden'):         score += 30
        if not item.get('competitor_fresh', True): score += 40  # no competitor = highest priority
        return -score

    queue.sort(key=priority_sort)

    # SAFETY NET: if queue still empty after all filters, use fallback
    if not queue:
        print('[SAFETY] Queue empty after filtering — using fallback keywords')
        queue = get_fallback_keywords(MAX_PER_RUN)

    # Only take this run's slice (MAX_PER_RUN); whatever's left stays queued
    # in QUEUE_FILE for later runs today to pick up. Also respect the
    # overall daily remaining budget in case today_count is already partway.
    run_slice = queue[:MAX_PER_RUN][:remaining]
    leftover_after_run = queue[len(run_slice):]

    # News Gap Finder (for this run's items only)
    print('\n  Running News Gap Finder...')
    run_slice = find_news_gaps(run_slice)
    golden = [i for i in run_slice if i.get('is_golden')]
    print(f'  🥇 Golden opportunities: {len(golden)}')

    print(f'\nThis run: {len(run_slice)} articles | Remaining in today\'s queue after this run: {len(leftover_after_run)}')
    for i, item in enumerate(run_slice):
        tags = []
        if item.get('is_national'): tags.append('🇺🇸 NATIONAL')
        if item.get('is_golden'):   tags.append('🥇 GOLDEN')
        tags.append(item['velocity']['label'])
        print(f'  {i+1}. {" | ".join(tags)} | {item["airport"]["c"]} | {item["kw"][:45]}')

    # ── STEP 3: Generate articles ──
    print(f'\n[STEP 3] Generating {len(run_slice)} articles...')
    generated = []
    errors = 0

    for i, item in enumerate(run_slice):
        nat = item.get('is_national', False)
        print(f'\n[{i+1}/{len(run_slice)}] {item["source"].upper()} | {item["event"]["n"]} | {item["airport"]["c"]}')
        try:
            page = generate_article(item, published, is_national=nat, active_clusters=active_clusters, platform_map=platform_map)
            if page:
                generated.append(page)
                published.add(make_slug(item['kw']))
                print(f'  ✅ {page["words"]}w | ${AIRLINES[page["airline"]]["rpm"]}/call')
        except Exception as e:
            errors += 1
            print(f'  ❌ Error: {e}')
        if i < len(run_slice) - 1:
            time.sleep(5)  # 5s delay between articles

    # Persist whatever's left in the queue (items not attempted this run)
    # so the next run today picks up right where this one left off.
    save_daily_queue(leftover_after_run, today_str)

    # ── STEP 4: Publish ──
    if generated:
        print(f'\n[STEP 4] Publishing {len(generated)} articles...')

        # All pages → GitHub Pages (this repo IS flyuss.com)
        gh_pages = generated

        gh_ok = publish_github(gh_pages)
        ensure_indexnow_key_file()
        ping_bing(gh_pages)
        ping_google(gh_pages)

        # Track which platform each slug was published to (for internal links next run)
        for p in gh_pages:
            platform_map[p['slug'].replace('.html','')] = 'github'

        daily = update_today_count(daily, today_count + len(generated))
        save_slugs(published, daily, platform_map=platform_map, daily_blogger=daily_blogger)

        # Summary — broken down by the 5 quota categories instead of the
        # old trend_intelligence/predictive_ai labels (not used by this agent).
        national_count = sum(1 for p in generated if p.get('national'))
        by_source = {}
        for p in generated:
            by_source[p.get('source', '?')] = by_source.get(p.get('source', '?'), 0) + 1

        print(f'\n{"="*60}')
        print('SUMMARY:')
        print(f'  Generated:           {len(generated)} articles')
        print(f'  National framing:    {national_count} articles')
        print(f'  Golden Gaps:         {len(golden)} found')
        for src, cnt in sorted(by_source.items()):
            print(f'  {src:<24} {cnt} articles')
        print(f'  Errors:              {errors}')
        print(f'  GitHub:              {gh_ok} published')
        print(f'  Total slugs:         {len(published)}')
        print(f'  Queue left today:    {len(leftover_after_run)}')
        print(f'  Est. cost:           ~${len(generated) * 0.018:.2f}')
        print(f'{"="*60}\n')
    else:
        print('\nNo articles generated this run.')

if __name__ == '__main__':
    main()
