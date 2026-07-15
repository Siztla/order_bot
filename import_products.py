"""
Загружает каталог позиций из seed_products.xlsx в базу.
Запускать заново каждый раз, когда меняете точки/категории/позиции в Excel.
(Если каталог уже отредактирован через бота — повторный запуск ПЕРЕЗАПИШЕТ
его содержимым Excel-файла, будьте аккуратны.)

Использование:
    python import_products.py [путь_к_файлу.xlsx]
"""
import sys
import openpyxl
import db


def main(path: str = "seed_products.xlsx"):
    db.init_db()
    wb = openpyxl.load_workbook(path, data_only=True)

    # --- Лист "Точки" ---
    ws_points = wb["Точки"]
    point_ids = {}
    for row in ws_points.iter_rows(min_row=5, values_only=True):
        name, chat_id, header_template = (row + (None, None, None))[:3]
        if not name:
            continue
        chat_id = int(chat_id) if chat_id not in (None, "") else None
        header_template = header_template or "📦 Заказ — {point}, {date}"
        point_id = db.upsert_point(str(name).strip(), chat_id, str(header_template).strip())
        point_ids[str(name).strip()] = point_id
        db.clear_products_for_point(point_id)   # products first (FK -> categories)
        db.clear_categories_for_point(point_id)
        print(f"Точка «{name}» -> id={point_id} (chat_id={chat_id})")

    # --- Лист "Позиции" ---
    ws_prod = wb["Позиции"]
    sort_counters = {}
    added = 0
    for row in ws_prod.iter_rows(min_row=5, values_only=True):
        point_name, category, name, unit, min_qty, max_qty = (row + (None,) * 6)[:6]
        if not point_name or not name:
            continue
        point_name = str(point_name).strip()
        if point_name not in point_ids:
            print(f"  ! Точка «{point_name}» не найдена на листе «Точки» — строка пропущена: {name}")
            continue
        if min_qty is None or max_qty is None:
            print(f"  ! Пропущена позиция без мин/макс: {name}")
            continue
        point_id = point_ids[point_name]
        category_name = str(category).strip() if category else "БЕЗ КАТЕГОРИИ"
        category_id = db.get_or_create_category(point_id, category_name)

        key = (point_id, category_id)
        sort_counters.setdefault(key, 0)
        db.add_product(
            point_id=point_id,
            category_id=category_id,
            name=str(name).strip(),
            unit=str(unit).strip() if unit else "шт",
            min_qty=float(min_qty),
            max_qty=float(max_qty),
            sort_order=sort_counters[key],
        )
        sort_counters[key] += 1
        added += 1

    print(f"\nГотово. Загружено позиций: {added}")


if __name__ == "__main__":
    path = sys.argv[1] if len(sys.argv) > 1 else "seed_products.xlsx"
    main(path)
