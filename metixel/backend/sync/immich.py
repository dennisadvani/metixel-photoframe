# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2024-2026 Metixel Photoframe Contributors
"""Immich API synchronization client.

Polls an Immich server for albums and assets, downloads new/updated media,
and feeds them into the MediaProcessor pipeline.
"""

from __future__ import annotations

import json
import logging
import time
from pathlib import Path

import requests

from metixel.backend.state import StateManager

logger = logging.getLogger(__name__)


class ImmichSyncer:
    """Polls the Immich REST API for album and asset changes."""

    def __init__(self, state: StateManager) -> None:
        self._state = state
        self._config = state.config
        self._base_url: str = self._config.sync["immich"]["server_url"].rstrip("/")
        self._api_key: str = self._config.sync["immich"]["api_key"]
        self._poll_interval: int = self._config.sync["immich"]["poll_interval_seconds"]
        self._cache_dir: Path = Path(self._config.system["cache_dir"])
        self._sync_state_path: Path = self._cache_dir / "sync_state.json"
        self._running: bool = False

    def run(self) -> None:
        """Main sync loop — polls Immich at the configured interval."""
        self._running = True
        logger.info("Immich syncer started (server: %s, interval: %ds)", self._base_url, self._poll_interval)

        while self._running:
            try:
                self._sync()
            except requests.exceptions.ConnectionError:
                logger.warning("Immich server unreachable — will retry")
            except Exception:
                logger.exception("Immich sync error")
            time.sleep(self._poll_interval)

    def stop(self) -> None:
        """Signal the sync loop to stop."""
        self._running = False

    # -- Sync logic ----------------------------------------------------------

    def _sync(self) -> None:
        """Perform one full sync cycle."""
        logger.debug("Starting Immich sync cycle")
        # TODO: Implement full Immich API sync
        # 1. GET /api/albums → list albums
        # 2. For each album: GET /api/albums/{id} → list assets
        # 3. Compare with sync_state.json for changes
        # 4. Download new/updated assets via GET /api/assets/{id}/original
        # 5. Pass each asset to MediaProcessor.ingest()
        logger.debug("Immich sync cycle complete (API integration pending)")

    def _get_headers(self) -> dict[str, str]:
        return {
            "Accept": "application/json",
            "x-api-key": self._api_key,
        }

    def _load_sync_state(self) -> dict:
        """Load the last-known sync state from disk."""
        if self._sync_state_path.exists():
            with open(self._sync_state_path, "r") as f:
                return json.load(f)
        return {"albums": {}}

    def _save_sync_state(self, state: dict) -> None:
        """Persist sync state to disk."""
        self._sync_state_path.parent.mkdir(parents=True, exist_ok=True)
        with open(self._sync_state_path, "w") as f:
            json.dump(state, f, indent=2)
