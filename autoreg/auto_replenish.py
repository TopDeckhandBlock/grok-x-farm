"""
Grok 账号自动补位系统 v2
───────────────────────
完整流水线: Clash IP轮换 → 邮箱注册(SSO) → PKCE转换(CPA) → Token刷新
监控 auths/ 目录，可用账号 < 阈值时自动补位。

用法:
  python auto_replenish.py                          # 一次性检查补位
  python auto_replenish.py --daemon 600             # 每 10 分钟守护
  python auto_replenish.py --min 3 --rotate-region  # 保持 3 个，跨区域切换
  python auto_replenish.py --check                  # 仅检查状态
"""
import os, sys, time, json, subprocess, argparse, random
from datetime import datetime, timezone
import requests as _requests
import urllib3
urllib3.disable_warnings()

if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

def _utc_to_ts(utc_str):
    """解析 UTC ISO 时间戳 → Unix timestamp"""
    try:
        return datetime.strptime(utc_str, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc).timestamp()
    except (ValueError, OverflowError):
        return None

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
AUTH_DIR = os.getenv("CPA_AUTHS_DIR") or r"D:\CLIProxyAPIPlus\auths"
PROXY = os.getenv("GROK_PROXY") or ""
GROK2API_BASE = os.getenv("GROK2API_BASE") or "http://127.0.0.1:8000"
GROK2API_USER = os.getenv("GROK2API_USER") or "admin"
GROK2API_PASS = os.getenv("GROK2API_PASS") or ""
GROK_REG = os.path.join(SCRIPT_DIR, "grok.py")          # YesCaptcha 版（高成功率）
if not os.path.exists(GROK_REG):
    # в этой копии архива grok.py нет — используем grok_auto.py (ферма токенов)
    GROK_REG = os.path.join(SCRIPT_DIR, "grok_auto.py")
SSO_TO_CPA = os.path.join(SCRIPT_DIR, "sso_to_cpa.py")
TOKEN_DAEMON = os.path.join(SCRIPT_DIR, "token_daemon.py")
# 直接导入转换函数，不走子进程
# 2026-08-06: 优先 Device Flow (device_mint, 修正 scope 版)，PKCE (sso_to_cpa) 被 CF 拦截
try:
    sys.path.insert(0, SCRIPT_DIR)
    from device_mint import sso_to_device as _sso_to_cpa_direct
    from sso_to_cpa import save_auth
    CONVERT_MODE = "device"
    HAS_DIRECT_CONVERT = True
except ImportError:
    try:
        sys.path.insert(0, SCRIPT_DIR)
        from sso_to_cpa import sso_to_cpa as _sso_to_cpa_direct, save_auth
        CONVERT_MODE = "pkce"
        HAS_DIRECT_CONVERT = True
    except ImportError:
        HAS_DIRECT_CONVERT = False

# Clash 轮换器
try:
    from clash_rotator import (random_switch, switch_region, get_current_ip,
                                health as clash_health, snapshot, restore)
    HAS_CLASH = True
except ImportError:
    HAS_CLASH = False


# ── IP 洁净度探测 ──
CF_MARKERS = [b"Cloudflare", b"Attention Required", b"cf-challenge",
              b"cf-browser-verification", b"Just a moment"]
# grok.py 实际请求 accounts.x.ai，不是 x.com
PROBE_URLS = ["https://accounts.x.ai/sign-up", "https://x.com"]
PROBE_TIMEOUT = 15


def probe_ip(ip=None):
    """通过 Clash 代理探测 accounts.x.ai（grok.py 的真正目标）。"""
    proxies = {"http": PROXY, "https": PROXY} if (HAS_CLASH and PROXY) else None
    for url in PROBE_URLS:
        try:
            resp = _requests.get(url, headers={"User-Agent": "Mozilla/5.0"},
                                proxies=proxies, timeout=PROBE_TIMEOUT,
                                verify=False)
            body = resp.content[:8192]
            for marker in CF_MARKERS:
                if marker in body:
                    return False, f"CF blocked {url} (marker={marker.decode()[:30]})"
        except Exception as e:
            return False, f"{url}: {type(e).__name__}"
    return True, "all clean"


