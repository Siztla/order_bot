"""
Слой работы с БД (SQLite). Хранит точки, категории, позиции (каталог) и
историю введённых остатков.
"""
import os
import sqlite3
from contextlib import contextmanager
from datetime import datetime

# Путь к файлу базы. Порядок:
# 1) DB_PATH, если задан явно — полный путь к файлу базы.
# 2) Иначе DATA_DIR/order_bot.db — так рекомендует bothost.ru: папка DATA_DIR
#    (по умолчанию /app/data) сохраняется между перезапусками/деплоями.
# 3) Если ни то ни другое не задано (например, при локальном запуске на Mac) —
#    просто "order_bot.db" рядом с кодом, как раньше.
DATA_DIR = os.environ.get("DATA_DIR")
if os.environ.get("DB_PATH"):
    DB_PATH = os.environ["DB_PATH"]
elif DATA_DIR:
    DB_PATH = os.path.join(DATA_DIR, "order_bot.db")
else:
    DB_PATH = "order_bot.db"

SCHEMA = """
CREATE TABLE IF NOT EXISTS points (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT UNIQUE NOT NULL,
    chat_id INTEGER,               -- куда слать готовый заказ; NULL = слать в чат, откуда пришёл запрос
    header_template TEXT NOT NULL DEFAULT '📦 Заказ — {point}, {date}',
    reminder_time TEXT,            -- 'ЧЧ:ММ' по времени сервера, NULL = напоминание выключено
    reminder_chat_id INTEGER       -- NULL = слать туда же, куда уходит заказ (chat_id)
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

CREATE TABLE IF NOT EXISTS reminder_log (
    point_id INTEGER NOT NULL REFERENCES points(id) ON DELETE CASCADE,
    sent_date TEXT NOT NULL,       -- 'YYYY-MM-DD'
    PRIMARY KEY (point_id, sent_date)
);

CREATE TABLE IF NOT EXISTS recipes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    category TEXT,                 -- произвольная группа для навигации, например "Супы"; может быть NULL
    method TEXT,                   -- способ приготовления, пошагово, свободный текст
    photo_file_id TEXT,            -- Telegram file_id фото готового блюда; может быть NULL
    sort_order INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS recipe_ingredients (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    recipe_id INTEGER NOT NULL REFERENCES recipes(id) ON DELETE CASCADE,
    name TEXT NOT NULL,
    amount REAL,                   -- может быть NULL, если количество не числовое ("по вкусу")
    unit TEXT,
    sort_order INTEGER NOT NULL DEFAULT 0
);
"""

MIGRATIONS = [
    ("points", "reminder_time", "TEXT"),
    ("points", "reminder_chat_id", "INTEGER"),
]


def _migrate(conn):
    """Добавляет недостающие колонки в уже существующую базу (созданную старой версией схемы)."""
    for table, column, coltype in MIGRATIONS:
        existing = {row["name"] for row in conn.execute(f"PRAGMA table_info({table})")}
        if column not in existing:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {coltype}")


@contextmanager
def get_conn():
    db_dir = os.path.dirname(DB_PATH)
    if db_dir:
        os.makedirs(db_dir, exist_ok=True)
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
        _migrate(conn)


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


def delete_point(point_id: int):
    """Удаляет точку целиком — категории и позиции удалятся каскадно (ON DELETE CASCADE)."""
    with get_conn() as conn:
        conn.execute("DELETE FROM points WHERE id = ?", (point_id,))


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


def last_facts_for(product_ids):
    """Возвращает {product_id: последний_введённый_факт} по самой свежей записи в entries.
    Для позиций без истории ключ отсутствует."""
    if not product_ids:
        return {}
    placeholders = ",".join("?" for _ in product_ids)
    with get_conn() as conn:
        rows = conn.execute(
            f"""SELECT e.product_id, e.fact_qty
                FROM entries e
                JOIN (
                    SELECT product_id, MAX(entered_at) AS latest
                    FROM entries WHERE product_id IN ({placeholders})
                    GROUP BY product_id
                ) last ON last.product_id = e.product_id AND last.latest = e.entered_at""",
            list(product_ids),
        ).fetchall()
        return {row["product_id"]: row["fact_qty"] for row in rows}


