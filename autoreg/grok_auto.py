import os, json, random, string, time, re, struct, argparse
import threading
import concurrent.futures
import sys
from urllib.parse import urljoin, urlparse
from curl_cffi import requests
from bs4 import BeautifulSoup
from dotenv import load_dotenv

if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

load_dotenv()

from email_service import EmailService
from grok_free import browser_init, solve_turnstile

# 基础配置
site_url = "https://accounts.x.ai"
user_agent = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/144.0.0.0 Safari/537.36"
_proxy_url = os.getenv("GROK_PROXY") or ""
PROXIES = {
    "http": _proxy_url,
    "https": _proxy_url
} if _proxy_url else None

# 动态获取的全局变量
config = {
    "site_key": "0x4AAAAAAAhr9JGVDZbrZOo0",
    "action_id": None,
    "state_tree": "",  # извлекается из живой страницы при init; пустой = не слать заголовок
}

post_lock = threading.Lock()
file_lock = threading.Lock()
count_lock = threading.Lock()
stop_event = threading.Event()
# Free Turnstile: одна общая DrissionPage-страница на весь запуск (паттерн из grok_free.py)
ts_lock = threading.Lock()
ts_page = None
success_count = 0
completed_count = 0
target_count = 0  # 0 = 无限
start_time = time.time()
EMAIL_PROVIDER = str(os.getenv("EMAIL_PROVIDER") or "luckmail").strip().lower()

def generate_random_name() -> str:
    length = random.randint(4, 6)
    return random.choice(string.ascii_uppercase) + ''.join(random.choice(string.ascii_lowercase) for _ in range(length - 1))

def generate_random_string(length: int = 15) -> str:
    return "".join(random.choice(string.ascii_lowercase + string.digits) for _ in range(length))

def encode_grpc_message(field_id, string_value):
    key = (field_id << 3) | 2
    value_bytes = string_value.encode('utf-8')
    length = len(value_bytes)
    payload = struct.pack('B', key) + struct.pack('B', length) + value_bytes
    return b'\x00' + struct.pack('>I', len(payload)) + payload

def encode_grpc_message_verify(email, code):
    p1 = struct.pack('B', (1 << 3) | 2) + struct.pack('B', len(email)) + email.encode('utf-8')
    p2 = struct.pack('B', (2 << 3) | 2) + struct.pack('B', len(code)) + code.encode('utf-8')
    payload = p1 + p2
    return b'\x00' + struct.pack('>I', len(payload)) + payload

def send_email_code_grpc(session, email):
    url = f"{site_url}/auth_mgmt.AuthManagement/CreateEmailValidationCode"
    data = encode_grpc_message(1, email)
    headers = {"content-type": "application/grpc-web+proto", "x-grpc-web": "1", "x-user-agent": "connect-es/2.1.1", "origin": site_url, "referer": f"{site_url}/sign-up?redirect=grok-com"}
    try:
        # print(f"[debug] {email} 正在发送验证码请求...")
        res = session.post(url, data=data, headers=headers, timeout=15)
        # print(f"[debug] {email} 请求结束，状态码: {res.status_code}")
        return res.status_code == 200
    except Exception as e:
        print(f"[-] {email} ошибка отправки кода: {e}")
        return False

def verify_email_code_grpc(session, email, code):
    url = f"{site_url}/auth_mgmt.AuthManagement/VerifyEmailValidationCode"
    data = encode_grpc_message_verify(email, code)
    headers = {"content-type": "application/grpc-web+proto", "x-grpc-web": "1", "x-user-agent": "connect-es/2.1.1", "origin": site_url, "referer": f"{site_url}/sign-up?redirect=grok-com"}
    try:
        print(f"[debug] {email} код: {code}, проверяю статус...")
        res = session.post(url, data=data, headers=headers, timeout=15)
        # print(f"[debug] {email} 验证响应状态: {res.status_code}, 内容长度: {len(res.content)}")
        return res.status_code == 200
    except Exception as e:
        print(f"[-] {email} ошибка проверки кода: {e}")
        return False

