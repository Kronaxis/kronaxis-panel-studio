"""
Panel Studio - Synthetic Consumer Panel Platform.

Copyright (c) 2026 Kronaxis Limited. All rights reserved.
Licensed under BSL 1.1. See LICENSE file.
https://kronaxis.co.uk | contact@kronaxis.co.uk

Standalone Flask service (port 8090) for psychographically accurate consumer panels
powered by the DYNAMICS-8 personality framework and local LLM inference.
"""

import hashlib
import os
import json
import queue
import secrets
import uuid
import logging
import threading
import time
from datetime import datetime, timezone
from functools import wraps

import requests as http_requests
import psycopg2
import psycopg2.pool
import psycopg2.extras
from flask import Flask, request, jsonify, render_template, Response, send_file, session, redirect, url_for

from response_engine import ResponseEngine
from export_engine import (
    export_conversation_jsonl,
    export_conversation_jsonl_full,
    export_conversation_parquet,
    export_conversation_csv,
    export_focus_group_jsonl,
    export_bulk_jsonl,
)
from panel_builder import start_build_job, get_job_progress, get_country_options, PANEL_PRESETS, interpret_panel_description
from scheduler import ConversationScheduler

# Register UUID adapter for psycopg2.
psycopg2.extras.register_uuid()

logging.basicConfig(level=logging.INFO, format="%(asctime)s [panel-studio] %(message)s")
log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

DATABASE_URL = os.environ.get("DATABASE_URL", "postgresql://titan:titan@localhost:5432/tfs")
PANEL_STUDIO_API_KEY = os.environ.get("PANEL_STUDIO_API_KEY", "")
FLASK_SECRET = os.environ.get("FLASK_SECRET_KEY", "")
if not FLASK_SECRET or FLASK_SECRET == "change-me":
    raise RuntimeError("FLASK_SECRET_KEY must be set to a secure random value (not 'change-me')")
AUTH_ENABLED = os.environ.get("PANEL_STUDIO_AUTH", "false").lower() == "true"
KRONAXIS_GATE_ENABLED = os.environ.get("KRONAXIS_GATE_ENABLED", "true").lower() == "true"
KRONAXIS_REGISTER_URL = os.environ.get("KRONAXIS_REGISTER_URL", "https://kronaxis.co.uk/register")

# In-memory cache for validated Kronaxis API keys: {key: (valid_bool, expiry_timestamp)}
_kronaxis_key_cache = {}
_KRONAXIS_KEY_TTL = 300  # 5 minutes

# ---------------------------------------------------------------------------
# App factory
# ---------------------------------------------------------------------------

app = Flask(__name__)
app.secret_key = FLASK_SECRET

log.info("Kronaxis Panel Studio v1.0 | kronaxis.co.uk | Built by Jason Duke")

# Connection pool.
pool = psycopg2.pool.ThreadedConnectionPool(2, 10, DATABASE_URL)


def _seed_if_empty():
    """Import seed personas on first boot if database is empty."""
    seed_path = "/seed/personas.jsonl"
    if not os.path.exists(seed_path):
        return
    conn = pool.getconn()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT COUNT(*) FROM soul_personas")
            count = cur.fetchone()[0]
        if count > 0:
            return
    finally:
        pool.putconn(conn)

    log.info("Database empty, importing seed personas from %s", seed_path)
    try:
        from import_personas import import_personas
        imported, skipped, memories = import_personas(seed_path, DATABASE_URL, "Seed Panel", 0)
        log.info("Seed import complete: %d personas, %d memories", imported, memories)
    except Exception as e:
        log.error("Seed import failed: %s", e)


_seed_if_empty()

# Response engine (local LLM-based persona cognition).
response_engine = ResponseEngine(pool)

# SSE subscriber registry: run_id -> list of queue.Queue
_sse_subscribers = {}
_sse_lock = threading.Lock()


def _stimulus_dispatch(method, path, body=None, timeout=30):
    """Route stimulus requests to the local ResponseEngine.

    The ConversationScheduler expects a callable with signature:
        (method, path, body, timeout) -> (status_code, data_dict)
    """
    try:
        # POST /api/soul/panels/{panel_id}/stimulate
        if method == "POST" and "/stimulate" in path:
            parts = path.rstrip("/").split("/")
            panel_id = parts[-2]  # .../panels/{panel_id}/stimulate
            stimulus = body.get("stimulus", "") if body else ""
            stimulus_type = body.get("stimulus_type", "standard") if body else "standard"
            sample_size = body.get("sample_size") if body else None
            run_id = response_engine.stimulate_panel(panel_id, stimulus, stimulus_type, sample_size)
            return 200, {"run_id": run_id}

        # GET /api/soul/panels/{panel_id}/runs/{run_id}
        if method == "GET" and "/runs/" in path:
            parts = path.rstrip("/").split("/")
            run_id = parts[-1]
            result = response_engine.get_run_status(run_id)
            if "error" in result:
                return 404, result
            return 200, result

        # GET /api/soul/panels/{panel_id}/segments
        if method == "GET" and "/segments" in path:
            parts = path.rstrip("/").split("/")
            panel_id = parts[-2]
            result = response_engine.get_segments(panel_id)
            return 200, result

        # POST /api/soul/panels (create panel)
        if method == "POST" and path.rstrip("/") == "/api/soul/panels":
            result = response_engine.create_panel(
                name=body.get("name", "New Panel"),
                description=body.get("description", ""),
                persona_ids=body.get("persona_ids", []),
                owner_id=body.get("owner_id"),
            )
            return 200, result

        # POST /api/soul/panels/{panel_id}/runs/{run_id}/focus-group
        if method == "POST" and "/focus-group" in path:
            parts = path.rstrip("/").split("/")
            run_id = parts[-2]  # .../runs/{run_id}/focus-group
            result = response_engine.generate_focus_group(run_id)
            if "error" in result:
                return 404, result
            return 200, result

        return 400, {"error": f"Unsupported path: {method} {path}"}
    except Exception as e:
        log.error("Response engine call failed: %s %s: %s", method, path, e)
        return 500, {"error": str(e)}


# Start conversation scheduler.
conv_scheduler = ConversationScheduler(pool, _stimulus_dispatch)
conv_scheduler.start()


def get_db():
    """Get a connection from the pool."""
    return pool.getconn()


def put_db(conn):
    """Return a connection to the pool."""
    pool.putconn(conn)


def _hash_password(password, salt=None):
    """Hash password with bcrypt-style scrypt KDF (no external dependency)."""
    if salt is None:
        salt = secrets.token_hex(16)
    h = hashlib.scrypt(password.encode(), salt=salt.encode(), n=16384, r=8, p=1, dklen=32).hex()
    return f"scrypt:{salt}:{h}"


def _verify_password(password, stored):
    """Verify password against stored hash."""
    parts = stored.split(":")
    if len(parts) == 3 and parts[0] == "scrypt":
        # New scrypt format.
        salt = parts[1]
        expected_h = parts[2]
        h = hashlib.scrypt(password.encode(), salt=salt.encode(), n=16384, r=8, p=1, dklen=32).hex()
        return h == expected_h
    elif len(parts) == 2:
        # Legacy SHA-256+salt format (migration support, read-only).
        salt = parts[0]
        h = hashlib.sha256((salt + password).encode()).hexdigest()
        return f"{salt}:{h}" == stored
    return False


def get_current_user():
    """Return current user dict from session, or None."""
    if not AUTH_ENABLED:
        return None
    user_id = session.get("user_id")
    if not user_id:
        return None
    conn = get_db()
    try:
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute("SELECT id, email, display_name, role FROM panel_studio_users WHERE id = %s", (user_id,))
        user = cur.fetchone()
        cur.close()
    finally:
        put_db(conn)
    return user


def require_auth(f):
    """Require login when AUTH_ENABLED, otherwise pass through."""
    @wraps(f)
    def decorated(*args, **kwargs):
        if not AUTH_ENABLED:
            return f(*args, **kwargs)
        if not session.get("user_id"):
            if request.is_json or request.path.startswith("/api/"):
                return jsonify({"error": "unauthorized"}), 401
            return redirect("/login")
        return f(*args, **kwargs)
    return decorated


def require_api_key(f):
    """API auth: checks session (if AUTH_ENABLED) or X-API-Key header."""
    @wraps(f)
    def decorated(*args, **kwargs):
        if AUTH_ENABLED:
            if session.get("user_id"):
                return f(*args, **kwargs)
            return jsonify({"error": "unauthorized"}), 401
        if not PANEL_STUDIO_API_KEY:
            return f(*args, **kwargs)
        key = request.headers.get("X-API-Key", "")
        if key != PANEL_STUDIO_API_KEY:
            return jsonify({"error": "unauthorized"}), 401
        return f(*args, **kwargs)
    return decorated


