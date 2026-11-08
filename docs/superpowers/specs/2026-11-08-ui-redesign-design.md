# Campfire UI Redesign

## Overview

Redesign `client.py` from its current 2-panel layout (sidebar + chat) to the 4-column layout shown in `ui_demo.html`. All existing functionality (DMs, groups, file transfer, TTS, timeouts, AI) is preserved — only the layout and message rendering change.

---

## Layout

Four fixed columns packed left-to-right inside the root `main` frame:

| Column | Widget | Width | Background |
|---|---|---|---|
| Icon bar | `icon_bar` Frame | 48 px | `BG` |
| Sidebar | `side` Frame | 220 px | `PANEL` |
| Chat | `center` Frame | flex expand | `PANEL` |
| Members | `members_panel` Frame | 200 px | `PANEL` |

The members panel is **always packed** — no `pack_forget` / `pack` toggling. It shows group members when in a group, and the other person when in a DM.

### Icon bar

Single flame label (`🔥`) centered at the top. No navigation icons.

```
┌──────┬────────────────┬──────────────────────────────┬──────────────┐
│  🔥  │  Campfire      │  general          🔍 👥 ℹ   │ MEMBERS — 5  │
│      │  Network · 5   │  ─────────────────────────── │ ● Taranon YOU│
│      │  GROUPS    +   │  [av] Maya Chen  10:24        │ ● Maya Chen  │
│      │  # general  5  │  Is everyone free…            │ ● Noah Park  │
│      │  # socket-…    │                               │ ● Leo Santos │
│      │  # project-…   │  [av] Noah Park  10:26        │ ● Iris Kim   │
│      │                │  Yes. I put the sequence…     │              │
│      │  DIRECT MSGS   │    ┌──────────────────────┐   │ About        │
│      │  ● Maya Chen   │    │ 📄 demo-checklist.md │   │ Project …    │
│      │  ● Noah Park   │    └──────────────────────┘   │ Joined       │
│      │  ● Leo Santos  │                               │              │
│      │  ● Iris Kim    │  [🎤][🔊][📎][✨ AI] [Send ➤]│              │
│      ├────────────────┤                               │              │
│      │ [av] Taranon ⚙ │                               │              │
└──────┴────────────────┴──────────────────────────────┴──────────────┘
```

---

## Sidebar

Sections top-to-bottom inside a `ScrollList`:

1. **Header** — "Campfire" bold + workspace subtitle ("Network Project · N online")
2. **GROUPS** label + `+` button (create group)
3. Group rows — `# name` + unread badge or member count
4. **DIRECT MESSAGES · Online N** label
5. DM rows — avatar + online dot + name
6. **Me strip** (bottom, always visible) — avatar + name + "● Online" + ⚙ settings

No "Browse groups" section.

### Sidebar row states

| State | Name colour | Dot | Badge |
|---|---|---|---|
| Active | `TEXT` + `RAISED` bg | green | — |
| Unread | `TEXT` bold | green | `ACCENT` pill |
| Inactive | `MUTED` | green | count dim |
| Offline (DM) | `DIM` | grey `DIM` | — |

When a DM target goes offline the dot turns `DIM` and the row dims. When re-selected, the composer locks (see below).

---

## Chat header

Single `tk.Frame` with:
- `#` prefix label (muted, 15 pt) for groups, omitted for DMs
- `self.title` — room name, bold 15 pt, no subtitle line
- Right-aligned icon buttons: search, members, info (cosmetic for now)
- Thin `LINE` separator below

The `self.subtitle` label is **removed**. Join state ("Public preview") moves into the stage area as a centred card (existing `self.preview` label, restyled).

---

## Message rendering

Each message block in the `tk.Text` box:

```
[36 px avatar]  SenderName   10:24 AM
                Message body text here,
                continues on the next line.
```

Implemented via `box.window_create("end", window=av_canvas)` for the avatar, then name/time/body tags. Grouped messages (same sender, within 5 min) omit the avatar and header — body indented 48 px to align.

### Tags used

| Tag | Style |
|---|---|
| `name:<sender>` | `color_for(sender)`, bold 11 pt, `spacing1=px(12)` |
| `time` | `DIM`, 9 pt |
| `body` | `#DCDAD5`, 11 pt, `spacing1=0` |
| `grouped` | `#DCDAD5`, 11 pt, left margin 48 px |

File attachments render as an embedded `tk.Frame` card (icon + filename + meta + badge) via `window_create`, same as today.

---

## Members panel

Always packed at `width=px(200)`. Content driven by `render_members()`:

| Active room | Panel shows |
|---|---|
| Group (member) | "MEMBERS — N" + member rows + About section |
| Group (preview) | "MEMBERS — N" + member rows, no About |
| DM | "CONVERSATION" + other person's row |
| None selected | "MEMBERS" + online count only |

Each member row: avatar (28 px) + green dot + name + "Online" subtext. "YOU" tag on own row. About section at bottom: group description + "Your membership — Joined".

---

## Composer

```
[🎤] [🔊] [📎] [✨ AI ▾]  [input field — flex]  [Send ➤]
```

**✨ AI popover** (opens above composer):

- **📋 Summarize** — always available
- **✏️ Auto-generate reply** (input empty) / **✏️ Rewrite my message** (input has text)
- Hint line below draft option updates to match

AI response renders as an inline bubble in the message area with an amber border. Draft/rewrite bubble includes a **"Use this reply"** / **"Use rewrite"** button that pastes into the input.

### Offline DM lock

When the DM target is offline:
- Input disabled, placeholder → `"{Name} is offline"`
- All composer buttons faded (`opacity: 0.35`, `pointer-events: none`)
- Notice line above composer: `"{Name} is offline — messages will be delivered when they reconnect."`
- Header status badge switches to "● Offline" (dim colour)

---

## Files touched

| File | Changes |
|---|---|
| `client.py` | `build_ui`, `open_room`, `show`, `render_sidebar`, `render_members`, `select` |
| `ui_demo.html` | Reference only — not shipped |
| `protocol.py` | No changes |
| `server.py` | No changes |

---

## Out of scope

- Reactions (not in protocol — deferred)
- Browse groups section (removed per design decision)
- Navigation icons in icon bar (logo only)
- Keyboard shortcuts, search, settings panel
