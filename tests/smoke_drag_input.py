"""Owned slider diagnostics, not a CAPTCHA solver or a production service change."""
from __future__ import annotations

import argparse
import hashlib
import json
import logging
import shlex
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from playwright.sync_api import expect, sync_playwright
from werkzeug.serving import WSGIRequestHandler, make_server

from taskapp import create_app
from taskapp.browser import AdsPower, RemoteBrowser
from tests.smoke_admin_browser import wait
from tests.smoke_input_ui import run_input_cases


def gesture(page, start: tuple[float, float], end: tuple[float, float]) -> None:
    page.mouse.move(*start)
    page.mouse.down()
    try:
        page.mouse.move(*end, steps=16)
    finally:
        page.mouse.up()


def run_lifecycle_cases(app, ui_browser, ui_origin, task, guest, target, manager, store,
                        reset, snapshot, record, errors, ads) -> None:
    """Check press cleanup and permission changes only on the owned task."""
    admin = app.test_client()
    admin.get('/')
    with admin.session_transaction() as session:
        admin_headers = {'X-CSRF-Token': session['csrf']}
    assert admin.post('/api/login', json={'password': 'owned-drag-test-admin'},
                      headers=admin_headers).status_code == 200
    with guest.session_transaction() as session:
        guest_headers = {'X-CSRF-Token': session['csrf']}
    base = '/api/task/' + task['token']
    admin_base = f'/api/admin/tasks/{task["id"]}/browser'

    def page(client, path):
        context = ui_browser.new_context(viewport={'width': 1440, 'height': 1000})
        context.add_cookies([{'name': 'session', 'value': client.get_cookie('session').value,
                             'url': ui_origin}])
        view = context.new_page()
        view.on('pageerror', lambda error: errors.append(str(error)))
        view.goto(ui_origin + path, wait_until='domcontentloaded')
        view.wait_for_function('() => document.getElementById("screen").naturalWidth === 1280')
        return context, view

    def point(view, selector, ratio):
        box, bounds = target.locator(selector).bounding_box(), view.locator('#screen').bounding_box()
        return (bounds['x'] + (box['x'] + box['width'] * ratio) * bounds['width'] / 1280,
                bounds['y'] + (box['y'] + box['height'] / 2) * bounds['height'] / 800)

    guest_context, guest_ui = page(guest, '/t/' + task['token'])
    admin_context, admin_ui = page(admin, f'/admin/tasks/{task["id"]}/browser')
    profile, endpoint_count = store.get(task['id'])['profile_id'], len(ads.endpoints)
    try:
        assert admin_ui.locator('#clear-input').is_disabled()
        assert admin_ui.locator('#task-state').inner_text() == '只读观察'
        reset()
        guest_ui.mouse.move(*point(guest_ui, '#slider', 0.02))
        guest_ui.mouse.down()
        try:
            wait(lambda: snapshot()['active'], 'guest held before observer exit')
            assert admin.post(admin_base + '/close', json={}, headers=admin_headers).status_code == 200
            # API exit avoids deliberately blurring the guest page in this specific check.
            admin_ui.wait_for_function(
                '() => document.getElementById("admin-mode").textContent === "管理员会话未打开"')
            assert snapshot()['active'], 'Observer exit released the user pointer'
            guest_ui.mouse.move(*point(guest_ui, '#slider', 0.98), steps=16)
            guest_ui.mouse.up()
            wait(lambda: snapshot()['passed'], 'guest drag after observer exit')
            assert store.get(task['id'])['state'] == 'active'
            record('observer_exit_preserves_guest_drag', {'drag_passed': True,
                                                         'same_browser': True})
        finally:
            guest_ui.mouse.up()

        opened = admin.post(admin_base + '/open', json={}, headers=admin_headers)
        assert opened.status_code == 202, (opened.status_code, opened.json)
        wait(lambda: store.get(task['id'])['admin_open'], 'observer reattached')
        reset()
        guest_ui.mouse.move(*point(guest_ui, '#slider', 0.02))
        guest_ui.mouse.down()
        try:
            wait(lambda: snapshot()['active'], 'guest held before submit')
            assert guest.post(base + '/submit', json={}, headers=guest_headers).status_code == 200
            wait(lambda: store.get(task['id'])['state'] == 'completed', 'owned task completed')
            wait(lambda: not snapshot()['active'], 'submit releases guest pointer')
        finally:
            guest_ui.mouse.up()
        guest_ui.wait_for_function('() => document.getElementById("submit").disabled')
        blocked = guest.post(base + '/input', json={'type': 'text', 'text': 'blocked-after-submit'},
                             headers=guest_headers)
        assert blocked.status_code == 409, blocked.json
        assert target.locator('#field').input_value() == ''
        record('completion_locks_and_releases', {'server_input_status': blocked.status_code,
                                                'released': True, 'user_locked': True})

        admin_ui.wait_for_function('() => !document.getElementById("clear-input").disabled')
        assert admin_ui.locator('#task-state').inner_text() == '管理员操作'
        admin_ui.mouse.move(*point(admin_ui, '#pad', 0.5))
        wait(lambda: any(event['type'] == 'pointermove' and event['target'] == 'pad'
                         and event['buttons'] == 0 for event in snapshot()['mouseEvents']),
             'admin takeover pointer released')
        admin_ui.mouse.click(*point(admin_ui, '#field', 0.1))
        admin_ui.keyboard.type('admin-takeover')
        target.wait_for_function('() => document.getElementById("field").value === "admin-takeover"')
        assert store.get(task['id'])['profile_id'] == profile and len(ads.endpoints) == endpoint_count
        record('admin_takeover_after_completion', {'input_ok': True, 'same_browser': True,
                                                  'initial_buttons': 0})
    finally:
        admin_context.close()
        guest_context.close()


