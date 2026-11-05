# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2024-2026 Metixel Photoframe Contributors
"""Shared Google Photos album reader — parsing and pagination.

The album format is undocumented, so these tests pin the *shape* the parser
expects: items found by shape rather than index, the continuation token read
from beside the item list, and every pagination loop bounded.  Fixtures mimic
the real page and ``batchexecute`` responses; nothing here touches the network.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from metixel.backend.sync import google_photos_album as album

ALBUM_URL = "https://photos.google.com/share/SHARE123?key=KEY456"
SHORT_URL = "https://photos.app.goo.gl/abc123"


# ── Fixtures builders ───────────────────────────────────────────────────────


def _item(key_suffix: str, *, url: str = "https://lh3.googleusercontent.com/x", video=False):
    """One raw item array, shaped like Google's."""
    media_key = f"AF1QipMediaKey{key_suffix}".ljust(40, "0")
    raw: list[Any] = [
        media_key,
        [url + key_suffix, 4032, 3024],
        1_700_000_000_000,
        f"dedup{key_suffix}",
        0,
        1_700_000_100_000,
    ]
    if video:
        raw.append({album._VIDEO_INFO_KEY: [12345]})
    return raw


def _payload(items: list, next_token: str | None = None, title: str = "Holiday"):
    """One decoded payload: the item list, its token, and album metadata."""
    return [items, next_token, ["AF1QipAlbumKey00000000000000000000", title]]


def _page_html(items: list, next_token: str | None = None, title: str = "Holiday") -> str:
    data = json.dumps(_payload(items, next_token, title))
    return (
        "<html><head>"
        f'<meta property="og:title" content="{title} - Google Photos">'
        "<script>AF_dataServiceRequests = {'ds:0' : {id:'snAcKc', request:[]}};</script>"
        '<script nonce="n">'
        '"SNlM0e":"AT-TOKEN","FdrFJe":"SID-TOKEN","cfb2h":"BL-TOKEN"'
        "</script>"
        "</head><body>"
        f"<script nonce=\"n\">AF_initDataCallback({{key: 'ds:0', hash: '1', "
        f"data:{data}, sideChannel: {{}}}});</script>"
        "</body></html>"
    )


def _batch_response(items: list, next_token: str | None = None) -> str:
    """A ``batchexecute`` body: prelude, length prefixes, wrb.fr envelopes."""
    inner = json.dumps(_payload(items, next_token))
    chunk = json.dumps([["wrb.fr", "snAcKc", inner, None, None, None, "generic"], ["di", 24]])
    return f")]}}'\n\n{len(chunk)}\n{chunk}\n"


def _fake_http(pages: list[str], album_html: str, status: int = 200):
    """Return ``(get_text, post_form, calls)`` serving canned responses."""
    calls: dict[str, list] = {"get": [], "post": []}

    def get_text(url: str):
        calls["get"].append(url)
        return ALBUM_URL, status, album_html

    def post_form(url: str, data: dict[str, str]):
        calls["post"].append((url, data))
        if not pages:
            return 200, _batch_response([])
        return 200, pages.pop(0)

    return get_text, post_form, calls


# ── URLs ────────────────────────────────────────────────────────────────────


class TestUrls:
    def test_parses_share_url(self) -> None:
        assert album.parse_album_url(ALBUM_URL) == ("SHARE123", "KEY456")

    def test_share_url_without_key(self) -> None:
        assert album.parse_album_url("https://photos.google.com/share/ABC") == ("ABC", "")

    @pytest.mark.parametrize(
        "url",
        [
            "",
            "not a url",
            "https://example.com/share/abc",
            "https://photos.google.com/albums",
            "https://photos.google.com/",
        ],
    )
    def test_rejects_other_urls(self, url: str) -> None:
        with pytest.raises(album.SharedAlbumError):
            album.parse_album_url(url)

    def test_is_album_url_accepts_both_forms(self) -> None:
        assert album.is_album_url(ALBUM_URL) is True
        assert album.is_album_url(SHORT_URL) is True
        assert album.is_album_url("https://photos.app.goo.gl/") is False
        assert album.is_album_url("https://immich.example.com/album/1") is False

    def test_short_links_get_the_desktop_parameter(self) -> None:
        """A bare short link serves an interstitial with no album data in it."""
        assert album.fetchable_url(SHORT_URL) == SHORT_URL + "?_imcp=1"

    def test_full_urls_are_left_alone(self) -> None:
        assert album.fetchable_url(ALBUM_URL) == ALBUM_URL


