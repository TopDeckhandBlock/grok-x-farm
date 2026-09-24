"""深度搜索 xAI 注册页的新 Action ID 格式"""
import requests, re, json
from urllib.parse import urljoin

s = requests.Session()
s.proxies = {"http": "http://127.0.0.1:7897", "https": "http://127.0.0.1:7897"}

html = s.get("https://accounts.x.ai/sign-up", timeout=15).text
print(f"HTML: {len(html)} bytes\n")

# === HTML search ===
print("=== HTML search ===")
patterns_html = [
    (r"\"action\"\s*:\s*\"([a-fA-F0-9]+)\"", "action JSON"),
    (r"next-action[^>]*\"([a-fA-F0-9]{20,})\"", "next-action attr"),
    (r"actionId[\":\s]+([a-fA-F0-9]{20,})", "actionId"),
    (r"release[\":\s]+\"([a-fA-F0-9]{30,})\"", "release"),
]
for pat, label in patterns_html:
    matches = re.findall(pat, html)
    if matches:
        print(f"  [{label}] {matches[:5]}")

# === check whether Server Action data is embedded in HTML ===
# Next.js may include the action ID in the RSC payload
for pat in [r'\"([a-fA-F0-9]{32,64})\"']:
    matches = re.findall(pat, html)
    # filter out obvious non-action-id matches
    hex_matches = [m for m in matches if all(c in '0123456789abcdefABCDEF' for c in m) and len(m) >= 32]
    if hex_matches:
        print(f"  [hex-ids] {list(set(hex_matches))[:10]}")

# === JS 文件搜索 ===
js_urls = list(set(urljoin("https://accounts.x.ai", m.group(0))
    for m in re.finditer(r"/_next/static/chunks/[^\"'\s>]+\.js", html)))
print(f"\n=== JS files: {len(js_urls)} ===\n")

patterns_js = [
    (r"7f[a-fA-F0-9]{40}", "7f+40hex (old)"),
    (r"release[:\s]+\"([a-fA-F0-9]{30,})\"", "release"),
    (r"\"action\"[:\s]+\"([a-fA-F0-9]{20,})\"", "action"),
    (r"actionId[\"':\s]+([a-fA-F0-9]{20,})", "actionId"),
    (r"createUserAndSession", "createUser (keyword)"),
    (r"sign-up[^}]*action[^}]*([a-fA-F0-9]{32,})", "sign-up action"),
    (r"\"([a-fA-F0-9]{40})\"", "40-char hex string"),
]

results = {}
for i, js_url in enumerate(js_urls[:40]):
    try:
        js = s.get(js_url, timeout=10).text
        for pat, label in patterns_js:
            matches = re.findall(pat, js, re.I)
            if matches:
                key = f"{js_url[-50:]}"
                if key not in results:
                    results[key] = []
                results[key].append((label, matches[:3]))
    except Exception as e:
        pass

for url_slug, findings in sorted(results.items()):
    print(f"  {url_slug}")
    for label, matches in findings:
        print(f"    [{label}] {matches}")
    print()

# === 搜索所有 40-char hex 字符串（可能的 action ID） ===
print("=== searching all JS for 40-char hex strings ===")
all_40hex = set()
for js_url in js_urls[:40]:
    try:
        js = s.get(js_url, timeout=10).text
        matches = re.findall(r"[a-fA-F0-9]{40}", js)
        for m in matches:
            all_40hex.add(m)
    except:
        pass
print(f"unique 40-hex strings: {len(all_40hex)}")
for h in sorted(all_40hex)[:20]:
    print(f"  {h}")