def run_http_owner_cases(ui_browser, ui_origin, task, guest, old_ui, target,
                         reset, snapshot, record, errors) -> None:
    """An old HTTP page may not release the new page's mouse press."""
    context = ui_browser.new_context(viewport={'width': 900, 'height': 1000})
    context.add_cookies([{'name': 'session', 'value': guest.get_cookie('session').value,
                         'url': ui_origin}])
    new_ui = context.new_page()
    old_actions = []

    def old_request(request):
        if request.method == 'POST' and request.url.endswith('/input'):
            old_actions.append(request.post_data_json)

    old_ui.on('request', old_request)
    new_ui.on('pageerror', lambda error: errors.append(str(error)))
    new_ui.route_web_socket('**/control', lambda socket: None)
    try:
        new_ui.goto(ui_origin + '/t/' + task['token'], wait_until='domcontentloaded')
        expect(new_ui.locator('#connection-state')).to_have_text('兼容连接', timeout=12000)
        new_ui.wait_for_function('() => document.getElementById("screen").naturalWidth === 1280')

        def point(view, ratio):
            box, bounds = target.locator('#slider').bounding_box(), view.locator('#screen').bounding_box()
            return (bounds['x'] + (box['x'] + box['width'] * ratio) * bounds['width'] / 1280,
                    bounds['y'] + (box['y'] + box['height'] / 2) * bounds['height'] / 800)

        for scenario in ('blur', 'pagehide'):
            reset()
            old_actions.clear()
            old_ui.mouse.move(*point(old_ui, 0.02))
            old_ui.mouse.down()
            try:
                wait(lambda: snapshot()['active'], 'old HTTP page pressed')
                old_action = next(action for action in old_actions if action['type'] == 'mouse_down')
                old_ui.mouse.up()
                wait(lambda: not snapshot()['active'], 'old HTTP page finished its own drag')
                new_ui.mouse.move(*point(new_ui, 0.02))
                new_ui.mouse.down()
                try:
                    wait(lambda: sum(event['type'] == 'pointerdown' and event['target'] == 'slider'
                                     for event in snapshot()['mouseEvents']) >= 2,
                         'new HTTP page pressed')
                    old_ui.evaluate('event => window.dispatchEvent(new Event(event))', scenario)
                    # Browser focus may already have cancelled the old page locally. Explicitly
                    # replay its release identity after takeover to verify the server boundary.
                    status = old_ui.evaluate('''async ({path, old}) => {
                        const response = await fetch(path, {method: 'POST',
                            headers: {'Content-Type': 'application/json',
                                'X-CSRF-Token': document.querySelector('meta[name=csrf-token]').content},
                            body: JSON.stringify({type: 'reset_input', source: old.source,
                                generation: old.generation})});
                        return response.status;
                    }''', {'path': '/api/task/' + task['token'] + '/input', 'old': old_action})
                    assert status == 200, status
                    input_status = old_ui.evaluate('''async ({path, old}) => {
                        const response = await fetch(path, {method: 'POST',
                            headers: {'Content-Type': 'application/json',
                                'X-CSRF-Token': document.querySelector('meta[name=csrf-token]').content},
                            body: JSON.stringify({type: 'text', text: 'old-page-blocked',
                                source: old.source, generation: old.generation})});
                        return response.status;
                    }''', {'path': '/api/task/' + task['token'] + '/input', 'old': old_action})
                    assert input_status == 409, input_status
                    # The old page's queued reset/release is given time to reach the actor.
                    new_ui.wait_for_timeout(300)
                    assert snapshot()['active'], 'Old HTTP page released the new page pointer'
                    new_ui.mouse.move(*point(new_ui, 0.98), steps=16)
                    new_ui.mouse.up()
                    wait(lambda: snapshot()['passed'], 'new HTTP owner drag after old exit')
                    record('http_old_page_' + scenario + '_preserves_new_drag', {
                        'new_drag_passed': True, 'old_release_ignored': True,
                        'old_text_while_drag_status': input_status})
                finally:
                    new_ui.mouse.up()
            finally:
                old_ui.mouse.up()
    finally:
        old_ui.remove_listener('request', old_request)
        context.close()


