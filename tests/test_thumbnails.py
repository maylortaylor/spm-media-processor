import json

from thumbnails import derive_event_title, is_placeholder, plan_event, short_date


def test_short_date():
    assert short_date("01.30.2026") == "01.30"
    assert short_date("1.5.2026") == "01.05"
    assert short_date("") is None


def test_placeholder():
    for label in ["OBS_ProgramOut_1080p", "C0022", "Set 1", "Unknown Artist", "", None]:
        assert is_placeholder(label)
    assert not is_placeholder("Seems")


def test_event_title_never_invented():
    assert derive_event_title("1.30 - FF Jan - Little Giver, Seems, Physical Plant",
                              ["LittleGiver", "Seems", "Physical Plant"]) == "FF Jan"
    assert derive_event_title("2.13 - Love Burn Decompression", ["JRose"]) == "Love Burn Decompression"
    assert derive_event_title("2.7 - Aliqua, movie props, viorica", ["Aliqua", "movie props", "viorica"]) is None
    assert derive_event_title("2.28 - Katara and Roger Thomas album release", ["Katara", "Roger Thomas"]) is None
    assert derive_event_title("2.27 - FF beach terror, harlow gold, then theres me", ["Beach Terror"]) is None


def test_plan_event(tmp_path):
    video = tmp_path / "raw.mp4"
    video.write_bytes(b"x")
    (tmp_path / "scan_result.json").write_text(json.dumps(
        {"folder_name": "2.20 - Mountain Hollar, Infinite Third", "event_date": "02.20.2026"}))
    (tmp_path / "a_segments.json").write_text(json.dumps({
        "source_file": str(video), "thumbnail_time": "00:10:00",
        "segments": [{"id": 1, "start": 60, "end": 900, "label": "Infinite Third", "approved": True},
                     {"id": 2, "start": 900, "end": 1800, "label": "OBS_ProgramOut_1", "approved": True}]}))
    rows = plan_event(tmp_path)
    good = next(r for r in rows if r["title"] == "Infinite Third")
    assert good["issues"] == [] and good["date"] == "02.20" and good["window"] == [60, 900]
    bad = next(r for r in rows if r["title"] is None and r["kind"] == "set")
    assert "band name unknown (placeholder label)" in bad["issues"]
