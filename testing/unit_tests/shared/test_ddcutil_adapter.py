# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2024-2026 Metixel Photoframe Contributors
"""DdcutilAdapter stdout parsers + Protocol conformance."""

from __future__ import annotations

import pytest

DETECT_STDOUT = """\
Display 1
   I2C bus:  /dev/i2c-2
   DRM connector:           card1-HDMI-A-1
   EDID synopsis:
      Mfg id:               DEL
      Model:                DELL U2415
      Serial number:        ABC123
   VCP version:         2.1
Display 2
   I2C bus:  /dev/i2c-3
   EDID synopsis:
      Mfg id:               SAM
      Model:                S24R35
   VCP version:         2.2
"""

CAPABILITIES_STDOUT = """\
Model: DELL U2415
MCCS version: 2.1
Commands:
   Op Code: 01 (VCP Request)
VCP Features:
   Feature: 02 (New control value)
      Values:
         01: No new control value
         02: New control value has been saved
   Feature: 10 (Brightness)
      Values: (continuous 0-100)
   Feature: 12 (Contrast)
      Values: (continuous 0-100)
   Feature: 14 (Select Color Preset)
      Values:
         01: sRGB
         04: 5000 K
         05: 6500 K
         06: 7500 K
   Feature: 60 (Input Source)
      Values: 0F 11 12
   Feature: 62 (Audio speaker volume)
      Values: (continuous 0-100)
"""

GETVCP_BRIGHTNESS = "VCP code 0x10 (Brightness): current value =    75, max value =   100\n"
GETVCP_PRESET = (
    "VCP code 0x14 (Select color preset): mh=0x00 ml=0x0b "
    "(00 01 04 05 06 08 0B), current value = 0x05\n"
)


class TestParseDetect:
    def test_parses_multiple_monitors(self) -> None:
        from metixel.shared.ddcutil_adapter import parse_detect

        monitors = parse_detect(DETECT_STDOUT)
        assert len(monitors) == 2
        assert monitors[0].display == 1
        assert monitors[0].model == "DELL U2415"
        assert monitors[0].mfg == "DEL"
        assert monitors[0].serial == "ABC123"
        assert monitors[0].i2c_bus == "/dev/i2c-2"
        assert monitors[1].display == 2
        assert monitors[1].model == "S24R35"

    def test_empty(self) -> None:
        from metixel.shared.ddcutil_adapter import parse_detect

        assert parse_detect("") == []
        assert parse_detect("No displays found") == []


class TestParseCapabilities:
    def test_parses_features(self) -> None:
        from metixel.shared.ddcutil_adapter import parse_capabilities

        caps = parse_capabilities(CAPABILITIES_STDOUT, display=1)
        assert caps.model == "DELL U2415"
        assert caps.mccs_version == "2.1"
        by_code = {f.code: f for f in caps.features}
        assert 0x10 in by_code
        assert by_code[0x10].feature_type == "continuous"
        assert by_code[0x10].name == "Brightness"
        assert by_code[0x14].feature_type == "discrete"
        assert {o.value for o in by_code[0x14].options} >= {0x01, 0x04, 0x05, 0x06}
        assert by_code[0x60].feature_type == "discrete"
        assert {o.value for o in by_code[0x60].options} == {0x0F, 0x11, 0x12}


class TestParseGetvcp:
    def test_continuous(self) -> None:
        from metixel.shared.ddcutil_adapter import parse_getvcp

        val = parse_getvcp(GETVCP_BRIGHTNESS, 0x10)
        assert val is not None
        assert val.current == 75
        assert val.maximum == 100
        assert val.feature_type == "continuous"

    def test_discrete(self) -> None:
        from metixel.shared.ddcutil_adapter import parse_getvcp

        val = parse_getvcp(GETVCP_PRESET, 0x14)
        assert val is not None
        assert val.current == 0x05
        assert val.feature_type == "discrete"

    def test_discrete_simple_level(self) -> None:
        """Real ddcutil output uses (sl=0x..) for discrete current value."""
        from metixel.shared.ddcutil_adapter import parse_getvcp

        stdout = "VCP code 0x14 (Select color preset           ): 6500 K (sl=0x05)\n"
        val = parse_getvcp(stdout, 0x14)
        assert val is not None
        assert val.current == 0x05
        assert val.feature_type == "discrete"


