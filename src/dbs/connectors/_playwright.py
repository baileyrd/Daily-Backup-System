"""Shared Playwright helpers for the browser-session connectors.

Like ``_util``/``_tiptap``: a private implementation detail of the built-in
connectors, not part of the ``dbs.core`` contract — third-party connector
packages should copy what they need rather than import from here.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any


def launch_scrubbed_context(pw: Any, session_dir: Path, *, headless: bool) -> Any:
    """Launch a captured persistent profile, dressed as a regular Chrome.

    Headless Chromium advertises ``HeadlessChrome/<ver>`` in its user agent —
    an instant bot signal to anti-automation edges (Reddit's, and plausibly
    others). Probe the launched browser's own UA and, if needed, relaunch
    once with the token scrubbed (version-exact by construction; Playwright
    derives the client-hint metadata from the supplied UA, so brands stay
    consistent). This lived copy-pasted in both the reddit and skool
    connectors; anti-bot handling must evolve in exactly one place.
    """
    kwargs: dict[str, Any] = dict(
        user_data_dir=str(session_dir),
        headless=headless,
        args=["--disable-blink-features=AutomationControlled"],
    )
    context = pw.chromium.launch_persistent_context(**kwargs)
    probe = context.pages[0] if context.pages else context.new_page()
    ua = probe.evaluate("() => navigator.userAgent")
    if "HeadlessChrome" in ua:
        context.close()
        kwargs["user_agent"] = ua.replace("HeadlessChrome", "Chrome")
        context = pw.chromium.launch_persistent_context(**kwargs)
    return context


__all__ = ["launch_scrubbed_context"]
