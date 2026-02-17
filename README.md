# HLS Proxy

HLS Proxy - originally designed for use with [TVH-IPTV-Config](https://github.com/Josh5/TVH-IPTV-Config) as a playlist proxy.

## Using the proxy

See Docker compose file for example on how to run it.

This proxy supports both HLS playlist rewriting and shared MPEG-TS stream multiplexing.

Quick examples:

- HLS playlist entrypoint:
  `http://localhost:9987/proxy.m3u8?url=https://example.com/live.m3u8`
- Direct base64 playlist path:
  `http://localhost:9987/<base64_of_playlist_url>.m3u8`
- Raw/live MPEG-TS multiplexed stream path:
  `http://localhost:9987/stream/<base64_of_stream_url>`

### Config:

| Variable                      | Description                                                                                                                                                                           |
| ----------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `DEVELOPMENT`                 | Run Quart development server when `true`. Default `false` (uses Hypercorn in container entrypoint).                                                                                   |
| `ENABLE_DEBUGGING`            | Enables debug logging when `true`. Default `false`.                                                                                                                                   |
| `HLS_PROXY_PORT`              | Listening port. Default `9987`.                                                                                                                                                       |
| `HLS_PROXY_PREFIX`            | Optional URL path prefix for proxied routes. Default `/` (no extra prefix). Example: set `HLS_PROXY_PREFIX=/proxy` to make routes like `/proxy/stream/<b64>` and `/proxy/<b64>.m3u8`. |
| `HLS_PROXY_HOST_IP`           | Optional host/IP override used when rewriting child playlist URLs. If set, rewritten URLs use this host instead of the incoming request host.                                         |
| `HLS_PROXY_MAX_BUFFER_BYTES`  | Max playlist size (bytes) processed in-memory before switching to streaming rewrite mode. Default `1048576` (1 MiB).                                                                  |
| `HLS_PROXY_MAX_HISTORY_BYTES` | Per active shared stream in-memory history buffer (bytes) for prebuffer priming/new subscribers. Default `209715200` (200 MiB).                                                       |
| `HLS_PROXY_DEFAULT_PREBUFFER` | Default prebuffer used by `/stream/<base64_url>` when no `prebuffer` query parameter is provided. Supports bytes or `K`/`M` suffixes (for example `64K`, `1M`). Default `0`.         |

### Endpoints:

| Method        | Path                             | Purpose                                  | Notes                                                                                                                                                                              |
| ------------- | -------------------------------- | ---------------------------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `GET`, `HEAD` | `/proxy.m3u8?url=<upstream_url>` | Redirect helper for HLS playlists.       | Redirects to `/<base64>.m3u8` and injects `connection_id`.                                                                                                                         |
| `GET`, `HEAD` | `/<base64_url>.m3u8`             | HLS playlist proxy + rewrite.            | Rewrites child URLs (`.m3u8`, `.ts`, `.key`, `.vtt`) back to this proxy and preserves query params like `connection_id`.                                                           |
| `GET`, `HEAD` | `/<base64_url>.ts`               | Segment proxy for TS content.            | For HLS segments. If `ffmpeg=true` or `prebuffer=<size>` is present, redirects to `/stream/<base64_url>`. If upstream content is playlist-like, redirects to `/<base64_url>.m3u8`. |
| `GET`, `HEAD` | `/<base64_url>.key`              | HLS key proxy.                           | Fetches and caches key content.                                                                                                                                                    |
| `GET`, `HEAD` | `/<base64_url>.vtt`              | Subtitle segment proxy.                  | Fetches and caches VTT content.                                                                                                                                                    |
| `GET`         | `/stream/<base64_url>`           | Shared live stream multiplexer endpoint. | Use for raw/live MPEG-TS sources. Default mode is direct async upstream read; add `?ffmpeg=true` for FFmpeg mode. Supports `prebuffer=<size>` and falls back to `HLS_PROXY_DEFAULT_PREBUFFER` when omitted. |

If `HLS_PROXY_PREFIX` is configured, prepend it to the paths above.

Examples with prefix `/proxy`:

- `/proxy/proxy.m3u8?url=...`
- `/proxy/<base64>.m3u8`
- `/proxy/stream/<base64>`

## Testing

Create a virtual environment and install dependencies:

```bash
python -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

Run the tests:

```bash
python -m unittest -v
```
