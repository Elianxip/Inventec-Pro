import os
import io
import csv
import secrets
import sqlite3
import re
import time
import logging
import threading
from urllib.parse import unquote
from datetime import datetime, date, timedelta
from functools import wraps
from flask import Flask, render_template, request, redirect, url_for, session, flash, abort, make_response, jsonify
from werkzeug.exceptions import HTTPException
from werkzeug.security import generate_password_hash, check_password_hash

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("inventec")

APP_DIR = os.path.abspath(os.path.dirname(__file__))
DB_PATH = os.path.join(APP_DIR, "inventec.db")
DATABASE_URL = os.environ.get("DATABASE_URL", "").strip()
# Alternativa más segura frente a caracteres especiales: variables separadas (ver README).
DB_HOST = os.environ.get("DB_HOST", "").strip()
USE_POSTGRES = bool(DATABASE_URL or DB_HOST)
if USE_POSTGRES:
    import psycopg2
    from psycopg2.extras import DictCursor
    DB_INTEGRITY_ERROR = psycopg2.IntegrityError
    DB_OPERATIONAL_ERRORS = (psycopg2.OperationalError, psycopg2.InterfaceError)
else:
    DB_INTEGRITY_ERROR = sqlite3.IntegrityError
    DB_OPERATIONAL_ERRORS = (sqlite3.OperationalError,)


class DatabaseUnavailable(Exception):
    """La base de datos no se puede usar ahora mismo. El mensaje es seguro de mostrar (no contiene secretos)."""


_URL_RE = re.compile(
    r"^postgres(?:ql)?://(?P<user>[^:@/]*):(?P<password>.*)@(?P<host>[^:/@?]+)"
    r"(?::(?P<port>\d+))?(?:/(?P<dbname>[^?]*))?(?:\?(?P<query>.*))?$", re.S)


def build_pg_config():
    """Construye los parámetros de conexión a PostgreSQL y valida los errores típicos de Supabase.

    Admite DATABASE_URL (postgresql://usuario:clave@host:puerto/base) o bien las variables
    separadas DB_HOST, DB_PORT, DB_NAME, DB_USER y DB_PASSWORD. La contraseña nunca se registra.
    """
    if DB_HOST:
        user = os.environ.get("DB_USER", "")
        password = os.environ.get("DB_PASSWORD", "")
        cfg = {"host": DB_HOST, "port": int(os.environ.get("DB_PORT") or 5432),
               "dbname": os.environ.get("DB_NAME") or "postgres", "user": user.strip(), "password": password}
        if user != user.strip() or password != password.strip():
            log.warning("DB_USER o DB_PASSWORD tienen espacios o saltos de línea al principio/final. "
                        "Si la autenticación falla, vuelve a pegar el valor sin espacios.")
    else:
        m = _URL_RE.match(DATABASE_URL)
        if not m:
            raise DatabaseUnavailable("DATABASE_URL no tiene el formato postgresql://usuario:contraseña@host:5432/postgres")
        # La contraseña puede contener '@', '#', '/'...: se toma hasta la ÚLTIMA '@' y se decodifica (%40 -> @).
        cfg = {"host": m["host"], "port": int(m["port"] or 5432), "dbname": m["dbname"] or "postgres",
               "user": unquote(m["user"]), "password": unquote(m["password"])}
        query = m["query"] or ""
        for part in query.split("&"):
            if part.startswith("sslmode="):
                cfg["sslmode"] = part.split("=", 1)[1]
    pw = cfg["password"]
    if not cfg["user"] or not pw:
        raise DatabaseUnavailable("Falta el usuario o la contraseña de la base de datos en las variables de entorno.")
    if "YOUR-PASSWORD" in pw.upper() or (pw.startswith("[") and pw.endswith("]")):
        raise DatabaseUnavailable("La contraseña sigue siendo el marcador [YOUR-PASSWORD] (o conserva los corchetes). "
                                  "Escribe la contraseña real, sin corchetes.")
    if cfg["host"].endswith(".pooler.supabase.com") and "." not in cfg["user"]:
        raise DatabaseUnavailable(
            f"El usuario '{cfg['user']}' no es válido para el pooler de Supabase: debe tener la forma "
            "postgres.<referencia-del-proyecto> (por ejemplo postgres.abcdefghijklmnop).")
    if cfg["host"].startswith("db.") and cfg["host"].endswith(".supabase.co"):
        log.warning("Se usa la conexión directa de Supabase (IPv6). Render solo soporta IPv4: usa el host del Session Pooler.")
    cfg.setdefault("sslmode", "require")
    return cfg


def explain_pg_error(exc, cfg):
    """Traduce un error de psycopg2 a una pista accionable (sin revelar secretos)."""
    msg = str(exc).strip()
    low = msg.lower()
    if "password authentication failed" in low or "authentication failed" in low:
        return (f"Autenticación rechazada para el usuario '{cfg['user']}'. Revisa: (1) que la contraseña sea la "
                "actual de la base de datos (Supabase > Project Settings > Database > Reset database password), "
                "(2) que el usuario sea postgres.<referencia> en el pooler, (3) que no haya espacios ni "
                "caracteres sin codificar en la contraseña.")
    if "tenant or user not found" in low:
        return ("El pooler de Supabase no reconoce el proyecto o el usuario. Comprueba la referencia del proyecto "
                "en el usuario (postgres.<referencia>) y que la región del host (aws-X-región) sea la de tu proyecto.")
    if "network is unreachable" in low:
        return "Red inalcanzable (probablemente IPv6). Usa el host del Session Pooler de Supabase, no el host directo."
    if "could not translate host" in low or "name or service not known" in low:
        return f"No se pudo resolver el host '{cfg['host']}'. Revisa que esté bien escrito."
    if "timeout" in low or "timed out" in low:
        return "Tiempo de espera agotado al conectar con Supabase. Puede ser un fallo temporal de red; se reintentará."
    if "too many" in low or "max client" in low or "remaining connection slots" in low:
        return "Supabase rechazó la conexión por exceso de conexiones simultáneas."
    return "No se pudo conectar con PostgreSQL. Revisa los registros del servidor para más detalles."


_PG_CFG = None
_PG_CFG_ERROR = None
if USE_POSTGRES:
    try:
        _PG_CFG = build_pg_config()
        log.info("PostgreSQL configurado: usuario=%s host=%s puerto=%s base=%s sslmode=%s",
                 _PG_CFG["user"], _PG_CFG["host"], _PG_CFG["port"], _PG_CFG["dbname"], _PG_CFG["sslmode"])
    except DatabaseUnavailable as exc:
        _PG_CFG_ERROR = str(exc)
        log.error("Configuración de base de datos inválida: %s", exc)
