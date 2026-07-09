#!/usr/bin/env python3
"""
Quant Radar — kod tabanlı günlük tarayıcı.

PROTOCOL.md'deki kaynak taraması + RADAR skorlama mantığını deterministik
Python koduyla uygular (LLM ajanı yerine gerçek API çağrıları). GitHub
Actions runner'ında çalışır (serbest ağ erişimi); çıktı: digests/radar-YYYY-MM-DD.md

Kullanım:
    python3 scripts/fetch_radar.py            # dünü (UTC) tarar
    python3 scripts/fetch_radar.py 2026-07-08  # belirli bir günü tarar (test/backfill)

Ortam değişkenleri:
    GITHUB_TOKEN / GH_TOKEN  — GitHub Search API rate limitini yükseltmek için (opsiyonel ama önerilir)
"""
import os
import re
import sys
import json
import time
import datetime
import urllib.request
import urllib.parse
import urllib.error
import xml.etree.ElementTree as ET

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
ROOT_DIR = os.path.dirname(SCRIPT_DIR)
DIGESTS_DIR = os.path.join(ROOT_DIR, "digests")

GITHUB_TOKEN = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")
UA = {"User-Agent": "quant-radar-bot/1.0 (+https://github.com/ERDEMRD/quant-radar)"}

ARXIV_CATEGORIES = ["q-fin.TR", "q-fin.PM", "q-fin.ST", "q-fin.CP", "q-fin.RM", "q-fin.MF"]

# GitHub taraması artık paper eşleştirmesiyle sınırlı değil — kendi başına geniş bir sorgu seti.
QUANT_REPO_QUERIES = [
    "quant trading", "trading strategy", "backtest", "alpha factor",
    "market making OR orderbook", "statistical arbitrage", "mean reversion strategy",
    "pairs trading", "options pricing model", "portfolio optimization",
    "factor investing", "reinforcement learning trading", "crypto trading bot",
    "high frequency trading", "quantitative research framework",
]

STOPWORDS = {
    "the", "a", "an", "of", "for", "and", "in", "on", "with", "to", "using",
    "via", "based", "approach", "study", "toward", "towards", "from", "into",
    "under", "over", "new", "novel", "analysis", "model", "models",
}

# "Büyük yerler" — akademik + kurumsal quant sinyali. Bulunursa öncelik puanı verilir.
INSTITUTION_MAP = {
    "mit": "MIT", "massachusetts institute of technology": "MIT",
    "stanford": "Stanford", "harvard": "Harvard", "princeton": "Princeton",
    "columbia university": "Columbia", "berkeley": "UC Berkeley",
    "caltech": "Caltech", "california institute of technology": "Caltech",
    "university of chicago": "U Chicago", "yale": "Yale", "cornell": "Cornell",
    "imperial college": "Imperial College London", "oxford": "Oxford",
    "cambridge": "Cambridge", "eth zurich": "ETH Zurich", "carnegie mellon": "CMU",
    "google deepmind": "Google DeepMind", "deepmind": "DeepMind", "openai": "OpenAI",
    "anthropic": "Anthropic", "meta ai": "Meta AI", "microsoft research": "Microsoft Research",
    "citadel": "Citadel", "two sigma": "Two Sigma", "renaissance technologies": "Renaissance Technologies",
    "d. e. shaw": "D.E. Shaw", "de shaw": "D.E. Shaw", "jane street": "Jane Street",
    "aqr capital": "AQR Capital", "man group": "Man Group", "millennium management": "Millennium",
    "point72": "Point72", "bridgewater": "Bridgewater", "goldman sachs": "Goldman Sachs",
    "morgan stanley": "Morgan Stanley", "jpmorgan": "JPMorgan", "j.p. morgan": "JPMorgan",
    "susquehanna": "Susquehanna (SIG)", "optiver": "Optiver", "imc trading": "IMC Trading",
    "hudson river trading": "Hudson River Trading", "akuna capital": "Akuna Capital",
    "winton": "Winton", "balyasny": "Balyasny",
}

# Somut performans iddiası (Sharpe/PnL/getiri) sinyali — sıralamada öne çıkarmak için.
PNL_PATTERNS = [
    r"sharpe\s*ratio", r"\bsharpe\b", r"\bpnl\b", r"\bp&l\b", r"\bprofit\b",
    r"cumulative return", r"annualized return", r"backtested? return",
    r"\d+(\.\d+)?\s*%\s*(return|profit|gain|drawdown)",
]

# --------------------------------------------------------------------------
# Konu sınıflandırması — kural-tabanlı (LLM yok). Sıra önemli: eşit isabette
# listede önce gelen kazanır. Her paper başlık+abstract üzerinden eşleştirilir.
# --------------------------------------------------------------------------
TOPIC_RULES = [
    ("Opsiyon & Türev Fiyatlama", "🧮", [
        "option", "derivative", "implied volatilit", "black-scholes", "black scholes",
        "variance swap", "risk-neutral", "pricing kernel", "greeks", "exotic", "vix",
        "delta hedg", "volatility surface", "skew", "term structure"]),
    ("Piyasa Mikroyapısı & LOB", "📊", [
        "limit order", "order book", "orderbook", "market making", "market-making",
        "microstructure", "price impact", "bid-ask", "order flow", "liquidity provision",
        "tick data", "quote", "price discovery"]),
    ("Execution & İşlem Maliyeti", "⚙️", [
        "optimal execution", "transaction cost", "slippage", "twap", "vwap",
        "implementation shortfall", "trade scheduling", "order splitting"]),
    ("İstatistiksel Arbitraj & Pairs", "🔁", [
        "statistical arbitrage", "stat-arb", "pairs trading", "cointegration",
        "mean reversion", "mean-reversion", "spread trading", "relative value"]),
    ("Portföy & Varlık Dağılımı", "📦", [
        "portfolio", "asset allocation", "markowitz", "mean-variance", "risk parity",
        "rebalanc", "efficient frontier", "diversif", "asset pricing"]),
    ("Faktör & Kesitsel Anomali", "🧲", [
        "factor", "cross-section", "anomal", "momentum", "value premium", "size effect",
        "carry", "risk premium", "alpha mining"]),
    ("Volatilite & Risk Yönetimi", "🌊", [
        "volatility forecast", "garch", "value-at-risk", "value at risk", "expected shortfall",
        "tail risk", "realized volatility", "risk management", "extreme value", "stress test",
        "drawdown control", "systemic risk"]),
    ("RL & Trading Ajanları", "🤖", [
        "reinforcement learning", "agent-based", "multi-agent", "q-learning",
        "policy gradient", "trading agent", "deep rl", "self-play"]),
    ("ML & Fiyat Tahmini", "🧠", [
        "machine learning", "deep learning", "neural network", "transformer", "lstm",
        "random forest", "gradient boosting", "xgboost", "prediction", "forecasting",
        "llm", "language model", "sentiment", "graph neural"]),
    ("Kripto & DeFi", "🪙", [
        "crypto", "bitcoin", "ethereum", "defi", "blockchain", "stablecoin",
        "decentralized", "amm", "perpetual"]),
    ("HFT & Düşük Gecikme", "⚡", [
        "high-frequency", "high frequency", "low latency", "ultra-fast", "nanosecond"]),
]
TOPIC_OTHER = ("Genel / Diğer", "📄")


def classify_topics(text, max_topics=2):
    """Başlık+abstract'tan konu çıkar. En çok anahtar-kelime isabeti alan konu birincil;
    isabet alan diğer konular (en fazla max_topics) etiket olarak eklenir."""
    tl = (text or "").lower()
    scored = []
    for order, (name, emoji, kws) in enumerate(TOPIC_RULES):
        hits = sum(1 for k in kws if k in tl)
        if hits:
            scored.append((-hits, order, name, emoji))
    if not scored:
        return [TOPIC_OTHER]
    scored.sort()
    return [(name, emoji) for _, _, name, emoji in scored[:max_topics]]


