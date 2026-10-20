"""Portable first-party teaching sources, never a runtime code runner."""
import hashlib
import io
import json
from pathlib import Path
import struct
import subprocess
import sys
import tomllib
import zipfile
import zlib

ROOT = Path(__file__).resolve().parents[1]


def resources():
    return json.loads((ROOT / "src/openclank/treehouse_curriculum/manifest.json").read_text())["resources"]


def archive(resource_id):
    record = next(r for r in resources() if r["id"] == resource_id)
    raw = (ROOT / record["path"]).read_bytes()
    return record, zipfile.ZipFile(io.BytesIO(raw))


def test_project_archives_are_self_contained_licensed_and_inventory_complete():
    required = {"first-programs", "practice-cards", "programming-repairs", "godot-target-square",
                "bevy-target-square", "minecraft-target-square", "trace-toy", "finite-game-models",
                "asset-handoff", "openclank-notes"}
    assert required <= {r["id"] for r in resources()}
    for record in resources():
        if record["mime"] != "application/zip":
            continue
        _, source = archive(record["id"])
        names = set(source.namelist())
        prefix = next(iter(names)).split("/")[0] + "/"
        assert prefix + "README.md" in names
        assert prefix + "LICENSE" in names
        inventory = json.loads(source.read(prefix + "PROVENANCE.json"))
        assert set(inventory["files"]) == {name[len(prefix):] for name in names if name != prefix + "PROVENANCE.json"}
        for relative, digest in inventory["files"].items():
            assert digest == "sha256:" + hashlib.sha256(source.read(prefix + relative)).hexdigest()
        assert not any(token in name.split("/") for name in names for token in [".git", ".godot", "target", "node_modules"])
    assert not list((ROOT / "static/treehouse/projects").rglob("*.html"))
    assert not list((ROOT / "static/treehouse/projects").rglob("*.js"))
    assert not list((ROOT / "static/treehouse/projects").rglob("*.svg"))


def test_real_bevy_lock_and_pinned_wrapper_are_delivered_without_runtime_claim():
    record, source = archive("bevy-target-square")
    prefix = "bevy-target-square/"
    config = tomllib.loads(source.read(prefix + "Cargo.toml").decode())
    lock = tomllib.loads(source.read(prefix + "Cargo.lock").decode())
    assert config["dependencies"]["bevy"] == "=0.17.3"
    assert config["package"]["rust-version"] == "1.90"
    assert any(p["name"] == "bevy" and p["version"] == "0.17.3" for p in lock["package"])
    assert record["provenance"]["status"] == "cargo-check-qualified-not-run"
    assert b"200.0, 0.0" in source.read(prefix + "src/main.rs")
    _, source = archive("minecraft-target-square")
    prefix = "minecraft-target-square/"
    wrapper = source.read(prefix + "gradle/wrapper/gradle-wrapper.jar")
    assert hashlib.sha256(wrapper).hexdigest() == "7a9ce74cff467ca1bf60a4fcd9f05185acceda4d0f382434d393e17864262c5d"
    props = source.read(prefix + "gradle/wrapper/gradle-wrapper.properties").decode()
    assert "gradle-9.7.1-bin.zip" in props
    assert "distributionSha256Sum=acd53f1edaf02f1a8ff99879f8a34b302661a057d9b063ae9e35b552f804d20a" in props
    assert prefix + "generated-stage/src/client/java/org/example/targetsquare/datagen/MarkerRecipeProvider.java" in source.namelist()
    assert source.getinfo(prefix + "gradlew").external_attr >> 16 & 0o111


def test_published_tiny_python_source_and_plain_page_attachment_are_complete():
    _, source = archive("first-programs")
    # Exactly the reviewed, shipped original toy; no user-uploaded or arbitrary archive execution.
    result = subprocess.run([sys.executable, "-c", source.read("first-programs/first_sum.py").decode()], capture_output=True, text=True, check=True)
    assert result.stdout == "5\n"
    assert b'first_sum.js' in source.read("first-programs/first_sum.html")
    _, source = archive("practice-cards")
    assert b'href="style.css"' in source.read("practice-cards/index.html")
    assert b'for="nickname"' in source.read("practice-cards/broken-label.html")
    assert b'@media (min-width: 48rem)' in source.read("practice-cards/style.css")


