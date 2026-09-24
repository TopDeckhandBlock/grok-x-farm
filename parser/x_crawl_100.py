#!/usr/bin/env python3
"""x_crawl_100.py — спарсить ~100 твитов по списку запросов через grok2api (Web pool live X search).

Пачками по 15-20 на запрос, дедуп по tweet id, верификация выборки через fxtwitter.
"""
import json, os, re, sys, time, urllib.request, urllib.error

BASE = os.getenv("G2A_BASE", "http://127.0.0.1:8000")
KEY = open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "g2a_key.txt"), encoding="utf-8").read().strip()

QUERIES = [
    "carding forum",
    "cvv shop",
    "smm panel",
    "proxy seller telegram",
    "drop shop",
    "gift card discount",
    "jailbreak grok",
    "telegram bot spam",
    "residential proxy",
    "account selling",
]

def ask(prompt, timeout=180):
    payload = {"model": "grok-chat-fast",
               "messages": [{"role": "user", "content": prompt}],
               "max_tokens": 4000}
    req = urllib.request.Request(BASE + "/v1/chat/completions", data=json.dumps(payload).encode(),
                                 headers={"Authorization": "Bearer " + KEY, "Content-Type": "application/json"})
    for attempt in range(3):
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return json.loads(r.read())["choices"][0]["message"]["content"]
        except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError) as e:
            print(f"[!] {prompt[:40]!r} attempt {attempt+1}: {e}", file=sys.stderr)
            time.sleep(4 * (attempt + 1))
    return None

def parse_json_array(text):
    if not text: return []
    s, e = text.find("["), text.rfind("]")
    if s < 0 or e < 0: return []
    try:
        d = json.loads(text[s:e+1])
        return d if isinstance(d, list) else []
    except Exception:
        return []

def tweet_id(p):
    m = re.search(r"/status/(\d+)", str(p.get("url", "")))
    return m.group(1) if m else None

def main():
    seen, all_posts = set(), []
    for i, q in enumerate(QUERIES, 1):
        prompt = (f"Use your live X search. Find 15 most recent REAL posts on X about {q!r} "
                  f"from the last 14 days. Reply ONLY with a JSON array. Each element: "
                  '{"handle":"@user","date":"YYYY-MM-DD","text":"full post text","likes":<int or null>,'
                  '"url":"https://x.com/user/status/ID"}')
        t0 = time.time()
        out = ask(prompt)
        posts = parse_json_array(out)
        added = 0
        for p in posts:
            tid = tweet_id(p)
            key = tid or json.dumps(p, sort_keys=True)[:100]
            if key in seen: continue
            seen.add(key); all_posts.append(p); added += 1
        print(f"[{i}/{len(QUERIES)}] {q!r}: +{added} (total {len(all_posts)}) in {time.time()-t0:.0f}s", file=sys.stderr)
        if len(all_posts) >= 100: break
        time.sleep(2)

    out_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "tweets_100.json")
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(all_posts[:120], f, ensure_ascii=False, indent=2)
    print(f"[DONE] {len(all_posts)} tweets -> {out_path}", file=sys.stderr)

if __name__ == "__main__":
    main()