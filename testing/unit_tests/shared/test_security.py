# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2024-2026 Metixel Photoframe Contributors
"""Tests for the stdlib security helpers (hashing, constant-time compare, PIN format)."""

from __future__ import annotations

import pytest

from metixel.shared.security import (
    PIN_MAX_LENGTH,
    PIN_MIN_LENGTH,
    constant_time_compare,
    generate_secret,
    hash_secret,
    validate_pin_format,
    verify_secret,
)


class TestHashSecret:
    def test_roundtrip(self):
        h = hash_secret("correct horse battery staple")
        assert verify_secret("correct horse battery staple", h) is True

    def test_wrong_password_rejected(self):
        h = hash_secret("secret")
        assert verify_secret("wrong", h) is False

    def test_salted(self):
        # Two hashes of the same secret differ (random salt).
        assert hash_secret("same") != hash_secret("same")

    def test_not_plaintext(self):
        h = hash_secret("mypassword")
        assert "mypassword" not in h

    def test_empty_stored_rejected(self):
        assert verify_secret("anything", "") is False

    def test_malformed_stored_rejected(self):
        assert verify_secret("anything", "not-a-valid-hash") is False
        assert verify_secret("anything", "scrypt$bad") is False

    def test_empty_secret(self):
        h = hash_secret("")
        assert verify_secret("", h) is True


class TestConstantTimeCompare:
    def test_equal(self):
        assert constant_time_compare("1234", "1234") is True

    def test_not_equal(self):
        assert constant_time_compare("1234", "1235") is False

    def test_different_lengths(self):
        assert constant_time_compare("1234", "12345") is False


class TestGenerateSecret:
    def test_unique_and_hex(self):
        a = generate_secret()
        b = generate_secret()
        assert a != b
        assert len(a) == 64  # 32 bytes hex
        int(a, 16)  # must be valid hex


class TestValidatePinFormat:
    @pytest.mark.parametrize("pin", ["1234", "12345", "123456"])
    def test_valid_lengths(self, pin):
        assert validate_pin_format(pin) is True

    @pytest.mark.parametrize("pin", ["", "123", "1234567", "12a4", "12 4", "abcd"])
    def test_invalid(self, pin):
        assert validate_pin_format(pin) is False

    def test_leading_zeros_preserved(self):
        # "0123" is a valid 4-digit PIN (string semantics).
        assert validate_pin_format("0123") is True

    def test_constants(self):
        assert PIN_MIN_LENGTH == 4
        assert PIN_MAX_LENGTH == 6