def test_literal_blue_png_and_all_practice_inputs_remain_offline():
    record = next(r for r in resources() if r["id"] == "oc-blue-square")
    data = (ROOT / record["path"]).read_bytes()
    assert struct.unpack(">II", data[16:24]) == (16, 16)
    compressed = b""; cursor = 8
    while cursor < len(data):
        length = struct.unpack(">I", data[cursor:cursor+4])[0]
        if data[cursor+4:cursor+8] == b"IDAT":
            compressed += data[cursor+8:cursor+8+length]
        cursor += length + 12
    assert zlib.decompress(compressed) == (b"\x00" + bytes([0,0,255,255])*16)*16
    for lane in ["programming", "games", "ai", "openclank"]:
        record = next(r for r in resources() if r["id"] == lane + "-practice-cases")
        cases = json.loads((ROOT / record["path"]).read_text())["cases"]
        assert {case["id"] for case in cases} == set(record["unitIds"])
        assert all(case["inputAndTask"].strip() and case["limit"] for case in cases)


def test_frontend_native_image_and_current_native_course_exports_are_exact():
    import base64
    from PIL import Image
    _, source = archive("oc-imps-layered-practice")
    prefix = "oc-imps-layered-practice/"
    expected = {"before.imps.json": "cf101920854f760d36de8f85b341fbf95e24149638b3ab9617ade0566c712dda",
                "after.imps.json": "97e426053d211a69a72b7a0598c1d8cce5895a3260fc14028cb538952b54d5a8"}
    for name, digest in expected.items():
        raw = source.read(prefix + name)
        assert hashlib.sha256(raw).hexdigest() == digest
        project = json.loads(raw)
        assert project["kind"] == "imps-project" and project["schema_version"] == 1
        assert project["image"] == {"provider": "", "resource_id": "", "revision": ""}
        layers = project["state"]["layers"]
        assert [layer["id"] for layer in layers] == ["layer-1", "layer-2"]
        base = Image.open(io.BytesIO(base64.b64decode(layers[0]["dataUrl"].split(",", 1)[1]))).convert("RGBA")
        overlay = Image.open(io.BytesIO(base64.b64decode(layers[1]["dataUrl"].split(",", 1)[1]))).convert("RGBA")
        assert base.size == overlay.size == (16,16)
        assert overlay.getpixel((0,0)) == ((255,0,0,255) if name.startswith("after") else (255,255,0,255))
    actual = source.read(prefix + "flattened-native.png")
    assert hashlib.sha256(actual).hexdigest() == "0ab2fbe703eb14620850af7978f321d607a97d784b94c2a0d1f543e2318b94bf"
    image = Image.open(io.BytesIO(actual)).convert("RGBA")
    assert [image.getpixel(point) for point in [(0,0),(1,1),(5,5)]] == [(255,0,0,255),(255,255,0,255),(0,0,255,255)]
    _, source = archive("oc-tiny-treehouse-course")
    actual = source.read("oc-tiny-treehouse-course/tiny-course.treehouse.json")
    assert hashlib.sha256(actual).hexdigest() == "89bbd608a390916ef93b76c78a195a250b193c22a1285eb5f5e377c52e609fd7"
    course = json.loads(actual)
    assert course["format"] == "copal-treehouse-course-v1" and course["schemaVersion"] == 1
    assert len(course["activities"]) == 2 and len(course["assignments"]) == 1
    assert "AGPL-3.0-only" in course["course"]["description"]
    assert "MIT practice course" not in course["course"]["description"]
    assert not {"profiles", "submissions", "events", "processedCommands", "progressResets"} & course.keys()
    assert b"AGPL-3.0-only" in source.read("oc-tiny-treehouse-course/README.md")


def test_published_bevy_setup_matches_actual_supplied_lock_status():
    record = next(r for r in resources() if r["id"] == "games-practice-cases")
    cases = json.loads((ROOT / record["path"]).read_text())["cases"]
    case = next(case for case in cases if case["id"] == "games:rust-games:pinned-project")
    assert "includes a real resolved" in case["inputAndTask"]
    assert "cargo check --locked --jobs 2" in case["inputAndTask"]
    assert "did not link or launch" in case["inputAndTask"]
    assert "There is no qualified distributed Cargo.lock yet" not in case["inputAndTask"]