def run_transport_upgrade_case(ui_browser, ui_origin, task, guest, target,
                               reset, snapshot, record, errors, pending_release=False,
                               close_during_release=False) -> None:
    """A held HTTP drag is cancelled when this page gains a real WebSocket."""
    context = ui_browser.new_context(viewport={'width': 900, 'height': 1000})
    context.add_cookies([{'name': 'session', 'value': guest.get_cookie('session').value,
                         'url': ui_origin}])
    ui = context.new_page()
    ui.on('pageerror', lambda error: errors.append(str(error)))
    ui.add_init_script('''window.localPointerLog = [];
        for (const type of ['pointerdown', 'pointerup', 'pointercancel', 'lostpointercapture'])
            document.addEventListener(type, event => localPointerLog.push({type,
                target: event.target.id, pointerId: event.pointerId}), true);''')
    enabled = {'value': False}
    delayed_releases = []
    delayed_resets, connected_sockets = [], []
    reset_seen = {'value': False}
    upgrade_inputs = []

    def route(socket):
        if enabled['value']:
            socket.connect_to_server()
            connected_sockets.append(socket)

    ui.route_web_socket('**/control', route)

    def input_route(route):
        action = route.request.post_data_json
        upgrade_inputs.append(dict(action))
        if pending_release and action['type'] == 'mouse_up':
            # Leave the old HTTP request unfinished. The native down was already delivered,
            # while the local pointer has been released and the release remains in flight.
            delayed_releases.append(route)
        else:
            if action['type'] == 'reset_input':
                reset_seen['value'] = True
                if close_during_release and enabled['value'] and not delayed_resets:
                    delayed_resets.append(route)
                    return
            route.continue_()

    ui.route('**/input', input_route)

    def routed(items):
        # Sync Playwright delivers route callbacks only while its event loop is pumped.
        ui.wait_for_timeout(20)
        return bool(items)
    try:
        ui.goto(ui_origin + '/t/' + task['token'], wait_until='domcontentloaded')
        expect(ui.locator('#connection-state')).to_have_text('兼容连接', timeout=12000)
        ui.wait_for_function('() => document.getElementById("screen").naturalWidth === 1280')
        reset()
        box = target.locator('#slider').bounding_box()

        def point(ratio):
            bounds = ui.locator('#screen').bounding_box()
            return (bounds['x'] + (box['x'] + box['width'] * ratio) * bounds['width'] / 1280,
                    bounds['y'] + (box['y'] + box['height'] / 2) * bounds['height'] / 800)

        ui.mouse.move(*point(0.02))
        ui.mouse.down()
        try:
            wait(lambda: snapshot()['active'], 'HTTP pointer held before transport upgrade')
            if pending_release:
                ui.mouse.up()
                try:
                    wait(lambda: routed(delayed_releases), 'HTTP release held in flight', timeout=8)
                except AssertionError:
                    record('pending_release_diagnostics', {'fixture': snapshot(),
                        'wire_actions': upgrade_inputs, 'pointer_events': ui.evaluate('() => localPointerLog'),
                        'ui_notice': ui.locator('#notice').inner_text(),
                        'connection': ui.locator('#connection-state').inner_text()})
                    raise
            enabled['value'] = True
            if close_during_release:
                wait(lambda: routed(delayed_resets), 'upgrade HTTP safety release pending')
                enabled['value'] = False
                connected_sockets[-1].close()
                ui.wait_for_timeout(100)
                assert ui.locator('#clear-input').is_disabled(), 'Input became enabled before safety release'
                delayed_resets[0].continue_()
                expect(ui.locator('#connection-state')).to_have_text('兼容连接', timeout=12000)
            else:
                expect(ui.locator('#connection-state')).to_have_text('实时连接', timeout=12000)
            wait(lambda: not snapshot()['active'], 'upgrade cancels held HTTP pointer')
            assert reset_seen['value'], 'Transport upgrade did not send an HTTP-source safety release'
            for delayed in delayed_releases:
                delayed.abort()
        finally:
            ui.mouse.up()
        reset()
        gesture(ui, point(0.02), point(0.98))
        wait(lambda: snapshot()['passed'], 'fresh drag after transport upgrade')
        field = target.locator('#field').bounding_box()
        bounds = ui.locator('#screen').bounding_box()
        ui.mouse.click(bounds['x'] + (field['x'] + 10) * bounds['width'] / 1280,
                       bounds['y'] + (field['y'] + 10) * bounds['height'] / 800)
        ui.keyboard.type('after-upgrade')
        target.wait_for_function('() => document.getElementById("field").value === "after-upgrade"')
        ui.locator('#clear-input').click()
        target.wait_for_function('() => document.getElementById("field").value === ""')
        name = ('ws_close_during_http_release' if close_during_release else
                'http_to_websocket_' + ('pending_release' if pending_release else 'upgrade'))
        record(name, {
            'old_pointer_released': True, 'fresh_drag_passed': True,
            'http_reset_seen': True, 'clear_input_after_upgrade': True})
    finally:
        context.close()


