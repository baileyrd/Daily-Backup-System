"""Reddit connector — backs up your *saved* posts and comments.

Saved feeds are private and Reddit offers no OAuth-free REST endpoint for them —
but its **cookie-authenticated JSON listings** work with a logged-in browser
session: ``GET /user/<name>/saved.json`` pages through the exact same data the
site renders, with real HTTP statuses instead of scrape-able markup. So this
connector loads your captured **persistent browser session** (Playwright) and
uses its request context (which shares the profile's cookies) to page the JSON
feed — no DOM scraping, no dependence on Reddit's ``shreddit`` web components.

Login is verified up front via ``GET /api/me.json``: logged-out sessions return
an empty body, which raises a clear :class:`ConnectorAuthError` instead of the
former failure mode (an empty saved page silently backed up as "0 items,
success"). The authenticated account name from that same response is used for
the feed URL, so the ``username`` config option is now just an optional
cross-check (a mismatch warns; the real account wins).

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
persistent-context directory holding your logged-in cookies (captured via the
web UI's "Reddit login" button, or any headed Playwright login). Nothing in the
core treats a secret as a token — a filesystem path works fine.

Heavy dependencies (Playwright) are imported **lazily** inside :meth:`_acquire`
so the module always imports cleanly and the connector stays discoverable even
when the optional ``dbs[reddit]`` extra is not installed; a missing dependency
surfaces as a clear :class:`ConnectorConfigError` at run time.
"""

from __future__ import annotations

import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

from pydantic import BaseModel, ConfigDict, Field

from ..core import (
    AuthCapture,
    BackupItem,
    Capabilities,
    Checkpoint,
    ConnectorAuthError,
    ConnectorConfigError,
    Connector,
    Cursor,
    ItemKind,
    MediaRef,
    RateLimitedError,
    ReconcileMarker,
    RunContext,
    TransientFetchError,
    iso_z,
    parse_iso,
)
from ._util import ext_for_mime

_TYPES = ("post", "comment")


class RedditConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    # Optional cross-check only: the account actually backed up is whichever
    # one the captured session is logged in as (detected via /api/me.json); a
    # mismatch with this value logs a loud warning but the real account wins.
    username: str | None = None
    include_types: list[str] = list(_TYPES)
    max_pages: int = Field(default=100, ge=1)
    delay: float = Field(default=2.0, ge=0.0)
    headless: bool = True
    checkpoint_every: int = Field(default=200, ge=1)
    # Name of the env var holding the path to the Playwright persistent-context
    # directory (your logged-in session). Mirrors raindrop's ``token_env``.
    session_dir_env: str = "REDDIT_SESSION_DIR"
    # Opt-in: best-effort fetch of the outbound link a saved *post* points to
    # (never attempted for comments, which have no separate outbound URL in
    # this scraper). Every backup run re-attempts this for every post that
    # has a link -- Reddit has no incremental/reconcile split (every run is
    # full), so there's no cheaper mode to defer to. Acceptable because
    # "saved" lists are typically small and human-curated, unlike Raindrop
    # collections. See _maybe_fetch_outbound_link.
    archive_outbound_link: bool = False


