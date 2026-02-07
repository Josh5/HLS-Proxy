#!/usr/bin/env python3
# -*- coding:utf-8 -*-
import asyncio
import base64
import logging
import os
import subprocess
import threading
import time
import uuid
from collections import deque

from quart import current_app, Response, stream_with_context, request

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

hls_proxy_prefix = os.environ.get('HLS_PROXY_PREFIX', '/')
if not hls_proxy_prefix.startswith("/"):
    hls_proxy_prefix = "/" + hls_proxy_prefix

hls_proxy_host_ip = os.environ.get('HLS_PROXY_HOST_IP')
hls_proxy_port = os.environ.get('HLS_PROXY_PORT')

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
        self.last_activity = time.time()
        self.thread.start()

    def run_ffmpeg(self):
        command = [
            'ffmpeg', '-hide_banner', '-loglevel', 'info', '-err_detect', 'ignore_err',
            '-probesize', '20M', '-analyzeduration', '0', '-fpsprobesize', '0',
            '-i', self.decoded_url,
            '-c', 'copy',
            '-f', 'mpegts', 'pipe:1'
        ]
        ffmpeg_logger.info("Executing FFmpeg with command: %s", command)
        self.process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE)

        # Start a thread to log stderr
        stderr_thread = threading.Thread(target=self.log_stderr)
        stderr_thread.daemon = True
        stderr_thread.start()

        chunk_size = 65536  # Read 64 KB at a time
        while self.running:
            try:
                import select
                ready, _, _ = select.select([self.process.stdout], [], [], 1.0)
                if not ready:
                    if time.time() - self.last_activity > 300:
                        ffmpeg_logger.info("No activity for 5 minutes, terminating FFmpeg stream")
                        self.stop()
                        break
                    continue

                chunk = self.process.stdout.read(chunk_size)
                if not chunk:
                    ffmpeg_logger.warning("FFmpeg has finished streaming.")
                    break

                self.last_activity = time.time()
                with self.lock:
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
                ffmpeg_logger.debug("FFmpeg: %s", line.decode('utf-8', errors='replace').strip())
            except Exception as e:
                ffmpeg_logger.error("Error reading stderr: %s", e)
                break

    def increment_connection(self):
        with self.lock:
            self.connection_count += 1
            ffmpeg_logger.info("Adding new connection. Connection count now at %s", self.connection_count)

    def decrement_connection(self):
        with self.lock:
            self.connection_count -= 1
            ffmpeg_logger.info("Removing connection. Connection count now at %s", self.connection_count)

            # Do not stop FFmpeg until all connections are closed
            if self.connection_count == 0:
                ffmpeg_logger.info("No more active connections. Stopping FFmpeg.")
                threading.Thread(target=self.stop).start()

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
        self.running = False
        if self.process:
            try:
                self.process.terminate()
                try:
                    self.process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    self.process.kill()
                    self.process.wait()
            except Exception as e:
                ffmpeg_logger.error("Error terminating FFmpeg process: %s", e)

            if self.process.stdout:
                self.process.stdout.close()
            if self.process.stderr:
                self.process.stderr.close()

        ffmpeg_logger.info("FFmpeg process cleaned up.")
        with self.lock:
            self.buffers.clear()

    def stop(self):
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
                buffer_logger.info("[Buffer] Removing chunk older than %d seconds", self.duration)
                self.buffer.popleft()  # Remove oldest chunk

    def read(self):
        with self.lock:
            if self.buffer:
                # Return the oldest chunk
                return self.buffer.popleft()[1]  # Return the chunk, not the timestamp
            return b''  # Return empty bytes if no data


class Cache:
    def __init__(self, ttl=3600):
        self.cache = {}
        self.expiration_times = {}
        self._lock = asyncio.Lock()
        self.ttl = ttl
        self.max_size = 100

    async def _cleanup_expired_items(self):
        current_time = time.time()
        expired_keys = [k for k, exp in self.expiration_times.items() if current_time > exp]
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


