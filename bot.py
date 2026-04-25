#!/usr/bin/env python3
"""
Tareas Diosleidys automation for GitHub Actions.

Subcommands:
    notify-and-move    Detect new tasks under "Notificar directamente",
                       send them to Telegram, and move completed ones to
                       the "Tareas completadas" toggle.
    daily              Send all tasks under "Repetir una vez al día"
                       to Telegram (recurring daily reminder).
"""
import json
import os
import sys
import time
from pathlib import Path

import requests

# --- Required env vars ---
NOTION_TOKEN = os.environ["NOTION_TOKEN"]
NOTION_PAGE_ID = os.environ["NOTION_PAGE_ID"]
TELEGRAM_BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
TELEGRAM_CHAT_ID = os.environ["TELEGRAM_CHAT_ID"]

NOTION_HEADERS = {
    "Authorization": f"Bearer {NOTION_TOKEN}",
    "Notion-Version": "2022-06-28",
    "Content-Type": "application/json",
}

STATE_FILE = Path(".state/snapshot.json")
SECTION_ACTIVE = "Notificar directamente"
SECTION_DAILY = "Repetir una vez al día"
TOGGLE_TITLE = "Tareas completadas"


# ---------- Notion helpers ----------

def notion_list_children(block_id):
    out, cursor = [], None
    while True:
        params = {"page_size": 100}
        if cursor:
            params["start_cursor"] = cursor
        r = requests.get(
            f"https://api.notion.com/v1/blocks/{block_id}/children",
            headers=NOTION_HEADERS,
            params=params,
            timeout=30,
        )
        r.raise_for_status()
        d = r.json()
        out.extend(d["results"])
        if not d.get("has_more"):
            return out
        cursor = d["next_cursor"]


def block_plain_text(block):
    bt = block["type"]
    rich = block.get(bt, {}).get("rich_text", [])
    return "".join(t.get("plain_text", "") for t in rich).strip()


def is_heading(block):
    return block["type"] in ("heading_1", "heading_2", "heading_3")


def archive_block(block_id):
    r = requests.patch(
        f"https://api.notion.com/v1/blocks/{block_id}",
        headers=NOTION_HEADERS,
        json={"archived": True},
        timeout=30,
    )
    r.raise_for_status()


def append_children(block_id, children):
    r = requests.patch(
        f"https://api.notion.com/v1/blocks/{block_id}/children",
        headers=NOTION_HEADERS,
        json={"children": children},
        timeout=30,
    )
    r.raise_for_status()
    return r.json()


def todo_block(text, checked=False):
    return {
        "object": "block",
        "type": "to_do",
        "to_do": {
            "rich_text": [{"type": "text", "text": {"content": text}}],
            "checked": checked,
        },
    }


# ---------- Page parsing ----------

def find_section_blocks(page_blocks, section_title):
    """Return blocks that belong under `section_title` heading until the next
    heading or toggle (sibling-level)."""
    started, out = False, []
    for b in page_blocks:
        if is_heading(b) and block_plain_text(b) == section_title:
            started = True
            continue
        if not started:
            continue
        if is_heading(b) or b["type"] == "toggle":
            break
        out.append(b)
    return out


def find_toggle_by_title(page_blocks, title):
    for b in page_blocks:
        if b["type"] == "toggle" and block_plain_text(b) == title:
            return b
    return None


# ---------- Telegram ----------

def send_telegram(text):
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    last = {}
    for parse_mode in ("Markdown", None):
        data = {"chat_id": TELEGRAM_CHAT_ID, "text": text}
        if parse_mode:
            data["parse_mode"] = parse_mode
        try:
            r = requests.post(url, data=data, timeout=30)
            last = r.json()
        except Exception as e:
            last = {"ok": False, "error": str(e)}
        if last.get("ok"):
            return True, last
    return False, last


# ---------- State ----------

def load_state():
    if not STATE_FILE.exists():
        return {"snapshot": {}}
    try:
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {"snapshot": {}}


def save_state(state):
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    state["last_checked"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    STATE_FILE.write_text(
        json.dumps(state, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


# ---------- Commands ----------

def cmd_notify_and_move():
    page_blocks = notion_list_children(NOTION_PAGE_ID)
    section_blocks = find_section_blocks(page_blocks, SECTION_ACTIVE)
    todo_blocks = [b for b in section_blocks if b["type"] == "to_do"]

    # 1) current multiset under section
    current = {}
    for b in todo_blocks:
        t = block_plain_text(b)
        if t:
            current[t] = current.get(t, 0) + 1

    # 2) diff vs snapshot
    state = load_state()
    prev = state.get("snapshot", {})
    new_items = []
    for t, c in current.items():
        diff = c - prev.get(t, 0)
        for _ in range(diff):
            new_items.append(t)

    # 3) Telegram if new
    if new_items:
        msg = "🆕 *Nuevas tareas — Diosleidys*\n" + "\n".join(f"☐ {t}" for t in new_items)
        ok, resp = send_telegram(msg)
        if not ok:
            print(f"Telegram FAILED: {resp}", file=sys.stderr)
            sys.exit(1)

    # 4) Move completed to toggle
    moved = []
    toggle = find_toggle_by_title(page_blocks, TOGGLE_TITLE)
    if toggle:
        completed = [b for b in todo_blocks if b["to_do"]["checked"]]
        children = []
        for b in completed:
            t = block_plain_text(b)
            if t:
                children.append(todo_block(t, checked=True))
        if children:
            append_children(toggle["id"], children)
            for b in completed:
                t = block_plain_text(b)
                if t:
                    archive_block(b["id"])
                    moved.append(t)

    # 5) Final snapshot reflects post-move state
    final = {}
    for t, c in current.items():
        rem = c - sum(1 for m in moved if m == t)
        if rem > 0:
            final[t] = rem

    state.update({
        "page_id": NOTION_PAGE_ID,
        "section": SECTION_ACTIVE,
        "snapshot": final,
    })
    save_state(state)

    print(f"new={len(new_items)} moved={len(moved)}")


def cmd_daily():
    page_blocks = notion_list_children(NOTION_PAGE_ID)
    section_blocks = find_section_blocks(page_blocks, SECTION_DAILY)
    todos = [block_plain_text(b) for b in section_blocks if b["type"] == "to_do"]
    todos = [t for t in todos if t]
    if not todos:
        print("no daily tasks")
        return
    msg = "🔁 *Tareas diarias — Diosleidys*\n" + "\n".join(f"☐ {t}" for t in todos)
    ok, resp = send_telegram(msg)
    if not ok:
        print(f"Telegram FAILED: {resp}", file=sys.stderr)
        sys.exit(1)
    print(f"daily sent={len(todos)}")


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else ""
    if cmd == "notify-and-move":
        cmd_notify_and_move()
    elif cmd == "daily":
        cmd_daily()
    else:
        print("Usage: bot.py {notify-and-move|daily}", file=sys.stderr)
        sys.exit(2)
