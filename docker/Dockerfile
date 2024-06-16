FROM python:3.12-alpine

# set version label
ARG BUILD_DATE
ARG VERSION
LABEL build_version="Version:- ${VERSION} Build-date:- ${BUILD_DATE}"
LABEL maintainer="Josh.5 <jsunnex@gmail.com>"

# environment settings
ENV HOME="/config"

RUN \
    echo "**** install runtime packages ****" \
        && apk add --no-cache \
            bash \
    && \
    echo

# set environment variables
ENV PYTHONDONTWRITEBYTECODE="1"
ENV PYTHONUNBUFFERED="1"
ENV DEVELOPMENT="false"
ENV ENABLE_DEBUGGING="false"
ENV HLS_PROXY_PREFIX="proxy"

# Install python dependencies
COPY requirements.txt /app/requirements.txt
RUN \
    echo "** Install python dependencies **" \
        && mkdir -p /app \
        && python3 -m venv --symlinks /var/venv-docker \
        && . /var/venv-docker/bin/activate \
        && python3 -m pip install --upgrade pip \
        && python3 -m pip install --no-cache-dir -r /app/requirements.txt \
    && \
    echo

# Install project
COPY backend /app/backend
COPY run.py /app/run.py

# Set working directory
WORKDIR /app/

# Expose ports
EXPOSE 9987/tcp

# Install entrypoint script
COPY ./docker/entrypoint.sh /entrypoint.sh
ENTRYPOINT [ "/entrypoint.sh" ]
