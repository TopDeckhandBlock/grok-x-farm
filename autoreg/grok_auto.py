import os, json, random, string, time, re, struct, argparse
import queue
import threading
import concurrent.futures
import sys
from urllib.parse import urljoin, urlparse

import ca_fix  # noqa: F401 — ASCII CA-бандл для кириллических путей (curl error 77)
from curl_cffi import requests
from bs4 import BeautifulSoup
from dotenv import load_dotenv

if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace", line_buffering=True)

load_dotenv()

from email_service import EmailService, mark_bad_email_domain, bad_email_domains
from turnstile_farm import TurnstileFarm

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
    "state_tree": "%5B%22%22%2C%7B%22children%22%3A%5B%22(app)%22%2C%7B%22children%22%3A%5B%22(auth)%22%2C%7B%22children%22%3A%5B%22sign-up%22%2C%7B%22children%22%3A%5B%22__PAGE__%22%2C%7B%7D%2C%22%2Fsign-up%22%2C%22refresh%22%5D%7D%5D%7D%2Cnull%2Cnull%5D%7D%2Cnull%2Cnull%5D%7D%2Cnull%2Cnull%2Ctrue%5D"
}

submit_limiter = None           # PaceLimiter: мин. интервал между сабмитами (инициализируется в main)
file_lock = threading.Lock()
count_lock = threading.Lock()
stop_event = threading.Event()
# Turnstile-ферма: ОДИН браузер на весь прогон, пачка виджетов за цикл,
# готовые токены в очереди — капча перестаёт быть серийным замком.
ts_farm = None  # инициализируется в main() после подготовки config
# CPA-конвертация уходит в фон: поток регистрации не ждёт 10–20 с на аккаунт.
cpa_queue = queue.Queue()
success_count = 0
completed_count = 0
target_count = 0  # 0 = 无限
start_time = time.time()
EMAIL_PROVIDER = str(os.getenv("EMAIL_PROVIDER") or "luckmail").strip().lower()

# Адаптивный опрос почты (из v2): первый заход через 2с — коды часто приходят
# быстро; нарастающие паузы; суммарное окно ~110с вместо 59с у v4.
MAIL_POLL_SCHEDULE = [2, 2, 3, 3, 4, 5, 6, 7, 8, 10, 12, 15, 15, 18]


class PaceLimiter:
    """Минимальный интервал между POST /sign-up глобально (анти-rate-limit).
    В отличие от post_lock v4 — не держит блокировку на время запроса."""

    def __init__(self, interval: float):
        self.interval = max(0.0, float(interval))
        self._lock = threading.Lock()
        self._next = 0.0

    def wait(self):
        if self.interval <= 0:
            return
        with self._lock:
            now = time.monotonic()
            delay = self._next - now
            self._next = max(self._next, now) + self.interval
        if delay > 0:
            time.sleep(delay)

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

def cpa_worker():
    """Фоновая конвертация SSO → CPA: не блокирует поток регистрации."""
    while True:
        item = cpa_queue.get()
        if item is None:
            cpa_queue.task_done()
            return
        sso, email = item
        try:
            from sso_to_cpa import sso_to_cpa as _sso2cpa, save_auth as _save_cpa
            _cpa = _sso2cpa(sso, email)
            if _cpa:
                _save_cpa(email, _cpa)
                print(f"[OK] {email} CPA-токен сохранён в auths/")
            else:
                print(f"[-] {email} конвертация CPA не удалась (SSO сохранён, догонка: sso_to_cpa.py --all)")
        except Exception as _cpa_e:
            print(f"[-] {email} исключение при конвертации CPA: {_cpa_e}")
        finally:
            cpa_queue.task_done()

def stats_worker():
    """Одна строка каждые 30с: успехи, темп акк/час, ферма, очереди."""
    while not stop_event.wait(30):
        with count_lock:
            ok, comp = success_count, completed_count
        el = max(1e-9, time.time() - start_time)
        rate = 3600 * ok / el if ok else 0.0
        tq = ts_farm.tokens.qsize() if ts_farm else 0
        ts_tot = ts_farm.solved_total if ts_farm else 0
        dead_mark = " (МЕРТВА!)" if (ts_farm and ts_farm.dead.is_set()) else ""
        print(f"[stats] ok={ok} прогресс={comp}/{target_count or 'безлимит'} "
              f"темп≈{rate:.0f} акк/ч | TS: очередь {tq}, всего {ts_tot}{dead_mark} | "
              f"CPA-очередь: {cpa_queue.qsize()} | blacklist доменов: {len(bad_email_domains())}")

