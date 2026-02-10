#!/usr/bin/env python3
# -*- coding:utf-8 -*-
import asyncio
import base64
import logging
import os
import re
import subprocess
import threading
import time
import uuid
from collections import deque

from quart import current_app, Response, stream_with_context, request, redirect

from backend.api import blueprint
import aiohttp
from urllib.parse import urlparse, urljoin

# Test:
#       > mkfifo /tmp/ffmpegpipe
#       > ffmpeg -probesize 10M -analyzeduration 0 -fpsprobesize 0 -i "<URL>" -c copy -y -f mpegts /tmp/ffmpegpipe
#       > vlc /tmp/ffmpegpipe
#
#   Or:
#       > ffmpeg -probesize 10M -analyzeduration 0 -fpsprobesize 0 -i "<URL>" -c copy -y -f mpegts - | vlc -
#

hls_proxy_prefix = os.environ.get("HLS_PROXY_PREFIX", "/")
if not hls_proxy_prefix.startswith("/"):
    hls_proxy_prefix = "/" + hls_proxy_prefix

hls_proxy_host_ip = os.environ.get("HLS_PROXY_HOST_IP")
hls_proxy_port = os.environ.get("HLS_PROXY_PORT")

proxy_logger = logging.getLogger("proxy")
ffmpeg_logger = logging.getLogger("ffmpeg")
buffer_logger = logging.getLogger("buffer")

# A dictionary to keep track of active streams
active_streams = {}


class FFmpegStream:
    def __init__(self, decoded_url):
        self.decoded_url = decoded_url
        self.buffers = {}
        self.process = None
        self.running = True
        self.thread = threading.Thread(target=self.run_ffmpeg)
        self.connection_count = 0
        self.lock = threading.Lock()
        self.last_activity = time.time()  # Track last activity time
        self.thread.start()

    def run_ffmpeg(self):
        command = [
            "ffmpeg",
            "-hide_banner",
            "-loglevel",
            "info",
            "-err_detect",
            "ignore_err",
            "-probesize",
            "20M",
            "-analyzeduration",
            "0",
            "-fpsprobesize",
            "0",
            "-i",
            self.decoded_url,
            "-c",
            "copy",
            "-f",
            "mpegts",
            "pipe:1",
        ]
        ffmpeg_logger.info("Executing FFmpeg with command: %s", command)
        self.process = subprocess.Popen(
            command, stdout=subprocess.PIPE, stderr=subprocess.PIPE
        )

        # Start a thread to log stderr
        stderr_thread = threading.Thread(target=self.log_stderr)
        stderr_thread.daemon = (
            True  # Make this a daemon thread so it exits when main thread exits
        )
        stderr_thread.start()

        chunk_size = 65536  # Read 64 KB at a time
        while self.running:
            try:
                # Use select to avoid blocking indefinitely
                import select

                ready, _, _ = select.select([self.process.stdout], [], [], 1.0)
                if not ready:
                    # No data available, check if we should terminate due to inactivity
                    if (
                        time.time() - self.last_activity > 300
                    ):  # 5 minutes of inactivity
                        ffmpeg_logger.info(
                            "No activity for 5 minutes, terminating FFmpeg stream"
                        )
                        self.stop()
                        break
                    continue

                chunk = self.process.stdout.read(chunk_size)
                if not chunk:
                    ffmpeg_logger.warning("FFmpeg has finished streaming.")
                    break

                # Update last activity time
                self.last_activity = time.time()

                # Append the chunk to all buffers
                with self.lock:  # Use lock when accessing buffers
                    for buffer in self.buffers.values():
                        buffer.append(chunk)
            except Exception as e:
                ffmpeg_logger.error("Error reading stdout: %s", e)
                break

        self.cleanup()

    def log_stderr(self):
        """Log stderr output from the FFmpeg process."""
        while self.running and self.process and self.process.stderr:
            try:
                line = self.process.stderr.readline()
                if not line:
                    break
                ffmpeg_logger.debug("FFmpeg: %s", line.decode("utf-8", errors="replace").strip())
            except Exception as e:
                ffmpeg_logger.error("Error reading stderr: %s", e)
                break

    def add_buffer(self, connection_id):
        with self.lock:
            if connection_id not in self.buffers:
                ffmpeg_logger.info("Adding new buffer with ID %s", connection_id)
                self.buffers[connection_id] = TimeBuffer()
                self.connection_count += 1
            return self.buffers[connection_id]

    def remove_buffer(self, connection_id):
        with self.lock:
            if connection_id in self.buffers:
                ffmpeg_logger.info("Removing buffer with ID %s", connection_id)
                del self.buffers[connection_id]
                self.connection_count -= 1
                if self.connection_count <= 0:
                    ffmpeg_logger.info("No more connections, stopping FFmpeg stream")
                    threading.Thread(target=self.stop).start()

    def cleanup(self):
        """Clean up resources properly"""
        self.running = False
        if self.process:
            try:
                # Try to terminate the process gracefully first
                self.process.terminate()
                # Wait a bit for it to terminate
                try:
                    self.process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    # If it doesn't terminate, kill it
                    self.process.kill()
                    self.process.wait()
            except Exception as e:
                ffmpeg_logger.error("Error terminating FFmpeg process: %s", e)

            # Close file descriptors
            if self.process.stdout:
                self.process.stdout.close()
            if self.process.stderr:
                self.process.stderr.close()

        ffmpeg_logger.info("FFmpeg process cleaned up.")

        # Clear buffers
        with self.lock:
            self.buffers.clear()

    def stop(self):
        """Stop the FFmpeg process and clean up resources"""
        if self.running:
            self.running = False
            self.cleanup()


