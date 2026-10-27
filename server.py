"""Chat server: TCP sockets, one thread per client, messages are newline-delimited JSON.

Run: python server.py
"""
import json
import socket
import threading

HOST, PORT = "0.0.0.0", 5000  # 0.0.0.0 = accept connections from other computers on the LAN

clients = {}  # name -> socket
groups = {}   # group name -> list of member names
lock = threading.Lock()  # guards clients/groups and keeps writes to a socket from interleaving


def send(sock, msg):
    try:
        sock.sendall((json.dumps(msg) + "\n").encode())
    except OSError:
        pass  # dead client; its own thread cleans it up


def broadcast_state():
    """Push the online-user list (R4) and group list with members (R9) to everyone."""
    for sock in clients.values():
        send(sock, {"type": "users", "users": sorted(clients)})
        send(sock, {"type": "groups", "groups": groups})


def handle(conn, addr):
    name = None
    try:
        for line in conn.makefile("r", encoding="utf-8"):
            msg = json.loads(line)
            kind = msg.get("type")
            with lock:
                if name is None:  # must log in first
                    new = str(msg.get("name", "")).strip()
                    if kind != "login" or not new:
                        send(conn, {"type": "error", "text": "Please log in with a name"})
                    elif new in clients:  # R3: unique names
                        send(conn, {"type": "error", "text": f"Name '{new}' is already taken"})
                    else:
                        name = new
                        clients[name] = conn
                        send(conn, {"type": "login_ok", "name": name})
                        print(f"{name} connected from {addr[0]}:{addr[1]}")
                        broadcast_state()

                elif kind == "pm":  # R7: only sender and receiver get it
                    to = msg.get("to")
                    if to not in clients:
                        send(conn, {"type": "error", "text": f"{to} is offline"})
                    else:
                        out = {"type": "pm", "from": name, "to": to, "text": msg.get("text", "")}
                        send(clients[to], out)
                        if to != name:
                            send(conn, out)

                elif kind == "create_group":  # R8: creator is the only initial member
                    g = str(msg.get("group", "")).strip()
                    if not g or g in groups:
                        send(conn, {"type": "error", "text": f"Group '{g}' is empty or already exists"})
                    else:
                        groups[g] = [name]
                        broadcast_state()

                elif kind == "join_group":  # R10: a client only ever adds itself
                    g = msg.get("group")
                    if g not in groups:
                        send(conn, {"type": "error", "text": f"Group '{g}' does not exist"})
                    elif name not in groups[g]:
                        groups[g].append(name)
                        broadcast_state()

                elif kind == "group_msg":  # R11: only members get it, only members may send
                    g = msg.get("group")
                    if name not in groups.get(g, []):
                        send(conn, {"type": "error", "text": f"You are not a member of '{g}'"})
                    else:
                        out = {"type": "group_msg", "group": g, "from": name, "text": msg.get("text", "")}
                        for member in groups[g]:
                            send(clients[member], out)
    except (OSError, ValueError):
        pass  # connection dropped or bad data: treat as disconnect
    finally:
        with lock:
            if name:
                del clients[name]
                for g in list(groups):
                    if name in groups[g]:
                        groups[g].remove(name)
                        if not groups[g]:
                            del groups[g]
                print(f"{name} disconnected")
                broadcast_state()
        conn.close()


def main():
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)  # IPv4 + TCP
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind((HOST, PORT))
    srv.listen()
    srv.settimeout(1)  # wake up every second so Ctrl+C works on Windows
    print(f"Server listening on port {PORT}. LAN IP (guess): {socket.gethostbyname(socket.gethostname())}")
    while True:
        try:
            conn, addr = srv.accept()
        except TimeoutError:
            continue
        conn.settimeout(None)
        threading.Thread(target=handle, args=(conn, addr), daemon=True).start()


if __name__ == "__main__":
    main()