async def prefetch_segments(segment_urls):
    async with aiohttp.ClientSession() as session:
        for url in segment_urls:
            if not await cache.exists(url):
                proxy_logger.info("[CACHE] Saved URL '%s' to cache", url)
                try:
                    async with session.get(url) as resp:
                        if resp.status == 200:
                            content = await resp.read()
                            await cache.set(url, content, expiration_time=30)  # Cache for 30 seconds
                except Exception as e:
                    proxy_logger.error("Failed to prefetch URL '%s': %s", url, e)


def _normalize_prefix(prefix):
    if not prefix or prefix == "/":
        return ""
    return "/" + prefix.strip("/")


def generate_base64_encoded_url(url_to_encode, extension, request_base_url=None):
    full_url_encoded = base64.b64encode(url_to_encode.encode('utf-8')).decode('utf-8')
    host_base_url = ''
    host_base_url_prefix = 'http'
    host_base_url_port = ''
    prefix_path = _normalize_prefix(hls_proxy_prefix)
    if hls_proxy_port:
        if hls_proxy_port == '443':
            host_base_url_prefix = 'https'
        host_base_url_port = f':{hls_proxy_port}'
    if hls_proxy_host_ip:
        host_base_url = f'{host_base_url_prefix}://{hls_proxy_host_ip}{host_base_url_port}{prefix_path}'
    elif request_base_url:
        host_base_url = f'{request_base_url.rstrip("/")}{prefix_path}'
    return f'{host_base_url}{full_url_encoded}.{extension}'


async def fetch_and_update_playlist(decoded_url, request_base_url=None):
    async with aiohttp.ClientSession() as session:
        async with session.get(decoded_url) as resp:
            if resp.status != 200:
                return None

            # Get actual URL after any redirects
            response_url = str(resp.url)

            # Read the original playlist content
            playlist_content = await resp.text()

            # Update child URLs in the playlist
            updated_playlist = update_child_urls(playlist_content, response_url, request_base_url=request_base_url)
            return updated_playlist


def get_key_uri_from_ext_x_key(line):
    """
    Extract the URI value from an #EXT-X-KEY line.
    """
    parts = line.split(',')
    for part in parts:
        if part.startswith('URI='):
            return part.split('=', 1)[1].strip('"')
    return None


def update_child_urls(playlist_content, source_url, request_base_url=None):
    proxy_logger.debug(f"Original Playlist Content:\n{playlist_content}")

    updated_lines = []
    lines = playlist_content.splitlines()
    segment_urls = []

    for line in lines:
        stripped_line = line.strip()

        # Ignore empty lines
        if not stripped_line:
            continue

        # Lines starting with # are preserved as is
        if line.startswith("#EXT-X-KEY"):
            key_uri = get_key_uri_from_ext_x_key(line)
            if key_uri:
                absolute_key_uri = urljoin(source_url, key_uri)
                new_key_uri = generate_base64_encoded_url(absolute_key_uri, 'key', request_base_url=request_base_url)
                updated_lines.append(line.replace(key_uri, new_key_uri))
                segment_urls.append(absolute_key_uri)
            else:
                updated_lines.append(line)
            continue
        elif stripped_line.startswith('#'):
            updated_lines.append(line)
            continue

        # Encode regular URL lines
        extension = 'ts'
        if stripped_line.endswith('m3u8'):
            extension = 'm3u8'
        url_to_encode = urljoin(source_url, stripped_line)
        updated_lines.append(generate_base64_encoded_url(url_to_encode, extension, request_base_url=request_base_url))

        # Add any ts files to list of segments to pre-fetch
        if extension == 'ts':
            segment_urls.append(url_to_encode)

    # Start prefetching segments in the background
    asyncio.create_task(prefetch_segments(segment_urls))

    # Join the updated lines into a single string
    modified_playlist = "\n".join(updated_lines)
    proxy_logger.debug(f"Modified Playlist Content:\n{modified_playlist}")
    return modified_playlist


