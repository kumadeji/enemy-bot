import discord
import os
import sys
import signal
import atexit
import gspread
import re
import json
import uuid
from datetime import datetime, timedelta
from collections import deque
import pytz
from apscheduler.schedulers.asyncio import AsyncIOScheduler
import asyncio

# ============== ЗАЩИТА ОТ ДВОЙНОГО ЗАПУСКА ==============

LOCK_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), '.bot.lock')


def _pid_is_alive(pid: int) -> bool:
    """Проверяет, жив ли процесс с данным PID (кроссплатформенно)."""
    try:
        import psutil
        return psutil.pid_exists(pid)
    except ImportError:
        pass

    if os.path.exists(f"/proc/{pid}"):
        return True

    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False


def acquire_single_instance_lock():
    if os.path.exists(LOCK_FILE):
        try:
            with open(LOCK_FILE, 'r') as f:
                old_pid_str = f.read().strip()
            old_pid = int(old_pid_str) if old_pid_str.isdigit() else None
        except Exception:
            old_pid = None

        if old_pid and _pid_is_alive(old_pid):
            print(f"❌ Бот уже запущен (PID {old_pid}). Останавливаю этот процесс, чтобы избежать дублирования.")
            sys.exit(1)
        else:
            print(f"⚠️ Найден устаревший lock-файл (PID {old_pid} не активен). Перезаписываю.")

    with open(LOCK_FILE, 'w') as f:
        f.write(str(os.getpid()))

    def _cleanup():
        try:
            if os.path.exists(LOCK_FILE):
                with open(LOCK_FILE, 'r') as f:
                    saved_pid = f.read().strip()
                if saved_pid == str(os.getpid()):
                    os.remove(LOCK_FILE)
        except Exception as e:
            print(f"Ошибка при удалении lock-файла: {e}")

    atexit.register(_cleanup)

    def _signal_handler(signum, frame):
        print(f"Получен сигнал {signum}, завершаю работу...")
        _cleanup()
        sys.exit(0)

    signal.signal(signal.SIGTERM, _signal_handler)
    signal.signal(signal.SIGINT, _signal_handler)


acquire_single_instance_lock()

# ============== НАСТРОЙКИ ==============

THREAD_ID = 1530860224724996237
PREFIX = '!s '

# Список администраторов (три ID)
ADMIN_USER_IDS = [
    316641571284058113,
    766919669838905364,
    115475534544109573
]

GOOGLE_CREDENTIALS_FILE = 'credentials.json'
SPREADSHEET_URL = 'https://docs.google.com/spreadsheets/d/1QGc-SRkWnFCaSx56_46UJPRK0XOe33KPou7yJznbQBM'

MSK = pytz.timezone('Europe/Moscow')

VACATION_EXCEPTIONS = {
    '[En-Y]Mr.GreyGoose': (datetime(2026, 8, 4), datetime(2026, 8, 10)),
    '[En-Y]Bercekle': (datetime(2026, 7, 26), datetime(2026, 8, 26)),
    '[En-Y]Slay': (datetime(2026, 7, 27), datetime(2026, 8, 27)),
    '[En-Y]Killa': (datetime(2026, 7, 23), datetime(2026, 8, 23)),
    '[En-Y]v1c': (datetime(2026, 7, 23), datetime(2026, 8, 23)),
    '[En-Y]Russo': (datetime(2026, 7, 30), datetime(2026, 8, 10)),
    '[En-Y]GDim': (datetime(2026, 8, 4), datetime(2026, 9, 4)),
}

NICKNAME_COLUMN = 'Discord клана (с клантегом)'
SHEET_NAME = 'Основная таблица'

COLUMNS_TO_CHECK = [
    'Discord клана (с клантегом)',
    'Discord ECHO (с клантегом)',
    'Discord AS VDV (с клантегом)',
    'Discord TT (с клантегом)',
    'Steam (с клантегом)',
    'Steam (в друзьях у BURBON?)',
    'Сайт клана (без клантега)',
    'Сайт ECHO (без клантега)',
    'Сайт AS VDV (без клантега)',
    'Сайт TT (без клантега - исправить только через администрацию)'
]

EXPECTED_INTRO_MAX_LEN = 700

# ============== НОВЫЕ НАСТРОЙКИ ДЛЯ МЕРОПРИЯТИЙ ==============

EVENTS_CHANNEL_ID = 1311705378140196926
VACATION_CHANNEL_ID = 1284905224099598407
ADMIN_CHANNEL_ID = 1470352063824265279

CLAN_MEMBERS = [
    "[En-Y]COKOJl", "[En-Y]Killa", "[En-Y]SIR", "[En-Y]Russo", "[En-Y]v1c",
    "[En-Y]Bercekle", "[En-Y]BURBON", "[En-Y]Sterben", "[En-Y]Bushwacker",
    "[En-Y]Prof. Kruglov", "[En-Y]Boba", "[En-Y]RainStars", "[En-Y]GDim",
    "[En-Y]Sliva88", "[En-Y]Mr.GreyGoose", "[En-Y]Libertad", "[En-Y]Komarant",
    "[En-Y]STARICHOK", "[En-Y]Sasce2044", "[En-Y]Metalist08", "[En-Y]Slay",
    "[En-Y]Vampire", "[En-Y]MAKER", "[En-Y]GhostCTPAX"
]

EVENTS_FILE = 'events_data.json'
VACATIONS_FILE = 'vacations.json'

# ============== ИНИЦИАЛИЗАЦИЯ ==============

intents = discord.Intents.default()
intents.message_content = True
intents.members = True

client = discord.Client(intents=intents)
scheduler = AsyncIOScheduler(timezone=MSK)

check_lock = asyncio.Lock()


