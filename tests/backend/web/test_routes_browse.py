# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2024-2026 Metixel Photoframe Contributors
"""Tests for the filesystem browse endpoint (``/api/browse``).

The route lists subdirectories for the folder-browser modal in the web UI.
These tests use absolute ``tmp_path`` directories (platform-independent) and
monkeypatch :func:`install_root` for the relative-path / default-path cases.
"""

from __future__ import annotations

from pathlib import Path


class TestBrowseFolder:
    """Exercises the folder-listing endpoint."""

    def test_lists_only_subdirectories(self, client, tmp_path: Path):
        """Subdirs are returned; files and hidden dirs are excluded, sorted."""
        # Browse a dedicated subdir so the fixture's run_dir doesn't show up.
        browse_dir = tmp_path / "browse"
        browse_dir.mkdir()
        (browse_dir / "media_a").mkdir()
        (browse_dir / "media_b").mkdir()
        (browse_dir / ".hidden").mkdir()
        (browse_dir / "notes.txt").write_text("hi", encoding="utf-8")

        resp = client.get("/api/browse", query_string={"path": str(browse_dir)})

        assert resp.status_code == 200
        data = resp.get_json()
        assert data["current_path"] == str(browse_dir.resolve())
        # Sorted, subdirs only, trailing slash on names.
        assert [e["name"] for e in data["entries"]] == ["media_a/", "media_b/"]
        # Each entry carries an absolute path under the browsed directory.
        for e in data["entries"]:
            assert e["path"].startswith(str(browse_dir.resolve()))

    def test_empty_directory_returns_no_entries(self, client, tmp_path: Path):
        empty = tmp_path / "empty"
        empty.mkdir()

        resp = client.get("/api/browse", query_string={"path": str(empty)})

        assert resp.status_code == 200
        assert resp.get_json()["entries"] == []

    def test_parent_path_reported(self, client, tmp_path: Path):
        nested = tmp_path / "a" / "b"
        nested.mkdir(parents=True)

        resp = client.get("/api/browse", query_string={"path": str(nested)})

        assert resp.status_code == 200
        data = resp.get_json()
        assert data["current_path"] == str(nested.resolve())
        assert data["parent_path"] == str(nested.resolve().parent)

    def test_missing_path_returns_404(self, client, tmp_path: Path):
        resp = client.get("/api/browse", query_string={"path": str(tmp_path / "nope")})

        assert resp.status_code == 404
        assert "Path not found" in resp.get_json()["error"]

    def test_file_path_returns_400(self, client, tmp_path: Path):
        f = tmp_path / "file.txt"
        f.write_text("x", encoding="utf-8")

        resp = client.get("/api/browse", query_string={"path": str(f)})

        assert resp.status_code == 400
        assert "Not a directory" in resp.get_json()["error"]

    def test_relative_path_resolved_against_install_root(self, client, tmp_path: Path, monkeypatch):
        """A relative path is joined onto the install root."""
        import metixel.backend.web.routes.browse as browse_mod

        root = tmp_path / "root"
        (root / "media").mkdir(parents=True)
        monkeypatch.setattr(browse_mod, "install_root", lambda: root)

        resp = client.get("/api/browse", query_string={"path": "media"})

        assert resp.status_code == 200
        data = resp.get_json()
        assert data["current_path"] == str((root / "media").resolve())
        assert data["entries"] == []

    def test_default_path_is_media_folder(self, client, tmp_path: Path, monkeypatch):
        """With no ``path`` param, browsing starts at ``<install root>/media``."""
        import metixel.backend.web.routes.browse as browse_mod

        root = tmp_path / "root"
        (root / "media" / "my_photos").mkdir(parents=True)
        monkeypatch.setattr(browse_mod, "install_root", lambda: root)

        resp = client.get("/api/browse")

        assert resp.status_code == 200
        data = resp.get_json()
        assert data["current_path"] == str((root / "media").resolve())
        assert [e["name"] for e in data["entries"]] == ["my_photos/"]

    def test_empty_path_defaults_to_media_folder(self, client, tmp_path: Path, monkeypatch):
        """An empty ``path`` param (sent when the field is blank) defaults to media."""
        import metixel.backend.web.routes.browse as browse_mod

        root = tmp_path / "root"
        (root / "media" / "my_photos").mkdir(parents=True)
        monkeypatch.setattr(browse_mod, "install_root", lambda: root)

        resp = client.get("/api/browse", query_string={"path": ""})

        assert resp.status_code == 200
        data = resp.get_json()
        assert data["current_path"] == str((root / "media").resolve())
        assert [e["name"] for e in data["entries"]] == ["my_photos/"]
