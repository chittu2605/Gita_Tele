# poster.py
import os
import re
import time
import json
import yaml
import requests
from bs4 import BeautifulSoup
from datetime import datetime

# load config
with open("config.yaml", "r", encoding="utf-8") as f:
    cfg = yaml.safe_load(f)

STATE_FILE = cfg.get("state_file", "state.json")
IMAGE_ROOT = cfg["images"]["root_path"]
HINDI_DOC = cfg["google_doc"].get("hindi_doc_id")
EN_DOC = cfg["google_doc"].get("english_doc_id")
# legacy single-string delimiter kept for compatibility but we now detect robust separators via regex
SPLIT_DELIM = cfg["content"].get("split_delimiter", "\n\n")
PREF_CAPTION = cfg["content"].get("prefer_caption_for_short_posts", False)
CAP_LEN = int(cfg["content"].get("caption_max_length", 1000))
POSTS_PER_RUN = int(cfg["posting"].get("posts_per_run", 1))
RATIO = cfg["posting"].get("language_ratio", [3,1])

TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
CHANNEL_USERNAME = os.environ.get("CHANNEL_USERNAME") or cfg.get("telegram_channel_username")
if not TELEGRAM_BOT_TOKEN or not CHANNEL_USERNAME:
    raise SystemExit("Set TELEGRAM_BOT_TOKEN and CHANNEL_USERNAME in environment secrets.")

# ----------------------------------------
# Helpers
# ----------------------------------------
def fetch_doc_text(doc_id):
    url = f"https://docs.google.com/document/d/{doc_id}/export?format=txt"
    r = requests.get(url, timeout=30)
    if r.status_code == 200 and r.text.strip():
        return r.text
    r = requests.get(f"https://docs.google.com/document/d/{doc_id}/export?format=html", timeout=30)
    if r.status_code == 200:
        return BeautifulSoup(r.text, "html.parser").get_text("\n")
    raise Exception(f"Cannot fetch doc {doc_id}: status {r.status_code}")

def split_msgs(text):
    """
    Robust splitter:
    - Normalize newlines.
    - First split on visible strong separators (lines that are only --- or ___ or repeated em-dash).
    - If none found, fallback to splitting on two-or-more newlines.
    - Trim each block and return non-empty list.
    """
    if not text or not text.strip():
        return []

    # normalize
    text = text.replace('\r\n', '\n').replace('\r', '\n')

    # regex: match a line that contains only dashes/underscores/em-dash (3 or more) possibly with surrounding spaces
    sep_pattern = re.compile(r'\n\s*(?:[-–—]{3,}|_{3,})\s*\n', flags=re.MULTILINE)

    # if explicit custom delimiter from config exists and is not the default, prefer that
    if SPLIT_DELIM and SPLIT_DELIM.strip() not in ["", "\\n\\n", "\n\n"]:
        # allow user to set something like "\n---\n" in config; interpret \n escapes
        user_delim = SPLIT_DELIM.encode('utf-8').decode('unicode_escape')
        parts = [p.strip() for p in text.split(user_delim) if p.strip()]
        if parts:
            return parts

    # first try the strong separator pattern
    parts = [p.strip() for p in re.split(sep_pattern, text) if p.strip()]
    if len(parts) > 1:
        return parts

    # fallback: split on 2+ blank lines (preserve paragraphs inside block)
    parts = [p.strip() for p in re.split(r'\n{2,}', text) if p.strip()]
    return parts

def gather_images(root):
    images = []
    if not os.path.isdir(root):
        return images
    items = sorted(os.listdir(root), key=lambda x: (0, int(x)) if x.isdigit() else (1, x.lower()))
    for it in items:
        full = os.path.join(root, it)
        if os.path.isdir(full):
            for f in sorted(os.listdir(full)):
                if f.lower().endswith((".jpg", ".jpeg", ".png", ".webp")):
                    images.append(os.path.join(full, f))
    # include root-level images
    for f in sorted(os.listdir(root)):
        fp = os.path.join(root, f)
        if os.path.isfile(fp) and f.lower().endswith((".jpg", ".jpeg", ".png", ".webp")) and fp not in images:
            images.append(fp)
    return images

def load_state():
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE, "r", encoding="utf-8") as sf:
            return json.load(sf)
    s = {"h_msg_index":0, "e_msg_index":0, "img_index":0, "lang_counter":0}
    with open(STATE_FILE, "w", encoding="utf-8") as sf:
        json.dump(s, sf, ensure_ascii=False, indent=2)
    return s

def save_state(s):
    with open(STATE_FILE, "w", encoding="utf-8") as sf:
        json.dump(s, sf, ensure_ascii=False, indent=2)

