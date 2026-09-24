#!/usr/bin/env python3
"""setup_newapi_grok.py — авто-деплой "new-api для grok" (панель поверх grok2api).

Полный цикл без ручных шагов:
  1. скачать new-api бинарь (если нет)
  2. запустить (SQLite, :3000)
  3. зарегистрировать root + повысить до role=100 (через SQLite — first user не авто-root в v1.0.0)
  4. вставить канал grok-pool -> grok2api + abilities (роутинг моделей)
  5. включить SelfUseModeEnabled (без биллинга) + дать root бесконечную квоту
  6. создать consumer-токен (sk-xxx)
  7. прогнать e2e-тест всех grok-моделей

Запуск:  python setup_newapi_grok.py
Env:     G2A_KEY (или файл g2a_key.txt рядом), NEWAPI_PORT, GROK2API_URL
Результ: new-api на :3000, consumer key в consumer_key.txt, creds в NEWAPI_CREDS.txt

Зачем SQLite-правки: new-api v1.0.0-rc.40 (а) не делает первого юзера root'ом,
(б) POST /api/channel/ отдаёт "channel cannot be empty" (bind-баг) — поэтому
канал + abilities + role + quota пишутся напрямую в one-api.db. Надёжно и идемпотентно.
"""
import os, sys, json, time, sqlite3, subprocess, urllib.request, urllib.error

HERE = os.path.dirname(os.path.abspath(__file__))
NEWAPI_VER = "v1.0.0-rc.40"
NEWAPI_DIR = os.getenv("NEWAPI_DIR", os.path.join(HERE, "new-api"))
NEWAPI_EXE = os.path.join(NEWAPI_DIR, "new-api.exe")
NEWAPI_URL = os.getenv("NEWAPI_URL", "http://127.0.0.1:3000")
GROK2API_URL = os.getenv("GROK2API_URL", "http://127.0.0.1:8000")
DB = os.path.join(NEWAPI_DIR, "one-api.db")
ROOT_PW = os.getenv("NEWAPI_ROOT_PW")
if not ROOT_PW:
    sys.exit("[!] set NEWAPI_ROOT_PW env var (e.g. export NEWAPI_ROOT_PW=... or set NEWAPI_ROOT_PW=... )")
GROK_MODELS = ["grok-4.6", "grok-4.7", "grok-4.5", "grok-chat-fast", "grok-composer-2.5-fast"]


def get_g2a_key():
    k = os.getenv("G2A_KEY")
    if k:
        return k.strip()
    for p in [os.path.join(HERE, "g2a_key.txt"),
              r"C:\Users\User\tmp\grok-x-farm\g2a_key.txt"]:
        if os.path.exists(p):
            return open(p, encoding="utf-8").read().strip()
    sys.exit("[!] no G2A_KEY (env or g2a_key.txt)")


def download_newapi():
    if os.path.exists(NEWAPI_EXE) and os.path.getsize(NEWAPI_EXE) > 130_000_000:
        print(f"[1] new-api binary exists ({os.path.getsize(NEWAPI_EXE)//1024//1024}MB)")
        return
    os.makedirs(NEWAPI_DIR, exist_ok=True)
    url = f"https://github.com/QuantumNous/new-api/releases/download/{NEWAPI_VER}/new-api-{NEWAPI_VER}.exe"
    print(f"[1] downloading new-api {NEWAPI_VER} ...")
    req = urllib.request.Request(url, headers={"User-Agent": "farm"})
    start = os.path.getsize(NEWAPI_EXE) if os.path.exists(NEWAPI_EXE) else 0
    if start:
        req.add_header("Range", f"bytes={start}-")
    with urllib.request.urlopen(req, timeout=600) as r:
        with open(NEWAPI_EXE, "ab" if start else "wb") as f:
            while True:
                chunk = r.read(262144)
                if not chunk:
                    break
                f.write(chunk)
    print(f"    downloaded {os.path.getsize(NEWAPI_EXE)//1024//1024}MB")


def start_newapi():
    # kill existing on :3000
    if os.name == "nt":
        r = subprocess.run(["powershell", "-NoProfile", "-Command",
            "Get-NetTCPConnection -LocalPort 3000 -State Listen -ErrorAction SilentlyContinue | Select-Object -ExpandProperty OwningProcess"],
            capture_output=True, text=True, timeout=15)
        for pid in [p.strip() for p in r.stdout.splitlines() if p.strip().isdigit()]:
            subprocess.run(["taskkill", "/F", "/PID", pid, "/T"], capture_output=True, timeout=10)
        time.sleep(2)
    p = subprocess.Popen([NEWAPI_EXE], cwd=NEWAPI_DIR,
                         creationflags=0x00000008 | 0x00000200 if os.name == "nt" else 0,
                         stdin=subprocess.DEVNULL,
                         stdout=open(os.path.join(NEWAPI_DIR, "new-api.log"), "ab"),
                         stderr=subprocess.STDOUT)
    print(f"[2] new-api started pid={p.pid}, waiting for :3000 ...")
    for _ in range(20):
        time.sleep(2)
        try:
            urllib.request.urlopen(NEWAPI_URL + "/api/status", timeout=5)
            print("    UP")
            return p
        except Exception:
            pass
    sys.exit("[!] new-api did not come up — see new-api.log")


