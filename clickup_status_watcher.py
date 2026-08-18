#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
ClickUp status watcher — production
===================================
Polls the whole workspace and emails when a task LEAVES a to-do status,
whatever it moves to (in progress, review, complete, ...).

State (task_id -> last seen status) is committed back to the repo, so a
status change is reported exactly once.

Env:
    CLICKUP_TOKEN   personal API token (pk_...)
    SMTP_USER       aof.group.auto@gmail.com
    SMTP_PASS       Gmail app password
    SEED=1          record statuses without sending anything (first run)
"""

import os
import json
import time
import smtplib
import pathlib
import datetime as dt
from email.mime.text import MIMEText
from email.utils import formataddr

import requests
import pytz

# ── Config ───────────────────────────────────────────────────────────────────
TOKEN    = os.environ["CLICKUP_TOKEN"]
TEAM_ID  = "7505450"
BASE_URL = "https://api.clickup.com/api/v2"

SMTP_USER   = os.environ["SMTP_USER"]
SMTP_PASS   = os.environ["SMTP_PASS"]
SENDER_NAME = "Business Intelligence"
MAIL_TO     = ["o.salahaddin@aofgroup.com"]
MAIL_CC     = ["a.alsalem@aofgroup.com"]

STATE_FILE     = pathlib.Path("state/clickup_status_state.json")
LOOKBACK_HOURS = 6      # re-scan window; must exceed the cron interval
MAX_INDIVIDUAL = 10     # above this, one digest email instead of many
KSA = pytz.timezone("Asia/Riyadh")

SEED = os.environ.get("SEED") == "1"

# ── Trigger rule ─────────────────────────────────────────────────────────────
#   "leaving_todo"  → fire whenever a task moves OUT of a to-do status,
#                     regardless of where it lands            ← current setting
#   "entering_done" → fire only when it reaches a completion status
#   "any_change"    → fire on every status change
TRIGGER = "leaving_todo"

# Statuses across the 8 spaces that count as "not started yet"
TODO_STATUSES = {"to do", "todo", "to-do", "open", "new"}

# Only used by TRIGGER = "entering_done". Matched by NAME, never by type:
# `cancelled` is type "closed" in OMO & Marketing & Finance.
DONE_STATUSES = {"complete", "completed", "closed"}

# Destinations that should never generate an email, in any mode.
# Empty = notify on everything, including cancellations.
IGNORE_DESTINATIONS = set()      # e.g. {"cancelled", "rejected"}

# Note: /task/{id}/time_in_status is NOT available on this ClickUp plan (403
# TIS_027), so the state file is the only source of the previous status.

norm    = lambda s: (s or "").strip().lower()
is_todo = lambda n: norm(n) in TODO_STATUSES
is_done = lambda n: norm(n) in DONE_STATUSES


def should_notify(prev, now):
    """prev/now are status names; both already known to differ."""
    if norm(now) in IGNORE_DESTINATIONS:
        return False
    if TRIGGER == "leaving_todo":
        return is_todo(prev) and not is_todo(now)
    if TRIGGER == "entering_done":
        return is_done(now) and not is_done(prev)
    return True          # any_change


# ── ClickUp ──────────────────────────────────────────────────────────────────
def _get(endpoint, params=None):
    for _ in range(4):
        r = requests.get(
            f"{BASE_URL}{endpoint}",
            headers={"Authorization": TOKEN},
            params=params,
            timeout=30,
        )
        if r.status_code == 200:
            return r.json()
        if r.status_code == 429:
            time.sleep(int(r.headers.get("Retry-After", 30)))
            continue
        print(f"[{r.status_code}] {endpoint} -> {r.text[:300]}")
        return None
    return None


def fetch_recent_tasks(since_ms):
    """Every task in the workspace updated since `since_ms`, across all spaces."""
    tasks, page = [], 0
    while True:
        data = _get(
            f"/team/{TEAM_ID}/task",
            {
                "page": page,
                "order_by": "updated",
                "reverse": "true",
                "subtasks": "true",
                "include_closed": "true",   # completed tasks vanish without this
                "date_updated_gt": since_ms,
            },
        )
        if not data:
            break
        batch = data.get("tasks", [])
        tasks.extend(batch)
        if data.get("last_page") or len(batch) < 100:
            break
        page += 1
    return tasks


# ── State ────────────────────────────────────────────────────────────────────
def load_state():
    if STATE_FILE.exists():
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    return {}


def save_state(state):
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(
        json.dumps(state, ensure_ascii=False, separators=(",", ":")),
        encoding="utf-8",
    )


# ── Email ────────────────────────────────────────────────────────────────────
def task_row(t):
    who = ", ".join(a.get("username", "") for a in t.get("assignees", [])) or "—"
    return f"""
    <tr>
      <td style="padding:8px 12px;border:1px solid #e5e7eb;">
        <a href="{t['url']}" style="color:#1a73e8;text-decoration:none;">{t['name']}</a>
      </td>
      <td style="padding:8px 12px;border:1px solid #e5e7eb;">{t.get('_space','—')}</td>
      <td style="padding:8px 12px;border:1px solid #e5e7eb;">{t.get('list',{}).get('name','—')}</td>
      <td style="padding:8px 12px;border:1px solid #e5e7eb;">{who}</td>
      <td style="padding:8px 12px;border:1px solid #e5e7eb;">{t['_prev']} &rarr; <b>{t['_now']}</b></td>
    </tr>"""


def build_html(tasks, when):
    rows  = "".join(task_row(t) for t in tasks)
    title = ("Task status changed" if len(tasks) == 1
             else f"{len(tasks)} task status changes")
    return f"""<!DOCTYPE html><html><body style="font-family:Segoe UI,Arial,sans-serif;background:#f6f7f9;padding:24px;">
  <div style="max-width:820px;margin:auto;background:#fff;border-radius:10px;padding:24px;">
    <h2 style="margin:0 0 4px;color:#111827;">{title}</h2>
    <p style="margin:0 0 18px;color:#6b7280;font-size:13px;">ClickUp &middot; {when} (KSA)</p>
    <table style="border-collapse:collapse;width:100%;font-size:13px;">
      <thead>
        <tr style="background:#f3f4f6;text-align:left;">
          <th style="padding:8px 12px;border:1px solid #e5e7eb;">Task</th>
          <th style="padding:8px 12px;border:1px solid #e5e7eb;">Space</th>
          <th style="padding:8px 12px;border:1px solid #e5e7eb;">List</th>
          <th style="padding:8px 12px;border:1px solid #e5e7eb;">Assignee</th>
          <th style="padding:8px 12px;border:1px solid #e5e7eb;">Status</th>
        </tr>
      </thead>
      <tbody>{rows}</tbody>
    </table>
  </div>
