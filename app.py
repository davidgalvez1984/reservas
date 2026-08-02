"""
App V4.3 - Reservas Parcelación Caña Brava
----------------------------------------
Novedades:
- Reserva individual o combinada (salón + piscina)
- Admin: aprobar, eliminar, modificar y solicitar ajuste al usuario
- Usuario: puede editar su reserva cuando está pendiente o requiere_ajuste
- Calendario mensual para usuario y admin
  * Usuario: solo ve "Reservado" y horarios
  * Admin: ve además la propiedad
- Priorización automática: ordinarias sobre solicitudes excepcionales
- Solicitudes por poca anticipación y por límite mensual no bloquean disponibilidad
- Selector mensual de permisos
- Inicio de sesión con fotografía institucional y crédito de desarrollo

Cómo ejecutar:
    pip install flask
    python app_reservas_cana_brava_v2.py

Usuarios iniciales:
    admin / admin123
    casa01 / demo123
"""

from __future__ import annotations

import calendar as pycalendar
from contextlib import closing
from datetime import date, datetime, timedelta
from functools import wraps
import os
from pathlib import Path
from typing import Optional
from zoneinfo import ZoneInfo

import psycopg
from psycopg.rows import dict_row
from werkzeug.security import check_password_hash, generate_password_hash

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
DATABASE_URL = os.environ.get("DATABASE_URL")

if not DATABASE_URL:
    raise RuntimeError("No se encontró la variable de entorno DATABASE_URL.")

app = Flask(__name__)
app.config["SECRET_KEY"] = os.environ.get("SECRET_KEY", "cambiar-esta-clave-en-produccion-v3")
COLOMBIA_TZ = ZoneInfo("America/Bogota")

def now_colombia() -> datetime:
    return datetime.now(COLOMBIA_TZ)

# =========================
# Base de datos (PostgreSQL)
# =========================
class PgCursorCompat:
    def __init__(self, cursor):
        self.cursor = cursor

    def fetchone(self):
        return self.cursor.fetchone()

    def fetchall(self):
        return self.cursor.fetchall()

    def __iter__(self):
        return iter(self.cursor)


class PgConnCompat:
    """Adaptador para conservar el estilo db.execute(sql, params) de SQLite."""
    def __init__(self, conn):
        self.conn = conn

    def execute(self, query, params=None):
        query = query.replace("?", "%s")
        cur = self.conn.cursor()
        cur.execute(query, params or ())
        return PgCursorCompat(cur)

    def executemany(self, query, seq_of_params):
        query = query.replace("?", "%s")
        cur = self.conn.cursor()
        cur.executemany(query, seq_of_params)
        return PgCursorCompat(cur)

    def commit(self):
        self.conn.commit()

    def rollback(self):
        self.conn.rollback()

    def close(self):
        self.conn.close()


def get_raw_pg_connection():
    return psycopg.connect(DATABASE_URL, row_factory=dict_row)


def get_db():
    if "db" not in g:
        raw_conn = get_raw_pg_connection()
        g.db = PgConnCompat(raw_conn)
    return g.db


@app.teardown_appcontext
def close_db(exception):
    db = g.pop("db", None)
    if db is not None:
        db.close()


def init_db() -> None:
    with psycopg.connect(DATABASE_URL) as db:
        with db.cursor() as cur:
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS users (
                    id SERIAL PRIMARY KEY,
                    username TEXT UNIQUE NOT NULL,
                    password TEXT NOT NULL,
                    documento TEXT,
                    nombre TEXT NOT NULL,
                    propiedad TEXT NOT NULL,
                    rol TEXT NOT NULL CHECK(rol IN ('admin', 'residente')),
                    activo INTEGER NOT NULL DEFAULT 1,
                    al_dia INTEGER NOT NULL DEFAULT 1,
                    residente_permanente INTEGER NOT NULL DEFAULT 1
                );
                """
            )

            cur.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS documento TEXT")
            cur.execute(
                """
                UPDATE users
                SET documento = password
                WHERE (documento IS NULL OR documento = '')
                  AND password NOT LIKE 'pbkdf2:%'
                  AND password NOT LIKE 'scrypt:%'
                """
            )

            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS resources (
                    id SERIAL PRIMARY KEY,
                    codigo TEXT UNIQUE NOT NULL,
                    nombre TEXT NOT NULL,
                    tipo_exclusividad TEXT NOT NULL CHECK(tipo_exclusividad IN ('exclusivo', 'compartido')),
                    capacidad_maxima INTEGER NOT NULL
                );
                """
            )

            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS config (
                    clave TEXT PRIMARY KEY,
                    valor TEXT NOT NULL
                );
                """
            )

            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS blocked_dates (
                    id SERIAL PRIMARY KEY,
                    resource_id INTEGER NOT NULL REFERENCES resources(id),
                    fecha TEXT NOT NULL,
                    motivo TEXT,
                    UNIQUE(resource_id, fecha)
                );
                """
            )

            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS reservations (
                    id SERIAL PRIMARY KEY,
                    user_id INTEGER NOT NULL REFERENCES users(id),
                    resource_id INTEGER NOT NULL REFERENCES resources(id),
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
                    solicitud_extra INTEGER NOT NULL DEFAULT 0,
                    poca_anticipacion INTEGER NOT NULL DEFAULT 0
                );
                """
            )

            cur.execute(
                "ALTER TABLE reservations ADD COLUMN IF NOT EXISTS solicitud_extra INTEGER NOT NULL DEFAULT 0"
            )
            cur.execute(
                "ALTER TABLE reservations ADD COLUMN IF NOT EXISTS poca_anticipacion INTEGER NOT NULL DEFAULT 0"
            )

            cur.execute("SELECT COUNT(*) FROM resources")
            if cur.fetchone()[0] == 0:
                cur.executemany(
                    "INSERT INTO resources (codigo, nombre, tipo_exclusividad, capacidad_maxima) VALUES (%s, %s, %s, %s)",
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
                "dia_cierre_piscina": "1",
                "max_reservas_salon_mes": "2",
                "max_reservas_piscina_mes": "8",
                "max_dias_adelanto": "60",
                "auto_aprobar_salon": "0",
                "auto_aprobar_piscina": "1",
            }
            for clave, valor in config_default.items():
                cur.execute(
                    "INSERT INTO config (clave, valor) VALUES (%s, %s) ON CONFLICT (clave) DO NOTHING",
                    (clave, valor),
                )

            cur.execute("SELECT COUNT(*) FROM users")
            if cur.fetchone()[0] == 0:
                cur.executemany(
                    """
                    INSERT INTO users (username, password, documento, nombre, propiedad, rol, activo, al_dia, residente_permanente)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                    """,
                    [
                        ("admin", "admin123", "admin", "Administrador", "Administración", "admin", 1, 1, 1),
                        ("casa01", "demo123", "demo", "Residente Demo", "Casa 01", "residente", 1, 1, 1),
                    ],
                )

        db.commit()


init_db()



def verify_password(stored_password: str, submitted_password: str) -> bool:
    """
    Compatibilidad gradual:
    - Contraseñas antiguas importadas: texto plano.
    - Contraseñas cambiadas o restablecidas: hash seguro.
    """
    if not stored_password:
        return False
    if stored_password.startswith(("pbkdf2:", "scrypt:")):
        return check_password_hash(stored_password, submitted_password)
    return stored_password == submitted_password


def hash_password(password: str) -> str:
    return generate_password_hash(password)


def display_property(value: str) -> str:
    """Muestra 'Lote N' cuando el valor contiene únicamente números."""
    text_value = str(value or "").strip()
    return f"Lote {text_value}" if text_value.isdigit() else text_value


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
          AND estado = 'aprobada'
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
          AND r.estado = 'aprobada'
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
          AND estado = 'aprobada'
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


def create_reservation_record(
    user_id: int,
    resource_id: int,
    fecha: str,
    hora_inicio: str,
    hora_fin: str,
    asistentes: int,
    invitados: str,
    estado: str,
    observaciones: str,
    solicitud_extra: int = 0,
    poca_anticipacion: int = 0,
) -> None:
    db = get_db()
    db.execute(
        """
        INSERT INTO reservations
        (user_id, resource_id, fecha, hora_inicio, hora_fin, asistentes,
         invitados_registrados, estado, observaciones, motivo_rechazo, nota_admin,
         created_at, updated_at, solicitud_extra, poca_anticipacion)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, '', '', ?, ?, ?, ?)
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
            solicitud_extra,
            poca_anticipacion,
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


def monthly_permission_summary(user_id: int, fecha: Optional[date] = None):
    fecha = fecha or now_colombia().date()
    salon = resource_by_codigo("SALON")
    piscina = resource_by_codigo("PISCINA")

    salon_limite = int(get_config("max_reservas_salon_mes", "2"))
    piscina_limite = int(get_config("max_reservas_piscina_mes", "8"))

    salon_usadas = count_user_reservations_month(user_id, salon["id"], fecha) if salon else 0
    piscina_usadas = count_user_reservations_month(user_id, piscina["id"], fecha) if piscina else 0

    return {
        "salon": {
            "usadas": salon_usadas,
            "limite": salon_limite,
            "disponibles": max(0, salon_limite - salon_usadas),
        },
        "piscina": {
            "usadas": piscina_usadas,
            "limite": piscina_limite,
            "disponibles": max(0, piscina_limite - piscina_usadas),
        },
        "mes": ["", "Enero", "Febrero", "Marzo", "Abril", "Mayo", "Junio", "Julio", "Agosto", "Septiembre", "Octubre", "Noviembre", "Diciembre"][fecha.month],
        "year": fecha.year,
    }


