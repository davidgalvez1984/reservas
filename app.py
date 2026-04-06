"""
App V2 - Reservas Parcelación Caña Brava
----------------------------------------
Novedades:
- Reserva individual o combinada (salón + piscina)
- Admin: aprobar, eliminar, modificar y solicitar ajuste al usuario
- Usuario: puede editar su reserva cuando está pendiente o requiere_ajuste
- Calendario mensual para usuario y admin
  * Usuario: solo ve "Reservado" y horarios
  * Admin: ve además la propiedad
- Nueva base de datos para evitar conflictos con la V1

Cómo ejecutar:
    pip install flask
    python app_reservas_cana_brava_v2.py

Usuarios iniciales:
    admin / admin123
    casa01 / demo123
"""

from __future__ import annotations

import calendar as pycalendar
import sqlite3
from contextlib import closing
from datetime import date, datetime, timedelta
from functools import wraps
from pathlib import Path
from typing import Optional

from flask import (
    Flask,
    abort,
    flash,
    g,
    redirect,
    render_template_string,
    request,
    session,
    url_for,
)

BASE_DIR = Path(__file__).resolve().parent
DB_PATH = BASE_DIR / "reservas_cana_brava_v2.db"

app = Flask(__name__)
app.config["SECRET_KEY"] = "cambiar-esta-clave-en-produccion-v2"


# =========================
# Base de datos
# =========================
def get_db() -> sqlite3.Connection:
    if "db" not in g:
        conn = sqlite3.connect(DB_PATH)
        conn.row_factory = sqlite3.Row
        g.db = conn
    return g.db


@app.teardown_appcontext
def close_db(exception):
    db = g.pop("db", None)
    if db is not None:
        db.close()


