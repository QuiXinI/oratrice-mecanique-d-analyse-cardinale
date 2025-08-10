import asyncio
import os
import time
import logging
import sqlite3
from datetime import datetime, timezone
from dotenv import load_dotenv
from pyrogram import Client, filters, idle
from pyrogram.types import ChatPermissions
from pyrogram.enums import ParseMode
from pyrogram.errors import RPCError

# --------- ПУТЬ К РЕСУРСАМ ---------
RESOURCES_DIR = "resources"
os.makedirs(RESOURCES_DIR, exist_ok=True)
DB_PATH = os.path.join(RESOURCES_DIR, "bot.db")

# ------------- НАСТРОЙКА ЛОГИРОВАНИЯ -------------
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logging.info("Бот запускается...")

# ------------- ЗАГРУЖАЕМ СЕКРЕТЫ -------------
load_dotenv()
API_ID = int(os.getenv("API_ID", "0"))
API_HASH = os.getenv("API_HASH", "")
BOT_TOKEN = os.getenv("BOT_TOKEN", "")

if not API_ID or not API_HASH or not BOT_TOKEN:
    logging.error("Проверь .env: не хватает API_ID/API_HASH/BOT_TOKEN")
    exit(1)

# ------------- ЗАГРУЖАЕМ КОНФИГ -------------
config_path = os.path.join(RESOURCES_DIR, "config.json")
if not os.path.isfile(config_path):
    logging.error(f"Не найден файл конфигурации: {config_path}")
    exit(1)
with open(config_path, "r", encoding="utf-8") as f:
    config = __import__('json').load(f)

DEFAULT_MUTE_SECONDS = config.get("default_mute_seconds", 600)
LOG_CHAT_ID = config.get("log_chat_id", 0)

