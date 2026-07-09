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
        out.append({
            "source": "arxiv", "category": cat, "title": title, "summary": summary,
            "url": link, "authors": authors, "pub_date": pub_date, "upd_date": upd_date,
            "base_id": base_arxiv_id(link),
        })
    return out


def _fetch_arxiv_feed(cat, sort_by, max_results, parse_retries=3):
    """arXiv Atom feed'ini çek + parse et. Ağ/format hatalarında ASLA exception fırlatmaz — boş liste döner.
    arXiv API'si art arda hızlı isteklerde bozuk/HTML hata sayfası dönebiliyor (200 OK ama XML değil);
    bu durumda http_get'in kendi retry'ı işe yaramaz (istek "başarılı" sayılır), o yüzden burada
    parse hatasında da tüm isteği ayrıca tekrar deniyoruz — kaynak sessizce boş dönmesin, gerçekten
    tükenmeden pes etmesin."""
    url = (f"http://export.arxiv.org/api/query?search_query=cat:{cat}"
           f"&sortBy={sort_by}&sortOrder=descending&max_results={max_results}")
    for attempt in range(1, parse_retries + 1):
        try:
            raw = http_get(url, timeout=25)
        except Exception as e:
            print(f"[arXiv] {cat} ({sort_by}) istek hatası (deneme {attempt}/{parse_retries}): {e}", file=sys.stderr)
            if attempt < parse_retries:
                time.sleep(4 * attempt)
                continue
            return []
        try:
            root = ET.fromstring(raw)
        except ET.ParseError as e:
            print(f"[arXiv] {cat} ({sort_by}) XML parse hatası — arXiv geçici hata sayfası döndürmüş olabilir "
                  f"(deneme {attempt}/{parse_retries}): {e}", file=sys.stderr)
            if attempt < parse_retries:
                time.sleep(4 * attempt)
                continue
            return []
        try:
            return _parse_arxiv_entries(root, {"a": "http://www.w3.org/2005/Atom"}, cat)
        except Exception as e:
            print(f"[arXiv] {cat} ({sort_by}) entry parse hatası: {e}", file=sys.stderr)
            return []
    return []


def fetch_arxiv_category(cat, target_date, max_results=150):
    """Dün İLK KEZ gönderilen paper'lar (yeni)."""
    out = []
    for e in _fetch_arxiv_feed(cat, "submittedDate", max_results):
        if e["pub_date"] != target_date:
            continue
        e["is_update"] = False
        e["date"] = str(e["pub_date"])
        out.append(e)
    return out


def fetch_arxiv_updates(cat, target_date, max_results=150):
    """Dün REVİZE edilmiş (v2/v3...) ama daha önce başka bir günde ilk gönderilmiş paper'lar."""
    out = []
    for e in _fetch_arxiv_feed(cat, "lastUpdatedDate", max_results):
        if e["upd_date"] != target_date:
            continue
        if e["pub_date"] == target_date:
            continue  # zaten "yeni" olarak yakalanıyor, tekrar sayma
        e["is_update"] = True
        e["date"] = str(e["upd_date"])
        out.append(e)
    return out


def fetch_all_arxiv(target_date):
    """Hem yeni gönderilen hem de dün revize edilen (v2/v3) paper'ları döner.
    Bir kategori/kaynak patlarsa diğerleri etkilenmez — her çağrı kendi içinde korumalı."""
    papers, seen_base_ids = [], set()
    for cat in ARXIV_CATEGORIES:
        try:
            found = fetch_arxiv_category(cat, target_date)
        except Exception as e:
            print(f"[arXiv] {cat} (yeni) beklenmeyen hata, atlanıyor: {e}", file=sys.stderr)
            found = []
        for p in found:
            if p["base_id"] in seen_base_ids:
                continue
            seen_base_ids.add(p["base_id"])
            papers.append(p)
        time.sleep(3)  # arXiv nezaket aralığı (resmi öneri: istekler arası >=3sn)
    for cat in ARXIV_CATEGORIES:
        try:
            found = fetch_arxiv_updates(cat, target_date)
        except Exception as e:
            print(f"[arXiv] {cat} (güncelleme) beklenmeyen hata, atlanıyor: {e}", file=sys.stderr)
            found = []
        for p in found:
            if p["base_id"] in seen_base_ids:
                continue
            seen_base_ids.add(p["base_id"])
            papers.append(p)
        time.sleep(3)
    return papers


