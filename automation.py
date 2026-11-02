import base64
import datetime as dt
import html
import json
import os
import random
import re
import sys
import threading
import time
import warnings
from email.header import Header
from email.mime.text import MIMEText
from typing import Any, Dict, List, Optional, Tuple

warnings.filterwarnings("ignore", category=FutureWarning)
warnings.filterwarnings("ignore", category=UserWarning)

import gspread
import requests
from dotenv import load_dotenv
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from gspread.utils import rowcol_to_a1

load_dotenv()


# ==============================================================================
# Configuration
# ==============================================================================
def _flag(name: str, default: str) -> bool:
    return os.getenv(name, default).strip().lower() in ("1", "true", "yes")


SHEET_ID = (os.getenv("SOURCE_SHEET_ID", "").strip() or os.getenv("SHEET_ID", "").strip())
SOURCE_TAB = os.getenv("SOURCE_TAB", "Sheet1").strip()
CONTROL_TAB = os.getenv("CONTROL_TAB", "Control").strip()

SENDER_NAME = os.getenv("SENDER_NAME", "Akash").strip()
COMPANY = os.getenv("COMPANY", "Adople AI").strip()

# Dynamic Template
TEMPLATE_FILE = os.getenv("TEMPLATE_FILE", "template.txt").strip()

# Autonomous Scheduler & Safety Controls
SEND_TIME = os.getenv("SEND_TIME", "19:00").strip()
WATCH_INTERVAL_MINUTES = int(os.getenv("WATCH_INTERVAL_MINUTES", "5"))
DAILY_CAP = int(os.getenv("DAILY_CAP", "0"))  # 0 = Unlimited (sends all pending verified leads)
SEND_ON_START = _flag("SEND_ON_START", "0")

DRY_RUN = _flag("DRY_RUN", "1")
DELAY_MIN = int(os.getenv("DELAY_MIN", "60"))
DELAY_MAX = int(os.getenv("DELAY_MAX", "120"))
DELAY_RANGE = (min(DELAY_MIN, DELAY_MAX), max(DELAY_MIN, DELAY_MAX))
ALLOWED_EMAIL_STATUS = {
    s.strip().lower()
    for s in os.getenv("ALLOWED_EMAIL_STATUS", "verified").split(",")
    if s.strip()
}
SKIP_IF_ALREADY_REPLIED = _flag("SKIP_IF_ALREADY_REPLIED", "1")

USE_GEMINI = _flag("USE_GEMINI", "1")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY") or os.getenv("GOOGLE_API_KEY")
GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-2.5-flash").strip()

SERVICE_ACCOUNT_FILE = os.getenv("SERVICE_ACCOUNT_FILE", "service_account.json")
GMAIL_CREDENTIALS_FILE = os.getenv("GMAIL_CREDENTIALS_FILE", "credentials.json")
GMAIL_TOKEN_FILE = os.getenv("GMAIL_TOKEN_FILE", "token.json")
STATE_FILE = os.getenv("STATE_FILE", "outreach_state.json")

GMAIL_SCOPES = [
    "https://www.googleapis.com/auth/gmail.send",
    "https://www.googleapis.com/auth/gmail.readonly",
]

REPLY_INTENTS = {
    "INTERESTED", "MEETING_REQUESTED", "OUT_OF_OFFICE", "REFERRAL",
    "NOT_INTERESTED", "UNSUBSCRIBE", "OTHER", "BOUNCE",
}

FINAL_STATUSES = ("sent", "sending", "failed")

# Thread safety locks for parallel workers
sheet_lock = threading.Lock()
state_lock = threading.Lock()


def now() -> str:
    return dt.datetime.now().isoformat(timespec="seconds")


def _norm(s: str) -> str:
    return re.sub(r"[^a-z0-9]", "", str(s).lower())


def get_first_name(raw_name: str) -> str:
    if not raw_name:
        return "there"
    words = raw_name.strip().split()
    prefixes = {"dr.", "dr", "prof.", "prof", "mr.", "mr", "ms.", "ms", "mrs.", "mrs"}
    if words and words[0].lower() in prefixes and len(words) > 1:
        return words[1]
    return words[0] if words else "there"


class SafeDict(dict):
    def __missing__(self, key):
        return ""


def load_template() -> Tuple[str, str]:
    if os.path.exists(TEMPLATE_FILE):
        try:
            with open(TEMPLATE_FILE, "r", encoding="utf-8") as f:
                content = f.read().strip()
                if content:
                    lines = content.splitlines()
                    subject = "Nice to connect"
                    body_start_idx = 0
                    if lines and lines[0].lower().startswith("subject:"):
                        subject = lines[0].split(":", 1)[1].strip()
                        body_start_idx = 1
                    body = "\n".join(lines[body_start_idx:]).strip()
                    return subject, body
        except Exception as e:
            print(f"[Template Notice] Could not read {TEMPLATE_FILE}: {e}")

    fallback_subj = "Nice to connect - {conference_name}"
    fallback_body = (
        "Hi {first_name},\n\n"
        "This is {sender} from {sender_company}. Noticed your session as {job_title} at {company} for {conference_name}.\n\n"
        "Would be great to connect with you!\n\n"
        "Thank you,\n"
        "{sender}\n"
        "{sender_company}"
    )
    return fallback_subj, fallback_body


def build_email(
    speaker_name: str,
    job_title: str = "",
    company: str = "",
    conference_name: str = ""
) -> Tuple[str, str]:
    subj_tmpl, body_tmpl = load_template()
    first_name = get_first_name(speaker_name)
    context = SafeDict({
        "first_name": first_name,
        "speaker_name": speaker_name or "there",
        "job_title": job_title or "Speaker",
        "company": company or "your organization",
        "conference_name": conference_name or "the conference",
        "sender": SENDER_NAME,
        "sender_company": COMPANY,
        "company_name": company or "your organization",
    })
    subject = subj_tmpl.format_map(context)
    body = body_tmpl.format_map(context)
    return subject, body


