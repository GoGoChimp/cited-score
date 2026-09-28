import watch_store as W

def _home(tmp_path, monkeypatch):
    monkeypatch.setattr(W, "WATCH_FILE", str(tmp_path / "watches.json"))
    monkeypatch.setattr(W, "ALERTS_FILE", str(tmp_path / "alerts.jsonl"))

def test_add_list_remove(tmp_path, monkeypatch):
    _home(tmp_path, monkeypatch)
    W.add("https://a.com/", "daily")
    assert [w["url"] for w in W.list_watches()] == ["https://a.com/"]
    assert W.list_watches()[0]["cadence"] == "daily" and W.list_watches()[0]["last_score"] is None
    assert W.remove("https://a.com/") is True
    assert W.list_watches() == []

def test_add_dedupes(tmp_path, monkeypatch):
    _home(tmp_path, monkeypatch)
    W.add("https://a.com/"); W.add("https://a.com/", "daily")
    assert len(W.list_watches()) == 1 and W.list_watches()[0]["cadence"] == "daily"

def test_update_score(tmp_path, monkeypatch):
    _home(tmp_path, monkeypatch)
    W.add("https://a.com/")
    W.update_score("https://a.com/", 72, "2026-09-28T00:00:00")
    assert W.get("https://a.com/")["last_score"] == 72

def test_alerts_roundtrip_newest_first(tmp_path, monkeypatch):
    _home(tmp_path, monkeypatch)
    W.record_alert({"url": "https://a.com/", "old": 60, "new": 68, "at": "2026-09-28T01"})
    W.record_alert({"url": "https://a.com/", "old": 68, "new": 61, "at": "2026-09-28T02"})
    al = W.list_alerts()
    assert len(al) == 2 and al[0]["new"] == 61   # newest first

def test_missing_files_degrade(tmp_path, monkeypatch):
    _home(tmp_path, monkeypatch)
    assert W.list_watches() == [] and W.list_alerts() == [] and W.get("nope") is None
    assert W.remove("nope") is False
