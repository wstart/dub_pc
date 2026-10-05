"""Real Mac profile acceptance; removes only its own temporary environment."""
from __future__ import annotations

import json
import shlex
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from taskapp.browser import AdsPower, RemoteBrowser


AUDIT = """async () => {
    const hints = navigator.userAgentData;
    const canvas = document.createElement('canvas');
    const gl = canvas.getContext('webgl');
    const debug = gl && gl.getExtension('WEBGL_debug_renderer_info');
    return {
        userAgent: navigator.userAgent, platform: navigator.platform,
        mobile: hints && hints.mobile, clientPlatform: hints && hints.platform,
        highEntropy: hints && await hints.getHighEntropyValues(
            ['architecture', 'bitness', 'platformVersion', 'fullVersionList']),
        headers: await (await fetch('/headers')).json(),
        languages: navigator.languages,
        timezone: Intl.DateTimeFormat().resolvedOptions().timeZone,
        screen: {width: screen.width, height: screen.height},
        maxTouchPoints: navigator.maxTouchPoints,
        webglVendor: debug && gl.getParameter(debug.UNMASKED_VENDOR_WEBGL),
        webglRenderer: debug && gl.getParameter(debug.UNMASKED_RENDERER_WEBGL)
    };
}"""


def audit(remote: RemoteBrowser) -> dict:
    value = remote.page.evaluate(AUDIT)
    assert 'Macintosh' in value['userAgent'], 'UA is not Mac desktop'
    assert 'Chrome/152.' in value['userAgent'], 'UA version does not match selected kernel'
    assert 'Linux' not in value['userAgent'] and 'HeadlessChrome' not in value['userAgent']
    assert value['platform'] == 'MacIntel', 'JavaScript platform is not Mac'
    assert value['clientPlatform'] == 'macOS', 'Client Hints platform is not Mac'
    assert value['mobile'] is False and value['maxTouchPoints'] == 0
    assert value['headers']['user-agent'] == value['userAgent'], 'HTTP/JavaScript UA mismatch'
    assert value['headers']['sec-ch-ua-platform'] == '"macOS"', 'HTTP platform mismatch'
    assert value['headers']['sec-ch-ua-mobile'] == '?0'
    versions = value['highEntropy']['fullVersionList']
    assert any(item['brand'] in {'Chromium', 'Google Chrome'}
               and item['version'].startswith('152.') for item in versions)
    product = remote.cdp_session().send('Browser.getVersion')['product']
    assert product.split('/')[-1].startswith('152.'), 'Actual kernel version mismatch'
    return value


def main():
    config = {}
    for line in Path('/etc/adspower-headless.env').read_text().splitlines():
        key, separator, value = line.strip().partition('=')
        if separator and key == 'ADSPOWER_API_KEY':
            config[key] = shlex.split(value)[0]
    ads = AdsPower('http://127.0.0.1:50325', config['ADSPOWER_API_KEY'])

    class Fixture(BaseHTTPRequestHandler):
        def do_GET(self):
            if self.path == '/headers':
                body = json.dumps({key: self.headers.get(key) for key in (
                    'user-agent', 'sec-ch-ua-platform', 'sec-ch-ua-mobile')}).encode()
                content_type = 'application/json'
            else:
                body = """<!doctype html><meta charset=utf-8><h1>Mac 独立验收</h1>
                    <input id=field value="中文 abc"><output id=count>0</output>
                    <button id=button onclick="count.textContent=Number(count.textContent)+1">点击</button>
                    """.encode()
                content_type = 'text/html; charset=utf-8'
            self.send_response(200)
            self.send_header('Content-Type', content_type)
            self.send_header('Content-Length', str(len(body)))
            self.send_header('Cache-Control', 'no-store')
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass

    fixture = ThreadingHTTPServer(('127.0.0.1', 0), Fixture)
    threading.Thread(target=fixture.serve_forever, daemon=True).start()
    origin = f'http://127.0.0.1:{fixture.server_port}'
    profile = remote = None
    try:
        profile = ads.create({'code': 'MAC-SMOKE-' + str(time.time_ns()), 'start_url': origin + '/'})
        remote = RemoteBrowser(ads.start(profile), origin + '/')
        before = audit(remote)
        print('MAC_FINGERPRINT_OK', json.dumps(before, ensure_ascii=False), flush=True)
        field = remote.page.locator('#field')
        field.click()
        remote.input({'type': 'key', 'key': 'Control+a'})
        remote.input({'type': 'key', 'key': 'Backspace'})
        assert field.input_value() == '', 'Remote clear failed in Mac profile'
        remote.input({'type': 'text', 'text': 'Mac 中文输入'})
        assert field.input_value() == 'Mac 中文输入'
        box = remote.page.locator('#button').bounding_box()
        remote.input({'type': 'click', 'x': box['x'] + 5, 'y': box['y'] + 5, 'button': 'left'})
        assert remote.page.locator('#count').inner_text() == '1'
        frames = []
        remote.start_frames(frames.append)
        deadline = time.monotonic() + 5
        while not frames and time.monotonic() < deadline:
            remote.pump_frames()
        assert frames and frames[-1].startswith(b'\xff\xd8'), 'Live JPEG frame missing'
        original = remote.page
        remote.input({'type': 'new_tab'})
        new_id = next(tab['id'] for tab in remote.tab_state() if tab['active'])
        remote.input({'type': 'close_tab', 'id': new_id})
        assert remote.page is original, 'Closing a tab did not restore the original page'
        remote.context.add_cookies([{'name': 'owned_mac_cookie', 'value': 'owned-fixture',
                                    'url': origin, 'expires': time.time() + 3600, 'httpOnly': True}])
        saved_tabs = remote.tab_state()
        remote.close()
        remote = None
        ads.stop(profile)
        remote = RemoteBrowser(ads.start(profile, restore_tabs=True), None)
        remote.restore_tabs(saved_tabs, origin + '/')
        page = next((page for page in remote.pages() if page.url.startswith(origin)), None)
        assert page is not None, 'Original fixture tab was not restored'
        remote.page = page
        after = audit(remote)
        for key in ('userAgent', 'platform', 'clientPlatform', 'highEntropy'):
            assert after[key] == before[key], key + ' changed after reopening'
        assert any(cookie['name'] == 'owned_mac_cookie' and cookie['value'] == 'owned-fixture'
                   and cookie['httpOnly'] for cookie in remote.cookies()), 'Fixture Cookie was lost'
        print('MAC_OPERATIONS_OK: Chinese, clear, mouse, live frame, close tab', flush=True)
        print('MAC_REOPEN_OK: same environment, fingerprint and Cookie retained', flush=True)
    finally:
        try:
            if remote:
                remote.close()
            if profile:
                try:
                    ads.stop(profile)
                finally:
                    ads.delete(profile)
                print('OWN_MAC_TEST_PROFILE_REMOVED', flush=True)
        finally:
            fixture.shutdown()
            fixture.server_close()


if __name__ == '__main__':
    main()
