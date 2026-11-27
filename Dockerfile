FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    STREAMLIT_SERVER_FILE_WATCHER_TYPE=none \
    STREAMLIT_SERVER_RUN_ON_SAVE=false \
    PIP_NO_CACHE_DIR=1 \
    LTL_DB_PATH=/data/umoyalogis.db \
    LTL_GEOCACHE_PATH=/data/geocache.json

RUN useradd --create-home --uid 1000 app && mkdir /data && chown app /data
WORKDIR /app

COPY requirements.txt .
RUN pip install -r requirements.txt

COPY --chown=app . .
USER app
VOLUME /data
EXPOSE 8501

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://localhost:8501/_stcore/health', timeout=4).status == 200 else 1)"

CMD ["streamlit", "run", "app.py", "--server.port=8501", "--server.address=0.0.0.0", "--server.headless=true"]
