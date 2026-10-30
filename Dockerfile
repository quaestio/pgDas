FROM python:3.13-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .
RUN PGDECK_SECRET_KEY=build-only python manage.py collectstatic --noinput

# PgDeck's own data is one SQLite file; mount a volume here to keep it across container restarts.
ENV PGDECK_SQLITE_PATH=/app/data/pgdeck.sqlite3
RUN mkdir -p /app/data
VOLUME ["/app/data"]

EXPOSE 8000
# Apply migrations, then serve. Waitress is a pure-Python server that runs the same on Windows and Linux.
CMD ["sh", "-c", "python manage.py migrate --noinput && waitress-serve --listen=0.0.0.0:8000 pgdeck.wsgi:application"]