# ==============================================================================
# Column mapping
# ==============================================================================
AGENT_ALIASES = {
    "outreach_status": ["outreach status", "outreach_status", "status"],
    "sent_message": ["sent message", "send message", "sent email", "outreach message", "email body"],
    "reply_status": ["reply status", "reply_status"],
    "reply_sentiment": ["reply sentiment", "reply_sentiment", "reply intent", "intent", "sentiment"],
    "received_reply": ["received reply", "replayed message", "replied message", "reply message", "reply text", "reply"],
    "thread_id": ["thread id", "thread_id", "gmail thread id"],
}
AGENT_HEADER_NAMES = {
    "outreach_status": "outreach status",
    "reply_status": "reply status",
    "reply_sentiment": "reply sentiment",
    "received_reply": "received reply",
    "sent_message": "sent message",
    "thread_id": "thread id",
}


class SheetColumnMap:
    def __init__(self, headers: List[str]):
        self.norm = [_norm(h) for h in headers]

        self.speaker_id = self._find(["speaker_id", "speakerid"], 0)
        self.conf_id = self._find(["conference_id", "conferenceid", "conf_id"], 1)
        self.conf_name = self._find(["conference_name", "conferencename", "conf_name"], 2)
        self.speaker_name = self._find(["speaker_name", "speakername", "full_name"], 3)
        self.job_title = self._find(["job_title", "jobtitle", "speaker_title", "title"], 4)
        self.company = self._find(["company_name", "companyname", "company"], 5)
        self.email = self._find(["professional_email", "professionalemail", "speaker_email", "email"], 8)
        self.verification_status = self._find(["verification_status", "verificationstatus", "email_status"], 11)

        for attr, aliases in AGENT_ALIASES.items():
            setattr(self, attr, self._find_exact(aliases))

    def _find(self, candidates: List[str], default_idx: int) -> int:
        cands = [_norm(c) for c in candidates]
        for cand in cands:
            for idx, h in enumerate(self.norm):
                if h == cand:
                    return idx
        for cand in cands:
            for idx, h in enumerate(self.norm):
                if h and cand in h:
                    return idx
        return default_idx

    def _find_exact(self, aliases: List[str]) -> Optional[int]:
        for alias in aliases:
            a = _norm(alias)
            for idx, h in enumerate(self.norm):
                if h == a:
                    return idx
        for alias in aliases:
            a = _norm(alias)
            for idx, h in enumerate(self.norm):
                if h and (a in h or h in a):
                    return idx
        return None

    def missing_agent_columns(self) -> List[str]:
        return [a for a in AGENT_ALIASES if getattr(self, a) is None]


def g(cells: List[str], idx: Optional[int]) -> str:
    if idx is None or idx >= len(cells):
        return ""
    return str(cells[idx]).strip()


# ==============================================================================
# Local state (crash-safe atomic write & thread-safe)
# ==============================================================================
def load_state() -> Dict[str, Any]:
    with state_lock:
        if os.path.exists(STATE_FILE):
            try:
                with open(STATE_FILE, "r", encoding="utf-8") as f:
                    content = f.read().strip()
                    if content:
                        return json.loads(content)
            except Exception as e:
                print(f"[State Notice] Could not read {STATE_FILE}: {e}")
        return {"threads": {}, "sent_log": []}


def save_state(state: Dict[str, Any]):
    with state_lock:
        try:
            tmp_file = f"{STATE_FILE}.tmp"
            with open(tmp_file, "w", encoding="utf-8") as f:
                json.dump(state, f, indent=2)
            os.replace(tmp_file, STATE_FILE)
        except Exception as e:
            print(f"[State Warning] Could not save {STATE_FILE}: {e}")


def get_sent_today(state: Dict[str, Any]) -> int:
    today = dt.date.today().isoformat()
    return sum(
        1 for item in state.get("sent_log", [])
        if str(item.get("sent_at", "")).startswith(today) and item.get("status") == "sent"
    )


def record_send_in_state(state, dedupe_key, email, conf_tag, thread_id, message_id, speaker_id):
    state.setdefault("sent_log", []).append(
        {"email": email, "conference": conf_tag, "sent_at": now(), "status": "sent"}
    )
    state.setdefault("threads", {})[dedupe_key] = {
        "thread_id": thread_id,
        "message_id": message_id,
        "sent_at": now(),
        "speaker_id": speaker_id,
        "last_msg_count": 1,
    }
    save_state(state)


# ==============================================================================
# Gemini - reply intent only
# ==============================================================================
class GeminiAgent:
    def __init__(self):
        self.client = None
        if USE_GEMINI and GEMINI_API_KEY:
            try:
                from google import genai
                self.client = genai.Client(api_key=GEMINI_API_KEY)
            except Exception as e:
                print(f"[Gemini Agent] Client init notice: {e}. Using rule-based fallback.\n"
                      f"                Fix: pip install -U google-genai")

    def is_available(self) -> bool:
        return self.client is not None

    def analyze_reply(self, reply_text: str, name: str, company: str, conf: str) -> Dict[str, Any]:
        if not self.is_available() or not reply_text.strip():
            return fallback_reply_analysis(reply_text)
        try:
            from google.genai import types

            prompt = f"""
You classify a reply from a conference speaker to our short outreach email.
Our email only said hello / nice to connect around {conf}.
Speaker: {name} ({company}).

Reply text:
\"\"\"
{reply_text[:2000]}
\"\"\"

Choose exactly one "intent":
- INTERESTED: positive, open to chatting or connecting.
- MEETING_REQUESTED: asks for a call, meeting time, or shares/asks for a calendar link.
- OUT_OF_OFFICE: automated away message or temporary absence.
- REFERRAL: points us to someone else.
- NOT_INTERESTED: polite decline or not relevant.
- UNSUBSCRIBE: asks to stop emailing / be removed.
- OTHER: only if truly unclear or unrelated (for example just "got it").

Guidance:
- Warm or friendly replies that engage with us ("pleasure to meet you", "nice to meet you",
  "likewise", "good to connect") count as INTERESTED.
- Asking how to reach us, for our contact details, or for a time or call counts as MEETING_REQUESTED.
- A clear "no", "not interested" or "not now" counts as NOT_INTERESTED.
- Judge only the reply text above.

Return ONLY valid JSON with exactly these keys:
{{"intent": "<one of the above>", "sentiment": "POSITIVE" | "NEUTRAL" | "NEGATIVE", "summary": "<one short sentence>"}}
"""
            config = types.GenerateContentConfig(response_mime_type="application/json", temperature=0.0)
            response = None
            for attempt in range(2):
                try:
                    response = self.client.models.generate_content(
                        model=GEMINI_MODEL, contents=prompt, config=config
                    )
                    break
                except Exception:
                    if attempt == 1:
                        raise
                    time.sleep(4)
            data = json.loads(response.text.strip())
            intent = str(data.get("intent", "OTHER")).upper().strip()
            if intent not in REPLY_INTENTS:
                intent = "OTHER"
            sentiment = str(data.get("sentiment", "NEUTRAL")).upper().strip()
            if sentiment not in ("POSITIVE", "NEUTRAL", "NEGATIVE"):
                sentiment = "NEUTRAL"
            return {
                "intent": intent,
                "sentiment": sentiment,
                "summary": str(data.get("summary", "")).strip() or reply_text[:150],
            }
        except Exception as e:
            print(f"[Gemini Agent] Reply analysis notice: {e}. Using rule-based fallback.")
            return fallback_reply_analysis(reply_text)


