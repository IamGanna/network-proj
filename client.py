"""Chat UI ("Campfire" dark theme, Tkinter). All networking lives in chat_core.py.

Run: python client.py <server-ip> [port]      e.g. python client.py 192.168.1.20
"""
import base64
import ctypes
import argparse
import os
import queue
import sys
import threading
import time
import uuid
import tkinter as tk
from tkinter import filedialog, messagebox

try:
    from PIL import Image, ImageTk
    HAS_PIL = True
except ImportError:
    HAS_PIL = False

from chat_core import ChatCore
from protocol import validate_server_event

HOST, PORT = "127.0.0.1", 5000

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


class ScrollList(tk.Frame):
    """A bounded list whose wheel and navigation bindings stay local to it."""
    def __init__(self, parent):
        super().__init__(parent, bg=PANEL)
        self.canvas = tk.Canvas(self, bg=PANEL, highlightthickness=0, takefocus=1)
        self.vertical = tk.Scrollbar(self, orient="vertical", command=self.canvas.yview)
        horizontal = tk.Scrollbar(self, orient="horizontal", command=self.canvas.xview)
        self.canvas.configure(yscrollcommand=self.vertical.set, xscrollcommand=horizontal.set)
        self.vertical.pack(side="right", fill="y")
        horizontal.pack(side="bottom", fill="x")
        self.canvas.pack(side="left", fill="both", expand=True)
        self.content = tk.Frame(self.canvas, bg=PANEL)
        self.window = self.canvas.create_window(0, 0, anchor="nw", window=self.content)
        self.content.bind("<Configure>", self._layout)
        self.canvas.bind("<Configure>", self._layout)
        self.bind_controls(self.canvas)
        self.bind_controls(self.content)

    def _layout(self, _event=None):
        self.canvas.itemconfigure(self.window, width=max(self.canvas.winfo_width(), self.content.winfo_reqwidth()))
        self.canvas.configure(scrollregion=self.canvas.bbox("all"))

    def bind_controls(self, widget):
        widget.bind("<MouseWheel>", self._wheel, add="+")
        widget.bind("<Button-4>", lambda e: self._scroll(-1), add="+")
        widget.bind("<Button-5>", lambda e: self._scroll(1), add="+")
        for key, amount, unit in (("Up", -1, "units"), ("Down", 1, "units"),
                                  ("Prior", -1, "pages"), ("Next", 1, "pages")):
            widget.bind("<" + key + ">", lambda e, a=amount, u=unit: self._scroll(a, u), add="+")
        widget.bind("<FocusIn>", self._focus_visible, add="+")
        for child in widget.winfo_children():
            self.bind_controls(child)

    def _scroll(self, amount, unit="units"):
        self.canvas.yview_scroll(amount, unit)
        return "break"

    def _wheel(self, event):
        return self._scroll(-max(1, abs(event.delta) // 120) if event.delta > 0
                            else max(1, abs(event.delta) // 120))

    def _focus_visible(self, event):
        widget = event.widget
        if widget in (self.canvas, self.content):
            return
        top = widget.winfo_rooty() - self.content.winfo_rooty()
        bottom = top + widget.winfo_height()
        visible_top = self.canvas.canvasy(0)
        height = self.canvas.winfo_height()
        total = max(self.content.winfo_height(), 1)
        if top < visible_top:
            self.canvas.yview_moveto(top / total)
        elif bottom > visible_top + height:
            self.canvas.yview_moveto((bottom - height) / total)


class ChatClient:
    def __init__(self, root):
        self.root = root
        root.title("Campfire")
        root.configure(bg=BG)
        root.geometry(f"{px(1100)}x{px(680)}")
        root.minsize(px(820), px(520))
        self.core = ChatCore(HOST, PORT)
        self.closed = False
        self.login_worker = None
        self.login_results = queue.Queue()
        self.login_job = self.poll_job = None
        root.protocol("WM_DELETE_WINDOW", self.close)

        self.users = []
        self.groups = {}
        self.incoming_files = {}  # file_id -> {"meta": msg, "chunks": {}}
        self.file_data_store = {}  # file_id -> raw_bytes
        self.file_photos = []  # retain PhotoImage references
        self.group_timeouts = {}  # group -> {member: expiry_timestamp}
        self.my_timeouts = {}     # group -> expiry_timestamp
        self.rooms = {}  # ("pm", name) or ("group", name) -> its chat window (R5, R6)
        self.unread = {}
        self.drafts = {}  # exact composer text keyed by room identity
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
        self.login_button = button(card, "Join server", self.login, primary=True)
        self.login_button.pack(fill="x", pady=(px(8), 0))

    def login(self, _event=None):
        if self.closed or self.login_worker is not None:
            return
        name = self.name_entry.get().strip()
        if not name:
            self.login_error.config(text="Enter a name first.")
            return
        self.login_error.config(text="")
        self.login_button.config(state="disabled")
        self.name_entry.config(state="disabled")

        def attempt():
            # Only the Tk thread touches widgets, including on failure or close.
            self.login_results.put(self.core.login(name))

        self.login_worker = threading.Thread(target=attempt, daemon=True)
        self.login_worker.start()
        self.login_job = self.root.after(25, self.poll_login)

    def poll_login(self):
        self.login_job = None
        if self.closed:
            return
        try:
            ok, result = self.login_results.get_nowait()
        except queue.Empty:
            self.login_job = self.root.after(25, self.poll_login)
            return
        self.login_worker.join(timeout=1)
        self.login_worker = None
        self.login_button.config(state="normal")
        self.name_entry.config(state="normal")
        if not ok:
            self.login_error.config(text=result)
            return
        self.name = result
        self.login_frame.destroy()
        self.root.title(f"Campfire - {self.name}")
        self.build_ui()
        self.core.start(self.inbox.put)
        self.poll()

    def close(self):
        if self.closed:
            return
        self.closed = True
        for job in (self.login_job, self.poll_job, self.flash_job):
            if job is not None:
                self.root.after_cancel(job)
        self.core.close()
        if self.login_worker is not None:
            self.login_worker.join(timeout=1)
        self.root.destroy()

    # ---------- UI ----------
    def build_ui(self):
        main = tk.Frame(self.root, bg=BG)
        main.pack(fill="both", expand=True, padx=px(8), pady=px(8))

        side = tk.Frame(main, bg=PANEL, width=px(250), padx=px(12), pady=px(16))
        side.pack(side="left", fill="y")
        side.pack_propagate(False)
        label(side, "Campfire", 14, bold=True).pack(anchor="w", padx=px(6))
        me = tk.Frame(side, bg=BG, padx=px(10), pady=px(10))
        me.pack(side="bottom", fill="x")
        avatar(me, self.name, 32).pack(side="left")
        info = tk.Frame(me, bg=BG)
        info.pack(side="left", padx=px(10))
        label(info, self.name, 10, bold=True).pack(anchor="w")
        label(info, "Connected", 9, ONLINE).pack(anchor="w")
        lists = tk.Frame(side, bg=PANEL)
        lists.pack(fill="both", expand=True)
        lists.grid_rowconfigure(1, weight=1, uniform="lists")
        lists.grid_rowconfigure(3, weight=1, uniform="lists")
        lists.grid_columnconfigure(0, weight=1)
        head = tk.Frame(lists, bg=PANEL)
        head.grid(row=0, column=0, sticky="ew", pady=(px(20), px(4)))
        label(head, "GROUPS", 9, MUTED, bold=True).pack(side="left", padx=px(6))
        button(head, "+", self.create_group, bg=PANEL, padx=10, pady=0).pack(side="right")
        self.group_list = ScrollList(lists)
        self.group_list.grid(row=1, column=0, sticky="nsew")
        self.group_box = self.group_list.content
        self.online_label = label(lists, "ONLINE", 9, MUTED, bold=True)
        self.online_label.grid(row=2, column=0, sticky="w", padx=px(6), pady=(px(20), px(4)))
        self.user_list = ScrollList(lists)
        self.user_list.grid(row=3, column=0, sticky="nsew")
        self.user_box = self.user_list.content  # online users (R4)

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
        self.join_button = button(header, "Join group", self.join_selected, primary=True)
        tk.Frame(self.center, bg=RAISED, height=px(1)).pack(fill="x")
        self.error_bar = tk.Label(self.center, bg="#3A2320", fg=ERROR, font=(FONT, 10),
                                  padx=px(24), pady=px(8), anchor="w")
        composer = tk.Frame(self.center, bg=PANEL, padx=px(24), pady=px(16))
        composer.pack(side="bottom", fill="x")
        self.entry = entry(composer)  # chat box (R6)
        self.entry.pack(side="left", fill="x", expand=True, ipady=px(10))
        self.entry.bind("<Return>", self.send_text)
        self.file_button = button(composer, "📎 File", self.choose_file, bg=RAISED, padx=10, pady=8)
        self.file_button.pack(side="right", padx=(px(8), 0))
        self.send_button = button(composer, "Send", self.send_text, primary=True)
        self.send_button.pack(side="right", padx=(px(8), 0))
        self.stage = tk.Frame(self.center, bg=PANEL)
        self.stage.pack(fill="both", expand=True)
        self.empty = label(self.stage, "Pick a group or someone online to start chatting.", 11, MUTED)
        self.empty.place(relwidth=1, relheight=1)
        self.preview = label(self.stage, "Group preview · Join to see messages and chat.", 11, MUTED)
        self.preview.place(relwidth=1, relheight=1)
        self.select(None)

    def open_room(self, key):
        if key not in self.rooms:
            box = tk.Text(self.stage, bg=PANEL, fg=TEXT, relief="flat", wrap="word", padx=px(24), pady=px(16),
                          font=(FONT, 11), state="disabled", cursor="arrow", highlightthickness=0)
            box.tag_config("time", foreground=DIM, font=(FONT, 9))
            box.tag_config("body", foreground="#DCDAD5", spacing3=px(12))
            box.place(relwidth=1, relheight=1)
            self.rooms[key] = box
            visible = (self.preview if self.active and self.active[0] == "group"
                       and not self.can_send(self.active) else self.rooms.get(self.active, self.empty))
            visible.tkraise()  # a new window stacks on top; keep the active view visible
        return self.rooms[key]

    def show(self, key, sender, text):
        box = self.open_room(key)
        tag = "name:" + sender
        if sender == "[System]":
            fg = "#FF9B8A"
        elif sender == self.name:
            fg = ACCENT
        else:
            fg = color_for(sender)
        box.tag_config(tag, foreground=fg, font=(FONT, 11, "bold"))
        box.configure(state="normal")
        box.insert("end", sender, tag, "  " + time.strftime("%H:%M") + "\n", "time", text + "\n", "body")
        box.configure(state="disabled")
        box.see("end")
        if key != self.active:
            self.unread[key] = self.unread.get(key, 0) + 1
            self.render_sidebar()

    def is_my_timeout_active(self, group):
        exp = self.my_timeouts.get(group)
        if exp is None:
            exp = self.group_timeouts.get(group, {}).get(self.name)
            if exp is not None:
                self.my_timeouts[group] = exp
        if exp is None:
            return False
        if time.time() >= exp:
            self.my_timeouts.pop(group, None)
            if group in self.group_timeouts:
                self.group_timeouts[group].pop(self.name, None)
            return False
        return True

    def format_remaining(self, seconds):
        if seconds >= 86400:
            return f"{seconds // 86400}d {(seconds % 86400) // 3600}h"
        if seconds >= 3600:
            return f"{seconds // 3600}h {(seconds % 3600) // 60}m"
        if seconds >= 60:
            return f"{seconds // 60}m {seconds % 60}s"
        return f"{seconds}s"

    def can_send(self, key):
        if not key:
            return False
        kind, name = key
        if kind == "pm":
            return True
        if kind == "group":
            if self.name not in self.groups.get(name, []):
                return False
            if self.is_my_timeout_active(name):
                return False
            return True
        return False

    def select(self, key):
        if key and key[0] == "group" and key[1] not in self.groups:
            key = None
        if self.active is not None:
            self.drafts[self.active] = self.entry.get()
        self.active = key
        self.join_button.pack_forget()
        allowed = self.can_send(key)
        self.entry.config(state="normal")
        self.entry.delete(0, "end")
        if key is not None:
            self.entry.insert(0, self.drafts.get(key, ""))
        self.entry.config(state="normal" if allowed else "disabled")
        self.send_button.config(state="normal" if allowed else "disabled")
        self.file_button.config(state="normal" if allowed else "disabled")
        if key is None:
            self.title.config(text=f"Welcome, {self.name}")
            self.subtitle.config(text="Groups and people online are on the left.")
            self.empty.tkraise()
        else:
            kind, name = key
            self.title.config(text=("#  " if kind == "group" else "@  ") + name)
            if kind == "group" and not allowed:
                if self.name not in self.groups.get(name, []):
                    self.subtitle.config(text="Public membership preview · You have not joined")
                    self.preview.tkraise()
                    self.join_button.pack(anchor="w", pady=(px(6), 0))
                else:
                    self.unread.pop(key, None)
                    self.open_room(key).tkraise()
                    rem = max(1, int(self.my_timeouts.get(name, time.time()) - time.time()))
                    self.subtitle.config(text=f"⏳ You are timed out in this group ({self.format_remaining(rem)} remaining)")
            else:
                self.unread.pop(key, None)
                self.open_room(key).tkraise()
                self.subtitle.config(text="Only members see these messages" if kind == "group"
                                     else f"Private: only you and {name} see this")
                self.entry.focus_set()
        self.render_sidebar()
        self.render_members()

    def join_selected(self):
        if self.active and self.active[0] == "group":
            self.join_group(self.active[1])

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
                self.row(self.group_box, "#  " + g, lambda k=key: self.select(k), key, badge="Preview")
        if not self.groups:
            label(self.group_box, "No groups yet. Press + to start one.", 9, DIM).pack(anchor="w", padx=px(6))
        self.online_label.config(text=f"ONLINE · {len(self.users)}")
        for u in self.users:
            key = ("pm", u)
            display_name = u + " (you)" if u == self.name else u
            self.row(self.user_box, display_name, lambda k=key: self.select(k), key, person=u)
        if not self.users:
            label(self.user_box, "No one is online yet.", 9, DIM).pack(anchor="w", padx=px(6))
        for area in (self.group_list, self.user_list):
            for child in area.content.winfo_children():
                area.bind_controls(child)

    def render_members(self):
        for w in self.members_panel.winfo_children():
            w.destroy()
        if not self.active or self.active[0] != "group" or self.active[1] not in self.groups:
            self.members_panel.pack_forget()
            return
        g = self.active[1]
        members = self.groups[g]
        owner = members[0] if members else None
        is_owner = (self.name == owner)
        timeouts = self.group_timeouts.get(g, {})
        now = time.time()
        for m in list(timeouts):
            if now >= timeouts[m]:
                del timeouts[m]
                if m == self.name:
                    self.my_timeouts.pop(g, None)

        label(self.members_panel, f"MEMBERS · {len(members)}", 9, MUTED, bold=True).pack(anchor="w", pady=(0, px(8)))
        self.member_list = ScrollList(self.members_panel)
        self.member_list.pack(fill="both", expand=True)
        for m in members:
            r = tk.Frame(self.member_list.content, bg=PANEL, pady=px(5))
            r.pack(fill="x")
            avatar(r, m).pack(side="left", padx=(0, px(8)))
            label(r, m + (" (you)" if m == self.name else ""), 10).pack(side="left")
            if m == owner:
                tk.Label(r, text=" Owner ", bg="#2E2818", fg="#D7C27A",
                         font=(FONT, 8, "bold")).pack(side="left", padx=(px(4), 0))
            is_timed_out = (m in timeouts and now < timeouts[m])
            if is_timed_out:
                tk.Label(r, text=" Timed out ", bg="#3A2320", fg=ERROR,
                         font=(FONT, 8, "bold")).pack(side="left", padx=(px(4), 0))
            if is_owner and m != self.name:
                btn_txt = "Manage" if is_timed_out else "Timeout"
                btn = tk.Button(r, text=btn_txt, command=lambda target=m: self.open_timeout_dialog(target),
                                relief="flat", bd=0, cursor="hand2", bg=RAISED, fg=TEXT,
                                activebackground=LINE, activeforeground=TEXT,
                                font=(FONT, 8, "bold"), padx=px(6), pady=px(2))
                btn.pack(side="right")
            self.member_list.bind_controls(r)
        self.members_panel.pack(side="right", fill="y", padx=(px(8), 0), before=self.center)

    def open_timeout_dialog(self, target):
        if not self.active or self.active[0] != "group":
            return
        g = self.active[1]
        dlg = tk.Toplevel(self.root, bg=PANEL, padx=px(24), pady=px(20))
        dlg.title(f"Timeout {target}")
        dlg.transient(self.root)
        dlg.resizable(False, False)

        label(dlg, f"Timeout {target}", 14, bold=True).pack(anchor="w")
        label(dlg, f"Select how long {target} will be unable to send messages in #{g}.",
              10, MUTED, wraplength=px(320), justify="left").pack(anchor="w", pady=(px(4), px(12)))

        timeouts = self.group_timeouts.get(g, {})
        now = time.time()
        for m in list(timeouts):
            if now >= timeouts[m]:
                del timeouts[m]
                if m == self.name:
                    self.my_timeouts.pop(g, None)
        is_currently_timed_out = target in timeouts and now < timeouts[target]
        if is_currently_timed_out:
            rem = max(1, int(timeouts[target] - now))
            status_box = tk.Frame(dlg, bg="#2A1E1E", padx=px(10), pady=px(8))
            status_box.pack(fill="x", pady=(0, px(12)))
            label(status_box, f"Currently timed out ({self.format_remaining(rem)} remaining)",
                  9, ERROR, bold=True).pack(anchor="w")

        dur_var = tk.StringVar(value="60")
        options = [
            ("60 seconds (1 minute)", "60"),
            ("5 minutes", "300"),
            ("10 minutes", "600"),
            ("1 hour", "3600"),
            ("1 day", "86400"),
            ("1 week", "604800"),
        ]

        opts_frame = tk.Frame(dlg, bg=PANEL)
        opts_frame.pack(fill="x", pady=(0, px(16)))

        for text, val in options:
            rb = tk.Radiobutton(opts_frame, text=text, value=val, variable=dur_var,
                                bg=PANEL, fg=TEXT, selectcolor=BG, activebackground=PANEL,
                                activeforeground=TEXT, font=(FONT, 10), cursor="hand2")
            rb.pack(anchor="w", pady=px(2))

        btns = tk.Frame(dlg, bg=PANEL)
        btns.pack(fill="x", pady=(px(8), 0))

        def apply_timeout(duration):
            self.core.timeout_member(g, target, duration)
            dlg.destroy()

        button(btns, "Apply Timeout", lambda: apply_timeout(dur_var.get()), primary=True).pack(side="right")
        if is_currently_timed_out:
            button(btns, "Remove Timeout", lambda: apply_timeout("0"), bg="#3A2320").pack(side="right", padx=(0, px(8)))
        button(btns, "Cancel", dlg.destroy).pack(side="right", padx=(0, px(8)))

        dlg.grab_set()

    def flash(self, text):
        self.error_bar.config(text=text)
        self.error_bar.pack(fill="x", before=self.stage)
        if self.flash_job:
            self.root.after_cancel(self.flash_job)
        self.flash_job = self.root.after(4000, self.error_bar.pack_forget)

    # ---------- user actions ----------
    def join_group(self, g):
        if g not in self.groups or self.name in self.groups[g]:
            return
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
        if not text or not self.can_send(self.active):
            return
        kind, name = self.active
        if kind == "pm":
            sent = self.core.pm(name, text)
        else:
            sent = self.core.group_msg(name, text)
        if sent:
            self.entry.delete(0, "end")
            self.drafts[self.active] = ""  # the server echo is the only displayed message

    def choose_file(self):
        if not self.can_send(self.active):
            return
        filepath = filedialog.askopenfilename(
            title="Select file or picture to send",
            filetypes=[
                ("All files", "*.*"),
                ("Images", "*.png;*.gif;*.jpg;*.jpeg;*.webp;*.bmp"),
                ("Documents", "*.pdf;*.txt;*.docx"),
            ]
        )
        if not filepath:
            return
        try:
            filesize = os.path.getsize(filepath)
            with open(filepath, "rb") as f:
                raw_bytes = f.read()
        except OSError as exc:
            self.flash(f"Could not open file: {exc}")
            return
        if filesize > 10 * 1024 * 1024:
            self.flash("File exceeds the 10 MB limit")
            return
        if filesize == 0:
            self.flash("Cannot send an empty file")
            return

        target_type, target = self.active
        filename = os.path.basename(filepath)
        file_id = uuid.uuid4().hex[:16]
        self.file_data_store[file_id] = raw_bytes
        self.flash(f"Sending {filename}...")

        def upload():
            ok = self.core.send_file(target_type, target, filepath, file_id=file_id)
            if not ok:
                self.file_data_store.pop(file_id, None)
                self.inbox.put({"type": "error", "text": f"Failed to send '{filename}'"})

        threading.Thread(target=upload, daemon=True).start()

    def format_bytes(self, n):
        if n >= 1024 * 1024:
            return f"{n / (1024 * 1024):.1f} MB"
        if n >= 1024:
            return f"{n / 1024:.1f} KB"
        return f"{n} B"

    def create_image_thumbnail(self, filename, raw_bytes):
        if HAS_PIL:
            try:
                import io
                pil_im = Image.open(io.BytesIO(raw_bytes))
                pil_im.thumbnail((360, 260))
                return ImageTk.PhotoImage(pil_im)
            except Exception:
                pass
        if filename.lower().endswith((".png", ".gif")):
            try:
                b64_str = base64.b64encode(raw_bytes).decode("ascii")
                img = tk.PhotoImage(data=b64_str)
                w, h = img.width(), img.height()
                if w > 360 or h > 260:
                    factor = max(1, (w + 359) // 360, (h + 259) // 260)
                    img = img.subsample(factor, factor)
                return img
            except Exception:
                pass
        return None

    def show_file(self, key, sender, filename, filesize, file_id, raw_bytes=None):
        box = self.open_room(key)
        tag = "name:" + sender
        box.tag_config(tag, foreground=ACCENT if sender == self.name else color_for(sender), font=(FONT, 11, "bold"))
        box.configure(state="normal")
        box.insert("end", sender, tag, "  " + time.strftime("%H:%M") + "\n", "time")

        card = tk.Frame(box, bg="#1B1F27", padx=px(14), pady=px(10),
                        highlightthickness=px(1), highlightbackground=LINE)

        # Image preview for image files
        is_image = filename.lower().endswith((".png", ".gif", ".jpg", ".jpeg", ".webp", ".bmp"))
        if is_image and raw_bytes:
            img = self.create_image_thumbnail(filename, raw_bytes)
            if img:
                self.file_photos.append(img)
                img_lbl = tk.Label(card, image=img, bg="#1B1F27")
                img_lbl.pack(anchor="w", pady=(0, px(8)))

        info_row = tk.Frame(card, bg="#1B1F27")
        info_row.pack(fill="x")
        icon = "🖼️ " if is_image else "📄 "
        label(info_row, icon + filename, 10, bold=True).pack(side="left")
        label(info_row, f" ({self.format_bytes(filesize)})", 9, MUTED).pack(side="left")

        # Save button for recipient or when data is available
        data_to_save = raw_bytes or self.file_data_store.get(file_id)
        if data_to_save is not None:
            def save():
                dest = filedialog.asksaveasfilename(initialfile=filename, title=f"Save {filename}")
                if dest:
                    try:
                        with open(dest, "wb") as f:
                            f.write(data_to_save)
                        self.flash(f"Saved {filename}")
                    except OSError as err:
                        self.flash(f"Save failed: {err}")
            save_btn = button(card, "💾 Save file", save, bg=RAISED, padx=8, pady=4)
            save_btn.pack(anchor="w", pady=(px(8), 0))

        box.window_create("end", window=card)
        box.insert("end", "\n\n")
        box.configure(state="disabled")
        box.see("end")
        if key != self.active:
            self.unread[key] = self.unread.get(key, 0) + 1
            self.render_sidebar()
        box.configure(state="disabled")
        box.see("end")
        if key != self.active:
            self.unread[key] = self.unread.get(key, 0) + 1
            self.render_sidebar()

    # ---------- network events (queued by ChatCore's thread) ----------
    def poll(self):
        if self.closed:
            return
        while not self.inbox.empty():
            self.handle(self.inbox.get())
            if self.closed:
                return
        self.check_timeout_countdown()
        self.poll_job = self.root.after(100, self.poll)

    def check_timeout_countdown(self):
        now = time.time()
        active_group_had_expiry = False
        active_g = self.active[1] if (self.active and self.active[0] == "group") else None

        for grp, grp_timeouts in list(self.group_timeouts.items()):
            for m, exp in list(grp_timeouts.items()):
                if now >= exp:
                    grp_timeouts.pop(m, None)
                    if m == self.name and grp in self.my_timeouts:
                        self.my_timeouts.pop(grp, None)
                    if grp == active_g:
                        active_group_had_expiry = True

        if active_g and active_g in self.my_timeouts and now >= self.my_timeouts[active_g]:
            self.my_timeouts.pop(active_g, None)
            active_group_had_expiry = True

        if active_group_had_expiry:
            self.render_members()
            allowed = self.can_send(self.active)
            self.entry.config(state="normal" if allowed else "disabled")
            self.send_button.config(state="normal" if allowed else "disabled")
            self.file_button.config(state="normal" if allowed else "disabled")
            if self.name in self.groups.get(active_g, []):
                self.subtitle.config(text="Only members see these messages")
            if allowed:
                self.entry.focus_set()

        if active_g and self.is_my_timeout_active(active_g):
            rem = max(1, int(self.my_timeouts[active_g] - now))
            self.subtitle.config(text=f"⏳ You are timed out in this group ({self.format_remaining(rem)} remaining)")
            self.entry.config(state="disabled")
            self.send_button.config(state="disabled")
            self.file_button.config(state="disabled")

    def handle(self, msg):
        try:
            msg = validate_server_event(msg)
        except ValueError as exc:
            self.flash(f"Invalid server event: {exc}")
            return
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
                if self.active and self.active[0] == "group" and self.active[1] not in self.groups:
                    if self.want == self.active:
                        self.want = None
                    self.select(None)
                elif self.active and self.active[0] == "group":
                    self.select(self.active)
                else:
                    self.render_sidebar()
                    self.render_members()
        elif kind == "pm":
            other = msg["to"] if msg["from"] == self.name else msg["from"]
            self.show(("pm", other), msg["from"], msg["text"])
        elif kind == "group_msg":
            if self.name in self.groups.get(msg["group"], []):
                self.show(("group", msg["group"]), msg["from"], msg["text"])
        elif kind == "file_start":
            file_id = msg["file_id"]
            sender = msg["from"]
            target_type = msg["target_type"]
            target = msg["target"]
            filesize = int(msg["filesize"])
            filename = msg["filename"]
            if sender == self.name:
                room_key = ("pm", target) if target_type == "pm" else ("group", target)
                raw_bytes = self.file_data_store.get(file_id)
                self.show_file(room_key, sender, filename, filesize, file_id, raw_bytes=raw_bytes)
            else:
                self.incoming_files[file_id] = {"meta": msg, "chunks": {}}
        elif kind == "file_chunk":
            file_id = msg["file_id"]
            if file_id in self.incoming_files:
                entry = self.incoming_files[file_id]
                chunk_idx = int(msg["chunk"])
                entry["chunks"][chunk_idx] = msg["data"]
                total = int(entry["meta"]["chunks"])
                if len(entry["chunks"]) >= total:
                    try:
                        raw_bytes = b"".join(base64.b64decode(entry["chunks"][i]) for i in range(total))
                    except Exception:
                        raw_bytes = b""
                    self.file_data_store[file_id] = raw_bytes
                    meta = entry["meta"]
                    del self.incoming_files[file_id]

                    sender = meta["from"]
                    target_type = meta["target_type"]
                    target = meta["target"]
                    room_key = ("pm", sender) if target_type == "pm" else ("group", target)
                    self.show_file(room_key, sender, meta["filename"], len(raw_bytes), file_id, raw_bytes=raw_bytes)
        elif kind == "timeout_notice":
            g = msg["group"]
            target = msg["target"]
            try:
                secs = int(msg.get("seconds", 0))
            except (ValueError, TypeError):
                secs = 0
            if secs <= 0:
                if g in self.group_timeouts:
                    self.group_timeouts[g].pop(target, None)
                if target == self.name:
                    self.my_timeouts.pop(g, None)
            else:
                exp = time.time() + secs
                self.group_timeouts.setdefault(g, {})[target] = exp
                if target == self.name:
                    self.my_timeouts[g] = exp
            if self.active and self.active == ("group", g):
                allowed = self.can_send(self.active)
                self.entry.config(state="normal" if allowed else "disabled")
                self.send_button.config(state="normal" if allowed else "disabled")
                self.file_button.config(state="normal" if allowed else "disabled")
                if self.is_my_timeout_active(g):
                    rem = max(1, int(self.my_timeouts[g] - time.time()))
                    self.subtitle.config(text=f"⏳ You are timed out in this group ({self.format_remaining(rem)} remaining)")
                elif self.name in self.groups.get(g, []):
                    self.subtitle.config(text="Only members see these messages")
            self.render_members()
        elif kind == "error":
            self.want = None
            self.flash(msg["text"])
        elif kind == "disconnected":
            messagebox.showerror("Disconnected", "Lost connection to the server")
            self.close()


def main(argv: list[str] | None = None) -> None:
    global HOST, PORT
    parser = argparse.ArgumentParser(description="Campfire socket chat client")
    parser.add_argument("host", nargs="?", default="127.0.0.1")
    parser.add_argument("port", nargs="?", type=int, default=5000)
    args = parser.parse_args(argv)
    HOST, PORT = args.host, args.port
    root = tk.Tk()
    ChatClient(root)
    root.mainloop()


if __name__ == "__main__":
    main()
