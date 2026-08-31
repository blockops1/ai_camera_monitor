---
name: apple
description: |
  macOS system integration skills: Notes, Reminders, Messages, FindMy, and
  background GUI automation. All Apple platform skills share the same macOS
  prerequisite layer (brew installs, permissions, Automation access).
version: 1.0.0
platforms: [macos]
metadata:
  hermes:
    tags: [macos, apple, desktop-automation]
    category: apple
---

# Apple Platform — Unified Skill

All skills in this directory target macOS system integration. They share the same
prerequisite layer and are documented here as labeled subsections.

---

## Shared Prerequisites (all apple skills)

- **macOS** with relevant Apple app signed into the user's iCloud account
- **Brew taps** and CLI tools must be installed per subsection below
- **Automation permissions** granted in System Settings → Privacy & Security → Automation
- **Full Disk Access** or **Screen Recording** where indicated

When in doubt about permissions: run the command once and read the error message —
it will state exactly which permission is missing.

---

## Skill Index

| Skill | What it does | CLI tool | Permission needed |
|-------|-------------|----------|-------------------|
| `apple-notes` | Apple Notes: create, search, edit, folder management | `memo` | Notes Automation |
| `apple-reminders` | Apple Reminders: to-dos, lists, due dates | `remindctl` | Reminders Automation |
| `imessage` | iMessage/SMS: send, read, attachments | `imsg` | Messages Automation + Full Disk Access |
| `findmy` | FindMy: track devices/AirTags via screen capture | `osascript` + `screencapture` | Screen Recording |
| `macos-computer-use` | Background GUI automation: any Mac app, any Space | `cua-driver` (hermes tools) | Accessibility + Screen Recording |

---

## Shared "When NOT to Use" Rules

- **Telegram/Discord/Slack/WhatsApp** → use the appropriate gateway channel tool
- **Project task management** → use GitHub Issues, Linear, Notion
- **Agent-internal notes** → use the `memory` tool
- **Cross-platform/Linux** → these skills are macOS-only

---

##apple-notes (per-subsection)

### Install
```bash
brew tap antoniorodr/memo && brew install antoniorodr/memo/memo
```
Grant Automation access to Notes.app when prompted.

### Workflow
| Action | Command |
|--------|---------|
| List notes | `memo notes` |
| Search | `memo notes -s "query"` |
| Create | `memo notes -a "Title"` |
| Edit | `memo notes -e` (interactive) |
| Move to folder | `memo notes -m` |
| Export | `memo notes -ex` |

### Limitations
- Cannot edit notes containing images or attachments
- Interactive prompts require terminal access (use pty=true if needed)

### Reference
- Full SKILL.md at `apple-notes/SKILL.md` (archived sibling — do not use directly)

---

## apple-reminders (per-subsection)

### Install
```bash
brew install steipete/tap/remindctl
```
Check: `remindctl status` / Authorize: `remindctl authorize`

### Workflow
| Action | Command |
|--------|---------|
| Today's tasks | `remindctl` or `remindctl today` |
| Specific date | `remindctl 2026-01-04` |
| All lists | `remindctl list` |
| Create reminder | `remindctl add --title "Task" --due tomorrow` |
| Complete | `remindctl complete <id>` |

### Clarify First
When user says "remind me", confirm: Apple Reminders (syncs to iPhone/iPad) vs agent cronjob alert.

### Reference
- Full SKILL.md at `apple-reminders/SKILL.md` (archived sibling — do not use directly)

---

## imessage (per-subsection)

### Install
```bash
brew install steipete/tap/imsg
```
Grant Full Disk Access for terminal and Automation permission for Messages.app.

### Workflow
| Action | Command |
|--------|---------|
| List chats | `imsg chats --limit 10 --json` |
| Read history | `imsg history --chat-id <id> --limit 20 --json` |
| Send text | `imsg send --to "+15555551212" --text "Hello!"` |
| Send with file | `imsg send --to "+1..." --file /path/img.jpg` |
| Force iMessage/SMS | `imsg send --service imessage` |

### Safety Rules
1. Always confirm recipient and message content before sending
2. Never send to unknown numbers without explicit approval
3. Verify file paths exist before attaching

### Reference
- Full SKILL.md at `imessage/SKILL.md` (archived sibling — do not use directly)

---

## findmy (per-subsection)

### Prerequisites
- FindMy.app with devices/AirTags already registered
- Screen Recording permission for terminal
- Optional: `peekaboo` (`brew install steipete/tap/peekaboo`) for reliable UI automation

### Method 1: AppleScript + Screenshot (Basic)
```bash
osascript -e 'tell application "FindMy" to activate'
sleep 3
screencapture -w -o /tmp/findmy.png
# Then analyze with vision_analyze
```

### Method 2: Peekaboo (Recommended)
```bash
peekaboo see --app "FindMy" --annotate --path /tmp/findmy-ui.png
peekaboo click --on B3 --app "FindMy"  # by element ID
```

### Limitation
AirTags only update location while the FindMy page is actively displayed.

### Reference
- Full SKILL.md at `findmy/SKILL.md` (archived sibling — do not use directly)

---

## macos-computer-use (per-subsection)

### Setup
Run `hermes tools` and enable Computer Use. The setup installs cua-driver via its
upstream script. Requires macOS + Accessibility + Screen Recording permissions.

### Core Pattern
```
Step 1: computer_use(action="capture", mode="som", app="Safari")
Step 2: computer_use(action="click", element=7)
Step 3: computer_use(action="capture_after=True")  # optional inline verify
```

### Key Rules
1. Never `raise_window=True` unless explicitly asked
2. Scope captures to an app (`app="Safari"`) for cleaner AX tree
3. Never click permission dialogs, password prompts, or payment UI — stop and ask

### Reference
- Full SKILL.md at `macos-computer-use/SKILL.md` (archived sibling — do not use directly)
