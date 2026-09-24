#!/usr/bin/env python3
"""panel.py — local control panel for grok-x-farm. Stdlib only, no deps.

Run:  python dashboard/panel.py   →  http://127.0.0.1:8010  (localhost only)

What it does:
  - live stack status (runs `farm.py doctor` on demand / auto-refresh)
  - start/stop the grok2api gateway
  - run reg / crawl as background tasks with live log tail
  - edit farm.config.json (email provider, captcha mode, proxy, parser limits)
  - store secrets in farm.secrets.json (gitignored, masked in UI, injected
    into child-process env)

Security: binds 127.0.0.1 only; secret values never returned to the browser;
config updates go through an editable-key whitelist with type/range checks;
all user paths are sandboxed to the repo dir.
"""
import json
import os
import re
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
FARM_DIR = os.path.dirname(HERE)
CONFIG_PATH = os.path.join(FARM_DIR, "farm.config.json")
EXAMPLE_PATH = os.path.join(FARM_DIR, "config", "farm.config.example.json")
SECRETS_PATH = os.path.join(FARM_DIR, "farm.secrets.json")
LOG_DIR = os.path.join(HERE, "logs")
GATEWAY_HEALTHZ = "http://127.0.0.1:8000/healthz"
GATEWAY_EXE_CANDIDATES = [
    r"C:\Users\User\grok2api-deploy\grok2api\grok2api.exe",
    os.path.join(FARM_DIR, "grok2api", "grok2api.exe"),
]

SECRET_KEYS = ("G2A_KEY", "G2A_ADMIN_PASS", "YESCAPTCHA_KEY", "CAPSOLVER_KEY", "NOPETCHA_KEY",
               "TWOCAPTCHA_KEY", "GMAIL_APP_PASSWORD",
               "LUCKMAIL_API_KEY", "LUCKMAIL_API_SECRET", "MAILNEST_API_KEY", "FCE_API_KEY")

ENUM_KEYS = {
    "email.provider": {"tmail", "luckmail", "mailnest", "fce", "gptmail", "gmail", "outlook", "imap", "1secmail", "tempmail-lol"},
    "captcha.mode": {"free_browser", "yescaptcha", "capsolver", "nopecha", "2captcha"},
    "proxy.mode": {"direct", "single", "pool"},
}
INT_KEYS = {"parser.posts_per_query", "parser.days_window", "parser.verify_sample_size",
            "captcha.fallback_after_n_failures", "captcha.solver_timeout_sec", "captcha.retry_per_solver",
            "farm.count_per_run", "farm.threads",
            "gateway.rpm_limit", "gateway.max_concurrent"}
FLOAT_KEYS = {"parser.delay_between_queries_sec"}
STR_KEYS = {"proxy.single", "email.gmail.base_email", "email.outlook.accounts_file",
            "captcha.turnstile_sitekey",
            "gateway.parse_model", "gateway.tool_model", "gateway.reasoning_model",
            "gateway.exe_path"}
LIST_KEYS = {"proxy.geo_whitelist"}
INT_RANGES = {"parser.posts_per_query": (1, 100), "parser.days_window": (0, 365),
              "parser.verify_sample_size": (0, 100), "captcha.fallback_after_n_failures": (1, 100),
              "captcha.solver_timeout_sec": (10, 600), "captcha.retry_per_solver": (1, 10),
              "farm.count_per_run": (1, 200), "farm.threads": (1, 4),
              "gateway.rpm_limit": (1, 100000), "gateway.max_concurrent": (1, 64)}
FLOAT_RANGES = {"parser.delay_between_queries_sec": (0.0, 300.0)}
EDITABLE = set(ENUM_KEYS) | INT_KEYS | FLOAT_KEYS | STR_KEYS | LIST_KEYS

# ---------------- helpers (unit-tested in tests/test_panel_offline.py) ----------------

def mask_secret(v):
    """Mask a secret for display: short → dots, long → first3…last2."""
    if not v:
        return ""
    v = str(v)
    if len(v) <= 6:
        return "•" * len(v)
    return v[:3] + "…" + v[-2:]

def redact_creds(v):
    """Redact credentials inside a URL or an email, keep the rest visible."""
    if not v:
        return ""
    s = str(v)
    s = re.sub(r"://[^@/\s]+@", "://***@", s)
    if "@" in s and "://" not in s:
        local, _, dom = s.partition("@")
        s = (local[:1] + "***@" + dom) if local else s
    return s

def load_config(path=None):
    p = path or (CONFIG_PATH if os.path.exists(CONFIG_PATH) else EXAMPLE_PATH)
    with open(p, encoding="utf-8") as f:
        return json.load(f)

