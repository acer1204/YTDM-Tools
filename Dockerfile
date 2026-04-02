FROM python:3.12-slim

# Install ffmpeg (optional but recommended for merging video+audio)
RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg \
    nodejs \
  && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app.py .
COPY static/ static/

# Default download path inside container (override via env or settings)
ENV DOWNLOAD_DIR=/app/downloads
ENV DATA_DIR=/app/data

RUN mkdir -p /app/downloads /app/data/cookies

EXPOSE 5000

CMD ["python", "-u", "app.py"]