def github_search_repos(query, extra="", max_results=10):
    q = urllib.parse.quote(f"{query} {extra}".strip())
    url = f"https://api.github.com/search/repositories?q={q}&sort=stars&order=desc&per_page={max_results}"
    try:
        data = http_get_json(url, gh_headers())
        return data.get("items", [])
    except Exception as e:
        print(f"[GitHub] arama hatasi '{query}': {e}", file=sys.stderr)
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
    """Kurum/affiliation tespiti için paper'ın tam metnine (varsa HTML render) bak.
    arXiv Atom API affiliation vermiyor; bu yüzden ayrı bir istek gerekiyor."""
    m = re.search(r"arxiv\.org/abs/([\w.]+)", paper_url or "")
    if not m:
        return ""
    arxiv_id = m.group(1)
    for url in (f"https://arxiv.org/html/{arxiv_id}", f"https://arxiv.org/abs/{arxiv_id}"):
        try:
            return http_get(url, timeout=15)
        except Exception:
            continue
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
    """Abstract içinde GitHub linki ara (resmi kod); yoksa başlık anahtar kelimeleriyle 3.taraf ara."""
    text = paper["summary"] + " " + paper["title"]
    m = re.search(r"https?://github\.com/[\w\-.]+/[\w\-.]+", text)
    if m:
        return {"url": m.group(0).rstrip(".,)"), "official": True, "stars": None}
    words = [w for w in re.findall(r"[A-Za-z]+", paper["title"]) if w.lower() not in STOPWORDS and len(w) > 3]
    keyword_query = " ".join(words[:5])
    if not keyword_query:
        return None
    items = github_search_repos(keyword_query, max_results=3)
    if items:
        top = items[0]
        return {"url": top["html_url"], "official": False, "stars": top.get("stargazers_count")}
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
        time.sleep(1)
    for q in QUANT_REPO_QUERIES:
        for it in github_search_repos(q, extra=f"pushed:{date_str} stars:>50", max_results=5):
            if it["html_url"] in seen:
                continue
            seen.add(it["html_url"])
            it["_hareketli"] = True
            repos.append(it)
        time.sleep(1)
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
def load_recent_urls(days=7):
    """Son N günün digest'lerinde geçen URL'leri + arXiv base-id'lerini döner
    (versiyon numarası değişse bile aynı paper tekrar tam kart olarak girmesin)."""
    keys = set()
    today = datetime.date.today()
    if not os.path.isdir(DIGESTS_DIR):
        return keys
    for i in range(1, days + 1):
        d = today - datetime.timedelta(days=i)
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