def reservation_status_badge(estado: str) -> str:
    mapping = {
        "aprobada": "success",
        "pendiente": "warning",
        "rechazada": "danger",
        "requiere_ajuste": "info",
        "cancelada": "secondary",
    }
    return mapping.get(estado, "secondary")


def validate_reservation_rules(
    user_row,
    resource_row,
    fecha_str: str,
    hora_inicio_str: str,
    hora_fin_str: str,
    asistentes: int,
    exclude_id: Optional[int] = None,
    permitir_solicitud_extra: bool = False,
):
    try:
        fecha = parse_fecha(fecha_str)
        datetime.strptime(hora_inicio_str, "%H:%M")
        datetime.strptime(hora_fin_str, "%H:%M")
    except ValueError:
        return False, "La fecha u hora tiene un formato inválido."

    ahora = now_colombia()
    hoy = ahora.date()
    if fecha < hoy:
        return False, "No se permiten reservas en fechas pasadas."

    max_dias_adelanto = int(get_config("max_dias_adelanto", "60"))
    if fecha > hoy + timedelta(days=max_dias_adelanto):
        return False, f"Solo se permiten reservas hasta {max_dias_adelanto} días hacia adelante."

    if hora_inicio_str >= hora_fin_str:
        return False, "La hora final debe ser posterior a la hora inicial."

    if fecha == hoy:
        inicio_solicitado = datetime.combine(fecha, datetime.strptime(hora_inicio_str, "%H:%M").time(), tzinfo=COLOMBIA_TZ)
        if inicio_solicitado <= ahora:
            return False, "El horario solicitado para hoy ya inició. Seleccione un horario posterior."

    if user_row["rol"] != "admin" and int(user_row["al_dia"]) != 1:
        return False, "Solo pueden reservar residentes o propietarios al día en administración."

    bloqueo = is_blocked(resource_row["id"], fecha_str)
    if bloqueo:
        return False, f"La fecha está bloqueada: {bloqueo}"

    if resource_row["codigo"] == "SALON":
        if hora_inicio_str < get_config("hora_inicio_salon", "09:00") or hora_fin_str > get_config("hora_fin_salon", "21:00"):
            return False, "El salón solo puede reservarse entre 09:00 y 21:00."

        if asistentes > int(resource_row["capacidad_maxima"]):
            return False, "La capacidad máxima del salón social es 40 personas."

        if has_conflict_exclusive(resource_row["id"], fecha_str, hora_inicio_str, hora_fin_str, exclude_id):
            return False, "El salón social ya se encuentra reservado en ese rango horario."

        limite_mes = int(get_config("max_reservas_salon_mes", "2"))
        usadas = count_user_reservations_month(user_row["id"], resource_row["id"], fecha, exclude_id)
        if usadas >= limite_mes and user_row["rol"] != "admin" and not permitir_solicitud_extra:
            return False, (
                f"Ya alcanzó el límite mensual de {limite_mes} reservas para el salón. "
                "Puede marcar la opción de solicitud adicional para que la administración evalúe una excepción."
            )

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
        if usadas >= limite_mes and user_row["rol"] != "admin" and not permitir_solicitud_extra:
            return False, (
                f"Ya alcanzó el límite mensual de {limite_mes} reservas para la piscina. "
                "Puede marcar la opción de solicitud adicional para que la administración evalúe una excepción."
            )

    return True, "Validación superada."



