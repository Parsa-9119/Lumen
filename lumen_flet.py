#!/usr/bin/env python3
"""
Lumen - Flet edition.
To-Do, Notes, Reminders, Calendar, Flashcards (spaced repetition), Focus timer with ambient sounds,
a garden game, and an agentic AI Tutor - same data and rules as the tkinter version (~/.lumen/lumen.db).

Run:      python lumen_flet.py          (keep lumen_core.py in the same folder)
Install:  pip install flet
Tutor:    set ANTHROPIC_API_KEY, or paste a key in Settings.
Quick add (top bar): buy milk fri !high | remind me to call mom tomorrow 9am | note: Title | body | card: Q | A
"""
import asyncio
import calendar
import json
import os
import sys
import threading
import time
from datetime import date, datetime, timedelta
from pathlib import Path

import flet as ft

sys.path.insert(0, str(Path(__file__).resolve().parent))
from lumen_core import (ACHIEVEMENTS, APP_DIR, APP_NAME, DEFAULT_MODEL, FMT, GARDEN_COLS, GARDEN_MIN_MINUTES,  # noqa: E402
                        GARDEN_PLOT, LEVEL_TITLES, SPECIES, STAGE_NAMES, TOOLS, Database, GardenGame, SoundPlayer,
                        ask_claude, execute_tool, garden_backfill, level_title, next_occurrence, now_str, parse_cards,
                        parse_date, parse_quick, parse_when, plant_kind, sm2, split_when, stage_of, stream_turn,
                        species_of, xp_for)

# ---------------------------------------------------------------------------------------------
# Look & feel - translucent greys work on both light and dark themes
# ---------------------------------------------------------------------------------------------
def alpha(hex_rgb, a):
    """A translucent colour. Uses Flet's own helper when present, else an #AARRGGBB string."""
    try:
        v = ft.Colors.with_opacity(a, hex_rgb)
        if isinstance(v, str) and not v.startswith("Colors."):
            return v
    except Exception:  # noqa: BLE001
        pass
    return "#%02x%s" % (int(a * 255), hex_rgb.lstrip("#"))


ACCENT, SOFT, CARD = "#6366f1", alpha("#6366f1", 0.22), alpha("#808080", 0.13)
MUTED, OK, WARN, DANGER = "#8a8f98", "#22c55e", "#f59e0b", "#ef4444"
FAINT, DARKTOAST, GREENTILE = alpha("#808080", 0.06), alpha("#1f2937", 0.95), alpha("#16a34a", 0.14)

NAV = [("dash", "🏠", "Dashboard"), ("todo", "✅", "To-Do"), ("notes", "📝", "Notes"), ("reminders", "⏰", "Reminders"),
       ("calendar", "📅", "Calendar"), ("cards", "🧠", "Flashcards"), ("focus", "🎯", "Focus"),
       ("garden", "🌻", "Garden"), ("tutor", "🤖", "AI Tutor"), ("settings", "⚙️", "Settings")]

OptionCls = getattr(ft, "DropdownOption", None) or getattr(getattr(ft, "dropdown", None), "Option", None)


def mk(ctor, *args, optional=None, **kw):
    """Build a control; if this Flet version rejects an optional keyword, retry without it."""
    try:
        return ctor(*args, **kw, **(optional or {}))
    except TypeError:
        return ctor(*args, **kw)


def txt(value="", size=14, bold=False, color=None, **kw):
    return ft.Text(str(value), size=size, weight=ft.FontWeight.BOLD if bold else None, color=color, **kw)


def btn(label, on_click=None, primary=False, small=False, disabled=False):
    t = ft.Text(label, size=12 if small else 14, weight=ft.FontWeight.W_600 if primary else None,
                color="#ffffff" if primary else None)
    c = mk(ft.Container, content=t, padding=7 if small else 10, border_radius=8, bgcolor=ACCENT if primary else CARD,
           on_click=None if disabled else on_click, opacity=0.4 if disabled else 1.0, optional={"ink": True})
    return c


def set_label(b, text):
    b.content.value = text


def field(label="", value="", **kw):
    return ft.TextField(label=label, value=value, **kw)


def dropdown(label, options, value, width=None, **kw):
    return ft.Dropdown(label=label, options=[OptionCls(o) for o in options], value=value, width=width, **kw)


def box(content, pad=14, bg=CARD, radius=12, **kw):
    return ft.Container(content=content, padding=pad, bgcolor=bg, border_radius=radius, **kw)


def row(*controls, spacing=8, wrap=False, **kw):
    return ft.Row(list(controls), spacing=spacing, wrap=wrap, **kw)


def col(*controls, spacing=8, **kw):
    return ft.Column(list(controls), spacing=spacing, **kw)


def chip(label, active, on_click):
    return ft.Container(content=txt(label, 13, active), padding=8, border_radius=16,
                        bgcolor=SOFT if active else CARD, on_click=on_click)


def bar(value, width=None, color=ACCENT, height=8):
    return mk(ft.ProgressBar, value=max(0.0, min(1.0, value)), width=width, color=color, optional={"bar_height": height})


def nice_due(d):
    try:
        return datetime.strptime(d, "%Y-%m-%d").strftime("%a %d %b")
    except ValueError:
        return d


class Pg:
    """Base class for pages. build() makes the widgets once; refresh() refills them with fresh data."""
    title, subtitle = "", ""

    def __init__(self, app):
        self.app, self.db = app, app.db
        self.view = ft.Column(expand=True, scroll=ft.ScrollMode.AUTO, spacing=12)
        self.build()

    def head(self):
        return [txt(self.title, 26, True), txt(self.subtitle, 13, color=MUTED)]

    def build(self):
        pass

    def refresh(self):
        pass


# ---------------------------------------------------------------------------------------------
# Dashboard
# ---------------------------------------------------------------------------------------------
class DashPage(Pg):
    title, subtitle = "Dashboard", "Your day at a glance"

    def refresh(self):
        db, today = self.db, date.today().isoformat()
        g = self.app.game
        stats = [("Open tasks", db.one("SELECT COUNT(*) FROM todos WHERE done=0"), "✅"),
                 ("Due / overdue", db.one("SELECT COUNT(*) FROM todos WHERE done=0 AND due<>'' AND due<=?", (today,)), "🔥"),
                 ("Reminders", db.one("SELECT COUNT(*) FROM reminders WHERE done=0"), "⏰"),
                 ("Cards due", db.one("SELECT COUNT(*) FROM cards WHERE due<=?", (today,)), "🧠"),
                 ("Focus today (min)", db.one("SELECT COALESCE(SUM(minutes),0) FROM focus_sessions WHERE substr(ended,1,10)=?", (today,)), "🎯"),
                 ("Focus streak (days)", g.focus_streak(), "⚡"),
                 ("Sun ☀", g.coins(), "🌞"), ("Garden level", g.level(), "🌻")]
        cards = [box(col(txt(f"{ic}  {v}", 24, True), txt(name, 12, color=MUTED), spacing=2), width=170) for name, v, ic in stats]
        days = [(date.today() - timedelta(days=i)) for i in range(6, -1, -1)]
        counts = [db.one("SELECT COUNT(*) FROM todos WHERE done=1 AND substr(done_at,1,10)=?", (d.isoformat(),)) for d in days]
        top = max(counts + [1])
        bars = row(*[col(txt(c, 12), ft.Container(width=34, height=8 + 90 * c / top, bgcolor=ACCENT, border_radius=6),
                         txt(d.strftime("%a"), 11, color=MUTED), spacing=4) for d, c in zip(days, counts)],
                   spacing=16, vertical_alignment=ft.CrossAxisAlignment.END)
        soon = [txt("Nothing scheduled - enjoy the calm.", color=MUTED)]
        items = [f"⏰ {r['due']}  {r['title']}" for r in db.rows("SELECT title,due FROM reminders WHERE done=0 ORDER BY due LIMIT 6")]
        items += [f"✅ {nice_due(r['due'])}  {r['title']}" for r in
                  db.rows("SELECT title,due FROM todos WHERE done=0 AND due<>'' ORDER BY due LIMIT 6")]
        if items:
            soon = [txt(i, 14) for i in items]
        qs = [row(txt(("✔ " if q["done"] else "○ ") + q["title"], 14), txt(f"{min(q['progress'], q['target'])}/{q['target']}", 12, color=MUTED))
              for q in g.quests()]
        self.view.controls = self.head() + [
            ft.Row(cards, wrap=True, spacing=12, run_spacing=12),
            box(col(txt("Tasks completed - last 7 days", 15, True), bars)),
            box(col(txt("Coming up", 15, True), *soon)),
            box(col(txt("Today's garden quests", 15, True), *qs))]


