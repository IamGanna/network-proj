"""Chat UI ("Campfire" dark theme, Tkinter). All networking lives in chat_core.py.

Run: python client.py <server-ip> [port]      e.g. python client.py 192.168.1.20
"""
import ctypes
import argparse
import queue
import threading
import time
import uuid
import tkinter as tk
from tkinter import messagebox

from chat_core import ChatCore
from protocol import validate_server_event, AI_MAX_INSTRUCTION

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
        self.rooms = {}  # ("pm", name) or ("group", name) -> its chat window (R5, R6)
        self.unread = {}
        self.drafts = {}  # exact composer text keyed by room identity
        self.active = None
        self.want = None  # group we created/joined: open it once the server confirms membership
        self.flash_job = None
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
        label(card, "Pull up a seat", 22, bold=True).pack(anchor="w", pady=(px(16), px(4)))
        label(card, "Pick a name to join. Everyone online sees it, so it has to be yours alone.",
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
        self.send_button = button(composer, "Send", self.send_text, primary=True)
        self.send_button.pack(side="right", padx=(px(10), 0))
        ai_row = tk.Frame(self.center, bg=PANEL, padx=px(24), pady=px(6))
        ai_row.pack(side='bottom', fill='x')
        self.summary_button = button(ai_row, 'Summarize', lambda: self.start_ai('summary'), pady=6)
        self.summary_button.pack(side='left')
        self.draft_button = button(ai_row, 'Draft reply', self.draft_reply, pady=6)
        self.draft_button.pack(side='left', padx=px(8))
        self.ai_status = label(ai_row, '', 9, DIM, wraplength=px(260), justify='left')
        self.ai_status.pack(side='left', fill='x', expand=True)
        self.ai_disclosure = label(self.center, 'AI sends this chat’s session messages to OpenRouter and its model provider.',
                                   9, DIM, wraplength=px(500), justify='left')
        self.ai_disclosure.pack(side='bottom', anchor='w', padx=px(24))
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
        box.tag_config(tag, foreground=ACCENT if sender == self.name else color_for(sender), font=(FONT, 11, "bold"))
        box.configure(state="normal")
        box.insert("end", sender, (tag,), "  " + time.strftime("%H:%M") + "\n", "time", text + "\n", "body")
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

    def can_send(self, key):
        if not key:
            return False
        kind, name = key
        return kind == "pm" or (kind == "group" and self.name in self.groups.get(name, []))

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
        if key is None:
            self.title.config(text=f"Welcome, {self.name}")
            self.subtitle.config(text="Groups and people online are on the left.")
            self.empty.tkraise()
        else:
            kind, name = key
            self.title.config(text=("#  " if kind == "group" else "@  ") + name)
            if kind == "group" and not allowed:
                self.subtitle.config(text="Public membership preview · You have not joined")
                self.preview.tkraise()
                self.join_button.pack(anchor="w", pady=(px(6), 0))
            else:
                self.unread.pop(key, None)
                self.open_room(key).tkraise()
                self.subtitle.config(text="Only members see these messages" if kind == "group"
                                     else f"Private: only you and {name} see this")
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
        writing = self.ai_send_worker is not None and self.ai_send_worker.is_alive()
        state = 'normal' if allowed and self.pending_ai is None and not writing and self.instruction_dialog is None else 'disabled'
        self.summary_button.config(state=state)
        self.draft_button.config(state=state)
        self.ai_status.config(text=('Generating ' + ('summary' if self.pending_ai['action']=='summary' else 'reply')
                                   + ' for ' + self.pending_ai['room'][1] + '…') if self.pending_ai else '')

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
        for kind, u in self.rooms:
            if kind == "pm" and u not in self.users:
                self.row(self.user_box, u, lambda k=("pm", u): self.select(k), ("pm", u), "offline", u)
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
        members = self.groups[self.active[1]]
        label(self.members_panel, f"MEMBERS · {len(members)}", 9, MUTED, bold=True).pack(anchor="w", pady=(0, px(8)))
        self.member_list = ScrollList(self.members_panel)
        self.member_list.pack(fill="both", expand=True)
        for m in members:
            r = tk.Frame(self.member_list.content, bg=PANEL, pady=px(5))
            r.pack(fill="x")
            avatar(r, m).pack(side="left", padx=(0, px(10)))
            label(r, m + (" (you)" if m == self.name else ""), 10).pack(side="left")
            self.member_list.bind_controls(r)
        self.members_panel.pack(side="right", fill="y", padx=(px(8), 0), before=self.center)

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
        if self.pending_ai and self.pending_ai['action']=='draft' and self.pending_ai['room']==self.active:
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

    # ---------- network events (queued by ChatCore's thread) ----------
    def poll(self):
        if self.closed:
            return
        try:
            while not self.inbox.empty():
                self.handle(self.inbox.get())
                if self.closed:
                    return
        finally:
            if not self.closed:  # an error still reaches Tk's report; later messages keep arriving
                self.poll_job = self.root.after(100, self.poll)

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
        elif kind == "group_msg":
            if self.can_send(("group", msg["group"])):
                self.show(("group", msg["group"]), msg["from"], msg["text"])
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
