from flask import (
    Flask,
    render_template,
    request,
    redirect,
    url_for,
    session,
    flash as _flash,
    g,
    make_response,
    jsonify,
    send_from_directory,
    abort
)

import sqlite3
import os
import base64
import time
import secrets
import math
import re
import json
import calendar
from werkzeug.security import generate_password_hash, check_password_hash
from datetime import datetime, timedelta, date
from zoneinfo import ZoneInfo
from functools import wraps


# =========================================================
# APP CONFIG
# =========================================================

app = Flask(__name__)

app.secret_key = "CHANGE_THIS_TO_A_LONG_RANDOM_SECRET_KEY"

DATABASE = "users.db"

# Duty start selfies (private folder, NOT under /static)
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
PHOTO_DIR = os.path.join(BASE_DIR, "duty_photos")
MAX_PHOTO_BYTES = 1_500_000
os.makedirs(PHOTO_DIR, exist_ok=True)
app.config["MAX_FORM_MEMORY_SIZE"] = 4 * 1024 * 1024


def flash(message, category="message"):
    """Normal flash(), but also remembers the last message so the
    admin activity log can describe what the admin just did."""

    g.last_flash = (message, category)

    _flash(message, category)

PH_TIMEZONE = ZoneInfo("Asia/Manila")

DAY_NAMES = [
    "Monday",
    "Tuesday",
    "Wednesday",
    "Thursday",
    "Friday",
    "Saturday",
    "Sunday"
]


# =========================================================
# DATABASE
# =========================================================

def get_db():
    conn = sqlite3.connect(DATABASE)
    conn.row_factory = sqlite3.Row
    return conn


# =========================================================
# TIME HELPERS
# =========================================================

def now():
    return datetime.now(PH_TIMEZONE)


def today():
    return now().strftime("%Y-%m-%d")


def now_string():
    return now().strftime("%Y-%m-%d %I:%M:%S %p")


def time_to_minutes(value):
    try:
        hour, minute = map(int, value.split(":"))
        return hour * 60 + minute
    except:
        return None


def format_time_12h(value):
    """
    Converts times to 12-hour format with AM/PM.

    "21:08"                   -> "9:08 PM"
    "2026-10-06 09:08:15 PM"  -> "9:08 PM"
    """

    if not value:
        return "—"

    value = str(value).strip()

    formats = (
        "%H:%M",
        "%Y-%m-%d %I:%M:%S %p",
    )

    for fmt in formats:

        try:

            parsed = datetime.strptime(value, fmt)

            return parsed.strftime("%I:%M %p").lstrip("0")

        except ValueError:
            continue

    return value


app.jinja_env.filters["time12"] = format_time_12h


def format_rate(value):
    """Pay rate (pesos per minute) with up to 4 decimals, at least 2.
    1.458333 -> "1.4583", 2 -> "2.00"."""

    try:
        number = float(value or 0)
    except (TypeError, ValueError):
        number = 0.0

    text = f"{number:.4f}".rstrip("0")

    whole, _, frac = text.partition(".")

    return f"{whole}.{frac.ljust(2, '0')}"


def format_late(minutes):
    """134 -> '134 min (2h 14m)', 120 -> '120 min (2h)', 25 -> '25 min'."""

    try:
        total = max(0, int(minutes or 0))
    except (TypeError, ValueError):
        total = 0

    if total < 60:
        return f"{total} min"

    hours, rest = divmod(total, 60)

    if rest:
        return f"{total} min ({hours}h {rest}m)"

    return f"{total} min ({hours}h)"


app.jinja_env.filters["late_text"] = format_late


BUILTIN_ROLES = ("admin", "employee")


def role_label(name):
    """admin/employee are shown capitalised; custom roles as typed."""

    name = str(name or "")

    return name.title() if name.lower() in BUILTIN_ROLES else name


app.jinja_env.filters["rate"] = format_rate
app.jinja_env.filters["role_label"] = role_label


def format_duration(seconds):
    seconds = max(0, int(seconds or 0))

    hours = seconds // 3600
    minutes = (seconds % 3600) // 60

    if hours > 0:
        return f"{hours}h {minutes}m"

    return f"{minutes}m"


def format_money(value):
    try:
        return f"{float(value or 0):.2f}"
    except:
        return "0.00"


# PHP deducted for every minute the user was late
LATE_DEDUCTION_PER_MINUTE = 1


def scheduled_minutes(start, end):
    """
    Length of a duty schedule in minutes (handles overnight).
    "17:00" -> "01:00" = 480.
    """

    a = time_to_minutes(start)
    b = time_to_minutes(end)

    if a is None or b is None:
        return 0

    diff = (b - a) % 1440

    return diff if diff else 1440


def compute_earnings(worked_seconds, pay_rate, late_minutes=0,
                     duty_minutes=None):
    """
    pay_rate = pesos PER MINUTE (example: 1.5 = P1.50 every minute).

    earnings = (worked minutes x pay_rate)
               - (late minutes x pay_rate)

    `duty_minutes` is kept only so old calls do not break; it is no
    longer used. Never goes below zero.
    """

    worked_minutes = max(0, int(worked_seconds or 0)) // 60
    pay_rate = float(pay_rate or 0)
    late_minutes = max(0, int(late_minutes or 0))

    return round(
        max(0, (worked_minutes - late_minutes) * pay_rate),
        2
    )


# =========================================================
# DATABASE INITIALIZATION
# =========================================================

