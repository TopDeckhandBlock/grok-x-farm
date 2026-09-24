"""
Grok 全自动注册 — 免费版 v4
─────────────────────────────
- 邮箱：GPTMail (mail.chatgpt.org.uk) 免费 API — 公共 Key gpt-test，日 20 万次
- Turnstile：DrissionPage 浏览器手动 turnstile.render()
- 验证码：gRPC-web 协议发送 + 验证（对齐原始 grok.py）
- 注册：Next.js Server Action POST（curl_cffi）
- Clash IP 轮换：每次注册前切换代理节点降低风控
- 输出：SSO + email:password:sso → keys/

无需 YesCaptcha / LuckMail / MailTM
"""
import os, re, sys, json, time, random, string, struct, urllib.parse, argparse, glob
sys.stdout.reconfigure(encoding='utf-8', errors='replace')

from dotenv import load_dotenv
load_dotenv()

import ca_fix  # noqa: F401 — ASCII CA-бандл для кириллических путей (curl error 77)
from curl_cffi import requests as cf_req
from DrissionPage import ChromiumPage, ChromiumOptions
import requests  # 标准 requests 用于 SSO 跳转，兼容 auth.grokipedia.com 等域名

# ── 配置 ──
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
SITE_URL = "https://accounts.x.ai"
FALLBACK_SITE_KEY = "0x4AAAAAAAhr9JGVDZbrZOo0"
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/144.0.0.0 Safari/537.36"
PROXY = os.getenv("GROK_PROXY") or ""
OUTPUT_DIR = os.path.join(SCRIPT_DIR, "keys")
os.makedirs(OUTPUT_DIR, exist_ok=True)

# Clash 轮换器（可选，导入失败则跳过 IP 轮换）
try:
    from clash_rotator import (random_switch, switch_region, get_current_ip,
                                health as clash_health, snapshot, restore,
                                list_fast_nodes)
    HAS_CLASH = True
except ImportError:
    HAS_CLASH = False
    print("[!] clash_rotator не найден, ротация IP отключена")

# ═══════════════════════ 工具函数 ═══════════════════════

def rand_str(length=15):
    return "".join(random.choice(string.ascii_lowercase + string.digits) for _ in range(length))

def rand_name():
    n = random.randint(4, 6)
    return random.choice(string.ascii_uppercase) + ''.join(random.choice(string.ascii_lowercase) for _ in range(n - 1))

def _proxy_dict():
    if not PROXY:
        return None
    return {"http": PROXY, "https": PROXY}

# ── gRPC 编码（对齐原始 grok.py） ──

def encode_grpc_msg(field_id, val):
    key = (field_id << 3) | 2
    vb = val.encode("utf-8")
    payload = struct.pack("B", key) + struct.pack("B", len(vb)) + vb
    return b"\x00" + struct.pack(">I", len(payload)) + payload

def encode_grpc_verify(email, code):
    p1 = struct.pack("B", (1 << 3) | 2) + struct.pack("B", len(email)) + email.encode()
    p2 = struct.pack("B", (2 << 3) | 2) + struct.pack("B", len(code)) + code.encode()
    payload = p1 + p2
    return b"\x00" + struct.pack(">I", len(payload)) + payload

GRPC_HEADERS = {
    "content-type": "application/grpc-web+proto",
    "x-grpc-web": "1",
    "x-user-agent": "connect-es/2.1.1",
    "origin": SITE_URL,
    "referer": f"{SITE_URL}/sign-up?redirect=grok-com",
}

def send_email_code_grpc(session, email):
    """gRPC: 发送邮箱验证码"""
    url = f"{SITE_URL}/auth_mgmt.AuthManagement/CreateEmailValidationCode"
    data = encode_grpc_msg(1, email)
    try:
        res = session.post(url, data=data, headers=GRPC_HEADERS, timeout=15)
        return res.status_code == 200
    except Exception as e:
        print(f"  [!] ошибка отправки кода: {e}")
        return False

