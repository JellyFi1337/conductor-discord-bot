FROM python:3.12-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_ROOT_USER_ACTION=ignore \
    DATA_DIR=/app/data

WORKDIR /app

RUN apt-get update \
    && apt-get install -y --no-install-recommends gosu \
    && rm -rf /var/lib/apt/lists/* \
    && useradd --create-home --uid 1000 --shell /usr/sbin/nologin conductor

COPY pyproject.toml README.md alembic.ini ./
COPY src ./src

RUN pip install --no-cache-dir ".[postgres]" \
    && mkdir -p /app/data \
    && chown -R conductor:conductor /app \
    && python -c "import conductor, asyncpg; print(conductor.__version__)"

# Эталоны конфигов лежат вне /app/data: том хоста их не перекрывает,
# и entrypoint может положить automod.yml в пустой каталог данных.
COPY data/automod.yml.example data/permissions.yml.example /app/defaults/

COPY docker/entrypoint.sh /usr/local/bin/entrypoint.sh
RUN chmod 0755 /usr/local/bin/entrypoint.sh

ENTRYPOINT ["entrypoint.sh"]
CMD ["conductor"]
