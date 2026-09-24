#!/usr/bin/env python3
"""gateway_keepalive.py — держит шлюз grok2api живым (авто-рестарт при падении).

Локальный grok2api.exe может упасть под нагрузкой (рега + пул 2000+ аккаунтов).
Watchdog:
  • каждые CHECK_INTERVAL сек пингует http://127.0.0.1:8000/healthz
  • если FAIL_LIMIT раз подряд не ответил -> перезапускает grok2api.exe
  • логирует в stdout (видно через `hub logs gateway-keepalive`)

Запуск:  python gateway_keepalive.py
Стоп:    Ctrl+C
"""
import os, subprocess, sys, time, urllib.request

EXE = os.getenv("G2A_EXE", r"C:\Users\User\grok2api-deploy\grok2api\grok2api.exe")
HEALTHZ = os.getenv("G2A_HEALTHZ", "http://127.0.0.1:8000/healthz")
CHECK_INTERVAL = int(os.getenv("KA_INTERVAL", "30"))   # сек между проверками
FAIL_LIMIT = int(os.getenv("KA_FAIL_LIMIT", "3"))      # сколько фейлов до рестарта


def healthy():
    try:
        with urllib.request.urlopen(HEALTHZ, timeout=5) as r:
            return r.status == 200
    except Exception:
        return False


def start_gateway():
    if not os.path.exists(EXE):
        print(f"[keepalive] EXE not found: {EXE}")
        return None
    cwd = os.path.dirname(EXE)
    flags = 0x00000008 | 0x00000200 if os.name == "nt" else 0  # DETACHED | NEW_PROCESS_GROUP
    p = subprocess.Popen([EXE], cwd=cwd, creationflags=flags,
                         stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                         stderr=subprocess.DEVNULL)
    print(f"[keepalive] started grok2api pid={p.pid}")
    # дождаться healthz (до 30с)
    for _ in range(15):
        time.sleep(2)
        if healthy():
            print("[keepalive] gateway healthy after restart")
            return p
    print("[keepalive] WARNING: started but healthz not up in 30s")
    return p


def main():
    print(f"[keepalive] watching {HEALTHZ} every {CHECK_INTERVAL}s (restart after {FAIL_LIMIT} fails)")
    print(f"[keepalive] exe: {EXE}")
    fails = 0
    restarts = 0
    while True:
        if healthy():
            if fails:
                print(f"[keepalive] recovered after {fails} fail(s)")
            fails = 0
        else:
            fails += 1
            print(f"[keepalive] healthz FAIL {fails}/{FAIL_LIMIT}")
            if fails >= FAIL_LIMIT:
                print(f"[keepalive] gateway down -> restarting (restart #{restarts+1})")
                start_gateway()
                restarts += 1
                fails = 0
        time.sleep(CHECK_INTERVAL)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\n[keepalive] stopped")
