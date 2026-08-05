import asyncio
import os
import sqlite3
import subprocess
from pathlib import Path

from aiogram import Bot, Dispatcher, F, Router
from aiogram.filters import Command, CommandStart
from aiogram.types import BotCommand, Message
from dotenv import load_dotenv


# Один и тот же код работает:
# - локально в Windows через scripts/stats.exe;
# - на Railway/Linux через scripts/stats.

PROJECT_DIR = Path(__file__).resolve().parent.parent
BOT_DIR = Path(__file__).resolve().parent
ENV_PATH = PROJECT_DIR / ".env"

DEFAULT_STATS_PROGRAM = (
    PROJECT_DIR / "scripts" / "stats.exe"
    if os.name == "nt"
    else PROJECT_DIR / "scripts" / "stats"
)

STATS_PROGRAM = Path(
    os.getenv(
        "STATS_PROGRAM_PATH",
        str(DEFAULT_STATS_PROGRAM),
    )
)

# Railway автоматически передаёт RAILWAY_VOLUME_MOUNT_PATH,
# когда к сервису подключён постоянный Volume.
volume_mount_path = os.getenv(
    "RAILWAY_VOLUME_MOUNT_PATH",
    "",
).strip()

default_database_path = (
    Path(volume_mount_path) / "users.db"
    if volume_mount_path
    else BOT_DIR / "users.db"
)

DATABASE_PATH = Path(
    os.getenv(
        "DATABASE_PATH",
        str(default_database_path),
    )
)

load_dotenv(ENV_PATH)

BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
FACEIT_API_KEY = os.getenv("FACEIT_API_KEY", "").strip()

router = Router()

# Здесь находятся Telegram ID пользователей,
# которым бот сейчас предложил ввести ник.
waiting_for_nickname: set[int] = set()