def verify_email_code_grpc(session, email, code):
    """gRPC: 验证邮箱验证码"""
    url = f"{SITE_URL}/auth_mgmt.AuthManagement/VerifyEmailValidationCode"
    data = encode_grpc_verify(email, code)
    try:
        res = session.post(url, data=data, headers=GRPC_HEADERS, timeout=15)
        return res.status_code == 200
    except Exception as e:
        print(f"  [!] ошибка проверки кода: {e}")
        return False


# ═══════════════════════ GPTMail 邮箱（新版 API） ═══════════════════════

class GPTMailInbox:
    """GPTMail 免费临时邮箱 —— 客户端生成邮箱 + inbox-token 注册

    API 流程（2026-07 新版）:
      1. GET  /api/domains/public  → 获取域名列表
      2. 客户端拼邮箱: prefix@random_domain
      3. POST /api/inbox-token     → 注册邮箱，获取 JWT token
      4. GET  /api/emails?email=.. → 轮询邮件
      5. GET  /api/email/{id}      → 获取邮件正文
    """

    def __init__(self):
        proxy_dict = _proxy_dict() if PROXY else None
        self.sess = cf_req.Session(impersonate="chrome120")
        if proxy_dict:
            self.sess.proxies = proxy_dict
        self.email = ""
        self.token = ""
        self._domains = []

    def _get_domains(self):
        """获取活跃域名列表"""
        if self._domains:
            return self._domains
        try:
            # 预热
            self.sess.get("https://mail.chatgpt.org.uk/", timeout=15)
        except Exception:
            pass
        r = self.sess.get("https://mail.chatgpt.org.uk/api/domains/public", timeout=15)
        if r.status_code != 200:
            raise RuntimeError(f"не удалось получить домены: {r.status_code}")
        data = r.json()
        domains_list = (data.get("data") or {}).get("domains") or []
        self._domains = [d["domain_name"] for d in domains_list if d.get("is_active")]
        if not self._domains:
            raise RuntimeError("нет активных доменов")
        return self._domains

    def create(self):
        """生成邮箱并注册"""
        domains = self._get_domains()
        prefix = rand_str(10)
        domain = random.choice(domains)
        self.email = f"{prefix}@{domain}"

        # 注册邮箱到 inbox token
        r = self.sess.post(
            "https://mail.chatgpt.org.uk/api/inbox-token",
            headers={"Content-Type": "application/json"},
            json={"email": self.email},
            timeout=15,
        )
        if r.status_code != 200:
            raise RuntimeError(f"ошибка inbox-token: {r.status_code}")
        data = r.json()
        if not data.get("success"):
            raise RuntimeError(f"inbox-token вернул ошибку: {data}")
        self.token = (data.get("auth") or {}).get("token") or ""
        if not self.token:
            raise RuntimeError("не получен inbox token")
        return self.email

    def wait_code(self, timeout=60, interval=5):
        """轮询 GPTMail 获取验证码"""
        deadline = time.time() + timeout
        while time.time() < deadline:
            time.sleep(interval)
            try:
                r = self.sess.get(
                    f"https://mail.chatgpt.org.uk/api/emails?email={urllib.parse.quote(self.email)}",
                    headers={"x-inbox-token": self.token},
                    timeout=15,
                )
                if r.status_code != 200:
                    continue
                data = r.json()
                emails = (data.get("data") or {}).get("emails") or data.get("data") or []
                if isinstance(emails, dict):
                    emails = [emails]
                for msg in (emails if isinstance(emails, list) else []):
                    subject = str(msg.get("subject") or "")
                    body = str(msg.get("text") or msg.get("html") or msg.get("body") or "")
                    text = subject + " " + body
                    m = re.search(r"([A-Z0-9]{3})-?([A-Z0-9]{3})", text)
                    if m:
                        return m.group(1) + m.group(2)

                # 如果邮件列表有 ID，获取详情
                for msg in (emails if isinstance(emails, list) else []):
                    msg_id = msg.get("id") or msg.get("message_id")
                    if msg_id:
                        r2 = self.sess.get(
                            f"https://mail.chatgpt.org.uk/api/email/{urllib.parse.quote(str(msg_id))}",
                            headers={"x-inbox-token": self.token},
                            timeout=15,
                        )
                        if r2.status_code == 200:
                            detail = r2.json()
                            d = (detail.get("data") or detail)
                            text2 = str(d.get("subject") or "") + " " + str(d.get("text") or d.get("html") or d.get("body") or "")
                            m = re.search(r"([A-Z0-9]{3})-?([A-Z0-9]{3})", text2)
                            if m:
                                return m.group(1) + m.group(2)
            except Exception:
                continue
        return None