# ---------------------------------------------------------------------------------------------
# To-Do
# ---------------------------------------------------------------------------------------------
class TodoPage(Pg):
    title, subtitle = "To-Do", "Dates understood: today, tomorrow, fri, +3d, 2026-12-31"
    FILTERS = ["All", "Open", "Done", "Overdue"]

    def build(self):
        self.filter, self.editing = "All", None
        self.new_title = field("New task", expand=True, on_submit=self.add)
        self.new_due = field("Due (e.g. fri)", width=160, on_submit=self.add)
        self.new_prio = dropdown("Priority", ["High", "Medium", "Low"], "Medium", 130)
        self.search = field("Search", width=200, on_change=lambda e: (self.refresh(), self.app.update()))
        self.chips, self.list = ft.Row(spacing=6, wrap=True), ft.Column(spacing=6)
        self.view.controls = self.head() + [row(self.new_title, self.new_due, self.new_prio, btn("Add", self.add, True)),
                                            row(self.chips, self.search, wrap=True), self.list]

    def add(self, e=None):
        title = (self.new_title.value or "").strip()
        if not title:
            return
        due = ""
        if (self.new_due.value or "").strip():
            due = parse_date(self.new_due.value)
            if due is None:
                self.app.toast("Couldn't read that date - try today, tomorrow, fri, +3d")
                return
        self.db.run("INSERT INTO todos(title,priority,due,created) VALUES(?,?,?,?)", (title, self.new_prio.value or "Medium", due, now_str()))
        self.new_title.value = self.new_due.value = ""
        self.refresh()
        self.app.update()

    def set_filter(self, f):
        self.filter = f
        self.refresh()
        self.app.update()

    def refresh(self):
        today = date.today().isoformat()
        self.chips.controls = [chip(f, f == self.filter, lambda e, f=f: self.set_filter(f)) for f in self.FILTERS]
        where, args = ["1=1"], []
        if self.filter == "Open":
            where.append("done=0")
        elif self.filter == "Done":
            where.append("done=1")
        elif self.filter == "Overdue":
            where.append("done=0 AND due<>'' AND due<?")
            args.append(today)
        q = (self.search.value or "").strip()
        if q:
            where.append("title LIKE ?")
            args.append(f"%{q}%")
        rows = self.db.rows(f"SELECT * FROM todos WHERE {' AND '.join(where)} ORDER BY done, due='', due, "
                            "CASE priority WHEN 'High' THEN 0 WHEN 'Medium' THEN 1 ELSE 2 END, id DESC", args)
        self.list.controls = [self.row_for(r, today) for r in rows] or [txt("Nothing here yet.", color=MUTED)]

    def row_for(self, r, today):
        tid = r["id"]
        if self.editing == tid:
            t = field("Title", r["title"], expand=True)
            d = field("Due", r["due"] or "", width=150)
            p = dropdown("Priority", ["High", "Medium", "Low"], r["priority"], 130)
            return box(row(t, d, p, btn("Save", lambda e: self.save_edit(tid, t, d, p), True), btn("Cancel", lambda e: self.edit(None))))
        overdue = r["due"] and not r["done"] and r["due"] < today
        pcol = {"High": DANGER, "Medium": WARN, "Low": OK}.get(r["priority"], MUTED)
        due = txt(nice_due(r["due"]), 12, color=DANGER if overdue else MUTED) if r["due"] else txt("", 12)
        return box(row(ft.Checkbox(value=bool(r["done"]), on_change=lambda e, i=tid: self.toggle(i, e.control.value)),
                       ft.Container(content=txt(r["title"], 15, color=MUTED if r["done"] else None), expand=True),
                       txt("●", 14, color=pcol), txt(r["priority"], 12, color=MUTED), due,
                       btn("Focus", lambda e, t=r["title"]: self.app.focus_on(t), small=True),
                       btn("Edit", lambda e, i=tid: self.edit(i), small=True),
                       btn("Delete", lambda e, i=tid: self.delete(i), small=True)), pad=8)

    def toggle(self, tid, done):
        self.db.run("UPDATE todos SET done=?, done_at=? WHERE id=?", (1 if done else 0, now_str() if done else None, tid))
        self.refresh()
        self.app.update()

    def edit(self, tid):
        self.editing = tid
        self.refresh()
        self.app.update()

    def save_edit(self, tid, t, d, p):
        due = ""
        if (d.value or "").strip():
            due = parse_date(d.value)
            if due is None:
                self.app.toast("Couldn't read that date")
                return
        if (t.value or "").strip():
            self.db.run("UPDATE todos SET title=?, due=?, priority=? WHERE id=?", (t.value.strip(), due, p.value, tid))
        self.edit(None)

    def delete(self, tid):
        snap = self.db.snapshot("todos", tid)
        self.db.run("DELETE FROM todos WHERE id=?", (tid,))
        self.refresh()
        self.app.toast("Task deleted", undo=lambda: (self.db.restore("todos", snap), self.refresh(), self.app.update()))
        self.app.update()


# ---------------------------------------------------------------------------------------------
# Notes
# ---------------------------------------------------------------------------------------------
class NotesPage(Pg):
    title, subtitle = "Notes", "Saved as you type"

    def build(self):
        self.cur = None
        self.search = field("Search notes", on_change=lambda e: (self.refresh(), self.app.update()))
        self.list = ft.Column(spacing=4, scroll=ft.ScrollMode.AUTO, expand=True)
        self.t_title = field("Title", on_change=self.save, on_blur=self.relist)
        self.t_tags = field("Tags (comma separated)", on_change=self.save, on_blur=self.relist)
        self.t_body = field("Write here...", multiline=True, min_lines=16, max_lines=30, on_change=self.save, on_blur=self.relist)
        self.status = txt("", 12, color=MUTED)
        self.pin_btn = btn("📌 Pin", lambda e: self.pin())
        self.editor = col(self.t_title, self.t_body, self.t_tags,
                          row(self.pin_btn, btn("Export .md", lambda e: self.export()), btn("Make flashcards", lambda e: self.cards()),
                              btn("Ask tutor", lambda e: self.ask()), btn("Delete", lambda e: self.delete()), self.status, wrap=True),
                          expand=True, scroll=ft.ScrollMode.AUTO)
        self.editor.visible = False
        self.empty = txt("Pick a note or create a new one.", color=MUTED)
        self.view.controls = self.head() + [row(btn("＋ New note", lambda e: self.new(), True), ft.Container(self.search, expand=True)),
                                            ft.Row([ft.Container(self.list, width=290, height=560),
                                                    ft.Container(col(self.empty, self.editor), expand=True)],
                                                   vertical_alignment=ft.CrossAxisAlignment.START, spacing=18)]

    def refresh(self):
        q = f"%{(self.search.value or '').strip()}%"
        rows = self.db.rows("SELECT * FROM notes WHERE title LIKE ? OR body LIKE ? OR tags LIKE ? ORDER BY pinned DESC, updated DESC", (q, q, q))
        self.list.controls = [ft.Container(content=col(txt(("📌 " if r["pinned"] else "") + (r["title"] or "Untitled"), 14, True),
                                                      txt((r["body"] or "").strip().replace("\n", " ")[:60], 12, color=MUTED), spacing=2),
                                           padding=10, border_radius=8, bgcolor=SOFT if r["id"] == self.cur else CARD,
                                           on_click=lambda e, i=r["id"]: self.open(i)) for r in rows] or [txt("No notes.", color=MUTED)]

    def relist(self, e=None):
        self.refresh()
        self.app.update()

    def new(self):
        cur = self.db.run("INSERT INTO notes(title,body,tags,pinned,updated) VALUES('Untitled','','',0,?)", (now_str(),))
        self.open(cur.lastrowid)

    def open(self, nid):
        r = self.db.snapshot("notes", nid)
        if not r:
            return
        self.cur = nid
        self.t_title.value, self.t_body.value, self.t_tags.value = r["title"] or "", r["body"] or "", r["tags"] or ""
        set_label(self.pin_btn, "📌 Unpin" if r["pinned"] else "📌 Pin")
        self.editor.visible, self.empty.visible = True, False
        self.status.value = ""
        self.refresh()
        self.app.update()

    def save(self, e=None):
        if self.cur is None:
            return
        self.db.run("UPDATE notes SET title=?, body=?, tags=?, updated=? WHERE id=?",
                    (self.t_title.value or "", self.t_body.value or "", self.t_tags.value or "", now_str(), self.cur))
        words = len((self.t_body.value or "").split())
        self.status.value = f"Saved · {words} words"

    def pin(self):
        if self.cur is None:
            return
        v = 0 if self.db.one("SELECT pinned FROM notes WHERE id=?", (self.cur,)) else 1
        self.db.run("UPDATE notes SET pinned=? WHERE id=?", (v, self.cur))
        set_label(self.pin_btn, "📌 Unpin" if v else "📌 Pin")
        self.refresh()
        self.app.update()

    def export(self):
        if self.cur is None:
            return
        out = Path.home() / "Lumen" / "exports"
        out.mkdir(parents=True, exist_ok=True)
        safe = "".join(c if c.isalnum() or c in " -_" else "_" for c in (self.t_title.value or "note")).strip() or "note"
        path = out / f"{safe}.md"
        path.write_text(f"# {self.t_title.value}\n\n{self.t_body.value}\n", encoding="utf-8")
        self.app.toast(f"Exported to {path}")

    def cards(self):
        if self.cur is not None:
            self.app.generate_cards(f"{self.t_title.value}\n\n{self.t_body.value}", self.t_title.value or "Notes")

    def ask(self):
        if self.cur is not None:
            self.app.show("tutor")
            self.app.pages["tutor"].send(f"Help me study this note. Summarize it and quiz me on it:\n\n{self.t_title.value}\n{self.t_body.value}")

    def delete(self):
        if self.cur is None:
            return
        snap, nid = self.db.snapshot("notes", self.cur), self.cur
        self.db.run("DELETE FROM notes WHERE id=?", (nid,))
        self.cur = None
        self.editor.visible, self.empty.visible = False, True
        self.refresh()
        self.app.toast("Note deleted", undo=lambda: (self.db.restore("notes", snap), self.refresh(), self.app.update()))
        self.app.update()


