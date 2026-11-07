FROM python:3.11-slim-bookworm

WORKDIR /app
COPY pyproject.toml ./
COPY genre_tagger ./genre_tagger
RUN pip install --no-cache-dir . && rm -rf /root/.cache

# /music: the library (bind mount). /data: state DB, reports and the .env file.
# These ENV values win over anything in /data/.env, so the same .env can be
# shared with a workstation install.
ENV GT_LIBRARY=/music \
    GT_DB=/data/genre_tagger.db \
    GT_REPORTS=/data/reports \
    GT_ENV_FILE=/data/.env \
    PYTHONUNBUFFERED=1
VOLUME ["/data"]
WORKDIR /data

ENTRYPOINT ["genre-tagger"]
CMD ["--help"]
