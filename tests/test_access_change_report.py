"""The report that has to be right before anyone flips the access model.

PRD v1.4 option A makes a key inherit its models from the workspaces its owner
belongs to. That is not an addition — it re-permissions every key already in
circulation, and the people affected find out when a call they made yesterday
stops working. `scripts/access_change_report.py` exists so that is a number
somebody read before the change, not a support ticket after it.

A report nobody checked is worse than no report: it produces a confident empty
answer and the change ships. These seed the cases the report has to catch.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.db.models import (
    ApiKey,
    Base,
    Membership,
    ModelRecord,
    User,
    Workspace,
    WorkspaceModel,
)

REPO = Path(__file__).resolve().parents[1]


def _report():
    spec = importlib.util.spec_from_file_location(
        "access_change_report", REPO / "scripts" / "access_change_report.py"
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules["access_change_report"] = module
    spec.loader.exec_module(module)
    return module


report = _report()


@pytest.fixture
def db(tmp_path):
    """A gateway database seeded the way a real one looks mid-term."""
    engine = create_engine(f"sqlite:///{tmp_path / 'gateway.db'}")
    Base.metadata.create_all(engine)

    with Session(engine) as session:
        for alias in ("coding", "vision", "general"):
            session.add(ModelRecord(alias=alias, display_name=alias, upstream_model=alias))

        cs101 = Workspace(code="CS101", name="Intro")
        art200 = Workspace(code="ART200", name="Studio")
        session.add_all([cs101, art200])
        session.flush()

        # CS101 อนุญาตแค่ coding · ART200 แค่ vision — คนที่อยู่ทั้งสองต้องได้ union
        session.add_all([
            WorkspaceModel(workspace_id=cs101.id, model_alias="coding"),
            WorkspaceModel(workspace_id=art200.id, model_alias="vision"),
        ])

        outsider = User(external_id="6400001", display_name="No group")
        student = User(external_id="6400002", display_name="In CS101")
        both = User(external_id="6400003", display_name="In both")
        session.add_all([outsider, student, both])
        session.flush()
        session.add_all([
            Membership(user_id=student.id, workspace_id=cs101.id),
            Membership(user_id=both.id, workspace_id=cs101.id),
            Membership(user_id=both.id, workspace_id=art200.id),
        ])

        def key(user, name, **kw):
            session.add(ApiKey(
                user_id=user.id, name=name, key_prefix=f"lg_sk_{name}",
                key_hash=f"hash-{name}", scopes=[], **kw,
            ))

        key(outsider, "outsider")                       # ไม่มีกลุ่ม ไม่ผูก workspace
        key(student, "student")                         # อยู่ CS101 ไม่ผูก workspace
        key(both, "both")                               # อยู่สองกลุ่ม
        key(student, "pinned", workspace_id=art200.id)  # ผูกไว้แล้ว กลุ่มไม่ควรมีผล
        key(student, "narrow", models=["coding"])       # จำกัดบน key อยู่แล้ว
        key(student, "gone")
        session.flush()
        session.query(ApiKey).filter(ApiKey.name == "gone").update(
            {"revoked_at": __import__("datetime").datetime.now(__import__("datetime").UTC)}
        )
        session.commit()

    return engine


def _rows(engine):
    keys, members, allowed, names, catalogue = report.load(engine)
    out = {}
    for key in keys:
        before = report.permitted_today(key, allowed, catalogue)
        after = report.permitted_under_a(key, members, allowed, catalogue)
        out[key["name"]] = (before, after)
    return out


def test_a_revoked_key_is_not_in_the_report(db):
    """เพิกถอนไปแล้วไม่ใช่คนที่จะพัง — นับรวมทำให้ตัวเลขดูน่ากลัวเกินจริง"""
    assert "gone" not in _rows(db)


def test_someone_in_no_group_keeps_everything(db):
    """ไม่ได้อยู่กลุ่มไหน = ไม่มีอะไรมาจำกัด · ถ้าตีเป็นเซตว่างคือล็อกคนออกยกชุด"""
    before, after = _rows(db)["outsider"]
    assert before == after == {"coding", "vision", "general"}


def test_joining_one_group_is_where_access_is_lost(db):
    """เคสที่เอกสารเตือนไว้: อยู่ CS101 แล้วเหลือแค่ coding"""
    before, after = _rows(db)["student"]
    assert before == {"coding", "vision", "general"}
    assert after == {"coding"}
    assert before - after == {"vision", "general"}


def test_being_in_two_groups_adds_up_rather_than_conflicts(db):
    """อยู่หลายกลุ่มต้องได้รวมกัน · ถ้าเป็น intersection คนจะเสียสิทธิ์เพราะถูกใส่กลุ่มเพิ่ม"""
    _, after = _rows(db)["both"]
    assert after == {"coding", "vision"}


def test_a_key_pinned_to_a_workspace_ignores_the_owners_groups(db):
    """FR-42 · ผูกไว้ตอนออกแล้วต้องชนะ ไม่งั้นการเปลี่ยนนี้ไปแก้ของที่ตั้งใจตั้งไว้"""
    before, after = _rows(db)["pinned"]
    assert before == after == {"vision"}, "เจ้าของอยู่ CS101 แต่ key ผูก ART200"


def test_a_key_that_already_names_its_models_only_narrows(db):
    before, after = _rows(db)["narrow"]
    assert before == after == {"coding"}


def test_the_exit_code_says_whether_anyone_loses(db, capsys, monkeypatch):
    """สคริปต์ต้องบอกผลผ่าน exit code ด้วย เพื่อให้เอาไปวางใน CI หรือ pre-flight ได้"""
    monkeypatch.setattr(sys, "argv", ["report", "--db", str(db.url)])
    assert report.main() == 1, "มีคนเสียสิทธิ์ = ไม่ใช่ 0"
    assert "เสียสิทธิ์" in capsys.readouterr().out
