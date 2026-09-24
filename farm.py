#!/usr/bin/env python3
"""farm.py — unified CLI for grok-x-farm.

Commands:
  python farm.py doctor              # health: gateway, pool, quotas, egress geo, config
  python farm.py reg --count 5       # register N accounts (email/captcha/proxy from config)
  python farm.py import              # import new SSO into gateway + convert to Build
  python farm.py parse "query" --max 20 --days 7 --json out.json
  python farm.py crawl --queries-file queries.txt --out tweets.json --target 100
  python farm.py keys                # create/show client key

Config: farm.config.json (copy from config/farm.config.example.json).
Env vars override config: G2A_KEY, G2A_ADMIN_PASS, G2A_BASE, YESCAPTCHA_KEY, GROK_PROXY, EMAIL_PROVIDER...
"""
import argparse, json, os, re, sys, time, urllib.request, urllib.error, uuid, subprocess
from datetime import date, datetime

HERE = os.path.dirname(os.path.abspath(__file__))
CFG_PATH = os.path.join(HERE, "farm.config.json")
CFG_EXAMPLE = os.path.join(HERE, "config", "farm.config.example.json")

def load_cfg():
    p = CFG_PATH if os.path.exists(CFG_PATH) else CFG_EXAMPLE
    return json.load(open(p, encoding="utf-8"))

def env_or(cfg_path, default=None):
    """cfg_path like 'gateway.client_key_env' -> read env var named by that config value."""
    node = CFG
    for part in cfg_path.split("."):
        node = node.get(part, {}) if isinstance(node, dict) else {}
    if isinstance(node, str) and node:
        return os.getenv(node, default)
    return default

CFG = load_cfg()
GATEWAY = os.getenv("G2A_BASE", CFG["gateway"]["base_url"])

def get_key():
    k = os.getenv(CFG["gateway"]["client_key_env"], "")
    if k: return k.strip()
    f = os.path.join(HERE, os.path.basename(CFG["gateway"]["client_key_file"]))
    if os.path.exists(f): return open(f, encoding="utf-8").read().strip()
    sys.exit("[!] no client key: set env or create via `farm.py keys`")

def admin_token():
    pw = os.getenv(CFG["gateway"]["admin_pass_env"], "")
    if not pw:
        f = CFG["gateway"]["admin_pass_file"]
        for cand in (os.path.join(HERE, os.path.basename(f)), f):
            if os.path.exists(cand):
                m = re.search(r"admin password: (\S+)", open(cand, encoding="utf-8").read())
                if m: pw = m.group(1); break
    if not pw: sys.exit("[!] no admin password (env or SECRETS file)")
    data = json.dumps({"username": CFG["gateway"]["admin_user"], "password": pw}).encode()
    req = urllib.request.Request(GATEWAY + "/api/admin/v1/auth/login", data=data,
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=15) as r:
        return json.loads(r.read())["data"]["tokens"]["accessToken"]

def api_admin(tok, path, data=None, ctype="application/json", timeout=60):
    req = urllib.request.Request(GATEWAY + path, method="POST" if data is not None else "GET")
    req.add_header("Authorization", "Bearer " + tok)
    if data is not None:
        if isinstance(data, (dict, list)): data = json.dumps(data).encode()
        req.add_header("Content-Type", ctype)
    with urllib.request.urlopen(req, data, timeout=timeout) as r:
        return r.status, r.read().decode()

def api_infer(payload, timeout=300):
    req = urllib.request.Request(GATEWAY + "/v1/chat/completions", data=json.dumps(payload).encode(),
                                 headers={"Authorization": "Bearer " + get_key(), "Content-Type": "application/json"})
    last = None
    for attempt in range(CFG["parser"]["retry_attempts"]):
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return json.loads(r.read())
        except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError) as e:
            last = e
            time.sleep(CFG["parser"]["retry_backoff_sec"] * (attempt + 1))
    raise RuntimeError(f"inference failed after retries: {last}")