def _validate_kronaxis_key(key):
    """Validate a kx_ API key against kronaxis.co.uk/register via POST, with 5-minute cache."""
    now = time.time()
    cached = _kronaxis_key_cache.get(key)
    if cached and cached[1] > now:
        return cached[0]
    try:
        resp = http_requests.post(
            KRONAXIS_REGISTER_URL,
            params={"validate": ""},
            json={"key": key},
            timeout=5,
        )
        valid = resp.status_code == 200 and resp.json().get("valid", False)
    except Exception:
        # Network failure: allow if previously validated, deny if unknown
        if cached:
            return cached[0]
        log.warning("Kronaxis key validation endpoint unreachable; denying unknown key")
        return False
    _kronaxis_key_cache[key] = (valid, now + _KRONAXIS_KEY_TTL)
    return valid


def require_kronaxis_key(f):
    """Gate decorator: requires a valid X-Kronaxis-Key header when KRONAXIS_GATE_ENABLED is true.

    Validates kx_ keys against the Kronaxis registration endpoint with caching.
    """
    @wraps(f)
    def decorated(*args, **kwargs):
        if not KRONAXIS_GATE_ENABLED:
            return f(*args, **kwargs)
        key = request.headers.get("X-Kronaxis-Key", "")
        if not key:
            return jsonify({
                "error": "This feature requires a free Kronaxis account. "
                         "Register at kronaxis.co.uk/register to get your API key."
            }), 403
        if not _validate_kronaxis_key(key):
            return jsonify({"error": "Invalid API key. Check your key at kronaxis.co.uk/register."}), 403
        return f(*args, **kwargs)
    return decorated


def _owner_filter():
    """Return (sql_clause, params) for tenant isolation. Empty when auth disabled."""
    if not AUTH_ENABLED or not session.get("user_id"):
        return "", []
    return "AND owner_id = %s", [session["user_id"]]


def _check_panel_ownership(cur, panel_id):
    """Verify the current user owns the given panel. Returns True if allowed.

    When auth is disabled, always returns True.
    When auth is enabled, checks soul_panels.owner_id matches session user.
    """
    if not AUTH_ENABLED or not session.get("user_id"):
        return True
    cur.execute("SELECT owner_id FROM soul_panels WHERE id = %s", (panel_id,))
    row = cur.fetchone()
    if not row:
        return False  # panel does not exist; caller handles 404
    owner = row["owner_id"] if isinstance(row, dict) else row[0]
    return str(owner) == session["user_id"] if owner else True  # NULL owner = pre-auth panel, allow


def _check_conversation_ownership(cur, conv_id):
    """Verify the current user owns the conversation (via its panel). Returns True if allowed."""
    if not AUTH_ENABLED or not session.get("user_id"):
        return True
    cur.execute("SELECT owner_id FROM panel_conversations WHERE id = %s", (conv_id,))
    row = cur.fetchone()
    if not row:
        return False
    owner = row["owner_id"] if isinstance(row, dict) else row[0]
    return str(owner) == session["user_id"] if owner else True


# ---------------------------------------------------------------------------
# Auth routes
# ---------------------------------------------------------------------------

@app.route("/login", methods=["GET"])
def page_login():
    if not AUTH_ENABLED:
        return redirect("/")
    return render_template("login.html")


@app.route("/api/auth/login", methods=["POST"])
def api_login():
    if not AUTH_ENABLED:
        return jsonify({"error": "auth not enabled"}), 400
    data = request.get_json(force=True)
    email = (data.get("email") or "").strip().lower()
    password = data.get("password", "")
    if not email or not password:
        return jsonify({"error": "email and password required"}), 400

    conn = get_db()
    try:
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute("SELECT id, email, display_name, role, password_hash FROM panel_studio_users WHERE email = %s", (email,))
        user = cur.fetchone()
        cur.close()
    finally:
        put_db(conn)

    if not user or not _verify_password(password, user["password_hash"]):
        return jsonify({"error": "invalid credentials"}), 401

    session["user_id"] = str(user["id"])
    session["user_email"] = user["email"]
    session["user_name"] = user["display_name"]

    conn = get_db()
    try:
        cur = conn.cursor()
        cur.execute("UPDATE panel_studio_users SET last_login_at = NOW() WHERE id = %s", (user["id"],))
        conn.commit()
        cur.close()
    finally:
        put_db(conn)

    return jsonify({"id": str(user["id"]), "email": user["email"], "display_name": user["display_name"]})


@app.route("/api/auth/register", methods=["POST"])
def api_register():
    if not AUTH_ENABLED:
        return jsonify({"error": "auth not enabled"}), 400
    data = request.get_json(force=True)
    email = (data.get("email") or "").strip().lower()
    password = data.get("password", "")
    display_name = data.get("display_name", "").strip()
    if not email or not password:
        return jsonify({"error": "email and password required"}), 400
    if len(password) < 8:
        return jsonify({"error": "password must be at least 8 characters"}), 400
    if not display_name:
        display_name = email.split("@")[0]

    pw_hash = _hash_password(password)
    conn = get_db()
    try:
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        # Only allow registration if no users exist (first-run setup)
        cur.execute("SELECT count(*) AS cnt FROM panel_studio_users")
        if cur.fetchone()["cnt"] > 0:
            cur.close()
            return jsonify({"error": "registration closed, contact admin"}), 403
        cur.execute("SELECT id FROM panel_studio_users WHERE email = %s", (email,))
        if cur.fetchone():
            cur.close()
            return jsonify({"error": "email already registered"}), 409
        uid = uuid.uuid4()
        cur.execute("""
            INSERT INTO panel_studio_users (id, email, password_hash, display_name)
            VALUES (%s, %s, %s, %s) RETURNING id, email, display_name
        """, (uid, email, pw_hash, display_name))
        user = cur.fetchone()
        conn.commit()
        cur.close()
    finally:
        put_db(conn)

    session["user_id"] = str(user["id"])
    session["user_email"] = user["email"]
    session["user_name"] = user["display_name"]
    return jsonify(user), 201


@app.route("/logout")
def page_logout():
    session.clear()
    return redirect("/login" if AUTH_ENABLED else "/")


# ---------------------------------------------------------------------------
# Page routes (HTML)
# ---------------------------------------------------------------------------

@app.route("/")
@require_auth
def page_dashboard():
    owner_sql, owner_params = _owner_filter()
    conn = get_db()
    try:
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute(f"SELECT count(*) AS cnt FROM soul_panels WHERE TRUE {owner_sql}", owner_params)
        panel_count = cur.fetchone()["cnt"]
        cur.execute(f"SELECT count(*) AS cnt FROM panel_conversations WHERE TRUE {owner_sql}", owner_params)
        conv_count = cur.fetchone()["cnt"]
        cur.execute(f"""
            SELECT pc.id, pc.panel_id, pc.title, pc.turn_count, pc.total_responses,
                   pc.status, pc.created_at, sp.name AS panel_name
            FROM panel_conversations pc
            JOIN soul_panels sp ON sp.id = pc.panel_id
            WHERE TRUE {owner_sql.replace('owner_id', 'pc.owner_id')}
            ORDER BY pc.created_at DESC LIMIT 10
        """, owner_params)
        recent = cur.fetchall()
        cur.close()
    finally:
        put_db(conn)

    engine_health = response_engine.health()
    return render_template(
        "dashboard.html",
        panel_count=panel_count,
        conv_count=conv_count,
        recent=recent,
        engine_health=engine_health,
    )


@app.route("/panels")
@require_auth
def page_panels():
    return render_template("panels.html")


@app.route("/panels/<panel_id>")
@require_auth
def page_panel_detail(panel_id):
    return render_template("panel_detail.html", panel_id=panel_id)


@app.route("/panels/<panel_id>/conversation")
@require_auth
def page_new_conversation(panel_id):
    return render_template("conversation.html", panel_id=panel_id, conversation_id=None)


@app.route("/panels/<panel_id>/conversations/<conv_id>")
@require_auth
def page_conversation(panel_id, conv_id):
    return render_template("conversation.html", panel_id=panel_id, conversation_id=conv_id)


@app.route("/panels/<panel_id>/export")
@require_auth
def page_export(panel_id):
    return render_template("export.html", panel_id=panel_id)


# ---------------------------------------------------------------------------
# API routes -- Panels
# ---------------------------------------------------------------------------

@app.route("/api/panels", methods=["GET"])
@require_api_key
def api_list_panels():
    owner_sql, owner_params = _owner_filter()
    conn = get_db()
    try:
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute(f"""
            SELECT id, name, description, status, created_at,
                   array_length(persona_ids, 1) AS persona_count
            FROM soul_panels
            WHERE TRUE {owner_sql}
            ORDER BY created_at DESC
        """, owner_params)
        panels = cur.fetchall()
        cur.close()
    finally:
        put_db(conn)
    return jsonify(panels)


