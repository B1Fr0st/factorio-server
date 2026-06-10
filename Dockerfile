FROM --platform=linux/amd64 factoriotools/factorio:stable

USER root

RUN apt-get update && apt-get install -y --no-install-recommends \
        python3 \
        python3-pip \
        python3-venv \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /manager

COPY manager/requirements.txt .
RUN python3 -m pip install --no-cache-dir --break-system-packages -r requirements.txt

COPY manager/ .

# Game port (UDP) + RCON (TCP) + Manager HTTP (TCP)
EXPOSE 34197/udp 27015/tcp 8080/tcp

ENTRYPOINT ["python3", "-u", "/manager/main.py"]
