import tkinter as tk
import pytest
from unittest.mock import MagicMock, patch
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

# One Tk root for all layout tests — avoids repeated create/destroy Tcl overhead
_TK_ROOT: tk.Tk | None = None


def _root():
    global _TK_ROOT
    if _TK_ROOT is None:
        _TK_ROOT = tk.Tk()
        _TK_ROOT.withdraw()
    return _TK_ROOT


def make_client(name="Tara"):
    """Minimal client fixture — connects fake socket, skips network."""
    with patch("client.ChatCore") as MockCore:
        mock_core = MagicMock()
        MockCore.return_value = mock_core
        import client as C
        top = tk.Toplevel(_root())
        top.withdraw()
        c = C.ChatClient.__new__(C.ChatClient)
        c.root = top
        c.name = name
        c.groups = {}
        c.users = []
        c.rooms = {}
        c.unread = {}
        c.drafts = {}
        c.active = None
        c.pending_ai = None
        c.ai_send_worker = None
        c.instruction_dialog = None
        c.my_timeouts = {}
        c.group_timeouts = {}
        c.want = None
        c.flash_job = None
        c.sidebar_pending = False
        c.file_data_store = {}
        c.file_photos = []
        c.room_last_msg = {}
        c.core = mock_core
        c._ai_popover_visible = False
        c.build_ui()
        try:
            yield c, top
        finally:
            top.destroy()


# ---- Task 1 tests ----

def test_members_panel_always_packed(tmp_path):
    """members_panel must be packed in the 4-column layout on startup."""
    for c, root in make_client():
        assert c.members_panel.winfo_manager() != "", "members_panel must be packed from startup"
        break


def test_no_subtitle_attribute(tmp_path):
    """self.subtitle must not exist in new design."""
    for c, root in make_client():
        assert not hasattr(c, "subtitle"), "subtitle was removed"
        break


# ---- Task 2 tests ----

def test_offline_dm_disables_composer(tmp_path):
    """Composer entry must be disabled when DM target is offline."""
    for c, root in make_client("Tara"):
        c.users = ["Noah"]
        c.rooms[("pm", "Maya")] = MagicMock()
        c.select(("pm", "Maya"))
        assert c.entry["state"] == "disabled"
        assert c.offline_bar.winfo_manager() != ""
        break


def test_online_dm_enables_composer(tmp_path):
    """Composer entry must be enabled when DM target is online."""
    for c, root in make_client("Tara"):
        c.users = ["Noah"]
        c.select(("pm", "Noah"))
        assert c.entry["state"] == "normal"
        assert c.offline_bar.winfo_manager() == ""
        break


# ---- Task 3 tests ----

def test_dm_rows_show_avatars(tmp_path):
    """DM section must render an avatar canvas per online user."""
    for c, root in make_client("Tara"):
        c.users = ["Noah", "Maya"]
        c.render_sidebar()
        root.update()
        children = c.user_box.winfo_children()
        assert any(
            any(isinstance(w, tk.Canvas) for w in row.winfo_children())
            for row in children
        ), "DM rows must include avatar canvases"
        break


# ---- Task 4 tests ----

def _widget_texts(panel):
    texts = []
    for w in panel.winfo_children():
        try:
            texts.append(w.cget("text"))
        except Exception:
            pass
    return texts


def test_members_panel_shows_dm_state(tmp_path):
    """In a DM, members panel must show 'CONVERSATION' header."""
    for c, root in make_client("Tara"):
        c.users = ["Noah"]
        c.groups = {}
        c.select(("pm", "Noah"))
        root.update()
        texts = _widget_texts(c.members_panel)
        assert any("CONVERSATION" in t for t in texts), f"Expected CONVERSATION, got {texts}"
        break


def test_members_panel_shows_group_state(tmp_path):
    """In a group, members panel must show member count."""
    for c, root in make_client("Tara"):
        c.groups = {"general": ["Tara", "Noah"]}
        c.users = ["Noah"]
        c.select(("group", "general"))
        root.update()
        texts = _widget_texts(c.members_panel)
        assert any("MEMBERS" in t for t in texts), f"Expected MEMBERS, got {texts}"
        assert c.members_panel.winfo_manager() != ""
        break


# ---- Task 5 tests ----

def test_grouped_messages_no_duplicate_avatar(tmp_path):
    """Second message from same sender within 5 min must use 'grouped' tag."""
    for c, root in make_client("Tara"):
        c.users = ["Noah"]
        c.groups = {"g": ["Tara", "Noah"]}
        c.select(("group", "g"))
        key = ("group", "g")
        c.show(key, "Noah", "first message")
        c.show(key, "Noah", "second message")
        box = c.rooms[key]
        ranges = box.tag_ranges("grouped")
        assert ranges, "Second consecutive message must use 'grouped' tag"
        break


# ---- Task 6 tests ----

def test_show_file_uses_grouped_tag(tmp_path):
    """show_file after show() from same sender within 5 min must use grouped tag."""
    for c, root in make_client("Tara"):
        c.users = ["Noah"]
        c.groups = {"g": ["Tara", "Noah"]}
        c.select(("group", "g"))
        key = ("group", "g")
        c.show(key, "Noah", "just sent a file")
        c.show_file(key, "Noah", "report.pdf", 1024, "id1", raw_bytes=b"fake")
        box = c.rooms[key]
        ranges = box.tag_ranges("grouped")
        assert ranges, "show_file after same-sender message must use grouped tag"
        break


# ---- Task 7 tests ----

def test_ai_popover_toggles(tmp_path):
    """Clicking AI button must show/hide the popover."""
    for c, root in make_client("Tara"):
        c.users = ["Noah"]
        c.groups = {"g": ["Tara", "Noah"]}
        c.select(("group", "g"))
        assert c._ai_popover.winfo_manager() == "", "popover must start hidden"
        c._toggle_ai_menu()
        root.update()
        assert c._ai_popover.winfo_manager() != "", "popover must show after toggle"
        c._toggle_ai_menu()
        root.update()
        assert c._ai_popover.winfo_manager() == "", "popover must hide after second toggle"
        break


def test_ai_draft_hint_updates(tmp_path):
    """Draft button text must change based on whether entry is empty."""
    for c, root in make_client("Tara"):
        c.users = ["Noah"]
        c.groups = {"g": ["Tara", "Noah"]}
        c.select(("group", "g"))
        c._update_ai_hint()
        assert "Auto-generate" in c.draft_button.cget("text")
        c.entry.insert(0, "hello")
        c._update_ai_hint()
        assert "Rewrite" in c.draft_button.cget("text")
        break
