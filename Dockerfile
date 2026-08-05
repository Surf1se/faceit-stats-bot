FROM python:3.12-slim-bookworm AS cpp-builder

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        g++ \
        libcurl4-openssl-dev \
        nlohmann-json3-dev \
        ca-certificates \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY scripts/stats.cpp /app/scripts/stats.cpp

RUN g++ /app/scripts/stats.cpp \
    -o /app/scripts/stats \
    -std=c++17 \
    -O2 \
    -lcurl


FROM python:3.12-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1
ENV PIP_NO_CACHE_DIR=1

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        libcurl4 \
        libstdc++6 \
        ca-certificates \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt /app/requirements.txt

RUN python -m pip install \
    --no-cache-dir \
    -r /app/requirements.txt

COPY bot /app/bot
COPY --from=cpp-builder /app/scripts/stats /app/scripts/stats

RUN chmod +x /app/scripts/stats

CMD ["python", "bot/main.py"]