def find_clean_ip(max_attempts=10):
    """轮换 Clash 节点直到找到洁净 IP，返回 IP 或 None。"""
    if not HAS_CLASH:
        return None
    for i in range(max_attempts):
        try:
            random_switch()
        except Exception as e:
            print(f"  [IP] switch failed: {e}")
        time.sleep(2)
        ip = get_current_ip()
        clean, detail = probe_ip()
        status = "✅" if clean else "❌"
        print(f"  [IP] #{i+1} {ip} {status} - {detail}")
        if clean:
            return ip
        time.sleep(1)
    return None


# ── 单号注册（带 IP 重试）──
GROK_TXT = os.path.join(SCRIPT_DIR, "keys", "grok.txt")
ACCOUNTS_TXT = os.path.join(SCRIPT_DIR, "keys", "accounts.txt")


def _file_lines(path):
    try:
        with open(path, encoding="utf-8-sig") as f:
            return [l.strip() for l in f if l.strip()]
    except FileNotFoundError:
        return []


def register_one(script, extra_args, max_retries=5):
    """注册 1 个账号，失败换 IP 重试。返回 (ok: bool, sso_token: str|None, email: str|None)。
    注意：不使用 probe_ip()（requests 库会触发 CF 拦截），
    直接跑 grok.py（curl_cffi 可绕过 CF），从 stdout 判断是否被拦截。"""
    for attempt in range(1, max_retries + 1):
        if attempt > 1 and HAS_CLASH:
            print(f"  [RETRY] switching IP and retrying ({attempt}/{max_retries})...")
            find_clean_ip(max_attempts=5)
        else:
            ip = get_current_ip()
            print(f"  [IP] {ip} — running grok.py directly (skipping probe to avoid CF block)")

        # 记录注册前 grok.txt 行数
        before_lines = _file_lines(GROK_TXT)
        before_accts = _file_lines(ACCOUNTS_TXT)

        cmd = [sys.executable, script] + extra_args + ["--count", "1"]
        try:
            result = subprocess.run(
                cmd, cwd=SCRIPT_DIR,
                capture_output=True, text=True,
                encoding="utf-8", errors="replace",
                timeout=180
            )
        except subprocess.TimeoutExpired:
            print(f"  [TIMEOUT] registration timed out")
            continue
        except Exception as e:
            print(f"  [ERR] {e}")
            continue

        # 检查是否被 CF 拦截（grok.py 返回码 0 但实际被拦截）
        if "Blocked due to abusive traffic patterns" in result.stdout or "Blocked due to abusive traffic patterns" in result.stderr:
            print(f"  [CF] blocked by Cloudflare, switching IP and retrying")
            continue

        # 从 stdout 提取 SSO（优先，因为文件写入可能因 CWD 问题失败）
        import re as _re
        sso_found = None
        for line in result.stdout.splitlines():
            # grok_auto.py: машинная строка "[SSO] {email} {sso}"
            m = _re.search(r'\[SSO\]\s*(\S+)\s+([A-Za-z0-9._-]{20,})', line)
            if not m:
                # старый формат grok.py: "注册成功: ... | SSO: ..."
                m = _re.search(r'注册成功:\s*(\S+)\s*\|\s*SSO:\s*(\S+)', line)
            if m:
                email_found = m.group(1)
                sso_found = m.group(2)
                break

        if sso_found and email_found:
            print(f"  [OK] {email_found} SSO={sso_found[:15]}... (stdout)")
            return True, sso_found, email_found

        # 兜底：检查文件是否写入
        after_lines = _file_lines(GROK_TXT)
        after_accts = _file_lines(ACCOUNTS_TXT)
        new_sso = [l for l in after_lines if l not in before_lines]
        new_accts = [l for l in after_accts if l not in before_accts]
        if new_accts:
            for line in new_accts:
                parts = line.split(":")
                if len(parts) >= 3:
                    email = parts[0].replace("﻿", "")
                    sso = parts[2]
                    print(f"  [OK] {email} SSO={sso[:15]}... (file)")
                    return True, sso, email

        # 打印输出以便排查
        if result.stdout.strip():
            preview = result.stdout.strip()[:500]
            # 只打印非空结果
            if "注册成功" not in preview and "Action ID" not in preview:
                print(f"  [STDOUT] {preview}")
        if result.stderr.strip():
            print(f"  [STDERR] {result.stderr.strip()[:300]}")

        print(f"  [FAIL] registration produced no SSO (exit code {result.returncode})")
    return False, None, None