def get_calendar_month_data(
    year: int,
    month: int,
    is_admin: bool,
    viewer_user_id: Optional[int] = None,
):
    db = get_db()
    cal = pycalendar.Calendar(firstweekday=0)
    days = list(cal.itermonthdates(year, month))
    inicio = min(days)
    fin = max(days)

    rows = db.execute(
        """
        SELECT r.*, rs.nombre AS recurso, rs.codigo AS recurso_codigo,
               u.propiedad, u.nombre, u.username
        FROM reservations r
        JOIN resources rs ON rs.id = r.resource_id
        JOIN users u ON u.id = r.user_id
        WHERE r.fecha >= ? AND r.fecha <= ?
          AND r.estado IN ('pendiente', 'aprobada', 'requiere_ajuste', 'cancelada')
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
        is_own = viewer_user_id is not None and int(r["user_id"]) == int(viewer_user_id)
        if not is_admin and not is_own and r["estado"] != "aprobada":
            continue
        if is_admin:
            texto = (
                f'{r["recurso"]} · {r["hora_inicio"]}-{r["hora_fin"]} · '
                f'{r["nombre"]} ({display_property(r["propiedad"])})'
            )
        elif is_own:
            texto = f'{r["recurso"]} · {r["hora_inicio"]}-{r["hora_fin"]} · {r["estado"].replace("_", " ").title()}'
        else:
            texto = f'{r["recurso"]} · {r["hora_inicio"]}-{r["hora_fin"]} · Reservado'

        item = {
            "id": r["id"],
            "texto": texto,
            "estado": r["estado"],
            "is_own": is_own,
            "recurso": r["recurso"],
            "fecha": r["fecha"],
            "hora_inicio": r["hora_inicio"],
            "hora_fin": r["hora_fin"],
            "asistentes": r["asistentes"],
            "nombre": r["nombre"] if is_admin else "",
            "propiedad": display_property(r["propiedad"]) if is_admin else "",
            "username": r["username"] if is_admin else "",
            "nota_admin": r["nota_admin"] or "",
            "observaciones": r["observaciones"] or "",
            "solicitud_extra": int(r["solicitud_extra"] or 0),
            "poca_anticipacion": int(r["poca_anticipacion"] or 0),
        }
        reservations_by_day.setdefault(r["fecha"], []).append(item)

    blocked_by_day = {}
    for b in bloqueos:
        blocked_by_day.setdefault(b["fecha"], []).append(
            f'{b["recurso"]}: {b["motivo"] or "Bloqueado"}'
        )

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
  <meta name="theme-color" content="#173f35">
  <link href="https://cdn.jsdelivr.net/npm/bootstrap@5.3.3/dist/css/bootstrap.min.css" rel="stylesheet">
  <link href="https://cdn.jsdelivr.net/npm/bootstrap-icons@1.11.3/font/bootstrap-icons.min.css" rel="stylesheet">
  <style>
    :root {
      --cb-green: #173f35;
      --cb-green-2: #245d4e;
      --cb-green-soft: #eaf2ef;
      --cb-gold: #c6a15b;
      --cb-ink: #24332e;
      --cb-muted: #71807a;
      --cb-bg: #f3f6f5;
      --cb-border: #dfe7e4;
    }

    * { box-sizing: border-box; }
    body {
      background: var(--cb-bg);
      color: var(--cb-ink);
      font-family: Inter, ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
    }
    .container-main { max-width: 1200px; }

    .navbar-cb {
      background: linear-gradient(110deg, #12352d 0%, #1c4d41 60%, #245d4e 100%);
      box-shadow: 0 8px 28px rgba(19, 53, 45, .15);
    }
    .navbar-brand { font-weight: 750; letter-spacing: -.02em; }
    .brand-mark {
      width: 36px; height: 36px; border-radius: 12px; display: inline-grid; place-items: center;
      background: rgba(255,255,255,.14); border: 1px solid rgba(255,255,255,.18);
      margin-right: .55rem;
    }
    .navbar-cb .btn-outline-light { border-color: rgba(255,255,255,.3); }
    .navbar-cb .btn-outline-light:hover { color: var(--cb-green); background: #fff; }
    .nav-user { font-size: .86rem; color: rgba(255,255,255,.78); white-space: nowrap; }

    .card {
      border: 1px solid var(--cb-border);
      border-radius: 18px;
    }
    .card-shadow { box-shadow: 0 14px 34px rgba(23, 63, 53, .08); border: 1px solid rgba(223,231,228,.9); }
    .table td, .table th { vertical-align: middle; }
    .small-muted { font-size: .92rem; color: var(--cb-muted); }
    .btn { border-radius: 10px; font-weight: 600; }
    .btn-dark { background: var(--cb-green); border-color: var(--cb-green); }
    .btn-dark:hover, .btn-dark:focus { background: #0f3028; border-color: #0f3028; }
    .btn-primary { background: var(--cb-green-2); border-color: var(--cb-green-2); }
    .btn-primary:hover { background: var(--cb-green); border-color: var(--cb-green); }
    .form-control, .form-select {
      border-radius: 11px; border-color: #d5dfdb; min-height: 44px;
    }
    .form-control:focus, .form-select:focus {
      border-color: #6a9d8e; box-shadow: 0 0 0 .2rem rgba(36,93,78,.12);
    }
    .alert { border: 0; border-radius: 14px; box-shadow: 0 8px 20px rgba(0,0,0,.05); }

    .calendar-table { border-radius: 16px; overflow: hidden; }
    .calendar-table td { width: 14.28%; min-height: 155px; height: 155px; vertical-align: top; background: #fff; }
    .day-box { font-size: .82rem; }
    .day-num { font-weight: 700; margin-bottom: .25rem; color: var(--cb-ink); }
    .muted-day { background: #f1f3f5 !important; color: #adb5bd; }
    .event-pill { font-size: .74rem; padding: .32rem .48rem; border-radius: .55rem; display: block; margin-bottom: .28rem; border: 0; width: 100%; text-align: left; cursor: pointer; transition: .15s ease; }
    .event-pill:hover { filter: brightness(.97); transform: translateY(-1px); }
    .event-pendiente { background: #fff3cd; color: #664d03; }
    .event-aprobada { background: #d1e7dd; color: #0f5132; }
    .event-requiere_ajuste { background: #cff4fc; color: #055160; }
    .event-cancelada { background: #e2e3e5; color: #41464b; }
    .event-reservado { background: #e9ecef; color: #343a40; }
    .event-block { background: #ffe3e3; color: #842029; }
    .permission-card .display-6 { font-weight: 750; color: var(--cb-green); }
    .calendar-legend span { display: inline-flex; align-items: center; gap: .3rem; margin-right: .8rem; font-size: .85rem; }
    .legend-dot { width: .8rem; height: .8rem; border-radius: 50%; display: inline-block; }
    .top-actions a { text-decoration: none; }

    /* Inicio de sesión */
    .login-page {
      min-height: 100vh;
      background:
        radial-gradient(circle at 10% 15%, rgba(198,161,91,.16), transparent 26%),
        radial-gradient(circle at 86% 80%, rgba(71,135,116,.22), transparent 28%),
        linear-gradient(135deg, #0f3028 0%, #173f35 48%, #245d4e 100%);
    }
    .login-shell { min-height: 100vh; display: grid; place-items: center; padding: 32px 16px; }
    .login-card {
      width: 100%; max-width: 980px; overflow: hidden; border: 1px solid rgba(255,255,255,.18);
      border-radius: 26px; box-shadow: 0 30px 80px rgba(7, 27, 22, .28); background: rgba(255,255,255,.98);
    }
    .login-brand-panel {
      min-height: 570px; padding: 54px 48px; color: white; position: relative;
      background:
        linear-gradient(165deg, rgba(13,48,39,.96), rgba(31,84,70,.95)),
        radial-gradient(circle at top right, rgba(198,161,91,.25), transparent 35%);
      display: flex; flex-direction: column; justify-content: space-between;
    }
    .login-brand-panel::after {
      content: ""; position: absolute; width: 260px; height: 260px; border: 1px solid rgba(255,255,255,.09);
      border-radius: 50%; right: -100px; bottom: -95px;
    }
    .login-photo {
      width: 100%; height: 150px; border-radius: 18px; overflow: hidden;
      border: 1px solid rgba(255,255,255,.18); box-shadow: 0 14px 28px rgba(4,24,19,.20);
      background: rgba(255,255,255,.08); margin-bottom: 24px; position: relative;
    }
    .login-photo img { width: 100%; height: 100%; object-fit: cover; object-position: center 58%; display: block; }
    .login-photo::after {
      content: ""; position: absolute; inset: 0;
      background: linear-gradient(180deg, rgba(8,34,28,.03), rgba(8,34,28,.18));
      pointer-events: none;
    }
    .login-credit { color: rgba(255,255,255,.58); font-size: .76rem; line-height: 1.55; }
    .login-credit strong { color: rgba(255,255,255,.84); font-weight: 650; }
    .login-brand-panel h1 { font-size: clamp(2rem, 3vw, 3.15rem); font-weight: 760; letter-spacing: -.04em; line-height: 1.03; }
    .login-brand-panel p { color: rgba(255,255,255,.77); font-size: 1.02rem; max-width: 430px; }
    .login-feature { display: flex; align-items: center; gap: .7rem; color: rgba(255,255,255,.83); margin-top: .65rem; }
    .login-feature i { color: #e1c17f; }
    .login-form-panel { padding: 54px 48px; display: flex; flex-direction: column; justify-content: center; background: #fff; }
    .login-form-panel h2 { font-weight: 760; letter-spacing: -.03em; }
    .login-eyebrow { text-transform: uppercase; letter-spacing: .12em; font-size: .74rem; font-weight: 750; color: var(--cb-green-2); }
    .login-input-wrap { position: relative; }
    .login-input-wrap > i:first-child { position: absolute; left: 14px; top: 50%; transform: translateY(-50%); color: #84938e; z-index: 2; }
    .login-input-wrap .form-control { padding-left: 42px; min-height: 50px; background: #fbfcfc; }
    .password-toggle { position: absolute; right: 10px; top: 50%; transform: translateY(-50%); border: 0; background: transparent; color: #73817c; padding: .35rem .5rem; }
    .login-submit { min-height: 50px; border-radius: 12px; font-weight: 700; box-shadow: 0 10px 20px rgba(23,63,53,.18); }
    .login-footer { color: #8a9893; font-size: .78rem; margin-top: 1.2rem; text-align: center; }

    @media (max-width: 991.98px) {
      .nav-user { display: none; }
      .login-brand-panel { min-height: auto; padding: 34px 30px; }
      .login-form-panel { padding: 38px 30px; }
    }
    @media (max-width: 767.98px) {
      .login-card { border-radius: 20px; }
      .login-brand-panel { display: none; }
      .login-form-panel { min-height: 560px; padding: 38px 26px; }
      .calendar-table td { min-height: 120px; height: 120px; }
    }
  </style>
</head>
<body class="{{ body_class or '' }}">
{% if show_nav is not defined or show_nav %}
<nav class="navbar navbar-expand-lg navbar-dark navbar-cb mb-4">
  <div class="container container-main py-1">
    <a class="navbar-brand d-flex align-items-center" href="{{ url_for('index') }}">
      <span class="brand-mark"><i class="bi bi-tree-fill"></i></span>
      <span>Reservas Caña Brava</span>
    </a>
    <div class="d-flex flex-wrap gap-2 align-items-center top-actions justify-content-end">
      {% if session.get('user_id') %}
        {% if session.get('rol') == 'admin' %}
          <a class="btn btn-outline-light btn-sm" href="{{ url_for('admin_dashboard') }}"><i class="bi bi-grid me-1"></i>Panel</a>
          <a class="btn btn-outline-light btn-sm" href="{{ url_for('admin_calendar') }}"><i class="bi bi-calendar3 me-1"></i>Calendario</a>
        {% else %}
          <a class="btn btn-outline-light btn-sm" href="{{ url_for('mis_reservas') }}"><i class="bi bi-bookmark-check me-1"></i>Mis reservas</a>
          <a class="btn btn-outline-light btn-sm" href="{{ url_for('user_calendar') }}"><i class="bi bi-calendar3 me-1"></i>Calendario</a>
        {% endif %}
        <a class="btn btn-outline-light btn-sm" href="{{ url_for('mi_cuenta') }}"><i class="bi bi-person me-1"></i>Mi cuenta</a>
        <span class="nav-user"><i class="bi bi-person-circle me-1"></i>{{ session.get('nombre') }}</span>
        <a class="btn btn-light btn-sm" style="color:#173f35" href="{{ url_for('logout') }}"><i class="bi bi-box-arrow-right me-1"></i>Salir</a>
      {% endif %}
    </div>
  </div>
</nav>
{% endif %}

{% if show_nav is not defined or show_nav %}
<div class="container container-main pb-5">
{% else %}
<div class="container-fluid p-0">
{% endif %}
  {% with messages = get_flashed_messages(with_categories=true) %}
    {% if messages %}
      <div class="{{ 'container position-fixed top-0 start-50 translate-middle-x pt-3' if show_nav == false else '' }}" style="z-index:1080; max-width:720px;">
      {% for category, message in messages %}
        <div class="alert alert-{{ category if category in ['success','danger','warning','info','primary','secondary'] else 'info' }} alert-dismissible fade show" role="alert">
          {{ message }}
          <button type="button" class="btn-close" data-bs-dismiss="alert"></button>
        </div>
      {% endfor %}
      </div>
    {% endif %}
  {% endwith %}

  {{ content|safe }}
</div>
<script src="https://cdn.jsdelivr.net/npm/bootstrap@5.3.3/dist/js/bootstrap.bundle.min.js"></script>
<script>
  document.querySelectorAll('[data-password-toggle]').forEach(function(btn) {
    btn.addEventListener('click', function() {
      const target = document.getElementById(btn.dataset.passwordToggle);
      if (!target) return;
      const showing = target.type === 'text';
      target.type = showing ? 'password' : 'text';
      btn.innerHTML = showing ? '<i class="bi bi-eye"></i>' : '<i class="bi bi-eye-slash"></i>';
      btn.setAttribute('aria-label', showing ? 'Mostrar contraseña' : 'Ocultar contraseña');
    });
  });
</script>
</body>
</html>
"""


def render_page(content: str, **context):
    context.setdefault("display_property", display_property)
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
            "SELECT * FROM users WHERE username = ? AND activo = 1",
            (username,),
        ).fetchone()

        if user and verify_password(user["password"], password):
            session.clear()
            session["user_id"] = user["id"]
            session["rol"] = user["rol"]
            session["nombre"] = user["nombre"]
            flash("Sesión iniciada correctamente.", "success")
            return redirect(url_for("index"))
        flash("Credenciales inválidas.", "danger")

    content = """
    <div class="login-shell">
      <div class="login-card">
        <div class="row g-0">
          <div class="col-lg-6 login-brand-panel">
            <div>
              <div class="login-photo">
                <img src="{{ url_for('static', filename='Foto_zonacomun.jpeg') }}" alt="Zona común de la Parcelación Caña Brava">
              </div>
              <div class="text-uppercase small fw-semibold mb-3" style="letter-spacing:.14em;color:#e1c17f;">Parcelación Caña Brava</div>
              <h1 class="mb-4">Tus espacios comunes, mejor organizados.</h1>
              <p class="mb-4">Consulta disponibilidad, solicita tus reservas y lleva el control de tus espacios desde un solo lugar.</p>
              <div class="login-feature"><i class="bi bi-check-circle-fill"></i><span>Reservas claras y organizadas</span></div>
              <div class="login-feature"><i class="bi bi-calendar-check-fill"></i><span>Disponibilidad y calendario en línea</span></div>
              <div class="login-feature"><i class="bi bi-shield-check"></i><span>Acceso exclusivo para residentes y administración</span></div>
            </div>
            <div class="login-credit">
              <div>Sistema de gestión de reservas</div>
              <div>Desarrollado por <strong>dfgalvez</strong> · 2026</div>
            </div>
          </div>
          <div class="col-lg-6 login-form-panel">
            <div class="login-eyebrow mb-2">Bienvenido</div>
            <h2 class="mb-2">Iniciar sesión</h2>
            <p class="small-muted mb-4">Ingresa con las credenciales asignadas por la administración.</p>
            <form method="post" autocomplete="on">
              <div class="mb-3">
                <label class="form-label fw-semibold" for="username">Usuario</label>
                <div class="login-input-wrap">
                  <i class="bi bi-person"></i>
                  <input id="username" name="username" class="form-control" placeholder="Escribe tu usuario" autocomplete="username" autofocus required>
                </div>
              </div>
              <div class="mb-4">
                <label class="form-label fw-semibold" for="password">Contraseña</label>
                <div class="login-input-wrap">
                  <i class="bi bi-lock"></i>
                  <input id="password" name="password" type="password" class="form-control pe-5" placeholder="Escribe tu contraseña" autocomplete="current-password" required>
                  <button type="button" class="password-toggle" data-password-toggle="password" aria-label="Mostrar contraseña"><i class="bi bi-eye"></i></button>
                </div>
              </div>
              <button class="btn btn-dark login-submit w-100" type="submit">
                Ingresar <i class="bi bi-arrow-right ms-1"></i>
              </button>
            </form>
            <div class="login-footer">Parcelación Caña Brava · Acceso privado<br><span>Desarrollado por dfgalvez · 2026</span></div>
          </div>
        </div>
      </div>
    </div>
    """
    return render_page(content, title="Ingreso | Caña Brava", show_nav=False, body_class="login-page")


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
# Mi cuenta
# =========================
@app.route("/mi-cuenta", methods=["GET", "POST"])
@login_required
def mi_cuenta():
    db = get_db()
    user = current_user()
    if not user:
        session.clear()
        return redirect(url_for("login"))

    if request.method == "POST":
        actual = request.form.get("password_actual", "")
        nueva = request.form.get("password_nueva", "")
        confirmar = request.form.get("password_confirmar", "")

        if not verify_password(user["password"], actual):
            flash("La contraseña actual no es correcta.", "danger")
        elif len(nueva) < 6:
            flash("La nueva contraseña debe tener mínimo 6 caracteres.", "warning")
        elif nueva != confirmar:
            flash("La confirmación no coincide con la nueva contraseña.", "danger")
        elif actual == nueva:
            flash("La nueva contraseña debe ser diferente de la actual.", "warning")
        else:
            db.execute(
                "UPDATE users SET password = ? WHERE id = ?",
                (hash_password(nueva), user["id"]),
            )
            db.commit()
            flash("Contraseña actualizada correctamente.", "success")
            return redirect(url_for("mi_cuenta"))

    content = """
    <div class="row g-4">
      <div class="col-lg-5">
        <div class="card card-shadow">
          <div class="card-body">
            <h3 class="mb-3">Mi cuenta</h3>
            <dl class="row mb-0">
              <dt class="col-sm-4">Nombre</dt><dd class="col-sm-8">{{ user['nombre'] }}</dd>
              <dt class="col-sm-4">Usuario</dt><dd class="col-sm-8">{{ user['username'] }}</dd>
              <dt class="col-sm-4">Propiedad</dt><dd class="col-sm-8">{{ display_property(user['propiedad']) }}</dd>
              <dt class="col-sm-4">Rol</dt><dd class="col-sm-8">{{ 'Administrador' if user['rol'] == 'admin' else 'Residente' }}</dd>
              <dt class="col-sm-4">Estado</dt>
              <dd class="col-sm-8">{{ 'Al día' if user['al_dia'] else 'Pendiente de pago' }}</dd>
            </dl>
          </div>
        </div>
      </div>

      <div class="col-lg-7">
        <div class="card card-shadow">
          <div class="card-body">
            <h3 class="mb-2">Cambiar contraseña</h3>
            <p class="small-muted">El cambio es voluntario. Puede conservar su contraseña actual si así lo prefiere.</p>
            <form method="post">
              <div class="mb-3">
                <label class="form-label">Contraseña actual</label>
                <input type="password" name="password_actual" class="form-control" required autocomplete="current-password">
              </div>
              <div class="mb-3">
                <label class="form-label">Nueva contraseña</label>
                <input type="password" name="password_nueva" class="form-control" required minlength="6" autocomplete="new-password">
                <div class="form-text">Mínimo 6 caracteres.</div>
              </div>
              <div class="mb-3">
                <label class="form-label">Confirmar nueva contraseña</label>
                <input type="password" name="password_confirmar" class="form-control" required minlength="6" autocomplete="new-password">
              </div>
              <button class="btn btn-primary">Guardar nueva contraseña</button>
            </form>
          </div>
        </div>
      </div>
    </div>
    """
    return render_page(content, title="Mi cuenta", user=user)


