#include <curl/curl.h>
#include <nlohmann/json.hpp>

#include <algorithm>
#include <cmath>
#include <ctime>
#include <cstdlib>
#include <iomanip>
#include <iostream>
#include <optional>
#include <sstream>
#include <stdexcept>
#include <string>
#include <vector>

#ifdef _WIN32
#include <windows.h>
#endif

using json = nlohmann::json;
using namespace std;


// API-ключ загружается из переменной окружения FACEIT_API_KEY.
string API_KEY;

// Максимальная пауза между матчами:
// 8 часов в миллисекундах
const long long MAX_SESSION_GAP_MS =
    8LL * 60 * 60 * 1000;


struct MatchInfo
{
    long long timestamp;
    double kd;
    string matchId;
    optional<double> eloChange;
};


size_t writeCallback(
    void* data,
    size_t size,
    size_t count,
    void* userData
)
{
    const size_t totalSize = size * count;

    auto* output =
        static_cast<string*>(userData);

    output->append(
        static_cast<char*>(data),
        totalSize
    );

    return totalSize;
}


string urlEncode(const string& value)
{
    CURL* curl = curl_easy_init();

    if (curl == nullptr)
    {
        throw runtime_error(
            "Не удалось запустить CURL."
        );
    }

    char* encoded = curl_easy_escape(
        curl,
        value.c_str(),
        static_cast<int>(value.size())
    );

    if (encoded == nullptr)
    {
        curl_easy_cleanup(curl);

        throw runtime_error(
            "Не удалось обработать ник FACEIT."
        );
    }

    const string result = encoded;

    curl_free(encoded);
    curl_easy_cleanup(curl);

    return result;
}


json getJson(
    const string& url,
    bool useApiKey = true
)
{
    CURL* curl = curl_easy_init();

    if (curl == nullptr)
    {
        throw runtime_error(
            "Не удалось создать HTTP-запрос."
        );
    }

    string responseBody;
    curl_slist* headers = nullptr;

    headers = curl_slist_append(
        headers,
        "Accept: application/json"
    );

    string authorizationHeader;

    if (useApiKey)
    {
        authorizationHeader =
            "Authorization: Bearer " + API_KEY;

        headers = curl_slist_append(
            headers,
            authorizationHeader.c_str()
        );
    }

    curl_easy_setopt(
        curl,
        CURLOPT_URL,
        url.c_str()
    );

    curl_easy_setopt(
        curl,
        CURLOPT_HTTPHEADER,
        headers
    );

    curl_easy_setopt(
        curl,
        CURLOPT_WRITEFUNCTION,
        writeCallback
    );

    curl_easy_setopt(
        curl,
        CURLOPT_WRITEDATA,
        &responseBody
    );

    curl_easy_setopt(
        curl,
        CURLOPT_FOLLOWLOCATION,
        1L
    );

    curl_easy_setopt(
        curl,
        CURLOPT_TIMEOUT,
        30L
    );

    curl_easy_setopt(
        curl,
        CURLOPT_USERAGENT,
        "FaceitStatsBot/1.0"
    );

    const CURLcode requestResult =
        curl_easy_perform(curl);

    long statusCode = 0;

    curl_easy_getinfo(
        curl,
        CURLINFO_RESPONSE_CODE,
        &statusCode
    );

    curl_slist_free_all(headers);
    curl_easy_cleanup(curl);

    if (requestResult != CURLE_OK)
    {
        throw runtime_error(
            string("Ошибка подключения: ") +
            curl_easy_strerror(requestResult)
        );
    }

    if (statusCode != 200)
    {
        throw runtime_error(
            "FACEIT вернул HTTP-код " +
            to_string(statusCode) +
            "\nОтвет: " +
            responseBody
        );
    }

    return json::parse(responseBody);
}