class MessageDeduplicator:
    """Защита от повторной обработки одного и того же события on_message
    (например, при реконнекте gateway)."""

    def __init__(self, maxlen=500):
        self._order = deque(maxlen=maxlen)
        self._seen = set()

    def mark_processed(self, message_id: int) -> bool:
        """Возвращает True, если сообщение уже было обработано ранее."""
        if message_id in self._seen:
            return True
        if len(self._order) == self._order.maxlen:
            oldest = self._order[0]
            self._seen.discard(oldest)
        self._order.append(message_id)
        self._seen.add(message_id)
        return False


dedup = MessageDeduplicator(maxlen=500)

try:
    gc = gspread.service_account(filename=GOOGLE_CREDENTIALS_FILE)
except Exception as e:
    print(f"Ошибка при инициализации Google Sheets: {e}")
    gc = None

# ============== ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ ==============

def extract_nickname(raw_text):
    raw_text = raw_text.strip()
    if len(raw_text) <= 35:
        return raw_text

    match_paren = re.search(r'\(["\']?(.*?)["\']?\)', raw_text)
    if match_paren:
        extracted = match_paren.group(1).strip()
        if len(extracted) <= 35:
            return extracted

    match_bracket = re.search(r'(\[.*?\])', raw_text)
    if match_bracket:
        extracted = match_bracket.group(1).strip()
        if len(extracted) <= 35:
            return extracted

    return None


def get_color_category(bg):
    if not bg:
        return None

    r = bg.get('red', 0)
    g = bg.get('green', 0)
    b = bg.get('blue', 0)

    if 0.85 < r < 0.98 and 0.50 < g < 0.70 and 0.50 < b < 0.70:
        if r > g and r > b:
            return 'red'

    if 0.90 < r <= 1.0 and 0.80 < g < 0.95 and 0.50 < b < 0.70:
        if r > g and g > b:
            return 'yellow'

    return None


def get_sheet_data_with_colors(sheet, range_name):
    try:
        base_url = 'https://sheets.googleapis.com/v4/spreadsheets'
        full_url = f'{base_url}/{sheet.spreadsheet.id}'

        range_str = f"'{sheet.title}'!{range_name}"
        params = {
            'ranges': range_str,
            'includeGridData': 'true',
            'fields': 'sheets.data.rowData.values(effectiveFormat/backgroundColor,userEnteredValue)'
        }

        res = sheet.client.request('get', full_url, params=params)
        data = res.json()

        rows_data = data['sheets'][0]['data'][0].get('rowData', [])
        result = []
        for row in rows_data:
            row_result = []
            for cell in row.get('values', []):
                val_obj = cell.get('userEnteredValue', {})
                val = str(val_obj.get('stringValue', val_obj.get('numberValue', val_obj.get('boolValue', ''))))
                bg = cell.get('effectiveFormat', {}).get('backgroundColor', None)
                row_result.append({'value': val, 'bg': bg})
            result.append(row_result)
        return result
    except Exception as e:
        print(f"❌ Ошибка при получении данных с цветами из API: {e}")
        return []


def is_on_vacation(nickname: str, current_date: datetime) -> bool:
    clean_nickname = nickname.strip().lower()
    current_date_only = current_date.date()

    for vac_name, (start_date, end_date) in VACATION_EXCEPTIONS.items():
        if clean_nickname == vac_name.strip().lower():
            if start_date.date() <= current_date_only <= end_date.date():
                print(f"✅ {nickname} в отпуске (до {end_date.strftime('%d.%m.%Y')}), пропускаем.")
                return True
            else:
                return False
    return False


async def find_discord_user(nickname: str, thread):
    try:
        guild = thread.guild
        for member in guild.members:
            if member.display_name == nickname:
                return member
        for member in guild.members:
            if nickname.lower() in member.display_name.lower():
                return member
        return None
    except Exception as e:
        print(f"Ошибка при поиске пользователя {nickname}: {e}")
        return None


async def send_chunked(thread, text, user_name=""):
    """Надёжная разбивка длинных сообщений на части"""
    if not text:
        return

    chunk_size = 1800

    if len(text) <= chunk_size:
        print(f"📤 Отправка сообщения для {user_name}: {len(text)} символов")
        await thread.send(text)
        await asyncio.sleep(0.5)
        return

    chunks = []
    for i in range(0, len(text), chunk_size):
        chunk = text[i:i + chunk_size]
        chunks.append(chunk)

    print(f"📤 Отправка сообщения для {user_name}: {len(text)} символов → {len(chunks)} частей")

    for i, chunk in enumerate(chunks, 1):
        print(f"  → Часть {i}/{len(chunks)}: {len(chunk)} символов")
        try:
            await thread.send(chunk)
            await asyncio.sleep(0.5)
        except Exception as e:
            print(f"  ❌ Ошибка при отправке части {i}: {e}")
            raise


# ============== ПОСТРОЕНИЕ ТЕКСТОВ СООБЩЕНИЙ ==============

def build_intro_lines(current_time: datetime) -> list:
    """Возвращает список строк вводного сообщения."""
    return [
        "🔔 **Проверяющий бот клана** 🔔",
        "",
        "Это автоматическая проверка клана по таблице — всех, кто не в отпуске. "
        "**[Полная таблица](<https://enemygaming.netlify.app/temptable>)** — обновляется ежедневно.",
        "Если вы исправили какую-либо проблему, поставьте лайк как реакцию на это сообщение.",
        "🔴 **Красные** проблемы — критические, требуют немедленного исправления.",
        "🟡 **Желтые** проблемы — менее важные, но тоже требуют своевременного исправления.",
        f"📅 Проверка от {current_time.strftime('%d.%m.%Y %H:%M')} МСК",
        " ",
        "─" * 50,
    ]


def build_intro_message(current_time: datetime) -> str:
    lines = build_intro_lines(current_time)
    return "\n".join(lines)


