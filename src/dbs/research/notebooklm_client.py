"""Thin async wrapper isolating every call into the unofficial ``notebooklm-py``
community library, so :mod:`dbs.research.pipeline` never imports it directly.

``notebooklm-py`` is lazily imported (never at module top level) so the rest of
``dbs`` stays importable without the ``[research]`` extra installed, matching
every existing connector's lazy-import discipline.

Auth is deliberately **not** managed by this repo's ``Secrets``/``AuthCapture``
machinery: ``notebooklm-py`` keeps its own Playwright-captured browser session,
written by running ``notebooklm login`` once, out-of-band, by the user. This
client only reads that session via ``NotebookLMClient.from_storage()``.

Every method signature below was confirmed by introspecting the actually
installed package with ``inspect.signature()`` (not just its README) —
``notebooklm.AuthError`` is fatal and deliberately left to propagate unchanged
so the CLI layer can print a `notebooklm login` pointer instead of a raw
traceback; the narrower per-source errors (``SourceAddError`` etc.) are caught
here since a single bad source shouldn't abort an otherwise-successful run.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from .models import ResearchPipelineError


class SourceIndexError(Exception):
    """One video failed to index into NotebookLM. Caught per-video by
    ``pipeline.py`` (tracked in an ``IndexOutcome``, never aborts the run) —
    deliberately narrower than the fatal ``notebooklm.AuthError``, which is
    left to propagate unchanged."""


def _import_notebooklm() -> Any:
    try:
        import notebooklm
    except ImportError as exc:  # pragma: no cover - exercised only without the extra
        raise ResearchPipelineError(
            "the research pipeline needs notebooklm-py; install it with "
            "`pip install 'daily-backup-system[research]'`, then run "
            "`notebooklm login` once to authenticate."
        ) from exc
    return notebooklm


# Where the DBS web UI's "NotebookLM login" capture writes the Playwright
# storageState, relative to the config dir. Same file format `notebooklm
# login` produces — either source of the file works.
DBS_STATE_SUBPATH = Path(".notebooklm") / "storage_state.json"


def resolve_auth_state(base_dir: str | Path) -> str | None:
    """The DBS-captured storage-state path if it exists, else ``None``.

    ``None`` means "let notebooklm-py use its own default" — the file that
    ``notebooklm login`` writes (``~/.notebooklm/…/storage_state.json``). The
    same Google session powers both; DBS's capture is just the in-UI way to
    produce it.
    """
    candidate = Path(base_dir) / DBS_STATE_SUBPATH
    return str(candidate) if candidate.exists() else None


def default_state_present() -> bool:
    """Whether ``notebooklm login``'s own storage state exists (best-effort;
    ``False`` when the package isn't installed)."""
    try:
        from notebooklm.paths import get_storage_path
    except ImportError:
        return False
    try:
        return Path(get_storage_path()).exists()
    except Exception:
        return False


def client_context(auth_state_path: str | None = None):
    """Return the ``async with`` context manager for a fresh client.

    ``auth_state_path`` points at a Playwright storageState JSON (e.g. the one
    the DBS web UI captured); ``None`` falls back to the file ``notebooklm
    login`` wrote at its default location.
    """
    notebooklm = _import_notebooklm()
    return notebooklm.NotebookLMClient.from_storage(path=auth_state_path)


async def create_notebook(client: Any, title: str) -> Any:
    return await client.notebooks.create(title=title)


async def add_source(client: Any, notebook_id: str, url: str) -> None:
    """Add one URL source and wait for it to finish indexing.

    Per-source failures (``SourceAddError``/``SourceProcessingError``/
    ``SourceTimeoutError``) are caught here and re-raised as
    :class:`SourceIndexError`, which the caller catches per-video and tracks
    in an :class:`~dbs.research.models.IndexOutcome` rather than aborting the
    whole run. ``notebooklm.AuthError`` is a different failure mode entirely
    (the whole session is unusable) and is deliberately NOT caught here — it
    propagates to the CLI boundary unchanged.
    """
    notebooklm = _import_notebooklm()
    from notebooklm.exceptions import (
        SourceAddError,
        SourceProcessingError,
        SourceTimeoutError,
    )

    try:
        await client.sources.add_url(notebook_id, url, wait=True, wait_timeout=120.0)
    except (SourceAddError, SourceProcessingError, SourceTimeoutError) as exc:
        raise SourceIndexError(str(exc)) from exc


async def ask(client: Any, notebook_id: str, question: str) -> str:
    result = await client.chat.ask(notebook_id, question)
    return result.answer


async def generate_infographic(
    client: Any,
    notebook_id: str,
    output_path: str,
    orientation: str = "landscape",
) -> str:
    """Kick off infographic generation, wait for it, download it. Returns the
    path it was written to."""
    notebooklm = _import_notebooklm()
    orientation_value = getattr(notebooklm, "InfographicOrientation", None)
    orientation_arg = (
        orientation_value(orientation) if orientation_value is not None else orientation
    )
    status = await client.artifacts.generate_infographic(
        notebook_id, orientation=orientation_arg
    )
    status = await client.artifacts.wait_for_completion(notebook_id, status.task_id)
    return await client.artifacts.download_infographic(notebook_id, output_path)


def is_auth_error(exc: BaseException) -> bool:
    """Whether ``exc`` is a real ``notebooklm.AuthError``.

    Used by ``pipeline.run_pipeline`` to re-wrap it as the ``dbs``-owned
    :class:`~dbs.research.models.NotebookLMAuthError` so ``cli.py`` can catch
    a single, always-importable exception type instead of reaching into
    ``notebooklm`` itself.
    """
    try:
        import notebooklm
    except ImportError:  # pragma: no cover - only without the extra installed
        return False
    return isinstance(exc, notebooklm.AuthError)


__all__ = [
    "SourceIndexError",
    "DBS_STATE_SUBPATH",
    "resolve_auth_state",
    "default_state_present",
    "client_context",
    "create_notebook",
    "add_source",
    "ask",
    "generate_infographic",
    "is_auth_error",
]
