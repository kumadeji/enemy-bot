import discord
import os
import sys
import signal
import atexit
import gspread
import re
from datetime import datetime
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

THREAD_ID = 1503003066641809418
ALLOWED_USER_ID = 115475534544109573
PREFIX = '!s '

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

# Максимально ожидаемая длина вводного сообщения.
# Если фактическая длина больше — значит, при копировании файла
# текст случайно продублировался, отправлять его нельзя.
EXPECTED_INTRO_MAX_LEN = 700

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
    """Возвращает список строк вводного сообщения.
    Каждая строка — отдельный элемент списка, поэтому случайное
    дублирование блока сразу вызовет синтаксическую ошибку
    (нужна запятая) или будет визуально заметно — молча склеиться,
    как в случае с конкатенацией литералов, это не может."""
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
    """Группирует проблемы пользователя по критичности и формирует текст:

    👤 @Пользователь

    🔴 Критические проблемы, требующие скорейшего исправления:
    * <текст проблемы 1>
    * <текст проблемы 2>

    🟡 Важные, но менее критические проблемы, также требующие своевременного исправления:
    * <текст проблемы 1>

    ──────────────────────────────────────────────────
    """
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

    if not scheduler.running:
        scheduler.start()
        print("Планировщик запущен. Следующая проверка через 2 дня в 18:00 МСК")
        print("Для ручной проверки используйте команду: !check")


@client.event
async def on_message(message):
    if dedup.mark_processed(message.id):
        return

    if message.author == client.user:
        return
    if message.author.id != ALLOWED_USER_ID:
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