def api(method, path, data=None, token=None):
    body = json.dumps(data).encode() if data is not None else None
    req = urllib.request.Request(NEWAPI_URL + path, data=body, method=method)
    req.add_header("Content-Type", "application/json")
    if token:
        req.add_header("Authorization", "Bearer " + token)
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return json.loads(r.read())
    except urllib.error.HTTPError as e:
        return {"_err": e.code, "_b": e.read().decode()[:200]}


def setup_root():
    # register (first user) — may already exist
    api("POST", "/api/user/register",
        {"username": "root", "password": ROOT_PW, "password2": ROOT_PW, "email": "root@grok-farm.local"})
    r = api("POST", "/api/user/login", {"username": "root", "password": ROOT_PW})
    tok = r.get("data", {}).get("access_token")
    if not tok:
        sys.exit(f"[!] login failed: {r}")
    return tok


def db_setup(g2a_key):
    """role=100, channel+abilities, self-use, quota — напрямую в SQLite (API v1.0.0 глючит)."""
    # stop new-api to edit DB safely
    if os.name == "nt":
        r = subprocess.run(["powershell", "-NoProfile", "-Command",
            "Get-NetTCPConnection -LocalPort 3000 -State Listen -ErrorAction SilentlyContinue | Select-Object -ExpandProperty OwningProcess"],
            capture_output=True, text=True, timeout=15)
        for pid in [p.strip() for p in r.stdout.splitlines() if p.strip().isdigit()]:
            subprocess.run(["taskkill", "/F", "/PID", pid, "/T"], capture_output=True, timeout=10)
        time.sleep(2)
    con = sqlite3.connect(DB)
    cur = con.cursor()
    cur.execute("UPDATE users SET role=100, quota=999999999999 WHERE username='root'")
    now = int(time.time())
    cur.execute("SELECT id FROM channels WHERE name='grok-pool'")
    ex = cur.fetchone()
    models = ",".join(GROK_MODELS)
    if ex:
        cid = ex[0]
        cur.execute("UPDATE channels SET key=?,base_url=?,models=?,status=1 WHERE id=?",
                    (g2a_key, GROK2API_URL, models, cid))
    else:
        cur.execute("""INSERT INTO channels
            (type,key,test_model,status,name,weight,created_time,base_url,other,models,`group`,
             model_mapping,status_code_mapping,priority,auto_ban,setting,channel_info,settings)
            VALUES (1,?,'grok-4.6',1,'grok-pool',1,?,?,'',?,'default','','',0,0,'{}','{}','{}')""",
            (g2a_key, now, GROK2API_URL, models))
        cid = cur.lastrowid
    cur.execute("DELETE FROM abilities WHERE channel_id=?", (cid,))
    for m in GROK_MODELS:
        cur.execute("INSERT OR REPLACE INTO abilities (`group`,model,channel_id,enabled,priority,weight,tag) "
                    "VALUES ('default',?,?,1,0,1,'')", (m, cid))
    con.commit()
    con.close()
    print(f"[4] DB: root=100, channel id={cid} + {len(GROK_MODELS)} abilities, quota=inf")
    return cid


def enable_selfuse(tok):
    r = api("PUT", "/api/option/", {"key": "SelfUseModeEnabled", "value": "true"}, token=tok)
    print(f"[5] SelfUseModeEnabled=true: {r.get('success', r)}")


def make_consumer_token(tok):
    api("POST", "/api/token/", {"name": "grok-farm-main", "remain_quota": 500000,
                                "expired_time": -1, "unlimited_quota": True}, token=tok)
    # fetch full key from DB (API masks it)
    con = sqlite3.connect(DB); cur = con.cursor()
    cur.execute("SELECT key FROM tokens WHERE name='grok-farm-main' ORDER BY id DESC LIMIT 1")
    row = cur.fetchone(); con.close()
    return f"sk-{row[0]}" if row else None


def e2e_test(ckey):
    print("[7] e2e test (consumer -> new-api -> grok2api -> grok):")
    ok = 0
    for m in GROK_MODELS:
        payload = {"model": m, "messages": [{"role": "user", "content": "Reply one word: OK"}], "max_tokens": 20}
        req = urllib.request.Request(NEWAPI_URL + "/v1/chat/completions", data=json.dumps(payload).encode(),
                                     headers={"Authorization": "Bearer " + ckey, "Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=60) as r:
                json.loads(r.read()); print(f"    {m}: OK"); ok += 1
        except Exception as e:
            print(f"    {m}: FAIL {type(e).__name__}")
    return ok


def main():
    g2a = get_g2a_key()
    download_newapi()
    start_newapi()
    tok = setup_root()
    print("[3] root registered + logged in")
    db_setup(g2a)
    start_newapi()           # restart to load DB changes
    tok = setup_root()       # fresh token
    enable_selfuse(tok)
    ckey = make_consumer_token(tok)
    open(os.path.join(NEWAPI_DIR, "consumer_key.txt"), "w").write(ckey or "")
    ok = e2e_test(ckey)
    creds = (f"new-api panel : {NEWAPI_URL}  (root / {ROOT_PW})\n"
             f"consumer key  : {ckey}\n"
             f"channel       : grok-pool -> {GROK2API_URL} ({','.join(GROK_MODELS)})\n"
             f"self-use mode : ON (no billing)\n"
             f"e2e           : {ok}/{len(GROK_MODELS)} models OK\n"
             f"usage         : OpenAI-compat -> {NEWAPI_URL}/v1, key {ckey}, model grok-4.6\n")
    open(os.path.join(NEWAPI_DIR, "NEWAPI_CREDS.txt"), "w").write(creds)
    print("\n" + creds)


if __name__ == "__main__":
    main()
