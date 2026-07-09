#!/usr/bin/env python3
"""Digest'i mail olarak gönderir. Kullanım: python3 scripts/send_digest.py digests/radar-YYYY-MM-DD.md

Ortam değişkenleri: MAIL_USERNAME (Gmail), MAIL_APP_PASSWORD (uygulama şifresi),
MAIL_TO (virgülle çok alıcı; boşsa MAIL_USERNAME).
Aynı isimli .html dosyası varsa onu HTML gövde olarak kullanır; yoksa markdown'dan sade HTML üretir.
"""
import os
import sys
import smtplib
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

path = sys.argv[1]
user = os.environ["MAIL_USERNAME"]
pw = os.environ["MAIL_APP_PASSWORD"]
to_raw = os.environ.get("MAIL_TO") or user
to_list = [a.strip() for a in to_raw.split(",") if a.strip()]

with open(path, encoding="utf-8") as f:
    md = f.read().strip()
if not md:
    print("Digest boş, gönderilmiyor.")
    sys.exit(0)

html_sibling = path.rsplit(".", 1)[0] + ".html"
if os.path.exists(html_sibling):
    with open(html_sibling, encoding="utf-8") as f:
        html = f.read()
else:
    try:
        import markdown  # yoksa: pip install markdown
        body = markdown.markdown(md, extensions=["tables", "fenced_code"])
    except ImportError:
        body = "<pre style='white-space:pre-wrap'>" + (
            md.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")) + "</pre>"
    html = (f'<div style="font-family:Georgia,serif;max-width:760px;margin:auto;'
            f'line-height:1.55;color:#1a1a1a">{body}</div>')

date = os.path.basename(path).replace("radar-", "").replace(".md", "")

# Her alıcıya AYRI mail — To alanında sadece kendi adresi görünür.
with smtplib.SMTP_SSL("smtp.gmail.com", 465) as s:
    s.login(user, pw)
    for recipient in to_list:
        msg = MIMEMultipart("alternative")
        msg["Subject"] = f"🔭 Quant Radar — {date}"
        msg["From"] = user
        msg["To"] = recipient
        msg.attach(MIMEText(md, "plain", "utf-8"))
        msg.attach(MIMEText(html, "html", "utf-8"))
        s.sendmail(user, [recipient], msg.as_string())
        print("Mail gönderildi:", recipient)
