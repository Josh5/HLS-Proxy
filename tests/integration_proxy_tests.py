#!/usr/bin/env python3
import base64
import json
import os
import urllib.request
from urllib.parse import urlparse


PROXY_BASE_URL = os.environ.get("HLS_PROXY_BASE_URL", "http://localhost:9987").rstrip("/")
TEST_URLS_FILE = os.environ.get("HLS_PROXY_TEST_URLS_FILE", os.path.join("tests", "test-urls.json"))
def _http_get(url):
    req = urllib.request.Request(url, headers={"User-Agent": "hls-proxy-test"})
    with urllib.request.urlopen(req, timeout=10) as resp:
        body = resp.read()
        headers = dict(resp.headers)
        return body.decode("utf-8", errors="replace"), headers


def _extract_urls(text):
    return [
        line.strip()
        for line in text.splitlines()
        if line.strip() and not line.strip().startswith("#")
    ]


def _decode_proxy_url(proxied_url):
    parsed = urlparse(proxied_url)
    filename = parsed.path.split("/")[-1]
    encoded = filename.split(".", 1)[0]
    padded = encoded + ("=" * (-len(encoded) % 4))
    return base64.urlsafe_b64decode(padded).decode("utf-8")


def _assert(condition, message):
    if not condition:
        raise AssertionError(message)


def _load_test_urls():
    if not os.path.exists(TEST_URLS_FILE):
        raise FileNotFoundError(
            f"Missing {TEST_URLS_FILE}. Provide a JSON file with a list of playlist URLs."
        )
    with open(TEST_URLS_FILE, "r", encoding="utf-8") as handle:
        payload = json.load(handle)
    if not isinstance(payload, dict) or "urls" not in payload:
        raise ValueError("test-urls.json must be a JSON object with a 'urls' array")
    urls = payload["urls"]
    if not isinstance(urls, list) or not urls:
        raise ValueError("'urls' must be a non-empty list")
    return urls


def _assert_rewrite(proxy_url):
    body, _headers = _http_get(proxy_url)
    urls = _extract_urls(body)
    _assert(len(urls) > 0, "Expected at least one URL in rewritten playlist")
    for proxied in urls:
        decoded = _decode_proxy_url(proxied)
        _assert(decoded.startswith("http"), f"Decoded URL is not absolute: {decoded}")


def run():
    urls = _load_test_urls()
    for url in urls:
        print(f"[TEST] {url}")
        proxy_url = f"{PROXY_BASE_URL}/proxy.m3u8?url={url}"
        _assert_rewrite(proxy_url)
    print("All integration tests passed.")


if __name__ == "__main__":
    run()
