"""
lumen_core.py - the logic behind Lumen (no UI): database, date parsing, spaced repetition,
Claude API + agent tools, ambient sounds and the garden game rules.
Used by lumen_flet.py. Standard library only.
"""
import array, calendar, json, math, os, queue, random, re, shutil, sqlite3, subprocess, sys, threading, time
import urllib.error, urllib.request, wave
from datetime import datetime, timedelta
from pathlib import Path


APP_NAME = "Lumen"
APP_DIR = Path.home() / ".lumen"
DB_PATH = APP_DIR / "lumen.db"
OLD_DB_PATH = Path.home() / ".focushub" / "focushub.db"
DEFAULT_MODEL = "claude-sonnet-5-5"
API_URL = "https://api.anthropic.com/v1/messages"
FMT = "%Y-%m-%d %H:%M"


def now_str():
    return datetime.now().strftime(FMT)


WEEKNAMES = {"mon": 0, "monday": 0, "tue": 1, "tues": 1, "tuesday": 1, "wed": 2, "weds": 2, "wednesday": 2,
             "thu": 3, "thur": 3, "thurs": 3, "thursday": 3, "fri": 4, "friday": 4, "sat": 5, "saturday": 5,
             "sun": 6, "sunday": 6}


def parse_date(text):
    """'today', 'tomorrow', '+3d', 'fri', '2026-12-31' -> 'YYYY-MM-DD' or None."""
    s = text.strip().lower()
    today = datetime.now().date()
    if s == "today":
        return today.isoformat()
    if s == "tomorrow":
        return (today + timedelta(days=1)).isoformat()
    m = re.fullmatch(r"\+(\d+)d?", s)
    if m:
        return (today + timedelta(days=int(m.group(1)))).isoformat()
    if s in WEEKNAMES:
        delta = (WEEKNAMES[s] - today.weekday()) % 7 or 7
        return (today + timedelta(days=delta)).isoformat()
    try:
        return datetime.strptime(s, "%Y-%m-%d").date().isoformat()
    except ValueError:
        return None


def _parse_time(s):
    m = re.fullmatch(r"(\d{1,2})(?::(\d{2}))?\s*(am|pm)?", s.strip())
    if not m:
        return None
    h, mi, ap = int(m.group(1)), int(m.group(2) or 0), m.group(3)
    if ap == "pm" and h < 12:
        h += 12
    if ap == "am" and h == 12:
        h = 0
    return (h, mi) if h < 24 and mi < 60 else None


def parse_when(text):
    """'in 30m', 'in 2h', 'tomorrow 9am', 'fri 14:30', '2026-12-31 18:00', '17:30'."""
    s = text.strip().lower()
    now = datetime.now().replace(second=0, microsecond=0)
    m = re.fullmatch(r"in\s+(\d+)\s*(m|min|mins|minutes?|h|hr|hrs|hours?|d|days?)", s)
    if m:
        n, u = int(m.group(1)), m.group(2)[0]
        return now + (timedelta(minutes=n) if u == "m" else timedelta(hours=n) if u == "h" else timedelta(days=n))
    day, rest = None, s
    m = re.match(r"(today|tomorrow|\+\d+d?|\d{4}-\d{2}-\d{2}|[a-z]{3,9})\s*(.*)", s)
    if m and not re.fullmatch(r"\d.*", m.group(1)):
        d = parse_date(m.group(1))
        if d:
            day, rest = d, m.group(2)
    elif m:
        day, rest = parse_date(m.group(1)), m.group(2)
    t = _parse_time(rest) if rest else None
    if rest and t is None:
        return None
    if day is None and t is None:
        return None
    if day is None:
        cand = now.replace(hour=t[0], minute=t[1])
        return cand if cand > now else cand + timedelta(days=1)
    base = datetime.strptime(day, "%Y-%m-%d")
    h, mi = t if t else (9, 0)
    return base.replace(hour=h, minute=mi)


def next_occurrence(due_str, repeat):
    d = datetime.strptime(due_str, FMT)
    now = datetime.now()
    while d <= now:
        if repeat == "Daily":
            d += timedelta(days=1)
        elif repeat == "Weekly":
            d += timedelta(days=7)
        elif repeat == "Monthly":
            y, mth = d.year + d.month // 12, d.month % 12 + 1
            d = d.replace(year=y, month=mth, day=min(d.day, calendar.monthrange(y, mth)[1]))
        else:
            break
    return d.strftime(FMT)


def stream_turn(api_key, model, system, messages, tools, on_chunk, cancelled, max_tokens=2000):
    """One streamed Messages API call. Returns {"content": [blocks], "stop_reason": str|None}."""
    payload = {"model": model, "max_tokens": max_tokens, "system": system, "messages": messages, "stream": True}
    if tools:
        payload["tools"] = tools
    req = urllib.request.Request(API_URL, data=json.dumps(payload).encode("utf-8"), headers={
        "content-type": "application/json", "x-api-key": api_key, "anthropic-version": "2023-06-01"})
    blocks, stop = {}, None
    try:
        with urllib.request.urlopen(req, timeout=90) as resp:
            for raw in resp:
                if cancelled():
                    break
                line = raw.decode("utf-8", "ignore").strip()
                if not line.startswith("data:"):
                    continue
                data = line[5:].strip()
                if not data or data == "[DONE]":
                    continue
                try:
                    ev = json.loads(data)
                except ValueError:
                    continue
                kind = ev.get("type")
                if kind == "content_block_start":
                    cb = ev.get("content_block", {})
                    blocks[ev.get("index", 0)] = {"type": cb.get("type"), "id": cb.get("id"),
                                                  "name": cb.get("name"), "text": "", "json": ""}
                elif kind == "content_block_delta":
                    d, b = ev.get("delta", {}), blocks.get(ev.get("index", 0))
                    if b is None:
                        continue
                    if d.get("type") == "text_delta":
                        b["text"] += d.get("text", "")
                        on_chunk(d.get("text", ""))
                    elif d.get("type") == "input_json_delta":
                        b["json"] += d.get("partial_json", "")
                elif kind == "message_delta":
                    stop = ev.get("delta", {}).get("stop_reason") or stop
                elif kind == "error":
                    raise RuntimeError(ev.get("error", {}).get("message", "Stream error"))
    except urllib.error.HTTPError as e:
        try:
            detail = json.load(e).get("error", {}).get("message", "")
        except Exception:
            detail = ""
        raise RuntimeError(f"API error {e.code}: {detail or e.reason}")
    except urllib.error.URLError as e:
        raise RuntimeError(f"Network error: {e.reason}")
    content = []
    for i in sorted(blocks):
        b = blocks[i]
        if b["type"] == "text" and b["text"]:
            content.append({"type": "text", "text": b["text"]})
        elif b["type"] == "tool_use":
            try:
                args = json.loads(b["json"]) if b["json"] else {}
            except ValueError:
                continue
            content.append({"type": "tool_use", "id": b["id"], "name": b["name"], "input": args})
    return {"content": content, "stop_reason": stop}


