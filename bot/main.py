import asyncio
import logging
import os
import sqlite3
import time
from pathlib import Path

from aiogram import Bot, Dispatcher, F, Router
from aiogram.filters import Command, CommandStart
from aiogram.types import BotCommand, Message
from dotenv import load_dotenv

from faceit_stats import (
    FaceitStatsError,
    SessionStats,
    collect_faceit_session,
    format_session_stats,
)


PROJECT_DIR = Path(__file__).resolve().parent.parent
BOT_DIR = Path(__file__).resolve().parent
ENV_PATH = PROJECT_DIR / ".env"

load_dotenv(ENV_PATH)

BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
FACEIT_API_KEY = os.getenv("FACEIT_API_KEY", "").strip()

try:
    ELO_TRACKING_INTERVAL_SECONDS = max(
        60,
        int(
            os.getenv(
                "ELO_TRACKING_INTERVAL_SECONDS",
                "900",
            )
        ),
    )
except ValueError:
    ELO_TRACKING_INTERVAL_SECONDS = 900

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

logging.basicConfig(
    level=logging.INFO,
    format=(
        "%(asctime)s | %(levelname)s | "
        "%(name)s | %(message)s"
    ),
)

logger = logging.getLogger("faceit-bot")
router = Router()

waiting_for_nickname: set[int] = set()


def open_database() -> sqlite3.Connection:
    connection = sqlite3.connect(
        DATABASE_PATH,
        timeout=30,
    )
    connection.row_factory = sqlite3.Row
    return connection


