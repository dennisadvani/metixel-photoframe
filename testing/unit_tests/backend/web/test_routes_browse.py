# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2024-2026 Metixel Photoframe Contributors
"""Tests for the filesystem browse endpoint (``/api/browse``).

The route lists subdirectories for the folder-browser modal in the web UI.
These tests use absolute ``tmp_path`` directories (platform-independent) and
monkeypatch :func:`data_dir` for the relative-path / default-path cases.
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

    def test_missing_path_falls_back_to_existing_ancestor(self, client, tmp_path: Path):
        """A missing path falls back to the nearest existing ancestor instead of 404."""
        resp = client.get("/api/browse", query_string={"path": str(tmp_path / "nope")})

        assert resp.status_code == 200
        data = resp.get_json()
        # tmp_path exists, so the browser opens there instead of erroring.
        assert data["current_path"] == str(tmp_path.resolve())

    def test_file_path_returns_400(self, client, tmp_path: Path):
        f = tmp_path / "file.txt"
        f.write_text("x", encoding="utf-8")

        resp = client.get("/api/browse", query_string={"path": str(f)})

        assert resp.status_code == 400
        assert "Not a directory" in resp.get_json()["error"]

    def test_relative_path_resolved_against_data_dir(self, client, tmp_path: Path, monkeypatch):
        """A relative path is joined onto the persistent data dir."""
        import metixel.backend.web.routes.browse as browse_mod

        root = tmp_path / "data"
        (root / "media").mkdir(parents=True)
        monkeypatch.setattr(browse_mod, "data_dir", lambda: root)

        resp = client.get("/api/browse", query_string={"path": "media"})

        assert resp.status_code == 200
        data = resp.get_json()
        assert data["current_path"] == str((root / "media").resolve())
        assert data["entries"] == []

    def test_default_path_is_media_folder(self, client, tmp_path: Path, monkeypatch):
        """With no ``path`` param, browsing starts at ``<data dir>/media``."""
        import metixel.backend.web.routes.browse as browse_mod

        root = tmp_path / "data"
        (root / "media" / "my_photos").mkdir(parents=True)
        monkeypatch.setattr(browse_mod, "data_dir", lambda: root)

        resp = client.get("/api/browse")

        assert resp.status_code == 200
        data = resp.get_json()
        assert data["current_path"] == str((root / "media").resolve())
        assert [e["name"] for e in data["entries"]] == ["my_photos/"]

    def test_empty_path_defaults_to_media_folder(self, client, tmp_path: Path, monkeypatch):
        """An empty ``path`` param (sent when the field is blank) defaults to media."""
        import metixel.backend.web.routes.browse as browse_mod

        root = tmp_path / "data"
        (root / "media" / "my_photos").mkdir(parents=True)
        monkeypatch.setattr(browse_mod, "data_dir", lambda: root)

        resp = client.get("/api/browse", query_string={"path": ""})

        assert resp.status_code == 200
        data = resp.get_json()
        assert data["current_path"] == str((root / "media").resolve())
        assert [e["name"] for e in data["entries"]] == ["my_photos/"]

    def test_missing_default_media_falls_back_to_data_dir(
        self, client, tmp_path: Path, monkeypatch
    ):
        """When the default media folder doesn't exist, fall back to the data dir."""
        import metixel.backend.web.routes.browse as browse_mod

        root = tmp_path / "data"
        root.mkdir(parents=True)
        monkeypatch.setattr(browse_mod, "data_dir", lambda: root)

        # No media folder on disk — the browser should open the data dir instead.
        resp = client.get("/api/browse")

        assert resp.status_code == 200
        data = resp.get_json()
        assert data["current_path"] == str(root.resolve())
        assert data["entries"] == []

    def test_missing_watch_path_falls_back_to_nearest_existing_ancestor(
        self, client, tmp_path: Path, monkeypatch
    ):
        """A configured watch path that isn't on disk falls back to an existing ancestor."""
        import metixel.backend.web.routes.browse as browse_mod

        root = tmp_path / "data"
        (root / "media").mkdir(parents=True)
        monkeypatch.setattr(browse_mod, "data_dir", lambda: root)

        # A watch path under media that doesn't exist yet.
        missing = root / "media" / "not_created_yet"
        resp = client.get("/api/browse", query_string={"path": str(missing)})

        assert resp.status_code == 200
        data = resp.get_json()
        # Falls back to the nearest existing ancestor (the media dir).
        assert data["current_path"] == str((root / "media").resolve())


