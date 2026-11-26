import json

import classify
import pipeline


def make_event(root, name, size_bytes=60 * 1024 * 1024):
    folder = root / name
    folder.mkdir()
    (folder / "set.mp4").write_bytes(b"0" * size_bytes)
    return folder


def write_scan(ws, names):
    ws.mkdir(parents=True)
    files = [{"name": n, "type": "video", "category": "short_set"} for n in names]
    (ws / "scan_result.json").write_text(json.dumps({"files": files}))


SCOPE = {"exclude": ["*karaoke*", "*wusf*"], "min_video_mb": 50}


def test_excluded_and_underscore_folders_are_skipped(tmp_path):
    make_event(tmp_path, "9.30 - Live Band Karaoke")
    make_event(tmp_path, "5.10 - WUSF Jazz")
    (tmp_path / "_processed").mkdir()
    rows = {r["name"]: r for r in pipeline.plan(tmp_path, SCOPE)}
    assert "_processed" not in rows
    assert rows["9.30 - Live Band Karaoke"]["action"] is None
    assert rows["5.10 - WUSF Jazz"]["action"] is None


def test_small_clip_folders_are_skipped(tmp_path):
    make_event(tmp_path, "10.20 - Sips", size_bytes=1024)
    assert pipeline.plan(tmp_path, SCOPE)[0]["action"] is None


def test_unscanned_gets_scan_and_analyze_and_scanned_is_never_rescanned(tmp_path):
    make_event(tmp_path, "11.1 - Burnt Toast")
    make_event(tmp_path, "2.7 - Done")
    write_scan(tmp_path / "_processed" / "2.7 - Done", ["set.mp4"])
    (tmp_path / "_processed" / "2.7 - Done" / "set_segments.json").write_text("{}")
    rows = {r["name"]: r for r in pipeline.plan(tmp_path, SCOPE)}
    assert rows["11.1 - Burnt Toast"]["action"] == "scan+analyze"
    assert rows["2.7 - Done"]["action"] is None


def test_scanned_but_unanalyzed_only_gets_analyze(tmp_path):
    make_event(tmp_path, "5.06 - Cheery")
    write_scan(tmp_path / "_processed" / "5.06 - Cheery", ["set.mp4"])
    assert pipeline.plan(tmp_path, SCOPE)[0]["action"] == "analyze"


def test_smallest_first_and_skips_last(tmp_path):
    make_event(tmp_path, "a big", size_bytes=200 * 1024 * 1024)
    make_event(tmp_path, "b small", size_bytes=60 * 1024 * 1024)
    make_event(tmp_path, "c karaoke")
    names = [r["name"] for r in pipeline.plan(tmp_path, {"exclude": ["*karaoke*"], "min_video_mb": 50})]
    assert names == ["b small", "a big", "c karaoke"]


def test_run_events_records_failures_and_honours_stop_file(tmp_path, monkeypatch):
    import analyze

    monkeypatch.setattr(classify, "scan_folder", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))
    monkeypatch.setattr(analyze, "run_analyze", lambda *a, **k: None)
    rows = [{"folder": str(tmp_path / "x"), "name": "x", "workspace": str(tmp_path / "ws"), "action": "scan+analyze", "gb": 1}]
    report = pipeline.run_events(rows, tmp_path, tmp_path / "pipeline.stop")
    assert report["failed"] == 1 and "boom" in report["results"][0]["error"]
    (tmp_path / "pipeline.stop").write_text("")
    assert pipeline.run_events(rows, tmp_path, tmp_path / "pipeline.stop")["processed"] == 0


def test_backoff_retries_quota_then_raises_other_errors(monkeypatch):
    monkeypatch.setattr(classify.time, "sleep", lambda s: None)
    calls = {"n": 0}

    class Client:
        class models:
            @staticmethod
            def generate_content(model, contents):
                calls["n"] += 1
                if calls["n"] < 3:
                    raise Exception("429 RESOURCE_EXHAUSTED")
                return "ok"

    assert classify._generate_with_backoff(Client, "m", "p") == "ok" and calls["n"] == 3

    class Bad:
        class models:
            @staticmethod
            def generate_content(model, contents):
                raise ValueError("bad request")

    try:
        classify._generate_with_backoff(Bad, "m", "p")
        assert False
    except ValueError:
        pass


def _workspace(tmp_path, bands, labels):
    ws = tmp_path / "ws"
    ws.mkdir()
    (ws / "scan_result.json").write_text(json.dumps({"bands": bands, "files": []}))
    for i, label in enumerate(labels):
        (ws / f"f{i}_segments.json").write_text(json.dumps({"segments": [{"id": 1, "label": label, "approved": True}]}))
    return ws


def test_suggest_labels_fills_only_unambiguous_placeholders(tmp_path):
    ws = _workspace(tmp_path, ["Nep"], ["Unknown Artist", "Seems"])
    assert pipeline.suggest_labels(ws) == ["f0_segments.json"]
    first = json.loads((ws / "f0_segments.json").read_text())
    assert first["segments"][0]["label"] == "Nep" and first["segments"][0]["label_source"] == "scan_bands"
    assert json.loads((ws / "f1_segments.json").read_text())["segments"][0]["label"] == "Seems"
    assert "reviewed" not in first


def test_suggest_labels_skips_when_several_bands(tmp_path):
    ws = _workspace(tmp_path, ["A", "B"], ["Unknown Artist"])
    assert pipeline.suggest_labels(ws) == []


def test_backoff_also_retries_503(monkeypatch):
    monkeypatch.setattr(classify.time, "sleep", lambda s: None)
    calls = {"n": 0}

    class Client:
        class models:
            @staticmethod
            def generate_content(model, contents):
                calls["n"] += 1
                if calls["n"] < 2:
                    raise Exception("503 UNAVAILABLE high demand")
                return "ok"

    assert classify._generate_with_backoff(Client, "m", "p") == "ok"
