from flask import Flask, request, jsonify, send_from_directory
import sqlite3
import os
import re
from datetime import datetime

app = Flask(__name__)

DB_PATH = os.environ.get("DATABASE_PATH", os.path.join(os.path.dirname(__file__), "data", "election.db"))
ADMIN_TOKEN = os.environ.get("ADMIN_TOKEN", "")

# All office bearers required to conduct a successful alumni meet
OFFICES = [
    {"slug": "president",            "title": "President",             "emoji": "👑", "desc": "Heads the association, chairs the meet"},
    {"slug": "vice_president",       "title": "Vice President",        "emoji": "🤝", "desc": "Supports president, acts in absence"},
    {"slug": "general_secretary",    "title": "General Secretary",     "emoji": "📝", "desc": "Convenes meet, minutes & coordination"},
    {"slug": "joint_secretary",      "title": "Joint Secretary",       "emoji": "📋", "desc": "Assists secretary, registrations"},
    {"slug": "treasurer",            "title": "Treasurer",             "emoji": "💰", "desc": "Budget, contributions & accounts"},
    {"slug": "organizing_secretary", "title": "Organising Secretary",  "emoji": "🎯", "desc": "Venue, logistics & volunteers"},
    {"slug": "cultural_secretary",   "title": "Cultural Secretary",    "emoji": "🎭", "desc": "Stage, entertainment & felicitations"},
]
OFFICE_MAP = {o["slug"]: o for o in OFFICES}

def slugify(s):
    s = (s or "").strip().lower().replace(" ", "_")
    s = re.sub(r"[^a-z0-9_]", "", s)
    return s[:40]

def pretty(slug, custom=None):
    if slug in OFFICE_MAP:
        return OFFICE_MAP[slug]
    if custom and slug in custom:
        return custom[slug]
    title = slug.replace("_", " ").title()
    return {"slug": slug, "title": title, "emoji": "📌", "desc": "Custom office"}