class TimeBuffer:
    def __init__(self, duration=60):  # Duration in seconds
        self.duration = duration
        self.buffer = deque()  # Use deque to hold (timestamp, chunk) tuples
        self.lock = threading.Lock()

    def append(self, chunk):
        current_time = time.time()
        with self.lock:
            # Append the current time and chunk to the buffer
            self.buffer.append((current_time, chunk))
            buffer_logger.debug("[Buffer] Appending chunk at time %f", current_time)

            # Remove chunks older than the specified duration
            while self.buffer and (current_time - self.buffer[0][0]) > self.duration:
                buffer_logger.info(
                    "[Buffer] Removing chunk older than %d seconds", self.duration
                )
                self.buffer.popleft()  # Remove oldest chunk

    def read(self):
        with self.lock:
            if self.buffer:
                # Return the oldest chunk
                return self.buffer.popleft()[1]  # Return the chunk, not the timestamp
            return b""  # Return empty bytes if no data


class Cache:
    def __init__(self, ttl=3600):
        self.cache = {}
        self.expiration_times = {}
        self._lock = asyncio.Lock()
        self.ttl = ttl
        self.max_size = 100  # Limit cache size to prevent memory issues

    async def _cleanup_expired_items(self):
        current_time = time.time()
        expired_keys = [
            k for k, exp in self.expiration_times.items() if current_time > exp
        ]
        for k in expired_keys:
            if isinstance(self.cache.get(k), FFmpegStream):
                try:
                    self.cache[k].stop()
                except Exception:
                    pass
            self.cache.pop(k, None)
            self.expiration_times.pop(k, None)
        return len(expired_keys)

    async def get(self, key):
        async with self._lock:
            if key in self.cache and time.time() <= self.expiration_times.get(key, 0):
                # Access refreshes TTL
                self.expiration_times[key] = time.time() + self.ttl
                return self.cache[key]
            return None

    async def set(self, key, value, expiration_time=None):
        async with self._lock:
            await self._cleanup_expired_items()
            if len(self.cache) >= self.max_size and self.expiration_times:
                oldest_key = min(self.expiration_times.items(), key=lambda x: x[1])[0]
                if isinstance(self.cache.get(oldest_key), FFmpegStream):
                    try:
                        self.cache[oldest_key].stop()
                    except Exception:
                        pass
                self.cache.pop(oldest_key, None)
                self.expiration_times.pop(oldest_key, None)
            ttl = expiration_time if expiration_time is not None else self.ttl
            self.cache[key] = value
            self.expiration_times[key] = time.time() + ttl

    async def exists(self, key):
        async with self._lock:
            await self._cleanup_expired_items()
            return key in self.cache

    async def evict_expired_items(self):
        async with self._lock:
            return await self._cleanup_expired_items()


