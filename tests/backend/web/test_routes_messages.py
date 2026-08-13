# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2024-2026 Metixel Photoframe Contributors
"""Message API endpoints — persistent message list and dismissal."""

from __future__ import annotations

import json


class TestPersistentMessages:
    def test_list_empty_by_default(self, client):
        resp = client.get("/api/messages/persistent")
        assert resp.status_code == 200
        assert json.loads(resp.data) == {"persistent": []}

    def test_list_returns_configured(self, client, mock_state):
        mock_state.update_config("messages", {"persistent": [{"id": "welcome_wifi"}]})
        resp = client.get("/api/messages/persistent")
        assert resp.status_code == 200
        assert json.loads(resp.data) == {"persistent": [{"id": "welcome_wifi"}]}


class TestDismissMessages:
    def test_dismiss_specific(self, client, mock_state, mock_ipc):
        mock_state.update_config("messages", {"persistent": [{"id": "a"}, {"id": "b"}]})
        resp = client.post("/api/messages/dismiss", json={"id": "a"})
        assert resp.status_code == 200
        data = json.loads(resp.data)
        assert data["status"] == "ok"
        assert data["dismissed"] == 1
        assert data["persistent"] == [{"id": "b"}]
        # A dismiss-all IPC command clears the screen immediately
        assert mock_ipc.send.call_count == 1
        assert mock_ipc.send.call_args[0][0].cmd == "dismiss_all_messages"

    def test_dismiss_all(self, client, mock_state, mock_ipc):
        mock_state.update_config("messages", {"persistent": [{"id": "a"}, {"id": "b"}]})
        resp = client.post("/api/messages/dismiss", json={"all": True})
        assert resp.status_code == 200
        data = json.loads(resp.data)
        assert data["status"] == "ok"
        assert data["dismissed"] == 2
        assert data["persistent"] == []
        assert mock_ipc.send.call_count == 1

    def test_dismiss_requires_id_or_all(self, client):
        resp = client.post("/api/messages/dismiss", json={})
        assert resp.status_code == 400

    def test_dismiss_unknown_id_returns_404(self, client, mock_state):
        mock_state.update_config("messages", {"persistent": [{"id": "a"}]})
        resp = client.post("/api/messages/dismiss", json={"id": "missing"})
        assert resp.status_code == 404