else:
    log.info("Sin DATABASE_URL: se usa SQLite local (%s)", DB_PATH)

# Si defines INVENTEC_SETUP_TOKEN en Render, /setup exige ese código para crear el primer administrador.
SETUP_TOKEN = os.environ.get("INVENTEC_SETUP_TOKEN", "")
if os.environ.get("RENDER") and not SETUP_TOKEN:
    log.warning("INVENTEC_SETUP_TOKEN no está definido: quien abra /setup primero podrá crear el administrador.")

PAGE_SIZE = 25
LIKE_OP = "ILIKE" if USE_POSTGRES else "LIKE"  # PostgreSQL distingue mayúsculas en LIKE; SQLite no.

app = Flask(__name__)
app.config["SECRET_KEY"] = os.environ.get("INVENTEC_SECRET_KEY") or secrets.token_hex(32)
if not os.environ.get("INVENTEC_SECRET_KEY") and os.environ.get("RENDER"):
    raise RuntimeError("Configura INVENTEC_SECRET_KEY en las variables de entorno de Render.")
app.config["SESSION_COOKIE_HTTPONLY"] = True
app.config["SESSION_COOKIE_SAMESITE"] = "Lax"
app.config["SESSION_COOKIE_SECURE"] = os.environ.get("INVENTEC_HTTPS", "0") == "1"
app.config["PERMANENT_SESSION_LIFETIME"] = timedelta(hours=8)

class DatabaseConnection:
    """Small compatibility layer so the same app runs on SQLite and PostgreSQL."""
    def __init__(self):
        if USE_POSTGRES:
            if _PG_CFG is None:
                raise DatabaseUnavailable(_PG_CFG_ERROR or "Configuración de base de datos inválida.")
            try:
                self.conn = psycopg2.connect(cursor_factory=DictCursor, connect_timeout=15, **_PG_CFG)
            except psycopg2.OperationalError as exc:
                hint = explain_pg_error(exc, _PG_CFG)
                log.error("Fallo al conectar con PostgreSQL: %s | DIAGNÓSTICO: %s", str(exc).strip(), hint)
                raise DatabaseUnavailable(hint) from exc
        else:
            self.conn = sqlite3.connect(DB_PATH)
            self.conn.row_factory = sqlite3.Row
            self.conn.execute("PRAGMA foreign_keys = ON")

    def execute(self, sql, params=()):
        if USE_POSTGRES:
            sql = sql.replace("?", "%s")
            cur = self.conn.cursor()
            cur.execute(sql, params)
            return cur
        return self.conn.execute(sql, params)

    def executescript(self, script):
        if USE_POSTGRES:
            cur = self.conn.cursor()
            cur.execute(script)
            cur.close()
        else:
            self.conn.executescript(script)

    def columns(self, table):
        if USE_POSTGRES:
            rows = self.execute("SELECT column_name AS name FROM information_schema.columns WHERE table_schema='public' AND table_name=%s", (table,)).fetchall()
            return {r["name"] for r in rows}
        return {r["name"] for r in self.execute(f"PRAGMA table_info({table})").fetchall()}

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        try:
            if exc_type is None:
                self.conn.commit()
            else:
                self.conn.rollback()
        finally:
            self.conn.close()
        return False

def connect_db():
    return DatabaseConnection()

def columns(conn, table):
    return conn.columns(table)

