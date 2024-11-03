#!/usr/bin/env python3
# -*- coding:utf-8 -*-
import asyncio
import os

from backend import create_app, enable_debugging

# Create app
app = create_app()
if enable_debugging:
    app.logger.info(' DEBUGGING   = ' + str(enable_debugging))

if __name__ == "__main__":
    # Create a custom loop
    loop = asyncio.get_event_loop()

    # Start Quart server
    app.logger.info("Starting Quart server...")
    app.run(loop=loop, debug=True, host='0.0.0.0', port=os.environ.get('HLS_PROXY_PORT', 9987))
    app.logger.info("Quart server completed.")