def stream_claude(api_key, model, system, messages, on_chunk, cancelled, max_tokens=1500):
    """Plain streamed text completion (no tools)."""
    stream_turn(api_key, model, system, messages, None, on_chunk, cancelled, max_tokens)


def split_when(text):
    """Pull a time phrase off the end (or start) of text -> (rest, datetime) or (text, None)."""
    words = text.split()
    for k in range(min(4, len(words)), 0, -1):
        for cand, rest in ((words[-k:], words[:-k]), (words[:k], words[k:])):
            c = cand[:]
            if len(c) > 1 and c[0].lower() in ("at", "on", "by", "for"):
                c = c[1:]
            phrase = " ".join(c)
            if re.fullmatch(r"\d+", phrase):
                continue  # a bare number is probably part of the title
            dt = parse_when(phrase)
            if dt:
                return " ".join(rest), dt
    return text, None


def parse_quick(text):
    """Natural-language quick add -> dict(kind=task|reminder|note|card, ...) or None."""
    t = text.strip()
    if not t:
        return None
    m = re.match(r"(?i)(?:remind(?:\s+me)?(?:\s+to)?\s+|r:\s*)(.+)$", t)
    if m:
        title, dt = split_when(m.group(1).strip())
        title = re.sub(r"(?i)^to\s+", "", title.strip())
        return {"kind": "reminder", "title": title or "Reminder",
                "due": dt.strftime(FMT) if dt else None}
    m = re.match(r"(?i)(?:note|n):\s*(.+)$", t)
    if m:
        title, _, body = m.group(1).partition("|")
        return {"kind": "note", "title": title.strip() or "Untitled", "body": body.strip()}
    m = re.match(r"(?i)(?:card|c):\s*(.+?)\s*\|\s*(.+)$", t)
    if m:
        return {"kind": "card", "front": m.group(1).strip(), "back": m.group(2).strip()}
    prio, due, words = "Medium", "", []
    for w in re.sub(r"(?i)^(?:todo|task|t):\s*", "", t).split():
        pm = re.fullmatch(r"!(high|h|med|medium|m|low|l)", w.lower())
        if pm:
            prio = {"h": "High", "m": "Medium", "l": "Low"}[pm.group(1)[0]]
        else:
            words.append(w)
    if len(words) > 1:
        d = parse_date(words[-1])
        if d:
            due = d
            words.pop()
            if words and words[-1].lower() in ("by", "due", "on", "until"):
                words.pop()
    title = " ".join(words).strip()
    return {"kind": "task", "title": title, "priority": prio, "due": due} if title else None


def sm2(ease, interval, reps, grade):
    """Spaced repetition (SM-2 style). grade 0=again 1=hard 2=good 3=easy -> (ease, days, reps)."""
    if grade == 0:
        return max(1.3, ease - 0.2), 0, 0
    if reps == 0:
        days = {1: 1, 2: 1, 3: 3}[grade]
    elif reps == 1:
        days = {1: 2, 2: 3, 3: 5}[grade]
    else:
        mult = {1: 1.2, 2: ease, 3: ease * 1.3}[grade]
        days = max(interval + 1, round(interval * mult))
    return max(1.3, ease + {1: -0.15, 2: 0.0, 3: 0.15}[grade]), days, reps + 1


def parse_cards(text):
    """Extract [(front, back), ...] from a model reply containing a JSON array."""
    a, b = text.find("["), text.rfind("]")
    if a < 0 or b <= a:
        return []
    try:
        data = json.loads(text[a:b + 1])
    except ValueError:
        return []
    out = []
    for d in data if isinstance(data, list) else []:
        if isinstance(d, dict):
            f, bk = str(d.get("front", "")).strip(), str(d.get("back", "")).strip()
            if f and bk:
                out.append((f, bk))
    return out


def ask_claude(api_key, model, system, prompt, max_tokens=3000):
    parts = []
    stream_claude(api_key, model, system, [{"role": "user", "content": prompt}],
                  parts.append, lambda: False, max_tokens)
    return "".join(parts)


def _obj(props, required=()):
    return {"type": "object", "properties": props, "required": list(required)}


TOOLS = [
    {"name": "add_task", "description": "Add an item to the student's to-do list.",
     "input_schema": _obj({"title": {"type": "string"},
                           "priority": {"type": "string", "enum": ["High", "Medium", "Low"]},
                           "due": {"type": "string", "description": "today, tomorrow, +3d, a weekday name, or YYYY-MM-DD"}},
                          ["title"])},
    {"name": "complete_task", "description": "Mark one open task as done, matched by part of its title.",
     "input_schema": _obj({"title_contains": {"type": "string"}}, ["title_contains"])},
    {"name": "add_reminder", "description": "Schedule a reminder that pops up at the given time.",
     "input_schema": _obj({"title": {"type": "string"},
                           "when": {"type": "string", "description": "e.g. 'in 30m', 'tomorrow 9am', 'fri 14:30', '2026-12-31 18:00'"},
                           "repeat": {"type": "string", "enum": ["Once", "Daily", "Weekly", "Monthly"]}},
                          ["title", "when"])},
    {"name": "add_note", "description": "Save a note (for example a summary or study guide).",
     "input_schema": _obj({"title": {"type": "string"}, "body": {"type": "string"},
                           "tags": {"type": "string", "description": "comma-separated"}}, ["title", "body"])},
    {"name": "add_flashcards", "description": "Create spaced-repetition flashcards in a deck.",
     "input_schema": _obj({"deck": {"type": "string"},
                           "cards": {"type": "array", "items": _obj({"front": {"type": "string"}, "back": {"type": "string"}},
                                                                   ["front", "back"])}}, ["cards"])},
    {"name": "list_items", "description": "See what is in the student's workspace.",
     "input_schema": _obj({"kind": {"type": "string", "enum": ["tasks", "reminders", "notes", "cards_due"]}}, ["kind"])},
    {"name": "read_notes", "description": "Search the student's notes by keyword and return their text (max 3 notes). "
                                          "An empty query lists all note titles.",
     "input_schema": _obj({"query": {"type": "string"}})},
]