@app.route("/api/panels", methods=["POST"])
@require_api_key
def api_create_panel():
    data = request.get_json(force=True)
    name = data.get("name", "New Panel")
    description = data.get("description", "")
    filters = data.get("filters", {})
    source_panel = data.get("source_panel")
    direct_ids = data.get("_direct_persona_ids")

    conn = get_db()
    try:
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)

        if direct_ids:
            # Direct persona ID list (for 1-to-1 or explicit selection).
            cur.execute("SELECT id FROM soul_personas WHERE id = ANY(%s::uuid[])", (direct_ids,))
            rows = cur.fetchall()
            persona_ids = [r["id"] for r in rows]
        else:
            clauses, params = _build_filter_clauses(filters, source_panel, cur)
            if clauses is None:
                cur.close()
                return jsonify({"error": "source panel not found"}), 404

            where = " AND ".join(clauses)
            cur.execute(f"SELECT id FROM soul_personas WHERE {where}", params)
            rows = cur.fetchall()
            persona_ids = [r["id"] for r in rows]

        if not persona_ids:
            cur.close()
            return jsonify({"error": "no personas match filters"}), 400

        # Create panel via response engine.
        owner_id = session.get("user_id") if AUTH_ENABLED else None
        result = response_engine.create_panel(
            name=name,
            description=description,
            persona_ids=[str(pid) for pid in persona_ids],
            owner_id=owner_id,
        )
        cur.close()
        conn.commit()
    finally:
        put_db(conn)

    return jsonify(result), 200


@app.route("/api/panels/<panel_id>", methods=["GET"])
@require_api_key
def api_panel_detail(panel_id):
    conn = get_db()
    try:
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        if not _check_panel_ownership(cur, panel_id):
            cur.close()
            return jsonify({"error": "panel not found"}), 404
        cur.execute("""
            SELECT id, name, description, status, created_at, persona_ids
            FROM soul_panels WHERE id = %s
        """, (panel_id,))
        panel = cur.fetchone()
        if not panel:
            cur.close()
            return jsonify({"error": "panel not found"}), 404

        persona_ids = panel["persona_ids"] or []
        demographics = {}
        if persona_ids:
            cur.execute("""
                SELECT age, location, occupation, dynamics, life_narrative
                FROM soul_personas WHERE id = ANY(%s)
            """, (persona_ids,))
            personas = cur.fetchall()

            # Age histogram.
            age_bands = {"18-24": 0, "25-34": 0, "35-44": 0, "45-54": 0,
                         "55-64": 0, "65-74": 0, "75+": 0}
            regions = {}
            occupations = {}
            genders = {}
            dynamics_sums = {"D": 0, "Y": 0, "N": 0, "A": 0, "M": 0, "I": 0, "C": 0, "S": 0}
            dynamics_count = 0
            political_parties = {}
            for p in personas:
                a = p["age"] or 0
                if a < 25:
                    age_bands["18-24"] += 1
                elif a < 35:
                    age_bands["25-34"] += 1
                elif a < 45:
                    age_bands["35-44"] += 1
                elif a < 55:
                    age_bands["45-54"] += 1
                elif a < 65:
                    age_bands["55-64"] += 1
                elif a < 75:
                    age_bands["65-74"] += 1
                else:
                    age_bands["75+"] += 1

                loc = (p["location"] or "").split(",")[0].strip()
                regions[loc] = regions.get(loc, 0) + 1

                occ = p["occupation"] or "Unknown"
                occupations[occ] = occupations.get(occ, 0) + 1

                # Gender from life_narrative.
                narrative = {}
                if p.get("life_narrative"):
                    try:
                        narrative = json.loads(p["life_narrative"]) if isinstance(p["life_narrative"], str) else p["life_narrative"]
                    except (json.JSONDecodeError, TypeError):
                        pass
                gender = (narrative.get("identity", {}).get("gender") or "unknown").capitalize()
                genders[gender] = genders.get(gender, 0) + 1

                # Political party.
                party = narrative.get("political", {}).get("party_affiliation", "")
                if party:
                    political_parties[party] = political_parties.get(party, 0) + 1

                # DYNAMICS-8 averages.
                dyn = p.get("dynamics") or {}
                if isinstance(dyn, str):
                    try:
                        dyn = json.loads(dyn)
                    except (json.JSONDecodeError, TypeError):
                        dyn = {}
                has_dyn = False
                for dim in dynamics_sums:
                    if dim in dyn:
                        dynamics_sums[dim] += float(dyn[dim])
                        has_dyn = True
                if has_dyn:
                    dynamics_count += 1

            dynamics_avg = {}
            if dynamics_count > 0:
                dynamics_avg = {k: round(v / dynamics_count, 2) for k, v in dynamics_sums.items()}

            demographics = {
                "age_bands": age_bands,
                "regions": dict(sorted(regions.items(), key=lambda x: -x[1])),
                "genders": dict(sorted(genders.items(), key=lambda x: -x[1])),
                "occupations": dict(sorted(occupations.items(), key=lambda x: -x[1])[:20]),
                "political_parties": dict(sorted(political_parties.items(), key=lambda x: -x[1])),
                "dynamics_avg": dynamics_avg,
                "total": len(personas),
            }

        panel["demographics"] = demographics
        panel["persona_count"] = len(persona_ids)
        # Return persona list with id/name for filters.
        cur.execute("SELECT id, name FROM soul_personas WHERE id = ANY(%s) ORDER BY name LIMIT 200",
                    (persona_ids,))
        panel["personas"] = [{"id": str(r["id"]), "name": r["name"]} for r in cur.fetchall()]
        del panel["persona_ids"]
        cur.close()
    finally:
        put_db(conn)
    return jsonify(panel)


@app.route("/api/panels/<panel_id>", methods=["PATCH"])
@require_api_key
def api_rename_panel(panel_id):
    """Rename a panel."""
    data = request.get_json(force=True)
    name = data.get("name")
    description = data.get("description")
    if not name and description is None:
        return jsonify({"error": "name or description required"}), 400
    conn = get_db()
    try:
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        if not _check_panel_ownership(cur, panel_id):
            cur.close()
            return jsonify({"error": "panel not found"}), 404
        sets, params = [], []
        if name:
            sets.append("name = %s")
            params.append(name)
        if description is not None:
            sets.append("description = %s")
            params.append(description)
        params.append(panel_id)
        cur.execute(f"UPDATE soul_panels SET {', '.join(sets)} WHERE id = %s RETURNING id, name, description", params)
        row = cur.fetchone()
        conn.commit()
        cur.close()
    finally:
        put_db(conn)
    if not row:
        return jsonify({"error": "panel not found"}), 404
    return jsonify(row)


@app.route("/api/personas/lookup", methods=["GET"])
@require_api_key
def api_persona_lookup():
    """Look up a persona by name (within a panel)."""
    name = request.args.get("name", "").strip()
    panel_id = request.args.get("panel_id", "").strip()
    if not name:
        return jsonify({"error": "name required"}), 400
    conn = get_db()
    try:
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        if panel_id:
            cur.execute("SELECT persona_ids FROM soul_panels WHERE id = %s", (panel_id,))
            panel = cur.fetchone()
            if panel and panel["persona_ids"]:
                cur.execute("""
                    SELECT id, name, age, occupation, location, dynamics, life_narrative
                    FROM soul_personas
                    WHERE name ILIKE %s AND id = ANY(%s)
                    LIMIT 1
                """, (name, panel["persona_ids"]))
            else:
                cur.close()
                return jsonify({"error": "panel not found"}), 404
        else:
            cur.execute("""
                SELECT id, name, age, occupation, location, dynamics, life_narrative
                FROM soul_personas WHERE name ILIKE %s LIMIT 1
            """, (name,))
        persona = cur.fetchone()
        cur.close()
    finally:
        put_db(conn)
    if not persona:
        return jsonify({"error": "persona not found"}), 404

    # Parse life_narrative JSON for rich detail.
    narrative = {}
    if persona.get("life_narrative"):
        try:
            if isinstance(persona["life_narrative"], str):
                narrative = json.loads(persona["life_narrative"])
            else:
                narrative = persona["life_narrative"]
        except (json.JSONDecodeError, TypeError):
            pass

    result = {
        "id": persona["id"],
        "name": persona["name"],
        "age": persona["age"],
        "occupation": persona["occupation"],
        "location": persona["location"],
        "dynamics": persona["dynamics"],
        "identity": narrative.get("identity", {}),
        "political": narrative.get("political", {}),
        "financial": narrative.get("financial", {}),
        "beliefs": narrative.get("beliefs", {}),
        "emotional_state": narrative.get("emotional_state", {}),
        "relationships": narrative.get("relationships", []),
        "lifecycle": narrative.get("lifecycle", {}),
        "religious_cultural": narrative.get("religious_cultural", {}),
        "questionnaire": narrative.get("questionnaire", {}),
    }
    return jsonify(result)