# ═══════════════════════ 浏览器初始化 ═══════════════════════

def _free_port() -> int:
    """Свободный TCP-порт для браузера DP: auto_port() DP перезаписывает
    user_data_path временным — для персистентного профиля порт берём сами."""
    import socket
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def browser_init(solve: bool = True, profile: bool = False):
    """打开注册页 → 获取 Action ID + Site Key + State Tree.
    solve=True: дополнительно дождаться первого токена (старое поведение grok_free).
    solve=False: вернуть страницу сразу — токены решает TurnstileFarm
    (пачками и с клик-фолбэком по чекбоксу).
    profile=True: персистентный профиль .chrome-profile + авто-порт (способ v3) —
    cf_clearance переживает перезапуски; НЕ сочетать с --incognito."""

    co = ChromiumOptions()
    _browser_path = os.getenv("GROK_BROWSER_PATH") or ""
    if not _browser_path:
        # fallback: patchright's patched Chromium when no system Chrome/Edge
        _cands = sorted(
            glob.glob(os.path.expanduser(
                "~/Library/Caches/ms-playwright/chromium-*/chrome-mac*/"
                "Google Chrome for Testing.app/Contents/MacOS/Google Chrome for Testing"))
            + glob.glob(os.path.expanduser(
                "~/Library/Caches/ms-playwright/chromium-*/chrome-mac*/"
                "Chromium.app/Contents/MacOS/Chromium")))
        if _cands:
            _browser_path = _cands[-1]
    if _browser_path:
        co.set_browser_path(_browser_path)
    co.set_argument("--disable-blink-features=AutomationControlled")
    if profile:
        # персистентный профиль: cf_clearance живёт между запусками (меньше
        # челленджей). ВНИМАНИЕ: DP auto_port() ПЕРЕЗАПИСЫВАЕТ user_data_path
        # временным каталогом и удаляет его на disconnect (_base/chromium.py
        # handle_options/_on_disconnect) — поэтому свободный порт берём сами,
        # а профиль фиксируем (set_user_data_path сбрасывает _auto_port).
        co.set_user_data_path(os.path.join(
            os.path.dirname(os.path.abspath(__file__)), ".chrome-profile"))
        co.set_local_port(_free_port())
    else:
        co.set_argument("--incognito")
    # ресурсосбережение: только безопасные флаги. --disable-gpu НЕ ставим:
    # софтверный WebGL-отпечаток в headed-браузере может наталкивать Turnstile
    # на интерактивный челлендж (виджет молча висит до timeout)
    for _arg in ("--disable-extensions", "--no-first-run",
                 "--disable-background-networking", "--disable-component-update",
                 "--mute-audio", "--disable-sync", "--disable-default-apps"):
        co.set_argument(_arg)
    w = random.randint(1200, 1400)
    h = random.randint(800, 1000)
    co.set_argument(f"--window-size={w},{h}")
    if PROXY:
        proxy_addr = PROXY.replace("http://", "").replace("https://", "")
        co.set_argument(f"--proxy-server={proxy_addr}")
    page = ChromiumPage(co)

    print("[Browser] открываю страницу регистрации...")
    page.get(f"{SITE_URL}/sign-up?redirect=grok-com")
    time.sleep(4)

    html = page.html
    if not html:
        raise RuntimeError("не удалось загрузить страницу")

    # --- Site Key ---
    site_key = FALLBACK_SITE_KEY
    m = re.search(r'sitekey":"(0x4[a-zA-Z0-9_-]+)"', html)
    if m:
        site_key = m.group(1)
    print(f"[Browser] SiteKey: {site_key}")

    # --- Action ID ---
    js_urls = re.findall(r"/_next/static/chunks/[^\"'\s>]+\.js", html)
    action_id = None
    js_sess = cf_req.Session(impersonate="chrome120")
    if PROXY:
        js_sess.proxies = {"http": PROXY, "https": PROXY}
    for js_path in js_urls:
        url = js_path if js_path.startswith("http") else f"{SITE_URL}{js_path}"
        try:
            js = js_sess.get(url, timeout=15).text
            # 支持新旧两种格式: release:"hex40" 或旧版 7f+hex40
            m = re.search(r'release[:\s]*["\']([a-fA-F0-9]{40})["\']', js)
            if not m:
                m = re.search(r'7f[a-fA-F0-9]{40}', js)
            if m:
                action_id = m.group(1) if m.lastindex else m.group(0)
                print(f"[Browser] ActionID: {action_id}")
                break
        except Exception:
            continue
    if not action_id:
        raise RuntimeError("не найден Action ID, регистрация невозможна")

    # --- State Tree ---
    state_tree = ""
    m = re.search(r'next-router-state-tree":"([^"]+)"', html)
    if m:
        state_tree = m.group(1)

    # --- 点击邮箱注册选项，触发自然 Turnstile ---
    print("[Browser] выбираю вариант регистрации по email...")
    page.run_js('''
    var all=document.querySelectorAll('button,[role=button]');
    for(var i=0;i<all.length;i++){
        if(!all[i].offsetParent) continue;
        var t=(all[i].innerText||'').trim();
        if(t.indexOf('邮箱')>=0||t.indexOf('email')>=0||t.indexOf('Email')>=0){
            all[i].click(); break;
        }
    }
    ''')
    time.sleep(3)

    if not solve:
        # страница готова; токены добудет TurnstileFarm (пачка + клик-фолбэк)
        print("[Browser] страница готова (без решения капчи — ферма решит)")
        return {
            "site_key": site_key,
            "action_id": action_id,
            "state_tree": state_tree,
            "ts_token": "",
            "page": page,
        }

    # --- Turnstile: 优先等待页面自带的 cf-turnstile-response 填充 ---
    ts_token = ""
    for attempt in range(45):
        time.sleep(2)
        ts_token = page.run_js(
            'return document.querySelector("[name=cf-turnstile-response]")?.value||""')
        if len(ts_token) > 50:
            print(f"[Browser] Turnstile решён естественно (попытка {attempt+1})")
            break
        # 检查是否有 iframe/challenge
        has_challenge = page.run_js(
            'return document.querySelector("iframe[src*=turnstile],iframe[src*=challenges]")!==null')
        if has_challenge and attempt == 0:
            print("[Browser] появился Turnstile-челлендж, жду решения...")

    if len(ts_token) < 50:
        # 手动渲染 fallback（70s 超时，需大于 Turnstile 的 60s）
        print("[Browser] естественное ожидание истекло, пробую ручной рендер (таймаут 70с)...")
        ts_token = page.run_js('''
        var _sitekey = arguments[0];
        return new Promise(function(resolve, _reject) {
            var sitekey = _sitekey;
            var tsDiv = document.createElement('div');
            tsDiv.id = '_grok_free_ts';
            tsDiv.style.cssText = 'position:fixed;top:10px;right:10px;z-index:99999';
            document.body.appendChild(tsDiv);

            var timeout = setTimeout(function() { resolve('timeout'); }, 60000);

            try {
                turnstile.render('#'+tsDiv.id, {
                    sitekey: sitekey, theme: 'light',
                    callback: function(token) {
                        clearTimeout(timeout);
                        var hidden = document.querySelector('[name="cf-turnstile-response"]');
                        if (hidden) hidden.value = token;
                        resolve(token);
                    },
                    'error-callback': function(e) {
                        clearTimeout(timeout);
                        resolve('error:' + (e && e.message ? e.message : String(e)));
                    }
                });
            } catch(e) {
                clearTimeout(timeout);
                resolve('exception:' + (e && e.message ? e.message : String(e)));
            }
        });
        ''', site_key, timeout=75)
        print(f"[Browser] результат ручного рендера: {ts_token[:80] if ts_token else 'None'}...")

    if not ts_token or ts_token.startswith('timeout') or ts_token.startswith('error'):
        # 尝试直接从已有字段获取
        ts_token = page.run_js(
            'return document.querySelector("[name=cf-turnstile-response]")?.value||""')
        if len(ts_token) < 50:
            page.quit()
            raise RuntimeError(f"Turnstile не решён: {ts_token}")

    print(f"[Browser] Turnstile решён ({len(ts_token)} симв.)")

    return {
        "site_key": site_key,
        "action_id": action_id,
        "state_tree": state_tree,
        "ts_token": ts_token,
        "page": page,
    }