def save_config(cfg, path=None):
    p = path or CONFIG_PATH
    tmp = p + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)
    os.replace(tmp, p)

def apply_updates(cfg, updates):
    """All-or-nothing whitelist update of cfg by dotted keys. Returns error list."""
    errors = []
    pending = {}
    for key, val in (updates or {}).items():
        if key not in EDITABLE:
            errors.append(f"{key}: not an editable key")
            continue
        if key in ENUM_KEYS:
            if val not in ENUM_KEYS[key]:
                errors.append(f"{key}: must be one of {sorted(ENUM_KEYS[key])}")
            else:
                pending[key] = val
        elif key in INT_KEYS:
            if isinstance(val, bool) or not isinstance(val, int):
                errors.append(f"{key}: must be an integer")
            elif not (INT_RANGES[key][0] <= val <= INT_RANGES[key][1]):
                errors.append(f"{key}: out of range {INT_RANGES[key]}")
            else:
                pending[key] = val
        elif key in FLOAT_KEYS:
            if isinstance(val, bool) or not isinstance(val, (int, float)):
                errors.append(f"{key}: must be a number")
            elif not (FLOAT_RANGES[key][0] <= float(val) <= FLOAT_RANGES[key][1]):
                errors.append(f"{key}: out of range {FLOAT_RANGES[key]}")
            else:
                pending[key] = float(val)
        elif key in STR_KEYS:
            if not isinstance(val, str):
                errors.append(f"{key}: must be a string")
            else:
                pending[key] = val
        elif key in LIST_KEYS:
            if not isinstance(val, list) or len(val) > 10 or \
                    not all(isinstance(x, str) and 1 <= len(x) <= 3 for x in val):
                errors.append(f"{key}: must be a list of short country codes")
            else:
                pending[key] = val
    if errors:
        return errors
    for key, val in pending.items():
        node = cfg
        parts = key.split(".")
        for p in parts[:-1]:
            node = node.setdefault(p, {})
            if not isinstance(node, dict):
                return [f"{key}: config shape broken"]
        node[parts[-1]] = val
    return []

def safe_path(base, rel):
    """Sandbox a user-supplied relative path inside base. Returns normalized rel or None."""
    if not rel or not isinstance(rel, str) or os.path.isabs(rel):
        return None
    if re.match(r"^[A-Za-z]:", rel):  # windows drive letter
        return None
    norm = os.path.normpath(rel)
    if norm == ".." or norm.startswith(".." + os.sep) or os.path.isabs(norm):
        return None
    full = os.path.normpath(os.path.join(base, norm))
    if not full.startswith(os.path.normpath(base) + os.sep):
        return None
    return norm

def public_config(cfg, secrets):
    """Config safe to send to the browser: credentials redacted, secrets as booleans."""
    pub = json.loads(json.dumps(cfg, ensure_ascii=False))
    if pub.get("proxy", {}).get("single"):
        pub["proxy"]["single"] = redact_creds(pub["proxy"]["single"])
    gmail = pub.get("email", {}).get("gmail")
    if isinstance(gmail, dict) and gmail.get("base_email"):
        gmail["base_email"] = redact_creds(gmail["base_email"])
    pub["secrets"] = {k: bool(secrets.get(k)) for k in SECRET_KEYS}
    return pub

def build_task_argv(task, params):
    """Whitelisted task → farm.py argv. Raises ValueError on anything else."""
    params = params or {}
    py = sys.executable
    if task == "reg":
        count = params.get("count")
        if isinstance(count, bool) or not isinstance(count, int) or not (1 <= count <= 20):
            raise ValueError("reg: count must be an integer 1..20")
        return [py, "farm.py", "reg", "--count", str(count)]
    if task == "crawl":
        qf = safe_path(FARM_DIR, params.get("queries_file"))
        if not qf:
            raise ValueError("crawl: queries_file must be a path inside the repo")
        target = params.get("target")
        if isinstance(target, bool) or not isinstance(target, int) or not (1 <= target <= 10000):
            raise ValueError("crawl: target must be an integer 1..10000")
        argv = [py, "farm.py", "crawl", "--queries-file", qf, "--target", str(target)]
        if params.get("days") is not None:
            days = params["days"]
            if isinstance(days, bool) or not isinstance(days, int) or not (0 <= days <= 365):
                raise ValueError("crawl: days must be an integer 0..365")
            argv += ["--days", str(days)]
        if params.get("state"):
            st = safe_path(FARM_DIR, params["state"])
            if not st:
                raise ValueError("crawl: state must be a path inside the repo")
            argv += ["--state", st]
        if params.get("out"):
            out = safe_path(FARM_DIR, params["out"])
            if not out:
                raise ValueError("crawl: out must be a path inside the repo")
            argv += ["--out", out]
        return argv
    raise ValueError(f"unknown task: {task}")