def build_user_message(discord_user, issues: list) -> str:
    """Группирует проблемы пользователя по критичности и формирует текст."""
    red_issues = [i for i in issues if i['severity'] == 'red']
    yellow_issues = [i for i in issues if i['severity'] == 'yellow']

    parts = [f"👤 **{discord_user.mention}**", ""]

    def issue_line(issue):
        text = issue['text'].strip()
        if not text:
            text = f"({issue['column']})"
        return f"* {text}"

    if red_issues:
        parts.append("🔴 **Критические проблемы, требующие скорейшего исправления:**")
        for issue in red_issues:
            parts.append(issue_line(issue))
        parts.append("")

    if yellow_issues:
        parts.append(
            "🟡 **Важные, но менее критические проблемы, "
            "также требующие своевременного исправления:**"
        )
        for issue in yellow_issues:
            parts.append(issue_line(issue))
        parts.append("")

    parts.append("─" * 50)

    return "\n".join(parts).strip("\n")


async def check_spreadsheet():
    """Основная функция проверки таблицы"""

    if check_lock.locked():
        print("⚠️ Проверка уже выполняется, пропускаем.")
        return

    async with check_lock:
        print(f"🔍 Начинаем проверку таблицы в {datetime.now(MSK).strftime('%H:%M:%S')}")

        try:
            if not gc:
                print("Google Sheets не инициализирован")
                return

            spreadsheet = gc.open_by_url(SPREADSHEET_URL)
            sheet = spreadsheet.worksheet(SHEET_NAME)

            data_with_colors = get_sheet_data_with_colors(sheet, 'A1:J28')

            if not data_with_colors or len(data_with_colors) < 2:
                print("Таблица пуста")
                return

            headers = [cell['value'] for cell in data_with_colors[0]]
            rows = data_with_colors[1:]

            current_time = datetime.now(MSK)
            thread = await client.fetch_channel(THREAD_ID)

            user_issues = {}
            users_not_found = []

            for row in rows:
                raw_nickname = ''
                if NICKNAME_COLUMN in headers:
                    nick_idx = headers.index(NICKNAME_COLUMN)
                    if nick_idx < len(row):
                        raw_nickname = row[nick_idx]['value'].strip()

                nickname = extract_nickname(raw_nickname)

                if not nickname:
                    continue

                if is_on_vacation(nickname, current_time):
                    continue

                issues = []
                for col_name in COLUMNS_TO_CHECK:
                    if col_name in headers:
                        col_idx = headers.index(col_name)
                        if col_idx < len(row):
                            cell_data = row[col_idx]
                            color = get_color_category(cell_data['bg'])

                            if color in ['red', 'yellow']:
                                issues.append({
                                    'column': col_name,
                                    'text': cell_data['value'].strip(),
                                    'severity': color
                                })

                if issues:
                    discord_user = await find_discord_user(nickname, thread)
                    if discord_user:
                        user_issues[discord_user] = issues
                    else:
                        users_not_found.append(nickname)

            # ОТПРАВКА СООБЩЕНИЙ
            if user_issues or users_not_found:
                intro = build_intro_message(current_time)

                if len(intro) > EXPECTED_INTRO_MAX_LEN:
                    print(f"⚠️ ВНИМАНИЕ: intro подозрительно длинный ({len(intro)} символов, "
                          f"ожидалось не более {EXPECTED_INTRO_MAX_LEN}). "
                          f"Похоже на дублирование текста в коде! Отправка intro отменена.")
                else:
                    await send_chunked(thread, intro, "вводное сообщение")

                for discord_user, issues in user_issues.items():
                    user_msg = build_user_message(discord_user, issues)
                    await send_chunked(thread, user_msg, discord_user.display_name)

                if users_not_found:
                    not_found_msg = (
                        "─" * 50 + "\n\n"
                        "⚠️ **Не удалось найти в Discord:**\n"
                        + ", ".join(users_not_found)
                    )
                    await send_chunked(thread, not_found_msg, "список ненайденных")

                print(f"✅ Проверка завершена. Отправлено уведомлений: {len(user_issues)}")
            else:
                print("✅ Проблем не обнаружено")

        except Exception as e:
            print(f"Ошибка при проверке: {e}")
            try:
                thread = await client.fetch_channel(THREAD_ID)
                await thread.send(f"❌ Ошибка при проверке таблицы: {e}")
            except Exception:
                pass

# ============== НОВЫЙ ФУНКЦИОНАЛ: МЕРОПРИЯТИЯ И ОТПУСКА ==============

def load_json(filename, default=None):
    """Загружает данные из JSON файла"""
    if default is None:
        default = {}
    if not os.path.exists(filename):
        return default
    try:
        with open(filename, 'r', encoding='utf-8') as f:
            return json.load(f)
    except Exception as e:
        print(f"Ошибка загрузки {filename}: {e}")
        return default


def save_json(filename, data):
    """Сохраняет данные в JSON файл"""
    try:
        with open(filename, 'w', encoding='utf-8') as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
    except Exception as e:
        print(f"Ошибка сохранения {filename}: {e}")


def is_on_vacation_dynamic(nickname: str, current_date: datetime) -> bool:
    """Проверяет отпуск: сначала hardcoded, потом динамические из JSON"""
    # Сначала проверяем hardcoded отпуска
    if is_on_vacation(nickname, current_date):
        return True
    
    # Потом проверяем динамические отпуска
    vacations = load_json(VACATIONS_FILE, {})
    clean_nickname = nickname.strip().lower()
    current_date_only = current_date.date()
    
    for vac_name, dates in vacations.items():
        if clean_nickname == vac_name.strip().lower():
            try:
                start = datetime.fromisoformat(dates['start']).date()
                end = datetime.fromisoformat(dates['end']).date()
                if start <= current_date_only <= end:
                    return True
            except Exception:
                continue
    return False