def register_single_thread(email_provider: str = "gptmail"):
    # 错峰启动，防止瞬时并发过高
    time.sleep(random.uniform(0, 5))

    try:
        email_service = EmailService(proxies=PROXIES, provider=email_provider)
    except Exception as e:
        print(f"[-] инициализация сервиса не удалась: {e}")
        return

    # 从 config 获取 action_id，缺少则直接退出
    final_action_id = config.get("action_id")
    if not final_action_id:
        print("[-] поток завершён: не найден Action ID")
        return

    while not stop_event.is_set():
        jwt = None
        try:
            with requests.Session(impersonate="chrome120", proxies=PROXIES) as session:
                # 预热连接
                try: session.get(site_url, timeout=10)
                except: pass

                password = generate_random_string()
                
                # print(f"[debug] 线程-{threading.get_ident()} 正在请求创建邮箱...")
                try:
                    jwt, email = email_service.create_email()
                except Exception as e:
                    print(f"[-] ошибка email-сервиса: {e}")
                    jwt, email = None, None

                if not email:
                    print(f"[-] поток-{threading.get_ident()} создание email вернуло пусто (API недоступен или таймаут), жду 5 с...")
                    time.sleep(5); continue
                
                print(f"[*] регистрация: {email}")

                # Step 1: 发送验证码
                if not send_email_code_grpc(session, email):
                    print(f"[-] {email} не удалось отправить код подтверждения")
                    time.sleep(5); continue
                
                # Step 2: 获取验证码
                verify_code = None
                for _ in range(12):
                    time.sleep(5)
                    content = email_service.fetch_first_email(jwt)
                    if content:
                        # 兼容新格式："SZ0-0SW xAI confirmation code" 以及 HTML 中的 "SZ0-0SW"
                        match = re.search(r"([A-Z0-9]{3}-[A-Z0-9]{3})", content)
                        if match:
                            verify_code = match.group(1)  # x.ai требует код С дефисом (ABC-DEF), не stripped
                            break
                if not verify_code:
                    print(f"[-] {email} код подтверждения не получен")
                    continue

                # Step 3: 先解 Turnstile（最耗时），避免验证码过期
                # Free bypass: DrissionPage manual render (тот же путь, что в grok_free.py)
                global ts_page
                ts_token = None
                with ts_lock:
                    for ts_attempt in range(3):
                        try:
                            if ts_page is None:
                                print(f"[*] {email} запускаю браузер Turnstile...")
                                _bi = browser_init()
                                ts_page = _bi["page"]
                                if _bi.get("site_key"):
                                    config["site_key"] = _bi["site_key"]
                                if _bi.get("action_id"):
                                    config["action_id"] = _bi["action_id"]
                                    final_action_id = config["action_id"]
                                if _bi.get("state_tree"):
                                    config["state_tree"] = _bi["state_tree"]
                            ts_token = solve_turnstile(ts_page, config["site_key"])
                        except Exception as e:
                            ts_token = None
                            print(f"[-] {email} ошибка браузера Turnstile: {e}")
                            try: ts_page.quit()
                            except Exception: pass
                            ts_page = None
                        if ts_token:
                            break
                        print(f"[-] {email} капча не решена, повторяю...")
                        time.sleep(2)
                if not ts_token:
                    print(f"[-] {email} капча не решена за все попытки")
                    continue

                # Step 4: 直接提交注册（跳过预验证，避免消耗验证码）
                for attempt in range(1):  # 只试一次，失败换号重来
                    headers = {
                        "user-agent": user_agent, "accept": "text/x-component", "content-type": "text/plain;charset=UTF-8",
                        "origin": site_url, "referer": f"{site_url}/sign-up", "cookie": f"__cf_bm={session.cookies.get('__cf_bm','')}",
                        "next-router-state-tree": config.get("state_tree") or "",  # заголовок ОБЯЗАТЕЛЕН (даже пустой): без него POST принимается за form-submit -> RSC 404
                    }
                    if final_action_id:
                        headers["next-action"] = final_action_id
                    payload = [{
                        "emailValidationCode": verify_code,
                        "createUserAndSessionRequest": {
                            "email": email, "givenName": generate_random_name(), "familyName": generate_random_name(),
                            "clearTextPassword": password, "tosAcceptedVersion": "$undefined"
                        },
                        "turnstileToken": ts_token, "promptOnDuplicateEmail": True
                    }]
                    
                    with post_lock:
                        res = session.post(f"{site_url}/sign-up", json=payload, headers=headers)
                    
                    if res.status_code == 200:
                        sso = None
                        _txt = res.text.replace("\\/", "/").replace("\\u0026", "&")

                        def _grab_sso(urls):
                            """GET set-cookie URL'ы; sso из curl-jar, иначе std-requests fallback (grokipedia TLS)."""
                            import requests as _std
                            for u in urls:
                                try:
                                    session.get(u, allow_redirects=True, timeout=15)
                                except Exception:
                                    pass
                                v = session.cookies.get("sso")
                                if v:
                                    return v
                                try:
                                    rs = _std.get(u, allow_redirects=True, timeout=15, headers={"user-agent": user_agent})
                                    for ck in list(rs.cookies) + [c for h in rs.history for c in h.cookies]:
                                        if ck.name == "sso" and ck.value:
                                            return ck.value
                                except Exception:
                                    pass
                            return None

                        # 方式0a (актуальный формат): auth.* set-cookie URL в RSC-потоке (после unescape)
                        sso = _grab_sso(re.findall(r'https://auth\.[^"\'\s\\]+?/set-cookie\?q=[A-Za-z0-9_.\-]+', _txt))
                        # 方式0b: cookie-chain JWT -> hops[].href
                        if not sso:
                            import base64 as _b64
                            chain_urls = []
                            for tok in re.findall(r'eyJ[A-Za-z0-9_\-]{80,}', _txt):
                                try:
                                    j = json.loads(_b64.urlsafe_b64decode(tok + "=" * (-len(tok) % 4)))
                                    if isinstance(j, dict) and j.get("kind") == "cookie-chain":
                                        chain_urls += [h["href"] for h in j.get("hops", []) if h.get("href")]
                                except Exception:
                                    pass
                            if chain_urls:
                                sso = _grab_sso(chain_urls)
                        # 方式1: set-cookie?q= URL (老格式)
                        if not sso:
                            for pat in [
                                r'(https://[^"\s]+set-cookie\?q=[^:"\s]+)',
                                r'(https://[^"\s]+set-cookie[^"\s]+)',
                            ]:
                                m = re.search(pat, _txt)
                                if m:
                                    sso_url = m.group(0).rstrip("1:").rstrip("2:").rstrip("3:")
                                    sso = _grab_sso([sso_url])
                                    if sso:
                                        break
                        # 方式2: 直接从 response cookies 取
                        if not sso:
                            sso = session.cookies.get("sso")
                        # 方式3: 检查 Set-Cookie header
                        if not sso:
                            set_cookie = res.headers.get("set-cookie", "")
                            for c in set_cookie.split(","):
                                if "sso=" in c:
                                    sso_val = c.split("sso=")[1].split(";")[0]
                                    if sso_val:
                                        sso = sso_val
                                        break
                        # 判断：如果响应中包含明确的 invalid-code 错误才是真失败
                        if '"error"' in res.text and 'invalid' in res.text.lower():
                            if not sso:
                                _i = res.text.find('"error"')
                                _snip = res.text[max(0, _i - 80):_i + 400] if _i >= 0 else res.text[:400]
                                try:
                                    open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "debug_last_response.txt"), "w", encoding="utf-8").write(res.text)
                                except Exception:
                                    pass
                                print(f"[-] {email} ошибка регистрации: {_snip}")
                            # 如果有 sso 还是算成功（响应格式混乱时）

                        if sso:
                            with file_lock:
                                os.makedirs("keys", exist_ok=True)
                                with open("keys/grok.txt", "a") as f: f.write(sso + "\n")
                                with open("keys/accounts.txt", "a") as f: f.write(f"{email}:{password}:{sso}\n")
                                global success_count, completed_count
                                success_count += 1
                                completed_count += 1
                                avg = (time.time() - start_time) / success_count

                            if target_count > 0 and completed_count >= target_count:
                                stop_event.set()

                            print(f"[OK] зарегистрирован: {email} | SSO: {sso[:15]}... | среднее: {avg:.1f}с | прогресс: {completed_count}/{target_count if target_count else 'безлимит'}")

                            # → сразу конвертируем SSO в CPA (auths/xai-*.json), без отдельного прогона sso_to_cpa
                            try:
                                from sso_to_cpa import sso_to_cpa as _sso2cpa, save_auth as _save_cpa
                                print(f"[*] {email} конвертирую SSO -> CPA...")
                                _cpa = _sso2cpa(sso, email)
                                if _cpa:
                                    _save_cpa(email, _cpa)
                                    print(f"[OK] {email} CPA-токен сохранён в auths/")
                                else:
                                    print(f"[-] {email} конвертация CPA не удалась (SSO сохранён, можно повторить через sso_to_cpa --all)")
                            except Exception as _cpa_e:
                                print(f"[-] {email} исключение при конвертации CPA: {_cpa_e}")
                            break
                        elif '"error"' not in res.text or 'invalid' not in res.text.lower():
                            # 无明显错误但也没 SSO，打印更多信息调试
                            print(f"[-] {email} нет SSO (200 OK, len={len(res.text)}): {res.text[:150]}")
                        # else: 有 invalid 错误且无 SSO，已在上面的 if 打印
                    else:
                        print(f"[-] {email} отправка не удалась ({res.status_code}): {res.text[:200]}")
                    time.sleep(2)
                else:
                    print(f"[-] {email} сдаюсь, переключаю аккаунт")
                    time.sleep(5)

        except Exception as e:
            # 捕获所有异常防止线程退出
            print(f"[-] исключение: {str(e)[:50]}")
            time.sleep(5)
        finally:
            # close the provider's browser/client for this attempt
            # (Tmail/GPTMail keep a headed Chrome open; Gmail keeps an IMAP session)
            try:
                if jwt and isinstance(jwt, dict):
                    _client = jwt.get("client")
                    if _client is not None and hasattr(_client, "close"):
                        _client.close()
            except Exception:
                pass

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--email-provider", choices=["gptmail", "luckmail", "mailtm", "gmail", "tmail", "fce", "outlook", "imap", "1secmail", "tempmail-lol"], default=os.getenv("EMAIL_PROVIDER", "luckmail"), help="email-провайдер: gptmail/luckmail/mailtm/gmail/tmail/fce/outlook")
    parser.add_argument("--threads", type=int, default=None, help="количество параллельных потоков")
    parser.add_argument("--count", type=int, default=0, help="количество регистраций (0 = безлимит)")
    args = parser.parse_args()

    global target_count
    target_count = args.count

    print("=" * 60 + "\nРегистратор аккаунтов Grok (xAI)\n" + "=" * 60)
    print(f"[*] email-провайдер: {args.email_provider}")
    print(f"[*] целевое количество: {args.count if args.count else 'безлимит'}")

    # 1. 扫描参数
    print("[*] инициализация...")
    start_url = f"{site_url}/sign-up"
    with requests.Session(impersonate="chrome120", proxies=PROXIES) as s:
        try:
            html = s.get(start_url).text
            # Key
            key_match = re.search(r'sitekey":"(0x4[a-zA-Z0-9_-]+)"', html)
            if key_match: config["site_key"] = key_match.group(1)
            # Tree
            tree_match = re.search(r'next-router-state-tree":"([^"]+)"', html)
            if tree_match: config["state_tree"] = tree_match.group(1)
            # Action ID — 并发抓取所有 JS 文件（用标准 requests，线程安全+快速）
            js_urls = list(set(urljoin(start_url, m.group(0)) for m in re.finditer(r"/_next/static/chunks/[^\"'\s>]+\.js", html)))
            if not js_urls:
                preview = html[:500].replace("\n", " ")
                print(f"[Warn] длина HTML {len(html)}, JS не найден, первые 500 символов: {preview}")
            action_found = None
            print(f"[*] ищу Action ID в {len(js_urls)} JS-файлах...")

            def _fetch_and_search(url):
                """用标准 requests（线程安全），快速扫描 JS 文件找 Action ID"""
                import requests as _req
                try:
                    js = _req.get(url, proxies=PROXIES, timeout=10).text
                    m = re.search(r'7f[a-fA-F0-9]{40}', js)
                    if m:
                        return m.group(0)
                except Exception:
                    pass
                return None

            with concurrent.futures.ThreadPoolExecutor(max_workers=10) as pool:
                for result in pool.map(_fetch_and_search, js_urls):
                    if result:
                        action_found = result
                        pool.shutdown(wait=False, cancel_futures=True)
                        break

            if action_found:
                config["action_id"] = action_found
                print(f"[+] Action ID: {action_found}")
                try:
                    open(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".action_id.cache"), "w").write(action_found)
                except Exception:
                    pass
            else:
                # 回退缓存 (2026-08-06: 扫描间歇失败时用上次成功的 ID)
                try:
                    cached = open(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".action_id.cache")).read().strip()
                    if re.match(r"^7f[a-fA-F0-9]{40}$", cached):
                        config["action_id"] = cached
                        print(f"[+] использую кэшированный Action ID: {cached}")
                except Exception:
                    pass
        except Exception as e:
            print(f"[-] сбой инициализации: {e}")
            return

    if not config["action_id"]:
        print("[-] ошибка: Action ID не найден")
        return

    # 2. 启动
    if args.threads is not None:
        t = args.threads
    else:
        try:
            t = int(input("\nКоличество потоков (по умолчанию 1): ").strip() or 1)
        except:
            t = 1
    
    print(f"[*] запускаю {t} потоков...")
    with concurrent.futures.ThreadPoolExecutor(max_workers=t) as executor:
        # 只提交与线程数相等的任务，让它们在内部无限循环
        futures = [executor.submit(register_single_thread, args.email_provider) for _ in range(t)]
        try:
            concurrent.futures.wait(futures)
        except KeyboardInterrupt:
            print("\n[!] получено прерывание, выходим...")

    # закрываем общий браузер Turnstile, если он был открыт
    global ts_page
    if ts_page is not None:
        try:
            ts_page.quit()
            print("[*] браузер Turnstile закрыт")
        except Exception:
            pass
        ts_page = None

if __name__ == "__main__":
    main()
