"""VK-бот: логирует каждое сообщение беседы, отвечает на команды статистики.

Запуск из корня проекта: python -m bot.main
"""
import asyncio
import json
import os
import re
import tempfile
import time
from datetime import date, datetime, timezone

from dotenv import load_dotenv
from vkbottle import PhotoMessageUploader, VKAPIError
from vkbottle.bot import Bot, Message

from bot import chart, db, parsing, ranking

load_dotenv()
bot = Bot(token=os.environ['VK_TOKEN'])
uploader = PhotoMessageUploader(bot.api)

USER_MILESTONES = {1_000, 5_000, 10_000, 25_000, 50_000, 100_000, 200_000, 500_000, 1_000_000}

HELP_TEXT = (
    'Команды:\n'
    '/я, /me - карточка активности\n'
    '/ты, /you - карточка активности (ответом на сообщение другого человека)\n'
    '/мы, /we - сравнение автора и другого участника (ответом)\n'
    '/чат, /chat - карточка по всей беседе\n'
    '/все, /all - график-гонка + места; можно задать диапазон мест на графике: '
    '/все 2-15, одно число N - топ-N\n'
    '/ping - pong\n'
    '/help - справка\n'
    'После команд статистики можно указать период: дата-дата, дата- или дата - '
    'от даты до конца, -дата или Х-дата - от начала до даты. Скобки не обязательны. '
    'Дата: дд/мм/гггг, дд.мм.гг, ддммгггг и т.п.'
)

_db = None
_render_lock = asyncio.Lock()  # pyplot не потокобезопасен — рендерим по одному


async def get_db():
    global _db
    if _db is None:
        _db = await db.connect()
    return _db


async def resolve_name(vk_id: int) -> str:
    try:
        if vk_id < 0:
            resp = await bot.api.groups.get_by_id(group_id=str(-vk_id))
            group = resp.groups[0] if hasattr(resp, 'groups') else resp[0]
            return group.name
        users = await bot.api.users.get(user_ids=[vk_id])
        return f'{users[0].first_name} {users[0].last_name}'
    except Exception:
        return f'id{vk_id}'


def to_local_iso(d) -> str:
    """vkbottle отдаёт date как datetime (pydantic), старые версии — как unix int."""
    if not isinstance(d, datetime):
        d = datetime.fromtimestamp(d, tz=timezone.utc)
    elif d.tzinfo is None:
        d = d.replace(tzinfo=timezone.utc)
    return d.astimezone(db.TZ).replace(tzinfo=None).isoformat(timespec='seconds')


def build_msg_row(item) -> dict | None:
    """Строка messages из объекта сообщения VK (live-событие или messages.getHistory)."""
    if item.conversation_message_id is None or item.from_id is None:
        return None
    if getattr(item, 'action', None):  # сервисное событие (пригласил/вышел/закреп)
        return None
    text = item.text or ''
    types = {getattr(a.type, 'value', str(a.type)) for a in (item.attachments or [])}
    reply_to = item.reply_message
    return {
        'cmid': item.conversation_message_id,
        'vk_id': item.from_id, 'ts': to_local_iso(item.date), 'text': text,
        'word_count': parsing.count_words(text),
        'is_reply': 1 if reply_to else 0,
        'reply_to_cmid': reply_to.conversation_message_id if reply_to else None,
        'has_sticker': 1 if 'sticker' in types else 0,
        'has_photo': 1 if 'photo' in types else 0,
        'has_attachment': 1 if (types - {'sticker', 'photo'}) or getattr(item, 'fwd_messages', None) else 0,
    }


async def ensure_user(conn, vk_id: int, ts: str) -> str:
    name = await db.get_display_name(conn, vk_id)
    if name is None:
        name = await resolve_name(vk_id)
    await db.upsert_user(conn, vk_id, name, 1 if vk_id < 0 else 0, ts)
    return name


_main_peer: int | None = None  # peer_id основной беседы (кэш settings)


async def should_log(conn, message: Message) -> bool:
    """В статистику пишем только основную беседу. ЛС и посторонние чаты не логируем:
    их cmid — независимые последовательности, и глобальный UNIQUE(cmid) в messages
    молча выкидывает коллизии, а «пролезшие» строки засоряют статистику беседы.
    Основной становится первая беседа, из которой пришло сообщение."""
    global _main_peer
    if message.peer_id < 2_000_000_000:  # ЛС
        return False
    if _main_peer is None:
        saved = await db.get_setting(conn, 'peer_id')
        if saved is None:
            await db.set_setting(conn, 'peer_id', str(message.peer_id))
            saved = str(message.peer_id)
        _main_peer = int(saved)
    return message.peer_id == _main_peer


