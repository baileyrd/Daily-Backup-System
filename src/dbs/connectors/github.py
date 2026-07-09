"""GitHub connector — backs up your starred repositories and gists.

A clean template-A source (REST + token + real incremental cursor, like
Raindrop) with one asymmetry worth reading:

* **Stars** have no server-side ``since`` filter, but the listing sorts by
  when *you* starred (``sort=created&direction=desc`` with the
  ``application/vnd.github.star+json`` media type, which adds ``starred_at``
  to each entry). Incremental mode pages newest-first and early-stops once
  ``starred_at`` drops below the stored high-water mark — Raindrop's exact
  fast path.
* **Gists** DO have a real delta filter (``GET /gists?since=ISO``, matched
  against ``updated_at``), so their incremental mode is a genuine
  server-side query.

Identity uses immutable numeric/hash ids (``star:<repo id>``,
``gist:<gist id>``) so a repository rename never forks the item. ``raw``
holds the verbatim API entry; for stars, the ``repo`` object is declared
volatile — GitHub mutates its counters (stargazers, forks, pushed_at, …)
constantly, which would otherwise spawn a revision per reconcile per repo.
Meaningful changes (rename, description, topics, language) still surface
through the semantic projection (title/url/body/tags), which IS hashed.

Deletion detection: a full/reconcile run enumerates both kinds and yields
one ``ReconcileMarker``. If either kind is disabled in config the marker is
withheld entirely — an enumeration that deliberately skipped a kind must
never offer that kind's stored items up for sweeping.

Auth: a personal access token (classic or fine-grained) in ``GITHUB_TOKEN``.
Needed scopes: none for public data; ``gist`` (classic) or Gists read
(fine-grained) to include secret gists.

Live-verification note: built against GitHub's documented v3 REST behavior
and fully covered by offline transport tests; not yet exercised against a
real account from this environment.
"""

from __future__ import annotations

from datetime import timedelta
from typing import TYPE_CHECKING, Any, ClassVar, Iterator

import httpx
from pydantic import BaseModel, ConfigDict, Field

from ..core import (
    BackupItem,
    Capabilities,
    Checkpoint,
    Connector,
    ConnectorAuthError,
    Cursor,
    ItemKind,
    RateLimitedError,
    ReconcileMarker,
    TransientFetchError,
)
from ..core.timeutil import parse_iso

if TYPE_CHECKING:  # pragma: no cover
    from ..core.models import FetchEvent, RunContext

_API = "https://api.github.com"
_STARS_ACCEPT = "application/vnd.github.star+json"  # adds starred_at per entry
_ACCEPT = "application/vnd.github+json"
_API_VERSION = "2022-11-28"
# Re-fetch window below the stored watermark, mirroring raindrop's overlap:
# clocks skew and pagination races; the idempotent upsert dedups the overlap.
_OVERLAP_SECONDS = 300


class GitHubConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    include_stars: bool = Field(
        True, description="Back up starred repositories."
    )
    include_gists: bool = Field(
        True, description="Back up your gists (secret ones need the gist scope)."
    )
    page_size: int = Field(100, ge=1, le=100, description="API page size (max 100).")
    token_env: str = Field(
        "GITHUB_TOKEN",
        description="Env var holding the personal access token.",
    )


