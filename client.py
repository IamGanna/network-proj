"""Chat UI ("Campfire" dark theme, Tkinter). All networking lives in chat_core.py.

Run: python client.py <server-ip> [port]      e.g. python client.py 192.168.1.20
"""
import base64
import ctypes
import argparse
import os
import queue
import threading
import time
import uuid
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

from chat_core import ChatCore
from protocol import validate_server_event, AI_MAX_INSTRUCTION, MAX_SPEECH_BYTES
try:
    import voice
    from PIL import Image, ImageTk
except ImportError as e:  # first run on this PC: install the client packages, then start again
    import subprocess
    import sys
    print(f"Missing package '{e.name}'. Installing requirements.txt (first run only)...")
    requirements = os.path.join(os.path.dirname(os.path.abspath(__file__)), "requirements.txt")
    if subprocess.call([sys.executable, "-m", "pip", "install", "-r", requirements]) != 0:
        raise SystemExit("Could not install the packages. Run: pip install -r requirements.txt")
    raise SystemExit(subprocess.call([sys.executable] + sys.argv))

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


BG, PANEL, CHAT_BG, MEMBER_BG = "#101012", "#151517", "#1B1B1E", "#171719"
RAISED, LINE = "#27272C", "#2B2B30"
TEXT, MUTED, DIM = "#EEEEF0", "#AAAAB5", "#9696A3"
ACCENT, ON_ACCENT, ERROR, ONLINE = "#FD771E", "#281C14", "#FF9B8A", "#78B99C"
FOCUS = "#849FE0"
SELECTED_BG, SELECTED_TEXT, SELECTED_META = "#241E19", "#EB8342", "#B7A08E"
HEADER_HEIGHT = 64
PALETTE = ["#9EB2C8", "#9AB8A4", "#C5A2B6", "#C7B18D", "#AAA1C5", "#8EADB8"]
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
                    disabledbackground=BG, disabledforeground=DIM,
                    highlightthickness=px(1), highlightbackground=LINE, highlightcolor=FOCUS)


def label(parent, text, size=11, fg=TEXT, bold=False, **kw):
    return tk.Label(parent, text=text, bg=parent["bg"], fg=fg,
                    font=(FONT, size, "bold" if bold else "normal"), **kw)


def avatar(parent, name, size=28):
    s = px(size)
    c = tk.Canvas(parent, width=s, height=s, bg=parent["bg"], highlightthickness=0)
    c.create_oval(1, 1, s - 1, s - 1, fill=color_for(name), outline="")
    c.create_text(s / 2, s / 2, text=name[0].upper(), fill="#14161B", font=(FONT, 10, "bold"))
    return c


def pane_header(parent, bg):
    header = tk.Frame(parent, bg=bg, height=px(HEADER_HEIGHT))
    header.pack(fill="x")
    header.pack_propagate(False)
    tk.Frame(header, bg=LINE, height=px(1)).pack(side="bottom", fill="x")
    return header


