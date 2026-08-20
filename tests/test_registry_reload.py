

def test_a_deleted_file_changes_the_fingerprint(tmp_path):
    """เทียบ mtime ใหม่สุดอย่างเดียวมองไม่เห็นการลบ — ลบตัวที่ใหม่ที่สุดทำให้ค่าลดลง"""
    from app.registry.store import fingerprint

    models = tmp_path / "models"
    models.mkdir()
    (tmp_path / "gateway.yaml").write_text("{}")
    a = models / "a.yaml"
    a.write_text("x")
    before = fingerprint(tmp_path)
    a.unlink()
    assert fingerprint(tmp_path) != before


def test_one_worker_deleting_is_seen_by_another(tmp_path):
    """แต่ละ worker ถือ snapshot ของตัวเอง · ลบผ่านตัวหนึ่งแล้วตัวอื่นต้องเลิกเสิร์ฟด้วย

    เคสจริง: กด Delete แล้วแถวเด้งกลับมา เพราะ GET รอบถัดไปไปโดน worker ที่ยังไม่รู้
    """
    import shutil
    from pathlib import Path

    from app.registry.store import RegistryStore

    src = Path(__file__).resolve().parents[1] / "config"
    shutil.copytree(src, tmp_path / "config")
    config = tmp_path / "config"

    worker_a = RegistryStore(config, reload_seconds=0)
    worker_b = RegistryStore(config, reload_seconds=0)
    worker_a.reload()
    worker_b.reload()
    assert "coding" in worker_b.snapshot.models

    (config / "models" / "coding.yaml").unlink()
    worker_a.reload()

    assert "coding" not in worker_b.refresh_if_stale().models