# Global cache instance (short default TTL for HLS segments)
cache = Cache(ttl=120)


async def periodic_cache_cleanup():
    while True:
        try:
            evicted_count = await cache.evict_expired_items()
            if evicted_count > 0:
                proxy_logger.info("Cache cleanup: evicted %s expired items", evicted_count)
            try:
                import psutil
                process = psutil.Process()
                memory_info = process.memory_info()
                proxy_logger.debug("Current memory usage: %.2f MB", memory_info.rss / (1024 * 1024))
            except Exception:
                pass
        except Exception as e:
            proxy_logger.error("Error during cache cleanup: %s", e)
        await asyncio.sleep(60)


@blueprint.record_once
def _register_startup(state):
    app = state.app

    @app.before_serving
    async def _start_periodic_cache_cleanup():
        asyncio.create_task(periodic_cache_cleanup())


async def prefetch_segments(segment_urls, headers=None):
    async with aiohttp.ClientSession() as session:
        for url in segment_urls:
            if not await cache.exists(url):
                proxy_logger.info("[CACHE] Saved URL '%s' to cache", url)
                try:
                    async with session.get(url, headers=headers) as resp:
                        if resp.status == 200:
                            content = await resp.read()
                            await cache.set(
                                url, content, expiration_time=30
                            )  # Cache for 30 seconds
                except Exception as e:
                    proxy_logger.error("Failed to prefetch URL '%s': %s", url, e)


def _normalize_prefix(prefix):
    if not prefix or prefix == "/":
        return ""
    return "/" + prefix.strip("/")


def generate_base64_encoded_url(url_to_encode, extension, request_base_url=None):
    full_url_encoded = base64.urlsafe_b64encode(url_to_encode.encode("utf-8")).decode("utf-8")
    host_base_url = ""
    host_base_url_prefix = "http"
    host_base_url_port = ""
    prefix_path = _normalize_prefix(hls_proxy_prefix)
    if hls_proxy_port:
        if hls_proxy_port == "443":
            host_base_url_prefix = "https"
        host_base_url_port = f":{hls_proxy_port}"
    if hls_proxy_host_ip:
        host_base_url = f"{host_base_url_prefix}://{hls_proxy_host_ip}{host_base_url_port}"
    elif request_base_url:
        host_base_url = request_base_url.rstrip("/")

    if host_base_url:
        if prefix_path:
            host_base_url = f"{host_base_url}{prefix_path}"
        host_base_url = host_base_url.rstrip("/") + "/"

    return f"{host_base_url}{full_url_encoded}.{extension}"


def _decode_base64_url(encoded_url):
    padded = encoded_url + ("=" * (-len(encoded_url) % 4))
    try:
        return base64.urlsafe_b64decode(padded).decode("utf-8")
    except Exception:
        return base64.b64decode(padded).decode("utf-8")


def _infer_extension(url_value):
    parsed = urlparse(url_value)
    path = (parsed.path or "").lower()
    if path.endswith(".m3u8"):
        return "m3u8"
    if path.endswith(".key"):
        return "key"
    if path.endswith(".vtt"):
        return "vtt"
    return "ts"


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


def _rewrite_uri_value(uri_value, source_url, request_base_url=None, forced_extension=None):
    absolute_url = urljoin(source_url, uri_value)
    extension = forced_extension or _infer_extension(absolute_url)
    return (
        generate_base64_encoded_url(absolute_url, extension, request_base_url=request_base_url),
        absolute_url,
        extension,
    )


