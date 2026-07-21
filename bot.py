"""
Telegram-бот заказов «по принципу таблицы остатков».

Логика 1-в-1 повторяет Excel-таблицу:
    Заказ = Макс. остаток − Факт
    Статус:  🟢 в норме (мин <= факт <= макс)
             🔴 ниже минимума — нужно заказать
             🔵 выше максимума — затоварено

Две части бота:
  1. Заказы (/start) — сотрудник выбирает точку и категорию, вводит факт
     списком, бот считает заказ и статус и отправляет готовое сообщение.
  2. Админ-панель (/admin) — доступна только пользователям из ADMIN_IDS.
     Позволяет прямо в Telegram создавать/удалять категории и позиции,
     менять мин/макс остаток и названия — без правки Excel.

Запуск:
    export BOT_TOKEN=xxxxx:yyyyy
    export ADMIN_IDS=123456789,987654321   # Telegram user id через запятую
    python bot.py
"""
import asyncio
import os
import re
from datetime import datetime

from aiogram import Bot, Dispatcher, F, Router
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (
    BotCommand,
    BotCommandScopeChat,
    BotCommandScopeDefault,
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    Message,
    ReplyKeyboardMarkup,
)

import db

router = Router()

ADMIN_IDS = {int(x) for x in os.environ.get("ADMIN_IDS", "").split(",") if x.strip().isdigit()}


def is_admin(user_id: int) -> bool:
    return not ADMIN_IDS or user_id in ADMIN_IDS


START_TRIGGERS = {
    "старт", "start", "заказ", "заказы", "остатки", "меню", "menu",
    "привет", "здравствуйте", "начать", "заполнить", "заполнить остатки",
    "📦 заполнить остатки",
}
ADMIN_TRIGGERS = {"админ", "admin", "панель", "админка", "админ-панель", "⚙️ админ-панель"}


def main_reply_keyboard(user_id: int) -> ReplyKeyboardMarkup:
    rows = [[KeyboardButton(text="📦 Заполнить остатки")]]
    if is_admin(user_id):
        rows.append([KeyboardButton(text="⚙️ Админ-панель")])
    return ReplyKeyboardMarkup(keyboard=rows, resize_keyboard=True)


# ============================================================================
#  ЗАКАЗЫ
# ============================================================================


class OrderForm(StatesGroup):
    choosing_point = State()
    choosing_category = State()
    filling = State()
    reviewing = State()
    editing_single = State()


def status_icon(fact: float, min_qty: float, max_qty: float) -> str:
    if fact < min_qty:
        return "🔴"
    if fact > max_qty:
        return "🔵"
    return "🟢"


def nav_row():
    return [
        InlineKeyboardButton(text="⬅️ Категории", callback_data="nav:back_categories"),
        InlineKeyboardButton(text="🏠 Точки", callback_data="back:points"),
    ]


def points_keyboard(prefix="point"):
    points = db.list_points()
    kb = [[InlineKeyboardButton(text=p["name"], callback_data=f"{prefix}:{p['id']}")] for p in points]
    return InlineKeyboardMarkup(inline_keyboard=kb)


def categories_with_products(point_id: int):
    return [c for c in db.list_categories(point_id) if db.products_for(point_id, c["id"])]


def categories_keyboard(categories):
    kb = [[InlineKeyboardButton(text="📦 Все категории", callback_data="cat:ALL")]]
    for c in categories:
        kb.append([InlineKeyboardButton(text=c["name"], callback_data=f"cat:{c['id']}")])
    kb.append([InlineKeyboardButton(text="🏠 Сменить точку", callback_data="back:points")])
    return InlineKeyboardMarkup(inline_keyboard=kb)


def category_form_keyboard():
    return InlineKeyboardMarkup(inline_keyboard=[nav_row()])


def review_keyboard():
    kb = [
        [InlineKeyboardButton(text="✅ Отправить", callback_data="review:send")],
        [InlineKeyboardButton(text="🔧 Исправить одну позицию", callback_data="review:fix_one")],
        [InlineKeyboardButton(text="✏️ Заполнить заново", callback_data="review:redo")],
        nav_row(),
        [InlineKeyboardButton(text="❌ Отмена", callback_data="review:cancel")],
    ]
    return InlineKeyboardMarkup(inline_keyboard=kb)


def fix_one_keyboard(product_ids, answers):
    kb = []
    for pid in product_ids:
        p = db.get_product(pid)
        fact = answers[str(pid)]
        kb.append([InlineKeyboardButton(text=f"✏️ {p['name']} (сейчас {fact:g})", callback_data=f"fixitem:{pid}")])
    kb.append([InlineKeyboardButton(text="⬅️ Назад к сводке", callback_data="review:cancel_fix")])
    return InlineKeyboardMarkup(inline_keyboard=kb)