optional<double> getNumber(
    const json& object,
    const vector<string>& possibleNames
)
{
    for (const string& name : possibleNames)
    {
        if (!object.contains(name))
        {
            continue;
        }

        const json& value = object.at(name);

        if (value.is_number())
        {
            return value.get<double>();
        }

        if (value.is_string())
        {
            try
            {
                return stod(
                    value.get<string>()
                );
            }
            catch (...)
            {
                continue;
            }
        }
    }

    return nullopt;
}


string getString(
    const json& object,
    const vector<string>& possibleNames
)
{
    for (const string& name : possibleNames)
    {
        if (!object.contains(name))
        {
            continue;
        }

        const json& value = object.at(name);

        if (value.is_string())
        {
            return value.get<string>();
        }

        if (value.is_number_integer())
        {
            return to_string(
                value.get<long long>()
            );
        }
    }

    return "";
}


long long getTimestamp(const json& stats)
{
    const optional<double> timestampValue =
        getNumber(
            stats,
            {
                "Match Finished At",
                "Match Finished at",
                "match_finished_at",
                "Finished At"
            }
        );

    if (!timestampValue.has_value())
    {
        throw runtime_error(
            "У матча отсутствует время завершения."
        );
    }

    long long timestamp =
        static_cast<long long>(
            timestampValue.value()
        );

    // Если время пришло в секундах,
    // переводим его в миллисекунды
    if (timestamp < 100000000000LL)
    {
        timestamp *= 1000;
    }

    return timestamp;
}


string formatDateTime(
    long long timestampMilliseconds
)
{
    const time_t timestamp =
        static_cast<time_t>(
            timestampMilliseconds / 1000
        );

    tm localTime{};

#ifdef _WIN32
    localtime_s(
        &localTime,
        &timestamp
    );
#else
    localtime_r(
        &timestamp,
        &localTime
    );
#endif

    ostringstream output;

    output << put_time(
        &localTime,
        "%d.%m.%y %H:%M"
    );

    return output.str();
}


optional<int> findPlayerEloInTeam(
    const json& team,
    const string& playerId
)
{
    if (
        !team.contains("players") ||
        !team.at("players").is_array()
    )
    {
        return nullopt;
    }

    for (
        const json& player :
        team.at("players")
    )
    {
        const string currentPlayerId =
            getString(
                player,
                {
                    "player_id",
                    "playerId"
                }
            );

        if (currentPlayerId != playerId)
        {
            continue;
        }

        const optional<double> elo =
            getNumber(
                player,
                {
                    "elo",
                    "faceit_elo",
                    "faceitElo"
                }
            );

        if (elo.has_value())
        {
            return static_cast<int>(
                round(elo.value())
            );
        }
    }

    return nullopt;
}


int findPlayerEloInMatch(
    const json& matchData,
    const string& playerId
)
{
    const json* match = &matchData;

    if (matchData.is_array())
    {
        if (matchData.empty())
        {
            throw runtime_error(
                "Получен пустой ответ по матчу."
            );
        }

        match = &matchData.at(0);
    }

    if (!match->contains("teams"))
    {
        throw runtime_error(
            "В данных матча отсутствуют команды."
        );
    }

    const json& teams =
        match->at("teams");

    if (teams.is_array())
    {
        for (const json& team : teams)
        {
            const optional<int> elo =
                findPlayerEloInTeam(
                    team,
                    playerId
                );

            if (elo.has_value())
            {
                return elo.value();
            }
        }
    }
    else if (teams.is_object())
    {
        for (const auto& item : teams.items())
        {
            const optional<int> elo =
                findPlayerEloInTeam(
                    item.value(),
                    playerId
                );

            if (elo.has_value())
            {
                return elo.value();
            }
        }
    }

    throw runtime_error(
        "Не удалось найти ELO игрока в матче."
    );
}