# Somut sayısal iddia çıkarımı — "Sharpe 1.8", "%12 getiri" gibi kompakt rozetler üretir.
CLAIM_PATTERNS = [
    (r"sharpe(?:\s*ratios?)?(?:\s*of|\s*[:=])?\s*(?:up\s+to\s*)?(\d+(?:\.\d+)?)", "Sharpe {}"),
    (r"information\s+ratios?\s*(?:of|[:=])?\s*(\d+(?:\.\d+)?)", "IR {}"),
    (r"(?:annual(?:ized)?|cumulative|excess|total)\s+returns?\s*(?:of|[:=])?\s*(\d+(?:\.\d+)?)\s*%", "%{} getiri"),
    (r"returns?\s+of\s+(\d+(?:\.\d+)?)\s*%", "%{} getiri"),
    (r"(\d+(?:\.\d+)?)\s*%\s*(?:annual(?:ized)?|cumulative|excess|total)?\s*returns?", "%{} getiri"),
    (r"(\d+(?:\.\d+)?)\s*%\s*(?:improvement|outperform\w*|gain)", "%{} iyileşme"),
    (r"outperform\w*\s*(?:\w+\s+){0,3}by\s+(\d+(?:\.\d+)?)\s*%", "%{} iyileşme"),
    (r"(?:max(?:imum)?\s*)?drawdowns?\s*(?:of|[:=])?\s*(\d+(?:\.\d+)?)\s*%", "maks. çekilme %{}"),
    (r"(\d+(?:\.\d+)?)\s*%\s*(?:max(?:imum)?\s*)?drawdown", "maks. çekilme %{}"),
    (r"accurac(?:y|ies)\s*(?:of|[:=])?\s*(\d+(?:\.\d+)?)\s*%", "doğruluk %{}"),
    (r"(\d+(?:\.\d+)?)\s*%\s*accuracy", "doğruluk %{}"),
    (r"hit\s*rates?\s*(?:of|[:=])?\s*(\d+(?:\.\d+)?)\s*%", "hit-rate %{}"),
    (r"alphas?\s+of\s+(\d+(?:\.\d+)?)\s*(%|bps)", "alfa {}{}"),
]


def extract_claims(text, max_claims=5):
    """Metinden somut performans sayılarını çek (abstract + varsa abs sayfası).
    Ayrıca test bağlamını (OOS / backtest) rozet olarak ekler. Tamamı iddiadır."""
    tl = clean_abstract(text).lower()
    chips, seen = [], set()
    for pattern, template in CLAIM_PATTERNS:
        for m in re.finditer(pattern, tl):
            chip = template.format(*m.groups())
            if chip not in seen:
                seen.add(chip)
                chips.append(chip)
            if len(chips) >= max_claims:
                break
        if len(chips) >= max_claims:
            break
    if any(k in tl for k in ["out-of-sample", "out of sample", "live trading", "paper trading", "walk-forward"]):
        chips.append("OOS/canlı test")
    elif "backtest" in tl or "historical simulation" in tl:
        chips.append("backtest (in-sample)")
    return chips


# --------------------------------------------------------------------------
# HTTP yardımcıları — Kafka tarzı: geçici hatada düşürme, tekrar dene; sadece
# gerçekten tükenince (retries bitince) çağıran tarafa boş/hata bırak, script çökmesin.
# --------------------------------------------------------------------------
def http_get(url, headers=None, timeout=20, retries=3, backoff=3):
    last_exc = None
    for attempt in range(1, retries + 1):
        try:
            req = urllib.request.Request(url, headers={**UA, **(headers or {})})
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return r.read().decode("utf-8", errors="ignore")
        except Exception as e:
            last_exc = e
            if attempt < retries:
                wait = backoff * attempt
                print(f"[http] {url[:80]}... deneme {attempt}/{retries} başarısız ({e}), {wait}sn sonra tekrar", file=sys.stderr)
                time.sleep(wait)
    raise last_exc


def http_get_json(url, headers=None, timeout=20, retries=3, backoff=3):
    raw = http_get(url, headers, timeout, retries, backoff)
    return json.loads(raw)


def gh_headers():
    h = {"Accept": "application/vnd.github+json"}
    if GITHUB_TOKEN:
        h["Authorization"] = f"Bearer {GITHUB_TOKEN}"
    return h


# --------------------------------------------------------------------------
# 1a/1b — arXiv taraması + paper-kod eşleştirmesi
# --------------------------------------------------------------------------
def base_arxiv_id(url_or_id):
    """'2607.00001v2' -> '2607.00001' — versiyon numarasından bağımsız kimlik (dedup için)."""
    m = re.search(r"(\d{4}\.\d{4,5})(v\d+)?", url_or_id or "")
    return m.group(1) if m else (url_or_id or "")


def _parse_arxiv_entries(root, ns, cat):
    out = []
    for entry in root.findall("a:entry", ns):
        published = entry.findtext("a:published", default="", namespaces=ns)
        updated = entry.findtext("a:updated", default="", namespaces=ns)
        try:
            pub_date = datetime.datetime.strptime(published[:10], "%Y-%m-%d").date()
            upd_date = datetime.datetime.strptime(updated[:10], "%Y-%m-%d").date()
        except ValueError:
            continue
        title = (entry.findtext("a:title", default="", namespaces=ns) or "").strip().replace("\n", " ")
        summary = (entry.findtext("a:summary", default="", namespaces=ns) or "").strip().replace("\n", " ")
        link = entry.findtext("a:id", default="", namespaces=ns)
        authors = [a.findtext("a:name", default="", namespaces=ns) for a in entry.findall("a:author", ns)]
        # Birincil kategoriyi entry'nin kendisinden al (aralıklı sorguda tek 'cat' parametresi yok).
        pc = entry.find("{http://arxiv.org/schemas/atom}primary_category")
        entry_cat = (pc.get("term") if pc is not None else None) or cat or "q-fin"
        out.append({
            "source": "arxiv", "category": entry_cat, "title": title, "summary": summary,
            "url": link, "authors": authors, "pub_date": pub_date, "upd_date": upd_date,
            "base_id": base_arxiv_id(link),
        })
    return out


def _fetch_arxiv_page(query, start, per_page, sort_field, parse_retries=3):
    """Tek bir arXiv API sayfasını çek + parse et. (entries, totalResults) döner.
    Hata durumunda ASLA exception fırlatmaz — ([], -1) döner.
    arXiv API'si bazen 200 OK ile bozuk/HTML hata sayfası ya da 'boş ama total>0' sayfa
    dönebiliyor; her iki durumda da isteği burada tekrar deniyoruz."""
    url = ("https://export.arxiv.org/api/query?search_query=" + urllib.parse.quote(query)
           + f"&start={start}&max_results={per_page}&sortBy={sort_field}&sortOrder=ascending")
    ns = {"a": "http://www.w3.org/2005/Atom", "os": "http://a9.com/-/spec/opensearch/1.1/"}
    for attempt in range(1, parse_retries + 1):
        try:
            raw = http_get(url, timeout=25)
            root = ET.fromstring(raw)
            total = int(root.findtext("os:totalResults", default="-1", namespaces=ns))
            entries = _parse_arxiv_entries(root, ns, cat="")
            if not entries and total > start:
                raise RuntimeError(f"arXiv boş sayfa döndürdü (total={total}, start={start})")
            return entries, total
        except Exception as e:
            print(f"[arXiv] sayfa start={start} deneme {attempt}/{parse_retries} başarısız: {e}",
                  file=sys.stderr, flush=True)
            if attempt < parse_retries:
                time.sleep(5 * attempt)
    return [], -1


