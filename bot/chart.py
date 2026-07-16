"""Карточка активности: 4 панели (сообщения в неделю, накопительный итог,
по часам, по дням недели). Одна серия — личная карточка или весь чат,
две серии — сравнение (/мы). Данные приходят списками datetime из БД.
"""
import matplotlib

matplotlib.use('Agg')
import matplotlib.dates as mdates  # noqa: E402
import matplotlib.pyplot as plt  # noqa: E402
import pandas as pd  # noqa: E402
from matplotlib import font_manager  # noqa: E402

# DejaVu без CJK-глифов — японские/китайские ники превращаются в квадраты.
# Подхватываем первый доступный CJK-шрифт как fallback (на VPS: apt install fonts-noto-cjk).
_CJK_CANDIDATES = ['Noto Sans CJK JP', 'Noto Sans CJK SC', 'Yu Gothic', 'Meiryo',
                   'MS Gothic', 'Malgun Gothic', 'Microsoft YaHei']
_available = {f.name for f in font_manager.fontManager.ttflist}
_cjk = [n for n in _CJK_CANDIDATES if n in _available]
plt.rcParams['font.family'] = ['DejaVu Sans'] + _cjk[:1]

SERIES_COLORS = ['#2a78d6', '#eda100']  # цвета серий в режиме сравнения

LABELS = {
    'ru': {
        'weekly': 'Сообщений в неделю (пик: {peak}, {date})',
        'weekly_plain': 'Сообщений в неделю',
        'per_week': 'Сообщений/неделю',
        'cumulative': 'Накопительный итог сообщений',
        'total': 'Всего сообщений',
        'by_hour': 'По часам суток',
        'hour': 'Час',
        'messages': 'Сообщений',
        'by_weekday': 'По дням недели',
        'weekdays': ['Пн', 'Вт', 'Ср', 'Чт', 'Пт', 'Сб', 'Вс'],
        'population': 'Население беседы',
        'population_writers': 'Население беседы (писали хоть раз)',
        'people': 'Человек',
    },
    'en': {
        'weekly': 'Messages per week (peak: {peak}, {date})',
        'weekly_plain': 'Messages per week',
        'per_week': 'Messages/week',
        'cumulative': 'Cumulative messages',
        'total': 'Total messages',
        'by_hour': 'By hour of day',
        'hour': 'Hour',
        'messages': 'Messages',
        'by_weekday': 'By day of week',
        'weekdays': ['Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat', 'Sun'],
        'population': 'Chat population',
        'population_writers': 'Chat population (posted at least once)',
        'people': 'People',
    },
}


def build_chart(dates, output_path: str, title: str | None = None,
                subtitle: str | None = None, lang: str = 'ru'):
    """Одна серия: dates — непустой список datetime; пишет PNG в output_path."""
    return _render([(title, dates)], output_path, subtitle, lang)


def build_compare_chart(series_a, series_b, output_path: str,
                        subtitle: str | None = None, lang: str = 'ru'):
    """Сравнение двух серий; series_* = (имя, список datetime)."""
    return _render([series_a, series_b], output_path, subtitle, lang)


def build_race_chart(series, output_path: str, lang: str = 'ru'):
    """Один большой график: накопительный итог каждого участника (/все).
    series — [(имя, [datetime, ...])], уже отсортированы по убыванию итога."""
    L = LABELS.get(lang, LABELS['ru'])
    fig, ax = plt.subplots(figsize=(13, 8))
    cmap = plt.get_cmap('tab10' if len(series) <= 10 else 'tab20')
    end = max(max(dates) for _, dates in series)  # общий правый край
    for i, (name, dates) in enumerate(series):
        s = pd.Series(1, index=pd.DatetimeIndex(dates))
        cumulative = s.resample('D').sum().cumsum()
        # ушедшие из чата: линия продолжается плоско до конца, а не обрывается
        full = pd.date_range(cumulative.index.min(), end, freq='D')
        cumulative = cumulative.reindex(full).ffill()
        ax.plot(cumulative.index, cumulative.values, linewidth=1.6,
                color=cmap(i % cmap.N), label=name)
    ax.set_title(L['cumulative'], fontsize=14, loc='left')
    ax.set_ylabel(L['total'])
    _style_dates_axis(ax)
    _style_common(ax)
    ax.legend(frameon=False, fontsize=10)
    plt.savefig(output_path, dpi=150, bbox_inches='tight', facecolor='white')
    plt.close(fig)
    return output_path


def build_population_chart(points, output_path: str, subtitle: str | None = None,
                           lang: str = 'ru', writers_curve: bool = False):
    """График населения (/население): points — [(datetime, участников)],
    ступенчатая линия, значения абсолютные (кроп периода делает вызывающий).
    writers_curve — фолбэк «писали хоть раз», когда событий входа/выхода нет."""
    L = LABELS.get(lang, LABELS['ru'])
    fig, ax = plt.subplots(figsize=(13, 6))
    xs, ys = zip(*points)
    ax.step(xs, ys, where='post', color='#2a78d6', linewidth=1.8)
    ax.fill_between(xs, ys, step='post', color='#2a78d6', alpha=0.15)
    title = L['population_writers' if writers_curve else 'population']
    title += f'   ·   {subtitle}' if subtitle else ''
    ax.set_title(title, fontsize=14, loc='left')
    ax.set_ylabel(L['people'])
    _style_dates_axis(ax)
    _style_common(ax)
    plt.savefig(output_path, dpi=150, bbox_inches='tight', facecolor='white')
    plt.close(fig)
    return output_path


