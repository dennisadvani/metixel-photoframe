# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2024-2026 Metixel Photoframe Contributors
"""Read a public Google Photos shared album.

**This talks to undocumented endpoints.**  Google's Photos Library API no
longer exposes user albums at all (the ``readonly`` scopes were withdrawn on
31 March 2025) and the Picker API only returns what a human just picked, so a
*continuously* syncing album — the thing a photo frame actually wants — has no
supported interface.  What does exist is the public share page, which is how
this module reads an album:

1. ``GET https://photos.google.com/share/<id>?key=<authKey>`` — the first page
   of items is embedded in the HTML inside ``AF_initDataCallback({… data: […]})``
   blobs.
2. Further pages come from ``POST /_/PhotosUi/data/batchexecute`` with the
   internal rpc ``snAcKc``, driven by the continuation token that sits beside
   the item list.

Because the format is undocumented it can change without notice.  Everything
below therefore locates data **by shape rather than by fixed index**, caps every
loop, and fails with a message that says what to check — a parser that breaks
should produce a clear sync error, never a crash or a silent empty album.
"""

from __future__ import annotations

import json
import logging
import random
import re
import time
from collections.abc import Callable
from typing import Any
from urllib.parse import parse_qs, urlencode, urlparse, urlunparse

import requests

logger = logging.getLogger(__name__)

# ── Endpoints and wire constants ────────────────────────────────────────────

PHOTOS_HOST = "https://photos.google.com"
SHORT_LINK_HOST = "photos.app.goo.gl"
_BATCH_PATH = "/_/PhotosUi/data/batchexecute"

#: The rpc the web UI uses to fetch the next page of a shared album.
_RPC_ALBUM_PAGE = "snAcKc"

#: Google ids for albums and media items start with this.
_KEY_PREFIX = "AF1Qip"
#: Key of the per-item dict that is only present on videos.
_VIDEO_INFO_KEY = "76647426"

#: Anonymous requests still have to look like a browser or the share page comes
#: back as an interstitial with no data blobs in it.
USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)

_REQUEST_TIMEOUT = (15.0, 60.0)  # (connect, read) seconds

# ── Safety caps ─────────────────────────────────────────────────────────────
#
# Pagination is driven by a token Google hands back, so every limit here exists
# to make a changed or hostile response finite rather than fatal.

#: Pages of continuation to follow.  Google returns roughly 100–250 items per
#: page, so this covers albums of tens of thousands of photos.
MAX_PAGES = 200
#: Hard ceiling on items collected, whatever the pages say.
MAX_ITEMS = 50_000
#: A continuation response larger than this is treated as a failure.
MAX_RESPONSE_BYTES = 10 * 1024 * 1024

#: HTTP statuses worth retrying, and how hard to try.
_RETRY_STATUS = frozenset({429, 500, 502, 503, 504})
_MAX_TRIES = 4
_MAX_RETRY_WAIT = 60.0

# ── Minimum length of a media key, used to recognise an item by shape ───────
_MIN_KEY_LENGTH = 30
#: A continuation token is a long opaque string; album/media keys are not.
_MIN_TOKEN_LENGTH = 10


class SharedAlbumError(RuntimeError):
    """A shared album could not be read (bad URL, no access, format change)."""


# ── URL handling ────────────────────────────────────────────────────────────


def parse_album_url(url: str) -> tuple[str, str]:
    """Return ``(share_id, auth_key)`` for a ``/share/`` album URL.

    The ``key`` query parameter is what authorises anonymous access, so it is
    carried through to the continuation requests.

    Raises:
        SharedAlbumError: when *url* is not a Google Photos share URL.
    """
    try:
        parsed = urlparse((url or "").strip())
    except ValueError as e:
        raise SharedAlbumError(f"Not a valid URL: {url}") from e

    match = re.match(r"^/share/([^/]+)", parsed.path or "")
    if parsed.hostname != "photos.google.com" or not match:
        raise SharedAlbumError(
            "Not a Google Photos shared album URL — it should look like "
            "https://photos.google.com/share/<id>?key=<key> or a "
            "https://photos.app.goo.gl/<id> short link"
        )
    auth_key = (parse_qs(parsed.query or "").get("key") or [""])[0]
    return match.group(1), auth_key