@bot.on.message()
async def on_message(message: Message):
    conn = await get_db()
    if await should_log(conn, message):
        row = build_msg_row(message)
        if row is not None:
            name = await ensure_user(conn, row['vk_id'], row['ts'])
            total_before = await db.get_total_count(conn)
            user_before = await db.get_user_count(conn, row['vk_id'])
            inserted = await db.insert_message(conn, row)
            await conn.commit()
            if inserted:
                await check_milestones(conn, message, total_before + 1, user_before + 1, name)
    # команды работают везде (в ЛС удобно тестировать), статистика — по основной беседе
    text = (message.text or '').strip()
    if text.startswith('/'):
        await handle_command(conn, message, text)


async def backfill_missed():
    """Догоняет сообщения, отправленные пока бот лежал: листает историю беседы
    назад до первого уже известного cmid. Вехи не триггерит (как и импорт)."""
    conn = await get_db()
    peer = await db.get_setting(conn, 'peer_id')
    if peer is None:
        return  # беседа станет известна с первого живого сообщения
    try:
        me = await bot.api.groups.get_by_id()
        groups = me.groups if hasattr(me, 'groups') else me
        own_id = -groups[0].id  # свои сообщения не логируем — их нет и в live-событиях
    except Exception:
        own_id = None
    added, offset = 0, 0
    try:
        for _ in range(50):  # ponytail: потолок 10 000 сообщений за одну догонку
            hist = await bot.api.messages.get_history(
                peer_id=int(peer), count=200, offset=offset)
            if not hist.items:
                break
            hit_known = False
            for item in hist.items:  # новые -> старые
                row = build_msg_row(item)
                if row is None or row['vk_id'] == own_id:
                    continue
                if await db.insert_message(conn, row):
                    await ensure_user(conn, row['vk_id'], row['ts'])
                    added += 1
                else:
                    hit_known = True
            await conn.commit()
            if hit_known or len(hist.items) < 200:
                break
            offset += len(hist.items)
        if added:
            print(f'backfill: догнал {added} пропущенных сообщений')
    except Exception as e:
        print(f'backfill не удался: {e!r}')


# --- вехи ---

def years_word(n: int) -> str:
    if n % 100 in (11, 12, 13, 14):
        return 'лет'
    if n % 10 == 1:
        return 'год'
    if n % 10 in (2, 3, 4):
        return 'года'
    return 'лет'


def fmt(n: int) -> str:
    return f'{n:,}'.replace(',', ' ')


async def check_milestones(conn, message: Message, total: int, user_total: int, name: str):
    """Сообщения вставляются по одному, поэтому «пересечение» вехи = точное попадание.
    Импортированная история вехи не триггерит: импорт идёт отдельным скриптом без бота."""
    if total in (10_000, 50_000) or (total >= 100_000 and total % 100_000 == 0):
        if not await db.milestone_was_sent(conn, 'chat_total', str(total)):
            await message.answer(f'В чате написано {fmt(total)} сообщений! 🎉')
            await db.mark_milestone_sent(conn, 'chat_total', str(total))

    if message.from_id > 0 and user_total in USER_MILESTONES:
        mtype = f'user_{message.from_id}'
        if not await db.milestone_was_sent(conn, mtype, str(user_total)):
            await message.answer(f'{name} отправил(а) своё {fmt(user_total)}-е сообщение в чате!')
            await db.mark_milestone_sent(conn, mtype, str(user_total))

    first_ts = await db.get_first_message_ts(conn)
    if first_ts:
        first = date.fromisoformat(first_ts[:10])
        today = db.now_local().date()
        years = today.year - first.year
        if years > 0 and (today.month, today.day) == (first.month, first.day):
            if not await db.milestone_was_sent(conn, 'anniversary', str(today.year)):
                await message.answer(f'Сегодня чату {years} {years_word(years)}! 🎂')
                await db.mark_milestone_sent(conn, 'anniversary', str(today.year))


# --- команды ---

async def resolve_target(message: Message) -> int:
    """Чью статистику показывать: автора reply-сообщения, если команда — ответ.

    Мобильные клиенты VK иногда кладут ответ в fwd_messages вместо reply_message,
    а Long Poll может вообще не приложить это поле — тогда дотягиваем сообщение по API."""
    if message.reply_message:
        return message.reply_message.from_id
    fwd = getattr(message, 'fwd_messages', None) or []
    if len(fwd) == 1 and fwd[0].from_id:
        return fwd[0].from_id
    try:
        resp = await bot.api.messages.get_by_conversation_message_id(
            peer_id=message.peer_id,
            conversation_message_ids=[message.conversation_message_id],
        )
        item = resp.items[0] if resp.items else None
        if item:
            if item.reply_message:
                return item.reply_message.from_id
            if item.fwd_messages and len(item.fwd_messages) == 1:
                return item.fwd_messages[0].from_id
    except Exception:
        pass
    return message.from_id


