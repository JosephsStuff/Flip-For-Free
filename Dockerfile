# Flip for Free in a container.  docker compose up -d --build   (see README → Docker)
ARG BASE=python:3.12-slim
FROM ${BASE}

ENV PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    FLIP_DOCKER=1 \
    FLIP_LAUNCHER=1 \
    EPT_CONFIG_DIR=/data/config \
    FLIP_DATA_DIR=/data \
    PATH=/opt/venv/bin:$PATH

WORKDIR /opt/flip
COPY requirements.txt .
RUN python3 -m venv /opt/venv && pip install --no-cache-dir -r requirements.txt

# The image's copy of the app. On first start it is copied to /data/app, which is what runs:
# that way Settings → Updates works inside the container and survives restarts.
COPY . /opt/flip
RUN chmod +x /opt/flip/deploy/docker-entrypoint.sh /opt/flip/deploy/flip-address \
 && ln -s /opt/flip/deploy/flip-address /usr/local/bin/flip-address

VOLUME /data
EXPOSE 8765
ENTRYPOINT ["/opt/flip/deploy/docker-entrypoint.sh"]
