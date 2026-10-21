FROM python:3.13-slim
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1
RUN groupadd -r -g 10001 claudevoice && useradd -r -u 10001 -g claudevoice -M -s /usr/sbin/nologin claudevoice
WORKDIR /app
COPY relay.py /app/relay.py
USER claudevoice
EXPOSE 8787
HEALTHCHECK --interval=30s --timeout=3s CMD python3 -c "import urllib.request;urllib.request.urlopen('http://127.0.0.1:8787/healthz',timeout=2)" || exit 1
ENTRYPOINT ["python3", "/app/relay.py", "--config", "/config/relay.toml", "--host", "0.0.0.0", "--port", "8787"]
