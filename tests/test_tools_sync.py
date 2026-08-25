"""Client-tools mirror: signature verification, registry, and gated promote."""

from __future__ import annotations

import base64
import hashlib
from pathlib import Path

import httpx
import pytest
import respx
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from app.registry.tools_schema import ToolDefinition, load_tool_registry
from app.tools import sync as S
from app.tools.minisign import MinisignError, verify_file

REPO_ROOT = Path(__file__).resolve().parent.parent


# --------------------------------------------------------------------------- #
# minisign helpers — build a real signed artefact from a throwaway key         #
# --------------------------------------------------------------------------- #
def _make_minisign(data: bytes, *, key_id: bytes = b"\x01\x02\x03\x04\x05\x06\x07\x08",
                   trusted_comment: str = "signed by test", prehashed: bool = True):
    sk = Ed25519PrivateKey.generate()
    pk_raw = sk.public_key().public_bytes_raw()
    pubkey_b64 = base64.b64encode(b"Ed" + key_id + pk_raw).decode()

    algo = b"ED" if prehashed else b"Ed"
    signed = hashlib.blake2b(data).digest() if prehashed else data
    sig = sk.sign(signed)
    blob = base64.b64encode(algo + key_id + sig).decode()
    global_sig = base64.b64encode(sk.sign(sig + trusted_comment.encode())).decode()
    sig_text = (
        f"untrusted comment: minisign signature\n{blob}\n"
        f"trusted comment: {trusted_comment}\n{global_sig}\n"
    )
    return pubkey_b64, key_id.hex().upper(), sig_text


def test_minisign_verifies_and_returns_trusted_comment(tmp_path):
    data = b"hello world" * 100
    pubkey, key_id, sig_text = _make_minisign(data, trusted_comment="release 1.0")
    target = tmp_path / "asset.bin"
    target.write_bytes(data)
    assert verify_file(target, sig_text, pubkey, key_id_hex=key_id) == "release 1.0"


def test_minisign_legacy_variant(tmp_path):
    data = b"raw signed file"
    pubkey, key_id, sig_text = _make_minisign(data, prehashed=False)
    target = tmp_path / "asset.bin"
    target.write_bytes(data)
    assert verify_file(target, sig_text, pubkey, key_id_hex=key_id) == "signed by test"


def test_minisign_accepts_tauri_base64_wrapped_sig(tmp_path):
    # Tauri base64-encodes the whole .sig file into one line.
    data = b"tauri-signed installer" * 20
    pubkey, key_id, sig_text = _make_minisign(data, trusted_comment="timestamp:1  file:x.msi")
    wrapped = base64.b64encode(sig_text.encode()).decode()
    target = tmp_path / "asset.msi"
    target.write_bytes(data)
    assert verify_file(target, wrapped, pubkey, key_id_hex=key_id).startswith("timestamp:")


def test_minisign_key_id_accepts_big_endian_display_form(tmp_path):
    # minisign shows the id byte-reversed vs the binary; a pin using the
    # human-readable form must still match.
    data = b"payload"
    key_id = bytes.fromhex("E32839579A8C02C8")
    pubkey, _le, sig_text = _make_minisign(data, key_id=key_id)
    target = tmp_path / "a.bin"
    target.write_bytes(data)
    big_endian_display = "C8028C9A573928E3"
    assert verify_file(target, sig_text, pubkey, key_id_hex=big_endian_display) == "signed by test"


def test_minisign_rejects_tampered_file(tmp_path):
    pubkey, key_id, sig_text = _make_minisign(b"original")
    target = tmp_path / "asset.bin"
    target.write_bytes(b"tampered!")
    with pytest.raises(MinisignError):
        verify_file(target, sig_text, pubkey, key_id_hex=key_id)


def test_minisign_rejects_wrong_key_id(tmp_path):
    data = b"payload"
    pubkey, _real, sig_text = _make_minisign(data)
    target = tmp_path / "asset.bin"
    target.write_bytes(data)
    with pytest.raises(MinisignError):
        verify_file(target, sig_text, pubkey, key_id_hex="DEADBEEFDEADBEEF")


def test_checksums_parse():
    text = "abc  file-a.zip\n" + "0" * 64 + "  file-b.tar.gz\n# comment\n"
    parsed = S.parse_checksums(text)
    assert parsed == {"file-b.tar.gz": "0" * 64}  # short hash on file-a is skipped


