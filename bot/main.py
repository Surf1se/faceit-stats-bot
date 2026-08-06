import asyncio
import os
import sqlite3
from pathlib import Path

from aiogram import Bot, Dispatcher, F, Router
from aiogram.filters import Command, CommandStart
from aiogram.types import BotCommand, Message
from dotenv import load_dotenv

from faceit_stats import FaceitStatsError, collect_faceit_stats


PROJECT_DIR = Path(__file__).resolve().parent.parent
BOT_DIR = Path(__file__).resolve().parent
ENV_PATH = PROJECT_DIR / ".env"

# Сначала загружаем локальный .env.
# На Railway переменные уже находятся в окружении.
load_dotenv(ENV_PATH)

BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
FACEIT_API_KEY = os.getenv("FACEIT_API_KEY", "").strip()

# Railway создаёт эту переменную после подключения Volume.
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

router = Router()

# Telegram ID пользователей, у которых бот ожидает ник.
waiting_for_nickname: set[int] = set()


def init_database() -> None:
    """Создаёт папку и таблицу пользователей."""
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
    """Сохраняет или обновляет FACEIT-ник."""
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
    return raw_nickname.strip()


def validate_nickname(nickname: str) -> str | None:
    if not nickname:
        return "Ник не может быть пустым."

    if len(nickname) > 64:
        return "Ник получился слишком длинным."

    if any(character.isspace() for character in nickname):
        return "В нике FACEIT не должно быть пробелов."

    if nickname.startswith("/"):
        return "Отправь ник обычным сообщением, без символа /."

    return None


def limit_error_text(text: str, limit: int = 1500) -> str:
    """Не даёт огромному ответу API сломать сообщение Telegram."""
    normalized = " ".join(text.split())

    if len(normalized) <= limit:
        return normalized

    return normalized[:limit] + "…"


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

    try:
        output = await asyncio.wait_for(
            asyncio.to_thread(
                collect_faceit_stats,
                nickname,
                FACEIT_API_KEY,
            ),
            timeout=75,
        )
    except FaceitStatsError as error:
        await status_message.edit_text(
            "Не удалось получить статистику.\n\n"
            + limit_error_text(str(error))
        )
    except asyncio.TimeoutError:
        await status_message.edit_text(
            "FACEIT слишком долго отвечает. "
            "Попробуй ещё раз через минуту."
        )
    except Exception as error:
        await status_message.edit_text(
            "Произошла непредвиденная ошибка.\n\n"
            + limit_error_text(str(error))
        )
    else:
        await status_message.edit_text(output)


@router.message(F.text)
async def text_handler(message: Message) -> None:
    if message.from_user is None or message.text is None:
        return

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
            "Не задан TELEGRAM_BOT_TOKEN."
        )

    if not FACEIT_API_KEY:
        raise RuntimeError(
            "Не задан FACEIT_API_KEY."
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