def is_album_url(url: str) -> bool:
    """True when *url* looks like something :func:`fetch_album` can read."""
    try:
        parsed = urlparse((url or "").strip())
    except ValueError:
        return False
    if parsed.hostname == SHORT_LINK_HOST and (parsed.path or "/").strip("/"):
        return True
    return parsed.hostname == "photos.google.com" and bool(
        re.match(r"^/share/([^/]+)", parsed.path or "")
    )


def fetchable_url(url: str) -> str:
    """Add ``_imcp=1`` to short links so they redirect to the album itself.

    A bare ``photos.app.goo.gl`` link serves an interstitial page (with no
    album data in it); the parameter is what its "open in browser" link uses.
    """
    parsed = urlparse((url or "").strip())
    if parsed.hostname != SHORT_LINK_HOST:
        return (url or "").strip()
    query = parse_qs(parsed.query or "")
    query["_imcp"] = ["1"]
    flat = urlencode({k: v[-1] for k, v in query.items()})
    return urlunparse(parsed._replace(query=flat))


# ── HTML / payload parsing ──────────────────────────────────────────────────

#: ``AF_dataServiceRequests = {'ds:0' : {id:'snAcKc', …}`` — says which rpc
#: filled each data block, which is how a block is identified by name.
_DS_RPC_RE = re.compile(r"'(ds:\d+)' : \{id:'(\w+)'")
_DATA_BLOCK_RE = re.compile(
    r"AF_initDataCallback\(\{key: '(ds:\d+)'.*?data:(.*?), sideChannel: \{\}\}\);</script>",
    re.DOTALL,
)


def extract_data_blocks(html: str) -> list[dict[str, Any]]:
    """Return every parseable ``AF_initDataCallback`` block in *html*."""
    rpcs = dict(_DS_RPC_RE.findall(html or ""))
    blocks: list[dict[str, Any]] = []
    for key, raw in _DATA_BLOCK_RE.findall(html or ""):
        try:
            data = json.loads(raw)
        except ValueError:
            continue  # not JSON — a block we don't care about
        blocks.append({"key": key, "rpc": rpcs.get(key), "data": data})
    return blocks


def _is_item(value: Any) -> bool:
    """True when *value* has the shape of a media item.

    ``[mediaKey, [url, width, height], takenMs, dedupKey, …]`` — matched on
    shape so a reordered or extended array still parses.
    """
    return (
        isinstance(value, list)
        and len(value) >= 3
        and isinstance(value[0], str)
        and len(value[0]) >= _MIN_KEY_LENGTH
        and isinstance(value[1], list)
        and bool(value[1])
        and isinstance(value[1][0], str)
        and value[1][0].startswith("https://")
        and isinstance(value[2], (int, float))
        and not isinstance(value[2], bool)
    )


