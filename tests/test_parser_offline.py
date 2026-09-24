"""Offline self-checks for farm.py parser helpers (no network, no gateway).

Run: python tests/test_parser_offline.py
"""
import json
import os
import sys
import tempfile
from datetime import date, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import farm  # noqa: E402

def test_parse_json_array():
    assert len(farm.parse_json_array('[{"a":1},{"a":2}]')) == 2
    assert len(farm.parse_json_array('```json\n[{"a":1}]\n```')) == 1
    assert len(farm.parse_json_array('Sure! Here you go: [{"a":1}] hope that helps')) == 1
    assert farm.parse_json_array("no json here") == []
    assert farm.parse_json_array("") == []
    assert farm.parse_json_array('{"not":"an array"}') == []

def test_tweet_key():
    assert farm.tweet_key({"url": "https://x.com/user/status/12345?s=20"}) == "12345"
    k1 = farm.tweet_key({"handle": "u", "date": "d", "text": "t"})
    k2 = farm.tweet_key({"handle": "u", "date": "d", "text": "t"})
    assert k1 == k2 and "12345" not in k1

def test_parse_post_date():
    assert farm.parse_post_date("2026-09-18") == date(2026, 9, 18)
    assert farm.parse_post_date("Aug 29, 2026") == date(2026, 8, 29)
    assert farm.parse_post_date("29.08.2026") == date(2026, 8, 29)
    assert farm.parse_post_date("Sun Aug 31 03:03:32 +0000 2026") == date(2026, 8, 31)
    assert farm.parse_post_date("yesterday") is None
    assert farm.parse_post_date("") is None
    assert farm.parse_post_date(None) is None

def test_date_within():
    today = date(2026, 9, 21)
    recent = {"date": (today - timedelta(days=2)).strftime("%Y-%m-%d")}
    old = {"date": (today - timedelta(days=40)).strftime("%Y-%m-%d")}
    weird = {"date": "soon"}
    assert farm.date_within(recent, 30, today) is True
    assert farm.date_within(old, 30, today) is False
    assert farm.date_within(weird, 30, today) is True  # unparseable kept
    assert farm.date_within(old, 0, today) is True  # filter disabled

def test_parse_likes():
    assert farm.parse_likes(18) == 18
    assert farm.parse_likes("18") == 18
    assert farm.parse_likes("1,234") == 1234
    assert farm.parse_likes("1.2K") == 1200
    assert farm.parse_likes("3M") == 3000000
    assert farm.parse_likes(None) is None
    assert farm.parse_likes("abc") is None

def test_seen_state_roundtrip():
    with tempfile.TemporaryDirectory() as td:
        p = os.path.join(td, "seen.json")
        assert farm.load_seen(p) == {}  # missing file -> empty
        entries = [{"url": "https://x.com/a/status/111", "likes": 5},
                   {"url": "https://x.com/b/status/222", "likes": 7}]
        state = farm.load_seen(p)
        assert farm.merge_seen(state, entries) == 2
        assert farm.merge_seen(state, entries) == 0  # idempotent
        with open(p, "w", encoding="utf-8") as f:
            json.dump(state, f)
        reloaded = farm.load_seen(p)
        assert set(reloaded.keys()) == {"111", "222"}
        assert reloaded["111"]["likes"] == 5
        # new entry merges into existing state
        assert farm.merge_seen(reloaded, [{"url": "https://x.com/c/status/333"}]) == 1
        assert len(reloaded) == 3
        # corrupt file -> empty state, no crash
        with open(p, "w", encoding="utf-8") as f:
            f.write("{broken")
        assert farm.load_seen(p) == {}

if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for fn in fns:
        fn()
        print(f"  ok {fn.__name__}")
    print(f"PASS {len(fns)}/{len(fns)} offline checks")