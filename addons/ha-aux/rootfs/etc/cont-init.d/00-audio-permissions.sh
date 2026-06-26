#!/usr/bin/with-contenv bashio

# Ensure audio groups and permissions are correct
if [ -e /dev/snd ]; then
    bashio::log.debug "Fixing permissions for /dev/snd"
    chmod -R 777 /dev/snd
fi

# Load ALSA modules if necessary (usually handled by host, but good to check)
if ! lsmod | grep -q snd; then
    bashio::log.warn "No ALSA kernel modules detected. Audio might not work."
fi