# ═══════════════════════ 注册流程 ═══════════════════════

def solve_turnstile(page, site_key):
    """在浏览器中解决 Turnstile，返回新 token。每次注册前调用"""
    # 刷新注册页
    page.get(f"{SITE_URL}/sign-up?redirect=grok-com")
    time.sleep(3)

    # 点击邮箱选项
    page.run_js('''
    var all=document.querySelectorAll('button,[role=button]');
    for(var i=0;i<all.length;i++){
        if(!all[i].offsetParent) continue;
        var t=(all[i].innerText||'').trim();
        if(t.indexOf('邮箱')>=0||t.indexOf('email')>=0||t.indexOf('Email')>=0){
            all[i].click(); break;
        }
    }
    ''')
    time.sleep(2)

    # 等待自然 Turnstile 解决
    ts_token = ""
    for attempt in range(35):
        time.sleep(1)  # фикса busy-wait: без sleep цикл молотил CDP вхолостую
        ts_token = page.run_js(
            'return document.querySelector("[name=cf-turnstile-response]")?.value||""')
        if len(ts_token) > 50:
            break

    # fallback: 手动渲染
    if len(ts_token) < 50:
        ts_token = page.run_js('''
        var _sitekey = arguments[0];
        return new Promise(function(resolve, _reject) {
            var sitekey = _sitekey;
            var tsDiv = document.createElement('div');
            tsDiv.id = '_grok_fresh_ts';
            tsDiv.style.cssText = 'position:fixed;top:10px;right:10px;z-index:99999';
            document.body.appendChild(tsDiv);
            var timeout = setTimeout(function() { resolve('timeout'); }, 60000);
            try {
                turnstile.render('#'+tsDiv.id, {
                    sitekey: sitekey, theme: 'light',
                    callback: function(token) {
                        clearTimeout(timeout);
                        var hidden = document.querySelector('[name="cf-turnstile-response"]');
                        if (hidden) hidden.value = token;
                        resolve(token);
                    },
                    'error-callback': function(e) {
                        clearTimeout(timeout);
                        resolve('error:' + (e && e.message ? e.message : String(e)));
                    }
                });
            } catch(e) {
                clearTimeout(timeout);
                resolve('exception:' + (e && e.message ? e.message : String(e)));
            }
        });
        ''', site_key, timeout=75)

    if not ts_token or ts_token.startswith('timeout') or ts_token.startswith('error'):
        return None
    return ts_token