def run_failed_safety_release_case(ui_browser, ui_origin, task, guest, target,
                                   reset, snapshot, record, errors) -> None:
    """Reject release/reset, verify input stays blocked until refresh after lease cleanup."""
    context = ui_browser.new_context(viewport={'width': 900, 'height': 1000})
    context.add_cookies([{'name': 'session', 'value': guest.get_cookie('session').value,
                         'url': ui_origin}])
    ui = context.new_page()
    ui.on('pageerror', lambda error: errors.append(str(error)))
    ui.route_web_socket('**/control', lambda socket: None)
    inputs = []
    ui.on('request', lambda request: inputs.append(request.post_data_json)
          if request.method == 'POST' and request.url.endswith('/input') else None)

    def fault(route):
        action = route.request.post_data_json
        if action['type'] in {'mouse_up', 'reset_input'}:
            route.fulfill(status=500, json={'error': 'owned release and safety reset unavailable'})
        else:
            route.continue_()

    def point(selector):
        box, bounds = target.locator(selector).bounding_box(), ui.locator('#screen').bounding_box()
        return (bounds['x'] + (box['x'] + box['width'] / 2) * bounds['width'] / 1280,
                bounds['y'] + (box['y'] + box['height'] / 2) * bounds['height'] / 800)

    try:
        ui.goto(ui_origin + '/t/' + task['token'], wait_until='domcontentloaded')
        expect(ui.locator('#connection-state')).to_have_text('兼容连接', timeout=12000)
        ui.wait_for_function('() => document.getElementById("screen").naturalWidth === 1280')
        reset()
        ui.route('**/input', fault)
        ui.mouse.move(*point('#action'))
        ui.mouse.down()
        wait(lambda: any(event['type'] == 'pointerdown' and event['target'] == 'action'
                         for event in snapshot()['mouseEvents']), 'failed release press delivered')
        started = time.monotonic()
        ui.mouse.up()
        ui.wait_for_function('() => document.getElementById("notice").textContent.includes("刷新")')
        assert ui.locator('#clear-input').is_disabled()
        count = len(inputs)
        ui.mouse.move(*point('#pad'), steps=8)
        ui.keyboard.type('blocked-input')
        ui.wait_for_timeout(1200)
        assert len(inputs) == count, 'Blocked page continued sending input and extending its lease'
        wait(lambda: any(event['type'] == 'pointerup' and event['target'] == 'action'
                         for event in snapshot()['mouseEvents']), 'lease releases blocked source', timeout=20)
        assert snapshot()['actionCount'] == 0, 'Lease cleanup accidentally clicked button'
        duration = time.monotonic() - started
        ui.unroute('**/input', fault)
        ui.reload(wait_until='domcontentloaded')
        expect(ui.locator('#connection-state')).to_have_text('兼容连接', timeout=12000)
        ui.wait_for_function('() => document.getElementById("screen").naturalWidth === 1280')
        ui.mouse.click(*point('#field'))
        ui.locator('#remote-text').fill('lease-recovered')
        ui.locator('#text-input button').first.click()
        target.wait_for_function('() => document.getElementById("field").value === "lease-recovered"')
        record('http_failed_release_blocks_followup', {'refresh_warning': True,
            'input_disabled': True, 'no_followup_inputs': True, 'accidental_clicks': 0,
            'lease_release_seconds': round(duration, 3), 'refresh_recovered': True})
    finally:
        context.close()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--env', default='/etc/adspower-headless.env')
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--require-remote-drag', action='store_true',
                        help='Fail if the current task page cannot deliver a drag.')
    options = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    fixture_html = (root / 'tests/fixtures/drag_input.html').read_bytes()
    options.output.mkdir(parents=True, exist_ok=True)
    config = {}
    for line in Path(options.env).read_text().splitlines():
        key, separator, value = line.strip().partition('=')
        if separator and key == 'ADSPOWER_API_KEY':
            config[key] = shlex.split(value)[0]
    report = {'source_hashes': {}, 'cases': [], 'production_database_opened': False}
    for name in ('taskapp/static/app.js', 'taskapp/templates/task.html', 'taskapp/browser.py',
                 'taskapp/__init__.py', 'taskapp/tasks.py', 'tests/fixtures/drag_input.html'):
        report['source_hashes'][name] = hashlib.sha256((root / name).read_bytes()).hexdigest()
    received, errors, requests = [], [], []

    class Fixture(BaseHTTPRequestHandler):
        def do_GET(self):
            requests.append(self.path)
            body = b'' if self.path == '/favicon.ico' else fixture_html
            self.send_response(204 if self.path == '/favicon.ico' else 200)
            self.send_header('Content-Type', 'text/html; charset=utf-8')
            self.send_header('Content-Length', str(len(body)))
            self.send_header('Cache-Control', 'no-store')
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass

    class OwnedAds(AdsPower):
        def __init__(self):
            super().__init__('http://127.0.0.1:50325', config['ADSPOWER_API_KEY'])
            self.profiles, self.endpoints = [], []

        def create(self, task):
            profile = super().create(task)
            self.profiles.append(profile)
            return profile

        def start(self, profile, restore_tabs=False):
            endpoint = super().start(profile, restore_tabs)
            self.endpoints.append(endpoint)
            return endpoint

    class ObservedBrowser(RemoteBrowser):
        def input(self, action):
            received.append(dict(action))
            return super().input(action)

    class QuietRequests(WSGIRequestHandler):
        def log_request(self, *args):
            pass

    fixture = ThreadingHTTPServer(('127.0.0.1', 0), Fixture)
    threading.Thread(target=fixture.serve_forever, daemon=True).start()
    origin = f'http://127.0.0.1:{fixture.server_port}'
    ads = OwnedAds()
    server = manager = None
    logging.getLogger('taskapp.browser').setLevel(logging.WARNING)
    with tempfile.TemporaryDirectory(prefix='taskroom-drag-owned-') as directory:
        try:
            app = create_app({'TESTING': True, 'SECRET_KEY': 'owned-drag-test-secret',
                              'ADMIN_PASSWORD': 'owned-drag-test-admin',
                              'DATABASE': str(Path(directory) / 'tasks.db')},
                             ads=ads, browser_factory=ObservedBrowser)
            manager, store = app.extensions['task_manager'], app.extensions['task_store']
            server = make_server('127.0.0.1', 0, app, threaded=True,
                                 request_handler=QuietRequests)
            threading.Thread(target=server.serve_forever, daemon=True).start()
            ui_origin = f'http://127.0.0.1:{server.server_port}'
            task = manager.create({'title': '自有滑块输入评测', 'start_url': origin + '/',
                                   'lifetime_minutes': 15})
            wait(lambda: store.get(task['id'])['state'] in {'active', 'error'}, 'owned startup')
            assert store.get(task['id'])['state'] == 'active', store.get(task['id'])['message']
            runtime = manager.runtimes[task['id']]
            guest = app.test_client()
            guest.get('/t/' + task['token'])
            with guest.session_transaction() as session:
                headers = {'X-CSRF-Token': session['csrf']}
            base = '/api/task/' + task['token']
            assert guest.post(base + '/claim', json={}, headers=headers).status_code == 200

            with sync_playwright() as driver:
                remote = driver.chromium.connect_over_cdp(ads.endpoints[-1])
                target = next(page for page in remote.contexts[0].pages if page.url == origin + '/')
                target.wait_for_function('() => Boolean(window.dragFixture)')
                assert target.evaluate('navigator.platform') == 'MacIntel'
                report['platform'] = target.evaluate('navigator.platform')
                report['kernel'] = remote.new_browser_cdp_session().send('Browser.getVersion')['product']
                slider = target.locator('#slider').bounding_box()
                start = (slider['x'] + 10, slider['y'] + slider['height'] / 2)
                end = (slider['x'] + slider['width'] - 10, start[1])

                def snapshot():
                    return target.evaluate('() => dragFixture.snapshot()')

                def reset():
                    before = runtime.frame_version
                    target.evaluate('() => dragFixture.reset()')
                    wait(lambda: runtime.frame_version > before, 'reset frame')

                def record(name, data):
                    report['cases'].append({'name': name, **data})
                    print(name.upper(), json.dumps(data, ensure_ascii=False), flush=True)

                reset()
                gesture(target, start, end)
                direct = snapshot()
                assert direct['passed'] and direct['heldMoves'] > 0, direct
                record('direct_drag', direct)
                target.screenshot(path=str(options.output / 'direct-drag.png'))

                reset()
                target.mouse.click(*end)
                clicked = snapshot()
                assert clicked['value'] > 0 and not clicked['passed'], clicked
                assert clicked['heldMoves'] == 0 and clicked['downSeen'] and clicked['upSeen']
                record('direct_click_moves_without_drag', clicked)

                ui_browser = driver.chromium.launch(headless=True, args=['--no-sandbox'])
                try:
                    for mode in ('websocket', 'http'):
                        context = ui_browser.new_context(viewport={'width': 1440, 'height': 1000})
                        context.add_cookies([{'name': 'session', 'value': guest.get_cookie('session').value,
                                             'url': ui_origin}])
                        ui = context.new_page()
                        wire, responses = [], []
                        ui.on('pageerror', lambda error: errors.append(str(error)))

                        def sent(value):
                            if isinstance(value, str):
                                message = json.loads(value)
                                if message.get('type') == 'input':
                                    wire.append(message['action'])

                        def requested(request):
                            if request.method == 'POST' and request.url.endswith('/input'):
                                wire.append(request.post_data_json)

                        ui.on('websocket', lambda ws: ws.on('framesent', sent))
                        ui.on('request', requested)
                        ui.on('response', lambda response: responses.append({
                            'status': response.status, 'action': response.request.post_data_json})
                            if response.request.method == 'POST' and response.url.endswith('/input') else None)
                        ui.add_init_script("""window.screenEvents = [];
                            window.ownedSockets = [];
                            const NativeSocket = WebSocket;
                            window.WebSocket = class extends NativeSocket {
                                constructor(...args) { super(...args); ownedSockets.push(this); }
                            };
                            for (const type of ['pointerdown','pointermove','pointerup','dragstart','dragend']) {
                                document.addEventListener(type, event => {
                                    if (event.target.id === 'screen') screenEvents.push({type, buttons:event.buttons});
                                }, true);
                            }""")
                        if mode == 'http':
                            ui.route_web_socket('**/control', lambda ws: None)
                        ui.goto(ui_origin + '/t/' + task['token'], wait_until='domcontentloaded')
                        expect(ui.locator('#connection-state')).to_have_text(
                            '实时连接' if mode == 'websocket' else '兼容连接', timeout=12000)
                        ui.wait_for_function('() => document.getElementById("screen").naturalWidth === 1280')
                        expect(ui.locator('#overlay')).to_be_hidden()

                        for width in (1440, 900):
                            ui.set_viewport_size({'width': width, 'height': 1000})
                            reset()
                            ui.wait_for_timeout(250)
                            screen = ui.locator('#screen').bounding_box()

                            def mapped(point):
                                return (screen['x'] + point[0] * screen['width'] / 1280,
                                        screen['y'] + point[1] * screen['height'] / 800)

                            received.clear()
                            wire.clear()
                            responses.clear()
                            ui.evaluate('() => { screenEvents = []; }')
                            started = time.monotonic()
                            gesture(ui, mapped(start), mapped(end))
                            if options.require_remote_drag:
                                try:
                                    wait(lambda: snapshot()['upSeen'], mode + ' drag completed', timeout=10)
                                except AssertionError:
                                    record(mode + '_incomplete_input', {'fixture': snapshot(),
                                        'wire_actions': list(wire), 'server_actions': list(received),
                                        'responses': list(responses), 'ui_notice': ui.locator('#notice').inner_text(),
                                        'connection': ui.locator('#connection-state').inner_text()})
                                    ui.screenshot(path=str(options.output / f'{mode}-input-incomplete.png'))
                                    raise
                            else:
                                ui.wait_for_timeout(500)
                            actual = snapshot()
                            record(f'{mode}_drag_{width}', {
                                'fixture': actual, 'wire_actions': list(wire),
                                'server_actions': list(received),
                                'responses': list(responses),
                                'ui_events': ui.evaluate('() => screenEvents'),
                                'scale': round(screen['width'] / 1280, 4),
                                'completion_seconds': round(time.monotonic() - started, 3),
                            })
                            ui.screenshot(path=str(options.output / f'{mode}-drag-{width}.png'))

                            reset()
                            ui.wait_for_timeout(250)
                            received.clear()
                            wire.clear()
                            ui.mouse.click(*mapped(end))
                            wait(lambda: snapshot()['upSeen'], mode + ' click reached server')
                            actual = snapshot()
                            assert actual['value'] > 0 and actual['heldMoves'] == 0, actual
                            assert not actual['passed'] and actual['downSeen'] and actual['upSeen']
                            click = next(action for action in received
                                         if action['type'] in {'click', 'mouse_down'})
                            assert abs(click['x'] - end[0]) < 2 and abs(click['y'] - end[1]) < 2, click
                            record(f'{mode}_click_{width}', {
                                'fixture': actual, 'wire_actions': list(wire),
                                'server_actions': list(received), 'coordinates_ok': True,
                            })
                        run_input_cases(ui, target, mode, reset, snapshot, record, received)
                        if mode == 'http':
                            run_http_owner_cases(ui_browser, ui_origin, task, guest, ui, target,
                                                 reset, snapshot, record, errors)
                            run_transport_upgrade_case(ui_browser, ui_origin, task, guest, target,
                                                       reset, snapshot, record, errors)
                            run_transport_upgrade_case(ui_browser, ui_origin, task, guest, target,
                                                       reset, snapshot, record, errors, pending_release=True)
                            run_transport_upgrade_case(ui_browser, ui_origin, task, guest, target,
                                                       reset, snapshot, record, errors,
                                                       close_during_release=True)
                            run_failed_safety_release_case(ui_browser, ui_origin, task, guest, target,
                                                           reset, snapshot, record, errors)
                        if mode == 'websocket':
                            for selector in ('#slider', '#action'):
                                expect(ui.locator('#connection-state')).to_have_text('实时连接', timeout=12000)
                                reset()
                                box = target.locator(selector).bounding_box()
                                selected = (box['x'] + (10 if selector == '#slider' else box['width'] / 2),
                                            box['y'] + box['height'] / 2)
                                screen = ui.locator('#screen').bounding_box()
                                ui.mouse.move(screen['x'] + selected[0] * screen['width'] / 1280,
                                              screen['y'] + selected[1] * screen['height'] / 800)
                                ui.mouse.down()
                                try:
                                    wait(lambda: any(event['type'] == 'pointerdown'
                                                     and event['target'] == selector[1:]
                                                     and event['buttons'] == 1
                                                     for event in snapshot()['mouseEvents']),
                                         'WS disconnect held pointer')
                                    ui.evaluate('() => ownedSockets.at(-1).close()')
                                    wait(lambda: any(event['type'] == 'pointerup'
                                                     and event['target'] == selector[1:]
                                                     and event['buttons'] == 0
                                                     for event in snapshot()['mouseEvents']),
                                         'WS disconnect releases pointer')
                                    assert not snapshot()['active'] and snapshot()['actionCount'] == 0
                                    record('websocket_disconnect_' + selector[1:], {
                                        'released': True, 'accidental_clicks': 0})
                                finally:
                                    ui.mouse.up()
                        context.close()
                    run_lifecycle_cases(app, ui_browser, ui_origin, task, guest, target, manager, store,
                                        reset, snapshot, record, errors, ads)
                finally:
                    ui_browser.close()
                assert not errors, errors
                assert runtime.network_warning is None, runtime.network_warning
                assert store.get(task['id'])['state'] == 'completed'
                report['ui_errors'] = errors
                report['target_network_warning'] = runtime.network_warning
                drag_cases = [case for case in report['cases'] if '_drag_' in case['name']]
                report['remote_drag_supported'] = all(case['fixture']['passed'] for case in drag_cases)
                report['fixture_requests'] = requests
        finally:
            try:
                if manager:
                    for runtime in list(manager.runtimes.values()):
                        runtime.cancel()
                        runtime.thread.join(20)
                        assert not runtime.thread.is_alive(), 'Owned runtime still running; not deleting profile'
                for profile in ads.profiles:
                    ads.delete(profile)
                report['owned_profiles_removed'] = len(ads.profiles)
                print('OWN_DRAG_PROFILE_REMOVED; production database not opened', flush=True)
            finally:
                if server:
                    server.shutdown()
                    server.server_close()
                fixture.shutdown()
                fixture.server_close()
                (options.output / 'drag-report.json').write_text(
                    json.dumps(report, ensure_ascii=False, indent=2) + '\n')
    if options.require_remote_drag:
        assert report['remote_drag_supported'], 'Remote drag failed; see drag-report.json (no service fix applied)'
    print('DRAG_DIAGNOSTIC_COMPLETE', json.dumps({
        'remote_drag_supported': report['remote_drag_supported'],
        'cases': len(report['cases']), 'owned_profiles_removed': report['owned_profiles_removed'],
    }), flush=True)


if __name__ == '__main__':
    main()