def init_database() -> None:
    """Создаёт базу пользователей и папку для неё."""
    DATABASE_PATH.parent.mkdir(
        parents=True,
        exist_ok=True,
    )

    with sqlite3.connect(DATABASE_PATH) as connection:
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS users (
                telegram_id INTEGER PRIMARY KEY,
                faceit_nickname TEXT NOT NULL
            )
            """
        )
        connection.commit()


def save_nickname(telegram_id: int, nickname: str) -> None:
    """Сохраняет или обновляет FACEIT-ник пользователя."""
    with sqlite3.connect(DATABASE_PATH) as connection:
        connection.execute(
            """
            INSERT INTO users (telegram_id, faceit_nickname)
            VALUES (?, ?)
            ON CONFLICT(telegram_id)
            DO UPDATE SET faceit_nickname = excluded.faceit_nickname
            """,
            (telegram_id, nickname),
        )
        connection.commit()


def get_nickname(telegram_id: int) -> str | None:
    """Возвращает сохранённый FACEIT-ник."""
    with sqlite3.connect(DATABASE_PATH) as connection:
        row = connection.execute(
            """
            SELECT faceit_nickname
            FROM users
            WHERE telegram_id = ?
            """,
            (telegram_id,),
        ).fetchone()

    return row[0] if row else None


def normalize_nickname(raw_nickname: str) -> str:
    """
    Убирает лишние пробелы.
    FACEIT-ник передаётся в stats.exe отдельным аргументом.
    """
    return raw_nickname.strip()


def validate_nickname(nickname: str) -> str | None:
    """Возвращает текст ошибки или None, если ник подходит."""
    if not nickname:
        return "Ник не может быть пустым."

    if len(nickname) > 64:
        return "Ник получился слишком длинным."

    if any(character.isspace() for character in nickname):
        return "В нике FACEIT не должно быть пробелов."

    if nickname.startswith("/"):
        return "Отправь ник обычным сообщением, без символа /."

    return None


def run_stats_program(nickname: str) -> tuple[bool, str]:
    """
    Запускает C++ программу:
    Windows: scripts/stats.exe <nickname>
    Linux:   scripts/stats <nickname>

    Переменная FACEIT_API_KEY передаётся
    дочернему процессу через окружение.
    """
    if not STATS_PROGRAM.exists():
        return (
            False,
            f"Файл статистики не найден: {STATS_PROGRAM}",
        )

    environment = os.environ.copy()
    environment["FACEIT_API_KEY"] = FACEIT_API_KEY

    creation_flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)

    try:
        result = subprocess.run(
            [str(STATS_PROGRAM), nickname],
            cwd=PROJECT_DIR,
            env=environment,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=90,
            check=False,
            creationflags=creation_flags,
        )
    except subprocess.TimeoutExpired:
        return False, "FACEIT слишком долго отвечает. Попробуй ещё раз позже."
    except OSError as error:
        return False, f"Не удалось запустить программу статистики: {error}"

    stdout = result.stdout.strip()
    stderr = result.stderr.strip()

    if result.returncode != 0:
        error_text = (
            stderr
            or stdout
            or "Неизвестная ошибка программы статистики."
        )
        return False, error_text

    if not stdout:
        return False, "Программа статистики завершилась без результата."

    return True, stdout


@router.message(CommandStart())
async def start_handler(message: Message) -> None:
    if message.from_user is None:
        return

    waiting_for_nickname.add(message.from_user.id)

    current_nickname = get_nickname(message.from_user.id)

    if current_nickname:
        await message.answer(
            f"Сейчас сохранён ник: {current_nickname}\n\n"
            "Отправь новый ник FACEIT, чтобы заменить его."
        )
    else:
        await message.answer(
            "Привет! Отправь мне свой ник на FACEIT."
        )


@router.message(Command("help"))
async def help_handler(message: Message) -> None:
    await message.answer(
        "Доступные команды:\n\n"
        "/start — указать или изменить ник FACEIT\n"
        "/stats — показать статистику последней сессии\n"
        "/help — показать список команд"
    )


@router.message(Command("stats"))
async def stats_handler(message: Message) -> None:
    if message.from_user is None:
        return

    nickname = get_nickname(message.from_user.id)

    if nickname is None:
        waiting_for_nickname.add(message.from_user.id)
        await message.answer(
            "Сначала отправь свой ник FACEIT."
        )
        return

    status_message = await message.answer(
        f"Собираю статистику игрока {nickname}..."
    )

    success, output = await asyncio.to_thread(
        run_stats_program,
        nickname,
    )

    if success:
        await status_message.edit_text(output)
    else:
        await status_message.edit_text(
            "Не удалось получить статистику.\n\n"
            f"{output}"
        )


@router.message(F.text)
async def text_handler(message: Message) -> None:
    if message.from_user is None or message.text is None:
        return

    # Команды обрабатываются отдельными обработчиками.
    if message.text.startswith("/"):
        return

    telegram_id = message.from_user.id

    if telegram_id not in waiting_for_nickname:
        await message.answer(
            "Используй /stats для получения статистики "
            "или /start для изменения ника."
        )
        return

    nickname = normalize_nickname(message.text)
    validation_error = validate_nickname(nickname)

    if validation_error:
        await message.answer(
            f"{validation_error}\n"
            "Попробуй отправить ник ещё раз."
        )
        return

    save_nickname(telegram_id, nickname)
    waiting_for_nickname.discard(telegram_id)

    await message.answer(
        f"Ник {nickname} сохранён.\n"
        "Теперь используй команду /stats."
    )


async def main() -> None:
    if not BOT_TOKEN:
        raise RuntimeError(
            "В файле .env не задан TELEGRAM_BOT_TOKEN."
        )

    if not FACEIT_API_KEY:
        raise RuntimeError(
            "В файле .env не задан FACEIT_API_KEY."
        )

    init_database()

    bot = Bot(token=BOT_TOKEN)
    dispatcher = Dispatcher()
    dispatcher.include_router(router)

    await bot.set_my_commands(
        [
            BotCommand(
                command="start",
                description="Указать ник FACEIT",
            ),
            BotCommand(
                command="stats",
                description="Статистика последней сессии",
            ),
            BotCommand(
                command="help",
                description="Доступные команды",
            ),
        ]
    )

    await dispatcher.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
