"""
Слой работы с БД (SQLite). Хранит точки, категории, позиции (каталог) и
историю введённых остатков.
"""
import sqlite3
from contextlib import contextmanager
from datetime import datetime

DB_PATH = "order_bot.db"

SCHEMA = """
CREATE TABLE IF NOT EXISTS points (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT UNIQUE NOT NULL,
    chat_id INTEGER,               -- куда слать готовый заказ; NULL = слать в чат, откуда пришёл запрос
    header_template TEXT NOT NULL DEFAULT '📦 Заказ — {point}, {date}'
);

CREATE TABLE IF NOT EXISTS categories (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    point_id INTEGER NOT NULL REFERENCES points(id) ON DELETE CASCADE,
    name TEXT NOT NULL,
    sort_order INTEGER NOT NULL DEFAULT 0,
    UNIQUE(point_id, name)
);

CREATE TABLE IF NOT EXISTS products (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    point_id INTEGER NOT NULL REFERENCES points(id) ON DELETE CASCADE,
    category_id INTEGER NOT NULL REFERENCES categories(id) ON DELETE CASCADE,
    name TEXT NOT NULL,
    unit TEXT NOT NULL DEFAULT 'шт',
    min_qty REAL NOT NULL,
    max_qty REAL NOT NULL,
    sort_order INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS entries (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    product_id INTEGER NOT NULL REFERENCES products(id) ON DELETE CASCADE,
    fact_qty REAL NOT NULL,
    employee TEXT,
    telegram_user_id INTEGER,
    entered_at TEXT NOT NULL
);
"""


@contextmanager
def get_conn():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def init_db():
    with get_conn() as conn:
        conn.executescript(SCHEMA)


# ---------- Points ----------

def upsert_point(name: str, chat_id, header_template: str):
    with get_conn() as conn:
        cur = conn.execute("SELECT id FROM points WHERE name = ?", (name,))
        row = cur.fetchone()
        if row:
            conn.execute(
                "UPDATE points SET chat_id = ?, header_template = ? WHERE id = ?",
                (chat_id, header_template, row["id"]),
            )
            return row["id"]
        cur = conn.execute(
            "INSERT INTO points (name, chat_id, header_template) VALUES (?, ?, ?)",
            (name, chat_id, header_template),
        )
        return cur.lastrowid


def list_points():
    with get_conn() as conn:
        return conn.execute("SELECT * FROM points ORDER BY name").fetchall()


def get_point(point_id: int):
    with get_conn() as conn:
        return conn.execute("SELECT * FROM points WHERE id = ?", (point_id,)).fetchone()


def create_point(name: str):
    with get_conn() as conn:
        cur = conn.execute("INSERT INTO points (name) VALUES (?)", (name,))
        return cur.lastrowid


# ---------- Categories ----------

def clear_categories_for_point(point_id: int):
    with get_conn() as conn:
        conn.execute("DELETE FROM categories WHERE point_id = ?", (point_id,))


def list_categories(point_id: int):
    with get_conn() as conn:
        return conn.execute(
            "SELECT * FROM categories WHERE point_id = ? ORDER BY sort_order, name",
            (point_id,),
        ).fetchall()


def get_category(category_id: int):
    with get_conn() as conn:
        return conn.execute("SELECT * FROM categories WHERE id = ?", (category_id,)).fetchone()


def get_or_create_category(point_id: int, name: str):
    name = name.strip()
    with get_conn() as conn:
        row = conn.execute(
            "SELECT * FROM categories WHERE point_id = ? AND name = ?", (point_id, name)
        ).fetchone()
        if row:
            return row["id"]
        max_sort = conn.execute(
            "SELECT COALESCE(MAX(sort_order), -1) m FROM categories WHERE point_id = ?", (point_id,)
        ).fetchone()["m"]
        cur = conn.execute(
            "INSERT INTO categories (point_id, name, sort_order) VALUES (?, ?, ?)",
            (point_id, name, max_sort + 1),
        )
        return cur.lastrowid


def rename_category(category_id: int, new_name: str):
    with get_conn() as conn:
        conn.execute("UPDATE categories SET name = ? WHERE id = ?", (new_name.strip(), category_id))


def delete_category(category_id: int):
    with get_conn() as conn:
        conn.execute("DELETE FROM categories WHERE id = ?", (category_id,))


# ---------- Products ----------

def clear_products_for_point(point_id: int):
    with get_conn() as conn:
        conn.execute("DELETE FROM products WHERE point_id = ?", (point_id,))


def add_product(point_id, category_id, name, unit, min_qty, max_qty, sort_order=None):
    with get_conn() as conn:
        if sort_order is None:
            max_sort = conn.execute(
                "SELECT COALESCE(MAX(sort_order), -1) m FROM products WHERE category_id = ?",
                (category_id,),
            ).fetchone()["m"]
            sort_order = max_sort + 1
        cur = conn.execute(
            """INSERT INTO products (point_id, category_id, name, unit, min_qty, max_qty, sort_order)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (point_id, category_id, name.strip(), unit.strip() or "шт", min_qty, max_qty, sort_order),
        )
        return cur.lastrowid


def products_for(point_id: int, category_id=None):
    with get_conn() as conn:
        if category_id and category_id != "ALL":
            rows = conn.execute(
                """SELECT p.*, c.name AS category_name FROM products p
                   JOIN categories c ON c.id = p.category_id
                   WHERE p.point_id = ? AND p.category_id = ?
                   ORDER BY p.sort_order, p.name""",
                (point_id, category_id),
            )
        else:
            rows = conn.execute(
                """SELECT p.*, c.name AS category_name FROM products p
                   JOIN categories c ON c.id = p.category_id
                   WHERE p.point_id = ?
                   ORDER BY c.sort_order, p.sort_order, p.name""",
                (point_id,),
            )
        return rows.fetchall()


def get_product(product_id: int):
    with get_conn() as conn:
        return conn.execute(
            """SELECT p.*, c.name AS category_name FROM products p
               JOIN categories c ON c.id = p.category_id
               WHERE p.id = ?""",
            (product_id,),
        ).fetchone()


def update_product_minmax(product_id: int, min_qty: float, max_qty: float):
    with get_conn() as conn:
        conn.execute(
            "UPDATE products SET min_qty = ?, max_qty = ? WHERE id = ?",
            (min_qty, max_qty, product_id),
        )


def update_product_field(product_id: int, field: str, value):
    assert field in ("name", "unit")
    with get_conn() as conn:
        conn.execute(f"UPDATE products SET {field} = ? WHERE id = ?", (value, product_id))


def delete_product(product_id: int):
    with get_conn() as conn:
        conn.execute("DELETE FROM products WHERE id = ?", (product_id,))


# ---------- Entries (history) ----------

def save_entry(product_id: int, fact_qty: float, employee: str, telegram_user_id: int):
    with get_conn() as conn:
        conn.execute(
            """INSERT INTO entries (product_id, fact_qty, employee, telegram_user_id, entered_at)
               VALUES (?, ?, ?, ?, ?)""",
            (product_id, fact_qty, employee, telegram_user_id, datetime.now().isoformat(timespec="seconds")),
        )