@app.route("/api/personas/names", methods=["POST"])
@require_api_key
def api_persona_names():
    """Batch resolve persona IDs to names."""
    data = request.get_json(force=True)
    ids = data.get("ids", [])
    if not ids:
        return jsonify({})
    conn = get_db()
    try:
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute("SELECT id, name FROM soul_personas WHERE id = ANY(%s::uuid[])", (ids,))
        rows = cur.fetchall()
        cur.close()
    finally:
        put_db(conn)
    return jsonify({str(r["id"]): r["name"] for r in rows})


# ---------------------------------------------------------------------------
# API routes -- Conversations
# ---------------------------------------------------------------------------

@app.route("/api/panels/<panel_id>/conversations", methods=["GET"])
@require_api_key
def api_list_conversations(panel_id):
    conn = get_db()
    try:
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute("""
            SELECT id, title, description, turn_count, total_responses, status,
                   created_at, completed_at
            FROM panel_conversations
            WHERE panel_id = %s
            ORDER BY created_at DESC
        """, (panel_id,))
        convs = cur.fetchall()
        cur.close()
    finally:
        put_db(conn)
    return jsonify(convs)


@app.route("/api/panels/<panel_id>/conversations", methods=["POST"])
@require_api_key
def api_create_conversation(panel_id):
    data = request.get_json(force=True) if request.is_json else {}
    title = data.get("title", "Untitled conversation")
    description = data.get("description", "")

    conn = get_db()
    try:
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        conv_id = uuid.uuid4()
        cur.execute("""
            INSERT INTO panel_conversations (id, panel_id, title, description)
            VALUES (%s, %s, %s, %s)
            RETURNING id, panel_id, title, description, turn_count, status, created_at
        """, (conv_id, panel_id, title, description))
        conv = cur.fetchone()
        conn.commit()
        cur.close()
    finally:
        put_db(conn)
    return jsonify(conv), 201


@app.route("/api/panels/<panel_id>/conversations/<conv_id>", methods=["PATCH"])
@require_api_key
def api_rename_conversation(panel_id, conv_id):
    """Rename a conversation."""
    data = request.get_json(force=True)
    title = data.get("title")
    description = data.get("description")
    if not title and description is None:
        return jsonify({"error": "title or description required"}), 400
    conn = get_db()
    try:
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        sets, params = [], []
        if title:
            sets.append("title = %s")
            params.append(title)
        if description is not None:
            sets.append("description = %s")
            params.append(description)
        params.extend([conv_id, panel_id])
        cur.execute(f"UPDATE panel_conversations SET {', '.join(sets)} WHERE id = %s AND panel_id = %s RETURNING id, title, description", params)
        row = cur.fetchone()
        conn.commit()
        cur.close()
    finally:
        put_db(conn)
    if not row:
        return jsonify({"error": "conversation not found"}), 404
    return jsonify(row)


@app.route("/api/panels/<panel_id>/conversations/<conv_id>", methods=["GET"])
@require_api_key
def api_conversation_detail(panel_id, conv_id):
    conn = get_db()
    try:
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute("""
            SELECT id, panel_id, title, description, turn_count, total_responses,
                   status, created_at, completed_at
            FROM panel_conversations
            WHERE id = %s AND panel_id = %s
        """, (conv_id, panel_id))
        conv = cur.fetchone()
        if not conv:
            cur.close()
            return jsonify({"error": "conversation not found"}), 404

        cur.execute("""
            SELECT id, turn_number, stimulus, stimulus_type, run_id,
                   response_count, aggregated, created_at, completed_at
            FROM panel_conversation_turns
            WHERE conversation_id = %s
            ORDER BY turn_number
        """, (conv_id,))
        turns = cur.fetchall()
        conv["turns"] = turns
        cur.close()
    finally:
        put_db(conn)
    return jsonify(conv)


@app.route("/api/panels/<panel_id>/conversations/<conv_id>/ask", methods=["POST"])
@require_api_key
def api_ask_panel(panel_id, conv_id):
    data = request.get_json(force=True)
    stimulus = data.get("stimulus", "")
    stimulus_type = data.get("stimulus_type", "standard")
    sample_size = data.get("sample_size", 0)  # 0 = all personas
    if not stimulus.strip():
        return jsonify({"error": "stimulus is required"}), 400

    conn = get_db()
    try:
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)

        # Get next turn number.
        cur.execute("""
            SELECT COALESCE(MAX(turn_number), 0) + 1 AS next_turn
            FROM panel_conversation_turns WHERE conversation_id = %s
        """, (conv_id,))
        next_turn = cur.fetchone()["next_turn"]

        # Create turn record.
        turn_id = uuid.uuid4()
        cur.execute("""
            INSERT INTO panel_conversation_turns
                (id, conversation_id, turn_number, stimulus, stimulus_type)
            VALUES (%s, %s, %s, %s, %s)
            RETURNING id, turn_number, stimulus, created_at
        """, (turn_id, conv_id, next_turn, stimulus, stimulus_type))
        turn = cur.fetchone()
        conn.commit()

        # Submit stimulus to response engine.
        try:
            sample = int(sample_size) if sample_size and int(sample_size) > 0 else None
            run_id = response_engine.stimulate_panel(panel_id, stimulus, stimulus_type, sample)
        except Exception as e:
            log.error("Stimulate panel failed: %s", e)
            run_id = None

        if run_id:
            cur.execute("""
                UPDATE panel_conversation_turns SET run_id = %s WHERE id = %s
            """, (run_id, turn_id))
            conn.commit()

        cur.close()
    finally:
        put_db(conn)

    return jsonify({
        "turn_id": str(turn_id),
        "turn_number": next_turn,
        "run_id": run_id,
        "status": "running" if run_id else "error",
    }), 202


@app.route("/api/panels/<panel_id>/conversations/<conv_id>/status", methods=["GET"])
@require_api_key
def api_conversation_status(panel_id, conv_id):
    """Poll the latest turn's run status."""
    conn = get_db()
    try:
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute("""
            SELECT id, turn_number, run_id, response_count, completed_at
            FROM panel_conversation_turns
            WHERE conversation_id = %s
            ORDER BY turn_number DESC LIMIT 1
        """, (conv_id,))
        turn = cur.fetchone()
        cur.close()
    finally:
        put_db(conn)

    if not turn or not turn["run_id"]:
        return jsonify({"status": "no_active_turn"})

    if turn["completed_at"]:
        return jsonify({"status": "complete", "turn": turn})

    # Poll response engine for run status.
    result = response_engine.get_run_status(str(turn["run_id"]))

    # If run is complete, update turn record.
    if isinstance(result, dict) and result.get("status") in ("completed", "complete"):
        conn = get_db()
        try:
            cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
            raw = result.get("raw_responses") or []
            agg = result.get("aggregated_responses") or {}
            if isinstance(raw, str):
                try:
                    raw = json.loads(raw)
                except (json.JSONDecodeError, TypeError):
                    raw = []
            if isinstance(agg, str):
                try:
                    agg = json.loads(agg)
                except (json.JSONDecodeError, TypeError):
                    agg = {}
            response_count = len(raw) if isinstance(raw, list) else agg.get("response_count", 0)

            # Merge response_count and raw responses into aggregated for the UI.
            agg["response_count"] = response_count
            agg["raw_responses"] = raw
            aggregated = json.dumps(agg, default=str)
            cur.execute("""
                UPDATE panel_conversation_turns
                SET response_count = %s, aggregated = %s, completed_at = NOW()
                WHERE id = %s
            """, (response_count, aggregated, turn["id"]))
            cur.execute("""
                UPDATE panel_conversations
                SET turn_count = turn_count + 1,
                    total_responses = total_responses + %s
                WHERE id = %s
            """, (response_count, conv_id))
            conn.commit()
            cur.close()
        finally:
            put_db(conn)

    return jsonify({"status": result.get("status", "unknown"), "detail": result})