def register_one(cfg):
    """Register a single account → returns (email, password, sso) or None.

    The provider email client (Tmail keeps a headed Chrome, Gmail an IMAP
    session) is closed in finally so nothing leaks between registrations.
    """
    holder = {}
    try:
        return _register_one_impl(cfg, holder)
    finally:
        client = holder.get("client")
        if client is not None and hasattr(client, "close"):
            try:
                client.close()
            except Exception:
                pass


def _register_one_impl(cfg, holder):
    # ── 创建邮箱 (provider 来自 .env: EMAIL_PROVIDER) ──
    print("[Mail] создаю email...")
    try:
        from email_service import EmailService
        mail = EmailService(proxies={"http": PROXY, "https": PROXY} if PROXY else None,
                            provider=os.getenv("EMAIL_PROVIDER", "gmail"))
        token_like, email = mail.create_email()
        holder["client"] = token_like.get("client") if isinstance(token_like, dict) else None
    except Exception as e:
        print(f"[Mail] не удалось создать email: {e}")
        return None
    print(f"[Mail] {email}")

    # ── curl_cffi session ──
    sess = cf_req.Session(impersonate="chrome120")
    if PROXY:
        sess.proxies = {"http": PROXY, "https": PROXY}
    try:
        sess.get(SITE_URL, timeout=10)
    except Exception:
        pass

    # ── Step 1: 发送验证码 (gRPC) ──
    print(f"[{email}] отправляю код подтверждения (gRPC)...")
    if not send_email_code_grpc(sess, email):
        print(f"[{email}] не удалось отправить код подтверждения")
        return None
    print(f"[{email}] код отправлен")

    # ── Step 2: 等待验证码 ──
    print(f"[{email}] жду код подтверждения...")
    code = None
    for _ in range(36):  # 36 x 5s = 180s
        time.sleep(5)
        content = mail.fetch_first_email(token_like)
        if content:
            m = re.search(r"([A-Z0-9]{3})-?([A-Z0-9]{3})", content)
            if m:
                code = m.group(1) + m.group(2)
                break
    if not code:
        print(f"[{email}] код не получен (таймаут)")
        return None
    print(f"[{email}] код: {code}")

    # ── Step 3: 验证验证码 (gRPC) ──
    print(f"[{email}] проверяю код (gRPC)...")
    if not verify_email_code_grpc(sess, email, code):
        print(f"[{email}] неверный код")
        return None
    print(f"[{email}] код подтверждён")

    # ── Step 4: 刷新 Turnstile token（每次注册用新 token） ──
    print(f"[{email}] обновляю Turnstile...")
    ts_token = solve_turnstile(cfg["page"], cfg["site_key"])
    if not ts_token:
        print(f"[{email}] не удалось обновить Turnstile")
        return None
    print(f"[{email}] Turnstile обновлён ({len(ts_token)} симв.)")

    # ── Step 5: 准备注册数据 ──
    password = rand_str(14) + "Aa1!"
    first = rand_name()
    last = rand_name()

    # ── Step 6: 注册 POST ──
    print(f"[{email}] отправляю регистрацию...")
    try:
        sess.get(SITE_URL, timeout=10)
    except Exception:
        pass

    cf_bm = sess.cookies.get("__cf_bm", "")
    headers = {
        "user-agent": UA,
        "accept": "text/x-component",
        "content-type": "text/plain;charset=UTF-8",
        "origin": SITE_URL,
        "referer": f"{SITE_URL}/sign-up",
        "cookie": f"__cf_bm={cf_bm}",
        "next-router-state-tree": cfg["state_tree"],
        "next-action": cfg["action_id"],
    }

    payload = [{
        "emailValidationCode": code,
        "createUserAndSessionRequest": {
            "email": email,
            "givenName": first,
            "familyName": last,
            "clearTextPassword": password,
            "tosAcceptedVersion": "$undefined",
        },
        "turnstileToken": ts_token,
        "promptOnDuplicateEmail": True,
    }]

    try:
        r = sess.post(f"{SITE_URL}/sign-up", json=payload, headers=headers, timeout=30)
        print(f"[{email}] статус POST: {r.status_code}")
    except Exception as e:
        print(f"[{email}] исключение POST: {e}")
        return None

    if r.status_code != 200:
        print(f"[{email}] регистрация не удалась: {r.text[:300]}")
        return None

    # ── Step 7: 提取 SSO ──
    resp_text = r.text
    # 多种 SSO URL 匹配尝试
    sso_url = None
    for pat in [
        r'(https://[^"\s]+set-cookie\?q=[^:"\s]+)1:',
        r'(https://[^"\s]+set-cookie\?q=[^"\s]+)',
        r'https://[^"\s]*set-cookie[^"\s]*',
    ]:
        m = re.search(pat, resp_text)
        if m:
            sso_url = m.group(0).rstrip("1:")
            break

    if sso_url:
        # 清理 URL 末尾残留
        sso_url = re.sub(r'[:\d]*$', '', sso_url) if sso_url.endswith(('1:', '2:', '3:')) else sso_url
        print(f"[{email}] SSO URL: {sso_url[:100]}...")

        # 用标准 requests 获取 SSO（curl_cffi 对 auth.grokipedia.com TLS 不兼容）
        sso = None
        rs = None
        try:
            rs = requests.Session()
            if PROXY:
                rs.proxies = {"http": PROXY, "https": PROXY}
            rs.get(sso_url, allow_redirects=True, timeout=15,
                   headers={"User-Agent": UA})
        except Exception as e:
            print(f"[{email}] исключение SSO std-запроса: {e}, переключаюсь на curl_cffi...")
            try:
                sess.get(sso_url, allow_redirects=True, timeout=15)
            except Exception as e2:
                print(f"[{email}] curl_cffi тоже не сработал: {e2}")

        # Извлекаем sso-куку вручную: редирект-цепочка кладёт несколько sso
        # для разных доменов, и jar.get("sso") бросает "multiple cookies".
        if sso is None and rs is not None:
            for c in rs.cookies:
                if c.name == "sso":
                    sso = c.value
                    break
        if sso is None:
            for c in sess.cookies:
                if c.name == "sso":
                    sso = c.value
                    break

        if sso:
            print(f"[{email}] ✅ SSO: {sso[:30]}...")
            with open(os.path.join(OUTPUT_DIR, "grok.txt"), "a", encoding="utf-8") as f:
                f.write(sso + "\n")
            with open(os.path.join(OUTPUT_DIR, "accounts.txt"), "a", encoding="utf-8") as f:
                f.write(f"{email}:{password}:{sso}\n")
            return (email, password, sso)
        else:
            print(f"[{email}] нет SSO cookie")
    else:
        print(f"[{email}] нет SSO URL в ответе, первые 300 символов:")
        print(f"  {resp_text[:300]}")

    return None


