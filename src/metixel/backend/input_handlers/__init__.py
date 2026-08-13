# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2024-2026 Metixel Photoframe Contributors
"""Input handlers for physical controls (CEC, IR)."""

from metixel.backend.input_handlers.cec import CECHandler
from metixel.backend.input_handlers.ir import IRHandler

__all__ = ["CECHandler", "IRHandler"]