def execute_tool(db, name, args):
    """Run a tutor tool against the database -> (result_for_model, notice_for_user or None, is_error)."""
    args = args if isinstance(args, dict) else {}
    today = datetime.now().date().isoformat()
    try:
        if name == "add_task":
            title = str(args.get("title", "")).strip()
            if not title:
                return "Error: title is required.", None, True
            due = ""
            if args.get("due"):
                due = parse_date(str(args["due"]))
                if due is None:
                    return (f"Error: couldn't read the due date {args['due']!r}. Use today, tomorrow, +3d, "
                            "a weekday name, or YYYY-MM-DD."), None, True
            prio = args.get("priority") if args.get("priority") in ("High", "Medium", "Low") else "Medium"
            db.run("INSERT INTO todos(title,priority,due,created) VALUES(?,?,?,?)", (title, prio, due, now_str()))
            return (f"Added task '{title}' ({prio}" + (f", due {due}" if due else "") + ").",
                    f"✓ Added task: {title}" + (f"  (due {due})" if due else ""), False)
        if name == "complete_task":
            q = str(args.get("title_contains", "")).strip()
            rows = db.rows("SELECT id,title FROM todos WHERE done=0 AND title LIKE ? LIMIT 5", (f"%{q}%",)) if q else []
            if not rows:
                return f"No open task matches {q!r}.", None, True
            if len(rows) > 1:
                return "Several tasks match - ask which one: " + "; ".join(r["title"] for r in rows), None, False
            db.run("UPDATE todos SET done=1, done_at=? WHERE id=?", (now_str(), rows[0]["id"]))
            return f"Completed '{rows[0]['title']}'.", f"✓ Completed: {rows[0]['title']}", False
        if name == "add_reminder":
            title, dt = str(args.get("title", "")).strip(), parse_when(str(args.get("when", "")))
            if not title or dt is None:
                return "Error: need a title and a time Lumen can read (e.g. 'in 30m', 'tomorrow 9am').", None, True
            rep = args.get("repeat") if args.get("repeat") in ("Once", "Daily", "Weekly", "Monthly") else "Once"
            db.run("INSERT INTO reminders(title,due,repeat) VALUES(?,?,?)", (title, dt.strftime(FMT), rep))
            when = dt.strftime("%a %d %b, %H:%M")
            return (f"Reminder '{title}' set for {when} ({rep}).",
                    f"✓ Reminder set: {title}  ({when}" + ("" if rep == "Once" else f", repeats {rep.lower()}") + ")", False)
        if name == "add_note":
            title = str(args.get("title", "")).strip() or "Untitled"
            db.run("INSERT INTO notes(title,body,tags,pinned,updated) VALUES(?,?,?,0,?)",
                   (title, str(args.get("body", "")).strip(), str(args.get("tags", "")).strip(), now_str()))
            return f"Saved note '{title}'.", f"✓ Saved note: {title}", False
        if name == "add_flashcards":
            deck = str(args.get("deck", "")).strip() or "Tutor"
            n = 0
            for c in (args.get("cards") or [])[:30]:
                if isinstance(c, dict) and str(c.get("front", "")).strip() and str(c.get("back", "")).strip():
                    db.run("INSERT INTO cards(deck,front,back,due,created) VALUES(?,?,?,?,?)",
                           (deck, str(c["front"]).strip(), str(c["back"]).strip(), today, now_str()))
                    n += 1
            if not n:
                return "Error: no valid cards (each needs a front and a back).", None, True
            return f"Added {n} flashcards to deck '{deck}'.", f"✓ Added {n} flashcards to '{deck}'", False
        if name == "list_items":
            kind = args.get("kind", "tasks")
            if kind == "tasks":
                rows = db.rows("SELECT title,priority,due FROM todos WHERE done=0 ORDER BY due='' , due LIMIT 40")
                out = [f"- {r['title']} [{r['priority']}]" + (f" due {r['due']}" if r["due"] else "") for r in rows]
                return "\n".join(out) or "No open tasks.", None, False
            if kind == "reminders":
                rows = db.rows("SELECT title,due,repeat FROM reminders WHERE done=0 ORDER BY due LIMIT 40")
                return "\n".join(f"- {r['due']}  {r['title']} ({r['repeat'] or 'Once'})" for r in rows) or "No pending reminders.", None, False
            if kind == "notes":
                rows = db.rows("SELECT title,tags FROM notes ORDER BY updated DESC LIMIT 40")
                return "\n".join(f"- {r['title']}" + (f" [{r['tags']}]" if r["tags"] else "") for r in rows) or "No notes yet.", None, False
            if kind == "cards_due":
                n = db.one("SELECT COUNT(*) FROM cards WHERE due<=?", (today,))
                decks = [r[0] for r in db.rows("SELECT DISTINCT deck FROM cards WHERE due<=?", (today,))]
                return f"{n} flashcards due today" + (f" (decks: {', '.join(decks)})" if decks else "") + ".", None, False
            return f"Error: unknown kind {kind!r}.", None, True
        if name == "read_notes":
            q = str(args.get("query", "")).strip()
            if not q:
                rows = db.rows("SELECT title FROM notes ORDER BY updated DESC LIMIT 40")
                return "\n".join(f"- {r['title']}" for r in rows) or "No notes yet.", None, False
            like = f"%{q}%"
            rows = db.rows("SELECT title,body FROM notes WHERE title LIKE ? OR body LIKE ? OR tags LIKE ? "
                           "ORDER BY updated DESC LIMIT 3", (like, like, like))
            return "\n\n".join(f"# {r['title']}\n{(r['body'] or '')[:3000]}" for r in rows) or f"No notes match {q!r}.", None, False
        return f"Error: unknown tool {name!r}.", None, True
    except Exception as e:  # noqa: BLE001
        return f"Error: {e}", None, True


# ==========================================================================
# Sound: generated ambient loops + a tiny cross-platform player (stdlib only)
# ==========================================================================
SAMPLE_RATE = 22050
LOOP_SECONDS = 8


def _write_wav(path, samples, rate=SAMPLE_RATE, rms=None):
    """Write mono 16-bit WAV. rms=target loudness for noise; otherwise peak-normalise to 0.8."""
    if rms:
        gain = rms / max(1e-9, math.sqrt(sum(x * x for x in samples) / len(samples)))
    else:
        gain = 0.8 / max(1e-9, max(abs(x) for x in samples))
    a = array.array("h", (int(max(-0.95, min(0.95, x * gain)) * 32767) for x in samples))
    if sys.byteorder == "big":
        a.byteswap()
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(a.tobytes())


def scale_wav(src, dst, volume):
    """Copy a WAV with its 16-bit samples scaled by volume (winsound has no volume control)."""
    with wave.open(str(src), "rb") as r:
        params, frames = r.getparams(), r.readframes(r.getnframes())
    if params.sampwidth == 2:
        a = array.array("h")
        a.frombytes(frames)
        if sys.byteorder == "big":
            a.byteswap()
        a = array.array("h", (int(x * volume) for x in a))
        if sys.byteorder == "big":
            a.byteswap()
        frames = a.tobytes()
    with wave.open(str(dst), "wb") as w:
        w.setparams(params)
        w.writeframes(frames)


def _noise(kind, count, rnd):
    if kind == "white":
        return [rnd.uniform(-1, 1) for _ in range(count)]
    out = []
    if kind == "pink":  # Paul Kellet's filter
        b0 = b1 = b2 = 0.0
        for _ in range(count):
            w = rnd.uniform(-1, 1)
            b0 = 0.99765 * b0 + w * 0.0990460
            b1 = 0.96300 * b1 + w * 0.2965164
            b2 = 0.57000 * b2 + w * 1.0526913
            out.append(b0 + b1 + b2 + w * 0.1848)
        return out
    b = 0.0  # brown: leaky random walk
    for _ in range(count):
        b = (b + 0.02 * rnd.uniform(-1, 1)) / 1.02
        out.append(b)
    return out


