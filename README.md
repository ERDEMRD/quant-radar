# quant-radar

Her sabah 07:00'de çalışan otonom quant araştırma radarı. Dünün arXiv/SSRN paper'larını,
yeni GitHub repolarını ve topluluk sinyallerini tarar; paper+kod kombinasyonu ve PnL kanıtı
öncelikli **RADAR skoru** ile ranklar; digest'i mail atar, en iyi bulguları Obsidian
vault'una not düşer.

- Protokol: [PROTOCOL.md](PROTOCOL.md)
- Digest arşivi: [digests/](digests/)
- Mail: `.github/workflows/mail-digest.yml` — `digests/` push'unda tetiklenir.

## Kurulum (bir kez)

1. GitHub'da boş `quant-radar` reposu aç.
2. Repo Settings → Secrets and variables → Actions, üç secret ekle:
   - `MAIL_USERNAME` — Gmail adresin
   - `MAIL_APP_PASSWORD` — Gmail uygulama şifresi
   - `MAIL_TO` — alıcı adres (boş bırakılabilir, o zaman MAIL_USERNAME'e gider)
3. GitHub'dan `repo` yetkili bir Personal Access Token üret ve bu klasördeki
   `config/github_token.txt` dosyasına yapıştır (dosya .gitignore'da, asla push edilmez).
4. `config/config.json` içindeki `remote` alanına repo adresini yaz.
5. İlk push'u yap (ya da Cowork'te "quant-radar'ı push et" de).

Zamanlanmış görev her sabah: tarar → skorlar → `digests/radar-YYYY-MM-DD.md` yazar →
commit+push → Action maili gönderir → top bulgular `PAPERS/papers/Radar/`e Obsidian
notu olarak düşer.
