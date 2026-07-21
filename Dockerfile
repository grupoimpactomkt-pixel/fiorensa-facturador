FROM python:3.12-slim
RUN apt-get update && apt-get install -y --no-install-recommends openssl && rm -rf /var/lib/apt/lists/*
RUN pip install --no-cache-dir fpdf2 segno   # PDF del comprobante + QR
WORKDIR /app
COPY facturador.py .
ENV ARCA_BIND=0.0.0.0 ARCA_DIR=/app
EXPOSE 8077
CMD ["python", "facturador.py", "serve", "8077"]