NEED_REPLY = {'ru': 'Ответь командой на сообщение человека.',
              'en': "Reply to someone's message with this command."}
PERIOD_HINT = {
    'ru': 'Формат периода: дата-дата, дата-, -дата, Х-дата или дата (скобки не обязательны). '
          'Дата: дд/мм/гггг, дд.мм.гг, ддммгггг и т.п.',
    'en': 'Period format: date-date, date-, -date, X-date or date (parentheses optional). '
          'Date: dd/mm/yyyy, dd.mm.yy, ddmmyyyy etc.',
}
RANGE_HINT = {
    'ru': 'Диапазон мест: N-M или (N-M), одно число N - топ-N.',
    'en': 'Place range: N-M or (N-M), a single number N - top N.',
}

_DATE_RE = re.compile(r'^(\d{2})[./]?(\d{2})[./]?(\d{4}|\d{2})$')


def _parse_date(s: str, end: bool = False) -> str:
    m = _DATE_RE.fullmatch(s.rstrip('.'))
    if not m:
        raise ValueError(s)
    dd, mm, yy = m.groups()
    year = int(yy) + 2000 if len(yy) == 2 else int(yy)
    d = date(year, int(mm), int(dd))  # ValueError на несуществующей дате
    return d.isoformat() + ('T23:59:59' if end else 'T00:00:00')


def parse_period(arg: str) -> tuple[str | None, str | None]:
    """a-b -> (a, b); a-, a -> (a, None); -b, Х-b -> (None, b). Скобки опциональны.
    Открытый/Х-край = None (начало статистики / последнее сообщение).
    ValueError — кривой формат или несуществующая дата."""
    if not arg:
        return None, None
    s = arg.strip()
    if s.startswith('(') and s.endswith(')'):
        s = s[1:-1]
    elif '(' in s or ')' in s:  # непарная скобка
        raise ValueError(arg)
    inner = s.strip()

    def is_open(tok):
        return tok == '' or tok.lower() in ('х', 'x')

    if '-' in inner:
        left, right = (p.strip() for p in inner.split('-', 1))
        since = None if is_open(left) else _parse_date(left)
        until = None if is_open(right) else _parse_date(right, end=True)
        if since is None and until is None:
            raise ValueError(arg)
        return since, until
    if is_open(inner):
        raise ValueError(arg)
    return _parse_date(inner), None  # одиночная дата = старт, финиш открыт


_RANGE_RE = re.compile(r'(\d{1,3})(?:-(\d{1,3}))?')


def extract_rank_range(arg: str) -> tuple[str, tuple[int, int] | None]:
    """Вынимает из аргументов /все диапазон мест: 'N-M', '(N-M)' или 'N' (= топ-N).
    Возвращает (остаток для parse_period, (lo, hi) | None). Числа до трёх цифр,
    поэтому с датами (от шести цифр) не путается. ValueError — место 0."""
    arg = re.sub(r'\s*-\s*', '-', arg)  # '2 - 15' и '01.01.23 - 31.12.23' -> без пробелов
    rank_range, rest = None, []
    for tok in re.findall(r'\([^()]*\)|\S+', arg):
        inner = tok[1:-1].strip() if tok.startswith('(') and tok.endswith(')') else tok
        m = _RANGE_RE.fullmatch(inner)
        if m and rank_range is None:
            if m.group(2) is not None:
                lo, hi = int(m.group(1)), int(m.group(2))
                if lo > hi:
                    lo, hi = hi, lo
            else:
                lo, hi = 1, int(m.group(1))
            if lo < 1 or hi < 1:
                raise ValueError(tok)
            rank_range = (lo, hi)
        else:
            rest.append(tok)
    return ' '.join(rest), rank_range


def period_label(since: str | None, until: str | None, lang: str = 'ru') -> str:
    if not (since or until):
        return ''
    def f(iso):
        return date.fromisoformat(iso[:10]).strftime('%d.%m.%Y')
    a = f(since) if since else ('начало' if lang == 'ru' else 'start')
    b = f(until) if until else ('сейчас' if lang == 'ru' else 'now')
    return f'{a} – {b}'


