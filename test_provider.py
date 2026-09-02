"""Contract + behaviour tests for the watched-source-github trigger source.

Contract: personalclaw.sdk.trigger_source:TriggerSourceProvider

No network: behaviour tests stub ``_get`` and drive the poll helpers directly.
"""

from __future__ import annotations

import asyncio

from provider import WatchedSourceGithubProvider, create_provider

# ── Contract shape ────────────────────────────────────────────────────────


def test_factory_returns_the_provider() -> None:
    assert isinstance(create_provider({}), WatchedSourceGithubProvider)


def test_factory_accepts_no_config() -> None:
    assert isinstance(create_provider(None), WatchedSourceGithubProvider)


def test_nothing_abstract_is_left() -> None:
    assert not getattr(WatchedSourceGithubProvider, "__abstractmethods__", frozenset())


def test_registers_under_the_app_name() -> None:
    assert create_provider({}).name == "watched-source-github"


def test_declares_its_event_vocabulary() -> None:
    assert create_provider({}).events == ("new_release", "new_issue")


def test_settings_reach_the_provider() -> None:
    p = create_provider(
        {"timeout_secs": 5, "repos": "a/b, c/d", "poll_interval_secs": 90}
    )
    assert p._timeout == 5
    assert p._repos == ["a/b", "c/d"]
    assert p._poll_secs == 90


def test_poll_interval_has_a_floor() -> None:
    assert create_provider({"poll_interval_secs": 1})._poll_secs == 60


# ── Lifecycle ─────────────────────────────────────────────────────────────


def test_start_returns_immediately_and_stop_is_idempotent() -> None:
    async def scenario() -> None:
        p = create_provider({"repos": ""})
        await asyncio.wait_for(p.start(lambda e: None), timeout=1)
        assert p._task is not None and not p._task.done()
        await p.stop()
        assert p._task is None
        await p.stop()  # second stop is a no-op

    asyncio.run(scenario())


def test_double_start_keeps_one_loop() -> None:
    async def scenario() -> None:
        p = create_provider({"repos": ""})
        await p.start(lambda e: None)
        first = p._task
        await p.start(lambda e: None)
        assert p._task is first
        await p.stop()

    asyncio.run(scenario())


# ── Releases: high-water then emit ───────────────────────────────────────


def _release(release_id: int, tag: str) -> dict:
    return {
        "id": release_id,
        "tag_name": tag,
        "name": f"Release {tag}",
        "html_url": f"https://github.com/a/b/releases/tag/{tag}",
    }


def test_first_release_observation_emits_nothing() -> None:
    p = create_provider({"repos": "a/b"})
    p._get = lambda path: _release(1, "v1.0.0")
    assert p._poll_release("a/b") == []
    assert p._seen_release["a/b"] == "1"


def test_new_release_emits_after_the_mark_is_planted() -> None:
    p = create_provider({"repos": "a/b"})
    p._get = lambda path: _release(1, "v1.0.0")
    p._poll_release("a/b")
    p._get = lambda path: _release(2, "v1.1.0")
    (event,) = p._poll_release("a/b")
    assert event.event == "new_release"
    assert event.key == "2"
    assert "v1.1.0" in event.text
    assert event.meta["repo"] == "a/b"


def test_unchanged_release_stays_silent() -> None:
    p = create_provider({"repos": "a/b"})
    p._get = lambda path: _release(1, "v1.0.0")
    p._poll_release("a/b")
    assert p._poll_release("a/b") == []


def test_repo_with_no_releases_is_a_normal_state() -> None:
    p = create_provider({"repos": "a/b"})
    p._get = lambda path: None  # 404 path returns None
    assert p._poll_release("a/b") == []


# ── Issues: since-window + PR filtering ──────────────────────────────────


def _issue(number: int, created: str, title: str = "t", pr: bool = False) -> dict:
    row = {
        "number": number,
        "created_at": created,
        "title": title,
        "user": {"login": "someone"},
        "html_url": f"https://github.com/a/b/issues/{number}",
    }
    if pr:
        row["pull_request"] = {"url": "..."}
    return row


def test_first_issue_observation_plants_the_mark_silently() -> None:
    p = create_provider({"repos": "a/b"})
    p._get = lambda path: [_issue(1, "2026-09-01T00:00:00Z")]
    assert p._poll_issues("a/b") == []
    assert p._seen_issue_at["a/b"] == "2026-09-01T00:00:00Z"


def test_newer_issue_emits_and_advances_the_mark() -> None:
    p = create_provider({"repos": "a/b"})
    p._get = lambda path: [_issue(1, "2026-09-01T00:00:00Z")]
    p._poll_issues("a/b")
    p._get = lambda path: [_issue(2, "2026-09-02T00:00:00Z", "newer")]
    (event,) = p._poll_issues("a/b")
    assert event.event == "new_issue"
    assert event.key == "a/b#2"
    assert "newer" in event.text
    assert p._seen_issue_at["a/b"] == "2026-09-02T00:00:00Z"


def test_pull_requests_are_filtered_out() -> None:
    p = create_provider({"repos": "a/b"})
    p._get = lambda path: [_issue(1, "2026-09-01T00:00:00Z")]
    p._poll_issues("a/b")
    p._get = lambda path: [_issue(2, "2026-09-02T00:00:00Z", pr=True)]
    assert p._poll_issues("a/b") == []


# ── Degrade to silence ────────────────────────────────────────────────────


def test_api_failure_skips_the_round_and_loop_survives() -> None:
    async def scenario() -> list:
        emitted: list = []
        p = create_provider({"repos": "a/b"})

        def boom(repo):
            raise OSError("down")

        p._poll_repo = boom
        p._poll_secs = 0.01  # type: ignore[assignment]
        await p.start(emitted.append)
        await asyncio.sleep(0.05)  # several rounds, all failing
        assert p._task is not None and not p._task.done()  # loop alive
        await p.stop()
        return emitted

    assert asyncio.run(scenario()) == []
