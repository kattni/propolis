# Propolis

A macOS app for working through your GitHub notifications, built with
[Toga](https://toga.beeware.org/) and packaged with
[Briefcase](https://briefcase.beeware.org/).

Propolis keeps an **inbox** of your GitHub notifications, and **labels** to file
threads under once you've triaged them. A thread stays in its label, in the
order you filed it, however much activity it gets. Some labels **flag on
activity**: when someone else acts on one of their threads, it's also put
under **Attention required**, so it comes back to you.

Only activity that needs a response from you counts. Your own doesn't, and
neither does a bot's, unless it mentions you, requests your review or assigns
you. A failing CI run on one of your pull requests does count.

## Features

- **Read notifications in Propolis.** Select a notification to see its page on
  GitHub underneath the list, just as it looks in your browser. Sign in to
  GitHub there, once, to see private repositories and to comment, react or
  review as you.
  - The address bar above the page shows where you are, with Back, Forward
    and Reload. Type an address there and press Return to go to it.
  - Showing a notification counts as viewing it: it clears the ● on it, and
    marks it as read on GitHub, as opening it on github.com does. In Unread,
    it stays in the list until you select another one.
  - Links to other sites open in your browser, and so do links that open a
    new window.
- **Inbox, Unread and Done views.** Everything in your GitHub inbox starts in
  the inbox, read or not, until you label it. The inbox is ordered by the
  latest activity that needed a response from you, newest first, so your own
  comment doesn't move a thread up. Unread shows the inbox's threads with
  something new on them.
  - A ● (a dot at the start of the title) marks a thread with activity you
    haven't seen. Threads that are unread on GitHub when Propolis first sees
    them start with one. The sidebar's counts say how many threads each view
    has, and how many are new, such as "3, 1 new".
  - **Mark as Done** takes a thread out of every view, its labels and
    Attention required included, and marks it as done on GitHub. Activity
    from someone else brings it back to the inbox. Threads you clear on
    github.com go to Done when Propolis next starts, unless they have a label.
  - Each row starts with GitHub's Octicon for what it's about: an issue, pull
    request, discussion, release, commit, CI run, Dependabot or security
    alert, or invitation (or a bell, for anything else). Issues, pull
    requests and discussions are coloured by state, as on GitHub: green when
    open, purple when merged, completed or resolved, red for a pull request
    closed without merging, and grey for drafts, issues not planned,
    duplicates and the like. Propolis looks the state up after each new bit of
    activity, so a thread's icon is grey until then. VoiceOver reads each
    icon's meaning, such as "Merged pull request".
  - The Updated column shows the date and time of GitHub's latest activity,
    in your time zone.
- **Labels.** Propolis starts with Needs review, Question, Deep response,
  Blocked, Low priority, High priority, In progress and Cold storage. Blocked
  and High priority flag on activity. To file the selected notifications under a
  label (select several to file them all at once), press its number, with the
  list or the sidebar selected: the first nine labels in the sidebar are
  numbered 1 to 9, and so are the labels in the **Labels** menu. Labelling a
  thread marks it as read. A thread can have more than one label.
  - **Delete**, with the list selected, takes the label you're viewing off the
    selected threads. In Attention required, that's how you say you've dealt
    with them. In the inbox, Delete means Mark as Done.
  - The **Label** menu in the toolbar adds or takes off any label, and
    Attention required. The labels of the notification you're reading are
    shown as coloured pills beside its address, after ⚠️ if it needs
    attention.
  - **New Label…** (⌘N) makes a label, and **Edit Label…** (⌘I, or
    double-click the label) renames the label you're viewing or changes
    whether it flags on activity. That only affects activity from then on.
    You can also add a label by typing its name below the sidebar.
  - **Move Label Up** and **Down** (⌘↑ and ⌘↓, with the sidebar selected)
    change a label's place in the sidebar, and so its number.
  - Each label lists its threads in the order they were filed, oldest first,
    so they stay put when there's new activity. A labelled thread doesn't
    come back to the inbox: take all its labels off to send it back.
  - Deleting a label (⇧⌘⌫) sends its threads back to the inbox, unless they
    have other labels. Deleting a default label is permanent; Propolis doesn't
    add it back.
- **Attention required.** When someone *other than you* acts on a thread with a
  label that flags on activity, Propolis also puts it in Attention required (it
  stays in its labels too), and marks it in the list with a yellow alert icon in
  place of the ●. Threads are listed in the order they were flagged. Activity
  from before you labelled a thread doesn't flag it. Viewing a thread doesn't
  clear it: take Attention required off with Delete, or mark the thread as done.
  - Applying a label that flags on activity also subscribes you to the thread
    on GitHub, so its activity keeps reaching Propolis.
- **What counts as activity.** Anything someone else does on a thread's
  timeline counts, comments, reviews, labelling, assigning and closing
  included, and so do their pushes to a pull request's branch. Your own
  activity never counts. A bot's counts only if it mentions you, requests
  your review or assigns you, so pre-commit.ci's fixes don't. For a
  discussion, a release, or a thread in a private repository, GitHub doesn't
  say who was active, so any update counts.
- **Failing CI.** Propolis checks the latest commit of each of your open pull
  requests for failing checks, including ones outside GitHub Actions, such as
  pre-commit.ci or Read the Docs. A failure counts as activity on the pull
  request, at the time the check failed. A pull request Propolis doesn't have
  yet goes in the inbox, with the reason "Failing CI". GitHub's own CI
  notifications are in the inbox too, but their updates never count, so a
  failure isn't counted twice.
- **Triage actions:** Open on GitHub (in your browser, which also marks the
  thread as read), Mark as Read, Mark as Done, Unsubscribe (which also marks
  it as done) and Remove from This View. After you mark something as done or
  label it, the selection moves on to the next notification. If GitHub
  doesn't accept a change, the change in Propolis stands, and the failure is
  logged.
- **Add by number.** Add any issue, pull request or discussion, even one you
  haven't had a notification about: choose **Add by Number…** (⌘T) and type
  `org/repo#1234`, or paste its GitHub link. It goes into the label you're
  viewing, or the inbox otherwise.
  - Propolis checks threads you've added this way for activity every 10 minutes
    (and when you choose Refresh), since GitHub doesn't send notifications
    about them.
  - If a notification arrives for one later, it takes the place of the one you
    added, keeping its labels and its place in them.
  - Marking one as read or done, or unsubscribing, only changes it in Propolis:
    there's no notification on GitHub to change.
- **Discussions.** A discussion's notification doesn't say which discussion it's
  about, so Propolis looks for the one discussion in the repository with its
  title. When there's exactly one, the thread opens at the discussion's page,
  and its icon is coloured by state. Otherwise, it opens a search for the title
  on the repository's Discussions page.
- **Filter** the current view by title or repository, by typing above the
  list, and by type (issue, pull request, discussion…), reason (mentioned,
  review requested…) or repository with the **Filter** menu in the toolbar.
  The menu lists what's in the view you're looking at, with how many
  notifications each choice would show. Choose a ticked one again to stop
  filtering by it, or choose Clear Filters (⌘F). While the list is filtered,
  the button says Filtered. Filters stay set when you switch views.
- **Checking GitHub.** When Propolis starts, it reads your whole GitHub inbox,
  to catch anything a check missed. After that, it checks about once a
  minute, following GitHub's `X-Poll-Interval`, for what's changed since the
  last check, using `If-Modified-Since`, so a check where nothing has changed
  doesn't count against your API rate limit.
- **More than one account.** **File → Add Account…** adds another GitHub
  account, such as one whose inbox you're looking after, with its own labels
  and history. Switch between them with **File → Switch Account**. Each
  account remembers its GitHub login, and refuses a sign-in as anyone else.
  An account can also be on another Mac, triaged through Propolis there (see
  [Sharing an inbox with another Mac](#sharing-an-inbox-with-another-mac)).

### Keyboard shortcuts

**Help → Keyboard Shortcuts** (⌘/) lists them all.

| Action             | Shortcut |
| ------------------ | -------- |
| Add label 1 to 9   | 1 to 9, with the list or sidebar selected |
| Remove from This View (Mark as Done in the inbox) | Delete, with the list selected |
| Mark as Done       | ⌘D       |
| Mark as Read       | ⇧⌘U      |
| Unsubscribe        | ⇧⌘M      |
| Open on GitHub     | ⌘O, or double-click |
| Add by Number      | ⌘T       |
| New Label          | ⌘N       |
| Edit Label         | ⌘I, or double-click the label |
| Delete Label       | ⇧⌘⌫      |
| Move Label Up / Down | ⌘↑ / ⌘↓, with the sidebar selected |
| Clear Filters      | ⌘F       |
| Refresh            | ⌘R       |
| Settings           | ⌘,       |
| Keyboard Shortcuts | ⌘/       |

## Connecting to GitHub

The first time Propolis opens, it shows a code. Click **Open GitHub**, enter the
code at github.com/login/device, and approve the app. Propolis asks GitHub for
the `notifications` permission only, and keeps the sign-in in your macOS
Keychain. To sign out, choose **File → Sign Out of GitHub**, or open **Propolis
→ Settings…** (⌘,). If GitHub stops accepting the sign-in, Propolis asks you to
sign in again.

GitHub's sign-in page approves whichever account your browser is signed in
to. An account that has already signed in refuses a different one: add an
account for it with **File → Add Account…** instead.

Propolis doesn't ask for the `repo` scope, which would give it full access to
your private repositories. So it can't see who was active on threads in
**private** repositories: any update to one counts as activity, and their
icons stay grey. Their notifications still appear, and marking them as read
or done works.

## Sharing an inbox with another Mac

A Mac that's signed in to GitHub can share an account's inbox, so that Propolis
on another Mac triages it through that one, without signing in to GitHub.
Everything you do on the other Mac happens on the sharing Mac, which keeps the
only copy of the labels and history, and makes every GitHub request itself. So
the sharing Mac has to be running Propolis for the other one to work. This is
also how to take over someone else's inbox: they share it, and you connect to
it.

**What the other Mac can do:** everything you can do on the sharing Mac, from
labelling and marking as done to adding, renaming and deleting labels. Changes
are made on GitHub as the sharing Mac's account. **What it can't do:** see the
GitHub sign-in, change settings, or stop sharing.

### Setting it up

Propolis reaches the sharing Mac through a tunnel you run, such as a
[Cloudflare Tunnel](https://developers.cloudflare.com/cloudflare-one/networks/connectors/cloudflare-tunnel/),
so it works from anywhere. On the sharing Mac:

1. Install `cloudflared`, for example with `brew install cloudflared`.
2. Make a tunnel with a public hostname, such as `propolis.example.com`, whose
   service is `http://localhost:47615`. This needs a Cloudflare account and a
   domain on Cloudflare. You can make the tunnel in Cloudflare's dashboard, or
   with `cloudflared tunnel create` and a config file. Run it as a service, so
   it's there whenever the Mac is.

   To try sharing without a Cloudflare account, run
   `cloudflared tunnel --url http://localhost:47615` instead. It prints a
   `trycloudflare.com` address, which changes every time.
3. In Propolis, choose **File → Share This Inbox…**, and turn on **Share this
   inbox**. The window shows a code. While sharing is on, the window's title
   says "📡 Remote control on", and the status line shows the last change the
   other Mac made.

On the other Mac, open Propolis, or choose **File → Add Account…** if it already
has an account. Click **Connect to Another Mac…**, and enter the tunnel's
address and the code. The shared inbox becomes one of that Mac's accounts:
switch between it and the others with **File → Switch Account**. While it's
open, the window title says which Mac it's on. Only an account that hasn't
signed in to GitHub yet can connect, since an account's history is one
account's.

To stop, choose **File → Disconnect from Other Mac** on the Mac that's
connected, or turn sharing off on the sharing Mac. To turn away every Mac that
has the code, click **New Code** in the Share This Inbox window.

### Things to know

- The sharing Mac needs to be awake, with Propolis open and signed in, for the
  other one to reach it. Sharing comes back on by itself when Propolis opens
  again.
- Propolis only listens on `127.0.0.1`, so the only way in from elsewhere is
  through the tunnel. Every request needs the code.
- A Mac with the code can see the notifications' titles and links, and the
  labels and history, and can triage them, so only give it to someone you'd
  let do that. Cloudflare carries the traffic, and can read it as it passes
  through.
- The other Mac checks the shared inbox every 10 seconds, and straight after
  each change it makes. Problems reaching it show in the status line.
- A notification's page shows GitHub signed in as *you*, in Propolis, and **Open
  on GitHub** opens it in your browser, signed in as you. Either one marks the
  notification as read in the shared inbox.
- The port, 47615, is fixed; the tunnel must point to it.

## Running it

You need macOS and Python 3.11 or newer.

```bash
git clone https://github.com/kattni/propolis.git
```

```bash
cd propolis
```

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

To build a standalone `Propolis.app` (in `build/propolis/macos/app/`):

```bash
briefcase build macOS
```

`briefcase run macOS` launches the built app, and `briefcase package macOS`
makes a DMG you can install from.

To release a version, push a tag for it, such as `v0.1.0`. The Release
workflow builds the app, signs and notarizes it with the Developer ID
certificate and App Store Connect key in the repository's secrets, and
publishes a GitHub release with the DMG. Running the workflow by hand makes a
prerelease instead.

## How it's put together

| File | What it does |
| ---- | ------------ |
| `src/propolis/app.py` | The Toga app: window layout, commands and menus, signing in, accounts, sharing an inbox and connecting to one on another Mac |
| `src/propolis/sync.py` | Bringing GitHub into the store: new threads, activity on known ones, failing CI on your pull requests, threads added by hand, and finding discussions' numbers |
| `src/propolis/activity.py` | Whether a bit of activity needs a response from you |
| `src/propolis/actions.py` | Changing threads in the store, and on GitHub or the other Mac, best effort |
| `src/propolis/remote.py` | Sharing an inbox: the local API Propolis on another Mac talks to, one endpoint per action, and the client for triaging through another Mac |
| `src/propolis/github.py` | A small async client for GitHub's API (using httpx): notifications, timelines and pushes, failing checks, finding discussions, looking up whether issues and pull requests are open (with the GraphQL API), and turning API URLs into github.com links |
| `src/propolis/auth.py` | Signing in to GitHub with OAuth device flow |
| `src/propolis/accounts.py` | The GitHub accounts Propolis triages, each with its own store, or on another Mac |
| `src/propolis/store.py` | An inbox's views and triage, kept in SQLite as an event log beside what GitHub says about each thread, or copied from another Mac |
| `src/propolis/events.py` | The events that make up the triage history, each named with the device it was made on |
| `src/propolis/state.py` | The triage state, made by replaying the events in order, and the default labels |
| `src/propolis/legacy.py` | Turning stores from before the event log into events, once |
| `src/propolis/resources/octicons/` | Icons from GitHub's [Octicons](https://primer.style/octicons/) 19.42.0, under the MIT licence (in `LICENSE` there), drawn as 32×32 PNGs |
| `src/propolis/resources/propolis.icns` | The app icon, which puts bees on the pull request Octicon |
| `src/propolis/credentials.py` | Saving the GitHub sign-in and sharing codes in the Keychain (using keyring) |
| `src/propolis/macos.py` | AppKit touches Toga doesn't offer yet: selecting a table row, column widths, copying to the clipboard, labels drawn as pills, the toolbar's Label and Filter menus, number keys, Delete and ⌘↑/⌘↓ in lists, icons' VoiceOver descriptions, turning off window tabs, the address bar's Back and Forward, and sending links to other sites to your browser |

Your data lives in `~/Library/Application Support/com.kattni.propolis/Data/`:
the first account's store is `propolis.sqlite3`, other accounts' are in
`accounts/`, and `accounts.json` lists them. An account on another Mac keeps
nothing here but that Mac's address, in `accounts.json`, and its code, in the
Keychain.

Propolis 0.0.1 kept its data in the same place. This version doesn't read
0.0.1's stores, which are the `.db` files, or its Keychain entries, so it
starts with no labels or history, and asks you to sign in again. It does read
`accounts.json`, so it starts with 0.0.1's accounts.

Each store records every triage change as an event, named with the device it
was made on, and rebuilds the triage state by replaying them. That's the
groundwork for syncing between devices. What GitHub says about each thread is
kept beside the events, as a cache. A store from before the event log becomes
one the first time this version opens it.

## Limitations

- Labels are only stored on this Mac. GitHub has no API for organising
  notifications, so they don't sync to github.com or to other computers.
- GitHub's API can't mark a notification as *unread*, or move a done
  notification back to the inbox, so Propolis can't either.
- Propolis only notices threads cleared on github.com when it starts, when it
  reads the whole inbox.
- Attention required relies on GitHub notifying you about a thread, which
  happens for threads you're subscribed to or participating in. Labels that
  flag on activity subscribe you for that reason. Propolis learns about activity
  when GitHub updates the notification, then asks GitHub who was active.
- A bot running on an ordinary user account looks like a person, so its
  activity counts.
- For discussions and releases, GitHub's API doesn't say who was active, so
  any activity on them counts, including your own.
- Threads you've added by number are checked one request each, every 10
  minutes; done ones aren't checked any more.
- What you do in Propolis is ordered after the latest activity it knows of, so
  it doesn't matter if your Mac's clock is a little behind GitHub's. If it's
  more than an hour behind, though, a thread you view can keep its ●, and one
  you take out of Attention required can stay there.