ai_agent = GeminiAgent()


def fallback_reply_analysis(reply_text: str) -> Dict[str, Any]:
    lower = (reply_text or "").lower()
    ooo = ["out of the office", "out of office", "auto-reply", "away from my desk", "on leave", "vacation"]
    unsub = ["unsubscribe", "remove me", "stop emailing", "take me off"]
    decline = ["not interested", "no thank you", "no thanks", "pass at this time", "not right now"]
    meeting = [
        "calendly", "calendar", "schedule a time", "book a call", "meet with you", "send a link",
        "love to chat", "what time", "available", "let me know when", "jump on a call", "grab coffee",
        "how to reach", "reach you", "contact you", "your number", "your email",
    ]
    positive = [
        "interested", "sounds interesting", "happy to chat", "let's connect", "open to",
        "sounds good", "happy to discuss", "fine lets connect", "fine let's connect",
        "pleasure to meet", "nice to meet", "nice to connect", "good to connect", "likewise",
    ]
    referral = ["reach out to", "looping in", "cc'ing", "contact my colleague", "better person to talk"]

    if any(k in lower for k in ooo):
        intent, sentiment = "OUT_OF_OFFICE", "NEUTRAL"
    elif any(k in lower for k in unsub):
        intent, sentiment = "UNSUBSCRIBE", "NEGATIVE"
    elif any(k in lower for k in decline):
        intent, sentiment = "NOT_INTERESTED", "NEGATIVE"
    elif any(k in lower for k in meeting):
        intent, sentiment = "MEETING_REQUESTED", "POSITIVE"
    elif any(k in lower for k in referral):
        intent, sentiment = "REFERRAL", "POSITIVE"
    elif any(k in lower for k in positive):
        intent, sentiment = "INTERESTED", "POSITIVE"
    else:
        intent, sentiment = "OTHER", "NEUTRAL"
    return {"intent": intent, "sentiment": sentiment, "summary": (reply_text or "")[:140].strip().replace("\n", " ")}


# ==============================================================================
# Google connections and safe writes
# ==============================================================================
def gmail():
    creds = None
    if os.path.exists(GMAIL_TOKEN_FILE):
        try:
            creds = Credentials.from_authorized_user_file(GMAIL_TOKEN_FILE, GMAIL_SCOPES)
        except Exception as e:
            print(f"[Gmail Auth] Token file notice: {e}")

    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            if not os.path.exists(GMAIL_CREDENTIALS_FILE):
                raise FileNotFoundError(f"Gmail OAuth client file '{GMAIL_CREDENTIALS_FILE}' not found.")
            flow = InstalledAppFlow.from_client_secrets_file(GMAIL_CREDENTIALS_FILE, GMAIL_SCOPES)
            creds = flow.run_local_server(port=8080)
        with open(GMAIL_TOKEN_FILE, "w", encoding="utf-8") as f:
            f.write(creds.to_json())

    return build("gmail", "v1", credentials=creds)


def sheets():
    if not SHEET_ID or SHEET_ID == "your_google_sheet_id":
        raise ValueError("SHEET_ID / SOURCE_SHEET_ID is not configured in .env.")
    if not os.path.exists(SERVICE_ACCOUNT_FILE):
        raise FileNotFoundError(f"Service account file '{SERVICE_ACCOUNT_FILE}' not found.")
    gc = gspread.service_account(filename=SERVICE_ACCOUNT_FILE)
    sh = gc.open_by_key(SHEET_ID)
    try:
        ws = sh.worksheet(SOURCE_TAB)
    except gspread.WorksheetNotFound:
        ws = sh.get_worksheet(0)
    return sh, ws


def ensure_columns(ws, create: bool = True) -> SheetColumnMap:
    with sheet_lock:
        headers = [str(h) for h in ws.row_values(1)]
        if not headers:
            raise ValueError("Row 1 (the header row) is empty.")
        cmap = SheetColumnMap(headers)
        missing = cmap.missing_agent_columns()
        if not missing:
            return cmap
        names = [AGENT_HEADER_NAMES[a] for a in missing]
        if not create:
            print(f"[Sheet Notice] Missing columns (will be added on a live run): {', '.join(names)}")
            return cmap
        new_headers = headers + names
        if ws.col_count < len(new_headers):
            ws.add_cols(len(new_headers) - ws.col_count)
        ws.update(range_name=f"A1:{rowcol_to_a1(1, len(new_headers))}", values=[new_headers])
        print(f"✅ Added missing columns to row 1: {', '.join(names)}")
        return SheetColumnMap(new_headers)


def paused(sh) -> bool:
    try:
        return str(sh.worksheet(CONTROL_TAB).acell("B1").value).strip().upper() == "TRUE"
    except Exception:
        return False