# ---------- doctor ----------
def cmd_doctor(_a):
    ok = True
    print("== grok-x-farm doctor ==")
    # gateway
    try:
        with urllib.request.urlopen(GATEWAY + "/healthz", timeout=10) as r:
            print(f"[OK] gateway {GATEWAY} healthz {r.status}")
    except Exception as e:
        print(f"[FAIL] gateway: {e}"); ok = False; return 1
    # egress geo
    try:
        with urllib.request.urlopen(CFG["proxy"]["healthcheck_url"], timeout=10) as r:
            g = json.loads(r.read())
        cc = g.get("countryCode") or g.get("country", "?")
        geo_ok = cc in CFG["proxy"]["geo_whitelist"]
        print(f"[{'OK' if geo_ok else 'WARN'}] egress: {g.get('query')} ({cc}, {g.get('isp','?')})"
              + ("" if geo_ok else " — not in geo_whitelist; xAI may reject"))
        ok = ok and geo_ok
    except Exception as e:
        print(f"[WARN] egress check failed: {e}")
    # pool (paginated: count ALL records, first page can be all-build on big pools)
    try:
        tok = admin_token()
        web = build = total = 0
        wex = bex = 0  # exhausted
        seen_ids = set()
        page = 1
        while page <= 100:
            st, body = api_admin(tok, f"/api/admin/v1/accounts?page={page}&pageSize=200")
            items = (json.loads(body).get("data") or {}).get("items") or []
            if not items:
                break
            new = 0
            for a in items:
                aid = str(a.get("id"))
                if aid in seen_ids:
                    continue
                seen_ids.add(aid); new += 1; total += 1
                qs = (a.get("quota") or {}).get("status", "?")
                is_ex = qs == "waitingReset"
                if a.get("provider") == "grok_web":
                    web += 1
                    if is_ex: wex += 1
                elif a.get("provider") == "grok_build":
                    build += 1
                    if is_ex: bex += 1
            if new == 0 or len(items) < 200:
                break
            page += 1
        print(f"[{'OK' if web else 'FAIL'}] pool: {total} records (web:{web}[ex:{wex}] build:{build}[ex:{bex}])")
        ok = ok and web > 0
    except Exception as e:
        print(f"[FAIL] admin API: {e}"); ok = False
    # models
    try:
        req = urllib.request.Request(GATEWAY + "/v1/models", headers={"Authorization": "Bearer " + get_key()})
        with urllib.request.urlopen(req, timeout=15) as r:
            models = [m["id"] for m in json.loads(r.read())["data"]]
        need = {CFG["gateway"]["parse_model"], CFG["gateway"]["tool_model"]}
        miss = need - set(models)
        print(f"[{'OK' if not miss else 'FAIL'}] models: {len(models)} ({', '.join(sorted(models))})"
              + (f" MISSING: {miss}" if miss else ""))
        ok = ok and not miss
    except Exception as e:
        print(f"[FAIL] client key/models: {e}"); ok = False
    # quick parse probe
    try:
        d = api_infer({"model": CFG["gateway"]["parse_model"],
                       "messages": [{"role": "user", "content": "Reply with exactly: FARM_OK"}], "max_tokens": 20})
        txt = d["choices"][0]["message"]["content"]
        good = "FARM_OK" in txt
        print(f"[{'OK' if good else 'FAIL'}] inference probe: {txt[:40]!r}")
        ok = ok and good
    except Exception as e:
        print(f"[FAIL] inference probe: {e}"); ok = False
    print("== RESULT:", "ALL OK" if ok else "HAS FAILURES", "==")
    return 0 if ok else 1

# ---------- reg / import ----------
def find_reg_dir():
    for cand in (os.getenv("REG_DIR", ""), os.path.join(HERE, "autoreg"), os.path.join(HERE, "grok-auto"),
                 os.path.join(HERE, "grok-register"), r"C:\Users\User\grok-reg\grok-auto"):
        if cand and (os.path.exists(os.path.join(cand, "keys")) or os.path.exists(os.path.join(cand, "grok_auto.py"))):
            return cand
    sys.exit("[!] REG_DIR not found; set env REG_DIR or clone grok-auto next to farm.py")

def accounts_file():
    return os.path.join(find_reg_dir(), "keys", "accounts.txt")

