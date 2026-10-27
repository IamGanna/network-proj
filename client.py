"""Chat UI ("Campfire" dark theme, Tkinter). All networking lives in chat_core.py.

Run: python client.py <server-ip> [port]      e.g. python client.py 192.168.1.20
"""
import ctypes
import queue
import sys
import time
import tkinter as tk
from tkinter import messagebox

from chat_core import ChatCore

HOST = sys.argv[1] if len(sys.argv) > 1 else "127.0.0.1"
PORT = int(sys.argv[2]) if len(sys.argv) > 2 else 5000

# Sharp text on scaled Windows displays (else Windows bitmap-stretches the window).
# Fonts are in points so they scale by themselves; pixel sizes go through px().
try:
    ctypes.windll.shcore.SetProcessDpiAwareness(1)
    SCALE = ctypes.windll.user32.GetDpiForSystem() / 96
except (AttributeError, OSError):  # not Windows, or Windows older than 8.1
    SCALE = 1


def px(n):
    return round(n * SCALE)


BG, PANEL, RAISED, LINE = "#0E1014", "#171A20", "#262A33", "#2E333D"
TEXT, MUTED, DIM = "#ECEAE5", "#A7ADB8", "#8A909C"
ACCENT, ON_ACCENT, ERROR, ONLINE = "#F2A65A", "#1A1206", "#FF9B8A", "#5FD39A"
PALETTE = ["#8FB4FF", "#7FD1AE", "#E58FB8", "#D7C27A", "#B9A3FF", "#7FC8D1"]
FONT = "Segoe UI"


def color_for(name):
    return PALETTE[sum(map(ord, name)) % len(PALETTE)]  # not hash(): must match across clients


def button(parent, text, command, primary=False, bg=RAISED, padx=14, pady=8):
    return tk.Button(parent, text=text, command=command, relief="flat", bd=0, cursor="hand2",
                     bg=ACCENT if primary else bg, fg=ON_ACCENT if primary else TEXT,
                     activebackground="#FFC48A" if primary else LINE,
                     activeforeground=ON_ACCENT if primary else TEXT,
                     font=(FONT, 10, "bold"), padx=px(padx), pady=px(pady))


def entry(parent):
    return tk.Entry(parent, bg=BG, fg=TEXT, insertbackground=TEXT, relief="flat", font=(FONT, 11),
                    highlightthickness=px(1), highlightbackground=LINE, highlightcolor=ACCENT)


def label(parent, text, size=11, fg=TEXT, bold=False, **kw):
    return tk.Label(parent, text=text, bg=parent["bg"], fg=fg,
                    font=(FONT, size, "bold" if bold else "normal"), **kw)


def avatar(parent, name, size=28):
    s = px(size)
    c = tk.Canvas(parent, width=s, height=s, bg=parent["bg"], highlightthickness=0)
    c.create_oval(1, 1, s - 1, s - 1, fill=color_for(name), outline="")
    c.create_text(s / 2, s / 2, text=name[0].upper(), fill="#14161B", font=(FONT, 10, "bold"))
    return c