class TestBrowseCanCreate:
    """The browse payload reports where folder creation is allowed."""

    def test_can_create_true_inside_media_tree(self, client, tmp_path: Path, monkeypatch):
        import metixel.backend.web.routes.browse as browse_mod

        root = tmp_path / "data"
        media = root / "media"
        (media / "sub").mkdir(parents=True)
        monkeypatch.setattr(browse_mod, "data_dir", lambda: root)

        resp = client.get("/api/browse", query_string={"path": str(media / "sub")})

        assert resp.status_code == 200
        assert resp.get_json()["can_create"] is True
        assert resp.get_json()["base_path"] == str(root.resolve())

    def test_can_create_false_outside_media_tree(self, client, tmp_path: Path, monkeypatch):
        import metixel.backend.web.routes.browse as browse_mod

        root = tmp_path / "data"
        root.mkdir(parents=True)
        monkeypatch.setattr(browse_mod, "data_dir", lambda: root)

        # Browsing the data root itself (not the media subdir) → no creation.
        resp = client.get("/api/browse", query_string={"path": str(root)})

        assert resp.status_code == 200
        assert resp.get_json()["can_create"] is False


class TestBrowseCreateFolder:
    """Exercises the folder-creation endpoint (``POST /api/browse/create``)."""

    def test_creates_folder_inside_media(self, client, tmp_path: Path, monkeypatch):
        import metixel.backend.web.routes.browse as browse_mod

        root = tmp_path / "data"
        media = root / "media"
        media.mkdir(parents=True)
        monkeypatch.setattr(browse_mod, "data_dir", lambda: root)

        resp = client.post(
            "/api/browse/create",
            json={"path": str(media), "name": "new_folder"},
        )

        assert resp.status_code == 200
        data = resp.get_json()
        assert data["status"] == "ok"
        assert (media / "new_folder").is_dir()

    def test_relative_parent_resolved_against_data_dir(self, client, tmp_path: Path, monkeypatch):
        import metixel.backend.web.routes.browse as browse_mod

        root = tmp_path / "data"
        media = root / "media"
        media.mkdir(parents=True)
        monkeypatch.setattr(browse_mod, "data_dir", lambda: root)

        resp = client.post(
            "/api/browse/create",
            json={"path": "media", "name": "rel_folder"},
        )

        assert resp.status_code == 200
        assert (media / "rel_folder").is_dir()

    def test_rejects_creating_outside_media(self, client, tmp_path: Path, monkeypatch):
        import metixel.backend.web.routes.browse as browse_mod

        root = tmp_path / "data"
        root.mkdir(parents=True)
        monkeypatch.setattr(browse_mod, "data_dir", lambda: root)

        resp = client.post(
            "/api/browse/create",
            json={"path": str(root), "name": "elsewhere"},
        )

        assert resp.status_code == 403
        assert "inside the media folder" in resp.get_json()["error"]
        assert not (root / "elsewhere").exists()

    def test_rejects_path_traversal_name(self, client, tmp_path: Path, monkeypatch):
        import metixel.backend.web.routes.browse as browse_mod

        root = tmp_path / "data"
        media = root / "media"
        media.mkdir(parents=True)
        monkeypatch.setattr(browse_mod, "data_dir", lambda: root)

        resp = client.post(
            "/api/browse/create",
            json={"path": str(media), "name": "../escape"},
        )

        assert resp.status_code == 400
        assert not (root / "escape").exists()
        assert not (media.parent / "escape").exists()

    def test_rejects_hidden_and_dot_names(self, client, tmp_path: Path, monkeypatch):
        import metixel.backend.web.routes.browse as browse_mod

        root = tmp_path / "data"
        media = root / "media"
        media.mkdir(parents=True)
        monkeypatch.setattr(browse_mod, "data_dir", lambda: root)

        for bad in (".hidden", "..", "."):
            resp = client.post(
                "/api/browse/create",
                json={"path": str(media), "name": bad},
            )
            assert resp.status_code == 400

    def test_rejects_existing_folder(self, client, tmp_path: Path, monkeypatch):
        import metixel.backend.web.routes.browse as browse_mod

        root = tmp_path / "data"
        media = root / "media"
        (media / "exists").mkdir(parents=True)
        monkeypatch.setattr(browse_mod, "data_dir", lambda: root)

        resp = client.post(
            "/api/browse/create",
            json={"path": str(media), "name": "exists"},
        )

        assert resp.status_code == 409

    def test_requires_path_and_name(self, client, tmp_path: Path, monkeypatch):
        import metixel.backend.web.routes.browse as browse_mod

        root = tmp_path / "data"
        media = root / "media"
        media.mkdir(parents=True)
        monkeypatch.setattr(browse_mod, "data_dir", lambda: root)

        resp = client.post("/api/browse/create", json={"path": str(media)})
        assert resp.status_code == 400

        resp = client.post("/api/browse/create", json={"name": "x"})
        assert resp.status_code == 400
