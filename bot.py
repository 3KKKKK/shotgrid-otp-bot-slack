import imaplib
import email
import re
import os
import logging
import json
import time
import urllib.request
from email.header import decode_header
from datetime import datetime, timezone, timedelta

# ─── Logging ────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger(__name__)

# Múi giờ Việt Nam (UTC+7)
VN_TZ = timezone(timedelta(hours=7))

# ─── Config từ Environment Variables ────────────────────────────────────────
GMAIL_USER         = os.environ["GMAIL_USER"]
GMAIL_APP_PASSWORD = os.environ["GMAIL_APP_PASSWORD"]
SLACK_WEBHOOK_URL  = os.environ["SLACK_WEBHOOK_URL"]

SHOTGRID_SENDER    = os.environ.get("SHOTGRID_SENDER", "noreply@signin.autodesk.com")
POLL_INTERVAL      = int(os.environ.get("POLL_INTERVAL_SECONDS", "30"))


# ─── Gmail IMAP Helper ───────────────────────────────────────────────────────

def get_email_body(msg):
    if msg.is_multipart():
        for part in msg.walk():
            if part.get_content_type() == "text/plain":
                charset = part.get_content_charset() or "utf-8"
                return part.get_payload(decode=True).decode(charset, errors="replace")
        for part in msg.walk():
            if part.get_content_type() == "text/html":
                charset = part.get_content_charset() or "utf-8"
                html = part.get_payload(decode=True).decode(charset, errors="replace")
                return re.sub(r"<[^>]+>", " ", html)
    else:
        charset = msg.get_content_charset() or "utf-8"
        return msg.get_payload(decode=True).decode(charset, errors="replace")
    return ""


def extract_otp(body):
    patterns = [
        r"(?:verification code|code|OTP|m[aã] x[aá]c minh)[^\d]*(\d{6})",
        r"\b(\d{6})\b",
    ]
    for pattern in patterns:
        match = re.search(pattern, body, re.IGNORECASE)
        if match:
            return match.group(1)
    return None


def fetch_new_otps():
    results = []
    try:
        mail = imaplib.IMAP4_SSL("imap.gmail.com", 993)
        mail.login(GMAIL_USER, GMAIL_APP_PASSWORD)
        mail.select("INBOX")

        status, data = mail.search(None, f'(UNSEEN FROM "{SHOTGRID_SENDER}")')
        if status != "OK" or not data[0]:
            mail.logout()
            return []

        msg_ids = data[0].split()
        log.info(f"Tìm thấy {len(msg_ids)} email OTP chưa đọc.")

        for msg_id in msg_ids:
            _, msg_data = mail.fetch(msg_id, "(RFC822)")
            msg = email.message_from_bytes(msg_data[0][1])

            subject_raw, encoding = decode_header(msg["Subject"])[0]
            if isinstance(subject_raw, bytes):
                subject = subject_raw.decode(encoding or "utf-8", errors="replace")
            else:
                subject = subject_raw or "(no subject)"

            date_str = msg.get("Date", "")
            try:
                from email.utils import parsedate_to_datetime
                dt_vn = parsedate_to_datetime(date_str).astimezone(VN_TZ)
                date_str = dt_vn.strftime("%H:%M:%S %d/%m/%Y (GMT+7)")
            except Exception:
                pass

            otp = extract_otp(get_email_body(msg))
            mail.store(msg_id, "+FLAGS", "\\Seen")

            if otp:
                results.append({"otp": otp, "subject": subject, "received_at": date_str})
                log.info(f"OTP: {otp} | {subject}")
            else:
                log.warning(f"Không tìm thấy OTP trong: {subject}")

        mail.close()
        mail.logout()

    except Exception as e:
        log.error(f"Lỗi Gmail: {e}")

    return results


# ─── Slack Webhook ───────────────────────────────────────────────────────────

def send_to_slack(otp_info):
    payload = {
        "blocks": [
            {
                "type": "header",
                "text": {"type": "plain_text", "text": "🔐 OTP ShotGrid mới"}
            },
            {
                "type": "section",
                "fields": [
                    {"type": "mrkdwn", "text": f"*Mã OTP:*\n```{otp_info['otp']}```"},
                    {"type": "mrkdwn", "text": f"*Nhận lúc:*\n{otp_info['received_at']}"},
                ]
            },
            {
                "type": "context",
                "elements": [
                    {"type": "mrkdwn", "text": f"📧 {otp_info['subject']}  •  ⏰ Hãy dùng ngay trước khi hết hạn!"}
                ]
            }
        ]
    }
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        SLACK_WEBHOOK_URL,
        data=data,
        headers={"Content-Type": "application/json"},
        method="POST"
    )
    with urllib.request.urlopen(req, timeout=10) as resp:
        log.info(f"Slack response: {resp.status} | OTP: {otp_info['otp']}")


# ─── Entry Point ─────────────────────────────────────────────────────────────
if __name__ == "__main__":
    required_env = ["GMAIL_USER", "GMAIL_APP_PASSWORD", "SLACK_WEBHOOK_URL"]
    missing = [k for k in required_env if not os.environ.get(k)]
    if missing:
        log.error(f"Thiếu environment variables: {', '.join(missing)}")
        raise SystemExit(1)

    log.info(f"Bot khởi động. Poll interval: {POLL_INTERVAL}s | Sender: {SHOTGRID_SENDER}")
    while True:
        try:
            log.info("Đang kiểm tra Gmail...")
            for otp_info in fetch_new_otps():
                send_to_slack(otp_info)
        except Exception as e:
            log.error(f"Lỗi vòng lặp chính: {e}")
        time.sleep(POLL_INTERVAL)