def snapshot_rows(all_values: List[List[str]], cmap: SheetColumnMap) -> List[Dict[str, Any]]:
    width = max(len(all_values[0]), 30)
    seen: Dict[str, int] = {}
    rows = []
    for i, raw in enumerate(all_values[1:]):
        cells = [str(c) for c in raw] + [""] * (width - len(raw))
        sid = cells[cmap.speaker_id].strip()
        occ = seen.get(sid, 0)
        seen[sid] = occ + 1
        if not sid and not g(cells, cmap.speaker_name) and not g(cells, cmap.email):
            continue
        rows.append({"row_num": i + 2, "sid": sid, "occ": occ, "c": cells})
    return rows


def _cell(v: Any) -> str:
    s = "" if v is None else str(v)
    return (" " + s) if s[:1] in ("=", "+", "@") else s


def write_rows(ws, cmap: SheetColumnMap, items: List[Dict[str, Any]]) -> bool:
    if not items:
        return True
    with sheet_lock:
        try:
            ids = [str(v).strip() for v in ws.col_values(cmap.speaker_id + 1)]
        except Exception as e:
            print(f"[Sheets Warning] Could not verify rows before writing: {e}")
            return False

        data = []
        for it in items:
            n, sid = it["row_num"], it["sid"]
            if n - 1 < len(ids) and ids[n - 1] == sid:
                row = n
            else:
                positions = [i + 1 for i, v in enumerate(ids) if v == sid]
                if sid and it["occ"] < len(positions):
                    row = positions[it["occ"]]
                else:
                    print(f"[Sheets Warning] Row for {sid or '(blank id)'} moved or was deleted - skipped this write.")
                    continue
            for col, val in it["updates"].items():
                if col is None:
                    continue
                data.append({"range": rowcol_to_a1(row, col + 1), "values": [[_cell(val)]]})
        if not data:
            return False
        for attempt in range(2):
            try:
                ws.batch_update(data)
                return True
            except Exception as e:
                if attempt == 1:
                    print(f"[Sheets Warning] Write failed: {e}")
                    return False
                time.sleep(2)
        return False


def item(row: Dict[str, Any], updates: Dict[Optional[int], Any]) -> Dict[str, Any]:
    return {"row_num": row["row_num"], "sid": row["sid"], "occ": row["occ"], "updates": updates}


# ==============================================================================
# Sending and alerts
# ==============================================================================
def send_mail(svc, to_email: str, subject: str, body: str) -> Tuple[str, str]:
    msg = MIMEText(body, "plain", "utf-8")
    msg["to"] = to_email
    msg["subject"] = Header(subject, "utf-8")
    raw = base64.urlsafe_b64encode(msg.as_bytes()).decode("utf-8")
    res = svc.users().messages().send(userId="me", body={"raw": raw}).execute()
    return res["id"], res["threadId"]


def alert(text: str):
    divider = "=" * 65
    print(f"\n{divider}\n🚨 OUTREACH ALERT | {now()}\n{divider}\n{text}\n{divider}\n")
    if TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID:
        url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
        try:
            r = requests.post(url, json={"chat_id": TELEGRAM_CHAT_ID, "text": text, "parse_mode": "Markdown"}, timeout=15)
            if r.status_code != 200:
                raise RuntimeError(r.text[:120])
        except Exception as e:
            try:
                requests.post(url, json={"chat_id": TELEGRAM_CHAT_ID, "text": text}, timeout=15)
            except Exception as tg_err:
                print(f"[Telegram Alert Error] {tg_err} (first error: {e})")