@app.route("/api/panels/filter-options", methods=["GET"])
@require_api_key
def api_filter_options():
    """Return all available filter values for panel creation."""
    source_panel = request.args.get("source_panel")
    conn = get_db()
    try:
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)

        # If filtering from a source panel, restrict to its personas.
        if source_panel:
            cur.execute("SELECT persona_ids FROM soul_panels WHERE id = %s", (source_panel,))
            row = cur.fetchone()
            if not row:
                cur.close()
                return jsonify({"error": "source panel not found"}), 404
            pid_filter = "id = ANY(%s)"
            pid_param = (row["persona_ids"],)
        else:
            pid_filter = "mode = 'synthetic' AND status = 'ready'"
            pid_param = ()

        cur.execute(f"""
            SELECT
                DISTINCT split_part(location, ', ', 1) AS val
            FROM soul_personas WHERE {pid_filter} AND location IS NOT NULL
            ORDER BY val
        """, pid_param)
        regions = [r["val"] for r in cur.fetchall() if r["val"]]

        cur.execute(f"""
            SELECT
                DISTINCT split_part(location, ', ', 2) AS val
            FROM soul_personas WHERE {pid_filter} AND location IS NOT NULL
            ORDER BY val
        """, pid_param)
        towns = [r["val"] for r in cur.fetchall() if r["val"]]

        cur.execute(f"""
            SELECT DISTINCT occupation AS val
            FROM soul_personas WHERE {pid_filter} AND occupation IS NOT NULL
            ORDER BY val
        """, pid_param)
        occupations = [r["val"] for r in cur.fetchall() if r["val"]]

        cur.execute(f"""
            SELECT MIN(age) AS min_age, MAX(age) AS max_age
            FROM soul_personas WHERE {pid_filter}
        """, pid_param)
        age_range = cur.fetchone()

        cur.close()
    finally:
        put_db(conn)

    return jsonify({
        "regions": regions,
        "towns": towns,
        "occupations": occupations,
        "age_min": age_range["min_age"] or 18,
        "age_max": age_range["max_age"] or 85,
    })


@app.route("/api/panels/preview-filter", methods=["POST"])
@require_api_key
def api_preview_filter():
    """Preview how many personas match given filters without creating a panel."""
    data = request.get_json(force=True)
    filters = data.get("filters", {})
    source_panel = data.get("source_panel")

    conn = get_db()
    try:
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        clauses, params = _build_filter_clauses(filters, source_panel, cur)
        if clauses is None:
            cur.close()
            return jsonify({"error": "source panel not found"}), 404

        where = " AND ".join(clauses)
        cur.execute(f"SELECT COUNT(*) AS cnt FROM soul_personas WHERE {where}", params)
        count = cur.fetchone()["cnt"]

        # Quick demographic breakdown.
        cur.execute(f"""
            SELECT
                split_part(location, ', ', 1) AS region,
                COUNT(*) AS cnt
            FROM soul_personas WHERE {where}
            GROUP BY region ORDER BY cnt DESC LIMIT 12
        """, params)
        regions = {r["region"]: r["cnt"] for r in cur.fetchall() if r["region"]}

        cur.execute(f"""
            SELECT
                CASE
                    WHEN age < 25 THEN '18-24'
                    WHEN age < 35 THEN '25-34'
                    WHEN age < 45 THEN '35-44'
                    WHEN age < 55 THEN '45-54'
                    WHEN age < 65 THEN '55-64'
                    WHEN age < 75 THEN '65-74'
                    ELSE '75+'
                END AS band,
                COUNT(*) AS cnt
            FROM soul_personas WHERE {where}
            GROUP BY band ORDER BY band
        """, params)
        age_bands = {r["band"]: r["cnt"] for r in cur.fetchall()}

        cur.close()
    finally:
        put_db(conn)

    return jsonify({
        "count": count,
        "regions": regions,
        "age_bands": age_bands,
    })


def _escape_like(s):
    """Escape SQL LIKE wildcards so user input is treated as literal text."""
    return s.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _build_filter_clauses(filters, source_panel, cur):
    """Build SQL WHERE clauses from filter dict. Returns (clauses, params) or (None, None) on error."""
    clauses = []
    params = []

    if source_panel:
        cur.execute("SELECT persona_ids FROM soul_panels WHERE id = %s", (source_panel,))
        row = cur.fetchone()
        if not row:
            return None, None
        clauses.append("id = ANY(%s)")
        params.append(row["persona_ids"])
    else:
        clauses.extend(["mode = 'synthetic'", "status = 'ready'"])

    if filters.get("regions"):
        regions = filters["regions"] if isinstance(filters["regions"], list) else [filters["regions"]]
        placeholders = ", ".join(["%s"] * len(regions))
        clauses.append(f"split_part(location, ', ', 1) IN ({placeholders})")
        params.extend(regions)

    if filters.get("towns"):
        towns = filters["towns"] if isinstance(filters["towns"], list) else [filters["towns"]]
        placeholders = ", ".join(["%s"] * len(towns))
        clauses.append(f"split_part(location, ', ', 2) IN ({placeholders})")
        params.extend(towns)

    if filters.get("occupations"):
        occs = filters["occupations"] if isinstance(filters["occupations"], list) else [filters["occupations"]]
        placeholders = ", ".join(["%s"] * len(occs))
        clauses.append(f"occupation IN ({placeholders})")
        params.extend(occs)

    if filters.get("age_min"):
        clauses.append("age >= %s")
        params.append(int(filters["age_min"]))
    if filters.get("age_max"):
        clauses.append("age <= %s")
        params.append(int(filters["age_max"]))

    # Location text search (free-text, matches anywhere in location string).
    if filters.get("location_search"):
        clauses.append("location ILIKE %s")
        escaped = _escape_like(filters["location_search"])
        params.append(f"%{escaped}%")

    if not clauses:
        clauses.append("TRUE")

    return clauses, params


@app.route("/api/panels/<panel_id>/segments", methods=["GET"])
@require_api_key
def api_panel_segments(panel_id):
    # Verify ownership.
    if AUTH_ENABLED and session.get("user_id"):
        conn = get_db()
        try:
            cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
            if not _check_panel_ownership(cur, panel_id):
                cur.close()
                return jsonify({"error": "panel not found"}), 404
            cur.close()
        finally:
            put_db(conn)
    result = response_engine.get_segments(panel_id)
    return jsonify(result), 200


@app.route("/api/panels/<panel_id>/conversations/<conv_id>/turns/<int:turn_number>/focus-group", methods=["POST"])
@require_api_key
def api_generate_focus_group(panel_id, conv_id, turn_number):
    """Generate a synthesised focus group discussion from a completed turn's responses."""
    conn = get_db()
    try:
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute("""
            SELECT run_id FROM panel_conversation_turns
            WHERE conversation_id = %s AND turn_number = %s AND completed_at IS NOT NULL
        """, (conv_id, turn_number))
        turn = cur.fetchone()
        cur.close()
    finally:
        put_db(conn)

    if not turn or not turn["run_id"]:
        return jsonify({"error": "turn not found or not yet complete"}), 404

    run_id = str(turn["run_id"])
    result = response_engine.generate_focus_group(run_id)
    if "error" in result:
        return jsonify(result), 404

    # Persist transcript for later retrieval and export.
    transcript = result.get("focus_group", "")
    speaker_count = len(result.get("participants", []))
    word_count = len(transcript.split())
    conn2 = get_db()
    try:
        cur2 = conn2.cursor()
        cur2.execute("""
            INSERT INTO panel_focus_group_transcripts
                (run_id, transcript, speaker_count, word_count, model_used)
            VALUES (%s, %s, %s, %s, %s)
        """, (run_id, transcript, speaker_count, word_count,
              os.environ.get("OLLAMA_MODEL", "")))
        conn2.commit()
        cur2.close()
    except Exception as e:
        log.warning("Failed to persist focus group transcript: %s", e)
    finally:
        put_db(conn2)

    return jsonify(result), 200


@app.route("/api/panels/<panel_id>/conversations/<conv_id>/turns/<int:turn_number>/focus-group", methods=["GET"])
@require_api_key
def api_get_focus_group(panel_id, conv_id, turn_number):
    """Retrieve an existing focus group transcript for a turn."""
    conn = get_db()
    try:
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute("""
            SELECT run_id FROM panel_conversation_turns
            WHERE conversation_id = %s AND turn_number = %s
        """, (conv_id, turn_number))
        turn = cur.fetchone()
        if not turn or not turn["run_id"]:
            cur.close()
            return jsonify({"error": "turn not found"}), 404

        cur.execute("""
            SELECT id, transcript, speaker_count, word_count, generated_at, model_used
            FROM panel_focus_group_transcripts
            WHERE run_id = %s
            ORDER BY generated_at DESC LIMIT 1
        """, (str(turn["run_id"]),))
        fg = cur.fetchone()
        cur.close()
    finally:
        put_db(conn)

    if not fg:
        return jsonify({"exists": False})
    fg["exists"] = True
    return jsonify(fg)


# ---------------------------------------------------------------------------
# API routes -- Export
# ---------------------------------------------------------------------------

