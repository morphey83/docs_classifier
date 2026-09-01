FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# unar: rar extraction. tesseract/ghostscript/qpdf: OCR (ocrmypdf + pytesseract).
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        unar \
        tesseract-ocr tesseract-ocr-rus tesseract-ocr-eng \
        ghostscript qpdf pngquant unpaper \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY alembic.ini ./
COPY alembic ./alembic
COPY app ./app

EXPOSE 8000

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
