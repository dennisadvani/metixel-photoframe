# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2024-2026 Metixel Photoframe Contributors
"""Tests for the media upload endpoint (``POST /api/media/upload``)."""

from __future__ import annotations

import io
from pathlib import Path
from unittest import mock


def _upload(client, files: list[tuple[str, bytes]]):
    """POST one or more ``(filename, bytes)`` pairs to the upload endpoint."""
    return client.post(
        "/api/media/upload",
        data={"files": [(io.BytesIO(blob), name) for name, blob in files]},
        content_type="multipart/form-data",
    )


def test_upload_saves_into_my_media(app, client, mock_state, tmp_path):
    """A valid image is saved under media/my_media and reported as saved."""
    mock_state.update_config("system", {"media_dir": str(tmp_path / "media")})
    upload_dir = tmp_path / "media" / "my_media"

    resp = _upload(client, [("photo.jpg", b"\xff\xd8\xff\xe0fakejpeg")])

    assert resp.status_code == 201
    body = resp.get_json()
    assert body["saved_count"] == 1
    assert body["error_count"] == 0
    assert body["saved"][0]["saved_as"] == "photo.jpg"
    assert (upload_dir / "photo.jpg").read_bytes() == b"\xff\xd8\xff\xe0fakejpeg"


def test_upload_auto_renames_collision(app, client, mock_state, tmp_path):
    """A second upload with the same name is saved with a -1 suffix."""
    mock_state.update_config("system", {"media_dir": str(tmp_path / "media")})
    upload_dir = tmp_path / "media" / "my_media"
    upload_dir.mkdir(parents=True, exist_ok=True)
    (upload_dir / "photo.jpg").write_bytes(b"existing")

    resp = _upload(client, [("photo.jpg", b"\xff\xd8\xff\xe0newdata")])

    assert resp.status_code == 201
    assert resp.get_json()["saved"][0]["saved_as"] == "photo-1.jpg"
    assert (upload_dir / "photo-1.jpg").read_bytes() == b"\xff\xd8\xff\xe0newdata"


def test_upload_rejects_unsupported_extension(app, client, mock_state, tmp_path):
    """Files outside the image/video/HEIC whitelist are rejected."""
    mock_state.update_config("system", {"media_dir": str(tmp_path / "media")})

    resp = _upload(client, [("evil.sh", b"#!/bin/sh\nrm -rf /")])

    assert resp.status_code == 400
    body = resp.get_json()
    assert body["saved_count"] == 0
    assert "Unsupported file type" in body["errors"][0]["error"]


def test_upload_sanitizes_path_traversal_filename(app, client, mock_state, tmp_path):
    """Path components in the filename are stripped, not honoured."""
    mock_state.update_config("system", {"media_dir": str(tmp_path / "media")})
    upload_dir = tmp_path / "media" / "my_media"

    resp = _upload(client, [("../../evil.jpg", b"\xff\xd8\xff\xe0x")])

    assert resp.status_code == 201
    # The file lands inside my_media with only the basename kept.
    saved = resp.get_json()["saved"][0]["saved_as"]
    assert "evil.jpg" in saved
    assert (upload_dir / saved).exists()


def test_upload_rejects_empty_file(app, client, mock_state, tmp_path):
    """Zero-byte uploads are rejected."""
    mock_state.update_config("system", {"media_dir": str(tmp_path / "media")})

    resp = _upload(client, [("empty.jpg", b"")])

    assert resp.status_code == 400
    assert resp.get_json()["errors"][0]["error"] == "Empty file"


def test_upload_rejects_when_disk_almost_full(app, client, mock_state, tmp_path):
    """Uploads are refused when they'd leave <5% of the disk free."""
    mock_state.update_config("system", {"media_dir": str(tmp_path / "media")})

    # 1 GB disk, 52 MB free → 5% buffer is 50 MB. A 10 MB upload would leave
    # 42 MB free (< 50 MB), so it must be refused.
    fake_usage = mock.Mock(total=1000 * 1024**2, free=52 * 1024**2, used=0)
    with mock.patch(
        "metixel.backend.web.routes.media.shutil.disk_usage",
        return_value=fake_usage,
    ):
        resp = _upload(client, [("big.mp4", b"\x00" * (10 * 1024**2))])

    assert resp.status_code == 400
    assert resp.get_json()["errors"][0]["error"] == "Insufficient disk space"


def test_upload_heic_converts_to_jpeg(app, client, mock_state, tmp_path, monkeypatch):
    """HEIC files are converted to JPEG on arrival (preserving orientation)."""
    import metixel.backend.web.routes.media as media_mod

    mock_state.update_config("system", {"media_dir": str(tmp_path / "media")})
    upload_dir = tmp_path / "media" / "my_media"

    converted = {"called": False}

    def fake_convert(source, out_path: Path) -> bool:
        converted["called"] = True
        out_path.write_bytes(b"\xff\xd8\xff\xe0converted-jpeg")
        return True

    monkeypatch.setattr(media_mod, "_convert_heic", fake_convert)

    resp = _upload(client, [("IMG_0001.HEIC", b"\x00\x00\x00\x18ftypheic")])

    assert resp.status_code == 201
    assert converted["called"] is True
    saved = resp.get_json()["saved"][0]
    assert saved["saved_as"] == "IMG_0001.jpg"
    assert (upload_dir / "IMG_0001.jpg").read_bytes() == b"\xff\xd8\xff\xe0converted-jpeg"


def test_upload_no_files_returns_400(app, client):
    """A request with no files is rejected."""
    resp = client.post("/api/media/upload", data={}, content_type="multipart/form-data")
    assert resp.status_code == 400
    assert resp.get_json()["errors"][0]["error"] == "No files supplied"