# ==============================================================================
# Workflow 1: send pass
# ==============================================================================
def send_batch():
    print(f"\n[{now()}] --- Starting Outreach Send Pass ---")
    if DRY_RUN:
        print("⚡ [DRY RUN ACTIVE] Emails are previewed only. Nothing is sent or written.")

    try:
        sh, ws = sheets()
        cmap = ensure_columns(ws, create=not DRY_RUN)
    except Exception as e:
        print(f"[Error] Failed to connect to Google Sheets: {e}")
        return

    if paused(sh):
        print("⏸️ Paused via Control!B1 (TRUE). Halting sending.")
        return

    svc = None
    if not DRY_RUN:
        try:
            svc = gmail()
        except Exception as e:
            print(f"[Error] Failed to connect to Gmail: {e}")
            return

    try:
        with sheet_lock:
            all_values = ws.get_all_values()
    except Exception as e:
        print(f"[Error] Failed to read sheet '{ws.title}': {e}")
        return
    if len(all_values) < 2:
        print(f"No data rows found in sheet '{ws.title}'.")
        return

    rows = snapshot_rows(all_values, cmap)
    state = load_state()
    sent_today = get_sent_today(state)
    cap_str = str(DAILY_CAP) if DAILY_CAP > 0 else "Unlimited"
    print(f"📊 Daily progress: {sent_today}/{cap_str} emails sent today. Rows in sheet: {len(rows)}")

    sent_pairs: set = set()
    replied_emails: set = set()
    for r in rows:
        c = r["c"]
        email = g(c, cmap.email).lower()
        status = g(c, cmap.outreach_status).lower()
        if email and status in ("sent", "sending"):
            if g(c, cmap.conf_id):
                sent_pairs.add((email, g(c, cmap.conf_id).lower()))
            if g(c, cmap.conf_name):
                sent_pairs.add((email, g(c, cmap.conf_name).lower()))
        if email and g(c, cmap.reply_status).lower() == "replied":
            replied_emails.add(email)

    pending: List[Dict[str, Any]] = []

    def flush():
        if pending and not DRY_RUN:
            write_rows(ws, cmap, pending)
        pending.clear()

    handled = skipped = sent_pass = 0

    try:
        for row in rows:
            c = row["c"]
            sid = row["sid"]
            conf_id, conf_name = g(c, cmap.conf_id), g(c, cmap.conf_name)
            name, title, company = g(c, cmap.speaker_name), g(c, cmap.job_title), g(c, cmap.company)
            email = g(c, cmap.email).lower()
            estatus = g(c, cmap.verification_status).lower()
            current = g(c, cmap.outreach_status).lower()

            if current in FINAL_STATUSES:
                handled += 1
                continue

            conf_tag = conf_id.lower() if conf_id else conf_name.lower()
            dedupe_key = f"{email}|{conf_tag}" if email else sid.lower()

            is_dup = False
            if email:
                if conf_id and (email, conf_id.lower()) in sent_pairs:
                    is_dup = True
                elif conf_name and (email, conf_name.lower()) in sent_pairs:
                    is_dup = True

            skip = None
            if is_dup:
                skip = "skipped_duplicate"
            elif not email:
                skip = "skipped_no_email"
            elif estatus not in ALLOWED_EMAIL_STATUS:
                skip = "skipped_unverified"
            elif SKIP_IF_ALREADY_REPLIED and email in replied_emails:
                skip = "skipped_replied"

            if skip:
                if current == skip:
                    continue
                skipped += 1
                print(f"⏩ {skip}: {name or sid} ({conf_name}) <{email or 'no email'}>")
                pending.append(item(row, {cmap.outreach_status: skip}))
                continue

            if DAILY_CAP > 0 and sent_today >= DAILY_CAP:
                print(f"🛑 Daily cap ({DAILY_CAP}) reached. Stopping send pass.")
                break
            if paused(sh):
                print("⏸️ Paused via Control!B1 during the pass. Halting.")
                break

            subject, body = build_email(name, title, company, conf_name)
            full_message = f"Subject: {subject}\n\n{body}"

            if DRY_RUN:
                print("\n--- [DRY RUN PREVIEW] ---")
                print(f"Row: {row['row_num']}\nTo: {name} <{email}>\nConference: {conf_name}")
                print(f"Subject: {subject}\nBody:\n{body}")
                print("-------------------------\n")
                if conf_id:
                    sent_pairs.add((email, conf_id.lower()))
                if conf_name:
                    sent_pairs.add((email, conf_name.lower()))
                sent_today += 1
                sent_pass += 1
                continue

            flush()

            # Record 'sending' BEFORE dispatching
            if not write_rows(ws, cmap, [item(row, {cmap.outreach_status: "sending", cmap.sent_message: full_message})]):
                print(f"❌ Could not record 'sending' for {name} - NOT sending. Stopping this pass.")
                break

            try:
                msg_id, thread_id = send_mail(svc, email, subject, body)
            except Exception as e:
                print(f"❌ FAILED to send to {email}: {e}")
                write_rows(ws, cmap, [item(row, {cmap.outreach_status: "failed"})])
                time.sleep(3)
                continue

            record_send_in_state(state, dedupe_key, email, conf_tag, thread_id, msg_id, sid)
            if conf_id:
                sent_pairs.add((email, conf_id.lower()))
            if conf_name:
                sent_pairs.add((email, conf_name.lower()))
            sent_today += 1
            sent_pass += 1
            print(f"✅ SENT: {name} <{email}> ({conf_name}) | Today: {sent_today}/{cap_str}")

            if not write_rows(ws, cmap, [item(row, {
                cmap.outreach_status: "sent", cmap.sent_message: full_message, cmap.thread_id: thread_id,
            })]):
                print(f"⚠️ Email was SENT but the sheet update failed. Row stays 'sending'. "
                      f"thread_id={thread_id} (saved in {STATE_FILE})")

            delay = random.randint(*DELAY_RANGE)
            print(f"⏳ Waiting {delay}s before next send...")
            time.sleep(delay)
    finally:
        flush()

    print(f"[{now()}] --- Send Pass Completed (sent: {sent_pass} | skipped: {skipped} | "
          f"already handled: {handled}) ---\n")


# ==============================================================================
# Workflow 2: reply watcher & full conversation transcript
# ==============================================================================
def decode_payload_part(part: Dict[str, Any]) -> str:
    data = part.get("body", {}).get("data", "")
    if not data:
        return ""
    data = data.replace("-", "+").replace("_", "/")
    if len(data) % 4:
        data += "=" * (4 - len(data) % 4)
    try:
        return base64.b64decode(data).decode("utf-8", errors="replace")
    except Exception:
        return ""


def clean_html(html_text: str) -> str:
    text = re.sub(r"<(script|style).*?</\1>", "", html_text, flags=re.IGNORECASE | re.DOTALL)
    text = re.sub(r"<br\s*/?>", "\n", text, flags=re.IGNORECASE)
    text = re.sub(r"</p>", "\n\n", text, flags=re.IGNORECASE)
    text = re.sub(r"<[^>]+>", "", text)
    text = html.unescape(text)
    return "\n".join(line.strip() for line in text.splitlines() if line.strip())


def extract_body_text(msg: Dict[str, Any]) -> str:
    plain_text = ""
    html_text = ""

    def walk(part: Dict[str, Any]):
        nonlocal plain_text, html_text
        mime = part.get("mimeType", "").lower()
        if mime == "text/plain" and not plain_text:
            plain_text = decode_payload_part(part)
        elif mime == "text/html" and not html_text:
            html_text = decode_payload_part(part)
        for sub in part.get("parts", []) or []:
            walk(sub)

    walk(msg.get("payload", {}))
    raw = plain_text or (clean_html(html_text) if html_text else "") or msg.get("snippet", "")
    if not raw:
        return ""

    # Normalize unicode non-breaking spaces common in Gmail timestamps
    raw = raw.replace("\u202f", " ").replace("\xa0", " ")

    # Strip multi-line reply headers (e.g. 'On Mon, ... wrote:')
    patterns = [
        r"(?:\r?\n|^)\s*On\s+[\s\S]+?wrote\s*:",
        r"(?:\r?\n|^)\s*[-]+\s*Original Message\s*[-]+",
        r"(?:\r?\n|^)\s*_{5,}",
        r"(?:\r?\n|^)\s*From:\s+[\s\S]+?Sent:\s+",
    ]
    for pat in patterns:
        parts = re.split(pat, raw, flags=re.IGNORECASE)
        if parts and parts[0].strip():
            raw = parts[0]
            break

    # Strip quoted lines starting with '>'
    lines = [line for line in raw.splitlines() if not line.strip().startswith(">")]
    return "\n".join(lines).strip()


