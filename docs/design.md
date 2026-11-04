# Propolis design

Propolis is a BeeWare app for triaging GitHub notifications. You file notifications under labels, and they stay in those labels in the order you triaged them. Labels can also flag a thread for your attention when someone else acts on it.

## Concepts

**Thread**: one GitHub notification thread (an issue, pull request, discussion, release or CI run).

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

How activity is detected: the notifications API only says that a thread's `updated_at` changed, not what changed. When it moves forward, Propolis fetches the thread's timeline (`/repos/{owner}/{repo}/issues/{n}/timeline`, which covers pull requests too) and looks at events since its last check. Discussions need GraphQL instead.

**Still to decide (phase 2):** how to detect failing CI. A CI failure doesn't update the pull request's notification thread. The options are:
- `ci_activity` notifications, which need Actions notifications turned on in your GitHub settings;
- polling the Checks API for the head commit of your open pull requests.

## Writing back to GitHub

| In Propolis | On GitHub |
|---|---|
| Open a thread or apply a label | Mark the thread read |
| Done | Mark the thread done (`DELETE /notifications/threads/{id}`) |
| Apply a flagging label | Subscribe to the thread, so its activity reaches Propolis |

## Authentication

Propolis signs in with OAuth device flow. This needs a GitHub **OAuth App**: GitHub App tokens and fine-grained PATs don't work with the notifications API. The app ships only the OAuth App's public client ID and no secret. The token is stored in the macOS Keychain via `keyring`.

Scopes:
- `notifications`: read notifications, mark them read or done, and manage thread subscriptions.
- `repo`: only needed to read timelines on private repos.
- `gist`: added later, for sync.

## Architecture

- `propolis.core`: pure Python with no Toga imports. It holds the domain model, storage, the activity logic and the GitHub client (async `httpx`). It's developed test-first with pytest. GitHub interactions are tested against recorded responses, never the live API.
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