def init_db():
    if USE_POSTGRES:
        schema = """
        CREATE TABLE IF NOT EXISTS users (id BIGSERIAL PRIMARY KEY, username TEXT NOT NULL UNIQUE, full_name TEXT NOT NULL, password_hash TEXT NOT NULL, role TEXT NOT NULL DEFAULT 'tecnico', active INTEGER NOT NULL DEFAULT 1, created_at TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS categories (id BIGSERIAL PRIMARY KEY, name TEXT NOT NULL UNIQUE, description TEXT DEFAULT '');
        CREATE TABLE IF NOT EXISTS equipment (id BIGSERIAL PRIMARY KEY, code TEXT NOT NULL UNIQUE, name TEXT NOT NULL, category_id BIGINT REFERENCES categories(id) ON DELETE SET NULL, brand TEXT DEFAULT '', model TEXT DEFAULT '', serial_number TEXT DEFAULT '', status TEXT NOT NULL DEFAULT 'Disponible', location TEXT DEFAULT '', assigned_to TEXT DEFAULT '', assigned_user_id BIGINT REFERENCES users(id) ON DELETE SET NULL, purchase_date TEXT DEFAULT '', warranty_until TEXT DEFAULT '', value DOUBLE PRECISION DEFAULT 0, notes TEXT DEFAULT '', created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS assignments (id BIGSERIAL PRIMARY KEY, equipment_id BIGINT NOT NULL REFERENCES equipment(id) ON DELETE CASCADE, person_name TEXT NOT NULL, department TEXT DEFAULT '', assigned_by BIGINT REFERENCES users(id) ON DELETE SET NULL, assigned_at TEXT NOT NULL, returned_at TEXT DEFAULT '', notes TEXT DEFAULT '');
        CREATE TABLE IF NOT EXISTS audit_logs (id BIGSERIAL PRIMARY KEY, user_id BIGINT REFERENCES users(id) ON DELETE SET NULL, action TEXT NOT NULL, details TEXT NOT NULL, created_at TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS login_attempts (id BIGSERIAL PRIMARY KEY, username TEXT NOT NULL, attempted_at TEXT NOT NULL, success INTEGER NOT NULL DEFAULT 0);
        CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
        """
        with connect_db() as db:
            # Evita choques si dos workers de Gunicorn arrancan a la vez.
            db.execute("SELECT pg_advisory_xact_lock(727274)")
            db.executescript(schema)
            # Supabase expone por su API pública las tablas del esquema 'public'. Activar RLS sin políticas
            # bloquea ese acceso (anon/authenticated); el usuario 'postgres' del servidor sigue funcionando.
            for table in ("users", "categories", "equipment", "assignments", "audit_logs", "login_attempts", "settings"):
                db.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
            equip_cols = columns(db, "equipment")
            for name, declaration in {"assigned_user_id":"BIGINT", "warranty_until":"TEXT DEFAULT ''", "purchase_date":"TEXT DEFAULT ''", "value":"DOUBLE PRECISION DEFAULT 0", "notes":"TEXT DEFAULT ''", "updated_at":"TEXT DEFAULT ''", "assigned_to":"TEXT DEFAULT ''", "serial_number":"TEXT DEFAULT ''", "brand":"TEXT DEFAULT ''", "model":"TEXT DEFAULT ''", "location":"TEXT DEFAULT ''", "category_id":"BIGINT", "version":"INTEGER NOT NULL DEFAULT 1"}.items():
                if name not in equip_cols:
                    db.execute(f"ALTER TABLE equipment ADD COLUMN {name} {declaration}")
            for name, declaration in {"full_name":"TEXT NOT NULL DEFAULT ''", "role":"TEXT NOT NULL DEFAULT 'tecnico'", "active":"INTEGER NOT NULL DEFAULT 1", "created_at":"TEXT NOT NULL DEFAULT ''"}.items():
                if name not in columns(db, "users"):
                    db.execute(f"ALTER TABLE users ADD COLUMN {name} {declaration}")
            db.execute("INSERT INTO settings(key,value) VALUES('company_name','Inventec') ON CONFLICT(key) DO NOTHING")
            for cat in ["Computadoras", "Monitores", "Impresoras", "Redes", "Accesorios", "Otros"]:
                db.execute("INSERT INTO categories(name,description) VALUES(?, '') ON CONFLICT(name) DO NOTHING", (cat,))
            now = datetime.now().isoformat(timespec="seconds")
            db.execute("UPDATE equipment SET created_at=? WHERE created_at IS NULL OR created_at=''", (now,))
            db.execute("UPDATE equipment SET updated_at=? WHERE updated_at IS NULL OR updated_at=''", (now,))
        return

    with connect_db() as db:
        db.executescript("""
        CREATE TABLE IF NOT EXISTS users (id INTEGER PRIMARY KEY AUTOINCREMENT, username TEXT NOT NULL UNIQUE, full_name TEXT NOT NULL, password_hash TEXT NOT NULL, role TEXT NOT NULL DEFAULT 'tecnico', active INTEGER NOT NULL DEFAULT 1, created_at TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS categories (id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL UNIQUE, description TEXT DEFAULT '');
        CREATE TABLE IF NOT EXISTS equipment (id INTEGER PRIMARY KEY AUTOINCREMENT, code TEXT NOT NULL UNIQUE, name TEXT NOT NULL, category_id INTEGER, brand TEXT DEFAULT '', model TEXT DEFAULT '', serial_number TEXT DEFAULT '', status TEXT NOT NULL DEFAULT 'Disponible', location TEXT DEFAULT '', assigned_to TEXT DEFAULT '', assigned_user_id INTEGER, purchase_date TEXT DEFAULT '', warranty_until TEXT DEFAULT '', value REAL DEFAULT 0, notes TEXT DEFAULT '', created_at TEXT NOT NULL, updated_at TEXT NOT NULL, FOREIGN KEY(category_id) REFERENCES categories(id) ON DELETE SET NULL, FOREIGN KEY(assigned_user_id) REFERENCES users(id) ON DELETE SET NULL);
        CREATE TABLE IF NOT EXISTS assignments (id INTEGER PRIMARY KEY AUTOINCREMENT, equipment_id INTEGER NOT NULL, person_name TEXT NOT NULL, department TEXT DEFAULT '', assigned_by INTEGER, assigned_at TEXT NOT NULL, returned_at TEXT DEFAULT '', notes TEXT DEFAULT '', FOREIGN KEY(equipment_id) REFERENCES equipment(id) ON DELETE CASCADE, FOREIGN KEY(assigned_by) REFERENCES users(id) ON DELETE SET NULL);
        CREATE TABLE IF NOT EXISTS audit_logs (id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER, action TEXT NOT NULL, details TEXT NOT NULL, created_at TEXT NOT NULL, FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE SET NULL);
        CREATE TABLE IF NOT EXISTS login_attempts (id INTEGER PRIMARY KEY AUTOINCREMENT, username TEXT NOT NULL, attempted_at TEXT NOT NULL, success INTEGER NOT NULL DEFAULT 0);
        CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
        """)
        equip_cols = columns(db, "equipment")
        for name, declaration in {"assigned_user_id":"INTEGER", "warranty_until":"TEXT DEFAULT ''", "purchase_date":"TEXT DEFAULT ''", "value":"REAL DEFAULT 0", "notes":"TEXT DEFAULT ''", "updated_at":"TEXT DEFAULT ''", "assigned_to":"TEXT DEFAULT ''", "serial_number":"TEXT DEFAULT ''", "brand":"TEXT DEFAULT ''", "model":"TEXT DEFAULT ''", "location":"TEXT DEFAULT ''", "category_id":"INTEGER", "version":"INTEGER NOT NULL DEFAULT 1"}.items():
            if name not in equip_cols: db.execute(f"ALTER TABLE equipment ADD COLUMN {name} {declaration}")
        for name, declaration in {"full_name":"TEXT NOT NULL DEFAULT ''", "role":"TEXT NOT NULL DEFAULT 'tecnico'", "active":"INTEGER NOT NULL DEFAULT 1", "created_at":"TEXT NOT NULL DEFAULT ''"}.items():
            if name not in columns(db, "users"): db.execute(f"ALTER TABLE users ADD COLUMN {name} {declaration}")
        db.execute("INSERT OR IGNORE INTO settings(key,value) VALUES('company_name','Inventec')")
        for cat in ["Computadoras", "Monitores", "Impresoras", "Redes", "Accesorios", "Otros"]:
            db.execute("INSERT OR IGNORE INTO categories(name,description) VALUES(?, '')", (cat,))
        now = datetime.now().isoformat(timespec="seconds")
        db.execute("UPDATE equipment SET created_at=? WHERE created_at IS NULL OR created_at=''", (now,))
        db.execute("UPDATE equipment SET updated_at=? WHERE updated_at IS NULL OR updated_at=''", (now,))

def now_text():
    return datetime.now().isoformat(timespec="seconds")

def audit(action, details):
    with connect_db() as db:
        db.execute("INSERT INTO audit_logs(user_id,action,details,created_at) VALUES(?,?,?,?)",
                   (session.get("user_id"), action, details[:500], now_text()))

_db_ready = False
_db_error = None
_db_last_attempt = 0.0
_db_lock = threading.Lock()
DB_RETRY_SECONDS = 5

