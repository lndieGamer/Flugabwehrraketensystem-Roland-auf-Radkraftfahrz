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

FIXTURE = """03.08.2022, 17:22:20 | Е. Океанов (vk.ru/id45749440) создал чат "Тестовая беседа" | cmid: 1

03.08.2022, 17:22:53 | Е. Океанов (vk.ru/id45749440) | cmid: 5
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

05.08.2022, 10:00:00 | Е. Океанов (vk.ru/id45749440) пригласил(а) Д. Мещерякова (vk.ru/id999) | cmid: 15

05.08.2022, 11:00:00 | В. Ссылочник (vk.ru/id777) вступил в чат по ссылке | cmid: 16

05.08.2022, 12:00:00 | Е. Океанов (vk.ru/id45749440) исключил(а) Iris (vk.ru/club190546023) из чата | cmid: 17

05.08.2022, 13:00:00 | Д. Мещеряков (vk.ru/id999) вышел из чата | cmid: 18
"""


def test_parser():
    blocks = list(parsing.parse_export(FIXTURE.splitlines()))
    assert len(blocks) == 12, len(blocks)

    by_cmid = {b['cmid']: b for b in blocks}
    assert by_cmid[10]['service'] is True
    assert by_cmid[15]['service'] is True  # «X пригласил Y» — событие, не сообщение Y
    assert sum(b['service'] for b in blocks) == 6

    # события состава беседы: (vk_id, ±1); закреп/фото — None
    assert by_cmid[1]['event'] == (45749440, 1)  # создал чат
    assert by_cmid[10]['event'] is None
    assert by_cmid[15]['event'] == (999, 1)
    assert by_cmid[16]['event'] == (777, 1)
    assert by_cmid[17]['event'] == (-190546023, -1)  # исключили сообщество
    assert by_cmid[18]['event'] == (999, -1)
    # имя с | внутри: цель — последний vk.ru-адрес
    assert parsing.member_event(
        'Е. Океанов (vk.ru/id45749440) пригласил(а) Iris | Чат-менеджер (vk.ru/club174105461)'
    ) == (-174105461, 1)
    assert parsing.member_event(
        'Действие "chat_pin_message, инициатор: Е. Океанов (vk.ru/id45749440), '
        'цель: Е. Океанов (vk.ru/id45749440)"'
    ) is None

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
    assert stats == {'headers': 12, 'service_skipped': 1, 'events': 5,
                     'inserted': 6, 'duplicates': 0}, stats

    stats2 = await import_history.import_file(str(export), db_path)
    assert stats2['inserted'] == 0 and stats2['duplicates'] == 6, stats2  # идемпотентность
    assert stats2['events'] == 0, stats2  # события тоже не дублируются

    conn = await db.connect(db_path)
    try:
        assert await db.get_total_count(conn) == 6

        s = await db.get_activity_stats(conn, 45749440, today=date(2022, 8, 4))
        assert s['total'] == 3
        assert s['streak'] == 2, s['streak']

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
        before = png.stat().st_size
        kb = chart.shrink_png(str(png))
        assert 0 < png.stat().st_size < before and kb == png.stat().st_size // 1024
        from PIL import Image
        assert Image.open(png).mode == 'P'  # палитровый PNG

        assert await db.count_writers(conn) == 2  # club исключён

        events = await db.get_member_events(conn)
        assert events == [('2022-08-03T17:22:20', 1), ('2022-08-05T10:00:00', 1),
                          ('2022-08-05T11:00:00', 1), ('2022-08-05T12:00:00', -1),
                          ('2022-08-05T13:00:00', -1)], events

        from bot.main import population_points
        png3 = Path(tmp) / 'population.png'
        chart.build_population_chart(
            population_points(events, 0, None, None, now=datetime(2022, 8, 6)), str(png3))
        assert png3.stat().st_size > 10_000, png3.stat().st_size

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


async def _test_msg_count(tmp: str):
    from bot import main as botmain

    conn = await db.connect(str(Path(tmp) / 'cnt.db'))
    try:
        botmain._counts.clear()
        row = {'cmid': 1, 'vk_id': 7, 'ts': '2022-08-03T10:00:00', 'text': 'a',
               'word_count': 1, 'is_reply': 0, 'reply_to_cmid': None,
               'has_sticker': 0, 'has_photo': 0, 'has_attachment': 0}
        await db.insert_message(conn, row)
        # первый вызов греет из БД, дальше — из кэша без COUNT
        assert await botmain.msg_count(conn, None) == 1
        assert await botmain.msg_count(conn, 7) == 1
        assert await botmain.msg_count(conn, 8) == 0
        await db.insert_message(conn, dict(row, cmid=2))
        assert await botmain.msg_count(conn, None) == 1  # кэш не видит вставку мимо него
        botmain._counts[None] += 1
        assert await botmain.msg_count(conn, None) == 2
    finally:
        botmain._counts.clear()
        await conn.close()
    print('test_msg_count ok')


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


