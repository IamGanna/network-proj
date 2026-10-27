# Campfire Chat

A small chat app in Python. One person runs the server and everyone else connects with a desktop client. It supports private messages and group chats.

## Features

- Log in with a unique name
- See who is online
- Private messages between two people
- Create groups, join groups, and chat with the group's members
- Dark desktop UI that stays sharp on high-DPI (scaled) Windows displays

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

## Playing over the internet with Tailscale

By default the clients must be on the same network as the server. To chat from anywhere:

1. Everyone installs [Tailscale](https://tailscale.com/download) and signs in.
2. The host shares their machine with friends from the [Tailscale admin console](https://login.tailscale.com/admin) (Machines → the host → Share), or invites them to the tailnet.
3. The host finds their Tailscale IP with `tailscale ip -4` (it looks like `100.x.x.x`).
4. Friends run `python client.py <host-tailscale-ip>`.

The first time the server runs, Windows Firewall may ask whether to allow Python. Allow it on **private** networks.

## Project layout

| File | What it does |
|---|---|
| `server.py` | TCP server. Tracks online users and groups, and routes messages. One thread per client. |
| `chat_core.py` | Client networking with no UI: login, sending, and a background reader. Any front end can reuse it. |
| `client.py` | Tkinter desktop UI built on `chat_core.py`. |

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

## Limitations

- Messages are sent unencrypted and there are no passwords. Use it on a trusted network or over Tailscale, and don't expose the port directly to the internet.
- Nothing is saved. Message history and groups are lost when the server stops.
- You can only message people who are online.
- A group is deleted when its last member leaves.
