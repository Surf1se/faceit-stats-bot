from __future__ import annotations

import json
import os
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any


FACEIT_API_BASE = "https://open.faceit.com/data/v4"
MAX_SESSION_GAP_SECONDS = 8 * 60 * 60
REQUEST_TIMEOUT_SECONDS = 30
MATCH_LIMIT = 100


class FaceitStatsError(RuntimeError):
    """Понятная пользователю ошибка получения статистики FACEIT."""


@dataclass(frozen=True)
class MatchInfo:
    timestamp_seconds: int
    kd: float


def _request_json(url: str, api_key: str) -> dict[str, Any]:
    request = urllib.request.Request(
        url,
        headers={
            "Authorization": f"Bearer {api_key}",
            "Accept": "application/json",
            "User-Agent": "FaceitStatsBot/2.0",
        },
        method="GET",
    )

    try:
        with urllib.request.urlopen(
            request,
            timeout=REQUEST_TIMEOUT_SECONDS,
        ) as response:
            body = response.read().decode(
                "utf-8",
                errors="replace",
            )
            return json.loads(body)

    except urllib.error.HTTPError as error:
        response_body = error.read().decode(
            "utf-8",
            errors="replace",
        )

        if error.code == 404:
            raise FaceitStatsError(
                "Игрок с таким ником не найден."
            ) from error

        if error.code in {401, 403}:
            raise FaceitStatsError(
                "FACEIT отклонил API-ключ. "
                "Проверь переменную FACEIT_API_KEY в Railway."
            ) from error

        short_body = " ".join(response_body.split())[:300]

        raise FaceitStatsError(
            f"FACEIT вернул HTTP-код {error.code}. "
            f"Ответ: {short_body or 'пустой ответ'}"
        ) from error

    except urllib.error.URLError as error:
        reason = getattr(error, "reason", error)

        raise FaceitStatsError(
            f"Не удалось подключиться к FACEIT: {reason}"
        ) from error

    except TimeoutError as error:
        raise FaceitStatsError(
            "FACEIT слишком долго отвечает. "
            "Попробуй ещё раз через минуту."
        ) from error

    except json.JSONDecodeError as error:
        raise FaceitStatsError(
            "FACEIT вернул ответ в неизвестном формате."
        ) from error


def _read_number(
    values: dict[str, Any],
    possible_names: tuple[str, ...],
) -> float | None:
    for name in possible_names:
        value = values.get(name)

        if isinstance(value, bool):
            continue

        if isinstance(value, (int, float)):
            return float(value)

        if isinstance(value, str):
            try:
                return float(value.replace(",", "."))
            except ValueError:
                continue

    return None


def _read_timestamp_seconds(stats: dict[str, Any]) -> int | None:
    raw_timestamp = _read_number(
        stats,
        (
            "Match Finished At",
            "Match Finished at",
            "match_finished_at",
            "Finished At",
        ),
    )

    if raw_timestamp is None:
        return None

    timestamp = int(raw_timestamp)

    # Некоторые источники могут вернуть миллисекунды.
    if timestamp >= 100_000_000_000:
        timestamp //= 1000

    return timestamp


def _parse_matches(response: dict[str, Any]) -> list[MatchInfo]:
    raw_items = response.get("items")

    if not isinstance(raw_items, list):
        raise FaceitStatsError(
            "FACEIT не вернул список матчей."
        )

    matches: list[MatchInfo] = []

    for item in raw_items:
        if not isinstance(item, dict):
            continue

        stats = item.get("stats")

        if not isinstance(stats, dict):
            continue

        kd = _read_number(
            stats,
            (
                "K/D Ratio",
                "K/D",
                "KD Ratio",
            ),
        )
        timestamp_seconds = _read_timestamp_seconds(stats)

        if kd is None or timestamp_seconds is None:
            continue

        matches.append(
            MatchInfo(
                timestamp_seconds=timestamp_seconds,
                kd=kd,
            )
        )

    if not matches:
        raise FaceitStatsError(
            "У игрока не найдены матчи со статистикой."
        )

    return sorted(
        matches,
        key=lambda match: match.timestamp_seconds,
        reverse=True,
    )


