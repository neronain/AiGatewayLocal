"""Flipping one flag in a registry file without disturbing the rest of it.

The full rewrite in `write_model` is correct for a form submission and wrong for
a switch: it regenerates the document from the parsed model, so every comment in
the file disappears. On the live gateway that cost six lines explaining why one
llama.cpp backend runs a single slot — the sort of note nobody writes twice.

These pin the structural cases the line editor has to survive, including the one
that actually bites: a comment block sitting between two list items belongs to
the *next* item, so an insert must land before it.
"""

from __future__ import annotations

from pathlib import Path

from app.registry.writer import set_enabled_in_file

FILE = """\
apiVersion: litegate.dev/v1
kind: Model

metadata:
  alias: coder
  visibility: member

spec:
  upstream_model: some/model

  endpoints:
    - name: msi-6
      server_type: vllm
      base_url: http://msi-6:8000
      enabled: true
    # เครื่องสำรอง · priority ต่ำกว่าจึงรับงานเมื่อตัวแรกล่ม
    #
    # ตั้ง 1 slot เพื่อให้แต่ละ request ได้ context เต็ม
    - name: spark-worker
      server_type: llama.cpp
      base_url: http://spark-worker:8000
      max_concurrency: 1
      enabled: true

  enabled: true
"""


def write(tmp_path: Path, text: str = FILE) -> Path:
    path = tmp_path / "coder.yaml"
    path.write_text(text, encoding="utf-8")
    return path


def test_only_the_one_line_changes(tmp_path):
    path = write(tmp_path)
    assert set_enabled_in_file(path, False, endpoint="msi-6") is True

    before = FILE.splitlines()
    after = path.read_text(encoding="utf-8").splitlines()
    differing = [i for i, (a, b) in enumerate(zip(before, after, strict=True)) if a != b]
    assert len(differing) == 1, "ควรต่างกันบรรทัดเดียว"
    assert after[differing[0]] == "      enabled: false"


def test_the_comment_block_stays_with_the_endpoint_it_describes(tmp_path):
    path = write(tmp_path)
    set_enabled_in_file(path, False, endpoint="spark-worker")
    text = path.read_text(encoding="utf-8")
    assert "ตั้ง 1 slot เพื่อให้แต่ละ request ได้ context เต็ม" in text
    assert text.index("# เครื่องสำรอง") < text.index("- name: spark-worker")


def test_the_alias_flag_is_separate_from_its_endpoints(tmp_path):
    path = write(tmp_path)
    set_enabled_in_file(path, False)
    text = path.read_text(encoding="utf-8")
    assert text.endswith("  enabled: false\n")
    assert text.count("enabled: true") == 2, "endpoint ทั้งสองต้องไม่ถูกแตะ"


def test_a_missing_key_is_written_in_rather_than_assumed(tmp_path):
    """ไม่มี key = ค่า default ซึ่งคือ true — ปิดจึงต้องเขียนลงไป"""
    path = write(tmp_path, FILE.replace("      base_url: http://msi-6:8000\n      enabled: true",
                                        "      base_url: http://msi-6:8000"))
    assert set_enabled_in_file(path, False, endpoint="msi-6") is True

    text = path.read_text(encoding="utf-8")
    assert "      base_url: http://msi-6:8000\n      enabled: false\n" in text
    # แทรกก่อนคอมเมนต์ ไม่ใช่หลัง — ไม่งั้นคอมเมนต์ของ endpoint ถัดไปโดนคั่น
    assert text.index("enabled: false") < text.index("# เครื่องสำรอง")


def test_asking_for_the_default_on_a_missing_key_leaves_the_file_alone(tmp_path):
    source = FILE.replace("      base_url: http://msi-6:8000\n      enabled: true",
                          "      base_url: http://msi-6:8000")
    path = write(tmp_path, source)
    assert set_enabled_in_file(path, True, endpoint="msi-6") is True
    assert path.read_text(encoding="utf-8") == source


def test_an_endpoint_it_cannot_find_falls_back_rather_than_guessing(tmp_path):
    path = write(tmp_path)
    assert set_enabled_in_file(path, False, endpoint="not-a-machine") is False
    assert path.read_text(encoding="utf-8") == FILE, "ไฟล์ต้องไม่ถูกแตะ"


def test_a_file_that_is_not_there_falls_back(tmp_path):
    assert set_enabled_in_file(tmp_path / "gone.yaml", False) is False


def test_a_trailing_comment_on_the_flag_is_kept(tmp_path):
    path = write(tmp_path, FILE.replace(
        "  enabled: true\n", "  enabled: true  # ปิดชั่วคราวได้จากคอนโซล\n", 1))
    # ตัวแรกที่ replace โดนคือของ endpoint msi-6
    set_enabled_in_file(path, False, endpoint="msi-6")
    assert "      enabled: false  # ปิดชั่วคราวได้จากคอนโซล" in path.read_text(encoding="utf-8")
