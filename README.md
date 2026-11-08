# Campfire Chat

A small chat app in Python. One person runs the server and everyone else connects with a desktop client. It supports private messages and group chats.

## Features

- Log in with a unique name (upper and lower case count as the same name)
- See who is online
- Private messages between two people
- Create groups, join groups, and chat with the group's members
- Dark desktop UI that stays sharp on high-DPI (scaled) Windows displays
- Private AI summaries and concise reply drafts for private chats and joined groups

### Chat activities

Type these in any private or group chat. The server picks every result, so everyone in the chat sees the same one.

| Command | Result |
|---|---|
| `/coin` | Heads or Tails |
| `/dice` | A number from 1 to 6 |
| `/rng` or `/rng <min> <max>` | A random number, 1 to 100 by default |
| `/poll Question \| option 1 \| option 2` | Starts a poll with 2 to 10 options |
| `/vote <poll number> <option number>` | Votes in a poll from the same chat; voting again changes your vote |

## Requirements

- Python 3.10 or newer, with Tkinter (included in the standard Windows and macOS installers)
- No other packages to install

## Quick start

On the computer that hosts the chat:

```
python server.py [port]
```

The server window shows each client's name and IP when it joins or leaves.

On each computer that wants to chat, including the host:

```
python client.py <server-ip> [port]
```

For example, `python client.py 127.0.0.1` on the host itself, or `python client.py 192.168.1.20` from another computer on the same Wi-Fi. The port defaults to `5000`; use the same port on both sides.

The login form opens even when the server is unavailable. Joining runs in a worker
so the window stays responsive. Connection and login-response waits default to
five seconds each. Errors leave the form open for retry; a rejected name can be
changed and submitted again. Closing the window cancels login and closes the
connection. A lost logged-in connection is reported once and closes the client;
start a new client to connect again. Healthy logged-in connections have no idle
timeout; TCP keepalive notices a dead link, such as a Wi-Fi drop, in about 20 seconds.
There is no automatic reconnection or offline delivery.

For other front ends, `ChatCore` connects on `login()`, which returns
`(success, name_or_error)`. `start(callback)` starts one reader and invokes the
callback on its background thread, preserving events buffered during login.
Send methods return a boolean for local transport acceptance; this does not
confirm recipient display. Use `close()` for idempotent cleanup. Constructor
keywords `connect_timeout` and `login_timeout` can override the login budgets.

Run the local regression suite with `python -B -m unittest discover -v`.
Tests require Tk and localhost socket access. Handler tests verify local routing;
they do not establish operation on different physical computers.

## AI setup (server computer)

Copy `.env.example` to `.env` beside `server.py`, then edit locally:

```dotenv
OPENROUTER_API_KEY=your-key-here
```

Restart the server after changing it. The loader uses this exact file even when
the server starts from a different directory. It accepts plain or quoted values,
optional `export`, and comments; it does not expand variables or execute shell
commands. A process `OPENROUTER_API_KEY` overrides `.env` (an empty override disables
AI). Missing/empty configuration leaves normal chat available. No extra packages
are needed. `.env` and `.env.*` are ignored by Git; `.env.example` contains no key.
Keep the real file on the server computer. GitHub Secrets are only useful for an
explicit deployment workflow; clients do not fetch a secret from GitHub.

OpenRouter charges the one shared key for all users. The operator's configured
$1/week cap is shared across the demo, rather than per person. The app reports
budget exhaustion and does not retry or switch models automatically.

Open a private chat or joined group, then:

- **Summarize** opens a private, read-only summary: topic, who said what, and the
  current discussion state. It understands Thai/English and follows the main
  conversation language, aiming for 200–300 output tokens (shorter for short chats).
- **Draft reply** accepts an optional instruction. Generate replaces that room's
  existing draft with a concise response in the conversation's language. It never
  sends automatically. Only that room's composer is frozen while drafting; other
  rooms remain usable. Errors/timeouts retain the exact original draft.

The server supplies only messages routed to your current login session. Group
context starts when you joined. Nothing is loaded from past app sessions, and
generated results are not added to chat history unless you explicitly send a
draft. Switching rooms during generation preserves the original target room.
Results are sent only to the requester, never broadcast to the chat.

