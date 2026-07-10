"""Одноразовый импорт выгрузки чата (data/export.txt) в БД.

Идемпотентен: повторный запуск ничего не дублирует (UNIQUE(cmid)).
Запуск из корня проекта: python scripts/import_history.py
"""
import asyncio
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from bot import db, parsing  # noqa: E402

EXPORT_PATH = os.getenv('EXPORT_PATH', 'data/export.txt')


async def import_file(export_path: str, db_path: str | None = None) -> dict:
    conn = await db.connect(db_path)
    stats = {'headers': 0, 'service_skipped': 0, 'inserted': 0, 'duplicates': 0}
    users: dict[int, dict] = {}
    try:
        with open(export_path, encoding='utf-8-sig') as f:
            for block in parsing.parse_export(f):
                stats['headers'] += 1
                if block['service']:
                    stats['service_skipped'] += 1
                    continue
                u = users.setdefault(block['vk_id'], {
                    'display_name': block['display_name'],
                    'is_community': block['is_community'],
                    'first': block['ts'], 'last': block['ts'],
                })
                u['first'] = min(u['first'], block['ts'])
                u['last'] = max(u['last'], block['ts'])
                u['display_name'] = block['display_name']
                if await db.insert_message(conn, block):
                    stats['inserted'] += 1
                else:
                    stats['duplicates'] += 1
        for vk_id, u in users.items():
            await db.upsert_user(conn, vk_id, u['display_name'], u['is_community'], u['first'])
            await db.upsert_user(conn, vk_id, u['display_name'], u['is_community'], u['last'])
        await conn.commit()
    finally:
        await conn.close()
    return stats


def main():
    if not os.path.exists(EXPORT_PATH):
        sys.exit(f'Файл не найден: {EXPORT_PATH} (путь настраивается через EXPORT_PATH в .env)')
    stats = asyncio.run(import_file(EXPORT_PATH))
    print(f"Заголовков в файле:   {stats['headers']}")
    print(f"Служебных пропущено:  {stats['service_skipped']}")
    print(f"Импортировано:        {stats['inserted']}")
    print(f"Дубликатов (пропуск): {stats['duplicates']}")


if __name__ == '__main__':
    main()