def get_active_members(current_date: datetime) -> list:
    """Возвращает список игроков клана, которые НЕ в отпуске"""
    return [m for m in CLAN_MEMBERS if not is_on_vacation_dynamic(m, current_date)]


# ============== UI КОМПОНЕНТЫ ДЛЯ МЕРОПРИЯТИЙ ==============

class EventView(discord.ui.View):
    """View с кнопками для отметки на мероприятие"""
    def __init__(self, event_id: str):
        super().__init__(timeout=None)
        self.event_id = event_id
    
    @discord.ui.button(label="✅ Приду", style=discord.ButtonStyle.success, custom_id="event_accept")
    async def accept_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        await handle_event_response(interaction, self.event_id, "accept")
    
    @discord.ui.button(label="❌ Не приду", style=discord.ButtonStyle.danger, custom_id="event_decline")
    async def decline_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        await handle_event_response(interaction, self.event_id, "decline")


class AdminEventView(discord.ui.View):
    """View с кнопками для админ-управления мероприятием"""
    def __init__(self, event_id: str):
        super().__init__(timeout=None)
        self.event_id = event_id
    
    @discord.ui.button(label="🔄 Обновить список", style=discord.ButtonStyle.primary, custom_id="event_refresh")
    async def refresh_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id not in ADMIN_USER_IDS:
            await interaction.response.send_message("⛔ Только админ может обновлять!", ephemeral=True)
            return
        await refresh_event_message(interaction, self.event_id)
    
    @discord.ui.button(label="❌ Отменить мероприятие", style=discord.ButtonStyle.danger, custom_id="event_cancel")
    async def cancel_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id not in ADMIN_USER_IDS:
            await interaction.response.send_message("⛔ Только админ может отменять!", ephemeral=True)
            return
        await cancel_event(interaction, self.event_id)


class VacationModal(discord.ui.Modal, title="🏖️ Оформление отпуска"):
    """Модальное окно для оформления отпуска"""
    start_date = discord.ui.TextInput(
        label="Дата начала (ДД.ММ.ГГГГ)",
        placeholder="15.08.2026",
        required=True
    )
    end_date = discord.ui.TextInput(
        label="Дата окончания (ДД.ММ.ГГГГ)",
        placeholder="22.08.2026",
        required=True
    )
    
    async def on_submit(self, interaction: discord.Interaction):
        await handle_vacation_request(interaction, self.start_date.value, self.end_date.value)


class VacationRequestView(discord.ui.View):
    """View с кнопкой для оформления отпуска"""
    def __init__(self):
        super().__init__(timeout=None)
    
    @discord.ui.button(label="🏖️ Оформить отпуск", style=discord.ButtonStyle.primary, custom_id="vacation_request")
    async def vacation_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        modal = VacationModal()
        await interaction.response.send_modal(modal)


# ============== ФУНКЦИИ УПРАВЛЕНИЯ МЕРОПРИЯТИЯМИ ==============

async def handle_event_response(interaction: discord.Interaction, event_id: str, response_type: str):
    """Обрабатывает нажатие кнопки ✅ или ❌"""
    events = load_json(EVENTS_FILE, {})
    if event_id not in events:
        await interaction.response.send_message("❌ Мероприятие не найдено!", ephemeral=True)
        return
    
    event = events[event_id]
    nickname = interaction.user.display_name
    current_date = datetime.now(MSK)
    
    # Проверка отпуска
    if is_on_vacation_dynamic(nickname, current_date):
        await interaction.response.send_message(
            "🏖️ Вы сейчас в отпуске и не можете отмечаться на мероприятия!",
            ephemeral=True
        )
        return
    
    # Обновление отметок
    if response_type == "accept":
        event['accepted'][nickname] = True
        event['declined'].pop(nickname, None)
        await interaction.response.send_message("✅ Вы записаны на мероприятие!", ephemeral=True)
    else:
        event['declined'][nickname] = True
        event['accepted'].pop(nickname, None)
        await interaction.response.send_message("❌ Вы отказались от участия!", ephemeral=True)
    
    save_json(EVENTS_FILE, events)
    
    # Обновляем сообщение
    try:
        channel = await client.fetch_channel(event['channel_id'])
        message = await channel.fetch_message(event['message_id'])
        embed = build_event_embed(event_id)
        await message.edit(embed=embed)
    except Exception as e:
        print(f"Ошибка обновления сообщения: {e}")


async def refresh_event_message(interaction: discord.Interaction, event_id: str):
    """Обновляет сообщение мероприятия (пересчитывает списки)"""
    events = load_json(EVENTS_FILE, {})
    if event_id not in events:
        await interaction.response.send_message("❌ Мероприятие не найдено!", ephemeral=True)
        return
    
    event = events[event_id]
    
    try:
        channel = await client.fetch_channel(event['channel_id'])
        message = await channel.fetch_message(event['message_id'])
        embed = build_event_embed(event_id)
        await message.edit(embed=embed)
        await interaction.response.send_message("✅ Список участников обновлен!", ephemeral=True)
    except Exception as e:
        print(f"Ошибка обновления: {e}")
        await interaction.response.send_message(f"❌ Ошибка: {e}", ephemeral=True)


async def cancel_event(interaction: discord.Interaction, event_id: str):
    """Отменяет мероприятие"""
    events = load_json(EVENTS_FILE, {})
    if event_id in events:
        event = events[event_id]
        try:
            channel = await client.fetch_channel(event['channel_id'])
            message = await channel.fetch_message(event['message_id'])
            await message.delete()
        except Exception:
            pass
        
        del events[event_id]
        save_json(EVENTS_FILE, events)
        await interaction.response.send_message("✅ Мероприятие отменено!", ephemeral=True)
    else:
        await interaction.response.send_message("❌ Мероприятие не найдено!", ephemeral=True)


