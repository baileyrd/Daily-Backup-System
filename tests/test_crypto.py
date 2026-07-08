"""Encrypted exports: format round-trip, tamper/truncation detection,
service/CLI integration."""

from __future__ import annotations

import io
import json

import pytest

from dbs.config import Config
from dbs.core.errors import ConfigError
from dbs.core.registry import ConnectorRegistry
from dbs.core.service import BackupService
from dbs.crypto import (
    EncryptingWriter,
    decrypt_file,
    decrypt_stream,
    is_encrypted,
    resolve_passphrase,
)
from dbs.export.base import ExportQuery
from dbs.storage.base import PreparedItem

PASS = "correct horse battery staple"


def _roundtrip(payload: bytes, passphrase=PASS) -> bytes:
    enc = io.BytesIO()
    w = EncryptingWriter(enc, PASS)
    w.write(payload)
    w.close()
    enc.seek(0)
    out = io.BytesIO()
    decrypt_stream(enc, out, passphrase)
    return out.getvalue()


def test_round_trip_across_chunk_boundaries():
    payload = bytes(range(256)) * (12 * 1024)  # ~3 MiB -> multiple frames
    assert _roundtrip(payload) == payload


def test_empty_payload_round_trips():
    assert _roundtrip(b"") == b""


def test_wrong_passphrase_fails_loudly():
    with pytest.raises(ConfigError, match="wrong passphrase"):
        _roundtrip(b"secret", passphrase="nope")


def test_truncation_is_detected():
    enc = io.BytesIO()
    w = EncryptingWriter(enc, PASS)
    w.write(b"x" * (2 << 20))  # two full frames
    w.close()
    data = enc.getvalue()
    truncated = io.BytesIO(data[: len(data) // 2])
    with pytest.raises(ConfigError, match="truncated"):
        decrypt_stream(truncated, io.BytesIO(), PASS)


def test_missing_final_frame_is_detected():
    # Drop exactly the terminator frame: a "clean-looking" prefix must fail.
    enc = io.BytesIO()
    w = EncryptingWriter(enc, PASS)
    w.write(b"y" * (1 << 20))  # exactly one full frame
    w.close()
    data = enc.getvalue()
    # Header is 32 bytes; first frame is 4 + (1 MiB + 16) bytes.
    cut = 32 + 4 + (1 << 20) + 16
    with pytest.raises(ConfigError, match="missing final frame"):
        decrypt_stream(io.BytesIO(data[:cut]), io.BytesIO(), PASS)


def test_trailing_garbage_is_detected():
    enc = io.BytesIO()
    w = EncryptingWriter(enc, PASS)
    w.write(b"z")
    w.close()
    tampered = io.BytesIO(enc.getvalue() + b"EXTRA")
    with pytest.raises(ConfigError, match="trailing data"):
        decrypt_stream(tampered, io.BytesIO(), PASS)


def test_resolve_passphrase_prefers_store_then_env(monkeypatch):
    monkeypatch.delenv("DBS_EXPORT_PASSPHRASE", raising=False)
    assert resolve_passphrase({"DBS_EXPORT_PASSPHRASE": "s"}) == "s"
    monkeypatch.setenv("DBS_EXPORT_PASSPHRASE", "e")
    assert resolve_passphrase({}) == "e"
    monkeypatch.delenv("DBS_EXPORT_PASSPHRASE")
    with pytest.raises(ConfigError, match="no passphrase"):
        resolve_passphrase({})


# --- service integration ------------------------------------------------------


def _seed(storage):
    src = storage.upsert_source("rd", "raindrop", "test:raindrop", "{}", 1)
    run = storage.begin_run(src.id, "test:raindrop", "full", None)
    storage.upsert_items(src.id, run, [
        PreparedItem("1", "link", "First", "https://a", "note", ["x"],
                     "2024-01-01T00:00:00Z", "2024-01-01T00:00:00Z", "h1",
                     json.dumps({"_id": 1}), False),
    ])


def _svc(storage, tmp_path, secret_store=None):
    reg = ConnectorRegistry()
    reg.discover()
    if secret_store is None:
        secret_store = {"DBS_EXPORT_PASSPHRASE": PASS}
    return BackupService(
        storage, Config(base_dir=tmp_path), reg, secret_store=secret_store,
    )


@pytest.mark.parametrize("fmt,ext", [("ndjson", ".ndjson"), ("archive", ".zip"), ("csv", ".csv")])
def test_encrypted_export_decrypts_to_the_plain_export(storage, tmp_path, fmt, ext):
    _seed(storage)
    svc = _svc(storage, tmp_path)
    plain = tmp_path / f"plain{ext}"
    enc = tmp_path / f"enc{ext}.enc"
    svc.export(ExportQuery(), fmt, plain)
    svc.export(ExportQuery(), fmt, enc, encrypt=True)
    assert is_encrypted(enc) and not is_encrypted(plain)

    out = tmp_path / f"roundtrip{ext}"
    decrypt_file(enc, out, PASS)
    if fmt == "archive":
        # Zips embed timestamps; compare the decrypted zip's item payloads.
        import zipfile
        with zipfile.ZipFile(out) as zf:
            got = zf.read("items/rd.ndjson")
        with zipfile.ZipFile(plain) as zf:
            want = zf.read("items/rd.ndjson")
        assert got == want
    else:
        assert out.read_bytes() == plain.read_bytes()


def test_restore_reads_encrypted_bundles_directly(storage, tmp_path):
    from dbs.storage.sqlite import SqliteStorage

    _seed(storage)
    enc = tmp_path / "bundle.zip.enc"
    _svc(storage, tmp_path).export(ExportQuery(), "archive", enc, encrypt=True)

    fresh = SqliteStorage(tmp_path / "fresh.sqlite3")
    fresh.migrate()
    try:
        report = _svc(fresh, tmp_path).restore(enc)
        assert report.created == 1
        assert report.path.endswith("bundle.zip.enc")  # names the user's file
        # Without a passphrase the restore refuses, loudly.
        with pytest.raises(ConfigError, match="no passphrase"):
            _svc(fresh, tmp_path, secret_store={}).restore(enc)
    finally:
        fresh.close()


def test_encrypted_export_requires_a_passphrase(storage, tmp_path):
    _seed(storage)
    svc = _svc(storage, tmp_path, secret_store={})
    with pytest.raises(ConfigError, match="no passphrase"):
        svc.export(ExportQuery(), "ndjson", tmp_path / "x.enc", encrypt=True)
    assert not (tmp_path / "x.enc").exists()  # atomic write left nothing behind
