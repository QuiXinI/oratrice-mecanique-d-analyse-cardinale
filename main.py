import asyncio
import json
import os
import time
import logging
from datetime import datetime, timezone, timedelta

from dotenv import load_dotenv
from pyrogram import Client, filters
from pyrogram.types import ChatPermissions, User, MessageEntity
from pyrogram.enums import ParseMode
from pyrogram.errors import RPCError

# --------- ПУТЬ К РЕСУРСАМ ---------
RESOURCES_DIR = "resources"
os.makedirs(RESOURCES_DIR, exist_ok=True)

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
    config = json.load(f)

ADMINS_FILE = os.path.join(RESOURCES_DIR, config.get("admins_file", "admins.json"))
MUTES_FILE = os.path.join(RESOURCES_DIR, config.get("mutes_file", "mutes.json"))
LOGS_FILE = os.path.join(RESOURCES_DIR, config.get("logs_file", "logs.json"))
DEFAULT_MUTE_SECONDS = config.get("default_mute_seconds", 600)
LOG_CHAT_ID = config.get("log_chat_id", 0)

# ------------- УТИЛИТЫ ДЛЯ JSON -------------
def load_json(path):
    if not os.path.isfile(path):
        with open(path, "w", encoding="utf-8") as f:
            json.dump({}, f)
    with open(path, "r", encoding="utf-8") as f:
        try:
            return json.load(f)
        except json.JSONDecodeError:
            logging.warning(f"Не удалось прочитать JSON из {path}, создаю пустой.")
            return {}


def save_json(path, data):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)

# ------------- ГЛОБАЛЬНЫЕ СЛОВАРИ -------------
all_admins = load_json(ADMINS_FILE)
mutes = load_json(MUTES_FILE)
logs = load_json(LOGS_FILE)

# ------------- ПОМОЩНИК ДЛЯ ЧАТОВ -------------
def ensure_chat(chat_id: int):
    global all_admins, mutes
    key = str(chat_id)
    if key not in all_admins:
        all_admins[key] = {}
        save_json(ADMINS_FILE, all_admins)
        all_admins = load_json(ADMINS_FILE)
        logging.info(f"Создан раздел админов для чата {chat_id}")
    if key not in mutes:
        mutes[key] = {}
        save_json(MUTES_FILE, mutes)
        mutes = load_json(MUTES_FILE)
        logging.info(f"Создан раздел мутов для чата {chat_id}")

# ------------- ФУНКЦИИ ДЛЯ РОЛЕЙ -------------
def get_role(chat_id: int, user_id: int) -> int:
    ensure_chat(chat_id)
    return all_admins.get(str(chat_id), {}).get(str(user_id), 0)


def role_to_str(role: int) -> str:
    return {1: "Модератор", 2: "Админ", 3: "Владелец", 4: "Его Фуррейшество QuiXinI"}.get(role, "Пользователь")

# ------------- ЛОГИРОВАНИЕ -------------
def log_action(target_id: int, action: str, by_id: int, chat_id: int):
    entry = {
        "time": int(time.time()),
        "action": action,
        "by": by_id,
        "chat_id": chat_id
    }
    user_logs = logs.get(str(target_id), [])
    user_logs.append(entry)
    logs[str(target_id)] = user_logs
    save_json(LOGS_FILE, logs)
    logging.info(f"Записано действие для {target_id}: {action} от {by_id} в чате {chat_id}")

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
    ensure_chat(chat_id)
    chat_mutes = mutes.get(str(chat_id), {})
    if str(user_id) in chat_mutes:
        del chat_mutes[str(user_id)]
        mutes[str(chat_id)] = chat_mutes
        save_json(MUTES_FILE, mutes)
        logging.info(f"Удалён {user_id} из списка мутов чата {chat_link}")
    chat_link = f"[Чатнейм](tg://chat?id={chat_id})"
    try:
        user = await app.get_users(user_id)
        username = user.username

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

# Функция для очистки логов
async def cleanup_logs():
    while True:
        now = time.time()
        backup_logs = {}
        for target_id, user_logs in logs.items():
            updated_logs = [log for log in user_logs if now - log["time"] <= 48 * 3600]
            backup_logs[target_id] = [log for log in user_logs if 48 * 3600 < now - log["time"] <= 96 * 3600]
            logs[target_id] = updated_logs
        save_json(LOGS_FILE, logs)
        save_json(os.path.join(RESOURCES_DIR, "backup_logs.json"), backup_logs)
        await asyncio.sleep(3600)  # Запуск очистки каждый час

# ------------- ИНИЦИАЛИЗАЦИЯ КЛИЕНТА -------------
# Сессия будет храниться в resources/admin_bot.session
session_path = os.path.join(RESOURCES_DIR, "admin_bot")
app = Client(session_path, api_id=API_ID, api_hash=API_HASH, bot_token=BOT_TOKEN)

# ------------- ДИНАМИЧЕСКИЙ /help -------------
@app.on_message(filters.command("help") & filters.group)
async def help_handler(client, message):
    chat_id = message.chat.id
    sender_id = message.from_user.id
    role = get_role(chat_id, sender_id)
    text = "Доступные команды:\n"
    text += "/help — показать список команд\n"
    text += "/report [сообщение] — отправить сообщение модераторам/админам/владельцам\n\n"
    if role >= 1:
        text += "/kick @username [причина] — кикнуть пользователя\n\n"
        text += "/mute @username [время: `число` (минуты) или `число`s\m\h\d\w] — замутить пользователя\n\n"
        text += "/unmute @username — размутить пользователя\n\n"
    if role == 2:
        text += "/promote @username 1 — назначить Модератора\n\n"
        text += "/demote @username — снять роль Модератора\n\n"
    elif role == 3:
        text += "/promote @username [1-3] — назначить Модератора, Админа или Владельца\n\n"
        text += "/demote @username — снять Модератора, Админа или Владельца\n\n"
    elif role == 4:
        text += "/promote @username [1-4] — назначить любую роль\n\n"
        text += "/demote @username — снять любую роль\n\n"
    await message.reply(text)