def _select_latest_session(
    matches: list[MatchInfo],
) -> list[MatchInfo]:
    session = [matches[0]]

    for older_match in matches[1:]:
        newer_match = session[-1]
        gap_seconds = (
            newer_match.timestamp_seconds
            - older_match.timestamp_seconds
        )

        if gap_seconds > MAX_SESSION_GAP_SECONDS:
            break

        session.append(older_match)

    return session


def _timezone_from_environment() -> timezone:
    raw_offset = os.getenv(
        "FACEIT_TIMEZONE_OFFSET_HOURS",
        "3",
    ).strip()

    try:
        offset_hours = float(raw_offset)
    except ValueError:
        offset_hours = 3.0

    return timezone(timedelta(hours=offset_hours))


def _format_datetime(timestamp_seconds: int) -> str:
    value = datetime.fromtimestamp(
        timestamp_seconds,
        tz=timezone.utc,
    ).astimezone(_timezone_from_environment())

    return value.strftime("%d.%m.%y %H:%M")


def collect_faceit_stats(
    nickname: str,
    api_key: str,
) -> str:
    """
    Получает статистику последней игровой сессии через
    официальный FACEIT Data API.

    Изменение ELO за уже сыгранную сессию не выводится,
    потому что официальный API не возвращает историю
    ELO по каждому матчу.
    """
    clean_nickname = nickname.strip()
    clean_api_key = api_key.strip()

    if not clean_nickname:
        raise FaceitStatsError("Не указан ник FACEIT.")

    if not clean_api_key:
        raise FaceitStatsError(
            "Не задан FACEIT_API_KEY."
        )

    encoded_nickname = urllib.parse.quote(
        clean_nickname,
        safe="",
    )

    player_url = (
        f"{FACEIT_API_BASE}/players"
        f"?nickname={encoded_nickname}"
        "&game=cs2"
    )
    player = _request_json(player_url, clean_api_key)

    player_id = player.get("player_id")

    if not isinstance(player_id, str) or not player_id:
        raise FaceitStatsError(
            "В профиле FACEIT отсутствует player_id."
        )

    games = player.get("games")
    cs2_data = (
        games.get("cs2")
        if isinstance(games, dict)
        else None
    )

    if not isinstance(cs2_data, dict):
        raise FaceitStatsError(
            "У игрока не найден профиль CS2."
        )

    current_elo_raw = cs2_data.get("faceit_elo")

    try:
        current_elo = int(current_elo_raw)
    except (TypeError, ValueError) as error:
        raise FaceitStatsError(
            "FACEIT не вернул текущее ELO игрока."
        ) from error

    stats_url = (
        f"{FACEIT_API_BASE}/players/{player_id}"
        f"/games/cs2/stats?limit={MATCH_LIMIT}"
    )
    stats_response = _request_json(
        stats_url,
        clean_api_key,
    )

    all_matches = _parse_matches(stats_response)
    session_matches = _select_latest_session(all_matches)

    average_kd = (
        sum(match.kd for match in session_matches)
        / len(session_matches)
    )

    newest_match = session_matches[0]
    oldest_match = session_matches[-1]

    return (
        f"Игрок: {clean_nickname}\n"
        "Последняя игровая сессия:\n"
        f"{_format_datetime(oldest_match.timestamp_seconds)}"
        " - "
        f"{_format_datetime(newest_match.timestamp_seconds)}"
        "\n\n"
        f"Матчей: {len(session_matches)}\n"
        f"Средний У/С: {average_kd:.2f}\n"
        f"Текущее ELO: {current_elo}\n"
        "Изменение ELO за сессию: недоступно"
    )
