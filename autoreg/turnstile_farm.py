"""turnstile_farm — ферма Turnstile-токенов на ОДНОМ браузере (v5).

Движки (TS_ENGINE):
  * drission (дефолт) — проверенный движок v4: DrissionPage/Chromium,
    виджеты accounts.x.ai решает. В v5 поднимается с персистентным профилем
    .chrome-profile + авто-порт (способ v3): cf_clearance переживает
    перезапуски — меньше челленджей на старте.
  * camoufox — ЭКСПЕРИМЕНТАЛЬНЫЙ антидетект-Firefox. С accounts.x.ai
    НЕ РАБОТАЕТ на 22.09.26: turnstile api.js загружается (200, тело —
    настоящий JS) и даже исполняется (onload инжекта fires), но тихо
    отказывается инициализироваться — window.turnstile не появляется и
    естественный виджет не рендерится. Голый запуск (без uBO/профиля/наших
    настроек) даёт то же — Firefox-базу анти-тампер Turnstile отклоняет.
    Движок оставлен для других целей/будущих версий camoufox.

Схема (общая для движков):
  * ОДИН браузер на весь процесс, ОДНА страница accounts.x.ai/sign-up.
  * Фоновый поток рендерит ПАЧКУ виджетов turnstile.render() за цикл
    (TS_BATCH, по умолчанию 6) — N токенов без перезагрузки страницы.
  * Готовые токены в очереди (TS_MAX_QUEUE, по умолчанию 12); протухшие
    (TTL 240с) отбрасываются потребителем.
  * Зависший виджет (интерактивный челлендж) — авто-клик по чекбоксу
    (расписание 20с/45с).
  * IP-cooldown breaker (из v2): 3 пустых батча → ремонт страницы;
    6 подряд → охлаждение IP 300с (×2 до 900с) вместо долбёжки CF.
  * dead-событие: браузер 3 раза подряд не поднялся → воркеры не ждут
    get_token впустую, прогон аккуратно завершается.

Переменные окружения:
  TS_ENGINE     — drission | camoufox (дефолт drission; camoufox — см. выше)
  TS_BATCH      — виджетов за один рендер (по умолчанию 6)
  TS_MAX_QUEUE  — максимум токенов в очереди (по умолчанию 12; глубже нет
                  смысла — токены протухают по TOKEN_TTL 240с)
  GROK_PROXY    — прокси для браузера фермы (пусто = системный туннель)
"""

import os
import queue
import re
import sys
import threading
import time

import ca_fix  # noqa: F401  — выставляет CURL_CA_BUNDLE до первого запроса

if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

TOKEN_TTL = 240.0        # отбрасывать токены старше этого (валидность ~300с)
WIDGET_WAIT = 75.0       # максимум ожидания решения пачки виджетов
POLL_INTERVAL = 2.0
CLICK_SCHEDULE = (20.0, 45.0)   # авто-клик зависших виджетов
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
SIGNUP_URL = "https://accounts.x.ai/sign-up?redirect=grok-com"

# ── JS-тела (общие; обёртка под движок добавляется в рантайме) ──────────

# Клик по варианту «email» — страница подгружает turnstile.js
CLICK_EMAIL_BODY = """
var all=document.querySelectorAll('button,[role=button]');
for(var i=0;i<all.length;i++){
  if(!all[i].offsetParent) continue;
  var t=(all[i].innerText||'').trim();
  if(t.indexOf('\u90ae\u7bb1')>=0||t.toLowerCase().indexOf('email')>=0){
    all[i].click(); break;
  }
}
"""

# Инжект turnstile.js, если страница его не подгрузила
INJECT_TS_BODY = """
window.__ts_script = '';
try {
  var s = document.createElement('script');
  s.src = 'https://challenges.cloudflare.com/turnstile/v0/api.js?render=explicit';
  s.onload = function(){ window.__ts_script = 'ok'; };
  s.onerror = function(){ window.__ts_script = 'fail'; };
  document.head.appendChild(s);
} catch(e) { window.__ts_script = 'fail'; }
"""