def _fetch_arxiv_by_date(field, target_date, per_page=100, max_pages=10):
    """Tarih-ARALIKLI sorgu: tüm q-fin kategorilerinde, verilen alan (submittedDate /
    lastUpdatedDate) target_date'e düşen TÜM paper'ları sayfalayarak toplar.
    Eski yöntem (kategori feed'inin ilk 150 kaydını çekip gün filtrelemek) yoğun günlerde
    ve feed sıralaması kaydığında paper kaçırıyordu — aralık sorgusu kapsamayı garanti eder.
    max_pages sabit üst sınırdır: sonsuz döngü/asılı kalma imkânsız."""
    d0 = target_date.strftime("%Y%m%d") + "0000"
    d1 = target_date.strftime("%Y%m%d") + "2359"
    cat_q = " OR ".join(f"cat:{c}" for c in ARXIV_CATEGORIES)
    query = f"({cat_q}) AND {field}:[{d0} TO {d1}]"
    out, start = [], 0
    for _page in range(max_pages):
        entries, total = _fetch_arxiv_page(query, start, per_page, field)
        out.extend(entries)
        start += per_page
        if total < 0 or start >= total or not entries:
            break
        time.sleep(3)  # arXiv nezaket aralığı (resmi öneri: istekler arası >=3sn)
    return out


def fetch_all_arxiv(target_date):
    """Hem yeni gönderilen hem de dün revize edilen (v2/v3) paper'ları döner.
    Kaynak patlarsa boş döner, exception yükseltmez."""
    papers, seen_base_ids = [], set()
    for e in _fetch_arxiv_by_date("submittedDate", target_date):
        if e["pub_date"] != target_date or e["base_id"] in seen_base_ids:
            continue
        e["is_update"] = False
        e["date"] = str(e["pub_date"])
        seen_base_ids.add(e["base_id"])
        papers.append(e)
    time.sleep(3)
    for e in _fetch_arxiv_by_date("lastUpdatedDate", target_date):
        if e["upd_date"] != target_date or e["pub_date"] == target_date:
            continue  # yeni gönderilenler zaten yukarıda; sadece gerçek revizyonlar
        if e["base_id"] in seen_base_ids:
            continue
        e["is_update"] = True
        e["date"] = str(e["upd_date"])
        seen_base_ids.add(e["base_id"])
        papers.append(e)
    return papers


def github_search_repos(query, extra="", max_results=10):
    q = urllib.parse.quote(f"{query} {extra}".strip())
    url = f"https://api.github.com/search/repositories?q={q}&sort=stars&order=desc&per_page={max_results}"
    for attempt in (1, 2):
        try:
            data = http_get_json(url, gh_headers(), retries=1)
            return data.get("items", [])
        except urllib.error.HTTPError as e:
            if e.code in (403, 429) and attempt == 1:
                # Search API rate limit — sabit bir kez bekle ve tekrar dene, sonra pes et.
                print(f"[GitHub] rate limit '{query}', 65sn bekleniyor...", file=sys.stderr, flush=True)
                time.sleep(65)
                continue
            print(f"[GitHub] arama hatasi '{query}': {e}", file=sys.stderr)
            return []
        except Exception as e:
            print(f"[GitHub] arama hatasi '{query}': {e}", file=sys.stderr)
            return []
    return []


def detect_institutions(text):
    """Metinde (özet, yazar listesi, tam metin) bilinen kurum/fon/lab adı ara."""
    tl = (text or "").lower()
    found = set()
    for kw, name in INSTITUTION_MAP.items():
        if re.search(r"\b" + re.escape(kw) + r"\b", tl):
            found.add(name)
    return sorted(found)


def has_pnl_claim(text):
    tl = (text or "").lower()
    return any(re.search(p, tl) for p in PNL_PATTERNS)


def fetch_arxiv_fulltext(paper_url):
    """Kurum/affiliation tespiti için paper'ın abs sayfasına bak.
    NOT: Eskiden önce tam HTML render (arxiv.org/html/...) indiriliyordu — sayfa başına
    megabaytlarca veri + retry çarpanı, taramanın 'asılı kalmış' gibi görünmesinin ana
    nedeniydi. abs sayfası küçüktür ve kurum sinyali için yeterlidir; tek deneme, kısa timeout."""
    m = re.search(r"arxiv\.org/abs/([\w.]+)", paper_url or "")
    if not m:
        return ""
    try:
        return http_get(f"https://arxiv.org/abs/{m.group(1)}", timeout=12, retries=1)
    except Exception:
        return ""


def fetch_repo_readme(full_name):
    """Repo README'sini ham metin olarak çek — paper referansı / PnL iddiası / backtest sinyali için."""
    url = f"https://api.github.com/repos/{full_name}/readme"
    headers = {**gh_headers(), "Accept": "application/vnd.github.raw"}
    try:
        return http_get(url, headers, timeout=15)
    except Exception:
        return ""


def find_code_for_paper(paper):
    """Abstract içinde GitHub linki ara (resmi kod); yoksa başlık anahtar kelimeleriyle 3.taraf ara.

    3.taraf eşleşme artık DOĞRULAMA şartına bağlı: eskiden aramanın ilk sonucu körlemesine
    kabul ediliyordu — alakasız popüler repolar 'Paper+3.Taraf Kod' diye +25 skor alıyordu.
    Şimdi aday repo ancak şunlardan biri sağlanırsa kabul edilir:
      a) repo adı+açıklamasında başlığın anlamlı kelimelerinin >=%60'ı (ve >=3'ü) geçiyorsa, veya
      b) README'sinde paper'ın arXiv id'si ya da tam başlığı geçiyorsa."""
    text = paper["summary"] + " " + paper["title"]
    m = re.search(r"https?://github\.com/[\w\-.]+/[\w\-.]+", text)
    if m:
        return {"url": m.group(0).rstrip(".,)"), "official": True, "stars": None}

    words = [w.lower() for w in re.findall(r"[A-Za-z][A-Za-z\-]+", paper["title"])
             if w.lower() not in STOPWORDS and len(w) > 3]
    if len(words) < 3:
        return None  # başlık çok kısa/genel — güvenilir eşleşme imkânsız, arama yapma
    items = github_search_repos(" ".join(words[:5]), max_results=3)
    title_lower = re.sub(r"\s+", " ", paper["title"].lower()).strip()
    arxiv_id = paper.get("base_id") or ""
    for cand in items:
        hay = f"{cand.get('full_name', '')} {cand.get('description') or ''}".lower()
        overlap = sum(1 for w in words if w in hay)
        if overlap >= 3 and overlap >= 0.6 * len(words):
            return {"url": cand["html_url"], "official": False, "stars": cand.get("stargazers_count")}
    # Ad/açıklama yetmedi — yalnızca İLK aday için README kontrolü (API bütçesi).
    if items:
        cand = items[0]
        readme = fetch_repo_readme(cand["full_name"]).lower()
        if readme and (arxiv_id and arxiv_id in readme or title_lower in re.sub(r"\s+", " ", readme)):
            return {"url": cand["html_url"], "official": False, "stars": cand.get("stargazers_count")}
    return None


# --------------------------------------------------------------------------
# 1c — Yeni / hareketlenen GitHub repoları
# --------------------------------------------------------------------------
def fetch_new_repos(target_date):
    repos, seen = [], set()
    date_str = str(target_date)
    for q in QUANT_REPO_QUERIES:
        for it in github_search_repos(q, extra=f"created:{date_str}", max_results=10):
            if it["html_url"] in seen:
                continue
            seen.add(it["html_url"])
            repos.append(it)
        time.sleep(2.2)  # Search API limiti 30 istek/dk — 1sn'lik aralık limit aşımına yol açıyordu
    for q in QUANT_REPO_QUERIES:
        for it in github_search_repos(q, extra=f"pushed:{date_str} stars:>50", max_results=5):
            if it["html_url"] in seen:
                continue
            seen.add(it["html_url"])
            it["_hareketli"] = True
            repos.append(it)
        time.sleep(2.2)  # Search API limiti 30 istek/dk — 1sn'lik aralık limit aşımına yol açıyordu
    return repos


# --------------------------------------------------------------------------
# 1e — Topluluk sinyalleri
# --------------------------------------------------------------------------
def fetch_hn(target_date):
    epoch = int(datetime.datetime.combine(target_date, datetime.time.min).timestamp())
    url = f"https://hn.algolia.com/api/v1/search_by_date?query=trading&tags=story&numericFilters=created_at_i%3E{epoch}"
    try:
        data = http_get_json(url)
        return [{"title": h["title"], "url": h.get("url") or f"https://news.ycombinator.com/item?id={h['objectID']}",
                  "points": h.get("points", 0)} for h in data.get("hits", [])]
    except Exception as e:
        print(f"[HN] hata: {e}", file=sys.stderr)
        return []


