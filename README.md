# quant-radar (v2 — yerel mod)

Her sabah 07:00'de Cowork zamanlanmış görevi (`quant-radar-daily`) çalışır:
dünün arXiv/SSRN paper'larını, yeni GitHub repolarını ve topluluk sinyallerini
düşük token bütçesiyle (≤14 web çağrısı) tarar, **RADAR skoru** ile ranklar ve:

- Digest → `digests/radar-YYYY-MM-DD.md` (ranklı, limitsiz liste)
- Top ≤3 bulgu → `PAPERS/papers/Radar/` (Obsidian formatında, gerçek hub/tag eşleşmeli)
- Index → `PAPERS/papers/Radar.md`

**Mail ve GitHub push devre dışı** — her şey yerel klasöre yazılır.
Kurulum gerekmez; uygulama açıkken görev kendiliğinden koşar (kapalıysa ilk açılışta).

Protokol (kanonik): [PROTOCOL.md](PROTOCOL.md)

## Opsiyonel: mail'i sonradan açmak

`.github/workflows/` ve `scripts/` klasörleri duruyor — istenirse repo GitHub'a
push edilip secrets (`MAIL_USERNAME`, `MAIL_APP_PASSWORD`, `MAIL_TO`) eklenerek
digest'lerin mail olarak gitmesi tekrar aktifleştirilebilir. Şu an kullanılmıyor.

## Paylaşım

Sistemi başkasına vermek için `quant-radar` klasörünü zip'leyip göndermek yeterli;
alan kişi Cowork'te klasörü seçip "PROTOCOL.md'ye göre her sabah 07:00'de çalışan
scheduled task kur" der — sistem aynen kurulur.