def ensure_db():
    """Inicializa la base de datos la primera vez que se necesita (no al importar el módulo).

    Así la aplicación arranca aunque Supabase esté caído o mal configurado, muestra un aviso claro
    y se recupera sola en cuanto la conexión vuelve."""
    global _db_ready, _db_error, _db_last_attempt
    if _db_ready:
        return
    with _db_lock:
        if _db_ready:
            return
        if _db_error and time.monotonic() - _db_last_attempt < DB_RETRY_SECONDS:
            raise DatabaseUnavailable(_db_error)
        _db_last_attempt = time.monotonic()
        try:
            init_db()
        except DatabaseUnavailable as exc:
            _db_error = str(exc)
            raise
        except Exception as exc:
            log.exception("Error inicializando la base de datos")
            _db_error = "No se pudo preparar la base de datos. Revisa los registros del servidor."
            raise DatabaseUnavailable(_db_error) from exc
        _db_ready, _db_error = True, None
        log.info("Base de datos lista (%s)", "PostgreSQL" if USE_POSTGRES else "SQLite")

_settings_cache = {}
SETTINGS_TTL = 30  # segundos; evita una consulta a Supabase en cada página

def setting(key, default=""):
    if not _db_ready:  # base aún no disponible: no intentes conectar solo para leer un ajuste
        return default
    hit = _settings_cache.get(key)
    if hit and time.monotonic() - hit[1] < SETTINGS_TTL:
        return hit[0]
    try:
        with connect_db() as db:
            row = db.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
    except (DatabaseUnavailable, *DB_OPERATIONAL_ERRORS):
        return default
    value = row["value"] if row else default
    _settings_cache[key] = (value, time.monotonic())
    return value

@app.context_processor
def common_context():
    return {"company_name": setting("company_name", "Inventec"), "current_user": session.get("full_name"),
            "current_role": session.get("role"), "csrf_token": csrf_token}

def csrf_token():
    if "_csrf" not in session:
        session["_csrf"] = secrets.token_urlsafe(32)
    return session["_csrf"]

@app.before_request
def prepare_database():
    if request.endpoint in ("healthz", "static"):
        return
    ensure_db()

@app.before_request
def security_checks():
    if request.method == "POST":
        expected = session.get("_csrf")
        supplied = request.form.get("_csrf", "")
        if not expected or not secrets.compare_digest(expected, supplied):
            abort(400, description="La sesión de seguridad expiró. Recarga la página e inténtalo de nuevo.")

@app.route("/healthz")
def healthz():
    """Comprobación para Render: solo indica que el servidor web responde (no toca la base de datos)."""
    return jsonify(status="ok")

@app.route("/healthz/db")
def healthz_db():
    """Diagnóstico público sin datos sensibles: ¿hay conexión con la base de datos?"""
    try:
        ensure_db()
        with connect_db() as db:
            db.execute("SELECT 1")
        return jsonify(database="ok", engine="postgresql" if USE_POSTGRES else "sqlite")
    except (DatabaseUnavailable, *DB_OPERATIONAL_ERRORS):
        return jsonify(database="unavailable"), 503

def safe_next(target):
    """Solo permite redirigir a rutas internas (evita 'open redirect' en el login)."""
    if target and target.startswith("/") and not target.startswith("//") and "\\" not in target:
        return target
    return url_for("dashboard")

def login_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if not session.get("user_id"):
            return redirect(url_for("login", next=request.path))
        with connect_db() as db:
            row = db.execute("SELECT full_name,role,active FROM users WHERE id=?", (session["user_id"],)).fetchone()
        if not row or not row["active"]:
            session.clear()
            flash("Tu cuenta ya no está activa. Inicia sesión de nuevo o contacta al administrador.", "error")
            return redirect(url_for("login"))
        session["role"], session["full_name"] = row["role"], row["full_name"]  # refleja cambios de rol/nombre al instante
        return view(*args, **kwargs)
    return wrapped

def roles_required(*roles):
    def decorator(view):
        @wraps(view)
        @login_required
        def wrapped(*args, **kwargs):
            if session.get("role") not in roles:
                abort(403)
            return view(*args, **kwargs)
        return wrapped
    return decorator

def get_equipment(equipment_id):
    with connect_db() as db:
        item = db.execute("""
            SELECT e.*, c.name AS category_name, u.full_name AS assigned_user_name
            FROM equipment e LEFT JOIN categories c ON c.id=e.category_id
            LEFT JOIN users u ON u.id=e.assigned_user_id WHERE e.id=?
        """, (equipment_id,)).fetchone()
    if not item:
        abort(404)
    return item

@app.route("/setup", methods=["GET", "POST"])
def setup():
    with connect_db() as db:
        count = db.execute("SELECT COUNT(*) AS n FROM users").fetchone()["n"]
    if count:
        return redirect(url_for("login"))
    if request.method == "POST":
        if SETUP_TOKEN and not secrets.compare_digest(request.form.get("setup_token", ""), SETUP_TOKEN):
            flash("El código de instalación no es correcto.", "error")
            return render_template("setup.html", token_required=True)
        username = request.form.get("username", "").strip()
        full_name = request.form.get("full_name", "").strip()
        password = request.form.get("password", "")
        confirm = request.form.get("confirm", "")
        if len(username) < 3 or not full_name:
            flash("Escribe un usuario de al menos 3 caracteres y tu nombre.", "error")
        elif len(password) < 10:
            flash("La contraseña debe tener al menos 10 caracteres.", "error")
        elif password != confirm:
            flash("Las contraseñas no coinciden.", "error")
        else:
            with connect_db() as db:
                db.execute("INSERT INTO users(username,full_name,password_hash,role,active,created_at) VALUES(?,?,?,?,1,?)",
                           (username, full_name, generate_password_hash(password), "admin", now_text()))
            flash("Cuenta administradora creada. Ya puedes iniciar sesión.", "success")
            return redirect(url_for("login"))
    return render_template("setup.html", token_required=bool(SETUP_TOKEN))

