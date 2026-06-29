"""Connector capability declarations.

Each connector declares a frozen :class:`Capabilities` instance describing what
it can and cannot do. The engine consults these flags to decide its behavior
(may it run incrementally? may it soft-delete missing items? should it persist
media?). Declaring capabilities up front — and validating their coherence at
registration time — means "declared X but didn't implement X" is caught when the
plugin loads, not deep inside a backup run.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class ItemKind:
    """One entry in a connector's item taxonomy (e.g. ``link``, ``post``)."""

    name: str
    display_name: str
    description: str = ""


@dataclass(frozen=True, slots=True)
class AuthCapture:
    """Declares that a connector's auth artifact can be captured interactively.

    Pure metadata — no UI code lives in the connector. A UI tier (the web app)
    reads this to offer a "capture login" action and knows *what* to capture and
    *where to record it*; it owns the actual browser automation per ``kind``.

    kind
        ``"browser_session"`` — a Playwright persistent-context directory holding
        a logged-in session (e.g. Reddit), or ``"browser_cookies"`` — a Netscape
        ``cookies.txt`` exported after login (e.g. YouTube for yt-dlp).
    secret_key
        The ``secret_keys`` env name the captured path is written to (in ``.env``).
    login_url
        The page the capture browser should open for the user to log in.
    label
        Human label for the action button.
    """

    kind: str
    secret_key: str
    login_url: str = ""
    label: str = ""


@dataclass(frozen=True, slots=True)
class Capabilities:
    """Declarative description of a connector's behavior.

    Attributes
    ----------
    supports_incremental:
        The connector can fetch only what changed since a prior cursor.
    supports_ordered_cursor:
        The cursor advances monotonically (newest-first early-stop is safe).
    cursor_kind:
        Human label for the cursor's nature (``"opaque"``, ``"timestamp"``, ...).
        The engine never interprets the cursor regardless of this value.
    supports_full_enumeration:
        The connector can list *all* live ids (a precondition for safe deletion
        detection via :class:`~dbs.core.models.ReconcileMarker`).
    supports_native_deletes:
        The connector can report deletions directly (e.g. a trash feed). When
        false the engine will never mark items deleted from this connector's
        normal output.
    produces_media:
        Items may carry :class:`~dbs.core.models.MediaRef` references worth
        persisting.
    media_inline:
        Media bytes are embedded inline (vs referenced by URL).
    items_mutable:
        Existing items can change upstream (so re-fetch may produce revisions).
    requires_auth:
        The connector needs at least one secret to operate.
    supports_rate_limit_backoff:
        The connector/HTTP layer honors rate-limit backoff (429/Retry-After).
    paginated:
        The source is paginated.
    """

    supports_incremental: bool = False
    supports_ordered_cursor: bool = False
    cursor_kind: str = "opaque"
    supports_full_enumeration: bool = False
    supports_native_deletes: bool = False
    produces_media: bool = False
    media_inline: bool = False
    items_mutable: bool = True
    requires_auth: bool = True
    supports_rate_limit_backoff: bool = False
    paginated: bool = True

    def assert_coherent(self) -> None:
        """Raise :class:`ValueError` on internally contradictory flag combinations."""
        if self.supports_ordered_cursor and not self.supports_incremental:
            raise ValueError(
                "supports_ordered_cursor=True requires supports_incremental=True"
            )
        if self.media_inline and not self.produces_media:
            raise ValueError("media_inline=True requires produces_media=True")


__all__ = ["Capabilities", "ItemKind", "AuthCapture"]