def make_sound(name, path, rate=SAMPLE_RATE):
    """Synthesize one of the built-in sounds to a WAV file. Loops are made seamless with a crossfade."""
    rnd = random.Random(sum(map(ord, name)))
    two_pi = 2 * math.pi
    if name == "Chime":
        s = []
        for i in range(int(rate * 1.8)):
            t = i / rate
            env = math.exp(-3.2 * t) * min(1.0, t / 0.005)
            s.append(env * (math.sin(two_pi * 880 * t) + 0.5 * math.sin(two_pi * 1320 * t) + 0.25 * math.sin(two_pi * 1760 * t)))
        _write_wav(path, s, rate)
        return
    n, xf = rate * LOOP_SECONDS, rate // 4
    total = n + xf
    if name == "White noise":
        buf = _noise("white", total, rnd)
    elif name == "Pink noise":
        buf = _noise("pink", total, rnd)
    elif name == "Brown noise":
        buf = _noise("brown", total, rnd)
    elif name == "Rain":
        buf, e, prev = [], 0.0, 0.0
        for x in _noise("pink", total, rnd):
            hp, prev = x - prev * 0.95, x
            if rnd.random() < 0.0015:
                e = rnd.uniform(0.5, 1.0)
            buf.append(hp * 0.6 + e * rnd.uniform(-1, 1))
            e *= 0.985
    elif name == "Ocean waves":
        buf = [x * (0.2 + 0.8 * (0.5 + 0.5 * math.sin(two_pi * 2 * i / n - 1.2)) ** 2)
               for i, x in enumerate(_noise("pink", total, rnd))]
    else:  # "Deep focus drone" - all partials fit a whole number of cycles in the loop
        buf = [(math.sin(two_pi * 110 * i / rate) + 0.6 * math.sin(two_pi * 165 * i / rate)
                + 0.45 * math.sin(two_pi * 220 * i / rate) + 0.3 * math.sin(two_pi * 55 * i / rate))
               * (0.75 + 0.25 * math.sin(two_pi * 2 * i / n)) for i in range(total)]
    out = buf[:n]
    for i in range(xf):  # blend the tail into the head so the loop has no click
        t = i / xf
        out[i] = buf[i] * t + buf[n + i] * (1 - t)
    _write_wav(path, out, rate, rms=0.15)