async def handle_command(conn, message: Message, text: str):
    parts = text.split(maxsplit=1)
    cmd = parts[0].lower()
    arg = parts[1].strip() if len(parts) > 1 else ''
    if cmd in ('/help', '/помощь'):
        await message.answer(HELP_TEXT)
        return
    if cmd == '/ping':
        await message.answer('pong')
        return
    if cmd not in ('/я', '/me', '/ты', '/you', '/мы', '/we', '/чат', '/chat', '/все', '/all'):
        return
    lang = 'en' if cmd in ('/me', '/you', '/we', '/chat', '/all') else 'ru'
    rank_range = None
    try:
        if cmd in ('/все', '/all'):
            arg, rank_range = extract_rank_range(arg)
        since, until = parse_period(arg)
    except ValueError:
        hint = PERIOD_HINT[lang]
        if cmd in ('/все', '/all'):
            hint += '\n' + RANGE_HINT[lang]
        await message.answer(hint)
        return
    if cmd in ('/я', '/me'):
        await send_stats_card(conn, message, message.from_id, lang, since, until)
    elif cmd in ('/ты', '/you', '/мы', '/we'):
        target = await resolve_target(message)
        if target == message.from_id:
            await message.answer(NEED_REPLY[lang])
            return
        if cmd in ('/ты', '/you'):
            await send_stats_card(conn, message, target, lang, since, until)
        else:
            await send_compare_card(conn, message, message.from_id, target, lang, since, until)
    elif cmd in ('/чат', '/chat'):
        await send_stats_card(conn, message, None, lang, since, until)
    else:
        await send_ranking_card(conn, message, lang, since, until, rank_range)


async def render_and_send(message: Message, lang: str, label: str, render_fn,
                          text: str | None = None):
    """Рендерит PNG через render_fn(out_path), грузит в VK, шлёт в чат."""
    fd, out_path = tempfile.mkstemp(prefix='vkstats_', suffix='.png')
    os.close(fd)
    try:
        t0 = time.monotonic()
        async with _render_lock:
            await asyncio.to_thread(render_fn, out_path)
        t1 = time.monotonic()
        try:
            photo = await upload_photo(out_path)
        except (VKAPIError, json.JSONDecodeError):
            await message.answer('VK не принял картинку, попробуй ещё раз.' if lang == 'ru'
                                 else 'VK rejected the image, try again.')
            return
        t2 = time.monotonic()
        print(f'card [{label}] lang={lang}: chart {t1 - t0:.1f}s, upload {t2 - t1:.1f}s')
    finally:
        try:
            os.remove(out_path)
        except OSError:
            pass
    await message.answer(text, attachment=photo)


NO_MESSAGES = {'ru': 'Сообщений за период нет.', 'en': 'No messages in this period.'}


async def send_stats_card(conn, message: Message, vk_id: int | None, lang: str = 'ru',
                          since: str | None = None, until: str | None = None):
    """Карточка активности одним PNG. vk_id=None — весь чат."""
    stats = await db.get_activity_stats(conn, vk_id, since=since, until=until)
    if not stats:
        await message.answer(NO_MESSAGES[lang])
        return
    if vk_id is None:
        name = 'Весь чат' if lang == 'ru' else 'Whole chat'
    else:
        name = await db.get_display_name(conn, vk_id) or f'id{vk_id}'

    if lang == 'ru':
        subtitle = (f'Streak: {stats["streak"]} дн.   ·   '
                    f'Слов на сообщение: {stats["avg_words"]:.1f}')
    else:
        subtitle = (f'Streak: {stats["streak"]} days   ·   '
                    f'Words per message: {stats["avg_words"]:.1f}')
    period = period_label(since, until, lang)
    if period:
        subtitle += f'   ·   {period}'

    dates = [datetime.fromisoformat(t)
             for t in await db.get_timestamps(conn, vk_id, since, until)]
    await render_and_send(
        message, lang, name,
        lambda p: chart.build_chart(dates, p, name, subtitle, lang))