def cmd_reg(a):
    reg = find_reg_dir()
    py = os.path.join(reg, ".venv", "Scripts", "python.exe")
    if not os.path.exists(py): py = os.path.join(reg, ".venv", "bin", "python")
    if not os.path.exists(py): py = sys.executable  # no venv -> current Python (Python311 has deps)
    script = "grok_auto.py" if os.path.exists(os.path.join(reg, "grok_auto.py")) else "grok_register_ttk.py"
    email_provider = os.getenv("EMAIL_PROVIDER", CFG["email"]["provider"])
    cmd = [py, script, "--count", str(a.count), "--email-provider", email_provider]
    if script == "grok_auto.py":
        cmd += ["--threads", str(a.threads)]  # не полагаться на stdin-хендшейк с input()
    env = dict(os.environ, PYTHONIOENCODING="utf-8", PYTHONUNBUFFERED="1")  # логи в реальном времени
    # proxy from config -> GROK_PROXY
    if CFG["proxy"]["mode"] == "single" and CFG["proxy"]["single"]:
        env["GROK_PROXY"] = CFG["proxy"]["single"]
    elif os.getenv("GROK_PROXY"):
        pass
    p = subprocess.Popen(cmd, cwd=reg, env=env, stdin=subprocess.PIPE,
                         stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, encoding="utf-8", errors="replace")
    if p.stdin:
        p.stdin.write(f"{a.threads}\n"); p.stdin.flush()
    for line in p.stdout or []:
        print("[REG]", line.rstrip())
    p.wait()
    print(f"[+] reg exit: {p.returncode}")
    if CFG["farm"]["auto_import_to_gateway"]:
        cmd_import(argparse.Namespace())

def marker_path():
    return os.path.join(HERE, "imported_sso.txt")

def cmd_import(_a):
    af = accounts_file()
    if not os.path.exists(af):
        print("[*] no accounts file"); return
    lines = set(open(af, encoding="utf-8").read().splitlines())
    imported = set(open(marker_path(), encoding="utf-8").read().splitlines()) if os.path.exists(marker_path()) else set()
    todo = [l for l in sorted(lines) if l.split(":")[0] not in imported]
    if not todo:
        print("[*] nothing new to import"); return
    tok = admin_token()
    ok = 0
    for line in todo:
        parts = line.split(":"); email, sso = parts[0], parts[-1]
        boundary = uuid.uuid4().hex
        mp = (f"--{boundary}\r\nContent-Disposition: form-data; name=\"file\"; filename=\"sso.txt\"\r\n"
              f"Content-Type: text/plain\r\n\r\n{sso}\r\n--{boundary}--\r\n").encode()
        try:
            st, body = api_admin(tok, "/api/admin/v1/accounts/web/import", mp,
                                 f"multipart/form-data; boundary={boundary}")
            m = re.search(r'"created":(\d+).*?"synced":(\d+)', body)
            print(f"[+] {email} -> created:{m.group(1) if m else '?'} synced:{m.group(2) if m else '?'}")
            ok += 1
            with open(marker_path(), "a", encoding="utf-8") as f: f.write(email + "\n")
        except Exception as e:
            print(f"[!] {email}: {e}")
        time.sleep(1)
    if ok and CFG["farm"]["auto_convert_to_build"]:
        st, body = api_admin(tok, "/api/admin/v1/accounts/web/convert-to-build", {"all": True, "strategy": "missing"}, timeout=180)
        m = re.search(r'"created":(\d+)', body)
        print(f"[+] convert web->build created:{m.group(1) if m else '?'}")
    print(f"[DONE] imported {ok}/{len(todo)}")

# ---------- keys ----------
def cmd_keys(_a):
    tok = admin_token()
    st, body = api_admin(tok, "/api/admin/v1/client-keys", {"name": f"farm-{int(time.time())}"})
    d = json.loads(body)
    secret = d.get("data", {}).get("secret") or d.get("data", {}).get("key", {}).get("secret")
    if not secret:
        # secret may be top-level on create
        secret = d.get("secret")
    print("[+] client key:", secret)
    open(os.path.join(HERE, "g2a_key.txt"), "w").write(secret or "")
    print("[+] saved to g2a_key.txt")

# ---------- parse / crawl ----------
def build_prompt(query, n, days, handle=None):
    p = (f"Use your live X search. Find {n} most recent REAL posts on X about {query!r} "
         f"from the last {days} days.")
    if handle: p += f" Only posts authored by @{handle}."
    p += (' STRICT: Reply ONLY with a JSON array, no prose. Each element: '
          '{"handle":"@user","date":"YYYY-MM-DD","text":"full post text","likes":<int or null>,'
          '"url":"https://x.com/user/status/ID"}')
    return p

def parse_json_array(text):
    if not text: return []
    s, e = text.find("["), text.rfind("]")
    if s < 0 or e < 0: return []
    try:
        d = json.loads(text[s:e + 1])
        return d if isinstance(d, list) else []
    except Exception:
        return []

