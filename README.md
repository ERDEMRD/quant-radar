# quant-radar

Her sabah 06:00'da çalışan otonom quant araştırma radarı. Dünün arXiv/SSRN paper'larını,
yeni GitHub repolarını ve topluluk sinyallerini tarar; paper+kod kombinasyonu ve PnL kanıtı
öncelikli **RADAR skoru** ile ranklar; digest'i mail atar, en iyi bulguları Obsidian
vault'una not düşer.

- Protokol: [PROTOCOL.md](PROTOCOL.md)
- Digest arşivi: [digests/](digests/)
- Mail: `.github/workflows/mail-digest.yml` — `digests/` push'unda tetiklenir.

## Kurulum (bir kez)

1. GitHub'da boş `quant-radar` reposu aç. ✅ (**bu repo private olmalı** — Settings → General →
   Danger Zone → Change visibility, eğer public ise hemen private'a çevir.)
2. Repo Settings → Secrets and variables → Actions, üç secret ekle (değerler burada, README'de
   YAZILMAZ — sadece GitHub'ın şifreli secret deposunda tutulur):
   - `MAIL_USERNAME` — gönderen Gmail adresin
   - `MAIL_APP_PASSWORD` — bu adres için üretilen Gmail **uygulama şifresi** (tek kullanımlık/app password;
     normal Gmail şifresi değil — myaccount.google.com/apppasswords adresinden üretilir)
   - `MAIL_TO` — alıcı adres(ler), virgülle ayrılmış (her biri ayrı mail alır, birbirini görmez)
3. GitHub'dan `repo` yetkili bir Personal Access Token üret ve bu klasördeki
   `config/github_token.txt` dosyasına yapıştır (dosya .gitignore'da, asla push edilmez).
   Not: Günlük tarama (`radar-scan.yml`) kendi push'u için Actions'ın otomatik verdiği
   `GITHUB_TOKEN`'ı kullanır — bunun için ayrıca secret eklemen gerekmiyor.
4. `config/config.json` içindeki `remote` alanına repo adresini yaz.
5. İlk push'u yap (ya da Cowork'te "quant-radar'ı push et" de).

Günlük akış: `radar-scan.yml` her sabah 06:00 TR'de `scripts/fetch_radar.py`'yi çalıştırır
(arXiv + GitHub Search API + HN/Reddit/Quantocracy'yi kod ile deterministik tarar, RADAR
skoru hesaplar) → `digests/radar-YYYY-MM-DD.md` yazar → commit+push eder → bu push
`mail-digest.yml`'i tetikler → digest HTML mail olarak iki adrese gider.