def get_msg_timestamp(msg: Dict[str, Any]) -> str:
    raw_ms = msg.get("internalDate")
    if raw_ms:
        try:
            return dt.datetime.fromtimestamp(int(raw_ms) / 1000).strftime("%Y-%m-%d %H:%M")
        except Exception:
            pass
    for h in msg.get("payload", {}).get("headers", []):
        if h.get("name", "").lower() == "date":
            return h.get("value", "")[:25]
    return now()[:16]


def build_conversation_transcript(
    messages: List[Dict[str, Any]],
    my_emails: set,
    speaker_name: str,
    speaker_email: str
) -> str:
    """Builds a neat, chronological back-and-forth transcript of all messages in the thread."""
    blocks = []
    for msg in messages:
        headers = {h["name"].lower(): h["value"] for h in msg.get("payload", {}).get("headers", [])}
        sender_raw = headers.get("from", "")
        subj = headers.get("subject", "")

        is_bounce = (
            "mailer-daemon" in sender_raw.lower() or "postmaster" in sender_raw.lower()
            or "delivery status notification" in subj.lower()
        )
        is_from_us = any(me and me in sender_raw.lower() for me in my_emails)
        timestamp = get_msg_timestamp(msg)

        if is_bounce:
            sender_label = "System (Bounce Notification)"
        elif is_from_us:
            sender_label = f"You ({SENDER_NAME})"
        else:
            clean_from = re.sub(r"<[^>]+>", "", sender_raw).strip().strip('"').strip("'")
            sender_label = clean_from or speaker_name or speaker_email

        body = extract_body_text(msg)
        if not body:
            body = msg.get("snippet", "") or "(empty message)"

        blocks.append(f"[{timestamp}] {sender_label}:\n{body}")

    divider = "\n\n" + ("-" * 50) + "\n\n"
    transcript = divider.join(blocks)

    # Google Sheets max cell size is 50,000 characters
    if len(transcript) > 45000:
        transcript = "[...earlier messages truncated...]\n\n" + transcript[-44000:]

    return transcript


def watch():
    print(f"\n[{now()}] --- Starting Reply Watcher Pass ---")
    try:
        sh, ws = sheets()
        cmap = ensure_columns(ws, create=True)
        svc = gmail()
        my_email = svc.users().getProfile(userId="me").execute().get("emailAddress", "").strip().lower()
        with sheet_lock:
            all_values = ws.get_all_values()
    except Exception as e:
        print(f"[Error] Watcher setup failed: {e}")
        return
    if len(all_values) < 2:
        print("No data rows found in sheet.")
        return

    my_emails = {my_email}
    if os.getenv("SENDER_EMAIL", "").strip():
        my_emails.add(os.getenv("SENDER_EMAIL").strip().lower())

    rows = snapshot_rows(all_values, cmap)
    state = load_state()
    checked = replies_found = bounces_found = 0
    sent_threads = state.get("threads", {})

    print(f"📊 Scanning {len(rows)} leads in sheet for replies & conversation updates...")

    for row in rows:
        c = row["c"]
        outreach = g(c, cmap.outreach_status).lower()
        email = g(c, cmap.email).lower()
        conf_id, conf_name = g(c, cmap.conf_id), g(c, cmap.conf_name)
        name = g(c, cmap.speaker_name) or "Speaker"
        company = g(c, cmap.company)
        conf_tag = conf_id.lower() if conf_id else conf_name.lower()
        dedupe_key = f"{email}|{conf_tag}"

        if not email:
            continue

        is_sent = (
            outreach in ("sent", "sending")
            or dedupe_key in sent_threads
            or bool(g(c, cmap.thread_id))
        )
        if not is_sent:
            continue

        # Thread ID lookup
        thread_id = g(c, cmap.thread_id) or sent_threads.get(dedupe_key, {}).get("thread_id", "")
        if not thread_id:
            try:
                q_res = svc.users().threads().list(userId="me", q=f"{email}", maxResults=5).execute()
                th_list = q_res.get("threads", [])
                if th_list:
                    thread_id = th_list[0]["id"]
            except Exception as e:
                print(f"[Watch Notice] Gmail query for {email} failed: {e}")

        if not thread_id:
            continue

        checked += 1
        try:
            th = svc.users().threads().get(userId="me", id=thread_id, format="full").execute()
        except Exception as e:
            print(f"[Watch Warning] Could not fetch thread {thread_id} for {email}: {e}")
            continue

        messages = th.get("messages", [])
        if len(messages) <= 1:
            continue

        # Full chronological conversation transcript
        transcript = build_conversation_transcript(messages, my_emails, name, email)
        current_transcript = g(c, cmap.received_reply)

        # Identify all incoming external messages (replies from lead or delivery bounces)
        incoming = []
        for idx_msg, msg in enumerate(messages):
            headers = {h["name"].lower(): h["value"] for h in msg.get("payload", {}).get("headers", [])}
            sender = headers.get("from", "").lower()
            subj = headers.get("subject", "").lower()

            is_bounce = (
                "mailer-daemon" in sender or "postmaster" in sender
                or "delivery status notification" in subj
            )
            is_from_us = any(me and me in sender for me in my_emails)
            if idx_msg > 0 and (is_bounce or not is_from_us or email in sender):
                incoming.append((msg, is_bounce))

        # Thread tracking from state to prevent duplicate alerts
        thread_rec = sent_threads.setdefault(dedupe_key, {})
        last_processed_id = thread_rec.get("last_processed_reply_id")

        if not incoming:
            # Only our sent messages exist in the thread so far
            if current_transcript != transcript:
                write_rows(ws, cmap, [item(row, {cmap.received_reply: transcript, cmap.thread_id: thread_id})])
                thread_rec["last_msg_count"] = len(messages)
                save_state(state)
                print(f"   📝 Updated transcript for {name} <{email}> (follow-up synced).")
            continue

        latest_msg, is_bounce = incoming[-1]
        latest_incoming_id = latest_msg.get("id")
        is_new_reply = (last_processed_id != latest_incoming_id)

        if is_bounce:
            notice = extract_body_text(latest_msg)[:500] or "Delivery Status Notification (Failure)"
            needs_update = (current_transcript != transcript or g(c, cmap.reply_status).lower() != "bounced")
            if needs_update:
                write_rows(ws, cmap, [item(row, {
                    cmap.reply_status: "bounced",
                    cmap.reply_sentiment: "BOUNCE (NEGATIVE)",
                    cmap.received_reply: transcript,
                    cmap.thread_id: thread_id,
                })])

            if is_new_reply:
                thread_rec["last_processed_reply_id"] = latest_incoming_id
                thread_rec["last_msg_count"] = len(messages)
                save_state(state)
                bounces_found += 1
                print(f"   ⚠️ Bounce recorded for {name} <{email}>!")
                alert(f"⚠️ BOUNCE DETECTED\nSpeaker: {name} ({company})\nEmail: {email}\n"
                      f"Conference: {conf_name}\n\nNotice:\n{notice[:250]}")
            continue

        # Normal reply from lead
        latest_text = extract_body_text(latest_msg)
        analysis = ai_agent.analyze_reply(latest_text, name, company, conf_name)
        intent = analysis.get("intent", "OTHER")
        sentiment = analysis.get("sentiment", "NEUTRAL")
        summary = analysis.get("summary", latest_text[:150])

        needs_update = (
            current_transcript != transcript
            or g(c, cmap.reply_status).lower() != "replied"
            or not g(c, cmap.reply_sentiment)
        )

        if needs_update:
            write_rows(ws, cmap, [item(row, {
                cmap.reply_status: "replied",
                cmap.reply_sentiment: f"{intent} ({sentiment})",
                cmap.received_reply: transcript,
                cmap.thread_id: thread_id,
            })])

        if is_new_reply:
            thread_rec["last_processed_reply_id"] = latest_incoming_id
            thread_rec["last_msg_count"] = len(messages)
            save_state(state)
            replies_found += 1
            print(f"   ✅ New reply received from {name} <{email}>! Intent: {intent} ({sentiment})")
            print(f"      Full back-and-forth transcript ({len(messages)} messages) logged to Sheet Column R.")

            badge = {
                "INTERESTED": "🔥 INTERESTED", "MEETING_REQUESTED": "📅 MEETING REQUESTED",
                "OUT_OF_OFFICE": "🌴 OUT OF OFFICE", "REFERRAL": "🔄 REFERRAL",
                "NOT_INTERESTED": "⛔ NOT INTERESTED", "UNSUBSCRIBE": "🚫 UNSUBSCRIBE",
            }.get(intent, "💬 NEW REPLY")
            alert(f"{badge}\nSpeaker: {name} ({company})\nEmail: {email}\nConference: {conf_name}\n"
                  f"Intent: {intent} ({sentiment})\n\nLatest Reply:\n{latest_text[:400]}")
        else:
            if needs_update:
                print(f"   📝 Updated conversation transcript for {name} <{email}> ({len(messages)} messages).")
                thread_rec["last_msg_count"] = len(messages)
                save_state(state)

    print(f"[{now()}] --- Watcher Completed (Scanned: {checked} | New Replies: {replies_found} | "
          f"Bounces: {bounces_found}) ---\n")