def build_event_embed(event_id: str) -> discord.Embed:
    """Создает embed-сообщение для мероприятия"""
    events = load_json(EVENTS_FILE, {})
    event = events[event_id]
    
    current_date = datetime.now(MSK)
    active_members = get_active_members(current_date)
    
    # Подсчет отметок
    accepted = list(event.get('accepted', {}).keys())
    declined = list(event.get('declined', {}).keys())
    unmarked = [m for m in active_members if m not in accepted and m not in declined]
    
    embed = discord.Embed(
        title=event['title'],
        description=event['description'],
        color=event.get('color', 15844367)
    )
    
    # Время
    start_ts = int(event['start_time'])
    end_ts = int(event['end_time'])
    embed.add_field(
        name="⏰ Время",
        value=f"<t:{start_ts}:F> - <t:{end_ts}:t>\n<:emoji:878391707727716413> <t:{start_ts}:R>",
        inline=False
    )
    
    # Отметки
    if accepted:
        embed.add_field(
            name=f"<:accepted:713124484436983971> Придут ({len(accepted)})",
            value=">>> " + "\n".join(accepted),
            inline=True
        )
    if declined:
        embed.add_field(
            name=f"<:declined:713124484688642068> Не придут ({len(declined)})",
            value=">>> " + "\n".join(declined),
            inline=True
        )
    if unmarked:
        embed.add_field(
            name=f"❓ Не определились ({len(unmarked)})",
            value=">>> " + "\n".join(unmarked),
            inline=False
        )
    
    embed.set_footer(text="Created by [En-Y]BURBON")
    if event.get('image'):
        embed.set_image(url=event['image'])
    
    return embed


async def create_event(title: str, description: str, start_time: datetime, end_time: datetime, 
                      image_url: str = None, color: int = 15844367):
    """Создает новое мероприятие и публикует его"""
    event_id = str(uuid.uuid4())
    events = load_json(EVENTS_FILE, {})
    
    events[event_id] = {
        'title': title,
        'description': description,
        'start_time': int(start_time.timestamp()),
        'end_time': int(end_time.timestamp()),
        'accepted': {},
        'declined': {},
        'image': image_url,
        'color': color,
        'channel_id': EVENTS_CHANNEL_ID,
        'message_id': None
    }
    save_json(EVENTS_FILE, events)
    
    # Публикация
    try:
        channel = await client.fetch_channel(EVENTS_CHANNEL_ID)
        embed = build_event_embed(event_id)
        view = EventView(event_id)
        
        message = await channel.send(embed=embed, view=view)
        events[event_id]['message_id'] = message.id
        save_json(EVENTS_FILE, events)
        
        # Отправляем админ-панель отдельным сообщением
        admin_embed = discord.Embed(
            title="🛠️ Админ-панель мероприятия",
            description=f"Мероприятие: **{title}**\nID: `{event_id}`",
            color=discord.Color.blue()
        )
        admin_view = AdminEventView(event_id)
        await channel.send(embed=admin_embed, view=admin_view)
        
        print(f"✅ Мероприятие '{title}' опубликовано")
    except Exception as e:
        print(f"❌ Ошибка публикации мероприятия: {e}")


async def post_weekly_events():
    """Автоматически публикует расписание на неделю (суббота и воскресенье)"""
    print("📅 Публикация еженедельного расписания...")
    
    today = datetime.now(MSK)
    
    # Находим следующую субботу
    days_until_saturday = (5 - today.weekday()) % 7
    if days_until_saturday == 0 and today.hour >= 16:
        days_until_saturday = 7
    
    saturday = today + timedelta(days=days_until_saturday)
    saturday_start = saturday.replace(hour=16, minute=30, second=0, microsecond=0)
    saturday_end = saturday.replace(hour=19, minute=30, second=0, microsecond=0)
    
    sunday = saturday + timedelta(days=1)
    sunday_start = sunday.replace(hour=17, minute=45, second=0, microsecond=0)
    sunday_end = sunday.replace(hour=22, minute=15, second=0, microsecond=0)
    
    # Создаем субботнее мероприятие
    await create_event(
        "СУББОТА - RTvT AS VDV",
        "На субботу запланировано мероприятие на сервере AS VDV. Требуется прибыть на мероприятие и в голосовой канал \"Мероприятие\" за 15 минут до начала игры.",
        saturday_start,
        saturday_end
    )
    
    await asyncio.sleep(2)  # Небольшая пауза между сообщениями
    
    # Создаем воскресное мероприятие
    await create_event(
        "ВОСКРЕСЕНЬЕ - TvT TT",
        "На воскресенье запланировано мероприятие на сервере TT. Требуется прибыть на мероприятие и в голосовой канал \"Мероприятие\" за 15 минут до начала игры.",
        sunday_start,
        sunday_end
    )