class RedditConnector(Connector):
    type = "reddit"
    display_name = "Reddit (saved)"
    description = "Your saved Reddit posts and comments, via a logged-in browser session."
    docs_url = "https://github.com/baileyrd/reddit_saved_extractor"
    setup_hint = (
        "Click ‘Reddit login’ to capture a session: a browser opens, you log in, "
        "and you CLOSE the window to finish. Make sure you end up logged-in ON "
        "reddit.com before closing — with ‘Continue with Google’, finish the "
        "redirect back to reddit first. The account is auto-detected from the "
        "session; the username option is just an optional cross-check."
    )
    config_model = RedditConfig
    secret_keys = ("REDDIT_SESSION_DIR",)
    # The primary acquisition step is Playwright-driven (see _acquire below)
    # and never touches ctx.http. This is only for the opt-in
    # archive_outbound_link feature, which fetches an arbitrary external URL
    # that needs no session cookies -- the browser session and ctx.http are
    # independent and coexist fine.
    wants_managed_http = True
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
        if cfg.archive_outbound_link and not ctx.store_media:
            ctx.logger.warning(
                "archive_outbound_link is set but store_media is off for this "
                "source; no outbound links will be fetched (set store_media = "
                "true in dbs.toml to actually persist them)."
            )

        live_ids: set[str] = set()
        cursor: dict[str, Any] = {}
        seen = 0

        for raw in self._acquire(ctx):
            item = self._to_item(ctx, raw)
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
        the dependency is missing or the session directory is invalid. This is
        the only Playwright-touching method: the persistent context is launched
        purely to load the captured cookies from disk, and its request context
        (``context.request``, which shares those cookies) does all the HTTP —
        no page is ever opened.
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
                f"Reddit session directory {session_dir} does not exist; capture "
                f"a login once (the web UI's ‘Reddit login’ button) to create it."
            )

        with sync_playwright() as pw:
            context = pw.chromium.launch_persistent_context(
                user_data_dir=str(session_dir), headless=cfg.headless
            )
            try:
                req = context.request
                name = self._verify_login(req, cfg, ctx)
                yield from self._walk_saved_json(req, name, cfg, ctx)
            finally:
                context.close()

    # -- authenticated JSON feed (fake-injectable: needs only req.get()) -----

    def _verify_login(self, req: Any, cfg: RedditConfig, ctx: RunContext) -> str:
        """Return the logged-in account name, or raise if the session is dead.

        ``/api/me.json`` returns ``{}`` for a logged-out session — the silent
        failure mode this check exists to make loud.
        """
        url = "https://www.reddit.com/api/me.json"
        resp = req.get(url)
        self._check_status(resp.status, url)
        try:
            body = resp.json() or {}
        except Exception as exc:  # non-JSON body (interstitial, gateway page)
            raise TransientFetchError(f"reddit: {url} returned a non-JSON body") from exc
        name = (body.get("data") or {}).get("name")
        if not name:
            raise ConnectorAuthError(
                "the captured Reddit session is not logged in — re-run the "
                "‘Reddit login’ capture. If you sign in with Google, finish the "
                "SSO redirect and make sure reddit.com shows you logged in "
                "BEFORE closing the window, so the session cookie is persisted."
            )
        if cfg.username and cfg.username.lower() != str(name).lower():
            ctx.logger.warning(
                "reddit: config username %r does not match the logged-in account "
                "u/%s — backing up the logged-in account (saved feeds are "
                "owner-only, so the config value would fetch nothing).",
                cfg.username, name,
            )
        ctx.logger.info("reddit: authenticated as u/%s", name)
        return str(name)

    def _walk_saved_json(
        self, req: Any, name: str, cfg: RedditConfig, ctx: RunContext
    ) -> Iterator[dict[str, Any]]:
        """Page through the cookie-authenticated saved listing."""
        extracted_at = iso_z(ctx.now())
        seen_ids: set[str] = set()
        after: str | None = None

        for _page in range(cfg.max_pages):
            url = f"https://www.reddit.com/user/{name}/saved.json?limit=100&raw_json=1"
            if after:
                url += f"&after={after}"
            resp = req.get(url)
            self._check_status(resp.status, url)
            try:
                data = (resp.json() or {}).get("data") or {}
            except Exception as exc:
                raise TransientFetchError(f"reddit: {url} returned a non-JSON body") from exc

            children = data.get("children") or []
            for child in children:
                rec = self._record_from_child(child, extracted_at)
                if rec is None:
                    continue
                rid = rec["id"]
                if rid and rid not in seen_ids:  # listings can repeat across pages
                    seen_ids.add(rid)
                    yield rec

            after = data.get("after")
            if not after or not children:
                break
            if cfg.delay:
                time.sleep(cfg.delay)

        if not seen_ids:
            ctx.logger.warning(
                "reddit: logged in as u/%s but the saved feed returned 0 items — "
                "either nothing is saved on this account, or Reddit served an "
                "empty listing.", name,
            )

    @staticmethod
    def _check_status(status: int, url: str) -> None:
        if 200 <= status < 300:
            return
        if status in (401, 403):
            raise ConnectorAuthError(
                f"reddit: {url} returned HTTP {status} — the session cookies were "
                f"rejected; re-run the ‘Reddit login’ capture."
            )
        if status == 429:
            raise RateLimitedError(f"reddit: rate-limited (HTTP 429) at {url}")
        raise TransientFetchError(f"reddit: HTTP {status} from {url}")

    @staticmethod
    def _record_from_child(child: dict[str, Any], extracted_at: str) -> dict[str, Any] | None:
        """Map one saved-listing child (kind t3 post / t1 comment) to the raw
        record shape the rest of this connector (and its tests) consume."""
        kind = child.get("kind")
        if kind not in ("t3", "t1"):
            return None
        d = child.get("data") or {}
        fullname = d.get("name") or ""
        if not fullname:
            return None
        permalink = RedditConnector._abs(d.get("permalink") or "")
        epoch = d.get("created_utc")
        created = (
            iso_z(datetime.fromtimestamp(epoch, tz=timezone.utc)) if epoch else ""
        )
        if kind == "t3":
            outbound = d.get("url_overridden_by_dest") or d.get("url") or ""
            if outbound == permalink:  # self post: "outbound" is just itself
                outbound = ""
            thumb = d.get("thumbnail") or ""
            if not thumb.startswith("http"):  # "self"/"default"/"nsfw"/... tokens
                thumb = ""
            return {
                "id": fullname,
                "item_type": "post",
                "title": d.get("title") or "",
                "subreddit": d.get("subreddit_name_prefixed") or "",
                "author": d.get("author") or "",
                "permalink": permalink,
                "url": outbound,
                "score": int(d.get("score") or 0),
                "num_comments": int(d.get("num_comments") or 0),
                "flair": d.get("link_flair_text") or "",
                "created_utc": created,
                "selftext": d.get("selftext") or "",
                "comment_body": "",
                "thumbnail": thumb,
                "extracted_at": extracted_at,
            }
        return {
            "id": fullname,
            "item_type": "comment",
            "title": "",
            "subreddit": d.get("subreddit_name_prefixed") or "",
            "author": d.get("author") or "",
            "permalink": permalink,
            "url": "",
            "score": int(d.get("score") or 0),
            "num_comments": 0,
            "flair": "",
            "created_utc": created,
            "selftext": "",
            "comment_body": d.get("body") or "",
            "thumbnail": "",
            "extracted_at": extracted_at,
        }

    @staticmethod
    def _abs(permalink: str) -> str:
        if permalink and not permalink.startswith("http"):
            return f"https://www.reddit.com{permalink}"
        return permalink

    # -- mapping (pure; the part tests assert on) ---------------------------

    def _to_item(self, ctx: RunContext, raw: dict[str, Any]) -> BackupItem | None:
        ext_id = str(raw.get("id") or "").strip()
        if not ext_id:
            return None
        kind = "comment" if raw.get("item_type") == "comment" else "post"
        tags = [t for t in (raw.get("subreddit"), raw.get("flair")) if t]
        thumb = raw.get("thumbnail")
        media = [MediaRef(url=thumb, kind="image")] if thumb else []

        cfg: RedditConfig = ctx.config  # type: ignore[assignment]
        outbound_url = raw.get("url") or ""
        permalink = raw.get("permalink") or ""
        if (
            cfg.archive_outbound_link
            and ctx.store_media
            and kind == "post"
            and outbound_url
            and outbound_url != permalink
        ):
            link_media = self._maybe_fetch_outbound_link(ctx, ext_id, outbound_url)
            if link_media is not None:
                media.append(link_media)

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

    def _maybe_fetch_outbound_link(
        self, ctx: RunContext, ext_id: str, url: str
    ) -> MediaRef | None:
        """Best-effort: fetch the outbound link a saved post points to.
        Returns a MediaRef with prefetched bytes, or None on any failure --
        this must never raise, since it's opportunistic enrichment and a dead
        link / timeout / non-2xx must never fail the run.

        Unlike Raindrop's permanent-copy fetch (a deliberate two-hop dance
        that drops the redirect's Authorization header before following it to
        S3), this is a single hop with no header to protect -- ctx.http.get()
        is called with no ``headers=`` at all (an arbitrary external site,
        not Reddit's own API), so it's safe -- and often necessary, given
        shorteners and http->https upgrades -- to pass follow_redirects=True.
        """
        assert ctx.http is not None
        try:
            resp = ctx.http.get(url, follow_redirects=True)
        except Exception as exc:  # noqa: BLE001 - best-effort, never raise
            ctx.logger.debug("outbound-link fetch failed for %s: %s", url, exc)
            return None
        if resp.is_error or not resp.content:
            return None
        mime = resp.headers.get("content-type")
        return MediaRef(
            url=url,
            kind="archive",
            mime=mime,
            filename=f"{ext_id}{ext_for_mime(mime)}",
            data=resp.content,
        )


__all__ = ["RedditConnector", "RedditConfig"]