def search_once(query, n, days, handle=None):
    d = api_infer({"model": CFG["gateway"]["parse_model"],
                   "messages": [{"role": "user", "content": build_prompt(query, n, days, handle)}],
                   "max_tokens": 4000})
    return parse_json_array(d["choices"][0]["message"]["content"])

def tweet_key(p):
    m = re.search(r"/status/(\d+)", str(p.get("url", "")))
    return m.group(1) if m else json.dumps(p, sort_keys=True)[:100]

def parse_post_date(raw):
    """Parse common post-date formats -> datetime.date, or None if unparseable."""
    if not raw:
        return None
    s = str(raw).strip()
    if s.isdigit():
        try:
            return datetime.utcfromtimestamp(int(s)).date()
        except (ValueError, OSError, OverflowError):
            return None
    for fmt in ("%a %b %d %H:%M:%S %z %Y", "%Y-%m-%d", "%b %d, %Y", "%B %d, %Y",
                "%d.%m.%Y", "%d %b %Y", "%d %B %Y", "%Y/%m/%d", "%m/%d/%Y"):
        try:
            return datetime.strptime(s, fmt).date()
        except ValueError:
            continue
    return None

def date_within(post, days, today=None):
    """True if post date is inside the window. Unparseable dates are kept
    (silent data loss is worse); +1 day grace on both edges for TZ drift."""
    if not days or days <= 0:
        return True
    d = parse_post_date(post.get("date"))
    if d is None:
        return True
    age = ((today or date.today()) - d).days
    return -1 <= age <= days + 1

def parse_likes(raw):
    """Normalize likes ('18', '1.2K', 18) -> int or None."""
    if raw is None:
        return None
    if isinstance(raw, (int, float)):
        return int(raw)
    s = str(raw).strip().replace(",", "").replace(" ", "")
    if not s:
        return None
    try:
        mult = 1
        if s[-1] in "Kk":
            mult, s = 1000, s[:-1]
        elif s[-1] in "Mm":
            mult, s = 1000000, s[:-1]
        return int(float(s) * mult)
    except ValueError:
        return None

def load_seen(path):
    """Load seen-state {tweet_key: entry}; {} when missing/corrupt."""
    if not path or not os.path.exists(path):
        return {}
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (OSError, json.JSONDecodeError) as exc:
        print(f"[WARN] state unreadable, starting fresh: {exc}", file=sys.stderr)
        return {}

def merge_seen(state, entries):
    """Merge entries into seen-state in place; return count of new keys."""
    new = 0
    for e in entries:
        k = tweet_key(e)
        if k and k not in state:
            state[k] = e
            new += 1
    return new

def cmd_parse(a):
    posts = search_once(a.query, a.max, a.days, a.handle)
    print(f"[+] {len(posts)} posts", file=sys.stderr)
    for p in posts:
        print(f"{p.get('handle','?')} | {p.get('date','?')} | {str(p.get('text',''))[:100]}")
    if a.json:
        json.dump(posts, open(a.json, "w", encoding="utf-8"), ensure_ascii=False, indent=2)
        print(f"[+] saved {a.json}", file=sys.stderr)

