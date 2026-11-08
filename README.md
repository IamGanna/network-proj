# Campfire Chat

A small chat app in Python. One person runs the server and everyone else connects with a desktop client. It supports private messages and group chats.

## Features

- Log in with a unique name
- See who is online
- Private messages between two people
- Create groups, join groups, and chat with the group's members
- Dark desktop UI that stays sharp on high-DPI (scaled) Windows displays

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
python server.py
```

On each computer that wants to chat, including the host:

```
python client.py <server-ip> [port]
```

For example, `python client.py 127.0.0.1` on the host itself, or `python client.py 192.168.1.20` from another computer on the same Wi-Fi. The port defaults to `5000`.

The login form opens even when the server is unavailable. Joining runs in a worker
so the window stays responsive. Connection and login-response waits default to
five seconds each. Errors leave the form open for retry; a rejected name can be
changed and submitted again. Closing the window cancels login and closes the
connection. A lost logged-in connection is reported once and closes the client;
start a new client to connect again. Healthy logged-in connections have no idle
timeout. There is no automatic reconnection or offline delivery.

For other front ends, `ChatCore` connects on `login()`, which returns
`(success, name_or_error)`. `start(callback)` starts one reader and invokes the
callback on its background thread, preserving events buffered during login.
Send methods return a boolean for local transport acceptance; this does not
confirm recipient display. Use `close()` for idempotent cleanup. Constructor
keywords `connect_timeout` and `login_timeout` can override the login budgets.

Run the local regression suite with `python -B -m unittest discover -v`.
Tests require Tk and localhost socket access. Handler tests verify local routing;
they do not establish operation on different physical computers.


## Project layout

| File | What it does |
|---|---|
| `server.py` | TCP server. Tracks online users and groups, and routes messages with a reader and FIFO writer per client. |
| `activity.py` | Server side of the chat activities (`/coin`, `/dice`, `/rng`, `/poll`, `/vote`). |
| `chat_core.py` | Client networking with no UI: login, sending, and a background reader. Any front end can reuse it. |
| `client.py` | Tkinter desktop UI built on `chat_core.py`. |
| `protocol.py` | Shared schemas, limits, serialized frames and bounded byte reader. |

## Protocol

Clients and the server exchange newline-delimited JSON over a single TCP connection.

| Client → server | Fields |
|---|---|
| `login` | `name` |
| `pm` | `to`, `text` |
| `create_group` | `group` |
| `join_group` | `group` |
| `group_msg` | `group`, `text` |

| Server → client | Fields |
|---|---|
| `login_ok` | `name` |
| `users` | `users` (list of online names) |
| `groups` | `groups` (group name → list of members) |
| `pm` | `from`, `to`, `text` |
| `group_msg` | `group`, `from`, `text` |
| `error` | `text` |

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
| Outbound queue | 64 frames per server session |
| Server login deadline | 10 seconds from accept, including partial frames and rejected-name retries |

Both endpoints bound byte input before JSON parsing. Split UTF-8 characters and
coalesced frames are retained correctly, including events received with the login
reply. An oversized or incomplete frame, invalid JSON, expired pending login, or
failed socket closes that connection. A connection beyond the admission cap is
closed without starting session threads. Closing a session releases its reader,
writer, identity and memberships. Healthy logged-in clients have no idle timeout.
Field violations receive controlled errors. `ChatCore` rejects invalid outgoing
fields or serialized frames locally with `False` and an error event; a rejected
login returns `(False, error)`. Local send rejection preserves the co
nnection and
the desktop composer draft. The server independently validates raw clients and
checks the complete serialized output after adding its own sender fields.

Group-capacity and metadata-budget errors reject the requested operation. Before
login, group creation or joining changes state, the server checks that the full
projected users and groups frames fit. Names and membership lists are never
truncated to fit. A full outbound queue disconnects the slow session; other
sessions continue using their own FIFO writers. No uncontrolled load or
machine-independent throughput claim is implied by these limits.