def init_db() -> None:
    db = sqlite3.connect(DB_PATH)
    with closing(db.cursor()) as cur:
        cur.executescript(
            """
            CREATE TABLE IF NOT EXISTS users (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                username TEXT UNIQUE NOT NULL,
                password TEXT NOT NULL,
                nombre TEXT NOT NULL,
                propiedad TEXT NOT NULL,
                rol TEXT NOT NULL CHECK(rol IN ('admin', 'residente')),
                activo INTEGER NOT NULL DEFAULT 1,
                al_dia INTEGER NOT NULL DEFAULT 1,
                residente_permanente INTEGER NOT NULL DEFAULT 1
            );

            CREATE TABLE IF NOT EXISTS resources (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                codigo TEXT UNIQUE NOT NULL,
                nombre TEXT NOT NULL,
                tipo_exclusividad TEXT NOT NULL CHECK(tipo_exclusividad IN ('exclusivo', 'compartido')),
                capacidad_maxima INTEGER NOT NULL
            );

            CREATE TABLE IF NOT EXISTS config (
                clave TEXT PRIMARY KEY,
                valor TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS blocked_dates (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                resource_id INTEGER NOT NULL,
                fecha TEXT NOT NULL,
                motivo TEXT,
                UNIQUE(resource_id, fecha),
                FOREIGN KEY(resource_id) REFERENCES resources(id)
            );

            CREATE TABLE IF NOT EXISTS reservations (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                resource_id INTEGER NOT NULL,
                fecha TEXT NOT NULL,
                hora_inicio TEXT NOT NULL,
                hora_fin TEXT NOT NULL,
                asistentes INTEGER NOT NULL DEFAULT 1,
                invitados_registrados TEXT DEFAULT '',
                estado TEXT NOT NULL CHECK(estado IN ('pendiente', 'aprobada', 'rechazada', 'cancelada', 'requiere_ajuste')) DEFAULT 'pendiente',
                observaciones TEXT DEFAULT '',
                motivo_rechazo TEXT DEFAULT '',
                nota_admin TEXT DEFAULT '',
                created_at TEXT NOT NULL,
                updated_at TEXT DEFAULT '',
                FOREIGN KEY(user_id) REFERENCES users(id),
                FOREIGN KEY(resource_id) REFERENCES resources(id)
            );
            """
        )

        cur.execute("SELECT COUNT(*) FROM resources")
        if cur.fetchone()[0] == 0:
            cur.executemany(
                "INSERT INTO resources (codigo, nombre, tipo_exclusividad, capacidad_maxima) VALUES (?, ?, ?, ?)",
                [
                    ("SALON", "Salón social", "exclusivo", 40),
                    ("PISCINA", "Piscina", "compartido", 10),
                ],
            )

        config_default = {
            "dias_anticipacion_salon": "2",
            "hora_inicio_salon": "09:00",
            "hora_fin_salon": "21:00",
            "hora_inicio_piscina": "09:00",
            "hora_fin_piscina": "21:00",
            "dia_cierre_piscina": "1",  # martes
            "max_reservas_salon_mes": "2",
            "max_reservas_piscina_mes": "8",
            "max_dias_adelanto": "60",
            "auto_aprobar_salon": "0",
            "auto_aprobar_piscina": "1",
        }
        for clave, valor in config_default.items():
            cur.execute("INSERT OR IGNORE INTO config (clave, valor) VALUES (?, ?)", (clave, valor))

        cur.execute("SELECT COUNT(*) FROM users")
        if cur.fetchone()[0] == 0:
            cur.executemany(
                """
                INSERT INTO users (username, password, nombre, propiedad, rol, activo, al_dia, residente_permanente)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                [
                    ("admin", "admin123", "Administrador", "Administración", "admin", 1, 1, 1),
                    ("casa01", "demo123", "Residente Demo", "Casa 01", "residente", 1, 1, 1),
                ],
            )
        db.commit()
    db.close()


# =========================
# Utilidades
# =========================
def get_config(clave: str, default: Optional[str] = None) -> str:
    db = get_db()
    row = db.execute("SELECT valor FROM config WHERE clave = ?", (clave,)).fetchone()
    if row:
        return row["valor"]
    if default is None:
        raise KeyError(f"No existe configuración: {clave}")
    return default


def parse_fecha(fecha_str: str) -> date:
    return datetime.strptime(fecha_str, "%Y-%m-%d").date()


def login_required(view_func):
    @wraps(view_func)
    def wrapper(*args, **kwargs):
        if "user_id" not in session:
            flash("Debe iniciar sesión.", "warning")
            return redirect(url_for("login"))
        return view_func(*args, **kwargs)
    return wrapper


def admin_required(view_func):
    @wraps(view_func)
    def wrapper(*args, **kwargs):
        if "user_id" not in session:
            return redirect(url_for("login"))
        if session.get("rol") != "admin":
            abort(403)
        return view_func(*args, **kwargs)
    return wrapper


def current_user():
    if "user_id" not in session:
        return None
    db = get_db()
    return db.execute("SELECT * FROM users WHERE id = ?", (session["user_id"],)).fetchone()


def resource_by_codigo(codigo: str):
    db = get_db()
    return db.execute("SELECT * FROM resources WHERE codigo = ?", (codigo,)).fetchone()


def resource_by_id(resource_id: int):
    db = get_db()
    return db.execute("SELECT * FROM resources WHERE id = ?", (resource_id,)).fetchone()


def count_user_reservations_month(user_id: int, resource_id: int, fecha: date, exclude_id: Optional[int] = None) -> int:
    db = get_db()
    inicio = fecha.replace(day=1)
    if fecha.month == 12:
        fin = date(fecha.year + 1, 1, 1)
    else:
        fin = date(fecha.year, fecha.month + 1, 1)

    query = """
        SELECT COUNT(*) AS total
        FROM reservations
        WHERE user_id = ?
          AND resource_id = ?
          AND estado IN ('pendiente', 'aprobada', 'requiere_ajuste')
          AND fecha >= ?
          AND fecha < ?
    """
    params = [user_id, resource_id, inicio.isoformat(), fin.isoformat()]
    if exclude_id is not None:
        query += " AND id <> ?"
        params.append(exclude_id)
    row = db.execute(query, params).fetchone()
    return int(row["total"])


def count_pool_people_same_slot(fecha: str, hora_inicio: str, hora_fin: str, exclude_id: Optional[int] = None) -> int:
    db = get_db()
    query = """
        SELECT COALESCE(SUM(asistentes), 0) AS total
        FROM reservations r
        JOIN resources rs ON rs.id = r.resource_id
        WHERE rs.codigo = 'PISCINA'
          AND r.fecha = ?
          AND r.estado IN ('pendiente', 'aprobada', 'requiere_ajuste')
          AND NOT (r.hora_fin <= ? OR r.hora_inicio >= ?)
    """
    params = [fecha, hora_inicio, hora_fin]
    if exclude_id is not None:
        query += " AND r.id <> ?"
        params.append(exclude_id)
    row = db.execute(query, params).fetchone()
    return int(row["total"])


def has_conflict_exclusive(resource_id: int, fecha: str, hora_inicio: str, hora_fin: str, exclude_id: Optional[int] = None) -> bool:
    db = get_db()
    query = """
        SELECT 1
        FROM reservations
        WHERE resource_id = ?
          AND fecha = ?
          AND estado IN ('pendiente', 'aprobada', 'requiere_ajuste')
          AND NOT (hora_fin <= ? OR hora_inicio >= ?)
    """
    params = [resource_id, fecha, hora_inicio, hora_fin]
    if exclude_id is not None:
        query += " AND id <> ?"
        params.append(exclude_id)
    query += " LIMIT 1"
    row = db.execute(query, params).fetchone()
    return row is not None


def is_blocked(resource_id: int, fecha: str) -> Optional[str]:
    db = get_db()
    row = db.execute(
        "SELECT motivo FROM blocked_dates WHERE resource_id = ? AND fecha = ?",
        (resource_id, fecha),
    ).fetchone()
    return row["motivo"] if row else None


def create_reservation_record(user_id: int, resource_id: int, fecha: str, hora_inicio: str, hora_fin: str,
                              asistentes: int, invitados: str, estado: str, observaciones: str) -> None:
    db = get_db()
    db.execute(
        """
        INSERT INTO reservations
        (user_id, resource_id, fecha, hora_inicio, hora_fin, asistentes,
         invitados_registrados, estado, observaciones, motivo_rechazo, nota_admin, created_at, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, '', '', ?, ?)
        """,
        (
            user_id,
            resource_id,
            fecha,
            hora_inicio,
            hora_fin,
            asistentes,
            invitados,
            estado,
            observaciones,
            datetime.now().isoformat(timespec="seconds"),
            datetime.now().isoformat(timespec="seconds"),
        ),
    )
    db.commit()


def update_reservation_record(reservation_id: int, fecha: str, hora_inicio: str, hora_fin: str,
                              asistentes: int, invitados: str, observaciones: str, estado: Optional[str] = None,
                              nota_admin: Optional[str] = None) -> None:
    db = get_db()
    if estado is None:
        db.execute(
            """
            UPDATE reservations
            SET fecha = ?, hora_inicio = ?, hora_fin = ?, asistentes = ?,
                invitados_registrados = ?, observaciones = ?, updated_at = ?
            WHERE id = ?
            """,
            (
                fecha, hora_inicio, hora_fin, asistentes, invitados, observaciones,
                datetime.now().isoformat(timespec="seconds"), reservation_id
            ),
        )
    else:
        db.execute(
            """
            UPDATE reservations
            SET fecha = ?, hora_inicio = ?, hora_fin = ?, asistentes = ?,
                invitados_registrados = ?, observaciones = ?, estado = ?,
                nota_admin = ?, updated_at = ?
            WHERE id = ?
            """,
            (
                fecha, hora_inicio, hora_fin, asistentes, invitados, observaciones,
                estado, nota_admin or "", datetime.now().isoformat(timespec="seconds"), reservation_id
            ),
        )
    db.commit()


def reservation_status_badge(estado: str) -> str:
    mapping = {
        "aprobada": "success",
        "pendiente": "warning",
        "rechazada": "danger",
        "requiere_ajuste": "info",
        "cancelada": "secondary",
    }
    return mapping.get(estado, "secondary")


def validate_reservation_rules(user_row, resource_row, fecha_str: str, hora_inicio_str: str, hora_fin_str: str,
                               asistentes: int, exclude_id: Optional[int] = None):
    try:
        fecha = parse_fecha(fecha_str)
        datetime.strptime(hora_inicio_str, "%H:%M")
        datetime.strptime(hora_fin_str, "%H:%M")
    except ValueError:
        return False, "La fecha u hora tiene un formato inválido."

    hoy = date.today()
    if fecha < hoy:
        return False, "No se permiten reservas en fechas pasadas."

    max_dias_adelanto = int(get_config("max_dias_adelanto", "60"))
    if fecha > hoy + timedelta(days=max_dias_adelanto):
        return False, f"Solo se permiten reservas hasta {max_dias_adelanto} días hacia adelante."

    if hora_inicio_str >= hora_fin_str:
        return False, "La hora final debe ser posterior a la hora inicial."

    if user_row["rol"] != "admin" and int(user_row["al_dia"]) != 1:
        return False, "Solo pueden reservar residentes o propietarios al día en administración."

    bloqueo = is_blocked(resource_row["id"], fecha_str)
    if bloqueo:
        return False, f"La fecha está bloqueada: {bloqueo}"

    if resource_row["codigo"] == "SALON":
        dias_min = int(get_config("dias_anticipacion_salon", "2"))
        if fecha < hoy + timedelta(days=dias_min):
            return False, f"El salón debe reservarse con al menos {dias_min} días de anticipación."

        if hora_inicio_str < get_config("hora_inicio_salon", "09:00") or hora_fin_str > get_config("hora_fin_salon", "21:00"):
            return False, "El salón solo puede reservarse entre 09:00 y 21:00."

        if asistentes > int(resource_row["capacidad_maxima"]):
            return False, "La capacidad máxima del salón social es 40 personas."

        if has_conflict_exclusive(resource_row["id"], fecha_str, hora_inicio_str, hora_fin_str, exclude_id):
            return False, "El salón social ya se encuentra reservado en ese rango horario."

        limite_mes = int(get_config("max_reservas_salon_mes", "2"))
        usadas = count_user_reservations_month(user_row["id"], resource_row["id"], fecha, exclude_id)
        if usadas >= limite_mes and user_row["rol"] != "admin":
            return False, f"Ya alcanzó el límite mensual de {limite_mes} reservas para el salón."

    elif resource_row["codigo"] == "PISCINA":
        dia_cierre = int(get_config("dia_cierre_piscina", "1"))
        if fecha.weekday() == dia_cierre:
            return False, "La piscina permanece cerrada los martes por mantenimiento."

        if hora_inicio_str < get_config("hora_inicio_piscina", "09:00") or hora_fin_str > get_config("hora_fin_piscina", "21:00"):
            return False, "La piscina solo puede reservarse entre 09:00 y 21:00."

        aforo_actual = count_pool_people_same_slot(fecha_str, hora_inicio_str, hora_fin_str, exclude_id)
        if aforo_actual + asistentes > int(resource_row["capacidad_maxima"]):
            return False, "El aforo máximo de la piscina en uso compartido es de 10 personas."

        limite_mes = int(get_config("max_reservas_piscina_mes", "8"))
        usadas = count_user_reservations_month(user_row["id"], resource_row["id"], fecha, exclude_id)
        if usadas >= limite_mes and user_row["rol"] != "admin":
            return False, f"Ya alcanzó el límite mensual de {limite_mes} reservas para la piscina."

    return True, "Validación superada."


def get_calendar_month_data(year: int, month: int, is_admin: bool):
    db = get_db()
    cal = pycalendar.Calendar(firstweekday=0)
    days = list(cal.itermonthdates(year, month))
    inicio = min(days)
    fin = max(days)

    rows = db.execute(
        """
        SELECT r.*, rs.nombre AS recurso, rs.codigo AS recurso_codigo, u.propiedad, u.nombre
        FROM reservations r
        JOIN resources rs ON rs.id = r.resource_id
        JOIN users u ON u.id = r.user_id
        WHERE r.fecha >= ? AND r.fecha <= ?
          AND r.estado IN ('pendiente', 'aprobada', 'requiere_ajuste')
        ORDER BY r.fecha ASC, r.hora_inicio ASC
        """,
        (inicio.isoformat(), fin.isoformat()),
    ).fetchall()

    bloqueos = db.execute(
        """
        SELECT b.fecha, b.motivo, r.nombre AS recurso
        FROM blocked_dates b
        JOIN resources r ON r.id = b.resource_id
        WHERE b.fecha >= ? AND b.fecha <= ?
        """,
        (inicio.isoformat(), fin.isoformat()),
    ).fetchall()

    reservations_by_day = {}
    for r in rows:
        key = r["fecha"]
        reservations_by_day.setdefault(key, []).append({
            "texto": f'{r["recurso"]}: {r["hora_inicio"]}-{r["hora_fin"]} - Reservado',
            "detalle": f'({r["propiedad"]})' if is_admin else "",
            "estado": r["estado"],
        })

    blocked_by_day = {}
    for b in bloqueos:
        blocked_by_day.setdefault(b["fecha"], []).append(f'{b["recurso"]}: {b["motivo"] or "Bloqueado"}')

    weeks = []
    week = []
    for d in days:
        week.append({
            "date": d,
            "in_month": d.month == month,
            "reservations": reservations_by_day.get(d.isoformat(), []),
            "blocks": blocked_by_day.get(d.isoformat(), []),
        })
        if len(week) == 7:
            weeks.append(week)
            week = []
    return weeks


# =========================
# Plantilla base
# =========================
BASE_HTML = """
<!doctype html>
<html lang="es">
<head>
  <meta charset="utf-8">
  <title>{{ title or "Reservas Caña Brava" }}</title>
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <link href="https://cdn.jsdelivr.net/npm/bootstrap@5.3.3/dist/css/bootstrap.min.css" rel="stylesheet">
  <style>
    body { background: #f4f6f9; }
    .container-main { max-width: 1200px; }
    .card-shadow { box-shadow: 0 10px 25px rgba(0,0,0,.08); border: none; }
    .table td, .table th { vertical-align: middle; }
    .small-muted { font-size: .92rem; color: #6c757d; }
    .calendar-table td { width: 14.28%; min-height: 155px; height: 155px; vertical-align: top; background: #fff; }
    .day-box { font-size: .82rem; }
    .day-num { font-weight: 700; margin-bottom: .25rem; }
    .muted-day { background: #f1f3f5 !important; color: #adb5bd; }
    .event-pill { font-size: .74rem; padding: .2rem .4rem; border-radius: .5rem; display: block; margin-bottom: .25rem; }
    .event-booked { background: #e9ecef; }
    .event-block { background: #ffe3e3; color: #842029; }
    .top-actions a { text-decoration: none; }
  </style>
</head>
<body>
<nav class="navbar navbar-expand-lg bg-dark navbar-dark mb-4">
  <div class="container container-main">
    <a class="navbar-brand" href="{{ url_for('index') }}">Reservas Caña Brava</a>
    <div class="d-flex gap-2 align-items-center top-actions">
      {% if session.get('user_id') %}
        {% if session.get('rol') == 'admin' %}
          <a class="btn btn-outline-light btn-sm" href="{{ url_for('admin_dashboard') }}">Panel</a>
          <a class="btn btn-outline-light btn-sm" href="{{ url_for('admin_calendar') }}">Calendario</a>
        {% else %}
          <a class="btn btn-outline-light btn-sm" href="{{ url_for('mis_reservas') }}">Mis reservas</a>
          <a class="btn btn-outline-light btn-sm" href="{{ url_for('user_calendar') }}">Calendario</a>
        {% endif %}
        <span class="navbar-text text-white me-2">{{ session.get('nombre') }} ({{ session.get('rol') }})</span>
        <a class="btn btn-outline-light btn-sm" href="{{ url_for('logout') }}">Salir</a>
      {% endif %}
    </div>
  </div>
</nav>

<div class="container container-main pb-5">
  {% with messages = get_flashed_messages(with_categories=true) %}
    {% if messages %}
      {% for category, message in messages %}
        <div class="alert alert-{{ category if category in ['success','danger','warning','info','primary','secondary'] else 'info' }} alert-dismissible fade show" role="alert">
          {{ message }}
          <button type="button" class="btn-close" data-bs-dismiss="alert"></button>
        </div>
      {% endfor %}
    {% endif %}
  {% endwith %}

  {{ content|safe }}
</div>
<script src="https://cdn.jsdelivr.net/npm/bootstrap@5.3.3/dist/js/bootstrap.bundle.min.js"></script>
</body>
</html>
"""


def render_page(content: str, **context):
    rendered_content = render_template_string(content, **context)
    return render_template_string(BASE_HTML, content=rendered_content, **context)


# =========================
# Autenticación
# =========================
@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "").strip()
        db = get_db()
        user = db.execute(
            "SELECT * FROM users WHERE username = ? AND password = ? AND activo = 1",
            (username, password),
        ).fetchone()

        if user:
            session.clear()
            session["user_id"] = user["id"]
            session["rol"] = user["rol"]
            session["nombre"] = user["nombre"]
            flash("Sesión iniciada correctamente.", "success")
            return redirect(url_for("index"))
        flash("Credenciales inválidas.", "danger")

    content = """
    <div class="row justify-content-center">
      <div class="col-md-5">
        <div class="card card-shadow">
          <div class="card-body p-4">
            <h2 class="mb-3">Ingreso</h2>
            <p class="small-muted">Versión 2 del sistema de reservas.</p>
            <form method="post">
              <div class="mb-3">
                <label class="form-label">Usuario</label>
                <input name="username" class="form-control" required>
              </div>
              <div class="mb-3">
                <label class="form-label">Contraseña</label>
                <input name="password" type="password" class="form-control" required>
              </div>
              <button class="btn btn-dark w-100">Entrar</button>
            </form>
            <hr>
            <div class="small-muted">
              <strong>Demo admin:</strong> admin / admin123<br>
              <strong>Demo residente:</strong> casa01 / demo123
            </div>
          </div>
        </div>
      </div>
    </div>
    """
    return render_page(content, title="Ingreso")


@app.route("/logout")
def logout():
    session.clear()
    flash("Sesión finalizada.", "info")
    return redirect(url_for("login"))


# =========================
# Inicio
# =========================
@app.route("/")
@login_required
def index():
    if session.get("rol") == "admin":
        return redirect(url_for("admin_dashboard"))
    return redirect(url_for("mis_reservas"))


# =========================
# Calendarios
# =========================
@app.route("/calendario")
@login_required
def user_calendar():
    today = date.today()
    year = int(request.args.get("year", today.year))
    month = int(request.args.get("month", today.month))
    weeks = get_calendar_month_data(year, month, is_admin=False)

    prev_month = month - 1 or 12
    prev_year = year - 1 if month == 1 else year
    next_month = month + 1 if month < 12 else 1
    next_year = year + 1 if month == 12 else year

    content = """
    <div class="d-flex justify-content-between align-items-center mb-3">
      <div>
        <h2 class="mb-1">Calendario de disponibilidad</h2>
        <div class="small-muted">Se muestra únicamente “Reservado” y el horario ocupado.</div>
      </div>
      <div class="d-flex gap-2">
        <a class="btn btn-outline-secondary" href="{{ url_for('user_calendar', year=prev_year, month=prev_month) }}">Mes anterior</a>
        <a class="btn btn-outline-secondary" href="{{ url_for('user_calendar', year=next_year, month=next_month) }}">Mes siguiente</a>
      </div>
    </div>

    <div class="card card-shadow mb-3">
      <div class="card-body">
        <h4>{{ month_name }} {{ year }}</h4>
        <div class="table-responsive">
          <table class="table table-bordered calendar-table">
            <thead>
              <tr>
                <th>Lun</th><th>Mar</th><th>Mié</th><th>Jue</th><th>Vie</th><th>Sáb</th><th>Dom</th>
              </tr>
            </thead>
            <tbody>
              {% for week in weeks %}
                <tr>
                  {% for day in week %}
                    <td class="{{ 'muted-day' if not day.in_month else '' }}">
                      <div class="day-box">
                        <div class="day-num">{{ day.date.day }}</div>
                        {% for b in day.blocks %}
                          <span class="event-pill event-block">{{ b }}</span>
                        {% endfor %}
                        {% for r in day.reservations %}
                          <span class="event-pill event-booked">{{ r.texto }}</span>
                        {% endfor %}
                      </div>
                    </td>
                  {% endfor %}
                </tr>
              {% endfor %}
            </tbody>
          </table>
        </div>
      </div>
    </div>
    """
    return render_page(
        content,
        title="Calendario",
        weeks=weeks,
        year=year,
        month=month,
        month_name=pycalendar.month_name[month],
        prev_month=prev_month,
        prev_year=prev_year,
        next_month=next_month,
        next_year=next_year,
    )


@app.route("/admin/calendario")
@admin_required
def admin_calendar():
    today = date.today()
    year = int(request.args.get("year", today.year))
    month = int(request.args.get("month", today.month))
    weeks = get_calendar_month_data(year, month, is_admin=True)

    prev_month = month - 1 or 12
    prev_year = year - 1 if month == 1 else year
    next_month = month + 1 if month < 12 else 1
    next_year = year + 1 if month == 12 else year

    content = """
    <div class="d-flex justify-content-between align-items-center mb-3">
      <div>
        <h2 class="mb-1">Calendario administrativo</h2>
        <div class="small-muted">Aquí sí se visualiza qué propiedad tiene cada reserva.</div>
      </div>
      <div class="d-flex gap-2">
        <a class="btn btn-outline-secondary" href="{{ url_for('admin_calendar', year=prev_year, month=prev_month) }}">Mes anterior</a>
        <a class="btn btn-outline-secondary" href="{{ url_for('admin_calendar', year=next_year, month=next_month) }}">Mes siguiente</a>
      </div>
    </div>

    <div class="card card-shadow mb-3">
      <div class="card-body">
        <h4>{{ month_name }} {{ year }}</h4>
        <div class="table-responsive">
          <table class="table table-bordered calendar-table">
            <thead>
              <tr>
                <th>Lun</th><th>Mar</th><th>Mié</th><th>Jue</th><th>Vie</th><th>Sáb</th><th>Dom</th>
              </tr>
            </thead>
            <tbody>
              {% for week in weeks %}
                <tr>
                  {% for day in week %}
                    <td class="{{ 'muted-day' if not day.in_month else '' }}">
                      <div class="day-box">
                        <div class="day-num">{{ day.date.day }}</div>
                        {% for b in day.blocks %}
                          <span class="event-pill event-block">{{ b }}</span>
                        {% endfor %}
                        {% for r in day.reservations %}
                          <span class="event-pill event-booked">{{ r.texto }} {{ r.detalle }}</span>
                        {% endfor %}
                      </div>
                    </td>
                  {% endfor %}
                </tr>
              {% endfor %}
            </tbody>
          </table>
        </div>
      </div>
    </div>
    """
    return render_page(
        content,
        title="Calendario admin",
        weeks=weeks,
        year=year,
        month=month,
        month_name=pycalendar.month_name[month],
        prev_month=prev_month,
        prev_year=prev_year,
        next_month=next_month,
        next_year=next_year,
    )


# =========================
# Usuario
# =========================
@app.route("/mis-reservas")
@login_required
def mis_reservas():
    db = get_db()
    reservas = db.execute(
        """
        SELECT r.*, rs.nombre AS recurso
        FROM reservations r
        JOIN resources rs ON rs.id = r.resource_id
        WHERE r.user_id = ?
        ORDER BY r.fecha DESC, r.hora_inicio DESC
        """,
        (session["user_id"],),
    ).fetchall()

    content = """
    <div class="d-flex justify-content-between align-items-center mb-3">
      <div>
        <h2 class="mb-1">Mis reservas</h2>
        <div class="small-muted">Aquí puede consultar, editar y registrar solicitudes.</div>
      </div>
      <div class="d-flex gap-2 flex-wrap">
        <a class="btn btn-dark" href="{{ url_for('nueva_reserva_combinada') }}">Reservar salón + piscina</a>
        <a class="btn btn-primary" href="{{ url_for('nueva_reserva', codigo='SALON') }}">Reservar salón</a>
        <a class="btn btn-success" href="{{ url_for('nueva_reserva', codigo='PISCINA') }}">Reservar piscina</a>
        <a class="btn btn-outline-secondary" href="{{ url_for('user_calendar') }}">Ver calendario</a>
      </div>
    </div>

    <div class="card card-shadow">
      <div class="card-body">
        <div class="table-responsive">
          <table class="table table-striped">
            <thead>
              <tr>
                <th>Recurso</th>
                <th>Fecha</th>
                <th>Horario</th>
                <th>Asistentes</th>
                <th>Estado</th>
                <th>Nota admin</th>
                <th>Observaciones</th>
                <th>Acciones</th>
              </tr>
            </thead>
            <tbody>
              {% for r in reservas %}
                <tr>
                  <td>{{ r['recurso'] }}</td>
                  <td>{{ r['fecha'] }}</td>
                  <td>{{ r['hora_inicio'] }} - {{ r['hora_fin'] }}</td>
                  <td>{{ r['asistentes'] }}</td>
                  <td><span class="badge text-bg-{{ reservation_status_badge(r['estado']) }}">{{ r['estado'] }}</span></td>
                  <td>{{ r['nota_admin'] or '' }}</td>
                  <td>{{ r['observaciones'] or '' }}</td>
                  <td>
                    {% if r['estado'] in ['pendiente', 'requiere_ajuste'] %}
                      <a class="btn btn-sm btn-outline-primary" href="{{ url_for('editar_mi_reserva', reserva_id=r['id']) }}">Editar</a>
                    {% else %}
                      <span class="text-muted">Sin acción</span>
                    {% endif %}
                  </td>
                </tr>
              {% else %}
                <tr><td colspan="8" class="text-center text-muted">No hay reservas registradas.</td></tr>
              {% endfor %}
            </tbody>
          </table>
        </div>
      </div>
    </div>
    """
    return render_page(content, title="Mis reservas", reservas=reservas, reservation_status_badge=reservation_status_badge)


@app.route("/nueva-reserva/<codigo>", methods=["GET", "POST"])
@login_required
def nueva_reserva(codigo: str):
    recurso = resource_by_codigo(codigo.upper())
    if not recurso:
        abort(404)

    user = current_user()
    if request.method == "POST":
        fecha = request.form.get("fecha", "").strip()
        hora_inicio = request.form.get("hora_inicio", "").strip()
        hora_fin = request.form.get("hora_fin", "").strip()
        asistentes = int(request.form.get("asistentes", "1"))
        invitados = request.form.get("invitados_registrados", "").strip()
        observaciones = request.form.get("observaciones", "").strip()

        ok, mensaje = validate_reservation_rules(user, recurso, fecha, hora_inicio, hora_fin, asistentes)
        if not ok:
            flash(mensaje, "danger")
        else:
            estado = "aprobada" if (
                recurso["codigo"] == "SALON" and get_config("auto_aprobar_salon", "0") == "1"
            ) or (
                recurso["codigo"] == "PISCINA" and get_config("auto_aprobar_piscina", "1") == "1"
            ) else "pendiente"
            create_reservation_record(user["id"], recurso["id"], fecha, hora_inicio, hora_fin, asistentes, invitados, estado, observaciones)
            flash(f"Reserva registrada con estado: {estado}.", "success")
            return redirect(url_for("mis_reservas"))

    ayuda = {
        "SALON": [
            "Reserva con al menos 2 días de anticipación.",
            "Horario permitido: 09:00 a 21:00.",
            "Uso exclusivo para quien lo reserve.",
            "Capacidad máxima: 40 personas.",
            "No se permite ceder la reserva a terceros no autorizados.",
        ],
        "PISCINA": [
            "Horario permitido: miércoles a lunes de 09:00 a 21:00.",
            "El martes está cerrado por mantenimiento.",
            "La piscina no es exclusiva por reservar el salón.",
            "Aforo compartido máximo: 10 personas.",
            "Solo residentes e invitados registrados.",
        ],
    }

    content = """
    <div class="row">
      <div class="col-lg-8">
        <div class="card card-shadow mb-3">
          <div class="card-body">
            <h2 class="mb-1">Nueva reserva - {{ recurso['nombre'] }}</h2>
            <div class="small-muted mb-3">La validación aplica automáticamente las reglas base de convivencia.</div>
            <form method="post">
              <div class="row">
                <div class="col-md-4 mb-3">
                  <label class="form-label">Fecha</label>
                  <input type="date" name="fecha" class="form-control" required>
                </div>
                <div class="col-md-4 mb-3">
                  <label class="form-label">Hora inicio</label>
                  <input type="time" name="hora_inicio" class="form-control" required>
                </div>
                <div class="col-md-4 mb-3">
                  <label class="form-label">Hora fin</label>
                  <input type="time" name="hora_fin" class="form-control" required>
                </div>
              </div>

              <div class="row">
                <div class="col-md-4 mb-3">
                  <label class="form-label">Número de asistentes</label>
                  <input type="number" name="asistentes" min="1" max="{{ recurso['capacidad_maxima'] }}" class="form-control" required value="1">
                </div>
              </div>

              <div class="mb-3">
                <label class="form-label">Invitados registrados</label>
                <textarea name="invitados_registrados" class="form-control" rows="4" placeholder="Un invitado por línea. Ejemplo: Nombre - Documento"></textarea>
              </div>

              <div class="mb-3">
                <label class="form-label">Observaciones</label>
                <textarea name="observaciones" class="form-control" rows="3"></textarea>
              </div>

              <div class="d-flex gap-2">
                <button class="btn btn-primary">Guardar solicitud</button>
                <a class="btn btn-outline-secondary" href="{{ url_for('mis_reservas') }}">Volver</a>
              </div>
            </form>
          </div>
        </div>
      </div>

      <div class="col-lg-4">
        <div class="card card-shadow">
          <div class="card-body">
            <h5>Reglas clave</h5>
            <ul class="mb-0">
              {% for item in ayuda[recurso['codigo']] %}
                <li>{{ item }}</li>
              {% endfor %}
            </ul>
          </div>
        </div>
      </div>
    </div>
    """
    return render_page(content, title=f"Nueva reserva - {recurso['nombre']}", recurso=recurso, ayuda=ayuda)


@app.route("/nueva-reserva-combinada", methods=["GET", "POST"])
@login_required
def nueva_reserva_combinada():
    user = current_user()
    salon = resource_by_codigo("SALON")
    piscina = resource_by_codigo("PISCINA")

    if request.method == "POST":
        fecha = request.form.get("fecha", "").strip()
        mismo_horario = request.form.get("mismo_horario") == "on"

        reservar_salon = request.form.get("reservar_salon") == "on"
        salon_hora_inicio = request.form.get("salon_hora_inicio", "").strip()
        salon_hora_fin = request.form.get("salon_hora_fin", "").strip()
        salon_asistentes = int(request.form.get("salon_asistentes", "1") or "1")

        reservar_piscina = request.form.get("reservar_piscina") == "on"
        piscina_hora_inicio = request.form.get("piscina_hora_inicio", "").strip()
        piscina_hora_fin = request.form.get("piscina_hora_fin", "").strip()
        piscina_asistentes = int(request.form.get("piscina_asistentes", "1") or "1")

        if mismo_horario:
            piscina_hora_inicio = salon_hora_inicio
            piscina_hora_fin = salon_hora_fin

        invitados = request.form.get("invitados_registrados", "").strip()
        observaciones = request.form.get("observaciones", "").strip()

        if not reservar_salon and not reservar_piscina:
            flash("Debe seleccionar al menos un espacio para reservar.", "danger")
        else:
            errores = []

            if reservar_salon:
                ok_salon, msg_salon = validate_reservation_rules(user, salon, fecha, salon_hora_inicio, salon_hora_fin, salon_asistentes)
                if not ok_salon:
                    errores.append(f"Salón: {msg_salon}")

            if reservar_piscina:
                ok_piscina, msg_piscina = validate_reservation_rules(user, piscina, fecha, piscina_hora_inicio, piscina_hora_fin, piscina_asistentes)
                if not ok_piscina:
                    errores.append(f"Piscina: {msg_piscina}")

            if errores:
                for err in errores:
                    flash(err, "danger")
            else:
                if reservar_salon:
                    estado_salon = "aprobada" if get_config("auto_aprobar_salon", "0") == "1" else "pendiente"
                    create_reservation_record(user["id"], salon["id"], fecha, salon_hora_inicio, salon_hora_fin, salon_asistentes, invitados, estado_salon, observaciones)
                if reservar_piscina:
                    estado_piscina = "aprobada" if get_config("auto_aprobar_piscina", "1") == "1" else "pendiente"
                    create_reservation_record(user["id"], piscina["id"], fecha, piscina_hora_inicio, piscina_hora_fin, piscina_asistentes, invitados, estado_piscina, observaciones)

                flash("Se registró la solicitud combinada.", "success")
                return redirect(url_for("mis_reservas"))

    content = """
    <div class="row">
      <div class="col-lg-8">
        <div class="card card-shadow mb-3">
          <div class="card-body">
            <h2 class="mb-1">Reserva combinada</h2>
            <div class="small-muted mb-3">Puede reservar salón y piscina en un solo formulario.</div>

            <form method="post">
              <div class="mb-3">
                <label class="form-label">Fecha única para la solicitud</label>
                <input type="date" name="fecha" class="form-control" required>
              </div>

              <div class="form-check mb-3">
                <input class="form-check-input" type="checkbox" name="mismo_horario" id="mismo_horario">
                <label class="form-check-label" for="mismo_horario">Usar el mismo horario para salón y piscina</label>
              </div>

              <div class="border rounded p-3 mb-3">
                <div class="form-check form-switch mb-3">
                  <input class="form-check-input" type="checkbox" name="reservar_salon" id="reservar_salon" checked>
                  <label class="form-check-label" for="reservar_salon"><strong>Reservar salón social</strong></label>
                </div>

                <div class="row">
                  <div class="col-md-4 mb-3">
                    <label class="form-label">Hora inicio salón</label>
                    <input type="time" name="salon_hora_inicio" class="form-control" value="09:00">
                  </div>
                  <div class="col-md-4 mb-3">
                    <label class="form-label">Hora fin salón</label>
                    <input type="time" name="salon_hora_fin" class="form-control" value="21:00">
                  </div>
                  <div class="col-md-4 mb-3">
                    <label class="form-label">Asistentes salón</label>
                    <input type="number" name="salon_asistentes" min="1" max="40" class="form-control" value="1">
                  </div>
                </div>
              </div>

              <div class="border rounded p-3 mb-3">
                <div class="form-check form-switch mb-3">
                  <input class="form-check-input" type="checkbox" name="reservar_piscina" id="reservar_piscina" checked>
                  <label class="form-check-label" for="reservar_piscina"><strong>Reservar piscina</strong></label>
                </div>

                <div class="row">
                  <div class="col-md-4 mb-3">
                    <label class="form-label">Hora inicio piscina</label>
                    <input type="time" name="piscina_hora_inicio" class="form-control" value="09:00">
                  </div>
                  <div class="col-md-4 mb-3">
                    <label class="form-label">Hora fin piscina</label>
                    <input type="time" name="piscina_hora_fin" class="form-control" value="21:00">
                  </div>
                  <div class="col-md-4 mb-3">
                    <label class="form-label">Asistentes piscina</label>
                    <input type="number" name="piscina_asistentes" min="1" max="10" class="form-control" value="1">
                  </div>
                </div>
              </div>

              <div class="mb-3">
                <label class="form-label">Invitados registrados</label>
                <textarea name="invitados_registrados" class="form-control" rows="4" placeholder="Un invitado por línea. Ejemplo: Nombre - Documento"></textarea>
              </div>

              <div class="mb-3">
                <label class="form-label">Observaciones</label>
                <textarea name="observaciones" class="form-control" rows="3"></textarea>
              </div>

              <div class="d-flex gap-2">
                <button class="btn btn-primary">Guardar solicitud</button>
                <a class="btn btn-outline-secondary" href="{{ url_for('mis_reservas') }}">Volver</a>
              </div>
            </form>
          </div>
        </div>
      </div>

      <div class="col-lg-4">
        <div class="card card-shadow">
          <div class="card-body">
            <h5>Notas importantes</h5>
            <ul class="mb-0">
              <li>El salón y la piscina se validan por separado.</li>
              <li>Si uno de los dos incumple reglas, no se guarda ninguno.</li>
              <li>La piscina no es exclusiva por reservar el salón.</li>
              <li>El martes la piscina está cerrada por mantenimiento.</li>
              <li>La reserva del salón exige al menos 2 días de anticipación.</li>
            </ul>
          </div>
        </div>
      </div>
    </div>
    """
    return render_page(content, title="Reserva combinada")


@app.route("/mis-reservas/<int:reserva_id>/editar", methods=["GET", "POST"])
@login_required
def editar_mi_reserva(reserva_id: int):
    db = get_db()
    reserva = db.execute(
        """
        SELECT r.*, rs.nombre AS recurso, rs.codigo AS recurso_codigo
        FROM reservations r
        JOIN resources rs ON rs.id = r.resource_id
        WHERE r.id = ? AND r.user_id = ?
        """,
        (reserva_id, session["user_id"]),
    ).fetchone()
    if not reserva:
        abort(404)

    if reserva["estado"] not in ("pendiente", "requiere_ajuste"):
        flash("Solo puede editar reservas pendientes o con solicitud de ajuste.", "warning")
        return redirect(url_for("mis_reservas"))

    recurso = resource_by_id(reserva["resource_id"])
    user = current_user()

    if request.method == "POST":
        fecha = request.form.get("fecha", "").strip()
        hora_inicio = request.form.get("hora_inicio", "").strip()
        hora_fin = request.form.get("hora_fin", "").strip()
        asistentes = int(request.form.get("asistentes", "1"))
        invitados = request.form.get("invitados_registrados", "").strip()
        observaciones = request.form.get("observaciones", "").strip()

        ok, mensaje = validate_reservation_rules(user, recurso, fecha, hora_inicio, hora_fin, asistentes, exclude_id=reserva_id)
        if not ok:
            flash(mensaje, "danger")
        else:
            nuevo_estado = "pendiente"
            update_reservation_record(reserva_id, fecha, hora_inicio, hora_fin, asistentes, invitados, observaciones, estado=nuevo_estado, nota_admin="")
            flash("Reserva actualizada y enviada nuevamente para validación.", "success")
            return redirect(url_for("mis_reservas"))

    content = """
    <div class="row justify-content-center">
      <div class="col-lg-8">
        <div class="card card-shadow">
          <div class="card-body">
            <h3>Editar reserva - {{ reserva['recurso'] }}</h3>
            <p class="small-muted">Nota administrativa: {{ reserva['nota_admin'] or 'Sin observaciones' }}</p>
            <form method="post">
              <div class="row">
                <div class="col-md-4 mb-3">
                  <label class="form-label">Fecha</label>
                  <input type="date" name="fecha" class="form-control" value="{{ reserva['fecha'] }}" required>
                </div>
                <div class="col-md-4 mb-3">
                  <label class="form-label">Hora inicio</label>
                  <input type="time" name="hora_inicio" class="form-control" value="{{ reserva['hora_inicio'] }}" required>
                </div>
                <div class="col-md-4 mb-3">
                  <label class="form-label">Hora fin</label>
                  <input type="time" name="hora_fin" class="form-control" value="{{ reserva['hora_fin'] }}" required>
                </div>
              </div>
              <div class="row">
                <div class="col-md-4 mb-3">
                  <label class="form-label">Asistentes</label>
                  <input type="number" name="asistentes" class="form-control" value="{{ reserva['asistentes'] }}" required>
                </div>
              </div>
              <div class="mb-3">
                <label class="form-label">Invitados registrados</label>
                <textarea name="invitados_registrados" class="form-control" rows="4">{{ reserva['invitados_registrados'] }}</textarea>
              </div>
              <div class="mb-3">
                <label class="form-label">Observaciones</label>
                <textarea name="observaciones" class="form-control" rows="3">{{ reserva['observaciones'] }}</textarea>
              </div>
              <button class="btn btn-primary">Guardar cambios</button>
              <a class="btn btn-outline-secondary" href="{{ url_for('mis_reservas') }}">Volver</a>
            </form>
          </div>
        </div>
      </div>
    </div>
    """
    return render_page(content, title="Editar reserva", reserva=reserva)


# =========================
# Admin
# =========================
@app.route("/admin")
@admin_required
def admin_dashboard():
    db = get_db()
    resumen = {
        "usuarios": db.execute("SELECT COUNT(*) total FROM users").fetchone()["total"],
        "pendientes": db.execute("SELECT COUNT(*) total FROM reservations WHERE estado = 'pendiente'").fetchone()["total"],
        "aprobadas": db.execute("SELECT COUNT(*) total FROM reservations WHERE estado = 'aprobada'").fetchone()["total"],
        "bloqueos": db.execute("SELECT COUNT(*) total FROM blocked_dates").fetchone()["total"],
    }

    reservas = db.execute(
        """
        SELECT r.*, u.nombre, u.propiedad, rs.nombre AS recurso
        FROM reservations r
        JOIN users u ON u.id = r.user_id
        JOIN resources rs ON rs.id = r.resource_id
        ORDER BY r.fecha DESC, r.hora_inicio DESC
        LIMIT 50
        """
    ).fetchall()

    content = """
    <div class="d-flex justify-content-between align-items-center mb-4">
      <div>
        <h2 class="mb-1">Panel administrador</h2>
        <div class="small-muted">Recomendación: es mejor usar “solicitar ajuste” antes de modificar directamente, para conservar trazabilidad.</div>
      </div>
      <div class="d-flex gap-2 flex-wrap">
        <a class="btn btn-outline-primary" href="{{ url_for('admin_users') }}">Usuarios</a>
        <a class="btn btn-outline-success" href="{{ url_for('admin_blocks') }}">Fechas bloqueadas</a>
        <a class="btn btn-outline-dark" href="{{ url_for('admin_config') }}">Configuración</a>
        <a class="btn btn-outline-secondary" href="{{ url_for('admin_calendar') }}">Calendario</a>
      </div>
    </div>

    <div class="row g-3 mb-4">
      <div class="col-md-3"><div class="card card-shadow"><div class="card-body"><h6>Usuarios</h6><div class="fs-3">{{ resumen['usuarios'] }}</div></div></div></div>
      <div class="col-md-3"><div class="card card-shadow"><div class="card-body"><h6>Pendientes</h6><div class="fs-3">{{ resumen['pendientes'] }}</div></div></div></div>
      <div class="col-md-3"><div class="card card-shadow"><div class="card-body"><h6>Aprobadas</h6><div class="fs-3">{{ resumen['aprobadas'] }}</div></div></div></div>
      <div class="col-md-3"><div class="card card-shadow"><div class="card-body"><h6>Bloqueos</h6><div class="fs-3">{{ resumen['bloqueos'] }}</div></div></div></div>
    </div>

    <div class="card card-shadow">
      <div class="card-body">
        <h5 class="mb-3">Reservas recientes</h5>
        <div class="table-responsive">
          <table class="table table-striped">
            <thead>
              <tr>
                <th>Residente</th>
                <th>Propiedad</th>
                <th>Recurso</th>
                <th>Fecha</th>
                <th>Horario</th>
                <th>Asistentes</th>
                <th>Estado</th>
                <th>Nota admin</th>
                <th>Acciones</th>
              </tr>
            </thead>
            <tbody>
              {% for r in reservas %}
                <tr>
                  <td>{{ r['nombre'] }}</td>
                  <td>{{ r['propiedad'] }}</td>
                  <td>{{ r['recurso'] }}</td>
                  <td>{{ r['fecha'] }}</td>
                  <td>{{ r['hora_inicio'] }} - {{ r['hora_fin'] }}</td>
                  <td>{{ r['asistentes'] }}</td>
                  <td><span class="badge text-bg-{{ reservation_status_badge(r['estado']) }}">{{ r['estado'] }}</span></td>
                  <td>{{ r['nota_admin'] or '' }}</td>
                  <td class="d-flex gap-1 flex-wrap">
                    {% if r['estado'] in ['pendiente', 'requiere_ajuste'] %}
                      <a class="btn btn-sm btn-success" href="{{ url_for('admin_decision_reserva', reserva_id=r['id'], decision='aprobar') }}">Aprobar</a>
                      <a class="btn btn-sm btn-info" href="{{ url_for('admin_requerir_ajuste', reserva_id=r['id']) }}">Solicitar ajuste</a>
                    {% endif %}
                    <a class="btn btn-sm btn-outline-primary" href="{{ url_for('admin_editar_reserva', reserva_id=r['id']) }}">Modificar</a>
                    <a class="btn btn-sm btn-outline-danger" href="{{ url_for('admin_eliminar_reserva', reserva_id=r['id']) }}" onclick="return confirm('¿Eliminar esta reserva?')">Eliminar</a>
                  </td>
                </tr>
              {% else %}
                <tr><td colspan="9" class="text-center text-muted">No hay reservas.</td></tr>
              {% endfor %}
            </tbody>
          </table>
        </div>
      </div>
    </div>
    """
    return render_page(content, title="Panel admin", resumen=resumen, reservas=reservas, reservation_status_badge=reservation_status_badge)


@app.route("/admin/reserva/<int:reserva_id>/<decision>")
@admin_required
def admin_decision_reserva(reserva_id: int, decision: str):
    db = get_db()
    reserva = db.execute("SELECT * FROM reservations WHERE id = ?", (reserva_id,)).fetchone()
    if not reserva:
        abort(404)

    if decision == "aprobar":
        db.execute(
            "UPDATE reservations SET estado = 'aprobada', motivo_rechazo = '', nota_admin = '', updated_at = ? WHERE id = ?",
            (datetime.now().isoformat(timespec="seconds"), reserva_id),
        )
        db.commit()
        flash("Reserva aprobada.", "success")
    else:
        abort(400)
    return redirect(url_for("admin_dashboard"))


@app.route("/admin/reserva/<int:reserva_id>/ajuste", methods=["GET", "POST"])
@admin_required
def admin_requerir_ajuste(reserva_id: int):
    db = get_db()
    reserva = db.execute(
        """
        SELECT r.*, u.nombre, rs.nombre AS recurso
        FROM reservations r
        JOIN users u ON u.id = r.user_id
        JOIN resources rs ON rs.id = r.resource_id
        WHERE r.id = ?
        """,
        (reserva_id,),
    ).fetchone()
    if not reserva:
        abort(404)

    if request.method == "POST":
        nota = request.form.get("nota_admin", "").strip()
        if not nota:
            flash("Debe indicar qué ajuste solicita.", "danger")
        else:
            db.execute(
                "UPDATE reservations SET estado = 'requiere_ajuste', nota_admin = ?, updated_at = ? WHERE id = ?",
                (nota, datetime.now().isoformat(timespec="seconds"), reserva_id),
            )
            db.commit()
            flash("Se solicitó ajuste al usuario.", "info")
            return redirect(url_for("admin_dashboard"))

    content = """
    <div class="row justify-content-center">
      <div class="col-lg-6">
        <div class="card card-shadow">
          <div class="card-body">
            <h3>Solicitar ajuste</h3>
            <p class="small-muted">{{ reserva['nombre'] }} - {{ reserva['recurso'] }} - {{ reserva['fecha'] }}</p>
            <form method="post">
              <div class="mb-3">
                <label class="form-label">Indicación para el usuario</label>
                <textarea name="nota_admin" class="form-control" rows="4" required placeholder="Ejemplo: ajustar horario, corregir número de asistentes, etc."></textarea>
              </div>
              <button class="btn btn-info">Enviar solicitud</button>
              <a class="btn btn-outline-secondary" href="{{ url_for('admin_dashboard') }}">Volver</a>
            </form>
          </div>
        </div>
      </div>
    </div>
    """
    return render_page(content, title="Solicitar ajuste", reserva=reserva)


@app.route("/admin/reserva/<int:reserva_id>/editar", methods=["GET", "POST"])
@admin_required
def admin_editar_reserva(reserva_id: int):
    db = get_db()
    reserva = db.execute(
        """
        SELECT r.*, u.nombre, u.propiedad, rs.nombre AS recurso, rs.codigo AS recurso_codigo
        FROM reservations r
        JOIN users u ON u.id = r.user_id
        JOIN resources rs ON rs.id = r.resource_id
        WHERE r.id = ?
        """,
        (reserva_id,),
    ).fetchone()
    if not reserva:
        abort(404)

    recurso = resource_by_id(reserva["resource_id"])
    user = db.execute("SELECT * FROM users WHERE id = ?", (reserva["user_id"],)).fetchone()

    if request.method == "POST":
        fecha = request.form.get("fecha", "").strip()
        hora_inicio = request.form.get("hora_inicio", "").strip()
        hora_fin = request.form.get("hora_fin", "").strip()
        asistentes = int(request.form.get("asistentes", "1"))
        invitados = request.form.get("invitados_registrados", "").strip()
        observaciones = request.form.get("observaciones", "").strip()
        estado = request.form.get("estado", "pendiente").strip()
        nota_admin = request.form.get("nota_admin", "").strip()

        ok, mensaje = validate_reservation_rules(user, recurso, fecha, hora_inicio, hora_fin, asistentes, exclude_id=reserva_id)
        if not ok:
            flash(mensaje, "danger")
        else:
            update_reservation_record(reserva_id, fecha, hora_inicio, hora_fin, asistentes, invitados, observaciones, estado=estado, nota_admin=nota_admin)
            flash("Reserva modificada correctamente.", "success")
            return redirect(url_for("admin_dashboard"))

    content = """
    <div class="row justify-content-center">
      <div class="col-lg-8">
        <div class="card card-shadow">
          <div class="card-body">
            <h3>Modificar reserva</h3>
            <p class="small-muted">{{ reserva['nombre'] }} - {{ reserva['propiedad'] }} - {{ reserva['recurso'] }}</p>
            <form method="post">
              <div class="row">
                <div class="col-md-4 mb-3">
                  <label class="form-label">Fecha</label>
                  <input type="date" name="fecha" class="form-control" value="{{ reserva['fecha'] }}" required>
                </div>
                <div class="col-md-4 mb-3">
                  <label class="form-label">Hora inicio</label>
                  <input type="time" name="hora_inicio" class="form-control" value="{{ reserva['hora_inicio'] }}" required>
                </div>
                <div class="col-md-4 mb-3">
                  <label class="form-label">Hora fin</label>
                  <input type="time" name="hora_fin" class="form-control" value="{{ reserva['hora_fin'] }}" required>
                </div>
              </div>
              <div class="row">
                <div class="col-md-4 mb-3">
                  <label class="form-label">Asistentes</label>
                  <input type="number" name="asistentes" class="form-control" value="{{ reserva['asistentes'] }}" required>
                </div>
                <div class="col-md-4 mb-3">
                  <label class="form-label">Estado</label>
                  <select name="estado" class="form-select">
                    {% for e in ['pendiente', 'aprobada', 'requiere_ajuste', 'rechazada', 'cancelada'] %}
                      <option value="{{ e }}" {{ 'selected' if reserva['estado'] == e else '' }}>{{ e }}</option>
                    {% endfor %}
                  </select>
                </div>
              </div>
              <div class="mb-3">
                <label class="form-label">Invitados registrados</label>
                <textarea name="invitados_registrados" class="form-control" rows="4">{{ reserva['invitados_registrados'] }}</textarea>
              </div>
              <div class="mb-3">
                <label class="form-label">Observaciones</label>
                <textarea name="observaciones" class="form-control" rows="3">{{ reserva['observaciones'] }}</textarea>
              </div>
              <div class="mb-3">
                <label class="form-label">Nota admin</label>
                <textarea name="nota_admin" class="form-control" rows="3">{{ reserva['nota_admin'] }}</textarea>
              </div>
              <button class="btn btn-primary">Guardar cambios</button>
              <a class="btn btn-outline-secondary" href="{{ url_for('admin_dashboard') }}">Volver</a>
            </form>
          </div>
        </div>
      </div>
    </div>
    """
    return render_page(content, title="Modificar reserva", reserva=reserva)


@app.route("/admin/reserva/<int:reserva_id>/eliminar")
@admin_required
def admin_eliminar_reserva(reserva_id: int):
    db = get_db()
    db.execute("DELETE FROM reservations WHERE id = ?", (reserva_id,))
    db.commit()
    flash("Reserva eliminada.", "warning")
    return redirect(url_for("admin_dashboard"))


@app.route("/admin/users", methods=["GET", "POST"])
@admin_required
def admin_users():
    db = get_db()

    if request.method == "POST":
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "").strip()
        nombre = request.form.get("nombre", "").strip()
        propiedad = request.form.get("propiedad", "").strip()
        rol = request.form.get("rol", "residente").strip()
        al_dia = 1 if request.form.get("al_dia") == "on" else 0
        residente_permanente = 1 if request.form.get("residente_permanente") == "on" else 0

        if not all([username, password, nombre, propiedad]):
            flash("Todos los campos principales son obligatorios.", "danger")
        else:
            try:
                db.execute(
                    """
                    INSERT INTO users (username, password, nombre, propiedad, rol, activo, al_dia, residente_permanente)
                    VALUES (?, ?, ?, ?, ?, 1, ?, ?)
                    """,
                    (username, password, nombre, propiedad, rol, al_dia, residente_permanente),
                )
                db.commit()
                flash("Usuario creado correctamente.", "success")
                return redirect(url_for("admin_users"))
            except sqlite3.IntegrityError:
                flash("El usuario ya existe.", "danger")

    users = db.execute("SELECT * FROM users ORDER BY rol DESC, propiedad ASC, nombre ASC").fetchall()

    content = """
    <div class="row g-4">
      <div class="col-lg-4">
        <div class="card card-shadow">
          <div class="card-body">
            <h4>Nuevo usuario</h4>
            <form method="post">
              <div class="mb-2"><label class="form-label">Usuario</label><input name="username" class="form-control" required></div>
              <div class="mb-2"><label class="form-label">Contraseña</label><input name="password" class="form-control" required></div>
              <div class="mb-2"><label class="form-label">Nombre</label><input name="nombre" class="form-control" required></div>
              <div class="mb-2"><label class="form-label">Propiedad</label><input name="propiedad" class="form-control" placeholder="Casa 01 / Lote 12" required></div>
              <div class="mb-2">
                <label class="form-label">Rol</label>
                <select name="rol" class="form-select">
                  <option value="residente">Residente</option>
                  <option value="admin">Admin</option>
                </select>
              </div>
              <div class="form-check">
                <input class="form-check-input" type="checkbox" name="al_dia" id="al_dia" checked>
                <label class="form-check-label" for="al_dia">Al día en administración</label>
              </div>
              <div class="form-check mb-3">
                <input class="form-check-input" type="checkbox" name="residente_permanente" id="residente_permanente" checked>
                <label class="form-check-label" for="residente_permanente">Residente permanente</label>
              </div>
              <button class="btn btn-primary">Guardar</button>
              <a class="btn btn-outline-secondary" href="{{ url_for('admin_dashboard') }}">Volver</a>
            </form>
          </div>
        </div>
      </div>

      <div class="col-lg-8">
        <div class="card card-shadow">
          <div class="card-body">
            <h4>Usuarios</h4>
            <div class="table-responsive">
              <table class="table table-striped">
                <thead>
                  <tr><th>Usuario</th><th>Nombre</th><th>Propiedad</th><th>Rol</th><th>Al día</th><th>Permanente</th></tr>
                </thead>
                <tbody>
                  {% for u in users %}
                    <tr>
                      <td>{{ u['username'] }}</td>
                      <td>{{ u['nombre'] }}</td>
                      <td>{{ u['propiedad'] }}</td>
                      <td>{{ u['rol'] }}</td>
                      <td>{{ 'Sí' if u['al_dia'] else 'No' }}</td>
                      <td>{{ 'Sí' if u['residente_permanente'] else 'No' }}</td>
                    </tr>
                  {% else %}
                    <tr><td colspan="6" class="text-center text-muted">No hay usuarios.</td></tr>
                  {% endfor %}
                </tbody>
              </table>
            </div>
          </div>
        </div>
      </div>
    </div>
    """
    return render_page(content, title="Usuarios", users=users)


@app.route("/admin/blocks", methods=["GET", "POST"])
@admin_required
def admin_blocks():
    db = get_db()
    recursos = db.execute("SELECT * FROM resources ORDER BY id").fetchall()

    if request.method == "POST":
        resource_id = int(request.form.get("resource_id"))
        fecha = request.form.get("fecha", "").strip()
        motivo = request.form.get("motivo", "").strip()

        if not fecha:
            flash("La fecha es obligatoria.", "danger")
        else:
            try:
                db.execute(
                    "INSERT INTO blocked_dates (resource_id, fecha, motivo) VALUES (?, ?, ?)",
                    (resource_id, fecha, motivo),
                )
                db.commit()
                flash("Fecha bloqueada correctamente.", "success")
                return redirect(url_for("admin_blocks"))
            except sqlite3.IntegrityError:
                flash("Esa fecha ya se encuentra bloqueada para ese recurso.", "warning")

    blocks = db.execute(
        """
        SELECT b.*, r.nombre AS recurso
        FROM blocked_dates b
        JOIN resources r ON r.id = b.resource_id
        ORDER BY b.fecha DESC
        """
    ).fetchall()

    content = """
    <div class="row g-4">
      <div class="col-lg-4">
        <div class="card card-shadow">
          <div class="card-body">
            <h4>Nueva fecha bloqueada</h4>
            <form method="post">
              <div class="mb-2">
                <label class="form-label">Recurso</label>
                <select name="resource_id" class="form-select">
                  {% for r in recursos %}
                    <option value="{{ r['id'] }}">{{ r['nombre'] }}</option>
                  {% endfor %}
                </select>
              </div>
              <div class="mb-2"><label class="form-label">Fecha</label><input type="date" name="fecha" class="form-control" required></div>
              <div class="mb-3"><label class="form-label">Motivo</label><textarea name="motivo" class="form-control" rows="3"></textarea></div>
              <button class="btn btn-success">Bloquear</button>
              <a class="btn btn-outline-secondary" href="{{ url_for('admin_dashboard') }}">Volver</a>
            </form>
          </div>
        </div>
      </div>

      <div class="col-lg-8">
        <div class="card card-shadow">
          <div class="card-body">
            <h4>Fechas bloqueadas</h4>
            <div class="table-responsive">
              <table class="table table-striped">
                <thead><tr><th>Recurso</th><th>Fecha</th><th>Motivo</th></tr></thead>
                <tbody>
                  {% for b in blocks %}
                    <tr><td>{{ b['recurso'] }}</td><td>{{ b['fecha'] }}</td><td>{{ b['motivo'] or '' }}</td></tr>
                  {% else %}
                    <tr><td colspan="3" class="text-center text-muted">No hay fechas bloqueadas.</td></tr>
                  {% endfor %}
                </tbody>
              </table>
            </div>
          </div>
        </div>
      </div>
    </div>
    """
    return render_page(content, title="Fechas bloqueadas", recursos=recursos, blocks=blocks)


@app.route("/admin/config", methods=["GET", "POST"])
@admin_required
def admin_config():
    db = get_db()
    claves = [
        "dias_anticipacion_salon",
        "hora_inicio_salon",
        "hora_fin_salon",
        "hora_inicio_piscina",
        "hora_fin_piscina",
        "dia_cierre_piscina",
        "max_reservas_salon_mes",
        "max_reservas_piscina_mes",
        "max_dias_adelanto",
        "auto_aprobar_salon",
        "auto_aprobar_piscina",
    ]

    if request.method == "POST":
        for clave in claves:
            valor = request.form.get(clave, "").strip()
            db.execute(
                """
                INSERT INTO config (clave, valor) VALUES (?, ?)
                ON CONFLICT(clave) DO UPDATE SET valor = excluded.valor
                """,
                (clave, valor),
            )
        db.commit()
        flash("Configuración actualizada.", "success")
        return redirect(url_for("admin_config"))

    config = {clave: get_config(clave, "") for clave in claves}

    content = """
    <div class="row justify-content-center">
      <div class="col-lg-8">
        <div class="card card-shadow">
          <div class="card-body">
            <h3>Configuración general</h3>
            <form method="post">
              <div class="row">
                <div class="col-md-4 mb-3"><label class="form-label">Días anticipación salón</label><input name="dias_anticipacion_salon" class="form-control" value="{{ config['dias_anticipacion_salon'] }}"></div>
                <div class="col-md-4 mb-3"><label class="form-label">Hora inicio salón</label><input name="hora_inicio_salon" class="form-control" value="{{ config['hora_inicio_salon'] }}"></div>
                <div class="col-md-4 mb-3"><label class="form-label">Hora fin salón</label><input name="hora_fin_salon" class="form-control" value="{{ config['hora_fin_salon'] }}"></div>

                <div class="col-md-4 mb-3"><label class="form-label">Hora inicio piscina</label><input name="hora_inicio_piscina" class="form-control" value="{{ config['hora_inicio_piscina'] }}"></div>
                <div class="col-md-4 mb-3"><label class="form-label">Hora fin piscina</label><input name="hora_fin_piscina" class="form-control" value="{{ config['hora_fin_piscina'] }}"></div>
                <div class="col-md-4 mb-3"><label class="form-label">Día cierre piscina (lunes=0)</label><input name="dia_cierre_piscina" class="form-control" value="{{ config['dia_cierre_piscina'] }}"></div>

                <div class="col-md-4 mb-3"><label class="form-label">Máx. reservas salón / mes</label><input name="max_reservas_salon_mes" class="form-control" value="{{ config['max_reservas_salon_mes'] }}"></div>
                <div class="col-md-4 mb-3"><label class="form-label">Máx. reservas piscina / mes</label><input name="max_reservas_piscina_mes" class="form-control" value="{{ config['max_reservas_piscina_mes'] }}"></div>
                <div class="col-md-4 mb-3"><label class="form-label">Máx. días hacia adelante</label><input name="max_dias_adelanto" class="form-control" value="{{ config['max_dias_adelanto'] }}"></div>

                <div class="col-md-6 mb-3"><label class="form-label">Auto aprobar salón (1 sí / 0 no)</label><input name="auto_aprobar_salon" class="form-control" value="{{ config['auto_aprobar_salon'] }}"></div>
                <div class="col-md-6 mb-3"><label class="form-label">Auto aprobar piscina (1 sí / 0 no)</label><input name="auto_aprobar_piscina" class="form-control" value="{{ config['auto_aprobar_piscina'] }}"></div>
              </div>

              <button class="btn btn-dark">Guardar cambios</button>
              <a class="btn btn-outline-secondary" href="{{ url_for('admin_dashboard') }}">Volver</a>
            </form>
          </div>
        </div>
      </div>
    </div>
    """
    return render_page(content, title="Configuración", config=config)


if __name__ == "__main__":
    init_db()
    app.run(debug=True)
