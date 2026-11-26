import json

import upload_gate

CONFIG = {"publish_intro_fade_sec": 15}


def render(publish_dir, name, label="Seems", kind="video", intro_fade=15.0, sidecar=True, approved=False):
    video = publish_dir / "YouTube" / f"{name}.mp4"
    video.parent.mkdir(parents=True, exist_ok=True)
    video.write_bytes(b"x")
    if sidecar:
        video.with_suffix(".json").write_text(
            json.dumps({"title": f"{label} | Live", "description": "d", "tags": ["t"], "event_date": "01.30.2026"})
        )
    key = f"YouTube/{name}.mp4"
    manifest_file = publish_dir / "publish_manifest.json"
    manifest = json.loads(manifest_file.read_text()) if manifest_file.exists() else {}
    manifest[key] = {"kind": kind, "label": label, "event": "FF", "intro_fade": intro_fade, "upload_approved": approved}
    manifest_file.write_text(json.dumps(manifest))
    return key


def test_clean_render_has_no_blockers(tmp_path):
    key = render(tmp_path, "a")
    assert upload_gate.rows(tmp_path, CONFIG)[0]["blockers"] == []
    assert key


def test_each_blocker_is_caught(tmp_path):
    render(tmp_path, "short", kind="short")
    render(tmp_path, "unnamed", label="Unknown Artist")
    render(tmp_path, "nosidecar", sidecar=False)
    render(tmp_path, "oldintro", intro_fade=0.0)
    by_key = {r["key"]: r["blockers"] for r in upload_gate.rows(tmp_path, CONFIG)}
    assert any("Shorts" in b for b in by_key["YouTube/short.mp4"])
    assert any("placeholder" in b for b in by_key["YouTube/unnamed.mp4"])
    assert any("sidecar" in b for b in by_key["YouTube/nosidecar.mp4"])
    assert any("intro" in b for b in by_key["YouTube/oldintro.mp4"])


def test_cannot_approve_a_blocked_video_but_can_approve_a_clean_one(tmp_path):
    render(tmp_path, "good")
    render(tmp_path, "bad", label="Set 1")
    changed = upload_gate.set_approval(tmp_path, CONFIG, ["good", "bad"], approve=True)
    assert changed == ["YouTube/good.mp4"]
    flags = {k: v["upload_approved"] for k, v in json.loads((tmp_path / "publish_manifest.json").read_text()).items()}
    assert flags == {"YouTube/good.mp4": True, "YouTube/bad.mp4": False}


def test_queue_contains_only_approved_and_still_eligible(tmp_path):
    render(tmp_path, "approved", approved=True)
    render(tmp_path, "waiting")
    render(tmp_path, "approved_but_rerendered_with_new_intro", approved=True, intro_fade=0.0)
    queue = upload_gate.build_queue(tmp_path, CONFIG)
    assert [q["video"].endswith("approved.mp4") for q in queue] == [True]
    assert queue[0]["title"] == "Seems | Live" and queue[0]["band"] == "Seems"


def test_revoke(tmp_path):
    render(tmp_path, "x", approved=True)
    upload_gate.set_approval(tmp_path, CONFIG, ["x"], approve=False)
    assert upload_gate.build_queue(tmp_path, CONFIG) == []