def fetch_reddit(sub, target_date):
    url = f"https://www.reddit.com/r/{sub}/new.json?limit=25"
    try:
        data = http_get_json(url, headers={"User-Agent": "quant-radar-bot/1.0"})
    except Exception as e:
        print(f"[Reddit] r/{sub} hata: {e}", file=sys.stderr)
        return []
    out = []
    for c in data.get("data", {}).get("children", []):
        d = c["data"]
        created = datetime.datetime.utcfromtimestamp(d["created_utc"])
        if created.date() != target_date:
            continue
        out.append({"title": d["title"], "url": f"https://reddit.com{d['permalink']}", "score": d.get("score", 0)})
    return out


def fetch_quantocracy():
    try:
        html = http_get("https://quantocracy.com/")
    except Exception as e:
        print(f"[Quantocracy] hata: {e}", file=sys.stderr)
        return []
    items = re.findall(r'<a[^>]+class="[^"]*qc-link[^"]*"[^>]+href="([^"]+)"[^>]*>([^<]+)</a>', html)
    if not items:
        items = re.findall(r'<h2 class="entry-title"><a href="([^"]+)"[^>]*>([^<]+)</a></h2>', html)
    return [{"title": t.strip(), "url": u} for u, t in items[:20]]


# --------------------------------------------------------------------------
# 2 — RADAR skorlama (PROTOCOL.md tablosu)
# --------------------------------------------------------------------------
def score_paper(paper, code_info, affiliations=None, extra_text=""):
    score = 0
    if code_info:
        score += 35 if code_info.get("official") else 25
    else:
        score += 15
    text = ((paper["summary"] or "") + " " + (extra_text or "")).lower()
    if any(k in text for k in ["out-of-sample", "live trading", "paper trading", "forward test", "walk-forward"]):
        score += 20
    elif any(k in text for k in ["backtest", "in-sample", "historical simulation"]):
        score += 10
    if any(k in text for k in ["walk-forward", "transaction cost", "multiple testing", "cross-validation", "robustness"]):
        score += 10
    if any(k in text for k in ["propose", "introduce a new", "novel", "we present"]):
        score += 15
    if code_info and code_info.get("stars"):
        score += min(10, code_info["stars"] // 20)
    if affiliations:
        score += 8  # MIT/Stanford/Citadel/Two Sigma vb. büyük kurum sinyali — öncelik
    if has_pnl_claim(text):
        score += 8  # somut Sharpe/PnL/getiri iddiası — sıralamada öne çıkar
    return min(score, 100)


def score_repo_only(repo, readme_text=""):
    score = 10
    stars = repo.get("stargazers_count", 0)
    if stars > 50:
        score += 10
    if repo.get("_hareketli"):
        score += 5
    rl = (readme_text or "").lower()
    has_paper_ref = bool(re.search(r"arxiv\.org|doi\.org|\bpaper\b", rl))
    has_pnl = has_pnl_claim(rl)
    has_backtest = any(k in rl for k in ["backtest", "walk-forward", "out-of-sample"])
    if has_paper_ref:
        score += 25  # README bir paper'a referans veriyor — repo+paper kombinasyonuna yaklaşır
    if has_pnl:
        score += 15
    if has_backtest:
        score += 10
    affiliations = detect_institutions(repo.get("description", "") + " " + rl)
    if affiliations:
        score += 8
    return min(score, 100), has_paper_ref, has_pnl, has_backtest, affiliations


def tier_of(score):
    if score >= 75:
        return "S"
    if score >= 60:
        return "A"
    if score >= 40:
        return "B"
    return "C"


# --------------------------------------------------------------------------
# 4 — Tekrar önleme: son 7 günün digest'lerindeki URL'leri topla
# --------------------------------------------------------------------------
def load_recent_urls(target_date=None, days=7):
    """target_date'ten önceki N günün digest'lerindeki URL'leri + arXiv base-id'lerini döner
    (versiyon numarası değişse bile aynı paper tekrar tam kart olarak girmesin).
    Backfill'de (geçmiş tarih verilince) bugüne değil hedef tarihe göre bakar."""
    keys = set()
    ref = target_date or datetime.date.today()
    if not os.path.isdir(DIGESTS_DIR):
        return keys
    for i in range(1, days + 1):  # target_date'in kendi digest'i hariç — aynı gün tekrar çalıştırılabilsin
        d = ref - datetime.timedelta(days=i)
        path = os.path.join(DIGESTS_DIR, f"radar-{d}.md")
        if os.path.exists(path):
            with open(path, encoding="utf-8") as f:
                content = f.read()
            found = re.findall(r"https?://\S+", content)
            keys.update(found)
            for u in found:
                if "arxiv.org" in u:
                    keys.add(base_arxiv_id(u))
    return keys


# --------------------------------------------------------------------------
# 3a — Digest render
# --------------------------------------------------------------------------
def summary_snippet(text, max_sentences=2):
    sentences = re.split(r"(?<=[.!?])\s+", text.strip())
    return " ".join(sentences[:max_sentences])


def clean_abstract(text):
    """Abstract'ı çeviri öncesi temizle: LaTeX komutları, inline matematik, çift boşluk.
    Makine çevirisine ham LaTeX gidince çıktı saçmalaşıyordu."""
    t = text or ""
    t = t.replace("--", ", ")                                  # arXiv'de sık: 'word--word' → çeviriyi bozuyor
    t = re.sub(r"\$\$?[^$]{1,120}\$\$?", " ", t)              # inline/display matematik
    t = re.sub(r"\\(?:textit|textbf|emph|texttt|mathrm|mathbf)\{([^}]*)\}", r"\1", t)
    t = re.sub(r"\\[a-zA-Z]+\*?(\[[^\]]*\])?(\{[^}]*\})?", " ", t)  # kalan \komutlar
    t = re.sub(r"[{}~]", " ", t)
    t = re.sub(r"\s+", " ", t).strip()
    return t


_CONTRIB_RE = re.compile(
    r"\bwe\s+(introduce|propose|present|develop|formalize|derive|construct|design|build)\b|"
    r"\bthis\s+paper\s+(introduces|proposes|presents|develops)\b", re.I)


def condense_abstract(text, max_sentences=6):
    """Abstract'ı öz hale getir: kısaysa dokunma; uzunsa ilk 2 cümle (bağlam/amaç) +
    İLK KATKI CÜMLESİ ('we introduce/propose...' — makalenin ne yaptığı, asla düşmemeli) +
    son 2 cümle (bulgular/sonuç). Kural-tabanlı, LLM yok."""
    t = clean_abstract(text)
    sents = [s for s in re.split(r"(?<=[.!?])\s+", t) if s.strip()]
    if len(sents) <= max_sentences:
        return " ".join(sents)
    picked = set(range(2)) | set(range(len(sents) - 2, len(sents)))
    for i, s in enumerate(sents):
        if _CONTRIB_RE.search(s):
            picked.add(i)
            break  # ilk katkı cümlesi yeter
    return " ".join(sents[i] for i in sorted(picked))


def translate_to_turkish(text):
    """Ücretsiz Google Translate public endpoint'i (translate.googleapis.com) — AI token maliyeti yok.
    Metni CÜMLE SINIRLARINDAN ~1200 karakterlik parçalara böler; eski kelime-ortası bölme
    cümleleri ikiye kesip çeviriyi bozuyordu. Abstract'ın TAMAMI çevrilir, akış korunur."""
    text = clean_abstract(text)
    if not text:
        return ""
    sentences = [s for s in re.split(r"(?<=[.!?])\s+", text) if s.strip()]
    chunks, current, current_len = [], [], 0
    for s in sentences:
        if current and current_len + len(s) + 1 > 1200:
            chunks.append(" ".join(current))
            current, current_len = [], 0
        current.append(s)
        current_len += len(s) + 1
    if current:
        chunks.append(" ".join(current))

    translated = []
    for chunk in chunks:
        url = f"https://translate.googleapis.com/translate_a/single?client=gtx&sl=en&tl=tr&dt=t&q={urllib.parse.quote(chunk)}"
        try:
            raw = http_get(url, timeout=15, retries=2)
            data = json.loads(raw)
            piece = "".join(seg[0] for seg in data[0] if seg and seg[0])
            translated.append(piece.strip())
        except Exception as e:
            print(f"[translate] hata: {e}", file=sys.stderr)
            translated.append(chunk)  # çeviri başarısızsa orijinal İngilizce kalsın, boş kalmasın
    result = re.sub(r"\s+", " ", " ".join(translated)).strip()
    return result


def _md_bold_to_html(text):
    """'**X:**' -> '<strong>X:</strong>', geri kalan metni escape ederek — tr_summary'yi HTML'de göstermek için."""
    out, last = [], 0
    for m in re.finditer(r"\*\*(.+?)\*\*", text):
        out.append(_html_escape(text[last:m.start()]))
        out.append(f"<strong>{_html_escape(m.group(1))}</strong>")
        last = m.end()
    out.append(_html_escape(text[last:]))
    return "".join(out)


def render_paper_card(item):
    p, code, score, t = item["paper"], item["code"], item["score"], item["tier"]
    affiliations = item.get("affiliations") or []
    pnl_flag = item.get("pnl_flag", False)
    kod_str = "Yok"
    if code:
        star_str = f"⭐ {code['stars']}, as-of {datetime.date.today()}" if code.get("stars") else "yıldız [KAYNAK YOK]"
        kod_str = f"{code['url']} ({star_str})"
    tur = "Paper+Resmi Kod" if (code and code.get("official")) else ("Paper+3.Taraf Kod" if code else "Sadece Paper")
    ozet = item.get("tr_summary") or summary_snippet(p["summary"])
    kurum_satiri = f"**Kurumsal sinyal:** {', '.join(affiliations)}\n" if affiliations else ""
    topics = item.get("topics") or []
    konu_satiri = f"**Konu:** {' · '.join(f'{e} {n}' for n, e in topics)}\n" if topics else ""
    claims = item.get("claims") or []
    guncelleme_etiketi = " *(v-güncelleme — daha önce raporlanmış paper'ın yeni versiyonu)*" if p.get("is_update") else ""
    if claims:
        pnl_satiri = f"**İddia edilen sonuçlar (doğrulanmamış):** {' · '.join(claims)}\n"
    elif pnl_flag:
        pnl_satiri = ("İddia edilen sonuç: metinde performans iddiası var ama sayı çıkarılamadı — "
                      "**paper içinde kontrol et.**\n")
    else:
        pnl_satiri = "İddia edilen sonuç: [KAYNAK YOK] (metinde somut performans sayısı bulunamadı).\n"
    return (
        f"### [Skor {score} · {t}] {p['title']}{guncelleme_etiketi}\n"
        f"**Tür:** {tur} · **Paper:** {p['url']} · **Kod:** {kod_str}\n"
        f"{konu_satiri}"
        f"{kurum_satiri}"
        f"{ozet}\n{pnl_satiri}"
        f"**Neden denemeye değer:** {p['category']} kategorisinde "
        f"{'resmi kodla birlikte yayınlanmış' if (code and code.get('official')) else '3.taraf kod eşleşmesi bulundu (elle doğrula)' if code else 'kod eşleşmesi bulunamadı'}"
        f"{', tanınmış kurum/fon imzası var' if affiliations else ''}. "
        f"**Dikkat:** PnL/Sharpe iddiaları paper içinde doğrulanmalı; bu satır otomatik taramadır.\n"
    )


def render_repo_card(repo, score, t, has_paper_ref=False, has_pnl=False, has_backtest=False, affiliations=None):
    affiliations = affiliations or []
    kurum_satiri = f"**Kurumsal sinyal:** {', '.join(affiliations)}\n" if affiliations else ""
    sinyaller = []
    if has_paper_ref:
        sinyaller.append("README bir paper'a referans veriyor")
    if has_pnl:
        sinyaller.append("somut PnL/Sharpe iddiası var")
    if has_backtest:
        sinyaller.append("backtest/walk-forward raporu var")
    sinyal_satiri = f"**README sinyalleri:** {', '.join(sinyaller)} (doğrulanmamış).\n" if sinyaller else ""
    return (
        f"### [Skor {score} · {t}] {repo['full_name']}\n"
        f"**Tür:** Sadece Repo · **Repo:** {repo['html_url']} (⭐ {repo.get('stargazers_count', '[KAYNAK YOK]')}, "
        f"as-of {datetime.date.today()})\n"
        f"{kurum_satiri}"
        f"{(repo.get('description') or '').strip() or '[KAYNAK YOK açıklama]'}\n"
        f"{sinyal_satiri}"
        f"**Neden denemeye değer:** {'Yıldız hızı yüksek / hareketli' if repo.get('_hareketli') else 'Yeni açılmış quant repo'}. "
        f"**Dikkat:** Backtest/PnL kanıtı bağımsız doğrulanmadı.\n"
    )


def group_by_topic(items):
    """Skorlanmış öğeleri birincil konuya göre grupla; TOPIC_RULES sırasıyla döner,
    grup içi skor sırası korunur."""
    groups = {}
    for it in sorted(items, key=lambda x: x["score"], reverse=True):
        topics = it.get("topics") or [TOPIC_OTHER]
        groups.setdefault(topics[0], []).append(it)
    ordered = []
    order_index = {(n, e): i for i, (n, e, _) in enumerate(TOPIC_RULES)}
    for key in sorted(groups, key=lambda k: order_index.get(k, len(TOPIC_RULES))):
        ordered.append((key, groups[key]))
    return ordered


def _compact_paper_line(it):
    p = it["paper"]
    claims = " — " + " · ".join(it["claims"][:3]) if it.get("claims") else ""
    line = f"- [Skor {it['score']} · {it['tier']}] [{p['title']}]({p['url']}){claims}"
    if it.get("tr_snippet"):
        line += f"\n  *{it['tr_snippet']}*"
    return line


def _compact_repo_line(it):
    r = it["repo"]
    claims = " — " + " · ".join(it["claims"][:3]) if it.get("claims") else ""
    return f"- [Skor {it['score']} · {it['tier']}] [{r['full_name']}]({r['html_url']}) (⭐ {r.get('stargazers_count', '?')}){claims}"


def build_digest(target_date, papers_scored, repos_scored, hn, reddit_q, reddit_algo, quantocracy, errors=None):
    all_items = papers_scored + repos_scored
    all_items.sort(key=lambda x: x["score"], reverse=True)

    n_papers = len(papers_scored)
    n_repos = len(repos_scored)
    n_matched = sum(1 for x in papers_scored if x.get("code"))
    n_institution = sum(1 for x in all_items if x.get("affiliations"))
    n_pnl = sum(1 for x in papers_scored if x.get("pnl_flag")) + sum(1 for x in repos_scored if x.get("has_pnl"))

    lines = []
    lines.append(f"# Quant Radar — {target_date}\n")
    lines.append(
        f"Dün ({target_date}, UTC) taranan çıktılar: **{n_papers} paper**, **{n_repos} yeni/hareketli repo** "
        f"(GitHub bağımsız da tarandı, sadece paper eşleşmesiyle sınırlı değil), **{n_matched} paper+kod eşleşmesi**, "
        f"**{n_institution} tanınmış kurum/fon imzalı bulgu**, **{n_pnl} somut PnL/Sharpe iddiası içeren bulgu**. "
        f"Sıralama skora göre: kod eşleşmesi, büyük kurum/fon imzası (MIT, Stanford, Citadel, Two Sigma vb.) ve "
        f"somut performans iddiası olan bulgular öne çıkarılır. Bu digest kod-tabanlı deterministik taramayla "
        f"üretildi (arXiv + GitHub Search API + HN/Reddit/Quantocracy).\n"
    )

    # Paper'lar — konuya göre gruplanmış. S/A tam kart, B/C tek satır (skor rozetiyle).
    if papers_scored:
        lines.append("## 📚 Paper'lar — konuya göre\n")
        for (topic_name, topic_emoji), items in group_by_topic(papers_scored):
            lines.append(f"### {topic_emoji} {topic_name} ({len(items)})\n")
            for it in items:
                if it["tier"] in ("S", "A") and it.get("card"):
                    lines.append(it["card"])
                else:
                    lines.append(_compact_paper_line(it))
            lines.append("")

    # GitHub repoları ayrı bölüm — S/A tam kart, B/C tek satır.
    if repos_scored:
        lines.append("## 🐙 GitHub Repoları\n")
        for it in sorted(repos_scored, key=lambda x: x["score"], reverse=True):
            if it["tier"] in ("S", "A") and it.get("card"):
                lines.append(it["card"])
            else:
                lines.append(_compact_repo_line(it))
        lines.append("")

    lines.append("## 📡 Topluluk nabzı\n")
    if hn:
        lines.append("**Hacker News:**")
        for h in hn[:8]:
            lines.append(f"- ({h['points']} pts) [{h['title']}]({h['url']})")
        lines.append("")
    if reddit_q or reddit_algo:
        lines.append("**Reddit r/quant, r/algotrading:**")
        for r in (reddit_q + reddit_algo)[:10]:
            lines.append(f"- ({r['score']} pts) [{r['title']}]({r['url']})")
        lines.append("")
    if quantocracy:
        lines.append("**Quantocracy:**")
        for q in quantocracy[:10]:
            lines.append(f"- [{q['title']}]({q['url']})")
        lines.append("")
    if not (hn or reddit_q or reddit_algo or quantocracy):
        lines.append("Topluluk kaynaklarından bugün [KAYNAK YOK] — çekilemedi veya boş döndü.\n")

    if errors:
        lines.append("## ⚠️ Kaynak hataları\n")
        for err in errors:
            lines.append(f"- [KAYNAK YOK] {err}")
        lines.append("")

    lines.append(
        "---\n*Bu digest kod-tabanlı otomatik taramanın çıktısıdır; yatırım tavsiyesi değildir, "
        "işlem önerisi içermez. Raporlanan tüm sayılar iddia olarak sunulmuştur, doğrulama gerektirir.*"
    )
    return "\n".join(lines)


# --------------------------------------------------------------------------
# 3b — HTML bülten render (mail için) — kurumsal görünümlü, kart tabanlı tasarım.
# mail-digest.yml bu dosyayı bulursa markdown->html çevirisi yerine direkt bunu yollar.
# --------------------------------------------------------------------------
_TIER_COLORS = {
    "S": ("#F5A623", "#3A2A00"),  # amber
    "A": ("#4A90D9", "#0B1F3A"),  # blue
    "B": ("#8896A6", "#1A2330"),  # slate gray
    "C": ("#C9D0D9", "#3A4250"),  # light gray
}


def _html_escape(text):
    return (text or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _badge_html(tier, score):
    bg, fg = _TIER_COLORS.get(tier, _TIER_COLORS["C"])
    return (
        f'<span style="display:inline-block;background:{bg};color:{fg};font-weight:700;'
        f'font-size:12px;padding:3px 10px;border-radius:12px;letter-spacing:0.3px;">'
        f'{tier} · {score}</span>'
    )


def _pill_html(label):
    return (
        f'<span style="display:inline-block;background:#EEF1F5;color:#3A4250;font-size:12px;'
        f'padding:4px 10px;border-radius:10px;margin:0 6px 6px 0;">{label}</span>'
    )


def _claim_chip_html(label):
    """Somut iddia rozeti (Sharpe 1.8, %12 getiri...) — turuncu tonlu, 'doğrulanmamış' vurgusu."""
    return (
        f'<span style="display:inline-block;background:#FFF3E0;color:#B5651D;font-size:12px;'
        f'font-weight:600;padding:3px 9px;border-radius:10px;margin:0 6px 6px 0;'
        f'border:1px solid #F0D9B8;">{_html_escape(label)}</span>'
    )


def _topic_chip_html(name, emoji):
    return (
        f'<span style="display:inline-block;background:#E8F0FA;color:#1F4E79;font-size:12px;'
        f'padding:3px 9px;border-radius:10px;margin:0 6px 6px 0;">{emoji} {_html_escape(name)}</span>'
    )


def _paper_card_html(item):
    p, code, score, t = item["paper"], item["code"], item["score"], item["tier"]
    affiliations = item.get("affiliations") or []
    pnl_flag = item.get("pnl_flag", False)
    tur = "Paper+Resmi Kod" if (code and code.get("official")) else ("Paper+3.Taraf Kod" if code else "Sadece Paper")
    ozet = _md_bold_to_html(item["tr_summary"]) if item.get("tr_summary") else _html_escape(summary_snippet(p["summary"]))
    guncelleme = (
        ' <span style="font-size:11px;color:#B5651D;font-style:italic;">(v-güncelleme)</span>'
        if p.get("is_update") else ""
    )
    kod_link = ""
    if code:
        star_suffix = f" ⭐{code['stars']}" if code.get("stars") else ""
        kod_link = f' · <a href="{code["url"]}" style="color:#4A90D9;text-decoration:none;">Kod{star_suffix}</a>'
    topics = item.get("topics") or []
    claims = item.get("claims") or []
    chips_html = "".join(_topic_chip_html(n, e) for n, e in topics)
    chips_html += "".join(_pill_html(_html_escape(a)) for a in affiliations)
    if claims:
        pnl_note = ('<span style="font-size:11px;color:#8896A6;">İddia (doğrulanmamış): </span>'
                    + "".join(_claim_chip_html(c) for c in claims))
    elif pnl_flag:
        pnl_note = '<span style="color:#B5651D;">Performans iddiası var, sayı çıkarılamadı — paper içinde kontrol et.</span>'
    else:
        pnl_note = '<span style="color:#8896A6;">[KAYNAK YOK] — metinde somut performans sayısı bulunamadı.</span>'
    return f"""
    <div style="border:1px solid #E2E5EA;border-radius:10px;padding:16px 20px;margin-bottom:14px;background:#FFFFFF;">
      <div style="margin-bottom:8px;">{_badge_html(t, score)}
        <span style="font-size:11px;color:#8896A6;margin-left:8px;">{tur}</span></div>
      <h3 style="margin:0 0 6px;font-size:16px;color:#0B1F3A;font-family:Georgia,serif;">{_html_escape(p['title'])}{guncelleme}</h3>
      <div style="margin-bottom:8px;">{chips_html}</div>
      <p style="font-size:13px;color:#3A4250;line-height:1.6;margin:0 0 8px;">{ozet}</p>
      <p style="font-size:12px;line-height:1.7;margin:0 0 8px;">{pnl_note}</p>
      <p style="font-size:12px;margin:0;"><a href="{p['url']}" style="color:#4A90D9;text-decoration:none;">Paper</a>{kod_link}</p>
    </div>"""


def _repo_card_html(item):
    r, score, t = item["repo"], item["score"], item["tier"]
    affiliations = item.get("affiliations") or []
    sinyaller = []
    if item.get("has_paper_ref"):
        sinyaller.append("README paper'a referans veriyor")
    if item.get("has_pnl"):
        sinyaller.append("somut PnL/Sharpe iddiası var")
    if item.get("has_backtest"):
        sinyaller.append("backtest/walk-forward raporu var")
    sinyal_html = (
        f'<p style="font-size:12px;color:#B5651D;margin:0 0 8px;">{_html_escape(", ".join(sinyaller))} (doğrulanmamış)</p>'
        if sinyaller else ""
    )
    chips_html = "".join(_topic_chip_html(n, e) for n, e in (item.get("topics") or []))
    chips_html += "".join(_pill_html(_html_escape(a)) for a in affiliations)
    chips_html += "".join(_claim_chip_html(c) for c in (item.get("claims") or []))
    stars = r.get("stargazers_count", "[KAYNAK YOK]")
    return f"""
    <div style="border:1px solid #E2E5EA;border-radius:10px;padding:16px 20px;margin-bottom:14px;background:#FFFFFF;">
      <div style="margin-bottom:8px;">{_badge_html(t, score)}
        <span style="font-size:11px;color:#8896A6;margin-left:8px;">Sadece Repo · ⭐ {stars}</span></div>
      <h3 style="margin:0 0 6px;font-size:16px;color:#0B1F3A;font-family:Georgia,serif;">{_html_escape(r['full_name'])}</h3>
      <div style="margin-bottom:8px;">{chips_html}</div>
      <p style="font-size:13px;color:#3A4250;line-height:1.6;margin:0 0 8px;">{_html_escape((r.get('description') or '').strip()) or '[KAYNAK YOK açıklama]'}</p>
      {sinyal_html}
      <p style="font-size:12px;margin:0;"><a href="{r['html_url']}" style="color:#4A90D9;text-decoration:none;">GitHub'da aç</a></p>
    </div>"""


def _compact_row_html(it):
    """B/C öğeleri için tek satır: rozet + başlık linki + iddia çipleri."""
    if "paper" in it:
        url, title = it["paper"]["url"], it["paper"]["title"]
    else:
        url, title = it["repo"]["html_url"], it["repo"]["full_name"] + f' (⭐ {it["repo"].get("stargazers_count", "?")})'
    chips = "".join(_claim_chip_html(c) for c in (it.get("claims") or [])[:3])
    snippet = ""
    if it.get("tr_snippet"):
        snippet = (f'<p style="font-size:12px;color:#5A6472;line-height:1.55;'
                   f'margin:2px 0 12px 6px;">{_html_escape(it["tr_snippet"])}</p>')
    return (
        f'<p style="font-size:13px;margin:6px 0 2px;line-height:1.8;">{_badge_html(it["tier"], it["score"])} '
        f'<a href="{url}" style="color:#0B1F3A;text-decoration:none;">{_html_escape(title)}</a> {chips}</p>'
        f'{snippet}'
    )


def render_digest_html(target_date, papers_scored, repos_scored, hn, reddit_q, reddit_algo, quantocracy, errors=None):
    all_items = papers_scored + repos_scored

    n_papers = len(papers_scored)
    n_repos = len(repos_scored)
    n_matched = sum(1 for x in papers_scored if x.get("code"))
    n_institution = sum(1 for x in all_items if x.get("affiliations"))
    n_pnl = sum(1 for x in papers_scored if x.get("claims") or x.get("pnl_flag")) \
        + sum(1 for x in repos_scored if x.get("claims") or x.get("has_pnl"))

    # Paper'lar — konuya göre gruplanmış bölümler. S/A tam kart, B/C kompakt satır.
    section_html = ""
    if papers_scored:
        section_html += ('<h2 style="font-size:16px;color:#0B1F3A;margin:20px 0 4px;'
                         'font-family:Georgia,serif;">📚 Paper&#39;lar — konuya göre</h2>')
        for (topic_name, topic_emoji), items in group_by_topic(papers_scored):
            section_html += (
                f'<h3 style="font-size:14px;color:#1F4E79;margin:20px 0 10px;font-family:Georgia,serif;'
                f'border-bottom:2px solid #E8F0FA;padding-bottom:6px;">{topic_emoji} {_html_escape(topic_name)} '
                f'<span style="font-weight:400;color:#8896A6;">({len(items)})</span></h3>'
            )
            for it in items:
                section_html += _paper_card_html(it) if it["tier"] in ("S", "A") else _compact_row_html(it)

    if repos_scored:
        section_html += ('<h2 style="font-size:16px;color:#0B1F3A;margin:28px 0 12px;'
                         'font-family:Georgia,serif;">🐙 GitHub Repoları</h2>')
        for it in sorted(repos_scored, key=lambda x: x["score"], reverse=True):
            section_html += _repo_card_html(it) if it["tier"] in ("S", "A") else _compact_row_html(it)

    community_html = ""
    if hn:
        community_html += '<p style="font-size:12px;font-weight:700;color:#0B1F3A;margin:10px 0 4px;">Hacker News</p>'
        community_html += "".join(
            f'<p style="font-size:12px;margin:2px 0;color:#3A4250;">({h["points"]} pts) '
            f'<a href="{h["url"]}" style="color:#4A90D9;text-decoration:none;">{_html_escape(h["title"])}</a></p>'
            for h in hn[:8]
        )
    if reddit_q or reddit_algo:
        community_html += '<p style="font-size:12px;font-weight:700;color:#0B1F3A;margin:10px 0 4px;">Reddit r/quant, r/algotrading</p>'
        community_html += "".join(
            f'<p style="font-size:12px;margin:2px 0;color:#3A4250;">({r["score"]} pts) '
            f'<a href="{r["url"]}" style="color:#4A90D9;text-decoration:none;">{_html_escape(r["title"])}</a></p>'
            for r in (reddit_q + reddit_algo)[:10]
        )
    if quantocracy:
        community_html += '<p style="font-size:12px;font-weight:700;color:#0B1F3A;margin:10px 0 4px;">Quantocracy</p>'
        community_html += "".join(
            f'<p style="font-size:12px;margin:2px 0;color:#3A4250;">'
            f'<a href="{q["url"]}" style="color:#4A90D9;text-decoration:none;">{_html_escape(q["title"])}</a></p>'
            for q in quantocracy[:10]
        )
    if not community_html:
        community_html = '<p style="font-size:12px;color:#8896A6;">[KAYNAK YOK] — topluluk kaynakları bugün çekilemedi veya boştu.</p>'

    errors_html = ""
    if errors:
        errors_html = (
            '<div style="background:#FFF7E6;border:1px solid #F5A623;border-radius:8px;padding:14px 18px;margin-top:20px;">'
            '<p style="font-size:12px;font-weight:700;color:#B5651D;margin:0 0 6px;">⚠️ Kaynak hataları</p>'
            + "".join(f'<p style="font-size:12px;color:#8A5A1E;margin:2px 0;">[KAYNAK YOK] {_html_escape(e)}</p>' for e in errors)
            + "</div>"
        )

    summary_paragraph = (
        f"Dün ({target_date}, UTC) taranan çıktılar: <strong>{n_papers} paper</strong>, "
        f"<strong>{n_repos} yeni/hareketli repo</strong>, <strong>{n_matched} paper+kod eşleşmesi</strong>, "
        f"<strong>{n_institution} tanınmış kurum/fon imzalı bulgu</strong>, "
        f"<strong>{n_pnl} somut PnL/Sharpe iddiası içeren bulgu</strong>. Sıralama skora göre: kod eşleşmesi, "
        f"büyük kurum/fon imzası ve somut performans iddiası olan bulgular öne çıkarılır."
    )

    return f"""<!DOCTYPE html>
<html>
<head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1.0"></head>
<body style="margin:0;padding:24px 12px;background:#F4F6F9;font-family:-apple-system,Helvetica,Arial,sans-serif;">
  <div style="max-width:680px;margin:0 auto;background:#FFFFFF;border-radius:12px;overflow:hidden;box-shadow:0 1px 4px rgba(11,31,58,0.08);">
    <div style="background:#0B1F3A;padding:28px 32px;">
      <h1 style="color:#FFFFFF;margin:0;font-size:23px;font-family:Georgia,serif;letter-spacing:0.2px;">🔭 Quant Radar — {target_date}</h1>
      <p style="color:#9FB0C8;margin:6px 0 0;font-size:12px;letter-spacing:0.5px;text-transform:uppercase;">Günlük Kod-Tabanlı Tarama</p>
    </div>
    <div style="padding:20px 32px;border-bottom:1px solid #E2E5EA;">
      <p style="font-size:13px;color:#3A4250;line-height:1.6;margin:0;">{summary_paragraph}</p>
    </div>
    <div style="padding:24px 32px;">
      {section_html if section_html else '<p style="font-size:13px;color:#8896A6;">Bugün eşik üstü bulgu yok — tüm sonuçlar arşiv dosyasında (.md) mevcut.</p>'}
    </div>
    <div style="padding:20px 32px;background:#F8F9FB;border-top:1px solid #E2E5EA;">
      <p style="font-size:13px;font-weight:700;color:#0B1F3A;margin:0 0 8px;font-family:Georgia,serif;">📡 Topluluk Nabzı</p>
      {community_html}
    </div>
    {errors_html}
    <div style="padding:18px 32px;background:#F4F6F9;">
      <p style="font-size:11px;color:#8896A6;line-height:1.6;margin:0;">
        Bu digest kod-tabanlı otomatik taramanın çıktısıdır; yatırım tavsiyesi değildir, işlem önerisi içermez.
        Raporlanan tüm sayılar iddia olarak sunulmuştur, doğrulama gerektirir.
      </p>
    </div>
  </div>
</body>
</html>"""


# --------------------------------------------------------------------------
# main
# --------------------------------------------------------------------------
def main():
    if len(sys.argv) > 1:
        target_date = datetime.datetime.strptime(sys.argv[1], "%Y-%m-%d").date()
    else:
        target_date = datetime.datetime.utcnow().date() - datetime.timedelta(days=1)

    print(f"Quant Radar taraması başlıyor — hedef tarih: {target_date}")

    errors = []  # digest sonunda "çekince" olarak listelenecek kaynak hataları
    recent_urls = load_recent_urls(target_date)

    try:
        papers = fetch_all_arxiv(target_date)
    except Exception as e:
        print(f"[arXiv] tüm kategori taraması beklenmeyen hatayla durdu: {e}", file=sys.stderr)
        errors.append(f"arXiv taraması tamamlanamadı: {e}")
        papers = []
    print(f"  arXiv: {len(papers)} paper bulundu", flush=True)

    papers_scored = []
    for i, p in enumerate(papers, 1):
        print(f"  [paper {i}/{len(papers)}] {p['title'][:70]}", flush=True)
        try:
            # Daha önce raporlanmış (aynı base arXiv id) ve bu kez de "yeni" değilse atla.
            # is_update=True olanlar (v2/v3 revizyonu) bilinçli olarak tekrar girer, kart üzerinde etiketlenir.
            if not p.get("is_update") and (p["url"] in recent_urls or p["base_id"] in recent_urls):
                continue
            code = find_code_for_paper(p)
            fulltext = fetch_arxiv_fulltext(p["url"])
            affiliations = detect_institutions(p["title"] + " " + p["summary"] + " " + " ".join(p["authors"]) + " " + fulltext)
            score = score_paper(p, code, affiliations=affiliations, extra_text=fulltext)
            t = tier_of(score)
            item = {
                "paper": p, "code": code, "score": score, "tier": t,
                "affiliations": affiliations, "pnl_flag": has_pnl_claim(p["summary"] + " " + fulltext),
                "topics": classify_topics(p["title"] + " " + p["summary"]),
                "claims": extract_claims(p["summary"] + " " + fulltext),
            }
            if t in ("S", "A"):
                # Sadece tam kart alacak (S/A) paper'lar için çeviri yap — B/C zaten kısa/tek satır,
                # gereksiz çeviri isteğiyle taramayı yavaşlatmayalım. Abstract'ın tamamı düz akışla
                # çevrilir (eski kural-tabanlı Amaç/Yöntem/Bulgular ayrıştırması saçma çıktı verdiği
                # için kaldırıldı).
                tr = translate_to_turkish(condense_abstract(p["summary"]))
                item["tr_summary"] = ("**Özet (otomatik çeviri):** " + tr) if tr else ""
                item["card"] = render_paper_card(item)
            else:
                item["card"] = None
                if t == "B":
                    # B-tier kompakt satırın altına 2 cümlelik mini özet — makale ne yapıyor görünsün.
                    item["tr_snippet"] = translate_to_turkish(summary_snippet(clean_abstract(p["summary"]), 2))
            papers_scored.append(item)
        except Exception as e:
            print(f"[paper] '{p.get('title', '?')}' işlenirken hata, atlanıyor: {e}", file=sys.stderr)
        time.sleep(0.5)

    try:
        repos = fetch_new_repos(target_date)
    except Exception as e:
        print(f"[GitHub] repo taraması beklenmeyen hatayla durdu: {e}", file=sys.stderr)
        errors.append(f"GitHub repo taraması tamamlanamadı: {e}")
        repos = []
    print(f"  GitHub: {len(repos)} yeni/hareketli repo bulundu (bağımsız tarama — paper eşleşmesiyle sınırlı değil)")

    # Yıldıza göre sırala; README derinlemesine taramasını en umut vaat eden N repoya uygula (API bütçesi için).
    repos.sort(key=lambda r: r.get("stargazers_count", 0), reverse=True)
    README_SCAN_LIMIT = 25

    repos_scored = []
    for idx, r in enumerate(repos):
        try:
            if r["html_url"] in recent_urls:
                continue
            readme_text = ""
            if idx < README_SCAN_LIMIT:
                readme_text = fetch_repo_readme(r["full_name"])
                time.sleep(0.3)
            score, has_paper_ref, has_pnl, has_backtest, affiliations = score_repo_only(r, readme_text)
            t = tier_of(score)
            item = {
                "repo": r, "score": score, "tier": t,
                "has_paper_ref": has_paper_ref, "has_pnl": has_pnl,
                "has_backtest": has_backtest, "affiliations": affiliations,
                "topics": classify_topics((r.get("description") or "") + " " + readme_text[:4000]),
                "claims": extract_claims((r.get("description") or "") + " " + readme_text[:4000]),
            }
            item["card"] = (
                render_repo_card(r, score, t, has_paper_ref, has_pnl, has_backtest, affiliations)
                if t in ("S", "A") else None
            )
            repos_scored.append(item)
        except Exception as e:
            print(f"[repo] '{r.get('full_name', '?')}' işlenirken hata, atlanıyor: {e}", file=sys.stderr)

    try:
        hn = fetch_hn(target_date)
    except Exception as e:
        print(f"[HN] beklenmeyen hata: {e}", file=sys.stderr)
        errors.append(f"HN taraması tamamlanamadı: {e}")
        hn = []
    try:
        reddit_q = fetch_reddit("quant", target_date)
    except Exception as e:
        print(f"[Reddit r/quant] beklenmeyen hata: {e}", file=sys.stderr)
        errors.append(f"Reddit r/quant taraması tamamlanamadı: {e}")
        reddit_q = []
    try:
        reddit_algo = fetch_reddit("algotrading", target_date)
    except Exception as e:
        print(f"[Reddit r/algotrading] beklenmeyen hata: {e}", file=sys.stderr)
        errors.append(f"Reddit r/algotrading taraması tamamlanamadı: {e}")
        reddit_algo = []
    try:
        quantocracy = fetch_quantocracy()
    except Exception as e:
        print(f"[Quantocracy] beklenmeyen hata: {e}", file=sys.stderr)
        errors.append(f"Quantocracy taraması tamamlanamadı: {e}")
        quantocracy = []

    digest = build_digest(target_date, papers_scored, repos_scored, hn, reddit_q, reddit_algo, quantocracy, errors)
    digest_html = render_digest_html(target_date, papers_scored, repos_scored, hn, reddit_q, reddit_algo, quantocracy, errors)

    os.makedirs(DIGESTS_DIR, exist_ok=True)
    out_path = os.path.join(DIGESTS_DIR, f"radar-{target_date}.md")
    with open(out_path, "w", encoding="utf-8") as f:
        f.write(digest)
    html_path = os.path.join(DIGESTS_DIR, f"radar-{target_date}.html")
    with open(html_path, "w", encoding="utf-8") as f:
        f.write(digest_html)
    print(f"Digest yazıldı: {out_path}")
    print(f"HTML bülten yazıldı: {html_path}")


if __name__ == "__main__":
    main()
