"""Passphrase encryption for export bundles.

The DB aggregates private bookmarks, saved posts, and archived page copies;
the copies people move *off*-machine (export bundles) are the most exposed.
This module encrypts any export stream with a passphrase so `dbs export
--encrypt` output is safe to park on untrusted storage.

Format (magic ``DBSENC01``)::

    DBSENC01 || salt(16) || nonce_prefix(8) || frame*

    frame = len(u32 BE) || AES-256-GCM ciphertext
    nonce = nonce_prefix(8) || counter(u32 BE)     # unique per frame
    AAD   = b"dbs-final" on the last frame, b"dbs" otherwise

Design notes:

* Key = scrypt(passphrase, salt, n=2**14, r=8, p=1) — memory-hard, so an
  offline brute-force of a weak passphrase stays expensive.
* Chunked (1 MiB plaintext per frame) so multi-GB archives stream through
  without buffering; the counter nonce makes frame *reordering* fail
  authentication, and the ``dbs-final`` AAD on the terminator frame makes
  *truncation* detectable — a prefix of a valid file never decrypts clean.
* The passphrase arrives via an env var (default ``DBS_EXPORT_PASSPHRASE``),
  never argv — command lines leak into shell history and process listings.
* ``cryptography`` is the optional ``[crypto]`` extra; imported lazily with
  an actionable error so the core stays dependency-light.
"""

from __future__ import annotations

import os
import struct
from pathlib import Path
from typing import Any, BinaryIO

from .core.errors import ConfigError

MAGIC = b"DBSENC01"
DEFAULT_PASSPHRASE_ENV = "DBS_EXPORT_PASSPHRASE"
_CHUNK = 1 << 20  # 1 MiB plaintext per frame
_AAD = b"dbs"
_AAD_FINAL = b"dbs-final"
_SCRYPT_N, _SCRYPT_R, _SCRYPT_P = 2**14, 8, 1


def _aesgcm(passphrase: str, salt: bytes):
    try:
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM
        from cryptography.hazmat.primitives.kdf.scrypt import Scrypt
    except ModuleNotFoundError as exc:
        raise ConfigError(
            "Encrypted exports need the optional 'crypto' dependency: "
            "pip install 'daily-backup-system[crypto]'"
        ) from exc
    kdf = Scrypt(salt=salt, length=32, n=_SCRYPT_N, r=_SCRYPT_R, p=_SCRYPT_P)
    return AESGCM(kdf.derive(passphrase.encode("utf-8")))


def is_encrypted(path: str | Path) -> bool:
    try:
        with open(path, "rb") as fh:
            return fh.read(len(MAGIC)) == MAGIC
    except OSError:
        return False


class EncryptingWriter:
    """A write-only binary stream that encrypts what flows through it.

    Exporters write plain bytes to their ``out`` handle; wrapping that handle
    with this class is the whole integration — no exporter knows encryption
    exists. ``close()`` writes the final (possibly empty) frame carrying the
    ``dbs-final`` AAD, which is what makes truncation detectable, so a writer
    that is never closed produces an *invalid* file rather than a silently
    short one.
    """

    def __init__(self, out: BinaryIO, passphrase: str) -> None:
        salt = os.urandom(16)
        self._prefix = os.urandom(8)
        self._gcm = _aesgcm(passphrase, salt)
        self._out = out
        self._counter = 0
        self._buf = bytearray()
        self._closed = False
        out.write(MAGIC + salt + self._prefix)

    def write(self, data: bytes) -> int:
        self._buf.extend(data)
        while len(self._buf) >= _CHUNK:
            self._emit(bytes(self._buf[:_CHUNK]), final=False)
            del self._buf[:_CHUNK]
        return len(data)

    def _emit(self, plaintext: bytes, *, final: bool) -> None:
        nonce = self._prefix + struct.pack(">I", self._counter)
        self._counter += 1
        ct = self._gcm.encrypt(nonce, plaintext, _AAD_FINAL if final else _AAD)
        self._out.write(struct.pack(">I", len(ct)) + ct)

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._emit(bytes(self._buf), final=True)
        self._buf.clear()

    # Duck-typing conveniences: io.TextIOWrapper (the csv exporter) probes
    # readable/writable/seekable; zipfile wraps un-tellable streams itself.
    def flush(self) -> None:  # buffered frames flush on boundaries/close
        self._out.flush()

    def writable(self) -> bool:
        return True

    @property
    def closed(self) -> bool:
        return self._closed

    def readable(self) -> bool:
        return False

    def seekable(self) -> bool:
        return False

    def __enter__(self) -> "EncryptingWriter":
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()


def decrypt_stream(src: BinaryIO, dest: BinaryIO, passphrase: str) -> int:
    """Decrypt ``src`` into ``dest``; returns plaintext bytes written.

    Raises :class:`ConfigError` on a wrong passphrase, corruption, frame
    reordering, or truncation (missing final frame) — never partial silence.
    """
    header = src.read(len(MAGIC) + 16 + 8)
    if len(header) < len(MAGIC) + 24 or not header.startswith(MAGIC):
        raise ConfigError("not a dbs-encrypted file (bad magic header)")
    salt = header[len(MAGIC):len(MAGIC) + 16]
    prefix = header[len(MAGIC) + 16:]
    gcm = _aesgcm(passphrase, salt)

    from cryptography.exceptions import InvalidTag

    counter = 0
    total = 0
    saw_final = False
    while True:
        head = src.read(4)
        if not head:
            break
        if len(head) < 4:
            raise ConfigError("encrypted file is truncated mid-frame")
        (clen,) = struct.unpack(">I", head)
        ct = src.read(clen)
        if len(ct) < clen:
            raise ConfigError("encrypted file is truncated mid-frame")
        nonce = prefix + struct.pack(">I", counter)
        counter += 1
        # Try the terminator AAD first only when this could be the last frame;
        # cheaper: try normal, fall back to final.
        try:
            pt = gcm.decrypt(nonce, ct, _AAD)
        except InvalidTag:
            try:
                pt = gcm.decrypt(nonce, ct, _AAD_FINAL)
            except InvalidTag as exc:
                raise ConfigError(
                    "decryption failed — wrong passphrase, or the file is "
                    "corrupt/tampered"
                ) from exc
            saw_final = True
        dest.write(pt)
        total += len(pt)
        if saw_final:
            break
    if not saw_final:
        raise ConfigError(
            "encrypted file is truncated (missing final frame) — refuse to "
            "treat a prefix as the whole backup"
        )
    if src.read(1):
        raise ConfigError("trailing data after the final encrypted frame")
    return total


def decrypt_file(src: str | Path, dest: str | Path, passphrase: str) -> int:
    with open(src, "rb") as fin, open(dest, "wb") as fout:
        return decrypt_stream(fin, fout, passphrase)


def resolve_passphrase(
    secret_store: dict[str, str] | Any, env_name: str = DEFAULT_PASSPHRASE_ENV
) -> str:
    value = (secret_store or {}).get(env_name, "") if hasattr(secret_store, "get") else ""
    if not value:
        value = os.environ.get(env_name, "")
    if not value:
        raise ConfigError(
            f"no passphrase: set {env_name} in the environment or .env "
            f"(never on the command line)"
        )
    return value


__all__ = [
    "DEFAULT_PASSPHRASE_ENV",
    "EncryptingWriter",
    "MAGIC",
    "decrypt_file",
    "decrypt_stream",
    "is_encrypted",
    "resolve_passphrase",
]