# Пачка виджетов: использует _n (число) и _sk (sitekey); токены → __ts_tokens
BATCH_BODY = """
var old = document.querySelectorAll('[id^="__ts_div_"]');
for (var i = 0; i < old.length; i++) old[i].remove();
window.__ts_tokens = [];
window.__ts_pending = {};
for (var i = 0; i < _n; i++) {
    var d = document.createElement('div');
    d.id = '__ts_div_' + i;
    d.style.cssText = 'position:fixed;top:' + (10 + i * 90) +
        'px;right:10px;z-index:99999';
    document.body.appendChild(d);
    window.__ts_pending[d.id] = true;
    try {
        var _cb = (function (div_id) {
            return function (t) {
                if (window.__ts_pending) delete window.__ts_pending[div_id];
                window.__ts_tokens.push(t);
            };
        })(d.id);
        turnstile.render(d, {
            sitekey: _sk, theme: 'light',
            callback: _cb,
            'error-callback': function () {}
        });
    } catch (e) {}
}
return _n;
"""

DRAIN_STMT = "return (window.__ts_tokens||[]).splice(0, 32)"
DRAIN_FN = "() => (window.__ts_tokens||[]).splice(0, 32)"
PENDING_STMT = "return Object.keys(window.__ts_pending || {})"
PENDING_FN = "() => Object.keys(window.__ts_pending || {})"