def count_available():
    """统计可用账号数（未禁用且 access_token 存在）"""
    if not os.path.isdir(AUTH_DIR):
        return 0, []
    available = []
    for fn in sorted(os.listdir(AUTH_DIR)):
        if not fn.startswith("xai-") or not fn.endswith(".json"):
            continue
        path = os.path.join(AUTH_DIR, fn)
        try:
            with open(path, encoding="utf-8") as f:
                d = json.load(f)
            if d.get("access_token") and not d.get("disabled"):
                # 检查是否过期
                expired_str = d.get("expired", "")
                if expired_str:
                    try:
                        ts = _utc_to_ts(expired_str)
                        if ts and ts < time.time():
                            continue  # 过期的不算可用
                    except (ValueError, OverflowError):
                        pass
                available.append(d.get("email", fn))
        except Exception:
            pass
    return len(available), available


def run_registration(count=3, rotate_region=False, use_free=False):
    """逐个注册账号，每个号失败自动换 IP 重试。返回 (success_count, sso_list)。"""
    script = GROK_FREE if use_free else GROK_REG
    script_name = "grok_free.py (DrissionPage)" if use_free else "grok.py (YesCaptcha)"
    print(f"\n{'='*50}")
    print(f"[STEP 1/3] registering {count} accounts — {script_name}")
    print(f"{'='*50}")

    if use_free:
        # DrissionPage 浏览器模式：自带 CF 绕过能力，不需要 IP 探测
        extra_args = ["--no-rotate", "--count", "1", "--min-delay", "8", "--max-delay", "15"]
        timeout = 300  # 浏览器单个号 5 分钟
    else:
        # провайдер из .env (по умолчанию tmail — как в основном регере)
        _email_provider = str(os.getenv("EMAIL_PROVIDER") or "tmail").strip().lower()
        extra_args = ["--threads", "1", "--email-provider", _email_provider]
        timeout = 240
    sso_list = []
    for idx in range(count):
        tag = f"[{idx+1}/{count}]"
        print(f"\n{tag} registering #{idx+1}...")
        if use_free:
            # 浏览器模式：直接跑，换 IP 交给 grok_free 的 --rotate-interval（但我们是 --no-rotate）
            ok, sso, email = register_one_free(script, extra_args, timeout)
        else:
            ok, sso, email = register_one(script, extra_args)
        if ok:
            success += 1
            if sso and email:
                sso_list.append({"sso": sso, "email": email})
            print(f"{tag} OK ({success}/{count} succeeded)")
            time.sleep(3)
        else:
            print(f"{tag} FAILED, skipping")

    print(f"\nregistration done: {success}/{count} succeeded")
    return success, sso_list