def test_population_points():
    from datetime import datetime
    from bot.main import population_points

    events = [('2022-08-03T17:22:20', 1), ('2022-08-04T01:30:00', 1),
              ('2022-08-05T12:00:00', -1)]
    now = datetime(2022, 8, 6)
    # без периода: от первого события до «сейчас», выход опускает линию
    assert population_points(events, 0, None, None, now=now) == [
        (datetime(2022, 8, 3, 17, 22, 20), 1),
        (datetime(2022, 8, 4, 1, 30), 2),
        (datetime(2022, 8, 5, 12, 0), 1),
        (now, 1),
    ]
    # base — якорь (текущее население минус сумма дельт): сдвигает всю кривую
    assert population_points(events, 10, None, None, now=now)[0][1] == 11
    # период кропает окно, значения абсолютные: к началу окна уже 1 участник
    assert population_points(events, 0, '2022-08-04T00:00:00', '2022-08-04T23:59:59') == [
        (datetime(2022, 8, 4), 1),
        (datetime(2022, 8, 4, 1, 30), 2),
        (datetime(2022, 8, 4, 23, 59, 59), 2),
    ]
    # период после всех событий: плоская линия на итоговом населении
    assert population_points(events, 0, '2022-09-01T00:00:00', '2022-09-02T23:59:59') == [
        (datetime(2022, 9, 1), 1),
        (datetime(2022, 9, 2, 23, 59, 59), 1),
    ]
    print('test_population_points ok')


def test_action_member_event():
    from types import SimpleNamespace as NS
    from bot.main import action_member_event

    assert action_member_event(
        NS(action=NS(type=NS(value='chat_invite_user'), member_id=42), from_id=1)) == (42, 1)
    # выход = kick самого себя; member_id может отсутствовать — берём from_id
    assert action_member_event(
        NS(action=NS(type=NS(value='chat_kick_user'), member_id=7), from_id=7)) == (7, -1)
    assert action_member_event(
        NS(action=NS(type=NS(value='chat_invite_user_by_link'), member_id=None),
           from_id=5)) == (5, 1)
    assert action_member_event(
        NS(action=NS(type=NS(value='chat_pin_message'), member_id=None), from_id=1)) is None
    assert action_member_event(NS(action=None, from_id=1)) is None
    print('test_action_member_event ok')


def test_plural_ru():
    from bot.main import plural_ru, years_word

    forms = ('участник', 'участника', 'участников')
    assert plural_ru(1, *forms) == 'участник'
    assert plural_ru(2, *forms) == 'участника'
    assert plural_ru(5, *forms) == 'участников'
    assert plural_ru(11, *forms) == 'участников'
    assert plural_ru(21, *forms) == 'участник'
    assert plural_ru(104, *forms) == 'участника'
    assert years_word(1) == 'год' and years_word(3) == 'года' and years_word(12) == 'лет'
    print('test_plural_ru ok')


def test_resolve_target_card():
    import asyncio
    from types import SimpleNamespace as NS
    from bot import main as botmain

    botmain._card_subject.clear()
    peer = 2_000_000_004
    # карточка бота (cmid 50) про пользователя 7; ответ на неё резолвится в 7, а не в бота
    botmain._card_subject[(peer, 50)] = 7
    bot_card = NS(from_id=-190546023, conversation_message_id=50)
    cmd = NS(peer_id=peer, from_id=9, reply_message=bot_card, fwd_messages=None)
    assert asyncio.run(botmain.resolve_target(cmd)) == 7
    # обычный ответ человеку — как раньше
    human = NS(from_id=8, conversation_message_id=51)
    assert asyncio.run(botmain.resolve_target(NS(peer_id=peer, from_id=9, reply_message=human,
                                                fwd_messages=None))) == 8
    # та же cmid в другой беседе — не карточка
    assert asyncio.run(botmain.resolve_target(NS(peer_id=peer + 1, from_id=9, reply_message=bot_card,
                                                fwd_messages=None))) == bot_card.from_id
    # мобильный клиент: ответ пришёл как fwd
    assert asyncio.run(botmain.resolve_target(NS(peer_id=peer, from_id=9, reply_message=None,
                                                fwd_messages=[bot_card]))) == 7
    botmain._card_subject.clear()
    print('test_resolve_target_card ok')


def test_timing():
    from bot import main as botmain

    botmain.mark('вне запроса')  # без contextvar — no-op, не падает
    marks = [('старт', 10.0), ('SQL', 10.25), ('рендер', 11.5)]
    out = botmain.format_timing('/я', 'id7', marks, lag=2)
    assert out.splitlines() == ['⏱ /я (id7): 1.50 s', '  доставка VK → бот: 2 s',
                                '  SQL: 250 ms', '  рендер: 1.25 s'], out
    print('test_timing ok')


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
    test_population_points()
    test_action_member_event()
    test_plural_ru()
    test_build_msg_row()
    test_resolve_target_card()
    test_timing()
    with tempfile.TemporaryDirectory() as tmp:
        asyncio.run(_test_import_and_queries(tmp))
        asyncio.run(_test_should_log(tmp))
        asyncio.run(_test_msg_count(tmp))
    print('все проверки прошли')