def render_paper_card(item):
    p, code, score, t = item["paper"], item["code"], item["score"], item["tier"]
    affiliations = item.get("affiliations") or []
    pnl_flag = item.get("pnl_flag", False)
    kod_str = "Yok"
    if code:
        star_str = f"⭐ {code['stars']}, as-of {datetime.date.today()}" if code.get("stars") else "yıldız [KAYNAK YOK]"
        kod_str = f"{code['url']} ({star_str})"
    tur = "Paper+Resmi Kod" if (code and code.get("official")) else ("Paper+3.Taraf Kod" if code else "Sadece Paper")
    ozet = summary_snippet(p["summary"])
    kurum_satiri = f"**Kurumsal sinyal:** {', '.join(affiliations)}\n" if affiliations else ""
    guncelleme_etiketi = " *(v-güncelleme — daha önce raporlanmış paper'ın yeni versiyonu)*" if p.get("is_update") else ""
    pnl_satiri = (
        "İddia edilen sonuç: metinde somut Sharpe/PnL/getiri iddiası tespit edildi — "
        "**doğrulanmamış, kaynağı paper içinde kontrol et.**\n"
        if pnl_flag else
        "İddia edilen sonuç: [KAYNAK YOK] (metinde somut performans sayısı bulunamadı).\n"
    )
    return (
        f"### [Skor {score} · {t}] {p['title']}{guncelleme_etiketi}\n"
        f"**Tür:** {tur} · **Paper:** {p['url']} · **Kod:** {kod_str}\n"
        f"{kurum_satiri}"
        f"{ozet} {pnl_satiri}"
        f"**Neden denemeye değer:** {p['category']} kategorisinde {'kod ile birlikte yayınlanmış' if code else 'kod eşleşmesi bulunamadı'}"
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


def build_digest(target_date, papers_scored, repos_scored, hn, reddit_q, reddit_algo, quantocracy, errors=None):
    all_items = papers_scored + repos_scored
    all_items.sort(key=lambda x: x["score"], reverse=True)

    s_tier = [x for x in all_items if x["tier"] == "S"]
    a_tier = [x for x in all_items if x["tier"] == "A"]
    b_tier = [x for x in all_items if x["tier"] == "B"]
    c_tier = [x for x in all_items if x["tier"] == "C"]

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

    if s_tier:
        lines.append("## 🏆 S-Tier\n")
        for it in s_tier:
            lines.append(it["card"])
    if a_tier:
        lines.append("## A-Tier\n")
        for it in a_tier:
            lines.append(it["card"])
    if b_tier:
        lines.append("## B-Tier\n")
        for it in b_tier:
            title = it["paper"]["title"] if "paper" in it else it["repo"]["full_name"]
            url = it["paper"]["url"] if "paper" in it else it["repo"]["html_url"]
            lines.append(f"- [Skor {it['score']} · B] [{title}]({url})")
        lines.append("")
    if c_tier:
        lines.append("## C-Tier / Radar altı\n")
        for it in c_tier:
            title = it["paper"]["title"] if "paper" in it else it["repo"]["full_name"]
            url = it["paper"]["url"] if "paper" in it else it["repo"]["html_url"]
            lines.append(f"- [Skor {it['score']} · C] [{title}]({url})")
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


def _paper_card_html(item):
    p, code, score, t = item["paper"], item["code"], item["score"], item["tier"]
    affiliations = item.get("affiliations") or []
    pnl_flag = item.get("pnl_flag", False)
    tur = "Paper+Resmi Kod" if (code and code.get("official")) else ("Paper+3.Taraf Kod" if code else "Sadece Paper")
    ozet = _html_escape(summary_snippet(p["summary"]))
    guncelleme = (
        ' <span style="font-size:11px;color:#B5651D;font-style:italic;">(v-güncelleme)</span>'
        if p.get("is_update") else ""
    )
    kod_link = ""
    if code:
        star_suffix = f" ⭐{code['stars']}" if code.get("stars") else ""
        kod_link = f' · <a href="{code["url"]}" style="color:#4A90D9;text-decoration:none;">Kod{star_suffix}</a>'
    kurum_html = (
        "".join(_pill_html(_html_escape(a)) for a in affiliations)
        if affiliations else ""
    )
    pnl_note = (
        '<span style="color:#B5651D;">Somut Sharpe/PnL/getiri iddiası tespit edildi — doğrulanmamış.</span>'
        if pnl_flag else
        '<span style="color:#8896A6;">[KAYNAK YOK] — metinde somut performans sayısı bulunamadı.</span>'
    )
    return f"""
    <div style="border:1px solid #E2E5EA;border-radius:10px;padding:16px 20px;margin-bottom:14px;background:#FFFFFF;">
      <div style="margin-bottom:8px;">{_badge_html(t, score)}
        <span style="font-size:11px;color:#8896A6;margin-left:8px;">{tur}</span></div>
      <h3 style="margin:0 0 6px;font-size:16px;color:#0B1F3A;font-family:Georgia,serif;">{_html_escape(p['title'])}{guncelleme}</h3>
      <div style="margin-bottom:8px;">{kurum_html}</div>
      <p style="font-size:13px;color:#3A4250;line-height:1.6;margin:0 0 8px;">{ozet}</p>
      <p style="font-size:12px;line-height:1.5;margin:0 0 8px;">{pnl_note}</p>
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
    kurum_html = "".join(_pill_html(_html_escape(a)) for a in affiliations) if affiliations else ""
    stars = r.get("stargazers_count", "[KAYNAK YOK]")
    return f"""
    <div style="border:1px solid #E2E5EA;border-radius:10px;padding:16px 20px;margin-bottom:14px;background:#FFFFFF;">
      <div style="margin-bottom:8px;">{_badge_html(t, score)}
        <span style="font-size:11px;color:#8896A6;margin-left:8px;">Sadece Repo · ⭐ {stars}</span></div>
      <h3 style="margin:0 0 6px;font-size:16px;color:#0B1F3A;font-family:Georgia,serif;">{_html_escape(r['full_name'])}</h3>
      <div style="margin-bottom:8px;">{kurum_html}</div>
      <p style="font-size:13px;color:#3A4250;line-height:1.6;margin:0 0 8px;">{_html_escape((r.get('description') or '').strip()) or '[KAYNAK YOK açıklama]'}</p>
      {sinyal_html}
      <p style="font-size:12px;margin:0;"><a href="{r['html_url']}" style="color:#4A90D9;text-decoration:none;">GitHub'da aç</a></p>
    </div>"""


def render_digest_html(target_date, papers_scored, repos_scored, hn, reddit_q, reddit_algo, quantocracy, errors=None):
    all_items = papers_scored + repos_scored
    all_items.sort(key=lambda x: x["score"], reverse=True)
    s_tier = [x for x in all_items if x["tier"] == "S"]
    a_tier = [x for x in all_items if x["tier"] == "A"]
    b_tier = [x for x in all_items if x["tier"] == "B"]
    c_tier = [x for x in all_items if x["tier"] == "C"]

    def card(it):
        return _paper_card_html(it) if "paper" in it else _repo_card_html(it)

    n_papers = len(papers_scored)
    n_repos = len(repos_scored)
    n_matched = sum(1 for x in papers_scored if x.get("code"))
    n_institution = sum(1 for x in all_items if x.get("affiliations"))
    n_pnl = sum(1 for x in papers_scored if x.get("pnl_flag")) + sum(1 for x in repos_scored if x.get("has_pnl"))

    section_html = ""
    if s_tier:
        section_html += '<h2 style="font-size:15px;color:#0B1F3A;margin:26px 0 12px;font-family:Georgia,serif;">🏆 S-Tier</h2>' + "".join(card(i) for i in s_tier)
    if a_tier:
        section_html += '<h2 style="font-size:15px;color:#0B1F3A;margin:26px 0 12px;font-family:Georgia,serif;">A-Tier</h2>' + "".join(card(i) for i in a_tier)
    if b_tier:
        section_html += '<h2 style="font-size:15px;color:#0B1F3A;margin:26px 0 12px;font-family:Georgia,serif;">B-Tier</h2>'
        section_html += "".join(
            f'<p style="font-size:13px;margin:4px 0;">{_badge_html(i["tier"], i["score"])} '
            f'<a href="{(i["paper"]["url"] if "paper" in i else i["repo"]["html_url"])}" style="color:#0B1F3A;text-decoration:none;">'
            f'{_html_escape(i["paper"]["title"] if "paper" in i else i["repo"]["full_name"])}</a></p>'
            for i in b_tier
        )
    if c_tier:
        section_html += '<h2 style="font-size:15px;color:#0B1F3A;margin:26px 0 12px;font-family:Georgia,serif;">C-Tier / Radar altı</h2>'
        section_html += "".join(
            f'<p style="font-size:12px;color:#8896A6;margin:3px 0;">{i["tier"]}·{i["score"]} '
            f'<a href="{(i["paper"]["url"] if "paper" in i else i["repo"]["html_url"])}" style="color:#8896A6;text-decoration:none;">'
            f'{_html_escape(i["paper"]["title"] if "paper" in i else i["repo"]["full_name"])}</a></p>'
            for i in c_tier
        )

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
    recent_urls = load_recent_urls()

    try:
        papers = fetch_all_arxiv(target_date)
    except Exception as e:
        print(f"[arXiv] tüm kategori taraması beklenmeyen hatayla durdu: {e}", file=sys.stderr)
        errors.append(f"arXiv taraması tamamlanamadı: {e}")
        papers = []
    print(f"  arXiv: {len(papers)} paper bulundu")

    papers_scored = []
    for p in papers:
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
            }
            item["card"] = render_paper_card(item) if t in ("S", "A") else None
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