def _style_dates_axis(ax):
    ax.xaxis.set_major_locator(mdates.MonthLocator(interval=3))
    ax.xaxis.set_major_formatter(mdates.DateFormatter('%m.%Y'))
    plt.setp(ax.xaxis.get_majorticklabels(), rotation=45, ha='right')


def _style_common(ax):
    ax.grid(axis='y', color='#e1e0d9', linewidth=0.8)
    ax.spines[['top', 'right']].set_visible(False)


def _render(series, output_path, subtitle, lang):
    L = LABELS.get(lang, LABELS['ru'])
    multi = len(series) > 1
    for _, dates in series:
        if not dates:
            raise ValueError('Пустой список сообщений')

    fig = plt.figure(figsize=(13, 10))
    title = ' vs '.join(str(n) for n, _ in series) if multi else series[0][0]
    if title:
        fig.suptitle(title, fontsize=16, y=0.99)
    if subtitle:
        fig.text(0.5, 0.955, subtitle, ha='center', fontsize=11, color='#555555')
    gs = fig.add_gridspec(3, 2, height_ratios=[1.3, 1, 1], hspace=0.45, wspace=0.25)

    # Панель 1: сообщений в неделю
    ax1 = fig.add_subplot(gs[0, :])
    for i, (name, dates) in enumerate(series):
        s = pd.Series(1, index=pd.DatetimeIndex(dates))
        # label='left': неделя подписывается своим понедельником, иначе
        # resample('W') подписывает воскресеньем-концом — «пик в будущем»
        weekly = s.resample('W-MON', closed='left', label='left').sum()
        color = SERIES_COLORS[i] if multi else '#2a78d6'
        ax1.plot(weekly.index, weekly.values, color=color, linewidth=1.2, label=name)
        ax1.fill_between(weekly.index, weekly.values, color=color,
                         alpha=0.08 if multi else 0.15)
        if not multi:
            peak_idx = weekly.values.argmax()
            peak_date, peak_val = weekly.index[peak_idx], weekly.values[peak_idx]
            ax1.plot(peak_date, peak_val, 'o', color='#eda100', markersize=7, zorder=3)
            ax1.set_title(
                L['weekly'].format(peak=peak_val, date=peak_date.strftime('%d.%m.%Y')),
                fontsize=13, loc='left',
            )
    if multi:
        ax1.set_title(L['weekly_plain'], fontsize=13, loc='left')
        ax1.legend(frameon=False)
    ax1.set_ylabel(L['per_week'])
    _style_dates_axis(ax1)
    _style_common(ax1)

    # Панель 2: накопительный итог
    ax2 = fig.add_subplot(gs[1, :])
    for i, (name, dates) in enumerate(series):
        s = pd.Series(1, index=pd.DatetimeIndex(dates))
        cumulative = s.resample('D').sum().cumsum()
        color = SERIES_COLORS[i] if multi else '#1baf7a'
        ax2.plot(cumulative.index, cumulative.values, color=color, linewidth=1.5, label=name)
    ax2.set_title(L['cumulative'], fontsize=13, loc='left')
    ax2.set_ylabel(L['total'])
    _style_dates_axis(ax2)
    _style_common(ax2)

    # Панель 3: по часам суток
    ax3 = fig.add_subplot(gs[2, 0])
    bar_w, offsets = (0.38, (-0.2, 0.2)) if multi else (0.75, (0,))
    for i, (name, dates) in enumerate(series):
        hours = [0] * 24
        for dt in dates:
            hours[dt.hour] += 1
        color = SERIES_COLORS[i] if multi else '#4a3aa7'
        bars = ax3.bar([h + offsets[i] for h in range(24)], hours,
                       width=bar_w, color=color, label=name)
        if not multi:
            bars[hours.index(max(hours))].set_color('#26215c')
    ax3.set_title(L['by_hour'], fontsize=13, loc='left')
    ax3.set_xlabel(L['hour'])
    ax3.set_ylabel(L['messages'])
    ax3.set_xticks(range(0, 24, 3))
    _style_common(ax3)

    # Панель 4: по дням недели
    ax4 = fig.add_subplot(gs[2, 1])
    bar_w, offsets = (0.32, (-0.17, 0.17)) if multi else (0.6, (0,))
    for i, (name, dates) in enumerate(series):
        weekdays_counts = [0] * 7
        for dt in dates:
            weekdays_counts[dt.weekday()] += 1
        color = SERIES_COLORS[i] if multi else '#eda100'
        ax4.bar([d + offsets[i] for d in range(7)], weekdays_counts,
                width=bar_w, color=color, label=name)
    ax4.set_title(L['by_weekday'], fontsize=13, loc='left')
    ax4.set_ylabel(L['messages'])
    ax4.set_xticks(range(7), L['weekdays'])
    _style_common(ax4)

    plt.savefig(output_path, dpi=150, bbox_inches='tight', facecolor='white')
    plt.close(fig)
    return output_path
