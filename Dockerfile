FROM python:3.13-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .
# The data cache (data/cache/*.json) is committed, so the image runs offline;
# the SQLite DB is rebuilt from it at startup.
ENV PORT=8000
EXPOSE 8000
CMD ["sh", "-c", "uvicorn app.main:app --host 0.0.0.0 --port ${PORT}"]