def rewrite_playlist_line(line, source_url, state, request_base_url=None):
    stripped_line = line.strip()
    if not stripped_line:
        return None, []

    segment_urls = []
    if stripped_line.startswith("#"):
        upper_line = stripped_line.upper()
        if upper_line.startswith("#EXT-X-STREAM-INF"):
            state["next_is_playlist"] = True
        elif upper_line.startswith("#EXTINF"):
            state["next_is_segment"] = True

        def replace_uri(match):
            original_uri = match.group(1)
            forced_extension = None
            if "#EXT-X-KEY" in upper_line:
                forced_extension = "key"
            elif "#EXT-X-MEDIA" in upper_line or "#EXT-X-I-FRAME-STREAM-INF" in upper_line:
                forced_extension = "m3u8"
            new_uri, absolute_url, extension = _rewrite_uri_value(
                original_uri,
                source_url,
                request_base_url=request_base_url,
                forced_extension=forced_extension,
            )
            if extension in ("ts", "vtt", "key"):
                segment_urls.append(absolute_url)
            return f'URI="{new_uri}"'

        updated_line = re.sub(r'URI="([^"]+)"', replace_uri, line)
        return updated_line, segment_urls

    absolute_url = urljoin(source_url, stripped_line)
    if state.get("next_is_playlist"):
        extension = "m3u8"
        state["next_is_playlist"] = False
        state["next_is_segment"] = False
    elif state.get("next_is_segment"):
        extension = "ts"
        state["next_is_segment"] = False
    else:
        extension = _infer_extension(absolute_url)
    if extension in ("ts", "vtt", "key"):
        segment_urls.append(absolute_url)
    return (
        generate_base64_encoded_url(absolute_url, extension, request_base_url=request_base_url),
        segment_urls,
    )


async def fetch_and_update_playlist(decoded_url, request_base_url=None, headers=None):
    async with aiohttp.ClientSession() as session:
        async with session.get(decoded_url, headers=headers) as resp:
            if resp.status != 200:
                return None

            # Get actual URL after any redirects
            response_url = str(resp.url)

            # Read the original playlist content
            playlist_content = await resp.text()

            # Update child URLs in the playlist
            updated_playlist = update_child_urls(
                playlist_content,
                response_url,
                request_base_url=request_base_url,
                headers=headers,
            )
            return updated_playlist


async def stream_updated_playlist(decoded_url, request_base_url=None, headers=None):
    session = aiohttp.ClientSession()
    try:
        resp = await session.get(decoded_url, headers=headers)
        if resp.status != 200:
            await resp.release()
            await session.close()
            return None, resp.status

        response_url = str(resp.url)

        async def generate():
            buffer = ""
            segment_urls = []
            batch_size = 50
            state = {
                "next_is_playlist": False,
                "next_is_segment": False,
            }
            try:
                async for chunk in resp.content.iter_chunked(8192):
                    buffer += chunk.decode("utf-8", errors="ignore")
                    while "\n" in buffer:
                        line, buffer = buffer.split("\n", 1)
                        updated_line, new_segment_urls = rewrite_playlist_line(
                            line,
                            response_url,
                            state,
                            request_base_url=request_base_url,
                        )
                        if updated_line:
                            yield updated_line + "\n"
                        if new_segment_urls:
                            segment_urls.extend(new_segment_urls)
                            if len(segment_urls) >= batch_size:
                                asyncio.create_task(prefetch_segments(segment_urls, headers=headers))
                                segment_urls = []
                if buffer:
                    updated_line, new_segment_urls = rewrite_playlist_line(
                        buffer,
                        response_url,
                        state,
                        request_base_url=request_base_url,
                    )
                    if updated_line:
                        yield updated_line + "\n"
                    if new_segment_urls:
                        segment_urls.extend(new_segment_urls)
            finally:
                if segment_urls:
                    asyncio.create_task(prefetch_segments(segment_urls, headers=headers))
                await resp.release()
                await session.close()

        return generate(), 200
    except Exception:
        await session.close()
        raise