class FakeRunner:
    """subprocess.run stand-in keyed by command args."""

    def __init__(self, responses: dict[tuple[str, ...], tuple[int, str, str]]) -> None:
        self.responses = responses
        self.calls: list[list[str]] = []

    def __call__(self, cmd, **kwargs):
        from subprocess import CompletedProcess

        self.calls.append(list(cmd))
        key = tuple(cmd[1:])  # drop binary
        # Match by prefix for flexible lookups
        for resp_key, (code, out, err) in self.responses.items():
            if key[: len(resp_key)] == resp_key:
                return CompletedProcess(cmd, code, out, err)
        return CompletedProcess(cmd, 1, "", "unhandled")


class TestDdcutilAdapter:
    def test_available_when_binary_present(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from metixel.shared.ddcutil_adapter import DdcutilAdapter
        from metixel.shared.ports import DdcController

        monkeypatch.setattr(
            "metixel.shared.ddcutil_adapter.shutil.which",
            lambda _name: "/usr/bin/ddcutil",
        )
        adapter = DdcutilAdapter(runner=FakeRunner({}).__call__)
        assert adapter.available() is True
        assert isinstance(adapter, DdcController)

    def test_detect_and_capabilities(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from metixel.shared.ddcutil_adapter import DdcutilAdapter

        monkeypatch.setattr(
            "metixel.shared.ddcutil_adapter.shutil.which",
            lambda _name: "/usr/bin/ddcutil",
        )

        def getvcp_for(code: str) -> str:
            if code == "0x10":
                return GETVCP_BRIGHTNESS
            if code == "0x14":
                return GETVCP_PRESET
            return f"VCP code {code} (X): current value =    10, max value =   100\n"

        runner = FakeRunner(
            {
                ("detect",): (0, DETECT_STDOUT, ""),
                ("capabilities", "--display", "1"): (0, CAPABILITIES_STDOUT, ""),
                ("getvcp", "0x10", "--display", "1"): (0, getvcp_for("0x10"), ""),
                ("getvcp", "0x12", "--display", "1"): (
                    0,
                    "VCP code 0x12 (Contrast): current value =    50, max value =   100\n",
                    "",
                ),
                ("getvcp", "0x14", "--display", "1"): (0, getvcp_for("0x14"), ""),
                ("getvcp", "0x60", "--display", "1"): (
                    0,
                    "VCP code 0x60 (Input Source): current value = 0x0f\n",
                    "",
                ),
                ("getvcp", "0x62", "--display", "1"): (
                    0,
                    "VCP code 0x62 (Audio speaker volume): "
                    "current value =    20, max value =   100\n",
                    "",
                ),
                ("getvcp", "0x02", "--display", "1"): (
                    0,
                    "VCP code 0x02 (New control value): current value = 0x01\n",
                    "",
                ),
            }
        )
        adapter = DdcutilAdapter(runner=runner)
        monitors = adapter.detect()
        assert len(monitors) == 2
        caps = adapter.capabilities(1)
        by_code = {f.code: f for f in caps.features}
        assert by_code[0x10].current == 75
        assert by_code[0x10].maximum == 100
        assert by_code[0x14].current == 0x05

    def test_set_vcp(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from metixel.shared.ddcutil_adapter import DdcutilAdapter

        monkeypatch.setattr(
            "metixel.shared.ddcutil_adapter.shutil.which",
            lambda _name: "/usr/bin/ddcutil",
        )
        runner = FakeRunner(
            {
                ("setvcp", "0x10", "80", "--display", "1"): (0, "", ""),
            }
        )
        adapter = DdcutilAdapter(runner=runner)
        adapter.set_vcp(1, 0x10, 80)
        assert any(c[1] == "setvcp" for c in runner.calls)

    def test_reset_factory(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from metixel.shared.ddcutil_adapter import DdcutilAdapter

        monkeypatch.setattr(
            "metixel.shared.ddcutil_adapter.shutil.which",
            lambda _name: "/usr/bin/ddcutil",
        )
        runner = FakeRunner(
            {
                ("setvcp", "0x04", "0x01", "--display", "1"): (0, "", ""),
            }
        )
        adapter = DdcutilAdapter(runner=runner)
        adapter.reset_factory(1)
        assert any(c[1] == "setvcp" and c[2] == "0x04" for c in runner.calls)