int main(int argc, char* argv[])
{
#ifdef _WIN32
    SetConsoleOutputCP(CP_UTF8);
    SetConsoleCP(CP_UTF8);
#endif

    if (argc < 2)
    {
        cerr
            << "Ошибка: передай ник FACEIT первым аргументом.\n"
            << "Пример: stats.exe Surf1se"
            << endl;

        return 1;
    }

    const string faceitNickname = argv[1];

    const char* apiKeyFromEnvironment =
        getenv("FACEIT_API_KEY");

    if (
        apiKeyFromEnvironment == nullptr ||
        string(apiKeyFromEnvironment).empty()
    )
    {
        cerr
            << "Ошибка: не задана переменная "
            << "окружения FACEIT_API_KEY."
            << endl;

        return 1;
    }

    API_KEY = apiKeyFromEnvironment;

    const CURLcode curlInitResult =
        curl_global_init(
            CURL_GLOBAL_DEFAULT
        );

    if (curlInitResult != CURLE_OK)
    {
        cerr
            << "Не удалось инициализировать CURL."
            << endl;

        return 1;
    }

    int exitCode = 0;

    try
    {
        // Получаем профиль игрока
        const string playerUrl =
            "https://open.faceit.com/data/v4/players"
            "?nickname=" +
            urlEncode(faceitNickname) +
            "&game=cs2";

        const json player =
            getJson(playerUrl);

        const string playerId =
            player.at("player_id")
                  .get<string>();

        const int currentElo =
            player.at("games")
                  .at("cs2")
                  .at("faceit_elo")
                  .get<int>();

        // Получаем последние 100 матчей
        const string statsUrl =
            "https://open.faceit.com/data/v4/players/" +
            playerId +
            "/games/cs2/stats?limit=100";

        const json response =
            getJson(statsUrl);

        if (
            !response.contains("items") ||
            !response.at("items").is_array() ||
            response.at("items").empty()
        )
        {
            throw runtime_error(
                "У игрока не найдены матчи."
            );
        }

        vector<MatchInfo> allMatches;

        for (
            const json& match :
            response.at("items")
        )
        {
            if (!match.contains("stats"))
            {
                continue;
            }

            const json& stats =
                match.at("stats");

            const optional<double> kd =
                getNumber(
                    stats,
                    {
                        "K/D Ratio",
                        "K/D",
                        "KD Ratio"
                    }
                );

            if (!kd.has_value())
            {
                continue;
            }

            const long long timestamp =
                getTimestamp(stats);

            const string matchId =
                getString(
                    stats,
                    {
                        "Match Id",
                        "Match ID",
                        "match_id"
                    }
                );

            const optional<double> eloChange =
                getNumber(
                    stats,
                    {
                        "Elo Change",
                        "ELO Change",
                        "Elo Difference",
                        "ELO Difference"
                    }
                );

            allMatches.push_back(
                {
                    timestamp,
                    kd.value(),
                    matchId,
                    eloChange
                }
            );
        }

        if (allMatches.empty())
        {
            throw runtime_error(
                "Не удалось получить статистику матчей."
            );
        }

        // Сортируем матчи:
        // от самого нового к самому старому
        sort(
            allMatches.begin(),
            allMatches.end(),
            [](
                const MatchInfo& first,
                const MatchInfo& second
            )
            {
                return first.timestamp >
                       second.timestamp;
            }
        );

        vector<MatchInfo> sessionMatches;

        // Последний матч всегда входит
        // в последнюю игровую сессию
        sessionMatches.push_back(
            allMatches.front()
        );

        // Добавляем более старые матчи,
        // пока пауза не превысит 8 часов
        for (
            size_t index = 1;
            index < allMatches.size();
            index++
        )
        {
            const MatchInfo& newerMatch =
                sessionMatches.back();

            const MatchInfo& olderMatch =
                allMatches.at(index);

            const long long gap =
                newerMatch.timestamp -
                olderMatch.timestamp;

            if (gap > MAX_SESSION_GAP_MS)
            {
                break;
            }

            sessionMatches.push_back(
                olderMatch
            );
        }

        double kdSum = 0.0;
        double eloChangeSum = 0.0;

        bool everyMatchHasEloChange = true;

        for (
            const MatchInfo& match :
            sessionMatches
        )
        {
            kdSum += match.kd;

            if (match.eloChange.has_value())
            {
                eloChangeSum +=
                    match.eloChange.value();
            }
            else
            {
                everyMatchHasEloChange = false;
            }
        }

        const double averageKd =
            kdSum /
            static_cast<double>(
                sessionMatches.size()
            );

        int totalEloChange = 0;

        if (everyMatchHasEloChange)
        {
            totalEloChange =
                static_cast<int>(
                    round(eloChangeSum)
                );
        }
        else
        {
            /*
             * Если FACEIT не вернул изменение ELO
             * для каждого матча, считаем изменение
             * по рейтингу до начала сессии.
             *
             * Матч сразу перед сессией хранит ELO,
             * которое было у игрока перед самым
             * старым матчем выбранной сессии.
             * Поэтому стрелка самого старого матча
             * теперь тоже входит в итог.
             */
            optional<int> startingElo;

            const size_t matchBeforeSessionIndex =
                sessionMatches.size();

            if (
                matchBeforeSessionIndex <
                allMatches.size()
            )
            {
                const MatchInfo& matchBeforeSession =
                    allMatches.at(
                        matchBeforeSessionIndex
                    );

                if (!matchBeforeSession.matchId.empty())
                {
                    const string matchUrl =
                        "https://www.faceit.com/api/stats/v3/matches/" +
                        matchBeforeSession.matchId;

                    const json matchData =
                        getJson(
                            matchUrl,
                            false
                        );

                    startingElo =
                        findPlayerEloInMatch(
                            matchData,
                            playerId
                        );
                }
            }

            /*
             * Резервный вариант для случая, когда
             * среди загруженных матчей нет матча
             * перед сессией, но известна стрелка
             * самого старого матча.
             */
            if (!startingElo.has_value())
            {
                const MatchInfo& oldestSessionMatch =
                    sessionMatches.back();

                if (
                    oldestSessionMatch.matchId.empty() ||
                    !oldestSessionMatch.eloChange.has_value()
                )
                {
                    throw runtime_error(
                        "Не удалось определить ELO "
                        "до первого матча сессии."
                    );
                }

                const string matchUrl =
                    "https://www.faceit.com/api/stats/v3/matches/" +
                    oldestSessionMatch.matchId;

                const json matchData =
                    getJson(
                        matchUrl,
                        false
                    );

                const int eloAfterOldestMatch =
                    findPlayerEloInMatch(
                        matchData,
                        playerId
                    );

                startingElo =
                    eloAfterOldestMatch -
                    static_cast<int>(
                        round(
                            oldestSessionMatch
                                .eloChange
                                .value()
                        )
                    );
            }

            totalEloChange =
                currentElo -
                startingElo.value();
        }

        const MatchInfo& newestMatch =
            sessionMatches.front();

        const MatchInfo& oldestMatch =
            sessionMatches.back();

        cout
            << "Игрок: "
            << faceitNickname
            << "\n"
            << "Последняя игровая сессия:\n"
            << formatDateTime(
                oldestMatch.timestamp
            )
            << " - "
            << formatDateTime(
                newestMatch.timestamp
            )
            << "\n\n";

        cout
            << "Матчей: "
            << sessionMatches.size()
            << '\n';

        cout
            << "Средний У/С: "
            << fixed
            << setprecision(2)
            << averageKd
            << '\n';

        cout
            << "Изменение ELO: ";

        if (totalEloChange > 0)
        {
            cout << "+";
        }

        cout
            << totalEloChange
            << endl;
    }
    catch (const json::exception& error)
    {
        cerr
            << "Ошибка обработки данных FACEIT: "
            << error.what()
            << endl;

        exitCode = 1;
    }
    catch (const exception& error)
    {
        cerr
            << "Ошибка: "
            << error.what()
            << endl;

        exitCode = 1;
    }

    curl_global_cleanup();

    return exitCode;
}