def get_db():
    d = os.path.dirname(DB_PATH)
    if d:
        os.makedirs(d, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA foreign_keys=ON;")
    return conn

def init_db():
    conn = get_db()
    # New flexible schema
    conn.executescript("""
    CREATE TABLE IF NOT EXISTS candidates (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        race TEXT NOT NULL,
        name TEXT NOT NULL,
        created_at TEXT NOT NULL,
        UNIQUE(race, name COLLATE NOCASE)
    );
    CREATE TABLE IF NOT EXISTS votes_new (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        voter TEXT NOT NULL UNIQUE COLLATE NOCASE,
        created_at TEXT NOT NULL
    );
    CREATE TABLE IF NOT EXISTS vote_choices (
        vote_id INTEGER NOT NULL REFERENCES votes_new(id) ON DELETE CASCADE,
        race TEXT NOT NULL,
        candidate_id INTEGER NOT NULL REFERENCES candidates(id),
        UNIQUE(vote_id, race)
    );
    CREATE TABLE IF NOT EXISTS custom_offices (
        slug TEXT PRIMARY KEY,
        title TEXT NOT NULL,
        emoji TEXT NOT NULL DEFAULT '📌',
        descr TEXT NOT NULL DEFAULT 'Custom office',
        created_at TEXT NOT NULL
    );
    """)
    # Migrate legacy tables if present
    cols = [r["name"] for r in conn.execute("PRAGMA table_info(votes)")]
    if "president_id" in cols:
        try:
            for v in conn.execute("SELECT voter, president_id, secretary_id, created_at FROM votes"):
                try:
                    cur = conn.execute("INSERT INTO votes_new (voter, created_at) VALUES (?,?)",
                                       (v["voter"], v["created_at"]))
                    vid = cur.lastrowid
                    conn.execute("INSERT INTO vote_choices (vote_id, race, candidate_id) VALUES (?,?,?)",
                                 (vid, "president", v["president_id"]))
                    conn.execute("INSERT INTO vote_choices (vote_id, race, candidate_id) VALUES (?,?,?)",
                                 (vid, "secretary", v["secretary_id"]))
                except sqlite3.IntegrityError:
                    pass
            conn.execute("ALTER TABLE votes RENAME TO votes_legacy")
        except Exception:
            pass
        conn.commit()
    # Normalise: votes_new -> votes (if fresh, just rename usage)
    # Keep using votes_new as canonical; create view alias via table check
    conn.commit()
    conn.close()

init_db()

def db_votes_table():
    conn = get_db()
    names = [r["name"] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")]
    conn.close()
    return "votes_new" if "votes_new" in names else "votes"

def custom_map(conn):
    try:
        return {r["slug"]: {"slug": r["slug"], "title": r["title"], "emoji": r["emoji"] or "📌",
                            "desc": r["descr"] or "Custom office"}
                for r in conn.execute("SELECT slug, title, emoji, descr FROM custom_offices")}
    except Exception:
        return {}

def get_offices(conn):
    cmap = custom_map(conn)
    offices = list(OFFICES)
    seen = {o["slug"] for o in offices}
    for r in conn.execute("SELECT slug, title, emoji, descr, created_at FROM custom_offices ORDER BY created_at"):
        if r["slug"] not in seen:
            offices.append({"slug": r["slug"], "title": r["title"], "emoji": r["emoji"] or "📌",
                            "desc": r["descr"] or "Custom office"})
            seen.add(r["slug"])
    # orphan races (e.g. added via write-in with a brand-new slug, or legacy 'secretary')
    try:
        for r in conn.execute("SELECT DISTINCT race FROM candidates"):
            if r["race"] not in seen:
                offices.append(pretty(r["race"], cmap))
                seen.add(r["race"])
        for r in conn.execute("SELECT DISTINCT race FROM vote_choices"):
            if r["race"] not in seen:
                offices.append(pretty(r["race"], cmap))
                seen.add(r["race"])
    except Exception:
        pass
    return offices

def all_races(conn):
    return [o["slug"] for o in get_offices(conn)]

def norm(s):
    return (s or "").strip()

@app.get("/health")
def health():
    return {"ok": True}

@app.get("/api/offices")
def offices():
    conn = get_db()
    offices = get_offices(conn)
    conn.close()
    return jsonify(offices)

@app.post("/api/offices")
def create_office():
    data = request.get_json(force=True, silent=True) or {}
    title = norm(data.get("title"))
    emoji = norm(data.get("emoji")) or "📌"
    desc = norm(data.get("desc") or data.get("description")) or "Custom office"
    first_nominee = norm(data.get("first_nominee") or data.get("firstNominee") or "")
    if not title or len(title) < 2 or len(title) > 40:
        return jsonify({"error": "Office title must be 2-40 characters"}), 400
    slug = slugify(data.get("slug") or title)
    if not slug or len(slug) < 2:
        return jsonify({"error": "Could not make a valid office id from that title"}), 400
    if slug in OFFICE_MAP:
        return jsonify({"error": f"'{OFFICE_MAP[slug]['title']}' already exists"}), 409
    if len(emoji) > 8:
        emoji = emoji[:8]
    if len(desc) > 120:
        desc = desc[:120]
    conn = get_db()
    try:
        conn.execute("INSERT INTO custom_offices (slug, title, emoji, descr, created_at) VALUES (?,?,?,?,?)",
                     (slug, title, emoji, desc, datetime.utcnow().isoformat()))
        cid = None
        if first_nominee:
            if len(first_nominee) < 2 or len(first_nominee) > 60:
                conn.rollback(); conn.close()
                return jsonify({"error": "First nominee must be 2-60 characters"}), 400
            try:
                cur = conn.execute("INSERT INTO candidates (race, name, created_at) VALUES (?,?,?)",
                                   (slug, first_nominee, datetime.utcnow().isoformat()))
                cid = cur.lastrowid
            except sqlite3.IntegrityError:
                pass
        conn.commit()
    except sqlite3.IntegrityError:
        conn.close()
        return jsonify({"error": "That office already exists"}), 409
    conn.close()
    return jsonify({"ok": True, "slug": slug, "title": title, "emoji": emoji,
                    "desc": desc, "first_candidate_id": cid})

@app.delete("/api/offices/<slug>")
def delete_office(slug):
    slug = slugify(slug)
    if slug in OFFICE_MAP:
        return jsonify({"error": "Core offices cannot be deleted"}), 400
    conn = get_db()
    row = conn.execute("SELECT slug FROM custom_offices WHERE slug=?", (slug,)).fetchone()
    if not row:
        # also allow removing orphan races created implicitly (no custom_offices row)
        has_c = conn.execute("SELECT 1 FROM candidates WHERE race=? LIMIT 1", (slug,)).fetchone()
        if not has_c:
            conn.close()
            return jsonify({"error": "Office not found"}), 404
    used = conn.execute("SELECT COUNT(*) c FROM vote_choices WHERE race=?", (slug,)).fetchone()["c"]
    if used:
        conn.close()
        return jsonify({"error": "Cannot delete — votes already cast for this office"}), 409
    conn.execute("DELETE FROM candidates WHERE race=?", (slug,))
    conn.execute("DELETE FROM custom_offices WHERE slug=?", (slug,))
    conn.commit(); conn.close()
    return jsonify({"ok": True})

@app.get("/api/state")
def state():
    conn = get_db()
    vt = "votes_new"
    offices = get_offices(conn)
    cmap = custom_map(conn)
    races = [o["slug"] for o in offices]
    cands = {}
    for race in races:
        cands[race] = [{"id": r["id"], "name": r["name"]}
                       for r in conn.execute("SELECT id, name FROM candidates WHERE race=? ORDER BY name COLLATE NOCASE", (race,))]
    try:
        total = conn.execute(f"SELECT COUNT(*) c FROM {vt}").fetchone()["c"]
    except Exception:
        total = 0
    results = {}
    for race in races:
        rows = conn.execute("""
            SELECT c.id, c.name, COUNT(vc.vote_id) as votes
            FROM candidates c LEFT JOIN vote_choices vc
              ON vc.candidate_id = c.id AND vc.race = ?
            WHERE c.race = ?
            GROUP BY c.id ORDER BY votes DESC, name COLLATE NOCASE
        """, (race, race)).fetchall()
        rlist = []
        for r in rows:
            pct = round((r["votes"] / total * 100) if total else 0, 1)
            rlist.append({"id": r["id"], "name": r["name"], "votes": r["votes"], "pct": pct})
        results[race] = rlist
    try:
        recent = [dict(r) for r in conn.execute(f"SELECT voter, created_at FROM {vt} ORDER BY id DESC LIMIT 8")]
    except Exception:
        recent = []
    conn.close()
    return jsonify({
        "offices": offices,
        "candidates": cands,
        "results": results,
        "total_votes": total,
        "recent": recent,
    })

@app.post("/api/candidates")
def add_candidate():
    data = request.get_json(force=True, silent=True) or {}
    race = slugify(data.get("race"))
    name = norm(data.get("name"))
    if not race:
        return jsonify({"error": "Office is required"}), 400
    if not name or len(name) < 2 or len(name) > 60:
        return jsonify({"error": "Name must be 2-60 characters"}), 400
    conn = get_db()
    try:
        cur = conn.execute("INSERT INTO candidates (race, name, created_at) VALUES (?,?,?)",
                           (race, name, datetime.utcnow().isoformat()))
        conn.commit()
        cid = cur.lastrowid
    except sqlite3.IntegrityError:
        conn.close()
        return jsonify({"error": f"'{name}' is already nominated for {pretty(race)['title']}"}), 409
    conn.close()
    return jsonify({"ok": True, "id": cid, "race": race, "name": name})

@app.post("/api/vote")
def vote():
    data = request.get_json(force=True, silent=True) or {}
    voter = norm(data.get("voter"))
    if not voter or len(voter) < 2 or len(voter) > 60:
        return jsonify({"error": "Enter your name to vote (2-60 chars)"}), 400

    choices = dict(data.get("choices") or {})
    writeins = dict(data.get("writeins") or {})

    # Backward compat: president_id / secretary_id / president_new / secretary_new
    for legacy_race in ("president", "secretary", "vice_president", "general_secretary",
                        "joint_secretary", "treasurer", "organizing_secretary", "cultural_secretary"):
        lid = data.get(f"{legacy_race}_id")
        if lid and legacy_race not in choices:
            choices[legacy_race] = lid
        wnew = norm(data.get(f"{legacy_race}_new"))
        if wnew and legacy_race not in writeins:
            writeins[legacy_race] = wnew
    # also generic *_new already covered; plus old keys president_id etc. already handled
    if data.get("president_id") and "president" not in choices:
        choices["president"] = data.get("president_id")
    if data.get("secretary_id") and "secretary" not in choices:
        # map legacy 'secretary' -> general_secretary if that office exists and secretary empty
        choices.setdefault("general_secretary", data.get("secretary_id"))
        choices.setdefault("secretary", data.get("secretary_id"))
    if norm(data.get("president_new")):
        writeins.setdefault("president", norm(data.get("president_new")))
    if norm(data.get("secretary_new")):
        writeins.setdefault("general_secretary", norm(data.get("secretary_new")))

    # Required races = all current offices (default 7 + any custom added).
    # Custom offices added later also become required for NEW ballots;
    # earlier ballots simply show lower % for the new race.
    conn = get_db()
    offices_now = get_offices(conn)
    cmap_now = custom_map(conn)
    required = [o["slug"] for o in offices_now]
    def pt(slug):
        for o in offices_now:
            if o["slug"] == slug:
                return o["title"]
        return pretty(slug, cmap_now)["title"]
    try:
        resolved = {}
        # apply write-ins first (create candidate if needed)
        for race, wname in list(writeins.items()):
            race = slugify(race)
            wname = norm(wname)
            if not wname:
                continue
            if len(wname) < 2 or len(wname) > 60:
                return jsonify({"error": f"Write-in for {pt(race)} must be 2-60 chars"}), 400
            try:
                cur = conn.execute("INSERT INTO candidates (race,name,created_at) VALUES (?,?,?)",
                                   (race, wname, datetime.utcnow().isoformat()))
                resolved[race] = cur.lastrowid
            except sqlite3.IntegrityError:
                row = conn.execute("SELECT id FROM candidates WHERE race=? AND name=? COLLATE NOCASE",
                                   (race, wname)).fetchone()
                resolved[race] = row["id"]
        for race, cid in choices.items():
            race = slugify(race)
            if race in resolved:
                continue
            try:
                cid = int(cid)
            except Exception:
                continue
            resolved[race] = cid

        missing = [pt(r) for r in required if r not in resolved]
        if missing:
            return jsonify({"error": "Missing vote for: " + ", ".join(missing)}), 400

        # validate candidate ids
        for race, cid in resolved.items():
            ok = conn.execute("SELECT id FROM candidates WHERE id=? AND race=?", (cid, race)).fetchone()
            if not ok:
                return jsonify({"error": f"Invalid candidate for {pt(race)}"}), 400

        try:
            cur = conn.execute("INSERT INTO votes_new (voter, created_at) VALUES (?,?)",
                               (voter, datetime.utcnow().isoformat()))
            vid = cur.lastrowid
            for race, cid in resolved.items():
                conn.execute("INSERT INTO vote_choices (vote_id, race, candidate_id) VALUES (?,?,?)",
                             (vid, race, cid))
            conn.commit()
        except sqlite3.IntegrityError:
            conn.rollback()
            return jsonify({"error": f"'{voter}' has already voted. One vote per person."}), 409
    finally:
        conn.close()
    return jsonify({"ok": True})

@app.get("/api/export")
def export_json():
    conn = get_db()
    offices = get_offices(conn)
    cands = [dict(r) for r in conn.execute("SELECT * FROM candidates ORDER BY race, name")]
    try:
        votes = [dict(r) for r in conn.execute("SELECT id, voter, created_at FROM votes_new ORDER BY id")]
        choices = [dict(r) for r in conn.execute("""
            SELECT vc.vote_id, vc.race, c.name as candidate
            FROM vote_choices vc JOIN candidates c ON c.id=vc.candidate_id
            ORDER BY vc.vote_id, vc.race
        """)]
    except Exception:
        votes, choices = [], []
    conn.close()
    return jsonify({"exported_at": datetime.utcnow().isoformat(), "offices": offices, "candidates": cands,
                    "votes": votes, "choices": choices})

@app.post("/api/reset")
def reset():
    if not ADMIN_TOKEN:
        return jsonify({"error": "Reset disabled (set ADMIN_TOKEN to enable)"}), 403
    data = request.get_json(force=True, silent=True) or {}
    if data.get("token") != ADMIN_TOKEN:
        return jsonify({"error": "Bad token"}), 403
    conn = get_db()
    if data.get("what") == "all":
        conn.execute("DELETE FROM vote_choices"); conn.execute("DELETE FROM votes_new")
        conn.execute("DELETE FROM candidates"); conn.execute("DELETE FROM custom_offices")
    else:
        conn.execute("DELETE FROM vote_choices"); conn.execute("DELETE FROM votes_new")
    conn.commit(); conn.close()
    return jsonify({"ok": True})

@app.get("/")
def index():
    return send_from_directory(os.path.dirname(__file__), "index.html")

if __name__ == "__main__":
    port = int(os.environ.get("PORT", "3000"))
    app.run(host="0.0.0.0", port=port)
