import os
import json
import sqlite3

# --------- Настройки путей ---------
RESOURCES_DIR = "resources"
DB_PATH = os.path.join(RESOURCES_DIR, "bot.db")
ADMINS_JSON = os.path.join(RESOURCES_DIR, "admins.json")
MUTES_JSON = os.path.join(RESOURCES_DIR, "mutes.json")
LOGS_JSON = os.path.join(RESOURCES_DIR, "logs.json")

# --------- Подключаемся к базе ---------
def init_db_connection(path):
    if not os.path.isfile(path):
        raise FileNotFoundError(f"Не найдена база данных: {path}")
    conn = sqlite3.connect(path)
    return conn

conn = init_db_connection(DB_PATH)
cursor = conn.cursor()

# --------- Функции для миграции ---------

def migrate_admins(json_path):
    if not os.path.isfile(json_path):
        print(f"Файл не найден: {json_path}, пропускаем admins.")
        return
    with open(json_path, 'r', encoding='utf-8') as f:
        data = json.load(f)
    count = 0
    # Ожидаем структуру: {chat_id: {user_id: role, ...}, ...}
    for chat_id_str, users in data.items():
        try:
            chat_id = int(chat_id_str)
        except ValueError:
            print(f"Пропускаем неверный chat_id: {chat_id_str}")
            continue
        if not isinstance(users, dict):
            continue
        for user_id_str, role in users.items():
            try:
                user_id = int(user_id_str)
                role_int = int(role)
            except ValueError:
                print(f"Пропускаем неверный user или роль: {user_id_str}, {role}")
                continue
            cursor.execute(
                "INSERT OR REPLACE INTO admins (chat_id, user_id, role) VALUES (?, ?, ?)",
                (chat_id, user_id, role_int)
            )
            count += 1
    conn.commit()
    print(f"Мигрировано админов: {count}")


def migrate_mutes(json_path):
    if not os.path.isfile(json_path):
        print(f"Файл не найден: {json_path}, пропускаем mutes.")
        return
    with open(json_path, 'r', encoding='utf-8') as f:
        data = json.load(f)
    count = 0
    # Ожидаем структуру: {chat_id: {user_id: unmute_ts, ...}, ...}
    for chat_id_str, users in data.items():
        try:
            chat_id = int(chat_id_str)
        except ValueError:
            print(f"Пропускаем неверный chat_id: {chat_id_str}")
            continue
        if not isinstance(users, dict):
            continue
        for user_id_str, unmute_ts in users.items():
            try:
                user_id = int(user_id_str)
                unmute_ts_int = int(unmute_ts)
            except ValueError:
                print(f"Пропускаем неверный user или unmute_ts: {user_id_str}, {unmute_ts}")
                continue
            cursor.execute(
                "INSERT OR REPLACE INTO mutes (chat_id, user_id, unmute_ts) VALUES (?, ?, ?)",
                (chat_id, user_id, unmute_ts_int)
            )
            count += 1
    conn.commit()
    print(f"Мигрировано мутов: {count}")


def migrate_logs(json_path):
    if not os.path.isfile(json_path):
        print(f"Файл не найден: {json_path}, пропускаем logs.")
        return
    with open(json_path, 'r', encoding='utf-8') as f:
        data = json.load(f)
    count = 0
    # Ожидаем структуру: {target_id: [{"time": ts, "action": str, "by": by_id, "chat_id": chat_id}, ...], ...}
    for target_id_str, entries in data.items():
        try:
            target_id = int(target_id_str)
        except ValueError:
            print(f"Пропускаем неверный target_id: {target_id_str}")
            continue
        if not isinstance(entries, list):
            continue
        for entry in entries:
            time_ts = entry.get("time")
            action = entry.get("action")
            by_id = entry.get("by")
            chat_id = entry.get("chat_id")
            if time_ts is None or action is None or by_id is None or chat_id is None:
                print(f"Пропускаем неполную запись логов: {entry}")
                continue
            try:
                time_int = int(time_ts)
                by_int = int(by_id)
                chat_int = int(chat_id)
            except ValueError:
                print(f"Пропускаем неверные типы в entry: {entry}")
                continue
            cursor.execute(
                "INSERT INTO logs (target_id, time_ts, action, by_id, chat_id) VALUES (?, ?, ?, ?, ?)",
                (target_id, time_int, action, by_int, chat_int)
            )
            count += 1
    conn.commit()
    print(f"Мигрировано записей логов: {count}")

# --------- Основная логика ---------
def main():
    print("Начинаем миграцию из JSON -> SQLite...")
    migrate_admins(ADMINS_JSON)
    migrate_mutes(MUTES_JSON)
    migrate_logs(LOGS_JSON)
    print("Миграция завершена.")

if __name__ == "__main__":
    main()
