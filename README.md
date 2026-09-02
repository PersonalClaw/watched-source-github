# GitHub Repo Watcher

A PersonalClaw **trigger source** that watches GitHub repositories and emits
events your triggers can bind to. Implements `TriggerSourceProvider` from
`personalclaw.sdk.trigger_source`, stdlib-only.

## Events it emits

Core namespaces every event under this app's name — bind triggers to:

| Trigger event | Fires when | `meta` |
| --- | --- | --- |
| `app:watched-source-github:new_release` | a watched repo publishes a release | `repo`, `tag`, `url` |
| `app:watched-source-github:new_issue` | a new issue opens (PRs filtered out) | `repo`, `number`, `author`, `url` |

## Settings

| Key | Default | Meaning |
| --- | --- | --- |
| `repos` | *(required)* | Comma-separated `owner/name` list to watch |
| `token` | *(unset)* | GitHub token — optional for public repos, raises rate limits |
| `poll_interval_secs` | `300` | Poll cadence (floor 60) |
| `api_base` | `https://api.github.com` | Override for GitHub Enterprise |
| `timeout_secs` | `20` | HTTP timeout per request |

## Design notes worth copying into your own source

- **Push contract, self-owned loop.** Core never polls a source: `start`
  spawns this provider's own asyncio task and returns immediately (blocking
  there would stall app enable); `stop` cancels it and is idempotent.
- **First observation plants a high-water mark.** The first poll of a repo
  records what exists and emits nothing — a freshly-watched repo must not
  flood history as "new" events.
- **Declared events.** `events` is the browsable vocabulary users pick from
  when authoring a trigger; emit only names declared there.
- **Degrade to silence.** A network or API failure logs a warning and skips
  that round; the loop survives. A watcher must never take the trigger bus
  down with it.

## Run the tests

```bash
pytest .
```

No network — the behaviour tests stub the fetch seam.

## Install it

From the dashboard: **Store → Add source**, point it at this repo's git URL (or
a local clone), then install and enable it. Enabling starts the watch loop;
disabling stops it and parks its triggers.

## License

MIT