@router.callback_query(F.data == "nav:back_categories")
async def nav_back_categories(cb: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    point_id = data.get("point_id")
    if not point_id:
        await cb.answer("Начните заново — /start", show_alert=True)
        return
    point = db.get_point(point_id)
    categories = categories_with_products(point_id)
    await cb.message.answer(
        f"Точка: <b>{point['name']}</b>\nЧто заполняем — все позиции или одну категорию?",
        reply_markup=categories_keyboard(categories),
        parse_mode="HTML",
    )
    await state.set_state(OrderForm.choosing_category)
    await cb.answer()


INTRO_TEXT = (
    "👋 <b>Как это работает</b>\n"
    "1️⃣ Выбираете точку\n"
    "2️⃣ Выбираете категорию (или «Все категории»)\n"
    "3️⃣ Бот присылает список позиций — отвечаете <b>одним сообщением</b>: "
    "числа по порядку, каждое на новой строке, столько же чисел, сколько позиций\n"
    "4️⃣ Бот сам считает, что нужно заказать, и показывает сводку\n"
    "5️⃣ Проверяете и жмёте «Отправить»\n\n"
    "❗️Вписываете не «сколько заказать», а <b>сколько сейчас есть по факту</b> — "
    "заказ бот посчитает сам.\n\n"
    "🧭 Ошиблись с точкой или категорией? На каждом шаге есть кнопки "
    "«⬅️ Категории» и «🏠 Точки» — вернут назад, ничего не отменяя.\n"
    "📋 Кнопка «Меню» рядом с полем ввода — быстрый доступ к /start и /help в любой момент.\n"
    "🔘 Кнопка «📦 Заполнить остатки» внизу экрана всегда под рукой — не нужно помнить команды.\n"
    "💬 Можно просто написать «остатки», «заказ» или «привет» — бот поймёт это так же, как /start."
)


async def do_start(message: Message, state: FSMContext):
    await state.clear()
    points = db.list_points()
    if not points:
        await message.answer(
            "Каталог точек пуст. Заполните seed_products.xlsx и запустите import_products.py, "
            "либо (если вы админ) создайте точку через /admin.",
            reply_markup=main_reply_keyboard(message.from_user.id),
        )
        return
    await message.answer(INTRO_TEXT, parse_mode="HTML", reply_markup=main_reply_keyboard(message.from_user.id))
    await message.answer(
        "Выберите точку, по которой заполняем остатки:",
        reply_markup=points_keyboard(),
    )
    await state.set_state(OrderForm.choosing_point)


@router.message(CommandStart())
async def cmd_start(message: Message, state: FSMContext):
    await do_start(message, state)


@router.message(F.text.func(lambda t: t and t.strip().lower() in START_TRIGGERS))
async def start_trigger(message: Message, state: FSMContext):
    await do_start(message, state)


@router.message(Command("help"))
async def cmd_help(message: Message):
    await message.answer(INTRO_TEXT, parse_mode="HTML")


async def do_admin(message: Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        await message.answer("Эта команда доступна только администраторам.")
        return
    await state.clear()
    await message.answer("🛠 Админ-панель", reply_markup=main_reply_keyboard(message.from_user.id))
    kb = points_keyboard(prefix="adminpoint")
    kb.inline_keyboard.append([InlineKeyboardButton(text="➕ Новая точка", callback_data="adminpoint:NEW")])
    await message.answer("Выберите точку для редактирования каталога:", reply_markup=kb)
    await state.set_state(AdminForm.picking_point)


@router.message(Command("admin"))
async def cmd_admin(message: Message, state: FSMContext):
    await do_admin(message, state)


@router.message(F.text.func(lambda t: t and t.strip().lower() in ADMIN_TRIGGERS))
async def admin_trigger(message: Message, state: FSMContext):
    await do_admin(message, state)


@router.callback_query(F.data == "back:points")
async def back_to_points(cb: CallbackQuery, state: FSMContext):
    await state.clear()
    await cb.message.edit_text("Выберите точку:", reply_markup=points_keyboard())
    await state.set_state(OrderForm.choosing_point)
    await cb.answer()


@router.callback_query(OrderForm.choosing_point, F.data.startswith("point:"))
async def point_chosen(cb: CallbackQuery, state: FSMContext):
    point_id = int(cb.data.split(":")[1])
    point = db.get_point(point_id)
    categories = categories_with_products(point_id)
    if not categories:
        await cb.answer("У этой точки пока нет ни одной позиции в каталоге.", show_alert=True)
        return
    await state.update_data(point_id=point_id, point_name=point["name"])
    await cb.message.edit_text(
        f"Точка: <b>{point['name']}</b>\nЧто заполняем — все позиции или одну категорию?",
        reply_markup=categories_keyboard(categories),
        parse_mode="HTML",
    )
    await state.set_state(OrderForm.choosing_category)
    await cb.answer()


@router.callback_query(OrderForm.choosing_category, F.data.startswith("cat:"))
async def category_chosen(cb: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    point_id = data["point_id"]
    raw = cb.data.split(":")[1]
    category_id = None if raw == "ALL" else int(raw)
    category_label = "все категории" if category_id is None else db.get_category(category_id)["name"]

    products = db.products_for(point_id, category_id)
    if not products:
        await cb.answer("В этой категории нет позиций.", show_alert=True)
        return

    await state.update_data(
        category_label=category_label,
        product_ids=[p["id"] for p in products],
        answers={},
    )
    await cb.message.edit_text(f"Заполняем: <b>{category_label}</b>", parse_mode="HTML")
    await state.set_state(OrderForm.filling)
    await send_category_form(cb.message, state)
    await cb.answer()


def format_category_form(products, last_facts=None) -> str:
    last_facts = last_facts or {}
    lines = [
        "📝 Впишите факт по каждой позиции <b>одним сообщением</b>:",
        "каждое число — на новой строке, по порядку, без нумерации.",
        "",
    ]
    has_history = False
    for i, p in enumerate(products, 1):
        last = last_facts.get(p["id"])
        suffix = ""
        if last is not None:
            has_history = True
            suffix = f" <i>(в прошлый раз: {last:g})</i>"
        lines.append(f"{i}. {p['name']} — мин {p['min_qty']:g} / макс {p['max_qty']:g} {p['unit']}{suffix}")

    if has_history:
        lines.append("")
        lines.append("🔁 Если по позиции ничего не изменилось — вместо числа поставьте <code>=</code>, "
                      "бот возьмёт значение из прошлого раза.")

    example_n = min(2, len(products))
    example_products = products[:example_n]
    example_values = [max(round((p["min_qty"] + p["max_qty"]) / 2), 1) for p in example_products]

    lines.append("")
    lines.append("💡 <b>Пример:</b> если сейчас")
    for p, v in zip(example_products, example_values):
        lines.append(f"• {p['name']} — {v:g} {p['unit']}")
    if len(products) > example_n:
        lines.append(f"• …а по остальным {len(products) - example_n} позициям — свои числа")
    lines.append("то ваш ответ начинается так:")
    lines.append("<code>" + "\n".join(f"{v:g}" for v in example_values) + ("\n…" if len(products) > example_n else "") + "</code>")

    return "\n".join(lines)


async def send_category_form(message: Message, state: FSMContext):
    data = await state.get_data()
    product_ids = data["product_ids"]
    products = [db.get_product(pid) for pid in product_ids]
    last_facts = db.last_facts_for(product_ids)
    await message.answer(
        format_category_form(products, last_facts), parse_mode="HTML", reply_markup=category_form_keyboard()
    )


def parse_bulk_numbers(text: str, expected: int, last_values=None):
    """Пытается разобрать текст в список из `expected` чисел ≥ 0.
    Токен '=' означает «взять значение из прошлого раза» (last_values[i]), если оно известно.
    Сначала пробует по строкам, затем — по пробелам. Возвращает (numbers, error)."""
    last_values = last_values or [None] * expected
    for splitter in (lambda t: [l.strip() for l in t.splitlines() if l.strip()],
                      lambda t: t.split()):
        tokens = splitter(text)
        if len(tokens) != expected:
            continue
        numbers = []
        for i, tok in enumerate(tokens):
            if tok == "=":
                if last_values[i] is None:
                    numbers = None
                    break
                numbers.append(last_values[i])
                continue
            tok_clean = tok.replace(",", ".")
            m = re.match(r"^\d+[.)]\s*(.+)$", tok_clean)  # снять нумерацию "1." если её вписали
            if m:
                tok_clean = m.group(1)
            try:
                val = float(tok_clean)
            except ValueError:
                numbers = None
                break
            if val < 0:
                numbers = None
                break
            numbers.append(val)
        if numbers is not None:
            return numbers, None
    return None, expected


@router.message(OrderForm.filling)
async def receive_bulk_facts(message: Message, state: FSMContext):
    data = await state.get_data()
    product_ids = data["product_ids"]
    expected = len(product_ids)
    last_facts = db.last_facts_for(product_ids)
    last_values = [last_facts.get(pid) for pid in product_ids]

    numbers, expected_count = parse_bulk_numbers(message.text or "", expected, last_values)
    if numbers is None:
        got = len((message.text or "").split()) if "\n" not in (message.text or "").strip() else len([l for l in (message.text or "").splitlines() if l.strip()])
        await message.answer(
            f"⚠️ Не получилось разобрать числа: похоже, их {got}, а нужно ровно "
            f"<b>{expected_count}</b> — по одному на каждую позицию ниже, каждое ≥ 0 (или <code>=</code>, "
            f"если для позиции есть значение из прошлого раза).\n"
            f"Отправьте ещё раз одним сообщением, число на новой строке:",
            parse_mode="HTML",
        )
        products = [db.get_product(pid) for pid in product_ids]
        await message.answer(
            format_category_form(products, last_facts), parse_mode="HTML", reply_markup=category_form_keyboard()
        )
        return

    answers = {str(pid): val for pid, val in zip(product_ids, numbers)}
    await state.update_data(answers=answers)
    await show_review(message, state)


async def show_review(message: Message, state: FSMContext):
    data = await state.get_data()
    answers = data["answers"]
    product_ids = data["product_ids"]

    lines_all = []
    order_lines = []
    current_cat = None
    for pid in product_ids:
        p = db.get_product(pid)
        fact = answers[str(pid)]
        order = round(p["max_qty"] - fact, 2)
        icon = status_icon(fact, p["min_qty"], p["max_qty"])
        if p["category_name"] != current_cat:
            current_cat = p["category_name"]
            lines_all.append(f"\n<b>{current_cat}</b>")
        lines_all.append(f"{icon} {p['name']}: факт {fact:g} → заказ {max(order, 0):g} {p['unit']}")
        if order > 0:
            order_lines.append(f"• {p['name']} — {order:g} {p['unit']}")

    summary = "📋 <b>Проверьте перед отправкой:</b>\n" + "\n".join(lines_all)
    if order_lines:
        summary += "\n\n🧾 <b>Итого к заказу:</b>\n" + "\n".join(order_lines)
    else:
        summary += "\n\n✅ Всё в норме, заказывать нечего."

    await state.set_state(OrderForm.reviewing)
    await message.answer(summary, parse_mode="HTML", reply_markup=review_keyboard())


@router.callback_query(OrderForm.reviewing, F.data == "review:redo")
async def review_redo(cb: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    await state.update_data(answers={})
    await state.set_state(OrderForm.filling)
    await cb.message.edit_text(f"Заполняем заново: <b>{data['category_label']}</b>", parse_mode="HTML")
    await send_category_form(cb.message, state)
    await cb.answer()


@router.callback_query(OrderForm.reviewing, F.data == "review:fix_one")
async def review_fix_one_start(cb: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    await cb.message.edit_text(
        "Какую позицию исправить?",
        reply_markup=fix_one_keyboard(data["product_ids"], data["answers"]),
    )
    await cb.answer()


@router.callback_query(OrderForm.reviewing, F.data == "review:cancel_fix")
async def review_cancel_fix(cb: CallbackQuery, state: FSMContext):
    await show_review(cb.message, state)
    await cb.answer()


@router.callback_query(OrderForm.reviewing, F.data.startswith("fixitem:"))
async def fixitem_chosen(cb: CallbackQuery, state: FSMContext):
    product_id = int(cb.data.split(":")[1])
    data = await state.get_data()
    product = db.get_product(product_id)
    old_fact = data["answers"][str(product_id)]
    await state.update_data(fix_product_id=product_id)
    await state.set_state(OrderForm.editing_single)
    await cb.message.edit_text(
        f"«{product['name']}»\nСейчас указано: {old_fact:g} {product['unit']}\n"
        f"Введите новое значение (мин {product['min_qty']:g} / макс {product['max_qty']:g}):",
    )
    await cb.answer()


@router.message(OrderForm.editing_single)
async def receive_fix_value(message: Message, state: FSMContext):
    text = (message.text or "").strip().replace(",", ".")
    try:
        val = float(text)
        if val < 0:
            raise ValueError
    except ValueError:
        await message.answer("Нужно одно число ≥ 0, например: 6 или 4.5. Введите ещё раз:")
        return

    data = await state.get_data()
    answers = data["answers"]
    answers[str(data["fix_product_id"])] = val
    await state.update_data(answers=answers)
    await show_review(message, state)


@router.callback_query(OrderForm.reviewing, F.data == "review:cancel")
async def review_cancel(cb: CallbackQuery, state: FSMContext):
    await state.clear()
    await cb.message.edit_text("Отменено. Наберите /start, чтобы начать заново.")
    await cb.answer()


@router.callback_query(OrderForm.reviewing, F.data == "review:send")
async def review_send(cb: CallbackQuery, state: FSMContext, bot: Bot):
    data = await state.get_data()
    point = db.get_point(data["point_id"])
    answers = data["answers"]
    product_ids = data["product_ids"]
    employee = cb.from_user.full_name

    order_lines = []
    current_cat = None
    for pid in product_ids:
        p = db.get_product(pid)
        fact = answers[str(pid)]
        order = round(p["max_qty"] - fact, 2)
        db.save_entry(pid, fact, employee, cb.from_user.id)
        if order > 0:
            if p["category_name"] != current_cat:
                current_cat = p["category_name"]
                order_lines.append(f"\n<b>{current_cat}</b>")
            order_lines.append(f"• {p['name']} — {order:g} {p['unit']}")

    header = point["header_template"].format(
        point=point["name"], date=datetime.now().strftime("%d.%m.%Y %H:%M")
    )
    if order_lines:
        final_text = f"{header}\n" + "\n".join(order_lines) + f"\n\nЗаполнил: {employee}"
    else:
        final_text = f"{header}\n\n✅ Всё в норме, заказ не требуется.\nЗаполнил: {employee}"

    target_chat = point["chat_id"] or cb.message.chat.id
    await bot.send_message(target_chat, final_text, parse_mode="HTML")
    if target_chat != cb.message.chat.id:
        await cb.message.answer("Заказ отправлен ✅")
    else:
        await cb.message.edit_text(final_text, parse_mode="HTML")

    await state.clear()
    await cb.answer("Готово!")


# ============================================================================
#  АДМИН-ПАНЕЛЬ: категории, позиции, мин/макс — без правки Excel
# ============================================================================


class AdminForm(StatesGroup):
    picking_point = State()
    add_point_name = State()
    menu = State()
    add_category_name = State()
    picking_category = State()
    add_product_name = State()
    add_product_unit = State()
    add_product_minmax = State()
    picking_product = State()
    edit_minmax = State()
    edit_name = State()
    rename_category = State()
    confirm_delete = State()
    set_reminder_time = State()


def admin_menu_keyboard():
    kb = [
        [InlineKeyboardButton(text="➕ Категория", callback_data="admin:add_category")],
        [InlineKeyboardButton(text="➕ Позиция", callback_data="admin:add_product")],
        [InlineKeyboardButton(text="✏️ Мин/Макс позиции", callback_data="admin:edit_minmax")],
        [InlineKeyboardButton(text="✏️ Переименовать позицию", callback_data="admin:rename_product")],
        [InlineKeyboardButton(text="✏️ Переименовать категорию", callback_data="admin:rename_category")],
        [InlineKeyboardButton(text="🗑 Удалить позицию", callback_data="admin:delete_product")],
        [InlineKeyboardButton(text="🗑 Удалить категорию", callback_data="admin:delete_category")],
        [InlineKeyboardButton(text="⏰ Напоминание", callback_data="admin:reminder")],
        [InlineKeyboardButton(text="📋 Показать каталог", callback_data="admin:show_catalog")],
        [InlineKeyboardButton(text="⬅️ Сменить точку", callback_data="admin:back_points")],
    ]
    return InlineKeyboardMarkup(inline_keyboard=kb)


def admin_categories_keyboard(point_id: int, extra_new_button=False):
    categories = db.list_categories(point_id)
    kb = [[InlineKeyboardButton(text=c["name"], callback_data=f"admincat:{c['id']}")] for c in categories]
    if extra_new_button:
        kb.append([InlineKeyboardButton(text="➕ Новая категория", callback_data="admincat:NEW")])
    kb.append([InlineKeyboardButton(text="⬅️ Отмена", callback_data="admin:cancel")])
    return InlineKeyboardMarkup(inline_keyboard=kb)


def admin_products_keyboard(point_id: int, category_id: int):
    products = db.products_for(point_id, category_id)
    kb = [[InlineKeyboardButton(text=p["name"], callback_data=f"adminprod:{p['id']}")] for p in products]
    kb.append([InlineKeyboardButton(text="⬅️ Отмена", callback_data="admin:cancel")])
    return InlineKeyboardMarkup(inline_keyboard=kb)


def confirm_keyboard():
    kb = [
        [InlineKeyboardButton(text="✅ Да, удалить", callback_data="admconfirm:yes")],
        [InlineKeyboardButton(text="❌ Отмена", callback_data="admconfirm:no")],
    ]
    return InlineKeyboardMarkup(inline_keyboard=kb)


@router.callback_query(AdminForm.picking_point, F.data.startswith("adminpoint:"))
async def admin_point_chosen(cb: CallbackQuery, state: FSMContext):
    raw = cb.data.split(":")[1]
    if raw == "NEW":
        await cb.message.edit_text("Введите название новой точки:")
        await state.set_state(AdminForm.add_point_name)
        await cb.answer()
        return
    point_id = int(raw)
    await state.update_data(point_id=point_id)
    await show_admin_menu(cb.message, state, edit=True)
    await cb.answer()


@router.message(AdminForm.add_point_name)
async def admin_add_point_name(message: Message, state: FSMContext):
    name = (message.text or "").strip()
    if not name:
        await message.answer("Название не может быть пустым. Введите ещё раз:")
        return
    point_id = db.create_point(name)
    await state.update_data(point_id=point_id)
    await message.answer(f"Точка «{name}» создана.")
    await show_admin_menu(message, state)


async def show_admin_menu(message: Message, state: FSMContext, edit: bool = False):
    data = await state.get_data()
    point = db.get_point(data["point_id"])
    text = f"🛠 Точка: <b>{point['name']}</b>\nЧто делаем?"
    if edit:
        await message.edit_text(text, reply_markup=admin_menu_keyboard(), parse_mode="HTML")
    else:
        await message.answer(text, reply_markup=admin_menu_keyboard(), parse_mode="HTML")
    await state.set_state(AdminForm.menu)


@router.callback_query(F.data == "admin:back_points")
async def admin_back_points(cb: CallbackQuery, state: FSMContext):
    await state.clear()
    kb = points_keyboard(prefix="adminpoint")
    kb.inline_keyboard.append([InlineKeyboardButton(text="➕ Новая точка", callback_data="adminpoint:NEW")])
    await cb.message.edit_text("Выберите точку для редактирования каталога:", reply_markup=kb)
    await state.set_state(AdminForm.picking_point)
    await cb.answer()


@router.callback_query(F.data == "admin:cancel")
async def admin_cancel(cb: CallbackQuery, state: FSMContext):
    await show_admin_menu(cb.message, state, edit=True)
    await cb.answer("Отменено")


@router.callback_query(F.data == "admin:show_catalog")
async def admin_show_catalog(cb: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    point_id = data["point_id"]
    categories = db.list_categories(point_id)
    if not categories:
        await cb.answer("Каталог пуст.", show_alert=True)
        return
    lines = []
    for c in categories:
        products = db.products_for(point_id, c["id"])
        lines.append(f"\n<b>{c['name']}</b>")
        if not products:
            lines.append("  (пусто)")
        for p in products:
            lines.append(f"  • {p['name']} — мин {p['min_qty']:g} / макс {p['max_qty']:g} {p['unit']}")
    await cb.message.answer("📋 <b>Текущий каталог:</b>\n" + "\n".join(lines), parse_mode="HTML")
    await cb.answer()


@router.callback_query(F.data == "admin:reminder")
async def admin_reminder_start(cb: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    point = db.get_point(data["point_id"])
    current = f"сейчас: {point['reminder_time']}" if point["reminder_time"] else "сейчас выключено"
    recipient = "чат заказа этой точки" if not point["reminder_chat_id"] else f"отдельный чат ({point['reminder_chat_id']})"
    await cb.message.edit_text(
        f"⏰ Напоминание, если к этому времени остатки ещё не заполнены ({current}).\n"
        f"Получатель: {recipient} — сменить получателя можно позже, сейчас не спрашиваю.\n\n"
        f"Введите время в формате <code>ЧЧ:ММ</code> (например 10:00), "
        f"или «-», чтобы выключить напоминание для этой точки:",
        parse_mode="HTML",
    )
    await state.set_state(AdminForm.set_reminder_time)
    await cb.answer()


@router.message(AdminForm.set_reminder_time)
async def admin_reminder_set(message: Message, state: FSMContext):
    text = (message.text or "").strip()
    data = await state.get_data()

    if text == "-":
        db.set_reminder(data["point_id"], None)
        await message.answer("Напоминание для этой точки выключено.")
        await show_admin_menu(message, state)
        return

    m = re.match(r"^([0-9]|[01]\d|2[0-3]):([0-5]\d)$", text)
    if not m:
        await message.answer("Неверный формат. Нужно ЧЧ:ММ, например 10:00 или 9:30, или «-» чтобы выключить. Введите ещё раз:")
        return
    normalized = f"{int(m.group(1)):02d}:{m.group(2)}"

    db.set_reminder(data["point_id"], normalized)
    await message.answer(f"Готово. Напоминание в {normalized}, если к этому времени остатки не заполнены.")
    await show_admin_menu(message, state)


# ---- добавить категорию ----

@router.callback_query(AdminForm.menu, F.data == "admin:add_category")
async def admin_add_category_start(cb: CallbackQuery, state: FSMContext):
    await cb.message.edit_text("Введите название новой категории:")
    await state.set_state(AdminForm.add_category_name)
    await cb.answer()


@router.message(AdminForm.add_category_name)
async def admin_add_category_name(message: Message, state: FSMContext):
    name = (message.text or "").strip()
    if not name:
        await message.answer("Название не может быть пустым. Введите ещё раз:")
        return
    data = await state.get_data()
    db.get_or_create_category(data["point_id"], name)
    await message.answer(f"Категория «{name}» добавлена.")
    await show_admin_menu(message, state)


# ---- добавить позицию ----

@router.callback_query(AdminForm.menu, F.data == "admin:add_product")
async def admin_add_product_start(cb: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    await state.update_data(pending_action="add_product")
    await cb.message.edit_text(
        "В какую категорию добавить позицию?",
        reply_markup=admin_categories_keyboard(data["point_id"], extra_new_button=True),
    )
    await state.set_state(AdminForm.picking_category)
    await cb.answer()


@router.callback_query(AdminForm.picking_category, F.data.startswith("admincat:"))
async def admin_category_picked(cb: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    raw = cb.data.split(":")[1]
    action = data["pending_action"]

    if raw == "NEW":
        await state.update_data(pending_new_category=True)
        await cb.message.edit_text("Введите название новой категории:")
        await state.set_state(AdminForm.add_category_name if action != "add_product" else AdminForm.add_product_name)
        # для add_product сразу попросим имя позиции после создания категории — упростим: создаём категорию тут же
        if action == "add_product":
            await cb.message.answer(
                "Сначала пришлите название категории, затем (следующим сообщением) — название позиции."
            )
        await cb.answer()
        return

    category_id = int(raw)
    await state.update_data(category_id=category_id)
    category = db.get_category(category_id)

    if action == "add_product":
        await cb.message.edit_text(f"Категория: <b>{category['name']}</b>\nВведите название позиции:", parse_mode="HTML")
        await state.set_state(AdminForm.add_product_name)
    elif action in ("edit_minmax", "rename_product", "delete_product"):
        products = db.products_for(data["point_id"], category_id)
        if not products:
            await cb.answer("В этой категории пока нет позиций.", show_alert=True)
            return
        await cb.message.edit_text(
            f"Категория: <b>{category['name']}</b>\nВыберите позицию:",
            reply_markup=admin_products_keyboard(data["point_id"], category_id),
            parse_mode="HTML",
        )
        await state.set_state(AdminForm.picking_product)
    elif action == "rename_category":
        await cb.message.edit_text(f"Текущее название: <b>{category['name']}</b>\nВведите новое название:", parse_mode="HTML")
        await state.set_state(AdminForm.rename_category)
    elif action == "delete_category":
        await cb.message.edit_text(
            f"Удалить категорию «{category['name']}» вместе со всеми её позициями?",
            reply_markup=confirm_keyboard(),
        )
        await state.set_state(AdminForm.confirm_delete)
    await cb.answer()


@router.message(AdminForm.add_product_name)
async def admin_add_product_name(message: Message, state: FSMContext):
    name = (message.text or "").strip()
    if not name:
        await message.answer("Название не может быть пустым. Введите ещё раз:")
        return
    data = await state.get_data()
    if data.get("pending_new_category"):
        category_id = db.get_or_create_category(data["point_id"], name)
        await state.update_data(category_id=category_id, pending_new_category=False)
        await message.answer(f"Категория «{name}» создана. Теперь введите название позиции:")
        return
    await state.update_data(new_product_name=name)
    await message.answer("Единица измерения? (например: шт, кг, уп, л) — или отправьте «-» для «шт»:")
    await state.set_state(AdminForm.add_product_unit)


@router.message(AdminForm.add_product_unit)
async def admin_add_product_unit(message: Message, state: FSMContext):
    unit = (message.text or "").strip()
    unit = "шт" if unit in ("", "-") else unit
    await state.update_data(new_product_unit=unit)
    await message.answer("Введите мин. и макс. остаток одним сообщением через пробел, например: 2 10")
    await state.set_state(AdminForm.add_product_minmax)


@router.message(AdminForm.add_product_minmax)
async def admin_add_product_minmax(message: Message, state: FSMContext):
    parts = (message.text or "").replace(",", ".").split()
    if len(parts) != 2:
        await message.answer("Нужно два числа через пробел: мин и макс. Например: 2 10")
        return
    try:
        min_qty, max_qty = float(parts[0]), float(parts[1])
        if min_qty < 0 or max_qty < 0 or max_qty < min_qty:
            raise ValueError
    except ValueError:
        await message.answer("Некорректные значения. Мин и макс должны быть числами ≥ 0, макс ≥ мин. Введите ещё раз:")
        return

    data = await state.get_data()
    db.add_product(
        point_id=data["point_id"],
        category_id=data["category_id"],
        name=data["new_product_name"],
        unit=data["new_product_unit"],
        min_qty=min_qty,
        max_qty=max_qty,
    )
    await message.answer(f"Позиция «{data['new_product_name']}» добавлена (мин {min_qty:g} / макс {max_qty:g} {data['new_product_unit']}).")
    await show_admin_menu(message, state)


# ---- редактирование мин/макс, названия, удаление позиции ----

@router.callback_query(AdminForm.menu, F.data.in_({"admin:edit_minmax", "admin:rename_product", "admin:delete_product"}))
async def admin_pick_category_for_product_action(cb: CallbackQuery, state: FSMContext):
    action = cb.data.split(":")[1]
    data = await state.get_data()
    await state.update_data(pending_action=action)
    await cb.message.edit_text(
        "Из какой категории позиция?",
        reply_markup=admin_categories_keyboard(data["point_id"]),
    )
    await state.set_state(AdminForm.picking_category)
    await cb.answer()


@router.callback_query(AdminForm.picking_product, F.data.startswith("adminprod:"))
async def admin_product_picked(cb: CallbackQuery, state: FSMContext):
    product_id = int(cb.data.split(":")[1])
    data = await state.get_data()
    action = data["pending_action"]
    product = db.get_product(product_id)
    await state.update_data(product_id=product_id)

    if action == "edit_minmax":
        await cb.message.edit_text(
            f"«{product['name']}»: сейчас мин {product['min_qty']:g} / макс {product['max_qty']:g}.\n"
            f"Введите новые значения через пробел, например: 3 12",
        )
        await state.set_state(AdminForm.edit_minmax)
    elif action == "rename_product":
        await cb.message.edit_text(f"Текущее название: «{product['name']}»\nВведите новое:")
        await state.set_state(AdminForm.edit_name)
    elif action == "delete_product":
        await cb.message.edit_text(f"Удалить позицию «{product['name']}»?", reply_markup=confirm_keyboard())
        await state.set_state(AdminForm.confirm_delete)
    await cb.answer()


@router.message(AdminForm.edit_minmax)
async def admin_edit_minmax_value(message: Message, state: FSMContext):
    parts = (message.text or "").replace(",", ".").split()
    if len(parts) != 2:
        await message.answer("Нужно два числа через пробел: мин и макс. Например: 3 12")
        return
    try:
        min_qty, max_qty = float(parts[0]), float(parts[1])
        if min_qty < 0 or max_qty < 0 or max_qty < min_qty:
            raise ValueError
    except ValueError:
        await message.answer("Некорректные значения. Мин и макс должны быть числами ≥ 0, макс ≥ мин. Введите ещё раз:")
        return
    data = await state.get_data()
    db.update_product_minmax(data["product_id"], min_qty, max_qty)
    await message.answer(f"Обновлено: мин {min_qty:g} / макс {max_qty:g}.")
    await show_admin_menu(message, state)


@router.message(AdminForm.edit_name)
async def admin_edit_name_value(message: Message, state: FSMContext):
    new_name = (message.text or "").strip()
    if not new_name:
        await message.answer("Название не может быть пустым. Введите ещё раз:")
        return
    data = await state.get_data()
    db.update_product_field(data["product_id"], "name", new_name)
    await message.answer(f"Название обновлено: «{new_name}».")
    await show_admin_menu(message, state)


@router.message(AdminForm.rename_category)
async def admin_rename_category_value(message: Message, state: FSMContext):
    new_name = (message.text or "").strip()
    if not new_name:
        await message.answer("Название не может быть пустым. Введите ещё раз:")
        return
    data = await state.get_data()
    db.rename_category(data["category_id"], new_name)
    await message.answer(f"Категория переименована в «{new_name}».")
    await show_admin_menu(message, state)


@router.callback_query(AdminForm.menu, F.data == "admin:rename_category")
async def admin_rename_category_start(cb: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    await state.update_data(pending_action="rename_category")
    await cb.message.edit_text(
        "Какую категорию переименовать?",
        reply_markup=admin_categories_keyboard(data["point_id"]),
    )
    await state.set_state(AdminForm.picking_category)
    await cb.answer()


@router.callback_query(AdminForm.menu, F.data == "admin:delete_category")
async def admin_delete_category_start(cb: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    await state.update_data(pending_action="delete_category")
    await cb.message.edit_text(
        "Какую категорию удалить? (удалятся и все её позиции)",
        reply_markup=admin_categories_keyboard(data["point_id"]),
    )
    await state.set_state(AdminForm.picking_category)
    await cb.answer()


@router.callback_query(AdminForm.confirm_delete, F.data.startswith("admconfirm:"))
async def admin_confirm_delete(cb: CallbackQuery, state: FSMContext):
    answer = cb.data.split(":")[1]
    data = await state.get_data()
    if answer == "yes":
        action = data["pending_action"]
        if action == "delete_category":
            category = db.get_category(data["category_id"])
            db.delete_category(data["category_id"])
            await cb.message.edit_text(f"Категория «{category['name']}» удалена.")
        elif action == "delete_product":
            product = db.get_product(data["product_id"])
            db.delete_product(data["product_id"])
            await cb.message.edit_text(f"Позиция «{product['name']}» удалена.")
    else:
        await cb.message.edit_text("Отменено.")
    await show_admin_menu(cb.message, state)
    await cb.answer()


async def setup_commands(bot: Bot):
    default_commands = [
        BotCommand(command="start", description="Начать / выбрать точку и заполнить остатки"),
        BotCommand(command="help", description="Как пользоваться ботом"),
    ]
    await bot.set_my_commands(default_commands, scope=BotCommandScopeDefault())

    admin_commands = default_commands + [
        BotCommand(command="admin", description="Админ-панель: категории, позиции, мин/макс"),
    ]
    for admin_id in ADMIN_IDS:
        try:
            await bot.set_my_commands(admin_commands, scope=BotCommandScopeChat(chat_id=admin_id))
        except Exception:
            pass  # админ ещё не писал боту — Telegram не даст выставить меню для этого chat_id, это ок


async def reminder_loop(bot: Bot):
    """Раз в минуту проверяет точки с включённым напоминанием: если наступило заданное
    время и по точке сегодня ещё никто не заполнял остатки — шлёт напоминание."""
    while True:
        try:
            now = datetime.now()
            current_hm = now.strftime("%H:%M")
            today = now.strftime("%Y-%m-%d")
            for point in db.points_with_reminders():
                if point["reminder_time"] != current_hm:
                    continue
                if db.was_reminder_sent(point["id"], today):
                    continue
                db.mark_reminder_sent(point["id"], today)  # помечаем сразу, чтобы не отправить дважды
                if db.has_entries_today(point["id"], today):
                    continue  # уже заполняли сегодня — напоминать не о чем
                target = point["reminder_chat_id"] or point["chat_id"]
                if not target:
                    print(f"⚠️  У точки «{point['name']}» не задан чат для напоминания — пропускаю.")
                    continue
                try:
                    await bot.send_message(
                        target,
                        f"⏰ Напоминание: сегодня ещё не заполнены остатки по точке «{point['name']}».\n"
                        f"Наберите /start, чтобы заполнить.",
                    )
                except Exception as e:
                    print(f"⚠️  Не удалось отправить напоминание для «{point['name']}»: {e}")
        except Exception as e:
            print(f"⚠️  Ошибка в reminder_loop: {e}")
        await asyncio.sleep(60)


async def main():
    token = os.environ.get("BOT_TOKEN")
    if not token:
        raise SystemExit("Задайте переменную окружения BOT_TOKEN перед запуском.")
    if not ADMIN_IDS:
        print("⚠️  ADMIN_IDS не задан — команда /admin будет доступна ВСЕМ. "
              "Укажите ADMIN_IDS=id1,id2 в переменных окружения для продакшена.")
    db.init_db()
    bot = Bot(token)
    await setup_commands(bot)
    dp = Dispatcher(storage=MemoryStorage())
    dp.include_router(router)
    asyncio.create_task(reminder_loop(bot))
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
