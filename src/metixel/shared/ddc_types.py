# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2024-2026 Metixel Photoframe Contributors
"""Data types and well-known VCP metadata for DDC/CI monitor control."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

FeatureType = Literal["continuous", "discrete", "table", "unknown"]


@dataclass(frozen=True)
class DdcMonitor:
    """A monitor detected via DDC/CI."""

    display: int
    model: str = ""
    mfg: str = ""
    serial: str = ""
    i2c_bus: str = ""
    vcp_version: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "display": self.display,
            "model": self.model,
            "mfg": self.mfg,
            "serial": self.serial,
            "i2c_bus": self.i2c_bus,
            "vcp_version": self.vcp_version,
        }


@dataclass
class DdcDiscreteOption:
    """One labelled value for a discrete (non-continuous) VCP feature."""

    value: int
    label: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {"value": self.value, "label": self.label or f"0x{self.value:02X}"}


@dataclass
class DdcFeature:
    """A single VCP feature reported by the monitor."""

    code: int
    name: str = ""
    feature_type: FeatureType = "unknown"
    current: int | None = None
    maximum: int | None = None
    options: list[DdcDiscreteOption] = field(default_factory=list)
    readable: bool = True
    writable: bool = True
    icon: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "code_hex": f"0x{self.code:02X}",
            "name": self.name or f"Feature 0x{self.code:02X}",
            "type": self.feature_type,
            "current": self.current,
            "maximum": self.maximum,
            "options": [o.to_dict() for o in self.options],
            "readable": self.readable,
            "writable": self.writable,
            "icon": self.icon,
        }


@dataclass
class DdcCapabilities:
    """Capability probe result for one display."""

    display: int
    model: str = ""
    mccs_version: str = ""
    features: list[DdcFeature] = field(default_factory=list)
    raw: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "display": self.display,
            "model": self.model,
            "mccs_version": self.mccs_version,
            "features": [f.to_dict() for f in self.features],
        }


@dataclass(frozen=True)
class DdcVcpValue:
    """Current value of one VCP feature."""

    code: int
    current: int
    maximum: int = 0
    name: str = ""
    feature_type: FeatureType = "unknown"

    def to_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "code_hex": f"0x{self.code:02X}",
            "current": self.current,
            "maximum": self.maximum,
            "name": self.name,
            "type": self.feature_type,
        }


# Well-known VCP codes — enrichment only; never assume a monitor has them.
# Icons are Material Symbols names.
VCP_META: dict[int, dict[str, str]] = {
    0x04: {"name": "Restore Factory Defaults", "icon": "restart_alt"},
    0x10: {"name": "Brightness", "icon": "brightness_6"},
    0x12: {"name": "Contrast", "icon": "contrast"},
    0x14: {"name": "Colour Preset", "icon": "palette"},
    0x16: {"name": "Red Gain", "icon": "filter_vintage"},
    0x18: {"name": "Green Gain", "icon": "filter_vintage"},
    0x1A: {"name": "Blue Gain", "icon": "filter_vintage"},
    0x1C: {"name": "Focus", "icon": "center_focus_strong"},
    0x52: {"name": "Active Control", "icon": "tune"},
    0x60: {"name": "Input Source", "icon": "input"},
    0x62: {"name": "Speaker Volume", "icon": "volume_up"},
    0x64: {"name": "Audio Mute", "icon": "volume_off"},
    0x6C: {"name": "Video Black Level Red", "icon": "tonality"},
    0x6E: {"name": "Video Black Level Green", "icon": "tonality"},
    0x70: {"name": "Video Black Level Blue", "icon": "tonality"},
    0x87: {"name": "Sharpness", "icon": "details"},
    0x8A: {"name": "Colour Saturation", "icon": "palette"},
    0x8D: {"name": "Audio Mute / Screen Blank", "icon": "volume_off"},
    0xAC: {"name": "Horizontal Frequency", "icon": "straighten"},
    0xAE: {"name": "Vertical Frequency", "icon": "straighten"},
    0xB6: {"name": "Display Technology Type", "icon": "tv"},
    0xC8: {"name": "Display Controller Type", "icon": "memory"},
    0xC9: {"name": "Display Firmware Level", "icon": "info"},
    0xCA: {"name": "OSD Enable", "icon": "settings_display"},
    0xCC: {"name": "OSD Language", "icon": "language"},
    0xD6: {"name": "Power Mode", "icon": "power_settings_new"},
    0xDF: {"name": "VCP Version", "icon": "info"},
    0xE0: {"name": "Manufacturer Specific", "icon": "build"},
}

# The only VCP features exposed as user controls.  Everything else (gain,
# sharpness, geometry, power mode, read-only info, manufacturer-specific,
# etc.) is deliberately hidden to keep the UI focused and avoid writing to
# features that could confuse or damage the picture.
USER_FACING_CODES: frozenset[int] = frozenset(
    {
        0x04,  # Restore Factory Defaults (rendered as a button)
        0x10,  # Brightness
        0x12,  # Contrast
        0x14,  # Select Colour Preset
    }
)

# Features that are usually read-only / not useful as user controls.
_HIDDEN_OR_RO_CODES = frozenset(
    {
        0x02,  # New Control Value
        0x52,  # Active Control
        0xAC,  # Horizontal Frequency
        0xAE,  # Vertical Frequency
        0xB6,  # Display Technology Type
        0xC8,  # Display Controller Type
        0xC9,  # Display Firmware Level
        0xDF,  # VCP Version
    }
)


def enrich_feature(feature: DdcFeature) -> DdcFeature:
    """Apply well-known name/icon metadata without inventing unsupported codes."""
    meta = VCP_META.get(feature.code)
    if meta:
        if not feature.name or feature.name.lower().startswith("feature "):
            feature.name = meta["name"]
        if not feature.icon:
            feature.icon = meta["icon"]
    if not feature.icon:
        feature.icon = "tune"
    if feature.code in _HIDDEN_OR_RO_CODES:
        feature.writable = False
    return feature


def is_user_facing(feature: DdcFeature) -> bool:
    """Return True if the feature should appear as a UI control.

    Only the curated :data:`USER_FACING_CODES` set is exposed.  This keeps
    the UI focused on the controls users actually need and avoids writing to
    features that could confuse or damage the picture.
    """
    return feature.code in USER_FACING_CODES