def find_item_lists(
    node: Any,
    parent: list | None = None,
    index: int = -1,
    found: list[dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    """Find arrays whose every element is a media item.

    The position is remembered too, because the continuation token and the
    album metadata are siblings of the list rather than part of it.
    """
    if found is None:
        found = []
    if not isinstance(node, list):
        return found
    if node and all(_is_item(child) for child in node):
        found.append({"list": node, "parent": parent, "index": index})
        return found
    for i, child in enumerate(node):
        find_item_lists(child, node, i, found)
    return found


def _normalise_item(raw: list) -> dict[str, Any]:
    """Turn one raw item array into a dict the syncer understands."""
    video_info = next(
        (v for v in raw if isinstance(v, dict) and _VIDEO_INFO_KEY in v),
        None,
    )
    dimensions = raw[1] if isinstance(raw[1], list) else []
    return {
        "media_key": raw[0],
        "base_url": dimensions[0] if dimensions else "",
        "width": dimensions[1] if len(dimensions) > 1 else None,
        "height": dimensions[2] if len(dimensions) > 2 else None,
        "taken_ms": raw[2] if len(raw) > 2 else None,
        # URL-safe base64 SHA-1 of the original file, when Google includes it.
        "dedup_key": raw[3] if len(raw) > 3 and isinstance(raw[3], str) else None,
        "is_video": video_info is not None,
    }


def parse_payload(data: Any) -> dict[str, Any]:
    """Pull ``{items, next_token, album_key, title}`` out of one decoded payload."""
    lists = find_item_lists(data)
    items = [_normalise_item(raw) for entry in lists for raw in entry["list"]]

    next_token: str | None = None
    album_key: str | None = None
    title: str | None = None
    for entry in lists:
        parent, index = entry["parent"], entry["index"]
        if parent is None:
            continue
        sibling = parent[index + 1] if index + 1 < len(parent) else None
        if (
            isinstance(sibling, str)
            and len(sibling) > _MIN_TOKEN_LENGTH
            and not sibling.startswith(_KEY_PREFIX)
        ):
            next_token = sibling
        meta = next(
            (
                v
                for v in parent
                if isinstance(v, list)
                and v
                and isinstance(v[0], str)
                and v[0].startswith(_KEY_PREFIX)
            ),
            None,
        )
        if meta is not None:
            album_key = album_key or meta[0]
            if len(meta) > 1 and isinstance(meta[1], str):
                title = title or meta[1]

    return {"items": items, "next_token": next_token, "album_key": album_key, "title": title}


def parse_batchexecute(text: str) -> list[dict[str, Any]]:
    """Decode a ``batchexecute`` response into ``{rpc, data}`` entries.

    The body is a length-prefixed sequence of JSON arrays preceded by an
    anti-JSON-hijacking prelude, so only the lines that start a JSON array are
    considered.
    """
    out: list[dict[str, Any]] = []
    for line in (text or "").splitlines():
        if not line.startswith("["):
            continue
        try:
            chunk = json.loads(line)
        except ValueError:
            continue
        if not isinstance(chunk, list):
            continue
        for entry in chunk:
            if (
                isinstance(entry, list)
                and len(entry) >= 3
                and entry[0] == "wrb.fr"
                and isinstance(entry[2], str)
            ):
                try:
                    out.append({"rpc": entry[1], "data": json.loads(entry[2])})
                except ValueError:
                    continue  # malformed payload — skip it
    return out


def _pick_wiz_value(html: str, name: str) -> str:
    """Read a ``"name":"value"`` token out of the page bootstrap data."""
    match = re.search(rf'"{name}":"((?:[^"\\]|\\.)*)"', html or "")
    if not match:
        return ""
    try:
        return json.loads(f'"{match.group(1)}"')  # type: ignore[no-any-return]
    except ValueError:
        return match.group(1)


def page_context(html: str) -> dict[str, str]:
    """Collect the tokens ``batchexecute`` wants from the album page."""
    return {
        "at": _pick_wiz_value(html, "SNlM0e"),
        "sid": _pick_wiz_value(html, "FdrFJe"),
        "bl": _pick_wiz_value(html, "cfb2h"),
    }


def page_title(html: str) -> str:
    """Best-effort album title from the page metadata."""
    match = re.search(r'<meta property="og:title" content="([^"]*)"', html or "") or re.search(
        r"<title>([^<]*)</title>", html or ""
    )
    if not match:
        return ""
    title = match.group(1)
    for entity, char in (("&amp;", "&"), ("&quot;", '"'), ("&#39;", "'")):
        title = title.replace(entity, char)
    return re.sub(r"\s*-\s*Google Photos$", "", title).strip()


# ── HTTP (injectable, so tests never touch the network) ─────────────────────

#: ``(url) -> (final_url, status_code, text)``
GetText = Callable[[str], tuple[str, int, str]]
#: ``(url, form_fields) -> (status_code, text)``
PostForm = Callable[[str, dict[str, str]], tuple[int, str]]


def _default_get_text(url: str) -> tuple[str, int, str]:
    resp = requests.get(
        url,
        headers={"User-Agent": USER_AGENT, "Accept-Language": "en-US,en;q=0.9"},
        timeout=_REQUEST_TIMEOUT,
        allow_redirects=True,
    )
    return resp.url, resp.status_code, resp.text


def _default_post_form(url: str, data: dict[str, str]) -> tuple[int, str]:
    resp = requests.post(
        url,
        data=data,
        headers={
            "User-Agent": USER_AGENT,
            "Content-Type": "application/x-www-form-urlencoded;charset=UTF-8",
            "Accept-Language": "en-US,en;q=0.9",
        },
        timeout=_REQUEST_TIMEOUT,
        stream=True,
    )
    # Cap what is read: the loop below trusts this body enough to parse it.
    chunks: list[bytes] = []
    total = 0
    for chunk in resp.iter_content(chunk_size=65536):
        if not chunk:
            continue
        total += len(chunk)
        if total > MAX_RESPONSE_BYTES:
            resp.close()
            raise SharedAlbumError(
                f"Album page response exceeded {MAX_RESPONSE_BYTES // (1024 * 1024)} MB"
            )
        chunks.append(chunk)
    resp.close()
    return resp.status_code, b"".join(chunks).decode("utf-8", errors="replace")


def _retry_wait(attempt: int) -> float:
    """Exponential backoff with jitter, matching what Google expects."""
    return min(_MAX_RETRY_WAIT, 2.0 * (2**attempt) * (0.5 + random.random() / 2))


# ── Album listing ───────────────────────────────────────────────────────────


def fetch_album(
    url: str,
    *,
    get_text: GetText | None = None,
    post_form: PostForm | None = None,
    max_pages: int = MAX_PAGES,
    on_progress: Callable[[int, int], None] | None = None,
    should_cancel: Callable[[], bool] | None = None,
    sleep: Callable[[float], None] = time.sleep,
) -> dict[str, Any]:
    """List every item in the public shared album at *url*.

    Args:
        url: A ``photos.google.com/share/…`` URL or a ``photos.app.goo.gl``
            short link.
        get_text / post_form: HTTP callables, injected by the tests.
        max_pages: Continuation pages to follow before stopping.
        on_progress: Called as ``(items_so_far, pages_done)`` after each page.
        should_cancel: Polled between pages so a sync can be cancelled.
        sleep: Injected for the retry backoff.

    Returns:
        ``{"url", "title", "album_key", "pages", "truncated", "complete",
        "items"}`` with items newest first.  ``complete`` is True only when
        pagination reached Google's last page — anything that stopped it early
        (cancel, a cap, a failed or stalled page) leaves it False, so callers
        must not treat the items as the whole album (e.g. for strict deletes).

    Raises:
        SharedAlbumError: bad URL, no access, or no items found (which usually
            means the album is not shared by link, or the page format changed).
    """
    get_text = get_text or _default_get_text
    post_form = post_form or _default_post_form

    if not is_album_url(url):
        # Raises with the explanatory message.
        parse_album_url(url)

    final_url, status, html = _get_with_retries(fetchable_url(url), get_text, sleep)
    if status == 404:
        raise SharedAlbumError(
            "Album not found (HTTP 404) — check the link, and that sharing is still on"
        )
    if "accounts.google.com" in (final_url or ""):
        raise SharedAlbumError(
            "Google asked for a sign-in — the album is not shared by link. In Google "
            "Photos open the album → Share → Create link"
        )
    if status != 200:
        raise SharedAlbumError(f"Album page returned HTTP {status}")

    if "/share/" in (final_url or ""):
        share_id, auth_key = parse_album_url(final_url)
    elif is_album_url(url) and urlparse(url.strip()).hostname != SHORT_LINK_HOST:
        share_id, auth_key = parse_album_url(url)
    else:
        # A short link that served its interstitial instead of redirecting:
        # the page has no album data, which the check below reports in terms
        # of sharing rather than calling the user's valid link invalid.
        share_id, auth_key = "", ""

    items: dict[str, dict[str, Any]] = {}
    album_key = ""
    title = ""
    next_token = ""

    for block in extract_data_blocks(html):
        parsed = parse_payload(block["data"])
        if not parsed["items"]:
            continue
        for item in parsed["items"]:
            items.setdefault(item["media_key"], item)
        album_key = album_key or (parsed["album_key"] or "")
        title = title or (parsed["title"] or "")
        # Only a block that carries a token may set it: a later block with
        # items but no token (a cover or highlights strip) must not wipe the
        # album's real continuation token and silently stop pagination.
        if parsed["next_token"]:
            next_token = parsed["next_token"]

    if not items:
        raise SharedAlbumError(
            "No photos found on the album page. Check that the album is shared by link "
            "(Google Photos → album → Share → Create link) and that the link opens in a "
            "private browser window. If it does, Google has changed the page format and "
            "Metixel's reader needs updating"
        )

    pages = 1
    if on_progress:
        on_progress(len(items), pages)

    context = page_context(html)
    album_key = album_key or share_id
    truncated = False

    while next_token and pages < max_pages:
        if should_cancel is not None and should_cancel():
            break
        if len(items) >= MAX_ITEMS:
            truncated = True
            logger.warning("Shared album hit the %d item cap — stopping", MAX_ITEMS)
            break

        before = len(items)
        token = ""
        try:
            payloads = _album_page(
                context, album_key, next_token, auth_key, share_id, post_form, sleep
            )
        except SharedAlbumError as e:
            # Keep what we already have: a partial album still beats none, and
            # the caller reports the error alongside the items.
            logger.warning("Stopping pagination after %d page(s): %s", pages, e)
            truncated = True
            break

        for payload in payloads:
            parsed = parse_payload(payload)
            for item in parsed["items"]:
                items.setdefault(item["media_key"], item)
            if parsed["next_token"]:
                token = parsed["next_token"]

        pages += 1
        if on_progress:
            on_progress(len(items), pages)

        if not token:
            next_token = ""  # Google says this was the last page
            break
        # No progress, or a token that repeats, means the next request would
        # loop forever on the same page.  next_token stays set, so the listing
        # reads as incomplete and strict sync will not delete from it.
        if len(items) == before or token == next_token:
            truncated = True
            logger.warning(
                "Shared album pagination stalled after %d page(s) — album may be incomplete",
                pages,
            )
            break
        next_token = token

    if next_token and pages >= max_pages:
        truncated = True
        logger.warning("Shared album hit the %d page cap — album may be incomplete", max_pages)

    ordered = sorted(items.values(), key=lambda i: i.get("taken_ms") or 0, reverse=True)
    return {
        "url": final_url or url,
        "title": title or page_title(html),
        "album_key": album_key,
        "pages": pages,
        "truncated": truncated,
        "complete": not next_token,
        "items": ordered,
    }


def _get_with_retries(
    url: str, get_text: GetText, sleep: Callable[[float], None]
) -> tuple[str, int, str]:
    """GET *url*, retrying the statuses Google uses to ask for a slow-down."""
    final_url, status, text = "", 0, ""
    for attempt in range(_MAX_TRIES):
        final_url, status, text = get_text(url)
        if status not in _RETRY_STATUS:
            return final_url, status, text
        if attempt + 1 < _MAX_TRIES:
            wait = _retry_wait(attempt)
            logger.debug("Album page HTTP %s — retrying in %.1fs", status, wait)
            sleep(wait)
    return final_url, status, text


def _album_page(
    context: dict[str, str],
    album_key: str,
    page_token: str,
    auth_key: str,
    share_id: str,
    post_form: PostForm,
    sleep: Callable[[float], None],
) -> list[Any]:
    """Fetch one continuation page via the ``snAcKc`` rpc."""
    request = [album_key, page_token, None, auth_key or None]
    f_req = json.dumps([[[_RPC_ALBUM_PAGE, json.dumps(request), None, "generic"]]])

    params = {
        "rpcids": _RPC_ALBUM_PAGE,
        "source-path": f"/share/{share_id}",
        "hl": "en",
        "_reqid": str(100000 + random.randrange(900000)),
        "rt": "c",
    }
    if context.get("sid"):
        params["f.sid"] = context["sid"]
    if context.get("bl"):
        params["bl"] = context["bl"]

    body = {"f.req": f_req}
    if context.get("at"):
        body["at"] = context["at"]

    url = f"{PHOTOS_HOST}{_BATCH_PATH}?{urlencode(params)}"

    status, text = 0, ""
    for attempt in range(_MAX_TRIES):
        status, text = post_form(url, body)
        if status not in _RETRY_STATUS:
            break
        if attempt + 1 < _MAX_TRIES:
            wait = _retry_wait(attempt)
            logger.debug("Album continuation HTTP %s — retrying in %.1fs", status, wait)
            sleep(wait)

    if status != 200:
        raise SharedAlbumError(f"Album continuation returned HTTP {status}")

    payloads = [p["data"] for p in parse_batchexecute(text) if p["rpc"] == _RPC_ALBUM_PAGE]
    if not payloads:
        raise SharedAlbumError("Album continuation returned no usable data")
    return payloads