@app.route("/login", methods=["GET", "POST"])
def login():
    if session.get("user_id"):
        return redirect(url_for("dashboard"))
    if request.method == "POST":
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "")
        with connect_db() as db:
            recent = db.execute("SELECT COUNT(*) AS n FROM login_attempts WHERE username=? AND attempted_at > ? AND success=0",
                                (username, (datetime.now()-timedelta(minutes=15)).isoformat(timespec="seconds"))).fetchone()["n"]
            user = db.execute("SELECT * FROM users WHERE username=? AND active=1", (username,)).fetchone()
            ok = bool(user and recent < 8 and check_password_hash(user["password_hash"], password))
            db.execute("INSERT INTO login_attempts(username,attempted_at,success) VALUES(?,?,?)",
                       (username, now_text(), 1 if ok else 0))
        if ok:
            session.clear()
            session["user_id"] = user["id"]
            session["username"] = user["username"]
            session["full_name"] = user["full_name"]
            session["role"] = user["role"]
            session.permanent = True
            audit("Inicio de sesión", f"Usuario {username}")
            return redirect(safe_next(request.args.get("next")))
        flash("Credenciales incorrectas o demasiados intentos. Espera unos minutos e inténtalo de nuevo.", "error")
    with connect_db() as db:
        users_exist = db.execute("SELECT COUNT(*) AS n FROM users").fetchone()["n"] > 0
    return render_template("login.html", users_exist=users_exist)

@app.route("/logout", methods=["POST"])
@login_required
def logout():
    audit("Cierre de sesión", f"Usuario {session.get('username','')}")
    session.clear()
    flash("Has cerrado sesión.", "success")
    return redirect(url_for("login"))

