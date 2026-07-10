"""VK-бот: логирует каждое сообщение беседы, отвечает на команды статистики.

Запуск из корня проекта: python -m bot.main
"""
import asyncio
import json
import os
import tempfile
import time
from datetime import date, datetime, timezone

from dotenv import load_dotenv
from vkbottle import PhotoMessageUploader, VKAPIError
from vkbottle.bot import Bot, Message

from bot import chart, db, parsing

load_dotenv()
bot = Bot(token=os.environ['VK_TOKEN'])
uploader = PhotoMessageUploader(bot.api)

USER_MILESTONES = {1_000, 5_000, 10_000, 25_000, 50_000, 100_000, 200_000, 500_000, 1_000_000}

HELP_TEXT = (
    'Команды:\n'
    '/я, /me - карточка активности\n'
    '/ты, /you - карточка активности (ответом на сообщение другого человека)\n'
    '/чат, /chat - карточка по всей беседе\n'
    '/ping - pong\n'
    '/help - справка'
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


_peer_saved = False


@bot.on.message()
async def on_message(message: Message):
    row = build_msg_row(message)
    if row is None:
        return
    conn = await get_db()

    global _peer_saved
    if not _peer_saved and message.peer_id > 2_000_000_000:  # только беседа, не ЛС
        await db.set_setting(conn, 'peer_id', str(message.peer_id))
        _peer_saved = True

    name = await ensure_user(conn, row['vk_id'], row['ts'])

    total_before = await db.get_total_count(conn)
    user_before = await db.get_user_count(conn, row['vk_id'])
    inserted = await db.insert_message(conn, row)
    await conn.commit()

    if inserted:
        await check_milestones(conn, message, total_before + 1, user_before + 1, name)
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


async def handle_command(conn, message: Message, text: str):
    cmd = text.split()[0].lower()
    if cmd in ('/help', '/помощь'):
        await message.answer(HELP_TEXT)
    elif cmd == '/ping':
        await message.answer('pong')
    elif cmd in ('/я', '/me'):
        await send_stats_card(conn, message, message.from_id, 'en' if cmd == '/me' else 'ru')
    elif cmd in ('/ты', '/you'):
        lang = 'en' if cmd == '/you' else 'ru'
        target = await resolve_target(message)
        if target == message.from_id:
            await message.answer('Ответь командой на сообщение человека.' if lang == 'ru'
                                 else "Reply to someone's message with this command.")
            return
        await send_stats_card(conn, message, target, lang)
    elif cmd in ('/чат', '/chat'):
        await send_stats_card(conn, message, None, 'en' if cmd == '/chat' else 'ru')


async def send_stats_card(conn, message: Message, vk_id: int | None, lang: str = 'ru'):
    """Карточка активности одним PNG. vk_id=None — весь чат."""
    stats = await db.get_activity_stats(conn, vk_id)
    if not stats:
        await message.answer('Сообщений ещё нет.' if lang == 'ru' else 'No messages yet.')
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

    ts_list = await db.get_timestamps(conn, vk_id)
    dates = [datetime.fromisoformat(t) for t in ts_list]
    fd, out_path = tempfile.mkstemp(prefix='vkstats_', suffix='.png')
    os.close(fd)
    try:
        t0 = time.monotonic()
        async with _render_lock:
            await asyncio.to_thread(chart.build_chart, dates, out_path, name, subtitle, lang)
        t1 = time.monotonic()
        try:
            photo = await upload_photo(out_path)
        except (VKAPIError, json.JSONDecodeError):
            await message.answer('VK не принял картинку, попробуй ещё раз.' if lang == 'ru'
                                 else 'VK rejected the image, try again.')
            return
        t2 = time.monotonic()
        print(f'card [{name}] lang={lang}: chart {t1 - t0:.1f}s, upload {t2 - t1:.1f}s')
    finally:
        try:
            os.remove(out_path)
        except OSError:
            pass
    await message.answer(attachment=photo)


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