class ChatClient:
    def __init__(self, root):
        self.root = root
        root.title("Campfire")
        root.configure(bg=BG)
        root.geometry(f"{px(1100)}x{px(680)}")
        root.minsize(px(820), px(520))
        self.core = ChatCore(HOST, PORT)

        self.users = []
        self.groups = {}
        self.rooms = {}  # ("pm", name) or ("group", name) -> its chat window (R5, R6)
        self.unread = {}
        self.active = None
        self.want = None  # group we created/joined: open it once the server confirms membership
        self.flash_job = None
        self.inbox = queue.Queue()  # network thread -> GUI thread (Tkinter is not thread-safe)
        self.build_login()

    # ---------- login ----------
    def build_login(self):
        self.login_frame = tk.Frame(self.root, bg=BG)
        self.login_frame.pack(fill="both", expand=True)
        card = tk.Frame(self.login_frame, bg=PANEL, padx=px(32), pady=px(32),
                        highlightthickness=px(1), highlightbackground=RAISED)
        card.place(relx=0.5, rely=0.5, anchor="center", width=px(420))
        label(card, "Campfire", 13, ACCENT, bold=True).pack(anchor="w")
        label(card, "Pull up a seat", 22, bold=True).pack(anchor="w", pady=(px(16), px(4)))
        label(card, "Pick a name to join. Everyone online sees it, so it has to be yours alone.",
              10, MUTED, wraplength=px(350), justify="left").pack(anchor="w")
        label(card, f"Server  {HOST}:{PORT}", 9, DIM).pack(anchor="w", pady=(px(16), 0))
        label(card, "Your name", 10, "#C9CDD4", bold=True).pack(anchor="w", pady=(px(12), px(6)))
        self.name_entry = entry(card)
        self.name_entry.pack(fill="x", ipady=px(8))
        self.name_entry.bind("<Return>", self.login)
        self.name_entry.focus_set()
        self.login_error = label(card, "", 10, ERROR)
        self.login_error.pack(anchor="w", pady=(px(6), 0))
        button(card, "Join server", self.login, primary=True).pack(fill="x", pady=(px(8), 0))

    def login(self, _event=None):
        name = self.name_entry.get().strip()
        if not name:
            self.login_error.config(text="Enter a name first.")
            return
        ok, result = self.core.login(name)
        if not ok:
            self.login_error.config(text=result)
            return
        self.name = result
        self.login_frame.destroy()
        self.root.title(f"Campfire - {self.name}")
        self.build_ui()
        self.core.start(self.inbox.put)
        self.poll()

    # ---------- UI ----------
    def build_ui(self):
        main = tk.Frame(self.root, bg=BG)
        main.pack(fill="both", expand=True, padx=px(8), pady=px(8))

        side = tk.Frame(main, bg=PANEL, width=px(250), padx=px(12), pady=px(16))
        side.pack(side="left", fill="y")
        side.pack_propagate(False)
        label(side, "Campfire", 14, bold=True).pack(anchor="w", padx=px(6))
        head = tk.Frame(side, bg=PANEL)
        head.pack(fill="x", pady=(px(20), px(4)))
        label(head, "GROUPS", 9, MUTED, bold=True).pack(side="left", padx=px(6))
        button(head, "+", self.create_group, bg=PANEL, padx=10, pady=0).pack(side="right")
        self.group_box = tk.Frame(side, bg=PANEL)
        self.group_box.pack(fill="x")
        self.online_label = label(side, "ONLINE", 9, MUTED, bold=True)
        self.online_label.pack(anchor="w", padx=px(6), pady=(px(20), px(4)))
        self.user_box = tk.Frame(side, bg=PANEL)  # online users (R4)
        self.user_box.pack(fill="x")
        me = tk.Frame(side, bg=BG, padx=px(10), pady=px(10))
        me.pack(side="bottom", fill="x")
        avatar(me, self.name, 32).pack(side="left")
        info = tk.Frame(me, bg=BG)
        info.pack(side="left", padx=px(10))
        label(info, self.name, 10, bold=True).pack(anchor="w")
        label(info, "Connected", 9, ONLINE).pack(anchor="w")

        self.center = tk.Frame(main, bg=PANEL)
        self.center.pack(side="left", fill="both", expand=True, padx=(px(8), 0))
        self.members_panel = tk.Frame(main, bg=PANEL, width=px(210), padx=px(12), pady=px(16))  # group members (R9)
        self.members_panel.pack_propagate(False)

        header = tk.Frame(self.center, bg=PANEL, padx=px(24), pady=px(14))
        header.pack(fill="x")
        self.title = label(header, "", 15, bold=True)
        self.title.pack(anchor="w")
        self.subtitle = label(header, "", 9, DIM)
        self.subtitle.pack(anchor="w")
        tk.Frame(self.center, bg=RAISED, height=px(1)).pack(fill="x")
        self.error_bar = tk.Label(self.center, bg="#3A2320", fg=ERROR, font=(FONT, 10),
                                  padx=px(24), pady=px(8), anchor="w")
        composer = tk.Frame(self.center, bg=PANEL, padx=px(24), pady=px(16))
        composer.pack(side="bottom", fill="x")
        self.entry = entry(composer)  # chat box (R6)
        self.entry.pack(side="left", fill="x", expand=True, ipady=px(10))
        self.entry.bind("<Return>", self.send_text)
        button(composer, "Send", self.send_text, primary=True).pack(side="right", padx=(px(10), 0))
        self.stage = tk.Frame(self.center, bg=PANEL)
        self.stage.pack(fill="both", expand=True)
        self.empty = label(self.stage, "Pick a group or someone online to start chatting.", 11, MUTED)
        self.empty.place(relwidth=1, relheight=1)
        self.select(None)

    def open_room(self, key):
        if key not in self.rooms:
            box = tk.Text(self.stage, bg=PANEL, fg=TEXT, relief="flat", wrap="word", padx=px(24), pady=px(16),
                          font=(FONT, 11), state="disabled", cursor="arrow", highlightthickness=0)
            box.tag_config("time", foreground=DIM, font=(FONT, 9))
            box.tag_config("body", foreground="#DCDAD5", spacing3=px(12))
            box.place(relwidth=1, relheight=1)
            self.rooms[key] = box
            self.rooms.get(self.active, self.empty).tkraise()  # a new window stacks on top; keep the active one visible
        return self.rooms[key]

    def show(self, key, sender, text):
        box = self.open_room(key)
        tag = "name:" + sender
        box.tag_config(tag, foreground=ACCENT if sender == self.name else color_for(sender), font=(FONT, 11, "bold"))
        box.configure(state="normal")
        box.insert("end", sender, tag, "  " + time.strftime("%H:%M") + "\n", "time", text + "\n", "body")
        box.configure(state="disabled")
        box.see("end")
        if key != self.active:
            self.unread[key] = self.unread.get(key, 0) + 1
            self.render_sidebar()

    def select(self, key):
        self.active = key
        self.unread.pop(key, None)
        if key is None:
            self.title.config(text=f"Welcome, {self.name}")
            self.subtitle.config(text="Groups and people online are on the left.")
            self.empty.tkraise()
            self.entry.config(state="disabled")
        else:
            kind, name = key
            self.open_room(key).tkraise()
            self.title.config(text=("#  " if kind == "group" else "@  ") + name)
            self.subtitle.config(text="Only members see these messages" if kind == "group"
                                 else f"Private: only you and {name} see this")
            self.entry.config(state="normal")
            self.entry.focus_set()
        self.render_sidebar()
        self.render_members()

    def row(self, parent, text, command, key=None, badge="", person=None):
        n = self.unread.get(key)
        bg = RAISED if key and key == self.active else PANEL
        row = tk.Frame(parent, bg=bg, padx=px(8), pady=px(4))
        row.pack(fill="x", pady=px(1))
        if person:
            avatar(row, person).pack(side="left", padx=(0, px(4)))
        if n:
            tk.Label(row, text=f" {n} ", bg=ACCENT, fg=ON_ACCENT, font=(FONT, 9, "bold")).pack(side="right")
        elif badge:
            label(row, badge, 9, ACCENT if badge == "Join" else DIM, bold=badge == "Join").pack(side="right")
        tk.Button(row, text=text, command=command, anchor="w", relief="flat", bd=0, cursor="hand2",
                  bg=bg, fg=TEXT if bg == RAISED or n else MUTED, activebackground=LINE, activeforeground=TEXT,
                  font=(FONT, 11, "bold" if n else "normal"), padx=px(6), pady=px(4)).pack(side="left", fill="x", expand=True)

    def render_sidebar(self):
        for box in (self.group_box, self.user_box):
            for w in box.winfo_children():
                w.destroy()
        for g, members in self.groups.items():
            key = ("group", g)
            if self.name in members:
                self.row(self.group_box, "#  " + g, lambda k=key: self.select(k), key, str(len(members)))
            else:
                self.row(self.group_box, "#  " + g, lambda g=g: self.join_group(g), badge="Join")
        if not self.groups:
            label(self.group_box, "No groups yet. Press + to start one.", 9, DIM).pack(anchor="w", padx=px(6))
        self.online_label.config(text=f"ONLINE · {len(self.users)}")
        others = [u for u in self.users if u != self.name]
        for u in others:
            key = ("pm", u)
            self.row(self.user_box, u, lambda k=key: self.select(k), key, person=u)
        if not others:
            label(self.user_box, "No one else is online yet.", 9, DIM).pack(anchor="w", padx=px(6))

    def render_members(self):
        for w in self.members_panel.winfo_children():
            w.destroy()
        if not self.active or self.active[0] != "group" or self.active[1] not in self.groups:
            self.members_panel.pack_forget()
            return
        members = self.groups[self.active[1]]
        label(self.members_panel, f"MEMBERS · {len(members)}", 9, MUTED, bold=True).pack(anchor="w", pady=(0, px(8)))
        for m in members:
            r = tk.Frame(self.members_panel, bg=PANEL, pady=px(5))
            r.pack(fill="x")
            avatar(r, m).pack(side="left", padx=(0, px(10)))
            label(r, m + (" (you)" if m == self.name else ""), 10).pack(side="left")
        self.members_panel.pack(side="right", fill="y", padx=(px(8), 0), before=self.center)

    def flash(self, text):
        self.error_bar.config(text=text)
        self.error_bar.pack(fill="x", before=self.stage)
        if self.flash_job:
            self.root.after_cancel(self.flash_job)
        self.flash_job = self.root.after(4000, self.error_bar.pack_forget)

    # ---------- user actions ----------
    def join_group(self, g):
        self.want = ("group", g)
        self.core.join_group(g)  # room opens when server confirms membership

    def create_group(self):
        dlg = tk.Toplevel(self.root, bg=PANEL, padx=px(24), pady=px(20))
        dlg.title("Create group")
        dlg.transient(self.root)
        dlg.resizable(False, False)
        label(dlg, "Create a group", 15, bold=True).pack(anchor="w")
        label(dlg, "You start as the only member. Others can join from the sidebar.",
              10, MUTED, wraplength=px(320), justify="left").pack(anchor="w", pady=(px(4), px(12)))
        label(dlg, "Group name", 10, "#C9CDD4", bold=True).pack(anchor="w")
        e = entry(dlg)
        e.pack(fill="x", ipady=px(8), pady=(px(6), px(16)))
        e.focus_set()

        def ok(_event=None):
            g = e.get().strip()
            if g:
                self.want = ("group", g)
                self.core.create_group(g)
            dlg.destroy()

        e.bind("<Return>", ok)
        dlg.bind("<Escape>", lambda _e: dlg.destroy())
        btns = tk.Frame(dlg, bg=PANEL)
        btns.pack(fill="x")
        button(btns, "Create", ok, primary=True).pack(side="right")
        button(btns, "Cancel", dlg.destroy).pack(side="right", padx=(0, px(8)))
        dlg.grab_set()

    def send_text(self, _event=None):
        text = self.entry.get().strip()
        if not text or not self.active:
            return
        kind, name = self.active
        if kind == "pm":
            self.core.pm(name, text)
        else:
            self.core.group_msg(name, text)
        self.entry.delete(0, "end")  # no local echo: the server sends our message back, proving delivery

    # ---------- network events (queued by ChatCore's thread) ----------
    def poll(self):
        while not self.inbox.empty():
            self.handle(self.inbox.get())
        self.root.after(100, self.poll)

    def handle(self, msg):
        kind = msg["type"]
        if kind == "users":  # R4
            self.users = msg["users"]
            self.render_sidebar()
        elif kind == "groups":  # R9
            self.groups = msg["groups"]
            for g, members in self.groups.items():
                if self.name in members:
                    self.open_room(("group", g))
            if self.want and self.name in self.groups.get(self.want[1], []):
                key, self.want = self.want, None
                self.select(key)
            else:
                self.render_sidebar()
                self.render_members()
        elif kind == "pm":
            other = msg["to"] if msg["from"] == self.name else msg["from"]
            self.show(("pm", other), msg["from"], msg["text"])
        elif kind == "group_msg":
            self.show(("group", msg["group"]), msg["from"], msg["text"])
        elif kind == "error":
            self.want = None
            self.flash(msg["text"])
        elif kind == "disconnected":
            messagebox.showerror("Disconnected", "Lost connection to the server")
            self.root.destroy()


if __name__ == "__main__":
    root = tk.Tk()
    ChatClient(root)
    root.mainloop()
