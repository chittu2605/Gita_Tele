import os, json, yaml, requests
from bs4 import BeautifulSoup
from datetime import datetime

# load config
with open("config.yaml", "r", encoding="utf-8") as f:
    cfg = yaml.safe_load(f)

STATE_FILE = cfg.get("state_file", "state.json")
IMAGE_ROOT = cfg["images"]["root_path"]
HINDI_DOC = cfg["google_doc"].get("hindi_doc_id")
EN_DOC = cfg["google_doc"].get("english_doc_id")
SPLIT_DELIM = cfg["content"].get("split_delimiter", "\n\n")
PREF_CAPTION = cfg["content"].get("prefer_caption_for_short_posts", False)
CAP_LEN = int(cfg["content"].get("caption_max_length", 1000))
POSTS_PER_RUN = int(cfg["posting"].get("posts_per_run", 1))
RATIO = cfg["posting"].get("language_ratio", [3,1])

TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
CHANNEL_USERNAME = os.environ.get("CHANNEL_USERNAME") or cfg.get("telegram_channel_username")
if not TELEGRAM_BOT_TOKEN or not CHANNEL_USERNAME:
    raise SystemExit("Set TELEGRAM_BOT_TOKEN and CHANNEL_USERNAME in environment secrets.")

def fetch_doc_text(doc_id):
    url = f"https://docs.google.com/document/d/{doc_id}/export?format=txt"
    r = requests.get(url, timeout=30)
    if r.status_code == 200 and r.text.strip():
        return r.text
    # fallback to HTML
    r = requests.get(f"https://docs.google.com/document/d/{doc_id}/export?format=html", timeout=30)
    if r.status_code == 200:
        return BeautifulSoup(r.text, "html.parser").get_text("\n")
    raise Exception(f"Cannot fetch doc {doc_id}: status {r.status_code}")

def split_msgs(text):
    return [p.strip() for p in text.split(SPLIT_DELIM) if p.strip()]

def gather_images(root):
    images = []
    if not os.path.isdir(root):
        return images
    items = sorted(os.listdir(root), key=lambda x: (int(x) if x.isdigit() else x))
    for it in items:
        full = os.path.join(root, it)
        if os.path.isdir(full):
            for f in sorted(os.listdir(full)):
                if f.lower().endswith((".jpg",".jpeg",".png",".webp")):
                    images.append(os.path.join(full,f))
    # include root-level images
    for f in sorted(os.listdir(root)):
        fp = os.path.join(root, f)
        if os.path.isfile(fp) and f.lower().endswith((".jpg",".jpeg",".png",".webp")) and fp not in images:
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

def main():
    print("Poster start:", datetime.utcnow().isoformat())
    hindi_msgs = split_msgs(fetch_doc_text(HINDI_DOC)) if HINDI_DOC else []
    eng_msgs = split_msgs(fetch_doc_text(EN_DOC)) if EN_DOC else []
    images = gather_images(IMAGE_ROOT)
    state = load_state()

    for _ in range(POSTS_PER_RUN):
        lang = choose_language(state)
        msgs = hindi_msgs if lang=="hindi" else eng_msgs
        mi_key = "h_msg_index" if lang=="hindi" else "e_msg_index"

        if state[mi_key] >= len(msgs) or state["img_index"] >= len(images):
            print("No more messages or images.")
            continue

        msg = msgs[state[mi_key]]
        img = images[state["img_index"]]

        try:
            if PREF_CAPTION and len(msg) <= CAP_LEN:
                send_photo(TELEGRAM_BOT_TOKEN, CHANNEL_USERNAME, img, caption=msg)
            else:
                send_photo(TELEGRAM_BOT_TOKEN, CHANNEL_USERNAME, img, caption=None)
                send_message(TELEGRAM_BOT_TOKEN, CHANNEL_USERNAME, msg)
            state[mi_key] += 1
            state["img_index"] += 1
            state["lang_counter"] += 1
            save_state(state)
        except Exception as e:
            print("Posting error:", e)
            break

if __name__ == "__main__":
    main()