def register_one_free(script, extra_args, timeout=300):
    """浏览器模式注册单个号。DrissionPage 自带 CF 绕过，不额外探测 IP。
    返回 (ok, sso, email)。"""
    before_lines = _file_lines(GROK_TXT)
    before_accts = _file_lines(ACCOUNTS_TXT)

    cmd = [sys.executable, script] + extra_args
    ip = get_current_ip() if HAS_CLASH else "unknown"
    print(f"  [IP] {ip} (browser mode, skip probe)")

    try:
        result = subprocess.run(
            cmd, cwd=SCRIPT_DIR,
            capture_output=True, text=True,
            encoding="utf-8", errors="replace",
            timeout=timeout
        )
    except subprocess.TimeoutExpired:
        print(f"  [TIMEOUT] registration timed out")
        return False, None, None
    except Exception as e:
        print(f"  [ERR] {e}")
        return False, None, None

    after_lines = _file_lines(GROK_TXT)
    after_accts = _file_lines(ACCOUNTS_TXT)
    new_sso = [l for l in after_lines if l not in before_lines]
    new_accts = [l for l in after_accts if l not in before_accts]

    if new_sso and new_accts:
        for line in new_accts[-len(new_sso):]:
            parts = line.split(":")
            if len(parts) >= 3:
                email = parts[0].replace("﻿", "")
                sso = parts[2]
                print(f"  [OK] {email} SSO={sso[:15]}...")
                return True, sso, email

    if result.returncode == 0:
        print(f"  [OK] registration process returned 0")
        return True, None, None

    print(f"  [FAIL] registration returned {result.returncode}")
    return False, None, None


def convert_sso_list(sso_list):
    """直接调用 sso_to_cpa 转换 SSO token 列表。返回成功数。"""
    if not HAS_DIRECT_CONVERT:
        return 0
    print(f"\n{'='*50}")
    print(f"[STEP 2/3] converting {len(sso_list)} SSO → CPA...")
    print(f"{'='*50}")
    success = 0
    for item in sso_list:
        email = item["email"]
        sso = item["sso"]
        print(f"\nconverting: {email}")
        for attempt in range(1, 4):
            if attempt > 1:
                print(f"  [RETRY] switching IP and retrying ({attempt}/3)...")
                find_clean_ip(max_attempts=5)
            result = _sso_to_cpa_direct(sso, email)
            if result:
                save_auth(email, result)
                success += 1
                break
            else:
                print(f"  [FAIL] attempt {attempt}/3 failed")
            time.sleep(2)
    print(f"\nconversion done: {success}/{len(sso_list)} succeeded")
    return success
    """运行 sso_to_cpa.py --all 转换所有 SSO → CPA"""
    print(f"\n{'='*50}")
    print(f"[STEP 2/3] SSO → CPA conversion...")
    print(f"{'='*50}")

    try:
        result = subprocess.run(
            [sys.executable, SSO_TO_CPA, "--all"],
            cwd=SCRIPT_DIR,
            capture_output=False,
            text=True,
            timeout=300,
        )
        return result.returncode == 0
    except subprocess.TimeoutExpired:
        print("[AUTO] conversion timed out")
        return False
    except Exception as e:
        print(f"[AUTO] conversion exception: {e}")
        return False


def _grok2api_login():
    """登录 grok2api 管理后台，返回 access_token 或 None。"""
    url = f"{GROK2API_BASE}/api/admin/v1/auth/login"
    try:
        r = _requests.post(url, json={"username": GROK2API_USER, "password": GROK2API_PASS}, timeout=15)
        if r.status_code == 200:
            token = r.json().get("data", {}).get("tokens", {}).get("accessToken")
            if token:
                return token
        print(f"  [G2A] login failed: {r.status_code} {r.text[:120]}")
    except Exception as e:
        print(f"  [G2A] login exception: {e}")
    return None


