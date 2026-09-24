#!/usr/bin/env python3
"""x_parser.py — парсер X/Twitter через локальный шлюз grok2api (бесплатный пул Grok-аккаунтов).

Использование:
  python x_parser.py "query"                      # живой поиск постов
  python x_parser.py "query" --handle elonmusk    # только посты конкретного автора
  python x_parser.py "query" --json out.json      # сохранить JSON
  python x_parser.py "query" --model grok-4.6     # другая модель (Build-пул)

Шлюз: http://127.0.0.1:8000 (grok2api), client key в env G2A_KEY или файле рядом.
"""
import argparse, json, os, sys, urllib.request, urllib.error

BASE = os.getenv("G2A_BASE", "http://127.0.0.1:8000")
KEY_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "g2a_key.txt")

def get_key():
    k = os.getenv("G2A_KEY")
    if k:
        return k.strip()
    if os.path.exists(KEY_FILE):
        return open(KEY_FILE, encoding="utf-8").read().strip()
    sys.exit("нет ключа: set G2A_KEY или файл g2a_key.txt")

def search(query, model="grok-chat-fast", handle=None, days=7, max_posts=10, timeout=180):
    prompt = (
        f"Use your live X search. Find the {max_posts} most recent real posts on X "
        f"from the last {days} days matching: {query!r}."
    )
    if handle:
        prompt += f" Only posts authored by @{handle}."
    prompt += (
        " STRICT OUTPUT FORMAT — reply ONLY with a JSON array, no prose, no markdown fences. "
        'Each element: {"handle":"@user","date":"YYYY-MM-DD","text":"full post text","likes":<int or null>,"url":"https://x.com/..."}'
    )
    payload = {
        "model": model,
        "messages": [{"role": "user", "content": prompt}],
        "max_tokens": 4000,
    }
    req = urllib.request.Request(
        BASE + "/v1/chat/completions",
        data=json.dumps(payload).encode(),
        headers={"Authorization": "Bearer " + get_key(), "Content-Type": "application/json"},
    )
    last_err = None
    for attempt in range(3):
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                d = json.loads(r.read())
            break
        except urllib.error.HTTPError as e:
            last_err = e
            body = e.read().decode()[:200]
            print(f"[!] attempt {attempt+1} HTTP {e.code}: {body}", file=sys.stderr)
            import time; time.sleep(5 * (attempt + 1))
    else:
        raise RuntimeError(f"3 attempts failed: {last_err}")
    content = d["choices"][0]["message"]["content"]
    # вырезаем JSON из возможной обёртки
    s, e = content.find("["), content.rfind("]")
    if s < 0 or e < 0:
        raise RuntimeError("модель не вернула JSON:\n" + content[:500])
    return json.loads(content[s : e + 1]), d.get("usage", {})

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("query")
    ap.add_argument("--handle")
    ap.add_argument("--model", default="grok-chat-fast")
    ap.add_argument("--days", type=int, default=7)
    ap.add_argument("--max", type=int, default=10)
    ap.add_argument("--json", help="save to file")
    a = ap.parse_args()
    posts, usage = search(a.query, a.model, a.handle, a.days, a.max)
    print(f"[+] {len(posts)} постов (tokens: {usage.get('total_tokens')})", file=sys.stderr)
    for p in posts:
        print(f"{p.get('handle','?')} | {p.get('date','?')} | {str(p.get('text',''))[:120]}")
    if a.json:
        with open(a.json, "w", encoding="utf-8") as f:
            json.dump(posts, f, ensure_ascii=False, indent=2)
        print(f"[+] saved {a.json}", file=sys.stderr)

if __name__ == "__main__":
    main()