# ------------- ПРИВЕТСТВИЕ ПРИ ВХОДЕ -------------
@app.on_message(filters.new_chat_members)
async def greet_new_users(client, message):
    for new_user in message.new_chat_members:
        try:
            await client.send_message(new_user.id, f"Ты присоединился к чату '{message.chat.title}'. Добро пожаловать!")
        except RPCError:
            await message.reply(f"Добро пожаловать, {new_user.mention}! Напиши мне в ЛС, чтобы получать уведомления.")

# ------------- ПАРСЕР ВРЕМЕНИ -------------
def parse_duration(text: str) -> int:
    units = {'s': 1, 'm': 60, 'h': 3600, 'd': 86400, 'w': 604800}
    try:
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

    ensure_chat(chat_id)
    chat_admins = all_admins.get(str(chat_id), {})
    mentions = []
    for uid_str, role_int in chat_admins.items():
        if role_int >= 1:
            try:
                user = await client.get_users(int(uid_str))
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

    await message.reply(f"{header}\n{content}\nВнимание: {ping_list}")

# ------------- ХАНДЛЕР ДЛЯ /promote -------------
@app.on_message(filters.command("promote") & filters.group)
async def promote_handler(client, message):
    global all_admins
    chat_id = message.chat.id
    sender = message.from_user
    ensure_chat(chat_id)
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
    all_admins[str(chat_id)][str(target_user.id)] = new_role
    save_json(ADMINS_FILE, all_admins)
    all_admins = load_json(ADMINS_FILE)
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
    global all_admins
    chat_id = message.chat.id
    sender = message.from_user
    ensure_chat(chat_id)
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
    del all_admins[str(chat_id)][str(target_user.id)]
    save_json(ADMINS_FILE, all_admins)
    all_admins = load_json(ADMINS_FILE)
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
    ensure_chat(chat_id)
    args = message.text.split(maxsplit=2)
    if len(args) < 2:
        await message.reply("Используй: /kick @username [причина]")
        return
    try:
        target_user = await client.get_users(args[1])
    except RPCError:
        await message.reply("Не могу найти пользователя.")
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
    chat_link = f"[{message.chat.title}](tg://chat?id={chat_id})"
    if reason:
        reply_text = f"{args[1]} кикнут(а) по причине \"{reason}\""
        user_text = f"Ты кикнут(а) из {chat_link} по причине \"{reason}\""
        log_action(target_user.id, f"кик по причине {reason}", sender.id, chat_id)
    else:
        reply_text = f"{args[1]} кикнут(а)"
        user_text = f"Ты кикнут(а) из {chat_link}"
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
    ensure_chat(chat_id)
    args = message.text.split()
    if len(args) < 2:
        await message.reply("Используй: /mute @username [время]")
        return
    try:
        target_user = await client.get_users(args[1])
    except RPCError:
        await message.reply("Не могу найти пользователя.")
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
    if len(args) >= 3:
        mute_seconds = parse_duration(args[2])
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
    mutes[str(chat_id)][str(target_user.id)] = unmute_ts
    save_json(MUTES_FILE, mutes)
    chat_link = f"[{message.chat.title}](tg://chat?id={chat_id})"
    until_str = until_date_dt.strftime("%Y-%m-%d %H:%M UTC")
    await message.reply(f"{args[1]} замучен(а) до {until_str}.")
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
    ensure_chat(chat_id)
    args = message.text.split()
    if len(args) < 2:
        await message.reply("Используй: /unmute @username")
        return
    try:
        target_user = await client.get_users(args[1])
    except RPCError:
        await message.reply("Не могу найти пользователя.")
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
    chat_mutes = mutes.get(str(chat_id), {})
    if str(target_user.id) in chat_mutes:
        del chat_mutes[str(target_user.id)]
        mutes[str(chat_id)] = chat_mutes
        save_json(MUTES_FILE, mutes)
    await message.reply(f"{args[1]} размучен(а).)")
    log_action(target_user.id, "размутил", sender.id, chat_id)
    chat_link = f"[{message.chat.title}](tg://chat?id={chat_id})"
    try:
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
    user_logs = logs.get(str(target_user.id), [])
    if not user_logs:
        await message.reply("У этого пользователя нет записей в логах.")
        return
    text = f"Логи для {args[1]}:\n"
    for entry in user_logs:
        t = datetime.fromtimestamp(entry["time"], timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
        action = entry["action"]
        text += f"{t} — {action}\n"
    try:
        await client.send_message(sender.id, text)
        await message.reply("Отправил логи в ЛС.")
    except RPCError:
        await message.reply("Не могу отправить ЛС. Напиши боту первым.")

# ------------- СТАРТ БОТА -------------
if __name__ == "__main__":
    now_ts = int(time.time())
    ensure_chat(0)
    for chat_id_str, users in mutes.items():
        for uid_str, unmute_ts in users.items():
            user_id = int(uid_str)
            chat_id = int(chat_id_str)
            if unmute_ts <= now_ts:
                asyncio.get_event_loop().create_task(schedule_unmute(app, chat_id, user_id, now_ts))
            else:
                asyncio.get_event_loop().create_task(schedule_unmute(app, chat_id, user_id, unmute_ts))

    # Запуск задачи очистки логов внутри цикла событий Pyrogram
    async def cleanup_task():
        while True:
            await cleanup_logs()
            await asyncio.sleep(3600*48)  # Ожидание перед следующей очисткой

    app.run()
    app.create_task(cleanup_task())