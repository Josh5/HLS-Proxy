HLS Proxy
===========================

HLS Proxy - originally designed for use with [TVH-IPTV-Config](https://github.com/Josh5/TVH-IPTV-Config) as a playlist proxy.


## Using the proxy

See Docker compose file for example on how to run it.

Once running, you can pass the playlist through to this web service. Eg: `http://localhost:9987/proxy.m3u8?url=https://example.com/playlist.m3u8`

You can also use the base64 path format directly:
`http://localhost:9987/<base64_of_playlist_url>.m3u8`

### Config:

| Variable | Description |
|----------|-------------|
| DEVELOPMENT      | Run with development server. Default `false`. |
| ENABLE_DEBUGGING | Enables debug logging. Default `false`. |
| HLS_PROXY_PREFIX | The proxy URL prefix string. The default is `proxy` which will produce the proxy URL `http://<host>:<PORT>/proxy.m3u8?url=<UPSTREAM_PLAYLIST_URL>`. |
