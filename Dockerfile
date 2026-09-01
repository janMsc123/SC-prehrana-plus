FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /srv

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app

# Create the user and prepare /data BEFORE declaring the volume. Anything a
# later layer writes into a VOLUME path is discarded, so a chown after the
# VOLUME line would be silently lost and the container would start as a
# non-root user unable to write its own database.
RUN useradd --create-home --uid 10001 prehrana \
 && mkdir -p /data \
 && chown -R prehrana:prehrana /data /srv

# The database lives on a volume so it survives image rebuilds -- the whole
# point of Phase 1 is the archive that builds up over the weeks.
VOLUME ["/data"]
ENV DATABASE_PATH=/data/prehrana.db

USER prehrana

EXPOSE 8000

HEALTHCHECK --interval=60s --timeout=5s --start-period=10s --retries=3 \
  CMD ["python", "-c", "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/healthz',timeout=4).status==200 else 1)"]

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
