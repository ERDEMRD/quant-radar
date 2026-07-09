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
# HTTP yardımcıları
# --------------------------------------------------------------------------
def http_get(url, headers=None, timeout=20):
    req = urllib.request.Request(url, headers={**UA, **(headers or {})})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read().decode("utf-8", errors="ignore")


def http_get_json(url, headers=None, timeout=20):
    return json.loads(http_get(url, headers, timeout))


def gh_headers():
    h = {"Accept": "application/vnd.github+json"}
    if GITHUB_TOKEN:
        h["Authorization"] = f"Bearer {GITHUB_TOKEN}"
    return h


# --------------------------------------------------------------------------
# 1a/1b — arXiv taraması + paper-kod eşleştirmesi
# --------------------------------------------------------------------------
def fetch_arxiv_category(cat, target_date, max_results=100):
    url = (f"http://export.arxiv.org/api/query?search_query=cat:{cat}"
           f"&sortBy=submittedDate&sortOrder=descending&max_results={max_results}")
    try:
        raw = http_get(url)
    except Exception as e:
        print(f"[arXiv] {cat} hata: {e}", file=sys.stderr)
        return []
    ns = {"a": "http://www.w3.org/2005/Atom"}
    root = ET.fromstring(raw)
    out = []
    for entry in root.findall("a:entry", ns):
        published = entry.findtext("a:published", default="", namespaces=ns)
        try:
            pub_date = datetime.datetime.strptime(published[:10], "%Y-%m-%d").date()
        except ValueError:
            continue
        if pub_date != target_date:
            continue
        title = (entry.findtext("a:title", default="", namespaces=ns) or "").strip().replace("\n", " ")
        summary = (entry.findtext("a:summary", default="", namespaces=ns) or "").strip().replace("\n", " ")
        link = entry.findtext("a:id", default="", namespaces=ns)
        authors = [a.findtext("a:name", default="", namespaces=ns) for a in entry.findall("a:author", ns)]
        out.append({
            "source": "arxiv", "category": cat, "title": title, "summary": summary,
            "url": link, "authors": authors, "date": str(pub_date),
        })
    return out


def fetch_all_arxiv(target_date):
    papers, seen = [], set()
    for cat in ARXIV_CATEGORIES:
        for p in fetch_arxiv_category(cat, target_date):
            key = p["title"].lower().strip()
            if key in seen:
                continue
            seen.add(key)
            papers.append(p)
        time.sleep(1)  # arXiv nezaket aralığı
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
    urls = set()
    today = datetime.date.today()
    if not os.path.isdir(DIGESTS_DIR):
        return urls
    for i in range(1, days + 1):
        d = today - datetime.timedelta(days=i)
        path = os.path.join(DIGESTS_DIR, f"radar-{d}.md")
        if os.path.exists(path):
            with open(path, encoding="utf-8") as f:
                urls.update(re.findall(r"https?://\S+", f.read()))
    return urls


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
    pnl_satiri = (
        "İddia edilen sonuç: metinde somut Sharpe/PnL/getiri iddiası tespit edildi — "
        "**doğrulanmamış, kaynağı paper içinde kontrol et.**\n"
        if pnl_flag else
        "İddia edilen sonuç: [KAYNAK YOK] (metinde somut performans sayısı bulunamadı).\n"
    )
    return (
        f"### [Skor {score} · {t}] {p['title']}\n"
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


def build_digest(target_date, papers_scored, repos_scored, hn, reddit_q, reddit_algo, quantocracy):
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

    lines.append(
        "---\n*Bu digest kod-tabanlı otomatik taramanın çıktısıdır; yatırım tavsiyesi değildir, "
        "işlem önerisi içermez. Raporlanan tüm sayılar iddia olarak sunulmuştur, doğrulama gerektirir.*"
    )
    return "\n".join(lines)


# --------------------------------------------------------------------------
# main
# --------------------------------------------------------------------------
def main():
    if len(sys.argv) > 1:
        target_date = datetime.datetime.strptime(sys.argv[1], "%Y-%m-%d").date()
    else:
        target_date = datetime.datetime.utcnow().date() - datetime.timedelta(days=1)

    print(f"Quant Radar taraması başlıyor — hedef tarih: {target_date}")

    recent_urls = load_recent_urls()

    papers = fetch_all_arxiv(target_date)
    print(f"  arXiv: {len(papers)} paper bulundu")

    papers_scored = []
    for p in papers:
        if p["url"] in recent_urls:
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
        time.sleep(0.5)

    repos = fetch_new_repos(target_date)
    print(f"  GitHub: {len(repos)} yeni/hareketli repo bulundu (bağımsız tarama — paper eşleşmesiyle sınırlı değil)")

    # Yıldıza göre sırala; README derinlemesine taramasını en umut vaat eden N repoya uygula (API bütçesi için).
    repos.sort(key=lambda r: r.get("stargazers_count", 0), reverse=True)
    README_SCAN_LIMIT = 25

    repos_scored = []
    for idx, r in enumerate(repos):
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

    hn = fetch_hn(target_date)
    reddit_q = fetch_reddit("quant", target_date)
    reddit_algo = fetch_reddit("algotrading", target_date)
    quantocracy = fetch_quantocracy()

    digest = build_digest(target_date, papers_scored, repos_scored, hn, reddit_q, reddit_algo, quantocracy)

    os.makedirs(DIGESTS_DIR, exist_ok=True)
    out_path = os.path.join(DIGESTS_DIR, f"radar-{target_date}.md")
    with open(out_path, "w", encoding="utf-8") as f:
        f.write(digest)
    print(f"Digest yazıldı: {out_path}")


if __name__ == "__main__":
    main()