async def send_compare_card(conn, message: Message, me_id: int, target_id: int,
                            lang: str = 'ru', since: str | None = None,
                            until: str | None = None):
    """Сравнение двух участников: по две серии в каждой панели."""
    s_me = await db.get_activity_stats(conn, me_id, since=since, until=until)
    s_t = await db.get_activity_stats(conn, target_id, since=since, until=until)
    if not s_me or not s_t:
        await message.answer('У одного из вас нет сообщений за период.' if lang == 'ru'
                             else 'One of you has no messages in this period.')
        return
    name_me = await db.get_display_name(conn, me_id) or f'id{me_id}'
    name_t = await db.get_display_name(conn, target_id) or f'id{target_id}'

    if lang == 'ru':
        subtitle = (f'{name_me}: streak {s_me["streak"]} дн., {s_me["avg_words"]:.1f} слов/сообщ.   ·   '
                    f'{name_t}: streak {s_t["streak"]} дн., {s_t["avg_words"]:.1f} слов/сообщ.')
    else:
        subtitle = (f'{name_me}: streak {s_me["streak"]} days, {s_me["avg_words"]:.1f} words/msg   ·   '
                    f'{name_t}: streak {s_t["streak"]} days, {s_t["avg_words"]:.1f} words/msg')
    period = period_label(since, until, lang)
    if period:
        subtitle += f'   ·   {period}'

    dates_me = [datetime.fromisoformat(t)
                for t in await db.get_timestamps(conn, me_id, since, until)]
    dates_t = [datetime.fromisoformat(t)
               for t in await db.get_timestamps(conn, target_id, since, until)]
    await render_and_send(
        message, lang, f'{name_me} vs {name_t}',
        lambda p: chart.build_compare_chart((name_me, dates_me), (name_t, dates_t), p, subtitle, lang))


RACE_TOP = 10  # линий на графике-гонке; больше — каша
MIN_RANK_MESSAGES = 500  # порог попадания в рейтинг /все


async def send_ranking_card(conn, message: Message, lang: str = 'ru',
                            since: str | None = None, until: str | None = None,
                            rank_range: tuple[int, int] | None = None):
    """/все: график-гонка + текстовый рейтинг с историей смен мест.
    rank_range — какие места рисовать на графике (по умолчанию топ-RACE_TOP)."""
    rows = await db.get_human_messages(conn, since, until)
    if not rows:
        await message.answer(NO_MESSAGES[lang])
        return
    places = ranking.compute_ranking(rows)
    # порог отсекает хвост, ранги остаются сплошными 1..N;
    # в маленьком/молодом чате порог не применяем, чтобы список не опустел
    filtered = [p for p in places if p['count'] >= MIN_RANK_MESSAGES]
    if filtered:
        places = filtered
    names = await db.get_all_names(conn)

    def name_of(vk_id):
        return names.get(vk_id) or f'id{vk_id}'

    header = '🏆 Рейтинг чата' if lang == 'ru' else '🏆 Chat ranking'
    period = period_label(since, until, lang)
    lines = [f'{header} ({period}):' if period else f'{header}:']
    for p in places:
        ev = p['event']
        if ev is None:
            suffix = '(💎 с самого начала)' if lang == 'ru' else '(💎 from the start)'
        else:
            d = date.fromisoformat(ev['date'][:10]).strftime('%d.%m.%Y')
            if ev['kind'] == 'up':
                word = 'обошёл' if lang == 'ru' else 'overtook'
            else:
                word = 'уступил' if lang == 'ru' else 'lost to'
            suffix = f'({word}: {name_of(ev["other"])}, {d})'
        lines.append(f'{ranking.place_emoji(p["rank"])} {p["rank"]}. '
                     f'{name_of(p["vk_id"])} — {fmt(p["count"])} {suffix}')
    text = '\n'.join(lines)

    # график: выбранные места (по умолчанию топ-N), накопительные линии
    lo, hi = rank_range if rank_range else (1, RACE_TOP)
    if lo > len(places):
        await message.answer(f'В рейтинге всего {len(places)} мест.' if lang == 'ru'
                             else f'The ranking only has {len(places)} places.')
        return
    by_user: dict[int, list] = {}
    for vk_id, ts in rows:
        by_user.setdefault(vk_id, []).append(datetime.fromisoformat(ts))
    series = [(name_of(p['vk_id']), by_user[p['vk_id']]) for p in places[lo - 1:hi]]
    await render_and_send(
        message, lang, 'ranking',
        lambda path: chart.build_race_chart(series, path, lang),
        text=text)


async def upload_photo(path: str) -> str:
    """Аплоад-сервер VK флейкует: то пустой photo (ошибка 100), то пустой/не-JSON
    ответ (JSONDecodeError внутри vkbottle) — ретраим и то и другое.
    peer_id=0: аплоад с peer_id беседы от имени группы даёт «photo is undefined» всегда."""
    last_err = None
    for attempt in range(3):
        try:
            return await uploader.upload(path, peer_id=0)
        except (VKAPIError[100], json.JSONDecodeError) as e:
            last_err = e
            await asyncio.sleep(1 + attempt)
    raise last_err


if __name__ == '__main__':
    bot.on_startup.append(backfill_missed())
    try:
        bot.run()
    except Exception:
        import traceback

        from bot.notify import notify_crash
        notify_crash(traceback.format_exc())
        raise
