FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PORT=5003

WORKDIR /app

# Dependencias primero (aprovecha la cache de capas)
COPY requirements.txt .
RUN pip install --upgrade pip && pip install -r requirements.txt

# Codigo de la aplicacion
COPY main.py .

# Usuario sin privilegios
RUN useradd --create-home --uid 1000 appuser \
    && mkdir -p /secrets \
    && chown -R appuser:appuser /app /secrets
USER appuser

EXPOSE 5003

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD python -c "import os,urllib.request as u; u.urlopen('http://127.0.0.1:%s/health' % os.getenv('PORT','5003'), timeout=3)" || exit 1

CMD ["python", "main.py"]