def push_to_grok2api(email_list):
    """把 email_list 中的 xai-*.json 上传到 grok2api Build 账号池。
    返回 (created, updated, skipped) 或 (0,0,0) on failure。"""
    if not email_list:
        return 0, 0, 0
    # 找到对应文件
    files_to_upload = []
    for email in email_list:
        safe = email.replace("@", "_").replace(".", "_")
        path = os.path.join(AUTH_DIR, f"xai-{safe}.json")
        if os.path.exists(path):
            files_to_upload.append(path)
        else:
            print(f"  [G2A] file missing, skipping: {path}")

    if not files_to_upload:
        print("  [G2A] no files to upload")
        return 0, 0, 0

    token = _grok2api_login()
    if not token:
        print("  [G2A] cannot get admin token, skipping upload")
        return 0, 0, 0

    url = f"{GROK2API_BASE}/api/admin/v1/accounts/import"
    headers = {"Authorization": f"Bearer {token}", "Accept": "text/event-stream"}

    created = updated = skipped = 0
    for path in files_to_upload:
        fname = os.path.basename(path)
        try:
            with open(path, "rb") as fh:
                r = _requests.post(url, headers=headers,
                                   files={"file": (fname, fh, "application/json")},
                                   stream=True, timeout=30)
            if r.status_code not in (200, 201):
                print(f"  [G2A] upload {fname} failed: HTTP {r.status_code} {r.text[:120]}")
                continue
            # 解析 SSE，找 event: complete
            for line in r.iter_lines():
                if not line:
                    continue
                text = line.decode("utf-8", errors="replace")
                if text.startswith("data:"):
                    import json as _json
                    try:
                        d = _json.loads(text[5:].strip())
                        created  += d.get("created",  0)
                        updated  += d.get("updated",  0)
                        skipped  += d.get("skipped",  0)
                    except Exception:
                        pass
            print(f"  [G2A] {fname} uploaded OK")
        except Exception as e:
            print(f"  [G2A] upload {fname} exception: {e}")

    print(f"\n[G2A] import result: created={created} updated={updated} skipped={skipped}")
    return created, updated, skipped


def _assign_web_egress(node_id=1):
    """SQL 直接分配未绑定 egress 的 Web 账号到指定节点。"""
    import sqlite3 as _sqlite3
    db = os.path.join(os.path.dirname(GROK2API_BASE.replace("http://", "")), "grok2api", "data", "backend.db")
    if not os.path.exists(db):
        db = r"D:\grok2api\data\backend.db"
    try:
        conn = _sqlite3.connect(db)
        cur = conn.cursor()
        cur.execute(
            "UPDATE provider_accounts SET egress_node_id = ?, egress_assignment_mode = 'manual' "
            "WHERE provider = 'grok_web' AND (egress_node_id IS NULL OR egress_node_id != ?)",
            (node_id, node_id))
        n = cur.rowcount
        conn.commit()
        conn.close()
        if n > 0:
            print(f"  [G2A-Web] bound {n} Web accounts to egress node {node_id}")
        return n
    except Exception as e:
        print(f"  [G2A-Web] egress assignment failed: {e}")
        return 0


def push_web_to_grok2api(sso_list):
    """把 sso_list (dict列表: {sso, email}) 推入 grok2api Web 账号池。
    返回 (created, updated, skipped) 或 (0,0,0) on failure。"""
    if not sso_list:
        return 0, 0, 0

    token = _grok2api_login()
    if not token:
        print("  [G2A-Web] cannot get admin token, skipping upload")
        return 0, 0, 0

    url = f"{GROK2API_BASE}/api/admin/v1/accounts/web/import"
    headers = {"Authorization": f"Bearer {token}", "Accept": "text/event-stream"}

    # 逐个导入 (2026-08-07: 批量 5 个一文件实测只成功 1/3, 逐个导入 100% 成功)
    created = updated = skipped = 0
    for item in sso_list:
        content = item["sso"].encode("utf-8")
        ok = False
        for attempt in (1, 2):
            try:
                r = _requests.post(url, headers=headers,
                                   files={"file": (f"sso.txt", content, "text/plain")},
                                   stream=True, timeout=60)
                if r.status_code not in (200, 201):
                    print(f"  [G2A-Web] {item['email'][:25]} upload failed: HTTP {r.status_code} {r.text[:120]}" +
                          ("" if attempt == 2 else ", retrying..."))
                    continue
                for line in r.iter_lines():
                    if not line:
                        continue
                    text = line.decode("utf-8", errors="replace")
                    if text.startswith("data:"):
                        try:
                            d = json.loads(text[5:].strip())
                            created += d.get("created", 0)
                            updated += d.get("updated", 0)
                            skipped += d.get("skipped", 0)
                        except Exception:
                            pass
                ok = True
                print(f"  [G2A-Web] {item['email'][:25]} uploaded OK")
                break
            except Exception as e:
                print(f"  [G2A-Web] {item['email'][:25]} upload exception: {e}" +
                      ("" if attempt == 2 else ", retrying..."))
        if not ok:
            skipped += 1

    # 分配 egress
    if created > 0:
        _assign_web_egress(node_id=1)

    print(f"\n[G2A-Web] import result: created={created} updated={updated} skipped={skipped}")
    return created, updated, skipped