def _detect_playlist_content_type(playlist_content):
    if "#EXT-X-" in playlist_content:
        return "application/vnd.apple.mpegurl", None
    return "audio/x-mpegurl", "inline; filename=\"playlist.m3u\""


async def get_playlist_response(decoded_url, request_base_url=None, headers=None):
    max_buffer_bytes = int(os.environ.get("HLS_PROXY_MAX_BUFFER_BYTES", "1048576"))
    session = aiohttp.ClientSession()
    try:
        resp = await session.get(decoded_url, headers=headers)
        if resp.status != 200:
            await resp.release()
            await session.close()
            return None, resp.status, None, None

        upstream_content_type = resp.headers.get("Content-Type")
        content_length = resp.headers.get("Content-Length")
        if content_length is not None:
            try:
                if int(content_length) <= max_buffer_bytes:
                    response_url = str(resp.url)
                    playlist_content = await resp.text()
                    await resp.release()
                    await session.close()
                    updated_playlist = update_child_urls(
                        playlist_content,
                        response_url,
                        request_base_url=request_base_url,
                        headers=headers,
                    )
                    if upstream_content_type:
                        content_type, disposition = upstream_content_type, None
                    else:
                        content_type, disposition = _detect_playlist_content_type(playlist_content)
                    headers = {}
                    if disposition:
                        headers["Content-Disposition"] = disposition
                    return updated_playlist, 200, content_type, headers
            except ValueError:
                pass

        await resp.release()
        await session.close()
        stream, status_code = await stream_updated_playlist(
            decoded_url,
            request_base_url=request_base_url,
            headers=headers,
        )
        content_type = upstream_content_type or "application/vnd.apple.mpegurl"
        return stream, status_code, content_type, None
    except Exception:
        await session.close()
        raise


def get_key_uri_from_ext_x_key(line):
    """
    Extract the URI value from an #EXT-X-KEY line.
    """
    parts = line.split(",")
    for part in parts:
        if part.startswith("URI="):
            return part.split("=", 1)[1].strip("\"")
    return None


def update_child_urls(playlist_content, source_url, request_base_url=None, headers=None):
    proxy_logger.debug(f"Original Playlist Content:\n{playlist_content}")

    updated_lines = []
    lines = playlist_content.splitlines()
    segment_urls = []
    state = {
        "next_is_playlist": False,
        "next_is_segment": False,
    }

    for line in lines:
        updated_line, new_segment_urls = rewrite_playlist_line(
            line,
            source_url,
            state,
            request_base_url=request_base_url,
        )
        if updated_line:
            updated_lines.append(updated_line)
        if new_segment_urls:
            segment_urls.extend(new_segment_urls)

    # Start prefetching segments in the background
    asyncio.create_task(prefetch_segments(segment_urls, headers=headers))

    # Join the updated lines into a single string
    modified_playlist = "\n".join(updated_lines)
    proxy_logger.debug(f"Modified Playlist Content:\n{modified_playlist}")
    return modified_playlist


@blueprint.route(f"{hls_proxy_prefix.lstrip('/')}/<encoded_url>.m3u8", methods=["GET"])
async def proxy_m3u8(encoded_url):
    # Decode the Base64 encoded URL
    try:
        decoded_url = _decode_base64_url(encoded_url)
    except Exception:
        proxy_logger.error("Invalid base64 URL: %s", encoded_url)
        return Response("Invalid base64 URL", status=400)

    headers = _build_upstream_headers()
    result = await get_playlist_response(
        decoded_url,
        request_base_url=request.host_url,
        headers=headers,
    )
    if result is None:
        proxy_logger.error("Failed to fetch the original playlist '%s'", decoded_url)
        return Response("Failed to fetch the original playlist.", status=404)
    body, status_code, content_type, headers = result
    if body is None:
        proxy_logger.error("Failed to fetch the original playlist '%s'", decoded_url)
        return Response("Failed to fetch the original playlist.", status=404)

    proxy_logger.info("[MISS] Serving m3u8 URL '%s' without cache", decoded_url)
    return Response(body, content_type=content_type or "application/vnd.apple.mpegurl", status=status_code, headers=headers)


