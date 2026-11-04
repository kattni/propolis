# Propolis design

Propolis is a BeeWare app for triaging GitHub notifications. You file notifications under labels, and they stay in those labels in the order you triaged them. Labels can also flag a thread for your attention when someone else acts on it.

## Concepts

**Thread**: one issue, pull request, discussion or release that you get notifications about. Propolis identifies a thread by its repo and number (`owner/repo#123`) rather than GitHub's notification thread ID, so it can track a pull request before any notification about it exists.

**Label**: the only way to organise threads. A thread can have any number of labels. Each label is also a view in the sidebar, listing its threads in the order they were labelled, oldest first. "Folders" are just labels viewed this way.

A label has one behaviour setting:

- **Flag on activity**: when someone else acts on a thread with this label, Propolis applies the *Attention required* label to it. Example: "High priority".
- Otherwise, activity leaves the thread where it is and shows a "new activity" marker on it. Example: "Needs deep analysis". Viewing or labelling the thread clears the marker.

Users never write rules. Behaviour is configured only through this per-label setting.

**Built-in views:**

- **Inbox**: threads with no labels, most recent activity first. Applying any label is the triage step, and it takes the thread out of the Inbox.
- **Attention required**: a built-in label that Propolis applies automatically. The thread keeps its other labels and its place in each of them. Removing the label means "dealt with it".

**Done**: removes all labels from a thread and marks it done on GitHub. If someone else later acts on it, it comes back to the Inbox.

## What counts as activity

Only activity that needs a response from you counts:

- Activity by other people counts. Your own activity never does.
- Bot activity (accounts GitHub marks as type `Bot`) counts only if the bot @mentions you, requests your review, or assigns you. A bot running on an ordinary user account looks like a person, so its activity counts.
- Failing CI on one of your own pull requests counts.
- Commits pushed to a pull request don't count. The timeline identifies commits by git author, not GitHub login, so there's no way to tell whose they are. Comments, reviews, force-pushes, labels and state changes by others all count.
- Any update to a discussion or release counts. GitHub's REST API doesn't say who acted on them.

How activity is detected: Propolis polls `/notifications?all=true` with `since` and `If-Modified-Since`, so a poll where nothing has changed costs nothing against the rate limit. The notifications API only says that a thread's `updated_at` changed, not what changed. When it moves forward, Propolis fetches the thread's timeline (`/repos/{owner}/{repo}/issues/{n}/timeline`, which covers pull requests too) and looks at events since its last check.

The first time Propolis sees a thread, it trusts GitHub's `unread` flag. A read thread starts with nothing new. An unread one is checked from its `last_read_at`, or from the beginning if that's missing. `last_read_at` alone is unreliable: GitHub marks threads read in bulk without setting it. On a real account with 160 notifications, the first sync cost 20 REST requests and put 19 threads in the Inbox.

A CI failure doesn't update the pull request's notification thread, so Propolis polls for it separately:
- One GraphQL query finds your open pull requests and reads the check runs and commit statuses on each one's latest commit.
- A failure counts as activity on the pull request itself, at the time the check completed, so flagging labels work for CI just as they do for comments.
- This covers checks outside GitHub Actions too, such as pre-commit.ci or Read the Docs builds.
- Propolis skips GitHub's own `ci_activity` notification threads, so a failure doesn't appear twice.

## Writing back to GitHub

| In Propolis | On GitHub |
|---|---|
| Open a thread or apply a label | Mark the thread read |
| Done | Mark the thread done (`DELETE /notifications/threads/{id}`) |
| Apply a flagging label | Subscribe to the thread, so its activity reaches Propolis |

Write-back is best effort. If a GitHub call fails, the change in Propolis stands and the failure is logged. Threads found only through CI polling have no notification thread yet, so there's nothing to write back for them.

## Authentication

Propolis signs in with OAuth device flow. This needs a GitHub **OAuth App**: GitHub App tokens and fine-grained PATs don't work with the notifications API. The app ships only the OAuth App's public client ID (`Ov23ligJYqr0ZNQGPQhL`) and no secret. The OAuth App has "Expire user access tokens" turned off, because refreshing short-lived tokens needs the client secret. The token is stored in the macOS Keychain via `keyring`.

Scopes:
- `notifications`: read notifications, mark them read or done, and manage thread subscriptions.
- No `repo` scope: for now Propolis covers public repos only, and reading their timelines and checks needs no scope. Private repos would need `repo`, which grants full read and write access to them.
- `gist`: added later, for sync.

## Architecture

- `propolis.core`: pure Python with no Toga imports. It holds the domain model, storage, the activity logic, sync, triage with write-back, sign-in, and the GitHub client (async `httpx2`). It's developed test-first with pytest, at a few agreed seams. GitHub interactions are tested against a fake GitHub (`httpx2.MockTransport` serving small hand-written JSON), never the live API.
- Thread details from GitHub (title, link, notification ID, how far each thread has been checked) are cached in the same SQLite database. They aren't events and aren't synced: each device fetches its own.
- `propolis.app` and the UI modules: Toga only. A `SplitContainer` has a sidebar of views on the left and a `Table` of threads on the right. Actions are `Command`s with keyboard shortcuts. Polling runs on Toga's asyncio loop, respecting GitHub's `X-Poll-Interval` and using `If-Modified-Since`.

### Event-sourced storage

Every change is an immutable event recorded in SQLite under `app.paths.data`:
- label added or removed
- thread viewed
- done
- activity detected
- label created, renamed or configured

Each event has a timestamp and a device ID. Current state (which labels a thread has, and when each was applied) is never stored. It's rebuilt by replaying the events in timestamp order when the store opens, then kept up to date in memory. Attention required isn't an event of its own: replaying "activity detected" on a thread with a flagging label applies it.

This costs little now, and it makes cross-device sync a matter of exchanging events:

- For each thread and label pair, the latest change wins.
- Activity events carry GitHub's timestamp, not the time Propolis noticed the activity, and their ID is the thread ID plus that timestamp. If two devices detect the same activity, their events collapse into one instead of conflicting.

Because activity carries GitHub's timestamp, it can arrive after events that it precedes, and the store replays the log when that happens. A consequence is that activity which happened before you labelled a thread doesn't flag it, even if Propolis only notices it afterwards.

## Sync (later)

The planned backend is a private gist with one event-log file per device. It needs no server and no new account, and it uses the same GitHub token plus the `gist` scope. Each device writes only its own file, so writes never conflict. The logs will need occasional compaction as they grow.

## Platforms

macOS desktop first. Because the logic lives in `core`, mobile is mostly UI work. The known differences on mobile are:
- `DetailedList` with swipe actions instead of a `Table`;
- token storage through the platform keychain instead of `keyring`;
- sync on open and on pull-to-refresh, because iOS doesn't allow background polling.

## Phases

1. Core model, event log and activity logic, test-first.
2. GitHub client: device-flow sign-in, notifications sync, timeline checks, write-back.
3. Toga MVP: Inbox and label views, apply and remove labels, Done, open in browser, manual refresh.
4. Label management (create, rename, reorder, flag on activity) and background polling.
5. Polish: filters by repo, reason and type; an optional `WebView` detail pane.
6. Cross-device sync, then mobile.
