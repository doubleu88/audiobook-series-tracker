FROM python:3.12-slim

WORKDIR /app

# curl is used directly (via subprocess) to talk to Audible — see app/scraper.py
# for why: plain Python HTTP clients get blocked by Audible's WAF even with full
# browser TLS fingerprint impersonation, but curl itself is unaffected.
#
# tzdata is required for the TZ environment variable (set in docker-compose.yml)
# to actually take effect — python:3.12-slim has no timezone data installed at
# all, so datetime.date.today() silently runs on UTC regardless of TZ until
# this is present. Confirmed this was happening in production: the container's
# clock was hours ahead of the real local date.
RUN apt-get update && apt-get install -y --no-install-recommends curl tzdata \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app
# app/version.py reads its own version from this at runtime, rather than the
# version being baked in separately and risking drifting out of sync.
COPY CHANGELOG.md .

VOLUME ["/app/data"]
EXPOSE 8000

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
