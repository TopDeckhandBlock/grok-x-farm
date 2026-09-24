"""Push a local file to a GitHub repo via API (git-remote-https broken locally).
Usage: python push_to_repo.py <owner> <repo> <branch> <repo_path> <local_path> <message>
"""
import sys, json, base64, time, urllib.request, urllib.error

def _get_pat():
    """Read GitHub PAT from GITHUB_PAT env, or meshfin_pat.json, or gh CLI token."""
    import os as _os
    tok = _os.getenv("GITHUB_PAT", "")
    if tok:
        return tok
    pat_file = _os.path.join(_os.path.dirname(_os.path.abspath(__file__)), "meshfin_pat.json")
    if _os.path.exists(pat_file):
        tok = json.loads(open(pat_file).read()).get("pat", "")
        if tok:
            return tok
    # fallback: gh CLI
    try:
        import subprocess as _sp
        r = _sp.run(["gh", "auth", "token"], capture_output=True, text=True, timeout=5)
        if r.returncode == 0 and r.stdout.strip():
            return r.stdout.strip()
    except Exception:
        pass
    sys.exit("[!] set GITHUB_PAT env, or gh auth login, or meshfin_pat.json next to repo")

TOKEN = _get_pat()
H = {"Authorization": f"token {TOKEN}", "User-Agent": "farm", "Accept": "application/vnd.github+json"}

def gh(method, path, data=None, retries=5):
    for a in range(retries):
        try:
            req = urllib.request.Request(f"https://api.github.com{path}", data=data, method=method, headers=H)
            with urllib.request.urlopen(req, timeout=45) as r:
                return json.loads(r.read())
        except Exception as e:
            if a < retries - 1:
                time.sleep(3); continue
            print(f"FAIL {method} {path}: {type(e).__name__} {e}")
            return None

def main():
    owner, repo, branch, repo_path, local_path, message = sys.argv[1:7]
    with open(local_path, "rb") as f:
        b64 = base64.b64encode(f.read()).decode()
    ref = gh("GET", f"/repos/{owner}/{repo}/git/ref/heads/{branch}")
    if not ref:
        sys.exit("no ref")
    base_sha = ref["object"]["sha"]
    base_tree = gh("GET", f"/repos/{owner}/{repo}/git/commits/{base_sha}")["tree"]["sha"]
    blob = gh("POST", f"/repos/{owner}/{repo}/git/blobs",
              json.dumps({"content": b64, "encoding": "base64"}).encode())
    if not blob:
        sys.exit("blob fail")
    tree = gh("POST", f"/repos/{owner}/{repo}/git/trees",
              json.dumps({"base_tree": base_tree,
                          "tree": [{"path": repo_path, "mode": "100644", "type": "blob", "sha": blob["sha"]}]}).encode())
    if not tree:
        sys.exit("tree fail")
    commit = gh("POST", f"/repos/{owner}/{repo}/git/commits",
                json.dumps({"message": message, "tree": tree["sha"], "parents": [base_sha]}).encode())
    if not commit:
        sys.exit("commit fail")
    upd = gh("PATCH", f"/repos/{owner}/{repo}/git/refs/heads/{branch}",
             json.dumps({"sha": commit["sha"], "force": False}).encode())
    if upd:
        print(f"PUSHED {owner}/{repo}/{repo_path}: {commit['sha'][:10]}")
        print(f"https://github.com/{owner}/{repo}/commit/{commit['sha']}")
    else:
        sys.exit("ref update fail")

if __name__ == "__main__":
    main()
