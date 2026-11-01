import os
import io
import csv
import secrets
import sqlite3
import re
from contextlib import contextmanager
from datetime import datetime, date, timedelta
from functools import wraps
from flask import Flask, render_template, request, redirect, url_for, session, flash, abort, make_response
from werkzeug.security import generate_password_hash, check_password_hash

APP_DIR = os.path.abspath(os.path.dirname(__file__))
DB_PATH = os.path.join(APP_DIR, "inventec.db")
DATABASE_URL = os.environ.get("DATABASE_URL", "").strip()
USE_POSTGRES = bool(DATABASE_URL)
if USE_POSTGRES:
    import psycopg2
    from psycopg2.extras import DictCursor
    DB_INTEGRITY_ERROR = psycopg2.IntegrityError
else:
    DB_INTEGRITY_ERROR = sqlite3.IntegrityError
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
            url = DATABASE_URL
            if "sslmode=" not in url:
                url += ("&" if "?" in url else "?") + "sslmode=require"
            self.conn = psycopg2.connect(url, cursor_factory=DictCursor, connect_timeout=15)
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
            db.executescript(schema)
            equip_cols = columns(db, "equipment")
            for name, declaration in {"assigned_user_id":"BIGINT", "warranty_until":"TEXT DEFAULT ''", "purchase_date":"TEXT DEFAULT ''", "value":"DOUBLE PRECISION DEFAULT 0", "notes":"TEXT DEFAULT ''", "updated_at":"TEXT DEFAULT ''", "assigned_to":"TEXT DEFAULT ''", "serial_number":"TEXT DEFAULT ''", "brand":"TEXT DEFAULT ''", "model":"TEXT DEFAULT ''", "location":"TEXT DEFAULT ''", "category_id":"BIGINT"}.items():
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
        for name, declaration in {"assigned_user_id":"INTEGER", "warranty_until":"TEXT DEFAULT ''", "purchase_date":"TEXT DEFAULT ''", "value":"REAL DEFAULT 0", "notes":"TEXT DEFAULT ''", "updated_at":"TEXT DEFAULT ''", "assigned_to":"TEXT DEFAULT ''", "serial_number":"TEXT DEFAULT ''", "brand":"TEXT DEFAULT ''", "model":"TEXT DEFAULT ''", "location":"TEXT DEFAULT ''", "category_id":"INTEGER"}.items():
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

def setting(key, default=""):
    with connect_db() as db:
        row = db.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
    return row["value"] if row else default

@app.context_processor
def common_context():
    return {"company_name": setting("company_name", "Inventec"), "current_user": session.get("full_name"),
            "current_role": session.get("role"), "csrf_token": csrf_token}

def csrf_token():
    if "_csrf" not in session:
        session["_csrf"] = secrets.token_urlsafe(32)
    return session["_csrf"]

@app.before_request
def security_checks():
    if request.method == "POST":
        expected = session.get("_csrf")
        supplied = request.form.get("_csrf", "")
        if not expected or not secrets.compare_digest(expected, supplied):
            abort(400, description="La sesión de seguridad expiró. Recarga la página e inténtalo de nuevo.")

