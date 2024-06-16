#!/usr/bin/env bash
###
# File: entrypoint.sh
# Project: docker
# File Created: Monday, 13th May 2024 4:20:35 pm
# Author: Josh.5 (jsunnex@gmail.com)
# -----
# Last Modified: Monday, 17th June 2024 11:27:23 am
# Modified By: Josh5 (jsunnex@gmail.com)
###

set -e -x

# All printed log lines from this script should be formatted with this function
print_log() {
  local timestamp
  local pid
  local level
  local message
  timestamp="$(date +'%Y-%m-%d %H:%M:%S %z')"
  pid="$$"
  level="$1"
  message="${*:2}"
  echo "[${timestamp}] [${pid}] [${level^^}] ${message}"
}

# Exec provided command
command="${*}"
if [ "X${command:-}" != "X" ]; then
    print_log info "Running command '${command:?}'"
    exec "${command:?}"
else
    # Install packages (if requested)
    if [ "${RUN_PIP_INSTALL}" = "true" ]; then
        python3 -m venv --symlinks --clear /var/venv-docker
        source /var/venv-docker/bin/activate
        python3 -m pip install --no-cache-dir -r /app/requirements.txt
    else
        source /var/venv-docker/bin/activate
    fi

    # Run proxy
    if [ "${DEVELOPMENT}" = "true" ]; then
        # Run Quart server
        python3 /app/run.py
    else
        # Run hypercorn server
        # REF: https://pgjones.gitlab.io/hypercorn/how_to_guides/configuring.html
        exec hypercorn --workers=1 --bind=0.0.0.0:9987 run:app
    fi
fi
