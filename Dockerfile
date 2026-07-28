FROM python:3.12-slim

# Systempakete: Tesseract OCR (inkl. deutschem Sprachpaket) + poppler-utils für PDF-Seiten
RUN apt-get update && apt-get install -y --no-install-recommends \
    tesseract-ocr \
    tesseract-ocr-deu \
    poppler-utils \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

EXPOSE 10000

CMD gunicorn app:app --bind 0.0.0.0:${PORT:-10000} --timeout 300 --workers 1
