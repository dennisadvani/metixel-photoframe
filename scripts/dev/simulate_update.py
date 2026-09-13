# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2024-2026 Metixel Photoframe Contributors
"""Run the Metixel backend locally with a SIMULATED pending update.

Purpose: let a developer see the Updates card's "automatic update paused" notice
in the real dashboard without a Raspberry Pi, an OTA-capable install layout, or
any network access to GitHub.

How it works
------------
Two things are simulated, and ONLY inside this process:

1. **The GitHub API.**  A fake ``HttpGateway`` (see ``metixel.shared.ports``)
   answers the two endpoints ``UpdateManager`` uses.  It is injected through the
   supported dependency-injection seam (``build_backend(ports=Ports(http=...))``),
   so no production code is stubbed or patched — the real
   ``check_for_updates()`` parses that response and the real ``get_status()``
   computes the hardware hurdle.  What you see in the browser is the genuine
   payload.

2. **The board model.**  ``detect_pi_model()`` returns ``None`` on a Windows dev
   box, so the hurdle would report "unknown board".  The simulation patches it
   *in this process only*.

Deliberately NOT a production env-var hook on ``detect_pi_model``: the board
probe is what protects a real Pi 3 from an unattended 2.0.0 upgrade, so adding a
bypass for the probe would weaken the very safety feature this UI demonstrates.

Auto-INSTALL is also neutralised here (the weekly schedule is patched to a
no-op) so running this tool can never kick off a real update against your
working copy.  The manual check/status paths — the ones the UI uses — stay real.

Usage
-----
    python scripts/dev/simulate_update.py                  # Pi 3 -> notice SHOWN
    python scripts/dev/simulate_update.py --model pi5      # Pi 5 -> notice hidden
    python scripts/dev/simulate_update.py --version 1.9.0  # below floor -> hidden

Then open http://127.0.0.1:8080/ -> Advanced -> Updates.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path
from typing import Any

# Make ``src/`` importable when run directly from the repo root.
_REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_REPO_ROOT / "src"))

#: The release we pretend is published.  Its shape mirrors what the GitHub
#: releases API returns, trimmed to the fields UpdateManager actually reads.
DEFAULT_SIM_VERSION = "2.0.0"


class _Response:
    """Minimal stand-in for ``requests.Response`` / the ``HttpResponse`` port."""

    def __init__(self, payload: Any, status_code: int = 200) -> None:
        self._payload = payload
        self.status_code = status_code

    @property
    def ok(self) -> bool:
        return 200 <= self.status_code < 300

    @property
    def text(self) -> str:
        return json.dumps(self._payload)

    def raise_for_status(self) -> None:
        if not self.ok:
            raise RuntimeError(f"HTTP {self.status_code}")

    def json(self) -> Any:
        return self._payload

    def iter_content(self, chunk_size: int = 1):  # noqa: ANN201 - port shape
        return iter(())

    def __enter__(self) -> _Response:
        return self

    def __exit__(self, *args: Any) -> None:
        return None


class FakeGitHubGateway:
    """Answers the GitHub releases/commits endpoints with canned data.

    Implements ``HttpGateway`` structurally, so it satisfies the same port the
    real ``RequestsHttpGateway`` does — no subclassing of production code.
    """

    def __init__(self, version: str, *, prerelease: bool = False) -> None:
        self._version = version
        self._prerelease = prerelease

    def get(
        self,
        url: str,
        *,
        headers: Any = None,
        params: Any = None,
        stream: bool = False,
        timeout: Any = None,
    ) -> _Response:
        if url.endswith("/releases"):
            return _Response([self._release()])
        if url.endswith("/commits"):
            return _Response([self._commit()])
        return _Response({}, status_code=404)

    def post(self, url: str, **kwargs: Any) -> _Response:
        return _Response({}, status_code=404)

    def _release(self) -> dict[str, Any]:
        return {
            "tag_name": f"v{self._version}",
            "name": f"v{self._version}",
            "prerelease": self._prerelease,
            "draft": False,
            "html_url": f"https://example.invalid/releases/tag/v{self._version}",
            "published_at": "2026-09-01T00:00:00Z",
            "assets": [],
        }

    def _commit(self) -> dict[str, Any]:
        import hashlib

        # sha1 is used only to derive a stable fake commit id; not security.
        sha = hashlib.sha1(self._version.encode()).hexdigest()  # noqa: S324
        return {
            "sha": sha,
            "html_url": "https://example.invalid/commit",
            "commit": {
                "message": "simulated dev commit",
                "committer": {"date": "2026-09-01T00:00:00Z"},
            },
        }


def _apply_simulation_patches(model: str) -> None:
    """Patch board detection + neutralise auto-install, in this process only."""
    import metixel.backend.update_manager as um
    import metixel.shared.platform as platform

    simulated = None if model.lower() == "unknown" else model.lower()
    platform.detect_pi_model = lambda: simulated  # type: ignore[assignment]

    def _no_auto_install(self: Any) -> None:
        """Keep the real manual check path, but never auto-install locally."""
        return None

    um.UpdateManager._maybe_auto_update = _no_auto_install  # type: ignore[method-assign]


def _install_update_manager_hook() -> None:
    """Populate the UpdateManager the ROUTES will use, before the server serves.

    ``BackendDaemon.run()`` constructs the UpdateManager itself (in
    ``_start_update_manager``), so pre-building one here would be discarded and
    replaced by a second, empty instance — the routes would then read a manager
    with no cached results and the notice would never appear.  Wrapping the
    starter means we force one synchronous check on the SAME instance the Flask
    app is wired to.
    """
    from metixel.backend.daemon import BackendDaemon

    original = BackendDaemon._start_update_manager

    def _start_then_prime(self: Any) -> None:
        original(self)
        mgr = getattr(self, "_update_mgr", None)
        if mgr is not None:
            # Synchronous so the status is ready before the first request.
            mgr.check_for_updates(force=True)

    BackendDaemon._start_update_manager = _start_then_prime  # type: ignore[method-assign]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--version",
        default=DEFAULT_SIM_VERSION,
        help=f"release to pretend is published (default {DEFAULT_SIM_VERSION})",
    )
    parser.add_argument(
        "--model",
        default="pi3",
        help="board to simulate: pi2/pi3/pi4/pi5, or 'unknown' for an "
        "undetectable board (default pi3)",
    )
    parser.add_argument(
        "--prerelease",
        action="store_true",
        help="publish the simulated release as a pre-release (beta channel)",
    )
    parser.add_argument(
        "--config",
        default=str(_REPO_ROOT / "etc" / "config.json"),
        help="path to the config file to use",
    )
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")

    config_path = Path(args.config)
    if not config_path.is_file():
        print(f"config not found: {config_path}", file=sys.stderr)
        return 1

    _apply_simulation_patches(args.model)
    _install_update_manager_hook()

    from metixel.backend.daemon import build_backend
    from metixel.shared.ports import Ports

    backend = build_backend(
        config_path,
        ports=Ports(http=FakeGitHubGateway(args.version, prerelease=args.prerelease)),
    )

    # Report the verdict using a throwaway manager (same inputs, same code path
    # as the one the server will build), so the banner prints before run().
    from metixel.backend.update_manager import UpdateManager

    preview = UpdateManager(backend._state, http=FakeGitHubGateway(args.version))  # noqa: SLF001
    preview.check_for_updates(force=True)
    status = preview.get_status()
    hurdle = status["auto_update_hurdle"]
    channel = status["current_channel"]
    available = (status.get("available") or {}).get(channel) or {}

    print()
    print("=" * 70)
    print("  SIMULATED UPDATE (nothing was installed; no network calls made)")
    print("=" * 70)
    print(f"  simulated board     : {args.model}")
    print(f"  channel             : {channel}")
    print(f"  simulated release   : v{args.version}")
    print(f"  offered as update   : {available.get('is_newer')}")
    print(f"  auto-update BLOCKED : {hurdle['applies']}")
    print(f"  hardware_ok         : {hurdle['hardware_ok']}")
    if hurdle["applies"]:
        print(f"\n  Notice text shown in the Updates card:\n    {hurdle['reason']}")
    else:
        print("\n  Notice text          : (none — the amber notice stays hidden)")
    print()
    print("  Open the dashboard:  http://127.0.0.1:8080/")
    print("  Then: Advanced -> Updates")
    print("  Ctrl+C to stop.")
    print()

    backend.run()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
