# 🚀 Conference Outreach Agent (`automation.py`)

An autonomous, safe cold outreach and lead engagement agent designed for single-sheet Google Sheets integration and 24/7 cloud VM deployment. It dispatches personalized emails, logs exact sent messages, tracks conversation threads, classifies replies with Gemini AI, and records the **full conversation transcript** directly onto the sheet.

---

## 📋 Complete Sheet Column Structure (20 Columns: A to T)

| Col # | Column Letter | Column Header | Managed By | Description |
|---|---|---|---|---|
| **1** | **A** | `Speaker_ID` | Extractor | Unique speaker identifier |
| **2** | **B** | `Conference_ID` | Extractor | Unique conference identifier |
| **3** | **C** | `Conference_Name` | Extractor | Conference name |
| **4** | **D** | `Speaker_Name` | Extractor | Full name |
| **5** | **E** | `Job_Title` | Extractor | Professional title / role |
| **6** | **F** | `Company_Name` | Extractor | Organization / Company |
| **7** | **G** | `Personal_LinkedIn_URL` | Extractor | Speaker LinkedIn profile |
| **8** | **H** | `LinkedIn_Status` | Extractor | LinkedIn verification status |
| **9** | **I** | `Professional_Email` | Extractor | Target email address |
| **10** | **J** | `Email_Source_URL` | Extractor | Email finder / source link |
| **11** | **K** | `Speaker_Source_URL` | Extractor | Speaker profile link |
| **12** | **L** | `Verification_Status` | Extractor | `verified`, `needs_review`, etc. |
| **13** | **M** | `Last_Checked_At` | Extractor | Scraper timestamp |
| **14** | **N** | `Weekly_Run_ID` | Extractor | Batch run identifier |
| **15** | **O** | `outreach status` | **Outreach Agent** | `sent`, `sending`, `skipped_unverified`, `failed` |
| **16** | **P** | `reply status` | **Outreach Agent** | `replied`, `bounced` |
| **17** | **Q** | `reply sentiment` | **Outreach Agent** | AI Intent & Sentiment (e.g. `INTERESTED (POSITIVE)`) |
| **18** | **R** | `received reply` | **Outreach Agent** | **Full conversation transcript** (all outreach, replies, and follow-ups) |
| **19** | **S** | `sent message` | **Outreach Agent** | Exact initial email (Subject + Body) dispatched |
| **20** | **T** | `thread id` | **Outreach Agent** | Gmail thread ID for accurate reply tracking |

---

## 💬 Full Conversation Transcript Logging (Column R)

Whenever a speaker replies, or you send a return follow-up email in Gmail, or the speaker replies again, the agent records the **entire chronological thread** inside Column R (`received reply`):

```text
[2026-11-02 11:18] You (Akash):
Hi Will,
This is Akash from Adople AI. Nice to connect with you.

--------------------------------------------------

[2026-11-02 12:30] Will Grannis:
Hey Akash, thanks for reaching out. Would love to chat. What time works for you?

--------------------------------------------------

[2026-11-02 14:00] You (Akash):
Great! How about Thursday at 2 PM?

--------------------------------------------------

[2026-11-02 15:10] Will Grannis:
Thursday 2 PM works. Send over a calendar invite.
```

- **Zero Alert Spam**: The agent tracks message IDs in `outreach_state.json` so Telegram alerts are only fired when a **new reply** from the speaker is detected.

---

## ✉️ Dynamic Email Template (`template.txt`)

The agent reads its outreach subject and body dynamically from `template.txt`:

```text
SUBJECT: Nice to connect - {conference_name}

Hi {first_name},

This is {sender} from {sender_company}. Noticed your session as {job_title} at {company} for {conference_name}.

Would be great to connect with you!

Thank you,
{sender}
{sender_company}
```

You can change the email text or subject anytime directly in `template.txt` without editing the Python code. Available dynamic tags:
`{first_name}`, `{speaker_name}`, `{job_title}`, `{company}`, `{conference_name}`, `{sender}`, `{sender_company}`.

---

## ⚙️ 24/7 Autonomous Scheduler (Option B for VM Deployment)

You can run the agent 24/7 on your machine or cloud VM using a single command:

```powershell
# Start 24/7 autonomous runner (Watcher 24/7 + Sender at 7:00 PM)
python automation.py run

# Start 24/7 autonomous runner AND execute an immediate send pass on launch
python automation.py run --send-now
```

### How the 24/7 Scheduler Works:
1. **Parallel 24/7 Watcher Worker**:
   - Runs in the background 24 hours a day, checking for incoming replies and follow-ups every `WATCH_INTERVAL_MINUTES` (default: 5 minutes).
   - Syncs conversation threads to Google Sheets and sends Telegram alerts.
2. **7:00 PM Daily Sender Worker**:
   - Sleeps until `SEND_TIME` (default: `19:00` / 7:00 PM).
   - Wakes up at 7:00 PM, reads all newly added verified leads from the sheet, and sends outreach emails with safe 60–120s jitter delays.
   - With `DAILY_CAP=0`, it sends to **all pending verified leads added that day** without an arbitrary cap cutoff.
   - Once the pass is complete, logs summary and safely sleeps until 7:00 PM the next day.

---

## 💻 Manual Commands

```powershell
# Verify columns and format sheet
python automation.py init-sheet

# Run an immediate manual send pass (sends to pending verified leads)
python automation.py send

# Run an immediate manual reply watcher pass
python automation.py watch

# View campaign performance metrics
python automation.py summary
```