def login_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if not session.get("user_id"):
            return redirect(url_for("login", next=request.path))
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
    return render_template("setup.html")

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
            return redirect(request.args.get("next") or url_for("dashboard"))
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
    sql = """SELECT e.*, c.name AS category_name FROM equipment e
             LEFT JOIN categories c ON c.id=e.category_id WHERE 1=1"""
    args = []
    if q:
        like = f"%{q}%"
        sql += " AND (e.code LIKE ? OR e.name LIKE ? OR e.brand LIKE ? OR e.model LIKE ? OR e.serial_number LIKE ? OR e.assigned_to LIKE ? OR e.location LIKE ?)"
        args.extend([like]*7)
    if status in ["Disponible", "Asignado", "En reparación", "Dado de baja"]:
        sql += " AND e.status=?"
        args.append(status)
    if category_id.isdigit():
        sql += " AND e.category_id=?"
        args.append(int(category_id))
    sql += " ORDER BY e.updated_at DESC, e.id DESC LIMIT 200"
    with connect_db() as db:
        items = db.execute(sql, args).fetchall()
        stats = {
            "total": db.execute("SELECT COUNT(*) FROM equipment").fetchone()[0],
            "available": db.execute("SELECT COUNT(*) FROM equipment WHERE status='Disponible'").fetchone()[0],
            "assigned": db.execute("SELECT COUNT(*) FROM equipment WHERE status='Asignado'").fetchone()[0],
            "repair": db.execute("SELECT COUNT(*) FROM equipment WHERE status='En reparación'").fetchone()[0],
            "users": db.execute("SELECT COUNT(*) FROM users WHERE active=1").fetchone()[0],
        }
        categories = db.execute("SELECT * FROM categories ORDER BY name").fetchall()
        recent_logs = db.execute("""SELECT a.*,u.full_name FROM audit_logs a LEFT JOIN users u ON u.id=a.user_id
                                    ORDER BY a.id DESC LIMIT 6""").fetchall()
        equipment = db.execute("SELECT id,name,code,warranty_until FROM equipment WHERE warranty_until<>'' AND warranty_until IS NOT NULL").fetchall()
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
                           recent_logs=recent_logs, alerts=alerts)

@app.route("/equipment/new", methods=["GET", "POST"])
@roles_required("admin", "tecnico")
def equipment_new():
    if request.method == "POST":
        data = equipment_form_data()
        error = validate_equipment(data)
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
            "status": val("status") or "Disponible", "location": val("location"), "assigned_to": val("assigned_to"),
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
        error = validate_equipment(data)
        if error:
            flash(error, "error")
            return render_template("equipment_form.html", item=data, categories=all_categories(), page_title="Editar equipo")
        try:
            with connect_db() as db:
                db.execute("""UPDATE equipment SET code=?,name=?,category_id=?,brand=?,model=?,serial_number=?,status=?,
                    location=?,assigned_to=?,purchase_date=?,warranty_until=?,value=?,notes=?,updated_at=? WHERE id=?""",
                    (data["code"], data["name"], data["category_id"] or None, data["brand"], data["model"],
                     data["serial_number"], data["status"], data["location"], data["assigned_to"],
                     data["purchase_date"], data["warranty_until"], data["value"], data["notes"], now_text(), equipment_id))
            audit("Equipo actualizado", f'{data["code"]} — {data["name"]}')
            flash("Cambios guardados.", "success")
            return redirect(url_for("equipment_detail", equipment_id=equipment_id))
        except DB_INTEGRITY_ERROR:
            flash("Ese código ya pertenece a otro equipo.", "error")
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
    else:
        with connect_db() as db:
            db.execute("INSERT INTO assignments(equipment_id,person_name,department,assigned_by,assigned_at,notes) VALUES(?,?,?,?,?,?)",
                       (equipment_id, person, department, session["user_id"], now_text(), notes))
            db.execute("UPDATE equipment SET status='Asignado',assigned_to=?,updated_at=? WHERE id=?",
                       (person, now_text(), equipment_id))
        audit("Equipo asignado", f'{item["code"]} asignado a {person}')
        flash("Asignación registrada.", "success")
    return redirect(url_for("equipment_detail", equipment_id=equipment_id))

@app.route("/equipment/<int:equipment_id>/return", methods=["POST"])
@roles_required("admin", "tecnico")
def equipment_return(equipment_id):
    item = get_equipment(equipment_id)
    with connect_db() as db:
        active = db.execute("SELECT * FROM assignments WHERE equipment_id=? AND returned_at='' ORDER BY id DESC LIMIT 1", (equipment_id,)).fetchone()
        if active:
            db.execute("UPDATE assignments SET returned_at=? WHERE id=?", (now_text(), active["id"]))
        db.execute("UPDATE equipment SET status='Disponible',assigned_to='',updated_at=? WHERE id=? AND status='Asignado'",
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
        writer.writerow([row[k] if row[k] is not None else "" for k in row.keys()])
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

init_db()

if __name__ == "__main__":
    app.run(debug=False, host="0.0.0.0", port=int(os.environ.get("PORT", "5000")))