# --------------------------------------------------------------------------- #
# curated registry (the real config/tools.yaml)                                #
# --------------------------------------------------------------------------- #
def test_real_registry_loads_both_tools():
    reg = load_tool_registry(REPO_ROOT / "config" / "tools.yaml")
    slugs = {t.slug for t in reg.tools}
    assert {"cc-switch", "cc-switch-cli", "rtk"} <= slugs
    cc = reg.get("cc-switch")
    assert cc.verify.method == "minisign" and cc.verify.pubkey
    assert any(a.signed for a in cc.assets)
    rtk = reg.get("rtk")
    assert rtk.verify.method == "sha256sums" and rtk.verify.checksums_asset == "checksums.txt"


def test_minisign_tool_without_signed_asset_is_rejected():
    with pytest.raises(ValueError):
        ToolDefinition.model_validate({
            "slug": "bad", "name": "Bad", "repo": "o/r",
            "license": {"spdx": "MIT"},
            "verify": {"method": "minisign", "pubkey": "x"},
            "assets": [{"platform": "linux", "file": "a", "signed": False}],
        })


# --------------------------------------------------------------------------- #
# end-to-end sync -> stage -> promote, GitHub mocked with respx                 #
# --------------------------------------------------------------------------- #
def _release(tag, assets):
    return {
        "tag_name": tag,
        "published_at": "2026-08-06T04:39:34Z",
        "assets": [
            {"name": n, "size": len(c), "browser_download_url": f"https://dl/{n}"}
            for n, c in assets.items()
        ],
    }


@respx.mock
async def test_sync_sha256_tool_stages_then_promotes(tmp_path):
    payload = b"rtk-binary-bytes" * 50
    good = hashlib.sha256(payload).hexdigest()
    checksums = f"{good}  rtk-x86_64-apple-darwin.tar.gz\n"
    assets = {"rtk-x86_64-apple-darwin.tar.gz": payload, "checksums.txt": checksums.encode()}

    respx.get("https://api.github.com/repos/o/rtk/releases/latest").mock(
        return_value=httpx.Response(200, json=_release("v0.45.0", assets)))
    for name, content in assets.items():
        respx.get(f"https://dl/{name}").mock(return_value=httpx.Response(200, content=content))
    respx.get("https://raw.githubusercontent.com/o/rtk/HEAD/LICENSE").mock(
        return_value=httpx.Response(200, text="Apache-2.0 text"))

    tool = ToolDefinition.model_validate({
        "slug": "rtk", "name": "RTK", "repo": "o/rtk",
        "license": {"spdx": "Apache-2.0", "files": ["LICENSE"]},
        "verify": {"method": "sha256sums", "checksums_asset": "checksums.txt"},
        "assets": [{"platform": "macos", "arch": "x86_64",
                    "file": "rtk-x86_64-apple-darwin.tar.gz", "kind": "archive"}],
    })

    async with httpx.AsyncClient() as client:
        manifest = await S.sync_tool(tool, tools_dir=tmp_path, client=client)

    assert manifest["status"] == "candidate"
    asset = manifest["assets"][0]
    assert asset["verified"] is True and asset["verify"] == "sha256"
    assert manifest["license"]["files"] == ["UPSTREAM-LICENSE"]
    # gated: nothing published until promote
    assert S.published_version(tmp_path, "rtk") is None
    S.promote(tmp_path, "rtk", "0.45.0")
    assert S.published_version(tmp_path, "rtk") == "0.45.0"
    assert S.load_manifest(tmp_path, "rtk", "0.45.0")["status"] == "published"


@respx.mock
async def test_sync_minisign_tool_and_promote_blocks_on_failure(tmp_path):
    payload = b"cc-switch-installer" * 100
    pubkey, key_id, sig_text = _make_minisign(payload, trusted_comment="v3.19.2")
    assets = {
        "CC-Switch-v3.19.2-Linux-x86_64.AppImage": payload,
        "CC-Switch-v3.19.2-Linux-x86_64.AppImage.sig": sig_text.encode(),
    }
    respx.get("https://api.github.com/repos/o/cc/releases/latest").mock(
        return_value=httpx.Response(200, json=_release("v3.19.2", assets)))
    for name, content in assets.items():
        respx.get(f"https://dl/{name}").mock(return_value=httpx.Response(200, content=content))
    respx.get("https://raw.githubusercontent.com/o/cc/HEAD/LICENSE").mock(
        return_value=httpx.Response(200, text="MIT text"))

    tool = ToolDefinition.model_validate({
        "slug": "cc", "name": "CC", "repo": "o/cc",
        "license": {"spdx": "MIT", "files": ["LICENSE"]},
        "verify": {"method": "minisign", "pubkey": pubkey, "key_id": key_id},
        "assets": [{"platform": "linux", "arch": "x86_64",
                    "file": "CC-Switch-v{version}-Linux-x86_64.AppImage",
                    "kind": "portable", "signed": True}],
    })

    async with httpx.AsyncClient() as client:
        manifest = await S.sync_tool(tool, tools_dir=tmp_path, client=client)
    assert manifest["assets"][0]["verified"] is True
    assert manifest["assets"][0]["trusted_comment"] == "v3.19.2"
    S.promote(tmp_path, "cc", "3.19.2")
    assert S.published_version(tmp_path, "cc") == "3.19.2"

    # now corrupt the manifest to simulate a failed verify and re-promote
    m = S.load_manifest(tmp_path, "cc", "3.19.2")
    m["assets"][0]["verified"] = False
    S._write_json(S.version_dir(tmp_path, "cc", "3.19.2") / "manifest.json", m)
    with pytest.raises(S.SyncError):
        S.promote(tmp_path, "cc", "3.19.2")


