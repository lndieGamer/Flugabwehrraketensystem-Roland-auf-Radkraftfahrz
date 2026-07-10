"""Карточка активности: 4 панели (сообщения в неделю, накопительный итог,
по часам, по дням недели). Адаптация скрипта plot_activity.py: данные приходят
списком datetime из БД, а не из текстовой выгрузки.
"""
import matplotlib

matplotlib.use('Agg')
import matplotlib.dates as mdates  # noqa: E402
import matplotlib.pyplot as plt  # noqa: E402
import pandas as pd  # noqa: E402

plt.rcParams['font.family'] = 'DejaVu Sans'

LABELS = {
    'ru': {
        'weekly': 'Сообщений в неделю (пик: {peak}, {date})',
        'per_week': 'Сообщений/неделю',
        'cumulative': 'Накопительный итог сообщений',
        'total': 'Всего сообщений',
        'by_hour': 'По часам суток',
        'hour': 'Час',
        'messages': 'Сообщений',
        'by_weekday': 'По дням недели',
        'weekdays': ['Пн', 'Вт', 'Ср', 'Чт', 'Пт', 'Сб', 'Вс'],
    },
    'en': {
        'weekly': 'Messages per week (peak: {peak}, {date})',
        'per_week': 'Messages/week',
        'cumulative': 'Cumulative messages',
        'total': 'Total messages',
        'by_hour': 'By hour of day',
        'hour': 'Hour',
        'messages': 'Messages',
        'by_weekday': 'By day of week',
        'weekdays': ['Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat', 'Sun'],
    },
}


def build_chart(dates, output_path: str, title: str | None = None,
                subtitle: str | None = None, lang: str = 'ru'):
    """dates — непустой список datetime; пишет PNG в output_path.
    subtitle — строка сводки под заголовком; lang — язык подписей ('ru'/'en')."""
    if not dates:
        raise ValueError('Пустой список сообщений')
    L = LABELS.get(lang, LABELS['ru'])

    s = pd.Series(1, index=pd.DatetimeIndex(dates))
    weekly = s.resample('W').sum()
    cumulative = s.resample('D').sum().cumsum()

    hours = [0] * 24
    weekdays_counts = [0] * 7
    for dt in dates:
        hours[dt.hour] += 1
        weekdays_counts[dt.weekday()] += 1
    weekday_labels = L['weekdays']

    fig = plt.figure(figsize=(13, 10))
    if title:
        fig.suptitle(title, fontsize=16, y=0.99)
    if subtitle:
        fig.text(0.5, 0.955, subtitle, ha='center', fontsize=11, color='#555555')
    gs = fig.add_gridspec(3, 2, height_ratios=[1.3, 1, 1], hspace=0.45, wspace=0.25)

    # Панель 1: сообщений в неделю
    ax1 = fig.add_subplot(gs[0, :])
    ax1.plot(weekly.index, weekly.values, color='#2a78d6', linewidth=1.2)
    ax1.fill_between(weekly.index, weekly.values, color='#2a78d6', alpha=0.15)
    peak_idx = weekly.values.argmax()
    peak_date = weekly.index[peak_idx]
    peak_val = weekly.values[peak_idx]
    ax1.set_title(
        L['weekly'].format(peak=peak_val, date=peak_date.strftime('%d.%m.%Y')),
        fontsize=13, loc='left',
    )
    ax1.set_ylabel(L['per_week'])
    ax1.xaxis.set_major_locator(mdates.MonthLocator(interval=3))
    ax1.xaxis.set_major_formatter(mdates.DateFormatter('%m.%Y'))
    ax1.grid(axis='y', color='#e1e0d9', linewidth=0.8)
    ax1.spines[['top', 'right']].set_visible(False)
    plt.setp(ax1.xaxis.get_majorticklabels(), rotation=45, ha='right')

    ax1.plot(peak_date, peak_val, 'o', color='#eda100', markersize=7, zorder=3)

    # Панель 2: накопительный итог
    ax2 = fig.add_subplot(gs[1, :])
    ax2.plot(cumulative.index, cumulative.values, color='#1baf7a', linewidth=1.5)
    ax2.set_title(L['cumulative'], fontsize=13, loc='left')
    ax2.set_ylabel(L['total'])
    ax2.xaxis.set_major_locator(mdates.MonthLocator(interval=3))
    ax2.xaxis.set_major_formatter(mdates.DateFormatter('%m.%Y'))
    ax2.grid(axis='y', color='#e1e0d9', linewidth=0.8)
    ax2.spines[['top', 'right']].set_visible(False)
    plt.setp(ax2.xaxis.get_majorticklabels(), rotation=45, ha='right')

    # Панель 3: по часам суток
    ax3 = fig.add_subplot(gs[2, 0])
    bars = ax3.bar(range(24), hours, color='#4a3aa7', width=0.75)
    ax3.set_title(L['by_hour'], fontsize=13, loc='left')
    ax3.set_xlabel(L['hour'])
    ax3.set_ylabel(L['messages'])
    ax3.set_xticks(range(0, 24, 3))
    ax3.grid(axis='y', color='#e1e0d9', linewidth=0.8)
    ax3.spines[['top', 'right']].set_visible(False)
    bars[hours.index(max(hours))].set_color('#26215c')

    # Панель 4: по дням недели
    ax4 = fig.add_subplot(gs[2, 1])
    ax4.bar(weekday_labels, weekdays_counts, color='#eda100', width=0.6)
    ax4.set_title(L['by_weekday'], fontsize=13, loc='left')
    ax4.set_ylabel(L['messages'])
    ax4.grid(axis='y', color='#e1e0d9', linewidth=0.8)
    ax4.spines[['top', 'right']].set_visible(False)

    plt.savefig(output_path, dpi=150, bbox_inches='tight', facecolor='white')
    plt.close(fig)
    return output_path
