#!/usr/bin/env python3
# -*- coding:utf-8 -*-
import asyncio
import logging
import os
import uuid
from urllib.parse import urlencode

from quart import Response, redirect, request, stream_with_context

from backend.api import blueprint
from backend.hls_multiplexer import (
    handle_m3u8_proxy,
    handle_segment_proxy,
    handle_multiplexed_stream,
    b64_urlsafe_decode,
    b64_urlsafe_encode,
    parse_size,
    mux_manager,
    SegmentCache
)

# Global cache instance (short default TTL for HLS segments)
hls_segment_cache = SegmentCache(ttl=120)

hls_proxy_prefix = os.environ.get("HLS_PROXY_PREFIX", "/")
if not hls_proxy_prefix.startswith("/"):
    hls_proxy_prefix = "/" + hls_proxy_prefix

hls_proxy_host_ip = os.environ.get("HLS_PROXY_HOST_IP")
hls_proxy_port = os.environ.get("HLS_PROXY_PORT")
hls_proxy_max_buffer_bytes = int(os.environ.get("HLS_PROXY_MAX_BUFFER_BYTES", "1048576"))
hls_proxy_default_prebuffer = parse_size(
    os.environ.get("HLS_PROXY_DEFAULT_PREBUFFER", "0"),
    default=0,
)

proxy_logger = logging.getLogger("proxy")
ffmpeg_logger = logging.getLogger("ffmpeg")
buffer_logger = logging.getLogger("buffer")


async def cleanup_hls_proxy_state():
    """Cleanup expired cache entries and idle stream activity."""
    try:
        evicted_count = await hls_segment_cache.evict_expired_items()
        if evicted_count > 0:
            proxy_logger.info(f"Cache cleanup: evicted {evicted_count} expired items")
        await mux_manager.cleanup_idle_streams(idle_timeout=300)
    except Exception as e:
        proxy_logger.error(f"Error during cache cleanup: {e}")


async def periodic_cache_cleanup():
    while True:
        try:
            await cleanup_hls_proxy_state()
        except Exception as e:
            proxy_logger.error("Error during cache cleanup: %s", e)
        await asyncio.sleep(60)


@blueprint.record_once
def _register_startup(state):
    app = state.app

    @app.before_serving
    async def _start_periodic_cache_cleanup():
        asyncio.create_task(periodic_cache_cleanup())


def _build_upstream_headers():
    headers = {}
    try:
        src = request.headers
    except Exception:
        return headers
    for name in ("User-Agent", "Referer", "Origin", "Accept", "Accept-Language"):
        value = src.get(name)
        if value:
            headers[name] = value
    return headers


def _build_proxy_base_url():
    base_path = hls_proxy_prefix.rstrip("/")
    if hls_proxy_host_ip:
        protocol = "http"
        host_port = ""
        if hls_proxy_port:
            if hls_proxy_port == "443":
                protocol = "https"
            host_port = f":{hls_proxy_port}"
        return f"{protocol}://{hls_proxy_host_ip}{host_port}{base_path}"
    return f"{request.host_url.rstrip('/')}{base_path}"


def _get_connection_id(default_new=False):
    value = (request.args.get("connection_id") or request.args.get("cid") or "").strip()
    if value:
        return value
    if default_new:
        return uuid.uuid4().hex
    return None


@blueprint.route(f"{hls_proxy_prefix.lstrip('/')}/<encoded_url>.m3u8", methods=["GET", "HEAD"])
async def proxy_m3u8(encoded_url):
    connection_id = _get_connection_id(default_new=False)
    if not connection_id:
        params = [(k, v) for k, v in request.args.items() if k not in {"cid", "connection_id"}]
        connection_id = _get_connection_id(default_new=True)
        params.append(("connection_id", connection_id))
        target = f"{hls_proxy_prefix.rstrip('/')}/{encoded_url}.m3u8?{urlencode(params)}"
        return redirect(target, code=302)

    decoded_url = b64_urlsafe_decode(encoded_url)

    headers = _build_upstream_headers()

    body, content_type, status, res_headers = await handle_m3u8_proxy(
        decoded_url,
        request_host_url=request.host_url,
        hls_proxy_prefix=hls_proxy_prefix,
        headers=headers,
        connection_id=connection_id,
        max_buffer_bytes=hls_proxy_max_buffer_bytes,
        proxy_base_url=_build_proxy_base_url(),
        segment_cache=hls_segment_cache,
        prefetch_segments_enabled=True,
    )

    if body is None:
        resp = Response("Failed to fetch the original playlist.", status=status)
        for k, v in res_headers.items():
            resp.headers[k] = v
        return resp

    if hasattr(body, '__aiter__'):
        @stream_with_context
        async def generate_playlist():
            async for chunk in body:
                yield chunk
        return Response(generate_playlist(), content_type=content_type)
    return Response(body, content_type=content_type)


