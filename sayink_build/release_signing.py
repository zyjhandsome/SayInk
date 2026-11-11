"""Optional Authenticode binary signing and Ed25519 detached release signatures.

Supply-chain hooks for release builds:
- ``SAYINK_SIGNTOOL_CMD`` signs ``SayInk.exe`` and ``SayInk-Setup-*.exe`` via
  Windows ``signtool`` / Azure Trusted Signing / SignPath when configured, and
  stays a clean no-op when unset.
- ``SAYINK_RELEASE_PRIVATE_KEY`` (or CLI ``python -m sayink_build.release_signing``)
  emits detached RFC 8032 Ed25519 ``.sig`` files verified by ``sayink.updater``.
"""

from __future__ import annotations

import argparse
import hashlib
import os
import re
import secrets
import shlex
import subprocess
import sys
from pathlib import Path
from typing import Callable

from sayink.updater import (
    _ED_B,
    _ED_L,
    _ed_encode_point,
    _ed_mul,
    canonical_release_manifest,
    parse_detached_signature,
    verify_ed25519_signature,
)

SIGNTOOL_ENV_VAR = "SAYINK_SIGNTOOL_CMD"
PRIVATE_KEY_ENV_VAR = "SAYINK_RELEASE_PRIVATE_KEY"


class SigningError(RuntimeError):
    """Raised when an explicitly configured signing step fails."""


def _strip_wrapping_quotes(token: str) -> str:
    if len(token) >= 2 and token[0] == token[-1] and token[0] in ('"', "'"):
        return token[1:-1]
    return token


def build_signtool_argv(template: str, target: Path) -> list[str]:
    """Turn ``SAYINK_SIGNTOOL_CMD`` into an ``argv`` list without ``shell=True``.

    Supports ``{file}`` or ``$f`` placeholders; when neither appears, appends
    ``str(target)`` as the final argument.
    """
    raw = (template or "").strip()
    if not raw:
        raise ValueError("empty signing command template")
    tokens = [_strip_wrapping_quotes(tok) for tok in shlex.split(raw, posix=(os.name != "nt"))]
    if not tokens:
        raise ValueError("empty signing command tokens")
    target_str = str(target)
    replaced = False
    argv: list[str] = []
    for tok in tokens:
        if "{file}" in tok or "$f" in tok:
            argv.append(tok.replace("{file}", target_str).replace("$f", target_str))
            replaced = True
        else:
            argv.append(tok)
    if not replaced:
        argv.append(target_str)
    return argv


def inno_signtool_command(template: str) -> str:
    """Format ``SAYINK_SIGNTOOL_CMD`` for Inno Setup's ``/Ssayinksign=...``."""
    raw = (template or "").strip()
    if not raw:
        return ""
    if "{file}" in raw:
        return raw.replace("{file}", "$q$f$q")
    if "$f" in raw:
        return raw
    return f"{raw} $q$f$q"


def sign_file_if_configured(
    target: Path,
    *,
    env: dict[str, str] | None = None,
    run: Callable[..., subprocess.CompletedProcess] = subprocess.run,
) -> bool:
    """Sign ``target`` when ``SAYINK_SIGNTOOL_CMD`` is set; no-op returning ``False`` otherwise."""
    environ = os.environ if env is None else env
    template = (environ.get(SIGNTOOL_ENV_VAR) or "").strip()
    if not template:
        return False
    path = Path(target)
    if not path.is_file():
        raise SigningError(f"Cannot sign missing file: {path}")
    argv = build_signtool_argv(template, path)
    try:
        result = run(
            argv,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
        )
    except OSError as exc:
        raise SigningError(f"Failed to execute signing command `{argv[0]}`: {exc}") from exc
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "").strip()
        raise SigningError(
            f"Signing failed for {path.name} (exit {result.returncode}): {detail}"
        )
    print(f"  [OK] Signed {path.name}")
    return True


def _parse_private_seed(private_key_hex: str) -> bytes:
    cleaned = (private_key_hex or "").strip()
    if cleaned.lower().startswith("ed25519:"):
        cleaned = cleaned.split(":", 1)[1].strip()
    if not re.fullmatch(r"[0-9a-fA-F]{64}", cleaned):
        raise SigningError("Ed25519 private seed must be 64 hex characters (32 bytes)")
    return bytes.fromhex(cleaned)


def ed25519_public_key_from_seed(seed: bytes) -> bytes:
    """Derive 32-byte RFC 8032 Ed25519 public key from 32-byte private seed."""
    if len(seed) != 32:
        raise ValueError("Ed25519 seed must be 32 bytes")
    digest = hashlib.sha512(seed).digest()
    scalar = int.from_bytes(digest[:32], "little")
    scalar &= (1 << 254) - 8
    scalar |= 1 << 254
    return _ed_encode_point(_ed_mul(_ED_B, scalar))


def ed25519_sign(seed: bytes, message: bytes) -> bytes:
    """Compute 64-byte deterministic RFC 8032 Ed25519 signature."""
    if len(seed) != 32:
        raise ValueError("Ed25519 seed must be 32 bytes")
    digest = hashlib.sha512(seed).digest()
    scalar = int.from_bytes(digest[:32], "little")
    scalar &= (1 << 254) - 8
    scalar |= 1 << 254
    prefix = digest[32:]
    pub = _ed_encode_point(_ed_mul(_ED_B, scalar))
    r_int = int.from_bytes(hashlib.sha512(prefix + message).digest(), "little") % _ED_L
    r_enc = _ed_encode_point(_ed_mul(_ED_B, r_int))
    k_int = int.from_bytes(hashlib.sha512(r_enc + pub + message).digest(), "little") % _ED_L
    s_int = (r_int + k_int * scalar) % _ED_L
    return r_enc + s_int.to_bytes(32, "little")


