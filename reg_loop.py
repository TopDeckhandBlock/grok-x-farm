#!/usr/bin/env python3
"""reg_loop.py — непрерывная регистрация; браузеры закрываются ПОСЛЕ каждого цикла.

Запуск: python reg_loop.py
Стоп:   Ctrl+C или создать файл STOP_REGISTRATION в корне репы.

Почему видимые браузеры: бесплатный Turnstile-солвер НЕ проходит в headless
(Cloudflare 428 — см. email_service.py: "实测必须用有头 Chrome"). Окна во время
регистрации неизбежны. Задача — не дать им КОПИТЬСЯ и закрывать ПОСЛЕ работы.

Стратегия уборки (безопасная, НЕ ломает активную регистрацию):
  • КОРОТКИЕ циклы (COUNT_PER_CYCLE аккаунтов) -> уборка частая.
  • ПОСЛЕ каждого цикла (subprocess завершился -> активных браузеров нет):
    kill_all_automation() закрывает ВСЕ automation-окна. Guaranteed clean.
  • ВО ВРЕМЯ цикла браузеры НЕ трогаем: регистрация аккаунта идёт 200-340с,
    убийство по возрасту/количеству оборвёт активный Turnstile
    ("Target page has been closed") -> email не создастся.
  • Safety-net watchdog: раз в WATCHDOG_INTERVAL убивает только браузеры СТАРШЕ
    STALE_AGE_SEC (600с > max 340с reg = точно протёк, активный не тронет).

Реальный Chrome юзера (без automation-флагов) не трогается НИКОГДА.
"""
import os, sys, time, json, threading, subprocess
from datetime import datetime

HERE = os.path.dirname(os.path.abspath(__file__))
STOP_FILE = os.path.join(HERE, "STOP_REGISTRATION")

COUNT_PER_CYCLE = 3      # browser-light: 3 acc/cycle (user: no browser spam)
THREADS = 1              # browser-light: ONE visible Chrome (no spam)
DELAY_BETWEEN = 10       # сек между циклами
# Команда регистрации (портабельно между grok-x-farm и grok-suite).
# grok-x-farm:  farm.py reg --count N --threads T   (default)
# grok-suite:   export REG_CMD="python autoreg/run_cli_auto.py --count {count} --workers {threads}"
# Плейсхолдеры {count}/{threads} подставляются из COUNT_PER_CYCLE/THREADS.
REG_CMD_TEMPLATE = os.getenv("REG_CMD", "python farm.py reg --count {count} --threads {threads}")

AUTO_FLAGS = ("--remote-debugging-port", "--enable-automation",
              "--disable-blink-features=automationcontrolled", "scoped_dir",
              "ms-playwright", "patchright", "playwright")
STALE_AGE_SEC = 600      # safety-net: >600с = точно протёк (max reg ~340с)
WATCHDOG_INTERVAL = 60   # как часто safety-net проверяет


def _ps_chrome():
    """Список {ProcessId, CreationDate, CommandLine} для chrome.exe."""
    ps = ('Get-CimInstance Win32_Process -Filter "name=\'chrome.exe\'" | '
          'Select-Object ProcessId,CreationDate,CommandLine | ConvertTo-Json -Depth 3')
    r = subprocess.run(["powershell", "-NoProfile", "-Command", ps],
                       capture_output=True, text=True, timeout=40,
                       encoding="utf-8", errors="replace")
    data = json.loads(r.stdout) if r.stdout.strip() else []
    if isinstance(data, dict):
        data = [data]
    return data


def _is_auto(cl):
    cl = (cl or "").lower()
    return any(f in cl for f in AUTO_FLAGS)


def _age_sec(creation):
    """CreationDate (CIM) -> возраст в секундах, или None."""
    if not creation:
        return None
    try:
        s = str(creation)
        if "T" in s:
            dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
        else:
            dt = datetime.strptime(s[:14], "%Y%m%d%H%M%S")
        return (datetime.now(dt.tzinfo) - dt).total_seconds()
    except Exception:
        return None


