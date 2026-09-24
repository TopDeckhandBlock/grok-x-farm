"""Offline self-checks for dashboard/panel.py helpers (no network, no server).

Run: python tests/test_panel_offline.py
"""
import json
import os
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, "dashboard"))
import panel  # noqa: E402

EXAMPLE = os.path.join(ROOT, "config", "farm.config.example.json")

def test_mask_secret():
    assert panel.mask_secret("") == ""
    assert panel.mask_secret(None) == ""
    m = panel.mask_secret("abc")
    assert m and "abc" not in m  # short values fully masked
    long_v = "g2a_7c502f8386fc_deadbeef"
    m = panel.mask_secret(long_v)
    assert long_v not in m and m.startswith("g2a") and "…" in m
    assert panel.mask_secret(long_v) != long_v

def test_apply_updates_valid():
    cfg = json.load(open(EXAMPLE, encoding="utf-8"))
    errors = panel.apply_updates(cfg, {
        "email.provider": "gmail",
        "captcha.mode": "yescaptcha",
        "proxy.mode": "single",
        "proxy.single": "http://u:p@1.2.3.4:8080",
        "parser.posts_per_query": 20,
        "parser.delay_between_queries_sec": 1.5,
        "proxy.geo_whitelist": ["US", "EU"],
    })
    assert errors == []
    assert cfg["email"]["provider"] == "gmail"
    assert cfg["captcha"]["mode"] == "yescaptcha"
    assert cfg["parser"]["posts_per_query"] == 20
    assert cfg["parser"]["delay_between_queries_sec"] == 1.5
    assert cfg["proxy"]["geo_whitelist"] == ["US", "EU"]

def test_apply_updates_rejects_bad():
    cfg = json.load(open(EXAMPLE, encoding="utf-8"))
    before = json.dumps(cfg, sort_keys=True)
    errors = panel.apply_updates(cfg, {
        "email.provider": "not_a_provider",       # not in whitelist
        "captcha.mode": "deadcaptcha",             # not in whitelist
        "gateway.admin_pass_env": "HACKED",       # not an editable key
        "parser.posts_per_query": "many",         # wrong type
        "parser.days_window": -5,                 # out of range
        "no.such.key": 1,                         # unknown
    })
    assert len(errors) == 6
    assert json.dumps(cfg, sort_keys=True) == before  # nothing applied on error

def test_config_roundtrip_atomic():
    cfg = json.load(open(EXAMPLE, encoding="utf-8"))
    with tempfile.TemporaryDirectory() as td:
        p = os.path.join(td, "farm.config.json")
        panel.save_config(cfg, p)
        assert os.path.exists(p)
        assert not os.path.exists(p + ".tmp")  # atomic rename leaves no tmp
        back = panel.load_config(p)
        assert back == cfg

def test_safe_path():
    assert panel.safe_path(ROOT, "parser/queries.txt") == os.path.join("parser", "queries.txt")
    assert panel.safe_path(ROOT, "../evil.txt") is None
    assert panel.safe_path(ROOT, "parser/../../evil.txt") is None
    assert panel.safe_path(ROOT, "C:/Windows/system32") is None
    assert panel.safe_path(ROOT, "") is None
    assert panel.safe_path(ROOT, None) is None

def test_public_config_masks_secrets():
    cfg = json.load(open(EXAMPLE, encoding="utf-8"))
    cfg["proxy"]["single"] = "http://user:superpass@1.2.3.4:8080"
    cfg["email"]["gmail"]["base_email"] = "real@gmail.com"
    secrets = {"G2A_KEY": "g2a_secret_value", "YESCAPTCHA_KEY": ""}
    pub = panel.public_config(cfg, secrets)
    dumped = json.dumps(pub, ensure_ascii=False)
    assert "superpass" not in dumped
    assert "real@gmail.com" not in dumped
    assert "g2a_secret_value" not in dumped
    assert pub["secrets"]["G2A_KEY"] is True
    assert pub["secrets"]["YESCAPTCHA_KEY"] is False
    assert pub["email"]["provider"] == cfg["email"]["provider"]  # non-secret intact

def test_build_task_argv():
    argv = panel.build_task_argv("reg", {"count": 3})
    assert argv[-4:] == ["farm.py", "reg", "--count", "3"]
    argv = panel.build_task_argv("crawl", {
        "queries_file": "parser/queries.txt", "target": 50,
        "days": 14, "state": "seen.json", "out": "tweets.json"})
    assert "crawl" in argv and "--state" in argv and "seen.json" in argv
    assert "--days" in argv and "14" in argv
    for bad in [("reg", {"count": 0}), ("reg", {"count": "x"}), ("reg", {"count": 99}),
                ("crawl", {}), ("crawl", {"queries_file": "../x.txt", "target": 5}),
                ("crawl", {"queries_file": "q.txt", "target": 100000}),
                ("rm", {})]:
        try:
            panel.build_task_argv(*bad)
            raise AssertionError(f"should reject {bad}")
        except ValueError:
            pass

if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for fn in fns:
        fn()
        print(f"  ok {fn.__name__}")
    print(f"PASS {len(fns)}/{len(fns)} panel offline checks")