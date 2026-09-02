"""The watched-source-github trigger source.

Watches GitHub repositories and emits typed events — ``new_release`` and
``new_issue`` — as ``SourceEvent``s through ``TriggerSourceProvider`` from
``personalclaw.sdk.trigger_source``. Core namespaces them
(``app:watched-source-github:new_release``) and matches them against
``kind: event`` triggers.

Design notes worth copying into your own source:

- **Push contract, self-owned loop.** Core never polls a source; ``start``
  spawns this provider's own ``asyncio`` task and returns immediately (blocking
  there would stall app enable). ``stop`` cancels it and is idempotent.
- **First observation plants a high-water mark.** The first poll of a repo
  records what exists and emits nothing — a freshly-watched repo must not
  flood history as "new".
- **Declared events.** ``events`` is the browsable vocabulary a user picks
  from when authoring a trigger; emit only names declared there.
- **Stdlib only, degrade to silence.** One ``urllib`` call per repo per poll;
  a network/API failure logs and skips the round — a watcher must never take
  the trigger bus down with it.
"""

from __future__ import annotations

import asyncio
import json
import logging
import urllib.error
import urllib.request
from collections.abc import Callable
from typing import Any

from personalclaw.sdk.trigger_source import SourceEvent, TriggerSourceProvider

logger = logging.getLogger("watched_source_github")

DEFAULT_API_BASE = "https://api.github.com"
DEFAULT_POLL_SECS = 300
MIN_POLL_SECS = 60


class WatchedSourceGithubProvider(TriggerSourceProvider):
    name = "watched-source-github"
    display_name = "GitHub Repo Watcher"

    def __init__(self, config: dict[str, Any] | None = None) -> None:
        self._config = dict(config or {})
        self._timeout = int(self._config.get("timeout_secs", 20))
        self._token = str(self._config.get("token", "") or "").strip()
        self._api_base = str(
            self._config.get("api_base", DEFAULT_API_BASE) or DEFAULT_API_BASE
        ).rstrip("/")
        self._poll_secs = max(
            MIN_POLL_SECS, int(self._config.get("poll_interval_secs", DEFAULT_POLL_SECS))
        )
        self._repos = [
            r.strip()
            for r in str(self._config.get("repos", "") or "").split(",")
            if r.strip()
        ]
        self._task: asyncio.Task[None] | None = None
        # Per-repo high-water marks: latest seen release id / issue timestamp.
        self._seen_release: dict[str, str] = {}
        self._seen_issue_at: dict[str, str] = {}

    # ── Contract ──────────────────────────────────────────────────────────

    @property
    def events(self) -> tuple[str, ...]:
        return ("new_release", "new_issue")

    async def start(self, emit: Callable[[SourceEvent], None]) -> None:
        """Spawn the watch loop and return — never block the enable path."""
        if self._task is not None and not self._task.done():
            return
        self._task = asyncio.get_running_loop().create_task(self._watch(emit))

    async def stop(self) -> None:
        """Cancel the loop and release it. Idempotent."""
        task, self._task = self._task, None
        if task is None or task.done():
            return
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    # ── Watch loop ────────────────────────────────────────────────────────

    async def _watch(self, emit: Callable[[SourceEvent], None]) -> None:
        while True:
            for repo in self._repos:
                try:
                    for event in await asyncio.to_thread(self._poll_repo, repo):
                        emit(event)
                except Exception as exc:  # degrade to silence, never die
                    logger.warning("watch %s failed (%s) — skipping round", repo, exc)
            await asyncio.sleep(self._poll_secs)

    def _poll_repo(self, repo: str) -> list[SourceEvent]:
        """One synchronous poll of one repo (runs in a thread)."""
        events: list[SourceEvent] = []
        events.extend(self._poll_release(repo))
        events.extend(self._poll_issues(repo))
        return events

    def _poll_release(self, repo: str) -> list[SourceEvent]:
        data = self._get(f"/repos/{repo}/releases/latest")
        if not isinstance(data, dict) or "id" not in data:
            return []
        release_id = str(data["id"])
        previous, self._seen_release[repo] = self._seen_release.get(repo), release_id
        if previous is None or previous == release_id:
            return []  # first observation, or nothing new
        tag = str(data.get("tag_name", "") or "")
        title = str(data.get("name", "") or tag)
        return [
            SourceEvent(
                event="new_release",
                key=release_id,
                text=f"{repo} released {tag}: {title}",
                meta={"repo": repo, "tag": tag, "url": str(data.get("html_url", ""))},
            )
        ]

    def _poll_issues(self, repo: str) -> list[SourceEvent]:
        since = self._seen_issue_at.get(repo)
        query = "?state=open&sort=created&direction=desc&per_page=20"
        if since:
            query += f"&since={since}"
        data = self._get(f"/repos/{repo}/issues{query}")
        if not isinstance(data, list):
            return []
        newest = since or ""
        events: list[SourceEvent] = []
        for issue in data:
            if not isinstance(issue, dict) or "pull_request" in issue:
                continue  # the issues API interleaves PRs; watch issues only
            created = str(issue.get("created_at", "") or "")
            if created > newest:
                newest = created
            if since is None or created <= since:
                continue  # first observation plants the mark; older rows skipped
            number = str(issue.get("number", "") or "")
            events.append(
                SourceEvent(
                    event="new_issue",
                    key=f"{repo}#{number}",
                    text=f"{repo}#{number}: {str(issue.get('title', '') or '')}",
                    meta={
                        "repo": repo,
                        "number": number,
                        "author": str((issue.get("user") or {}).get("login", "")),
                        "url": str(issue.get("html_url", "") or ""),
                    },
                )
            )
        if newest:
            self._seen_issue_at[repo] = newest
        return events

    def _get(self, path: str) -> Any:
        headers = {
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "personalclaw-watched-source-github",
        }
        if self._token:
            headers["Authorization"] = f"Bearer {self._token}"
        request = urllib.request.Request(f"{self._api_base}{path}", headers=headers)
        try:
            with urllib.request.urlopen(request, timeout=self._timeout) as response:
                return json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            if exc.code == 404:  # repo with no releases yet — a normal state
                return None
            raise


def create_provider(config: dict[str, Any] | None = None) -> WatchedSourceGithubProvider:
    """Manifest factory — core calls this with this app's saved settings."""
    return WatchedSourceGithubProvider(config)