# ---------------- runtime ----------------

TASKS = {}          # name -> {"p": Popen, "log": path}
TASK_LOCK = threading.Lock()

def load_secrets_dict():
    if os.path.exists(SECRETS_PATH):
        try:
            with open(SECRETS_PATH, encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, dict):
                return {k: v for k, v in data.items() if isinstance(v, str)}
        except (OSError, json.JSONDecodeError):
            pass
    return {}

def child_env():
    env = dict(os.environ)
    env["PYTHONIOENCODING"] = "utf-8"   # children print ✓/→ etc; cp1252 console kills them
    env["PYTHONUTF8"] = "1"
    for k, v in load_secrets_dict().items():
        if v:
            env[k] = v
    return env

def gateway_running():
    try:
        with urllib.request.urlopen(GATEWAY_HEALTHZ, timeout=3) as r:
            return r.status == 200
    except Exception:
        return False

def gateway_exe(cfg):
    for cand in (cfg.get("gateway", {}).get("exe_path", ""), os.getenv("G2A_EXE", "")):
        if cand and os.path.exists(cand):
            return cand
    for cand in GATEWAY_EXE_CANDIDATES:
        if os.path.exists(cand):
            return cand
    return None

def gateway_start():
    if gateway_running():
        return {"ok": False, "error": "gateway already running (healthz 200)"}
    cfg = load_config()
    exe = gateway_exe(cfg)
    if not exe:
        return {"ok": False, "error": "grok2api executable not found; set gateway.exe_path in config"}
    os.makedirs(LOG_DIR, exist_ok=True)
    logf = open(os.path.join(LOG_DIR, "gateway.log"), "ab")
    flags = 0x00000008 | 0x00000200 if os.name == "nt" else 0  # DETACHED | NEW_PROCESS_GROUP
    subprocess.Popen([exe], cwd=os.path.dirname(exe), stdout=logf,
                     stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
                     creationflags=flags)
    for _ in range(15):
        time.sleep(1)
        if gateway_running():
            return {"ok": True, "detail": f"started {exe}, healthz 200"}
    return {"ok": False, "error": "started but healthz not up in 15s; see logs/gateway.log"}

def gateway_stop():
    if not gateway_running():
        return {"ok": False, "error": "gateway is not running"}
    if os.name == "nt":
        out = subprocess.run(["netstat", "-ano", "-p", "tcp"], capture_output=True,
                             text=True, timeout=15).stdout
        pids = {ln.split()[-1] for ln in out.splitlines()
                if ":8000" in ln and "LISTENING" in ln and ln.split()[-1].isdigit()}
        for pid in pids:
            subprocess.run(["taskkill", "/PID", pid, "/F"], capture_output=True, timeout=15)
        stopped = bool(pids)
    else:
        stopped = subprocess.run(["pkill", "-f", "[g]rok2api"], capture_output=True).returncode == 0
    time.sleep(1)
    return {"ok": stopped and not gateway_running(),
            "error": None if stopped else "could not find gateway process"}

def run_status():
    try:
        r = subprocess.run([sys.executable, "farm.py", "doctor"], cwd=FARM_DIR,
                           capture_output=True, text=True, timeout=150, env=child_env(),
                           encoding="utf-8", errors="replace")
        out = (r.stdout or "") + (r.stderr or "")
        return {"ok": "ALL OK" in out, "output": out.strip(), "exit": r.returncode}
    except subprocess.TimeoutExpired:
        return {"ok": False, "output": "doctor timed out (150s)", "exit": -1}

def task_state(name):
    with TASK_LOCK:
        t = TASKS.get(name)
        if not t:
            return {"running": False, "exit": None}
        code = t["p"].poll()
        return {"running": code is None, "exit": code}

def start_task(name, argv):
    with TASK_LOCK:
        t = TASKS.get(name)
        if t and t["p"].poll() is None:
            return {"ok": False, "error": f"{name} is already running"}
        os.makedirs(LOG_DIR, exist_ok=True)
        logp = os.path.join(LOG_DIR, f"{name}.log")
        logf = open(logp, "wb")
        p = subprocess.Popen(argv, cwd=FARM_DIR, stdout=logf, stderr=subprocess.STDOUT,
                             stdin=subprocess.DEVNULL, env=child_env(),
                             creationflags=0x08000000 if os.name == "nt" else 0)  # NO_WINDOW
        TASKS[name] = {"p": p, "log": logp}
    return {"ok": True, "log": name}