# ==============================================================================
# Summary and init
# ==============================================================================
def summary_stats():
    with sheet_lock:
        print(f"\n[{now()}] --- Outreach Campaign Metrics ---")
        try:
            sh, ws = sheets()
            all_values = ws.get_all_values()
        except Exception as e:
            print(f"[Error] {e}")
            return
        if len(all_values) < 2:
            print("No data rows found.")
            return
        cmap = SheetColumnMap(all_values[0])
        rows = snapshot_rows(all_values, cmap)
        sent_today = get_sent_today(load_state())
        cap_str = str(DAILY_CAP) if DAILY_CAP > 0 else "Unlimited"

        by_status: Dict[str, int] = {}
        for r in rows:
            s = g(r["c"], cmap.outreach_status).lower() or "(not processed yet)"
            by_status[s] = by_status.get(s, 0) + 1
        sent = [r for r in rows if g(r["c"], cmap.outreach_status).lower() == "sent"]
        replies = [r for r in rows if g(r["c"], cmap.reply_status).lower() == "replied"]
        bounces = [r for r in rows if g(r["c"], cmap.reply_status).lower() == "bounced"]
        intents: Dict[str, int] = {}
        for r in replies:
            k = g(r["c"], cmap.reply_sentiment).upper() or "UNCLASSIFIED"
            intents[k] = intents.get(k, 0) + 1

        rr = (len(replies) / len(sent) * 100) if sent else 0.0
        br = (len(bounces) / len(sent) * 100) if sent else 0.0
        print("=" * 55)
        print("📈 OUTREACH AGENT DASHBOARD (Single Sheet)")
        print("=" * 55)
        print(f"Leads in sheet:  {len(rows)}")
        print(f"Sent today:      {sent_today} / {cap_str}")
        print(f"Total sent:      {len(sent)}")
        print(f"Replies:         {len(replies)} ({rr:.1f}%)")
        print(f"Bounces:         {len(bounces)} ({br:.1f}%)")
        print("-" * 55)
        print("Outreach status:")
        for k, v in sorted(by_status.items(), key=lambda x: -x[1]):
            print(f"  • {k:<24}: {v}")
        print("Reply sentiment / intent:")
        if intents:
            for k, v in sorted(intents.items(), key=lambda x: -x[1]):
                print(f"  • {k:<28}: {v}")
        else:
            print("  (no replies yet)")
        print("=" * 55 + "\n")