@app.route("/")
@login_required
def dashboard():
    q = request.args.get("q", "").strip()
    status = request.args.get("status", "").strip()
    category_id = request.args.get("category", "").strip()
    where = ""
    args = []
    if q:
        like = f"%{q}%"
        where += (" AND (e.code {0} ? OR e.name {0} ? OR e.brand {0} ? OR e.model {0} ? OR e.serial_number {0} ? "
                  "OR e.assigned_to {0} ? OR e.location {0} ?)").format(LIKE_OP)
        args.extend([like]*7)
    if status in ["Disponible", "Asignado", "En reparación", "Dado de baja"]:
        where += " AND e.status=?"
        args.append(status)
    if category_id.isdigit():
        where += " AND e.category_id=?"
        args.append(int(category_id))
    page_arg = request.args.get("page", "1")
    page = int(page_arg) if page_arg.isdigit() and int(page_arg) > 0 else 1
    with connect_db() as db:
        matches = db.execute("SELECT COUNT(*) FROM equipment e WHERE 1=1" + where, args).fetchone()[0]
        pages = max(1, -(-matches // PAGE_SIZE))
        page = min(page, pages)
        items = db.execute("""SELECT e.*, c.name AS category_name FROM equipment e
                              LEFT JOIN categories c ON c.id=e.category_id WHERE 1=1""" + where +
                           " ORDER BY e.updated_at DESC, e.id DESC LIMIT ? OFFSET ?",
                           args + [PAGE_SIZE, (page - 1) * PAGE_SIZE]).fetchall()
        stats = {
            "total": db.execute("SELECT COUNT(*) FROM equipment").fetchone()[0],
            "available": db.execute("SELECT COUNT(*) FROM equipment WHERE status='Disponible'").fetchone()[0],
            "assigned": db.execute("SELECT COUNT(*) FROM equipment WHERE status='Asignado'").fetchone()[0],
            "repair": db.execute("SELECT COUNT(*) FROM equipment WHERE status='En reparación'").fetchone()[0],
            "retired": db.execute("SELECT COUNT(*) FROM equipment WHERE status='Dado de baja'").fetchone()[0],
            "users": db.execute("SELECT COUNT(*) FROM users WHERE active=1").fetchone()[0],
        }
        categories = db.execute("SELECT * FROM categories ORDER BY name").fetchall()
        top_categories = db.execute("""SELECT COALESCE(c.name,'Sin categoría') AS name,COUNT(e.id) AS total
                                       FROM equipment e LEFT JOIN categories c ON c.id=e.category_id
                                       GROUP BY c.name ORDER BY total DESC LIMIT 5""").fetchall()
        recent_logs = db.execute("""SELECT a.*,u.full_name FROM audit_logs a LEFT JOIN users u ON u.id=a.user_id
                                    ORDER BY a.id DESC LIMIT 6""").fetchall()
        equipment = db.execute("SELECT id,name,code,warranty_until FROM equipment WHERE warranty_until<>'' AND warranty_until IS NOT NULL").fetchall()
    # Gráfico de dona (CSS puro, sin librerías externas)
    segments, start = [], 0.0
    for key, color in (("available", "#26b889"), ("assigned", "#5277f1"), ("repair", "#e4a23b"), ("retired", "#e56a7c")):
        if stats["total"] and stats[key]:
            end = start + stats[key] * 100.0 / stats["total"]
            segments.append(f"{color} {start:.2f}% {end:.2f}%")
            start = end
    donut = "conic-gradient(" + ", ".join(segments) + ")" if segments else "#eef1f8"
    today = date.today()
    alerts = []
    for e in equipment:
        try:
            warranty = date.fromisoformat(e["warranty_until"])
            days = (warranty - today).days
            if -1 <= days <= 30:
                alerts.append({"name": e["name"], "code": e["code"], "days": days})
        except (ValueError, TypeError):
            pass
    alerts.sort(key=lambda x: x["days"])
    return render_template("dashboard.html", items=items, stats=stats, categories=categories,
                           q=q, selected_status=status, selected_category=category_id,
                           recent_logs=recent_logs, alerts=alerts, page=page, pages=pages, matches=matches,
                           page_size=PAGE_SIZE, donut=donut, top_categories=top_categories)

@app.route("/equipment/new", methods=["GET", "POST"])
@roles_required("admin", "tecnico")
def equipment_new():
    if request.method == "POST":
        data = equipment_form_data()
        error = validate_equipment(data)
        if not error and data["status"] == "Asignado":
            error = "Registra el equipo como «Disponible» y luego asígnalo desde su ficha con «Registrar asignación»."
        if error:
            flash(error, "error")
            return render_template("equipment_form.html", item=data, categories=all_categories(), page_title="Registrar equipo")
        try:
            with connect_db() as db:
                db.execute("""INSERT INTO equipment(code,name,category_id,brand,model,serial_number,status,location,
                    assigned_to,purchase_date,warranty_until,value,notes,created_at,updated_at)
                    VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (data["code"], data["name"], data["category_id"] or None, data["brand"], data["model"],
                     data["serial_number"], data["status"], data["location"], data["assigned_to"],
                     data["purchase_date"], data["warranty_until"], data["value"], data["notes"], now_text(), now_text()))
            audit("Equipo registrado", f'{data["code"]} — {data["name"]}')
            flash("Equipo registrado correctamente.", "success")
            return redirect(url_for("dashboard"))
        except DB_INTEGRITY_ERROR:
            flash("Ese código ya existe. Usa un código único.", "error")
    return render_template("equipment_form.html", item={}, categories=all_categories(), page_title="Registrar equipo")

def equipment_form_data():
    def val(k): return request.form.get(k, "").strip()
    try: value = max(0, float(val("value") or 0))
    except ValueError: value = 0
    cat = val("category_id")
    return {"code": val("code"), "name": val("name"), "category_id": cat if cat.isdigit() else "",
            "brand": val("brand"), "model": val("model"), "serial_number": val("serial_number"),
            "status": val("status") or "Disponible", "location": val("location"), "assigned_to": "",
            "purchase_date": val("purchase_date"), "warranty_until": val("warranty_until"),
            "value": value, "notes": val("notes")}

def validate_equipment(d):
    if not d["code"] or not d["name"]:
        return "El código y el nombre del equipo son obligatorios."
    if d["status"] not in ["Disponible", "Asignado", "En reparación", "Dado de baja"]:
        return "Selecciona un estado válido."
    for field in ("purchase_date", "warranty_until"):
        if d[field]:
            try: date.fromisoformat(d[field])
            except ValueError: return "Las fechas deben tener un formato válido."
    return None

def all_categories():
    with connect_db() as db:
        return db.execute("SELECT * FROM categories ORDER BY name").fetchall()

@app.route("/equipment/<int:equipment_id>")
@login_required
def equipment_detail(equipment_id):
    item = get_equipment(equipment_id)
    with connect_db() as db:
        history = db.execute("""SELECT a.*,u.full_name AS assigned_by_name FROM assignments a
                              LEFT JOIN users u ON u.id=a.assigned_by WHERE equipment_id=? ORDER BY id DESC""",
                             (equipment_id,)).fetchall()
    return render_template("equipment_detail.html", item=item, history=history)

@app.route("/equipment/<int:equipment_id>/edit", methods=["GET", "POST"])
@roles_required("admin", "tecnico")
def equipment_edit(equipment_id):
    old = get_equipment(equipment_id)
    if request.method == "POST":
        data = equipment_form_data()
        data["version"] = request.form.get("version", "")
        error = validate_equipment(data)
        if not error and data["status"] == "Asignado" and old["status"] != "Asignado":
            error = "Para marcar un equipo como asignado usa «Registrar asignación» en su ficha."
        if error:
            flash(error, "error")
            data["assigned_to"] = old["assigned_to"]
            return render_template("equipment_form.html", item=data, categories=all_categories(), page_title="Editar equipo")
        version = int(data["version"]) if data["version"].isdigit() else -1
        still_assigned = data["status"] == "Asignado"
        try:
            with connect_db() as db:
                cur = db.execute("""UPDATE equipment SET code=?,name=?,category_id=?,brand=?,model=?,serial_number=?,status=?,
                    location=?,assigned_to=?,purchase_date=?,warranty_until=?,value=?,notes=?,updated_at=?,version=version+1
                    WHERE id=? AND version=?""",
                    (data["code"], data["name"], data["category_id"] or None, data["brand"], data["model"],
                     data["serial_number"], data["status"], data["location"], old["assigned_to"] if still_assigned else "",
                     data["purchase_date"], data["warranty_until"], data["value"], data["notes"], now_text(),
                     equipment_id, version))
                conflict = cur.rowcount == 0
                if not conflict and old["status"] == "Asignado" and not still_assigned:
                    db.execute("UPDATE assignments SET returned_at=? WHERE equipment_id=? AND returned_at=''",
                               (now_text(), equipment_id))
            if conflict:
                flash("Otra persona modificó este equipo mientras lo editabas. Cargamos los datos más recientes: "
                      "revisa y vuelve a aplicar tus cambios.", "error")
                return render_template("equipment_form.html", item=dict(get_equipment(equipment_id)),
                                       categories=all_categories(), page_title="Editar equipo")
            audit("Equipo actualizado", f'{data["code"]} — {data["name"]}')
            flash("Cambios guardados.", "success")
            return redirect(url_for("equipment_detail", equipment_id=equipment_id))
        except DB_INTEGRITY_ERROR:
            flash("Ese código ya pertenece a otro equipo.", "error")
            data["assigned_to"] = old["assigned_to"]
            return render_template("equipment_form.html", item=data, categories=all_categories(), page_title="Editar equipo")
    return render_template("equipment_form.html", item=dict(old), categories=all_categories(), page_title="Editar equipo")

@app.route("/equipment/<int:equipment_id>/delete", methods=["POST"])
@roles_required("admin")
def equipment_delete(equipment_id):
    item = get_equipment(equipment_id)
    with connect_db() as db:
        db.execute("DELETE FROM equipment WHERE id=?", (equipment_id,))
    audit("Equipo eliminado", f'{item["code"]} — {item["name"]}')
    flash("Equipo eliminado.", "success")
    return redirect(url_for("dashboard"))

@app.route("/equipment/<int:equipment_id>/assign", methods=["POST"])
@roles_required("admin", "tecnico")
def equipment_assign(equipment_id):
    item = get_equipment(equipment_id)
    person = request.form.get("person_name", "").strip()
    department = request.form.get("department", "").strip()
    notes = request.form.get("notes", "").strip()
    if not person:
        flash("Escribe el nombre de la persona que recibirá el equipo.", "error")
    elif item["status"] == "Dado de baja":
        flash("No puedes asignar un equipo dado de baja.", "error")
    elif item["status"] == "Asignado":
        flash("Este equipo ya está asignado. Registra primero su devolución.", "error")
    else:
        with connect_db() as db:
            # Primero se reclama el equipo (solo si no está ya asignado ni dado de baja): atómico ante dos clics a la vez.
            claimed = db.execute("UPDATE equipment SET status='Asignado',assigned_to=?,updated_at=?,version=version+1 "
                                 "WHERE id=? AND status NOT IN ('Asignado','Dado de baja')",
                                 (person, now_text(), equipment_id)).rowcount
            if claimed:
                db.execute("INSERT INTO assignments(equipment_id,person_name,department,assigned_by,assigned_at,notes) VALUES(?,?,?,?,?,?)",
                           (equipment_id, person, department, session["user_id"], now_text(), notes))
        if claimed:
            audit("Equipo asignado", f'{item["code"]} asignado a {person}')
            flash("Asignación registrada.", "success")
        else:
            flash("El equipo cambió de estado y ya no puede asignarse. Revisa su ficha.", "error")
    return redirect(url_for("equipment_detail", equipment_id=equipment_id))

@app.route("/equipment/<int:equipment_id>/return", methods=["POST"])
@roles_required("admin", "tecnico")
def equipment_return(equipment_id):
    item = get_equipment(equipment_id)
    with connect_db() as db:
        active = db.execute("SELECT * FROM assignments WHERE equipment_id=? AND returned_at='' ORDER BY id DESC LIMIT 1", (equipment_id,)).fetchone()
        if active:
            db.execute("UPDATE assignments SET returned_at=? WHERE id=?", (now_text(), active["id"]))
        db.execute("UPDATE equipment SET status='Disponible',assigned_to='',updated_at=?,version=version+1 WHERE id=? AND status='Asignado'",
                   (now_text(), equipment_id))
    audit("Equipo devuelto", item["code"])
    flash("Se ha registrado la devolución si el equipo estaba asignado.", "success")
    return redirect(url_for("equipment_detail", equipment_id=equipment_id))

@app.route("/categories", methods=["GET", "POST"])
@roles_required("admin", "tecnico")
def categories():
    if request.method == "POST":
        name = request.form.get("name", "").strip()
        desc = request.form.get("description", "").strip()
        if not name:
            flash("El nombre de la categoría es obligatorio.", "error")
        else:
            try:
                with connect_db() as db:
                    db.execute("INSERT INTO categories(name,description) VALUES(?,?)", (name, desc))
                audit("Categoría creada", name)
                flash("Categoría creada.", "success")
            except DB_INTEGRITY_ERROR:
                flash("Esa categoría ya existe.", "error")
        return redirect(url_for("categories"))
    with connect_db() as db:
        rows = db.execute("""SELECT c.*,COUNT(e.id) AS equipment_count FROM categories c
                             LEFT JOIN equipment e ON e.category_id=c.id GROUP BY c.id ORDER BY c.name""").fetchall()
    return render_template("categories.html", categories=rows)

@app.route("/categories/<int:category_id>/edit", methods=["GET", "POST"])
@roles_required("admin", "tecnico")
def category_edit(category_id):
    with connect_db() as db:
        row = db.execute("SELECT * FROM categories WHERE id=?", (category_id,)).fetchone()
    if not row:
        abort(404)
    if request.method == "POST":
        name = request.form.get("name", "").strip()
        desc = request.form.get("description", "").strip()
        if not name:
            flash("El nombre de la categoría es obligatorio.", "error")
            return render_template("category_edit.html", c={"id": category_id, "name": name, "description": desc})
        try:
            with connect_db() as db:
                db.execute("UPDATE categories SET name=?,description=? WHERE id=?", (name, desc, category_id))
        except DB_INTEGRITY_ERROR:
            flash("Ya existe otra categoría con ese nombre.", "error")
            return render_template("category_edit.html", c={"id": category_id, "name": name, "description": desc})
        audit("Categoría actualizada", f'{row["name"]} → {name}')
        flash("Categoría actualizada.", "success")
        return redirect(url_for("categories"))
    return render_template("category_edit.html", c=dict(row))

@app.route("/categories/<int:category_id>/delete", methods=["POST"])
@roles_required("admin")
def category_delete(category_id):
    with connect_db() as db:
        row = db.execute("SELECT * FROM categories WHERE id=?", (category_id,)).fetchone()
        if not row:
            abort(404)
        db.execute("DELETE FROM categories WHERE id=?", (category_id,))
    audit("Categoría eliminada", row["name"])
    flash("Categoría eliminada. Los equipos quedan sin categoría.", "success")
    return redirect(url_for("categories"))

@app.route("/reports")
@roles_required("admin", "tecnico", "consulta")
def reports():
    with connect_db() as db:
        status_rows = db.execute("SELECT status,COUNT(*) AS total FROM equipment GROUP BY status ORDER BY total DESC").fetchall()
        category_rows = db.execute("""SELECT COALESCE(c.name,'Sin categoría') AS name,COUNT(e.id) AS total
                                      FROM equipment e LEFT JOIN categories c ON c.id=e.category_id
                                      GROUP BY c.name ORDER BY total DESC""").fetchall()
        recent = db.execute("""SELECT e.*,c.name AS category_name FROM equipment e LEFT JOIN categories c
                               ON c.id=e.category_id ORDER BY e.updated_at DESC LIMIT 20""").fetchall()
        logs = db.execute("""SELECT a.*,u.full_name FROM audit_logs a LEFT JOIN users u ON u.id=a.user_id
                             ORDER BY a.id DESC LIMIT 50""").fetchall()
    return render_template("reports.html", status_rows=status_rows, category_rows=category_rows, recent=recent, logs=logs)

def csv_safe(value):
    """Evita inyección de fórmulas: Excel ejecuta celdas de texto que empiezan por = + - @."""
    if value is None:
        return ""
    if isinstance(value, str) and value[:1] in ("=", "+", "-", "@", "\t", "\r"):
        return "'" + value
    return value

@app.route("/reports/export.csv")
@roles_required("admin", "tecnico", "consulta")
def export_csv():
    with connect_db() as db:
        rows = db.execute("""SELECT e.code,e.name,c.name AS category,e.brand,e.model,e.serial_number,e.status,
                             e.location,e.assigned_to,e.purchase_date,e.warranty_until,e.value,e.notes
                             FROM equipment e LEFT JOIN categories c ON c.id=e.category_id ORDER BY e.name""").fetchall()
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(["Código","Equipo","Categoría","Marca","Modelo","Número de serie","Estado","Ubicación",
                     "Asignado a","Fecha de compra","Garantía hasta","Valor","Notas"])
    for row in rows:
        writer.writerow([csv_safe(row[k]) for k in row.keys()])
    response = make_response("\ufeff" + output.getvalue())
    response.headers["Content-Type"] = "text/csv; charset=utf-8"
    response.headers["Content-Disposition"] = "attachment; filename=inventec_inventario.csv"
    return response

@app.route("/users", methods=["GET", "POST"])
@roles_required("admin")
def users():
    if request.method == "POST":
        username = request.form.get("username", "").strip()
        full_name = request.form.get("full_name", "").strip()
        password = request.form.get("password", "")
        role = request.form.get("role", "tecnico")
        if len(username) < 3 or not full_name:
            flash("Completa el usuario (mínimo 3 caracteres) y el nombre.", "error")
        elif len(password) < 10:
            flash("La contraseña debe tener al menos 10 caracteres.", "error")
        elif role not in ("admin", "tecnico", "consulta"):
            flash("Rol inválido.", "error")
        else:
            try:
                with connect_db() as db:
                    db.execute("INSERT INTO users(username,full_name,password_hash,role,active,created_at) VALUES(?,?,?,?,1,?)",
                               (username, full_name, generate_password_hash(password), role, now_text()))
                audit("Usuario creado", f"{username} ({role})")
                flash("Usuario creado correctamente.", "success")
            except DB_INTEGRITY_ERROR:
                flash("Ese nombre de usuario ya existe.", "error")
        return redirect(url_for("users"))
    with connect_db() as db:
        rows = db.execute("SELECT id,username,full_name,role,active,created_at FROM users ORDER BY id").fetchall()
    return render_template("users.html", users=rows)

@app.route("/users/<int:user_id>/toggle", methods=["POST"])
@roles_required("admin")
def user_toggle(user_id):
    if user_id == session.get("user_id"):
        flash("No puedes desactivar tu propia cuenta.", "error")
        return redirect(url_for("users"))
    with connect_db() as db:
        user = db.execute("SELECT * FROM users WHERE id=?", (user_id,)).fetchone()
        if not user:
            abort(404)
        if user["role"] == "admin" and user["active"]:
            admins = db.execute("SELECT COUNT(*) FROM users WHERE role='admin' AND active=1").fetchone()[0]
            if admins <= 1:
                flash("Debe quedar al menos un administrador activo.", "error")
                return redirect(url_for("users"))
        new_status = 0 if user["active"] else 1
        db.execute("UPDATE users SET active=? WHERE id=?", (new_status, user_id))
    audit("Estado de usuario cambiado", f'{user["username"]}: {"activo" if new_status else "inactivo"}')
    flash("Estado del usuario actualizado.", "success")
    return redirect(url_for("users"))

def active_admin_count(db):
    return db.execute("SELECT COUNT(*) AS n FROM users WHERE role='admin' AND active=1").fetchone()["n"]

@app.route("/users/<int:user_id>/edit", methods=["GET", "POST"])
@roles_required("admin")
def user_edit(user_id):
    with connect_db() as db:
        user = db.execute("SELECT id,username,full_name,role,active FROM users WHERE id=?", (user_id,)).fetchone()
    if not user:
        abort(404)
    if request.method == "POST":
        full_name = request.form.get("full_name", "").strip()
        role = request.form.get("role", user["role"])
        password = request.form.get("password", "")
        error = None
        if not full_name:
            error = "El nombre completo es obligatorio."
        elif role not in ("admin", "tecnico", "consulta"):
            error = "Rol inválido."
        elif password and len(password) < 10:
            error = "La nueva contraseña debe tener al menos 10 caracteres."
        elif user["role"] == "admin" and role != "admin":
            if user_id == session.get("user_id"):
                error = "No puedes quitarte a ti mismo el rol de administrador."
            else:
                with connect_db() as db:
                    if user["active"] and active_admin_count(db) <= 1:
                        error = "Debe quedar al menos un administrador activo."
        if error:
            flash(error, "error")
            return render_template("user_edit.html", u={**dict(user), "full_name": full_name, "role": role})
        with connect_db() as db:
            if password:
                db.execute("UPDATE users SET full_name=?,role=?,password_hash=? WHERE id=?",
                           (full_name, role, generate_password_hash(password), user_id))
            else:
                db.execute("UPDATE users SET full_name=?,role=? WHERE id=?", (full_name, role, user_id))
        audit("Usuario actualizado", f'{user["username"]} ({role})' + (" · contraseña restablecida" if password else ""))
        flash("Usuario actualizado.", "success")
        return redirect(url_for("users"))
    return render_template("user_edit.html", u=dict(user))

@app.route("/account", methods=["GET", "POST"])
@login_required
def account():
    if request.method == "POST":
        current = request.form.get("current_password", "")
        new = request.form.get("new_password", "")
        confirm = request.form.get("confirm", "")
        with connect_db() as db:
            row = db.execute("SELECT password_hash FROM users WHERE id=?", (session["user_id"],)).fetchone()
        if not check_password_hash(row["password_hash"], current):
            flash("La contraseña actual no es correcta.", "error")
        elif len(new) < 10:
            flash("La nueva contraseña debe tener al menos 10 caracteres.", "error")
        elif new != confirm:
            flash("Las contraseñas nuevas no coinciden.", "error")
        else:
            with connect_db() as db:
                db.execute("UPDATE users SET password_hash=? WHERE id=?", (generate_password_hash(new), session["user_id"]))
            audit("Contraseña cambiada", f'Usuario {session.get("username","")}')
            flash("Contraseña actualizada.", "success")
        return redirect(url_for("account"))
    return render_template("account.html")

@app.route("/settings", methods=["GET", "POST"])
@roles_required("admin")
def settings():
    if request.method == "POST":
        company = request.form.get("company_name", "").strip()
        if not company:
            flash("El nombre de la empresa no puede quedar vacío.", "error")
        else:
            with connect_db() as db:
                db.execute("INSERT INTO settings(key,value) VALUES('company_name',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (company,))
            _settings_cache.clear()
            audit("Configuración actualizada", f"Nombre de la empresa: {company}")
            flash("Configuración guardada.", "success")
        return redirect(url_for("settings"))
    return render_template("settings.html", company=setting("company_name", "Inventec"))

@app.errorhandler(400)
def bad_request(error):
    return render_template("error.html", code=400, message=getattr(error, "description", "Solicitud no válida.")), 400

@app.errorhandler(403)
def forbidden(error):
    return render_template("error.html", code=403, message="No tienes permiso para acceder a esta sección."), 403

@app.errorhandler(404)
def not_found(error):
    return render_template("error.html", code=404, message="No encontramos esa página o registro."), 404

@app.errorhandler(DatabaseUnavailable)
def db_unavailable(error):
    return render_template("error.html", code=503,
        message="No se puede conectar con la base de datos en este momento. Inténtalo de nuevo en unos segundos. "
                "Si el problema continúa, avisa al administrador."), 503

for _exc in DB_OPERATIONAL_ERRORS:
    @app.errorhandler(_exc)
    def db_operational(error):
        log.error("Error de base de datos durante la petición: %s", error)
        return db_unavailable(error)

@app.errorhandler(Exception)
def unexpected(error):
    if isinstance(error, HTTPException):
        return error
    log.exception("Error inesperado")
    return render_template("error.html", code=500, message="Ocurrió un error inesperado. Ya quedó registrado."), 500

if __name__ == "__main__":
    app.run(debug=False, host="0.0.0.0", port=int(os.environ.get("PORT", "5000")))