@blueprint.route("/proxy.m3u8", methods=["GET"])
async def proxy_m3u8_url():
    source_url = request.args.get("url")
    if not source_url:
        return Response("Missing url parameter", status=400)

    encoded = base64.urlsafe_b64encode(source_url.encode("utf-8")).decode("utf-8")
    base_url = request.host_url.rstrip("/")
    prefix_path = _normalize_prefix(hls_proxy_prefix)
    if prefix_path:
        base_url = f"{base_url}{prefix_path}"
    target = f"{base_url.rstrip('/')}/{encoded}.m3u8"
    proxy_logger.info("[MISS] Redirecting playlist URL '%s' -> '%s'", source_url, target)
    return redirect(target, code=302)


@blueprint.route(f"{hls_proxy_prefix.lstrip('/')}/<encoded_url>.key", methods=["GET"])
async def proxy_key(encoded_url):
    # Decode the Base64 encoded URL
    try:
        decoded_url = _decode_base64_url(encoded_url)
    except Exception:
        proxy_logger.error("Invalid base64 URL: %s", encoded_url)
        return Response("Invalid base64 URL", status=400)

    # Check if the .key file is already cached
    if await cache.exists(decoded_url):
        proxy_logger.info(f"[HIT] Serving key URL from cache: %s", decoded_url)
        cached_content = await cache.get(decoded_url)
        return Response(cached_content, content_type="application/octet-stream")

    # If not cached, fetch the file and cache it
    proxy_logger.info(f"[MISS] Serving key URL '%s' without cache", decoded_url)
    headers = _build_upstream_headers()
    async with aiohttp.ClientSession() as session:
        async with session.get(decoded_url, headers=headers) as resp:
            if resp.status != 200:
                proxy_logger.error("Failed to fetch key file '%s'", decoded_url)
                return Response("Failed to fetch the file.", status=404)
            content = await resp.read()
            await cache.set(
                decoded_url, content, expiration_time=30
            )  # Cache for 30 seconds
            return Response(content, content_type="application/octet-stream")


@blueprint.route(f"{hls_proxy_prefix.lstrip('/')}/<encoded_url>.ts", methods=["GET"])
async def proxy_ts(encoded_url):
    # Decode the Base64 encoded URL
    try:
        decoded_url = _decode_base64_url(encoded_url)
    except Exception:
        proxy_logger.error("Invalid base64 URL: %s", encoded_url)
        return Response("Invalid base64 URL", status=400)

    # Check if the .ts file is already cached
    if await cache.exists(decoded_url):
        proxy_logger.info(f"[HIT] Serving ts URL from cache: %s", decoded_url)
        cached_content = await cache.get(decoded_url)
        return Response(cached_content, content_type="video/mp2t")

    # If not cached, fetch the file and cache it
    proxy_logger.info("[MISS] Serving ts URL '%s' without cache", decoded_url)
    headers = _build_upstream_headers()
    async with aiohttp.ClientSession() as session:
        async with session.get(decoded_url, headers=headers) as resp:
            if resp.status != 200:
                proxy_logger.error("Failed to fetch ts file '%s'", decoded_url)
                return Response("Failed to fetch the file.", status=404)
            content = await resp.read()
            content_type = (resp.headers.get("Content-Type") or "").lower()

            # If upstream is actually a playlist, redirect to the .m3u8 endpoint.
            # This handles cases where the proxy URL was built with a .ts suffix
            # but the upstream URL is a master/variant playlist.
            if "mpegurl" in content_type or content.lstrip().startswith(b"#EXTM3U"):
                target = f"{hls_proxy_prefix.rstrip('/')}/{encoded_url}.m3u8"
                if request.query_string:
                    target = f"{target}?{request.query_string.decode()}"
                return redirect(target, code=302)

            await cache.set(
                decoded_url, content, expiration_time=30
            )  # Cache for 30 seconds
            return Response(content, content_type="video/mp2t")