# ── Page parsing ────────────────────────────────────────────────────────────


class TestPageParsing:
    def test_extracts_data_blocks_with_their_rpc(self) -> None:
        blocks = album.extract_data_blocks(_page_html([_item("a")]))
        assert len(blocks) == 1
        assert blocks[0]["key"] == "ds:0"
        assert blocks[0]["rpc"] == "snAcKc"

    def test_non_json_blocks_are_ignored(self) -> None:
        html = (
            "<script>AF_initDataCallback({key: 'ds:9', hash: '1', "
            "data:function(){}, sideChannel: {}});</script>"
        )
        assert album.extract_data_blocks(html) == []

    def test_items_are_found_by_shape(self) -> None:
        parsed = album.parse_payload(_payload([_item("a"), _item("b")], "TOKEN-NEXT-PAGE"))
        assert len(parsed["items"]) == 2
        assert parsed["items"][0]["base_url"].startswith("https://")
        assert parsed["items"][0]["width"] == 4032
        assert parsed["items"][0]["dedup_key"] == "dedupa"

    def test_items_are_found_when_nested_deeper(self) -> None:
        """The list's depth in the payload is not fixed, so it is searched for."""
        nested = [[["unrelated"], _payload([_item("a")], "TOKEN-NEXT-PAGE")]]
        parsed = album.parse_payload(nested)
        assert len(parsed["items"]) == 1
        assert parsed["next_token"] == "TOKEN-NEXT-PAGE"

    def test_continuation_token_read_from_the_sibling(self) -> None:
        parsed = album.parse_payload(_payload([_item("a")], "TOKEN-NEXT-PAGE"))
        assert parsed["next_token"] == "TOKEN-NEXT-PAGE"

    def test_no_token_on_the_last_page(self) -> None:
        assert album.parse_payload(_payload([_item("a")], None))["next_token"] is None

    def test_album_key_is_not_mistaken_for_a_token(self) -> None:
        """Album/media keys share the sibling slot shape; the prefix rules them out."""
        parsed = album.parse_payload([[_item("a")], "AF1QipNotAToken0000000000", []])
        assert parsed["next_token"] is None

    def test_videos_are_flagged(self) -> None:
        parsed = album.parse_payload(_payload([_item("v", video=True), _item("p")]))
        by_video = {i["is_video"] for i in parsed["items"]}
        assert by_video == {True, False}

    def test_album_key_and_title(self) -> None:
        parsed = album.parse_payload(_payload([_item("a")], title="Trip to Rome"))
        assert parsed["album_key"].startswith("AF1Qip")
        assert parsed["title"] == "Trip to Rome"

    def test_page_context_tokens(self) -> None:
        ctx = album.page_context(_page_html([_item("a")]))
        assert ctx == {"at": "AT-TOKEN", "sid": "SID-TOKEN", "bl": "BL-TOKEN"}

    def test_page_title_strips_the_suffix(self) -> None:
        assert album.page_title(_page_html([_item("a")], title="Summer")) == "Summer"

    def test_batchexecute_decoding(self) -> None:
        decoded = album.parse_batchexecute(_batch_response([_item("a")], "T2"))
        assert len(decoded) == 1
        assert decoded[0]["rpc"] == "snAcKc"

    def test_batchexecute_ignores_noise_and_bad_json(self) -> None:
        body = ')]}\'\n\n12\nnot json\n[["wrb.fr","snAcKc","{{{"]]\n'
        assert album.parse_batchexecute(body) == []