def test_the_terminal_switcher_pins_versioned_asset_names():
    """release ปล่อยชื่อซ้ำสองชุด มีเวอร์ชันกับไม่มี — ไบต์เดียวกันเป๊ะ

    มิเรอร์ชื่อที่ไม่มีเวอร์ชันก็ได้ไฟล์ถูก แต่พอลงดิสก์แล้วแยกไม่ออกว่าอันไหน
    รุ่นไหน ซึ่งพังตอนที่ต้องบอกลูกค้าว่าเขาถืออะไรอยู่
    """
    cli = load_tool_registry(REPO_ROOT / "config" / "tools.yaml").get("cc-switch-cli")
    assert cli is not None
    assert cli.verify.method == "sha256sums"
    assert cli.verify.checksums_asset == "checksums.txt"
    for asset in cli.assets:
        assert "{version}" in asset.file, f"{asset.file} ไม่มีเวอร์ชันในชื่อ"
        # ตัวนี้ไม่มีคีย์เซ็นจากต้นทาง ต่างจากรุ่นเดสก์ท็อป — อ้างว่า signed
        # ไม่ได้ ไม่งั้นหน้าดาวน์โหลดจะบอกลูกค้าว่ามีอะไรที่พิสูจน์ไม่ได้
        assert asset.signed is False


def test_the_desktop_switcher_never_claims_proof_it_does_not_have():
    """ต้นทางเซ็นแค่บางไฟล์ · .deb กับ portable zip ไม่ได้เซ็น

    ปักหมุด `signed: true` ผิดตัวเมื่อไหร่ ตัว sync จะพยายามหา .sig ที่ไม่มีอยู่
    แล้วรายงานว่า FAILED — หรือแย่กว่านั้นคือหน้าดาวน์โหลดบอกลูกค้าว่าไฟล์นี้
    พิสูจน์ที่มาได้ ทั้งที่พิสูจน์ไม่ได้
    """
    cc = load_tool_registry(REPO_ROOT / "config" / "tools.yaml").get("cc-switch")
    signed = {a.file for a in cc.assets if a.signed}
    unsigned = {a.file for a in cc.assets if not a.signed}
    assert any("AppImage" in f for f in signed), "AppImage ต้นทางเซ็นให้"
    assert any(".msi" in f for f in signed), ".msi ต้นทางเซ็นให้"
    for name in (".deb", "Portable.zip", ".dmg"):
        assert any(name in f for f in unsigned), f"{name} ต้นทางไม่ได้เซ็น"
        assert not any(name in f for f in signed), f"{name} ห้ามอ้างว่าเซ็นแล้ว"


def test_only_release_assets_can_be_mirrored():
    """ทุกอย่างใน registry ต้องเป็นไฟล์ใน GitHub release

    เขียนไว้เพราะเคยพิจารณาเพิ่มเครื่องมือที่แจกด้วยการ clone แล้ว npm install
    ซึ่งระบบนี้รับไม่ได้เลย — ไม่มี asset ให้ยืนยัน และไม่มีอะไรให้ pin
    """
    reg = load_tool_registry(REPO_ROOT / "config" / "tools.yaml")
    for tool in reg.tools:
        if tool.assets:
            assert tool.verify is not None, f"{tool.slug}: มีไฟล์แต่ไม่มีวิธีตรวจ"
            assert tool.verify.method in {"minisign", "sha256sums"}
        else:
            # ตัวที่แจกผ่าน npm/Docker ลงรายการได้ แต่ต้องบอกวิธีติดตั้ง
            # ไม่งั้นการ์ดจะไม่มีอะไรให้คนกดต่อ
            assert tool.install, f"{tool.slug}: ไม่มีทั้งไฟล์และวิธีติดตั้ง"