</body></html>"""


def subject_for(t):
    icon = "✅" if is_done(t["_now"]) else "🔄"
    return f"{icon} {t['_prev']} → {t['_now']}: {t['name']}"


def send_mail(subject, html):
    msg = MIMEText(html, "html", "utf-8")
    msg["Subject"] = subject
    msg["From"] = formataddr((SENDER_NAME, SMTP_USER))
    msg["To"] = ", ".join(MAIL_TO)
    msg["Cc"] = ", ".join(MAIL_CC)
    with smtplib.SMTP_SSL("smtp.gmail.com", 465) as s:
        s.login(SMTP_USER, SMTP_PASS)
        # delivery comes from this list, not the Cc header
        s.sendmail(SMTP_USER, MAIL_TO + MAIL_CC, msg.as_string())
    print(f"📧 sent: {subject}")


# ── Main ─────────────────────────────────────────────────────────────────────
def main():
    state     = load_state()
    first_run = not state
    since_ms  = int((time.time() - LOOKBACK_HOURS * 3600) * 1000)

    tasks = fetch_recent_tasks(since_ms)
    print(f"Scanned {len(tasks)} recently-updated task(s) · TRIGGER={TRIGGER}")

    space_names = {
        sp["id"]: sp["name"]
        for sp in (_get(f"/team/{TEAM_ID}/space", {"archived": "false"}) or {}).get("spaces", [])
    }

    flipped = []
    for t in tasks:
        tid = t["id"]
        now = t.get("status", {}).get("status")
        prev = state.get(tid)
        state[tid] = now

        if SEED or first_run or not prev or norm(prev) == norm(now):
            continue
        if not should_notify(prev, now):
            continue

        t["_prev"], t["_now"] = prev, now
        t["_space"] = space_names.get(str(t.get("space", {}).get("id")), "—")
        flipped.append(t)

    when = dt.datetime.now(KSA).strftime("%Y-%m-%d %H:%M")

    if SEED or first_run:
        print(f"🌱 Seeded {len(state)} task statuses — no email sent.")
    elif not flipped:
        print("No qualifying status changes.")
    elif len(flipped) <= MAX_INDIVIDUAL:
        for t in flipped:
            send_mail(subject_for(t), build_html([t], when))
    else:
        send_mail(f"🔄 {len(flipped)} task status changes", build_html(flipped, when))

    save_state(state)
    print(f"State: {len(state)} task(s) tracked.")


if __name__ == "__main__":
    main()