def read_log(name, offset):
    with TASK_LOCK:
        t = TASKS.get(name)
    if not t:
        return {"text": "", "offset": 0, "running": False, "exit": None}
    try:
        size = os.path.getsize(t["log"])
        with open(t["log"], "rb") as f:
            f.seek(min(offset, size))
            chunk = f.read(200_000)
        return {"text": chunk.decode("utf-8", errors="replace"),
                "offset": min(offset, size) + len(chunk),
                **task_state(name)}
    except OSError:
        return {"text": "", "offset": offset, **task_state(name)}

def save_secrets(updates):
    data = load_secrets_dict()
    for k, v in (updates or {}).items():
        if k not in SECRET_KEYS or not isinstance(v, str):
            continue
        if v:
            data[k] = v
        else:
            data.pop(k, None)
    tmp = SECRETS_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=1)
    os.replace(tmp, SECRETS_PATH)
    try:
        os.chmod(SECRETS_PATH, 0o600)
    except OSError:
        pass
    return {k: bool(data.get(k)) for k in SECRET_KEYS}

# ---------------- HTTP ----------------

PAGE = """<!DOCTYPE html>
<html lang="ru"><head><meta charset="utf-8"><title>grok-x-farm control</title><style>
:root{--bg:#0b0d11;--surface:#12151c;--surface2:#0e1117;--line:#1f2530;--text:#e8ecf2;
--muted:#8a93a5;--dim:#5c6575;--ok:#3fb950;--warn:#e3b341;--err:#f85149;--accent:#7ee787;
--mono:'Cascadia Code',Consolas,monospace}
*{box-sizing:border-box}
body{background:var(--bg);color:var(--text);font-family:'Segoe UI',Arial,sans-serif;margin:0;padding:24px 28px}
h1{font-size:20px;margin:0 0 2px}.sub{color:var(--muted);font-size:12.5px;margin-bottom:18px;font-family:var(--mono)}
.grid{display:grid;grid-template-columns:1fr 1fr;gap:14px}
.card{background:var(--surface);border:1px solid var(--line);border-radius:8px;padding:16px;margin-bottom:14px}
.card h2{font-family:var(--mono);font-size:11px;text-transform:uppercase;letter-spacing:1.4px;color:var(--muted);margin:0 0 12px;display:flex;align-items:center;gap:8px}
.card h2::before{content:"";width:8px;height:8px;background:var(--ok);border-radius:2px}
button{background:var(--surface2);border:1px solid var(--line);color:var(--text);border-radius:6px;padding:7px 14px;font-size:13px;cursor:pointer;font-family:var(--mono)}
button:hover{border-color:var(--ok);color:var(--accent)}
button:disabled{opacity:.4;cursor:default}
button.danger:hover{border-color:var(--err);color:var(--err)}
input,select{background:var(--surface2);border:1px solid var(--line);color:var(--text);border-radius:5px;padding:6px 9px;font-size:13px;font-family:var(--mono);width:100%}
label{display:block;font-size:11px;color:var(--dim);text-transform:uppercase;letter-spacing:.8px;margin:10px 0 4px}
pre{background:#05070a;border:1px solid var(--line);border-radius:6px;padding:12px;font-family:var(--mono);font-size:12px;line-height:1.6;overflow:auto;max-height:320px;margin:8px 0 0;white-space:pre-wrap}
.pill{display:inline-block;font-family:var(--mono);font-size:11px;border-radius:20px;padding:2px 12px;border:1px solid}
.pill.ok{color:var(--ok);border-color:var(--ok)}.pill.err{color:var(--err);border-color:var(--err)}
.pill.run{color:var(--warn);border-color:var(--warn)}
.row{display:flex;gap:8px;align-items:center;margin-top:10px;flex-wrap:wrap}
.hint{color:var(--dim);font-size:11.5px;margin-top:6px}
.msg{font-family:var(--mono);font-size:12px;margin-top:8px}
.msg.ok{color:var(--ok)}.msg.err{color:var(--err)}
.two{display:grid;grid-template-columns:1fr 1fr;gap:0 12px}
</style></head><body>
<h1>GROK-X-FARM · CONTROL</h1>
<div class="sub">127.0.0.1:8010 · local only · config → farm.config.json · secrets → farm.secrets.json (gitignored)</div>

<div class="grid"><div>
<div class="card"><h2>Stack status (farm.py doctor)</h2>
<div class="row" style="margin-top:0"><button onclick="loadStatus(this)">Run doctor</button>
<label style="margin:0;display:flex;align-items:center;gap:5px;text-transform:none"><input type="checkbox" id="autoref" style="width:auto" onchange="autoRefresh()"> auto 30s</label>
<span id="statusPill"></span></div>
<pre id="statusOut">not checked yet</pre></div>

<div class="card"><h2>Gateway (grok2api :8000)</h2>
<div class="row" style="margin-top:0"><button onclick="gw('start',this)">Start</button><button class="danger" onclick="gw('stop',this)">Stop</button><span id="gwPill"></span></div>
<div class="hint">exe: gateway.exe_path в конфиге, или env G2A_EXE, или стандартные пути</div></div>

<div class="card"><h2>Run tasks</h2>
<label>reg — count (1..20)</label><div class="two"><input id="regCount" type="number" value="1" min="1" max="20"></div>
<div class="row"><button onclick="runTask('reg',{count:parseInt(document.getElementById('regCount').value)},this)">Run reg</button><span id="regPill"></span></div>
<label>crawl — queries file / target / days</label>
<div class="two"><input id="cQ" value="parser/queries.txt"><input id="cT" type="number" value="100" min="1" max="10000"></div>
<div class="two"><input id="cD" type="number" value="14" min="0" max="365" placeholder="days (0=off)"><input id="cS" value="seen.json" placeholder="state file (empty=off)"></div>
<div class="row"><button onclick="runCrawl(this)">Run crawl</button><span id="crawlPill"></span></div>
<div class="msg" id="taskMsg"></div></div>
</div><div>

<div class="card"><h2>Settings (farm.config.json)</h2>
<div class="two">
<div><label>email provider</label><select id="s_email">
<option>tmail</option><option>luckmail</option><option>mailnest</option><option>fce</option><option>gptmail</option><option>gmail</option><option>outlook</option></select></div>
<div><label>gmail base (redacted)</label><input id="s_gmail" placeholder="пусто = без изменений"></div>
<div><label>captcha mode</label><select id="s_cap"><option>free_browser</option><option>yescaptcha</option><option>capsolver</option><option>nopecha</option><option>2captcha</option></select></div>
<div><label>turnstile sitekey</label><input id="s_sitekey" placeholder="0x4AAAAAAAhr9JGVDZbrZOo0"></div>
<div><label>solver timeout sec</label><input id="s_captimeout" type="number" min="10" max="600"></div>
<div><label>retries per solver</label><input id="s_capretry" type="number" min="1" max="10"></div>
<div><label>fallback after N fails</label><input id="s_capfallback" type="number" min="1" max="100"></div>
<div><label>yescaptcha key (secret)</label><input id="s_capkey" type="password" placeholder="пусто = без изменений"></div>
<div><label>capsolver key (secret)</label><input id="s_capsolverkey" type="password" placeholder="пусто = без изменений"></div>
<div><label>nopecha key (secret)</label><input id="s_nopechakey" type="password" placeholder="пусто = без изменений"></div>
<div><label>2captcha key (secret)</label><input id="s_2captchakey" type="password" placeholder="пусто = без изменений"></div>
<div><label>proxy mode</label><select id="s_pmode"><option>direct</option><option>single</option><option>pool</option></select></div>
<div><label>proxy single (redacted)</label><input id="s_psingle" placeholder="пусто = без изменений"></div>
<div><label>geo whitelist (через запятую)</label><input id="s_geo"></div>
<div><label>parse model</label><input id="s_pmodel"></div>
<div><label>tool model</label><input id="s_toolmodel"></div>
<div><label>reasoning model</label><input id="s_reasonmodel"></div>
<div><label>count per run</label><input id="s_cpr" type="number" min="1" max="200"></div>
<div><label>reg threads</label><input id="s_threads" type="number" min="1" max="4"></div>
<div><label>gateway rpm</label><input id="s_rpm" type="number" min="1" max="100000"></div>
<div><label>gateway max concurrent</label><input id="s_conc" type="number" min="1" max="64"></div>
<div><label>posts per query</label><input id="s_ppq" type="number" min="1" max="100"></div>
<div><label>days window</label><input id="s_days" type="number" min="0" max="365"></div>
<div><label>query delay sec</label><input id="s_delay" type="number" step="0.5" min="0" max="300"></div>
<div><label>verify sample</label><input id="s_vs" type="number" min="0" max="100"></div>
<div><label>gateway exe path</label><input id="s_exe" placeholder="пусто = авто"></div>
<div><label>G2A_KEY (secret)</label><input id="s_g2a" type="password" placeholder="пусто = без изменений"></div>
</div>
<div class="row"><button onclick="saveAll(this)">Save settings</button><span id="cfgState"></span></div>
<div class="msg" id="cfgMsg"></div></div>

<div class="card"><h2>Logs <span id="logName" style="color:var(--dim)"></span></h2>
<div class="row" style="margin-top:0"><button onclick="showLog('reg')">reg</button><button onclick="showLog('crawl')">crawl</button><button onclick="showLog('gateway')">gateway</button></div>
<pre id="logOut">выбери лог</pre></div>
</div></div>

<script>
let logTimer=null, statusTimer=null, curLog=null, loadedCfg=null;
const $=id=>document.getElementById(id);
async function api(path,opt){const r=await fetch(path,opt);return r.json();}
function pill(el,txt,cls){el.innerHTML=`<span class="pill ${cls}">${txt}</span>`;}
async function loadStatus(btn){btn&&(btn.disabled=true);
 try{const d=await api('/api/status');
  $('statusOut').textContent=d.output;
  pill($('statusPill'),d.ok?'ALL OK':'HAS FAILURES',d.ok?'ok':'err');
 }catch(e){$('statusOut').textContent='panel error: '+e;}
 btn&&(btn.disabled=false);}
function autoRefresh(){clearInterval(statusTimer);
 if($('autoref').checked)statusTimer=setInterval(()=>loadStatus(null),30000);}
async function gwStatus(){const d=await api('/api/gateway');
 pill($('gwPill'),d.running?'RUNNING :8000':'STOPPED',d.running?'ok':'err');}
async function gw(action,btn){btn.disabled=true;
 const d=await api('/api/gateway',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({action})});
 showMsg('taskMsg',d.ok?('gateway: '+(d.detail||action+' ok')):('gateway: '+d.error),d.ok);
 btn.disabled=false;gwStatus();}
function showMsg(id,txt,ok){const m=$(id);m.textContent=txt;m.className='msg '+(ok?'ok':'err');}
async function runTask(name,params,btn){btn.disabled=true;
 const d=await api('/api/run',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({task:name,params})});
 if(!d.ok){showMsg('taskMsg',d.error,false);btn.disabled=false;return;}
 showMsg('taskMsg',name+' запущен — лог внизу',true);
 showLog(name);pollPills();btn.disabled=false;}
function runCrawl(btn){const p={queries_file:$('cQ').value,target:parseInt($('cT').value)};
 const d=parseInt($('cD').value);if(!isNaN(d))p.days=d;
 if($('cS').value.trim())p.state=$('cS').value.trim();
 runTask('crawl',p,btn);}
async function pollPills(){clearInterval(window._pp);
 window._pp=setInterval(async()=>{
  let busy=false;
  for(const n of ['reg','crawl']){const s=await api('/api/task?name='+n);
   const el=$(n+'Pill');
   if(s.running){pill(el,'RUNNING','run');busy=true;}
   else if(s.exit===0)pill(el,'DONE exit 0','ok');
   else if(s.exit!==null)pill(el,'FAILED exit '+s.exit,'err');
   else el.innerHTML='';}
  if(curLog)await tailLog();
  if(!busy&&!curLog)clearInterval(window._pp);},2000);}
function showLog(name){curLog=name;logOff=0;$('logOut').textContent='';$('logName').textContent='· '+name;
 tailLog();clearInterval(window._pp);pollPills();}
let logOff=0;
async function tailLog(){if(!curLog)return;
 const d=await api(`/api/logs?name=${curLog}&offset=${logOff}`);
 logOff=d.offset;if(d.text)$('logOut').textContent+=d.text;
 $('logOut').scrollTop=$('logOut').scrollHeight;}
async function loadConfig(){const d=await api('/api/config');loadedCfg=d;
 $('s_email').value=d.email.provider;$('s_cap').value=d.captcha.mode;$('s_pmode').value=d.proxy.mode;
 $('s_geo').value=(d.proxy.geo_whitelist||[]).join(', ');$('s_pmodel').value=d.gateway.parse_model||'';
 $('s_toolmodel').value=d.gateway.tool_model||'';$('s_reasonmodel').value=d.gateway.reasoning_model||'';
 $('s_sitekey').value=d.captcha.turnstile_sitekey||'';$('s_captimeout').value=d.captcha.solver_timeout_sec||70;
 $('s_capretry').value=d.captcha.retry_per_solver||2;$('s_capfallback').value=d.captcha.fallback_after_n_failures||3;
 $('s_cpr').value=d.farm.count_per_run||5;$('s_threads').value=d.farm.threads||1;
 $('s_rpm').value=d.gateway.rpm_limit||120;$('s_conc').value=d.gateway.max_concurrent||8;
 $('s_ppq').value=d.parser.posts_per_query;$('s_days').value=d.parser.days_window;
 $('s_delay').value=d.parser.delay_between_queries_sec!==undefined?d.parser.delay_between_queries_sec:3;
 $('s_vs').value=d.parser.verify_sample_size;
 $('s_exe').value=d.gateway.exe_path||'';
 $('cfgState').innerHTML=`<span class="pill ${d.secrets.G2A_KEY?'ok':'err'}">G2A_KEY ${d.secrets.G2A_KEY?'set':'missing'}</span> <span class="pill ${d.secrets.YESCAPTCHA_KEY?'ok':'err'}">YESCAPTCHA ${d.secrets.YESCAPTCHA_KEY?'set':'—'}</span> <span class="pill ${d.secrets.CAPSOLVER_KEY?'ok':'err'}">CAPSOLVER ${d.secrets.CAPSOLVER_KEY?'set':'—'}</span>`;
 $('s_gmail').placeholder=d.email.gmail&&d.email.gmail.base_email?d.email.gmail.base_email:'пусто = без изменений';
 $('s_psingle').placeholder=d.proxy.single||'пусто = без изменений';}
async function saveAll(btn){btn.disabled=true;const u={};
 const v=(id,key,parse)=>{const x=$(id).value.trim();if(x&&!x.includes('***'))u[key]=parse?parse(x):x;};
 if($('s_email').value!==loadedCfg.email.provider)u['email.provider']=$('s_email').value;
 if($('s_cap').value!==loadedCfg.captcha.mode)u['captcha.mode']=$('s_cap').value;
 if($('s_sitekey').value&&$('s_sitekey').value!==loadedCfg.captcha.turnstile_sitekey)u['captcha.turnstile_sitekey']=$('s_sitekey').value;
 const cto=+$('s_captimeout').value;if(cto&&cto!==loadedCfg.captcha.solver_timeout_sec)u['captcha.solver_timeout_sec']=cto;
 const cre=+$('s_capretry').value;if(cre&&cre!==loadedCfg.captcha.retry_per_solver)u['captcha.retry_per_solver']=cre;
 const cfb=+$('s_capfallback').value;if(cfb&&cfb!==loadedCfg.captcha.fallback_after_n_failures)u['captcha.fallback_after_n_failures']=cfb;
 if($('s_toolmodel').value&&$('s_toolmodel').value!==loadedCfg.gateway.tool_model)u['gateway.tool_model']=$('s_toolmodel').value;
 if($('s_reasonmodel').value&&$('s_reasonmodel').value!==loadedCfg.gateway.reasoning_model)u['gateway.reasoning_model']=$('s_reasonmodel').value;
 const cpr=+$('s_cpr').value;if(cpr&&cpr!==loadedCfg.farm.count_per_run)u['farm.count_per_run']=cpr;
 const thr=+$('s_threads').value;if(thr&&thr!==loadedCfg.farm.threads)u['farm.threads']=thr;
 const rpm=+$('s_rpm').value;if(rpm&&rpm!==loadedCfg.gateway.rpm_limit)u['gateway.rpm_limit']=rpm;
 const conc=+$('s_conc').value;if(conc&&conc!==loadedCfg.gateway.max_concurrent)u['gateway.max_concurrent']=conc;
 if($('s_pmode').value!==loadedCfg.proxy.mode)u['proxy.mode']=$('s_pmode').value;
 v('s_gmail','email.gmail.base_email');v('s_psingle','proxy.single');v('s_exe','gateway.exe_path');v('s_pmodel','gateway.parse_model');
 const geo=$('s_geo').value.split(',').map(s=>s.trim()).filter(Boolean);
 if(JSON.stringify(geo)!==JSON.stringify(loadedCfg.proxy.geo_whitelist||[]))u['proxy.geo_whitelist']=geo;
 const num=(id,key)=>{const x=$(id).value;if(x!=='')u[key]=parseFloat(x);};
 num('s_ppq','parser.posts_per_query');num('s_days','parser.days_window');num('s_delay','parser.delay_between_queries_sec');num('s_vs','parser.verify_sample_size');
 const d=await api('/api/config',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({updates:u})});
 if(d.ok){const sec={};
  if($('s_capkey').value)sec.YESCAPTCHA_KEY=$('s_capkey').value;
  if($('s_capsolverkey').value)sec.CAPSOLVER_KEY=$('s_capsolverkey').value;
  if($('s_nopechakey').value)sec.NOPETCHA_KEY=$('s_nopechakey').value;
  if($('s_2captchakey').value)sec.TWOCAPTCHA_KEY=$('s_2captchakey').value;
  if($('s_g2a').value)sec.G2A_KEY=$('s_g2a').value;
  if(Object.keys(sec).length){const s=await api('/api/secrets',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(sec)});
   showMsg('cfgMsg','saved: config + '+Object.keys(sec).join(', '),s.ok);$('s_capkey').value='';$('s_capsolverkey').value='';$('s_nopechakey').value='';$('s_2captchakey').value='';$('s_g2a').value='';}
  else showMsg('cfgMsg','config saved ('+Object.keys(u).length+' keys)',true);
  loadConfig();}
 else showMsg('cfgMsg','errors: '+d.errors.join('; '),false);
 btn.disabled=false;}
loadConfig();gwStatus();loadStatus(null);
</script></body></html>"""

