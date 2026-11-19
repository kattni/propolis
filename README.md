# Propolis

Triage your GitHub notifications with labels that keep their order.

GitHub's inbox sorts by latest activity, so anything you've set aside moves every time someone comments on it. Propolis keeps its own triage state instead:

- Everything in your GitHub inbox starts in the Propolis Inbox, read or not.
- File a thread under a label, such as "Deep response", and it stays there, in the order you filed it, however much activity it gets. You can work through each label in the order you triaged it.
- Some labels, such as "High priority", **flag on activity**. When someone else acts on one of their threads, it's also put under **Attention required**, so it comes back to you.
- After that, only activity that needs a response from you counts. Your own activity doesn't, and neither do bots, unless they mention you, request your review or assign you. A failing CI run on one of your pull requests does count.

Propolis is a [BeeWare](https://beeware.org/) app, built with Toga and Briefcase. For now, it runs on macOS.

## Getting started

You need macOS, Python 3.11 or later, and a GitHub account.

```console
$ git clone https://github.com/kattni/propolis.git
$ cd propolis
$ python3 -m venv .venv
$ .venv/bin/pip install briefcase
$ .venv/bin/briefcase dev
```

There's no account to create and nothing to register. The first time Propolis opens, it shows a code. Click **Open GitHub**, enter the code at github.com/login/device, and approve Propolis. It then imports your whole GitHub inbox, and keeps syncing every minute. Each time Propolis starts, it reads the whole inbox again, to catch anything a sync missed.

## Using Propolis

The sidebar lists the **Inbox**, **Attention required** and your labels. The Inbox holds threads you haven't triaged yet, newest first. An icon shows each thread's type, such as pull request, issue or discussion. A ● marks a thread with activity you haven't seen. Imported threads that are unread on GitHub start with a ●. A label's count shows how many threads it has, and how many have something new, for example "(3, 1 new)".

Select a thread to read it in the pane below the list. That counts as viewing it, so it's marked read on GitHub. To comment, sign in to GitHub inside that pane once; the sign-in is remembered.

To triage, select a thread and type a label's number (1 to 9, shown beside each label). The thread moves into that label, and the next thread is selected, so you can work down the list from the keyboard.

| Key | Action |
|---|---|
| 1 to 9 | Apply that label to the selected thread |
| ⌘D | Done: take the thread out of every view, and mark it done on GitHub. Activity from someone else brings it back to the Inbox. |
| ⌫ | Take this view's label off the selected thread. In Attention required, this is how you say you've dealt with it. In the Inbox, it marks the thread done, like ⌘D. |
| ⌘O | Open the selected thread in your browser |
| ⌘T | Add an issue, pull request or discussion to the Inbox by its number, such as `beeware/toga#123`, so you can file it like any other thread |
| ⌘N, ⌘I | New Label, Edit Label (rename, or change "Flag on activity") |
| ⇧⌘⌫ | Delete the selected label, after asking. Its threads go back to the Inbox, unless they have other labels. |
| ⌘↑, ⌘↓ | Move the selected label up or down, which changes its number. Works while the sidebar has focus. |
| ⌘F | Clear the repo, reason and type filters above the thread list |
| ⌘R | Sync now |
| ⌘/ | Show every keyboard shortcut |

Propolis starts with eight labels: Needs review, Question, Deep response, Blocked, Low priority, High priority, In progress and Cold storage. Blocked and High priority flag on activity. You can change all of them. Only the first nine labels have a number key, so to give one of your own labels a number, select it in the sidebar and move it up with ⌘↑.

## Using Propolis on another Mac

A Mac that's signed in to GitHub can share its inbox, so that Propolis on another Mac triages through it, without signing in to GitHub. Everything you do on the other Mac happens on the sharing Mac, which keeps the only copy of your labels and history, and does all the talking to GitHub. So the sharing Mac has to be running Propolis for the other one to work.

Propolis reaches the sharing Mac through a [Cloudflare Tunnel](https://developers.cloudflare.com/cloudflare-one/networks/connectors/cloudflare-tunnel/), so it works from anywhere. On the sharing Mac:

1. Install `cloudflared`, for example with `brew install cloudflared`.
2. Make a tunnel with a public hostname, such as `propolis.example.com`, whose service is `http://localhost:47615`. This needs a Cloudflare account and a domain on Cloudflare. You can make the tunnel in Cloudflare's dashboard, or with `cloudflared tunnel create` and a config file. Run it as a service, so it's there whenever the Mac is.

   To try sharing without a Cloudflare account, run `cloudflared tunnel --url http://localhost:47615` instead. It prints a `trycloudflare.com` address, which changes every time.
3. In Propolis, choose File → Share This Inbox…, and turn on **Share this inbox**. The window shows a code.

On the other Mac, open Propolis, or choose File → Add Account… if it already has an account. Click **Connect to Another Mac…**, and enter the tunnel's address and the code.

To stop, choose File → Disconnect from Other Mac on the Mac that's connected, or turn sharing off on the sharing Mac. To turn away every Mac that has the code, click **New Code** in the Share This Inbox window.

## Taking over someone else's inbox

Propolis can triage more than one GitHub account's notifications, each with its own labels and history. To take over someone's inbox, starting from their history:

1. On their Mac, signed in to Propolis as them, they share their inbox as above, and send you the address and the code.
2. On your Mac, choose File → Add Account…, click **Connect to Another Mac…**, and enter them.

You then triage their notifications on their Mac, from yours. Nothing of yours goes to them, and you never need their GitHub sign-in. To switch between your notifications and theirs, choose File → Switch Account. While you have more than one account, the window title shows whose notifications you're looking at.

The reading pane shows GitHub as you see it, so threads in private repos that only they can see won't open there.

If you sign an account in to GitHub instead, sign in as the account whose notifications it has. GitHub's sign-in page approves whichever account your browser is signed in to, and Propolis refuses a sign-in that doesn't match the account's history.

## Your data

- **It stays on your Mac and on GitHub.** Propolis has no online service of its own. Your GitHub token is kept in your macOS Keychain, and your labels and triage history are in `~/Library/Application Support/com.kattni.propolis/`. Each account you add has its own store in the `accounts` folder there, and its own Keychain entry. An account on another Mac keeps nothing here but that Mac's address, in `accounts.json`, and its code, in the Keychain.
- **Sharing an inbox goes through Cloudflare.** A Mac with the code can see your notifications' titles and links, and your labels and history, and can triage them, so give the code only to someone you'd let do that. Cloudflare carries the traffic, and can read it as it passes through. Your GitHub token never leaves your Mac.
- **Propolis asks GitHub for the `notifications` permission only.** With it, Propolis reads your notifications, marks them read or done, and subscribes you to threads you give a flagging label, so their activity reaches you. It reads timelines, pull request pushes and CI checks on public repos, which needs no extra permission.
- **Private repos work, with less detail.** Their notifications appear, but GitHub doesn't let Propolis see who acted on them, so any update counts as activity. The same goes for discussions and releases.
- **To sign out,** choose File → Sign Out of GitHub. To revoke Propolis's access completely, go to GitHub's Settings → Applications → Authorized OAuth Apps.

## Development

Run the tests with:

```console
$ .venv/bin/briefcase dev --test
```

[docs/design.md](docs/design.md) explains how Propolis works and why: the event log behind the triage state, what counts as activity, the GitHub API costs, triaging through another Mac, and the plans for mobile.

---

The thread type icons are GitHub's [Octicons](https://primer.style/octicons/), used under the MIT License, which is included alongside them in `src/propolis/resources/octicons/`. The app icon puts bees on the pull request Octicon.

This app was generated by [Briefcase](https://briefcase.beeware.org/), part of [The BeeWare Project](https://beeware.org/). If you want to see more tools like Briefcase, please consider [becoming a financial member of BeeWare](https://beeware.org/membership).
