# Propolis design

Propolis is a BeeWare app for triaging GitHub notifications. You file notifications under labels, and they stay in those labels in the order you triaged them. Labels can also flag a thread for your attention when someone else acts on it.

## Concepts

**Thread**: one issue, pull request, discussion or release that you get notifications about. Propolis identifies a thread by its repo and number (`owner/repo#123`) rather than GitHub's notification thread ID, so it can track a pull request before any notification about it exists.

**Label**: the only way to organise threads. A thread can have any number of labels. Each label is also a view in the sidebar, listing its threads in the order they were labelled, oldest first. "Folders" are just labels viewed this way.

A label has one behaviour setting:

- **Flag on activity**: when someone else acts on a thread with this label, Propolis applies the *Attention required* label to it. Example: "High priority".
- Otherwise, activity leaves the thread where it is and shows a "new activity" marker on it. Example: "Needs deep analysis". Viewing or labelling the thread clears the marker.

Users never write rules. Behaviour is configured only through this per-label setting.

**Default labels.** Propolis comes with six labels, in this order. They're part of the Store's starting state, not events, so every device has them from the start and syncing can't duplicate them.

| Label | Flag on activity |
|---|---|
| Needs review | |
| Question | |
| Deep response | |
| Blocked | ✓ Activity may mean it's unblocked. |
| Low priority | |
| High priority | ✓ |

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
- Thread details from GitHub (title, link, notification ID, GitHub's latest reason for notifying you, how far each thread has been checked) are cached in the same SQLite database. They aren't events and aren't synced: each device fetches its own. When the details gain a field, a cache version makes the next sync refetch every notification once, so existing threads catch up. That costs pages of notifications, but no timelines. A pull request known only through CI polling has the reason "Failing CI".
- `propolis.views`: pure Python with no Toga imports. It turns the Store's state into what the UI shows, and it's tested. This is where the UI's logic lives:
  - the sidebar's views, each with its number key, its thread count and how many have new activity, shown as "4   Blocked  (3, 1 new)";
  - each thread's row: title with a ● for new activity, repo, type, and when;
  - filtering: the repos, reasons and types to offer, and the rows that match.
- `propolis.app`: Toga only, and kept to wiring, so it has no tests; it's checked by running it.
  - Without a token in the keychain, the window shows the device-flow code and a button that opens GitHub. If GitHub rejects the token, the app forgets it and shows sign-in again.
  - Otherwise, the window has a sidebar of views on the left. On the right, the thread list sits above a web view showing the selected thread's GitHub page, so you can read and reply without leaving the app. A status line shows the last sync.
  - Selecting a thread counts as viewing it: its ● goes, and it's marked read on GitHub.
  - After you label, unlabel or finish a thread, the thread that takes its place is selected, so you can keep triaging from the keyboard.
  - The web view has its own cookies, so to comment you sign in to GitHub inside it once. That sign-in survives quitting and reopening the app, confirmed under `briefcase dev`.
- `propolis.macos`: what Toga doesn't offer yet, and a no-op on other platforms. It has a key-event monitor for plain number keys, and selects table rows through `NSTableView`.

  | Command | Shortcut |
  |---|---|
  | Apply label *n*. The default labels come first, then yours in the order you created them. The number shows in the sidebar and the Labels menu. | 1 to 9, while the thread list or sidebar has focus |
  | Refresh (sync now) | ⌘R |
  | Open in Browser (also double-click), which marks it viewed | ⌘O |
  | Done | ⌘E |
  | Remove from This View (removes the current view's label) | ⌘⌫ |
  | New Label… (name and "Flag on activity") | ⇧⌘N |
  | Edit Label… for the label selected in the sidebar: rename, change "Flag on activity", or delete. Delete asks first, and says how many threads go back to the Inbox. Double-clicking a label also opens it. | ⌘I |
  | Move Label Up / Down, which changes the number keys | ⌥⌘↑ / ⌥⌘↓ |
  | Sign Out of GitHub | |
  | Clear Filters | ⇧⌘F |
  | Keyboard Shortcuts (Help menu) | ⌘/ |

  A filter bar above the thread list narrows it by repo, reason and type. Reasons are in plain words: Mentioned, Review requested, Watching (you watch the repo), Subscribed (you subscribed to the thread), Failing CI, and so on. A reason is GitHub's latest for the thread, so it can change. Filters stay set as you switch views and reset when you quit. Sidebar counts ignore them, and the window title says "(filtered)" while one is on. Number keys and Done move to the next thread that matches.

  Each command's description is both its tooltip in the menus and its entry in the Keyboard Shortcuts window, so the two can't drift apart. A note at the bottom of the sidebar says how to open that window. Until a thread is selected, the web view shows a placeholder that follows light and dark mode.

  Number keys aren't menu shortcuts: a menu item with a plain-number shortcut can swallow digits typed into text fields, including a comment box in the web view. A local event monitor handles them instead, and only when a list has focus.

  The app syncs when it starts, then in the background on Toga's asyncio loop, as often as GitHub's `X-Poll-Interval` allows (currently 60 seconds). Refresh syncs at once. Each background sync is one conditional notifications request, which costs nothing against the rate limit when nothing has changed, plus the CI query. When a sync changes the list, the selected thread stays selected. If a background sync fails, the status line says so and polling carries on.

### Event-sourced storage

Every change is an immutable event recorded in SQLite under `app.paths.data`:
- label added or removed
- thread viewed
- done
- activity detected
- label created, renamed or configured
- label deleted
- labels reordered

Each event has a timestamp and a device ID. Current state (which labels a thread has, and when each was applied) is never stored. It's rebuilt by replaying the events in timestamp order when the store opens, then kept up to date in memory. Attention required isn't an event of its own: replaying "activity detected" on a thread with a flagging label applies it.

This costs little now, and it makes cross-device sync a matter of exchanging events:

- For each thread and label pair, the latest change wins.
- Deleting a label takes it off every thread. Threads with no other labels go back to the Inbox. A label applied after it was deleted, for example from a device that hadn't yet seen the deletion, is ignored, so no thread can end up under a label that no longer exists.
- A reorder records the whole order, and the latest reorder wins. Labels it doesn't mention, such as ones another device created meanwhile, keep their relative order after the ones it does.
- Changing a label's "Flag on activity" affects only activity from then on. Threads that already have the label aren't flagged retroactively.
- Attention required can't be renamed, moved or deleted. The six default labels can.
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
4. Label management: rename, reorder, delete, and flag on activity. Creating labels and background polling arrived early, in phase 3.
5. Polish: filters by repo, reason and type. The `WebView` detail pane arrived early, in phase 3.
6. Cross-device sync, then mobile.
