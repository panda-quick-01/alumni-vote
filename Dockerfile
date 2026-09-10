FROM python:3.11-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY app.py index.html ./
ENV PORT=3000 DATABASE_PATH=/data/election.db PYTHONUNBUFFERED=1
VOLUME ["/data"]
EXPOSE 3000
CMD ["sh","-c","gunicorn -w 2 -b 0.0.0.0:${PORT:-3000} app:app"]
