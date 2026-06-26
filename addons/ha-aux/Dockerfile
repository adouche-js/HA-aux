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
COPY rootfs /
COPY src/ha_aux_addon/ /usr/local/lib/ha_aux_addon/
ENV PYTHONPATH="/usr/local/lib"

RUN chmod a+x /etc/services.d/ha-aux/run \
    && chmod a+x /etc/cont-init.d/00-audio-permissions.sh