@blueprint.route("/proxy.m3u8", methods=["GET", "HEAD"])
async def proxy_m3u8_redirect():
    url = request.args.get("url")
    if not url:
        return Response("Missing url parameter", status=400)

    encoded = b64_urlsafe_encode(url)
    connection_id = _get_connection_id(default_new=True)
    target = f"{hls_proxy_prefix.rstrip('/')}/{encoded}.m3u8"
    return redirect(f"{target}?connection_id={connection_id}", code=302)


@blueprint.route(f"{hls_proxy_prefix.lstrip('/')}/<encoded_url>.key", methods=["GET", "HEAD"])
async def proxy_key(encoded_url):
    # Decode the Base64 encoded URL
    decoded_url = b64_urlsafe_decode(encoded_url)
    content, status, _ = await handle_segment_proxy(decoded_url, _build_upstream_headers(), hls_segment_cache)
    if content is None:
        return Response("Failed to fetch.", status=status)
    return Response(content, content_type="application/octet-stream")


@blueprint.route(f"{hls_proxy_prefix.lstrip('/')}/<encoded_url>.ts", methods=["GET", "HEAD"])
async def proxy_ts(encoded_url):
    """
    TIC Legacy/Segment Proxy Endpoint

    This endpoint serves individual .ts segments for HLS or provides a 
    direct stream when configured.

    Parameters:
    - ffmpeg=true: (Optional) Fallback to FFmpeg remuxer for better compatibility.
    - prebuffer=X: (Optional) Buffer size cushion (e.g. 2M, 512K). Default: 1M.
    """
    # Decode the Base64 encoded URL
    decoded_url = b64_urlsafe_decode(encoded_url)

    # Multiplexer routing
    if request.args.get("ffmpeg", "false").lower() == "true" or request.args.get("prebuffer"):
        target = f"{hls_proxy_prefix.rstrip('/')}/stream/{encoded_url}"
        if request.query_string:
            target = f"{target}?{request.query_string.decode()}"
        return redirect(target, code=302)

    content, status, content_type = await handle_segment_proxy(decoded_url, _build_upstream_headers(), hls_segment_cache)
    if content is None:
        return Response("Failed to fetch.", status=status)

    # Playlist detection
    if "mpegurl" in (content_type or "") or content.lstrip().startswith(b"#EXTM3U"):
        target = f"{hls_proxy_prefix.rstrip('/')}/{encoded_url}.m3u8"
        if request.query_string:
            target = f"{target}?{request.query_string.decode()}"
        return redirect(target, code=302)

    return Response(content, content_type="video/mp2t")


@blueprint.route(f"{hls_proxy_prefix.lstrip('/')}/<encoded_url>.vtt", methods=["GET", "HEAD"])
async def proxy_vtt(encoded_url):
    # Decode the Base64 encoded URL
    decoded_url = b64_urlsafe_decode(encoded_url)
    content, status, _ = await handle_segment_proxy(decoded_url, _build_upstream_headers(), hls_segment_cache)
    if content is None:
        return Response("Failed to fetch.", status=status)
    return Response(content, content_type="text/vtt")


@blueprint.route(f"{hls_proxy_prefix.lstrip('/')}/stream/<encoded_url>", methods=["GET"])
async def stream_ts(encoded_url):
    """
    TIC Shared Multiplexer Stream Endpoint

    This endpoint provides a shared upstream connection for multiple TIC clients.

    Default Mode (Direct):
    Uses high-performance async socket reads. Best for 99% of streams.

    Fallback Mode (FFmpeg):
    Enabled by appending '?ffmpeg=true'. Uses an external FFmpeg process to 
    remux/clean the stream. Use this ONLY if 'direct' mode has playback issues.

    Parameters:
    - ffmpeg=true: (Optional) Fallback to FFmpeg remuxer for better compatibility.
    - prebuffer=X: (Optional) Buffer size cushion (e.g. 2M, 512K). Default: 1M.
    """
    decoded_url = b64_urlsafe_decode(encoded_url)
    connection_id = _get_connection_id(default_new=True)

    use_ffmpeg = request.args.get("ffmpeg", "false").lower() == "true"
    prebuffer_bytes = parse_size(
        request.args.get("prebuffer"),
        default=hls_proxy_default_prebuffer,
    )
    mode = "ffmpeg" if use_ffmpeg else "direct"

    generator = await handle_multiplexed_stream(
        decoded_url,
        mode,
        _build_upstream_headers(),
        prebuffer_bytes,
        connection_id
    )

    @stream_with_context
    async def generate_stream():
        async for chunk in generator:
            yield chunk
    response = Response(generate_stream(), content_type="video/mp2t")
    response.timeout = None  # Disable timeout for streaming response
    return response