def workspace_accent(parent):
    width, height = px(46), px(HEADER_HEIGHT)
    strip = tk.Canvas(parent, width=width, height=height, bg=PANEL, highlightthickness=0)
    for x in range(width):
        strength = x / max(1, width - 1)
        rib = 0.82 if (x // max(1, px(3))) % 2 else 1
        r = 21 + round(39 * strength * rib)
        g = 21 + round(19 * strength * rib)
        b = 23 + round(3 * strength * rib)
        strip.create_line(x, 0, x, height, fill=f"#{r:02x}{g:02x}{b:02x}")
    strip.pack(side="right", fill="y")


def campfire_mark(parent):
    tile = tk.Canvas(parent, width=px(34), height=px(34), bg=ACCENT, highlightthickness=0)
    # Draw the brand mark independently of the platform's emoji font.
    flame = (17, 6, 21, 14, 24, 11, 25, 20, 22, 26, 17, 28, 12, 26, 9, 21, 10, 16, 13, 18)
    tile.create_polygon(*(px(value) for value in flame), fill=ON_ACCENT, smooth=True)
    tile.pack(pady=px(14))


class ScrollList(tk.Frame):
    """A bounded list whose wheel and navigation bindings stay local to it."""
    def __init__(self, parent):
        surface = parent["bg"]
        super().__init__(parent, bg=surface)
        self.canvas = tk.Canvas(self, bg=surface, highlightthickness=0, takefocus=1)
        style = ttk.Style(self)
        if style.theme_use() != "clam":
            style.theme_use("clam")
        for axis in ("Vertical", "Horizontal"):
            name = f"Campfire.{axis}.TScrollbar"
            style.configure(name, background="#414147", troughcolor=MEMBER_BG,
                            arrowcolor=MUTED, lightcolor="#414147", darkcolor="#414147",
                            bordercolor=MEMBER_BG, borderwidth=0, arrowsize=px(8))
            style.map(name, background=[("active", "#505057"), ("!active", "#414147")])
        self.vertical = ttk.Scrollbar(self, orient="vertical", command=self.canvas.yview,
                                      style="Campfire.Vertical.TScrollbar")
        horizontal = ttk.Scrollbar(self, orient="horizontal", command=self.canvas.xview,
                                   style="Campfire.Horizontal.TScrollbar")
        self.canvas.configure(
            yscrollcommand=lambda first, last: self._set_scrollbar(self.vertical, first, last, "right", "y"),
            xscrollcommand=lambda first, last: self._set_scrollbar(horizontal, first, last, "bottom", "x"),
        )
        self.canvas.pack(side="left", fill="both", expand=True)
        self.content = tk.Frame(self.canvas, bg=surface)
        self.window = self.canvas.create_window(0, 0, anchor="nw", window=self.content)
        self.content.bind("<Configure>", self._layout)
        self.canvas.bind("<Configure>", self._layout)
        self.bind_controls(self.canvas)
        self.bind_controls(self.content)

    def _set_scrollbar(self, scrollbar, first, last, side, fill):
        scrollbar.set(first, last)
        overflow = float(first) > 0 or float(last) < 1
        if overflow and not scrollbar.winfo_manager():
            scrollbar.pack(side=side, fill=fill, before=self.canvas)
        elif not overflow and scrollbar.winfo_manager():
            scrollbar.pack_forget()

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
        self.group_timeouts = {}  # group -> {member: expiry_timestamp}
        self.my_timeouts = {}     # group -> expiry_timestamp
        self.rooms = {}  # ("pm", name) or ("group", name) -> its chat window (R5, R6)
        self.room_last_msg = {}  # {key: (sender, timestamp)} for grouping
        self.unread = {}
        self.drafts = {}  # exact composer text keyed by room identity
        self.active = None
        self.want = None  # group we created/joined: open it once the server confirms membership
        self.flash_job = None
        self.auto_speak = False
        self.recording = False
        self.stt_id = None  # the recording the server is transcribing for us
        self.sidebar_pending = False
        self.pending_ai = None
        self.ai_dialogs = set()
        self.instruction_dialog = None
        self.ai_send_worker = None
        self.ai_send_results = queue.Queue()
        self.ai_send_job = None
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
        label(card, "Join the workspace", 22, bold=True).pack(anchor="w", pady=(px(16), px(4)))
        label(card, "Choose the display name people will see in chat.",
              10, MUTED, wraplength=px(350), justify="left").pack(anchor="w")
        label(card, f"Server  {HOST}:{PORT}", 9, DIM).pack(anchor="w", pady=(px(16), 0))
        label(card, "Your name", 10, "#C9CDD4", bold=True).pack(anchor="w", pady=(px(12), px(6)))
        self.name_entry = entry(card)
        self.name_entry.pack(fill="x", ipady=px(8))
        self.name_entry.bind("<Return>", self.login)
        self.name_entry.focus_set()
        self.login_error = label(card, "", 10, ERROR, wraplength=px(350), justify="left")
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
        if self.pending_ai is not None:
            timer = self.pending_ai.get('timer')
            if timer is not None:
                self.root.after_cancel(timer)
            self.pending_ai = None
        for dialog in tuple(self.ai_dialogs):
            dialog.destroy()
        self.ai_dialogs.clear()
        if self.ai_send_job is not None:
            self.root.after_cancel(self.ai_send_job)
            self.ai_send_job = None
        for job in (self.login_job, self.poll_job, self.flash_job):
            if job is not None:
                self.root.after_cancel(job)
        self.core.close()
        if self.ai_send_worker is not None:
            self.ai_send_worker.join(timeout=1)
        if self.login_worker is not None:
            self.login_worker.join(timeout=1)
        self.root.destroy()

    # ---------- UI ----------
    def build_ui(self):
        main = tk.Frame(self.root, bg=BG)
        main.pack(fill="both", expand=True)

        # Column 1 — icon bar (48px)
        icon_bar = tk.Frame(main, bg=BG, width=px(48))
        icon_bar.pack(side="left", fill="y")
        icon_bar.pack_propagate(False)
        campfire_mark(icon_bar)

        # Column 2 — sidebar (220px)
        side = tk.Frame(main, bg=PANEL, width=px(220))
        side.pack(side="left", fill="y")
        side.pack_propagate(False)
        tk.Frame(main, bg=LINE, width=px(1)).pack(side="left", fill="y")

        # Sidebar header
        hdr = pane_header(side, PANEL)
        workspace_accent(hdr)
        identity = tk.Frame(hdr, bg=PANEL)
        identity.pack(side="left", padx=(px(12), 0))
        label(identity, "Campfire", 13, bold=True).pack(anchor="w")
        self.workspace_sub = label(identity, "Network Project · 0 online", 9, MUTED)
        self.workspace_sub.pack(anchor="w")

        # Sidebar lists
        lists = tk.Frame(side, bg=PANEL)
        lists.pack(fill="both", expand=True)
        lists.grid_rowconfigure(1, weight=1, uniform="lists")
        lists.grid_rowconfigure(3, weight=1, uniform="lists")
        lists.grid_columnconfigure(0, weight=1)

        grp_head = tk.Frame(lists, bg=PANEL)
        grp_head.grid(row=0, column=0, sticky="ew", padx=px(12), pady=(px(16), px(4)))
        label(grp_head, "GROUPS", 9, MUTED, bold=True).pack(side="left")
        button(grp_head, "+", self.create_group, bg=PANEL, padx=8, pady=0).pack(side="right")
        self.group_list = ScrollList(lists)
        self.group_list.grid(row=1, column=0, sticky="nsew")
        self.group_box = self.group_list.content

        self.dm_label = label(lists, "DIRECT MESSAGES · 0 online", 9, MUTED, bold=True)
        self.dm_label.grid(row=2, column=0, sticky="w", padx=px(12), pady=(px(16), px(4)))
        self.user_list = ScrollList(lists)
        self.user_list.grid(row=3, column=0, sticky="nsew")
        self.user_box = self.user_list.content

        # Sidebar me-strip (bottom)
        me = tk.Frame(side, bg=BG, padx=px(10), pady=px(8))
        me.pack(side="bottom", fill="x")
        tk.Frame(side, bg=LINE, height=px(1)).pack(side="bottom", fill="x")
        avatar(me, self.name, 28).pack(side="left")
        info = tk.Frame(me, bg=BG)
        info.pack(side="left", padx=px(8))
        label(info, self.name, 10, bold=True).pack(anchor="w")
        label(info, "● Online", 9, ONLINE).pack(anchor="w")

        # Column 3 — chat area (flex)
        self.center = tk.Frame(main, bg=CHAT_BG)
        self.center.pack(side="left", fill="both", expand=True)
        tk.Frame(main, bg=LINE, width=px(1)).pack(side="left", fill="y")

        # Column 4 — members panel (200px, always packed)
        self.members_panel = tk.Frame(main, bg=MEMBER_BG, width=px(200))
        self.members_panel.pack(side="left", fill="y")
        self.members_panel.pack_propagate(False)

        # Chat header
        chat_hdr = pane_header(self.center, CHAT_BG)
        name_row = tk.Frame(chat_hdr, bg=CHAT_BG)
        name_row.pack(fill="x", expand=True, padx=px(16))
        self.hash_label = label(name_row, "", 15, MUTED)
        self.hash_label.pack(side="left")
        self.title = label(name_row, f"Welcome, {self.name}", 13, bold=True, anchor="w")
        self.title.pack(side="left", fill="x", expand=True)

        # Persistent error/offline bars
        self.error_bar = tk.Label(self.center, bg="#3A2320", fg=ERROR, font=(FONT, 10),
                                  padx=px(16), pady=px(6), anchor="w")
        self.offline_bar = tk.Label(self.center, bg=RAISED, fg=MUTED, font=(FONT, 9),
                                    padx=px(16), pady=px(5), anchor="w")

        # Composer (bottom)
        composer = tk.Frame(self.center, bg=CHAT_BG, padx=px(16), pady=px(12))
        composer.pack(side="bottom", fill="x")
        tk.Frame(self.center, bg=LINE, height=px(1)).pack(side="bottom", fill="x")
        self.group_hint = tk.Label(self.center, bg=CHAT_BG, fg=DIM, font=(FONT, 9),
                                    padx=px(24), pady=px(4), anchor="w",
                                    text="Only group members can view this channel.")
        self.entry = entry(composer)
        self.entry.config(width=1)
        self.entry.pack(side="left", fill="x", expand=True, ipady=px(8))
        self.entry.bind("<Return>", self.send_text)
        self._entry_has_placeholder = False
        self._entry_placeholder_text = ""
        self.entry.bind("<FocusIn>", self._entry_focus_in, add="+")
        self.entry.bind("<FocusOut>", self._entry_focus_out, add="+")

        self.send_button = button(composer, "Send", self.send_text)
        self.send_button.pack(side="right", padx=(px(8), 0))
        self.composer_text = tk.StringVar(master=composer)
        self.entry.config(textvariable=self.composer_text)
        self.composer_text.trace_add("write", self._style_send)
        self.ai_btn = button(composer, "AI", self._toggle_ai_menu, bg=RAISED, padx=10, pady=8)
        self.ai_btn.pack(side="right", padx=(px(4), 0))
        self.file_button = button(composer, "📎", self.choose_file, bg=RAISED, padx=10, pady=8)
        self.file_button.pack(side="right", padx=(px(4), 0))
        self.speak_btn = button(composer, "🔊", self._toggle_speak, bg=RAISED, padx=10, pady=8)
        self.speak_btn.pack(side="right", padx=(px(4), 0))
        self.mic_btn = button(composer, "🎤", self._toggle_mic, bg=RAISED, padx=10, pady=8)
        self.mic_btn.pack(side="right", padx=(px(4), 0))

        # AI popover (packed above composer, hidden by default)
        self._ai_popover = tk.Frame(self.center, bg=RAISED, padx=px(12), pady=px(10))
        self.summary_button = button(self._ai_popover, "📋 Summarize",
                                     lambda: self.start_ai("summary"), pady=6)
        self.summary_button.pack(anchor="w")
        self.draft_button = button(self._ai_popover, "✏️ Auto-generate reply",
                                   self.draft_reply, pady=6)
        self.draft_button.pack(anchor="w", pady=(px(4), 0))
        self.ai_status = label(self._ai_popover, "", 9, DIM, wraplength=px(260), justify="left")
        self.ai_status.pack(anchor="w", pady=(px(4), 0))
        self.ai_disclosure = label(self._ai_popover,
                                   "AI sends this chat’s session messages to OpenRouter and its model provider.",
                                   9, DIM, wraplength=px(260), justify="left")
        self.ai_disclosure.pack(anchor="w", pady=(px(2), 0))
        self._ai_popover_visible = False

        # Stage (chat messages area)
        self.stage = tk.Frame(self.center, bg=CHAT_BG)
        self.stage.pack(fill="both", expand=True)
        self.empty = label(self.stage, "Pick a group or someone to start chatting.", 11, MUTED)
        self.empty.place(relwidth=1, relheight=1)

        # Preview card (shown for non-member group view)
        self.preview = tk.Frame(self.stage, bg=CHAT_BG)
        self.preview.place(relwidth=1, relheight=1)
        preview_card = tk.Frame(self.preview, bg=RAISED, padx=px(24), pady=px(20))
        preview_card.place(relx=0.5, rely=0.4, anchor="center")
        label(preview_card, "Public preview", 13, bold=True).pack(anchor="w")
        label(preview_card, "You have not joined this group.", 10, MUTED).pack(anchor="w", pady=(px(4), px(12)))
        self.join_button = button(preview_card, "Join group", self.join_selected, primary=True)
        self.join_button.pack(anchor="w")

        self.select(None)

    def open_room(self, key):
        if key not in self.rooms:
            box = tk.Text(self.stage, bg=CHAT_BG, fg=TEXT, relief="flat", wrap="word",
                          padx=px(20), pady=px(16), font=(FONT, 11), state="disabled",
                          cursor="arrow", highlightthickness=0)
            box.tag_config("time", foreground=DIM, font=(FONT, 9))
            box.tag_config("body", foreground=TEXT, spacing1=px(2),
                           lmargin1=px(48), lmargin2=px(48))
            box.tag_config("grouped", foreground=TEXT,
                           lmargin1=px(48), lmargin2=px(48))
            box.tag_config("datesep", justify="center", foreground=DIM, font=(FONT, 9),
                           spacing1=px(8), spacing3=px(4))
            import time as _t
            _d = _t.localtime()
            _date = _t.strftime(f"%B {_d.tm_mday}, %Y")
            box.configure(state="normal")
            box.insert("end", f"────────  {_date}  ────────\n", "datesep")
            box.configure(state="disabled")
            box.place(relwidth=1, relheight=1)
            self.rooms[key] = box
            visible = (self.preview if self.active and self.active[0] == "group"
                       and not self.can_send(self.active) else self.rooms.get(self.active, self.empty))
            visible.tkraise()
        return self.rooms[key]

    def show(self, key, sender, text):
        box = self.open_room(key)
        now = time.time()
        last_sender, last_time = self.room_last_msg.get(key, (None, 0))
        grouped = (sender == last_sender and now - last_time < 300)
        self.room_last_msg[key] = (sender, now)

        box.configure(state="normal")
        if grouped:
            box.insert("end", text + "\n", "grouped")
        else:
            if sender == "[System]":
                fg = "#FF9B8A"
            elif sender == self.name:
                fg = ACCENT
            else:
                fg = TEXT
            name_tag = "name:" + sender
            box.tag_config(name_tag, foreground=fg, font=(FONT, 11, "bold"), spacing1=px(14))
            av = avatar(box, sender, 36)
            box.window_create("end", window=av)
            box.insert("end", " " + sender, (name_tag,),
                       "  " + time.strftime("%H:%M") + "\n", "time",
                       text + "\n", "body")
        box.configure(state="disabled")
        box.see("end")
        if key != self.active:
            self.unread[key] = self.unread.get(key, 0) + 1
            if not self.sidebar_pending:
                self.sidebar_pending = True
                self.root.after_idle(self.flush_sidebar)

    def flush_sidebar(self):
        self.sidebar_pending = False
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
            return name in self.users  # offline users can't receive
        if kind == "group":
            if self.name not in self.groups.get(name, []):
                return False
            if self.is_my_timeout_active(name):
                return False
            return True
        return False

    def _entry_focus_in(self, _event=None):
        if getattr(self, '_entry_has_placeholder', False):
            self.entry.delete(0, "end")
            self.entry.config(fg=TEXT)
            self._entry_has_placeholder = False
            self._style_send()

    def _entry_focus_out(self, _event=None):
        if not getattr(self, '_entry_has_placeholder', False) and not self.entry.get():
            pt = getattr(self, '_entry_placeholder_text', '')
            if pt and self.entry["state"] == "normal":
                self.entry.insert(0, pt)
                self.entry.config(fg=DIM)
                self._entry_has_placeholder = True
                self._style_send()

    def _style_send(self, *_):
        ready = (self.send_button["state"] == "normal"
                 and not self._entry_has_placeholder and bool(self.composer_text.get().strip()))
        self.send_button.config(bg=ACCENT if ready else RAISED,
                                fg=ON_ACCENT if ready else MUTED,
                                activebackground="#FF934D" if ready else LINE,
                                activeforeground=ON_ACCENT if ready else TEXT,
                                disabledforeground=DIM)

    def select(self, key):
        if key and key[0] == "group" and key[1] not in self.groups:
            key = None
        if self.active is not None:
            self.drafts[self.active] = ("" if getattr(self, '_entry_has_placeholder', False)
                                        else self.entry.get())
        # Clear placeholder state before modifying entry
        if getattr(self, '_entry_has_placeholder', False):
            self._entry_has_placeholder = False
            self.entry.config(fg=TEXT)
        self.active = key
        allowed = self.can_send(key)

        self.entry.config(state="normal")
        self.entry.delete(0, "end")
        draft = self.drafts.get(key, "") if key else ""
        if draft:
            self.entry.insert(0, draft)
        self.entry.config(state="normal" if allowed else "disabled")
        self.send_button.config(state="normal" if allowed else "disabled")

        # Placeholder
        self._entry_placeholder_text = ""
        if allowed and not draft and key:
            self._entry_placeholder_text = (f"Message #{key[1]}" if key[0] == "group"
                                             else f"Message {key[1]}")
            if self.root.focus_get() is not self.entry:
                self.entry.insert(0, self._entry_placeholder_text)
                self.entry.config(fg=DIM)
                self._entry_has_placeholder = True

        # Group members-only hint
        if key and key[0] == "group" and self.name in self.groups.get(key[1], []):
            self.group_hint.pack(side="bottom", fill="x")
        else:
            self.group_hint.pack_forget()

        # offline notice bar
        if key and key[0] == "pm" and not allowed:
            self.offline_bar.config(text=f"{key[1]} is offline. You can still read this conversation.")
            self.offline_bar.pack(fill="x", before=self.stage)
        else:
            self.offline_bar.pack_forget()

        if key is None:
            self.hash_label.config(text="")
            self.title.config(text=f"Welcome, {self.name}")
            self.empty.tkraise()
        else:
            kind, name = key
            self.hash_label.config(text="# " if kind == "group" else "")
            self.title.config(text=name)
            if kind == "group" and not allowed:
                if self.name not in self.groups.get(name, []):
                    self.preview.tkraise()
                else:
                    self.unread.pop(key, None)
                    self.open_room(key).tkraise()
            else:
                self.unread.pop(key, None)
                self.open_room(key).tkraise()
                self.entry.focus_set()

        self.render_sidebar()
        self.render_members()
        self.refresh_ai_controls()

    def refresh_ai_controls(self):
        allowed = self.can_send(self.active)
        frozen = bool(self.pending_ai and self.pending_ai['action'] == 'draft'
                      and self.pending_ai['room'] == self.active)
        self.entry.config(state='normal' if allowed and not frozen else 'disabled')
        self.send_button.config(state='normal' if allowed and not frozen else 'disabled')
        self._style_send()
        self.file_button.config(state='normal' if allowed else 'disabled')
        writing = self.ai_send_worker is not None and self.ai_send_worker.is_alive()
        state = 'normal' if allowed and self.pending_ai is None and not writing and self.instruction_dialog is None else 'disabled'
        self.summary_button.config(state=state)
        self.draft_button.config(state=state)
        self.ai_status.config(text=('Generating ' + ('summary' if self.pending_ai['action']=='summary' else 'reply')
                                   + ' for ' + self.pending_ai['room'][1] + '…') if self.pending_ai else '')
        if self.pending_ai is not None and self._ai_popover_visible:
            self._toggle_ai_menu()

    def _toggle_ai_menu(self):
        if self._ai_popover_visible:
            self._ai_popover.pack_forget()
            self._ai_popover_visible = False
        else:
            self._update_ai_hint()
            self._ai_popover.pack(side="bottom", fill="x", before=self.stage)
            self._ai_popover_visible = True

    def _update_ai_hint(self):
        if self.entry.get():
            self.draft_button.config(text="✏️ Rewrite my message")
        else:
            self.draft_button.config(text="✏️ Auto-generate reply")

    def start_ai(self, action, instruction='', *, room=None):
        room = self.active if room is None else room
        if self.closed or self.pending_ai is not None or not self.can_send(room):
            return False
        if self.instruction_dialog is not None:
            self.flash('Finish or cancel the draft instruction first.')
            return False
        if self.ai_send_worker is not None and self.ai_send_worker.is_alive():
            self.flash('An AI request is still being sent. Try again shortly.')
            return False
        if action not in ('summary', 'draft') or len(instruction) > AI_MAX_INSTRUCTION:
            self.flash('AI instruction must be at most 500 characters.')
            return False
        original = self.entry.get() if room == self.active else self.drafts.get(room, '')
        self.drafts[room] = original
        pending = dict(request_id=uuid.uuid4().hex, room=room, action=action,
                       original_draft=original, timer=None)
        self.pending_ai = pending
        self.refresh_ai_controls()
        pending['timer'] = self.root.after(35000, lambda: self.finish_ai_error('AI timed out. Your draft was kept.'))
        def write_request():
            try:
                sent = self.core.request_ai(pending['request_id'], action, room[0], room[1], instruction)
            except Exception:
                sent = False
            self.ai_send_results.put((pending['request_id'], sent))
        self.ai_send_worker = threading.Thread(target=write_request, daemon=True, name='ai-request-write')
        self.ai_send_worker.start()
        if self.ai_send_job is None:
            self.ai_send_job = self.root.after(25, self._poll_ai_send)
        return True

    def _poll_ai_send(self):
        self.ai_send_job = None
        if self.closed:
            return
        while not self.ai_send_results.empty():
            rid, sent = self.ai_send_results.get_nowait()
            if not sent and self.pending_ai and self.pending_ai['request_id'] == rid:
                self.finish_ai_error('Could not request AI. Your draft was kept.')
        if self.ai_send_worker is not None and not self.ai_send_worker.is_alive():
            self.ai_send_worker = None
        self.refresh_ai_controls()
        if self.ai_send_worker is not None:
            self.ai_send_job = self.root.after(25, self._poll_ai_send)

    def _clear_ai(self):
        pending, self.pending_ai = self.pending_ai, None
        if pending and pending['timer'] is not None:
            self.root.after_cancel(pending['timer'])
        return pending

    def _replace_draft(self, room, text):
        self.drafts[room] = text
        if self.active == room:
            self.entry.config(state='normal')
            self.entry.delete(0, 'end')
            self.entry.insert(0, text)
            self.entry.icursor('end')

    def finish_ai_error(self, text):
        pending = self._clear_ai()
        if pending and pending['action'] == 'draft':
            self._replace_draft(pending['room'], pending['original_draft'])
        self.refresh_ai_controls()
        self.flash(text)

    def handle_ai(self, msg):
        pending = self.pending_ai
        if not pending or any((msg['request_id'] != pending['request_id'], msg['action'] != pending['action'],
                               (msg['room_type'],msg['room']) != pending['room'])):
            return
        if msg['type'] == 'ai_error':
            self.finish_ai_error(msg['text'])
            return
        if not self.can_send(pending['room']):
            self.finish_ai_error('You no longer have access to that chat.')
            return
        self._clear_ai()
        if pending['action'] == 'draft':
            self._replace_draft(pending['room'], msg['text'])
        else:
            self.show_summary(msg)
        self.refresh_ai_controls()

    def _ai_dialog(self, title):
        dialog = tk.Toplevel(self.root, bg=PANEL, padx=px(20), pady=px(16))
        dialog.title(title)
        dialog.transient(self.root)
        self.ai_dialogs.add(dialog)
        def dismiss():
            self.ai_dialogs.discard(dialog)
            if self.instruction_dialog is dialog:
                self.instruction_dialog = None
            dialog.destroy()
            if not self.closed:
                self.refresh_ai_controls()
                self.entry.focus_set()
        dialog.protocol('WM_DELETE_WINDOW', dismiss)
        dialog.bind('<Escape>', lambda _event: dismiss())
        return dialog, dismiss

    def draft_reply(self):
        if self.closed or self.pending_ai or not self.can_send(self.active):
            return
        if self.instruction_dialog is not None:
            self.instruction_dialog.lift()
            return
        room = self.active  # Generate must still target this room after switching.
        dialog, dismiss = self._ai_dialog('Draft reply · ' + room[1])
        self.instruction_dialog = dialog
        label(dialog, 'Optional instruction', 13, bold=True).pack(anchor='w')
        label(dialog, 'Keep blank to use the conversation. Replaces your draft; you press Send.',
              10, MUTED, wraplength=px(380), justify='left').pack(anchor='w', pady=px(8))
        instruction = entry(dialog)
        instruction.pack(fill='x', ipady=px(8))
        instruction.focus_set()
        def generate(_event=None):
            text = instruction.get()
            if len(text) > AI_MAX_INSTRUCTION:
                self.flash('AI instruction must be at most 500 characters.')
                return
            dismiss()
            self.start_ai('draft', text, room=room)
        controls = tk.Frame(dialog, bg=PANEL)
        controls.pack(fill='x', pady=(px(12),0))
        self.instruction_generate = button(controls, 'Generate', generate, primary=True)
        self.instruction_generate.pack(side='right')
        button(controls, 'Cancel', dismiss).pack(side='right', padx=px(8))
        instruction.bind('<Return>', generate)
        self.refresh_ai_controls()

    def show_summary(self, msg):
        dialog, dismiss = self._ai_dialog('Summary · ' + msg['room'])
        dialog.geometry(f'{px(600)}x{px(430)}')
        label(dialog, msg['room_type'].upper() + ' · ' + msg['room'], 13, bold=True).pack(anchor='w')
        label(dialog, f"Private to you · {msg['message_count']} messages · through session message #{msg['cutoff']}",
              9, DIM).pack(anchor='w', pady=px(8))
        button(dialog, 'Close', dismiss).pack(side='bottom', anchor='e', pady=(px(10),0))
        area = tk.Frame(dialog, bg=PANEL)
        area.pack(fill='both', expand=True)
        scrollbar = tk.Scrollbar(area)
        scrollbar.pack(side='right', fill='y')
        box = tk.Text(area, bg=BG, fg=TEXT, font=(FONT,11), wrap='word',
                      relief='flat', padx=px(12), pady=px(12), yscrollcommand=scrollbar.set)
        box.pack(fill='both', expand=True)
        scrollbar.config(command=box.yview)
        box.insert('1.0', msg['text'])
        box.config(state='disabled')
        box.focus_set()

    def join_selected(self):
        if self.active and self.active[0] == "group":
            self.join_group(self.active[1])

    def row(self, parent, text, command, key=None, badge="", person=None):
        n = self.unread.get(key)
        selected = key is not None and key == self.active
        bg = SELECTED_BG if selected else PANEL
        row = tk.Frame(parent, bg=bg, padx=px(8), pady=px(4))
        row.pack(fill="x", pady=px(1))
        if person:
            avatar(row, person).pack(side="left", padx=(0, px(4)))
        if n:
            tk.Label(row, text=f" {n} ", bg=ACCENT, fg=ON_ACCENT, font=(FONT, 9, "bold")).pack(side="right")
        elif badge:
            label(row, badge, 9, SELECTED_META if selected else DIM).pack(side="right")
        tk.Button(row, text=text, command=command, anchor="w", relief="flat", bd=0, cursor="hand2",
                  bg=bg, fg=SELECTED_TEXT if selected else TEXT if n else MUTED,
                  activebackground=bg if selected else LINE, activeforeground=SELECTED_TEXT if selected else TEXT,
                  font=(FONT, 11, "bold" if n else "normal"), padx=px(6), pady=px(4)).pack(side="left", fill="x", expand=True)

    def _dm_row(self, parent, name, key):
        selected = key == self.active
        bg = SELECTED_BG if selected else PANEL
        row = tk.Frame(parent, bg=bg, padx=px(8), pady=px(4))
        row.pack(fill="x", pady=px(1))
        avatar(row, name, 26).pack(side="left", padx=(0, px(6)))
        online = name in self.users
        dot = tk.Canvas(row, width=px(8), height=px(8), bg=bg, highlightthickness=0)
        dot.create_oval(0, 0, px(8), px(8), fill=ONLINE if online else DIM, outline="")
        dot.pack(side="left", padx=(0, px(6)))
        n = self.unread.get(key)
        if n:
            tk.Label(row, text=f" {n} ", bg=ACCENT, fg=ON_ACCENT,
                     font=(FONT, 9, "bold")).pack(side="right")
        display = name + (" (you)" if name == self.name else "")
        color = SELECTED_TEXT if selected else TEXT if n else MUTED if online else DIM
        tk.Button(row, text=display, command=lambda: self.select(key),
                  anchor="w", relief="flat", bd=0, cursor="hand2",
                  bg=bg, fg=color, activebackground=bg if selected else LINE,
                  activeforeground=SELECTED_TEXT if selected else TEXT,
                  font=(FONT, 11, "bold" if n else "normal"),
                  padx=px(4), pady=px(3)).pack(side="left", fill="x", expand=True)
        return row

    def render_sidebar(self):
        for box in (self.group_box, self.user_box):
            for w in box.winfo_children():
                w.destroy()
        for g, members in self.groups.items():
            key = ("group", g)
            badge = str(len(members)) if self.name in members else "Preview"
            self.row(self.group_box, "#  " + g, lambda k=key: self.select(k), key, badge)
        if not self.groups:
            label(self.group_box, "No groups yet. Press + to start one.", 9, DIM).pack(anchor="w", padx=px(6))

        online_count = len([u for u in self.users if u != self.name])
        self.dm_label.config(text=f"DIRECT MESSAGES · {online_count} online")
        self.workspace_sub.config(text=f"Network Project · {len(self.users)} online")

        seen = set()
        for u in self.users:
            key = ("pm", u)
            seen.add(u)
            self._dm_row(self.user_box, u, key)
        for kind, u in list(self.rooms):
            if kind == "pm" and u not in seen:
                self._dm_row(self.user_box, u, ("pm", u))
        if not self.users:
            label(self.user_box, "No one is online yet.", 9, DIM).pack(anchor="w", padx=px(6))

        for area in (self.group_list, self.user_list):
            for child in area.content.winfo_children():
                area.bind_controls(child)

    def _members_heading(self, text):
        heading = label(self.members_panel, text, 10, TEXT, bold=True, anchor="w", padx=px(14))
        padding = max(0, px(HEADER_HEIGHT) - px(1) - heading.winfo_reqheight())
        heading.pack(fill="x", pady=(padding // 2, padding - padding // 2))
        tk.Frame(self.members_panel, bg=LINE, height=px(1)).pack(fill="x")

    def render_members(self):
        for w in self.members_panel.winfo_children():
            w.destroy()

        if not self.active:
            self._members_heading("MEMBERS")
            label(self.members_panel, f"{len(self.users)} online", 9, DIM).pack(
                anchor="w", padx=px(14), pady=px(10))
            return

        kind, name = self.active

        if kind == "pm":
            self._members_heading("CONVERSATION")
            r = tk.Frame(self.members_panel, bg=MEMBER_BG, padx=px(12), pady=px(12))
            r.pack(fill="x")
            avatar(r, name, 28).pack(side="left", padx=(0, px(8)))
            col = tk.Frame(r, bg=MEMBER_BG)
            col.pack(side="left")
            label(col, name, 10, bold=True).pack(anchor="w")
            online = name in self.users
            label(col, "● Online" if online else "● Offline", 9,
                  ONLINE if online else DIM).pack(anchor="w")
            return

        # group state
        if name not in self.groups:
            return
        g = name
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

        self._members_heading(f"MEMBERS — {len(members)}")
        self.member_list = ScrollList(self.members_panel)
        self.member_list.pack(fill="both", expand=True)
        for m in members:
            r = tk.Frame(self.member_list.content, bg=MEMBER_BG, padx=px(8), pady=px(5))
            r.pack(fill="x")
            avatar(r, m, 26).pack(side="left", padx=(0, px(4)))
            online = m in self.users
            dot = tk.Canvas(r, width=px(8), height=px(8), bg=MEMBER_BG, highlightthickness=0)
            dot.create_oval(0, 0, px(8), px(8), fill=ONLINE if online else DIM, outline="")
            dot.pack(side="left", padx=(0, px(6)))
            # Right badges (pack before col so they claim space first)
            is_timed_out = (m in timeouts and now < timeouts[m])
            if is_owner and m != self.name:
                btn_txt = "Manage" if is_timed_out else "Timeout"
                btn = tk.Button(r, text=btn_txt,
                                command=lambda target=m: self.open_timeout_dialog(target),
                                relief="flat", bd=0, cursor="hand2", bg=RAISED, fg=TEXT,
                                activebackground=LINE, activeforeground=TEXT,
                                font=(FONT, 8, "bold"), padx=px(6), pady=px(2))
                btn.pack(side="right")
            if is_timed_out:
                tk.Label(r, text=" Timed out ", bg="#3A2320", fg=ERROR,
                         font=(FONT, 8, "bold")).pack(side="right", padx=(px(4), 0))
            if m == self.name:
                tk.Label(r, text="YOU", bg=MEMBER_BG, fg=DIM,
                         font=(FONT, 9)).pack(side="right", padx=(px(4), 0))
            elif m == owner:
                tk.Label(r, text=" Owner ", bg="#2E2818", fg="#D7C27A",
                         font=(FONT, 8, "bold")).pack(side="right", padx=(px(4), 0))
            col = tk.Frame(r, bg=MEMBER_BG)
            col.pack(side="left", fill="x", expand=True)
            label(col, m, 10, bold=True).pack(anchor="w")
            label(col, "Online" if online else "Offline", 9,
                  ONLINE if online else DIM).pack(anchor="w")
            self.member_list.bind_controls(r)

        # About section (members only)
        if self.name in members:
            tk.Frame(self.members_panel, bg=LINE, height=px(1)).pack(fill="x", padx=px(8), pady=(px(8), 0))
            about = tk.Frame(self.members_panel, bg=MEMBER_BG, padx=px(12), pady=px(8))
            about.pack(fill="x")
            label(about, "About this group", 9, DIM, bold=True).pack(anchor="w")
            label(about, f"#{g}", 10).pack(anchor="w", pady=(px(4), 0))
            mem_row = tk.Frame(about, bg=MEMBER_BG)
            mem_row.pack(fill="x", pady=(px(6), 0))
            label(mem_row, "Your membership", 9, MUTED).pack(side="left")
            label(mem_row, "Joined", 9, ONLINE).pack(side="right")

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

    def _toggle_mic(self):
        if not self.recording:
            self.recording = True
            self.mic_btn.config(text="🎤", bg=ERROR)
            voice.start_recording()
            return
        self.recording = False
        self.mic_btn.config(bg=RAISED)
        wav = voice.stop_recording()
        if not wav:
            return
        if len(wav) > MAX_SPEECH_BYTES:
            self.flash("Recording is longer than 60 seconds. Try a shorter one.")
            return
        stt_id = self.stt_id = uuid.uuid4().hex
        self.mic_btn.config(text="⏳")

        def upload():  # the server transcribes it and answers with stt_text or stt_error
            if not self.core.send_speech(stt_id, wav):
                self.inbox.put({"type": "stt_error", "stt_id": stt_id, "text": "Could not send the recording"})
        threading.Thread(target=upload, daemon=True).start()

    def _toggle_speak(self):
        self.auto_speak = not self.auto_speak
        self.speak_btn.config(bg=ACCENT if self.auto_speak else RAISED,
                              fg=ON_ACCENT if self.auto_speak else TEXT)
        if not self.auto_speak:
            voice.stop()

    def send_text(self, _event=None):
        if self.pending_ai and self.pending_ai['action']=='draft' and self.pending_ai['room']==self.active:
            return
        if getattr(self, '_entry_has_placeholder', False):
            return
        text = self.entry.get().strip()
        if not text or not self.can_send(self.active):
            return
        kind, name = self.active
        if kind == "pm" and name not in self.users:
            self.flash(f"{name} is offline")
            return
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
        now = time.time()
        last_sender, last_time = self.room_last_msg.get(key, (None, 0))
        grouped = (sender == last_sender and now - last_time < 300)
        self.room_last_msg[key] = (sender, now)

        box.configure(state="normal")
        if not grouped:
            fg = ACCENT if sender == self.name else color_for(sender)
            name_tag = "name:" + sender
            box.tag_config(name_tag, foreground=fg, font=(FONT, 11, "bold"), spacing1=px(14))
            av = avatar(box, sender, 36)
            box.window_create("end", window=av)
            box.insert("end", " " + sender, (name_tag,),
                       "  " + time.strftime("%H:%M") + "\n", "time")

        card = tk.Frame(box, bg="#1B1F27", padx=px(14), pady=px(10),
                        highlightthickness=px(1), highlightbackground=LINE)

        is_image = filename.lower().endswith((".png", ".gif", ".jpg", ".jpeg", ".webp", ".bmp"))
        if is_image and raw_bytes:
            img = self.create_image_thumbnail(filename, raw_bytes)
            if img:
                self.file_photos.append(img)
                tk.Label(card, image=img, bg="#1B1F27").pack(anchor="w", pady=(0, px(8)))

        info_row = tk.Frame(card, bg="#1B1F27")
        info_row.pack(fill="x")
        icon = "🖼️ " if is_image else "📄 "
        label(info_row, icon + filename, 10, bold=True).pack(side="left")
        label(info_row, f" ({self.format_bytes(filesize)})", 9, MUTED).pack(side="left")

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
            button(card, "💾 Save file", save, bg=RAISED, padx=8, pady=4).pack(
                anchor="w", pady=(px(8), 0))

        box.window_create("end", window=card)
        box.insert("end", "\n\n", "grouped" if grouped else ())
        box.configure(state="disabled")
        box.see("end")
        if key != self.active:
            self.unread[key] = self.unread.get(key, 0) + 1
            self.render_sidebar()

    # ---------- network events (queued by ChatCore's thread) ----------
    def poll(self):
        if self.closed:
            return
        try:
            while not self.inbox.empty():
                self.handle(self.inbox.get())
                if self.closed:
                    return
            self.check_timeout_countdown()
        finally:
            if not self.closed:  # an error still reaches Tk's report; later messages keep arriving
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
            self.refresh_ai_controls()
            if self.can_send(self.active):
                self.entry.focus_set()

    def handle(self, msg):
        try:
            msg = validate_server_event(msg)
        except ValueError as exc:
            self.flash(f"Invalid server event: {exc}")
            return
        kind = msg["type"]
        if kind in ('ai_result', 'ai_error'):
            self.handle_ai(msg)
        elif kind == "users":  # R4
            self.users = msg["users"]
            self.render_sidebar()
        elif kind == "groups":  # R9
            could_send = self.can_send(self.active)
            self.groups = msg["groups"]
            if self.pending_ai and self.pending_ai['room'][0]=='group' and not self.can_send(self.pending_ai['room']):
                self.finish_ai_error('You no longer have access to that group.')
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
                elif self.active and self.active[0] == "group" and could_send != self.can_send(self.active):
                    self.select(self.active)
                else:
                    self.render_sidebar()
                    self.render_members()
                    self.refresh_ai_controls()
        elif kind == "pm":
            other = msg["to"] if msg["from"] == self.name else msg["from"]
            self.show(("pm", other), msg["from"], msg["text"])
            if self.auto_speak and msg["from"] != self.name:
                voice.speak(msg["text"])
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
                self.refresh_ai_controls()
            self.render_members()
        elif kind in ("stt_text", "stt_error"):
            if msg["stt_id"] != self.stt_id:
                return  # an older recording
            self.stt_id = None
            self.mic_btn.config(text="🎤")
            if kind == "stt_error":
                self.flash(f"STT error: {msg['text']}")
            elif msg["text"] and self.entry["state"] == "normal":
                self.entry.insert("end", (" " + msg["text"]) if self.entry.get() else msg["text"])
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