@app.route("/api/panels/<panel_id>/conversations/<conv_id>/export", methods=["GET"])
@require_api_key
@require_kronaxis_key
def api_export_conversation(panel_id, conv_id):
    # Verify panel ownership before allowing export.
    if AUTH_ENABLED and session.get("user_id"):
        conn_chk = get_db()
        try:
            cur_chk = conn_chk.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
            if not _check_panel_ownership(cur_chk, panel_id):
                cur_chk.close()
                return jsonify({"error": "panel not found"}), 404
            cur_chk.close()
        finally:
            put_db(conn_chk)
    fmt = request.args.get("format", "jsonl")
    conn = get_db()
    try:
        if fmt == "jsonl":
            data, filename = export_conversation_jsonl(conv_id, conn)
            return Response(
                data,
                mimetype="application/x-ndjson",
                headers={"Content-Disposition": f"attachment; filename={filename}"},
            )
        elif fmt == "jsonl_full":
            data, filename = export_conversation_jsonl_full(conv_id, conn)
            return Response(
                data,
                mimetype="application/x-ndjson",
                headers={"Content-Disposition": f"attachment; filename={filename}"},
            )
        elif fmt == "focus_group":
            data, filename = export_focus_group_jsonl(conv_id, conn)
            if not data:
                return jsonify({"error": "no focus group transcripts for this conversation"}), 404
            return Response(
                data,
                mimetype="application/x-ndjson",
                headers={"Content-Disposition": f"attachment; filename={filename}"},
            )
        elif fmt == "parquet":
            filepath, filename = export_conversation_parquet(conv_id, conn)
            return send_file(filepath, as_attachment=True, download_name=filename)
        elif fmt == "csv":
            data, filename = export_conversation_csv(conv_id, conn)
            return Response(
                data,
                mimetype="text/csv",
                headers={"Content-Disposition": f"attachment; filename={filename}"},
            )
        else:
            return jsonify({"error": f"unsupported format: {fmt}"}), 400
    finally:
        put_db(conn)


@app.route("/api/export/training-data", methods=["POST"])
@require_api_key
@require_kronaxis_key
def api_export_training_data():
    data = request.get_json(force=True)
    conversation_ids = data.get("conversation_ids", [])
    if not conversation_ids:
        return jsonify({"error": "conversation_ids required"}), 400

    # Verify all requested conversations belong to the current user.
    if AUTH_ENABLED and session.get("user_id"):
        conn_chk = get_db()
        try:
            cur_chk = conn_chk.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
            cur_chk.execute("""
                SELECT id FROM panel_conversations
                WHERE id = ANY(%s::uuid[]) AND (owner_id IS NULL OR owner_id = %s)
            """, (conversation_ids, session["user_id"]))
            allowed = {str(r["id"]) for r in cur_chk.fetchall()}
            cur_chk.close()
        finally:
            put_db(conn_chk)
        denied = [cid for cid in conversation_ids if cid not in allowed]
        if denied:
            return jsonify({"error": "access denied to one or more conversations"}), 403

    conn = get_db()
    try:
        result, filename = export_bulk_jsonl(conversation_ids, conn)
    finally:
        put_db(conn)

    return Response(
        result,
        mimetype="application/x-ndjson",
        headers={"Content-Disposition": f"attachment; filename={filename}"},
    )


# ---------------------------------------------------------------------------
# API routes -- Panel Builder (self-service generation)
# ---------------------------------------------------------------------------

@app.route("/build")
@require_auth
def page_build():
    return render_template("build.html")


@app.route("/api/build/presets", methods=["GET"])
@require_api_key
def api_build_presets():
    """Return available panel presets."""
    return jsonify(PANEL_PRESETS)


@app.route("/api/build/country-options", methods=["GET"])
@require_api_key
def api_country_options():
    """Return filter options for a country (regions, ethnicities, parties, etc.)."""
    country = request.args.get("country", "United Kingdom").strip()
    if not country:
        return jsonify({"error": "country parameter required"}), 400
    import asyncio as _aio
    loop = _aio.new_event_loop()
    try:
        opts = loop.run_until_complete(get_country_options(country))
    finally:
        loop.close()
    return jsonify(opts)


@app.route("/api/build/interpret", methods=["POST"])
@require_api_key
def api_interpret_description():
    """Interpret a free-form English description into a structured panel spec."""
    data = request.get_json(force=True)
    description = (data.get("description") or "").strip()
    if not description:
        return jsonify({"error": "description is required"}), 400
    if len(description) > 2000:
        return jsonify({"error": "description too long (max 2000 chars)"}), 400

    import asyncio as _aio
    loop = _aio.new_event_loop()
    try:
        result = loop.run_until_complete(interpret_panel_description(description))
    except Exception as e:
        log.error("Interpret description failed: %s", e)
        return jsonify({"error": str(e)}), 500
    finally:
        loop.close()
    return jsonify(result)


@app.route("/api/build", methods=["POST"])
@require_api_key
@require_kronaxis_key
def api_start_build():
    """Start a panel build job."""
    data = request.get_json(force=True)
    name = data.get("name", "").strip()
    country = data.get("country", "United Kingdom").strip()
    target_count = int(data.get("target_count", 100))
    demographic_spec = data.get("demographic_spec")
    persona_guidance = data.get("persona_guidance", "").strip() or None

    if not name:
        return jsonify({"error": "name is required"}), 400
    if target_count < 10 or target_count > 10000:
        return jsonify({"error": "target_count must be 10-10000"}), 400

    job_id = uuid.uuid4()
    conn = get_db()
    try:
        cur = conn.cursor()
        cur.execute("""
            INSERT INTO panel_build_jobs (id, name, country, target_count, demographic_spec, status)
            VALUES (%s, %s, %s, %s, %s, 'pending')
        """, (job_id, name, country, target_count,
              json.dumps(demographic_spec) if demographic_spec else None))
        conn.commit()
        cur.close()
    finally:
        put_db(conn)

    start_build_job(job_id, name, country, target_count, demographic_spec, pool,
                    persona_guidance=persona_guidance)
    return jsonify({"job_id": str(job_id), "status": "pending"}), 202


@app.route("/api/build", methods=["GET"])
@require_api_key
def api_list_builds():
    """List all build jobs."""
    conn = get_db()
    try:
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute("""
            SELECT id, name, country, target_count, status, progress_current,
                   progress_total, progress_pass, panel_id, error_message,
                   created_at, completed_at
            FROM panel_build_jobs
            ORDER BY created_at DESC
            LIMIT 50
        """)
        jobs = cur.fetchall()
        cur.close()
    finally:
        put_db(conn)
    return jsonify(jobs)


@app.route("/api/build/<job_id>", methods=["GET"])
@require_api_key
def api_build_status(job_id):
    """Get build job status with live progress."""
    conn = get_db()
    try:
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute("""
            SELECT id, name, country, target_count, status, progress_current,
                   progress_total, progress_pass, panel_id, error_message,
                   created_at, completed_at
            FROM panel_build_jobs WHERE id = %s
        """, (job_id,))
        job = cur.fetchone()
        cur.close()
    finally:
        put_db(conn)
    if not job:
        return jsonify({"error": "job not found"}), 404

    # Overlay live in-memory progress if available.
    live = get_job_progress(job_id)
    if live:
        job["progress_current"] = live["current"]
        job["progress_total"] = live["total"]
        job["progress_pass"] = live["pass"]

    return jsonify(job)


# ---------------------------------------------------------------------------
# API routes -- Sub-panel from conversation results
# ---------------------------------------------------------------------------

@app.route("/api/panels/<panel_id>/conversations/<conv_id>/turns/<int:turn_number>/subpanel", methods=["POST"])
@require_api_key
def api_create_subpanel_from_results(panel_id, conv_id, turn_number):
    """Create a sub-panel from personas matching a sentiment filter on a turn's results."""
    data = request.get_json(force=True)
    sentiment_filter = data.get("sentiment")  # positive, negative, neutral, mixed
    name = data.get("name", "")

    if not sentiment_filter:
        return jsonify({"error": "sentiment filter required"}), 400

    conn = get_db()
    try:
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute("""
            SELECT aggregated FROM panel_conversation_turns
            WHERE conversation_id = %s AND turn_number = %s AND completed_at IS NOT NULL
        """, (conv_id, turn_number))
        turn = cur.fetchone()
        if not turn or not turn["aggregated"]:
            cur.close()
            return jsonify({"error": "turn not found or not complete"}), 404

        agg = turn["aggregated"]
        responses = agg.get("raw_responses", agg.get("responses", []))
        matching_ids = [
            r["persona_id"] for r in responses
            if r.get("sentiment") == sentiment_filter and r.get("persona_id")
        ]

        if not matching_ids:
            cur.close()
            return jsonify({"error": f"no personas with sentiment '{sentiment_filter}'"}), 400

        # Resolve to UUID list.
        cur.execute("SELECT id FROM soul_personas WHERE id = ANY(%s::uuid[])", (matching_ids,))
        persona_uuids = [r["id"] for r in cur.fetchall()]

        if not persona_uuids:
            cur.close()
            return jsonify({"error": "no valid persona IDs found"}), 400

        if not name:
            name = f"{sentiment_filter.capitalize()} respondents -- Turn {turn_number}"

        owner_id = session.get("user_id") if AUTH_ENABLED else None
        result = response_engine.create_panel(
            name=name,
            description=f"Sub-panel from {sentiment_filter} responses in turn {turn_number}",
            persona_ids=[str(pid) for pid in persona_uuids],
            owner_id=owner_id,
        )
        cur.close()
    finally:
        put_db(conn)

    return jsonify(result), 200


