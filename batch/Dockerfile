FROM python:3.12-slim
WORKDIR /batch
RUN pip install --no-cache-dir prometheus-client==0.20.0
COPY scheduler.py .
RUN mkdir -p /var/log/batch /control
CMD ["python", "-u", "scheduler.py"]
