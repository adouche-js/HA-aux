ARG BUILD_FROM
FROM $BUILD_FROM

ENV LANG=C.UTF-8
ENV TERM=xterm

# -------------------------------------------------------------------
# Dependencies
# -------------------------------------------------------------------
RUN apk add --no-cache --virtual .build-deps \
        gcc \
        musl-dev \
    && apk add --no-cache \
        python3 \
        py3-pip \
        alsa-utils \
        alsa-lib \
        alsa-utils-doc \
        mpv \
        ffmpeg \
        bash \
        curl \
        socat \
    && pip3 install --no-cache-dir --break-system-packages \
        aiohttp \
    && apk del .build-deps \
    && rm -rf /tmp/* /var/cache/apk/*

# -------------------------------------------------------------------
# Application
# -------------------------------------------------------------------
COPY src/ha_aux_addon/ /usr/local/lib/ha_aux_addon/
ENV PYTHONPATH="/usr/local/lib"

# -------------------------------------------------------------------
# Bypass s6-overlay init entirely — the HA base image uses it as the
# default entrypoint, but s6-overlay-suexec crashes at runtime with
# "can only run as pid 1" in Supervisor 2026.06.
# We set an empty ENTRYPOINT and run the Python server directly as
# PID 1, where it handles its own signals.
# -------------------------------------------------------------------
ENTRYPOINT []
CMD ["python3", "-m", "ha_aux_addon"]