# ---------------------------------------------------------------------------
# API routes -- Cross-panel comparison
# ---------------------------------------------------------------------------

@app.route("/compare")
@require_auth
def page_compare():
    return render_template("compare.html")


@app.route("/api/compare", methods=["POST"])
@require_api_key
def api_cross_panel_compare():
    """Submit the same stimulus to multiple panels and return comparison data."""
    data = request.get_json(force=True)
    panel_ids = data.get("panel_ids", [])
    stimulus = data.get("stimulus", "").strip()
    stimulus_type = data.get("stimulus_type", "standard")
    sample_size = data.get("sample_size", 0)

    if not stimulus:
        return jsonify({"error": "stimulus required"}), 400
    if len(panel_ids) < 2:
        return jsonify({"error": "at least 2 panel_ids required"}), 400

    results = {}
    for pid in panel_ids:
        try:
            sample = int(sample_size) if sample_size and int(sample_size) > 0 else None
            run_id = response_engine.stimulate_panel(pid, stimulus, stimulus_type, sample)
            results[pid] = {"status_code": 200, "run_id": run_id}
        except Exception as e:
            log.error("Cross-panel stimulate failed for %s: %s", pid, e)
            results[pid] = {"status_code": 500, "run_id": None}

    return jsonify({"stimulus": stimulus, "panels": results}), 202


# ---------------------------------------------------------------------------
# API routes -- Scheduled / longitudinal conversations
# ---------------------------------------------------------------------------

@app.route("/api/schedules", methods=["POST"])
@require_api_key
def api_create_schedule():
    """Create a recurring schedule for a conversation."""
    data = request.get_json(force=True)
    panel_id = data.get("panel_id")
    conversation_id = data.get("conversation_id")
    stimulus = data.get("stimulus", "").strip()
    stimulus_type = data.get("stimulus_type", "standard")
    sample_size = data.get("sample_size", 0)
    cron_expression = data.get("cron_expression", "@daily")
    max_runs = int(data.get("max_runs", 0))

    if not panel_id or not conversation_id or not stimulus:
        return jsonify({"error": "panel_id, conversation_id, and stimulus required"}), 400

    from scheduler import _parse_cron
    next_run = _parse_cron(cron_expression)

    conn = get_db()
    try:
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        sid = uuid.uuid4()
        cur.execute("""
            INSERT INTO panel_schedules
                (id, panel_id, conversation_id, stimulus, stimulus_type, sample_size,
                 cron_expression, next_run_at, max_runs, status)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, 'active')
            RETURNING id, panel_id, conversation_id, stimulus, cron_expression,
                      next_run_at, max_runs, status
        """, (sid, panel_id, conversation_id, stimulus, stimulus_type,
              sample_size if sample_size > 0 else None,
              cron_expression, next_run, max_runs))
        schedule = cur.fetchone()
        conn.commit()
        cur.close()
    finally:
        put_db(conn)

    return jsonify(schedule), 201


@app.route("/api/schedules", methods=["GET"])
@require_api_key
def api_list_schedules():
    """List all schedules."""
    conn = get_db()
    try:
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute("""
            SELECT ps.id, ps.panel_id, ps.conversation_id, ps.stimulus, ps.stimulus_type,
                   ps.cron_expression, ps.next_run_at, ps.last_run_at, ps.run_count,
                   ps.max_runs, ps.status, sp.name AS panel_name, pc.title AS conversation_title
            FROM panel_schedules ps
            JOIN soul_panels sp ON sp.id = ps.panel_id
            JOIN panel_conversations pc ON pc.id = ps.conversation_id
            ORDER BY ps.created_at DESC
        """)
        schedules = cur.fetchall()
        cur.close()
    finally:
        put_db(conn)
    return jsonify(schedules)


@app.route("/api/schedules/<schedule_id>", methods=["DELETE"])
@require_api_key
def api_delete_schedule(schedule_id):
    """Cancel a schedule."""
    conn = get_db()
    try:
        cur = conn.cursor()
        cur.execute("UPDATE panel_schedules SET status='cancelled' WHERE id = %s", (schedule_id,))
        conn.commit()
        cur.close()
    finally:
        put_db(conn)
    return jsonify({"status": "cancelled"})


@app.route("/api/schedules/<schedule_id>", methods=["PATCH"])
@require_api_key
def api_update_schedule(schedule_id):
    """Pause or resume a schedule."""
    data = request.get_json(force=True)
    action = data.get("action")
    if action == "pause":
        new_status = "paused"
    elif action == "resume":
        new_status = "active"
    else:
        return jsonify({"error": "action must be 'pause' or 'resume'"}), 400

    conn = get_db()
    try:
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute("""
            UPDATE panel_schedules SET status = %s WHERE id = %s
            RETURNING id, status
        """, (new_status, schedule_id))
        row = cur.fetchone()
        conn.commit()
        cur.close()
    finally:
        put_db(conn)
    if not row:
        return jsonify({"error": "schedule not found"}), 404
    return jsonify(row)


# ---------------------------------------------------------------------------
# API routes -- SSE progress streaming
# ---------------------------------------------------------------------------

@app.route("/api/panels/<panel_id>/conversations/<conv_id>/stream", methods=["GET"])
def api_sse_stream(panel_id, conv_id):
    """Server-Sent Events stream for real-time progress updates."""

    def event_stream():
        q = queue.Queue()
        sub_key = f"{panel_id}:{conv_id}"

        with _sse_lock:
            if sub_key not in _sse_subscribers:
                _sse_subscribers[sub_key] = []
            _sse_subscribers[sub_key].append(q)

        try:
            # Poll for updates on a tight loop, forwarding run status.
            last_status = None
            while True:
                # Check run status.
                conn = get_db()
                try:
                    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
                    cur.execute("""
                        SELECT run_id, completed_at FROM panel_conversation_turns
                        WHERE conversation_id = %s ORDER BY turn_number DESC LIMIT 1
                    """, (conv_id,))
                    turn = cur.fetchone()
                    cur.close()
                finally:
                    put_db(conn)

                if not turn or not turn.get("run_id"):
                    yield f"data: {json.dumps({'status': 'no_active_turn'})}\n\n"
                    time.sleep(2)
                    continue

                if turn.get("completed_at"):
                    yield f"data: {json.dumps({'status': 'complete'})}\n\n"
                    break

                result = response_engine.get_run_status(str(turn["run_id"]))
                if isinstance(result, dict):
                    status = result.get("status", "unknown")
                    raw = result.get("raw_responses")
                    response_count = 0
                    if raw:
                        if isinstance(raw, list):
                            response_count = len(raw)
                        elif isinstance(raw, str):
                            try:
                                response_count = len(json.loads(raw))
                            except (json.JSONDecodeError, TypeError):
                                pass

                    event_data = {
                        "status": status,
                        "completed": response_count,
                        "total": result.get("response_count", response_count),
                    }

                    if event_data != last_status:
                        yield f"data: {json.dumps(event_data)}\n\n"
                        last_status = event_data

                    if status in ("completed", "complete"):
                        break

                time.sleep(1.5)
        finally:
            with _sse_lock:
                subs = _sse_subscribers.get(sub_key, [])
                if q in subs:
                    subs.remove(q)

    return Response(event_stream(), mimetype="text/event-stream",
                    headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


# ---------------------------------------------------------------------------
# API routes -- Cost dashboard (unit economics)
# ---------------------------------------------------------------------------

@app.route("/costs")
@require_auth
def page_costs():
    return render_template("costs.html")


@app.route("/api/costs/unit-economics", methods=["GET"])
@require_api_key
def api_unit_economics():
    """Per-response, per-turn, per-panel cost breakdown."""
    conn = get_db()
    try:
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)

        # Total responses and cost.
        cur.execute("""
            SELECT
                COUNT(*) AS total_turns,
                COALESCE(SUM(response_count), 0) AS total_responses
            FROM panel_conversation_turns
            WHERE completed_at IS NOT NULL
        """)
        turn_stats = cur.fetchone()

        cur.execute("""
            SELECT
                COALESCE(SUM(estimated_cost), 0)::float AS total_cost,
                COUNT(*) AS total_calls
            FROM llm_call_log WHERE success = true
        """)
        cost_stats = cur.fetchone()

        total_cost = cost_stats["total_cost"]
        total_responses = turn_stats["total_responses"]
        total_turns = turn_stats["total_turns"]

        cost_per_response = total_cost / total_responses if total_responses > 0 else 0
        cost_per_turn = total_cost / total_turns if total_turns > 0 else 0

        # Per-panel breakdown.
        cur.execute("""
            SELECT sp.id, sp.name,
                   COUNT(DISTINCT pct.id) AS turns,
                   COALESCE(SUM(pct.response_count), 0) AS responses,
                   array_length(sp.persona_ids, 1) AS panel_size
            FROM soul_panels sp
            JOIN panel_conversations pc ON pc.panel_id = sp.id
            JOIN panel_conversation_turns pct ON pct.conversation_id = pc.id AND pct.completed_at IS NOT NULL
            GROUP BY sp.id, sp.name
            ORDER BY responses DESC
            LIMIT 20
        """)
        panels = cur.fetchall()

        # Daily cost time series (last 30 days).
        cur.execute("""
            SELECT DATE(created_at) AS day,
                   COALESCE(SUM(estimated_cost), 0)::float AS cost,
                   COUNT(*) AS calls
            FROM llm_call_log
            WHERE success = true AND created_at >= CURRENT_DATE - INTERVAL '30 days'
            GROUP BY DATE(created_at)
            ORDER BY day
        """)
        daily = cur.fetchall()

        cur.close()
    finally:
        put_db(conn)

    return jsonify({
        "cost_per_response": round(cost_per_response, 6),
        "cost_per_turn": round(cost_per_turn, 4),
        "total_cost": round(total_cost, 4),
        "total_responses": total_responses,
        "total_turns": total_turns,
        "total_calls": cost_stats["total_calls"],
        "panels": panels,
        "daily": daily,
    })


