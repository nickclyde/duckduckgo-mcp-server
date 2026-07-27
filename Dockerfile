FROM python:3.13-slim

WORKDIR /app

COPY . /app

RUN pip install --no-cache-dir --upgrade pip \
    && pip install --no-cache-dir .

# Updated to allow command-line switches to be passed via docker commands
ENTRYPOINT ["python", "-m", "duckduckgo_mcp_server.server"]
CMD []
