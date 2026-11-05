FROM python:3.13-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .
RUN PGDAS_SECRET_KEY=build-only python manage.py collectstatic --noinput

# PGDAS's own data lives in a PostgreSQL database (PGDAS_APP_DB_* settings, see docker-compose.yml). The data folder
# holds the backup files PGDAS makes; mount a volume here to keep them across container restarts.
RUN mkdir -p /app/data
VOLUME ["/app/data"]

EXPOSE 8000
# Apply migrations, then serve. Waitress is a pure-Python server that runs the same on Windows and Linux.
CMD ["sh", "-c", "python manage.py migrate --noinput && waitress-serve --listen=0.0.0.0:8000 pgdas.wsgi:application"]
