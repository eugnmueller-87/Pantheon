FROM python:3.11-slim

WORKDIR /app

# System deps (for ib_insync, chromadb build)
RUN apt-get update -qq && apt-get install -y -qq --no-install-recommends \
    gcc g++ curl \
    && rm -rf /var/lib/apt/lists/*

# Python deps first (cached layer)
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# App source
COPY . .

# Non-root user with home dir for ChromaDB
RUN useradd -r -u 1001 -m pantheon && chown -R pantheon:pantheon /app
USER pantheon

EXPOSE 8080 8081

HEALTHCHECK --interval=30s --timeout=10s --start-period=20s --retries=3 \
    CMD curl -f http://localhost:8080/health || exit 1

CMD ["python", "main.py", "--port", "8080"]