Clicking either feature sends the selected room's session messages to OpenRouter
and its model provider over HTTPS. The key stays in the server's Authorization
header. Client/server chat remains on the existing TCP connection. The provider
request uses `data_collection: deny`, `zdr: true`, and `require_parameters: true`;
availability and retention exceptions depend on provider/account policy, as
described in [OpenRouter's ZDR documentation](https://openrouter.ai/docs/guides/features/zdr).
These controls do not encrypt the application's existing plaintext chat TCP link.

The pinned model is `deepseek/deepseek-v4.1-flash-20261017`, exactly as requested.
Its exact date-long API identifier and configured routing succeeded in a
synthetic summary/draft smoke on 2026-11-07. The app does not substitute another
identifier silently. Settings in `ai_service.py`:

| Setting | Summary | Reply draft |
|---|---:|---:|
| Temperature | 0.2 | 0.4 |
| Maximum output tokens | 384 | 120 |
| Maximum output characters | 3,000 | 600 |

Both use `top_p=1`, non-streaming responses and disabled reasoning. Empty,
truncated, tool-call, invalid or overlong answers are rejected. Sampling and
prompting do not guarantee factuality; review generated text before sending.

AI has one pending request per user, a five-second accepted-request cooldown,
and two total jobs across the server. Complete histories are bounded to 512
messages and 256 KiB of canonical escaped JSON per session across rooms. If either
limit is exceeded, AI is disabled for that session instead of silently omitting
older context; normal chat continues. Reopening the app starts a new session.
Each request has a 30-second response acceptance budget and a 35-second UI timeout.
Socket operations have bounded waits, but DNS/header trickling may delay a worker
beyond the budget; late results are rejected, and no extra workers are spawned.

For an opt-in live check with synthetic Thai/English messages only:

```powershell
python -X utf8 tools/ai_smoke.py --live
```

This makes at most one summary and one draft request, stops on the first error,
and may spend from the shared key. Without `--live`, it reads no key and makes no
API call. The automated suite uses fake model/HTTP responses; it cannot establish
live model quality or privacy-route eligibility.


## Project layout

| File | What it does |
|---|---|
| `server.py` | TCP server. Tracks online users and groups, and routes messages with a reader and FIFO writer per client. |
| `activity.py` | Server side of the chat activities (`/coin`, `/dice`, `/rng`, `/poll`, `/vote`). |
| `chat_core.py` | Client networking with no UI: login, sending, and a background reader. Any front end can reuse it. |
| `client.py` | Tkinter desktop UI built on `chat_core.py`. |
| `protocol.py` | Shared schemas, limits, serialized frames and bounded byte reader. |
| `ai_history.py` | Complete, bounded histories per session and room. |
| `ai_service.py` | Server-only .env loading, prompts and bounded OpenRouter HTTPS. |
| `ai_runtime.py` | Two workers with nonblocking, bounded job admission. |
| `tools/ai_smoke.py` | Explicit opt-in live check using synthetic conversations. |

## Protocol

Clients and the server exchange newline-delimited JSON over a single TCP connection.

| Client → server | Fields |
|---|---|
| `login` | `name` |
| `pm` | `to`, `text` |
| `create_group` | `group` |
| `join_group` | `group` |
| `group_msg` | `group`, `text` |
| `ai_request` | `request_id` (32 lowercase hex), `action` (`summary`/`draft`), `room_type`, `room`, `instruction` (0–500 characters; empty for summary) |

| Server → client | Fields |
|---|---|
| `login_ok` | `name` |
| `users` | `users` (list of online names) |
| `groups` | `groups` (group name → list of members) |
| `pm` | `from`, `to`, `text` |
| `group_msg` | `group`, `from`, `text` |
| `error` | `text` |
| `ai_result` | `request_id`, `action`, `room_type`, `room`, `text`, `message_count`, `cutoff` (session message sequence) |
| `ai_error` | `request_id`, `action`, `room_type`, `room`, `code`, `text` |

## Resource policy

These are project defaults, not assignment requirements or measured capacity guarantees.
The constants are centralized in `protocol.py`.

| Resource | Default |
|---|---|
| Encoded JSON frame | 65,536 UTF-8 bytes, including the newline and JSON escaping |
| Message text | 4,096 Unicode characters |
| User or group name | 64 Unicode characters |
| Accepted server connections | 32 total, including pending logins |
| Existing groups | 64 |
| Outbound queue | 1,024 frames per server session |
| Server login deadline | 10 seconds from accept, including partial frames and rejected-name retries |

Both endpoints bound byte input before JSON parsing. Split UTF-8 characters and
coalesced frames are retained correctly, including events received with the login
reply. An oversized or incomplete frame, invalid JSON, expired pending login, or
failed socket closes that connection. A connection beyond the admission cap is
closed without starting session threads. Closing a session releases its reader,
writer, identity and memberships. Healthy logged-in clients have no idle timeout.

Field violations receive controlled errors. `ChatCore` rejects invalid outgoing
fields or serialized frames locally with `False` and an error event; a rejected
login returns `(False, error)`. Local send rejection preserves the connection and
the desktop composer draft. The server independently validates raw clients and
checks the complete serialized output after adding its own sender fields.

Group-capacity and metadata-budget errors reject the requested operation. Before
login, group creation or joining changes state, the server checks that the full
projected users and groups frames fit. Names and membership lists are never
truncated to fit. A full outbound queue disconnects the slow session; other
sessions continue using their own FIFO writers. No uncontrolled load or
machine-independent throughput claim is implied by these limits.

## todo 
    - deploy on cloud 
    - physical-computer demonstration


