# Buzz

A macOS app for working through your GitHub notifications, built with
[Toga](https://toga.beeware.org/) and packaged with
[Briefcase](https://briefcase.beeware.org/).

Buzz keeps an **inbox** of your GitHub notifications, and **labels** to file
threads under once you've triaged them. A thread stays in its label, in the
order you filed it, however much activity it gets. Some labels **flag on
activity**: when someone else acts on one of their threads, it's also put
under **Attention required**, so it comes back to you.

Only activity that needs a response from you counts. Your own doesn't, and
neither does a bot's, unless it mentions you, requests your review or assigns
you. A failing CI run on one of your pull requests does count.

## Features

- **Read notifications in Buzz.** Select a notification to see its page on
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
    haven't seen. Threads that are unread on GitHub when Buzz first sees them
    start with one. The sidebar's counts say how many threads each view has,
    and how many are new, such as "3, 1 new".
  - **Mark as Done** takes a thread out of every view, its labels and
    Attention required included, and marks it as done on GitHub. Activity
    from someone else brings it back to the inbox. Threads you clear on
    github.com go to Done when Buzz next starts, unless they have a label.
  - Each row starts with GitHub's Octicon for what it's about: an issue, pull
    request, discussion, release, commit, CI run, Dependabot or security
    alert, or invitation (or a bell, for anything else). Issues, pull
    requests and discussions are coloured by state, as on GitHub: green when
    open, purple when merged, completed or resolved, red for a pull request
    closed without merging, and grey for drafts, issues not planned,
    duplicates and the like. Buzz looks the state up after each new bit of
    activity, so a thread's icon is grey until then. VoiceOver reads each
    icon's meaning, such as "Merged pull request".
  - The Updated column shows the date and time of GitHub's latest activity,
    in your time zone.
- **Labels.** Buzz starts with Needs review, Question, Deep response, Blocked,
  Low priority, High priority, In progress and Cold storage. Blocked and High
  priority flag on activity. To file the selected notifications under a label
  (select several to file them all at once), press its number, with the list
  or the sidebar selected: the first nine labels in the sidebar are numbered
  1 to 9, and so are the labels in the **Labels** menu. Labelling a thread
  marks it as read. A thread can have more than one label.
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
    have other labels. Deleting a default label is permanent; Buzz doesn't
    add it back.
- **Attention required.** When someone *other than you* acts on a thread with
  a label that flags on activity, Buzz also puts it in Attention required (it
  stays in its labels too), and marks it in the list with a yellow alert icon
  in place of the ●. Threads are listed in the order they were flagged.
  Activity from before you labelled a thread doesn't flag it. Viewing a
  thread doesn't clear it: take Attention required off with Delete, or mark
  the thread as done.
  - Applying a label that flags on activity also subscribes you to the thread
    on GitHub, so its activity keeps reaching Buzz.
- **What counts as activity.** Anything someone else does on a thread's
  timeline counts, comments, reviews, labelling, assigning and closing
  included, and so do their pushes to a pull request's branch. Your own
  activity never counts. A bot's counts only if it mentions you, requests
  your review or assigns you, so pre-commit.ci's fixes don't. For a
  discussion, a release, or a thread in a private repository, GitHub doesn't
  say who was active, so any update counts.
- **Failing CI.** Buzz checks the latest commit of each of your open pull
  requests for failing checks, including ones outside GitHub Actions, such as
  pre-commit.ci or Read the Docs. A failure counts as activity on the pull
  request, at the time the check failed. A pull request Buzz doesn't have yet
  goes in the inbox, with the reason "Failing CI". GitHub's own CI
  notifications are in the inbox too, but their updates never count, so a
  failure isn't counted twice.
- **Triage actions:** Open on GitHub (in your browser, which also marks the
  thread as read), Mark as Read, Mark as Done, Unsubscribe (which also marks
  it as done) and Remove from This View. After you mark something as done or
  label it, the selection moves on to the next notification. If GitHub
  doesn't accept a change, the change in Buzz stands, and the failure is
  logged.
- **Add by number.** Add any issue, pull request or discussion, even one you
  haven't had a notification about: choose **Add by Number…** (⌘T) and type
  `org/repo#1234`, or paste its GitHub link. It goes into the label you're
  viewing, or the inbox otherwise.
  - Buzz checks threads you've added this way for activity every 10 minutes
    (and when you choose Refresh), since GitHub doesn't send notifications
    about them.
  - If a notification arrives for one later, it takes the place of the one you
    added, keeping its labels and its place in them.
  - Marking one as read or done, or unsubscribing, only changes it in Buzz:
    there's no notification on GitHub to change.
- **Discussions.** A discussion's notification doesn't say which discussion
  it's about, so Buzz looks for the one discussion in the repository with its
  title. When there's exactly one, the thread opens at the discussion's page,
  and its icon is coloured by state. Otherwise, it opens a search for the
  title on the repository's Discussions page.
- **Filter** the current view by title or repository, by typing above the
  list, and by type (issue, pull request, discussion…), reason (mentioned,
  review requested…) or repository with the **Filter** menu in the toolbar.
  The menu lists what's in the view you're looking at, with how many
  notifications each choice would show. Choose a ticked one again to stop
  filtering by it, or choose Clear Filters (⌘F). While the list is filtered,
  the button says Filtered. Filters stay set when you switch views.
- **Checking GitHub.** When Buzz starts, it reads your whole GitHub inbox,
  to catch anything a check missed. After that, it checks about once a
  minute, following GitHub's `X-Poll-Interval`, for what's changed since the
  last check, using `If-Modified-Since`, so a check where nothing has changed
  doesn't count against your API rate limit.
- **More than one account.** **File → Add Account…** adds another GitHub
  account, such as one whose inbox you're looking after, with its own labels
  and history. Switch between them with **File → Switch Account**. Each
  account remembers its GitHub login, and refuses a sign-in as anyone else.

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

The first time Buzz opens, it shows a code. Click **Open GitHub**, enter the
code at github.com/login/device, and approve the app. Buzz asks GitHub for the
`notifications` permission only, and keeps the sign-in in your macOS
Keychain. To sign out, choose **File → Sign Out of GitHub**, or open **Buzz →
Settings…** (⌘,). If GitHub stops accepting the sign-in, Buzz asks you to
sign in again.

GitHub's sign-in page approves whichever account your browser is signed in
to. An account that has already signed in refuses a different one: add an
account for it with **File → Add Account…** instead.

For now, Buzz signs in through Propolis's OAuth App, so GitHub's page names
Propolis. Register an OAuth App for Buzz, with Device Flow enabled and "Expire
user access tokens" turned off, and put its client ID in `src/buzz/auth.py`.

Buzz doesn't ask for the `repo` scope, which would give it full access to
your private repositories. So it can't see who was active on threads in
**private** repositories: any update to one counts as activity, and their
icons stay grey. Their notifications still appear, and marking them as read
or done works.

## Remote control

You can use your own Buzz to read and triage the inbox of someone else's Buzz,
on their Mac, signed in as them, without ever having their GitHub token. Their
Buzz makes every GitHub request itself; yours only sends it instructions,
through a Cloudflare named tunnel.

**What you can do** in their inbox: everything you can do in your own, from
labelling and marking as done to adding, renaming and deleting labels. Changes
are made as them on GitHub. **What you can't do:** see their GitHub token,
change their settings, or turn remote control off or on.

**How it's protected:**

- Remote control is off until they switch it on, in their Buzz's settings.
  While it's on, their window's title says "📡 Remote control on", and their
  status line shows the last change you made.
- Their Buzz only listens on `127.0.0.1` (port 47321), so the only way in is
  through the tunnel.
- Every request needs an access key that their Buzz makes. They can make a new
  one at any time, and the old one stops working straight away.
- You should also put Cloudflare Access in front of the tunnel, with a service
  token, so requests without it never reach their Mac.

### Setting it up

**You, in the Cloudflare dashboard** (you need a domain on Cloudflare):

1. Go to **Networking → Tunnels → Create a tunnel**, and name it (for example
   `buzz-alice`). When it shows the command to install a connector, copy the
   long token at the end of the command. Don't run the command: their Buzz
   runs the tunnel itself.
2. On the tunnel's **Routes** tab, choose **Add route → Published
   application**. Pick a subdomain and domain (for example
   `buzz-alice.example.com`), and set the **Service URL** to
   `http://localhost:47321`.
3. Create a service token in **Zero Trust → Access controls → Service
   credentials → Service Tokens**, and keep its client ID and secret (the
   secret is only shown once).
4. Add a self-hosted Access application for that hostname, with a policy whose
   action is **Service Auth** and which includes the service token.
5. Send them the tunnel token, privately.

**Them, on their Mac:**

1. Install cloudflared: `brew install cloudflared`.
2. In Buzz, open **Settings** (⌘,). Under **Remote Control**, paste the tunnel
   token and choose **Save**, then tick **Allow remote control of this inbox**.
   The status underneath should say "Tunnel connected."
3. Choose **Copy** next to the access key, and send it to you, privately.

**You, in your Buzz:**

1. Choose **File → Add Remote Inbox…**, and enter their name, the address
   (`https://buzz-alice.example.com`), the access key, and the service token's
   client ID and secret. Buzz checks that it can connect before saving.
2. Switch between your inbox and theirs with the menu at the top of the
   sidebar. While you're in theirs, the window title says so.

To stop, they untick **Allow remote control** (which closes the tunnel
straight away), or choose **New Key**. You can also revoke the service token
or delete the tunnel in Cloudflare. **File → Remove Remote Inbox…** makes your
Buzz forget theirs, without changing anything in their inbox.

### Things to know

- Their Mac needs to be awake, with Buzz open, for you to reach it. Remote
  control comes back on by itself when they open Buzz again.
- Your Buzz checks their inbox every 10 seconds, and straight after each change
  you make.
- A notification's page shows GitHub signed in as *you*, in Buzz, and **Open
  on GitHub** opens it in your browser, signed in as you. Either one marks the
  notification as read in *their* inbox.
- Access sign-in through a browser isn't supported; use a service token.
- The local port, 47321, is fixed; the tunnel's route must point to it.

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
| `src/buzz/app.py` | The Toga app: window layout, commands and menus, signing in, accounts, and switching between your inbox and remote ones |
| `src/buzz/sync.py` | Bringing GitHub into the store: new threads, activity on known ones, failing CI on your pull requests, threads added by hand, and finding discussions' numbers |
| `src/buzz/activity.py` | Whether a bit of activity needs a response from you |
| `src/buzz/actions.py` | Changing threads in the store, and on GitHub or the other Buzz, best effort |
| `src/buzz/remote.py` | Remote control: the local API another Buzz talks to, running cloudflared, and the client for controlling another Buzz |
| `src/buzz/github.py` | A small async client for GitHub's API (using httpx): notifications, timelines and pushes, failing checks, finding discussions, looking up whether issues and pull requests are open (with the GraphQL API), and turning API URLs into github.com links |
| `src/buzz/auth.py` | Signing in to GitHub with OAuth device flow |
| `src/buzz/accounts.py` | The GitHub accounts Buzz triages, each with its own store |
| `src/buzz/store.py` | SQLite storage for threads, labels, which threads have which labels, when there was activity and when you saw it, and attention flags |
| `src/buzz/resources/octicons/` | Icons from GitHub's [Octicons](https://primer.style/octicons/) 19.42.0, under the MIT licence (in `LICENSE` there), drawn as 32×32 PNGs |
| `src/buzz/credentials.py` | Saving the GitHub sign-in and remote control keys in the Keychain (using keyring) |
| `src/buzz/macos.py` | AppKit touches Toga doesn't offer yet: selecting a table row, column widths, copying to the clipboard, labels drawn as pills, the toolbar's Label and Filter menus, number keys, Delete and ⌘↑/⌘↓ in lists, icons' VoiceOver descriptions, turning off window tabs, the address bar's Back and Forward, and sending links to other sites to your browser |

Your data lives in `~/Library/Application Support/com.example.buzz/Data/`:
the first account's store is `buzz.sqlite3`, other accounts' are in
`accounts/`, and `accounts.json` lists them.

## Limitations

- Labels are only stored on this Mac. GitHub has no API for organising
  notifications, so they don't sync to github.com or to other computers.
- GitHub's API can't mark a notification as *unread*, or move a done
  notification back to the inbox, so Buzz can't either.
- Buzz only notices threads cleared on github.com when it starts, when it
  reads the whole inbox.
- Attention required relies on GitHub notifying you about a thread, which
  happens for threads you're subscribed to or participating in. Labels that
  flag on activity subscribe you for that reason. Buzz learns about activity
  when GitHub updates the notification, then asks GitHub who was active.
- A bot running on an ordinary user account looks like a person, so its
  activity counts.
- For discussions and releases, GitHub's API doesn't say who was active, so
  any activity on them counts, including your own.
- Threads you've added by number are checked one request each, every 10
  minutes; done ones aren't checked any more.
- Times are compared with your Mac's clock, so a thread viewed while your
  clock is behind GitHub's can keep its ●.
- The bundle ID is the placeholder `com.example` and the app uses Briefcase's
  default icon. Change `bundle` in `pyproject.toml` (and add an `icon`) before
  you share a build. Changing the bundle ID also moves where the app keeps its
  data and its Keychain entries.