class GitHubConnector(Connector):
    type = "github"
    display_name = "GitHub"
    description = "Backs up your starred repositories and gists."
    docs_url = "https://docs.github.com/en/rest"
    config_model: ClassVar[type[BaseModel]] = GitHubConfig
    secret_keys: ClassVar[tuple[str, ...]] = ("GITHUB_TOKEN",)
    item_kinds: ClassVar[tuple[ItemKind, ...]] = (
        ItemKind("star", "Starred repository"),
        ItemKind("gist", "Gist"),
    )
    wants_managed_http: ClassVar[bool] = True
    # The nested repo object churns constantly (counters, pushed_at, ...);
    # semantic fields (title/url/body/tags) still catch meaningful edits.
    volatile_fields: ClassVar[tuple[str, ...]] = ("repo",)
    capabilities: ClassVar[Capabilities] = Capabilities(
        supports_incremental=True,
        supports_ordered_cursor=True,
        cursor_kind="timestamp",
        supports_full_enumeration=True,
        supports_native_deletes=False,
        produces_media=False,
        requires_auth=True,
        supports_rate_limit_backoff=True,
        paginated=True,
    )

    # -- fetch ----------------------------------------------------------------

    def fetch(self, ctx: "RunContext") -> Iterator["FetchEvent"]:
        cfg: GitHubConfig = ctx.config  # type: ignore[assignment]
        if cfg.token_env not in self.secret_keys:
            from ..core import ConnectorConfigError

            raise ConnectorConfigError(
                f"token_env={cfg.token_env!r} must be one of the declared "
                f"secret_keys {self.secret_keys}"
            )
        token = ctx.secrets.get(cfg.token_env)
        headers = {
            "Authorization": f"Bearer {token}",
            "X-GitHub-Api-Version": _API_VERSION,
        }
        full = ctx.mode in ("full", "reconcile")
        cursor = dict(ctx.cursor.value) if ctx.cursor else {}
        live_ids: set[str] = set()

        if cfg.include_stars:
            yield from self._fetch_stars(ctx, cfg, headers, cursor, full, live_ids)
        if cfg.include_gists:
            yield from self._fetch_gists(ctx, cfg, headers, cursor, full, live_ids)

        if full:
            if cfg.include_stars and cfg.include_gists:
                yield ReconcileMarker(live_ids=live_ids)
            else:
                # A deliberately-partial enumeration must never offer the
                # skipped kind's stored items up for the sweep.
                ctx.logger.warning(
                    "github: a kind is disabled (include_stars=%s, "
                    "include_gists=%s) — deletion detection skipped",
                    cfg.include_stars, cfg.include_gists,
                )

    # -- stars ---------------------------------------------------------------

    def _fetch_stars(
        self, ctx: "RunContext", cfg: GitHubConfig, headers: dict[str, str],
        cursor: dict[str, Any], full: bool, live_ids: set[str],
    ) -> Iterator["FetchEvent"]:
        high = None if full else parse_iso(cursor.get("stars_high_watermark") or "")
        max_seen: str | None = cursor.get("stars_high_watermark")
        page = 1
        stop = False
        while not stop:
            batch = self._get_json(
                ctx, f"{_API}/user/starred",
                params={
                    "sort": "created", "direction": "desc",
                    "per_page": cfg.page_size, "page": page,
                },
                headers={**headers, "Accept": _STARS_ACCEPT},
            )
            if not isinstance(batch, list) or not batch:
                break
            for entry in batch:
                starred_at = str(entry.get("starred_at") or "")
                ts = parse_iso(starred_at)
                if high is not None and ts is not None:
                    if ts < high - timedelta(seconds=_OVERLAP_SECONDS):
                        stop = True
                        break
                item = self._star_item(entry)
                if item is None:
                    continue
                live_ids.add(item.external_id)
                if max_seen is None or starred_at > max_seen:
                    max_seen = starred_at
                yield item
            # Mid-phase checkpoints carry the OLD watermark: advancing it
            # before the walk completes would let a crash skip the gap
            # between the old mark and the last committed page forever.
            yield Checkpoint(Cursor(dict(cursor)), note=f"stars page {page}")
            if len(batch) < cfg.page_size:
                break
            page += 1
        if max_seen:
            cursor["stars_high_watermark"] = max_seen
            yield Checkpoint(Cursor(dict(cursor)), note="stars done")

    @staticmethod
    def _star_item(entry: dict[str, Any]) -> BackupItem | None:
        repo = entry.get("repo") or {}
        repo_id = repo.get("id")
        if repo_id is None:
            return None
        topics = [t for t in (repo.get("topics") or []) if isinstance(t, str)]
        if repo.get("language"):
            topics.append(str(repo["language"]))
        return BackupItem(
            external_id=f"star:{repo_id}",
            item_kind="star",
            raw={"starred_at": entry.get("starred_at"), "repo": repo},
            title=repo.get("full_name"),
            url=repo.get("html_url"),
            body=repo.get("description"),
            tags=topics,
            created_at=parse_iso(str(entry.get("starred_at") or "")),
            updated_at=None,
        )

    # -- gists ---------------------------------------------------------------

    def _fetch_gists(
        self, ctx: "RunContext", cfg: GitHubConfig, headers: dict[str, str],
        cursor: dict[str, Any], full: bool, live_ids: set[str],
    ) -> Iterator["FetchEvent"]:
        since = None if full else cursor.get("gists_high_watermark")
        max_seen: str | None = cursor.get("gists_high_watermark")
        page = 1
        while True:
            params: dict[str, Any] = {"per_page": cfg.page_size, "page": page}
            if since:
                params["since"] = since  # server-side delta on updated_at
            batch = self._get_json(
                ctx, f"{_API}/gists", params=params,
                headers={**headers, "Accept": _ACCEPT},
            )
            if not isinstance(batch, list) or not batch:
                break
            for gist in batch:
                gid = gist.get("id")
                if not gid:
                    continue
                item = self._gist_item(gist)
                live_ids.add(item.external_id)
                updated = str(gist.get("updated_at") or "")
                if updated and (max_seen is None or updated > max_seen):
                    max_seen = updated
                yield item
            yield Checkpoint(Cursor(dict(cursor)), note=f"gists page {page}")
            if len(batch) < cfg.page_size:
                break
            page += 1
        if max_seen:
            cursor["gists_high_watermark"] = max_seen
            yield Checkpoint(Cursor(dict(cursor)), note="gists done")

    @staticmethod
    def _gist_item(gist: dict[str, Any]) -> BackupItem:
        files = gist.get("files") or {}
        title = gist.get("description") or ", ".join(sorted(files)) or gist["id"]
        return BackupItem(
            external_id=f"gist:{gist['id']}",
            item_kind="gist",
            raw=gist,
            title=title,
            url=gist.get("html_url"),
            body=None,
            tags=sorted({
                str(f.get("language")) for f in files.values()
                if isinstance(f, dict) and f.get("language")
            }),
            created_at=parse_iso(str(gist.get("created_at") or "")),
            updated_at=parse_iso(str(gist.get("updated_at") or "")),
        )

    # -- transport ------------------------------------------------------------

    @staticmethod
    def _get_json(
        ctx: "RunContext", url: str, *, params: dict[str, Any],
        headers: dict[str, str],
    ) -> Any:
        try:
            resp = ctx.http.get(url, params=params, headers=headers)
        except httpx.HTTPStatusError as err:  # non-429 4xx from the managed client
            status = err.response.status_code
            if status == 401:
                raise ConnectorAuthError(
                    "GitHub rejected the token (401) — check GITHUB_TOKEN"
                ) from err
            if status == 403:
                if err.response.headers.get("X-RateLimit-Remaining") == "0":
                    raise RateLimitedError(
                        "GitHub API rate limit exhausted — the next scheduled "
                        "run resumes from the last checkpoint"
                    ) from err
                raise ConnectorAuthError(
                    "GitHub returned 403 — the token lacks access "
                    "(secret gists need the gist scope)"
                ) from err
            raise TransientFetchError(f"GitHub API error {status}") from err
        return resp.json()


__all__ = ["GitHubConnector", "GitHubConfig"]
