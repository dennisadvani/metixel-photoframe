# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2024-2026 Metixel Photoframe Contributors
"""Tests for the shared web helpers and global error handlers."""


class TestGlobalErrorHandlers:
    """Verify the unified error shape is returned for errors raised in routes."""

    def test_500_returns_unified_shape(self, app, client):
        """A route that raises should return the unified {status, error, message} shape."""

        @app.route("/api/_test_boom")
        def _boom():
            raise RuntimeError("kaboom")

        resp = client.get("/api/_test_boom")
        assert resp.status_code == 500
        data = resp.get_json()
        assert data["status"] == "error"
        assert "error" in data
        assert "message" in data


class TestRequireFields:
    """Verify require_fields validation helper."""

    def test_returns_none_when_all_present(self, app):
        from metixel.backend.web.helpers import require_fields

        with app.test_request_context():
            result = require_fields({"a": 1, "b": 2}, "a", "b")
        assert result is None

    def test_returns_error_when_missing(self, app):
        from metixel.backend.web.helpers import require_fields

        with app.test_request_context():
            resp, status = require_fields({"a": 1}, "a", "b")
        assert status == 400
        data = resp.get_json()
        assert data["status"] == "error"
        assert "b" in data["error"]

    def test_empty_value_treated_missing(self, app):
        from metixel.backend.web.helpers import require_fields

        with app.test_request_context():
            result = require_fields({"a": "", "b": "x"}, "a")
        assert result is not None


class TestJsonifyError:
    """Verify the unified error shape."""

    def test_shape(self, app):
        from metixel.backend.web.helpers import jsonify_error

        with app.test_request_context():
            resp, status = jsonify_error("boom", 400, hint="try again")
        assert status == 400
        data = resp.get_json()
        assert data == {
            "status": "error",
            "error": "boom",
            "message": "boom",
            "hint": "try again",
        }
