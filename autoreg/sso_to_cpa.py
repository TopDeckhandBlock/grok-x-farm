"""
SSO -> CPA PKCE 转换器
用 curl_cffi 走完整 PKCE 流程，包括 OAuth consent 表单提交
"""
import json, os, sys, time, hashlib, base64, secrets, urllib.parse, argparse, re
import threading
from concurrent.futures import ThreadPoolExecutor
from curl_cffi import requests as cf_req, CurlOpt

if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

CLIENT_ID = "b1a00492-073a-47ea-816f-4c329264a828"
REDIRECT_URI = "http://127.0.0.1:56121/callback"
TOKEN_ENDPOINT = "https://auth.x.ai/oauth2/token"
AUTHORIZE_URL = "https://auth.x.ai/oauth2/authorize"
SCOPE = "openid profile email offline_access grok-cli:access api:access"
PROXY = os.getenv("GROK_PROXY") or ""
if sys.platform == "win32":
    AUTH_DIR = os.getenv("CPA_AUTHS_DIR") or os.path.join(os.path.dirname(os.path.abspath(__file__)), "auths")
else:
    AUTH_DIR = os.getenv("CPA_AUTHS_DIR") or os.path.join(os.path.dirname(os.path.abspath(__file__)), "auths")
KEYS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "keys")


def load_sso_tokens():
    grok_file = os.path.join(KEYS_DIR, "grok.txt")
    if not os.path.exists(grok_file):
        return []
    with open(grok_file, encoding="utf-8") as f:
        return [line.strip() for line in f if line.strip()]


def load_accounts():
    acc_file = os.path.join(KEYS_DIR, "accounts.txt")
    if not os.path.exists(acc_file):
        return []
    accounts = []
    with open(acc_file, encoding="utf-8") as f:
        for line in f:
            parts = line.strip().split(":")
            if len(parts) >= 3:
                accounts.append({"email": parts[0], "password": parts[1], "sso": parts[2]})
    return accounts