def init_db():

    conn = get_db()

    # =====================================================
    # USERS
    # =====================================================

    conn.execute("""
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,

            username TEXT UNIQUE NOT NULL,

            password TEXT NOT NULL,

            role TEXT NOT NULL DEFAULT 'employee',

            enabled INTEGER NOT NULL DEFAULT 1,

            duty_time TEXT NOT NULL DEFAULT '17:00',

            pay_rate REAL NOT NULL DEFAULT 0,

            created_at TEXT NOT NULL
        )
    """)

    conn.commit()

    # =====================================================
    # USERS MIGRATION
    # =====================================================

    user_columns = conn.execute("""
        PRAGMA table_info(users)
    """).fetchall()

    user_existing = {
        column["name"]
        for column in user_columns
    }

    if "role" not in user_existing:

        conn.execute("""
            ALTER TABLE users
            ADD COLUMN role TEXT NOT NULL DEFAULT 'employee'
        """)

    if "enabled" not in user_existing:

        conn.execute("""
            ALTER TABLE users
            ADD COLUMN enabled INTEGER NOT NULL DEFAULT 1
        """)

    if "duty_time" not in user_existing:

        conn.execute("""
            ALTER TABLE users
            ADD COLUMN duty_time TEXT NOT NULL DEFAULT '17:00'
        """)

    if "pay_rate" not in user_existing:

        conn.execute("""
            ALTER TABLE users
            ADD COLUMN pay_rate REAL NOT NULL DEFAULT 0
        """)

    if "created_at" not in user_existing:

        conn.execute("""
            ALTER TABLE users
            ADD COLUMN created_at TEXT NOT NULL DEFAULT ''
        """)

        conn.execute("""
            UPDATE users
            SET created_at = ?
            WHERE created_at = ''
        """, (now_string(),))

    conn.commit()

    # =====================================================
    # ROLES
    # "admin" and "employee" are built in. Admins can create more
    # (Cashier, Manager...). Custom roles work like employees
    # (duty tracking) and can have a default pay rate per minute.
    # =====================================================

    conn.execute("""
        CREATE TABLE IF NOT EXISTS roles (
            id INTEGER PRIMARY KEY AUTOINCREMENT,

            name TEXT NOT NULL UNIQUE COLLATE NOCASE,

            pay_rate REAL NOT NULL DEFAULT 0,

            created_at TEXT NOT NULL
        )
    """)

    for builtin in BUILTIN_ROLES:

        conn.execute("""
            INSERT OR IGNORE INTO roles (name, pay_rate, created_at)
            VALUES (?, 0, ?)
        """, (builtin, now_string()))

    conn.commit()

    # Unknown / deleted roles fall back to "employee".
    conn.execute("""
        UPDATE users
        SET role = 'employee'
        WHERE LOWER(role) NOT IN (SELECT LOWER(name) FROM roles)
    """)

    conn.commit()

    # =====================================================
    # PAY RATE IS NOW PER MINUTE (one-time conversion)
    #
    # Old pay_rate = pay for a full duty. It is divided by 480
    # (an 8-hour duty) ONCE so old rates become per-minute rates.
    # Past logs keep the earnings they already have.
    # =====================================================

    conn.execute("""
        CREATE TABLE IF NOT EXISTS meta (
            key TEXT PRIMARY KEY,
            value TEXT
        )
    """)

    if not conn.execute("""
        SELECT 1 FROM meta WHERE key = 'pay_rate_per_minute'
    """).fetchone():

        conn.execute("""
            UPDATE users
            SET pay_rate = ROUND(pay_rate / 480.0, 4)
            WHERE pay_rate > 0
        """)

        conn.execute("""
            INSERT INTO meta (key, value)
            VALUES ('pay_rate_per_minute', ?)
        """, (now_string(),))

    conn.commit()

    # =====================================================
    # DAILY RECURRING DUTY SCHEDULES
    # =====================================================

    conn.execute("""
        CREATE TABLE IF NOT EXISTS duty_schedules (
            id INTEGER PRIMARY KEY AUTOINCREMENT,

            user_id INTEGER NOT NULL,

            start_time TEXT NOT NULL,

            off_duty_time TEXT NOT NULL,

            enabled INTEGER NOT NULL DEFAULT 1,

            created_at TEXT NOT NULL,

            FOREIGN KEY(user_id)
            REFERENCES users(id)
        )
    """)

    conn.commit()

    # =====================================================
    # WEEKLY SCHEDULE MIGRATION
    # weekday: 0 = Monday ... 6 = Sunday
    # =====================================================

    schedule_columns = {
        column["name"]
        for column in conn.execute(
            "PRAGMA table_info(duty_schedules)"
        ).fetchall()
    }

    if "weekday" not in schedule_columns:

        conn.execute("""
            ALTER TABLE duty_schedules
            ADD COLUMN weekday INTEGER
        """)

    conn.commit()

    # Old schedules were "every day". Convert them to Mon-Sun
    # rows so admin can later set a day off (example: Sunday).
    conn.execute("""
        DELETE FROM duty_schedules
        WHERE weekday IS NULL
        AND enabled = 0
    """)

    legacy = conn.execute("""
        SELECT *
        FROM duty_schedules
        WHERE weekday IS NULL
    """).fetchall()

    for row in legacy:

        conn.execute("""
            UPDATE duty_schedules
            SET weekday = 0
            WHERE id = ?
        """, (row["id"],))

        for day in range(1, 7):

            conn.execute("""
                INSERT INTO duty_schedules (
                    user_id,
                    start_time,
                    off_duty_time,
                    enabled,
                    created_at,
                    weekday
                )

                VALUES (?, ?, ?, ?, ?, ?)
            """, (
                row["user_id"],
                row["start_time"],
                row["off_duty_time"],
                1,
                row["created_at"],
                day
            ))

    conn.commit()

    # =====================================================
    # CALENDAR SCHEDULE MIGRATION
    # duty_date = the exact date a schedule row applies to.
    # Old weekly rows (weekday only) are expanded ONCE into real
    # dates, from today until the end of next month.
    # =====================================================

    schedule_columns = {
        column["name"]
        for column in conn.execute(
            "PRAGMA table_info(duty_schedules)"
        ).fetchall()
    }

    if "duty_date" not in schedule_columns:

        conn.execute("""
            ALTER TABLE duty_schedules
            ADD COLUMN duty_date TEXT
        """)

    conn.commit()

    weekly_rows = conn.execute("""
        SELECT *
        FROM duty_schedules
        WHERE duty_date IS NULL
        AND weekday IS NOT NULL
    """).fetchall()

    if weekly_rows:

        first_day = now().date()

        next_year = first_day.year + (first_day.month // 12)
        next_month = first_day.month % 12 + 1

        last_day = date(
            next_year,
            next_month,
            calendar.monthrange(next_year, next_month)[1]
        )

        span = (last_day - first_day).days + 1

        for row in weekly_rows:

            matches = [
                first_day + timedelta(days=i)
                for i in range(span)
                if (first_day + timedelta(days=i)).weekday() == row["weekday"]
            ]

            for index, day in enumerate(matches):

                if index == 0:

                    # reuse the row so today's log stays linked
                    conn.execute("""
                        UPDATE duty_schedules
                        SET duty_date = ?
                        WHERE id = ?
                    """, (day.isoformat(), row["id"]))

                else:

                    conn.execute("""
                        INSERT INTO duty_schedules (
                            user_id,
                            start_time,
                            off_duty_time,
                            enabled,
                            created_at,
                            weekday,
                            duty_date
                        )

                        VALUES (?, ?, ?, ?, ?, ?, ?)
                    """, (
                        row["user_id"],
                        row["start_time"],
                        row["off_duty_time"],
                        row["enabled"],
                        row["created_at"],
                        row["weekday"],
                        day.isoformat()
                    ))

        conn.commit()

    # =====================================================
    # DUTY LOGS
    # =====================================================

    conn.execute("""
        CREATE TABLE IF NOT EXISTS duty_logs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,

            user_id INTEGER NOT NULL,

            schedule_id INTEGER,

            username TEXT NOT NULL,

            duty_date TEXT NOT NULL,

            scheduled_time TEXT NOT NULL DEFAULT '17:00',

            scheduled_start TEXT NOT NULL DEFAULT '17:00',

            scheduled_end TEXT NOT NULL DEFAULT '19:00',

            duty_start TEXT,

            duty_end TEXT,

            late_minutes INTEGER NOT NULL DEFAULT 0,

            worked_seconds INTEGER NOT NULL DEFAULT 0,

            earnings REAL NOT NULL DEFAULT 0,

            status TEXT NOT NULL DEFAULT 'ABSENT',

            FOREIGN KEY(user_id)
            REFERENCES users(id),

            FOREIGN KEY(schedule_id)
            REFERENCES duty_schedules(id)
        )
    """)

    conn.commit()

    # =====================================================
    # DUTY LOG MIGRATION
    # =====================================================

    log_columns = conn.execute("""
        PRAGMA table_info(duty_logs)
    """).fetchall()

    log_existing = {
        column["name"]
        for column in log_columns
    }

    # Start-of-duty selfie (file name inside duty_photos/)
    if "start_photo" not in log_existing:

        conn.execute("""
            ALTER TABLE duty_logs
            ADD COLUMN start_photo TEXT
        """)

    # Old database compatibility
    if "schedule_id" not in log_existing:

        conn.execute("""
            ALTER TABLE duty_logs
            ADD COLUMN schedule_id INTEGER
        """)

    if "scheduled_time" not in log_existing:

        conn.execute("""
            ALTER TABLE duty_logs
            ADD COLUMN scheduled_time TEXT NOT NULL DEFAULT '17:00'
        """)

    if "scheduled_start" not in log_existing:

        conn.execute("""
            ALTER TABLE duty_logs
            ADD COLUMN scheduled_start TEXT NOT NULL DEFAULT '17:00'
        """)

    if "scheduled_end" not in log_existing:

        conn.execute("""
            ALTER TABLE duty_logs
            ADD COLUMN scheduled_end TEXT NOT NULL DEFAULT '19:00'
        """)

    if "status" not in log_existing:

        conn.execute("""
            ALTER TABLE duty_logs
            ADD COLUMN status TEXT NOT NULL DEFAULT 'ABSENT'
        """)

    # 1 = late deduction already applied to this log's earnings
    if "late_deducted" not in log_existing:

        conn.execute("""
            ALTER TABLE duty_logs
            ADD COLUMN late_deducted INTEGER NOT NULL DEFAULT 0
        """)

    # device clock minus server clock (seconds) at the moment the
    # employee pressed Start Duty. Large values = changed device time.
    if "clock_skew" not in log_existing:

        conn.execute("""
            ALTER TABLE duty_logs
            ADD COLUMN clock_skew INTEGER
        """)

    conn.commit()

    # =====================================================
    # LATE DEDUCTION BACKFILL (all users, runs once per log)
    #
    # Completed logs saved BEFORE the late rule existed still
    # have the full earnings. Deduct their late minutes once,
    # then flag them so this never runs twice.
    # =====================================================

    conn.execute("""
        UPDATE duty_logs

        SET
            earnings = MAX(
                0,
                earnings - (late_minutes * ?)
            ),

            late_deducted = 1

        WHERE status = 'COMPLETED'

        AND late_deducted = 0
    """, (LATE_DEDUCTION_PER_MINUTE,))

    conn.commit()

    # =====================================================
    # REPAIR OLD LOG DATA
    # =====================================================

    # Old logs may have scheduled_time but not the new fields.
    conn.execute("""
        UPDATE duty_logs

        SET scheduled_start = scheduled_time

        WHERE (
            scheduled_start IS NULL
            OR scheduled_start = ''
        )

        AND scheduled_time IS NOT NULL
        AND scheduled_time != ''
    """)

    conn.execute("""
        UPDATE duty_logs

        SET scheduled_time = scheduled_start

        WHERE (
            scheduled_time IS NULL
            OR scheduled_time = ''
        )

        AND scheduled_start IS NOT NULL
        AND scheduled_start != ''
    """)

    conn.commit()

    # =====================================================
    # EMPLOYEE DEBTS
    # status: UNPAID / PAID  (overdue is computed from due_date)
    # =====================================================

    conn.execute("""
        CREATE TABLE IF NOT EXISTS debts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,

            user_id INTEGER NOT NULL,

            amount REAL NOT NULL,

            reason TEXT NOT NULL,

            due_date TEXT NOT NULL,

            notes TEXT NOT NULL DEFAULT '',

            status TEXT NOT NULL DEFAULT 'UNPAID',

            created_at TEXT NOT NULL,

            paid_at TEXT,

            FOREIGN KEY(user_id)
            REFERENCES users(id)
        )
    """)

    conn.commit()

    # =====================================================
    # ADMIN ACTIVITY LOG (what admins did, newest first)
    # =====================================================

    conn.execute("""
        CREATE TABLE IF NOT EXISTS admin_logs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,

            admin_id INTEGER,

            admin_username TEXT NOT NULL,

            category TEXT NOT NULL,

            details TEXT NOT NULL,

            ip TEXT,

            created_at TEXT NOT NULL
        )
    """)

    conn.commit()

    # =====================================================
    # DEFAULT ADMIN
    # =====================================================

    admin = conn.execute("""
        SELECT id
        FROM users
        WHERE username = ?
    """, ("admin",)).fetchone()

    if not admin:

        conn.execute("""
            INSERT INTO users (
                username,
                password,
                role,
                enabled,
                duty_time,
                pay_rate,
                created_at
            )

            VALUES (?, ?, ?, ?, ?, ?, ?)
        """, (
            "admin",
            generate_password_hash("admin123"),
            "admin",
            1,
            "17:00",
            0,
            now_string()
        ))

        print()
        print("==========================================")
        print(" DEFAULT ADMIN")
        print(" Username : admin")
        print(" Password : admin123")
        print("==========================================")
        print()

    conn.commit()

    conn.close()


# =========================================================
# ADMIN ACTIVITY LOG
# =========================================================

ADMIN_ACTION_CATEGORIES = {
    "create_user": "Employee",
    "update_account": "Employee",
    "delete_user": "Employee",
    "update_user_settings": "Pay rate",
    "change_role": "Role",
    "create_role": "Role",
    "update_role": "Role",
    "delete_role": "Role",
    "set_calendar_schedule": "Schedule",
    "delete_schedule": "Schedule",
    "enable_schedule": "Schedule",
    "disable_schedule": "Schedule",
    "admin_mark_absent": "Duty",
    "add_debt": "Debt",
    "mark_debt_paid": "Debt",
    "delete_debt": "Debt"
}

ADMIN_ACTION_FALLBACK = {
    "enable_schedule": "Enabled a duty schedule.",
    "disable_schedule": "Disabled a duty schedule."
}


def log_admin(category, details):
    """Writes one row to the admin activity log."""

    try:

        conn = get_db()

        conn.execute("""
            INSERT INTO admin_logs (
                admin_id, admin_username, category,
                details, ip, created_at
            )

            VALUES (?, ?, ?, ?, ?, ?)
        """, (
            session.get("user_id"),
            session.get("username") or "?",
            category,
            str(details)[:500],
            request.remote_addr,
            now_string()
        ))

        conn.commit()

        conn.close()

    except Exception:
        # logging must never break the page
        pass


@app.after_request
def audit_admin_actions(response):
    """
    Every admin action that SUCCEEDED (POST under /admin/) is
    written to the activity log, using the same message the admin
    saw ("Debt of P500.00 added for juan.", "Role 'Cashier' created."...).
    Failed attempts (error messages) are not logged.
    """

    try:

        if (
            request.method == "POST"
            and request.path.startswith("/admin/")
            and session.get("role") == "admin"
            and response.status_code < 400
        ):

            last = getattr(g, "last_flash", None)

            if last:

                message, category = last

                if category != "success":
                    return response

            else:

                message = ADMIN_ACTION_FALLBACK.get(request.endpoint)

                if not message:
                    return response

            log_admin(
                ADMIN_ACTION_CATEGORIES.get(request.endpoint, "Admin"),
                message
            )

    except Exception:
        pass

    return response


# =========================================================
# AUTH
# =========================================================

def login_required(func):

    @wraps(func)
    def wrapper(*args, **kwargs):

        if "user_id" not in session:
            return redirect(url_for("login"))

        # stale session (account deleted / DB reset / disabled)
        conn = get_db()
        row = conn.execute(
            "SELECT id, enabled, role FROM users WHERE id = ?",
            (session["user_id"],)
        ).fetchone()
        conn.close()

        if not row or not row["enabled"]:
            session.clear()
            return redirect(url_for("login"))

        # the role may have been changed by an admin since login
        session["role"] = row["role"]

        return func(*args, **kwargs)

    return wrapper


def admin_required(func):

    @wraps(func)
    def wrapper(*args, **kwargs):

        if "user_id" not in session:
            return redirect(url_for("login"))

        conn = get_db()
        row = conn.execute(
            "SELECT id, enabled, role FROM users WHERE id = ?",
            (session["user_id"],)
        ).fetchone()
        conn.close()

        if not row or not row["enabled"]:
            session.clear()
            return redirect(url_for("login"))

        # always trust the database, not the cookie
        session["role"] = row["role"]

        if row["role"] != "admin":

            flash(
                "Admin access required.",
                "error"
            )

            return redirect(url_for("dashboard"))

        return func(*args, **kwargs)

    return wrapper


# =========================================================
# AUTOMATIC ABSENT GENERATION
# =========================================================

ABSENT_LOOKBACK_DAYS = 30


def parse_created(value):
    for fmt in ("%Y-%m-%d %I:%M:%S %p", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
        try:
            return datetime.strptime(
                str(value).strip(), fmt
            ).replace(tzinfo=PH_TIMEZONE)
        except ValueError:
            continue

    return None


def sync_absent_logs(user_id=None):
    """
    Marks a duty as ABSENT once its off-duty time has passed
    without the user starting it.

    Example: schedule 9:00 PM -> 10:00 PM, no Start Duty click,
    clock reaches 10:00 PM -> ABSENT log (0 worked, 0 earnings).

    - Overnight schedules (11 PM -> 1 AM) end the next day.
    - Schedules created/edited after their window already ended
      are skipped, so nobody is absent for a duty that did not
      exist yet.
    - Looks back ABSENT_LOOKBACK_DAYS days (idempotent).
    """

    current = now()
    first_day = current.date() - timedelta(days=ABSENT_LOOKBACK_DAYS)

    conn = get_db()

    query = """
        SELECT ds.*, u.username
        FROM duty_schedules ds
        JOIN users u ON u.id = ds.user_id
        WHERE ds.enabled = 1
        AND ds.duty_date IS NOT NULL
        AND u.enabled = 1
        AND u.role != 'admin'
    """
    params = []

    if user_id is not None:
        query += " AND ds.user_id = ?"
        params.append(user_id)

    schedules = conn.execute(query, params).fetchall()

    if not schedules:
        conn.close()
        return

    existing = {
        (row["schedule_id"], row["duty_date"])
        for row in conn.execute("""
            SELECT schedule_id, duty_date
            FROM duty_logs
            WHERE duty_date >= ?
        """, (first_day.isoformat(),)).fetchall()
    }

    changed = False

    for offset in range(ABSENT_LOOKBACK_DAYS, -1, -1):

        day = current.date() - timedelta(days=offset)

        for s in schedules:

            if s["duty_date"] != day.isoformat():
                continue

            start_min = time_to_minutes(s["start_time"])
            end_min = time_to_minutes(s["off_duty_time"])

            if start_min is None or end_min is None:
                continue

            overnight = end_min <= start_min

            end_dt = datetime(
                day.year, day.month, day.day,
                end_min // 60, end_min % 60,
                tzinfo=PH_TIMEZONE
            )

            if overnight:
                end_dt += timedelta(days=1)

            # Window not over yet
            if end_dt > current:
                continue

            # Schedule did not exist yet when the window ended
            created = parse_created(s["created_at"])

            if created and end_dt <= created:
                continue

            next_day = (day + timedelta(days=1)).isoformat()

            if (
                (s["id"], day.isoformat()) in existing
                or (overnight and (s["id"], next_day) in existing)
            ):
                continue

            conn.execute("""
                INSERT INTO duty_logs (
                    user_id, schedule_id, username, duty_date,
                    scheduled_time, scheduled_start, scheduled_end,
                    duty_start, duty_end,
                    late_minutes, worked_seconds, earnings,
                    status
                )
                SELECT ?, ?, ?, ?, ?, ?, ?, NULL, NULL, 0, 0, 0, 'ABSENT'
                WHERE NOT EXISTS (
                    SELECT 1 FROM duty_logs
                    WHERE schedule_id = ? AND duty_date = ?
                )
            """, (
                s["user_id"], s["id"], s["username"], day.isoformat(),
                s["start_time"], s["start_time"], s["off_duty_time"],
                s["id"], day.isoformat()
            ))

            existing.add((s["id"], day.isoformat()))
            changed = True

    if changed:
        conn.commit()

    conn.close()


# =========================================================
# INDEX
# =========================================================

@app.route("/")
def index():

    if "user_id" not in session:
        return redirect(url_for("login"))

    if session.get("role") == "admin":
        return redirect(url_for("admin"))

    return redirect(url_for("dashboard"))


# =========================================================
# LOGIN
# =========================================================

@app.route("/login", methods=["GET", "POST"])
def login():
    """
    One login page for everyone. The account's role decides where
    it goes after signing in (admin -> /admin, employee -> /dashboard).
    """

    if "user_id" in session:

        if session.get("role") == "admin":
            return redirect(url_for("admin"))

        return redirect(url_for("dashboard"))

    if request.method == "POST":

        username = request.form.get("username", "").strip()
        password = request.form.get("password", "")

        if not username or not password:

            flash("Username and password are required.", "error")

            return redirect(url_for("login"))

        conn = get_db()

        user = conn.execute("""
            SELECT *
            FROM users

            WHERE username = ?

            LIMIT 1
        """, (username,)).fetchone()

        conn.close()

        if not user or not check_password_hash(user["password"], password):

            flash("Invalid username or password.", "error")

            return redirect(url_for("login"))

        if user["enabled"] != 1:

            flash("This account is disabled.", "error")

            return redirect(url_for("login"))

        session.clear()

        session["user_id"] = user["id"]

        session["username"] = user["username"]

        session["role"] = user["role"]

        if user["role"] == "admin":

            log_admin("Login", "Signed in.")

            return redirect(url_for("admin"))

        return redirect(url_for("dashboard"))

    return render_template("login.html")


# =========================================================
# LOGOUT
# =========================================================

@app.route("/logout")
@login_required
def logout():

    session.clear()

    return redirect(url_for("login"))


# =========================================================
# SETTINGS (change own password)
# =========================================================

@app.route("/settings", methods=["GET", "POST"])
@login_required
def settings():

    user_id = session["user_id"]

    if request.method == "POST":

        current = request.form.get("current_password", "")
        new = request.form.get("new_password", "")
        confirm = request.form.get("confirm_password", "")

        conn = get_db()

        user = conn.execute("""
            SELECT id, password
            FROM users

            WHERE id = ?
        """, (user_id,)).fetchone()

        error = None

        if not user or not check_password_hash(user["password"], current):

            error = "Current password is incorrect."

        elif len(new) < MIN_PASSWORD_LENGTH:

            error = f"New password must be at least {MIN_PASSWORD_LENGTH} characters."

        elif len(new) > 128:

            error = "New password is too long (max 128)."

        elif new != confirm:

            error = "New passwords do not match."

        elif new == current:

            error = "New password must be different from the current one."

        if error:

            conn.close()

            flash(error, "error")

            return redirect(url_for("settings"))

        conn.execute("""
            UPDATE users
            SET password = ?
            WHERE id = ?
        """, (generate_password_hash(new), user_id))

        conn.commit()

        conn.close()

        flash("Password changed successfully.", "success")

        return redirect(url_for("settings"))

    return render_template(
        "settings.html",
        username=session.get("username"),
        is_admin=session.get("role") == "admin"
    )


# =========================================================
# DASHBOARD
# =========================================================

def build_dashboard_context(user_id):

    sync_absent_logs(user_id)

    conn = get_db()

    user = conn.execute("""
        SELECT *
        FROM users

        WHERE id = ?
    """, (user_id,)).fetchone()

    current = now()

    today_weekday = current.weekday()

    # this week (Mon-Sun), real dates only
    monday = current.date() - timedelta(days=today_weekday)
    sunday = monday + timedelta(days=6)

    week_rows = conn.execute("""
        SELECT *
        FROM duty_schedules

        WHERE user_id = ?

        AND enabled = 1

        AND duty_date BETWEEN ? AND ?

        ORDER BY duty_date ASC, start_time ASC
    """, (
        user_id,
        monday.isoformat(),
        sunday.isoformat()
    )).fetchall()

    week = {}

    for row in week_rows:
        week.setdefault(
            date.fromisoformat(row["duty_date"]).weekday(),
            []
        ).append(row)

    schedules = week.get(today_weekday, [])

    logs = conn.execute("""
        SELECT *
        FROM duty_logs

        WHERE user_id = ?

        AND duty_date = ?

        ORDER BY scheduled_start ASC, id ASC
    """, (
        user_id,
        today()
    )).fetchall()

    conn.close()

    cards = []

    for schedule in schedules:

        log = None

        for row in logs:
            if row["schedule_id"] == schedule["id"]:
                log = row

        start_min = time_to_minutes(schedule["start_time"])
        end_min = time_to_minutes(schedule["off_duty_time"])

        start_ts = end_ts = duty_ts = None
        phase = "open"

        if start_min is not None and end_min is not None:

            day = current.date()

            start_dt = datetime(
                day.year, day.month, day.day,
                start_min // 60, start_min % 60,
                tzinfo=PH_TIMEZONE
            )

            end_dt = datetime(
                day.year, day.month, day.day,
                end_min // 60, end_min % 60,
                tzinfo=PH_TIMEZONE
            )

            if end_min <= start_min:
                end_dt += timedelta(days=1)

            start_ts = start_dt.timestamp()
            end_ts = end_dt.timestamp()

            if current < start_dt:
                phase = "upcoming"
            elif current >= end_dt:
                phase = "ended"

        if log and log["duty_start"] and log["status"] == "ON DUTY":

            try:
                duty_ts = datetime.strptime(
                    log["duty_start"],
                    "%Y-%m-%d %I:%M:%S %p"
                ).replace(tzinfo=PH_TIMEZONE).timestamp()
            except ValueError:
                duty_ts = None

        cards.append({
            "schedule": schedule,
            "log": log,
            "phase": phase,
            "start_ts": start_ts,
            "end_ts": end_ts,
            "duty_ts": duty_ts
        })

    # ---------------- activity (all records, newest first) ----------------
    conn = get_db()

    all_logs = conn.execute("""
        SELECT *
        FROM duty_logs

        WHERE user_id = ?

        ORDER BY
            duty_date DESC,
            scheduled_start ASC,
            id DESC
    """, (user_id,)).fetchall()

    conn.close()

    ACTIVITY_LIMIT = 30

    activity = dict(
        activity_logs=all_logs[:ACTIVITY_LIMIT],
        activity_total=len(all_logs),
        activity_limit=ACTIVITY_LIMIT,
        total_earnings=sum(
            float(row["earnings"] or 0) for row in all_logs
        ),
        **debt_context(user_id)
    )

    return dict(
        **activity,
        user=user,
        cards=cards,
        week=week,
        day_names=DAY_NAMES,
        week_dates=[monday + timedelta(days=i) for i in range(7)],
        today_weekday=today_weekday,
        today_name=DAY_NAMES[today_weekday],
        has_any_schedule=bool(week_rows),
        now_ts=current.timestamp(),
        format_duration=format_duration
    )


@app.route("/dashboard")
@login_required
def dashboard():

    return render_template(
        "dashboard.html",
        **build_dashboard_context(session["user_id"])
    )


# Realtime partial. Polled by dashboard.html. Does not touch flash
# messages, so they are not consumed by the background refresh.
@app.route("/dashboard/live")
@login_required
def dashboard_live():

    response = make_response(
        render_template(
            "dashboard.html",
            partial=True,
            **build_dashboard_context(session["user_id"])
        )
    )

    response.headers["Cache-Control"] = "no-store"

    return response



# =========================================================
# TIME API (Philippine time, from this server's clock)
# =========================================================

@app.route("/api/time")
def api_time():

    current = datetime.now(PH_TIMEZONE)

    response = jsonify(
        datetime=current.isoformat(),
        timezone="Asia/Manila",
        utc_offset="+08:00",
        unix=int(current.timestamp())
    )

    response.headers["Cache-Control"] = "no-store"

    return response


# =========================================================
# DUTY PHOTO (selfie required to start duty)
# =========================================================

def save_duty_photo(data_url, user_id):
    """
    Validates a JPEG data URL sent by the dashboard camera and
    saves it to duty_photos/. Returns the file name, or None
    when the photo is missing / invalid.
    """

    prefix = "data:image/jpeg;base64,"

    if not data_url or not data_url.startswith(prefix):
        return None

    try:
        raw = base64.b64decode(data_url[len(prefix):], validate=True)
    except Exception:
        return None

    if not raw or len(raw) > MAX_PHOTO_BYTES:
        return None

    # real JPEG magic bytes
    if not raw.startswith(b"\xff\xd8\xff"):
        return None

    name = "u{}_{}_{}.jpg".format(
        user_id,
        now().strftime("%Y%m%d_%H%M%S"),
        secrets.token_hex(6)
    )

    with open(os.path.join(PHOTO_DIR, name), "wb") as f:
        f.write(raw)

    return name


@app.route("/duty/photo/<int:log_id>")
@login_required
def duty_photo(log_id):

    conn = get_db()

    row = conn.execute(
        "SELECT user_id, start_photo FROM duty_logs WHERE id = ?",
        (log_id,)
    ).fetchone()

    conn.close()

    if not row or not row["start_photo"]:
        abort(404)

    # file names start with u<user_id>_ : must match the log's owner
    if not row["start_photo"].startswith(
        "u{}_".format(row["user_id"])
    ):
        abort(404)

    # only the admin or the owner of the log can see it
    if (
        session.get("role") != "admin"
        and row["user_id"] != session["user_id"]
    ):
        abort(403)

    response = send_from_directory(
        PHOTO_DIR,
        os.path.basename(row["start_photo"]),
        mimetype="image/jpeg"
    )

    response.headers["Cache-Control"] = "private, max-age=3600"

    return response


# =========================================================
# START DUTY
# =========================================================

@app.route(
    "/duty/start/<int:schedule_id>",
    methods=["POST"]
)
@login_required
def start_duty(schedule_id):

    user_id = session["user_id"]

    conn = get_db()

    schedule = conn.execute("""
        SELECT
            ds.*,
            u.username,
            u.pay_rate

        FROM duty_schedules ds

        JOIN users u
            ON u.id = ds.user_id

        WHERE ds.id = ?

        AND ds.user_id = ?

        AND ds.enabled = 1

        LIMIT 1
    """, (
        schedule_id,
        user_id
    )).fetchone()

    if not schedule:

        conn.close()

        flash(
            "Duty schedule not found.",
            "error"
        )

        return redirect(url_for("dashboard"))

    current = now()

    # Day-off protection: a schedule can only be started
    # on the date it was set for.
    if (
        schedule["duty_date"] is not None
        and schedule["duty_date"] != current.date().isoformat()
    ):

        conn.close()

        flash(
            "You are off duty today.",
            "error"
        )

        return redirect(url_for("dashboard"))

    current_minutes = (
        current.hour * 60
        + current.minute
    )

    start_minutes = time_to_minutes(
        schedule["start_time"]
    )

    end_minutes = time_to_minutes(
        schedule["off_duty_time"]
    )

    if start_minutes is None or end_minutes is None:

        conn.close()

        flash(
            "Invalid duty schedule.",
            "error"
        )

        return redirect(url_for("dashboard"))

    # =====================================================
    # START TIME VALIDATION (SERVER CLOCK ONLY)
    #
    # The duty window is built as real datetimes on the schedule's
    # own date, so a user can never start before the scheduled start
    # time (not even after midnight on overnight schedules).
    # The device clock is NEVER used here.
    # =====================================================

    try:
        duty_day = (
            date.fromisoformat(schedule["duty_date"])
            if schedule["duty_date"]
            else current.date()
        )
    except ValueError:
        duty_day = current.date()

    window_start = datetime(
        duty_day.year, duty_day.month, duty_day.day,
        start_minutes // 60, start_minutes % 60,
        tzinfo=PH_TIMEZONE
    )

    window_end = datetime(
        duty_day.year, duty_day.month, duty_day.day,
        end_minutes // 60, end_minutes % 60,
        tzinfo=PH_TIMEZONE
    )

    if end_minutes <= start_minutes:
        # Overnight schedule (11 PM -> 1 AM): ends the next day
        window_end += timedelta(days=1)

    if current < window_start:

        conn.close()

        flash(
            f"You cannot start yet. Duty starts at "
            f"{format_time_12h(schedule['start_time'])}.",
            "error"
        )

        return redirect(url_for("dashboard"))

    if current >= window_end:

        conn.close()

        flash(
            f"This duty ended at "
            f"{format_time_12h(schedule['off_duty_time'])}.",
            "error"
        )

        return redirect(url_for("dashboard"))

    # =====================================================
    # CHECK EXISTING RECORD
    # =====================================================

    existing = conn.execute("""
        SELECT *
        FROM duty_logs

        WHERE user_id = ?

        AND schedule_id = ?

        AND duty_date = ?

        LIMIT 1
    """, (
        user_id,
        schedule_id,
        today()
    )).fetchone()

    if existing:

        if existing["status"] == "ABSENT":

            conn.close()

            flash(
                "This duty has already been marked ABSENT.",
                "error"
            )

            return redirect(url_for("dashboard"))

        if existing["duty_start"]:

            conn.close()

            flash(
                "This duty has already been started.",
                "error"
            )

            return redirect(url_for("dashboard"))

    # =====================================================
    # CHECK ACTIVE DUTY
    # =====================================================

    active = conn.execute("""
        SELECT id
        FROM duty_logs

        WHERE user_id = ?

        AND duty_date = ?

        AND status = 'ON DUTY'

        AND duty_start IS NOT NULL

        AND duty_end IS NULL

        LIMIT 1
    """, (
        user_id,
        today()
    )).fetchone()

    if active:

        conn.close()

        flash(
            "You already have an active duty. End it first.",
            "error"
        )

        return redirect(url_for("dashboard"))

    # =====================================================
    # LATE
    # =====================================================

    late_minutes = max(
        0,
        int((current - window_start).total_seconds() // 60)
    )

    start_string = current.strftime(
        "%Y-%m-%d %I:%M:%S %p"
    )

    # =====================================================
    # PHOTO IS REQUIRED TO START DUTY
    # =====================================================

    # The dashboard sends the id of the user it was opened for.
    # If someone else logged in on the same device/tab meanwhile,
    # the photo is rejected (never saved under the wrong user).
    if request.form.get("expected_user") != str(user_id):

        conn.close()

        flash(
            "Your session changed. Please take the photo again.",
            "error"
        )

        return redirect(url_for("dashboard"))

    photo_name = save_duty_photo(
        request.form.get("photo", ""),
        user_id
    )

    if not photo_name:

        conn.close()

        flash(
            "A photo is required to start duty. "
            "Please take a picture and try again.",
            "error"
        )

        return redirect(url_for("dashboard"))

    # =====================================================
    # CREATE LOG
    # =====================================================

    if existing:

        conn.execute("""
            UPDATE duty_logs

            SET
                duty_start = ?,

                late_minutes = ?,

                start_photo = ?,

                status = 'ON DUTY'

            WHERE id = ?

            AND user_id = ?
        """, (
            start_string,
            late_minutes,
            photo_name,
            existing["id"],
            user_id
        ))

    else:

        conn.execute("""
            INSERT INTO duty_logs (
                user_id,
                schedule_id,
                username,
                duty_date,

                scheduled_time,

                scheduled_start,
                scheduled_end,

                duty_start,
                duty_end,

                late_minutes,

                worked_seconds,

                earnings,

                status,

                start_photo
            )

            VALUES (
                ?, ?, ?, ?,
                ?,
                ?, ?,
                ?, ?,
                ?,
                ?,
                ?,
                ?,
                ?
            )
        """, (
            user_id,
            schedule_id,
            schedule["username"],
            today(),

            # Compatibility with old database
            schedule["start_time"],

            schedule["start_time"],
            schedule["off_duty_time"],

            start_string,
            None,

            late_minutes,

            0,

            0,

            "ON DUTY",

            photo_name
        ))

    conn.commit()

    conn.close()

    if late_minutes > 0:

        flash(
            f"Duty started. Late by {format_late(late_minutes)}.",
            "warning"
        )

    else:

        flash(
            "Duty started on time.",
            "success"
        )

    return redirect(url_for("dashboard"))


# =========================================================
# END DUTY
# =========================================================

@app.route(
    "/duty/end/<int:log_id>",
    methods=["POST"]
)
@login_required
def end_duty(log_id):

    user_id = session["user_id"]

    conn = get_db()

    duty = conn.execute("""
        SELECT *
        FROM duty_logs

        WHERE id = ?

        AND user_id = ?

        AND status = 'ON DUTY'

        AND duty_start IS NOT NULL

        AND duty_end IS NULL

        LIMIT 1
    """, (
        log_id,
        user_id
    )).fetchone()

    user = conn.execute("""
        SELECT *
        FROM users

        WHERE id = ?

        LIMIT 1
    """, (user_id,)).fetchone()

    if not duty:

        conn.close()

        flash(
            "Active duty not found.",
            "error"
        )

        return redirect(url_for("dashboard"))

    current = now()

    try:

        start_dt = datetime.strptime(
            duty["duty_start"],
            "%Y-%m-%d %I:%M:%S %p"
        ).replace(
            tzinfo=PH_TIMEZONE
        )

    except:

        conn.close()

        flash(
            "Invalid duty start time.",
            "error"
        )

        return redirect(url_for("dashboard"))

    # =====================================================
    # WORKED TIME (whole minutes)
    #
    # - Start is counted from the minute the user started,
    #   so late minutes are automatically NOT paid.
    #   9:12 PM -> 9:13 PM on time = 1 minute.
    # - End is capped at the scheduled off-duty time.
    #   Off duty 10:00 PM, out at 11:00 PM -> 10:00 PM.
    # =====================================================

    start_dt = start_dt.replace(second=0, microsecond=0)

    end_dt = current.replace(second=0, microsecond=0)

    off_minutes = time_to_minutes(
        duty["scheduled_end"]
    )

    if off_minutes is not None:

        off_dt = start_dt.replace(
            hour=off_minutes // 60,
            minute=off_minutes % 60
        )

        # Overnight schedule (example: 11 PM -> 1 AM)
        if off_dt <= start_dt:
            off_dt += timedelta(days=1)

        if end_dt > off_dt:
            end_dt = off_dt

    worked_seconds = max(
        0,
        int(
            (
                end_dt
                - start_dt
            ).total_seconds()
        )
    )

    try:

        pay_rate = float(
            user["pay_rate"] or 0
        )

    except:

        pay_rate = 0

    earnings = compute_earnings(
        worked_seconds,
        pay_rate,
        duty["late_minutes"]
    )

    end_string = end_dt.strftime(
        "%Y-%m-%d %I:%M:%S %p"
    )

    conn.execute("""
        UPDATE duty_logs

        SET
            duty_end = ?,

            worked_seconds = ?,

            earnings = ?,

            late_deducted = 1,

            status = 'COMPLETED'

        WHERE id = ?
    """, (
        end_string,
        worked_seconds,
        earnings,
        log_id
    ))

    conn.commit()

    conn.close()

    flash(
        "Duty completed successfully.",
        "success"
    )

    return redirect(url_for("dashboard"))


# =========================================================
# DEBT HELPERS (shown on the activity pages)
# =========================================================

def debt_context(user_id):
    """Debts of one employee + totals, for the dashboard."""

    conn = get_db()

    rows = conn.execute("""
        SELECT *
        FROM debts

        WHERE user_id = ?

        ORDER BY
            (status = 'PAID') ASC,
            due_date ASC,
            id DESC
    """, (user_id,)).fetchall()

    conn.close()

    today_str = today()

    debts = []

    for row in rows:

        item = dict(row)

        item["overdue"] = (
            row["status"] == "UNPAID"
            and row["due_date"] < today_str
        )

        debts.append(item)

    return dict(
        debts=debts,

        debt_outstanding=sum(
            d["amount"] for d in debts if d["status"] == "UNPAID"
        ),

        debt_overdue_count=sum(
            1 for d in debts if d["overdue"]
        ),

        debt_paid_total=sum(
            d["amount"] for d in debts if d["status"] == "PAID"
        )
    )


# =========================================================
# ACTIVITY / LOGS
# =========================================================

@app.route("/logs")
@login_required
def my_logs():
    """Old /logs page: activity now lives on the dashboard."""

    return redirect(url_for("dashboard") + "#activity")


# =========================================================
# ADMIN
# =========================================================

@app.route("/admin")
@admin_required
def admin():

    sync_absent_logs()

    conn = get_db()

    users = conn.execute("""
        SELECT *
        FROM users

        ORDER BY LOWER(username) ASC
    """).fetchall()

    roles = conn.execute("""
        SELECT
            r.*,

            (
                SELECT COUNT(*)
                FROM users u
                WHERE LOWER(u.role) = LOWER(r.name)
            ) AS user_count

        FROM roles r

        ORDER BY
            (LOWER(r.name) = 'admin') DESC,
            (LOWER(r.name) = 'employee') DESC,
            LOWER(r.name) ASC
    """).fetchall()

    admin_logs = conn.execute("""
        SELECT *
        FROM admin_logs

        ORDER BY id DESC

        LIMIT 500
    """).fetchall()

    schedules = conn.execute("""
        SELECT
            ds.*,

            u.username

        FROM duty_schedules ds

        JOIN users u
            ON u.id = ds.user_id

        ORDER BY
            u.username ASC,

            ds.start_time ASC
    """).fetchall()

    logs = conn.execute("""
        SELECT *
        FROM duty_logs

        ORDER BY
            duty_date DESC,

            id DESC

        LIMIT 200
    """).fetchall()

    # users currently on duty (clocked in, not yet clocked out)
    active_rows = conn.execute("""
        SELECT user_id, duty_start, scheduled_start, scheduled_end
        FROM duty_logs
        WHERE status = 'ON DUTY'
        AND duty_start IS NOT NULL
        AND duty_end IS NULL
    """).fetchall()

    # ---------------- EARNINGS (date range) ----------------
    today_str = today()

    def valid_date(value, default):
        try:
            datetime.strptime(value, "%Y-%m-%d")
            return value
        except (ValueError, TypeError):
            return default

    range_applied = "from" in request.args or "to" in request.args

    from_date = valid_date(
        request.args.get("from", "").strip(),
        today_str[:8] + "01"
    )

    to_date = valid_date(
        request.args.get("to", "").strip(),
        today_str
    )

    if from_date > to_date:
        from_date, to_date = to_date, from_date

    earnings_rows = conn.execute("""
        SELECT
            u.id,
            u.username,
            u.pay_rate,
            u.enabled,

            COALESCE(SUM(l.earnings), 0) AS total_earnings,
            COALESCE(SUM(l.worked_seconds), 0) AS total_worked,
            COALESCE(SUM(CASE WHEN l.status = 'COMPLETED'
                              THEN 1 ELSE 0 END), 0) AS days_worked,
            COALESCE(SUM(CASE WHEN l.status = 'ABSENT'
                              THEN 1 ELSE 0 END), 0) AS days_absent,
            COALESCE(SUM(l.late_minutes), 0) AS total_late

        FROM users u

        LEFT JOIN duty_logs l
            ON l.user_id = u.id
            AND l.duty_date BETWEEN ? AND ?

        WHERE u.role != 'admin'

        GROUP BY u.id

        ORDER BY total_earnings DESC, u.username ASC
    """, (from_date, to_date)).fetchall()

    grand_total = sum(float(r["total_earnings"] or 0) for r in earnings_rows)
    grand_worked = sum(int(r["total_worked"] or 0) for r in earnings_rows)

    duty_status = build_duty_status(
        conn,
        parse_view_date(request.args.get("date"))
    )

    # ---------------- DEBTS ----------------
    debt_rows = conn.execute("""
        SELECT d.*, u.username

        FROM debts d

        JOIN users u
            ON u.id = d.user_id

        ORDER BY
            (d.status = 'PAID') ASC,
            d.due_date ASC,
            d.id DESC
    """).fetchall()

    debts = []

    for row in debt_rows:

        item = dict(row)

        item["overdue"] = (
            row["status"] == "UNPAID"
            and row["due_date"] < today_str
        )

        debts.append(item)

    debt_summary = {
        "outstanding": sum(
            d["amount"] for d in debts if d["status"] == "UNPAID"
        ),
        "unpaid_count": sum(
            1 for d in debts if d["status"] == "UNPAID"
        ),
        "overdue_amount": sum(
            d["amount"] for d in debts if d["overdue"]
        ),
        "overdue_count": sum(
            1 for d in debts if d["overdue"]
        ),
        "paid_total": sum(
            d["amount"] for d in debts if d["status"] == "PAID"
        )
    }

    # ---------------- PAYSLIPS (tab) ----------------
    ps_cutoff = parse_cutoff(
        request.args.get("ps_start"),
        request.args.get("ps_end")
    )

    ps_slips = employees_slips(conn, ps_cutoff)

    conn.close()

    active_map = {row["user_id"]: row for row in active_rows}

    on_duty_users = [u for u in users if u["id"] in active_map]
    off_duty_users = [u for u in users if u["id"] not in active_map]

    # schedule_dates[user_id][date] = [start, end]  (calendar editor)
    schedule_dates = {}

    for schedule in schedules:

        if schedule["enabled"] and schedule["duty_date"]:

            schedule_dates.setdefault(
                schedule["user_id"],
                {}
            ).setdefault(
                schedule["duty_date"],
                [schedule["start_time"], schedule["off_duty_time"]]
            )

    return render_template(
        "admin.html",

        users=users,

        roles=roles,

        admin_logs=admin_logs,

        schedules=schedules,

        logs=logs,

        schedule_dates=schedule_dates,

        active_map=active_map,

        on_duty_users=on_duty_users,

        off_duty_users=off_duty_users,

        earnings_rows=earnings_rows,

        grand_total=grand_total,

        grand_worked=grand_worked,

        from_date=from_date,

        to_date=to_date,

        today_str=today_str,

        ps_cutoff=ps_cutoff,
        ps_slips=ps_slips,
        ps_presets=cutoff_presets(),
        ps_gross=round(sum(s["totals"]["gross"] for s in ps_slips), 2),
        ps_late=round(
            sum(s["totals"]["late_deduction"] for s in ps_slips), 2
        ),
        ps_net=round(sum(s["totals"]["net"] for s in ps_slips), 2),

        range_applied=range_applied,

        duty_status=duty_status,

        debts=debts,

        recent_debts=sorted(
            debts, key=lambda d: d["id"], reverse=True
        )[:200],

        debt_summary=debt_summary,

        debt_users=[
            u for u in users
            if u["role"] != "admin" and u["enabled"]
        ],

        current_user_id=session["user_id"],

        format_duration=format_duration
    )


# =========================================================
# ADMIN DUTY STATUS (calendar view + mark absent)
# =========================================================

STATE_LABELS = {
    "on_duty": "ON DUTY",
    "completed": "COMPLETED",
    "absent": "ABSENT",
    "waiting": "NOT STARTED",
    "upcoming": "UPCOMING",
    "no_record": "NO RECORD"
}


def parse_view_date(value):
    """Returns a date from 'YYYY-MM-DD', or today if missing/invalid."""

    try:
        return datetime.strptime(str(value).strip(), "%Y-%m-%d").date()
    except (ValueError, TypeError):
        return now().date()


def build_duty_status(conn, view_date):
    """
    Everyone scheduled on `view_date` (today, yesterday, any day)
    with their duty state:

    ON DUTY / COMPLETED / ABSENT / NOT STARTED / UPCOMING / NO RECORD

    Users with no schedule on that date are returned as day_off.
    """

    current = now()
    today_date = current.date()
    date_str = view_date.isoformat()
    is_today = view_date == today_date

    rows = conn.execute("""
        SELECT
            ds.id AS schedule_id,
            ds.start_time,
            ds.off_duty_time,
            u.id AS user_id,
            u.username,
            l.id AS log_id,
            l.status AS log_status,
            l.duty_start,
            l.duty_end,
            l.late_minutes,
            l.worked_seconds,
            l.earnings

        FROM duty_schedules ds

        JOIN users u
            ON u.id = ds.user_id

        LEFT JOIN duty_logs l
            ON l.schedule_id = ds.id
            AND l.duty_date = ?

        WHERE ds.enabled = 1
        AND ds.duty_date = ?
        AND u.role != 'admin'
        AND (u.enabled = 1 OR l.id IS NOT NULL)

        ORDER BY ds.start_time ASC, u.username ASC
    """, (date_str, date_str)).fetchall()

    matched_logs = {r["log_id"] for r in rows if r["log_id"]}

    # Logs without a matching schedule row (schedule edited/deleted)
    # + overnight duties still running from the previous day.
    extra = conn.execute("""
        SELECT
            l.schedule_id,
            l.scheduled_start AS start_time,
            l.scheduled_end AS off_duty_time,
            u.id AS user_id,
            u.username,
            l.id AS log_id,
            l.status AS log_status,
            l.duty_start,
            l.duty_end,
            l.late_minutes,
            l.worked_seconds,
            l.earnings

        FROM duty_logs l

        JOIN users u
            ON u.id = l.user_id

        WHERE u.role != 'admin'
        AND (
            l.duty_date = ?
            OR (
                ? = 1
                AND l.status = 'ON DUTY'
                AND l.duty_end IS NULL
                AND l.duty_date < ?
            )
        )
    """, (date_str, 1 if is_today else 0, date_str)).fetchall()

    all_rows = list(rows) + [r for r in extra if r["log_id"] not in matched_logs]

    entries = []

    for r in all_rows:

        status = r["log_status"]

        if status == "ON DUTY":
            state = "on_duty"
        elif status == "COMPLETED":
            state = "completed"
        elif status == "ABSENT":
            state = "absent"
        else:
            # no log yet
            state = "upcoming"

            if view_date < today_date:
                state = "no_record"

            elif is_today:

                start_min = time_to_minutes(r["start_time"])
                end_min = time_to_minutes(r["off_duty_time"])

                if start_min is not None and end_min is not None:

                    start_dt = datetime(
                        view_date.year, view_date.month, view_date.day,
                        start_min // 60, start_min % 60,
                        tzinfo=PH_TIMEZONE
                    )

                    end_dt = start_dt.replace(
                        hour=end_min // 60,
                        minute=end_min % 60
                    )

                    if end_min <= start_min:
                        end_dt += timedelta(days=1)

                    if current >= end_dt:
                        state = "no_record"
                    elif current >= start_dt:
                        state = "waiting"

        entries.append({
            "user_id": r["user_id"],
            "username": r["username"],
            "schedule_id": r["schedule_id"],
            "log_id": r["log_id"],
            "start_time": r["start_time"],
            "off_duty_time": r["off_duty_time"],
            "duty_start": r["duty_start"],
            "duty_end": r["duty_end"],
            "late_minutes": r["late_minutes"] or 0,
            "worked_seconds": r["worked_seconds"] or 0,
            "earnings": float(r["earnings"] or 0),
            "state": state,
            "label": STATE_LABELS[state],
            "can_absent": (
                state in ("on_duty", "waiting", "upcoming")
                and view_date <= today_date
            )
        })

    scheduled_user_ids = {e["user_id"] for e in entries}

    day_off_users = [
        u["username"]
        for u in conn.execute("""
            SELECT id, username
            FROM users
            WHERE role != 'admin'
            AND enabled = 1
            ORDER BY username ASC
        """).fetchall()
        if u["id"] not in scheduled_user_ids
    ]

    counts = {
        "on_duty": sum(1 for e in entries if e["state"] == "on_duty"),
        "completed": sum(1 for e in entries if e["state"] == "completed"),
        "absent": sum(1 for e in entries if e["state"] == "absent"),
        "pending": sum(
            1 for e in entries
            if e["state"] in ("waiting", "upcoming", "no_record")
        )
    }

    # ---- week strip for the calendar (Mon-Sun of the viewed week)
    monday = view_date - timedelta(days=view_date.weekday())
    sunday = monday + timedelta(days=6)

    per_day = {
        r["duty_date"]: r["c"]
        for r in conn.execute("""
            SELECT ds.duty_date, COUNT(*) AS c

            FROM duty_schedules ds

            JOIN users u
                ON u.id = ds.user_id

            WHERE ds.enabled = 1
            AND u.role != 'admin'
            AND u.enabled = 1
            AND ds.duty_date BETWEEN ? AND ?

            GROUP BY ds.duty_date
        """, (monday.isoformat(), sunday.isoformat())).fetchall()
    }

    week_days = []

    for i in range(7):

        day = monday + timedelta(days=i)

        week_days.append({
            "date": day.isoformat(),
            "name": DAY_NAMES[i][:3],
            "num": day.day,
            "selected": day == view_date,
            "today": day == today_date,
            "count": per_day.get(day.isoformat(), 0)
        })

    if monday.month == sunday.month:
        month_label = f"{monday:%B %Y}"
    elif monday.year == sunday.year:
        month_label = f"{monday:%b} – {sunday:%b %Y}"
    else:
        month_label = f"{monday:%b %Y} – {sunday:%b %Y}"

    delta = (view_date - today_date).days

    if delta == 0:
        heading = "Today"
    elif delta == -1:
        heading = "Yesterday"
    elif delta == 1:
        heading = "Tomorrow"
    else:
        heading = DAY_NAMES[view_date.weekday()]

    return {
        "date": date_str,
        "heading": heading,
        "long_date": (
            f"{DAY_NAMES[view_date.weekday()]}, "
            f"{view_date:%B} {view_date.day}, {view_date.year}"
        ),
        "is_today": is_today,
        "prev_date": (view_date - timedelta(days=1)).isoformat(),
        "next_date": (view_date + timedelta(days=1)).isoformat(),
        "today_date": today_date.isoformat(),
        "entries": entries,
        "day_off_users": day_off_users,
        "counts": counts,
        "week_days": week_days,
        "month_label": month_label,
        "prev_week": (view_date - timedelta(days=7)).isoformat(),
        "next_week": (view_date + timedelta(days=7)).isoformat()
    }


@app.route(
    "/admin/duty/absent",
    methods=["POST"]
)
@admin_required
def admin_mark_absent():
    """
    Admin marks a duty as ABSENT.

    - ON DUTY user  -> forced to ABSENT, worked time and earnings
      become 0 (even if they already worked hours, e.g. in at
      5 PM, marked absent at 6 PM = no pay).
    - Not started   -> an ABSENT log is created.
    - COMPLETED     -> not allowed (already finished and paid).

    Once ABSENT, start_duty() refuses the user (existing check).
    """

    view_date = parse_view_date(request.form.get("date"))

    back = url_for("admin", date=view_date.isoformat()) + "#status"

    log_id = request.form.get("log_id", type=int)
    schedule_id = request.form.get("schedule_id", type=int)

    conn = get_db()

    log = None
    schedule = None

    if log_id:

        log = conn.execute("""
            SELECT *
            FROM duty_logs
            WHERE id = ?
        """, (log_id,)).fetchone()

    elif schedule_id:

        schedule = conn.execute("""
            SELECT ds.*, u.username
            FROM duty_schedules ds
            JOIN users u ON u.id = ds.user_id
            WHERE ds.id = ?
        """, (schedule_id,)).fetchone()

        if schedule:

            log = conn.execute("""
                SELECT *
                FROM duty_logs
                WHERE schedule_id = ?
                AND duty_date = ?
                LIMIT 1
            """, (schedule_id, view_date.isoformat())).fetchone()

    if not log and not schedule:

        conn.close()

        flash("Duty not found.", "error")

        return redirect(back)

    if log:

        if log["status"] == "ABSENT":

            conn.close()

            flash("Already marked ABSENT.", "error")

            return redirect(back)

        if log["status"] == "COMPLETED":

            conn.close()

            flash(
                "This duty is already completed. It cannot be marked absent.",
                "error"
            )

            return redirect(back)

        conn.execute("""
            UPDATE duty_logs

            SET
                status = 'ABSENT',
                duty_end = ?,
                late_minutes = 0,
                worked_seconds = 0,
                earnings = 0,
                late_deducted = 1

            WHERE id = ?
        """, (
            now_string() if log["duty_start"] else None,
            log["id"]
        ))

        username = log["username"]

    else:

        # No log yet: absent before/without starting.
        # Only today or past dates, and only on the schedule's date.
        if view_date > now().date():

            conn.close()

            flash("You cannot mark a future date as absent.", "error")

            return redirect(back)

        if schedule["duty_date"] != view_date.isoformat():

            conn.close()

            flash("That employee has no duty on this date.", "error")

            return redirect(back)

        conn.execute("""
            INSERT INTO duty_logs (
                user_id, schedule_id, username, duty_date,
                scheduled_time, scheduled_start, scheduled_end,
                duty_start, duty_end,
                late_minutes, worked_seconds, earnings,
                status, late_deducted
            )

            SELECT ?, ?, ?, ?, ?, ?, ?, NULL, NULL, 0, 0, 0, 'ABSENT', 1

            WHERE NOT EXISTS (
                SELECT 1 FROM duty_logs
                WHERE schedule_id = ? AND duty_date = ?
            )
        """, (
            schedule["user_id"], schedule["id"], schedule["username"],
            view_date.isoformat(),
            schedule["start_time"], schedule["start_time"],
            schedule["off_duty_time"],
            schedule["id"], view_date.isoformat()
        ))

        username = schedule["username"]

    conn.commit()

    conn.close()

    flash(
        f"{username} marked ABSENT. No earnings for this duty.",
        "success"
    )

    return redirect(back)


# =========================================================
# CREATE USER
# =========================================================

@app.route(
    "/admin/create-user",
    methods=["POST"]
)
@admin_required
def create_user():

    username = request.form.get(
        "username",
        ""
    ).strip()

    password = request.form.get(
        "password",
        ""
    )

    role_input = request.form.get("role", "employee").strip() or "employee"

    conn = get_db()

    role_row = conn.execute("""
        SELECT name, pay_rate
        FROM roles

        WHERE LOWER(name) = LOWER(?)
    """, (role_input,)).fetchone()

    conn.close()

    # admin accounts can't be created from the panel
    if not role_row or role_row["name"].lower() == "admin":

        flash(
            "Invalid role.",
            "error"
        )

        return redirect(url_for("admin"))

    role = role_row["name"]

    # blank pay rate = use the role's default (pesos per minute)
    pay_rate_raw = request.form.get(
        "pay_rate",
        ""
    ).strip()

    if pay_rate_raw == "":
        pay_rate_raw = str(role_row["pay_rate"] or 0)

    if not username or not password:

        flash(
            "Username and password are required.",
            "error"
        )

        return redirect(url_for("admin"))

    try:

        pay_rate = float(
            pay_rate_raw
        )

        if pay_rate < 0:
            raise ValueError

    except:

        flash(
            "Invalid pay rate.",
            "error"
        )

        return redirect(url_for("admin"))

    conn = get_db()

    existing = conn.execute("""
        SELECT id
        FROM users

        WHERE username = ?
    """, (username,)).fetchone()

    if existing:

        conn.close()

        flash(
            "Username already exists.",
            "error"
        )

        return redirect(url_for("admin"))

    conn.execute("""
        INSERT INTO users (
            username,
            password,
            role,
            enabled,
            duty_time,
            pay_rate,
            created_at
        )

        VALUES (?, ?, ?, ?, ?, ?, ?)
    """, (
        username,

        generate_password_hash(
            password
        ),

        role,

        1,

        "17:00",

        pay_rate,

        now_string()
    ))

    conn.commit()

    conn.close()

    flash(
        f"Employee '{username}' created successfully.",
        "success"
    )

    return redirect(url_for("admin"))


def wants_json():
    """True when the admin page saves in the background (no page reload)."""

    return request.headers.get("X-Requested-With") == "fetch"


def admin_reply(ok, message, anchor="users", **extra):
    """
    JSON for background saves, flash + redirect for normal form posts.
    `extra` are additional JSON fields (username, role, enabled...).
    """

    g.last_flash = (message, "success" if ok else "error")

    if wants_json():
        return jsonify(ok=ok, message=message, **extra), (200 if ok else 400)

    flash(message, "success" if ok else "error")

    return redirect(url_for("admin") + "#" + anchor)


# =========================================================
# SET SCHEDULE (calendar)
# Selected dates = duty days. Unselected dates = day off.
# Only today and future dates are changed.
# =========================================================

@app.route(
    "/admin/user/<int:user_id>/schedule/calendar",
    methods=["POST"]
)
@admin_required
def set_calendar_schedule(user_id):

    try:
        data = json.loads(request.form.get("days", "{}"))
    except ValueError:
        data = None

    if not isinstance(data, dict) or len(data) > 800:
        return admin_reply(False, "Invalid schedule data.")

    today_date = now().date()
    last_allowed = today_date + timedelta(days=400)

    new_days = {}

    for date_str, times in data.items():

        try:
            day = date.fromisoformat(date_str)
        except (TypeError, ValueError):
            return admin_reply(False, f"Invalid date: {date_str}")

        # the past is never changed
        if day < today_date:
            continue

        if day > last_allowed:
            return admin_reply(False, f"{date_str}: too far ahead.")

        if not isinstance(times, list) or len(times) != 2:
            return admin_reply(False, f"{date_str}: set both times.")

        start_time = str(times[0]).strip()[:5]
        off_time = str(times[1]).strip()[:5]

        start_minutes = time_to_minutes(start_time)
        end_minutes = time_to_minutes(off_time)

        if start_minutes is None or end_minutes is None:
            return admin_reply(False, f"{date_str}: set both times.")

        if start_minutes == end_minutes:
            return admin_reply(
                False,
                f"{date_str}: start and off-duty time cannot be the same."
            )

        new_days[day.isoformat()] = (start_time, off_time)

    conn = get_db()

    user = conn.execute("""
        SELECT id, username
        FROM users

        WHERE id = ?
    """, (user_id,)).fetchone()

    if not user:

        conn.close()

        return admin_reply(False, "Employee not found.")

    existing = {}

    for row in conn.execute("""
        SELECT id, duty_date, start_time, off_duty_time
        FROM duty_schedules

        WHERE user_id = ?

        AND duty_date >= ?

        ORDER BY id ASC
    """, (user_id, today_date.isoformat())).fetchall():

        existing.setdefault(row["duty_date"], []).append(row)

    def has_log(schedule_id):

        return conn.execute("""
            SELECT 1
            FROM duty_logs
            WHERE schedule_id = ?
            LIMIT 1
        """, (schedule_id,)).fetchone() is not None

    # Removed dates -> day off (rows that already have a log stay)
    for date_str, rows in existing.items():

        if date_str in new_days:
            continue

        for row in rows:

            if not has_log(row["id"]):

                conn.execute("""
                    DELETE FROM duty_schedules
                    WHERE id = ?
                """, (row["id"],))

    for date_str, (start_time, off_time) in new_days.items():

        rows = existing.get(date_str, [])

        if rows:

            # Update in place so today's log stays linked
            conn.execute("""
                UPDATE duty_schedules

                SET
                    start_time = ?,
                    off_duty_time = ?,
                    enabled = 1,
                    created_at = CASE
                        WHEN start_time != ? OR off_duty_time != ?
                        THEN ?
                        ELSE created_at
                    END

                WHERE id = ?
            """, (
                start_time, off_time,
                start_time, off_time, now_string(),
                rows[0]["id"]
            ))

            for extra in rows[1:]:

                if not has_log(extra["id"]):

                    conn.execute("""
                        DELETE FROM duty_schedules
                        WHERE id = ?
                    """, (extra["id"],))

        else:

            conn.execute("""
                INSERT INTO duty_schedules (
                    user_id,
                    start_time,
                    off_duty_time,
                    enabled,
                    created_at,
                    weekday,
                    duty_date
                )

                VALUES (?, ?, ?, ?, ?, ?, ?)
            """, (
                user_id,
                start_time,
                off_time,
                1,
                now_string(),
                date.fromisoformat(date_str).weekday(),
                date_str
            ))

    conn.commit()

    conn.close()

    return admin_reply(
        True,
        f"Schedule saved for {user['username']}."
    )


# =========================================================
# DELETE SCHEDULE
# =========================================================

@app.route(
    "/admin/schedule/<int:schedule_id>/delete",
    methods=["POST"]
)
@admin_required
def delete_schedule(schedule_id):

    conn = get_db()

    conn.execute("""
        DELETE FROM duty_schedules

        WHERE id = ?
    """, (schedule_id,))

    conn.commit()

    conn.close()

    flash(
        "Duty schedule deleted.",
        "success"
    )

    return redirect(url_for("admin"))


# =========================================================
# ENABLE SCHEDULE
# =========================================================

@app.route(
    "/admin/schedule/<int:schedule_id>/enable",
    methods=["POST"]
)
@admin_required
def enable_schedule(schedule_id):

    conn = get_db()

    conn.execute("""
        UPDATE duty_schedules

        SET enabled = 1

        WHERE id = ?
    """, (schedule_id,))

    conn.commit()

    conn.close()

    return redirect(url_for("admin"))


# =========================================================
# DISABLE SCHEDULE
# =========================================================

@app.route(
    "/admin/schedule/<int:schedule_id>/disable",
    methods=["POST"]
)
@admin_required
def disable_schedule(schedule_id):

    conn = get_db()

    conn.execute("""
        UPDATE duty_schedules

        SET enabled = 0

        WHERE id = ?
    """, (schedule_id,))

    conn.commit()

    conn.close()

    return redirect(url_for("admin"))


# =========================================================
# UPDATE PAY RATE
# =========================================================

@app.route(
    "/admin/user/<int:user_id>/settings",
    methods=["POST"]
)
@admin_required
def update_user_settings(user_id):

    try:

        pay_rate = float(
            request.form.get("pay_rate", "0")
        )

        if pay_rate < 0:
            raise ValueError

    except (TypeError, ValueError):

        return admin_reply(False, "Invalid pay rate.")

    conn = get_db()

    cursor = conn.execute("""
        UPDATE users

        SET pay_rate = ?

        WHERE id = ?
    """, (
        pay_rate,
        user_id
    ))

    conn.commit()

    found = cursor.rowcount > 0

    row = conn.execute(
        "SELECT username FROM users WHERE id = ?", (user_id,)
    ).fetchone()

    conn.close()

    if not found:
        return admin_reply(False, "Employee not found.")

    return admin_reply(
        True,
        f"Pay rate updated to ₱{format_rate(pay_rate)}/min for {row['username']}."
    )


# =========================================================
# ACCOUNT MANAGEMENT (employees + admins)
# =========================================================

MIN_PASSWORD_LENGTH = 6

def get_account(conn, user_id):

    return conn.execute("""
        SELECT id, username, role, enabled
        FROM users

        WHERE id = ?
    """, (user_id,)).fetchone()


# -------- edit username / password ------------------------

@app.route(
    "/admin/user/<int:user_id>/account",
    methods=["POST"]
)
@admin_required
def update_account(user_id):

    username = request.form.get("username", "").strip()
    password = request.form.get("password", "")

    if not username:
        return admin_reply(False, "Username cannot be empty.")

    if len(username) > 50:
        return admin_reply(False, "Username is too long (max 50).")

    if password and len(password) < MIN_PASSWORD_LENGTH:

        return admin_reply(
            False,
            f"Password must be at least {MIN_PASSWORD_LENGTH} characters."
        )

    if len(password) > 128:
        return admin_reply(False, "Password is too long (max 128).")

    conn = get_db()

    target = get_account(conn, user_id)

    if not target:

        conn.close()

        return admin_reply(False, "Account not found.")

    clash = conn.execute("""
        SELECT id
        FROM users

        WHERE LOWER(username) = LOWER(?)

        AND id != ?
    """, (username, user_id)).fetchone()

    if clash:

        conn.close()

        return admin_reply(False, "Username already exists.")

    if password:

        conn.execute("""
            UPDATE users
            SET username = ?, password = ?
            WHERE id = ?
        """, (username, generate_password_hash(password), user_id))

    else:

        conn.execute("""
            UPDATE users
            SET username = ?
            WHERE id = ?
        """, (username, user_id))

    # logs keep a copy of the name; keep it in sync
    conn.execute("""
        UPDATE duty_logs
        SET username = ?
        WHERE user_id = ?
    """, (username, user_id))

    conn.commit()

    conn.close()

    if user_id == session.get("user_id"):
        session["username"] = username

    message = "Account updated."

    if password:
        message = "Account updated. Password changed."

    return admin_reply(True, message, username=username)


# -------- change role -------------------------------------

@app.route(
    "/admin/user/<int:user_id>/role",
    methods=["POST"]
)
@admin_required
def change_role(user_id):

    role_input = request.form.get("role", "").strip()

    # also guarantees there is always at least one admin
    if user_id == session.get("user_id"):
        return admin_reply(False, "You cannot change your own role.")

    conn = get_db()

    role_row = conn.execute("""
        SELECT name
        FROM roles

        WHERE LOWER(name) = LOWER(?)
    """, (role_input,)).fetchone()

    if not role_row:

        conn.close()

        return admin_reply(False, "Invalid role.")

    role = role_row["name"]

    target = get_account(conn, user_id)

    if not target:

        conn.close()

        return admin_reply(False, "Account not found.")

    conn.execute("""
        UPDATE users
        SET role = ?
        WHERE id = ?
    """, (role, user_id))

    conn.commit()

    conn.close()

    return admin_reply(
        True,
        f"{target['username']} is now {role_label(role)}.",
        role=role,
        role_label=role_label(role)
    )


# -------- roles: create / default pay rate / delete -------------

ROLE_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 _.\-]{0,29}$")


def parse_rate(raw):
    """Pesos per minute. Returns a float, or None when invalid."""

    try:

        value = float(
            str(raw).replace(",", "").replace("₱", "").strip() or 0
        )

        if not math.isfinite(value) or value < 0 or value > 100000:
            raise ValueError

        return round(value, 4)

    except (TypeError, ValueError):

        return None


@app.route(
    "/admin/role/create",
    methods=["POST"]
)
@admin_required
def create_role():

    name = " ".join(request.form.get("name", "").split())

    rate = parse_rate(request.form.get("pay_rate", "0"))

    if not ROLE_NAME_RE.match(name):

        return admin_reply(
            False,
            "Role name must be 1-30 characters "
            "(letters, numbers, space, dot, dash, underscore).",
            anchor="roles"
        )

    if rate is None:
        return admin_reply(False, "Invalid pay rate.", anchor="roles")

    conn = get_db()

    clash = conn.execute("""
        SELECT id
        FROM roles

        WHERE LOWER(name) = LOWER(?)
    """, (name,)).fetchone()

    if clash:

        conn.close()

        return admin_reply(
            False,
            "That role already exists.",
            anchor="roles"
        )

    conn.execute("""
        INSERT INTO roles (name, pay_rate, created_at)
        VALUES (?, ?, ?)
    """, (name, rate, now_string()))

    conn.commit()

    conn.close()

    return admin_reply(
        True,
        f"Role '{name}' created.",
        anchor="roles"
    )


@app.route(
    "/admin/role/<int:role_id>/update",
    methods=["POST"]
)
@admin_required
def update_role(role_id):

    rate = parse_rate(request.form.get("pay_rate", "0"))

    if rate is None:
        return admin_reply(False, "Invalid pay rate.", anchor="roles")

    conn = get_db()

    role = conn.execute("""
        SELECT id, name
        FROM roles

        WHERE id = ?
    """, (role_id,)).fetchone()

    if not role or role["name"].lower() == "admin":

        conn.close()

        return admin_reply(False, "Role not found.", anchor="roles")

    conn.execute("""
        UPDATE roles
        SET pay_rate = ?
        WHERE id = ?
    """, (rate, role_id))

    conn.commit()

    conn.close()

    return admin_reply(
        True,
        f"Default pay rate for {role_label(role['name'])} updated. "
        "It applies to newly added employees only.",
        anchor="roles"
    )


@app.route(
    "/admin/role/<int:role_id>/delete",
    methods=["POST"]
)
@admin_required
def delete_role(role_id):

    conn = get_db()

    role = conn.execute("""
        SELECT id, name
        FROM roles

        WHERE id = ?
    """, (role_id,)).fetchone()

    if not role:

        conn.close()

        return admin_reply(False, "Role not found.", anchor="roles")

    if role["name"].lower() in BUILTIN_ROLES:

        conn.close()

        return admin_reply(
            False,
            "Built-in roles cannot be deleted.",
            anchor="roles"
        )

    moved = conn.execute("""
        UPDATE users
        SET role = 'employee'
        WHERE LOWER(role) = LOWER(?)
    """, (role["name"],)).rowcount

    conn.execute("DELETE FROM roles WHERE id = ?", (role_id,))

    conn.commit()

    conn.close()

    message = f"Role '{role['name']}' deleted."

    if moved:
        message += f" {moved} account(s) moved to Employee."

    return admin_reply(True, message, anchor="roles")


# -------- delete ------------------------------------------

@app.route(
    "/admin/user/<int:user_id>/delete",
    methods=["POST"]
)
@admin_required
def delete_user(user_id):

    if user_id == session.get("user_id"):
        return admin_reply(False, "You cannot delete yourself.")

    conn = get_db()

    target = get_account(conn, user_id)

    if not target:

        conn.close()

        return admin_reply(False, "Account not found.")

    if target["role"] == "admin":

        conn.close()

        return admin_reply(
            False,
            "Admin accounts cannot be deleted. Change the role to Employee first."
        )

    conn.execute("DELETE FROM duty_logs WHERE user_id = ?", (user_id,))

    conn.execute("DELETE FROM duty_schedules WHERE user_id = ?", (user_id,))

    conn.execute("DELETE FROM debts WHERE user_id = ?", (user_id,))

    conn.execute("DELETE FROM users WHERE id = ?", (user_id,))

    conn.commit()

    conn.close()

    return admin_reply(
        True,
        f"{target['username']} removed.",
        deleted=user_id
    )


# =========================================================
# EMPLOYEE DEBTS
# =========================================================

DEBT_MAX_AMOUNT = 10_000_000


@app.route(
    "/admin/debt/add",
    methods=["POST"]
)
@admin_required
def add_debt():

    user_id = request.form.get("user_id", type=int)

    reason = request.form.get("reason", "").strip()

    due_date = request.form.get("due_date", "").strip()

    notes = request.form.get("notes", "").strip()

    # allow "1,000.00" and "₱1,000.00"
    amount_raw = (
        request.form.get("amount", "")
        .replace(",", "")
        .replace("₱", "")
        .strip()
    )

    if not user_id:
        return admin_reply(False, "Select an employee.", anchor="debts")

    try:

        amount = round(float(amount_raw), 2)

        if not math.isfinite(amount) or amount <= 0:
            raise ValueError

        if amount > DEBT_MAX_AMOUNT:
            raise ValueError

    except (TypeError, ValueError):

        return admin_reply(False, "Invalid debt amount.", anchor="debts")

    if not reason:
        return admin_reply(False, "Reason is required.", anchor="debts")

    if len(reason) > 200:
        return admin_reply(
            False, "Reason is too long (max 200).", anchor="debts"
        )

    if len(notes) > 1000:
        return admin_reply(
            False, "Notes are too long (max 1000).", anchor="debts"
        )

    try:
        datetime.strptime(due_date, "%Y-%m-%d")
    except ValueError:
        return admin_reply(False, "Invalid due date.", anchor="debts")

    conn = get_db()

    employee = conn.execute("""
        SELECT id, username, role
        FROM users

        WHERE id = ?
    """, (user_id,)).fetchone()

    if not employee or employee["role"] == "admin":

        conn.close()

        return admin_reply(False, "Employee not found.", anchor="debts")

    conn.execute("""
        INSERT INTO debts (
            user_id,
            amount,
            reason,
            due_date,
            notes,
            status,
            created_at
        )

        VALUES (?, ?, ?, ?, ?, 'UNPAID', ?)
    """, (
        user_id,
        amount,
        reason,
        due_date,
        notes,
        now_string()
    ))

    conn.commit()

    conn.close()

    return admin_reply(
        True,
        f"Debt of ₱{amount:,.2f} added for {employee['username']}.",
        anchor="debts"
    )


@app.route(
    "/admin/debt/<int:debt_id>/paid",
    methods=["POST"]
)
@admin_required
def mark_debt_paid(debt_id):

    conn = get_db()

    cursor = conn.execute("""
        UPDATE debts

        SET
            status = 'PAID',
            paid_at = ?

        WHERE id = ?

        AND status = 'UNPAID'
    """, (now_string(), debt_id))

    conn.commit()

    changed = cursor.rowcount > 0

    conn.close()

    if not changed:

        return admin_reply(
            False,
            "Debt not found or already paid.",
            anchor="debts"
        )

    return admin_reply(True, "Debt marked as PAID.", anchor="debts")


@app.route(
    "/admin/debt/<int:debt_id>/delete",
    methods=["POST"]
)
@admin_required
def delete_debt(debt_id):

    conn = get_db()

    cursor = conn.execute("""
        DELETE FROM debts

        WHERE id = ?
    """, (debt_id,))

    conn.commit()

    changed = cursor.rowcount > 0

    conn.close()

    if not changed:
        return admin_reply(False, "Debt not found.", anchor="debts")

    return admin_reply(True, "Debt deleted.", anchor="debts")


# =========================================================
# ADMIN USER ACTIVITY
# =========================================================

@app.route(
    "/admin/user/<int:user_id>/logs"
)
@admin_required
def admin_user_logs(user_id):
    """Old per-employee page: logs + photos are in the admin Duty Logs tab."""

    return redirect(url_for("admin") + "#dutylogs")


# =========================================================
# PAYSLIP
#
# The cut-off is any From - To date range. By default it is the
# current half of the month (1st-15th or 16th-end); the admin or the
# employee can type other dates (example: Oct 10 to Oct 25).
#
# Payslips are computed from duty_logs, so they always match the
# Earnings tab and the Duty Logs. Nothing is stored.
# =========================================================

CUTOFF_SPLIT_DAY = 15

CUTOFF_MAX_DAYS = 93


def range_label(start, end):
    """'Oct 10 – 25, 2026' / 'Sep 28 – Oct 12, 2026' / across years."""

    if start.year != end.year:
        return (
            f"{start:%b} {start.day}, {start.year} – "
            f"{end:%b} {end.day}, {end.year}"
        )

    if start.month != end.month:
        return f"{start:%b} {start.day} – {end:%b} {end.day}, {end.year}"

    return f"{start:%b} {start.day} – {end.day}, {end.year}"


def build_cutoff(start, end):

    return {
        "start": start,
        "end": end,
        "start_iso": start.isoformat(),
        "end_iso": end.isoformat(),
        "label": range_label(start, end),
        "in_progress": end >= now().date()
    }


def normal_cutoff(year, month, half):
    """1st-15th (half 1) or 16th-end of month (half 2)."""

    last_day = calendar.monthrange(year, month)[1]

    if half == 1:
        return build_cutoff(
            date(year, month, 1),
            date(year, month, CUTOFF_SPLIT_DAY)
        )

    return build_cutoff(
        date(year, month, CUTOFF_SPLIT_DAY + 1),
        date(year, month, last_day)
    )


def current_cutoff():

    day = now().date()

    return normal_cutoff(
        day.year,
        day.month,
        1 if day.day <= CUTOFF_SPLIT_DAY else 2
    )


def parse_cutoff(start_raw, end_raw):
    """
    From/To dates -> cut-off dict.
    Missing or invalid -> the current half of the month.
    """

    try:

        start = date.fromisoformat(str(start_raw or "").strip())
        end = date.fromisoformat(str(end_raw or "").strip())

    except ValueError:

        return current_cutoff()

    if start > end:
        start, end = end, start

    if (
        not (2000 <= start.year <= 2100 and 2000 <= end.year <= 2100)
        or (end - start).days + 1 > CUTOFF_MAX_DAYS
    ):
        return current_cutoff()

    return build_cutoff(start, end)


def request_cutoff():

    return parse_cutoff(
        request.args.get("start"),
        request.args.get("end")
    )


def cutoff_presets(count=12):
    """Quick picks: the current + previous 1st-15th / 16th-end periods."""

    day = now().date()

    year, month = day.year, day.month
    half = 1 if day.day <= CUTOFF_SPLIT_DAY else 2

    presets = []

    for _ in range(count):

        presets.append(normal_cutoff(year, month, half))

        if half == 2:
            half = 1
        else:
            half = 2
            month -= 1

            if month == 0:
                month = 12
                year -= 1

    return presets


def build_payslip(conn, user, cutoff):
    """
    Payslip of ONE employee for ONE cut-off.

    Per COMPLETED duty:
        gross     = worked minutes x rate
        late      = late minutes   x rate     (deduction)
        net       = what the duty log already paid (gross - late)

    The rate is taken from the log itself (earnings / paid minutes) so
    a pay-rate change later never rewrites an old payslip. ABSENT duties
    earn 0. A duty still ON DUTY is listed but not counted until it ends.
    """

    logs = conn.execute("""
        SELECT *
        FROM duty_logs

        WHERE user_id = ?

        AND duty_date BETWEEN ? AND ?

        ORDER BY duty_date ASC, scheduled_start ASC, id ASC
    """, (user["id"], cutoff["start_iso"], cutoff["end_iso"])).fetchall()

    fallback_rate = float(user["pay_rate"] or 0)

    records = []

    totals = {
        "days_worked": 0,
        "days_absent": 0,
        "worked_seconds": 0,
        "late_minutes": 0,
        "gross": 0.0,
        "late_deduction": 0.0,
        "net": 0.0,
        "ongoing": 0
    }

    for log in logs:

        status = log["status"]

        worked_seconds = int(log["worked_seconds"] or 0)
        late_minutes = int(log["late_minutes"] or 0)

        gross = late_ded = net = 0.0

        if status == "COMPLETED":

            net = round(float(log["earnings"] or 0), 2)

            worked_minutes = worked_seconds // 60
            paid_minutes = worked_minutes - late_minutes

            if paid_minutes > 0 and net > 0:
                rate = net / paid_minutes
            else:
                rate = fallback_rate

            late_ded = round(
                max(0.0, worked_minutes * rate - net), 2
            )

            gross = round(net + late_ded, 2)

            totals["days_worked"] += 1
            totals["worked_seconds"] += worked_seconds
            totals["late_minutes"] += late_minutes
            totals["gross"] += gross
            totals["late_deduction"] += late_ded
            totals["net"] += net

        elif status == "ABSENT":

            totals["days_absent"] += 1

        else:

            totals["ongoing"] += 1

        try:
            day = date.fromisoformat(log["duty_date"])
            date_label = f"{day:%b} {day.day} ({day:%a})"
        except ValueError:
            date_label = log["duty_date"]

        records.append({
            "date_label": date_label,
            "status": status,
            "scheduled": (
                f"{format_time_12h(log['scheduled_start'])} → "
                f"{format_time_12h(log['scheduled_end'])}"
            ),
            "duty_start": log["duty_start"],
            "duty_end": log["duty_end"],
            "late_minutes": late_minutes,
            "worked_seconds": worked_seconds,
            "gross": gross,
            "late_deduction": late_ded,
            "net": net
        })

    # debts are shown for information only (not deducted automatically)
    debt_rows = conn.execute("""
        SELECT amount, due_date
        FROM debts

        WHERE user_id = ?
        AND status = 'UNPAID'
    """, (user["id"],)).fetchall()

    today_str = today()

    for key in ("gross", "late_deduction", "net"):
        totals[key] = round(totals[key], 2)

    return {
        "user": user,
        "cutoff": cutoff,
        "records": records,
        "totals": totals,
        "debt_outstanding": sum(d["amount"] for d in debt_rows),
        "debt_overdue_count": sum(
            1 for d in debt_rows if d["due_date"] < today_str
        )
    }


def employees_slips(conn, cutoff):
    """Payslips of every employee (disabled ones only if they have records)."""

    users = conn.execute("""
        SELECT *
        FROM users

        WHERE role != 'admin'

        ORDER BY LOWER(username) ASC
    """).fetchall()

    slips = [build_payslip(conn, u, cutoff) for u in users]

    return [
        s for s in slips
        if s["user"]["enabled"] or s["records"]
    ]


def payslip_page(slips, cutoff, **extra):
    """Shared render for the payslip page (admin only)."""

    return render_template(
        "payslip.html",
        slips=slips,
        cutoff=cutoff,
        presets=cutoff_presets(),
        generated_at=now_string(),
        format_duration=format_duration,
        **extra
    )


# -------- admin: the list lives in the Admin Panel > Payslips tab --

@app.route("/admin/payslips")
@admin_required
def admin_payslips():
    """Old address: send to the Payslips tab of the admin panel."""

    return redirect(
        url_for(
            "admin",
            ps_start=request.args.get("start"),
            ps_end=request.args.get("end"),
            _anchor="payslips"
        )
    )


# -------- admin: one employee (or print everyone) --------------

@app.route("/admin/payslip/<int:user_id>")
@admin_required
def admin_payslip(user_id):

    cutoff = request_cutoff()

    sync_absent_logs(user_id)

    conn = get_db()

    user = conn.execute(
        "SELECT * FROM users WHERE id = ? AND role != 'admin'",
        (user_id,)
    ).fetchone()

    if not user:

        conn.close()

        flash("Employee not found.", "error")

        return redirect(url_for("admin", _anchor="payslips"))

    slip = build_payslip(conn, user, cutoff)

    conn.close()

    return payslip_page(
        [slip],
        cutoff,
        admin_view=True,
        form_action=url_for("admin_payslip", user_id=user_id)
    )


@app.route("/admin/payslips/print")
@admin_required
def admin_payslips_print():
    """Every employee's payslip for the cut-off, one per printed page."""

    cutoff = request_cutoff()

    sync_absent_logs()

    conn = get_db()

    slips = employees_slips(conn, cutoff)

    conn.close()

    return payslip_page(
        slips,
        cutoff,
        admin_view=True,
        form_action=url_for("admin_payslips_print")
    )


# =========================================================
# START SERVER
# =========================================================

if __name__ == "__main__":

    init_db()

    # Localhost only (test mode): open http://127.0.0.1:5000
    # Only this computer can reach it. Use host="0.0.0.0" to allow
    # other devices on your network.
    app.run(
        host="127.0.0.1",
        port=5000,
        debug=True
    )
