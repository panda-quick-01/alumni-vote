from flask import Flask, request, jsonify, send_from_directory
import sqlite3
import os
import re
import time
from collections import defaultdict
from datetime import datetime
from functools import wraps

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 32 * 1024  # small JSON APIs; rejects giant bodies

@app.after_request
def security_headers(resp):
    resp.headers["X-Content-Type-Options"] = "nosniff"
    resp.headers["X-Frame-Options"] = "DENY"
    resp.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
    resp.headers["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains"
    resp.headers["Content-Security-Policy"] = (
        "default-src 'self'; style-src 'self' 'unsafe-inline'; "
        "script-src 'self' 'unsafe-inline'; img-src 'self' data:; "
        "connect-src 'self'; frame-ancestors 'none'")
    # Never let browsers (especially mobiles) cache the page or API data:
    # every visit must fetch the latest version. No hard-refresh needed.
    p = request.path or "/"
    if p == "/" or p.startswith("/api/") or (ADMIN_PATH and p.strip("/") == ADMIN_PATH):
        resp.headers["Cache-Control"] = "no-store, max-age=0"
        resp.headers["Pragma"] = "no-cache"
    return resp

# Light in-memory throttle (generous: many voters may share school wifi / one IP).
_RL = defaultdict(list)

def client_ip():
    xff = request.headers.get("X-Forwarded-For", "")
    if xff:
        return xff.split(",")[0].strip()
    return request.remote_addr or "?"

def rate_limit(max_calls, per_seconds):
    def deco(fn):
        @wraps(fn)
        def wrapper(*a, **k):
            now = time.time()
            key = (fn.__name__, client_ip())
            hits = [t for t in _RL[key] if now - t < per_seconds]
            if len(hits) >= max_calls:
                return jsonify({"error": "Too many tries — please wait a minute and retry."}), 429
            hits.append(now)
            _RL[key] = hits
            return fn(*a, **k)
        return wrapper
    return deco

DB_PATH = os.environ.get("DATABASE_PATH", os.path.join(os.path.dirname(__file__), "data", "election.db"))
ADMIN_TOKEN = os.environ.get("ADMIN_TOKEN", "")
# Secret admin URL path, e.g. ADMIN_PATH=committee-9f3a2c -> site.com/committee-9f3a2c
# No Admin tab is shown publicly; only this path reveals it.
ADMIN_PATH = os.environ.get("ADMIN_PATH", "").strip().strip("/")
ELECTION_YEAR = os.environ.get("ELECTION_YEAR", "2027")
MEET_WHEN = os.environ.get("MEET_WHEN", "November 2027")
TERM_LABEL = os.environ.get("TERM_LABEL", "2026–27")
# Fraud reports: set CONTACT_NUMBER (e.g. +91 98XXX XXXXX) to show a call link
# on the endorsement card ("Someone endorsed in your name? Call …").
CONTACT_NUMBER = os.environ.get("CONTACT_NUMBER", "").strip()
CONTACT_LABEL = os.environ.get("CONTACT_LABEL", "the committee").strip() or "the committee"

# Chairman is ex-officio (Principal or his appointee) — never on the ballot.
CHAIRMAN = {"title": "Chairman", "held_by": "Principal or his appointee",
            "desc": "Ex-officio chair. Not elected here."}

# Public plan shown on the homepage so any visitor understands the road to Nov 2027.
PLAN = [
    {"phase": "1. Foundation", "when": "Sep – Dec 2026", "what": "Volunteers step forward: every batch gets 2 places per role, one person one role. The role list gets fixed once endorsements open."},
    {"phase": "2. Build", "when": "Jan – Jun 2027", "what": "Directory, school premises layout & permissions, budget, sponsors, batch coordinators, save-the-date + WhatsApp updates."},
    {"phase": "3. Execution", "when": "Jul – Oct 2027", "what": "Registrations, food, stay & travel help for outstation alumni, culture & sports program, mementos. Registration closes Oct 2027."},
    {"phase": "4. Meet + Audit", "when": "Nov – Dec 2027", "what": "Alumni Meet Nov 2027, felicitations, AGM, accounts + directory published."},
    {"phase": "Deciding ties", "when": "If equal", "what": "Most supporters wins the role. Same support? Whoever stepped forward first leads. Exact tie? Our Chairman decides."},
]
ELECTED_NOW = ["Convener", "Co-Convener", "Operations Convener", "Registration & Outreach Convener", "Finance Convener", "Logistics Convener", "Programmes Convener", "Comms & PR Convener"]
COOPT_LATER = ["Registration", "Hospitality & Stay", "Transport", "Food & Catering", "Sponsorship", "Social Media / IT", "Batch Coordinators", "Volunteers", "Safety & Medical", "Stage & Tech", "Photography", "Auditor"]

# Convener model. Chairman stays ex-officio (see CHAIRMAN). Slugs kept stable so existing ballots stay valid.
OFFICES = [
    {"slug": "president",            "title": "Convener",                          "emoji": "", "desc": "Leads the committee"},
    {"slug": "vice_president",       "title": "Co-Convener",                       "emoji": "", "desc": "Helps the convener"},
    {"slug": "general_secretary",    "title": "Operations Convener",               "emoji": "", "desc": "Meetings, notes and coordination"},
    {"slug": "joint_secretary",      "title": "Registration & Outreach Convener",  "emoji": "", "desc": "Batches and registrations"},
    {"slug": "treasurer",            "title": "Finance Convener",                  "emoji": "", "desc": "Money and accounts"},
    {"slug": "organizing_secretary", "title": "Logistics Convener",                "emoji": "", "desc": "School premises, stay, transport, volunteers"},
    {"slug": "cultural_secretary",   "title": "Programmes Convener",               "emoji": "", "desc": "Stage, games and prizes"},
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
    return {"slug": slug, "title": title, "emoji": "", "desc": "Custom job"}

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
        phone TEXT NOT NULL DEFAULT '',
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
    CREATE TABLE IF NOT EXISTS settings (
        key TEXT PRIMARY KEY,
        value TEXT NOT NULL
    );
    """)
    # Phone numbers for volunteers (admin eyes only, never in public state).
    try:
        cols0 = [r["name"] for r in conn.execute("PRAGMA table_info(candidates)")]
        if "phone" not in cols0:
            conn.execute("ALTER TABLE candidates ADD COLUMN phone TEXT NOT NULL DEFAULT ''")
            conn.commit()
    except Exception:
        pass
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
        return {r["slug"]: {"slug": r["slug"], "title": r["title"], "emoji": r["emoji"] or "",
                            "desc": r["descr"] or "Custom job"}
                for r in conn.execute("SELECT slug, title, emoji, descr FROM custom_offices")}
    except Exception:
        return {}

def get_offices(conn):
    cmap = custom_map(conn)
    offices = list(OFFICES)
    seen = {o["slug"] for o in offices}
    for r in conn.execute("SELECT slug, title, emoji, descr, created_at FROM custom_offices ORDER BY created_at"):
        if r["slug"] not in seen:
            offices.append({"slug": r["slug"], "title": r["title"], "emoji": r["emoji"] or "",
                            "desc": r["descr"] or "Custom job"})
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

# Self-nomination rules: batches 1996–2021; every batch gets 2 places per role;
# one person stands for only one role. Batch of 2021 is excused to focus on studies.
BATCH_MIN, BATCH_MAX = 1996, 2021
BATCH_STUDY_CUTOFF = 2020  # volunteers must be this batch or earlier
BATCH_SLOT_LIMIT = 2
BATCH_YEAR_RE = re.compile(r"\b(199[6-9]|200\d|202[01])\b")
ANY_YEAR_RE = re.compile(r"\b(19\d\d|20\d\d)\b")

def parse_batch(name):
    m = BATCH_YEAR_RE.search(name or "")
    return m.group(1) if m else None

def person_key(name):
    s = re.sub(r"\(.*?\)", " ", name or "")
    s = BATCH_YEAR_RE.sub(" ", s)
    return re.sub(r"\s+", " ", s).strip().lower()

def clean_phone(raw):
    d = re.sub(r"\D", "", raw or "")
    if d.startswith("91") and len(d) == 12:
        d = d[2:]
    return d if len(d) == 10 else None

def nomination_error(conn, race, name):
    """Plain-language error, or None if this name may stand for this role."""
    batch = parse_batch(name)
    if not batch:
        if ANY_YEAR_RE.search(name or ""):
            return ("Sorry, this process covers batches 1996 to 2021. "
                    "Please check the batch year.")
        return "Please add the batch year with the name, e.g. Anita Rao (2004)."
    if int(batch) > BATCH_STUDY_CUTOFF:
        return (f"Batch {batch} — thank you, but please focus on your studies for now. "
                "You can still endorse others.")
    if len(person_key(name)) < 2:
        return "Please type the full name."
    cmap = custom_map(conn)
    same_batch = 0
    for r in conn.execute("SELECT race, name FROM candidates").fetchall():
        if person_key(r["name"]) == person_key(name) and r["race"] != race:
            return (f"{norm(name)} is already standing for "
                    f"{pretty(r['race'], cmap)['title']} — one person, one job.")
        if r["race"] == race and parse_batch(r["name"]) == batch:
            same_batch += 1
    if same_batch >= BATCH_SLOT_LIMIT:
        return (f"Batch {batch} already has 2 names for "
                f"{pretty(race, cmap)['title']} — those places are full.")
    return None

def voting_open(conn):
    # Nominations-only by default; admin opens voting when ready.
    try:
        row = conn.execute("SELECT value FROM settings WHERE key='voting_open'").fetchone()
        return bool(row and row["value"] == "1")
    except Exception:
        return False

def set_voting(conn, open_):
    conn.execute("INSERT INTO settings (key, value) VALUES ('voting_open', ?) "
                 "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                 ("1" if open_ else "0",))
    conn.commit()

def admin_ok(provided):
    # Admin tools (export/reset) need the token when one is configured.
    if not ADMIN_TOKEN:
        return True
    return bool(provided) and provided == ADMIN_TOKEN

def admin_token_from_request(data=None):
    tok = request.args.get("token") or request.headers.get("X-Admin-Token")
    if tok:
        return tok
    try:
        d = data if data is not None else request.get_json(force=True, silent=True) or {}
    except Exception:
        d = {}
    return (d.get("token") if isinstance(d, dict) else None)

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
@rate_limit(30, 60)
def create_office():
    data = request.get_json(force=True, silent=True) or {}
    title = norm(data.get("title"))
    emoji = norm(data.get("emoji")) or ""
    desc = norm(data.get("desc") or data.get("description")) or "Custom job"
    first_nominee = norm(data.get("first_nominee") or data.get("firstNominee") or "")
    if not title or len(title) < 2 or len(title) > 40:
        return jsonify({"error": "Job title must be 2-40 characters"}), 400
    slug = slugify(data.get("slug") or title)
    if not slug or len(slug) < 2:
        return jsonify({"error": "Could not make a valid job from that title"}), 400
    if slug in OFFICE_MAP:
        return jsonify({"error": f"'{OFFICE_MAP[slug]['title']}' already exists"}), 409
    if len(emoji) > 8:
        emoji = emoji[:8]
    if len(desc) > 120:
        desc = desc[:120]
    conn = get_db()
    # Fairness freeze: adding an office after ballots are cast would make
    # old ballots incomplete. Block it once voting has started.
    try:
        nvotes = conn.execute("SELECT COUNT(*) c FROM votes_new").fetchone()["c"]
    except Exception:
        nvotes = 0
    if nvotes > 0:
        conn.close()
        return jsonify({"error": "Voting has started, so new jobs cannot be added."}), 409
    try:
        conn.execute("INSERT INTO custom_offices (slug, title, emoji, descr, created_at) VALUES (?,?,?,?,?)",
                     (slug, title, emoji, desc, datetime.utcnow().isoformat()))
        cid = None
        if first_nominee:
            if len(first_nominee) < 2 or len(first_nominee) > 60:
                conn.rollback(); conn.close()
                return jsonify({"error": "That name must be 2-60 characters"}), 400
            try:
                cur = conn.execute("INSERT INTO candidates (race, name, created_at) VALUES (?,?,?)",
                                   (slug, first_nominee, datetime.utcnow().isoformat()))
                cid = cur.lastrowid
            except sqlite3.IntegrityError:
                pass
        conn.commit()
    except sqlite3.IntegrityError:
        conn.close()
        return jsonify({"error": "That role already exists"}), 409
    conn.close()
    return jsonify({"ok": True, "slug": slug, "title": title, "emoji": emoji,
                    "desc": desc, "first_candidate_id": cid})

@app.delete("/api/offices/<slug>")
def delete_office(slug):
    slug = slugify(slug)
    if slug in OFFICE_MAP:
        return jsonify({"error": "The 7 main jobs cannot be removed"}), 400
    conn = get_db()
    row = conn.execute("SELECT slug FROM custom_offices WHERE slug=?", (slug,)).fetchone()
    if not row:
        # also allow removing orphan races created implicitly (no custom_offices row)
        has_c = conn.execute("SELECT 1 FROM candidates WHERE race=? LIMIT 1", (slug,)).fetchone()
        if not has_c:
            conn.close()
            return jsonify({"error": "Job not found"}), 404
    used = conn.execute("SELECT COUNT(*) c FROM vote_choices WHERE race=?", (slug,)).fetchone()["c"]
    if used:
        conn.close()
        return jsonify({"error": "Voting has started, so this job cannot be removed"}), 409
    conn.execute("DELETE FROM candidates WHERE race=?", (slug,))
    conn.execute("DELETE FROM custom_offices WHERE slug=?", (slug,))
    conn.commit(); conn.close()
    return jsonify({"ok": True})

@app.get("/api/state")
@rate_limit(600, 60)
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
    committee = {}
    for race in races:
        rows = conn.execute("""
            SELECT c.id, c.name, c.created_at, COUNT(vc.vote_id) as votes
            FROM candidates c LEFT JOIN vote_choices vc
              ON vc.candidate_id = c.id AND vc.race = ?
            WHERE c.race = ?
            GROUP BY c.id ORDER BY votes DESC, c.created_at ASC, name COLLATE NOCASE
        """, (race, race)).fetchall()
        rlist = []
        for r in rows:
            pct = round((r["votes"] / total * 100) if total else 0, 1)
            rlist.append({"id": r["id"], "name": r["name"], "votes": r["votes"], "pct": pct,
                          "since": r["created_at"]})
        results[race] = rlist
        # One holder per role. Tie rules, applied in the open:
        # 1. most supporters wins; 2. tie -> stepped forward first;
        # 3. same timestamp -> Chairman decides.
        if rlist and total > 0:
            top = rlist[0]
            tied = [x for x in rlist[1:] if x["votes"] == top["votes"]]
            if len(rlist) == 1:
                how = "only volunteer"
            elif not tied:
                how = "most supported"
            else:
                same_time = [x for x in tied if x["since"] == top["since"]]
                how = "chairman decides" if same_time else "tie — stepped forward first"
            committee[race] = {"id": top["id"], "name": top["name"], "votes": top["votes"],
                               "tie": bool(tied), "how": how,
                               "tied_with": [x["name"] for x in tied] if tied else []}
    try:
        recent = [dict(r) for r in conn.execute(f"SELECT voter, created_at FROM {vt} ORDER BY id DESC LIMIT 8")]
    except Exception:
        recent = []
    is_open = voting_open(conn)
    conn.close()
    return jsonify({
        "election": f"Alumni Meet {ELECTION_YEAR}",
        "year": ELECTION_YEAR,
        "meet_when": MEET_WHEN,
        "term": TERM_LABEL,
        "chairman": CHAIRMAN,
        "plan": PLAN,
        "elected_now": ELECTED_NOW,
        "coopt_later": COOPT_LATER,
        "offices_frozen": total > 0,
        "voting_open": is_open,
        "committee": committee,
        "admin_enabled": bool(ADMIN_TOKEN),
        "contact": {"number": CONTACT_NUMBER, "label": CONTACT_LABEL} if CONTACT_NUMBER else None,
        "offices": offices,
        "candidates": cands,
        "results": results,
        "total_votes": total,
        "recent": recent,
    })

@app.post("/api/candidates")
@rate_limit(120, 60)
def add_candidate():
    data = request.get_json(force=True, silent=True) or {}
    race = slugify(data.get("race"))
    name = norm(data.get("name"))
    if not race:
        return jsonify({"error": "Please pick a job first"}), 400
    if not name or len(name) < 2 or len(name) > 60:
        return jsonify({"error": "Name must be 2-60 characters"}), 400
    conn = get_db()
    # New race after voting started would orphan old ballots — block it.
    # Adding names to an existing office is still allowed (nominations).
    try:
        existing = {o["slug"] for o in get_offices(conn)}
        try:
            nv = conn.execute("SELECT COUNT(*) c FROM votes_new").fetchone()["c"]
        except Exception:
            nv = 0
        if nv > 0 and race not in existing:
            # allow legacy 'secretary' alias etc. only if already known
            conn.close()
            return jsonify({"error": "Voting has started, so new jobs cannot be added."}), 409
    except Exception:
        pass
    err = nomination_error(conn, race, name)
    if err:
        conn.close()
        return jsonify({"error": err}), 409
    phone = ""
    if data.get("phone"):
        phone = clean_phone(data.get("phone")) or ""
        if not phone:
            conn.close()
            return jsonify({"error": "Please check the mobile number — 10 digits."}), 400
    try:
        cur = conn.execute("INSERT INTO candidates (race, name, phone, created_at) VALUES (?,?,?,?)",
                           (race, name, phone, datetime.utcnow().isoformat()))
        conn.commit()
        cid = cur.lastrowid
    except sqlite3.IntegrityError:
        conn.close()
        return jsonify({"error": f"'{name}' is already added for {pretty(race)['title']}"}), 409
    conn.close()
    return jsonify({"ok": True, "id": cid, "race": race, "name": name})

@app.post("/api/vote")
@rate_limit(60, 60)
def vote():
    data = request.get_json(force=True, silent=True) or {}
    voter = norm(data.get("voter"))
    if not voter or len(voter) < 2 or len(voter) > 60:
        return jsonify({"error": "Please type your name"}), 400

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

    # Required races = all current offices. Offices are frozen once the
    # first ballot is cast (see POST /api/offices), so all ballots stay complete.
    conn = get_db()
    if not voting_open(conn):
        conn.close()
        return jsonify({"error": "Voting has not opened yet — we are still collecting names. Please come back when voting opens."}), 403
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
        # how many ballots exist already? (a new job mid-vote would be unfair)
        try:
            prior_votes = conn.execute("SELECT COUNT(*) c FROM votes_new").fetchone()["c"]
        except Exception:
            prior_votes = 0
        # apply write-ins first (create candidate if needed)
        for race, wname in list(writeins.items()):
            race = slugify(race)
            wname = norm(wname)
            if not wname:
                continue
            if len(wname) < 2 or len(wname) > 60:
                return jsonify({"error": f"Name for {pt(race)} must be 2-60 characters"}), 400
            if prior_votes > 0 and race not in required:
                return jsonify({"error": "Voting has started, so new jobs cannot be added."}), 409
            dup = conn.execute("SELECT id FROM candidates WHERE race=? AND name=? COLLATE NOCASE",
                               (race, wname)).fetchone()
            if dup:
                resolved[race] = dup["id"]
                continue
            err = nomination_error(conn, race, wname)
            if err:
                return jsonify({"error": err}), 409
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
            return jsonify({"error": "Please pick someone for: " + ", ".join(missing)}), 400

        # validate candidate ids
        for race, cid in resolved.items():
            ok = conn.execute("SELECT id FROM candidates WHERE id=? AND race=?", (cid, race)).fetchone()
            if not ok:
                return jsonify({"error": f"That name is not listed for {pt(race)}"}), 400

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
@rate_limit(30, 60)
def export_json():
    if not admin_ok(admin_token_from_request()):
        return jsonify({"error": "Admin only"}), 403
    return jsonify(_export_data())


@app.post("/api/export")
@rate_limit(30, 60)
def export_json_post():
    # Same data, token via JSON body (avoids putting it in URL logs).
    if not admin_ok(admin_token_from_request()):
        return jsonify({"error": "Admin only"}), 403
    return jsonify(_export_data())


def _export_data():
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
    return {"exported_at": datetime.utcnow().isoformat(), "election": f"Alumni Meet {ELECTION_YEAR}",
            "year": ELECTION_YEAR, "meet_when": MEET_WHEN, "term": TERM_LABEL,
            "plan": PLAN, "offices": offices, "candidates": cands,
            "votes": votes, "choices": choices}

@app.post("/api/reset")
@rate_limit(30, 60)
def reset():
    if not ADMIN_TOKEN:
        return jsonify({"error": "Reset disabled (set ADMIN_TOKEN to enable)"}), 403
    data = request.get_json(force=True, silent=True) or {}
    if not admin_ok(data.get("token")):
        return jsonify({"error": "Bad token"}), 403
    conn = get_db()
    if data.get("what") == "all":
        conn.execute("DELETE FROM vote_choices"); conn.execute("DELETE FROM votes_new")
        conn.execute("DELETE FROM candidates"); conn.execute("DELETE FROM custom_offices")
    else:
        conn.execute("DELETE FROM vote_choices"); conn.execute("DELETE FROM votes_new")
    conn.commit(); conn.close()
    return jsonify({"ok": True})

@app.post("/api/admin/voting")
@rate_limit(30, 60)
def admin_voting():
    if not ADMIN_TOKEN:
        return jsonify({"error": "Admin disabled (set ADMIN_TOKEN to enable)"}), 403
    data = request.get_json(force=True, silent=True) or {}
    if not admin_ok(data.get("token")):
        return jsonify({"error": "Bad token"}), 403
    want = data.get("open")
    if want not in (True, False):
        return jsonify({"error": "Send {open: true/false}"}), 400
    conn = get_db()
    set_voting(conn, want)
    conn.close()
    return jsonify({"ok": True, "voting_open": want})

@app.post("/api/admin/remove-support")
@rate_limit(30, 60)
def admin_remove_support():
    # Delete one person's endorsement (their picked names stay listed).
    if not ADMIN_TOKEN:
        return jsonify({"error": "Admin disabled (set ADMIN_TOKEN to enable)"}), 403
    data = request.get_json(force=True, silent=True) or {}
    if not admin_ok(data.get("token")):
        return jsonify({"error": "Bad token"}), 403
    voter = norm(data.get("voter"))
    if not voter:
        return jsonify({"error": "Please type the name"}), 400
    conn = get_db()
    row = conn.execute("SELECT id, voter FROM votes_new WHERE voter=? COLLATE NOCASE",
                       (voter,)).fetchone()
    if not row:
        conn.close()
        return jsonify({"error": f"No endorsement found from '{voter}'"}), 404
    conn.execute("DELETE FROM votes_new WHERE id=?", (row["id"],))
    conn.commit(); conn.close()
    return jsonify({"ok": True, "removed": row["voter"]})

@app.post("/api/admin/remove-name")
@rate_limit(30, 60)
def admin_remove_name():
    # Delete one volunteered name (only if nobody has picked it yet).
    if not ADMIN_TOKEN:
        return jsonify({"error": "Admin disabled (set ADMIN_TOKEN to enable)"}), 403
    data = request.get_json(force=True, silent=True) or {}
    if not admin_ok(data.get("token")):
        return jsonify({"error": "Bad token"}), 403
    try:
        cid = int(data.get("id"))
    except Exception:
        return jsonify({"error": "Please give a valid name id (see downloaded data)"}), 400
    conn = get_db()
    row = conn.execute("SELECT id, race, name FROM candidates WHERE id=?", (cid,)).fetchone()
    if not row:
        conn.close()
        return jsonify({"error": "Name not found"}), 404
    used = conn.execute("SELECT COUNT(*) c FROM vote_choices WHERE candidate_id=?",
                        (cid,)).fetchone()["c"]
    if used:
        conn.close()
        return jsonify({"error": f"'{row['name']}' already has support — remove those endorsements first"}), 409
    conn.execute("DELETE FROM candidates WHERE id=?", (cid,))
    conn.commit(); conn.close()
    return jsonify({"ok": True, "removed": row["name"]})

@app.get("/")
def index():
    return send_from_directory(os.path.dirname(__file__), "index.html")


@app.get("/<slug>")
def admin_route(slug):
    # Secret admin entry: only the exact ADMIN_PATH serves the app; all else 404.
    if ADMIN_PATH and slug == ADMIN_PATH:
        return send_from_directory(os.path.dirname(__file__), "index.html")
    return jsonify({"error": "Not found"}), 404

if __name__ == "__main__":
    port = int(os.environ.get("PORT", "3000"))
    app.run(host="0.0.0.0", port=port)