async def handle_vacation_request(interaction: discord.Interaction, start_str: str, end_str: str):
    """Обрабатывает запрос на оформление отпуска"""
    try:
        # Парсинг дат
        start_date = datetime.strptime(start_str, "%d.%m.%Y")
        end_date = datetime.strptime(end_str, "%d.%m.%Y")
        
        # Проверка длительности (от 7 дней до месяца)
        duration = (end_date - start_date).days
        if duration < 7:
            await interaction.response.send_message(
                "❌ Отпуск должен быть не менее 7 дней!",
                ephemeral=True
            )
            return
        if duration > 31:
            await interaction.response.send_message(
                "❌ Отпуск не может быть дольше 31 дня!",
                ephemeral=True
            )
            return
        
        # Проверка, что даты в будущем
        if start_date.date() < datetime.now(MSK).date():
            await interaction.response.send_message(
                "❌ Дата начала должна быть в будущем!",
                ephemeral=True
            )
            return
        
        # Сохранение отпуска
        vacations = load_json(VACATIONS_FILE, {})
        nickname = interaction.user.display_name
        
        vacations[nickname] = {
            'start': start_date.isoformat(),
            'end': end_date.isoformat(),
            'requested_at': datetime.now(MSK).isoformat()
        }
        save_json(VACATIONS_FILE, vacations)
        
        await interaction.response.send_message(
            f"✅ Отпуск оформлен с {start_str} по {end_str}!\n"
            f"Вы не сможете отмечаться на мероприятия в этот период.",
            ephemeral=True
        )
        
        print(f"🏖️ {nickname} оформил отпуск с {start_str} по {end_str}")
        
    except ValueError:
        await interaction.response.send_message(
            "❌ Неверный формат даты! Используйте ДД.ММ.ГГГГ (например, 15.08.2026)",
            ephemeral=True
        )
    except Exception as e:
        print(f"Ошибка оформления отпуска: {e}")
        await interaction.response.send_message(f"❌ Ошибка: {e}", ephemeral=True)


# ============== КОМАНДЫ АДМИНА ==============

async def parse_datetime(dt_str: str) -> datetime:
    """Парсит дату и время в формате 'DD.MM.YYYY HH:MM'"""
    try:
        dt = datetime.strptime(dt_str, "%d.%m.%Y %H:%M")
        return MSK.localize(dt)
    except ValueError:
        raise ValueError("Неверный формат! Используйте ДД.ММ.ГГГГ ЧЧ:ММ")


async def send_faq(message: discord.Message):
    """Отправляет подробную справку по функциям бота"""
    faq_text = """📖 **СПРАВКА ПО ФУНКЦИЯМ БОТА** 📖

**═══════════════════════════════════════**
**🔍 ПРОВЕРКА ТАБЛИЦЫ (Основная функция)**
**═══════════════════════════════════════**

📌 **Автоматическая проверка**
• Бот автоматически проверяет таблицу клана каждые 2 дня в 18:00 МСК
• Проверяет наличие красных и желтых проблем у игроков
• Учитывает отпуска (игроки в отпуске не проверяются)
• Отправляет уведомления в основной тред клана

📌 **`!check`** - Ручная проверка таблицы
• Запускает проверку немедленно (вне расписания)
• Используется для срочной проверки или тестирования
• Работает в любом канале (только для админов)

**═══════════════════════════════════════**
**💬 ОТПРАВКА СООБЩЕНИЙ**
**═══════════════════════════════════════**

📌 **`!s <текст>`** - Отправка сообщения в тред клана
• Отправляет любое текстовое сообщение в основной тред клана
• Пример: `!s Внимание! Завтра тренировка в 20:00`
• Полезно для срочных объявлений
• Работает в любом канале (только для админов)

**═══════════════════════════════════════**
**📅 МЕРОПРИЯТИЯ (Система записи)**
**═══════════════════════════════════════**

📌 **Автоматическая публикация расписания**
• Каждый понедельник в 12:00 МСК бот автоматически создает два мероприятия:
  - **Суббота (16:30-19:30 МСК)** - RTvT на AS VDV
  - **Воскресенье (17:45-22:15 МСК)** - TvT на TT
• Публикуется в канале мероприятий
• Игроки могут отмечаться кнопками ✅ Приду / ❌ Не приду
• Автоматически показывает список не отметившихся (исключая отпускников)

📌 **`!event add "Название" | "Описание" | "ДД.ММ.ГГГГ ЧЧ:ММ" | "ДД.ММ.ГГГГ ЧЧ:ММ"`**
• Создает новое мероприятие вручную
• Пример: `!event add "Турнир" | "Еженедельный турнир" | "20.08.2026 18:00" | "20.08.2026 22:00"`
• Автоматически публикует embed-сообщение с кнопками
• Создает админ-панель для управления мероприятием

📌 **`!event cancel <message_id>`** - Отмена мероприятия
• Удаляет мероприятие и его сообщение
• Пример: `!event cancel 1234567890123456789`
• message_id можно найти через `!event list`

📌 **`!event list`** - Список всех активных мероприятий
• Показывает все текущие мероприятия с их ID
• Показывает количество отметившихся игроков
• Используется для получения message_id для отмены

📌 **Кнопки в сообщении мероприятия:**
• ✅ **Приду** - Игрок записывается на мероприятие
• ❌ **Не приду** - Игрок отказывается от участия
• 🔄 **Обновить список** (только админ) - Пересчитывает списки с учетом новых отпусков
• ❌ **Отменить мероприятие** (только админ) - Быстрая отмена через кнопку

**═══════════════════════════════════════**
**🏖️ СИСТЕМА ОТПУСКОВ**
**═══════════════════════════════════════**

📌 **Автоматическое оформление отпусков игроками**
• Игроки могут сами оформить отпуск через кнопку 🏖️ в канале отпусков
• Отпуск должен быть от 7 до 31 дня
• Дата начала должна быть в будущем
• Во время отпуска игрок не может отмечаться на мероприятия
• Игрок в отпуске не попадает в список "Не определившихся"

📌 **`!vacation setup`** - Публикация кнопки оформления отпуска
• Создает сообщение с кнопкой для оформления отпусков
• Публикуется в канале отпусков
• Используется один раз для настройки системы

📌 **`!vacation list`** - Список всех активных отпусков
• Показывает всех игроков, которые сейчас в отпуске
• Показывает даты начала и окончания отпуска
• Полезно для проверки статуса игроков

📌 **`!vacation remove <ник>`** - Досрочный вывод из отпуска
• Выводит игрока из отпуска раньше срока
• Пример: `!vacation remove [En-Y]Killa`
• Поиск работает по частичному совпадению ника
• После вывода игрок снова может отмечаться на мероприятия

**═══════════════════════════════════════**
**🛠️ ТЕХНИЧЕСКИЕ КОМАНДЫ**
**═══════════════════════════════════════**

📌 **`!faq`** - Эта справка
• Показывает подробное описание всех функций бота
• Доступна только в админском канале

📌 **Защита от двойного запуска**
• Бот использует lock-файл для предотвращения дублирования
• Если бот уже запущен, новая копия автоматически завершится
• Lock-файл автоматически удаляется при корректном завершении

📌 **Автоматическое обновление**
• Сервер автоматически проверяет обновления каждую минуту
• При наличии новых коммитов на GitHub бот перезапускается
• Все данные (мероприятия, отпуска) сохраняются в JSON-файлах

**═══════════════════════════════════════**
**📝 ПРИМЕРЫ ИСПОЛЬЗОВАНИЯ**
**═══════════════════════════════════════**

🔹 **Обычный день:**
• !check → Проверить таблицу сейчас
• !s Внимание! → Отправить объявление

🔹 **Создание мероприятия:**
• !event add "Турнир" | "Еженедельный турнир по Arma" | "25.08.2026 19:00" | "25.08.2026 23:00"
• !event list → Посмотреть список мероприятий
• !event cancel 123 → Отменить мероприятие

🔹 **Управление отпусками:**
• !vacation setup → Опубликовать кнопку для игроков
• !vacation list → Посмотреть всех в отпуске
• !vacation remove [En-Y]Killa → Вывести из отпуска


**═══════════════════════════════════════**
**⚠️ ВАЖНЫЕ ЗАМЕЧАНИЯ**
**═══════════════════════════════════════**

• Все команды работают только в админском канале
• Все админские команды доступны только трем администраторам
• Данные мероприятий и отпусков сохраняются автоматически
• Бот автоматически перезапускается при обновлениях кода
• Игроки в отпуске автоматически исключаются из всех проверок

**═══════════════════════════════════════**
**📞 ПОДДЕРЖКА**
**═══════════════════════════════════════**

Если возникли проблемы или вопросы:
1. Проверьте логи бота на сервере
2. Убедитесь, что файлы `events_data.json` и `vacations.json` существуют
3. Проверьте права бота в Discord (отправка сообщений, управление ролями)
4. Обратитесь к разработчику бота

**═══════════════════════════════════════**
"""
    await send_chunked(message.channel, faq_text, "FAQ")
    await message.add_reaction('✅')


