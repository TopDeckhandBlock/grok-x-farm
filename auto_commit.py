#!/usr/bin/env python3
"""auto_commit.py — авто-коммит изменений в GitHub-репу по кулдауну.

Зачем: локальный `git push` сломан (нет git-remote-https), поэтому:
  - локальный git отслеживает изменения (add/diff/commit)
  - пуш идёт через GitHub REST API (blob -> tree -> commit -> ref update)

Запуск:
  python auto_commit.py              # однократно: закоммитить+запушить если есть изменения
  python auto_commit.py --loop       # по кулдауну (каждые COOLDOWN сек), стоп = файл STOP_AUTOCOMMIT
  python auto_commit.py --interval 120   # свой кулдаун (сек)

Безопасность: файлы-секреты (g2a_key, SECRETS, .env, *pat*.json, imported_sso,
farm.secrets) НИКОГДА не коммитятся — двойная проверка поверх .gitignore.
"""
import argparse, base64, json, os, re, subprocess, sys, time, urllib.request, urllib.error

HERE = os.path.dirname(os.path.abspath(__file__))

# --- конфиг (можно переопределить env) ---
OWNER = os.getenv("AC_OWNER", "MeshFinancial")
REPO = os.getenv("AC_REPO", "grok-x-farm")
BRANCH = os.getenv("AC_BRANCH", "main")
COOLDOWN = int(os.getenv("AC_COOLDOWN", "300"))   # сек между циклами в --loop
PAT_SOURCES = [
    os.path.join(HERE, "..", "meshfin_pat.json"),  # рядом с репой
    os.path.join(HERE, "meshfin_pat.json"),
]
STOP_FILE = os.path.join(HERE, "STOP_AUTOCOMMIT")

# паттерны секретов — никогда не коммитить (даже если .gitignore промахнулся)
SECRET_PATTERNS = re.compile(
    r"(g2a_key|secrets?\.local|\.env$|meshfin_pat|.*pat.*\.json$|imported_sso|"
    r"farm\.secrets|accounts.*\.txt$|.*\.session$|credential)", re.I)


def get_pat():
    """PAT из env GITHUB_PAT или из файла meshfin_pat.json."""
    env = os.getenv("GITHUB_PAT") or os.getenv("GH_PAT")
    if env:
        return env.strip()
    for p in PAT_SOURCES:
        p = os.path.abspath(p)
        if os.path.exists(p):
            try:
                data = json.load(open(p, encoding="utf-8"))
                if isinstance(data, list) and data:
                    return data[0].get("pat", "").strip()
                if isinstance(data, dict):
                    return (data.get("pat") or data.get("token") or "").strip()
            except Exception:
                pass
    sys.exit("[!] no PAT: set GITHUB_PAT env or put meshfin_pat.json next to repo")


PAT = get_pat()
API = f"https://api.github.com/repos/{OWNER}/{REPO}"
HDRS = {"Authorization": f"token {PAT}", "User-Agent": "auto-commit",
        "Accept": "application/vnd.github+json"}


def gh(method, path, data=None, full_url=False):
    url = path if full_url else f"https://api.github.com{path}"
    req = urllib.request.Request(url, data=data, method=method, headers=HDRS)
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            return json.loads(r.read())
    except urllib.error.HTTPError as e:
        body = e.read().decode()[:300]
        print(f"[!] GitHub API {method} {path}: HTTP {e.code} {body}")
        return None


def git(*args):
    r = subprocess.run(["git", *args], cwd=HERE, capture_output=True, text=True,
                       timeout=60, encoding="utf-8", errors="replace")
    return r.returncode, (r.stdout or "").strip(), (r.stderr or "").strip()


def is_secret(path):
    return bool(SECRET_PATTERNS.search(path))


def stage_and_diff():
    """git add -A (уважает .gitignore), вернуть [(status, path)] изменённых файлов.
    status: A/M/D. Секреты фильтруются."""
    git("add", "-A")
    rc, out, _ = git("diff", "--cached", "--name-status")
    if rc != 0 or not out:
        return []
    changes = []
    for line in out.splitlines():
        parts = line.split("\t")
        if len(parts) < 2:
            continue
        status, path = parts[0][0], parts[-1]   # A/M/D/Rxx -> первая буква
        if is_secret(path):
            print(f"[skip-secret] {path}")
            git("reset", "-q", "--", path)       # снять с индекса
            continue
        changes.append((status, path))
    return changes


