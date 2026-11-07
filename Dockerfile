# Production image for DigitalOcean App Platform (spec 01 §1.8).
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

COPY requirements.txt .
RUN pip install -r requirements.txt

COPY . .

# Collect static files at build time (they're served by WhiteNoise).
RUN DEBUG=False SECRET_KEY=build-only ALLOWED_HOSTS=localhost python manage.py collectstatic --noinput

# Run as an unprivileged user.
RUN useradd --create-home bbms && chown -R bbms /app
USER bbms

EXPOSE 8080
# App Platform sets $PORT. Migrations run as a separate pre-deploy job, not here.
CMD ["sh", "-c", "gunicorn bbms.wsgi:application --bind 0.0.0.0:${PORT:-8080} --workers ${WEB_CONCURRENCY:-2} --access-logfile -"]
