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
RECIPE_TRIGGERS = {"рецепты", "рецепт", "техкарты", "техкарта", "тех.карты", "тех.карта", "📖 тех.карты"}
RECIPE_CATEGORY_PRESETS = [
    "Пельмени классические",
    "Пельмени заварные",
    "Вареники",
    "Салаты",
    "Супы",
    "Горячее",
    "Закуски",
]
KB_TRIGGERS = {"база знаний", "знания", "обучение", "санпин", "хранение", "маркировка", "📚 обучение", "📚 база знаний"}
KB_SECTIONS = ["Обучение", "Хранение и маркировка", "СанПиН"]
KB_SECTION_ICONS = {"Обучение": "📚", "Хранение и маркировка": "🧊", "СанПиН": "🧼"}


def main_reply_keyboard(user_id: int) -> ReplyKeyboardMarkup:
    rows = [
        [KeyboardButton(text="📦 Заполнить остатки")],
        [KeyboardButton(text="📖 Тех.карты")],
        [KeyboardButton(text="📚 Обучение")],
    ]
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
    manual_entry = State()
    reviewing = State()
    editing_single = State()


class PointHub(StatesGroup):
    menu = State()


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
    kb.append([InlineKeyboardButton(text="⬅️ Меню точки", callback_data="hub:back")])
    return InlineKeyboardMarkup(inline_keyboard=kb)


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
    "2️⃣ В меню точки — «📦 Заказ остатков» или «📖 Тех.карты»\n"
    "3️⃣ Для заказа: выбираете категорию (или «Все категории» — бот сам объявит, когда перейдёт к следующей)\n"
    "4️⃣ Бот показывает позиции по одной — жмёте +1/+5/+10 или −1/−5/−10, "
    "либо «🔢 Ввести число» для точного значения, затем «✅ Далее»\n"
    "5️⃣ Бот сам считает, что нужно заказать, и показывает сводку\n"
    "6️⃣ Проверяете и жмёте «Отправить»\n\n"
    "❗️Указываете не «сколько заказать», а <b>сколько сейчас есть по факту</b> — "
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


def admin_points_keyboard():
    kb = points_keyboard(prefix="adminpoint")
    kb.inline_keyboard.append([InlineKeyboardButton(text="➕ Новая точка", callback_data="adminpoint:NEW")])
    kb.inline_keyboard.append([InlineKeyboardButton(text="📥 Импорт из seed_products.xlsx", callback_data="admin:reimport")])
    kb.inline_keyboard.append([InlineKeyboardButton(text="📥 Импорт тех.карт и статей", callback_data="admin:reimport_content")])
    kb.inline_keyboard.append([InlineKeyboardButton(text="📖 Тех.карты", callback_data="admin:recipes")])
    kb.inline_keyboard.append([InlineKeyboardButton(text="📚 Обучение", callback_data="admin:kb")])
    return kb