async def handle_admin_command(message: discord.Message):
    """Обрабатывает команды админа"""
    if message.author.id not in ADMIN_USER_IDS:
        return
    
    content = message.content.strip()
    
    if not content.startswith('!'):
        return
    
    # Убираем префикс !
    command_text = content[1:]
    
    # Разделяем на команду и аргументы
    parts = command_text.split(maxsplit=1)
    command_name = parts[0] if parts else ""
    command_args = parts[1] if len(parts) > 1 else ""
    
    try:
        # Команда !faq (без аргументов)
        if command_name == "faq":
            await send_faq(message)
            return
        
        # Команды мероприятий: !event add/cancel/list
        elif command_name == "event":
            if command_args.startswith("add"):
                # Формат: !event add "Название" | "Описание" | "ДД.ММ.ГГГГ ЧЧ:ММ" | "ДД.ММ.ГГГГ ЧЧ:ММ"
                args = command_args[len("add"):].strip().split("|")
                if len(args) < 4:
                    await message.channel.send("❌ Формат: `!event add \"Название\" | \"Описание\" | \"ДД.ММ.ГГГГ ЧЧ:ММ\" | \"ДД.ММ.ГГГГ ЧЧ:ММ\"`")
                    return
                
                title = args[0].strip().strip('"')
                description = args[1].strip().strip('"')
                start_time = await parse_datetime(args[2].strip().strip('"'))
                end_time = await parse_datetime(args[3].strip().strip('"'))
                
                await create_event(title, description, start_time, end_time)
                await message.add_reaction('✅')
                
            elif command_args.startswith("cancel"):
                # Формат: !event cancel <message_id>
                args = command_args.split()
                if len(args) < 2:
                    await message.channel.send("❌ Формат: `!event cancel <message_id>`")
                    return
                
                message_id = int(args[1])
                events = load_json(EVENTS_FILE, {})
                
                for event_id, event in list(events.items()):
                    if event.get('message_id') == message_id:
                        del events[event_id]
                        save_json(EVENTS_FILE, events)
                        await message.channel.send(f"✅ Мероприятие отменено!")
                        await message.add_reaction('✅')
                        return
                
                await message.channel.send("❌ Мероприятие не найдено!")
                
            elif command_args == "list":
                events = load_json(EVENTS_FILE, {})
                if not events:
                    await message.channel.send("📭 Нет активных мероприятий")
                    return
                
                text = "📋 **Активные мероприятия:**\n\n"
                for event_id, event in events.items():
                    start = datetime.fromtimestamp(event['start_time'], MSK)
                    text += f"**{event['title']}**\n"
                    text += f"ID: `{event_id}`\n"
                    text += f"Дата: {start.strftime('%d.%m.%Y %H:%M')}\n"
                    text += f"✅ Придут: {len(event.get('accepted', {}))}\n"
                    text += f"❌ Не придут: {len(event.get('declined', {}))}\n\n"
                
                await message.channel.send(text)
            else:
                await message.channel.send("❌ Неизвестная команда! Используйте `!event add`, `!event cancel` или `!event list`")
        
        # Команды отпусков: !vacation remove/list/setup
        elif command_name == "vacation":
            if command_args.startswith("remove"):
                # Формат: !vacation remove <nickname>
                args = command_args.split(maxsplit=1)
                if len(args) < 2:
                    await message.channel.send("❌ Формат: `!vacation remove [En-Y]Nickname`")
                    return
                
                nickname = args[1].strip()
                vacations = load_json(VACATIONS_FILE, {})
                
                # Ищем по частичному совпадению
                found = False
                for vac_name in list(vacations.keys()):
                    if nickname.lower() in vac_name.lower():
                        del vacations[vac_name]
                        save_json(VACATIONS_FILE, vacations)
                        await message.channel.send(f"✅ {vac_name} выведен из отпуска досрочно!")
                        found = True
                        break
                
                if not found:
                    await message.channel.send(f"❌ Игрок {nickname} не найден в отпусках!")
                else:
                    await message.add_reaction('✅')
                    
            elif command_args == "list":
                vacations = load_json(VACATIONS_FILE, {})
                if not vacations:
                    await message.channel.send("🏖️ Нет активных отпусков")
                    return
                
                text = "🏖️ **Активные отпуска:**\n\n"
                for nickname, dates in vacations.items():
                    start = datetime.fromisoformat(dates['start']).strftime('%d.%m.%Y')
                    end = datetime.fromisoformat(dates['end']).strftime('%d.%m.%Y')
                    text += f"**{nickname}**: {start} - {end}\n"
                
                await message.channel.send(text)
                
            elif command_args == "setup":
                # Публикует кнопку для оформления отпуска
                channel = await client.fetch_channel(VACATION_CHANNEL_ID)
                embed = discord.Embed(
                    title="🏖️ Оформление отпуска",
                    description=(
                        "Здесь вы можете оформить отпуск на период от 7 до 31 дня.\n\n"
                        "Во время отпуска вы не сможете отмечаться на мероприятия.\n"
                        "Если вы хотите выйти из отпуска досрочно, обратитесь к администратору."
                    ),
                    color=discord.Color.green()
                )
                view = VacationRequestView()
                await channel.send(embed=embed, view=view)
                await message.add_reaction('✅')
            else:
                await message.channel.send("❌ Неизвестная команда! Используйте `!vacation remove`, `!vacation list` или `!vacation setup`")
        
        else:
            await message.channel.send(f"❌ Неизвестная команда `!{command_name}`. Напишите `!faq` для справки.")
            
    except Exception as e:
        print(f"Ошибка обработки команды: {e}")
        await message.channel.send(f"❌ Ошибка: {e}")