# ── Album listing ───────────────────────────────────────────────────────────


class TestFetchAlbum:
    def test_single_page_album(self) -> None:
        get_text, post_form, calls = _fake_http([], _page_html([_item("a"), _item("b")]))

        listing = album.fetch_album(ALBUM_URL, get_text=get_text, post_form=post_form)

        assert len(listing["items"]) == 2
        assert listing["pages"] == 1
        assert listing["title"] == "Holiday"
        assert listing["truncated"] is False
        assert calls["post"] == []  # no continuation needed

    def test_follows_continuation_pages(self) -> None:
        pages = [
            _batch_response([_item("c"), _item("d")], "TOKEN-PAGE-3"),
            _batch_response([_item("e")], None),
        ]
        get_text, post_form, calls = _fake_http(
            pages, _page_html([_item("a"), _item("b")], "TOKEN-PAGE-2")
        )

        listing = album.fetch_album(ALBUM_URL, get_text=get_text, post_form=post_form)

        assert len(listing["items"]) == 5
        assert listing["pages"] == 3
        assert len(calls["post"]) == 2
        assert listing["truncated"] is False

    def test_continuation_request_carries_key_and_token(self) -> None:
        get_text, post_form, calls = _fake_http(
            [_batch_response([_item("c")], None)], _page_html([_item("a")], "TOKEN-PAGE-2")
        )

        album.fetch_album(ALBUM_URL, get_text=get_text, post_form=post_form)

        url, body = calls["post"][0]
        assert album._BATCH_PATH in url
        assert "rpcids=snAcKc" in url
        assert "source-path=%2Fshare%2FSHARE123" in url
        assert body["at"] == "AT-TOKEN"
        # f.req = [[[rpc, "[albumKey, pageToken, null, authKey]", null, "generic"]]]
        inner = json.loads(json.loads(body["f.req"])[0][0][1])
        assert inner[1] == "TOKEN-PAGE-2"
        assert inner[3] == "KEY456"

    def test_duplicate_items_are_collapsed(self) -> None:
        get_text, post_form, _ = _fake_http(
            [_batch_response([_item("a")], None)], _page_html([_item("a")], "TOKEN-PAGE-2")
        )

        listing = album.fetch_album(ALBUM_URL, get_text=get_text, post_form=post_form)

        assert len(listing["items"]) == 1

    def test_items_are_newest_first(self) -> None:
        older, newer = _item("old"), _item("new")
        older[2] = 1_600_000_000_000
        newer[2] = 1_700_000_000_000
        get_text, post_form, _ = _fake_http([], _page_html([older, newer]))

        listing = album.fetch_album(ALBUM_URL, get_text=get_text, post_form=post_form)

        assert listing["items"][0]["taken_ms"] == 1_700_000_000_000

    def test_repeated_token_breaks_the_loop(self) -> None:
        """A server that keeps handing back the same token must not loop forever."""
        same = _batch_response([_item("a")], "TOKEN-PAGE-2")
        get_text, post_form, calls = _fake_http(
            [same, same, same], _page_html([_item("a")], "TOKEN-PAGE-2")
        )

        listing = album.fetch_album(ALBUM_URL, get_text=get_text, post_form=post_form)

        assert len(calls["post"]) == 1
        assert listing["pages"] == 2

    def test_page_cap_is_honoured_and_reported(self) -> None:
        def post_form(_url, _data):
            # Always a new item plus a new token — infinite without the cap.
            post_form.n += 1  # type: ignore[attr-defined]
            return 200, _batch_response(
                [_item(f"x{post_form.n}")], f"TOKEN-CONTINUATION-{post_form.n}"
            )

        post_form.n = 0  # type: ignore[attr-defined]

        listing = album.fetch_album(
            ALBUM_URL,
            get_text=lambda _u: (ALBUM_URL, 200, _page_html([_item("a")], "TOKEN-PAGE-2")),
            post_form=post_form,
            max_pages=4,
        )

        assert listing["pages"] == 4
        assert listing["truncated"] is True

    def test_cancellation_stops_paging(self) -> None:
        get_text, post_form, calls = _fake_http(
            [_batch_response([_item("c")], "TOKEN-PAGE-3")],
            _page_html([_item("a")], "TOKEN-PAGE-2"),
        )

        listing = album.fetch_album(
            ALBUM_URL, get_text=get_text, post_form=post_form, should_cancel=lambda: True
        )

        assert calls["post"] == []
        assert len(listing["items"]) == 1

    def test_progress_is_reported_per_page(self) -> None:
        seen: list[tuple[int, int]] = []
        get_text, post_form, _ = _fake_http(
            [_batch_response([_item("b")], None)], _page_html([_item("a")], "TOKEN-PAGE-2")
        )

        album.fetch_album(
            ALBUM_URL,
            get_text=get_text,
            post_form=post_form,
            on_progress=lambda count, pages: seen.append((count, pages)),
        )

        assert seen == [(1, 1), (2, 2)]

    def test_failed_continuation_keeps_what_was_read(self) -> None:
        """A partial album beats none — the gap is reported as truncation."""

        def post_form(_url, _data):
            return 500, ""

        listing = album.fetch_album(
            ALBUM_URL,
            get_text=lambda _u: (ALBUM_URL, 200, _page_html([_item("a")], "TOKEN-PAGE-2")),
            post_form=post_form,
            sleep=lambda _s: None,
        )

        assert len(listing["items"]) == 1
        assert listing["truncated"] is True

    def test_later_block_without_a_token_keeps_the_real_one(self) -> None:
        """A second item block (cover/highlights) with no token must not stop paging."""
        album_block = json.dumps(_payload([_item("a"), _item("b")], "TOKEN-PAGE-2"))
        extra_block = json.dumps(_payload([_item("cover")], None))
        html = (
            "<html><body>"
            f"<script>AF_initDataCallback({{key: 'ds:0', hash: '1', "
            f"data:{album_block}, sideChannel: {{}}}});</script>"
            f"<script>AF_initDataCallback({{key: 'ds:1', hash: '2', "
            f"data:{extra_block}, sideChannel: {{}}}});</script>"
            "</body></html>"
        )
        get_text, post_form, calls = _fake_http([_batch_response([_item("c")], None)], html)

        listing = album.fetch_album(ALBUM_URL, get_text=get_text, post_form=post_form)

        assert len(calls["post"]) == 1
        assert len(listing["items"]) == 4
        assert listing["complete"] is True

    def test_reaching_the_last_page_is_complete(self) -> None:
        get_text, post_form, _ = _fake_http(
            [_batch_response([_item("b")], None)], _page_html([_item("a")], "TOKEN-PAGE-2")
        )

        listing = album.fetch_album(ALBUM_URL, get_text=get_text, post_form=post_form)

        assert listing["complete"] is True

    def test_single_page_album_is_complete(self) -> None:
        get_text, post_form, _ = _fake_http([], _page_html([_item("a")]))

        assert album.fetch_album(ALBUM_URL, get_text=get_text, post_form=post_form)["complete"]

    def test_stalled_pagination_is_incomplete_and_reported(self) -> None:
        """A page with nothing new but a token means more may exist — not complete,
        and flagged as truncated so the syncer reports the gap rather than 'Success'."""
        same = _batch_response([_item("a")], "TOKEN-PAGE-2")
        get_text, post_form, _ = _fake_http([same], _page_html([_item("a")], "TOKEN-PAGE-2"))

        listing = album.fetch_album(ALBUM_URL, get_text=get_text, post_form=post_form)

        assert listing["complete"] is False
        assert listing["truncated"] is True

    def test_failed_cancelled_or_capped_paging_is_incomplete(self) -> None:
        html = _page_html([_item("a")], "TOKEN-PAGE-2")

        failed = album.fetch_album(
            ALBUM_URL,
            get_text=lambda _u: (ALBUM_URL, 200, html),
            post_form=lambda _u, _d: (500, ""),
            sleep=lambda _s: None,
        )
        get_text, post_form, _ = _fake_http([], html)
        cancelled = album.fetch_album(
            ALBUM_URL, get_text=get_text, post_form=post_form, should_cancel=lambda: True
        )
        capped = album.fetch_album(
            ALBUM_URL,
            get_text=lambda _u: (ALBUM_URL, 200, html),
            post_form=lambda _u, _d: (200, _batch_response([_item("b")], "TOKEN-PAGE-3")),
            max_pages=2,
        )

        assert failed["complete"] is False
        assert cancelled["complete"] is False
        assert capped["complete"] is False


