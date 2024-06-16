#!/usr/bin/env python3
# -*- coding:utf-8 -*-
from backend import create_app, enable_debugging

# Create app
app = create_app()
if enable_debugging:
    app.logger.info(' DEBUGGING   = ' + str(enable_debugging))

if __name__ == "__main__":
    # Start Quart server
    app.logger.info("Starting Quart server...")
    app.run(debug=True, host='0.0.0.0', port=9987)