def run_token_refresh():
    """运行 token_daemon.py 检查并刷新所有 token"""
    print(f"\n{'='*50}")
    print(f"[STEP 3/3] Token refresh check...")
    print(f"{'='*50}")

    try:
        result = subprocess.run(
            [sys.executable, TOKEN_DAEMON],
            cwd=SCRIPT_DIR,
            capture_output=False,
            text=True,
            timeout=120,
        )
        return result.returncode == 0
    except subprocess.TimeoutExpired:
        print("[AUTO] refresh timed out")
        return False
    except Exception as e:
        print(f"[AUTO] refresh exception: {e}")
        return False


def count_web_available():
    """统计 grok2api Web 池可用账号数"""
    import sqlite3 as _sq
    db = r"D:\grok2api\data\backend.db"
    if not os.path.exists(db):
        return 0, []
    try:
        conn = _sq.connect(db)
        cur = conn.cursor()
        cur.execute(
            "SELECT p.email FROM provider_accounts p "
            "JOIN account_credentials c ON c.account_id = p.id "
            "WHERE p.provider = 'grok_web' AND p.auth_status = 'active' AND c.refresh_permanent = 0 "
            "AND p.egress_node_id IS NOT NULL")
        emails = [r[0] for r in cur.fetchall()]
        conn.close()
        return len(emails), emails
    except Exception as e:
        print(f"  [WEB-COUNT] {e}")
        return 0, []