def cmd_crawl(a):
    queries = [q.strip() for q in open(a.queries_file, encoding="utf-8") if q.strip() and not q.startswith("#")]
    seen, allp = set(), []
    per = CFG["parser"]["posts_per_query"]
    state = load_seen(a.state) if a.state else {}
    if state:
        seen.update(state.keys())
        print(f"[i] state: {len(state)} tweets from previous runs loaded", file=sys.stderr)
    for i, q in enumerate(queries, 1):
        if len(allp) >= a.target: break
        t0 = time.time()
        try:
            posts = search_once(q, per, a.days)
            if a.days > 0:
                before = len(posts)
                posts = [p for p in posts if date_within(p, a.days)]
                if len(posts) < before:
                    print(f"[i] {q!r}: {before - len(posts)} post(s) outside {a.days}d window dropped", file=sys.stderr)
        except Exception as e:
            print(f"[{i}] {q!r}: ERROR {e}", file=sys.stderr); continue
        added = 0
        for p in posts:
            k = tweet_key(p)
            if k in seen: continue
            seen.add(k); allp.append(p); added += 1
        print(f"[{i}/{len(queries)}] {q!r}: +{added} (total {len(allp)}) {time.time()-t0:.0f}s", file=sys.stderr)
        time.sleep(CFG["parser"].get("delay_between_queries_sec", 3))
    if a.state:
        new_n = merge_seen(state, allp)
        allp = list(state.values())
        st = os.path.abspath(a.state)
        os.makedirs(os.path.dirname(st) or ".", exist_ok=True)
        tmp = st + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(state, f, ensure_ascii=False, indent=2)
        os.replace(tmp, st)
        print(f"[STATE] +{new_n} new, {len(allp)} total across runs -> {a.state}", file=sys.stderr)
    tmp_out = os.path.abspath(a.out) + ".tmp"
    with open(tmp_out, "w", encoding="utf-8") as f:
        json.dump(allp, f, ensure_ascii=False, indent=2)
    os.replace(tmp_out, os.path.abspath(a.out))
    print(f"[DONE] {len(allp)} tweets -> {a.out}", file=sys.stderr)
    # verification sample: exists + date match + likes proximity, random (no fixed seed)
    vs = CFG["parser"]["verify_sample_size"]
    if vs and allp:
        import random
        pool = [p for p in allp if re.search(r"/status/\d+", str(p.get("url", "")))]
        if pool:
            sample = random.sample(pool, min(vs, len(pool)))
            print(f"[i] verifying {len(sample)} random tweets via fxtwitter...", file=sys.stderr)
            n_exist = n_date = n_likes = 0
            for p in sample:
                m = re.search(r"x\.com/([^/]+)/status/(\d+)", p["url"])
                if not m: continue
                try:
                    req = urllib.request.Request(f"https://api.fxtwitter.com/{m.group(1)}/status/{m.group(2)}",
                                                 headers={"User-Agent": "Mozilla/5.0"})
                    with urllib.request.urlopen(req, timeout=15) as r:
                        tw = json.loads(r.read()).get("tweet") or {}
                except Exception as e:
                    print(f"[VERIFY-FAIL] {m.group(1)}/{m.group(2)}: {e}", file=sys.stderr)
                    continue
                if not tw:
                    print(f"[VERIFY-FAIL] {m.group(1)}/{m.group(2)}: not found", file=sys.stderr)
                    continue
                n_exist += 1
                pd_, fd_ = parse_post_date(p.get("date")), parse_post_date(tw.get("created_at"))
                date_ok = (pd_ == fd_) if (pd_ and fd_) else None
                if date_ok: n_date += 1
                pl, fl = parse_likes(p.get("likes")), parse_likes(tw.get("likes"))
                # likes only grow: parsed snapshot must be <= live, within 30%+3
                likes_ok = (pl <= fl and (fl - pl) <= max(3, fl * 0.3)) if (pl is not None and fl is not None) else None
                if likes_ok: n_likes += 1
                d_s = {True: "date✓", False: "date✗", None: "date?"}[date_ok]
                l_s = {True: "likes✓", False: "likes✗", None: "likes?"}[likes_ok]
                print(f"[VERIFY] {m.group(1)}/{m.group(2)}: exists {d_s} {pd_}/{fd_} {l_s} {pl}/{fl}", file=sys.stderr)
            n = len(sample)
            print(f"[VERIFY] fxtwitter random sample {n}: exists {n_exist}/{n}, date-match {n_date}/{n}, likes-close {n_likes}/{n}", file=sys.stderr)

def main():
    ap = argparse.ArgumentParser(description="grok-x-farm CLI")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("doctor")
    r = sub.add_parser("reg"); r.add_argument("--count", type=int, default=CFG["farm"]["count_per_run"]); r.add_argument("--threads", type=int, default=CFG["farm"]["threads"])
    sub.add_parser("import")
    sub.add_parser("keys")
    p = sub.add_parser("parse"); p.add_argument("query"); p.add_argument("--max", type=int, default=15); p.add_argument("--days", type=int, default=CFG["parser"]["days_window"]); p.add_argument("--handle"); p.add_argument("--json")
    c = sub.add_parser("crawl"); c.add_argument("--queries-file", default="queries.txt"); c.add_argument("--out", default="tweets.json"); c.add_argument("--target", type=int, default=100); c.add_argument("--days", type=int, default=CFG["parser"]["days_window"]); c.add_argument("--state", default=None, help="seen-state JSON for cross-run dedup (output merges all runs)")
    a = ap.parse_args()
    {"doctor": cmd_doctor, "reg": cmd_reg, "import": cmd_import, "keys": cmd_keys,
     "parse": cmd_parse, "crawl": cmd_crawl}[a.cmd](a)

if __name__ == "__main__":
    main()