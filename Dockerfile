FROM python:3.12-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1
ENV PIP_NO_CACHE_DIR=1

WORKDIR /app

COPY requirements.txt /app/requirements.txt

RUN python -m pip install \
    --no-cache-dir \
    -r /app/requirements.txt

COPY bot /app/bot

CMD ["python", "bot/main.py"]