def replenish(min_accounts=2, rotate_region=False, use_free=False):
    """一次完整补位周期"""
    count, emails = count_available()
    web_count, web_emails = count_web_available()
    print(f"\n[{time.strftime('%H:%M:%S')}] Build pool: {count} | Web pool: {web_count}")
    if emails:
        print(f"  Build: {', '.join(emails[:5])}{'...' if len(emails)>5 else ''}")
    if web_emails:
        print(f"  Web:   {', '.join(e[:25] for e in web_emails[:5])}{'...' if len(web_emails)>5 else ''}")

    if count >= min_accounts and web_count >= min_accounts:
        print(f"  accounts sufficient (Build {count}>={min_accounts}, Web {web_count}>={min_accounts}), no replenish needed")
        print(f"  checking token refresh...")
        run_token_refresh()
        return True

    # ── 快照当前节点 ──
    original_node = None
    if HAS_CLASH:
        try:
            original_node = snapshot()
            print(f"[IP] snapshot node: {original_node[:40]}")
        except Exception as e:
            print(f"[IP] snapshot failed: {e}")

    shortage = max(min_accounts - count, min_accounts - web_count)
    to_register = min(shortage + 1, 5)
    print(f"  not enough accounts ({count}<{min_accounts}), need {shortage} (will register {to_register})")

    try:
        # Step 1: 注册 + 收集 SSO
        reg_ok, sso_list = run_registration(to_register, rotate_region=rotate_region, use_free=use_free)
        if not reg_ok:
            print("[AUTO] registration failed, skipping later steps")
            return False

        time.sleep(3)

        # Step 2: 推入 grok2api Web 池 (SSO 直推, 不依赖 OAuth PKCE)
        if sso_list:
            print(f"\n{'='*50}")
            print(f"[STEP 2/5] pushing {len(sso_list)} accounts to grok2api Web pool...")
            print(f"{'='*50}")
            push_web_to_grok2api(sso_list)

        # Step 3: 尝试 OAuth PKCE 转换 Build 池（可能失败, 不影响 Web 池）
        if sso_list:
            convert_sso_list(sso_list)

        # Step 4: Token 刷新
        time.sleep(2)
        run_token_refresh()

        # Step 5: 推入 grok2api Build 账号池（依赖 OAuth 转换, Web 池已先行）
        if sso_list:
            print(f"\n{'='*50}")
            print(f"[STEP 5/5] pushing {len(sso_list)} accounts to grok2api Build pool...")
            print(f"{'='*50}")
            push_to_grok2api([item["email"] for item in sso_list])

        # 最终验证
        time.sleep(2)
        new_count, new_emails = count_available()
        new_web_count, new_web_emails = count_web_available()
        print(f"\n{'='*50}")
        print(f"replenish done. Build pool: {count} → {new_count} | Web pool: {new_web_count}")
        for e in new_emails:
            print(f"  Build: {e}")
        for e in new_web_emails:
            print(f"  Web:   {e}")

        if new_count >= min_accounts:
            print(f"[AUTO] replenish OK")
            return True
        else:
            print(f"[AUTO] replenish insufficient ({new_count}<{min_accounts}), may need to run again")
            return False

    finally:
        # ── 恢复原始节点 ──
        if original_node and HAS_CLASH:
            try:
                restore(original_node)
            except Exception as e:
                print(f"[IP] failed to restore node: {e}")


def main():
    parser = argparse.ArgumentParser(description="Grok 账号自动补位系统")
    parser.add_argument("--min", type=int, default=2, help="最少可用账号数（默认 2）")
    parser.add_argument("--daemon", type=int, default=0, help="守护模式，每 N 秒检查一次")
    parser.add_argument("--check", action="store_true", help="仅检查状态")
    parser.add_argument("--rotate-region", action="store_true", help="跨区域切换 IP（而非随机节点）")
    parser.add_argument("--refresh-only", action="store_true", help="仅刷新 token，不注册")
    parser.add_argument("--free", action="store_true", help="使用免费 DrissionPage 注册（默认用 YesCaptcha）")
    args = parser.parse_args()

    if args.check:
        count, emails = count_available()
        web_count, web_emails = count_web_available()
        print(f"=== Grok account status ===")
        print(f"Build pool: {count} available")
        for e in emails:
            print(f"  ✅ {e}")
        print(f"Web pool:   {web_count} available")
        for e in web_emails:
            print(f"  ✅ {e}")

        if HAS_CLASH:
            try:
                h = clash_health()
                if h["ok"]:
                    print(f"\nClash proxy: OK")
                    print(f"  current node: {h['current_node'][:50]}")
                    print(f"  egress IP: {h['current_ip']}")
                    print(f"  region: {h['region']}")
                else:
                    print(f"\nClash proxy: DOWN {h.get('error','?')}")
            except Exception as e:
                print(f"\nClash proxy: DOWN {e}")
        return

    if args.refresh_only:
        run_token_refresh()
        return

    if args.daemon:
        print(f"[DAEMON] checking every {args.daemon}s | keeping at least {args.min} accounts | "
              f"region rotation={'on' if args.rotate_region else 'off'}")
        while True:
            try:
                replenish(args.min, rotate_region=args.rotate_region, use_free=args.free)
            except KeyboardInterrupt:
                print("\n[DAEMON] exiting")
                break
            except Exception as e:
                print(f"[DAEMON] error: {e}")
                import traceback
                traceback.print_exc()
            time.sleep(args.daemon)
    else:
        replenish(args.min, rotate_region=args.rotate_region, use_free=args.free)


if __name__ == "__main__":
    main()