# ---------------------------------------------------------------------------------------------
# Reminders
# ---------------------------------------------------------------------------------------------
class RemindersPage(Pg):
    title, subtitle = "Reminders", "Try: in 30m, tomorrow 9am, fri 14:30, 2026-12-31 18:00"

    def build(self):
        self.what = field("Remind me to...", expand=True, on_submit=self.add)
        self.when = field("When (e.g. in 30m)", width=210, on_submit=self.add)
        self.rep = dropdown("Repeat", ["Once", "Daily", "Weekly", "Monthly"], "Once", 130)
        self.list = ft.Column(spacing=6)
        self.view.controls = self.head() + [row(self.what, self.when, self.rep, btn("Add", self.add, True)), self.list]

    def add(self, e=None):
        title, when = (self.what.value or "").strip(), (self.when.value or "").strip()
        if not title:
            return
        dt = parse_when(when) if when else None
        if not when:
            title, dt = split_when(title)
        if dt is None:
            self.app.toast("Couldn't understand the time - try 'in 30m' or 'tomorrow 9am'")
            return
        self.db.run("INSERT INTO reminders(title,due,repeat) VALUES(?,?,?)", (title or "Reminder", dt.strftime(FMT), self.rep.value or "Once"))
        self.what.value = self.when.value = ""
        self.refresh()
        self.app.update()

    def refresh(self):
        now = now_str()
        out = []
        for r in self.db.rows("SELECT * FROM reminders ORDER BY done, due"):
            late = not r["done"] and r["due"] < now
            rep = r["repeat"] or "Once"
            out.append(box(row(ft.Checkbox(value=bool(r["done"]), on_change=lambda e, i=r["id"]: self.set_done(i, e.control.value)),
                               ft.Container(content=txt(r["title"], 15, color=MUTED if r["done"] else None), expand=True),
                               txt(r["due"], 13, color=DANGER if late else MUTED), txt("" if rep == "Once" else f"↻ {rep}", 12, color=MUTED),
                               btn("Delete", lambda e, i=r["id"]: self.delete(i), small=True)), pad=8))
        self.list.controls = out or [txt("No reminders.", color=MUTED)]

    def set_done(self, rid, v):
        self.db.run("UPDATE reminders SET done=? WHERE id=?", (1 if v else 0, rid))
        self.refresh()
        self.app.update()

    def delete(self, rid):
        snap = self.db.snapshot("reminders", rid)
        self.db.run("DELETE FROM reminders WHERE id=?", (rid,))
        self.refresh()
        self.app.toast("Reminder deleted", undo=lambda: (self.db.restore("reminders", snap), self.refresh(), self.app.update()))
        self.app.update()