class TurnstileFarm:
    """Фоновый производитель токенов; get_token() — потребитель.

    Публичный API потокобезопасен: start/shutdown/get_token/stats/dead.
    Всё, что трогает браузер, живёт строго в потоке _run (playwright sync
    привязан к создавшему потоку).
    """

    def __init__(self, config: dict, batch: int | None = None,
                 max_queue: int | None = None, engine: str | None = None):
        self.config = config  # общий словарь grok_auto: site_key/action_id/state_tree
        eng = (engine or os.getenv("TS_ENGINE") or "drission").strip().lower()
        self.engine = eng if eng in ("camoufox", "drission") else "drission"
        self.batch = batch or int(os.getenv("TS_BATCH") or 6)
        self.max_queue = max_queue or int(os.getenv("TS_MAX_QUEUE") or 12)
        self.tokens: "queue.Queue[tuple[str, float]]" = queue.Queue(maxsize=max(1, self.max_queue))
        self.solved_total = 0
        self.stop = threading.Event()
        self.dead = threading.Event()   # браузер не поднимается — прогону пора сворачиваться
        self.page = None
        self.site_key = config.get("site_key") or None
        self._fail_streak = 0
        self._opening = False           # True, пока _open_page поднимает браузер/страницу
        self._thread: threading.Thread | None = None
        self._cm = None                 # camoufox context manager (владеет потоком _run)
        self._ctx = None
        self._proxy = (os.getenv("GROK_PROXY") or "").strip()

    # ── жизненный цикл ──────────────────────────────────────────────

    def start(self):
        if self._thread and self._thread.is_alive():
            return
        self._thread = threading.Thread(target=self._run, daemon=True, name="ts-farm")
        self._thread.start()

    def shutdown(self):
        self.stop.set()
        if self._thread:
            self._thread.join(timeout=20)
            if self._thread.is_alive():
                print("[TS-farm] предупреждение: продюсер не завершился за 20с "
                      "(браузер закроется вместе с процессом)")
        # браузер закрывает сам поток _run в finally — playwright sync
        # нельзя трогать из чужого потока

    def _sleep_interruptible(self, seconds: float):
        end = time.time() + seconds
        while not self.stop.is_set() and time.time() < end:
            time.sleep(min(2.0, max(0.0, end - time.time())))

    # ── главный цикл продюсера (breaker + dead) ─────────────────────

    def _run(self):
        fail_streak = 0
        cooldown = 300          # старт паузы IP-брейкера, сек (×2 до 900)
        launch_fails = 0
        try:
            while not self.stop.is_set():
                try:
                    if self.page is None:
                        self._opening = True
                        try:
                            self._open_page()
                        finally:
                            self._opening = False
                        launch_fails = 0
                        continue
                    self._drain_pending()
                    if self.tokens.qsize() >= self.max_queue - 1:
                        time.sleep(POLL_INTERVAL)   # очередь полна — не решаем впрок
                        continue
                    got = self._batch_render()
                    if self.tokens.qsize() >= self.max_queue - 1:
                        continue                    # очередь полна = не фейл
                    if got > 0:
                        fail_streak = 0
                        cooldown = 300
                    else:
                        fail_streak += 1
                        if fail_streak >= 6:
                            # IP-cooldown (breaker из v2): CF эскалировал —
                            # долбёжка одного IP только усугубляет
                            print(f"[TS-farm] {fail_streak} пустых батчей подряд "
                                  f"— охлаждение IP {cooldown}с (breaker)")
                            self._sleep_interruptible(cooldown)
                            cooldown = min(cooldown * 2, 900)
                            fail_streak = 0
                        elif fail_streak >= 3:
                            print("[TS-farm] 3 пустых батча — ремонтирую страницу")
                            self._reset_page()
                            time.sleep(5)
                        else:
                            time.sleep(2)
                except Exception as e:
                    print(f"[TS-farm] ошибка: {type(e).__name__} {e}")
                    opening = self._opening
                    self._close_engine()
                    if opening:
                        # браузер/страница не поднялись (нет Chrome, профиль занят,
                        # сайт недоступен, prepare упал)
                        launch_fails += 1
                        if launch_fails >= 3:
                            print("[TS-farm] 3 раза подряд не поднялся браузер/страница — ферма мертва")
                            self.dead.set()
                            return
                        time.sleep(10)
                    else:
                        # страница/браузер сломались на ходу — полный перезапуск,
                        # эскалация через fail_streak (ремонт → cooldown)
                        launch_fails = 0
                        fail_streak += 1
                        time.sleep(5)
        finally:
            self._close_engine()

    # ── движки: открытие/ремонт/закрытие ────────────────────────────

    def _open_page(self):
        if self.engine == "camoufox":
            self._cmf_open()
            self._cmf_prepare()
        else:
            self._dp_open()

    def _reset_page(self):
        """Лёгкий ремонт без перезапуска браузера."""
        if self.engine == "camoufox":
            self._cmf_prepare()   # повторный goto + клик email + turnstile-check
        else:
            # DrissionPage: v4-поведение — quit, следующий цикл пересоздаст
            self._dp_close()

    def _close_engine(self):
        if self.engine == "camoufox":
            self._cmf_close()
        else:
            self._dp_close()
        self.page = None

    # camoufox ────────────────────────────────────────────────────────

    def _cmf_open(self):
        from camoufox.sync_api import Camoufox
        kwargs: dict = {
            "headless": False,           # Turnstile в headless эскалирует чаще
            "persistent_context": True,  # cf_clearance живёт между запусками
            "user_data_dir": os.path.join(SCRIPT_DIR, ".camoufox-profile"),
        }
        if self._proxy:
            kwargs["proxy_server"] = (self._proxy if "://" in self._proxy
                                      else "http://" + self._proxy)
        try:
            import camoufox.geoip  # noqa: F401 — extra [geoip]
            kwargs["geoip"] = True      # локаль/TZ по egress-IP (одна личность)
        except Exception:
            pass
        try:
            # camoufox комплектуется uBlock Origin с 3p-фильтрами — он режет
            # challenges.cloudflare.com/turnstile/v0/api.js (window.turnstile
            # не появляется, виджет не рендерится). Адблок для реги не нужен.
            from camoufox.addons import DefaultAddons
            kwargs["exclude_addons"] = [DefaultAddons.UBO]
        except Exception:
            pass
        try:
            cm = Camoufox(**kwargs)
            ctx = cm.__enter__()
        except Exception:
            if not kwargs.pop("geoip", None):
                raise
            print("[TS-farm:camoufox] geoip-запуск не удался — повторяю без geoip")
            cm = Camoufox(**kwargs)
            ctx = cm.__enter__()
        self._cm, self._ctx = cm, ctx
        self.page = ctx.new_page()
        print(f"[TS-farm:camoufox] браузер поднят (профиль .camoufox-profile"
              f"{', geoip' if kwargs.get('geoip') else ''}"
              f"{', proxy' if self._proxy else ''})")

    def _cmf_close(self):
        cm, self._cm, self._ctx = self._cm, None, None
        if cm is not None:
            try:
                cm.__exit__(None, None, None)
            except Exception:
                pass

    def _cmf_prepare(self):
        page = self.page
        page.goto(SIGNUP_URL, timeout=60000, wait_until="domcontentloaded")
        page.wait_for_timeout(4000)
        page.evaluate("() => {" + CLICK_EMAIL_BODY + "}")
        page.wait_for_timeout(1500)
        if not page.evaluate("() => !!window.turnstile"):
            page.evaluate("() => {" + INJECT_TS_BODY + "}")
            for _ in range(40):                       # до 20с
                v = page.evaluate("() => window.__ts_script || ''")
                if v in ("ok", "fail"):
                    break
                page.wait_for_timeout(500)
        if not page.evaluate("() => !!window.turnstile"):
            raise RuntimeError("window.turnstile недоступен после инжекта")
        # свежие site_key/state_tree с живой страницы (action_id — из HTTP-скана main)
        html = page.content()
        m = re.search(r'sitekey":"(0x4[a-zA-Z0-9_-]+)"', html)
        if m:
            self.config["site_key"] = m.group(1)
        m = re.search(r'next-router-state-tree":"([^"]+)"', html)
        if m:
            self.config["state_tree"] = m.group(1)
        if self.config.get("site_key"):
            self.site_key = self.config["site_key"]
        print(f"[TS-farm:camoufox] страница готова: site_key={self.site_key} "
              f"action_id={str(self.config.get('action_id'))[:12]}…")

    # drission (движок v4) ────────────────────────────────────────────

    def _dp_open(self):
        from grok_free import browser_init
        bi = browser_init(solve=False, profile=True)
        self.page = bi["page"]
        for k in ("site_key", "action_id", "state_tree"):
            if bi.get(k):
                self.config[k] = bi[k]
        if self.config.get("site_key"):
            self.site_key = self.config["site_key"]
        tok = bi.get("ts_token")
        if tok and len(tok) > 50:
            self._push(tok)
        print(f"[TS-farm:drission] готова: site_key={self.site_key} "
              f"action_id={str(self.config.get('action_id'))[:12]}…")

    def _dp_close(self):
        if self.page is not None:
            try:
                self.page.quit()
            except Exception:
                pass
        self.page = None

    # ── производство токенов (общее) ────────────────────────────────

    def _push(self, token: str):
        if not token or len(token) <= 50:
            return
        try:
            self.tokens.put_nowait((token, time.time()))
            self.solved_total += 1
        except queue.Full:
            pass

    def _drain_pending(self):
        """Забрать поздно пришедшие токены из прошлого рендера."""
        if self.page is None:
            return
        try:
            got = (self.page.evaluate(DRAIN_FN) if self.engine == "camoufox"
                   else self.page.run_js(DRAIN_STMT))
        except Exception:
            return
        for t in got or []:
            if isinstance(t, str):
                self._push(t)

    def _pending_ids(self):
        """id виджетов, чей токен ещё не пришёл."""
        if self.page is None:
            return []
        try:
            ids = (self.page.evaluate(PENDING_FN) if self.engine == "camoufox"
                   else self.page.run_js(PENDING_STMT))
        except Exception:
            return []
        return [i for i in ids or [] if isinstance(i, str)]

    def _batch_render(self) -> int:
        """Рендер self.batch виджетов на живой странице; вернуть число токенов.
        Если виджет завис (интерактивный челлендж) — кликнуть чекбокс."""
        if self.page is None:
            return 0
        n = min(self.batch, self.max_queue - self.tokens.qsize())
        if n <= 0:
            return 0
        site_key = self.site_key or self.config.get("site_key")
        if not site_key:
            return 0
        before = self.solved_total
        self._drain_pending()
        try:
            if self.engine == "camoufox":
                self.page.evaluate("([_n,_sk]) => {" + BATCH_BODY + "}",
                                   [n, site_key], timeout=30000)
            else:
                self.page.run_js("var _n=arguments[0],_sk=arguments[1];" + BATCH_BODY,
                                 n, site_key, timeout=30)
        except Exception as e:
            print(f"[TS-farm] рендер не удался: {e}")
            return 0

        deadline = time.time() + WIDGET_WAIT
        clicked_at = []  # уже сделанные попытки клика (моменты)
        t_render = time.time()
        while time.time() < deadline and not self.stop.is_set():
            time.sleep(POLL_INTERVAL)
            self._drain_pending()
            pending = self._pending_ids()
            # интерактивный челлендж: кликнуть чекбокс зависших виджетов
            for cs in CLICK_SCHEDULE:
                if not pending or len(clicked_at) >= len(CLICK_SCHEDULE):
                    break
                if (time.time() - t_render) >= cs and cs not in clicked_at:
                    clicked_at.append(cs)
                    print(f"[TS-farm] виджет завис ({len(pending)}), кликаю чекбокс…")
                    for div_id in pending:
                        self._click_checkbox(div_id)
                    break
            if not pending and self.solved_total > before:
                break   # весь батч решён — не выжигаем WIDGET_WAIT впустую
                # (раньше цикл всегда ждал 75с → ферма была узким местом)
            if self.tokens.qsize() >= self.max_queue:
                break
        return self.solved_total - before

    def _click_checkbox(self, div_id: str):
        """Клик по чекбоксу Turnstile внутри виджета (в iframe)."""
        sel_id = div_id.replace('"', "")
        try:
            if self.engine == "camoufox":
                try:
                    self.page.frame_locator(f'#{sel_id} iframe') \
                        .locator('input[type=checkbox]').first.click(timeout=2500)
                except Exception:
                    # фолбэк: клик по области чекбокса (30,33) внутри iframe
                    self.page.locator(f'#{sel_id} iframe').click(
                        position={"x": 30, "y": 33}, timeout=2000)
            else:
                ele = self.page.ele(f"#{sel_id}", timeout=2)
                if ele:
                    ele.click.at(30, 33)
        except Exception as e:
            print(f"[TS-farm] клик {div_id} не удался: {e}")

    # ── потребитель ─────────────────────────────────────────────────

    def get_token(self, timeout: float = 180.0) -> str | None:
        """Свежий токен или None. Протухшие отбрасывает и ждёт дальше.
        Ферма мертва и очередь пуста → сразу None (не ждём впустую)."""
        deadline = time.time() + timeout
        while time.time() < deadline and not self.stop.is_set():
            if self.dead.is_set() and self.tokens.empty():
                return None
            remaining = max(0.05, deadline - time.time())
            try:
                token, ts = self.tokens.get(timeout=min(3.0, remaining))
            except queue.Empty:
                continue
            if len(token) > 50 and (time.time() - ts) <= TOKEN_TTL:
                return token
            # протух — взять следующий
        return None

    def stats(self) -> dict:
        return {"queue": self.tokens.qsize(), "solved_total": self.solved_total,
                "engine": self.engine, "dead": self.dead.is_set()}