def kill_all_automation():
    """Убить ВСЕ automation-chrome. Безопасно когда reg-процесс завершён
    (между циклами / при выходе). Реальный Chrome юзера не трогает."""
    if os.name != "nt":
        return 0
    try:
        chrome = _ps_chrome()
    except Exception:
        return 0
    killed = 0
    for c in chrome:
        if _is_auto(c.get("CommandLine")):
            subprocess.run(["taskkill", "/F", "/PID", str(c.get("ProcessId")), "/T"],
                           capture_output=True, text=True, timeout=10)
            killed += 1
    return killed


def kill_stale_browsers():
    """Safety-net: убить automation-chrome СТАРШЕ STALE_AGE_SEC (протёкшие).
    Активные (reg идёт 200-340с < 600с) и реальный Chrome юзера не трогает."""
    if os.name != "nt":
        return 0
    try:
        chrome = _ps_chrome()
    except Exception:
        return 0
    killed = 0
    for c in chrome:
        if not _is_auto(c.get("CommandLine")):
            continue
        age = _age_sec(c.get("CreationDate"))
        if age is not None and age > STALE_AGE_SEC:
            subprocess.run(["taskkill", "/F", "/PID", str(c.get("ProcessId")), "/T"],
                           capture_output=True, text=True, timeout=10)
            killed += 1
    return killed


def watchdog(stop_event):
    """Фоновый safety-net: убивает ТОЛЬКО протёкшие (>600с) браузеры.
    Активные не трогает — они моложе порога."""
    while not stop_event.is_set():
        stop_event.wait(WATCHDOG_INTERVAL)
        if stop_event.is_set():
            break
        try:
            n = kill_stale_browsers()
            if n:
                print(f"[watchdog] killed {n} stale browsers (>{STALE_AGE_SEC}s)")
        except Exception as e:
            print(f"[watchdog] error: {e}")


def main():
    py = sys.executable
    cycle = 0
    print("[loop] continuous registration started")
    print(f"[loop] {COUNT_PER_CYCLE} acc/cycle, threads={THREADS}, {DELAY_BETWEEN}s cooldown")
    print(f"[loop] browsers closed AFTER each cycle; safety-net kills only >{STALE_AGE_SEC}s stale")
    print("[loop] visible Chrome REQUIRED (headless -> Cloudflare 428); user tabs never touched")
    print(f"[loop] stop: create {os.path.basename(STOP_FILE)} or Ctrl+C")

    stop_event = threading.Event()
    wd = threading.Thread(target=watchdog, args=(stop_event,), daemon=True)
    wd.start()

    # стартовая уборка (наследие прошлых запусков)
    n = kill_all_automation()
    if n:
        print(f"[loop] pre-clean: closed {n} leftover browsers")

    try:
        while True:
            if os.path.exists(STOP_FILE):
                print("[loop] STOP_REGISTRATION found, exiting")
                os.remove(STOP_FILE)
                break

            cycle += 1
            print(f"\n{'='*50}\n[loop] CYCLE {cycle}\n{'='*50}")

            import shlex
            cmd = shlex.split(REG_CMD_TEMPLATE.format(count=COUNT_PER_CYCLE, threads=THREADS))
            if cmd and cmd[0].lower() in ("python", "python3", "py"):
                cmd[0] = py   # использовать текущий интерпретатор
            r = subprocess.run(cmd, cwd=HERE,
                               env=dict(os.environ, PYTHONIOENCODING="utf-8", PYTHONUTF8="1"))
            print(f"[loop] cycle {cycle} exit: {r.returncode}")

            # ГЛАВНАЯ уборка: цикл завершён, активных браузеров нет -> закрыть все
            n = kill_all_automation()
            print(f"[loop] post-cycle clean: closed {n} browsers")

            if os.path.exists(STOP_FILE):
                print("[loop] stop signal received")
                os.remove(STOP_FILE)
                break

            print(f"[loop] sleeping {DELAY_BETWEEN}s...")
            time.sleep(DELAY_BETWEEN)
    finally:
        stop_event.set()
        wd.join(timeout=5)
        n = kill_all_automation()
        print(f"[loop] exit-clean: closed {n} browsers. bye")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\n[loop] interrupted, cleaning up...")
        kill_all_automation()
        print("[loop] bye")