# ---------- Напоминания ----------

def set_reminder(point_id: int, reminder_time, reminder_chat_id=None):
    """reminder_time: строка 'ЧЧ:ММ' или None, чтобы выключить напоминание для точки."""
    with get_conn() as conn:
        conn.execute(
            "UPDATE points SET reminder_time = ?, reminder_chat_id = ? WHERE id = ?",
            (reminder_time, reminder_chat_id, point_id),
        )


def points_with_reminders():
    with get_conn() as conn:
        return conn.execute(
            "SELECT * FROM points WHERE reminder_time IS NOT NULL"
        ).fetchall()


def has_entries_today(point_id: int, today: str) -> bool:
    """today: 'YYYY-MM-DD'. Проверяет, заполнял ли кто-то остатки по этой точке сегодня."""
    with get_conn() as conn:
        row = conn.execute(
            """SELECT COUNT(*) c FROM entries e
               JOIN products p ON p.id = e.product_id
               WHERE p.point_id = ? AND substr(e.entered_at, 1, 10) = ?""",
            (point_id, today),
        ).fetchone()
        return row["c"] > 0


def was_reminder_sent(point_id: int, date_str: str) -> bool:
    with get_conn() as conn:
        row = conn.execute(
            "SELECT 1 FROM reminder_log WHERE point_id = ? AND sent_date = ?",
            (point_id, date_str),
        ).fetchone()
        return row is not None


def mark_reminder_sent(point_id: int, date_str: str):
    with get_conn() as conn:
        conn.execute(
            "INSERT OR IGNORE INTO reminder_log (point_id, sent_date) VALUES (?, ?)",
            (point_id, date_str),
        )


# ---------- Тех.карты (рецепты) ----------

def create_recipe(name: str, category: str, method: str) -> int:
    with get_conn() as conn:
        max_sort = conn.execute("SELECT COALESCE(MAX(sort_order), -1) m FROM recipes").fetchone()["m"]
        cur = conn.execute(
            "INSERT INTO recipes (name, category, method, sort_order) VALUES (?, ?, ?, ?)",
            (name.strip(), (category or "").strip() or None, method.strip(), max_sort + 1),
        )
        return cur.lastrowid


def add_recipe_ingredient(recipe_id: int, name: str, amount, unit: str, sort_order: int):
    with get_conn() as conn:
        conn.execute(
            """INSERT INTO recipe_ingredients (recipe_id, name, amount, unit, sort_order)
               VALUES (?, ?, ?, ?, ?)""",
            (recipe_id, name.strip(), amount, (unit or "").strip() or None, sort_order),
        )


def list_recipes():
    with get_conn() as conn:
        return conn.execute("SELECT * FROM recipes ORDER BY sort_order, name").fetchall()


def get_recipe(recipe_id: int):
    with get_conn() as conn:
        return conn.execute("SELECT * FROM recipes WHERE id = ?", (recipe_id,)).fetchone()


def list_recipe_ingredients(recipe_id: int):
    with get_conn() as conn:
        return conn.execute(
            "SELECT * FROM recipe_ingredients WHERE recipe_id = ? ORDER BY sort_order, name",
            (recipe_id,),
        ).fetchall()


def set_recipe_photo(recipe_id: int, file_id):
    with get_conn() as conn:
        conn.execute("UPDATE recipes SET photo_file_id = ? WHERE id = ?", (file_id, recipe_id))


def delete_recipe(recipe_id: int):
    with get_conn() as conn:
        conn.execute("DELETE FROM recipes WHERE id = ?", (recipe_id,))
