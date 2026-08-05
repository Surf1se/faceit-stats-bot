# FACEIT Stats Bot — Railway

Проект запускает Telegram-бота на Railway 24/7.

## Структура

```text
AppFaceit/
├── bot/
│   └── main.py
├── scripts/
│   └── stats.cpp
├── Dockerfile
├── requirements.txt
├── .env.example
├── .gitignore
└── .dockerignore
```

Windows-файл `stats.exe` в GitHub и Railway загружать не нужно.
Docker сам соберёт Linux-версию программы из `stats.cpp`.

## Проверка локально в Windows

Создай `.env`:

```env
TELEGRAM_BOT_TOKEN=токен_бота
FACEIT_API_KEY=ключ_FACEIT
```

Пересобери локальный файл:

```powershell
C:\msys64\ucrt64\bin\g++.exe scripts\stats.cpp -o scripts\stats.exe -std=c++17 -lcurl
```

Запусти:

```powershell
.\.venv\Scripts\Activate.ps1
python bot\main.py
```

## Публикация в GitHub

В корне проекта:

```powershell
git init
git add .
git commit -m "Deploy FACEIT bot to Railway"
git branch -M main
git remote add origin АДРЕС_РЕПОЗИТОРИЯ
git push -u origin main
```

Файл `.env` не попадёт в GitHub благодаря `.gitignore`.

## Размещение на Railway

1. Создай новый проект Railway.
2. Выбери развёртывание из GitHub-репозитория.
3. Railway обнаружит `Dockerfile` и соберёт контейнер.
4. В Variables добавь:
   - `TELEGRAM_BOT_TOKEN`
   - `FACEIT_API_KEY`
5. Подключи Volume к этому же сервису.
6. Укажи Mount Path:

```text
/app/data
```

После подключения Volume Railway передаст приложению путь через
`RAILWAY_VOLUME_MOUNT_PATH`. База будет храниться в:

```text
/app/data/users.db
```

Отдельный домен и публичный порт боту не нужны, потому что он
работает через Telegram long polling.

## Обновления

После любого `git push` Railway пересоберёт контейнер и перезапустит
бота. База пользователей останется в Volume.

## Важно

Не добавляй настоящие токены и API-ключи в исходный код, `.env.example`
или GitHub.
