"""Самопроверка парсера, импортёра и запросов. Запуск: python test_parsing.py"""
import asyncio
import os
import sys
import tempfile
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / 'scripts'))
os.environ.setdefault('VK_TOKEN', 'dummy')  # для импорта bot.main

from bot import db, parsing  # noqa: E402
import import_history  # noqa: E402

FIXTURE = """03.08.2022, 17:22:53 | Е. Океанов (vk.ru/id45749440) | cmid: 5
Привет всем! Это первое сообщение

03.08.2022, 17:24:54 | Е. Океанов (vk.ru/id45749440) | cmid: 9
[Ответ на сообщение: cmid 5]
Отвечаю сам себе, вот ссылка https://www.youtube.com/watch?v=x

03.08.2022, 18:00:00 | Действие "Смена фотографии беседы" | cmid: 10
Е. Океанов сменил фотографию беседы

04.08.2022, 01:30:00 | А. Тестова (vk.ru/id123) | cmid: 11
[Фото (https://sun9-1.userapi.com/x.jpg)]
Смотрите какое фото

04.08.2022, 02:00:00 | А. Тестова (vk.ru/id123) | cmid: 12
[Нет текста]
[Стикер]

04.08.2022, 12:00:00 | Iris (vk.ru/club190546023) | cmid: 13
Бот-модератор пишет

04.08.2022, 13:00:00 | Е. Океанов (vk.ru/id45749440) | cmid: 14
Многострочное

с пустой строкой внутри
"""


def test_parser():
    blocks = list(parsing.parse_export(FIXTURE.splitlines()))
    assert len(blocks) == 7, len(blocks)

    by_cmid = {b['cmid']: b for b in blocks}
    assert by_cmid[10]['service'] is True
    assert sum(b['service'] for b in blocks) == 1

    first = by_cmid[5]
    assert first['vk_id'] == 45749440
    assert first['display_name'] == 'Е. Океанов'
    assert first['ts'] == '2022-08-03T17:22:53'
    assert first['word_count'] == 5, first['word_count']

    reply = by_cmid[9]
    assert reply['is_reply'] == 1 and reply['reply_to_cmid'] == 5

    photo = by_cmid[11]
    assert photo['has_photo'] == 1 and photo['text'] == 'Смотрите какое фото'

    sticker = by_cmid[12]
    assert sticker['has_sticker'] == 1 and sticker['text'] == '' and sticker['word_count'] == 0

    club = by_cmid[13]
    assert club['vk_id'] == -190546023 and club['is_community'] == 1

    multi = by_cmid[14]
    assert multi['text'] == 'Многострочное\n\nс пустой строкой внутри', repr(multi['text'])
    print('test_parser ok')


async def _test_import_and_queries(tmp: str):
    export = Path(tmp) / 'export.txt'
    export.write_text(FIXTURE, encoding='utf-8')
    db_path = str(Path(tmp) / 'test.db')

    stats = await import_history.import_file(str(export), db_path)
    assert stats == {'headers': 7, 'service_skipped': 1, 'inserted': 6, 'duplicates': 0}, stats

    stats2 = await import_history.import_file(str(export), db_path)
    assert stats2['inserted'] == 0 and stats2['duplicates'] == 6, stats2  # идемпотентность

    conn = await db.connect(db_path)
    try:
        assert await db.get_total_count(conn) == 6

        s = await db.get_activity_stats(conn, 45749440, today=date(2022, 8, 4))
        assert s['total'] == 3
        assert s['streak'] == 2, s['streak']
        assert s['night_share'] == 0 and s['morning_share'] == 0

        s2 = await db.get_activity_stats(conn, 123, today=date(2022, 8, 4))
        assert s2['night_share'] == 1.0

        chat = await db.get_activity_stats(conn, today=date(2022, 8, 4))
        assert chat['total'] == 6 and chat['streak'] == 2

        ts_user = await db.get_timestamps(conn, 45749440)
        assert len(ts_user) == 3 and ts_user == sorted(ts_user)
        ts_all = await db.get_timestamps(conn)
        assert len(ts_all) == 6

        from datetime import datetime
        from bot import chart
        png = Path(tmp) / 'card.png'
        chart.build_chart([datetime.fromisoformat(t) for t in ts_all], str(png), 'Тест')
        assert png.stat().st_size > 10_000, png.stat().st_size

        png2 = Path(tmp) / 'compare.png'
        chart.build_compare_chart(
            ('A', [datetime.fromisoformat(t) for t in ts_user]),
            ('B', [datetime.fromisoformat(t) for t in await db.get_timestamps(conn, 123)]),
            str(png2))
        assert png2.stat().st_size > 10_000, png2.stat().st_size
    finally:
        await conn.close()
    print('test_import_and_queries ok')


def test_build_msg_row():
    from types import SimpleNamespace as NS
    from bot.main import build_msg_row

    att = NS(type=NS(value='photo'))
    reply = NS(conversation_message_id=41)
    item = NS(conversation_message_id=42, from_id=123, date=1690000000,
              text='привет мир', attachments=[att], reply_message=reply,
              fwd_messages=None)
    row = build_msg_row(item)
    assert row['cmid'] == 42 and row['vk_id'] == 123
    assert row['word_count'] == 2 and row['has_photo'] == 1
    assert row['is_reply'] == 1 and row['reply_to_cmid'] == 41
    assert row['ts'].startswith('2023-07-22T'), row['ts']  # unix int сконвертирован

    bad = NS(conversation_message_id=None, from_id=1, date=0, text='',
             attachments=None, reply_message=None, fwd_messages=None)
    assert build_msg_row(bad) is None
    print('test_build_msg_row ok')


if __name__ == '__main__':
    test_parser()
    test_build_msg_row()
    with tempfile.TemporaryDirectory() as tmp:
        asyncio.run(_test_import_and_queries(tmp))
    print('все проверки прошли')
