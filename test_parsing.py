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

05.08.2022, 10:00:00 | Е. Океанов пригласил Д. Мещерякова (vk.ru/id999) | cmid: 15
Е. Океанов пригласил Д. Мещерякова
"""


def test_parser():
    blocks = list(parsing.parse_export(FIXTURE.splitlines()))
    assert len(blocks) == 8, len(blocks)

    by_cmid = {b['cmid']: b for b in blocks}
    assert by_cmid[10]['service'] is True
    assert by_cmid[15]['service'] is True  # «X пригласил Y» — событие, не сообщение Y
    assert sum(b['service'] for b in blocks) == 2

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
    assert stats == {'headers': 8, 'service_skipped': 2, 'inserted': 6, 'duplicates': 0}, stats

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

        # фильтр по периоду: только 04.08 -> 4 сообщения; streak от конца периода
        aug4 = await db.get_activity_stats(conn, since='2022-08-04T00:00:00',
                                           until='2022-08-04T23:59:59')
        assert aug4['total'] == 4 and aug4['streak'] == 1, aug4  # 03.08 вне периода
        ts_aug3 = await db.get_timestamps(conn, until='2022-08-03T23:59:59')
        assert len(ts_aug3) == 2
        rows_aug4 = await db.get_human_messages(conn, since='2022-08-04T00:00:00')
        assert len(rows_aug4) == 3  # club исключён

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


def test_ranking():
    from bot.ranking import compute_ranking, place_emoji

    # A пишет первым, B догоняет и обгоняет, C присоединяется последним
    rows = [(1, '2022-01-01T10:00:00'),
            (2, '2022-01-02T10:00:00'),
            (2, '2022-01-03T10:00:00'),
            (3, '2022-01-04T10:00:00')]
    places = compute_ranking(rows)
    assert [p['vk_id'] for p in places] == [2, 1, 3]

    b, a, c = places
    assert b['count'] == 2 and b['event']['kind'] == 'up' and b['event']['other'] == 1
    assert b['event']['date'] == '2022-01-03T10:00:00'
    assert a['event']['kind'] == 'down' and a['event']['other'] == 2
    assert c['event'] is None  # на своём месте с самого начала — алмаз

    # равный счёт: выше тот, кто достиг его раньше
    rows2 = rows + [(3, '2022-01-05T10:00:00')]  # C догнал A по счёту 1->2? нет: A=1, C=2
    places2 = compute_ranking(rows2)
    assert [p['vk_id'] for p in places2] == [2, 3, 1]  # C(2) обошёл A(1)
    assert places2[1]['event']['kind'] == 'up' and places2[1]['event']['other'] == 1

    assert place_emoji(1) == '🥇' and place_emoji(4) == '⚡' and place_emoji(9) == '🔸'
    print('test_ranking ok')


def test_parse_period():
    from bot.main import parse_period

    assert parse_period('') == (None, None)
    assert parse_period('(01/01/2023-31/12/2023)') == ('2023-01-01T00:00:00', '2023-12-31T23:59:59')
    assert parse_period('(Х-05/06/2024)') == (None, '2024-06-05T23:59:59')
    assert parse_period('(x-05/06/2024)') == (None, '2024-06-05T23:59:59')
    assert parse_period('(15/03/2023)') == ('2023-03-15T00:00:00', None)
    # разные форматы дат: точки, слитно, двузначный год
    assert parse_period('(01.01.23-31122023)') == ('2023-01-01T00:00:00', '2023-12-31T23:59:59')
    assert parse_period('(15.03.23.)') == ('2023-03-15T00:00:00', None)
    # открытые края
    assert parse_period('(15/03/2023-)') == ('2023-03-15T00:00:00', None)
    assert parse_period('(-15/03/2023)') == (None, '2023-03-15T23:59:59')
    assert parse_period('(15/03/2023-х)') == ('2023-03-15T00:00:00', None)
    # без скобок
    assert parse_period('01/01/2023-31/12/2023') == ('2023-01-01T00:00:00', '2023-12-31T23:59:59')
    assert parse_period('15/03/2023-') == ('2023-03-15T00:00:00', None)
    assert parse_period('-15/03/2023') == (None, '2023-03-15T23:59:59')
    assert parse_period('х-05/06/2024') == (None, '2024-06-05T23:59:59')
    assert parse_period('15.03.23') == ('2023-03-15T00:00:00', None)
    assert parse_period('01.01.23 - 31.12.23') == ('2023-01-01T00:00:00', '2023-12-31T23:59:59')
    for bad in ('(х)', '(2023)', '(32/01/2023)', 'вчера', '(01/01/2023', '(-)', '(х-х)',
                '2023', '-', 'х-х', '01/01/2023)'):
        try:
            parse_period(bad)
            assert False, bad
        except ValueError:
            pass
    print('test_parse_period ok')


def test_extract_rank_range():
    from bot.main import extract_rank_range

    assert extract_rank_range('') == ('', None)
    assert extract_rank_range('2-15') == ('', (2, 15))
    assert extract_rank_range('(2-15)') == ('', (2, 15))
    assert extract_rank_range('2 - 15') == ('', (2, 15))
    assert extract_rank_range('15-2') == ('', (2, 15))  # перепутанные края — меняем местами
    assert extract_rank_range('5') == ('', (1, 5))  # одно число = топ-N
    # диапазон + период в любом порядке; период уходит в остаток нетронутым
    assert extract_rank_range('2-15 (01.01.23-31.12.23)') == ('(01.01.23-31.12.23)', (2, 15))
    assert extract_rank_range('01.01.23-31.12.23 2-15') == ('01.01.23-31.12.23', (2, 15))
    assert extract_rank_range('(01.01.23 - 31.12.23) 3') == ('(01.01.23-31.12.23)', (1, 3))
    # даты (6+ цифр) диапазоном не считаются
    assert extract_rank_range('15.03.23') == ('15.03.23', None)
    assert extract_rank_range('150323') == ('150323', None)
    for bad in ('0-5', '0'):
        try:
            extract_rank_range(bad)
            assert False, bad
        except ValueError:
            pass
    print('test_extract_rank_range ok')


async def _test_should_log(tmp: str):
    from types import SimpleNamespace as NS
    from bot import main as botmain

    conn = await db.connect(str(Path(tmp) / 'log.db'))
    try:
        botmain._main_peer = None
        dm = NS(peer_id=489228536)
        chat = NS(peer_id=2_000_000_004)
        other = NS(peer_id=2_000_000_099)
        assert await botmain.should_log(conn, dm) is False  # ЛС не логируем
        assert await botmain.should_log(conn, chat) is True  # первая беседа - основная
        assert await db.get_setting(conn, 'peer_id') == '2000000004'
        assert await botmain.should_log(conn, other) is False  # чужая беседа
        # после «перезапуска» peer_id читается из settings, а не захватывается заново
        botmain._main_peer = None
        assert await botmain.should_log(conn, other) is False
        assert await botmain.should_log(conn, chat) is True
    finally:
        botmain._main_peer = None
        await conn.close()
    print('test_should_log ok')


def test_extract_mentions():
    from bot.main import extract_mentions

    assert extract_mentions('') == ('', [])
    assert extract_mentions('[id123|Вася]') == ('', [123])
    assert extract_mentions('[id123|@vasya] [id456|Петя]') == ('', [123, 456])
    assert extract_mentions('[club190546023|Iris]') == ('', [-190546023])
    # период остаётся в остатке для parse_period
    assert extract_mentions('[id123|Вася] 01.01.23-31.12.23') == ('01.01.23-31.12.23', [123])
    assert extract_mentions('01.01.23- [id1|A] [id2|B]') == ('01.01.23-', [1, 2])
    print('test_extract_mentions ok')


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
    test_ranking()
    test_parse_period()
    test_extract_rank_range()
    test_extract_mentions()
    test_build_msg_row()
    with tempfile.TemporaryDirectory() as tmp:
        asyncio.run(_test_import_and_queries(tmp))
        asyncio.run(_test_should_log(tmp))
    print('все проверки прошли')
