---
name: macos-permissions
description: macOS Privacy & Security permissions primer for agent-driven workflows. What each pane in System Settings → Privacy & Security unlocks, how to detect which permissions are missing, and the prompt-driven workflow when the user has local physical access to the Mac. Trigger when a script/launchd/Playwright/python process fails with "permission denied", "operation not permitted", silent hang (e.g. launchd+SMBFS), or when the user mentions granting access, clicking Allow, or getting on the Mac to fix permissions.
---

# macOS Permissions — Class-level primer

The Privacy & Security pane in System Settings controls which apps/scripts/automations can access what on macOS. Many agent-driven failures are **permission failures**, not code bugs. This skill is the diagnostic map.

## Where permissions live

**System Settings → Privacy & Security** (lock icon top-right → authenticate to grant/change).

| Pane | Unlocks |
|------|---------|
| **Local Network** | TCP connections to other devices on 192.168.x.x, 10.x.x.x, etc. Cameras, NAS, printers, IoT. |
| **Full Disk Access** | Read access to TCC.db, system logs, Mail, Messages, Safari, LaunchAgents dirs, anything under `/Library` |
| **Files and Folders** | Per-folder access to Documents, Downloads, Desktop, removable media |
| **Accessibility** | Synthetic input (mouse clicks, keyboard) for any process that automates other apps |
| **Automation** | Per-app cross-app scripting (e.g. Terminal controlling System Events, Safari, Messages) |
| **Camera / Microphone** | Webcam / mic access |
| **Screen Recording** | Screen capture (screencapture, screenshot tools) |
| **App Management** | Terminating other apps, modifying their files |

## Diagnostic ladder — which permission is missing?

| Symptom | Likely missing permission | How to confirm |
|---------|--------------------------|----------------|
| `os.listdir(/Volumes/something)` hangs forever under launchd, instant from shell | **Local Network** (SMBFS opendir blocked) | See `local-infra` skill → `references/launchd-smbfs-opendir-hang-2026-07-19.md` |
| `requests.get("http://192.168.x.x")` returns `Errno 65 No route to host` from launchd, works from shell | **Local Network** (launchd context restriction) | See `local-infra` skill — "macOS 26.x launchd — local network restrictions" |
| `sqlite3 ~/Library/Application Support/com.apple.TCC/TCC.db` → "authorization denied" | **Full Disk Access** for the calling shell | Add Terminal to Full Disk Access in Privacy & Security |
| `osascript` fails with "not authorized to send Apple events to System Events" | **Automation → System Events** for the calling shell | Grant Automation access when prompted, or add to Privacy & Security → Automation |
| Playwright browser session: pages load but login button click does nothing | **Accessibility** for the calling process | Add to Privacy & Security → Accessibility |
| Vision API calls (`127.0.0.1:8080`) time out from a launchd-launched script | **Local Network** for the script's binary | Even localhost is gated under launchd on macOS 26.x |
| Telegram bot can't reach `api.telegram.org` | (rare) **Outgoing Network Connections** in Firewall | System Settings → Network → Firewall → Options |
| File copy to a USB drive or external volume silently fails | **Files and Folders → Removable Volumes** | Grant when prompted or in Privacy & Security |
| `cp` to `~/Library/Application Support/...` from a launchd context fails | **Files and Folders** (and possibly **Full Disk Access**) | Same |

## The prompt-driven workflow (when user has local Mac access)

The user is the only one who can click "Allow" on macOS permission prompts. The agent cannot grant them remotely. Established workflow:

1. **Agent enumerates** what permissions the current task needs based on what failed (see table above)
2. **Agent posts the ordered checklist** to the user — explicit, in the order they should be granted
3. **User sits down at the Mac**, opens System Settings → Privacy & Security
4. **User walks the checklist top to bottom**, granting permissions
5. **User reports back** ("done") and Agent re-runs the failing probe
6. If a permission prompt fires *during* a test (e.g. the script tries to read TCC.db, prompt appears), **user clicks Allow**, then Agent verifies

**Critical pattern**: when the user says *"I'm gonna get on the Mac"* / *"on my way to the farm so I'll have local access"* / *"I just need to get on there and then have you test things"*, the Agent should:
- Switch from "fix it remotely" mode to "guide them through permissions" mode
- Have a pre-staged ordered list ready (Local Network first, then Full Disk Access, then specific items)
- Run a pre-test from the current context first, so the user sees a baseline before they start granting

## Diagnostic commands (run BEFORE asking the user for permissions)

These don't require permissions themselves and tell you what's actually missing:

```bash
# 1. Is Local Network reachable at all?
nc -z -G 3 192.168.1.X 554 && echo OPEN || echo CLOSED

# 2. Can we listdir a known local volume?
.venv/bin/python3 -c "import os; print(os.listdir('/Volumes/surveillance'))"  # from shell

# 3. Can we read TCC.db (gates everything below)?
sqlite3 ~/Library/Application\ Support/com.apple.TCC/TCC.db ".tables" 2>&1
# → "unable to open database" = need Full Disk Access

# 4. Is the listener/launchd-managed process healthy?
curl -s --max-time 2 http://127.0.0.1:8090/health

# 5. Are there permission-related errors in the recent log?
grep -iE "permission|denied|not authorized|errfi_client" logs/*.log | tail -10
```

The TRIANGLE that closes a permission issue:
- Shell-launched python works → not a code issue
- Same code in launchd hangs/fails → launchd context restriction (most likely Local Network or FDA)
- Same code from `launchctl asuser $(id -u)` works → confirmation that the user-session is the missing context

## What NOT to do

- **Don't grant permissions programmatically from a script.** macOS does not expose a "grant X to app Y" command from inside TCC. The only mechanism is the GUI prompt, which requires user interaction.
- **Don't try `tccutil reset` to "fix" a missing permission.** `tccutil reset` *removes* existing grants — it does not create new ones.
- **Don't assume the Mac has FDA for Terminal just because other Terminal commands worked.** FDA is per-app; the user may have granted it for iTerm but not Terminal.
- **Don't ship a script that depends on a permission that isn't granted** without telling the user which permission it needs.
- **Don't use sudo** to "get around" permission issues — sudo doesn't bypass TCC, and macOS 26.x's TCC integrity checks are strict.
- **Don't roll back macOS or downgrade python hoping permissions "reset."** They don't.

## When this bites you — symptom-recognition shortcuts

| Symptom | Most likely cause | Where to look |
|---------|-------------------|---------------|
| Script works from Terminal but hangs/fails under launchd | Local Network or FDA missing for the shell | Re-test from shell with same code path |
| `os.listdir` works on `/Users/...` but hangs on `/Volumes/...` | SMBFS-specific launchd block | sample <pid> for `__opendir2` |
| `sqlite3 TCC.db` fails with "authorization denied" | Full Disk Access missing | Add the calling shell to FDA |
| `osascript -e 'tell application "X" to ...'` silently does nothing | Automation permission missing for X | Grant in Privacy & Security → Automation |
| Playwright clicks land on coordinates but nothing happens | Accessibility missing | Add the calling shell to Accessibility |
| Vision API calls from launchd-launched script time out at localhost | Local Network (launchd doesn't get "default allow localhost" on macOS 26.x) | Add python binary to Local Network |

## Pre-staged ordered checklist (use this when user says they're getting on the Mac)

When the conversation context is "we need to fix permissions" (e.g. launching a new camera, debugging launchd, installing a new agent process), post this ordered list to the user as their action items:

```
1. System Settings → Privacy & Security → Local Network
   - Add: Terminal (or whichever shell you use)
   - Add: Python (if it appears as an option)

2. System Settings → Privacy & Security → Full Disk Access
   - Add: Terminal
   - Add: any launchd-managed helper (e.g. ~/bin/*-watchdog.sh scripts)

3. System Settings → Privacy & Security → Files and Folders
   - Toggle ON for Terminal: Documents, Downloads, Desktop, Removable Volumes

4. System Settings → Privacy & Security → Accessibility
   - Add: Terminal (only if we're doing browser/playwright automation)

5. System Settings → Privacy & Security → Automation
   - Allow Terminal to control: System Events (and any specific app we'll script)

6. System Settings → Network → Firewall → Options
   - Allow incoming connections for our listener at port 8090 (if not already)

After granting, say "done" and I'll re-run the failing probe.
```

## Cross-references

- `local-infra` skill → `references/launchd-smbfs-opendir-hang-2026-07-19.md` — full SMBFS hang investigation transcript (this skill's most common symptom)
- `local-infra` skill → "macOS 26.x launchd — local network restrictions" — RTSP/TCP socket restrictions in launchd
- `local-infra` skill → "Web UI Automation — Persistent Playwright for Local Devices" — why browser automation also needs Accessibility
- `add-camera` skill — what permissions new camera wiring needs (Local Network for RTSP, Accessibility for browser UI)
- `daily-report-cron` skill — what permissions daily-report scripts need under launchd
