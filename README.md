# Buzz

A macOS app for working through your GitHub notifications, built with
[Toga](https://toga.beeware.org/) and packaged with
[Briefcase](https://briefcase.beeware.org/).

Buzz keeps an **inbox** that mirrors the one on github.com/notifications, and
adds your own **labels**, so you can sort threads into groups ("Review later",
"Release blockers", …) and find them again after you've cleared them out of the
inbox.

## Features

- **Inbox, Unread and Done views.** The inbox is GitHub's: anything you mark as
  done in Buzz is marked as done on GitHub, and anything you clear on
  github.com disappears from Buzz on the next check. A done thread with new
  activity comes back to the inbox, as it does on GitHub.
- **Labels.** Create labels in the sidebar, then tick them in the details pane
  for the selected notifications (select several to label them all at once).
  Each label has its own view in the sidebar. A labelled thread stays in its
  label's view after it's done, marked with ✓.
- **Triage actions:** Open on GitHub (which also marks the thread as read),
  Mark as Read, Mark as Done, and Unsubscribe (which also marks it as done).
  After you mark something as done, the selection moves on to the next
  notification.
- **Filter** the current view by title or repository.
- **Background checking** about once a minute, following GitHub's
  `X-Poll-Interval` and using `If-Modified-Since`, so a check where nothing
  has changed doesn't count against your API rate limit.

### Keyboard shortcuts

| Action             | Shortcut |
| ------------------ | -------- |
| Refresh            | ⌘R       |
| Open on GitHub     | ⌘O, or double-click |
| Mark as Read       | ⇧⌘U      |
| Mark as Done       | ⌘E       |
| Unsubscribe        | ⇧⌘M      |
| New Label          | ⌘L       |
| Settings (token)   | ⌘,       |

Rename Label… and Delete Label… are in the **Labels** menu; they apply to the
label you're viewing.

## Connecting to GitHub

Buzz needs a **personal access token (classic)** with the `notifications`
scope. The app's "Create a Token on GitHub…" button opens the right page with
the scope already ticked. Fine-grained tokens can't read notifications.

Buzz keeps the token in your macOS Keychain. To change it or sign out, open
**Buzz → Settings…** (⌘,).

## Running it

You need macOS and Python 3.11 or newer.

```bash
python3 -m venv .venv
```

```bash
source .venv/bin/activate
```

```bash
python -m pip install briefcase
```

```bash
briefcase dev
```

Run the tests with:

```bash
briefcase dev --test
```

To build a standalone `Buzz.app` (in `build/buzz/macos/app/`):

```bash
briefcase build macOS
```

`briefcase run macOS` launches the built app, and `briefcase package macOS`
makes a DMG you can install from.

## How it's put together

| File | What it does |
| ---- | ------------ |
| `src/buzz/app.py` | The Toga app: window layout, commands, and syncing in the background |
| `src/buzz/github.py` | A small async client for GitHub's notifications API (using httpx), and turning API URLs into github.com links |
| `src/buzz/store.py` | SQLite storage for threads, labels, and which threads have which labels |
| `src/buzz/credentials.py` | Saving the token in the Keychain (using keyring) |
| `src/buzz/macos.py` | A few AppKit tweaks Toga doesn't offer yet (selecting a table row, column widths) |

Your data lives in `~/Library/Application Support/com.example.buzz/buzz.sqlite3`.

## Limitations

- Labels are only stored on this Mac. GitHub has no API for organising
  notifications, so they don't sync to github.com or to other computers.
- GitHub's API can't mark a notification as *unread*, or move a done
  notification back to the inbox, so Buzz can't either.
- Each check fetches at most 1,000 inbox threads (the newest). If your inbox is
  bigger than that, Buzz won't notice older threads being cleared elsewhere.
- The bundle ID is the placeholder `com.example` and the app uses Briefcase's
  default icon. Change `bundle` in `pyproject.toml` (and add an `icon`) before
  you share a build. Changing the bundle ID also moves where the app keeps its
  data and its Keychain entry.