async def do_admin(message: Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        await message.answer("Эта команда доступна только администраторам.")
        return
    await state.clear()
    await message.answer("🛠 Админ-панель", reply_markup=main_reply_keyboard(message.from_user.id))
    await message.answer("Выберите точку для редактирования каталога:", reply_markup=admin_points_keyboard())
    await state.set_state(AdminForm.picking_point)


@router.message(Command("admin"))
async def cmd_admin(message: Message, state: FSMContext):
    await do_admin(message, state)


@router.message(F.text.func(lambda t: t and t.strip().lower() in ADMIN_TRIGGERS))
async def admin_trigger(message: Message, state: FSMContext):
    await do_admin(message, state)


# ============================================================================
#  ТЕХ.КАРТЫ (просмотр — доступен всем; редактирование — см. блок АДМИН-ПАНЕЛЬ)
# ============================================================================


class RecipeView(StatesGroup):
    picking_category = State()
    browsing = State()


def recipe_categories_keyboard(categories, has_uncategorized):
    kb = [[InlineKeyboardButton(text="📖 Все блюда", callback_data="rcat:ALL")]]
    for i, c in enumerate(categories):
        kb.append([InlineKeyboardButton(text=c, callback_data=f"rcat:{i}")])
    if has_uncategorized:
        kb.append([InlineKeyboardButton(text="📄 Без категории", callback_data="rcat:NONE")])
    kb.append([InlineKeyboardButton(text="⬅️ Назад", callback_data="hub:back")])
    return InlineKeyboardMarkup(inline_keyboard=kb)


def recipe_list_keyboard(recipes, categorized: bool):
    kb = [[InlineKeyboardButton(text=r["name"], callback_data=f"recipe:{r['id']}")] for r in recipes]
    if categorized:
        kb.append([InlineKeyboardButton(text="⬅️ Разделы", callback_data="rcat:menu")])
    else:
        kb.append([InlineKeyboardButton(text="⬅️ Назад", callback_data="hub:back")])
    return InlineKeyboardMarkup(inline_keyboard=kb)


def format_recipe_card(recipe, ingredients) -> str:
    title = recipe["name"]
    if recipe["category"]:
        title += f" ({recipe['category']})"
    lines = [f"🍲 <b>{title}</b>", "", "<b>Ингредиенты:</b>"]
    for ing in ingredients:
        if ing["amount"] is not None:
            amount_str = f"{ing['amount']:g}"
            if ing["unit"]:
                amount_str += f" {ing['unit']}"
        else:
            amount_str = ing["unit"] or ""
        lines.append(f"• {ing['name']} — {amount_str}" if amount_str else f"• {ing['name']}")
    if recipe["method"]:
        lines.append("")
        lines.append("<b>Способ приготовления:</b>")
        lines.append(recipe["method"])
    return "\n".join(lines)


async def show_recipe_categories(message: Message, state: FSMContext, edit: bool = False):
    recipes = db.list_recipes()
    categories = sorted({r["category"] for r in recipes if r["category"]})
    has_uncategorized = any(not r["category"] for r in recipes)
    await state.update_data(recipe_categories=categories)
    kb = recipe_categories_keyboard(categories, has_uncategorized)
    text = "📖 Выберите раздел меню:"
    if edit:
        await message.edit_text(text, reply_markup=kb)
    else:
        await message.answer(text, reply_markup=kb)
    await state.set_state(RecipeView.picking_category)


async def do_recipes(message: Message, state: FSMContext, user_id: int = None):
    user_id = user_id if user_id is not None else message.from_user.id
    recipes = db.list_recipes()
    if not recipes:
        await message.answer(
            "Тех.карт пока нет." + (" Добавить можно через /admin." if is_admin(user_id) else "")
        )
        return
    categories = sorted({r["category"] for r in recipes if r["category"]})
    if not categories:
        # ни у одного рецепта нет категории — плоский список без лишнего шага
        await message.answer("📖 Выберите блюдо:", reply_markup=recipe_list_keyboard(recipes, categorized=False))
        await state.set_state(RecipeView.browsing)
        return
    await show_recipe_categories(message, state)


@router.message(Command("recipes"))
async def cmd_recipes(message: Message, state: FSMContext):
    await do_recipes(message, state)


@router.message(F.text.func(lambda t: t and t.strip().lower() in RECIPE_TRIGGERS))
async def recipes_trigger(message: Message, state: FSMContext):
    await do_recipes(message, state)


@router.callback_query(RecipeView.picking_category, F.data.startswith("rcat:"))
async def recipe_category_chosen(cb: CallbackQuery, state: FSMContext):
    raw = cb.data.split(":", 1)[1]
    if raw == "ALL":
        filtered = db.list_recipes()
        label = "все блюда"
    elif raw == "NONE":
        filtered = [r for r in db.list_recipes() if not r["category"]]
        label = "без категории"
    else:
        data = await state.get_data()
        category = data["recipe_categories"][int(raw)]
        filtered = [r for r in db.list_recipes() if r["category"] == category]
        label = category

    if not filtered:
        await cb.answer("В этом разделе пока нет блюд.", show_alert=True)
        return
    await cb.message.edit_text(f"📖 {label}:", reply_markup=recipe_list_keyboard(filtered, categorized=True))
    await state.set_state(RecipeView.browsing)
    await cb.answer()


@router.callback_query(RecipeView.browsing, F.data == "rcat:menu")
async def recipe_back_to_categories(cb: CallbackQuery, state: FSMContext):
    await show_recipe_categories(cb.message, state, edit=True)
    await cb.answer()


@router.callback_query(RecipeView.browsing, F.data.startswith("recipe:"))
async def recipe_selected(cb: CallbackQuery, state: FSMContext):
    recipe_id = int(cb.data.split(":")[1])
    recipe = db.get_recipe(recipe_id)
    if not recipe:
        await cb.answer("Рецепт не найден — возможно, его удалили.", show_alert=True)
        return
    ingredients = db.list_recipe_ingredients(recipe_id)
    if recipe["photo_file_id"]:
        await cb.message.answer_photo(recipe["photo_file_id"])
    await cb.message.answer(format_recipe_card(recipe, ingredients), parse_mode="HTML")
    await cb.answer()


# ============================================================================
#  БАЗА ЗНАНИЙ (обучение / хранение и маркировка / СанПиН) — просмотр для всех
# ============================================================================


class KBView(StatesGroup):
    picking_section = State()
    browsing = State()


def kb_sections_keyboard():
    kb = [
        [InlineKeyboardButton(text=f"{KB_SECTION_ICONS[s]} {s}", callback_data=f"kbsec:{i}")]
        for i, s in enumerate(KB_SECTIONS)
    ]
    kb.append([InlineKeyboardButton(text="⬅️ Назад", callback_data="hub:back")])
    return InlineKeyboardMarkup(inline_keyboard=kb)


def kb_articles_keyboard(articles):
    kb = [[InlineKeyboardButton(text=a["title"], callback_data=f"kbart:{a['id']}")] for a in articles]
    kb.append([InlineKeyboardButton(text="⬅️ Разделы", callback_data="kb:menu")])
    return InlineKeyboardMarkup(inline_keyboard=kb)


def format_kb_article(article) -> str:
    lines = [f"📄 <b>{article['title']}</b>"]
    if article["body"]:
        lines.append("")
        lines.append(article["body"])
    return "\n".join(lines)


async def do_kb(message: Message, state: FSMContext, edit: bool = False):
    text = "📚 Выберите раздел:"
    kb = kb_sections_keyboard()
    if edit:
        await message.edit_text(text, reply_markup=kb)
    else:
        await message.answer(text, reply_markup=kb)
    await state.set_state(KBView.picking_section)


@router.message(F.text.func(lambda t: t and t.strip().lower() in KB_TRIGGERS))
async def kb_trigger(message: Message, state: FSMContext):
    await do_kb(message, state)


@router.callback_query(KBView.picking_section, F.data.startswith("kbsec:"))
async def kb_section_chosen(cb: CallbackQuery, state: FSMContext):
    section = KB_SECTIONS[int(cb.data.split(":")[1])]
    articles = db.list_kb_articles(section)
    await state.update_data(kb_section=section)
    if not articles:
        await cb.answer("В этом разделе пока нет статей.", show_alert=True)
        return
    await cb.message.edit_text(
        f"{KB_SECTION_ICONS[section]} <b>{section}</b>",
        reply_markup=kb_articles_keyboard(articles),
        parse_mode="HTML",
    )
    await state.set_state(KBView.browsing)
    await cb.answer()


@router.callback_query(KBView.browsing, F.data == "kb:menu")
async def kb_back_to_sections(cb: CallbackQuery, state: FSMContext):
    await do_kb(cb.message, state, edit=True)
    await cb.answer()


@router.callback_query(KBView.browsing, F.data.startswith("kbart:"))
async def kb_article_selected(cb: CallbackQuery, state: FSMContext):
    article_id = int(cb.data.split(":")[1])
    article = db.get_kb_article(article_id)
    if not article:
        await cb.answer("Статья не найдена — возможно, её удалили.", show_alert=True)
        return
    if article["photo_file_id"]:
        await cb.message.answer_photo(article["photo_file_id"])
    await cb.message.answer(format_kb_article(article), parse_mode="HTML")
    await cb.answer()


@router.callback_query(F.data == "back:points")
async def back_to_points(cb: CallbackQuery, state: FSMContext):
    await state.clear()
    await cb.message.edit_text("Выберите точку:", reply_markup=points_keyboard())
    await state.set_state(OrderForm.choosing_point)
    await cb.answer()


def point_hub_keyboard():
    kb = [
        [InlineKeyboardButton(text="📦 Заказ остатков", callback_data="hub:order")],
        [InlineKeyboardButton(text="📖 Тех.карты", callback_data="hub:recipes")],
        [InlineKeyboardButton(text="📚 Обучение", callback_data="hub:kb")],
        [InlineKeyboardButton(text="🏠 Сменить точку", callback_data="back:points")],
    ]
    return InlineKeyboardMarkup(inline_keyboard=kb)


@router.callback_query(OrderForm.choosing_point, F.data.startswith("point:"))
async def point_chosen(cb: CallbackQuery, state: FSMContext):
    point_id = int(cb.data.split(":")[1])
    point = db.get_point(point_id)
    await state.update_data(point_id=point_id, point_name=point["name"])
    await cb.message.edit_text(
        f"Точка: <b>{point['name']}</b>\nЧто делаем?",
        reply_markup=point_hub_keyboard(),
        parse_mode="HTML",
    )
    await state.set_state(PointHub.menu)
    await cb.answer()


@router.callback_query(PointHub.menu, F.data == "hub:order")
async def hub_order(cb: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    point_id = data["point_id"]
    categories = categories_with_products(point_id)
    if not categories:
        await cb.answer("У этой точки пока нет ни одной позиции в каталоге.", show_alert=True)
        return
    await cb.message.edit_text(
        f"Точка: <b>{data['point_name']}</b>\nЧто заполняем — все позиции или одну категорию?",
        reply_markup=categories_keyboard(categories),
        parse_mode="HTML",
    )
    await state.set_state(OrderForm.choosing_category)
    await cb.answer()


@router.callback_query(PointHub.menu, F.data == "hub:recipes")
async def hub_recipes(cb: CallbackQuery, state: FSMContext):
    await do_recipes(cb.message, state, user_id=cb.from_user.id)
    await cb.answer()


@router.callback_query(PointHub.menu, F.data == "hub:kb")
async def hub_kb(cb: CallbackQuery, state: FSMContext):
    await do_kb(cb.message, state, edit=True)
    await cb.answer()


@router.callback_query(F.data == "hub:back")
async def hub_back(cb: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    point_id = data.get("point_id")
    if not point_id:
        await back_to_points(cb, state)
        return
    await cb.message.edit_text(
        f"Точка: <b>{data['point_name']}</b>\nЧто делаем?",
        reply_markup=point_hub_keyboard(),
        parse_mode="HTML",
    )
    await state.set_state(PointHub.menu)
    await cb.answer()


def item_keyboard(show_back: bool):
    kb = [
        [
            InlineKeyboardButton(text="−10", callback_data="qty:-10"),
            InlineKeyboardButton(text="−5", callback_data="qty:-5"),
            InlineKeyboardButton(text="−1", callback_data="qty:-1"),
            InlineKeyboardButton(text="+1", callback_data="qty:+1"),
            InlineKeyboardButton(text="+5", callback_data="qty:+5"),
            InlineKeyboardButton(text="+10", callback_data="qty:+10"),
        ],
        [InlineKeyboardButton(text="🔢 Ввести число", callback_data="qty:manual")],
        [InlineKeyboardButton(text="✅ Далее", callback_data="qty:next")],
    ]
    if show_back:
        kb.append([InlineKeyboardButton(text="⬅️ Предыдущая позиция", callback_data="qty:back")])
    kb.append(nav_row())
    return InlineKeyboardMarkup(inline_keyboard=kb)


def render_item_text(product, current_value: float, position_no: int, total: int) -> str:
    lines = [
        f"({position_no}/{total}) <b>{product['name']}</b>",
        f"Мин {product['min_qty']:g} / Макс {product['max_qty']:g} {product['unit']}",
        "",
        f"Сейчас указано: <b>{current_value:g}</b> {product['unit']}",
    ]
    return "\n".join(lines)


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
        index=0,
        answers={},
        current_value=None,
        last_shown_category=None,
    )
    await cb.message.edit_text(f"Заполняем: <b>{category_label}</b>", parse_mode="HTML")
    await state.set_state(OrderForm.filling)
    await advance_to_item(cb.message, state)
    await cb.answer()


async def advance_to_item(message: Message, state: FSMContext):
    """Показывает экран текущей позиции (index в state), объявляя смену категории,
    если сотрудник перешёл в другую категорию (актуально для «Все категории»)."""
    data = await state.get_data()
    idx = data["index"]
    product_ids = data["product_ids"]

    if idx >= len(product_ids):
        await show_review(message, state)
        return

    product = db.get_product(product_ids[idx])

    if idx > 0 and data.get("last_shown_category") != product["category_name"]:
        await message.answer(f"📍 <b>{product['category_name']}</b>", parse_mode="HTML")
    await state.update_data(last_shown_category=product["category_name"])

    answers = data["answers"]
    if str(product["id"]) in answers:
        current_value = answers[str(product["id"])]
    else:
        last_fact = db.last_facts_for([product["id"]]).get(product["id"])
        current_value = last_fact if last_fact is not None else 0
    await state.update_data(current_value=current_value)

    await message.answer(
        render_item_text(product, current_value, idx + 1, len(product_ids)),
        parse_mode="HTML",
        reply_markup=item_keyboard(show_back=idx > 0),
    )


@router.callback_query(OrderForm.filling, F.data.startswith("qty:"))
async def qty_button(cb: CallbackQuery, state: FSMContext):
    action = cb.data.split(":", 1)[1]
    data = await state.get_data()
    idx = data["index"]
    product_ids = data["product_ids"]
    product = db.get_product(product_ids[idx])

    if action == "manual":
        await cb.message.edit_text(
            f"«{product['name']}»\nВведите число ({product['unit']}, ≥ 0):",
        )
        await state.set_state(OrderForm.manual_entry)
        await cb.answer()
        return

    if action == "back":
        if idx > 0:
            answers = data["answers"]
            answers.pop(str(product_ids[idx - 1]), None)
            await state.update_data(index=idx - 1, answers=answers)
            await advance_to_item(cb.message, state)
        await cb.answer()
        return

    if action == "next":
        answers = data["answers"]
        answers[str(product["id"])] = data["current_value"]
        await state.update_data(answers=answers, index=idx + 1)
        await advance_to_item(cb.message, state)
        await cb.answer()
        return

    # действия +N / -N
    delta = float(action)
    new_value = max(0, data["current_value"] + delta)
    await state.update_data(current_value=new_value)
    await cb.message.edit_text(
        render_item_text(product, new_value, idx + 1, len(product_ids)),
        parse_mode="HTML",
        reply_markup=item_keyboard(show_back=idx > 0),
    )
    await cb.answer()


@router.message(OrderForm.manual_entry)
async def receive_manual_value(message: Message, state: FSMContext):
    text = (message.text or "").strip().replace(",", ".")
    try:
        val = float(text)
        if val < 0:
            raise ValueError
    except ValueError:
        await message.answer("Нужно число ≥ 0, например: 6 или 4.5. Введите ещё раз:")
        return

    data = await state.get_data()
    idx = data["index"]
    product_ids = data["product_ids"]
    answers = data["answers"]
    answers[str(product_ids[idx])] = val
    await state.update_data(answers=answers, index=idx + 1)
    await state.set_state(OrderForm.filling)
    await advance_to_item(message, state)


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
    await state.update_data(answers={}, index=0, current_value=None, last_shown_category=None)
    await state.set_state(OrderForm.filling)
    await cb.message.edit_text(f"Заполняем заново: <b>{data['category_label']}</b>", parse_mode="HTML")
    await advance_to_item(cb.message, state)
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


class RecipeAdminForm(StatesGroup):
    menu = State()
    add_name = State()
    add_category = State()
    add_category_custom = State()
    add_ingredients = State()
    add_method = State()
    add_photo = State()
    listing = State()
    confirm_delete = State()


def admin_menu_keyboard():
    kb = [
        [InlineKeyboardButton(text="➕ Категория", callback_data="admin:add_category")],
        [InlineKeyboardButton(text="➕ Позиция", callback_data="admin:add_product")],
        [InlineKeyboardButton(text="✏️ Мин/Макс позиции", callback_data="admin:edit_minmax")],
        [InlineKeyboardButton(text="✏️ Переименовать позицию", callback_data="admin:rename_product")],
        [InlineKeyboardButton(text="✏️ Переименовать категорию", callback_data="admin:rename_category")],
        [InlineKeyboardButton(text="🗑 Удалить позицию", callback_data="admin:delete_product")],
        [InlineKeyboardButton(text="🗑 Удалить категорию", callback_data="admin:delete_category")],
        [InlineKeyboardButton(text="🗑 Удалить точку целиком", callback_data="admin:delete_point")],
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
    await cb.message.edit_text("Выберите точку для редактирования каталога:", reply_markup=admin_points_keyboard())
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


@router.callback_query(F.data == "admin:reimport")
async def admin_reimport_start(cb: CallbackQuery, state: FSMContext):
    await state.update_data(pending_action="reimport_excel")
    await cb.message.edit_text(
        "📥 Загрузить каталог из seed_products.xlsx?\n\n"
        "⚠️ Для каждой точки, перечисленной на листе «Точки» этого файла, "
        "её текущие категории и позиции будут <b>полностью заменены</b> тем, "
        "что в файле — включая всё, что вы добавляли через /admin вручную. "
        "Точки, которых нет в файле, не затронутся.",
        reply_markup=confirm_keyboard(),
        parse_mode="HTML",
    )
    await state.set_state(AdminForm.confirm_delete)
    await cb.answer()


# ---- Тех.карты: управление (только админ) ----

def recipe_admin_menu_keyboard():
    kb = [
        [InlineKeyboardButton(text="➕ Добавить рецепт", callback_data="radm:add")],
        [InlineKeyboardButton(text="📋 Список / удалить", callback_data="radm:list")],
        [InlineKeyboardButton(text="⬅️ Назад", callback_data="admin:back_points")],
    ]
    return InlineKeyboardMarkup(inline_keyboard=kb)


def parse_ingredient_line(line: str):
    """«Свёкла — 200 г» -> (Свёкла, 200.0, г). «Соль — по вкусу» -> (Соль, None, по вкусу).
    «Укроп» (без разделителя) -> (Укроп, None, None)."""
    line = line.strip()
    if not line:
        return None
    parts = re.split(r"\s+—\s+|\s+-\s+", line, maxsplit=1)
    if len(parts) != 2:
        return (line, None, None)
    name, rest = parts[0].strip(), parts[1].strip()
    m = re.match(r"^([\d.,]+)\s*(.*)$", rest)
    if m:
        amount = float(m.group(1).replace(",", "."))
        unit = m.group(2).strip() or None
        return (name, amount, unit)
    return (name, None, rest or None)


def format_method(text: str) -> str:
    """Если шаги уже пронумерованы — оставляет как есть, иначе нумерует сама."""
    lines = [l.strip() for l in text.splitlines() if l.strip()]
    if not lines:
        return ""
    if all(re.match(r"^\d+[.)]", l) for l in lines):
        return "\n".join(lines)
    return "\n".join(f"{i}. {l}" for i, l in enumerate(lines, 1))


@router.callback_query(F.data == "admin:recipes")
async def admin_recipes_menu(cb: CallbackQuery, state: FSMContext):
    if not is_admin(cb.from_user.id):
        await cb.answer("Только для администраторов.", show_alert=True)
        return
    await cb.message.edit_text("📖 Тех.карты — управление", reply_markup=recipe_admin_menu_keyboard())
    await state.set_state(RecipeAdminForm.menu)
    await cb.answer()


@router.callback_query(RecipeAdminForm.menu, F.data == "radm:add")
async def radm_add_start(cb: CallbackQuery, state: FSMContext):
    await cb.message.edit_text("Введите название блюда:")
    await state.set_state(RecipeAdminForm.add_name)
    await cb.answer()


def recipe_category_pick_keyboard(options):
    kb = [[InlineKeyboardButton(text=c, callback_data=f"radmcat:{i}")] for i, c in enumerate(options)]
    kb.append([InlineKeyboardButton(text="➕ Другая категория", callback_data="radmcat:NEW")])
    kb.append([InlineKeyboardButton(text="🚫 Без категории", callback_data="radmcat:NONE")])
    return InlineKeyboardMarkup(inline_keyboard=kb)


@router.message(RecipeAdminForm.add_name)
async def radm_add_name(message: Message, state: FSMContext):
    name = (message.text or "").strip()
    if not name:
        await message.answer("Название не может быть пустым. Введите ещё раз:")
        return
    await state.update_data(new_name=name)

    used = {r["category"] for r in db.list_recipes() if r["category"]}
    options = list(RECIPE_CATEGORY_PRESETS) + [c for c in sorted(used) if c not in RECIPE_CATEGORY_PRESETS]
    await state.update_data(recipe_category_options=options)
    await message.answer("Раздел меню для этого блюда:", reply_markup=recipe_category_pick_keyboard(options))
    await state.set_state(RecipeAdminForm.add_category)


@router.callback_query(RecipeAdminForm.add_category, F.data.startswith("radmcat:"))
async def radm_add_category_picked(cb: CallbackQuery, state: FSMContext):
    raw = cb.data.split(":", 1)[1]
    if raw == "NEW":
        await cb.message.edit_text("Введите название нового раздела меню:")
        await state.set_state(RecipeAdminForm.add_category_custom)
        await cb.answer()
        return
    category = "" if raw == "NONE" else (await state.get_data())["recipe_category_options"][int(raw)]
    await state.update_data(new_category=category)
    await cb.message.edit_text(
        "Ингредиенты — по одному на строку, в формате «Название — количество ед»:\n"
        "<code>Свёкла — 200 г\nКапуста — 150 г\nСоль — по вкусу</code>",
        parse_mode="HTML",
    )
    await state.set_state(RecipeAdminForm.add_ingredients)
    await cb.answer()


@router.message(RecipeAdminForm.add_category_custom)
async def radm_add_category_custom(message: Message, state: FSMContext):
    category = (message.text or "").strip()
    if not category:
        await message.answer("Название раздела не может быть пустым. Введите ещё раз:")
        return
    await state.update_data(new_category=category)
    await message.answer(
        "Ингредиенты — по одному на строку, в формате «Название — количество ед»:\n"
        "<code>Свёкла — 200 г\nКапуста — 150 г\nСоль — по вкусу</code>",
        parse_mode="HTML",
    )
    await state.set_state(RecipeAdminForm.add_ingredients)


@router.message(RecipeAdminForm.add_ingredients)
async def radm_add_ingredients(message: Message, state: FSMContext):
    lines = (message.text or "").splitlines()
    parsed = [parse_ingredient_line(l) for l in lines]
    parsed = [p for p in parsed if p]
    if not parsed:
        await message.answer("Не нашёл ни одной строки с ингредиентом. Введите ещё раз, каждый на новой строке:")
        return
    await state.update_data(new_ingredients=parsed)
    await message.answer("Способ приготовления — по шагам (каждый шаг на новой строке, нумеровать не обязательно):")
    await state.set_state(RecipeAdminForm.add_method)


@router.message(RecipeAdminForm.add_method)
async def radm_add_method(message: Message, state: FSMContext):
    method = format_method(message.text or "")
    if not method:
        await message.answer("Нужен хотя бы один шаг. Введите ещё раз:")
        return
    data = await state.get_data()
    recipe_id = db.create_recipe(data["new_name"], data["new_category"], method)
    for i, (ing_name, amount, unit) in enumerate(data["new_ingredients"]):
        db.add_recipe_ingredient(recipe_id, ing_name, amount, unit, i)
    await state.update_data(new_recipe_id=recipe_id)
    await message.answer(
        f"Рецепт «{data['new_name']}» сохранён.\n"
        f"Пришлите фото готового блюда одним сообщением, или «-» чтобы пропустить:"
    )
    await state.set_state(RecipeAdminForm.add_photo)


@router.message(RecipeAdminForm.add_photo, F.photo)
async def radm_add_photo_received(message: Message, state: FSMContext):
    data = await state.get_data()
    file_id = message.photo[-1].file_id
    db.set_recipe_photo(data["new_recipe_id"], file_id)
    await message.answer("Фото добавлено. ✅")
    await radm_back_to_menu(message, state)


@router.message(RecipeAdminForm.add_photo)
async def radm_add_photo_skip(message: Message, state: FSMContext):
    if (message.text or "").strip() != "-":
        await message.answer("Пришлите фото как изображение, или «-» чтобы пропустить:")
        return
    await radm_back_to_menu(message, state)


async def radm_back_to_menu(message: Message, state: FSMContext):
    await message.answer("📖 Тех.карты — управление", reply_markup=recipe_admin_menu_keyboard())
    await state.set_state(RecipeAdminForm.menu)


@router.callback_query(RecipeAdminForm.menu, F.data == "radm:list")
async def radm_list(cb: CallbackQuery, state: FSMContext):
    recipes = db.list_recipes()
    if not recipes:
        await cb.answer("Рецептов пока нет.", show_alert=True)
        return
    kb = [[InlineKeyboardButton(text=r["name"], callback_data=f"radmview:{r['id']}")] for r in recipes]
    kb.append([InlineKeyboardButton(text="⬅️ Назад", callback_data="radm:back")])
    await cb.message.edit_text("Выберите рецепт:", reply_markup=InlineKeyboardMarkup(inline_keyboard=kb))
    await state.set_state(RecipeAdminForm.listing)
    await cb.answer()


@router.callback_query(RecipeAdminForm.menu, F.data == "radm:back")
@router.callback_query(RecipeAdminForm.listing, F.data == "radm:back")
async def radm_back(cb: CallbackQuery, state: FSMContext):
    await cb.message.edit_text("📖 Тех.карты — управление", reply_markup=recipe_admin_menu_keyboard())
    await state.set_state(RecipeAdminForm.menu)
    await cb.answer()


@router.callback_query(RecipeAdminForm.listing, F.data.startswith("radmview:"))
async def radm_view(cb: CallbackQuery, state: FSMContext):
    recipe_id = int(cb.data.split(":")[1])
    recipe = db.get_recipe(recipe_id)
    ingredients = db.list_recipe_ingredients(recipe_id)
    if recipe["photo_file_id"]:
        await cb.message.answer_photo(recipe["photo_file_id"])
    kb = [
        [InlineKeyboardButton(text="🗑 Удалить рецепт", callback_data=f"radmdel:{recipe_id}")],
        [InlineKeyboardButton(text="⬅️ К списку", callback_data="radm:list")],
    ]
    await cb.message.answer(
        format_recipe_card(recipe, ingredients), parse_mode="HTML", reply_markup=InlineKeyboardMarkup(inline_keyboard=kb)
    )
    await cb.answer()


@router.callback_query(RecipeAdminForm.listing, F.data.startswith("radmdel:"))
async def radm_delete_confirm(cb: CallbackQuery, state: FSMContext):
    recipe_id = int(cb.data.split(":")[1])
    recipe = db.get_recipe(recipe_id)
    await state.update_data(delete_recipe_id=recipe_id)
    await cb.message.edit_text(f"Удалить рецепт «{recipe['name']}»?", reply_markup=confirm_keyboard())
    await state.set_state(RecipeAdminForm.confirm_delete)
    await cb.answer()


@router.callback_query(RecipeAdminForm.confirm_delete, F.data.startswith("admconfirm:"))
async def radm_delete_execute(cb: CallbackQuery, state: FSMContext):
    answer = cb.data.split(":")[1]
    data = await state.get_data()
    if answer == "yes":
        recipe = db.get_recipe(data["delete_recipe_id"])
        db.delete_recipe(data["delete_recipe_id"])
        await cb.message.edit_text(f"Рецепт «{recipe['name']}» удалён.")
    else:
        await cb.message.edit_text("Отменено.")
    await cb.message.answer("📖 Тех.карты — управление", reply_markup=recipe_admin_menu_keyboard())
    await state.set_state(RecipeAdminForm.menu)
    await cb.answer()


# ---- База знаний: управление (только админ) ----

class KBAdminForm(StatesGroup):
    picking_section = State()
    menu = State()
    add_title = State()
    add_body = State()
    add_photo = State()
    listing = State()
    confirm_delete = State()


def kb_admin_sections_keyboard():
    kb = [
        [InlineKeyboardButton(text=f"{KB_SECTION_ICONS[s]} {s}", callback_data=f"kbadmsec:{i}")]
        for i, s in enumerate(KB_SECTIONS)
    ]
    kb.append([InlineKeyboardButton(text="⬅️ Назад", callback_data="admin:back_points")])
    return InlineKeyboardMarkup(inline_keyboard=kb)


def kb_admin_menu_keyboard():
    kb = [
        [InlineKeyboardButton(text="➕ Добавить статью", callback_data="kbadm:add")],
        [InlineKeyboardButton(text="📋 Список / удалить", callback_data="kbadm:list")],
        [InlineKeyboardButton(text="⬅️ Разделы", callback_data="admin:kb")],
    ]
    return InlineKeyboardMarkup(inline_keyboard=kb)


@router.callback_query(F.data == "admin:kb")
async def admin_kb_sections(cb: CallbackQuery, state: FSMContext):
    if not is_admin(cb.from_user.id):
        await cb.answer("Только для администраторов.", show_alert=True)
        return
    await cb.message.edit_text("📚 Обучение — выберите раздел:", reply_markup=kb_admin_sections_keyboard())
    await state.set_state(KBAdminForm.picking_section)
    await cb.answer()


@router.callback_query(KBAdminForm.picking_section, F.data.startswith("kbadmsec:"))
async def kbadm_section_chosen(cb: CallbackQuery, state: FSMContext):
    section = KB_SECTIONS[int(cb.data.split(":")[1])]
    await state.update_data(kb_section=section)
    await cb.message.edit_text(
        f"{KB_SECTION_ICONS[section]} <b>{section}</b> — управление",
        reply_markup=kb_admin_menu_keyboard(),
        parse_mode="HTML",
    )
    await state.set_state(KBAdminForm.menu)
    await cb.answer()


@router.callback_query(KBAdminForm.menu, F.data == "kbadm:add")
async def kbadm_add_start(cb: CallbackQuery, state: FSMContext):
    await cb.message.edit_text("Заголовок статьи:")
    await state.set_state(KBAdminForm.add_title)
    await cb.answer()


@router.message(KBAdminForm.add_title)
async def kbadm_add_title(message: Message, state: FSMContext):
    title = (message.text or "").strip()
    if not title:
        await message.answer("Заголовок не может быть пустым. Введите ещё раз:")
        return
    await state.update_data(new_title=title)
    await message.answer("Текст статьи:")
    await state.set_state(KBAdminForm.add_body)


@router.message(KBAdminForm.add_body)
async def kbadm_add_body(message: Message, state: FSMContext):
    body = (message.text or "").strip()
    if not body:
        await message.answer("Текст не может быть пустым. Введите ещё раз:")
        return
    data = await state.get_data()
    article_id = db.create_kb_article(data["kb_section"], data["new_title"], body)
    await state.update_data(new_article_id=article_id)
    await message.answer(
        f"Статья «{data['new_title']}» сохранена.\nПришлите фото/схему, или «-» чтобы пропустить:"
    )
    await state.set_state(KBAdminForm.add_photo)


@router.message(KBAdminForm.add_photo, F.photo)
async def kbadm_add_photo_received(message: Message, state: FSMContext):
    data = await state.get_data()
    file_id = message.photo[-1].file_id
    db.set_kb_article_photo(data["new_article_id"], file_id)
    await message.answer("Фото добавлено. ✅")
    await kbadm_back_to_menu(message, state)


@router.message(KBAdminForm.add_photo)
async def kbadm_add_photo_skip(message: Message, state: FSMContext):
    if (message.text or "").strip() != "-":
        await message.answer("Пришлите фото как изображение, или «-» чтобы пропустить:")
        return
    await kbadm_back_to_menu(message, state)


async def kbadm_back_to_menu(message: Message, state: FSMContext):
    data = await state.get_data()
    section = data["kb_section"]
    await message.answer(
        f"{KB_SECTION_ICONS[section]} <b>{section}</b> — управление",
        reply_markup=kb_admin_menu_keyboard(),
        parse_mode="HTML",
    )
    await state.set_state(KBAdminForm.menu)


@router.callback_query(KBAdminForm.menu, F.data == "kbadm:list")
async def kbadm_list(cb: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    articles = db.list_kb_articles(data["kb_section"])
    if not articles:
        await cb.answer("Статей пока нет.", show_alert=True)
        return
    kb = [[InlineKeyboardButton(text=a["title"], callback_data=f"kbadmview:{a['id']}")] for a in articles]
    kb.append([InlineKeyboardButton(text="⬅️ Назад", callback_data="kbadm:back")])
    await cb.message.edit_text("Выберите статью:", reply_markup=InlineKeyboardMarkup(inline_keyboard=kb))
    await state.set_state(KBAdminForm.listing)
    await cb.answer()


@router.callback_query(KBAdminForm.menu, F.data == "kbadm:back")
@router.callback_query(KBAdminForm.listing, F.data == "kbadm:back")
async def kbadm_back(cb: CallbackQuery, state: FSMContext):
    await kbadm_back_to_menu(cb.message, state)
    await cb.answer()


@router.callback_query(KBAdminForm.listing, F.data.startswith("kbadmview:"))
async def kbadm_view(cb: CallbackQuery, state: FSMContext):
    article_id = int(cb.data.split(":")[1])
    article = db.get_kb_article(article_id)
    if article["photo_file_id"]:
        await cb.message.answer_photo(article["photo_file_id"])
    kb = [
        [InlineKeyboardButton(text="🗑 Удалить статью", callback_data=f"kbadmdel:{article_id}")],
        [InlineKeyboardButton(text="⬅️ К списку", callback_data="kbadm:list")],
    ]
    await cb.message.answer(
        format_kb_article(article), parse_mode="HTML", reply_markup=InlineKeyboardMarkup(inline_keyboard=kb)
    )
    await cb.answer()


@router.callback_query(KBAdminForm.listing, F.data.startswith("kbadmdel:"))
async def kbadm_delete_confirm(cb: CallbackQuery, state: FSMContext):
    article_id = int(cb.data.split(":")[1])
    article = db.get_kb_article(article_id)
    await state.update_data(delete_article_id=article_id)
    await cb.message.edit_text(f"Удалить статью «{article['title']}»?", reply_markup=confirm_keyboard())
    await state.set_state(KBAdminForm.confirm_delete)
    await cb.answer()


@router.callback_query(KBAdminForm.confirm_delete, F.data.startswith("admconfirm:"))
async def kbadm_delete_execute(cb: CallbackQuery, state: FSMContext):
    answer = cb.data.split(":")[1]
    data = await state.get_data()
    if answer == "yes":
        article = db.get_kb_article(data["delete_article_id"])
        db.delete_kb_article(data["delete_article_id"])
        await cb.message.edit_text(f"Статья «{article['title']}» удалена.")
    else:
        await cb.message.edit_text("Отменено.")
    await kbadm_back_to_menu(cb.message, state)
    await cb.answer()
async def admin_delete_point_start(cb: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    point = db.get_point(data["point_id"])
    await state.update_data(pending_action="delete_point")
    await cb.message.edit_text(
        f"Удалить точку «{point['name']}» целиком — вместе со всеми категориями, "
        f"позициями и историей остатков? Это необратимо.",
        reply_markup=confirm_keyboard(),
    )
    await state.set_state(AdminForm.confirm_delete)
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
        elif action == "delete_point":
            point = db.get_point(data["point_id"])
            db.delete_point(data["point_id"])
            await cb.message.edit_text(f"Точка «{point['name']}» удалена целиком.")
            await state.clear()
            await cb.message.answer("Выберите точку для редактирования каталога:", reply_markup=admin_points_keyboard())
            await state.set_state(AdminForm.picking_point)
            await cb.answer()
            return
        elif action == "reimport_excel":
            seed_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "seed_products.xlsx")
            try:
                import import_products
                result = import_products.main(seed_path)
                text = f"✅ Импорт завершён. Точек: {result['points']}, позиций: {result['products']}."
                if result["skipped"]:
                    text += f"\n\n⚠️ Пропущено строк каталога: {len(result['skipped'])}:\n" + "\n".join(
                        f"• {s}" for s in result["skipped"][:10]
                    )
                    if len(result["skipped"]) > 10:
                        text += f"\n…и ещё {len(result['skipped']) - 10}"
                await cb.message.edit_text(text)
            except Exception as e:
                await cb.message.edit_text(f"⚠️ Не удалось выполнить импорт: {e}")
            await state.clear()
            await cb.message.answer("Выберите точку для редактирования каталога:", reply_markup=admin_points_keyboard())
            await state.set_state(AdminForm.picking_point)
            await cb.answer()
            return
        elif action == "reimport_content":
            content_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "seed_content.xlsx")
            try:
                import import_content
                result = import_content.main(content_path)
                text = (
                    f"✅ Импорт завершён.\n"
                    f"📖 Рецепты: добавлено {result['recipes_added']}, обновлено {result['recipes_updated']}\n"
                    f"📚 Обучение: добавлено {result['kb_added']}, обновлено {result['kb_updated']}"
                )
                all_skipped = result["recipes_skipped"] + result["kb_skipped"]
                if all_skipped:
                    text += f"\n\n⚠️ Пропущено строк: {len(all_skipped)}:\n" + "\n".join(
                        f"• {s}" for s in all_skipped[:10]
                    )
                    if len(all_skipped) > 10:
                        text += f"\n…и ещё {len(all_skipped) - 10}"
                await cb.message.edit_text(text)
            except Exception as e:
                await cb.message.edit_text(f"⚠️ Не удалось выполнить импорт: {e}")
            await state.clear()
            await cb.message.answer("Выберите точку для редактирования каталога:", reply_markup=admin_points_keyboard())
            await state.set_state(AdminForm.picking_point)
            await cb.answer()
            return
    else:
        await cb.message.edit_text("Отменено.")
    await show_admin_menu(cb.message, state)
    await cb.answer()


@router.callback_query(F.data == "admin:reimport_content")
async def admin_reimport_content_start(cb: CallbackQuery, state: FSMContext):
    await state.update_data(pending_action="reimport_content")
    await cb.message.edit_text(
        "📥 Загрузить тех.карты и статьи обучения из seed_content.xlsx?\n\n"
        "Блюда и статьи с тем же названием (для рецепта) или разделом+заголовком (для статьи) "
        "будут <b>обновлены</b> — ингредиенты и текст заменятся содержимым файла. "
        "Новые названия просто добавятся. Фото через этот импорт не переносятся — "
        "их по-прежнему нужно добавлять внутри бота.",
        reply_markup=confirm_keyboard(),
        parse_mode="HTML",
    )
    await state.set_state(AdminForm.confirm_delete)
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

    if not db.list_points():
        seed_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "seed_products.xlsx")
        if os.path.exists(seed_path):
            print(f"⚠️  Каталог пуст (похоже, база сбросилась при деплое) — "
                  f"автоматически восстанавливаю из {seed_path}...")
            try:
                import import_products
                import_products.main(seed_path)
                print("✅ Каталог восстановлен из seed_products.xlsx.")
            except Exception as e:
                print(f"⚠️  Не удалось автоматически восстановить каталог: {e}. "
                      f"Создайте точки вручную через /admin.")
        else:
            print("⚠️  Каталог пуст, и seed_products.xlsx не найден рядом с bot.py — "
                  "создайте точки вручную через /admin.")

    bot = Bot(token)
    await setup_commands(bot)
    dp = Dispatcher(storage=MemoryStorage())
    dp.include_router(router)
    asyncio.create_task(reminder_loop(bot))
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