def generate_ed25519_keypair(seed: bytes | None = None) -> tuple[str, str]:
    """Return ``(private_seed_hex, public_key_hex)`` for RFC 8032 Ed25519."""
    raw_seed = secrets.token_bytes(32) if seed is None else bytes(seed)
    if len(raw_seed) != 32:
        raise ValueError("Ed25519 seed must be 32 bytes")
    pub = ed25519_public_key_from_seed(raw_seed)
    return raw_seed.hex(), pub.hex()


def file_sha256_and_size(path: Path) -> tuple[str, int]:
    hasher = hashlib.sha256()
    total = 0
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(256 * 1024), b""):
            hasher.update(chunk)
            total += len(chunk)
    return hasher.hexdigest(), total


def format_detached_signature(signature: bytes, asset_name: str) -> str:
    if len(signature) != 64:
        raise ValueError("Ed25519 signature must be 64 bytes")
    return f"ed25519:{signature.hex()}  {Path(asset_name).name}\n"


def sign_release_file(
    target: Path,
    private_key_hex: str,
    *,
    sig_path: Path | None = None,
) -> Path:
    """Create ``<target>.sig`` signing ``canonical_release_manifest`` with Ed25519."""
    path = Path(target)
    if not path.is_file():
        raise SigningError(f"Cannot sign missing release asset: {path}")
    seed = _parse_private_seed(private_key_hex)
    pub = ed25519_public_key_from_seed(seed)
    sha256, size = file_sha256_and_size(path)
    manifest = canonical_release_manifest(asset_name=path.name, sha256=sha256, size=size)
    sig = ed25519_sign(seed, manifest)
    if not verify_ed25519_signature(pub, manifest, sig):
        raise SigningError("Self-verification of generated Ed25519 signature failed")
    out = Path(sig_path) if sig_path is not None else path.with_name(path.name + ".sig")
    out.write_text(format_detached_signature(sig, path.name), encoding="utf-8")
    return out


def verify_release_file(
    target: Path,
    public_key_hex: str,
    *,
    sig_path: Path | None = None,
) -> bool:
    """Verify ``<target>`` against ``<target>.sig`` and ``public_key_hex``."""
    cleaned = (public_key_hex or "").strip()
    if cleaned.lower().startswith("ed25519:"):
        cleaned = cleaned.split(":", 1)[1].strip()
    if not re.fullmatch(r"[0-9a-fA-F]{64}", cleaned):
        return False
    path = Path(target)
    sig_file = Path(sig_path) if sig_path is not None else path.with_name(path.name + ".sig")
    if not path.is_file() or not sig_file.is_file():
        return False
    try:
        sig = parse_detached_signature(sig_file.read_bytes())
        sha256, size = file_sha256_and_size(path)
        manifest = canonical_release_manifest(asset_name=path.name, sha256=sha256, size=size)
    except Exception:
        return False
    return verify_ed25519_signature(bytes.fromhex(cleaned), manifest, sig)


def write_detached_signature_if_configured(
    target: Path,
    *,
    env: dict[str, str] | None = None,
) -> Path | None:
    """Emit ``<target>.sig`` when ``SAYINK_RELEASE_PRIVATE_KEY`` is set in ``env``."""
    environ = os.environ if env is None else env
    key_hex = (environ.get(PRIVATE_KEY_ENV_VAR) or "").strip()
    if not key_hex:
        return None
    out = sign_release_file(target, key_hex)
    print(f"  [OK] Wrote detached signature {out.name}")
    return out


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="SayInk Ed25519 detached release signing utility.")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("keygen", help="Generate a fresh RFC 8032 Ed25519 keypair.")

    sign_p = sub.add_parser("sign", help="Write <asset>.sig for a release binary.")
    sign_p.add_argument("file", type=Path, help="Release asset to sign.")
    sign_p.add_argument(
        "--private-key",
        default="",
        help=f"64-char hex seed (defaults to ${PRIVATE_KEY_ENV_VAR}).",
    )

    ver_p = sub.add_parser("verify", help="Verify <asset> against <asset>.sig.")
    ver_p.add_argument("file", type=Path, help="Release asset to verify.")
    ver_p.add_argument("--public-key", required=True, help="64-char hex Ed25519 public key.")
    ver_p.add_argument("--sig", type=Path, default=None, help="Optional explicit .sig path.")

    args = parser.parse_args(argv)
    if args.command == "keygen":
        priv_hex, pub_hex = generate_ed25519_keypair()
        print(f"{PRIVATE_KEY_ENV_VAR}={priv_hex}")
        print(f"RELEASE_SIGNING_PUBLIC_KEY={pub_hex}")
        return 0
    if args.command == "sign":
        key = args.private_key or os.environ.get(PRIVATE_KEY_ENV_VAR, "")
        sig_out = sign_release_file(args.file, key)
        print(sig_out)
        return 0
    if args.command == "verify":
        ok = verify_release_file(args.file, args.public_key, sig_path=args.sig)
        if not ok:
            print("Signature verification FAILED", file=sys.stderr)
            return 1
        print("Signature verified OK")
        return 0
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