def push_changes(changes, message):
    """Запушить изменённые файлы через GitHub API поверх remote HEAD."""
    ref = gh("GET", f"/repos/{OWNER}/{REPO}/git/ref/heads/{BRANCH}")
    if not ref:
        return False
    base_sha = ref["object"]["sha"]
    base_commit = gh("GET", f"/repos/{OWNER}/{REPO}/git/commits/{base_sha}")
    if not base_commit:
        return False
    base_tree = base_commit["tree"]["sha"]

    has_delete = any(s == "D" for s, _ in changes)
    tree_entries = []

    if has_delete:
        # удаление: строим ПОЛНОЕ дерево без base_tree (иначе файл не удалить)
        rc, files, _ = git("ls-files")
        all_files = [f for f in files.splitlines() if f.strip() and not is_secret(f)]
        for path in all_files:
            full = os.path.join(HERE, path)
            if not os.path.exists(full):
                continue
            with open(full, "rb") as fh:
                content = fh.read()
            blob = gh("POST", f"/repos/{OWNER}/{REPO}/git/blobs",
                      json.dumps({"content": base64.b64encode(content).decode(),
                                  "encoding": "base64"}).encode())
            if not blob:
                return False
            tree_entries.append({"path": path.replace("\\", "/"), "mode": "100644",
                                 "type": "blob", "sha": blob["sha"]})
        new_tree = gh("POST", f"/repos/{OWNER}/{REPO}/git/trees",
                      json.dumps({"tree": tree_entries}).encode())
    else:
        # только add/modify: base_tree + изменённые файлы (быстро)
        for status, path in changes:
            full = os.path.join(HERE, path)
            if not os.path.exists(full):
                continue
            with open(full, "rb") as fh:
                content = fh.read()
            blob = gh("POST", f"/repos/{OWNER}/{REPO}/git/blobs",
                      json.dumps({"content": base64.b64encode(content).decode(),
                                  "encoding": "base64"}).encode())
            if not blob:
                return False
            tree_entries.append({"path": path.replace("\\", "/"), "mode": "100644",
                                 "type": "blob", "sha": blob["sha"]})
        new_tree = gh("POST", f"/repos/{OWNER}/{REPO}/git/trees",
                      json.dumps({"base_tree": base_tree, "tree": tree_entries}).encode())

    if not new_tree:
        return False
    commit = gh("POST", f"/repos/{OWNER}/{REPO}/git/commits",
                json.dumps({"message": message, "tree": new_tree["sha"],
                            "parents": [base_sha]}).encode())
    if not commit:
        return False
    upd = gh("PATCH", f"/repos/{OWNER}/{REPO}/git/refs/heads/{BRANCH}",
             json.dumps({"sha": commit["sha"], "force": False}).encode())
    if upd:
        print(f"[push] {commit['sha'][:8]} -> {OWNER}/{REPO}@{BRANCH}")
        return True
    return False


def commit_once(message=None):
    """Один цикл: stage -> diff -> push -> local commit. True если что-то запушено."""
    changes = stage_and_diff()
    if not changes:
        return False
    names = ", ".join(p for _, p in changes[:6])
    if len(changes) > 6:
        names += f" (+{len(changes)-6})"
    msg = message or f"chore(auto): {len(changes)} file(s) — {names}"
    print(f"[commit] {len(changes)} changed: {names}")
    ok = push_changes(changes, msg)
    # локальный коммит чтоб очистить индекс (SHA разойдётся с remote — ок, пуш всегда из файлов)
    git("commit", "-q", "-m", msg)
    return ok


def main():
    ap = argparse.ArgumentParser(description="auto-commit to GitHub via API (git push broken)")
    ap.add_argument("--loop", action="store_true", help="по кулдауну, стоп = STOP_AUTOCOMMIT")
    ap.add_argument("--interval", type=int, default=COOLDOWN, help=f"кулдаун сек (default {COOLDOWN})")
    ap.add_argument("--message", "-m", help="свой текст коммита (для однократного)")
    a = ap.parse_args()

    if not a.loop:
        done = commit_once(a.message)
        print("[done] pushed" if done else "[done] nothing to commit")
        return

    print(f"[loop] auto-commit every {a.interval}s -> {OWNER}/{REPO}@{BRANCH}")
    print(f"[loop] stop: create {os.path.basename(STOP_FILE)} or Ctrl+C")
    cycle = 0
    try:
        while True:
            if os.path.exists(STOP_FILE):
                print("[loop] STOP_AUTOCOMMIT found, exiting")
                os.remove(STOP_FILE)
                break
            cycle += 1
            try:
                if commit_once():
                    print(f"[loop] cycle {cycle}: pushed")
            except Exception as e:
                print(f"[loop] cycle {cycle} error: {e}")
            time.sleep(a.interval)
    except KeyboardInterrupt:
        print("\n[loop] interrupted, bye")


if __name__ == "__main__":
    main()
