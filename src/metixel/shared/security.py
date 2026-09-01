# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2024-2026 Metixel Photoframe Contributors
"""Password / PIN hashing and validation helpers (stdlib only).

These are pure functions with no third-party dependencies, so they can be
used from core business logic without violating the Clean Architecture rule
that core never imports libraries directly.

Design notes
------------
* Passwords and PINs are stored as **salted hashes** (``hashlib.scrypt``),
  never plaintext.  ``scrypt`` is memory-hard and is a good fit for a
  low-power Pi because it is fast enough to verify on every login while
  still resisting offline brute force.
* All comparisons use :func:`hmac.compare_digest` (constant-time) to resist
  timing attacks.
* PINs are handled as **strings** (never ints) so leading zeros are
  preserved — mirroring how ``network_controller.py`` treats its ``04d``
  AP-fallback PIN.
"""

from __future__ import annotations

import hashlib
import hmac
import secrets

__all__ = [
    "hash_secret",
    "verify_secret",
    "constant_time_compare",
    "generate_secret",
    "validate_pin_format",
    "PIN_MIN_LENGTH",
    "PIN_MAX_LENGTH",
]

#: Minimum accepted screen-PIN length (digits).
PIN_MIN_LENGTH = 4
#: Maximum accepted screen-PIN length (digits).
PIN_MAX_LENGTH = 6

#: scrypt parameters.  ``n`` is a power of two; 2**14 is a reasonable
#: cost for a low-power Pi while still being memory-hard.
_SCRYPT_N = 2**14
_SCRYPT_R = 8
_SCRYPT_P = 1
_SALT_BYTES = 16
_HASH_BYTES = 32

#: Storage format: ``scrypt$<n>$<r>$<p>$<salt_hex>$<hash_hex>``
_HASH_PREFIX = "scrypt"


def hash_secret(secret: str) -> str:
    """Return a salted, self-describing hash of *secret*.

    The returned string embeds the scrypt parameters and salt so
    :func:`verify_secret` needs no external state to check it.
    """
    salt = secrets.token_bytes(_SALT_BYTES)
    digest = hashlib.scrypt(
        secret.encode("utf-8"),
        salt=salt,
        n=_SCRYPT_N,
        r=_SCRYPT_R,
        p=_SCRYPT_P,
        dklen=_HASH_BYTES,
    )
    return f"{_HASH_PREFIX}${_SCRYPT_N}${_SCRYPT_R}${_SCRYPT_P}${salt.hex()}${digest.hex()}"


def verify_secret(secret: str, stored: str) -> bool:
    """Constant-time check of *secret* against a hash from :func:`hash_secret`.

    Returns ``False`` for any malformed *stored* value rather than raising,
    so a corrupt config never crashes the login path.
    """
    if not stored:
        return False
    try:
        prefix, n, r, p, salt_hex, hash_hex = stored.split("$")
        if prefix != _HASH_PREFIX:
            return False
        salt = bytes.fromhex(salt_hex)
        expected = bytes.fromhex(hash_hex)
    except (ValueError, TypeError):
        return False
    try:
        digest = hashlib.scrypt(
            secret.encode("utf-8"),
            salt=salt,
            n=int(n),
            r=int(r),
            p=int(p),
            dklen=len(expected),
        )
    except (ValueError, TypeError):
        return False
    return hmac.compare_digest(digest, expected)


def constant_time_compare(a: str, b: str) -> bool:
    """Constant-time string comparison (wraps :func:`hmac.compare_digest`)."""
    return hmac.compare_digest(a, b)


def generate_secret() -> str:
    """Return a cryptographically random hex secret (for the session cookie)."""
    return secrets.token_hex(32)


def validate_pin_format(pin: str) -> bool:
    """Return ``True`` if *pin* is 4-6 digits (and non-empty).

    PINs are compared/stored as strings so leading zeros are preserved.
    """
    if not pin:
        return False
    if not pin.isdigit():
        return False
    return PIN_MIN_LENGTH <= len(pin) <= PIN_MAX_LENGTH