def init_database() -> None:
    """Создаёт базу и таблицы пользователей, ELO и сессий."""
    DATABASE_PATH.parent.mkdir(
        parents=True,
        exist_ok=True,
    )

    with open_database() as connection:
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS users (
                telegram_id INTEGER PRIMARY KEY,
                faceit_nickname TEXT NOT NULL
            )
            """
        )

        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS elo_snapshots (
                telegram_id INTEGER NOT NULL,
                faceit_nickname TEXT NOT NULL,
                match_id TEXT NOT NULL,
                match_finished_at INTEGER NOT NULL,
                elo INTEGER NOT NULL,
                recorded_at INTEGER NOT NULL,
                PRIMARY KEY (telegram_id, match_id)
            )
            """
        )

        connection.execute(
            """
            CREATE INDEX IF NOT EXISTS
                idx_elo_snapshots_user_time
            ON elo_snapshots (
                telegram_id,
                match_finished_at
            )
            """
        )

        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS session_results (
                telegram_id INTEGER NOT NULL,
                faceit_nickname TEXT NOT NULL,
                session_start_match_id TEXT NOT NULL,
                session_end_match_id TEXT NOT NULL,
                session_started_at INTEGER NOT NULL,
                session_ended_at INTEGER NOT NULL,
                elo_change INTEGER NOT NULL,
                match_count INTEGER NOT NULL,
                updated_at INTEGER NOT NULL,
                PRIMARY KEY (
                    telegram_id,
                    session_start_match_id
                )
            )
            """
        )

        connection.execute(
            """
            CREATE INDEX IF NOT EXISTS
                idx_session_results_leaderboard
            ON session_results (
                elo_change DESC,
                session_ended_at DESC
            )
            """
        )

        connection.commit()


def save_nickname(telegram_id: int, nickname: str) -> None:
    """
    Сохраняет ник. Если пользователь сменил ник,
    старые снимки ELO очищаются.
    """
    with open_database() as connection:
        current_row = connection.execute(
            """
            SELECT faceit_nickname
            FROM users
            WHERE telegram_id = ?
            """,
            (telegram_id,),
        ).fetchone()

        current_nickname = (
            current_row["faceit_nickname"]
            if current_row
            else None
        )

        connection.execute(
            """
            INSERT INTO users (
                telegram_id,
                faceit_nickname
            )
            VALUES (?, ?)
            ON CONFLICT(telegram_id)
            DO UPDATE SET
                faceit_nickname = excluded.faceit_nickname
            """,
            (telegram_id, nickname),
        )

        if (
            current_nickname is not None
            and current_nickname.casefold()
            != nickname.casefold()
        ):
            connection.execute(
                """
                DELETE FROM elo_snapshots
                WHERE telegram_id = ?
                """,
                (telegram_id,),
            )

            connection.execute(
                """
                DELETE FROM session_results
                WHERE telegram_id = ?
                """,
                (telegram_id,),
            )

        connection.commit()


def get_nickname(telegram_id: int) -> str | None:
    with open_database() as connection:
        row = connection.execute(
            """
            SELECT faceit_nickname
            FROM users
            WHERE telegram_id = ?
            """,
            (telegram_id,),
        ).fetchone()

    return row["faceit_nickname"] if row else None


def get_all_users() -> list[tuple[int, str]]:
    with open_database() as connection:
        rows = connection.execute(
            """
            SELECT telegram_id, faceit_nickname
            FROM users
            ORDER BY telegram_id
            """
        ).fetchall()

    return [
        (
            int(row["telegram_id"]),
            str(row["faceit_nickname"]),
        )
        for row in rows
    ]


def save_latest_elo_snapshot(
    telegram_id: int,
    session: SessionStats,
) -> None:
    """
    Связывает текущее ELO с последним завершённым матчем.
    Повторная проверка обновит значение, если FACEIT
    применил изменение рейтинга с небольшой задержкой.
    """
    latest_match = session.newest_match

    with open_database() as connection:
        connection.execute(
            """
            INSERT INTO elo_snapshots (
                telegram_id,
                faceit_nickname,
                match_id,
                match_finished_at,
                elo,
                recorded_at
            )
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(telegram_id, match_id)
            DO UPDATE SET
                faceit_nickname = excluded.faceit_nickname,
                match_finished_at = excluded.match_finished_at,
                elo = excluded.elo,
                recorded_at = excluded.recorded_at
            """,
            (
                telegram_id,
                session.nickname,
                latest_match.match_id,
                latest_match.timestamp_seconds,
                session.current_elo,
                int(time.time()),
            ),
        )
        connection.commit()


def get_snapshot_elo(
    telegram_id: int,
    match_id: str,
) -> int | None:
    with open_database() as connection:
        row = connection.execute(
            """
            SELECT elo
            FROM elo_snapshots
            WHERE telegram_id = ?
              AND match_id = ?
            """,
            (telegram_id, match_id),
        ).fetchone()

    return int(row["elo"]) if row else None


def calculate_session_elo_change(
    telegram_id: int,
    session: SessionStats,
) -> int | None:
    """
    Сравнивает текущее ELO с ELO после матча,
    который был непосредственно перед новой сессией.
    """
    previous_match = session.previous_match

    if previous_match is None:
        return None

    previous_elo = get_snapshot_elo(
        telegram_id,
        previous_match.match_id,
    )

    if previous_elo is None:
        return None

    return session.current_elo - previous_elo


def save_session_result(
    telegram_id: int,
    session: SessionStats,
    elo_change: int | None,
) -> None:
    """
    Сохраняет текущий итог игровой сессии.
    Пока сессия продолжается, одна и та же запись обновляется.
    """
    if elo_change is None:
        return

    with open_database() as connection:
        connection.execute(
            """
            INSERT INTO session_results (
                telegram_id,
                faceit_nickname,
                session_start_match_id,
                session_end_match_id,
                session_started_at,
                session_ended_at,
                elo_change,
                match_count,
                updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(
                telegram_id,
                session_start_match_id
            )
            DO UPDATE SET
                faceit_nickname = excluded.faceit_nickname,
                session_end_match_id = excluded.session_end_match_id,
                session_ended_at = excluded.session_ended_at,
                elo_change = excluded.elo_change,
                match_count = excluded.match_count,
                updated_at = excluded.updated_at
            """,
            (
                telegram_id,
                session.nickname,
                session.oldest_match.match_id,
                session.newest_match.match_id,
                session.oldest_match.timestamp_seconds,
                session.newest_match.timestamp_seconds,
                elo_change,
                len(session.session_matches),
                int(time.time()),
            ),
        )
        connection.commit()


def get_leaderboard(
    limit: int = 10,
) -> list[tuple[str, int, int]]:
    """
    Возвращает лучшую положительную сессию каждого игрока.
    Формат: (nickname, elo_change, match_count).
    """
    with open_database() as connection:
        rows = connection.execute(
            """
            SELECT
                telegram_id,
                faceit_nickname,
                elo_change,
                match_count,
                session_ended_at
            FROM session_results
            WHERE elo_change > 0
            ORDER BY
                elo_change DESC,
                session_ended_at DESC
            """
        ).fetchall()

    leaderboard: list[tuple[str, int, int]] = []
    seen_users: set[int] = set()

    for row in rows:
        telegram_id = int(row["telegram_id"])

        if telegram_id in seen_users:
            continue

        seen_users.add(telegram_id)
        leaderboard.append(
            (
                str(row["faceit_nickname"]),
                int(row["elo_change"]),
                int(row["match_count"]),
            )
        )

        if len(leaderboard) >= limit:
            break

    return leaderboard


def match_word(count: int) -> str:
    if 11 <= count % 100 <= 14:
        return "матчей"

    last_digit = count % 10

    if last_digit == 1:
        return "матч"

    if 2 <= last_digit <= 4:
        return "матча"

    return "матчей"


def format_leaderboard(
    rows: list[tuple[str, int, int]],
) -> str:
    if not rows:
        return (
            "🏆 Лидерборд лучших сессий\n\n"
            "Пока нет сессий с положительным "
            "изменением ELO."
        )

    medals = ["🥇", "🥈", "🥉"]
    lines = ["🏆 Лидерборд лучших сессий", ""]

    for position, (nickname, elo_change, match_count) in enumerate(
        rows,
        start=1,
    ):
        place = (
            medals[position - 1]
            if position <= len(medals)
            else f"{position}."
        )

        lines.append(f"{place} {nickname}")
        lines.append(
            f"+{elo_change} ELO 🟢 | "
            f"{match_count} {match_word(match_count)}"
        )

        if position != len(rows):
            lines.append("")

    return "\n".join(lines)


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
    normalized = " ".join(text.split())

    if len(normalized) <= limit:
        return normalized

    return normalized[:limit] + "…"


async def fetch_and_store_session(
    telegram_id: int,
    nickname: str,
) -> SessionStats:
    session = await asyncio.to_thread(
        collect_faceit_session,
        nickname,
        FACEIT_API_KEY,
    )

    await asyncio.to_thread(
        save_latest_elo_snapshot,
        telegram_id,
        session,
    )

    return session


async def track_single_user(
    telegram_id: int,
    nickname: str,
) -> None:
    try:
        session = await asyncio.wait_for(
            fetch_and_store_session(
                telegram_id,
                nickname,
            ),
            timeout=75,
        )
    except Exception as error:
        logger.warning(
            "Не удалось обновить ELO для %s: %s",
            nickname,
            limit_error_text(str(error), 300),
        )
        return

    elo_change = await asyncio.to_thread(
        calculate_session_elo_change,
        telegram_id,
        session,
    )

    await asyncio.to_thread(
        save_session_result,
        telegram_id,
        session,
        elo_change,
    )

    logger.info(
        "ELO зафиксировано: user=%s nickname=%s "
        "match=%s elo=%s",
        telegram_id,
        nickname,
        session.newest_match.match_id,
        session.current_elo,
    )


async def elo_tracking_loop() -> None:
    """
    Раз в 15 минут проверяет всех сохранённых игроков.
    Интервал можно изменить переменной
    ELO_TRACKING_INTERVAL_SECONDS.
    """
    await asyncio.sleep(5)

    while True:
        cycle_started_at = asyncio.get_running_loop().time()
        users = await asyncio.to_thread(get_all_users)

        logger.info(
            "Запуск фоновой проверки ELO. Пользователей: %s",
            len(users),
        )

        for telegram_id, nickname in users:
            await track_single_user(
                telegram_id,
                nickname,
            )

            # Небольшая пауза между игроками,
            # чтобы не отправлять API-запросы одновременно.
            await asyncio.sleep(1)

        elapsed = (
            asyncio.get_running_loop().time()
            - cycle_started_at
        )
        sleep_seconds = max(
            30,
            ELO_TRACKING_INTERVAL_SECONDS - elapsed,
        )

        await asyncio.sleep(sleep_seconds)


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
        "/leaderboard — топ-10 лучших сессий по ELO\n"
        "/help — показать список команд"
    )


@router.message(Command("stats"))
async def stats_handler(message: Message) -> None:
    if message.from_user is None:
        return

    telegram_id = message.from_user.id
    nickname = get_nickname(telegram_id)

    if nickname is None:
        waiting_for_nickname.add(telegram_id)
        await message.answer(
            "Сначала отправь свой ник FACEIT."
        )
        return

    status_message = await message.answer(
        f"Собираю статистику игрока {nickname}..."
    )

    try:
        session = await asyncio.wait_for(
            fetch_and_store_session(
                telegram_id,
                nickname,
            ),
            timeout=75,
        )

        elo_change = await asyncio.to_thread(
            calculate_session_elo_change,
            telegram_id,
            session,
        )

        await asyncio.to_thread(
            save_session_result,
            telegram_id,
            session,
            elo_change,
        )

        output = format_session_stats(
            session,
            elo_change,
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
        logger.exception(
            "Ошибка команды /stats для %s",
            nickname,
        )
        await status_message.edit_text(
            "Произошла непредвиденная ошибка.\n\n"
            + limit_error_text(str(error))
        )
    else:
        await status_message.edit_text(output)


@router.message(Command("leaderboard"))
async def leaderboard_handler(message: Message) -> None:
    rows = await asyncio.to_thread(
        get_leaderboard,
        10,
    )

    await message.answer(
        format_leaderboard(rows)
    )


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
        "Отслеживание ELO включено.\n"
        "Теперь используй команду /stats."
    )

    # Не заставляем пользователя ждать ответа FACEIT.
    asyncio.create_task(
        track_single_user(
            telegram_id,
            nickname,
        )
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
                command="leaderboard",
                description="Топ-10 лучших сессий по ELO",
            ),
            BotCommand(
                command="help",
                description="Доступные команды",
            ),
        ]
    )

    tracking_task = asyncio.create_task(
        elo_tracking_loop()
    )

    try:
        await dispatcher.start_polling(bot)
    finally:
        tracking_task.cancel()

        try:
            await tracking_task
        except asyncio.CancelledError:
            pass


if __name__ == "__main__":
    asyncio.run(main())
