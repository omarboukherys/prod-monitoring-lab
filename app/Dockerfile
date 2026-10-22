FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY app.py traffic.py receiver.py ./
RUN mkdir -p /var/log/app
EXPOSE 8000
HEALTHCHECK --interval=15s --timeout=3s --retries=3 \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8000/health')" || exit 1
# 1 worker : l'état chaos est en mémoire et doit être partagé
CMD ["gunicorn", "-w", "1", "--threads", "8", "-b", "0.0.0.0:8000", "app:app"]