# =========================
# Calendarios
# =========================
@app.route("/calendario")
@login_required
def user_calendar():
    today = now_colombia().date()
    year = int(request.args.get("year", today.year))
    month = int(request.args.get("month", today.month))
    weeks = get_calendar_month_data(
        year,
        month,
        is_admin=False,
        viewer_user_id=session["user_id"],
    )

    prev_month = month - 1 or 12
    prev_year = year - 1 if month == 1 else year
    next_month = month + 1 if month < 12 else 1
    next_year = year + 1 if month == 12 else year

    content = """
    <div class="d-flex justify-content-between align-items-center mb-3 flex-wrap gap-2">
      <div>
        <h2 class="mb-1">Calendario de disponibilidad</h2>
        <div class="small-muted">
          Sus reservas aparecen con su estado. Las reservas de otros usuarios solo se muestran como “Reservado”.
        </div>
      </div>
      <div class="d-flex gap-2">
        <a class="btn btn-outline-secondary" href="{{ url_for('user_calendar', year=prev_year, month=prev_month) }}">Mes anterior</a>
        <a class="btn btn-outline-secondary" href="{{ url_for('user_calendar', year=next_year, month=next_month) }}">Mes siguiente</a>
      </div>
    </div>

    <div class="calendar-legend mb-3">
      <span><i class="legend-dot bg-warning"></i>Pendiente</span>
      <span><i class="legend-dot bg-success"></i>Aprobada</span>
      <span><i class="legend-dot bg-info"></i>Requiere ajuste</span>
      <span><i class="legend-dot bg-secondary"></i>Reservado por otro usuario</span>
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
                          {% if r.is_own %}
                            <button
                              type="button"
                              class="event-pill event-{{ r.estado }}"
                              onclick='openUserReservation({{ r|tojson }})'>
                              {{ r.texto }}
                            </button>
                          {% else %}
                            <button
                              type="button"
                              class="event-pill event-reservado"
                              onclick='openOccupiedSlot({{ r.recurso|tojson }}, {{ r.fecha|tojson }}, {{ r.hora_inicio|tojson }}, {{ r.hora_fin|tojson }})'>
                              {{ r.texto }}
                            </button>
                          {% endif %}
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

    <div class="modal fade" id="reservationModal" tabindex="-1">
      <div class="modal-dialog modal-dialog-centered">
        <div class="modal-content">
          <div class="modal-header">
            <h5 class="modal-title" id="reservationModalTitle">Reserva</h5>
            <button type="button" class="btn-close" data-bs-dismiss="modal"></button>
          </div>
          <div class="modal-body" id="reservationModalBody"></div>
          <div class="modal-footer">
            <button type="button" class="btn btn-secondary" data-bs-dismiss="modal">Cerrar</button>
          </div>
        </div>
      </div>
    </div>

    <script>
      function escapeHtml(value) {
        return String(value ?? '')
          .replaceAll('&', '&amp;')
          .replaceAll('<', '&lt;')
          .replaceAll('>', '&gt;')
          .replaceAll('"', '&quot;')
          .replaceAll("'", '&#039;');
      }

      function openUserReservation(r) {
        document.getElementById('reservationModalTitle').textContent = 'Mi reserva';
        document.getElementById('reservationModalBody').innerHTML = `
          <dl class="row mb-0">
            <dt class="col-5">Espacio</dt><dd class="col-7">${escapeHtml(r.recurso)}</dd>
            <dt class="col-5">Fecha</dt><dd class="col-7">${escapeHtml(r.fecha)}</dd>
            <dt class="col-5">Horario</dt><dd class="col-7">${escapeHtml(r.hora_inicio)} - ${escapeHtml(r.hora_fin)}</dd>
            <dt class="col-5">Estado</dt><dd class="col-7"><span class="badge text-bg-${statusColor(r.estado)}">${escapeHtml(formatStatus(r.estado))}</span></dd>
            <dt class="col-5">Observación admin</dt><dd class="col-7">${escapeHtml(r.nota_admin || 'Sin observaciones')}</dd>
            <dt class="col-5">Solicitud adicional</dt><dd class="col-7">${r.solicitud_extra ? 'Sí, sujeta a autorización' : 'No'}</dd>
            <dt class="col-5">Poca anticipación</dt><dd class="col-7">${r.poca_anticipacion ? 'Sí, requiere aprobación' : 'No'}</dd>
          </dl>`;
        bootstrap.Modal.getOrCreateInstance(document.getElementById('reservationModal')).show();
      }

      function openOccupiedSlot(recurso, fecha, inicio, fin) {
        document.getElementById('reservationModalTitle').textContent = 'Horario reservado';
        document.getElementById('reservationModalBody').innerHTML = `
          <p class="mb-2"><strong>${escapeHtml(recurso)}</strong></p>
          <p class="mb-1">${escapeHtml(fecha)}</p>
          <p class="mb-0">${escapeHtml(inicio)} - ${escapeHtml(fin)}</p>
          <div class="alert alert-secondary mt-3 mb-0">Este horario está reservado. Por privacidad no se muestran datos del propietario.</div>`;
        bootstrap.Modal.getOrCreateInstance(document.getElementById('reservationModal')).show();
      }

      function formatStatus(status) {
        return String(status).replaceAll('_', ' ').replace(/\b\w/g, c => c.toUpperCase());
      }

      function statusColor(status) {
        return {
          aprobada: 'success',
          pendiente: 'warning',
          requiere_ajuste: 'info',
          cancelada: 'secondary',
          rechazada: 'danger'
        }[status] || 'secondary';
      }
    </script>
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
    today = now_colombia().date()
    year = int(request.args.get("year", today.year))
    month = int(request.args.get("month", today.month))
    weeks = get_calendar_month_data(year, month, is_admin=True)

    prev_month = month - 1 or 12
    prev_year = year - 1 if month == 1 else year
    next_month = month + 1 if month < 12 else 1
    next_year = year + 1 if month == 12 else year
    return_url = url_for("admin_calendar", year=year, month=month)

    content = """
    <div class="d-flex justify-content-between align-items-center mb-3 flex-wrap gap-2">
      <div>
        <h2 class="mb-1">Calendario administrativo</h2>
        <div class="small-muted">Seleccione una reserva para consultar detalles y gestionar su estado sin salir del calendario.</div>
      </div>
      <div class="d-flex gap-2">
        <a class="btn btn-outline-secondary" href="{{ url_for('admin_calendar', year=prev_year, month=prev_month) }}">Mes anterior</a>
        <a class="btn btn-outline-secondary" href="{{ url_for('admin_calendar', year=next_year, month=next_month) }}">Mes siguiente</a>
      </div>
    </div>

    <div class="calendar-legend mb-3">
      <span><i class="legend-dot bg-warning"></i>Pendiente</span>
      <span><i class="legend-dot bg-success"></i>Aprobada</span>
      <span><i class="legend-dot bg-info"></i>Requiere ajuste</span>
      <span><i class="legend-dot bg-secondary"></i>Cancelada</span>
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
                          <button
                            type="button"
                            class="event-pill event-{{ r.estado }}"
                            onclick='openAdminReservation({{ r|tojson }})'>
                            {{ r.texto }}
                          </button>
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

    <div class="modal fade" id="adminReservationModal" tabindex="-1">
      <div class="modal-dialog modal-lg modal-dialog-centered">
        <div class="modal-content">
          <div class="modal-header">
            <h5 class="modal-title">Detalle y gestión de reserva</h5>
            <button type="button" class="btn-close" data-bs-dismiss="modal"></button>
          </div>
          <div class="modal-body">
            <div id="adminReservationDetails"></div>
            <hr>
            <form method="post" id="quickAdjustForm">
              <input type="hidden" name="next" value="{{ return_url }}">
              <label class="form-label"><strong>Solicitar ajuste</strong></label>
              <textarea name="nota_admin" class="form-control" rows="3" required placeholder="Indique claramente el ajuste que debe realizar el usuario."></textarea>
              <button class="btn btn-info mt-2">Enviar solicitud de ajuste</button>
            </form>
          </div>
          <div class="modal-footer justify-content-between">
            <div class="d-flex gap-2 flex-wrap" id="adminActionButtons"></div>
            <button type="button" class="btn btn-secondary" data-bs-dismiss="modal">Cerrar</button>
          </div>
        </div>
      </div>
    </div>

    <script>
      const calendarReturnUrl = {{ return_url|tojson }};

      function escapeHtml(value) {
        return String(value ?? '')
          .replaceAll('&', '&amp;')
          .replaceAll('<', '&lt;')
          .replaceAll('>', '&gt;')
          .replaceAll('"', '&quot;')
          .replaceAll("'", '&#039;');
      }

      function formatStatus(status) {
        return String(status).replaceAll('_', ' ').replace(/\b\w/g, c => c.toUpperCase());
      }

      function statusColor(status) {
        return {
          aprobada: 'success',
          pendiente: 'warning',
          requiere_ajuste: 'info',
          cancelada: 'secondary',
          rechazada: 'danger'
        }[status] || 'secondary';
      }

      function openAdminReservation(r) {
        document.getElementById('adminReservationDetails').innerHTML = `
          <div class="row g-3">
            <div class="col-md-6">
              <dl class="row mb-0">
                <dt class="col-5">Usuario</dt><dd class="col-7">${escapeHtml(r.username)}</dd>
                <dt class="col-5">Propietario</dt><dd class="col-7">${escapeHtml(r.nombre)}</dd>
                <dt class="col-5">Propiedad</dt><dd class="col-7">${escapeHtml(r.propiedad)}</dd>
                <dt class="col-5">Espacio</dt><dd class="col-7">${escapeHtml(r.recurso)}</dd>
              </dl>
            </div>
            <div class="col-md-6">
              <dl class="row mb-0">
                <dt class="col-5">Fecha</dt><dd class="col-7">${escapeHtml(r.fecha)}</dd>
                <dt class="col-5">Horario</dt><dd class="col-7">${escapeHtml(r.hora_inicio)} - ${escapeHtml(r.hora_fin)}</dd>
                <dt class="col-5">Asistentes</dt><dd class="col-7">${escapeHtml(r.asistentes)}</dd>
                <dt class="col-5">Estado</dt><dd class="col-7"><span class="badge text-bg-${statusColor(r.estado)}">${escapeHtml(formatStatus(r.estado))}</span></dd>
                <dt class="col-5">Solicitud adicional</dt><dd class="col-7">${r.solicitud_extra ? '<span class="badge text-bg-primary">Sí</span>' : 'No'}</dd>
                <dt class="col-5">Poca anticipación</dt><dd class="col-7">${r.poca_anticipacion ? '<span class="badge text-bg-warning">Sí</span>' : 'No'}</dd>
              </dl>
            </div>
          </div>
          <div class="mt-3">
            <strong>Observaciones del usuario</strong>
            <div class="border rounded p-2 mt-1">${escapeHtml(r.observaciones || 'Sin observaciones')}</div>
          </div>
          <div class="mt-3">
            <strong>Nota administrativa</strong>
            <div class="border rounded p-2 mt-1">${escapeHtml(r.nota_admin || 'Sin observaciones')}</div>
          </div>`;

        const nextParam = encodeURIComponent(calendarReturnUrl);
        const buttons = [];
        if (['pendiente', 'requiere_ajuste'].includes(r.estado)) {
          buttons.push(`<a class="btn btn-success" href="/admin/reserva/${r.id}/aprobar?next=${nextParam}">Aprobar</a>`);
        }
        if (r.estado !== 'cancelada') {
          buttons.push(`<a class="btn btn-outline-secondary" href="/admin/reserva/${r.id}/cancelar?next=${nextParam}" onclick="return confirm('¿Cancelar esta reserva?')">Cancelar</a>`);
        }
        buttons.push(`<a class="btn btn-outline-danger" href="/admin/reserva/${r.id}/eliminar?next=${nextParam}" onclick="return confirm('¿Eliminar definitivamente esta reserva?')">Eliminar</a>`);
        document.getElementById('adminActionButtons').innerHTML = buttons.join('');

        document.getElementById('quickAdjustForm').action = `/admin/reserva/${r.id}/ajuste-rapido`;
        bootstrap.Modal.getOrCreateInstance(document.getElementById('adminReservationModal')).show();
      }
    </script>
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
        return_url=return_url,
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
    mes_param = request.args.get("mes", "").strip()
    try:
        fecha_consulta = datetime.strptime(mes_param, "%Y-%m").date().replace(day=1) if mes_param else now_colombia().date().replace(day=1)
    except ValueError:
        fecha_consulta = now_colombia().date().replace(day=1)
    permisos = monthly_permission_summary(session["user_id"], fecha_consulta)
    selected_month = fecha_consulta.strftime("%Y-%m")

    content = """
    <div class="d-flex justify-content-between align-items-center mb-3 flex-wrap gap-2">
      <div>
        <h2 class="mb-1">Mis reservas</h2>
        <div class="small-muted">Consulte sus reservas y los permisos disponibles del mes.</div>
      </div>
      <div class="d-flex gap-2 flex-wrap">
        <a class="btn btn-dark" href="{{ url_for('nueva_reserva_combinada') }}">Reservar salón + piscina</a>
        <a class="btn btn-primary" href="{{ url_for('nueva_reserva', codigo='SALON') }}">Reservar salón</a>
        <a class="btn btn-success" href="{{ url_for('nueva_reserva', codigo='PISCINA') }}">Reservar piscina</a>
        <a class="btn btn-outline-secondary" href="{{ url_for('user_calendar') }}">Ver calendario</a>
      </div>
    </div>

    <form method="get" class="card card-shadow mb-3">
      <div class="card-body d-flex align-items-end gap-2 flex-wrap">
        <div>
          <label class="form-label mb-1">Consultar permisos de otro mes</label>
          <input type="month" name="mes" class="form-control" value="{{ selected_month }}">
        </div>
        <button class="btn btn-outline-dark">Consultar</button>
      </div>
    </form>

    <div class="row g-3 mb-4">
      <div class="col-md-6">
        <div class="card card-shadow permission-card h-100">
          <div class="card-body">
            <div class="d-flex justify-content-between align-items-start">
              <div>
                <h5>Salón social</h5>
                <div class="small-muted">{{ permisos.mes }} {{ permisos.year }}</div>
              </div>
              <span class="badge text-bg-primary">Límite {{ permisos.salon.limite }}</span>
            </div>
            <div class="display-6 mt-3">{{ permisos.salon.disponibles }}</div>
            <div>permiso(s) disponible(s)</div>
            <div class="small-muted mt-2">Utilizados: {{ permisos.salon.usadas }}</div>
          </div>
        </div>
      </div>
      <div class="col-md-6">
        <div class="card card-shadow permission-card h-100">
          <div class="card-body">
            <div class="d-flex justify-content-between align-items-start">
              <div>
                <h5>Piscina</h5>
                <div class="small-muted">{{ permisos.mes }} {{ permisos.year }}</div>
              </div>
              <span class="badge text-bg-success">Límite {{ permisos.piscina.limite }}</span>
            </div>
            <div class="display-6 mt-3">{{ permisos.piscina.disponibles }}</div>
            <div>permiso(s) disponible(s)</div>
            <div class="small-muted mt-2">Utilizados: {{ permisos.piscina.usadas }}</div>
          </div>
        </div>
      </div>
    </div>

    <div class="alert alert-info">
      Si ya agotó el límite mensual, puede enviar una <strong>solicitud adicional</strong>.
      La administración podrá aprobarla excepcionalmente si existe disponibilidad y no afecta a otros propietarios.
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
                <th>Tipo</th>
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
                  <td><span class="badge text-bg-{{ reservation_status_badge(r['estado']) }}">{{ r['estado'].replace('_', ' ') }}</span></td>
                  <td>
                    {% if r['poca_anticipacion'] %}
                      <span class="badge text-bg-warning">Poca anticipación</span>
                    {% endif %}
                    {% if r['solicitud_extra'] %}
                      <span class="badge text-bg-primary">Adicional</span>
                    {% endif %}
                    {% if not r['poca_anticipacion'] and not r['solicitud_extra'] %}
                      <span class="badge text-bg-success">Ordinaria</span>
                    {% endif %}
                  </td>
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
                <tr><td colspan="9" class="text-center text-muted">No hay reservas registradas.</td></tr>
              {% endfor %}
            </tbody>
          </table>
        </div>
      </div>
    </div>
    """
    return render_page(
        content,
        title="Mis reservas",
        reservas=reservas,
        permisos=permisos,
        reservation_status_badge=reservation_status_badge,
        selected_month=selected_month,
    )


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
        solicitar_extra = request.form.get("solicitar_extra") == "on"

        ok, mensaje = validate_reservation_rules(
            user,
            recurso,
            fecha,
            hora_inicio,
            hora_fin,
            asistentes,
            permitir_solicitud_extra=solicitar_extra,
        )
        if not ok:
            flash(mensaje, "danger")
        else:
            fecha_obj = parse_fecha(fecha)
            limite_clave = "max_reservas_salon_mes" if recurso["codigo"] == "SALON" else "max_reservas_piscina_mes"
            limite_mes = int(get_config(limite_clave, "2" if recurso["codigo"] == "SALON" else "8"))
            usadas = count_user_reservations_month(user["id"], recurso["id"], fecha_obj)
            es_extra = solicitar_extra and usadas >= limite_mes
            es_poca_anticipacion = user["rol"] != "admin" and fecha_obj < now_colombia().date() + timedelta(days=2)

            # Las reservas ordinarias de ambos espacios se aprueban automáticamente.
            # Las excepcionales quedan pendientes y no bloquean el espacio.
            estado = "pendiente" if (es_extra or es_poca_anticipacion) else "aprobada"
            create_reservation_record(
                user["id"],
                recurso["id"],
                fecha,
                hora_inicio,
                hora_fin,
                asistentes,
                invitados,
                estado,
                observaciones,
                solicitud_extra=1 if es_extra else 0,
                poca_anticipacion=1 if es_poca_anticipacion else 0,
            )
            if estado == "aprobada":
                flash("Reserva aprobada automáticamente.", "success")
            else:
                motivos = []
                if es_poca_anticipacion:
                    motivos.append("poca anticipación")
                if es_extra:
                    motivos.append("solicitud adicional")
                flash("Solicitud registrada como pendiente por " + " y ".join(motivos) + ".", "warning")
            return redirect(url_for("mis_reservas"))

    ayuda = {
        "SALON": [
            "Con 2 días o más se aprueba automáticamente si cumple las reglas; con menos tiempo queda pendiente.",
            "Horario permitido: 09:00 a 21:00.",
            "Uso exclusivo para quien lo reserve.",
            "Capacidad máxima: 40 personas.",
            "No se permite ceder la reserva a terceros no autorizados.",
        ],
        "PISCINA": [
            "Con 2 días o más se aprueba automáticamente si cumple las reglas; con menos tiempo queda pendiente.",
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

              <div class="form-check mb-3">
                <input class="form-check-input" type="checkbox" name="solicitar_extra" id="solicitar_extra">
                <label class="form-check-label" for="solicitar_extra">
                  Solicitar autorización adicional si ya agoté el límite mensual
                </label>
                <div class="form-text">
                  La solicitud quedará pendiente y será evaluada por la administración según la disponibilidad.
                </div>
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
        solicitar_extra = request.form.get("solicitar_extra") == "on"

        if not reservar_salon and not reservar_piscina:
            flash("Debe seleccionar al menos un espacio para reservar.", "danger")
        else:
            errores = []

            if reservar_salon:
                ok_salon, msg_salon = validate_reservation_rules(
                    user, salon, fecha, salon_hora_inicio, salon_hora_fin, salon_asistentes,
                    permitir_solicitud_extra=solicitar_extra
                )
                if not ok_salon:
                    errores.append(f"Salón: {msg_salon}")

            if reservar_piscina:
                ok_piscina, msg_piscina = validate_reservation_rules(
                    user, piscina, fecha, piscina_hora_inicio, piscina_hora_fin, piscina_asistentes,
                    permitir_solicitud_extra=solicitar_extra
                )
                if not ok_piscina:
                    errores.append(f"Piscina: {msg_piscina}")

            if errores:
                for err in errores:
                    flash(err, "danger")
            else:
                fecha_obj = parse_fecha(fecha)
                if reservar_salon:
                    limite_salon = int(get_config("max_reservas_salon_mes", "2"))
                    usadas_salon = count_user_reservations_month(user["id"], salon["id"], fecha_obj)
                    extra_salon = solicitar_extra and usadas_salon >= limite_salon
                    poca_salon = user["rol"] != "admin" and fecha_obj < now_colombia().date() + timedelta(days=2)
                    estado_salon = "pendiente" if (extra_salon or poca_salon) else "aprobada"
                    create_reservation_record(
                        user["id"], salon["id"], fecha, salon_hora_inicio, salon_hora_fin,
                        salon_asistentes, invitados, estado_salon, observaciones,
                        solicitud_extra=1 if extra_salon else 0,
                        poca_anticipacion=1 if poca_salon else 0,
                    )
                if reservar_piscina:
                    limite_piscina = int(get_config("max_reservas_piscina_mes", "8"))
                    usadas_piscina = count_user_reservations_month(user["id"], piscina["id"], fecha_obj)
                    extra_piscina = solicitar_extra and usadas_piscina >= limite_piscina
                    poca_piscina = user["rol"] != "admin" and fecha_obj < now_colombia().date() + timedelta(days=2)
                    estado_piscina = "pendiente" if (extra_piscina or poca_piscina) else "aprobada"
                    create_reservation_record(
                        user["id"], piscina["id"], fecha, piscina_hora_inicio, piscina_hora_fin,
                        piscina_asistentes, invitados, estado_piscina, observaciones,
                        solicitud_extra=1 if extra_piscina else 0,
                        poca_anticipacion=1 if poca_piscina else 0,
                    )

                flash("Se registró la reserva combinada. Los espacios ordinarios fueron aprobados automáticamente; las excepciones quedaron pendientes.", "success")
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

              <div class="form-check mb-3">
                <input class="form-check-input" type="checkbox" name="solicitar_extra" id="solicitar_extra_combinada">
                <label class="form-check-label" for="solicitar_extra_combinada">
                  Solicitar autorización adicional si ya agoté el límite mensual de alguno de los espacios
                </label>
                <div class="form-text">La administración evaluará la disponibilidad antes de aprobar.</div>
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

        permitir_extra = bool(int(reserva["solicitud_extra"] or 0))
        ok, mensaje = validate_reservation_rules(
            user, recurso, fecha, hora_inicio, hora_fin, asistentes,
            exclude_id=reserva_id, permitir_solicitud_extra=permitir_extra
        )
        if not ok:
            flash(mensaje, "danger")
        else:
            fecha_obj = parse_fecha(fecha)
            limite_clave = "max_reservas_salon_mes" if recurso["codigo"] == "SALON" else "max_reservas_piscina_mes"
            limite_mes = int(get_config(limite_clave, "2" if recurso["codigo"] == "SALON" else "8"))
            usadas = count_user_reservations_month(user["id"], recurso["id"], fecha_obj, exclude_id=reserva_id)
            es_extra = permitir_extra and usadas >= limite_mes
            es_poca = user["rol"] != "admin" and fecha_obj < now_colombia().date() + timedelta(days=2)
            nuevo_estado = "pendiente" if (es_extra or es_poca) else "aprobada"
            update_reservation_record(
                reserva_id, fecha, hora_inicio, hora_fin, asistentes, invitados, observaciones,
                estado=nuevo_estado, nota_admin=""
            )
            db.execute(
                "UPDATE reservations SET solicitud_extra = ?, poca_anticipacion = ? WHERE id = ?",
                (1 if es_extra else 0, 1 if es_poca else 0, reserva_id),
            )
            db.commit()
            if nuevo_estado == "aprobada":
                flash("La reserva actualizada cumple las condiciones ordinarias y fue aprobada automáticamente.", "success")
            else:
                flash("Reserva actualizada y enviada nuevamente para aprobación administrativa.", "warning")
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
                <th>Prioridad</th>
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
                  <td>
                    {% if r['poca_anticipacion'] %}<span class="badge text-bg-warning">Poca anticipación</span>{% endif %}
                    {% if r['solicitud_extra'] %}<span class="badge text-bg-primary">Adicional</span>{% endif %}
                    {% if not r['poca_anticipacion'] and not r['solicitud_extra'] %}<span class="badge text-bg-success">Ordinaria</span>{% endif %}
                  </td>
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
                <tr><td colspan="10" class="text-center text-muted">No hay reservas.</td></tr>
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
        usuario = db.execute("SELECT * FROM users WHERE id = ?", (reserva["user_id"],)).fetchone()
        recurso = resource_by_id(reserva["resource_id"])
        ok, mensaje = validate_reservation_rules(
            usuario, recurso, reserva["fecha"], reserva["hora_inicio"], reserva["hora_fin"],
            int(reserva["asistentes"]), exclude_id=reserva_id, permitir_solicitud_extra=True
        )
        if not ok:
            flash("No se puede aprobar: " + mensaje, "danger")
            destino = request.args.get("next") or request.referrer or url_for("admin_dashboard")
            return redirect(destino)
        db.execute(
            "UPDATE reservations SET estado = 'aprobada', motivo_rechazo = '', nota_admin = '', updated_at = ? WHERE id = ?",
            (now_colombia().isoformat(timespec="seconds"), reserva_id),
        )
        db.commit()
        flash("Reserva aprobada. Desde este momento el horario queda ocupado.", "success")
    else:
        abort(400)
    destino = request.args.get("next") or request.referrer or url_for("admin_dashboard")
    return redirect(destino)


@app.route("/admin/reserva/<int:reserva_id>/cancelar")
@admin_required
def admin_cancelar_reserva(reserva_id: int):
    db = get_db()
    reserva = db.execute("SELECT id FROM reservations WHERE id = ?", (reserva_id,)).fetchone()
    if not reserva:
        abort(404)
    db.execute(
        "UPDATE reservations SET estado = 'cancelada', updated_at = ? WHERE id = ?",
        (datetime.now().isoformat(timespec="seconds"), reserva_id),
    )
    db.commit()
    flash("Reserva cancelada.", "warning")
    destino = request.args.get("next") or request.referrer or url_for("admin_dashboard")
    return redirect(destino)


@app.route("/admin/reserva/<int:reserva_id>/ajuste-rapido", methods=["POST"])
@admin_required
def admin_ajuste_rapido(reserva_id: int):
    db = get_db()
    nota = request.form.get("nota_admin", "").strip()
    destino = request.form.get("next") or url_for("admin_calendar")
    if not nota:
        flash("Debe indicar el ajuste solicitado.", "danger")
        return redirect(destino)
    reserva = db.execute("SELECT id FROM reservations WHERE id = ?", (reserva_id,)).fetchone()
    if not reserva:
        abort(404)
    db.execute(
        "UPDATE reservations SET estado = 'requiere_ajuste', nota_admin = ?, updated_at = ? WHERE id = ?",
        (nota, datetime.now().isoformat(timespec="seconds"), reserva_id),
    )
    db.commit()
    flash("Se solicitó el ajuste al usuario.", "info")
    return redirect(destino)


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
    destino = request.args.get("next") or request.referrer or url_for("admin_dashboard")
    return redirect(destino)


@app.route("/admin/users", methods=["GET", "POST"])
@admin_required
def admin_users():
    db = get_db()

    if request.method == "POST":
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "").strip()
        documento = request.form.get("documento", "").strip()
        nombre = request.form.get("nombre", "").strip()
        propiedad = request.form.get("propiedad", "").strip()
        rol = request.form.get("rol", "residente").strip()
        al_dia = 1 if request.form.get("al_dia") == "on" else 0
        residente_permanente = 1 if request.form.get("residente_permanente") == "on" else 0

        if not all([username, password, documento, nombre, propiedad]):
            flash("Usuario, documento, contraseña, nombre y propiedad son obligatorios.", "danger")
        elif rol not in ("admin", "residente"):
            flash("El rol seleccionado no es válido.", "danger")
        else:
            try:
                db.execute(
                    """
                    INSERT INTO users
                    (username, password, documento, nombre, propiedad, rol, activo, al_dia, residente_permanente)
                    VALUES (?, ?, ?, ?, ?, ?, 1, ?, ?)
                    """,
                    (username, hash_password(password), documento, nombre, propiedad, rol, al_dia, residente_permanente),
                )
                db.commit()
                flash("Usuario creado correctamente.", "success")
                return redirect(url_for("admin_users"))
            except psycopg.IntegrityError:
                db.rollback()
                flash("No fue posible crear el usuario. Verifique que el nombre de usuario no esté repetido.", "danger")

    q = request.args.get("q", "").strip()
    estado = request.args.get("estado", "").strip()

    query = "SELECT * FROM users WHERE 1=1"
    params = []
    if q:
        query += " AND (LOWER(username) LIKE LOWER(?) OR LOWER(COALESCE(documento, '')) LIKE LOWER(?) OR LOWER(nombre) LIKE LOWER(?) OR LOWER(propiedad) LIKE LOWER(?))"
        criterio = f"%{q}%"
        params.extend([criterio, criterio, criterio, criterio])
    if estado == "activos":
        query += " AND activo = 1"
    elif estado == "inactivos":
        query += " AND activo = 0"
    elif estado == "morosos":
        query += " AND al_dia = 0"

    query += " ORDER BY rol DESC, propiedad ASC, nombre ASC"
    users = db.execute(query, params).fetchall()

    content = """
    <div class="row g-4">
      <div class="col-xl-4">
        <div class="card card-shadow">
          <div class="card-body">
            <h4>Nuevo usuario</h4>
            <form method="post">
              <div class="mb-2"><label class="form-label">Usuario</label><input name="username" class="form-control" required></div>
              <div class="mb-2"><label class="form-label">Documento</label><input name="documento" class="form-control" required></div>
              <div class="mb-2">
                <label class="form-label">Contraseña</label>
                <input name="password" type="password" class="form-control" required>
                <div class="form-text">El usuario podrá cambiarla posteriormente desde “Mi cuenta”.</div>
              </div>
              <div class="mb-2"><label class="form-label">Nombre</label><input name="nombre" class="form-control" required></div>
              <div class="mb-2">
                <label class="form-label">Propiedad</label>
                <input name="propiedad" class="form-control" placeholder="Ejemplos: 12, Casa 8, Local 4" required>
              </div>
              <div class="mb-2">
                <label class="form-label">Rol</label>
                <select name="rol" class="form-select">
                  <option value="residente">Residente</option>
                  <option value="admin">Administrador</option>
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

      <div class="col-xl-8">
        <div class="card card-shadow">
          <div class="card-body">
            <div class="d-flex justify-content-between align-items-center flex-wrap gap-2 mb-3">
              <h4 class="mb-0">Usuarios</h4>
              <span class="badge text-bg-secondary">{{ users|length }} resultado(s)</span>
            </div>

            <form method="get" class="row g-2 mb-3">
              <div class="col-md-7">
                <input name="q" class="form-control" value="{{ q }}" placeholder="Buscar por usuario, documento, nombre o propiedad">
              </div>
              <div class="col-md-3">
                <select name="estado" class="form-select">
                  <option value="">Todos</option>
                  <option value="activos" {{ 'selected' if estado == 'activos' else '' }}>Activos</option>
                  <option value="inactivos" {{ 'selected' if estado == 'inactivos' else '' }}>Inactivos</option>
                  <option value="morosos" {{ 'selected' if estado == 'morosos' else '' }}>No están al día</option>
                </select>
              </div>
              <div class="col-md-2 d-grid">
                <button class="btn btn-dark">Buscar</button>
              </div>
            </form>

            <div class="table-responsive">
              <table class="table table-striped">
                <thead>
                  <tr>
                    <th>Usuario</th><th>Nombre</th><th>Propiedad</th><th>Rol</th>
                    <th>Al día</th><th>Activo</th><th>Acciones</th>
                  </tr>
                </thead>
                <tbody>
                  {% for u in users %}
                    <tr>
                      <td>{{ u['username'] }}</td>
                      <td>{{ u['nombre'] }}</td>
                      <td>{{ display_property(u['propiedad']) }}</td>
                      <td>{{ u['rol'] }}</td>
                      <td><span class="badge text-bg-{{ 'success' if u['al_dia'] else 'danger' }}">{{ 'Sí' if u['al_dia'] else 'No' }}</span></td>
                      <td><span class="badge text-bg-{{ 'success' if u['activo'] else 'secondary' }}">{{ 'Activo' if u['activo'] else 'Inactivo' }}</span></td>
                      <td>
                        <div class="d-flex gap-1 flex-wrap">
                          <a class="btn btn-sm btn-outline-primary" href="{{ url_for('admin_editar_usuario', user_id=u['id']) }}">Editar</a>
                          <a class="btn btn-sm btn-outline-warning" href="{{ url_for('admin_restablecer_password', user_id=u['id']) }}">Restablecer contraseña</a>
                          {% if u['id'] != session.get('user_id') %}
                            <form method="post" action="{{ url_for('admin_toggle_usuario', user_id=u['id']) }}" class="d-inline">
                              <button
                                class="btn btn-sm btn-outline-{{ 'secondary' if u['activo'] else 'success' }}"
                                onclick='return confirm({{ ("¿Desea desactivar este usuario?\n\nNo podrá ingresar al sistema hasta que vuelva a activarse." if u["activo"] else "¿Desea activar este usuario?\n\nPodrá ingresar nuevamente al sistema.")|tojson }})'>
                                {{ 'Desactivar' if u['activo'] else 'Activar' }}
                              </button>
                            </form>
                          {% endif %}
                        </div>
                      </td>
                    </tr>
                  {% else %}
                    <tr><td colspan="7" class="text-center text-muted">No se encontraron usuarios.</td></tr>
                  {% endfor %}
                </tbody>
              </table>
            </div>
          </div>
        </div>
      </div>
    </div>
    """
    return render_page(content, title="Usuarios", users=users, q=q, estado=estado)


@app.route("/admin/users/<int:user_id>/editar", methods=["GET", "POST"])
@admin_required
def admin_editar_usuario(user_id: int):
    db = get_db()
    user = db.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
    if not user:
        abort(404)

    if request.method == "POST":
        username = request.form.get("username", "").strip()
        documento = request.form.get("documento", "").strip()
        nombre = request.form.get("nombre", "").strip()
        propiedad = request.form.get("propiedad", "").strip()
        rol = request.form.get("rol", "residente").strip()
        al_dia = 1 if request.form.get("al_dia") == "on" else 0
        residente_permanente = 1 if request.form.get("residente_permanente") == "on" else 0

        if not all([username, documento, nombre, propiedad]):
            flash("Usuario, documento, nombre y propiedad son obligatorios.", "danger")
        elif rol not in ("admin", "residente"):
            flash("El rol seleccionado no es válido.", "danger")
        else:
            try:
                db.execute(
                    """
                    UPDATE users
                    SET username = ?, documento = ?, nombre = ?, propiedad = ?, rol = ?,
                        al_dia = ?, residente_permanente = ?
                    WHERE id = ?
                    """,
                    (username, documento, nombre, propiedad, rol, al_dia, residente_permanente, user_id),
                )
                db.commit()
                if user_id == session.get("user_id"):
                    session["nombre"] = nombre
                    session["rol"] = rol
                flash("Usuario actualizado correctamente.", "success")
                return redirect(url_for("admin_users"))
            except psycopg.IntegrityError:
                db.rollback()
                flash("El nombre de usuario ya está asignado a otra cuenta.", "danger")

    content = """
    <div class="row justify-content-center">
      <div class="col-lg-7">
        <div class="card card-shadow">
          <div class="card-body">
            <h3>Editar usuario</h3>
            <form method="post">
              <div class="mb-3"><label class="form-label">Usuario</label><input name="username" class="form-control" value="{{ user['username'] }}" required></div>
              <div class="mb-3"><label class="form-label">Documento</label><input name="documento" class="form-control" value="{{ user['documento'] or '' }}" required></div>
              <div class="mb-3"><label class="form-label">Nombre</label><input name="nombre" class="form-control" value="{{ user['nombre'] }}" required></div>
              <div class="mb-3"><label class="form-label">Propiedad</label><input name="propiedad" class="form-control" value="{{ display_property(user['propiedad']) }}" required></div>
              <div class="mb-3">
                <label class="form-label">Rol</label>
                <select name="rol" class="form-select">
                  <option value="residente" {{ 'selected' if user['rol'] == 'residente' else '' }}>Residente</option>
                  <option value="admin" {{ 'selected' if user['rol'] == 'admin' else '' }}>Administrador</option>
                </select>
              </div>
              <div class="form-check">
                <input class="form-check-input" type="checkbox" name="al_dia" id="edit_al_dia" {{ 'checked' if user['al_dia'] else '' }}>
                <label class="form-check-label" for="edit_al_dia">Al día en administración</label>
              </div>
              <div class="form-check mb-3">
                <input class="form-check-input" type="checkbox" name="residente_permanente" id="edit_permanente" {{ 'checked' if user['residente_permanente'] else '' }}>
                <label class="form-check-label" for="edit_permanente">Residente permanente</label>
              </div>
              <button class="btn btn-primary">Guardar cambios</button>
              <a class="btn btn-outline-secondary" href="{{ url_for('admin_users') }}">Cancelar</a>
            </form>
          </div>
        </div>
      </div>
    </div>
    """
    return render_page(content, title="Editar usuario", user=user)


@app.route("/admin/users/<int:user_id>/restablecer-password", methods=["GET", "POST"])
@admin_required
def admin_restablecer_password(user_id: int):
    db = get_db()
    user = db.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
    if not user:
        abort(404)

    if request.method == "POST":
        nueva = request.form.get("password_nueva", "").strip()
        confirmar = request.form.get("password_confirmar", "").strip()

        if len(nueva) < 6:
            flash("La contraseña temporal debe tener mínimo 6 caracteres.", "warning")
        elif nueva != confirmar:
            flash("La confirmación no coincide.", "danger")
        else:
            db.execute(
                "UPDATE users SET password = ? WHERE id = ?",
                (hash_password(nueva), user_id),
            )
            db.commit()
            flash(f"Contraseña restablecida para {user['username']}.", "success")
            return redirect(url_for("admin_users"))

    content = """
    <div class="row justify-content-center">
      <div class="col-lg-6">
        <div class="card card-shadow">
          <div class="card-body">
            <h3>Restablecer contraseña</h3>
            <p class="small-muted">
              Usuario: <strong>{{ user['username'] }}</strong><br>
              Propietario: {{ user['nombre'] }} — {{ display_property(user['propiedad']) }}
            </p>
            <div class="alert alert-warning">
              La contraseña actual no se muestra. Solo será reemplazada por una nueva.
            </div>
            <form method="post">
              <div class="mb-3">
                <label class="form-label">Nueva contraseña</label>
                <input type="password" name="password_nueva" class="form-control" required minlength="6">
              </div>
              <div class="mb-3">
                <label class="form-label">Confirmar nueva contraseña</label>
                <input type="password" name="password_confirmar" class="form-control" required minlength="6">
              </div>
              <button class="btn btn-warning">Restablecer contraseña</button>
              <a class="btn btn-outline-secondary" href="{{ url_for('admin_users') }}">Cancelar</a>
            </form>
          </div>
        </div>
      </div>
    </div>
    """
    return render_page(content, title="Restablecer contraseña", user=user)


@app.route("/admin/users/<int:user_id>/toggle", methods=["POST"])
@admin_required
def admin_toggle_usuario(user_id: int):
    if user_id == session.get("user_id"):
        flash("No puede desactivar su propia cuenta.", "warning")
        return redirect(url_for("admin_users"))

    db = get_db()
    user = db.execute("SELECT id, activo, username FROM users WHERE id = ?", (user_id,)).fetchone()
    if not user:
        abort(404)

    nuevo_estado = 0 if int(user["activo"]) == 1 else 1
    db.execute("UPDATE users SET activo = ? WHERE id = ?", (nuevo_estado, user_id))
    db.commit()
    flash(
        f"Usuario {user['username']} {'activado' if nuevo_estado else 'desactivado'}.",
        "success" if nuevo_estado else "warning",
    )
    return redirect(url_for("admin_users"))


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
            except psycopg.IntegrityError:
                db.rollback()
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
    app.run(debug=os.environ.get("FLASK_DEBUG", "0") == "1")