def send_photo(bot_token, chat_id, image_path, caption=None):
    url = f"https://api.telegram.org/bot{bot_token}/sendPhoto"
    with open(image_path, "rb") as f:
        files = {"photo": f}
        data = {"chat_id": chat_id}
        if caption:
            data["caption"] = caption
        r = requests.post(url, files=files, data=data, timeout=60)
    if r.status_code != 200:
        raise Exception(f"sendPhoto error {r.status_code}: {r.text}")
    return r.json()

def send_message(bot_token, chat_id, text):
    url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
    data = {"chat_id": chat_id, "text": text, "parse_mode":"HTML"}
    r = requests.post(url, data=data, timeout=30)
    if r.status_code != 200:
        raise Exception(f"sendMessage error {r.status_code}: {r.text}")
    return r.json()

def choose_language(state):
    h_ratio, e_ratio = RATIO if len(RATIO) >= 2 else (3,1)
    total = h_ratio + e_ratio
    idx = state.get("lang_counter", 0) % total
    return "hindi" if idx < h_ratio else "english"

def split_and_send_text(bot_token, chat_id, text, max_len=4000):
    # preserve paragraph boundaries while chunking
    text = text.replace('\r\n', '\n').replace('\r', '\n')
    # collapse multiple blank lines to exactly two for consistency
    text = re.sub(r'\n{3,}', '\n\n', text)

    # split into paragraphs on two newlines (we already used stronger separators earlier)
    paragraphs = [p.strip() for p in re.split(r'\n{2,}', text) if p.strip()]

    chunks = []
    cur = ""
    for para in paragraphs:
        # collapse internal single newlines into spaces so paragraphs are single-line blocks
        para_clean = re.sub(r'\s*\n\s*', ' ', para).strip()
        if not cur:
            if len(para_clean) <= max_len:
                cur = para_clean
            else:
                # long paragraph -> break by words
                words = para_clean.split()
                tmp = ""
                for w in words:
                    if len(tmp) + (1 if tmp else 0) + len(w) <= max_len:
                        tmp = (tmp + " " + w).strip()
                    else:
                        if tmp:
                            chunks.append(tmp)
                        tmp = w
                if tmp:
                    cur = tmp
        else:
            candidate = cur + "\n\n" + para_clean
            if len(candidate) <= max_len:
                cur = candidate
            else:
                chunks.append(cur)
                if len(para_clean) <= max_len:
                    cur = para_clean
                else:
                    words = para_clean.split()
                    tmp = ""
                    for w in words:
                        if len(tmp) + (1 if tmp else 0) + len(w) <= max_len:
                            tmp = (tmp + " " + w).strip()
                        else:
                            if tmp:
                                chunks.append(tmp)
                            tmp = w
                    cur = tmp
    if cur:
        chunks.append(cur)

    for part in chunks:
        to_send = part if part.strip() else " "
        send_message(bot_token, chat_id, to_send)
        time.sleep(1)

def main():
    print("Poster start:", datetime.utcnow().isoformat())
    hindi_msgs = split_msgs(fetch_doc_text(HINDI_DOC)) if HINDI_DOC else []
    eng_msgs = split_msgs(fetch_doc_text(EN_DOC)) if EN_DOC else []
    images = gather_images(IMAGE_ROOT)
    state = load_state()

    for _ in range(POSTS_PER_RUN):
        lang = choose_language(state)
        msgs = hindi_msgs if lang == "hindi" else eng_msgs
        mi_key = "h_msg_index" if lang == "hindi" else "e_msg_index"

        if state.get(mi_key, 0) >= len(msgs):
            print(f"No more {lang} messages available.")
            state["lang_counter"] = state.get("lang_counter", 0) + 1
            save_state(state)
            continue
        if state.get("img_index", 0) >= len(images):
            print("No more images available.")
            continue

        msg = msgs[state[mi_key]]
        img = images[state["img_index"]]

        try:
            if PREF_CAPTION and len(msg) <= CAP_LEN:
                send_photo(TELEGRAM_BOT_TOKEN, CHANNEL_USERNAME, img, caption=msg)
            else:
                send_photo(TELEGRAM_BOT_TOKEN, CHANNEL_USERNAME, img, caption=None)
                split_and_send_text(TELEGRAM_BOT_TOKEN, CHANNEL_USERNAME, msg, max_len=4000)

            state[mi_key] = state.get(mi_key, 0) + 1
            state["img_index"] = state.get("img_index", 0) + 1
            state["lang_counter"] = state.get("lang_counter", 0) + 1
            save_state(state)
            print(f"Posted {lang} msg #{state[mi_key]-1} with image #{state['img_index']-1}")
        except Exception as e:
            print("Posting error:", e)
            break

if __name__ == "__main__":
    main()
