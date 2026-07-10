"""Рейтинг участников с историей смен мест (/все).

Симуляция по хронологии: на каждом сообщении счётчик автора растёт, и он
всплывает в рейтинге, пока его счёт строго больше соседа сверху (при равенстве
выше тот, кто достиг счёта раньше). Запоминается последняя смена места каждого:
'up' — обошёл кого-то, 'down' — его обошли. Нет событий — место с самого начала.
"""


def compute_ranking(rows) -> list[dict]:
    """rows — [(vk_id, ts)] по возрастанию ts (только люди).

    Возвращает список по местам: [{'vk_id', 'count', 'rank', 'event'}],
    event = None | {'date': ts, 'other': vk_id, 'kind': 'up'|'down'}.
    """
    counts: dict[int, int] = {}
    order: list[int] = []      # порядок мест: order[0] — 1-е место
    pos: dict[int, int] = {}   # vk_id -> индекс в order
    event: dict[int, dict] = {}

    for vk_id, ts in rows:
        if vk_id not in counts:
            counts[vk_id] = 0
            pos[vk_id] = len(order)
            order.append(vk_id)  # новичок встаёт в конец, это не «обгон»
        counts[vk_id] += 1
        i = pos[vk_id]
        last_passed = None
        while i > 0 and counts[order[i - 1]] < counts[vk_id]:
            passed = order[i - 1]
            order[i - 1], order[i] = order[i], order[i - 1]
            pos[passed], pos[vk_id] = i, i - 1
            event[passed] = {'date': ts, 'other': vk_id, 'kind': 'down'}
            last_passed = passed
            i -= 1
        if last_passed is not None:
            event[vk_id] = {'date': ts, 'other': last_passed, 'kind': 'up'}

    return [{'vk_id': v, 'count': counts[v], 'rank': i + 1, 'event': event.get(v)}
            for i, v in enumerate(order)]


def place_emoji(rank: int) -> str:
    return {1: '🥇', 2: '🥈', 3: '🥉', 4: '⚡', 5: '⚡'}.get(rank, '🔸')