# ── Failure modes ───────────────────────────────────────────────────────────


class TestFetchAlbumErrors:
    def test_not_an_album_url(self) -> None:
        with pytest.raises(album.SharedAlbumError, match="Not a Google Photos shared album"):
            album.fetch_album("https://example.com/x")

    def test_short_link_that_does_not_redirect_is_not_called_invalid(self) -> None:
        """Google served the interstitial at the short URL: blame sharing, not the link."""
        get_text, post_form, _ = _fake_http([], "<html><body>interstitial</body></html>")
        get_text = lambda _u: (SHORT_URL, 200, "<html><body>interstitial</body></html>")  # noqa: E731

        with pytest.raises(album.SharedAlbumError) as exc:
            album.fetch_album(SHORT_URL, get_text=get_text, post_form=post_form)

        assert "No photos found" in str(exc.value)
        assert "Not a Google Photos shared album URL" not in str(exc.value)

    def test_404_is_explained(self) -> None:
        with pytest.raises(album.SharedAlbumError, match="not found"):
            album.fetch_album(
                ALBUM_URL,
                get_text=lambda _u: (ALBUM_URL, 404, ""),
                post_form=lambda _u, _d: (200, ""),
            )

    def test_sign_in_redirect_means_sharing_is_off(self) -> None:
        with pytest.raises(album.SharedAlbumError, match="not shared by link"):
            album.fetch_album(
                ALBUM_URL,
                get_text=lambda _u: ("https://accounts.google.com/signin", 200, ""),
                post_form=lambda _u, _d: (200, ""),
            )

    def test_other_status_is_surfaced(self) -> None:
        with pytest.raises(album.SharedAlbumError, match="HTTP 503"):
            album.fetch_album(
                ALBUM_URL,
                get_text=lambda _u: (ALBUM_URL, 503, ""),
                post_form=lambda _u, _d: (200, ""),
                sleep=lambda _s: None,
            )

    def test_unparseable_page_says_what_to_check(self) -> None:
        with pytest.raises(album.SharedAlbumError, match="No photos found"):
            album.fetch_album(
                ALBUM_URL,
                get_text=lambda _u: (ALBUM_URL, 200, "<html>nothing useful</html>"),
                post_form=lambda _u, _d: (200, ""),
            )

    def test_retryable_status_is_retried(self) -> None:
        attempts: list[int] = []

        def get_text(_url):
            attempts.append(1)
            if len(attempts) < 3:
                return ALBUM_URL, 429, ""
            return ALBUM_URL, 200, _page_html([_item("a")])

        listing = album.fetch_album(
            ALBUM_URL, get_text=get_text, post_form=lambda _u, _d: (200, ""), sleep=lambda _s: None
        )

        assert len(attempts) == 3
        assert len(listing["items"]) == 1
