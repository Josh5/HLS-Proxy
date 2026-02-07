import base64
import importlib
import os
import unittest
from urllib.parse import urlparse


def _load_module():
    import backend.api.routes_hls_proxy as mod
    return importlib.reload(mod)


def _decode_proxy_url(proxied_url):
    parsed = urlparse(proxied_url)
    filename = parsed.path.split('/')[-1]
    encoded = filename.split('.', 1)[0]
    return base64.b64decode(encoded).decode('utf-8')


def _fixtures_dir():
    return os.path.abspath(
        os.path.join(os.path.dirname(__file__), "..", "..", "playlist-fixtures")
    )


def _load_fixture(name):
    path = os.path.join(_fixtures_dir(), name)
    with open(path, "r", encoding="utf-8", errors="replace") as handle:
        return handle.read()


def _extract_urls(playlist_text):
    return [
        line.strip()
        for line in playlist_text.splitlines()
        if line.strip() and not line.strip().startswith("#")
    ]


class HlsProxyRewriteTests(unittest.TestCase):
    def setUp(self):
        os.environ.pop('HLS_PROXY_HOST_IP', None)
        os.environ.pop('HLS_PROXY_PORT', None)
        os.environ['HLS_PROXY_PREFIX'] = 'proxy'
        self.mod = _load_module()

    def test_generate_base64_uses_request_host(self):
        url = 'https://example.com/seg.ts'
        proxied = self.mod.generate_base64_encoded_url(url, 'ts', request_base_url='http://proxy.local:9987/')
        self.assertTrue(proxied.startswith('http://proxy.local:9987/proxy/'))
        self.assertEqual(_decode_proxy_url(proxied), url)

    def test_update_child_urls_relative_and_key(self):
        playlist = """#EXTM3U
#EXT-X-VERSION:3
#EXT-X-KEY:METHOD=AES-128,URI=keys/key1.key,IV=0xabcdef
#EXTINF:10,
seg1.ts
#EXTINF:10,
sub/seg2.ts
#EXT-X-STREAM-INF:BANDWIDTH=1280000
variant/playlist.m3u8
"""
        source_url = 'https://origin.example.com/path/master.m3u8'
        updated = self.mod.update_child_urls(playlist, source_url, request_base_url='http://proxy.local:9987/')
        lines = updated.splitlines()

        key_line = next(line for line in lines if line.startswith('#EXT-X-KEY'))
        key_uri = key_line.split('URI=', 1)[1].split(',', 1)[0].strip('"')
        self.assertEqual(
            _decode_proxy_url(key_uri),
            'https://origin.example.com/path/keys/key1.key'
        )

        segment_lines = [line for line in lines if line and not line.startswith('#')]
        self.assertEqual(
            _decode_proxy_url(segment_lines[0]),
            'https://origin.example.com/path/seg1.ts'
        )
        self.assertEqual(
            _decode_proxy_url(segment_lines[1]),
            'https://origin.example.com/path/sub/seg2.ts'
        )
        self.assertEqual(
            _decode_proxy_url(segment_lines[2]),
            'https://origin.example.com/path/variant/playlist.m3u8'
        )

    def test_update_child_urls_absolute_urls(self):
        playlist = """#EXTM3U
#EXTINF:10,
https://cdn.example.com/a.ts
#EXTINF:10,
https://cdn.example.com/b.m3u8
"""
        source_url = 'https://origin.example.com/root/playlist.m3u8'
        updated = self.mod.update_child_urls(playlist, source_url, request_base_url='http://proxy.local:9987/')
        lines = [line for line in updated.splitlines() if line and not line.startswith('#')]
        self.assertEqual(_decode_proxy_url(lines[0]), 'https://cdn.example.com/a.ts')
        self.assertEqual(_decode_proxy_url(lines[1]), 'https://cdn.example.com/b.m3u8')

    def test_fixture_ip_tv_playlists_rewrite(self):
        fixtures = [
            "raw.m3u8",
            "raw-tv.m3u8",
            "playlist.m3u",
        ]
        for fixture in fixtures:
            with self.subTest(fixture=fixture):
                original = _load_fixture(fixture)
                original_urls = _extract_urls(original)
                updated = self.mod.update_child_urls(
                    original,
                    "https://origin.example.com/root/list.m3u8",
                    request_base_url="http://proxy.local:9987/",
                )
                updated_urls = _extract_urls(updated)
                self.assertEqual(len(updated_urls), len(original_urls))
                for expected, proxied in zip(original_urls, updated_urls):
                    self.assertEqual(_decode_proxy_url(proxied), expected)

    def test_fixture_master_playlist_rewrite(self):
        original = _load_fixture("7now-fast.m3u8")
        original_urls = _extract_urls(original)
        updated = self.mod.update_child_urls(
            original,
            "https://origin.example.com/root/master.m3u8",
            request_base_url="http://proxy.local:9987/",
        )
        updated_urls = _extract_urls(updated)
        self.assertEqual(len(updated_urls), len(original_urls))
        self.assertTrue(any(line.startswith("#EXT-X-STREAM-INF") for line in updated.splitlines()))
        for expected, proxied in zip(original_urls, updated_urls):
            self.assertEqual(_decode_proxy_url(proxied), expected)


if __name__ == '__main__':
    unittest.main()