class SoundPlayer:
    """Plays looping ambient sounds and user audio files using whatever the OS provides."""
    SOUNDS = ["Brown noise", "Pink noise", "White noise", "Rain", "Ocean waves", "Deep focus drone"]

    def __init__(self):
        self.dir = APP_DIR / "sounds"
        self.proc, self.playing, self._gen = None, False, 0
        self.backend = self._detect()

    @staticmethod
    def _detect():
        if sys.platform.startswith("win"):
            return "winsound"
        if sys.platform == "darwin":
            return "afplay" if shutil.which("afplay") else None
        for exe in ("paplay", "aplay", "ffplay", "play"):
            if shutil.which(exe):
                return exe
        return None

    def available(self):
        return self.backend is not None

    def path_for(self, name):
        return self.dir / (re.sub(r"\W+", "_", name.lower()).strip("_") + "_v1.wav")

    def has(self, name):
        return self.path_for(name).exists()

    def ensure(self, name):
        p = self.path_for(name)
        if not p.exists():
            self.dir.mkdir(parents=True, exist_ok=True)
            make_sound(name, p)
        return p

    def _argv(self, path, volume):
        v, b = max(0.0, min(1.0, volume)), self.backend
        if b == "afplay":
            return ["afplay", "-v", f"{v:.2f}", str(path)]
        if b == "paplay":
            return ["paplay", f"--volume={int(65536 * v)}", str(path)]
        if b == "aplay":
            return ["aplay", "-q", str(path)]
        if b == "ffplay":
            return ["ffplay", "-nodisp", "-autoexit", "-loglevel", "quiet", "-volume", str(int(v * 100)), str(path)]
        return ["play", "-q", "-v", f"{v:.2f}", str(path)]

    def play(self, path, volume=0.6, loop=True):
        """Start playback (replacing anything playing). Returns None on success, else a short error."""
        self.stop()
        if not self.available():
            return "No audio player found (install paplay, aplay, ffplay or sox)."
        path = Path(path)
        if not path.exists():
            return "That audio file no longer exists."
        if self.backend == "winsound":
            import winsound
            if path.suffix.lower() != ".wav":
                return "Windows' built-in player only handles .wav files - convert your file to WAV."
            src = path
            try:
                self.dir.mkdir(parents=True, exist_ok=True)
                scale_wav(path, self.dir / "_playing.wav", volume)
                src = self.dir / "_playing.wav"
            except Exception:  # noqa: BLE001 - fall back to full volume
                pass
            try:
                winsound.PlaySound(str(src), winsound.SND_FILENAME | winsound.SND_ASYNC | (winsound.SND_LOOP if loop else 0))
            except RuntimeError as e:
                return str(e)
            self.playing = loop
            return None
        self.playing = True
        self._gen += 1
        gen, argv = self._gen, self._argv(path, volume)

        def run():
            while self.playing and self._gen == gen:
                t0 = time.monotonic()
                try:
                    self.proc = subprocess.Popen(argv, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                    self.proc.wait()
                except OSError:
                    break
                if not loop or time.monotonic() - t0 < 0.3:  # one-shot, or the player failed instantly
                    break
            if self._gen == gen:
                self.playing = False
        threading.Thread(target=run, daemon=True).start()
        return None

    def stop(self):
        self.playing = False
        self._gen += 1
        if self.backend == "winsound":
            try:
                import winsound
                winsound.PlaySound(None, winsound.SND_PURGE)
            except Exception:  # noqa: BLE001
                pass
        p, self.proc = self.proc, None
        if p and p.poll() is None:
            try:
                p.terminate()
            except OSError:
                pass


# ==========================================================================
# Storage
# ==========================================================================
class Database:
    def __init__(self):
        APP_DIR.mkdir(parents=True, exist_ok=True)
        if not DB_PATH.exists() and OLD_DB_PATH.exists():
            shutil.copy2(OLD_DB_PATH, DB_PATH)  # carry over data from FocusHub
        self.lock = threading.RLock()
        self.conn = sqlite3.connect(DB_PATH, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript("""
            CREATE TABLE IF NOT EXISTS todos(
                id INTEGER PRIMARY KEY, title TEXT NOT NULL, priority TEXT DEFAULT 'Medium',
                due TEXT, done INTEGER DEFAULT 0, created TEXT);
            CREATE TABLE IF NOT EXISTS notes(
                id INTEGER PRIMARY KEY, title TEXT, body TEXT, updated TEXT);
            CREATE TABLE IF NOT EXISTS reminders(
                id INTEGER PRIMARY KEY, title TEXT NOT NULL, due TEXT NOT NULL,
                notified INTEGER DEFAULT 0, done INTEGER DEFAULT 0);
            CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY, value TEXT);
            CREATE TABLE IF NOT EXISTS chat(
                id INTEGER PRIMARY KEY, role TEXT, content TEXT, ts TEXT);
            CREATE TABLE IF NOT EXISTS focus_sessions(
                id INTEGER PRIMARY KEY, task TEXT, minutes INTEGER, ended TEXT);
            CREATE TABLE IF NOT EXISTS cards(
                id INTEGER PRIMARY KEY, deck TEXT DEFAULT 'General', front TEXT NOT NULL, back TEXT NOT NULL,
                ease REAL DEFAULT 2.5, interval INTEGER DEFAULT 0, reps INTEGER DEFAULT 0, due TEXT, created TEXT);
            CREATE TABLE IF NOT EXISTS garden(
                id INTEGER PRIMARY KEY, kind TEXT, minutes INTEGER, planted TEXT, task TEXT,
                plot INTEGER DEFAULT 0, slot INTEGER DEFAULT 0, seed INTEGER DEFAULT 1);
            CREATE TABLE IF NOT EXISTS g_inventory(item TEXT PRIMARY KEY, qty INTEGER DEFAULT 0);
            CREATE TABLE IF NOT EXISTS g_events(id INTEGER PRIMARY KEY, day TEXT, kind TEXT, n INTEGER DEFAULT 1);
        """)
        for table, col, ddl in [("todos", "done_at", "TEXT"), ("notes", "pinned", "INTEGER DEFAULT 0"),
                                ("notes", "tags", "TEXT DEFAULT ''"),
                                ("reminders", "repeat", "TEXT DEFAULT 'Once'"),
                                ("garden", "species", "TEXT"), ("garden", "growth", "INTEGER DEFAULT 100"),
                                ("garden", "watered", "TEXT DEFAULT ''"), ("garden", "harvested", "TEXT DEFAULT ''"),
                                ("garden", "cls", "TEXT DEFAULT 'plant'")]:
            if col not in [r["name"] for r in self.conn.execute(f"PRAGMA table_info({table})")]:
                self.conn.execute(f"ALTER TABLE {table} ADD COLUMN {col} {ddl}")
        self.conn.commit()

    def run(self, sql, args=()):
        with self.lock:
            cur = self.conn.execute(sql, args)
            self.conn.commit()
            return cur

    def rows(self, sql, args=()):
        with self.lock:
            return self.conn.execute(sql, args).fetchall()

    def one(self, sql, args=()):
        with self.lock:
            r = self.conn.execute(sql, args).fetchone()
        return r[0] if r else 0

    def get(self, key, default=""):
        r = self.rows("SELECT value FROM settings WHERE key=?", (key,))
        return r[0]["value"] if r else default

    def put(self, key, value):
        self.run("INSERT OR REPLACE INTO settings(key,value) VALUES(?,?)", (key, value))

    def export_all(self):
        out = {}
        for t in ("todos", "notes", "reminders", "chat", "focus_sessions", "cards", "garden", "g_inventory", "g_events"):
            out[t] = [dict(r) for r in self.rows(f"SELECT * FROM {t}")]
        out["game"] = {k: self.get(k) for k in ("g_coins", "g_xp", "g_earned", "g_plots", "g_ach", "g_init")}
        return out

    def snapshot(self, table, rid):
        r = self.rows(f"SELECT * FROM {table} WHERE id=?", (rid,))
        return dict(r[0]) if r else None

    def restore(self, table, row):
        cols = ", ".join(row)
        self.run(f"INSERT OR REPLACE INTO {table}({cols}) VALUES({', '.join('?' * len(row))})", list(row.values()))

    IMPORT_KEYS = {"todos": ("title", "created"), "notes": ("title", "body"),
                   "reminders": ("title", "due"), "cards": ("front", "back"), "garden": ("planted", "seed")}

    def import_all(self, data):
        added = 0
        for table, keys in self.IMPORT_KEYS.items():
            cols = [r["name"] for r in self.conn.execute(f"PRAGMA table_info({table})")]
            for row in data.get(table, []) if isinstance(data, dict) else []:
                if not isinstance(row, dict):
                    continue
                row = {k: v for k, v in row.items() if k in cols and k != "id"}
                if not all(k in row for k in keys):
                    continue
                where = " AND ".join(f"{k}=?" for k in keys)
                if self.one(f"SELECT COUNT(*) FROM {table} WHERE {where}", [row[k] for k in keys]):
                    continue
                self.restore(table, row)
                added += 1
        game = data.get("game") if isinstance(data, dict) else None
        if isinstance(game, dict):  # keep the better of local and imported progress
            for k in ("g_coins", "g_xp", "g_earned", "g_plots"):
                try:
                    self.put(k, str(max(int(self.get(k, "0") or 0), int(game.get(k) or 0))))
                except ValueError:
                    pass
            try:
                self.put("g_ach", json.dumps(sorted(set(json.loads(self.get("g_ach", "[]") or "[]")) | set(json.loads(game.get("g_ach") or "[]")))))
            except ValueError:
                pass
            self.put("g_init", "1")
        for row in data.get("g_inventory", []) if isinstance(data, dict) else []:
            if isinstance(row, dict) and row.get("item") in SPECIES:
                have = self.one("SELECT COALESCE(SUM(qty),0) FROM g_inventory WHERE item=?", (row["item"],))
                if have < int(row.get("qty") or 0):
                    self.run("INSERT OR REPLACE INTO g_inventory(item,qty) VALUES(?,?)", (row["item"], int(row["qty"])))
        return added


# ==========================================================================
# Styling + widget helpers
# ==========================================================================

GARDEN_MIN_MINUTES = 30
GARDEN_PLOT = 24          # tiles per plot (4 rows x 6 columns)
GARDEN_COLS = 6
STAGE_NAMES = ["Seed", "Sprout", "Growing", "Almost grown", "In bloom"]
TYPE_NAMES = {"flower": "Flower", "tree": "Tree", "bush": "Bush", "special": "Special", "deco": "Decor"}
GREENS = ["#166534", "#15803d", "#16a34a", "#22c55e", "#4ade80"]
PINKS = ["#be185d", "#db2777", "#f472b6", "#f9a8d4", "#fbcfe8"]
SESSION_SPECIES = {"Flower": "daisy", "Tree": "oak", "Grand tree": "grand_oak"}
LEVEL_TITLES = ["Seedling", "Sprout", "Gardener", "Green Thumb", "Botanist", "Grove Keeper", "Forest Warden",
                "Garden Sage", "Archdruid", "Garden Legend"]
GARDEN_HINT = ("Click a plant to water or collect it. Focus sessions earn ☀ and make everything grow. "
               "A blue drop means thirsty, a gold star means ready to collect.")


def plant_kind(minutes):
    """Which free reward a focus session of this length earns (None if too short)."""
    return "Grand tree" if minutes >= 90 else "Tree" if minutes >= 60 else "Flower" if minutes >= GARDEN_MIN_MINUTES else None


def stage_of(growth):
    return 0 if growth < 25 else 1 if growth < 50 else 2 if growth < 80 else 3 if growth < 100 else 4


def xp_for(level):
    return 30 * (level - 1) * level


def level_for(xp):
    lvl = 1
    while xp >= xp_for(lvl + 1):
        lvl += 1
    return lvl


def level_title(level):
    return LEVEL_TITLES[min(level, len(LEVEL_TITLES)) - 1]


def _sp(name, cls, cost, lvl, rare, kind, water=20, pay=0, cd=24, **kw):
    return {"name": name, "cls": cls, "cost": cost, "lvl": lvl, "rare": rare, "kind": kind,
            "water": water, "pay": pay, "cd": cd, "kw": kw, "shop": cost > 0}


SPECIES = {
    "daisy": _sp("Daisy", "flower", 10, 1, "Common", "flower", 25, 3, 20, petal="#ffffff", center="#facc15", n=8),
    "tulip": _sp("Tulip", "flower", 25, 1, "Common", "tulip", 25, 5, 20, color="#ef4444"),
    "mushroom": _sp("Magic mushroom", "special", 35, 1, "Common", "mushroom", 25, 6, 20),
    "lavender": _sp("Lavender", "flower", 40, 2, "Uncommon", "lavender", 22, 7, 22),
    "sunflower": _sp("Sunflower", "flower", 45, 2, "Uncommon", "flower", 22, 8, 22,
                     petal="#facc15", center="#78350f", n=12, tall=1.35, big=1.25),
    "berry": _sp("Berry bush", "bush", 60, 2, "Uncommon", "bush", 20, 10, 24, color="#15803d", dots="#dc2626"),
    "cactus": _sp("Cactus", "special", 50, 3, "Uncommon", "cactus", 20, 9, 24),
    "rose": _sp("Rose", "flower", 70, 3, "Rare", "flower", 15, 14, 24, petal="#dc2626", center="#7f1d1d", n=6, big=1.1),
    "pine": _sp("Pine tree", "tree", 90, 3, "Uncommon", "pine", 18, 14, 36),
    "oak": _sp("Oak tree", "tree", 110, 4, "Uncommon", "tree", 18, 16, 36, canopy=GREENS, trunk=(8, 32), rad=22),
    "hydrangea": _sp("Hydrangea", "bush", 100, 4, "Rare", "bush", 14, 18, 28, color="#4f7cac", dots="#93c5fd", big=1.6),
    "apple": _sp("Apple tree", "tree", 160, 5, "Rare", "tree", 14, 28, 40,
                 canopy=GREENS, trunk=(9, 34), rad=24, fruit="#ef4444"),
    "cherry": _sp("Cherry blossom", "tree", 220, 6, "Rare", "tree", 12, 36, 40,
                  canopy=PINKS, trunk=(9, 34), rad=26, fruit="#ffffff"),
    "bonsai": _sp("Bonsai", "special", 300, 7, "Rare", "bonsai", 12, 45, 48),
    "moonflower": _sp("Moonflower", "flower", 400, 8, "Legendary", "flower", 9, 70, 48,
                      petal="#c7d2fe", center="#e0e7ff", n=7, big=1.2, glow="#c7d2fe"),
    "golden_tulip": _sp("Golden tulip", "flower", 900, 10, "Legendary", "tulip", 8, 150, 48,
                        color="#fbbf24", glow="#fde68a"),
    "grand_oak": _sp("Grand oak", "tree", 0, 1, "Rare", "tree", 15, 30, 40,
                     canopy=GREENS, trunk=(13, 46), rad=34, fruit="auto"),
    "lantern": _sp("Lantern", "deco", 40, 1, "Common", "lantern"),
    "bench": _sp("Bench", "deco", 60, 2, "Common", "bench"),
    "scarecrow": _sp("Scarecrow", "deco", 90, 2, "Common", "scarecrow"),
    "gnome": _sp("Garden gnome", "deco", 100, 3, "Uncommon", "gnome"),
    "birdbath": _sp("Bird bath", "deco", 140, 4, "Uncommon", "birdbath"),
    "pond": _sp("Pond", "deco", 200, 5, "Rare", "pond"),
    "fountain": _sp("Fountain", "deco", 350, 6, "Rare", "fountain"),
    "windmill": _sp("Windmill", "deco", 500, 8, "Rare", "windmill"),
}


def species_id(row):
    sid = row["species"] or SESSION_SPECIES.get(row["kind"], "daisy")
    return sid if sid in SPECIES else "daisy"


def species_of(row):
    return SPECIES[species_id(row)]


# -- painters: every one draws a finished plant with its base at (x, y); s is the scale ---------------------

QUEST_POOL = [  # (id, title, target, reward, group)
    ("focus25", "Focus for 25 minutes today", 25, 30, "focus"),
    ("focus60", "Focus for 60 minutes today", 60, 70, "focus"),
    ("long", "Finish a 30+ minute focus session", 1, 50, "session"),
    ("water3", "Water 3 plants", 3, 20, "water"),
    ("water8", "Water 8 plants", 8, 45, "water"),
    ("harvest2", "Collect from 2 plants", 2, 25, "harvest"),
    ("tasks3", "Complete 3 tasks", 3, 30, "tasks"),
    ("plant1", "Plant something new", 1, 25, "plant"),
]

ACHIEVEMENTS = [  # (id, name, description, reward, test)
    ("sprout", "First Sprout", "Have your first plant", 20, lambda g: g.count_plants() >= 1),
    ("ten", "Green Thumb", "Have 10 plants", 50, lambda g: g.count_plants() >= 10),
    ("collector", "Collector", "Grow 5 different species", 60, lambda g: g.species_owned() >= 5),
    ("rare", "Rare Find", "Own a Rare or Legendary plant", 80, lambda g: g.has_rarity({"Rare", "Legendary"})),
    ("decorator", "Decorator", "Place 5 decorations", 50, lambda g: g.count_decos() >= 5),
    ("hours", "Deep Work", "Focus for 10 hours in total", 100, lambda g: g.total_focus() >= 600),
    ("marathon", "Marathon", "Finish a 90+ minute session", 100, lambda g: g.longest_session() >= 90),
    ("streak3", "On a Roll", "Focus 3 days in a row", 40, lambda g: g.focus_streak() >= 3),
    ("streak7", "Unstoppable", "Focus 7 days in a row", 120, lambda g: g.focus_streak() >= 7),
    ("harvester", "Harvester", "Collect from plants 25 times", 60, lambda g: g.event_total("harvest") >= 25),
    ("tycoon", "Sun Baron", "Earn 1000 ☀ in total", 150, lambda g: g.earned() >= 1000),
]


class GardenGame:
    """Wallet, XP, shop, bag, planting, watering, collecting, quests and badges. Pure logic on the database."""

    def __init__(self, db):
        self.db = db
        self.notes = []  # short messages for the UI to toast (level-ups, badges...)

    def start(self):
        """First launch: starter coins, plus a thank-you for focus sessions logged before the game existed."""
        if self.db.get("g_init"):
            return
        past = self.db.one("SELECT COALESCE(SUM(minutes),0) FROM focus_sessions WHERE minutes>=10")
        self.db.put("g_init", "1")
        self.db.put("g_coins", str(40 + past))
        self.db.put("g_earned", str(40 + past))
        self.db.put("g_xp", str(past))
        self.notes.append("Welcome to your garden game! " +
                          (f"Your past focus earned {past} ☀." if past else "Here are 40 ☀ to get started - visit the shop."))

    # -- wallet / xp
    def _num(self, key):
        try:
            return int(self.db.get(key, "0") or 0)
        except ValueError:
            return 0

    def coins(self):
        return self._num("g_coins")

    def xp(self):
        return self._num("g_xp")

    def earned(self):
        return self._num("g_earned")

    def level(self):
        return level_for(self.xp())

    def add_coins(self, n):
        self.db.put("g_coins", str(max(0, self.coins() + n)))
        if n > 0:
            self.db.put("g_earned", str(self.earned() + n))

    def add_xp(self, n):
        before = self.level()
        self.db.put("g_xp", str(self.xp() + n))
        after = self.level()
        if after > before:
            new = [v["name"] for v in SPECIES.values() if v["shop"] and before < v["lvl"] <= after]
            self.notes.append(f"Level up! Lv {after} · {level_title(after)}" + (f" - new in shop: {', '.join(new)}" if new else ""))

    def log(self, kind, n=1):
        self.db.run("INSERT INTO g_events(day,kind,n) VALUES(?,?,?)", (datetime.now().date().isoformat(), kind, n))

    def event_today(self, kind):
        return self.db.one("SELECT COALESCE(SUM(n),0) FROM g_events WHERE day=? AND kind=?",
                           (datetime.now().date().isoformat(), kind))

    def event_total(self, kind):
        return self.db.one("SELECT COALESCE(SUM(n),0) FROM g_events WHERE kind=?", (kind,))

    # -- bag / shop / plots
    def bag(self):
        return [(r["item"], r["qty"]) for r in self.db.rows("SELECT item,qty FROM g_inventory WHERE qty>0 ORDER BY item")]

    def bag_count(self):
        return self.db.one("SELECT COALESCE(SUM(qty),0) FROM g_inventory")

    def bag_qty(self, item):
        return self.db.one("SELECT COALESCE(SUM(qty),0) FROM g_inventory WHERE item=?", (item,))

    def bag_add(self, item, n=1):
        if self.db.run("UPDATE g_inventory SET qty=qty+? WHERE item=?", (n, item)).rowcount == 0:
            self.db.run("INSERT INTO g_inventory(item,qty) VALUES(?,?)", (item, n))

    def bag_take(self, item):
        self.db.run("UPDATE g_inventory SET qty=qty-1 WHERE item=? AND qty>0", (item,))

    def buy(self, sid):
        sp = SPECIES.get(sid)
        if not sp or not sp["shop"]:
            return False, "That isn't for sale."
        if self.level() < sp["lvl"]:
            return False, f"Unlocks at level {sp['lvl']}."
        if self.coins() < sp["cost"]:
            return False, f"You need {sp['cost'] - self.coins()} more ☀."
        self.add_coins(-sp["cost"])
        self.bag_add(sid)
        return True, f"Bought {sp['name']} - it's in your Bag."

    def plots(self):
        return max(self._num("g_plots") or 1, self.db.one("SELECT COALESCE(MAX(plot),-1)+1 FROM garden") or 1)

    def plot_cost(self):
        return 150 * self.plots()

    def buy_plot(self):
        cost = self.plot_cost()
        if self.coins() < cost:
            return False, f"A new plot costs {cost} ☀."
        self.add_coins(-cost)
        self.db.put("g_plots", str(self.plots() + 1))
        return True, "New plot unlocked!"

    # -- planting
    def taken(self, plot):
        return {r["slot"]: r for r in self.db.rows("SELECT * FROM garden WHERE plot=?", (plot,))}

    def insert_item(self, sid, plot, slot, growth, minutes=0, task="", when=None):
        sp = SPECIES[sid]
        cur = self.db.run(
            "INSERT INTO garden(kind,minutes,planted,task,plot,slot,seed,species,growth,watered,harvested,cls) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
            (sp["name"], minutes, when or now_str(), task or "", plot, slot, random.randrange(1, 10 ** 9), sid, growth, "", "",
             "deco" if sp["cls"] == "deco" else "plant"))
        return cur.lastrowid

    def free_slot(self):
        used = {(r["plot"], r["slot"]) for r in self.db.rows("SELECT plot,slot FROM garden")}
        for plot in range(self.plots()):
            for slot in random.Random(plot * 7919 + 13).sample(range(GARDEN_PLOT), GARDEN_PLOT):
                if (plot, slot) not in used:
                    return plot, slot
        return None

    def plant_free(self, sid, minutes=0, task="", when=None, growth=40):
        """Plant a reward on a random free tile; if the garden is full it goes to the Bag instead (-> None)."""
        spot = self.free_slot()
        if spot is None:
            self.bag_add(sid)
            return None
        return self.insert_item(sid, spot[0], spot[1], growth, minutes, task, when)

    def place(self, sid, plot, slot):
        if not (0 <= slot < GARDEN_PLOT and 0 <= plot < self.plots()):
            return False, "That tile isn't part of your garden.", None
        if slot in self.taken(plot):
            return False, "That tile is already taken.", None
        if self.bag_qty(sid) < 1:
            return False, "You don't have that in your Bag.", None
        sp = SPECIES[sid]
        self.bag_take(sid)
        deco = sp["cls"] == "deco"
        pid = self.insert_item(sid, plot, slot, 100 if deco else 0)
        if not deco:
            self.log("plant")
        self.add_xp(4 if deco else 10)
        return True, f"{'Placed' if deco else 'Planted'} {sp['name']}", pid

    def remove(self, pid):
        r = self.db.snapshot("garden", pid)
        if not r:
            return 0
        refund = int(species_of(r)["cost"] * 0.25)
        self.db.run("DELETE FROM garden WHERE id=?", (pid,))
        self.add_coins(refund)
        return refund

    # -- caring and collecting
    def water(self, pid):
        r = self.db.snapshot("garden", pid)
        if not r or r["cls"] == "deco":
            return False, "Decorations don't need water."
        today = datetime.now().date().isoformat()
        if r["watered"] == today:
            return False, "Already watered today."
        grown = min(100, r["growth"] + species_of(r)["water"])
        self.db.run("UPDATE garden SET growth=?, watered=? WHERE id=?", (grown, today, pid))
        self.add_xp(3)
        self.log("water")
        return True, f"+{grown - r['growth']}% growth" if grown > r["growth"] else "Refreshed"

    def water_all(self):
        today = datetime.now().date().isoformat()
        return sum(1 for r in self.db.rows("SELECT id FROM garden WHERE cls='plant' AND watered!=?", (today,))
                   if self.water(r["id"])[0])

    def ready_hours(self, r):
        """None = not collectable (decoration or still growing), 0 = ready now, else hours to wait."""
        if r["cls"] == "deco" or r["growth"] < 100:
            return None
        if not r["harvested"]:
            return 0.0
        try:
            last = datetime.strptime(r["harvested"], FMT)
        except ValueError:
            return 0.0
        return max(0.0, species_of(r)["cd"] - (datetime.now() - last).total_seconds() / 3600)

    def harvest(self, pid):
        r = self.db.snapshot("garden", pid)
        rh = self.ready_hours(r) if r else None
        if rh is None:
            return False, "It isn't fully grown yet.", 0
        if rh > 0:
            return False, f"Ready again in {rh:.0f}h.", 0
        pay = species_of(r)["pay"]
        self.db.run("UPDATE garden SET harvested=? WHERE id=?", (now_str(), pid))
        self.add_coins(pay)
        self.add_xp(5)
        self.log("harvest")
        return True, f"+{pay} ☀", pay

    def harvest_all(self):
        n = total = 0
        for r in self.db.rows("SELECT id FROM garden WHERE cls='plant'"):
            ok, _, pay = self.harvest(r["id"])
            n, total = n + ok, total + pay
        return n, total

    # -- focus sessions feed the garden
    def coin_reward(self, minutes):
        return 0 if minutes < 10 else minutes + (minutes // 4 if minutes >= 60 else 0)

    def on_session(self, minutes, task=""):
        """Call after a completed focus session has been logged. Returns what it earned."""
        streak = self.focus_streak()
        out = {"coins": 0, "planted": None, "stored": False, "kind": plant_kind(minutes), "streak": streak}
        if minutes >= 10:
            out["coins"] = self.coin_reward(minutes) + (min(25, 5 * (streak - 1)) if streak >= 2 else 0)
            self.add_coins(out["coins"])
            self.add_xp(minutes)
            self.log("focus_min", minutes)
            self.db.run("UPDATE garden SET growth=MIN(100, growth+10) WHERE cls='plant' AND growth<100")
        if out["kind"]:
            self.log("long_session")
            out["planted"] = self.plant_free(SESSION_SPECIES[out["kind"]], minutes, task)
            out["stored"] = out["planted"] is None
        self.check_achievements()
        return out

    # -- stats used by quests and badges
    def focus_streak(self):
        days = {r[0] for r in self.db.rows("SELECT DISTINCT substr(ended,1,10) FROM focus_sessions")}
        d, n = datetime.now().date(), 0
        if d.isoformat() not in days:
            d -= timedelta(days=1)
        while d.isoformat() in days:
            n += 1
            d -= timedelta(days=1)
        return n

    def total_focus(self):
        return self.db.one("SELECT COALESCE(SUM(minutes),0) FROM focus_sessions")

    def longest_session(self):
        return self.db.one("SELECT COALESCE(MAX(minutes),0) FROM focus_sessions")

    def count_plants(self):
        return self.db.one("SELECT COUNT(*) FROM garden WHERE cls='plant'")

    def count_decos(self):
        return self.db.one("SELECT COUNT(*) FROM garden WHERE cls='deco'")

    def species_owned(self):
        return len({species_id(r) for r in self.db.rows("SELECT * FROM garden WHERE cls='plant'")})

    def has_rarity(self, rarities):
        return any(species_of(r)["rare"] in rarities for r in self.db.rows("SELECT * FROM garden WHERE cls='plant'"))

    @staticmethod
    def weather():
        v = random.Random(datetime.now().date().toordinal() * 31 + 7).random()
        return "Sunny" if v < 0.55 else "Cloudy" if v < 0.8 else "Rainy"

    # -- daily quests
    def todays_quests(self):
        rnd = random.Random(datetime.now().date().toordinal())
        pool, used, chosen = QUEST_POOL[:], set(), []
        rnd.shuffle(pool)
        for q in pool:
            if q[4] not in used:
                used.add(q[4])
                chosen.append(q)
            if len(chosen) == 3:
                break
        return chosen

    def _progress(self, qid):
        today = datetime.now().date().isoformat()
        return {"focus25": lambda: self.event_today("focus_min"), "focus60": lambda: self.event_today("focus_min"),
                "long": lambda: self.event_today("long_session"), "water3": lambda: self.event_today("water"),
                "water8": lambda: self.event_today("water"), "harvest2": lambda: self.event_today("harvest"),
                "plant1": lambda: self.event_today("plant"),
                "tasks3": lambda: self.db.one("SELECT COUNT(*) FROM todos WHERE substr(done_at,1,10)=?", (today,))}[qid]()

    def quests(self):
        day = datetime.now().date().isoformat()
        out = []
        for qid, title, target, reward, _ in self.todays_quests():
            prog = self._progress(qid)
            out.append({"id": qid, "title": title, "target": target, "reward": reward, "progress": prog,
                        "done": prog >= target, "claimed": bool(self.db.get(f"g_claim_{day}_{qid}"))})
        return out

    def ready_quests(self):
        return sum(1 for q in self.quests() if q["done"] and not q["claimed"])

    def claim(self, qid):
        day = datetime.now().date().isoformat()
        q = next((q for q in self.quests() if q["id"] == qid), None)
        if not q or not q["done"] or q["claimed"]:
            return False
        self.db.put(f"g_claim_{day}_{qid}", "1")
        self.add_coins(q["reward"])
        self.add_xp(q["reward"] // 2)
        self.notes.append(f"Quest complete: +{q['reward']} ☀")
        if all(x["claimed"] for x in self.quests()) and not self.db.get(f"g_bonus_{day}"):
            self.db.put(f"g_bonus_{day}", "1")
            self.add_coins(40)
            self.notes.append("All daily quests done: bonus +40 ☀")
        return True

    # -- badges
    def unlocked(self):
        try:
            return set(json.loads(self.db.get("g_ach", "[]") or "[]"))
        except ValueError:
            return set()

    def check_achievements(self):
        have, new = self.unlocked(), []
        for aid, name, _, reward, test in ACHIEVEMENTS:
            if aid not in have and test(self):
                have.add(aid)
                self.add_coins(reward)
                self.notes.append(f"Badge unlocked: {name} (+{reward} ☀)")
                new.append((name, reward))
        if new:
            self.db.put("g_ach", json.dumps(sorted(have)))
        return new


def plant_session(db, minutes, task="", when=None, growth=40):
    """Plant the free reward for a session of this length (None if it's too short)."""
    kind = plant_kind(minutes)
    return GardenGame(db).plant_free(SESSION_SPECIES[kind], minutes, task, when, growth) if kind else None


def garden_backfill(db):
    """One time: turn focus sessions logged before the garden existed into fully grown plants."""
    if db.get("garden_backfilled"):
        return
    db.put("garden_backfilled", "1")
    if db.one("SELECT COUNT(*) FROM garden"):
        return
    for r in db.rows("SELECT * FROM focus_sessions WHERE minutes>=? ORDER BY id", (GARDEN_MIN_MINUTES,)):
        plant_session(db, r["minutes"], r["task"], r["ended"], growth=100)

