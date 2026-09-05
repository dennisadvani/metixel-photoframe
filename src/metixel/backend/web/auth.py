# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2024-2026 Metixel Photoframe Contributors
"""Web authentication + screen-PIN services.

These services own the *credential* logic for the optional web-dashboard
password (Part A) and the optional on-screen UI PIN (Part C).  They read
and write the ``web`` config section via the injected ``StateManager`` so
all persistence goes through the atomic config write path.

Three independent credentials (never conflated):
* **Web dashboard password** — ``web.password``.  Protects the web UI and
  ``/api/*``.  Entered in a browser login screen.
* **Device password** — SSH console + Samba share, kept in sync.  *Not*
  stored here; it lives in the system ``chpasswd`` / ``smbpasswd`` stores
  (see ``routes/security.py``).
* **Screen PIN** — ``web.screen_pin``.  Protects the future on-screen UI.
  Entered on the frame via a keypad.  Independent of the web password.

The services are injectable via the ``Ports`` dataclass (Clean Architecture
rule 13) but default to the real ``StateManager``-backed implementation, so
behaviour is identical when nothing is injected.
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Any, cast

from metixel.shared.security import (
    constant_time_compare,
    generate_secret,
    hash_secret,
    validate_pin_format,
    verify_secret,
)

logger = logging.getLogger(__name__)

#: Max failed web-login attempts before a cooldown (mirrors the AP PIN
#: lockout in ``network_controller.py``).
MAX_LOGIN_ATTEMPTS = 5
#: Cooldown (seconds) after too many failed web logins.
LOGIN_COOLDOWN_SECONDS = 300  # 5 minutes

#: Max failed screen-PIN attempts before a cooldown.
MAX_PIN_ATTEMPTS = 3
#: Cooldown (seconds) after too many failed PIN attempts.
PIN_COOLDOWN_SECONDS = 600  # 10 minutes

#: Upper bound for the screen-PIN unlock timeout (minutes).  Anything
#: higher defeats the PIN's purpose.
SCREEN_PIN_TIMEOUT_MAX_MINUTES = 1440  # 24 hours


class WebAuthService:
    """Owns the optional web-dashboard password and session signing secret.

    Reads/writes the ``web`` config section through a ``StateManager``-like
    object exposing ``config`` (a ``Config``) and ``update_config(section,
    values)``.  The real ``StateManager`` satisfies this; tests inject a
    lightweight fake.
    """

    def __init__(self, state: Any) -> None:
        self._state = state
        self._lock = threading.Lock()
        self._login_attempts = 0
        self._login_locked_until = 0.0

    # -- Config access -------------------------------------------------------

    def _web(self) -> dict[str, Any]:
        return cast(dict[str, Any], self._state.config.web)

    def is_enabled(self) -> bool:
        """Return whether a web password is set (auth required)."""
        return bool(self._web().get("password"))

    def session_timeout_minutes(self) -> int:
        """Return the web session idle timeout in minutes (0 = forever)."""
        try:
            return int(self._web().get("session_timeout_minutes", 30))
        except (TypeError, ValueError):
            return 30

    # -- Session signing secret ---------------------------------------------

    def ensure_secret(self) -> str:
        """Return the persisted session-signing secret, generating it if absent.

        The secret is stored in ``web.auth_secret`` so logins survive a
        backend restart.  Generated once and persisted atomically.
        """
        with self._lock:
            secret = self._web().get("auth_secret")
            if secret:
                return str(secret)
            secret = generate_secret()
            self._state.update_config("web", {"auth_secret": secret})
            logger.info("Generated and persisted web auth_secret")
            return secret

    def rotate_secret(self) -> str:
        """Generate a fresh session-signing secret (used on password clear)."""
        with self._lock:
            secret = generate_secret()
            self._state.update_config("web", {"auth_secret": secret})
            logger.info("Rotated web auth_secret")
            return secret

    # -- Password verification ----------------------------------------------

    def verify(self, password: str) -> bool:
        """Verify *password* against the stored hash, with lockout.

        Returns ``True`` on success.  On failure, increments the attempt
        counter and enforces a cooldown after ``MAX_LOGIN_ATTEMPTS``.
        """
        with self._lock:
            now = time.monotonic()
            if self._login_locked_until > 0 and now < self._login_locked_until:
                return False
            stored = self._web().get("password")
            if not stored:
                return False
            if verify_secret(password, stored):
                self._login_attempts = 0
                return True
            self._login_attempts += 1
            if self._login_attempts >= MAX_LOGIN_ATTEMPTS:
                self._login_locked_until = now + LOGIN_COOLDOWN_SECONDS
                self._login_attempts = 0
                logger.warning("Web login locked after %d failed attempts", MAX_LOGIN_ATTEMPTS)
            return False

    def is_locked(self) -> bool:
        """Return whether the web login is currently in its cooldown."""
        with self._lock:
            return self._login_locked_until > 0 and time.monotonic() < self._login_locked_until

    def lock_remaining_seconds(self) -> int:
        """Return seconds remaining in the login cooldown (0 if none)."""
        with self._lock:
            if self._login_locked_until <= 0:
                return 0
            remaining = int(self._login_locked_until - time.monotonic())
            return max(0, remaining)

    # -- Password mutation ---------------------------------------------------

    def set_password(self, password: str) -> None:
        """Set (or change) the web password, storing a salted hash."""
        with self._lock:
            self._state.update_config("web", {"password": hash_secret(password)})
            self._login_attempts = 0
            self._login_locked_until = 0.0
            logger.info("Web password set")

    def clear_password(self) -> None:
        """Clear the web password (auth disabled) and rotate the secret."""
        with self._lock:
            self._state.update_config("web", {"password": ""})
            self._login_attempts = 0
            self._login_locked_until = 0.0
            logger.info("Web password cleared")


class ScreenPinService:
    """Owns the optional on-screen UI PIN (Part C).

    The PIN is stored as a salted hash in ``web.screen_pin`` and validated
    with a constant-time compare plus an attempt counter + cooldown (mirroring
    ``network_controller.py``).  PINs are strings (never ints) so leading
    zeros are preserved.
    """

    def __init__(self, state: Any) -> None:
        self._state = state
        self._lock = threading.Lock()
        self._pin_attempts = 0
        self._pin_locked_until = 0.0
        self._unlocked_until = 0.0

    def _web(self) -> dict[str, Any]:
        return cast(dict[str, Any], self._state.config.web)

    def is_enabled(self) -> bool:
        """Return whether a screen PIN is set."""
        return bool(self._web().get("screen_pin"))

    def timeout_minutes(self) -> int:
        """Return the PIN unlock timeout in minutes (capped at 24h)."""
        try:
            val = int(self._web().get("screen_pin_timeout_minutes", 60))
        except (TypeError, ValueError):
            val = 60
        if val <= 0:
            return 60
        return min(val, SCREEN_PIN_TIMEOUT_MAX_MINUTES)

    # -- Validation ----------------------------------------------------------

    def validate(self, candidate: str) -> tuple[bool, str]:
        """Validate *candidate* against the stored PIN.

        Returns ``(ok, message)``.  Enforces the attempt limit + cooldown.
        On success, records the unlock timestamp so the PIN stays unlocked
        for ``timeout_minutes()``.
        """
        with self._lock:
            stored = self._web().get("screen_pin")
            if not stored:
                return False, "No screen PIN set"
            now = time.monotonic()
            if self._pin_locked_until > 0 and now < self._pin_locked_until:
                remaining = int(self._pin_locked_until - now)
                return False, f"Too many attempts. Try again in {remaining}s."
            if verify_secret(candidate, stored):
                self._pin_attempts = 0
                self._unlocked_until = now + self.timeout_minutes() * 60
                return True, "ok"
            self._pin_attempts += 1
            remaining = MAX_PIN_ATTEMPTS - self._pin_attempts
            if self._pin_attempts >= MAX_PIN_ATTEMPTS:
                self._pin_locked_until = now + PIN_COOLDOWN_SECONDS
                self._pin_attempts = 0
                logger.warning("Screen PIN locked after %d failed attempts", MAX_PIN_ATTEMPTS)
                return False, f"Locked. Try again in {PIN_COOLDOWN_SECONDS}s."
            logger.warning(
                "Screen PIN validation failed (%d/%d attempts)",
                self._pin_attempts,
                MAX_PIN_ATTEMPTS,
            )
            return False, f"Incorrect PIN. {remaining} attempt(s) remaining."

    def is_unlocked(self) -> bool:
        """Return whether the on-screen UI is currently unlocked."""
        with self._lock:
            return time.monotonic() < self._unlocked_until

    def lock(self) -> None:
        """Immediately lock the on-screen UI (clear the unlock window)."""
        with self._lock:
            self._unlocked_until = 0.0

    # -- Mutation ------------------------------------------------------------

    def set_pin(self, pin: str) -> None:
        """Set (or change) the screen PIN, storing a salted hash."""
        with self._lock:
            self._state.update_config("web", {"screen_pin": hash_secret(pin)})
            self._pin_attempts = 0
            self._pin_locked_until = 0.0
            self._unlocked_until = 0.0
            logger.info("Screen PIN set")

    def clear_pin(self) -> None:
        """Clear the screen PIN (disabled)."""
        with self._lock:
            self._state.update_config("web", {"screen_pin": ""})
            self._pin_attempts = 0
            self._pin_locked_until = 0.0
            self._unlocked_until = 0.0
            logger.info("Screen PIN cleared")


def validate_pin_input(pin: str) -> bool:
    """Validate a candidate PIN's format (4-6 digits)."""
    return validate_pin_format(pin)


def pins_match(a: str, b: str) -> bool:
    """Constant-time equality for PIN confirmation fields."""
    return constant_time_compare(a, b)
