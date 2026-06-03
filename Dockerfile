FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    CHROME_BIN=/usr/bin/chromium

WORKDIR /app

RUN apt-get update \
    && apt-get install -y --no-install-recommends chromium chromium-driver fonts-liberation \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

EXPOSE 8000
CMD ["gunicorn", "--bind", "0.0.0.0:8000", "--timeout", "2700", "app:app"]