# ============== СОБЫТИЯ DISCORD ==============

@client.event
async def on_ready():
    print(f'Бот запущен как {client.user} (PID {os.getpid()})')

    if not scheduler.get_job('spreadsheet_check'):
        scheduler.add_job(
            check_spreadsheet,
            'cron',
            day='*/2',
            hour=18,
            minute=0,
            id='spreadsheet_check',
            replace_existing=True
        )

    # Добавляем еженедельную публикацию мероприятий (каждый понедельник в 12:00 МСК)
    if not scheduler.get_job('weekly_events'):
        scheduler.add_job(
            post_weekly_events,
            'cron',
            day_of_week='mon',
            hour=12,
            minute=0,
            id='weekly_events',
            replace_existing=True
        )
        print("📅 Планировщик еженедельных мероприятий добавлен (понедельник, 12:00 МСК)")

    if not scheduler.running:
        scheduler.start()
        print("Планировщик запущен. Следующая проверка таблицы через 2 дня в 18:00 МСК")
        print("Для ручной проверки используйте команду: !check")
    
    # Регистрируем View для persistent кнопок
    client.add_view(EventView("dummy"))
    client.add_view(AdminEventView("dummy"))
    client.add_view(VacationRequestView())
    print("✅ UI компоненты зарегистрированы")


@client.event
async def on_message(message):
    if dedup.mark_processed(message.id):
        return

    if message.author == client.user:
        return
    
    # Команды админа (только в админ-канале)
    if message.channel.id == ADMIN_CHANNEL_ID and message.author.id in ADMIN_USER_IDS:
        if message.content.startswith('!'):
            await handle_admin_command(message)
            return
    
    # Команда для публикации кнопки отпуска (только админ в любом канале)
    if message.author.id in ADMIN_USER_IDS and message.content == '!vacation setup':
        channel = await client.fetch_channel(VACATION_CHANNEL_ID)
        embed = discord.Embed(
            title="🏖️ Оформление отпуска",
            description=(
                "Здесь вы можете оформить отпуск на период от 7 до 31 дня.\n\n"
                "Во время отпуска вы не сможете отмечаться на мероприятия.\n"
                "Если вы хотите выйти из отпуска досрочно, обратитесь к администратору."
            ),
            color=discord.Color.green()
        )
        view = VacationRequestView()
        await channel.send(embed=embed, view=view)
        await message.add_reaction('✅')
        return

    # Старый функционал (только для админов)
    if message.author.id not in ADMIN_USER_IDS:
        return

    if message.content.startswith('!check'):
        if check_lock.locked():
            await message.channel.send('⚠️ Проверка уже выполняется, подождите.')
            return
        await message.channel.send('🔍 Запускаю проверку таблицы...')
        await check_spreadsheet()
        await message.add_reaction('✅')
        return

    if not message.content.startswith(PREFIX):
        return

    text = message.content[len(PREFIX):].strip()
    if not text:
        return

    try:
        thread = await client.fetch_channel(THREAD_ID)
        await thread.send(text)
        await message.add_reaction('✅')
    except Exception as e:
        await message.channel.send(f'❌ Ошибка: {e}')


if __name__ == '__main__':
    try:
        client.run(os.environ['DISCORD_TOKEN'])
    finally:
        if os.path.exists(LOCK_FILE):
            try:
                with open(LOCK_FILE, 'r') as f:
                    saved_pid = f.read().strip()
                if saved_pid == str(os.getpid()):
                    os.remove(LOCK_FILE)
            except Exception:
                pass