def sso_to_cpa(sso_token, email=""):
    """用 SSO cookie 走 PKCE 流程，获取 CPA access_token。"""
    code_verifier = secrets.token_urlsafe(64)[:128]
    code_challenge = base64.urlsafe_b64encode(
        hashlib.sha256(code_verifier.encode()).digest()
    ).rstrip(b"=").decode()

    sess = cf_req.Session(impersonate="chrome120")
    if PROXY:
        sess.proxies = {"http": PROXY, "https": PROXY}
    sess.cookies.set("sso", sso_token, domain=".x.ai")

    params = {
        "response_type": "code",
        "client_id": CLIENT_ID,
        "redirect_uri": REDIRECT_URI,
        "code_challenge": code_challenge,
        "code_challenge_method": "S256",
        "scope": SCOPE,
        "state": secrets.token_urlsafe(16),
    }
    auth_url = f"{AUTHORIZE_URL}?{urllib.parse.urlencode(params)}"

    print(f"  [{email}] запускаю PKCE-авторизацию...")
    try:
        r = sess.get(auth_url, allow_redirects=False, timeout=30,
                     headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"})
    except Exception as e:
        print(f"  [{email}] ошибка запроса авторизации: {e}")
        return None

    code = None
    if r.status_code in (301, 302, 303, 307, 308):
        location = r.headers.get("Location", "")
        if location.startswith("/"):
            parsed_base = urllib.parse.urlparse(r.url)
            location = f"{parsed_base.scheme}://{parsed_base.netloc}{location}"
        if REDIRECT_URI in location or "code=" in location:
            parsed_loc = urllib.parse.urlparse(location)
            params_loc = urllib.parse.parse_qs(parsed_loc.query)
            code = params_loc.get("code", [None])[0]
        elif "/oauth2/consent" in location or "/consent" in location:
            # 需要 consent 页面 -> 用 requests 库提交（curl_cffi 的 cookie 在 POST 时不可靠）
            code = _handle_consent(sess, location, sso_token, code_verifier, email=email)

    if not code:
        print(f"  [{email}] [FAIL] нет кода авторизации (HTTP {r.status_code}, "
              f"Location={r.headers.get('Location', '')[:120]})")
        return None

    print(f"  [{email}] получен код авторизации: {code[:20]}...")
    print(f"  [{email}] обмениваю на токен...")
    try:
        r = sess.post(TOKEN_ENDPOINT, data={
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": REDIRECT_URI,
            "code_verifier": code_verifier,
            "client_id": CLIENT_ID,
        }, timeout=30)
    except Exception as e:
        print(f"  [{email}] ошибка обмена токена: {e}")
        return None

    if r.status_code != 200:
        print(f"  [{email}] [FAIL] ошибка токена: {r.status_code} {r.text[:200]}")
        return None

    data = r.json()
    access_token = data.get("access_token")
    if not access_token:
        print(f"  [{email}] [FAIL] нет access_token в ответе: {json.dumps(data)[:200]}")
        return None

    print(f"  [{email}] [OK] CPA-токен получен (действует {data.get('expires_in', '?')}с)")
    return {
        "access_token": access_token,
        "refresh_token": data.get("refresh_token", ""),
        "expires_in": data.get("expires_in", 21600),
        "id_token": data.get("id_token", ""),
        "token_type": data.get("token_type", "Bearer"),
    }


def _handle_consent_http(sess, location, email=""):
    """Submit the OAuth consent form over HTTP (action=approve), no browser needed."""
    import re as _re
    try:
        print(f"  [{email}] загружаю страницу consent...")
        r = sess.get(location, timeout=30)
        if r.status_code != 200:
            print(f"  [{email}] [FAIL] страница consent HTTP {r.status_code}")
            return None
        form = _re.search(r'<form[^>]*action=["\']([^"\']+)["\'][^>]*>(.*?)</form>', r.text, _re.S)
        if not form:
            print(f"  [{email}] [FAIL] нет формы на странице consent")
            return None
        action, body = form.group(1), form.group(2)
        action_url = urllib.parse.urljoin(location, action)
        data = {}
        for tag in _re.findall(r'<input\b[^>]*>', body):
            name_m = _re.search(r'name=["\']([^"\']+)["\']', tag)
            if not name_m:
                continue
            val_m = _re.search(r'value=["\']([^"\']*)["\']', tag)
            data[name_m.group(1)] = val_m.group(1) if val_m else ""
        data["action"] = "approve"
        print(f"  [{email}] отправляю форму consent (action=approve)...")
        r = sess.post(action_url, data=data, allow_redirects=False, timeout=30)
        for _ in range(6):
            if r.status_code in (301, 302, 303, 307, 308):
                loc = r.headers.get("Location", "")
                parsed = urllib.parse.urlparse(loc)
                qs = urllib.parse.parse_qs(parsed.query)
                if qs.get("code"):
                    print(f"  [{email}] получен код авторизации")
                    return qs["code"][0]
                if "error=" in loc:
                    print(f"  [{email}] ошибка авторизации: {loc[:120]}")
                    return None
                r = sess.get(urllib.parse.urljoin(r.url, loc), allow_redirects=False, timeout=30)
            else:
                break
        print(f"  [{email}] [FAIL] нет кода после отправки consent (HTTP {r.status_code})")
        return None
    except Exception as e:
        print(f"  [{email}] ошибка HTTP consent: {e}")
        return None


def _handle_consent(sess, location, sso_token, code_verifier, email=""):
    """Handle the OAuth consent page.
    Windows: ruyipage (Firefox) browser flow; other platforms: pure-HTTP form submit.
    """
    try:
        if os.path.isdir("D:/ruyipage"): sys.path.insert(0, "D:/ruyipage")

        from ruyipage import FirefoxPage, FirefoxOptions
    except ImportError:
        return _handle_consent_http(sess, location, email)

    # 重新生成 auth_url（用传入的 code_verifier，保证 code_challenge 一致）
    _code_challenge = base64.urlsafe_b64encode(
        hashlib.sha256(code_verifier.encode()).digest()
    ).rstrip(b"=").decode()
    _params = {
        "response_type": "code",
        "client_id": CLIENT_ID,
        "redirect_uri": REDIRECT_URI,
        "code_challenge": _code_challenge,
        "code_challenge_method": "S256",
        "scope": SCOPE,
        "state": secrets.token_urlsafe(16),
    }
    _auth_url = f"{AUTHORIZE_URL}?{urllib.parse.urlencode(_params)}"

    opts = FirefoxOptions()
    opts.set_browser_path(r"C:\Program Files\Mozilla Firefox\firefox.exe")
    opts.headless(True)

    page = None
    try:
        page = FirefoxPage(opts)

        # 先访问 auth.x.ai（CF 防护较弱），设置 cookie
        print(f"  [{email}] устанавливаю cookie...")
        page.get("https://auth.x.ai/")
        time.sleep(2)
        if "Attention Required" in page.title:
            page.handle_cloudflare_challenge(timeout=30)
            time.sleep(2)
        page.set_cookies({"name": "sso", "value": sso_token, "domain": ".x.ai"})

        # 导航到授权 URL（auth.x.ai CF 防护较弱，能绕过）
        print(f"  [{email}] перехожу на страницу авторизации...")
        page.get(_auth_url)
        time.sleep(3)

        # 如果被重定向到 consent 页面并触发 CF，处理挑战
        if "Attention Required" in page.title:
            print(f"  [{email}] прохожу CF-челлендж...")
            page.handle_cloudflare_challenge(timeout=60)
            time.sleep(3)

        # 等待并点击 Allow 按钮
        import urllib.parse as _up
        for _ in range(30):
            time.sleep(1)
            current_url = page.url
            if REDIRECT_URI in current_url or "code=" in current_url:
                parsed_loc = _up.urlparse(current_url)
                params_loc = _up.parse_qs(parsed_loc.query)
                code = params_loc.get("code", [None])[0]
                if code:
                    print(f"  [{email}] получен код авторизации")
                    return code
                if "error=" in current_url:
                    print(f"  [{email}] ошибка авторизации: {current_url[:120]}")
                    return None
            # 尝试点击 Allow 按钮
            try:
                # 先等待页面完全加载
                page.wait_loading()
                html = page.html.lower()
                if "allow" in html:
                    # 方法1: 用原生 JS 点击 Allow 按钮
                    clicked = page.run_js("""
                        (() => {
                            var btn = Array.from(document.querySelectorAll('button'))
                                .find(b => b.textContent.trim() === 'Allow');
                            if (btn) {
                                btn.click();
                                return true;
                            }
                            return false;
                        })()
                    """)
                    if clicked:
                        print(f"  [{email}] нажимаю кнопку Allow...")
                        time.sleep(3)
                        # 检查是否已跳转
                        if REDIRECT_URI in page.url or "code=" in page.url:
                            continue

                    # 方法2: 直接提交表单 + action=approve
                    print(f"  [{email}] отправляю форму напрямую...")
                    page.run_js("""
                        (() => {
                            var form = document.querySelector('form');
                            if (form) {
                                var input = document.createElement('input');
                                input.type = 'hidden';
                                input.name = 'action';
                                input.value = 'approve';
                                form.appendChild(input);
                                form.submit();
                                return true;
                            }
                            return false;
                        })()
                    """)
                    time.sleep(3)
            except:
                pass

        print(f"  [{email}] [FAIL] таймаут браузера")
        return None
    except Exception as e:
        print(f"  [{email}] исключение браузера: {e}")
        import traceback; traceback.print_exc()
        return None
    finally:
        if page:
            try: page.quit()
            except: pass


def save_auth(email, cpa_data):
    """保存为代理可用的 xai-*.json 格式"""
    safe_email = email.replace("@", "_").replace(".", "_")
    path = os.path.join(AUTH_DIR, f"xai-{safe_email}.json")
    now = time.time()
    expires_in = cpa_data.get("expires_in", 21600)
    record = {
        "type": "xai", "auth_kind": "oauth",
        "access_token": cpa_data["access_token"],
        "refresh_token": cpa_data["refresh_token"],
        "token_type": cpa_data.get("token_type", "Bearer"),
        "expires_in": expires_in,
        "expired": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now + expires_in)),
        "last_refresh": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now)),
        "email": email,
        "base_url": "https://cli-chat-proxy.grok.com/v1",
        "token_endpoint": TOKEN_ENDPOINT,
        "redirect_uri": REDIRECT_URI,
        "client_id": CLIENT_ID,
        "disabled": False, "mint_method": "pkce", "protocol_flow": "pkce",
        "headers": {
            "X-XAI-Token-Auth": "xai-grok-cli",
            "x-grok-client-version": "0.2.93",
            "x-grok-client-identifier": "grok-shell",
        },
    }
    if cpa_data.get("id_token"):
        record["id_token"] = cpa_data["id_token"]
    os.makedirs(AUTH_DIR, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(record, f, ensure_ascii=False, indent=2)
    print(f"  [{email}] сохранено в {path}")
    return path


def convert_all(pending, threads=3):
    """Convert SSO accounts to CPA in parallel.

    Each account is re-checked right before conversion: if auths/xai-<email>.json
    already exists (from an earlier run or another script instance), it is skipped.
    Returns (ok, failed, skipped) counts.
    """
    _print_lock = threading.Lock()

    def _safe_name(email):
        return email.replace("@", "_").replace(".", "_")

    def _convert_one(a):
        email = a["email"]
        auth_path = os.path.join(AUTH_DIR, f"xai-{_safe_name(email)}.json")
        if os.path.exists(auth_path):
            with _print_lock:
                print(f"  {email} уже сконвертирован, пропускаю")
            return "skip"
        with _print_lock:
            print(f"\nконвертирую: {email}")
        result = sso_to_cpa(a["sso"], email)
        time.sleep(2)  # лёгкий pacing против rate-limit на auth.x.ai
        if result:
            save_auth(email, result)
            return "ok"
        with _print_lock:
            print(f"  {email} конвертация не удалась, пропускаю")
        return "fail"

    with ThreadPoolExecutor(max_workers=threads) as executor:
        results = list(executor.map(_convert_one, pending))
    ok = results.count("ok")
    failed = results.count("fail")
    skipped = results.count("skip")
    print(f"\nготово: {ok}/{len(pending)} сконвертировано ({failed} ошибок, {skipped} уже готовы)")
    return ok, failed, skipped


def main():
    parser = argparse.ArgumentParser(description="SSO -> CPA PKCE")
    parser.add_argument("--sso", help="单个 SSO token")
    parser.add_argument("--email", help="email for the SSO token")
    parser.add_argument("--all", action="store_true", help="convert all unconverted SSO")
    parser.add_argument("--dry-run", action="store_true", help="check only")
    parser.add_argument("--threads", type=int, default=3,
                        help="parallel workers for --all (default 3)")
    args = parser.parse_args()
    if args.sso:
        email = args.email or "unknown"
        if args.dry_run:
            print(f"[DRY RUN] конвертировал бы SSO: {args.sso[:30]}... (email={email})")
            return
        result = sso_to_cpa(args.sso, email)
        if result: save_auth(email, result)
        else: print("конвертация не удалась"); sys.exit(1)
        return
    if args.all:
        accounts = load_accounts()
        if not accounts: print("нет аккаунтов в accounts.txt"); return
        existing = set()
        for fn in os.listdir(AUTH_DIR):
            if fn.startswith("xai-") and fn.endswith(".json"):
                try:
                    with open(os.path.join(AUTH_DIR, fn), encoding="utf-8") as f:
                        d = json.load(f)
                    if d.get("email"): existing.add(d["email"])
                except: pass
        pending = [a for a in accounts if a["email"] not in existing]
        print(f"всего: {len(accounts)}  сконвертировано: {len(existing)}  ожидают: {len(pending)}")
        if args.dry_run:
            for a in pending: print(f"  [DRY RUN] {a['email']}")
            return
        success, failed, skipped = convert_all(pending, args.threads)
        return
    parser.print_help()


if __name__ == "__main__":
    main()