# ---------------------------------------------------------------------------------------------
# Calendar
# ---------------------------------------------------------------------------------------------
class CalendarPage(Pg):
    title, subtitle = "Calendar", "Task due dates and reminders - click a day"

    def build(self):
        t = date.today()
        self.y, self.m, self.sel = t.year, t.month, t.isoformat()
        self.label, self.grid, self.detail = txt("", 20, True), ft.Column(spacing=6), ft.Column(spacing=6)
        self.new = field("Add a task for this day", expand=True, on_submit=self.add)
        self.view.controls = self.head() + [row(btn("◀", lambda e: self.move(-1)), self.label, btn("▶", lambda e: self.move(1)),
                                                btn("Today", lambda e: self.today())),
                                            self.grid, box(col(row(self.new, btn("Add", self.add, True)), self.detail))]

    def move(self, d):
        self.m += d
        if self.m < 1:
            self.y, self.m = self.y - 1, 12
        elif self.m > 12:
            self.y, self.m = self.y + 1, 1
        self.refresh()
        self.app.update()

    def today(self):
        t = date.today()
        self.y, self.m, self.sel = t.year, t.month, t.isoformat()
        self.refresh()
        self.app.update()

    def pick(self, iso):
        self.sel = iso
        self.refresh()
        self.app.update()

    def add(self, e=None):
        title = (self.new.value or "").strip()
        if title:
            self.db.run("INSERT INTO todos(title,priority,due,created) VALUES(?,'Medium',?,?)", (title, self.sel, now_str()))
            self.new.value = ""
            self.refresh()
            self.app.update()

    def refresh(self):
        self.label.value = f"{calendar.month_name[self.m]} {self.y}"
        pre = f"{self.y}-{self.m:02d}-"
        tasks = {r[0]: r[1] for r in self.db.rows("SELECT due,COUNT(*) FROM todos WHERE done=0 AND due LIKE ? GROUP BY due", (pre + "%",))}
        rems = {r[0]: r[1] for r in self.db.rows("SELECT substr(due,1,10),COUNT(*) FROM reminders WHERE done=0 AND due LIKE ? GROUP BY 1", (pre + "%",))}
        today = date.today().isoformat()
        rows = [row(*[ft.Container(content=txt(d, 12, True, MUTED), width=96) for d in ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")], spacing=6)]
        for week in calendar.monthcalendar(self.y, self.m):
            cells = []
            for d in week:
                if not d:
                    cells.append(ft.Container(width=96, height=64))
                    continue
                iso = f"{pre}{d:02d}"
                marks = ("●" * min(tasks.get(iso, 0), 3)) + ("⏰" if rems.get(iso) else "")
                cells.append(ft.Container(content=col(txt(d, 14, iso == today), txt(marks, 11, color=ACCENT), spacing=2), width=96, height=64,
                                          padding=8, border_radius=8, bgcolor=SOFT if iso == self.sel else CARD, on_click=lambda e, i=iso: self.pick(i)))
            rows.append(row(*cells, spacing=6))
        self.grid.controls = rows
        items = [txt(f"⏰ {r['due'][11:]}  {r['title']}") for r in self.db.rows("SELECT title,due FROM reminders WHERE done=0 AND substr(due,1,10)=? ORDER BY due", (self.sel,))]
        items += [txt(("✔ " if r["done"] else "○ ") + r["title"]) for r in self.db.rows("SELECT title,done FROM todos WHERE due=? ORDER BY done", (self.sel,))]
        self.detail.controls = [txt(nice_due(self.sel), 15, True)] + (items or [txt("Nothing on this day.", color=MUTED)])


# ---------------------------------------------------------------------------------------------
# Flashcards
# ---------------------------------------------------------------------------------------------
class CardsPage(Pg):
    title, subtitle = "Flashcards", "Spaced repetition - cards come back right before you'd forget them"

    def build(self):
        self.mode, self.queue, self.card, self.shown, self.done = "Study", [], None, False, 0
        self.tabs = ft.Row(spacing=6)
        self.deck = dropdown("Deck", ["All decks"], "All decks", 220)
        self.body = ft.Column(spacing=12)
        self.view.controls = self.head() + [self.tabs, self.body]

    def decks(self):
        return [r[0] for r in self.db.rows("SELECT DISTINCT deck FROM cards ORDER BY deck")]

    def set_mode(self, m):
        self.mode = m
        self.refresh()
        self.app.update()

    def refresh(self):
        self.tabs.controls = [chip(m, m == self.mode, lambda e, m=m: self.set_mode(m)) for m in ("Study", "Manage")]
        keep = self.deck.value if self.deck.value in ["All decks"] + self.decks() else "All decks"
        self.deck.options = [OptionCls(o) for o in ["All decks"] + self.decks()]
        self.deck.value = keep
        self.study_view() if self.mode == "Study" else self.manage_view()

    def deck_filter(self):
        d = self.deck.value
        return ("", []) if not d or d == "All decks" else (" AND deck=?", [d])

    def study_view(self):
        today, (extra, args) = date.today().isoformat(), self.deck_filter()
        due = self.db.one(f"SELECT COUNT(*) FROM cards WHERE due<=?{extra}", [today] + args)
        total = self.db.one(f"SELECT COUNT(*) FROM cards WHERE 1=1{extra}", args)
        head = row(self.deck, btn("Start session", lambda e: self.start(), True), txt(f"{due} due · {total} total", 13, color=MUTED))
        if self.card is None:
            msg = "Press Start session to review your due cards." if due else (
                "All caught up! 🎉" if total else "No cards yet - add some under Manage, or generate them from a note.")
            self.body.controls = [head, box(txt(msg, 16))] + ([txt(f"Reviewed this session: {self.done}", 13, color=MUTED)] if self.done else [])
            return
        c = self.card
        parts = [head, bar(self.done / max(1, self.done + len(self.queue) + 1), 420),
                 box(col(txt(c["deck"], 12, color=MUTED), txt(c["front"], 22, True),
                         *([ft.Divider(), txt(c["back"], 20)] if self.shown else [])), pad=24)]
        if not self.shown:
            parts.append(btn("Show answer", lambda e: self.reveal(), True))
        else:
            parts.append(row(*[btn(label, lambda e, g=g: self.grade(g), g == 2) for g, label in enumerate(["Again", "Hard", "Good", "Easy"])]))
        self.body.controls = parts

    def start(self):
        today, (extra, args) = date.today().isoformat(), self.deck_filter()
        self.queue = [r["id"] for r in self.db.rows(f"SELECT id FROM cards WHERE due<=?{extra} ORDER BY due, id LIMIT 100", [today] + args)]
        self.done = 0
        self.next_card()

    def next_card(self):
        self.shown = False
        self.card = self.db.snapshot("cards", self.queue.pop(0)) if self.queue else None
        self.refresh()
        self.app.update()

    def reveal(self):
        self.shown = True
        self.refresh()
        self.app.update()

    def grade(self, g):
        c = self.card
        ease, days, reps = sm2(c["ease"], c["interval"], c["reps"], g)
        due = (date.today() + timedelta(days=days)).isoformat()
        self.db.run("UPDATE cards SET ease=?, interval=?, reps=?, due=? WHERE id=?", (ease, days, reps, due, c["id"]))
        if g == 0:
            self.queue.append(c["id"])
        else:
            self.done += 1
        self.next_card()

    def manage_view(self):
        self.n_deck, self.n_front, self.n_back = field("Deck", "General", width=180), field("Front", expand=True), field("Back", expand=True)
        self.gen = field("Generate with AI: a topic, or paste notes", multiline=True, min_lines=2, max_lines=6, expand=True)
        rows = []
        for r in self.db.rows("SELECT * FROM cards ORDER BY id DESC LIMIT 60"):
            rows.append(box(row(txt(r["deck"], 12, color=MUTED), ft.Container(content=txt(r["front"], 14), expand=True),
                                ft.Container(content=txt(r["back"], 14, color=MUTED), expand=True),
                                btn("Delete", lambda e, i=r["id"]: self.delete(i), small=True)), pad=8))
        self.body.controls = [box(col(txt("Add a card", 15, True), row(self.n_deck, self.n_front, self.n_back, btn("Add", self.add, True)))),
                              box(col(txt("Generate cards", 15, True), row(self.gen, btn("Generate", self.generate, True)))),
                              txt("Recent cards", 15, True)] + (rows or [txt("No cards.", color=MUTED)])

    def add(self, e=None):
        f, b = (self.n_front.value or "").strip(), (self.n_back.value or "").strip()
        if f and b:
            self.db.run("INSERT INTO cards(deck,front,back,due,created) VALUES(?,?,?,?,?)",
                        ((self.n_deck.value or "General").strip(), f, b, date.today().isoformat(), now_str()))
            self.refresh()
            self.app.update()

    def generate(self, e=None):
        self.app.generate_cards(self.gen.value or "", "Generated")

    def delete(self, cid):
        snap = self.db.snapshot("cards", cid)
        self.db.run("DELETE FROM cards WHERE id=?", (cid,))
        self.refresh()
        self.app.toast("Card deleted", undo=lambda: (self.db.restore("cards", snap), self.refresh(), self.app.update()))
        self.app.update()


# ---------------------------------------------------------------------------------------------
# Focus timer + sounds
# ---------------------------------------------------------------------------------------------
class FocusPage(Pg):
    title, subtitle = "Focus", "Set your own lengths, add background sound, and grow a garden"
    MODES = ["Focus", "Short break", "Long break"]
    KEYS = {"Focus": ("focus_min", 25, 1, 180), "Short break": ("short_min", 5, 1, 60), "Long break": ("long_min", 15, 1, 60)}
    MY_FILE = "My audio file..."

    def build(self):
        self.player = self.app.player
        self.mins = {m: self._load(m) for m in self.MODES}
        self.mode, self.running, self.end_at = "Focus", False, 0.0
        self.total = self.left = self.mins["Focus"] * 60
        self.now_playing = None
        self.time_txt, self.mode_txt, self.reward = txt("25:00", 72, True), txt("", 13, color=MUTED), txt("", 13, color=MUTED)
        self.prog = bar(0, 440, height=10)
        self.mode_chips = ft.Row(spacing=6)
        self.task = field("What are you working on?", width=440)
        self.start_b = btn("Start", lambda e: self.toggle(), True)
        self.len_fields = {m: field(f"{m} (min)", str(self.mins[m]), width=130, on_change=lambda e, m=m: self.on_len(m, e.control.value)) for m in self.MODES}
        names = ["Off"] + SoundPlayer.SOUNDS + [self.MY_FILE]
        saved = self.db.get("sound_choice", "Brown noise")
        self.sound = dropdown("Background sound", names, saved if saved in names else "Brown noise", 220)
        self.custom = field("Path to your audio file", self.db.get("sound_file"), width=330)
        self.sound_b = btn("▶ Play", lambda e: self.toggle_sound())
        self.snd_status = txt("", 12, color=MUTED)
        try:
            vol = float(self.db.get("sound_volume", "60") or 60)
        except ValueError:
            vol = 60.0
        self.volume = ft.Slider(min=0, max=100, value=vol, divisions=20, width=220, on_change_end=self.apply_volume)
        self.auto = ft.Checkbox(label="Play sound automatically while focusing", value=self.db.get("sound_auto", "1") == "1", on_change=self.save_prefs)
        self.chime = ft.Checkbox(label="Chime when a session ends", value=self.db.get("chime_on", "1") == "1", on_change=self.save_prefs)
        self.view.controls = self.head() + [
            box(col(self.mode_chips, self.time_txt, self.mode_txt, self.prog, self.task,
                    row(btn("−5 min", lambda e: self.add_time(-5)), self.start_b, btn("Reset", lambda e: self.reset()), btn("+5 min", lambda e: self.add_time(5))),
                    self.reward, spacing=12), pad=22),
            box(col(txt("Timer lengths", 15, True), row(*self.len_fields.values()))),
            box(col(txt("Background sound", 15, True), row(self.sound, self.sound_b, txt("Volume", 12, color=MUTED), self.volume),
                    self.custom, self.auto, self.chime, self.snd_status))]
        self.draw()

    def _load(self, mode):
        key, default, lo, hi = self.KEYS[mode]
        try:
            return max(lo, min(hi, int(self.db.get(key, str(default)) or default)))
        except ValueError:
            return default

    def refresh(self):
        self.draw()

    def on_len(self, mode, value):
        key, default, lo, hi = self.KEYS[mode]
        try:
            n = max(lo, min(hi, int(value)))
        except ValueError:
            return
        self.mins[mode] = n
        self.db.put(key, str(n))
        if mode == self.mode and not self.running:
            self.total = self.left = n * 60
            self.draw()
            self.app.update()

    def set_mode(self, name):
        self.halt()
        self.mode = name
        self.total = self.left = self.mins[name] * 60
        if self.auto.value:
            self.stop_sound()
        self.draw()
        self.app.update()

    def draw(self):
        self.time_txt.value = f"{self.left // 60:02d}:{self.left % 60:02d}"
        self.mode_txt.value = self.mode.upper()
        self.prog.value = (self.total - self.left) / max(1, self.total)
        self.mode_chips.controls = [chip(m, m == self.mode, lambda e, m=m: self.set_mode(m)) for m in self.MODES]
        set_label(self.start_b, "Pause" if self.running else "Start")
        if self.mode == "Focus":
            m, kind, bits = self.total // 60, plant_kind(self.total // 60), []
            if m >= 10:
                bits.append(f"+{self.app.game.coin_reward(m)} ☀")
            bits.append(f"plants a {kind.lower()}" if kind else f"{GARDEN_MIN_MINUTES}+ min plants something")
            self.reward.value = "Finishing earns:  " + "  ·  ".join(bits)
        else:
            self.reward.value = ""

    def toggle(self):
        self.pause() if self.running else self.begin()
        self.app.update()

    def begin(self):
        self.running = True
        self.end_at = time.monotonic() + self.left
        if self.mode == "Focus" and self.auto.value and self.sound.value != "Off" and not self.player.playing:
            self.play_sound()
        self.draw()

    def halt(self):
        self.running = False

    def pause(self):
        self.halt()
        if self.auto.value:
            self.stop_sound()
        self.draw()

    def reset(self):
        self.halt()
        self.total = self.left = self.mins[self.mode] * 60
        if self.auto.value:
            self.stop_sound()
        self.draw()
        self.app.update()

    def add_time(self, delta):
        if self.running:
            self.end_at += delta * 60
            self.total = max(60, self.total + delta * 60)
            self.left = max(1, math_ceil(self.end_at - time.monotonic()))
        else:
            self.total = max(60, self.total + delta * 60)
            self.left = self.total
        self.draw()
        self.app.update()

    def tick(self):
        """Called by the app's background loop every half second."""
        if not self.running:
            return
        remaining = max(0, math_ceil(self.end_at - time.monotonic()))
        if remaining != self.left:
            self.left = remaining
            self.draw()
            self.app.update()
        if remaining <= 0:
            self.finish()

    def finish(self):
        self.halt()
        was_focus = self.mode == "Focus"
        minutes, res = max(1, self.total // 60), None
        if was_focus:
            self.db.run("INSERT INTO focus_sessions(task,minutes,ended) VALUES(?,?,?)", (self.task.value or "", minutes, now_str()))
            res = self.app.game.on_session(minutes, self.task.value or "")
        self.stop_sound()
        if self.chime.value:
            self.play_chime()
        self.mode = "Short break" if was_focus else "Focus"
        self.total = self.left = self.mins[self.mode] * 60
        self.draw()
        if res:
            parts = ["Focus session complete!"]
            if res["coins"]:
                parts.append(f"+{res['coins']} ☀" + (f" (includes a {res['streak']}-day streak bonus)" if res["streak"] >= 2 else "") + ".")
            if res["planted"]:
                parts.append(f"A {res['kind'].lower()} just started growing in your garden.")
            elif res["stored"]:
                parts.append("Your garden is full - the new plant is waiting in your Bag.")
            elif minutes < GARDEN_MIN_MINUTES:
                parts.append(f"Sessions of {GARDEN_MIN_MINUTES}+ minutes plant something in your garden.")
            self.app.notice(" ".join(parts) + " Time for a break.", [("🌻 Open garden", lambda: self.app.show("garden"))])
            self.app.flush_notes()
        else:
            self.app.notice("Break over - ready to focus?")
        self.app.update()

    # -- sound
    def play_chime(self):
        def run():
            try:
                self.player.play(self.player.ensure("Chime"), min(1.0, self.volume.value / 100 + 0.25), loop=False)
            except Exception:  # noqa: BLE001 - a missing chime must never break the timer
                pass
        threading.Thread(target=run, daemon=True).start()

    def save_prefs(self, e=None):
        self.db.put("sound_auto", "1" if self.auto.value else "0")
        self.db.put("chime_on", "1" if self.chime.value else "0")

    def toggle_sound(self):
        self.stop_sound() if self.player.playing else self.play_sound()
        self.app.update()

    def play_sound(self):
        name = self.sound.value
        self.db.put("sound_choice", name or "Off")
        if name == "Off" or not name:
            self.snd_status.value = "Pick a sound first."
            return
        if not self.player.available():
            self.snd_status.value = "No audio player found. On Linux install paplay, aplay, ffplay or sox."
            return
        self.snd_status.value = "Starting..."

        def run():
            try:
                if name == self.MY_FILE:
                    path = (self.custom.value or "").strip()
                    if not path:
                        raise RuntimeError("Type the path to an audio file first.")
                    self.db.put("sound_file", path)
                else:
                    path = self.player.ensure(name)
                err = self.player.play(path, (self.volume.value or 0) / 100)
                self.now_playing = None if err else name
                self.snd_status.value = err or f"Playing: {name}"
                set_label(self.sound_b, "▶ Play" if err else "■ Stop")
            except Exception as ex:  # noqa: BLE001
                self.snd_status.value = f"Couldn't play: {ex}"
            self.app.update()
        threading.Thread(target=run, daemon=True).start()

    def stop_sound(self):
        self.player.stop()
        self.now_playing = None
        set_label(self.sound_b, "▶ Play")
        self.snd_status.value = ""

    def apply_volume(self, e=None):
        self.db.put("sound_volume", str(int(self.volume.value)))
        if self.player.playing and self.now_playing:
            self.play_sound()


def math_ceil(x):
    return int(x) if x == int(x) else int(x) + 1


# ---------------------------------------------------------------------------------------------
# Garden game
# ---------------------------------------------------------------------------------------------
EMOJI = {"daisy": "🌼", "tulip": "🌷", "mushroom": "🍄", "lavender": "🪻", "sunflower": "🌻", "berry": "🫐", "cactus": "🌵",
         "rose": "🌹", "pine": "🌲", "oak": "🌳", "hydrangea": "💮", "apple": "🍎", "cherry": "🌸", "bonsai": "🪴",
         "moonflower": "🌙", "golden_tulip": "🏵️", "grand_oak": "🌳", "lantern": "🏮", "bench": "🪑", "scarecrow": "🎃",
         "gnome": "🧙", "birdbath": "🐦", "pond": "🪷", "fountain": "⛲", "windmill": "🌾"}
WEATHER = {"Sunny": "☀️ Sunny", "Cloudy": "☁️ Cloudy", "Rainy": "🌧️ Rainy"}


class GardenPage(Pg):
    title, subtitle = "Garden", "Focus earns ☀. Spend it, plant, water, collect, and decorate"

    def build(self):
        self.game = self.app.game
        self.sub, self.plot, self.sel, self.placing = "Plot", 0, None, None
        self.header, self.tabs, self.body = ft.Column(spacing=6), ft.Row(spacing=6, wrap=True), ft.Column(spacing=10)
        self.view.controls = self.head() + [self.header, self.tabs, self.body]

    def go(self, sub):
        self.sub, self.sel = sub, None
        self.refresh()
        self.app.update()

    def refresh(self):
        g = self.game
        lvl = g.level()
        lo, hi = xp_for(lvl), xp_for(lvl + 1)
        self.header.controls = [row(txt(f"☀ {g.coins()}", 22, True), txt(f"Lv {lvl} · {level_title(lvl)}", 16, True, ACCENT),
                                    txt(WEATHER.get(g.weather(), ""), 14, color=MUTED), spacing=18),
                                row(bar((g.xp() - lo) / max(1, hi - lo), 320), txt(f"{g.xp() - lo}/{hi - lo} XP", 12, color=MUTED))]
        ready = g.ready_quests()
        names = ["Plot", "Shop", "Bag", "Quests" + (f" ({ready})" if ready else ""), "Badges"]
        self.tabs.controls = [chip(n, n.split(" ")[0] == self.sub, lambda e, n=n.split(" ")[0]: self.go(n)) for n in names]
        {"Plot": self.plot_view, "Shop": self.shop_view, "Bag": self.bag_view, "Quests": self.quest_view, "Badges": self.badge_view}[self.sub]()

    # -- plot
    def glyph(self, r):
        sid, sp = r["species"], species_of(r)
        if sp["cls"] == "deco":
            return txt(EMOJI.get(sid, "🪴"), 34), ""
        st = stage_of(r["growth"])
        face = ["🌰", "🌱", "🌿", EMOJI.get(sid, "🌳"), EMOJI.get(sid, "🌳")][st]
        size = [18, 22, 26, 30, 38][st]
        tag = ""
        if self.game.ready_hours(r) == 0:
            tag = "⭐"
        elif r["watered"] != date.today().isoformat() and r["growth"] < 100:
            tag = "💧"
        return txt(face, size), tag

    def plot_view(self):
        g = self.game
        taken = g.taken(self.plot)
        tiles = []
        for slot in range(GARDEN_PLOT):
            r = taken.get(slot)
            if r:
                gl, tag = self.glyph(r)
                inner = col(gl, txt(tag or (f"{r['growth']}%" if r["cls"] == "plant" else ""), 10, color=MUTED), spacing=0)
            else:
                inner = txt("＋" if self.placing else "", 18, color=MUTED)
            tiles.append(ft.Container(content=inner, width=80, height=80, border_radius=10, padding=6,
                                      bgcolor=SOFT if slot == self.sel else (GREENTILE if not r else CARD),
                                      on_click=lambda e, s=slot: self.tile(s)))
        grid = ft.Column([row(*tiles[i:i + GARDEN_COLS], spacing=8) for i in range(0, GARDEN_PLOT, GARDEN_COLS)], spacing=8)
        nav = row(btn("◀", lambda e: self.shift(-1), small=True), txt(f"Plot {self.plot + 1} of {g.plots()}", 14, True),
                  btn("▶", lambda e: self.shift(1), small=True), btn(f"Buy plot ({g.plot_cost()} ☀)", lambda e: self.buy_plot(), small=True),
                  btn("💧 Water all", lambda e: self.water_all(), small=True), btn("⭐ Collect all", lambda e: self.collect_all(), small=True))
        parts = [nav]
        if self.placing:
            parts.append(box(row(txt(f"Placing {SPECIES[self.placing]['name']} - click an empty tile", 14, True),
                                 btn("Cancel", lambda e: self.cancel_place(), small=True)), bg=SOFT))
        parts.append(grid)
        if self.sel is not None and self.sel in taken:
            parts.append(self.detail(taken[self.sel]))
        parts.append(txt("💧 thirsty · ⭐ ready to collect · water once a day to make plants grow", 12, color=MUTED))
        self.body.controls = parts

    def detail(self, r):
        sp = species_of(r)
        info = [txt(f"{sp['name']} · {sp['rare']}", 15, True)]
        if r["cls"] == "plant":
            info.append(txt(f"{STAGE_NAMES[stage_of(r['growth'])]} · {r['growth']}% grown · pays {sp['pay']} ☀ every {sp['cd']}h", 13, color=MUTED))
            rh = self.game.ready_hours(r)
            if rh:
                info.append(txt(f"Ready to collect again in {rh:.0f}h", 12, color=MUTED))
        acts = [btn("Remove (25% back)", lambda e, i=r["id"]: self.remove(i), small=True)]
        if r["cls"] == "plant":
            acts = [btn("💧 Water", lambda e, i=r["id"]: self.water(i), small=True), btn("⭐ Collect", lambda e, i=r["id"]: self.collect(i), small=True)] + acts
        return box(col(*info, row(*acts)))

    def tile(self, slot):
        taken = self.game.taken(self.plot)
        if self.placing and slot not in taken:
            ok, msg, _ = self.game.place(self.placing, self.plot, slot)
            self.app.toast(msg)
            if not ok or self.game.bag_qty(self.placing) < 1:
                self.placing = None
        else:
            self.sel = slot if slot in taken else None
        self.after_action()

    def after_action(self):
        self.app.flush_notes(redraw=False)
        self.refresh()
        self.app.update()

    def shift(self, d):
        self.plot = (self.plot + d) % self.game.plots()
        self.sel = None
        self.after_action()

    def buy_plot(self):
        ok, msg = self.game.buy_plot()
        self.app.toast(msg)
        self.after_action()

    def water(self, pid):
        ok, msg = self.game.water(pid)
        self.app.toast(msg)
        self.after_action()

    def collect(self, pid):
        ok, msg, _ = self.game.harvest(pid)
        self.app.toast(msg)
        self.after_action()

    def water_all(self):
        self.app.toast(f"Watered {self.game.water_all()} plants")
        self.after_action()

    def collect_all(self):
        n, total = self.game.harvest_all()
        self.app.toast(f"Collected from {n} plants: +{total} ☀" if n else "Nothing is ready yet")
        self.after_action()

    def remove(self, pid):
        self.app.toast(f"Removed - refunded {self.game.remove(pid)} ☀")
        self.sel = None
        self.after_action()

    def cancel_place(self):
        self.placing = None
        self.after_action()

    # -- shop / bag
    def shop_view(self):
        g, lvl, rows = self.game, self.game.level(), []
        for sid, sp in sorted(SPECIES.items(), key=lambda kv: (kv[1]["lvl"], kv[1]["cost"])):
            if not sp["shop"]:
                continue
            locked = lvl < sp["lvl"]
            can = not locked and g.coins() >= sp["cost"]
            rows.append(box(row(txt(EMOJI.get(sid, "🌱"), 26), ft.Container(content=col(txt(sp["name"], 15, True), txt(
                f"{sp['rare']} · Lv {sp['lvl']}" + ("" if sp["cls"] == "deco" else f" · pays {sp['pay']} ☀ / {sp['cd']}h"), 12, color=MUTED), spacing=0), expand=True),
                               txt(f"{sp['cost']} ☀", 15, True), btn("Buy", lambda e, s=sid: self.buy(s, False), small=True, disabled=not can),
                               btn("Buy & place", lambda e, s=sid: self.buy(s, True), small=True, disabled=not can)) if not locked else
                            row(txt("🔒", 26), ft.Container(content=txt(f"{sp['name']} - unlocks at level {sp['lvl']}", 14, color=MUTED), expand=True)), pad=8))
        self.body.controls = rows

    def buy(self, sid, place):
        ok, msg = self.game.buy(sid)
        self.app.toast(msg)
        if ok and place:
            self.placing, self.sub = sid, "Plot"
        self.after_action()

    def bag_view(self):
        items = self.game.bag()
        self.body.controls = [box(row(txt(EMOJI.get(s, "🌱"), 26), ft.Container(content=txt(f"{SPECIES[s]['name']}  ×{q}", 15), expand=True),
                                      btn("Place", lambda e, s=s: self.start_place(s), small=True)), pad=8) for s, q in items
                              if s in SPECIES] or [txt("Your bag is empty - visit the shop.", color=MUTED)]

    def start_place(self, sid):
        self.placing, self.sub = sid, "Plot"
        self.after_action()

    # -- quests / badges
    def quest_view(self):
        out = []
        for q in self.game.quests():
            state = btn("Claimed ✔", None, small=True, disabled=True) if q["claimed"] else btn(
                f"Claim +{q['reward']} ☀", lambda e, i=q["id"]: self.claim(i), True, True, disabled=not q["done"])
            out.append(box(row(ft.Container(content=col(txt(q["title"], 15, True), bar(q["progress"] / q["target"], 280), spacing=4), expand=True),
                               txt(f"{min(q['progress'], q['target'])}/{q['target']}", 13, color=MUTED), state)))
        self.body.controls = out + [txt("Finish all three for a +40 ☀ bonus. New quests every day.", 12, color=MUTED)]

    def claim(self, qid):
        self.game.claim(qid)
        self.after_action()

    def badge_view(self):
        have = self.game.unlocked()
        self.body.controls = [box(row(txt("🏅" if a[0] in have else "🔒", 22), ft.Container(content=col(txt(a[1], 15, True), txt(a[2], 12, color=MUTED), spacing=0), expand=True),
                                      txt(f"+{a[3]} ☀", 13, color=OK if a[0] in have else MUTED)), pad=8, opacity=1.0 if a[0] in have else 0.6)
                              for a in ACHIEVEMENTS]


# ---------------------------------------------------------------------------------------------
# AI Tutor
# ---------------------------------------------------------------------------------------------
class TutorPage(Pg):
    title, subtitle = "AI Tutor", "Answers stream in live. The Tutor can add tasks, reminders, notes and flashcards for you"
    SUBJECTS = ["General", "Mathematics", "Physics", "Chemistry", "Biology", "Computer Science", "History", "Languages",
                "Literature", "Economics", "Law", "Medicine (study)"]
    LEVELS = ["Beginner", "Intermediate", "Advanced"]
    MODES = {"Explain": "Explain concepts clearly with examples and analogies, then check understanding.",
             "Socratic": "Guide the student with questions rather than giving answers directly. Give hints first and only reveal the full answer if they are stuck.",
             "Quiz me": "Quiz the student one question at a time, wait for their answer, then give feedback and the next question.",
             "ELI5": "Explain everything as simply as possible, as if to a curious ten-year-old."}
    CHIPS = [("Simpler", "Explain that more simply, with an everyday analogy."), ("Example", "Give me a worked example."),
             ("Quiz me", "Quiz me on what we just covered, one question at a time."), ("Summarize", "Summarize the key points as short bullets."),
             ("Plan my day", "Look at my tasks, reminders and flashcards due, then suggest a realistic plan for today. Offer to schedule anything that needs a reminder."),
             ("Quiz my notes", "Use read_notes to look at my notes, then quiz me on one of them, one question at a time.")]

    def build(self):
        self.busy, self.cancel, self.reply, self.cur_md, self._last = False, False, "", None, 0.0
        self.subject = dropdown("Subject", self.SUBJECTS, self.db.get("t_subject", "General"), 190)
        self.level = dropdown("Level", self.LEVELS, self.db.get("t_level", "Intermediate"), 160)
        self.mode = dropdown("Style", list(self.MODES), self.db.get("t_mode", "Explain"), 140)
        self.ctx = ft.Checkbox(label="Use my tasks as context", value=False)
        self.chat = ft.ListView(expand=True, spacing=10, auto_scroll=True)
        self.input = mk(ft.TextField, hint_text="Ask anything...", multiline=True, min_lines=1, max_lines=5, expand=True,
                        on_submit=lambda e: self.send(), optional={"shift_enter": True})
        self.send_b = btn("Send", lambda e: self.send_or_stop(), True)
        self.history = [{"role": r["role"], "content": r["content"]} for r in
                        self.db.rows("SELECT role,content FROM chat ORDER BY id DESC LIMIT 40")][::-1]
        self.chat.controls = [self.sys_msg("Welcome! Pick a subject, level and style, then ask me anything. "
                                           "Add your API key under Settings first.")] + [self.bubble(m["role"], m["content"]) for m in self.history]
        chips = ft.Row([btn(l, lambda e, p=p: self.send(p), small=True) for l, p in self.CHIPS], wrap=True, spacing=6)
        self.view = ft.Column(expand=True, spacing=10)
        self.view.controls = self.head() + [row(self.subject, self.level, self.mode, self.ctx, btn("Clear", lambda e: self.clear(), small=True), wrap=True),
                                            ft.Container(self.chat, expand=True, padding=6, border_radius=12, bgcolor=FAINT), chips,
                                            row(self.input, self.send_b, vertical_alignment=ft.CrossAxisAlignment.END),
                                            row(btn("Save last reply as note", lambda e: self.save_note(), small=True),
                                                btn("Make flashcards from it", lambda e: self.make_cards(), small=True))]

    def sys_msg(self, text):
        return txt(text, 12, color=MUTED)

    def bubble(self, role, text):
        if role == "user":
            return ft.Container(content=ft.Text(text, selectable=True), padding=12, border_radius=12, bgcolor=SOFT)
        body = mk(ft.Markdown, text, selectable=True)
        return ft.Container(content=body, padding=12, border_radius=12, bgcolor=CARD)

    def clear(self):
        if self.busy:
            return
        self.history.clear()
        self.db.run("DELETE FROM chat")
        self.chat.controls = [self.sys_msg("Chat cleared.")]
        self.app.update()

    def system_prompt(self):
        p = (f"You are an expert, patient tutor helping a student with {self.subject.value} at a {(self.level.value or 'Intermediate').lower()} level. "
             f"{self.MODES[self.mode.value or 'Explain']} Be accurate and concise. Use short paragraphs; you may use **bold**, `code`, and '- ' bullets. "
             "If the student seems confused, simplify. If you are unsure of something, say so rather than guessing.")
        if self.ctx.value:
            tasks = [r["title"] for r in self.db.rows("SELECT title FROM todos WHERE done=0 ORDER BY id DESC LIMIT 10")]
            if tasks:
                p += " The student's open tasks: " + "; ".join(tasks) + "."
        if self.db.get("t_tools", "On") == "On":
            now = datetime.now()
            p += (f" Today is {now:%A %d %B %Y}, {now:%H:%M}. You can manage the student's Lumen workspace with tools: add tasks, set reminders, "
                  "save notes, create flashcards, list items, read notes and complete tasks. Use them when the student asks you to add, schedule, "
                  "save or look something up, or when seeing their tasks or notes would help you teach. Never claim something was done unless a "
                  "tool call succeeded. After using tools, confirm briefly in plain words.")
        return p

    def send_or_stop(self):
        if self.busy:
            self.cancel = True
        else:
            self.send()

    def send(self, text=None):
        if self.busy:
            return
        text = (text or self.input.value or "").strip()
        if not text:
            return
        if not self.app.api_key():
            self.chat.controls.append(self.sys_msg("No API key found. Open Settings to add one, or set ANTHROPIC_API_KEY."))
            self.app.update()
            return
        for k, v in (("t_subject", self.subject), ("t_level", self.level), ("t_mode", self.mode)):
            self.db.put(k, v.value or "")
        self.input.value = ""
        self.history.append({"role": "user", "content": text})
        self.db.run("INSERT INTO chat(role,content,ts) VALUES('user',?,?)", (text, now_str()))
        self.chat.controls.append(self.bubble("user", text))
        self.cur_md = mk(ft.Markdown, "…", selectable=True)
        self.chat.controls.append(ft.Container(content=self.cur_md, padding=12, border_radius=12, bgcolor=CARD))
        self.busy, self.cancel, self.reply = True, False, ""
        set_label(self.send_b, "Stop")
        self.app.update()
        msgs = self.history[-20:]
        while msgs and msgs[0]["role"] != "user":
            msgs = msgs[1:]
        tools = TOOLS if self.db.get("t_tools", "On") == "On" else None
        threading.Thread(target=self.worker, args=(self.app.api_key(), self.app.model(), self.system_prompt(), [dict(m) for m in msgs], tools), daemon=True).start()

    def on_chunk(self, t):
        self.reply += t
        self.cur_md.value = self.reply
        if time.monotonic() - self._last > 0.1:
            self._last = time.monotonic()
            self.app.update()

    def worker(self, key, model, system, msgs, tools):
        err = None
        try:
            turns = 0
            while True:
                turns += 1
                res = stream_turn(key, model, system, msgs, tools, self.on_chunk, lambda: self.cancel)
                blocks = res["content"]
                calls = [b for b in blocks if b.get("type") == "tool_use"]
                if calls and res["stop_reason"] == "tool_use" and not self.cancel and turns < 6:
                    msgs.append({"role": "assistant", "content": blocks})
                    results = []
                    for c in calls:
                        out, notice, is_err = execute_tool(self.db, c["name"], c.get("input"))
                        item = {"type": "tool_result", "tool_use_id": c["id"], "content": out}
                        if is_err:
                            item["is_error"] = True
                        results.append(item)
                        if notice:
                            self.on_chunk(("\n\n" if self.reply else "") + notice + "\n\n")
                    msgs.append({"role": "user", "content": results})
                    continue
                break
        except Exception as e:  # noqa: BLE001
            err = str(e)
        self.finish(err)

    def finish(self, error):
        full = self.reply.strip()
        if full:
            self.cur_md.value = full + ("\n\n*(stopped)*" if self.cancel else "")
            self.history.append({"role": "assistant", "content": full})
            self.db.run("INSERT INTO chat(role,content,ts) VALUES('assistant',?,?)", (full, now_str()))
        else:
            self.chat.controls = self.chat.controls[:-2]
            if self.history and self.history[-1]["role"] == "user":
                self.history.pop()
                self.db.run("DELETE FROM chat WHERE id=(SELECT MAX(id) FROM chat)")
        if error:
            self.chat.controls.append(txt(error, 13, color=DANGER))
        self.busy = False
        set_label(self.send_b, "Send")
        self.app.update()
        self.app.refresh_current()

    def save_note(self):
        last = next((m for m in reversed(self.history) if m["role"] == "assistant"), None)
        if not last:
            self.app.toast("No tutor reply to save yet.")
            return
        q = next((m["content"] for m in reversed(self.history) if m["role"] == "user"), "Tutor reply")
        self.db.run("INSERT INTO notes(title,body,tags,updated) VALUES(?,?,?,?)", ("Tutor: " + q.strip().split("\n")[0][:50], last["content"], "tutor", now_str()))
        self.app.toast("Saved to Notes")

    def make_cards(self):
        last = next((m for m in reversed(self.history) if m["role"] == "assistant"), None)
        if last:
            self.app.generate_cards(last["content"], "Tutor")
        else:
            self.app.toast("No tutor reply to turn into flashcards yet.")


# ---------------------------------------------------------------------------------------------
# Settings, backup
# ---------------------------------------------------------------------------------------------
class SettingsPage(Pg):
    title, subtitle = "Settings", "API key, theme and backups"

    def build(self):
        self.key = field("Anthropic API key (blank = use ANTHROPIC_API_KEY)", self.db.get("api_key"), password=True, can_reveal_password=True, width=520)
        self.model = field("Model", self.db.get("model", DEFAULT_MODEL), width=320)
        self.tools = ft.Switch(label="Let the Tutor add tasks, reminders, notes and flashcards for me", value=self.db.get("t_tools", "On") == "On")
        self.dark = ft.Switch(label="Dark mode", value=self.app.dark, on_change=lambda e: self.app.set_theme(e.control.value))
        self.imp = field("Path of a backup .json to import", width=520)
        self.out = txt("", 13, color=MUTED)
        self.view.controls = self.head() + [
            box(col(txt("AI Tutor", 16, True), self.key, self.model, self.tools, btn("Save", lambda e: self.save(), True))),
            box(col(txt("Appearance", 16, True), self.dark)),
            box(col(txt("Backup", 16, True), btn("Export backup now", lambda e: self.export()), self.imp,
                    btn("Import (merges, skips duplicates)", lambda e: self.import_()), self.out,
                    txt(f"Your data lives in {APP_DIR}", 12, color=MUTED)))]

    def save(self):
        self.db.put("api_key", (self.key.value or "").strip())
        self.db.put("model", (self.model.value or "").strip() or DEFAULT_MODEL)
        self.db.put("t_tools", "On" if self.tools.value else "Off")
        self.app.toast("Settings saved")

    def export(self):
        out = Path.home() / "Lumen" / "backups"
        out.mkdir(parents=True, exist_ok=True)
        path = out / f"lumen-backup-{datetime.now():%Y%m%d-%H%M}.json"
        path.write_text(json.dumps(self.db.export_all(), indent=2, ensure_ascii=False), encoding="utf-8")
        self.out.value = f"Saved: {path}"
        self.app.update()

    def import_(self):
        try:
            data = json.loads(Path((self.imp.value or "").strip().strip("'\"")).expanduser().read_text(encoding="utf-8"))
            added = self.db.import_all(data)
        except (OSError, ValueError) as e:
            self.out.value = f"Couldn't read that backup: {e}"
        else:
            self.out.value = f"Imported {added} new item{'s' if added != 1 else ''}"
        self.app.update()


# ---------------------------------------------------------------------------------------------
# Search
# ---------------------------------------------------------------------------------------------
class SearchPage(Pg):
    title, subtitle = "Search", ""

    def build(self):
        self.q = ""
        self.out = ft.Column(spacing=6)
        self.view.controls = self.head() + [self.out]

    def run(self, q):
        self.q = q.strip()
        self.refresh()

    def refresh(self):
        like, db, out = f"%{self.q}%", self.db, []
        self.subtitle_txt = f"Results for '{self.q}'"

        def line(icon, text, page, nid=None):
            return box(txt(f"{icon}  {text}", 14), pad=10, on_click=lambda e: self.app.open_result(page, nid))
        for r in db.rows("SELECT id,title FROM todos WHERE title LIKE ? LIMIT 15", (like,)):
            out.append(line("✅", r["title"], "todo"))
        for r in db.rows("SELECT id,title,body FROM notes WHERE title LIKE ? OR body LIKE ? OR tags LIKE ? LIMIT 15", (like, like, like)):
            out.append(line("📝", r["title"] or "Untitled", "notes", r["id"]))
        for r in db.rows("SELECT title,due FROM reminders WHERE title LIKE ? LIMIT 15", (like,)):
            out.append(line("⏰", f"{r['title']}  ({r['due']})", "reminders"))
        for r in db.rows("SELECT front FROM cards WHERE front LIKE ? OR back LIKE ? LIMIT 15", (like, like)):
            out.append(line("🧠", r["front"], "cards"))
        self.out.controls = [txt(f"Results for '{self.q}'", 15, True)] + (out or [txt("Nothing found.", color=MUTED)])


# ---------------------------------------------------------------------------------------------
# App shell
# ---------------------------------------------------------------------------------------------
class App:
    def __init__(self, page):
        self.page = page
        self.db = Database()
        self.dark = self.db.get("theme", "dark") == "dark"
        self.player = SoundPlayer()
        self.seed_if_new()
        garden_backfill(self.db)
        self.game = GardenGame(self.db)
        self.game.start()
        page.title = "Lumen"
        page.padding = 0
        page.theme_mode = ft.ThemeMode.DARK if self.dark else ft.ThemeMode.LIGHT
        try:
            page.theme = ft.Theme(color_scheme_seed=ACCENT)
            page.dark_theme = ft.Theme(color_scheme_seed=ACCENT)
        except Exception:  # noqa: BLE001 - theming is cosmetic
            pass
        self.pages = {"dash": DashPage(self), "todo": TodoPage(self), "notes": NotesPage(self), "reminders": RemindersPage(self),
                      "calendar": CalendarPage(self), "cards": CardsPage(self), "focus": FocusPage(self), "garden": GardenPage(self),
                      "tutor": TutorPage(self), "settings": SettingsPage(self), "search": SearchPage(self)}
        self.focus = self.pages["focus"]
        self.current, self._toast_id = "dash", 0
        self.nav_items = {}
        for key, icon, name in NAV:
            self.nav_items[key] = ft.Container(content=txt(f"{icon}  {name}", 15), padding=11, border_radius=10, on_click=lambda e, k=key: self.show(k))
        self.quick = field("Quick add:  buy milk fri !high   |   remind me to stretch in 30m   |   note: Title | body   |   card: Q | A",
                           expand=True, on_submit=self.quick_add, dense=True)
        self.search = field("Search", width=240, on_submit=self.do_search, dense=True)
        self.notices = ft.Column(spacing=6)
        self.body = ft.Container(expand=True, padding=24)
        self.toast_row = ft.Row(spacing=12)
        self.toast_box = ft.Container(content=self.toast_row, padding=12, border_radius=10, bgcolor=DARKTOAST, visible=False)
        side = ft.Container(content=col(txt("✦ Lumen", 22, True, ACCENT), ft.Divider(), *self.nav_items.values(), spacing=4),
                            width=200, padding=16, bgcolor=FAINT)
        main = ft.Column([row(self.quick, self.search, ft.Container(content=txt("🌓"), padding=8, on_click=lambda e: self.set_theme(not self.dark))),
                          self.notices, self.body, self.toast_box], expand=True, spacing=10)
        page.add(ft.Row([side, ft.Container(content=main, expand=True, padding=18)], expand=True, spacing=0))
        self.show("dash")
        self.flush_notes()
        page.run_task(self.background)

    # -- plumbing
    def update(self):
        try:
            self.page.update()
        except Exception:  # noqa: BLE001 - a late update after closing must not crash a worker thread
            pass

    def api_key(self):
        return self.db.get("api_key") or os.environ.get("ANTHROPIC_API_KEY", "")

    def model(self):
        return self.db.get("model", DEFAULT_MODEL) or DEFAULT_MODEL

    def show(self, key):
        self.current = key
        pg = self.pages[key]
        pg.refresh()
        self.body.content = pg.view
        for k, item in self.nav_items.items():
            item.bgcolor = SOFT if k == key else None
        self.update()

    def refresh_current(self):
        self.pages[self.current].refresh()
        self.update()

    def set_theme(self, dark):
        self.dark = bool(dark)
        self.db.put("theme", "dark" if self.dark else "light")
        self.page.theme_mode = ft.ThemeMode.DARK if self.dark else ft.ThemeMode.LIGHT
        self.pages["settings"].dark.value = self.dark
        self.update()

    def toast(self, text, undo=None, ms=5000):
        self._toast_id += 1
        me = self._toast_id
        ctrls = [txt(text, 14, color="#ffffff")]
        if undo:
            def do_undo(e):
                self.toast_box.visible = False
                undo()
                self.update()
            ctrls.append(ft.Container(content=txt("Undo", 14, True, "#93c5fd"), on_click=do_undo, padding=4))
        self.toast_row.controls, self.toast_box.visible = ctrls, True
        self.update()

        def hide():
            if self._toast_id == me:
                self.toast_box.visible = False
                self.update()
        t = threading.Timer(ms / 1000, hide)
        t.daemon = True
        t.start()

    def notice(self, text, actions=()):
        box_ = ft.Container(padding=12, border_radius=10, bgcolor=SOFT)

        def close(e=None):
            if box_ in self.notices.controls:
                self.notices.controls.remove(box_)
            self.update()
        btns = [btn(label, lambda e, fn=fn: (fn(), close()), small=True) for label, fn in actions] + [btn("Dismiss", close, small=True)]
        box_.content = row(ft.Container(content=txt(text, 14), expand=True), *btns)
        self.notices.controls.append(box_)
        self.update()

    def flush_notes(self, redraw=True):
        if self.game.notes:
            self.toast("   ·   ".join(self.game.notes), ms=7000)
            self.game.notes.clear()

    def focus_on(self, task):
        self.show("focus")
        self.focus.task.value = task
        self.focus.set_mode("Focus")
        if not self.focus.running:
            self.focus.begin()
            self.update()

    def open_result(self, page, nid):
        self.show(page)
        if page == "notes" and nid:
            self.pages["notes"].open(nid)

    def do_search(self, e=None):
        q = (self.search.value or "").strip()
        if q:
            self.pages["search"].run(q)
            self.show("search")

    def quick_add(self, e=None):
        d = parse_quick(self.quick.value or "")
        if not d:
            return
        k = d["kind"]
        if k == "reminder" and not d["due"]:
            self.toast("Add a time to the reminder, e.g. 'in 30m' or 'tomorrow 9am'")
            return
        if k == "task":
            self.db.run("INSERT INTO todos(title,priority,due,created) VALUES(?,?,?,?)", (d["title"], d["priority"], d["due"], now_str()))
            msg = "Task added"
        elif k == "reminder":
            self.db.run("INSERT INTO reminders(title,due,repeat) VALUES(?,?,'Once')", (d["title"], d["due"]))
            msg = f"Reminder set for {d['due']}"
        elif k == "note":
            self.db.run("INSERT INTO notes(title,body,tags,pinned,updated) VALUES(?,?,'',0,?)", (d["title"], d["body"], now_str()))
            msg = "Note saved"
        else:
            self.db.run("INSERT INTO cards(deck,front,back,due,created) VALUES('Quick',?,?,?,?)", (d["front"], d["back"], date.today().isoformat(), now_str()))
            msg = "Flashcard added"
        self.quick.value = ""
        self.toast(msg)
        self.refresh_current()

    def generate_cards(self, source, deck, n=10):
        key, model, source = self.api_key(), self.model(), (source or "").strip()[:12000]
        if not source:
            self.toast("Type a topic or paste some notes first.")
            return
        if not key:
            self.toast("Add your API key in Settings first.")
            return
        self.toast("Generating flashcards...", ms=4000)
        system = "You write excellent study flashcards. Reply with ONLY a JSON array - no prose, no code fences."
        prompt = (f"Create {n} flashcards for a deck called '{deck}' from the material or topic below. "
                  'Each item must be {"front": a clear question or prompt, "back": a concise answer}. '
                  "Keep each side under 40 words, cover the most important ideas, and avoid duplicates.\n\n" + source)

        def work():
            try:
                cards = parse_cards(ask_claude(key, model, system, prompt))
                err = None if cards else "The reply had no usable cards."
            except Exception as e:  # noqa: BLE001
                cards, err = [], str(e)
            if err:
                self.toast("Couldn't generate cards. " + err, ms=7000)
                return
            today = date.today().isoformat()
            for f, b in cards[:30]:
                self.db.run("INSERT INTO cards(deck,front,back,due,created) VALUES(?,?,?,?,?)", (deck, f, b, today, now_str()))
            self.toast(f"Added {min(len(cards), 30)} cards to '{deck}'")
            if self.current == "cards":
                self.refresh_current()
        threading.Thread(target=work, daemon=True).start()

    def seed_if_new(self):
        if self.db.get("seeded"):
            return
        self.db.put("seeded", "1")
        if any(self.db.one(f"SELECT COUNT(*) FROM {t}") for t in ("todos", "notes", "reminders", "cards")):
            return
        self.db.run("INSERT INTO notes(title,body,tags,pinned,updated) VALUES(?,?,?,1,?)", ("Welcome to Lumen", (
            "Quick tour\n\n- The top bar adds anything in one line: buy milk fri !high  or  remind me to stretch in 30m\n"
            "- Flashcards: generate a deck from any note with 'Make flashcards'\n"
            "- Focus sessions earn sun (☀) and grow your garden\n"
            "- AI Tutor: add your API key under Settings, then ask it to 'plan my day'.\n\nDelete this note whenever you like."), "welcome", now_str()))
        self.db.run("INSERT INTO todos(title,priority,due,created) VALUES(?,?,?,?)", ("Try Lumen: add a task from the top bar", "Medium", "", now_str()))

    # -- background loop: focus timer + reminder alerts
    async def background(self):
        last_check = 0.0
        while True:
            await asyncio.sleep(0.5)
            try:
                self.focus.tick()
                if time.monotonic() - last_check > 15:
                    last_check = time.monotonic()
                    self.check_reminders()
            except Exception:  # noqa: BLE001 - never let the loop die
                pass

    def check_reminders(self):
        for r in self.db.rows("SELECT * FROM reminders WHERE done=0 AND notified=0 AND due<=?", (now_str(),)):
            self.db.run("UPDATE reminders SET notified=1 WHERE id=?", (r["id"],))
            rep = r["repeat"] or "Once"
            if rep != "Once":
                self.db.run("INSERT INTO reminders(title,due,repeat) VALUES(?,?,?)", (r["title"], next_occurrence(r["due"], rep), rep))
            rid = r["id"]

            def snooze(mins, rid=rid):
                self.db.run("UPDATE reminders SET due=?, notified=0 WHERE id=?", ((datetime.now() + timedelta(minutes=mins)).strftime(FMT), rid))
            self.notice(f"⏰ Reminder: {r['title']}  (due {r['due']})",
                        [("Done", lambda rid=rid: self.db.run("UPDATE reminders SET done=1 WHERE id=?", (rid,))),
                         ("Snooze 10m", lambda s=snooze: s(10)), ("Snooze 1h", lambda s=snooze: s(60))])
            self.focus.play_chime()
            if self.current == "reminders":
                self.refresh_current()


def main(page: ft.Page):
    App(page)


if __name__ == "__main__":
    (getattr(ft, "run", None) or ft.app)(main)