def register_single_thread(email_provider: str = "gptmail"):
    global success_count, completed_count
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

    session = None  # сессия воркера живёт между аккаунтами (экономия TLS-хендшейков)
    while not stop_event.is_set():
        # ферма обновляет action_id из живой страницы — подхватываем свежий
        final_action_id = config.get("action_id") or final_action_id
        jwt = None
        try:
            if session is None:
                session = requests.Session(impersonate="chrome120", proxies=PROXIES)
            # чистые куки на каждый аккаунт + 预热: свежий __cf_bm
            session.cookies.clear()
            try: session.get(site_url, timeout=10)
            except: pass

            password = generate_random_string()

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

            # Step 2: адаптивный опрос ящика (первый заход 2с, окно ~110с)
            verify_code = None
            for _pause in MAIL_POLL_SCHEDULE:
                time.sleep(_pause)
                if stop_event.is_set():
                    break   # цель достигнута — не дожариваем окно опроса
                content = email_service.fetch_first_email(jwt)
                if content:
                    # 兼容新格式："SZ0-0SW xAI confirmation code" 以及 HTML 中的 "SZ0-0SW"
                    match = re.search(r"([A-Z0-9]{3}-[A-Z0-9]{3})", content)
                    if match:
                        verify_code = match.group(1).replace("-", "")
                        break
            if not verify_code:
                if stop_event.is_set():
                    continue    # не фейл домена: просто прогон остановлен
                print(f"[-] {email} код подтверждения не получен")
                mark_bad_email_domain(email)  # счётчик фейлов; blacklist после 2-го
                continue

            # Step 3: токен Turnstile из фермы; ферма мертва — сворачиваем прогон
            if ts_farm is not None and ts_farm.dead.is_set():
                print("[-] Turnstile-ферма мертва — прогон остановлен")
                stop_event.set()
                break
            ts_token = ts_farm.get_token(timeout=180) if ts_farm else None
            if not ts_token:
                print(f"[-] {email} капча не решена (ферма пуста/таймаут)")
                if ts_farm is not None and ts_farm.dead.is_set():
                    stop_event.set()
                    break
                continue

            # Step 4: 直接提交注册（跳过预验证，避免消耗验证码）
            headers = {
                "user-agent": user_agent, "accept": "text/x-component", "content-type": "text/plain;charset=UTF-8",
                "origin": site_url, "referer": f"{site_url}/sign-up", "cookie": f"__cf_bm={session.cookies.get('__cf_bm','')}",
                "next-router-state-tree": config["state_tree"],
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

            # pacing: минимальный интервал между сабмитами (без жёсткого замка на запрос)
            if submit_limiter is not None:
                submit_limiter.wait()
            res = session.post(f"{site_url}/sign-up", json=payload, headers=headers)

            if res.status_code == 200:
                # 尝试多种 SSO 提取方式
                sso = None
                # 方式1: set-cookie?q= URL (老格式)
                for pat in [
                    r'(https://[^"\s]+set-cookie\?q=[^:"\s]+)',
                    r'(https://[^"\s]+set-cookie[^"\s]+)',
                ]:
                    m = re.search(pat, res.text)
                    if m:
                        sso_url = m.group(0).rstrip("1:").rstrip("2:").rstrip("3:")
                        try:
                            session.get(sso_url, allow_redirects=True, timeout=15)
                        except:
                            pass
                        sso = session.cookies.get("sso")
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
                        print(f"[-] {email} неверный код подтверждения: {res.text[:150]}")
                    # 如果有 sso 还是算成功（响应格式混乱时）

                if sso:
                    with file_lock:
                        os.makedirs("keys", exist_ok=True)
                        with open("keys/grok.txt", "a") as f: f.write(sso + "\n")
                        with open("keys/accounts.txt", "a") as f: f.write(f"{email}:{password}:{sso}\n")
                        success_count += 1
                        completed_count += 1
                        avg = (time.time() - start_time) / success_count

                    if target_count > 0 and completed_count >= target_count:
                        stop_event.set()

                    print(f"[OK] зарегистрирован: {email} | SSO: {sso[:15]}... | среднее: {avg:.1f}с | прогресс: {completed_count}/{target_count if target_count else 'безлимит'}")
                    # полная строка для машинного парсинга (auto_replenish.py)
                    print(f"[SSO] {email} {sso}")
                    # CPA конвертируется фоновым воркером (cpa_worker),
                    # поток регистрации сразу берёт следующий аккаунт
                    cpa_queue.put((sso, email))
                elif '"error"' not in res.text or 'invalid' not in res.text.lower():
                    # 无明显错误但也没 SSO，打印更多信息调试
                    print(f"[-] {email} нет SSO (200 OK, len={len(res.text)}): {res.text[:150]}")
                # else: 有 invalid 错误且无 SSO，已在上面的 if 打印
            else:
                print(f"[-] {email} отправка не удалась ({res.status_code}): {res.text[:200]}")
                if res.status_code in (403, 503):
                    # CF/сервер порвали сессию — следующая итерация создаст новую
                    try: session.close()
                    except Exception: pass
                    session = None
            time.sleep(1)

        except Exception as e:
            # 捕获所有异常防止线程退出; сетевой сбой → новая сессия
            print(f"[-] исключение: {str(e)[:50]}")
            time.sleep(5)
            try:
                if session is not None: session.close()
            except Exception: pass
            session = None
        finally:
            # close the provider's browser/client for this attempt
            # (общий tmail-клиент: close() — no-op, браузер живёт весь прогон)
            try:
                if jwt and isinstance(jwt, dict):
                    _client = jwt.get("client")
                    if _client is not None and hasattr(_client, "close"):
                        _client.close()
            except Exception:
                pass

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--email-provider", choices=["gptmail", "luckmail", "mailtm", "gmail", "tmail", "fce", "outlook"], default=os.getenv("EMAIL_PROVIDER", "luckmail"), help="email-провайдер: gptmail/luckmail/mailtm/gmail/tmail/fce/outlook")
    parser.add_argument("--threads", type=int, default=None, help="количество параллельных потоков")
    parser.add_argument("--count", type=int, default=0, help="количество регистраций (0 = безлимит)")
    parser.add_argument("--cpa-threads", type=int, default=None, help="фоновые воркеры SSO→CPA (дефолт CPA_THREADS из env или 2)")
    parser.add_argument("--post-interval", type=float, default=None, help="мин. интервал между POST /sign-up, с (дефолт POST_INTERVAL или 2.0)")
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

    # 2. 启动 (фикс no-TTY: без input(), по умолчанию THREADS из .env или 2)
    if args.threads is not None:
        t = args.threads
    else:
        try:
            t = max(1, int(os.getenv("THREADS") or 2))
        except ValueError:
            t = 2

    # ферма Turnstile: один браузер на весь прогон (TS_ENGINE: camoufox|drission)
    global ts_farm, submit_limiter
    submit_limiter = PaceLimiter(args.post_interval if args.post_interval is not None
                                 else float(os.getenv("POST_INTERVAL") or 2.0))
    ts_farm = TurnstileFarm(config)
    ts_farm.start()
    print(f"[*] Turnstile-ферма: движок {ts_farm.engine}, батч {ts_farm.batch}, очередь ≤{ts_farm.max_queue}")

    # фоновая CPA-конвертация (не блокирует потоки регистрации)
    try:
        n_cpa = args.cpa_threads if args.cpa_threads is not None else int(os.getenv("CPA_THREADS") or 2)
    except ValueError:
        n_cpa = 2
    n_cpa = max(1, n_cpa)
    _cpa_threads = []
    for _i in range(n_cpa):
        _t = threading.Thread(target=cpa_worker, daemon=True, name=f"cpa-worker-{_i}")
        _t.start()
        _cpa_threads.append(_t)

    # телеметрия: одна строка каждые 30с
    threading.Thread(target=stats_worker, daemon=True, name="stats").start()

    print(f"[*] запускаю {t} потоков...")
    with concurrent.futures.ThreadPoolExecutor(max_workers=t) as executor:
        # 只提交与线程数相等的任务，让它们在内部无限循环
        futures = [executor.submit(register_single_thread, args.email_provider) for _ in range(t)]
        try:
            concurrent.futures.wait(futures)
        except KeyboardInterrupt:
            print("\n[!] получено прерывание, выходим...")
            stop_event.set()

    # дожидаемся фоновых CPA-конвертаций: очередь может быть уже пуста,
    # пока воркер ещё конвертирует взятый элемент — поэтому join, а не только empty()
    _drain_deadline = time.time() + 300
    while not cpa_queue.empty() and time.time() < _drain_deadline:
        time.sleep(2)
    for _ in _cpa_threads:
        cpa_queue.put(None)  # sentinel → воркеры завершаются
    for _t in _cpa_threads:
        _t.join(timeout=320)  # дать конвертации дописать auths/*.json

    # статистика и остановка фермы (один браузер закрывается в конце)
    if ts_farm is not None:
        try:
            _st = ts_farm.stats()
            print(f"[*] Turnstile-ферма: решено токенов {_st['solved_total']}, в очереди {_st['queue']}")
            ts_farm.shutdown()
            print("[*] браузер Turnstile закрыт")
        except Exception:
            pass

if __name__ == "__main__":
    main()