# ═══════════════════════ 主程序 ═══════════════════════

def main():
    parser = argparse.ArgumentParser(description="Grok 全自动注册")
    parser.add_argument("--count", type=int, default=0, help="registration count (0 = unlimited, default: unlimited)")
    parser.add_argument("--no-rotate", action="store_true", help="禁用 IP 轮换")
    parser.add_argument("--rotate-interval", type=int, default=1,
                        help="每注册 N 个后切换 IP（默认 1，即每次切换）")
    parser.add_argument("--min-delay", type=int, default=8, help="注册间隔最小值（秒，默认 8）")
    parser.add_argument("--max-delay", type=int, default=25, help="注册间隔最大值（秒，默认 25）")
    parser.add_argument("--rotate-region", action="store_true", help="切换不同区域节点（而非随机节点）")
    args = parser.parse_args()

    print("=" * 55)
    print(f"Grok Registrar · free v4 (GPTMail + gRPC + Clash)")
    print(f"количество: {args.count if args.count else 'безлимит'}  ротация IP: {'вкл' if not args.no_rotate and HAS_CLASH else 'выкл'}")
    print("=" * 55)

    # ── Clash: 快照当前节点（注册完恢复） ──
    original_node = None
    if not args.no_rotate and HAS_CLASH:
        try:
            original_node = snapshot()
            h = clash_health()
            # 显示低延迟节点数
            fast, slow = list_fast_nodes()
            print(f"[Clash] снимок: {h['current_node'][:40]}")
            print(f"[Clash] исходящий IP: {h['current_ip']}  ({h['region']})")
            print(f"[Clash] быстрые ноды: {len(fast)}  медленные/упавшие: {len(slow)}")
            if slow:
                for n, reason in slow[:3]:
                    print(f"[Clash]   ⚠ {n[:35]} — {reason}")
        except Exception as e:
            print(f"[Clash] проверка не удалась: {e}")

    # 1. 浏览器初始化（获取 Turnstile token + Action ID 等），失败重试
    cfg = None
    for retry in range(3):
        try:
            cfg = browser_init()
            break
        except Exception as e:
            print(f"\n[!] инициализация браузера не удалась (попытка {retry+1}/3): {e}")
            if retry < 2 and HAS_CLASH and not args.no_rotate:
                try:
                    # 换个区域重试 Turnstile
                    random_switch()
                    print(f"[Clash] повторяю после смены региона...")
                except Exception:
                    pass
            time.sleep(5)
    if not cfg:
        print("[!] инициализация браузера не удалась, прерываю")
        return

    # 2. 注册循环
    success = 0
    fail = 0
    t0 = time.time()

    # 使用过的区域追踪（避免重复用同一区域）
    used_regions = set()

    total = args.count if args.count else None  # None = unlimited
    i = 0
    try:
        while total is None or i < total:
            i += 1
            label = f"{i}/{total}" if total else f"{i}/∞"
            print(f"\n{'─'*40}")
            print(f"регистрация {label}")
            print(f"{'─'*40}")

            # ── 浏览器保活: 合盖/休眠后页面断开则重新初始化 ──
            if cfg and cfg.get("page"):
                try:
                    cfg["page"].run_js("1")
                except Exception:
                    print("[Browser] страница отключена, переинициализирую браузер...")
                    cfg = None
                    for retry in range(3):
                        try:
                            cfg = browser_init()
                            break
                        except Exception as e:
                            print(f"[Browser] повторная инициализация не удалась (попытка {retry+1}/3): {e}")
                            time.sleep(5)
                    if not cfg:
                        print("[Browser] повторная инициализация не удалась, прерываю")
                        break

            # ── IP 轮换 ──
            if not args.no_rotate and HAS_CLASH and i > 0 and i % args.rotate_interval == 0:
                try:
                    if args.rotate_region:
                        switch_region(exclude_regions=used_regions)
                    else:
                        random_switch()
                    new_ip = get_current_ip()
                    if new_ip:
                        print(f"  [IP] новый исходящий IP: {new_ip}")
                except Exception as e:
                    print(f"  [IP] переключение не удалось: {e}, продолжаю с текущим IP")

            try:
                result = register_one(cfg)
                if result:
                    success += 1
                    avg = (time.time() - t0) / success
                    print(f"  ok={success} fail={fail} среднее={avg:.0f}с/акк")
                else:
                    fail += 1
                    print(f"  FAIL ok={success} fail={fail}")
            except KeyboardInterrupt:
                break
            except Exception as e:
                fail += 1
                print(f"[!] исключение: {e}")
                import traceback
                traceback.print_exc()
                time.sleep(5)

            # ── 随机间隔（最后一个不需要等待；无限模式每次都要等） ──
            if total is None or i < total:
                delay = random.uniform(args.min_delay, args.max_delay)
                print(f"  жду {delay:.1f}с...")
                time.sleep(delay)
    finally:
        if cfg and cfg.get("page"):
            cfg["page"].quit()
        # ── 恢复原始节点 ──
        if original_node and HAS_CLASH:
            try:
                restore(original_node)
            except Exception as e:
                print(f"[Clash] не удалось восстановить ноду: {e}")

    elapsed = time.time() - t0
    print(f"\n{'='*55}")
    print(f"готово. ok={success} fail={fail} прошло={elapsed:.0f}с")
    if success > 0:
        print(f"SSO сохранены в: {OUTPUT_DIR}")
    print(f"{'='*55}")


if __name__ == "__main__":
    main()
