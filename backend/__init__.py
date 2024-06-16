#!/usr/bin/env python3
# -*- coding:utf-8 -*-
import logging
import os
from logging.config import dictConfig
from importlib import import_module

from quart import Quart

dictConfig({
    'version':    1,
    'formatters': {
        'default': {
            'format': '%(asctime)s:%(levelname)s:%(name)s: %(message)s',
        }
    },
    'handlers':   {
        'wsgi': {
            'class':     'logging.StreamHandler',
            'stream':    'ext://sys.stderr',
            'formatter': 'default'
        }
    },
    'root':       {
        'level':    'INFO',
        'handlers': ['wsgi']
    }
})

enable_debugging = False
if os.environ.get('ENABLE_DEBUGGING', 'false').lower() == 'true':
    enable_debugging = True


def create_app():
    # Create app
    app = Quart(__name__, instance_relative_config=True)

    # Register the route blueprints
    module = import_module('backend.api.routes_hls_proxy')
    app.register_blueprint(module.blueprint)

    # TODO: Configure logging
    log = logging.getLogger('werkzeug')
    app.logger.setLevel(logging.INFO)
    log.setLevel(logging.INFO)
    if enable_debugging:
        app.logger.setLevel(logging.DEBUG)
        log.setLevel(logging.DEBUG)

    return app