def init_single_sheet():
    with sheet_lock:
        print(f"\n[{now()}] --- Initializing & Verifying Single Outreach Sheet ---")
        try:
            sh, ws = sheets()
            cmap = ensure_columns(ws, create=True)
        except Exception as e:
            print(f"[Error] {e}")
            return
        try:
            ws.format("1:1", {"textFormat": {"bold": True}})
            ws.freeze(rows=1)
        except Exception:
            pass
        print(f"\n📋 Column map on '{ws.title}' (column numbers):")
        for label, idx in [
            ("Speaker ID", cmap.speaker_id), ("Conference ID", cmap.conf_id), ("Conference Name", cmap.conf_name),
            ("Speaker Name", cmap.speaker_name), ("Job Title", cmap.job_title), ("Company", cmap.company),
            ("Email", cmap.email), ("Verification Status", cmap.verification_status),
            ("outreach status", cmap.outreach_status), ("reply status", cmap.reply_status),
            ("reply sentiment", cmap.reply_sentiment), ("received reply", cmap.received_reply),
            ("sent message", cmap.sent_message), ("thread id", cmap.thread_id),
        ]:
            print(f"   • {label:<20} {rowcol_to_a1(1, idx + 1)[:-1] if idx is not None else '-'}")
        print(f"\n✅ '{sh.title}' -> '{ws.title}' is ready.\n")


# ==============================================================================
# Option B: 24/7 Autonomous Scheduler
# ==============================================================================
def get_seconds_until(target_time_str: str) -> Tuple[float, dt.datetime]:
    now_dt = dt.datetime.now()
    try:
        parts = target_time_str.split(":")
        target_h, target_m = int(parts[0]), int(parts[1])
    except Exception:
        target_h, target_m = 19, 0

    target_today = now_dt.replace(hour=target_h, minute=target_m, second=0, microsecond=0)
    if now_dt >= target_today:
        target_dt = target_today + dt.timedelta(days=1)
    else:
        target_dt = target_today

    wait_secs = (target_dt - now_dt).total_seconds()
    return max(0.0, wait_secs), target_dt


def watcher_worker(stop_event: threading.Event):
    print(f"👀 [Watcher Worker] 24/7 Reply Monitor started (polling every {WATCH_INTERVAL_MINUTES}m).")
    while not stop_event.is_set():
        try:
            watch()
        except Exception as e:
            print(f"[Watcher Error] Exception during watcher pass: {e}")
        if stop_event.wait(timeout=WATCH_INTERVAL_MINUTES * 60):
            break
    print("👀 [Watcher Worker] Stopped.")


def sender_worker(stop_event: threading.Event, run_immediately: bool = False):
    print(f"📨 [Sender Worker] Daily Outreach Scheduler started (scheduled daily at {SEND_TIME}).")

    if run_immediately:
        print("🚀 [Sender Worker] Running immediate send pass as requested...")
        try:
            send_batch()
        except Exception as e:
            print(f"[Sender Error] Immediate send pass failed: {e}")

    while not stop_event.is_set():
        wait_secs, next_run = get_seconds_until(SEND_TIME)
        hours = int(wait_secs // 3600)
        mins = int((wait_secs % 3600) // 60)
        print(f"⏰ [Sender Worker] Next send pass scheduled for: {next_run.strftime('%Y-%m-%d %H:%M:%S')} (in {hours}h {mins}m).")

        if stop_event.wait(timeout=wait_secs):
            break

        print(f"\n🔔 [Sender Worker] {SEND_TIME} Trigger Activated! Starting outreach send pass...")
        try:
            send_batch()
        except Exception as e:
            print(f"[Sender Error] Scheduled send pass failed: {e}")

        # Short buffer to prevent double triggers within the exact same minute
        time.sleep(60)

    print("📨 [Sender Worker] Stopped.")


def run_autonomous(send_now: bool = False):
    cap_str = f"{DAILY_CAP}/day" if DAILY_CAP > 0 else "Unlimited (sends all pending verified leads)"
    print("=" * 68)
    print("🤖 24/7 AUTONOMOUS CONFERENCE OUTREACH AGENT")
    print("=" * 68)
    print(f"  • Google Sheet ID:    {SHEET_ID}")
    print(f"  • Watcher (24/7):     Scans for replies every {WATCH_INTERVAL_MINUTES} minute(s)")
    print(f"  • Sender (Scheduled): Triggers daily at {SEND_TIME}")
    print(f"  • Sending Volume:     {cap_str}")
    print(f"  • Safe Pacing Delay:  {DELAY_MIN}s - {DELAY_MAX}s random jitter between emails")
    print(f"  • Conversation Log:   Full back-and-forth transcript stored in Column R")
    print("=" * 68)
    print("Press Ctrl+C to stop.\n")

    stop_event = threading.Event()
    should_send_now = send_now or SEND_ON_START

    t_watcher = threading.Thread(target=watcher_worker, args=(stop_event,), daemon=True, name="WatcherWorker")
    t_sender = threading.Thread(target=sender_worker, args=(stop_event, should_send_now), daemon=True, name="SenderWorker")

    t_watcher.start()
    t_sender.start()

    try:
        while not stop_event.is_set():
            time.sleep(1)
    except KeyboardInterrupt:
        print("\n🛑 Shutdown signal received (Ctrl+C). Stopping worker threads...")
        stop_event.set()
        t_watcher.join(timeout=5)
        t_sender.join(timeout=5)
        print("✅ Outreach Agent successfully stopped.")


# ==============================================================================
# Entry point
# ==============================================================================
if __name__ == "__main__":
    args = [a.lower() for a in sys.argv[1:]]
    mode = args[0] if args and not args[0].startswith("-") else "run"
    send_now_flag = "--send-now" in args or "-s" in args

    if mode in ("init-sheet", "init"):
        init_single_sheet()
    elif mode == "send":
        send_batch()
    elif mode == "watch":
        watch()
    elif mode == "summary":
        summary_stats()
    elif mode in ("run", "schedule", "start"):
        run_autonomous(send_now=send_now_flag)
    else:
        print(f"Unknown command: '{mode}'")
        print("Usage: python automation.py [run [--send-now] | send | watch | summary | init-sheet]")
