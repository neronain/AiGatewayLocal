"""Minimal minisign signature verification (Ed25519), pure-Python.

Verifies a detached minisign ``.sig`` against a *pinned* public key. We store
the publisher's public key in the tools registry (see ``config/tools.yaml``),
so this proves **authenticity** — the file was signed by the key we vetted —
not merely that a checksum matches something we also downloaded.

Supports both minisign variants, chosen by the 2-byte algorithm tag in the
signature: ``Ed`` (legacy, signs the raw file) and ``ED`` (prehashed, signs
BLAKE2b-512 of the file). Tauri's release signer emits the prehashed form.

No external process: verification uses ``cryptography`` (already a dependency),
so it runs the same inside the hardened Docker image where ``minisign`` is absent.
"""

from __future__ import annotations

import base64
import hashlib
from dataclasses import dataclass
from pathlib import Path

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey


class MinisignError(Exception):
    """A signature failed to parse or did not verify against the pinned key."""


@dataclass(frozen=True)
class Signature:
    algorithm: bytes  # b"Ed" (legacy) or b"ED" (prehashed)
    key_id: bytes  # 8 bytes
    signature: bytes  # 64-byte Ed25519 signature
    trusted_comment: str
    global_signature: bytes  # 64-byte Ed25519 signature over sig+trusted_comment


def _b64(line: str) -> bytes:
    try:
        return base64.b64decode(line.strip(), validate=True)
    except Exception as exc:  # noqa: BLE001 - re-raised as our own error
        raise MinisignError(f"invalid base64 payload: {exc}") from exc


def _payload_lines(text: str) -> list[str]:
    """Non-empty lines that are not minisign comment headers."""
    out = []
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        if stripped.startswith("untrusted comment:") or stripped.startswith("trusted comment:"):
            continue
        out.append(stripped)
    return out


def parse_public_key(text: str) -> tuple[bytes, Ed25519PublicKey]:
    """Parse a minisign public key.

    Accepts either the full two-line ``.pub`` file or the bare base64 payload
    (what we pin in the registry). Returns ``(key_id, public_key)``.
    """
    payload = _payload_lines(text)
    if not payload:
        raise MinisignError("empty public key")
    raw = _b64(payload[0])
    if len(raw) != 42:
        raise MinisignError(f"public key must be 42 bytes, got {len(raw)}")
    algorithm, key_id, key = raw[:2], raw[2:10], raw[10:]
    if algorithm != b"Ed":
        raise MinisignError(f"unsupported public-key algorithm {algorithm!r}")
    return key_id, Ed25519PublicKey.from_public_bytes(key)


def _normalize_sig_text(text: str) -> str:
    """Unwrap a Tauri-style signature.

    Tauri's release signer base64-encodes the *whole* minisign file into a single
    line. Standard ``minisign`` .sig files start with an ``untrusted comment:``
    header; if this one does not but decodes to such text, use the decoded form.
    """
    stripped = text.strip()
    if stripped.startswith("untrusted comment:"):
        return text
    try:
        decoded = base64.b64decode(stripped, validate=True).decode("utf-8", "replace")
    except Exception:  # noqa: BLE001 - not wrapped; fall through to the raw text
        return text
    return decoded if decoded.lstrip().startswith("untrusted comment:") else text


def parse_signature(text: str) -> Signature:
    """Parse a detached minisign ``.sig`` file (4 significant lines).

    Accepts both plain minisign and Tauri's base64-wrapped form.
    """
    text = _normalize_sig_text(text)
    payload = _payload_lines(text)
    # The trusted-comment line is a "comment" header, so recover it separately.
    trusted_comment = ""
    for line in text.splitlines():
        if line.strip().startswith("trusted comment:"):
            trusted_comment = line.split(":", 1)[1].strip()
            break
    if len(payload) < 2:
        raise MinisignError("signature file is missing its base64 blocks")
    blob = _b64(payload[0])
    if len(blob) != 74:
        raise MinisignError(f"signature block must be 74 bytes, got {len(blob)}")
    algorithm, key_id, signature = blob[:2], blob[2:10], blob[10:]
    global_signature = _b64(payload[1])
    if len(global_signature) != 64:
        raise MinisignError("global signature must be 64 bytes")
    return Signature(algorithm, key_id, signature, trusted_comment, global_signature)


def _signed_bytes(algorithm: bytes, data: bytes) -> bytes:
    if algorithm == b"ED":  # prehashed
        return hashlib.blake2b(data).digest()  # 64-byte digest
    if algorithm == b"Ed":  # legacy, signs the file directly
        return data
    raise MinisignError(f"unknown signature algorithm {algorithm!r}")


def verify_file(
    path: Path, sig_text: str, pinned_pubkey: str, *, key_id_hex: str | None = None
) -> str:
    """Verify ``path`` against ``sig_text`` using the pinned public key.

    Returns the signature's trusted comment on success; raises ``MinisignError``
    on any mismatch. If ``key_id_hex`` is given it must match the signature's
    key id, catching a file signed by a *different* (even if valid) key.
    """
    pk_key_id, public_key = parse_public_key(pinned_pubkey)
    sig = parse_signature(sig_text)
    if sig.key_id != pk_key_id:
        raise MinisignError("signature key id does not match the pinned public key")
    if key_id_hex:
        want = key_id_hex.replace("0x", "").upper()
        # minisign shows the key id big-endian in comments but stores it
        # little-endian in the binary; accept either so a pin can use the
        # human-readable "minisign public key: <ID>" form.
        forms = {sig.key_id.hex().upper(), sig.key_id[::-1].hex().upper()}
        if want not in forms:
            raise MinisignError("signature key id does not match the expected key id")

    data = Path(path).read_bytes()
    try:
        public_key.verify(sig.signature, _signed_bytes(sig.algorithm, data))
    except InvalidSignature as exc:
        raise MinisignError("file signature does not verify against the pinned key") from exc
    # Bind the trusted comment: minisign signs (signature || trusted_comment).
    try:
        public_key.verify(sig.global_signature, sig.signature + sig.trusted_comment.encode())
    except InvalidSignature as exc:
        raise MinisignError("trusted-comment (global) signature is invalid") from exc
    return sig.trusted_comment