class Handler(BaseHTTPRequestHandler):
    server_version = "farm-panel/2.1"

    def log_message(self, fmt, *args):  # quiet access log
        pass

    def _send(self, code, body, ctype="application/json"):
        data = body if isinstance(body, bytes) else json.dumps(body, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype + "; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def _body(self):
        n = int(self.headers.get("Content-Length") or 0)
        try:
            return json.loads(self.rfile.read(n)) if n else {}
        except json.JSONDecodeError:
            return None

    def do_GET(self):
        path, _, qs = self.path.partition("?")
        if path == "/":
            return self._send(200, PAGE.encode(), "text/html")
        if path == "/api/status":
            return self._send(200, run_status())
        if path == "/api/config":
            try:
                secrets = load_secrets_dict()
                # farm.py falls back to local files; reflect that in UI flags
                if not secrets.get("G2A_KEY") and os.path.exists(os.path.join(FARM_DIR, "g2a_key.txt")):
                    secrets["G2A_KEY"] = "file:g2a_key.txt"
                if not secrets.get("G2A_ADMIN_PASS") and \
                        os.path.exists(os.path.join(FARM_DIR, "SECRETS.local.txt")):
                    secrets["G2A_ADMIN_PASS"] = "file:SECRETS.local.txt"
                return self._send(200, public_config(load_config(), secrets))
            except Exception as e:
                return self._send(500, {"ok": False, "error": str(e)})
        if path == "/api/gateway":
            return self._send(200, {"running": gateway_running()})
        if path == "/api/task":
            name = dict(p.split("=", 1) for p in qs.split("&") if "=" in p).get("name", "")
            return self._send(200, task_state(name))
        if path == "/api/logs":
            q = dict(p.split("=", 1) for p in qs.split("&") if "=" in p)
            name = q.get("name", "")
            if name not in ("reg", "crawl", "gateway"):
                return self._send(400, {"error": "bad log name"})
            if name == "gateway":
                logp = os.path.join(LOG_DIR, "gateway.log")
                try:
                    off = min(int(q.get("offset", 0)), os.path.getsize(logp))
                    with open(logp, "rb") as f:
                        f.seek(off)
                        chunk = f.read(200_000)
                    return self._send(200, {"text": chunk.decode("utf-8", "replace"),
                                            "offset": off + len(chunk),
                                            "running": gateway_running(), "exit": None})
                except OSError:
                    return self._send(200, {"text": "", "offset": 0, "running": gateway_running(), "exit": None})
            try:
                offset = int(q.get("offset", 0))
            except ValueError:
                offset = 0
            return self._send(200, read_log(name, offset))
        return self._send(404, {"error": "not found"})

    def do_POST(self):
        path = self.path.partition("?")[0]
        body = self._body()
        if body is None:
            return self._send(400, {"ok": False, "error": "invalid json"})
        if path == "/api/config":
            cfg = load_config()
            errors = apply_updates(cfg, body.get("updates"))
            if errors:
                return self._send(400, {"ok": False, "errors": errors})
            save_config(cfg)
            return self._send(200, {"ok": True, "applied": sorted(body.get("updates") or {})})
        if path == "/api/secrets":
            return self._send(200, {"ok": True, "secrets": save_secrets(body)})
        if path == "/api/run":
            task = body.get("task")
            try:
                argv = build_task_argv(task, body.get("params"))
            except ValueError as e:
                return self._send(400, {"ok": False, "error": str(e)})
            r = start_task(task, argv)
            return self._send(200 if r["ok"] else 409, r)
        if path == "/api/gateway":
            action = body.get("action")
            r = gateway_start() if action == "start" else gateway_stop() if action == "stop" \
                else {"ok": False, "error": "action must be start|stop"}
            return self._send(200 if r.get("ok") else 400, r)
        return self._send(404, {"ok": False, "error": "not found"})

def main():
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    port = int(os.getenv("PANEL_PORT", "8010"))
    os.makedirs(LOG_DIR, exist_ok=True)
    srv = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    print(f"[panel] grok-x-farm control -> http://127.0.0.1:{port} (localhost only)")
    print(f"[panel] config: {CONFIG_PATH if os.path.exists(CONFIG_PATH) else EXAMPLE_PATH}")
    print(f"[panel] secrets: {SECRETS_PATH} ({'exists' if os.path.exists(SECRETS_PATH) else 'none yet'})")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass

if __name__ == "__main__":
    main()