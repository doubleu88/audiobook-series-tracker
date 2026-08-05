FROM python:3.12-slim

WORKDIR /app

# curl is used directly (via subprocess) to talk to Audible — see app/scraper.py
# for why: plain Python HTTP clients get blocked by Audible's WAF even with full
# browser TLS fingerprint impersonation, but curl itself is unaffected.
RUN apt-get update && apt-get install -y --no-install-recommends curl \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app

VOLUME ["/app/data"]
EXPOSE 8000

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
