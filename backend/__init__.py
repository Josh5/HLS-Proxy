#!/usr/bin/env python3
# -*- coding:utf-8 -*-
import logging
import os
from importlib import import_module
from logging.config import dictConfig

from quart import Quart

dictConfig(
    {
        "version": 1,
        "formatters": {
            "default": {
                "format": "%(asctime)s:%(levelname)s:%(name)s: - %(message)s",
            }
        },
        "loggers": {
            "quart.app": {
                "level": "ERROR",
            },
        },
        "handlers": {"wsgi": {"class": "logging.StreamHandler", "stream": "ext://sys.stderr", "formatter": "default"}},
        "root": {"level": "INFO", "handlers": ["wsgi"]},
    }
)

enable_debugging = False
if os.environ.get("ENABLE_DEBUGGING", "false").lower() == "true":
    enable_debugging = True


def create_app():
    # Create app
    app = Quart(__name__, instance_relative_config=True)

    # Register the route blueprints
    module = import_module("backend.api.routes_hls_proxy")
    app.register_blueprint(module.blueprint)

    access_logger = logging.getLogger("hypercorn.access")
    app.logger.setLevel(logging.INFO)
    access_logger.setLevel(logging.INFO)
    if enable_debugging:
        logging.getLogger().setLevel(logging.DEBUG)
        app.logger.setLevel(logging.DEBUG)
        access_logger.setLevel(logging.DEBUG)

    return app
