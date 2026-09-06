FROM python:3.12-alpine

LABEL org.opencontainers.image.title="NetworkMap" \
      org.opencontainers.image.description="Self-hosted network topology workspace" \
      org.opencontainers.image.source="https://github.com/Mr-TopG/NetworkMap" \
      org.opencontainers.image.licenses="MIT"

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

RUN apk add --no-cache iproute2 nmap \
    && addgroup -S -g 10001 networkmap \
    && adduser -S -D -H -u 10001 -G networkmap networkmap

WORKDIR /app
COPY --chown=networkmap:networkmap server.py LICENSE ./
COPY --chown=networkmap:networkmap static ./static

RUN install -d -o networkmap -g networkmap -m 0700 /data \
    && python3 -m py_compile server.py

USER 10001:10001
EXPOSE 8765
VOLUME ["/data"]

HEALTHCHECK --interval=30s --timeout=3s --start-period=5s --retries=3 \
  CMD python3 -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8765/api/health', timeout=2).read()"

ENTRYPOINT ["python3", "/app/server.py"]
CMD ["--host", "0.0.0.0", "--port", "8765", "--data-dir", "/data", "--static-dir", "/app/static"]
