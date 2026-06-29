"""Reddit connector — backs up your *saved* posts and comments.

Reddit's saved feed has the same shape of constraints that make Raindrop awkward,
only more so: there is **no** authenticated REST endpoint for "saved" without an
OAuth app, and the public site exposes the list only through its internal
``shreddit`` web components behind your logged-in session. So instead of a bearer
token + HTTP API, this connector drives a **persistent browser session**
(Playwright) and walks the infinite-scroll ``faceplate-partial`` pagination,
exactly like the standalone ``reddit_saved_extractor`` tool it is adapted from.

Two consequences shape the strategy:

* there is no server-side ``since`` filter and no cheap delta — every run walks
  the whole saved feed, and
* "un-saving" an item simply removes it from the feed (there is no trash/tombstone
  to poll).

So this connector is a **full-enumeration** source: ``supports_incremental`` is
False (the engine therefore runs every backup in ``full`` mode), and it yields a
single :class:`ReconcileMarker` covering every live id so the engine can
soft-delete anything you have since un-saved. Deletion is driven entirely by the
reconcile sweep, so ``supports_native_deletes`` is False.

Auth is a **path-valued secret**: ``REDDIT_SESSION_DIR`` points at the Playwright
persistent-context directory holding your logged-in cookies (create it once with
``reddit-saved --login`` from the reddit_saved_extractor tool, or any first run
with ``headless=false``). Nothing in the core treats a secret as a token — a
filesystem path works fine.

Heavy dependencies (Playwright) are imported **lazily** inside :meth:`_acquire`
so the module always imports cleanly and the connector stays discoverable even
when the optional ``dbs[reddit]`` extra is not installed; a missing dependency
surfaces as a clear :class:`ConnectorConfigError` at run time.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Iterator

from pydantic import BaseModel, ConfigDict, Field

from ..core import (
    AuthCapture,
    BackupItem,
    Capabilities,
    Checkpoint,
    ConnectorConfigError,
    Connector,
    Cursor,
    ItemKind,
    MediaRef,
    ReconcileMarker,
    RunContext,
    parse_iso,
)

_TYPES = ("post", "comment")


class RedditConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    username: str
    include_types: list[str] = list(_TYPES)
    max_pages: int = Field(default=100, ge=1)
    delay: float = Field(default=2.0, ge=0.0)
    headless: bool = True
    checkpoint_every: int = Field(default=200, ge=1)
    # Name of the env var holding the path to the Playwright persistent-context
    # directory (your logged-in session). Mirrors raindrop's ``token_env``.
    session_dir_env: str = "REDDIT_SESSION_DIR"


class RedditConnector(Connector):
    type = "reddit"
    display_name = "Reddit (saved)"
    description = "Your saved Reddit posts and comments, via a logged-in browser session."
    docs_url = "https://github.com/baileyrd/reddit_saved_extractor"
    setup_hint = (
        "Set username, then click ‘Reddit login’ to capture a session: a browser "
        "opens, you log in, and you CLOSE the window to finish."
    )
    config_model = RedditConfig
    secret_keys = ("REDDIT_SESSION_DIR",)
    wants_managed_http = False
    schema_version = 1
    # Optional runtime deps (the `[reddit]` extra) — declared so the UI/CLI can
    # report readiness and offer a one-click install.
    pip_requirements = ("playwright>=1.40",)
    runtime_imports = ("playwright",)
    needs_playwright_browser = True
    # The logged-in session can be captured from a UI by opening a browser.
    auth_capture = AuthCapture(
        kind="browser_session",
        secret_key="REDDIT_SESSION_DIR",
        login_url="https://www.reddit.com/login/",
        label="Reddit login",
    )
    item_kinds = (
        ItemKind(name="post", display_name="Post"),
        ItemKind(name="comment", display_name="Comment"),
    )
    capabilities = Capabilities(
        supports_incremental=False,  # no server-side delta -> every run is full
        supports_full_enumeration=True,  # enables the soft-delete reconcile sweep
        supports_native_deletes=False,  # un-saves are detected via reconcile only
        produces_media=True,
        media_inline=False,
        items_mutable=True,
        requires_auth=True,
        supports_rate_limit_backoff=False,
        paginated=True,
    )
    # The capture timestamp churns every run; strip it before hashing so an
    # otherwise-unchanged saved item never spawns a spurious revision.
    volatile_fields = ("extracted_at",)

    # -- main entrypoint ----------------------------------------------------

    def fetch(self, ctx: RunContext) -> Iterator["BackupItem | Checkpoint | ReconcileMarker"]:
        cfg: RedditConfig = ctx.config  # type: ignore[assignment]
        if cfg.session_dir_env not in self.secret_keys:
            raise ConnectorConfigError(
                f"session_dir_env={cfg.session_dir_env!r} must be one of the declared "
                f"secret_keys {self.secret_keys}; set REDDIT_SESSION_DIR in your .env "
                f"to the path of your logged-in Playwright session directory."
            )

        live_ids: set[str] = set()
        cursor: dict[str, Any] = {}
        seen = 0

        for raw in self._acquire(ctx):
            item = self._to_item(raw)
            if item is None:
                continue
            if cfg.include_types and item.item_kind not in cfg.include_types:
                # Still record the id so the reconcile sweep never deletes an
                # item that exists upstream but is merely out of current scope.
                live_ids.add(item.external_id)
                continue
            live_ids.add(item.external_id)
            yield item
            seen += 1
            if seen % cfg.checkpoint_every == 0:
                cursor["items_seen"] = seen
                yield Checkpoint(Cursor(dict(cursor)), note=f"after {seen} items")

        cursor["items_seen"] = seen
        yield Checkpoint(Cursor(dict(cursor)), note="final")
        yield ReconcileMarker(live_ids=live_ids)

    # -- acquisition (the only browser-touching part; overridden in tests) --

    def _acquire(self, ctx: RunContext) -> Iterator[dict[str, Any]]:
        """Walk the saved feed and yield one raw ``SavedItem``-shaped dict per item.

        Playwright is imported lazily here so the module imports without the
        optional ``dbs[reddit]`` extra. Raises :class:`ConnectorConfigError` if
        the dependency is missing or the session directory is invalid.
        """
        cfg: RedditConfig = ctx.config  # type: ignore[assignment]
        try:
            from playwright.sync_api import sync_playwright
        except ImportError as exc:  # pragma: no cover - exercised only without the extra
            raise ConnectorConfigError(
                "the Reddit connector needs Playwright; install it with "
                "`pip install 'daily-backup-system[reddit]'` and run "
                "`playwright install chromium`."
            ) from exc

        session_dir = Path(ctx.secrets.get(cfg.session_dir_env)).expanduser()
        if not session_dir.exists():
            raise ConnectorConfigError(
                f"Reddit session directory {session_dir} does not exist; log in "
                f"once (e.g. `reddit-saved -u {cfg.username} --login`) to create it."
            )

        with sync_playwright() as pw:
            context = pw.chromium.launch_persistent_context(
                user_data_dir=str(session_dir), headless=cfg.headless
            )
            try:
                page = context.new_page()
                yield from self._walk_saved_feed(page, cfg, ctx)
            finally:
                context.close()

    # -- pagination walk (adapted from reddit_saved_extractor.walk_saved_feed) --

    def _walk_saved_feed(self, page: Any, cfg: RedditConfig, ctx: RunContext) -> Iterator[dict[str, Any]]:
        from ..core.errors import ConnectorAuthError

        url = f"https://www.reddit.com/user/{cfg.username}/saved/"
        ctx.logger.info("reddit: navigating to %s", url)
        page.goto(url, wait_until="domcontentloaded", timeout=30000)
        page.wait_for_timeout(3000)

        if "login" in page.url or "register" in page.url:
            raise ConnectorAuthError(
                "Reddit redirected to login — the saved session is missing or "
                "expired. Re-run a logged-in capture to refresh the session dir."
            )

        seen_ids: set[str] = set()
        pages_loaded = 0
        consecutive_empty = 0

        while pages_loaded < cfg.max_pages:
            for raw in self._extract_page(page):
                rid = raw.get("id")
                if rid and rid not in seen_ids:
                    seen_ids.add(rid)
                    yield raw

            pages_loaded += 1
            before = len(seen_ids)

            partial = page.query_selector(
                "faceplate-partial[src*='profile_saved-more-posts']"
            ) or page.query_selector("faceplate-partial[src*='more-posts']")
            if not partial:
                break

            partial.scroll_into_view_if_needed()
            page.wait_for_timeout(int(cfg.delay * 1000))
            try:
                page.wait_for_selector(
                    "shreddit-post, shreddit-comment", state="attached", timeout=10000
                )
                page.wait_for_timeout(1000)
            except Exception:  # noqa: BLE001 - timeout likely means end of feed
                pass
            page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
            page.wait_for_timeout(int(cfg.delay * 500))

            if len(seen_ids) == before:
                consecutive_empty += 1
                if consecutive_empty >= 3:
                    break
            else:
                consecutive_empty = 0

    @staticmethod
    def _extract_page(page: Any) -> Iterator[dict[str, Any]]:
        """Yield raw dicts for every shreddit-post / shreddit-comment in the DOM."""
        for el in page.query_selector_all("shreddit-post"):
            rec = RedditConnector._parse_post(el)
            if rec:
                yield rec
        for el in page.query_selector_all("shreddit-comment"):
            rec = RedditConnector._parse_comment(el)
            if rec:
                yield rec

    @staticmethod
    def _parse_post(el: Any) -> dict[str, Any] | None:
        fullname = (
            el.get_attribute("fullname")
            or el.get_attribute("thingid")
            or el.get_attribute("id")
            or ""
        )
        permalink = el.get_attribute("permalink") or ""
        if not fullname and not permalink:
            return None
        content_href = el.get_attribute("content-href") or ""
        subreddit = el.get_attribute("subreddit-prefixed-name") or ""
        if subreddit and not subreddit.startswith("r/"):
            subreddit = f"r/{subreddit}"
        item_type = "comment" if fullname.startswith("t1_") else "post"
        return {
            "id": fullname,
            "item_type": item_type,
            "title": el.get_attribute("post-title") or "",
            "subreddit": subreddit,
            "author": el.get_attribute("author") or "",
            "permalink": RedditConnector._abs(permalink),
            "url": content_href if content_href != permalink else "",
            "score": _safe_int(el.get_attribute("score")),
            "num_comments": _safe_int(el.get_attribute("comment-count")),
            "flair": el.get_attribute("flair-text") or "",
            "created_utc": el.get_attribute("created-timestamp") or "",
            "selftext": "",
            "comment_body": "",
            "thumbnail": "",
        }

    @staticmethod
    def _parse_comment(el: Any) -> dict[str, Any] | None:
        fullname = el.get_attribute("thingid") or el.get_attribute("id") or ""
        permalink = el.get_attribute("permalink") or ""
        if not fullname:
            return None
        return {
            "id": fullname,
            "item_type": "comment",
            "title": "",
            "subreddit": "",
            "author": el.get_attribute("author") or "",
            "permalink": RedditConnector._abs(permalink),
            "url": "",
            "score": _safe_int(el.get_attribute("score")),
            "num_comments": 0,
            "flair": "",
            "created_utc": "",
            "selftext": "",
            "comment_body": "",
            "thumbnail": "",
        }

    @staticmethod
    def _abs(permalink: str) -> str:
        if permalink and not permalink.startswith("http"):
            return f"https://www.reddit.com{permalink}"
        return permalink

    # -- mapping (pure; the part tests assert on) ---------------------------

    def _to_item(self, raw: dict[str, Any]) -> BackupItem | None:
        ext_id = str(raw.get("id") or "").strip()
        if not ext_id:
            return None
        kind = "comment" if raw.get("item_type") == "comment" else "post"
        tags = [t for t in (raw.get("subreddit"), raw.get("flair")) if t]
        thumb = raw.get("thumbnail")
        media = [MediaRef(url=thumb, kind="image")] if thumb else []
        return BackupItem(
            external_id=ext_id,
            item_kind=kind,
            raw=raw,
            title=raw.get("title") or None,
            url=raw.get("permalink") or raw.get("url") or None,
            body=raw.get("selftext") or raw.get("comment_body") or None,
            tags=tags,
            created_at=parse_iso(raw.get("created_utc") or None),
            media=media,
        )


def _safe_int(val: Any) -> int:
    import re

    try:
        return int(re.sub(r"[^\d\-]", "", str(val or "0")))
    except (ValueError, TypeError):
        return 0


__all__ = ["RedditConnector", "RedditConfig"]