@blueprint.route(f"{hls_proxy_prefix.lstrip('/')}/<encoded_url>.vtt", methods=["GET"])
async def proxy_vtt(encoded_url):
    # Decode the Base64 encoded URL
    try:
        decoded_url = _decode_base64_url(encoded_url)
    except Exception:
        proxy_logger.error("Invalid base64 URL: %s", encoded_url)
        return Response("Invalid base64 URL", status=400)

    # Check if the .vtt file is already cached
    if await cache.exists(decoded_url):
        proxy_logger.info("[HIT] Serving vtt URL from cache: %s", decoded_url)
        cached_content = await cache.get(decoded_url)
        return Response(cached_content, content_type="text/vtt")

    # If not cached, fetch the file and cache it
    proxy_logger.info("[MISS] Serving vtt URL '%s' without cache", decoded_url)
    headers = _build_upstream_headers()
    async with aiohttp.ClientSession() as session:
        async with session.get(decoded_url, headers=headers) as resp:
            if resp.status != 200:
                proxy_logger.error("Failed to fetch vtt file '%s'", decoded_url)
                return Response("Failed to fetch the file.", status=404)
            content = await resp.read()
            await cache.set(
                decoded_url, content, expiration_time=30
            )  # Cache for 30 seconds
            return Response(content, content_type="text/vtt")


@blueprint.route(f"{hls_proxy_prefix.lstrip('/')}/stream/<encoded_url>", methods=["GET"])
async def stream_ts(encoded_url):
    # Decode the Base64 encoded URL
    try:
        decoded_url = _decode_base64_url(encoded_url)
    except Exception:
        proxy_logger.error("Invalid base64 URL: %s", encoded_url)
        return Response("Invalid base64 URL", status=400)

    # Generate a unique identifier (UUID) for the connection
    connection_id = str(uuid.uuid4())  # Use a UUID for the connection ID

    # Check if the stream is active and has connections
    if decoded_url not in active_streams or not active_streams[decoded_url].running or active_streams[
            decoded_url].connection_count == 0:
        buffer_logger.info("Creating new FFmpeg stream with connection ID %s.", connection_id)
        # Create a new stream if it does not exist or if there are no connections
        stream = FFmpegStream(decoded_url)
        active_streams[decoded_url] = stream
    else:
        buffer_logger.info("Connecting to existing FFmpeg stream with connection ID %s.", connection_id)

    # Get the existing stream
    stream = active_streams[decoded_url]
    stream.last_activity = time.time()  # Update last activity time

    # Add a new buffer for this connection
    stream.add_buffer(connection_id)

    # Create a generator to stream data from the connection-specific buffer
    @stream_with_context
    async def generate():
        try:
            while True:
                # Check if the buffer exists before reading
                if connection_id in stream.buffers:
                    data = stream.buffers[connection_id].read()
                    if data:
                        yield data
                    else:
                        # Check if FFmpeg is still running
                        if not stream.running:
                            buffer_logger.info("FFmpeg has stopped, closing stream.")
                            break
                        # Sleep briefly if no data is available
                        await asyncio.sleep(0.1)  # Wait before checking again
                else:
                    # If the buffer doesn't exist, break the loop
                    break
        finally:
            stream.remove_buffer(connection_id)  # Remove the buffer on connection close
            # Stop logging is handled by inactivity cleanup to avoid per-connection spam.

    # Create a response object with the correct content type and set timeout to None
    response = Response(generate(), content_type="video/mp2t")
    response.timeout = None  # Disable timeout for streaming response
    return response