# ---------------------------------------------------------------------------
# API routes -- Conjoint analysis
# ---------------------------------------------------------------------------

@app.route("/panels/<panel_id>/conjoint")
@require_auth
def page_conjoint(panel_id):
    return render_template("conjoint.html", panel_id=panel_id)


@app.route("/api/panels/<panel_id>/conjoint", methods=["POST"])
@require_api_key
def api_create_conjoint(panel_id):
    """Create a conjoint study with product attributes and levels."""
    data = request.get_json(force=True)
    name = data.get("name", "").strip()
    attributes = data.get("attributes", [])

    if not name:
        return jsonify({"error": "name required"}), 400
    if not attributes or len(attributes) < 2:
        return jsonify({"error": "at least 2 attributes required"}), 400

    # Validate attribute structure.
    for attr in attributes:
        if not attr.get("name") or not attr.get("levels") or len(attr["levels"]) < 2:
            return jsonify({"error": f"attribute '{attr.get('name', '?')}' needs a name and at least 2 levels"}), 400

    # Generate choice profiles using fractional factorial design.
    profiles = _generate_conjoint_profiles(attributes)

    conn = get_db()
    try:
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        study_id = uuid.uuid4()
        owner_id = session.get("user_id") if AUTH_ENABLED else None
        cur.execute("""
            INSERT INTO panel_conjoint_studies (id, panel_id, name, attributes, profiles, owner_id)
            VALUES (%s, %s, %s, %s, %s, %s)
            RETURNING id, panel_id, name, attributes, profiles, status, created_at
        """, (study_id, panel_id, name, json.dumps(attributes), json.dumps(profiles), owner_id))
        study = cur.fetchone()
        conn.commit()
        cur.close()
    finally:
        put_db(conn)

    return jsonify(study), 201


@app.route("/api/panels/<panel_id>/conjoint", methods=["GET"])
@require_api_key
def api_list_conjoint(panel_id):
    """List conjoint studies for a panel."""
    conn = get_db()
    try:
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute("""
            SELECT id, name, status, created_at, completed_at
            FROM panel_conjoint_studies WHERE panel_id = %s
            ORDER BY created_at DESC
        """, (panel_id,))
        studies = cur.fetchall()
        cur.close()
    finally:
        put_db(conn)
    return jsonify(studies)


@app.route("/api/panels/<panel_id>/conjoint/<study_id>", methods=["GET"])
@require_api_key
def api_conjoint_detail(panel_id, study_id):
    """Get conjoint study detail with results."""
    conn = get_db()
    try:
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute("""
            SELECT id, panel_id, name, attributes, profiles, results, status,
                   conversation_id, created_at, completed_at
            FROM panel_conjoint_studies WHERE id = %s AND panel_id = %s
        """, (study_id, panel_id))
        study = cur.fetchone()
        cur.close()
    finally:
        put_db(conn)
    if not study:
        return jsonify({"error": "study not found"}), 404
    return jsonify(study)


@app.route("/api/panels/<panel_id>/conjoint/<study_id>/run", methods=["POST"])
@require_api_key
def api_run_conjoint(panel_id, study_id):
    """Run the conjoint study by submitting profile pairs to the panel."""
    conn = get_db()
    try:
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute("SELECT * FROM panel_conjoint_studies WHERE id = %s AND panel_id = %s", (study_id, panel_id))
        study = cur.fetchone()
        if not study:
            cur.close()
            return jsonify({"error": "study not found"}), 404

        # Create a conversation for the study.
        conv_id = uuid.uuid4()
        cur.execute("""
            INSERT INTO panel_conversations (id, panel_id, title, description)
            VALUES (%s, %s, %s, %s)
        """, (conv_id, panel_id, f"Conjoint: {study['name']}", "Automated conjoint analysis"))

        cur.execute("UPDATE panel_conjoint_studies SET conversation_id = %s, status = 'running' WHERE id = %s",
                    (conv_id, study_id))
        conn.commit()
        cur.close()
    finally:
        put_db(conn)

    profiles = study["profiles"]
    attributes = study["attributes"]
    attr_names = [a["name"] for a in attributes]

    # Build choice-set stimuli and submit each as a turn.
    run_ids = []
    for i in range(0, len(profiles), 2):
        if i + 1 >= len(profiles):
            break
        a = profiles[i]
        b = profiles[i + 1]

        stimulus = _format_conjoint_stimulus(attr_names, a, b, i // 2 + 1)

        # Create turn.
        conn = get_db()
        try:
            cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
            turn_id = uuid.uuid4()
            turn_num = (i // 2) + 1
            cur.execute("""
                INSERT INTO panel_conversation_turns
                    (id, conversation_id, turn_number, stimulus, stimulus_type)
                VALUES (%s, %s, %s, %s, 'conjoint')
            """, (turn_id, conv_id, turn_num, stimulus))
            conn.commit()
            cur.close()
        finally:
            put_db(conn)

        # Submit to response engine.
        try:
            rid = response_engine.stimulate_panel(panel_id, stimulus, "conjoint")
        except Exception as e:
            log.error("Conjoint stimulate failed: %s", e)
            rid = None
        if rid:
            run_ids.append(rid)
            conn = get_db()
            try:
                cur = conn.cursor()
                cur.execute("UPDATE panel_conversation_turns SET run_id = %s WHERE id = %s", (rid, turn_id))
                conn.commit()
                cur.close()
            finally:
                put_db(conn)

    return jsonify({
        "study_id": str(study_id),
        "conversation_id": str(conv_id),
        "choice_sets": len(profiles) // 2,
        "run_ids": run_ids,
    }), 202


def _generate_conjoint_profiles(attributes, max_profiles=16):
    """Generate balanced conjoint product profiles using random sampling."""
    import itertools
    import random

    level_lists = [attr["levels"] for attr in attributes]
    all_combos = list(itertools.product(*level_lists))

    if len(all_combos) <= max_profiles:
        profiles = [list(c) for c in all_combos]
    else:
        profiles = [list(c) for c in random.sample(all_combos, max_profiles)]

    # Ensure even number for pairwise comparison.
    if len(profiles) % 2 != 0:
        remaining = [c for c in all_combos if list(c) not in profiles]
        if remaining:
            profiles.append(list(random.choice(remaining)))
        else:
            profiles = profiles[:-1]

    random.shuffle(profiles)
    return profiles


def _format_conjoint_stimulus(attr_names, profile_a, profile_b, choice_num):
    """Format a conjoint choice-set as a clear stimulus for the panel."""
    lines = [f"Choice Set {choice_num}: Compare these two product options and state which you prefer and why.\n"]
    lines.append("Option A:")
    for name, val in zip(attr_names, profile_a):
        lines.append(f"  - {name}: {val}")
    lines.append("\nOption B:")
    for name, val in zip(attr_names, profile_b):
        lines.append(f"  - {name}: {val}")
    lines.append("\nState your choice (A or B) and explain your reasoning. "
                 "Consider how each attribute influences your preference.")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# API routes -- Response engine health
# ---------------------------------------------------------------------------

@app.route("/api/health", methods=["GET"])
def api_health():
    """Return response engine health status."""
    return jsonify(response_engine.health())


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8090, debug=False)