# ------------- ИНИЦИАЛИЗАЦИЯ БАЗЫ ДАННЫХ -------------
def init_db():
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    # Таблица админов
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS admins (
            chat_id INTEGER NOT NULL,
            user_id INTEGER NOT NULL,
            role INTEGER NOT NULL,
            PRIMARY KEY (chat_id, user_id)
        )
    """)
    # Таблица мутов
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS mutes (
            chat_id INTEGER NOT NULL,
            user_id INTEGER NOT NULL,
            unmute_ts INTEGER NOT NULL,
            PRIMARY KEY (chat_id, user_id)
        )
    """)
    # Таблица логов
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS logs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            target_id INTEGER NOT NULL,
            time_ts INTEGER NOT NULL,
            action TEXT NOT NULL,
            by_id INTEGER NOT NULL,
            chat_id INTEGER NOT NULL
        )
    """)
    conn.commit()
    return conn

# Создаём или открываем БД
conn = init_db()

# Класс для отслеживания запросов (на подтверждение действий)
class RequestTracker:
    def __init__(self):
        # структура: { (command_type, message_id) : [ (user_id, timestamp), ... ] }
        self.requests = {}

    def add_request(self, command_type, message_id, user_id, timestamp):
        key = (command_type, message_id)
        self.requests.setdefault(key, []).append((user_id, timestamp))

    def get_requestors(self, command_type, message_id):
        key = (command_type, message_id)
        return [u for u, t in self.requests.get(key, [])]

    def clean_old_requests(self, command_type, message_id, current_time, time_window=600):
        key = (command_type, message_id)
        if key in self.requests:
            self.requests[key] = [(u, t) for (u, t) in self.requests[key] if current_time - t <= time_window]
            if not self.requests[key]:
                del self.requests[key]

request_tracker = RequestTracker()

# ------------- ФУНКЦИИ ДЛЯ РОЛЕЙ -------------
def get_role(chat_id: int, user_id: int) -> int:
    cursor = conn.execute(
        "SELECT role FROM admins WHERE chat_id = ? AND user_id = ?", (chat_id, user_id)
    )
    row = cursor.fetchone()
    return row[0] if row else 0

# Преобразование роли в строку
def role_to_str(role: int) -> str:
    mapping = {
        0: "Пользователь",
        1: "Модератор",
        2: "Админ",
        3: "Владелец",
        4: "Основатель"
    }
    return mapping.get(role, "Неизвестно")

def set_role(chat_id: int, user_id: int, role: int):
    conn.execute(
        "INSERT OR REPLACE INTO admins (chat_id, user_id, role) VALUES (?, ?, ?)",
        (chat_id, user_id, role)
    )
    conn.commit()

def del_role(chat_id: int, user_id: int):
    conn.execute(
        "DELETE FROM admins WHERE chat_id = ? AND user_id = ?", (chat_id, user_id)
    )
    conn.commit()

# ------------- МУТЫ -------------
def add_mute(chat_id: int, user_id: int, unmute_ts: int):
    conn.execute(
        "INSERT OR REPLACE INTO mutes (chat_id, user_id, unmute_ts) VALUES (?, ?, ?)",
        (chat_id, user_id, unmute_ts)
    )
    conn.commit()

def del_mute(chat_id: int, user_id: int):
    conn.execute(
        "DELETE FROM mutes WHERE chat_id = ? AND user_id = ?", (chat_id, user_id)
    )
    conn.commit()

def get_all_mutes():
    cursor = conn.execute("SELECT chat_id, user_id, unmute_ts FROM mutes")
    return cursor.fetchall()

# ------------- ЛОГИРОВАНИЕ -------------
def log_action(target_id: int, action: str, by_id: int, chat_id: int):
    now_ts = int(time.time())
    conn.execute(
        "INSERT INTO logs (target_id, time_ts, action, by_id, chat_id) VALUES (?, ?, ?, ?, ?)",
        (target_id, now_ts, action, by_id, chat_id)
    )
    conn.commit()
    logging.info(f"Записано действие для {target_id}: {action} от {by_id} в чате {chat_id}")

def get_user_logs(target_id: int):
    cursor = conn.execute(
        "SELECT time_ts, action FROM logs WHERE target_id = ? ORDER BY time_ts DESC", (target_id,)
    )
    return cursor.fetchall()

# ------------- ФОНОВАЯ ФУНКЦИЯ ДЛЯ РАЗМЮТА -------------
async def schedule_unmute(app: Client, chat_id: int, user_id: int, unmute_ts: int):
    now = int(time.time())
    delay = unmute_ts - now
    chat_link = f"[чат](tg://chat?id={chat_id})"
    if delay > 0:
        await asyncio.sleep(delay)
    try:
        await app.restrict_chat_member(chat_id, user_id, permissions=ChatPermissions(
            can_send_messages=True,
            can_send_media_messages=True,
            can_send_other_messages=True,
            can_add_web_page_previews=True
        ))
        logging.info(f"Размутил {user_id} в чате {chat_link}")
    except RPCError as e:
        logging.warning(f"Не удалось размутить {user_id} в чате {chat_link}: {e}")
    del_mute(chat_id, user_id)
    try:
        user = await app.get_users(user_id)
        username = getattr(user, 'username', None)
        if username:
            await app.send_message(chat_id, f"@{username} размучен автоматически.")
        else:
            await app.send_message(chat_id, f"Пользователь [ID:{user_id}] размучен автоматически.")
    except RPCError:
        pass
    try:
        await app.send_message(user_id, f"Ты размучен(а) в {chat_link}.", parse_mode=ParseMode.MARKDOWN)
    except RPCError:
        pass

# ------------- ФУНКЦИЯ ДЛЯ ОЧИСТКИ ЛОГОВ -------------
async def cleanup_logs():
    while True:
        now = int(time.time())
        cutoff = now - 48 * 3600
        # Удаляем записи старше 48 часов
        conn.execute("DELETE FROM logs WHERE time_ts <= ?", (cutoff,))
        conn.commit()
        await asyncio.sleep(3600)

# ------------- ИНИЦИАЛИЗАЦИЯ КЛИЕНТА -------------
session_path = os.path.join(RESOURCES_DIR, "admin_bot")
app = Client(session_path, api_id=API_ID, api_hash=API_HASH, bot_token=BOT_TOKEN)

# ------------- ДИНАМИЧЕСКИЙ /help -------------
@app.on_message(filters.command("help") & filters.group)
async def help_handler(client, message):
    chat_id = message.chat.id
    sender_id = message.from_user.id
    role = get_role(chat_id, sender_id)
    text = (
        "Доступные команды:\n"
        "/help — показать список команд\n"
        "/report [сообщение] — отправить сообщение модераторам/админам/владельцам\n"
    )
    # общедоступные для модераторов и выше
    if role >= 1:
        text += (
            "/kick @username [причина] — кикнуть пользователя (работает и в ответ на сообщение)\n"
            "/mute [время] — замутить пользователя (в ответ на сообщение: /mute 1h или /mute 30m)\n"
            "/unmute — размутить пользователя (в ответ на сообщение или /unmute @username)\n"
            "/delete — удалить сообщение (в ответ на сообщение)\n"
            "/clear — блокировка пользователя и удаление его сообщений\n"
            "/шлюхобот — блокировка и отправка публичного сообщения\n"
        )
    if role == 1:
        text += (
            "/clear — блокировка пользователя и удаление его сообщений (требуется подтверждение другого админа при уровне < 2)\n"
            "/шлюхобот — блокировка и отправка публичного сообщения (требуется подтверждение другого админа при уровне < 2)\n"
        )
    elif role == 2:
        text += "/promote @username 1 — назначить Модератора\n"
        text += "/demote @username — снять роль Модератора\n"
    elif role == 3:
        text += (
            "/promote @username [1-3] — назначить Модератора, Админа или Владельца\n"
            "/demote @username — снять роль Модератора, Админа или Владельца\n"
        )
    elif role == 4:
        text += (
            "/promote @username [1-4] — назначить любую роль\n"
            "/demote @username — снять любую роль\n"
        )
    await message.reply(text)

# ------------- ПРИВЕТСТВИЕ ПРИ ВХОДЕ -------------
@app.on_message(filters.new_chat_members)
async def greet_new_users(client, message):
    for new_user in message.new_chat_members:
        try:
            await client.send_message(new_user.id, f"Ты присоединился к чату '{message.chat.title}'. Добро пожаловать!")
        except RPCError:
            await message.reply_photo(
                photo=f"{RESOURCES_DIR}/greeting.jpg",  # относительный путь или абсолютный
                caption=f"Добро пожаловать, {new_user.mention}! Напиши мне в ЛС, чтобы получать уведомления."
            )

# ------------- ПАРСЕР ВРЕМЕНИ -------------
def parse_duration(text: str) -> int:
    units = {'s': 1, 'm': 60, 'h': 3600, 'd': 86400, 'w': 604800}
    try:
        text = text.strip()
        if not text:
            return DEFAULT_MUTE_SECONDS
        unit = text[-1].lower()
        if unit in units and text[:-1].isdigit():
            return int(text[:-1]) * units[unit]
        if text.isdigit():
            return int(text) * 60
        return DEFAULT_MUTE_SECONDS
    except (ValueError, IndexError):
        return DEFAULT_MUTE_SECONDS

# ------------- ХАНДЛЕР ДЛЯ /report -------------
@app.on_message(filters.command("report") & filters.group)
async def report_handler(client, message):
    chat_id = message.chat.id
    sender = message.from_user
    args = message.text.split(maxsplit=2)
    if message.reply_to_message:
        reported_message = message.reply_to_message
        reported_user = reported_message.from_user
        content = reported_message.text or "[Без текста]"
        if reported_message.photo:
            content = "[Картинка]"
        elif reported_message.video:
            content = "[Видео]"
        elif reported_message.animation:
            content = "[Гиф]"
        elif reported_message.document:
            content = "[Документ]"
        elif reported_message.sticker:
            content = "[Стикер]"
        elif reported_message.audio:
            content = "[Аудио]"
        elif reported_message.voice:
            content = "[Голосовое сообщение]"
    else:
        if len(args) >= 2 and args[1].startswith("@"):
            try:
                reported_user = await client.get_users(args[1])
            except RPCError:
                await message.reply("Не могу найти пользователя.")
                return
            content = args[2] if len(args) > 2 else "[Без текста]"
        else:
            await message.reply("Используй: /report в ответ на сообщение или /report @username [сообщение]")
            return
    chat_admins = conn.execute("SELECT user_id, role FROM admins WHERE chat_id = ?", (chat_id,)).fetchall()
    mentions = []
    for uid, role_int in chat_admins:
        if role_int >= 1:
            try:
                user = await client.get_users(uid)
                mentions.append(f"@{user.username}" if user.username else f"[{user.first_name}](tg://user?id={user.id})")
            except RPCError:
                pass
    if not mentions:
        await message.reply("Нет активных модераторов/админов/владельцев.")
        return
    ping_list = " ".join(mentions)
    reporter_link = f"[{sender.first_name}](tg://user?id={sender.id})"
    reported_link = f"[{reported_user.first_name}](tg://user?id={reported_user.id})"
    header = f"{reporter_link} зарепортил(а) {reported_link}"
    reply_msg = f"{header} > {content} Внимание: {ping_list}"
    await message.reply(reply_msg)

# ------------- ХАНДЛЕР ДЛЯ /promote -------------
@app.on_message(filters.command("promote") & filters.group)
async def promote_handler(client, message):
    chat_id = message.chat.id
    sender = message.from_user
    args = message.text.split()

    if len(args) < 3:
        await message.reply("Используй: /promote @username уровень")
        return

    try:
        target_user = await client.get_users(args[1])
    except RPCError:
        await message.reply("Не могу найти пользователя.")
        return

    if sender.id == target_user.id:
        await message.reply("Себя продвигать нельзя.")
        return

    try:
        new_role = int(args[2])
    except ValueError:
        await message.reply("Роль должна быть числом.")
        return

    sender_role = get_role(chat_id, sender.id)
    target_role = get_role(chat_id, target_user.id)

    if sender_role == 2 and new_role != 1:
        await message.reply("Нельзя: админ может дать только роль Модератора.")
        return

    if sender_role == 3 and (new_role < 1 or new_role > 3):
        await message.reply("Нельзя: владелец может дать роли Модератора, Админа или Владельца.")
        return

    if sender_role < 2:
        await message.reply("Нельзя: недостаточно прав.")
        return

    if sender_role <= target_role:
        if sender_role == target_role:
            await message.reply(f"Нельзя: вы оба {role_to_str(sender_role)}.")
        else:
            await message.reply(f"Нельзя: цель — {role_to_str(target_role)}, а вы — {role_to_str(sender_role)}.")
        return

    set_role(chat_id, target_user.id, new_role)
    await message.reply(f"{args[1]} теперь {role_to_str(new_role)}.")
    log_action(target_user.id, f"повышение до {new_role}", sender.id, chat_id)

    try:
        chat_link = f"[{message.chat.title}](tg://chat?id={chat_id})"
        await client.send_message(target_user.id, f"Тебя назначили {role_to_str(new_role)} в {chat_link}.", parse_mode=ParseMode.MARKDOWN)
    except RPCError:
        pass

# ------------- ХАНДЛЕР ДЛЯ /demote -------------
@app.on_message(filters.command("demote") & filters.group)
async def demote_handler(client, message):
    chat_id = message.chat.id
    sender = message.from_user
    args = message.text.split()
    if len(args) < 2:
        await message.reply("Используй: /demote @username")
        return
    try:
        target_user = await client.get_users(args[1])
    except RPCError:
        await message.reply("Не могу найти пользователя.")
        return
    if sender.id == target_user.id:
        await message.reply("Себя разжаловать нельзя.")
        return
    sender_role = get_role(chat_id, sender.id)
    target_role = get_role(chat_id, target_user.id)
    if target_role == 0:
        await message.reply("Нельзя: пользователь и так прост.")
        return
    if sender_role == 2 and target_role != 1:
        await message.reply("Нельзя: админ может понижать только модераторов.")
        return
    if sender_role == 3 and (target_role < 1 or target_role > 3):
        await message.reply("Нельзя: владелец может понижать только модераторов, админов и владельцев.")
        return
    if sender_role < 2:
        await message.reply("Нельзя: недостаточно прав.")
        return
    if sender_role <= target_role and target_role != 3:
        if sender_role == target_role:
            await message.reply(f"Нельзя: вы оба {role_to_str(sender_role)}.")
        else:
            await message.reply(f"Нельзя: цель — {role_to_str(target_role)}, а вы — {role_to_str(sender_role)}.")
        return
    del_role(chat_id, target_user.id)
    await message.reply(f"{args[1]} понижен(а).)")
    log_action(target_user.id, "понижение", sender.id, chat_id)
    try:
        chat_link = f"[{message.chat.title}](tg://chat?id={chat_id})"
        await client.send_message(target_user.id, f"Тебя понизили в {chat_link}.", parse_mode=ParseMode.MARKDOWN)
    except RPCError:
        pass

# ------------- ХАНДЛЕР ДЛЯ /kick -------------
@app.on_message(filters.command("kick") & filters.group)
async def kick_handler(client, message):
    chat_id = message.chat.id
    sender = message.from_user
    args = message.text.split(maxsplit=2)
    target_user = None

    # Обработка ответа на сообщение
    if message.reply_to_message:
        target_user = message.reply_to_message.from_user
    elif len(args) >= 2 and args[1].startswith("@"):
        try:
            target_user = await client.get_users(args[1])
        except RPCError:
            await message.reply("Не могу найти пользователя.")
            return
    else:
        await message.reply("Используй: /kick @username [причина] или в ответ на сообщение")
        return

    if sender.id == target_user.id:
        await message.reply("Себя кикнуть нельзя.")
        return

    sender_role = get_role(chat_id, sender.id)
    target_role = get_role(chat_id, target_user.id)

    if sender_role < 1:
        await message.reply("Нельзя: недостаточно прав.")
        return

    if sender_role <= target_role:
        if sender_role == target_role:
            await message.reply(f"Нельзя: вы оба {role_to_str(sender_role)}.")
        else:
            await message.reply(f"Нельзя: цель — {role_to_str(target_role)}, а вы — {role_to_str(sender_role)}.")
        return

    reason = args[2] if len(args) == 3 else None
    try:
        await client.ban_chat_member(chat_id, target_user.id)
        await client.unban_chat_member(chat_id, target_user.id)
    except RPCError as e:
        await message.reply(f"Не смог кикнуть: {e}")
        return

    if reason:
        reply_text = f"{target_user.first_name} кикнут(а) по причине \"{reason}\""
        user_text = f"Ты кикнут(а) из [чат](tg://chat?id={chat_id}) по причине \"{reason}\""
        log_action(target_user.id, f"кик по причине {reason}", sender.id, chat_id)
    else:
        reply_text = f"{target_user.first_name} кикнут(а)"
        user_text = f"Ты кикнут(а) из [чат](tg://chat?id={chat_id})"
        log_action(target_user.id, "кик без причины", sender.id, chat_id)

    await message.reply(reply_text)
    try:
        await client.send_message(target_user.id, user_text, parse_mode=ParseMode.MARKDOWN)
    except RPCError:
        pass

# ------------- ХАНДЛЕР ДЛЯ /mute -------------
@app.on_message(filters.command("mute") & filters.group)
async def mute_handler(client, message):
    chat_id = message.chat.id
    sender = message.from_user
    args = message.text.split()
    target_user = None

    if message.reply_to_message:
        target_user = message.reply_to_message.from_user
        # allow: reply + "/mute 1h" (time is args[1])
        if len(args) >= 2:
            time_arg = args[1]
        else:
            time_arg = None
    elif len(args) >= 2 and args[1].startswith("@"):
        try:
            target_user = await client.get_users(args[1])
        except RPCError:
            await message.reply("Не могу найти пользователя.")
            return
        time_arg = args[2] if len(args) >= 3 else None
    else:
        await message.reply("Используй: /mute @username [время] или в ответ на сообщение (/mute 1h)")
        return

    if sender.id == target_user.id:
        await message.reply("Себя замутить нельзя.")
        return

    sender_role = get_role(chat_id, sender.id)
    target_role = get_role(chat_id, target_user.id)

    if sender_role < 1:
        await message.reply("Нельзя: недостаточно прав.")
        return

    if sender_role <= target_role:
        if sender_role == target_role:
            await message.reply(f"Нельзя: вы оба {role_to_str(sender_role)}.")
        else:
            await message.reply(f"Нельзя: цель — {role_to_str(target_role)}, а вы — {role_to_str(sender_role)}.")
        return

    mute_seconds = DEFAULT_MUTE_SECONDS
    if time_arg:
        mute_seconds = parse_duration(time_arg)
    unmute_ts = int(time.time()) + mute_seconds
    until_date_dt = datetime.fromtimestamp(unmute_ts, timezone.utc)
    try:
        await client.restrict_chat_member(chat_id, target_user.id,
            permissions=ChatPermissions(
                can_send_messages=False,
                can_send_media_messages=False,
                can_send_other_messages=False,
                can_add_web_page_previews=False
            ), until_date=until_date_dt)
        logging.info(f"Замутил {target_user.id} в чате {chat_id} до {unmute_ts}")
    except RPCError as e:
        await message.reply(f"Не смог замутить: {e}")
        return
    add_mute(chat_id, target_user.id, unmute_ts)
    chat_link = f"[{message.chat.title}](tg://chat?id={chat_id})"
    until_str = until_date_dt.strftime("%Y-%m-%d %H:%M UTC")
    await message.reply(f"{target_user.first_name} замучен(а) до {until_str}.")
    log_action(target_user.id, f"замьютил до {until_str}", sender.id, chat_id)
    try:
        await client.send_message(target_user.id, f"Ты замучен(а) до {until_str} в {chat_link}.", parse_mode=ParseMode.MARKDOWN)
    except RPCError:
        pass
    asyncio.create_task(schedule_unmute(client, chat_id, target_user.id, unmute_ts))

# ------------- ХАНДЛЕР ДЛЯ /unmute -------------
@app.on_message(filters.command("unmute") & filters.group)
async def unmute_handler(client, message):
    chat_id = message.chat.id
    sender = message.from_user
    args = message.text.split()
    target_user = None

    if message.reply_to_message:
        target_user = message.reply_to_message.from_user
    elif len(args) >= 2 and args[1].startswith("@"):
        try:
            target_user = await client.get_users(args[1])
        except RPCError:
            await message.reply("Не могу найти пользователя.")
            return
    else:
        await message.reply("Используй: /unmute @username или в ответ на сообщение")
        return

    if sender.id == target_user.id:
        await message.reply("Себя размутить нельзя.")
        return

    sender_role = get_role(chat_id, sender.id)
    target_role = get_role(chat_id, target_user.id)

    if sender_role < 1:
        await message.reply("Нельзя: недостаточно прав.")
        return

    if sender_role <= target_role:
        if sender_role == target_role:
            await message.reply(f"Нельзя: вы оба {role_to_str(sender_role)}.")
        else:
            await message.reply(f"Нельзя: цель — {role_to_str(target_role)}, а вы — {role_to_str(sender_role)}.")
        return

    try:
        await client.restrict_chat_member(chat_id, target_user.id,
            permissions=ChatPermissions(
                can_send_messages=True,
                can_send_media_messages=True,
                can_send_other_messages=True,
                can_add_web_page_previews=True
            ))
        logging.info(f"Размутил {target_user.id} в чате {chat_id}")
    except RPCError as e:
        await message.reply(f"Не смог размутить: {e}")
        return

    del_mute(chat_id, target_user.id)
    await message.reply(f"{target_user.first_name} размучен(а).")
    log_action(target_user.id, "размутил", sender.id, chat_id)
    try:
        chat_link = f"[{message.chat.title}](tg://chat?id={chat_id})"
        await client.send_message(target_user.id, f"Ты размучен(а) в {chat_link}.", parse_mode=ParseMode.MARKDOWN)
    except RPCError:
        pass

# ------------- ХАНДЛЕР ДЛЯ /logs -------------
@app.on_message(filters.command("logs") & filters.group)
async def logs_handler(client, message):
    chat_id = message.chat.id
    sender = message.from_user
    if get_role(chat_id, sender.id) < 2:
        await message.reply("Нельзя: недостаточно прав.")
        return
    args = message.text.split()
    if len(args) < 2:
        await message.reply("Используй: /logs @username")
        return
    try:
        target_user = await client.get_users(args[1])
    except RPCError:
        await message.reply("Не могу найти пользователя.")
        return
    user_logs = get_user_logs(target_user.id)
    if not user_logs:
        await message.reply("У этого пользователя нет записей в логах.")
        return
    text = f"Логи для {args[1]}:"
    for time_ts, action in user_logs:
        t = datetime.fromtimestamp(time_ts, timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
        text += f"{t} — {action}"
    try:
        await client.send_message(sender.id, text)
        await message.reply("Отправил логи в ЛС.")
    except RPCError:
        await message.reply("Не могу отправить ЛС. Напиши боту первым.")

# ------------- ХАНДЛЕР ДЛЯ /clear -------------
@app.on_message(filters.command("clear") & filters.group)
async def clear_handler(client, message):
    chat_id = message.chat.id
    sender = message.from_user
    sender_role = get_role(chat_id, sender.id)
    # теперь доступен всем с ролью >= 1
    if sender_role < 1:
        await message.reply("Нельзя: недостаточно прав.")
        return

    if message.reply_to_message:
        target_user = message.reply_to_message.from_user
    elif len(message.text.split()) >= 2 and message.text.split()[1].startswith("@"):
        try:
            target_user = await client.get_users(message.text.split()[1])
        except RPCError:
            await message.reply("Не могу найти пользователя.")
            return
    else:
        await message.reply("Используй: в ответ на сообщение или /clear @username.")
        return

    target_role = get_role(chat_id, target_user.id)
    # проверяем, что команда не используется против >= допуска
    if sender_role <= target_role:
        if sender_role == target_role:
            await message.reply(f"Нельзя: вы оба {role_to_str(sender_role)}.")
        else:
            await message.reply(f"Нельзя: цель — {role_to_str(target_role)}, а вы — {role_to_str(sender_role)}.")
        return

    current_time = time.time()
    message_id = message.reply_to_message.id if message.reply_to_message else None

    if message_id:
        # Очистка старых запросов
        request_tracker.clean_old_requests("clear", message_id, current_time)

        # Получение текущих запросов (id юзеров)
        requestors = request_tracker.get_requestors("clear", message_id)

        # Если уже есть достаточное количество запросов от разных админов, выполняем команду
        distinct_requestors = set(requestors)
        if len(distinct_requestors) >= 1:  # если есть хотя бы одна запись — это подтверждение
            try:
                await client.ban_chat_member(chat_id, target_user.id)
                await message.reply(f"Пользователь {target_user.first_name} заблокирован и все его сообщения удалены.")
                log_action(target_user.id, "clear (блокировка и удаление сообщений)", sender.id, chat_id)
                return
            except RPCError as e:
                await message.reply(f"Не удалось выполнить операцию: {e}")
                return

        # Добавляем текущий запрос (отправителя)
        request_tracker.add_request("clear", message_id, sender.id, current_time)
        await message.reply("Требуется подтверждение от другого админа с доступом для выполнения этой команды.")
    else:
        await message.reply("Эта команда работает только в ответ на сообщение или через @username.")

# ------------- ХАНДЛЕР ДЛЯ /delete -------------
@app.on_message(filters.command("delete") & filters.group & filters.reply)
async def delete_handler(client, message):
    chat_id = message.chat.id
    sender = message.from_user
    sender_role = get_role(chat_id, sender.id)
    if not message.reply_to_message:
        await message.reply("Эта команда работает только в ответ на сообщение.")
        return
    if sender_role >= 1:
        try:
            await message.reply_to_message.delete()
            await message.reply("Сообщение удалено.")
            log_action(message.reply_to_message.from_user.id, "delete (удаление сообщения)", sender.id, chat_id)
        except RPCError as e:
            await message.reply(f"Не удалось удалить сообщение: {e}")
    else:
        # Для уровня доступа 1 нужно, чтобы два пользователя написали /delete на одно и то же сообщение
        target_message = message.reply_to_message
        target_message_id = target_message.id
        # Проверяем, есть ли уже запрос на удаление этого сообщения
        if not hasattr(client, 'pending_deletes'):
            client.pending_deletes = {}
        if target_message_id in client.pending_deletes:
            # Если уже есть запрос на удаление этого сообщения, удаляем его
            try:
                await target_message.delete()
                await message.reply("Сообщение удалено.")
                del client.pending_deletes[target_message_id]
                log_action(target_message.from_user.id, "delete (удаление сообщения)", sender.id, chat_id)
            except RPCError as e:
                await message.reply(f"Не удалось удалить сообщение: {e}")
        else:
            # Добавляем запрос на удаление
            client.pending_deletes[target_message_id] = True
            await message.reply("Требуется подтверждение от другого пользователя с доступом 1 для удаления этого сообщения.")

# ------------- ХАНДЛЕР ДЛЯ /шлюхобот -------------
@app.on_message(filters.command("шлюхобот") & filters.group)
async def whorebot_handler(client, message):
    chat_id = message.chat.id
    sender = message.from_user
    sender_role = get_role(chat_id, sender.id)
    # теперь доступен всем с ролью >= 1
    if sender_role < 1:
        await message.reply("Нельзя: недостаточно прав.")
        return

    if message.reply_to_message:
        target_user = message.reply_to_message.from_user
    elif len(message.text.split()) >= 2 and message.text.split()[1].startswith("@"):
        try:
            target_user = await client.get_users(message.text.split()[1])
        except RPCError:
            await message.reply("Не могу найти пользователя.")
            return
    else:
        await message.reply("Используй: в ответ на сообщение или /шлюхобот @username.")
        return

    target_role = get_role(chat_id, target_user.id)
    if sender_role <= target_role:
        if sender_role == target_role:
            await message.reply(f"Нельзя: вы оба {role_to_str(sender_role)}.")
        else:
            await message.reply(f"Нельзя: цель — {role_to_str(target_role)}, а вы — {role_to_str(sender_role)}.")
        return

    current_time = time.time()
    message_id = message.reply_to_message.id if message.reply_to_message else None

    if message_id:
        # Очистка старых запросов
        request_tracker.clean_old_requests("whorebot", message_id, current_time)

        # Получение текущих запросов
        requestors = request_tracker.get_requestors("whorebot", message_id)
        distinct_requestors = set(requestors)

        # Если уже есть достаточное количество запросов от разных админов, выполняем команду
        if len(distinct_requestors) >= 1:
            try:
                await client.ban_chat_member(chat_id, target_user.id)
                whore_message = config.get("whore", "Сообщение не найдено в конфигурации.")
                await message.reply(whore_message)
                log_action(target_user.id, "шлюхобот (блокировка и отправка сообщения)", sender.id, chat_id)
                return
            except RPCError as e:
                await message.reply(f"Не удалось выполнить операцию: {e}")
                return

        # Добавляем текущий запрос
        request_tracker.add_request("whorebot", message_id, sender.id, current_time)
        await message.reply("Требуется подтверждение от другого админа с доступом для выполнения этой команды.")
    else:
        await message.reply("Эта команда работает только в ответ на сообщение или через @username.")

# ------------- СТАРТ БОТА -----------
if __name__ == "__main__":
    app.start()
    now_ts = int(time.time())
    # Восстанавливаем незавершённые мьюты
    for chat_id, user_id, unmute_ts in get_all_mutes():
        if unmute_ts <= now_ts:
            asyncio.create_task(schedule_unmute(app, chat_id, user_id, now_ts))
        else:
            asyncio.create_task(schedule_unmute(app, chat_id, user_id, unmute_ts))

    idle()
    app.stop()
