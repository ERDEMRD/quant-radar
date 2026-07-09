# Quant Radar — 2026-07-09

Dün (2026-07-09, UTC) taranan çıktılar: **1 paper**, **1 yeni/hareketli repo** (GitHub bağımsız da tarandı, sadece paper eşleşmesiyle sınırlı değil), **1 paper+kod eşleşmesi**, **2 tanınmış kurum/fon imzalı bulgu**, **2 somut PnL/Sharpe iddiası içeren bulgu**. Sıralama skora göre: kod eşleşmesi, büyük kurum/fon imzası (MIT, Stanford, Citadel, Two Sigma vb.) ve somut performans iddiası olan bulgular öne çıkarılır. Bu digest kod-tabanlı deterministik taramayla üretildi (arXiv + GitHub Search API + HN/Reddit/Quantocracy).

## 🏆 S-Tier

### [Skor 95 · S] A Novel Walk-Forward Alpha Factor Framework
**Tür:** Paper+Resmi Kod · **Paper:** https://arxiv.org/abs/2607.00001 · **Kod:** https://github.com/example/alpha-wf (⭐ 142, as-of 2026-07-09)
**Kurumsal sinyal:** MIT
**Amaç:** Bu calismada, gercekci islem maliyetleri altinda yol-boyu dogrulamali (walk-forward) bir alfa faktoru cercevesi oneriyoruz. Yontem, gunluk BIST verisi uzerinde egitim-test ayrimiyla ve coklu-test duzeltmesiyle degerlendirilmistir. **Yöntem:** Sonuclarda, ornek-disi (out-of-sample) Sharpe orani 1.8 olarak raporlanmis ve islem maliyeti dahil edildiginde bile getiri pozitif kalmistir. İddia edilen sonuç: metinde somut Sharpe/PnL/getiri iddiası tespit edildi — **doğrulanmamış, kaynağı paper içinde kontrol et.**
**Neden denemeye değer:** q-fin.TR kategorisinde kod ile birlikte yayınlanmış, tanınmış kurum/fon imzası var. **Dikkat:** PnL/Sharpe iddiaları paper içinde doğrulanmalı; bu satır otomatik taramadır.

## A-Tier

### [Skor 72 · A] citadel-research/microstructure-toolkit
**Tür:** Sadece Repo · **Repo:** https://github.com/citadel-research/microstructure-toolkit (⭐ 310, as-of 2026-07-09)
**Kurumsal sinyal:** Citadel
Market microstructure & order book simulation toolkit.
**README sinyalleri:** README bir paper'a referans veriyor, somut PnL/Sharpe iddiası var, backtest/walk-forward raporu var (doğrulanmamış).
**Neden denemeye değer:** Yeni açılmış quant repo. **Dikkat:** Backtest/PnL kanıtı bağımsız doğrulanmadı.

## 📡 Topluluk nabzı

**Hacker News:**
- (58 pts) [Show HN: open-source backtest engine](https://news.ycombinator.com/item?id=1)

**Reddit r/quant, r/algotrading:**
- (24 pts) [How do you handle regime shifts in factor models?](https://reddit.com/r/quant/1)

**Quantocracy:**
- [Weekly links roundup](https://quantocracy.com/x)

---
*Bu digest kod-tabanlı otomatik taramanın çıktısıdır; yatırım tavsiyesi değildir, işlem önerisi içermez. Raporlanan tüm sayılar iddia olarak sunulmuştur, doğrulama gerektirir.*