@blueprint.route(f'{hls_proxy_prefix.lstrip("/")}/<encoded_url>.m3u8', methods=['GET'])
async def proxy_m3u8(encoded_url):
    # Decode the Base64 encoded URL
    decoded_url = base64.b64decode(encoded_url).decode('utf-8')

    updated_playlist = await fetch_and_update_playlist(decoded_url, request_base_url=request.host_url)
    if updated_playlist is None:
        proxy_logger.error("Failed to fetch the original playlist '%s'", decoded_url)
        return Response("Failed to fetch the original playlist.", status=404)

    proxy_logger.info(f"[MISS] Serving m3u8 URL '%s' without cache", decoded_url)
    return Response(updated_playlist, content_type='application/vnd.apple.mpegurl')


@blueprint.route('/proxy.m3u8', methods=['GET'])
async def proxy_m3u8_url():
    source_url = request.args.get('url')
    if not source_url:
        return Response("Missing url parameter", status=400)

    updated_playlist = await fetch_and_update_playlist(source_url, request_base_url=request.host_url)
    if updated_playlist is None:
        proxy_logger.error("Failed to fetch the original playlist '%s'", source_url)
        return Response("Failed to fetch the original playlist.", status=404)

    proxy_logger.info(f"[MISS] Serving m3u8 URL '%s' without cache", source_url)
    return Response(updated_playlist, content_type='application/vnd.apple.mpegurl')


@blueprint.route(f'{hls_proxy_prefix.lstrip("/")}/<encoded_url>.key', methods=['GET'])
async def proxy_key(encoded_url):
    # Decode the Base64 encoded URL
    decoded_url = base64.b64decode(encoded_url).decode('utf-8')

    # Check if the .key file is already cached
    if await cache.exists(decoded_url):
        proxy_logger.info(f"[HIT] Serving key URL from cache: %s", decoded_url)
        cached_content = await cache.get(decoded_url)
        return Response(cached_content, content_type='application/octet-stream')

    # If not cached, fetch the file and cache it
    proxy_logger.info(f"[MISS] Serving key URL '%s' without cache", decoded_url)
    async with aiohttp.ClientSession() as session:
        async with session.get(decoded_url) as resp:
            if resp.status != 200:
                proxy_logger.error("Failed to fetch key file '%s'", decoded_url)
                return Response("Failed to fetch the file.", status=404)
            content = await resp.read()
            await cache.set(decoded_url, content, expiration_time=30)  # Cache for 30 seconds
            return Response(content, content_type='application/octet-stream')


@blueprint.route(f'{hls_proxy_prefix.lstrip("/")}/<encoded_url>.ts', methods=['GET'])
async def proxy_ts(encoded_url):
    # Decode the Base64 encoded URL
    decoded_url = base64.b64decode(encoded_url).decode('utf-8')

    # Check if the .ts file is already cached
    if await cache.exists(decoded_url):
        proxy_logger.info(f"[HIT] Serving ts URL from cache: %s", decoded_url)
        cached_content = await cache.get(decoded_url)
        return Response(cached_content, content_type='video/mp2t')

    # If not cached, fetch the file and cache it
    proxy_logger.info(f"[MISS] Serving ts URL '%s' without cache", decoded_url)
    async with aiohttp.ClientSession() as session:
        async with session.get(decoded_url) as resp:
            if resp.status != 200:
                proxy_logger.error("Failed to fetch ts file '%s'", decoded_url)
                return Response("Failed to fetch the file.", status=404)
            content = await resp.read()
            await cache.set(decoded_url, content, expiration_time=30)  # Cache for 30 seconds
            return Response(content, content_type='video/mp2t')


@blueprint.route(f'{hls_proxy_prefix.lstrip("/")}/stream/<encoded_url>', methods=['GET'])
async def stream_ts(encoded_url):
    # Decode the Base64 encoded URL
    decoded_url = base64.b64decode(encoded_url).decode('utf-8')

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
    stream.increment_connection()
    stream.last_activity = time.time()

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
            stream.decrement_connection()  # Decrement on connection close

    # Create a response object with the correct content type and set timeout to None
    response = Response(generate(), content_type='video/mp2t')
    response.timeout = None  # Disable timeout for streaming response
    return response
