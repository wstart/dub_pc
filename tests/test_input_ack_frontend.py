from __future__ import annotations

import json
import shutil
import subprocess
import unittest
from pathlib import Path


NODE_HARNESS = r"""
const fs = require('fs');
const vm = require('vm');
const source = fs.readFileSync(process.argv[1], 'utf8');
const scenario = process.argv[2];
const elements = new Map(), sockets = [], notices = [], timers = new Map(), httpInputs = [];
const blobs = new Map(), revokedUrls = [];
let clock = 0, timerId = 0;

class TestURL extends URL {
  static createObjectURL(blob) {
    const identifier = 'blob:owned-fixture-' + (blobs.size + 1);
    blobs.set(identifier, blob);
    return identifier;
  }
  static revokeObjectURL(identifier) { revokedUrls.push(identifier); }
}

class Element {
  constructor(id) { this.id = id; this.value = ''; this.dataset = {}; this.hidden = false; }
  append() {}
  replaceChildren() {}
  setAttribute() {}
  removeAttribute(name) { if (name === 'src') this.src = ''; }
  addEventListener() {}
  querySelector() { return null; }
  querySelectorAll() { return []; }
  focus() {}
  hasPointerCapture() { return false; }
  releasePointerCapture() {}
  setPointerCapture() {}
  getBoundingClientRect() { return {left: 0, top: 0, width: 1280, height: 800}; }
  set textContent(value) {
    this.text = value;
    if (this.id === 'notice' && value) notices.push(value);
  }
  get textContent() { return this.text || ''; }
}

const element = id => {
  if (!elements.has(id)) elements.set(id, new Element(id));
  return elements.get(id);
};

class Socket {
  static OPEN = 1;
  constructor() { this.readyState = 1; this.sent = []; this.closes = 0; sockets.push(this); }
  send(value) { this.sent.push(JSON.parse(value)); }
  close() {
    this.closes++;
    this.readyState = 3;
    Promise.resolve().then(() => { if (this.onclose) this.onclose(); });
  }
  receive(value) { this.onmessage({data: JSON.stringify(value)}); }
  receiveBinary(bytes) { this.onmessage({data: Uint8Array.from(bytes).buffer}); }
}

const context = {
  Promise, Map, Set, JSON, Math, Error, URL: TestURL, AbortController, Blob,
  WebSocket: Socket,
  crypto: {getRandomValues(values) { values.fill(1); return values; }},
  location: {protocol: 'http:', host: 'localhost', origin: 'http://localhost'},
  performance: {now: () => clock},
  document: {
    body: {dataset: {page: 'task'}},
    querySelector: () => ({content: 'owned-test-token'}),
    querySelectorAll: () => [],
    getElementById: element,
    createElement: tag => new Element(tag),
    activeElement: null,
  },
  window: {addEventListener() {}},
  setTimeout(callback, delay) {
    const id = ++timerId;
    timers.set(id, {callback, at: clock + delay});
    return id;
  },
  clearTimeout(id) { timers.delete(id); },
  fetch(path, options) {
    if (path.endsWith('/input')) {
      httpInputs.push(JSON.parse(options.body));
      return Promise.resolve({ok: true, json: async () => ({ok: true})});
    }
    // Hold the initial background poll. All test state is supplied explicitly below.
    return new Promise(() => {});
  },
};

vm.createContext(context);
const anchor = "  if(isAdmin)api(base+'/open'";
if (!source.includes(anchor)) throw new Error('Task-page harness anchor missing');
const hooks = `globalThis.inputHarness = {
  render: renderState, send,
  snapshot: () => ({operable, channelReady, inputBlocked, pending: inputPending.size, epoch: inputEpoch}),
};\n`;
vm.runInContext(source.replace(anchor, hooks + anchor), context, {filename: process.argv[1]});

async function flush() { for (let i = 0; i < 16; i++) await Promise.resolve(); }

function advance(milliseconds) {
  clock += milliseconds;
  while (true) {
    const due = [...timers].filter(([, timer]) => timer.at <= clock).sort((a, b) => a[1].at - b[1].at);
    if (!due.length) break;
    for (const [id, timer] of due) {
      if (!timers.delete(id)) continue;
      timer.callback();
    }
  }
}

function inputs(socket) { return socket.sent.filter(value => value.type === 'input'); }

async function main() {
  const task = {state: 'active', mine: true, claimed: true, message: 'owned fixture',
    stream_generation: 17, expires_at: Date.now() / 1000 + 600, tabs: [], network_warning: null};
  context.inputHarness.render(task);
  const socket = sockets[0];
  socket.onopen();
  socket.receive({type: 'state', task});
  await flush();
  if (!context.inputHarness.snapshot().operable) throw new Error('Owned fake channel did not become ready');

  if (scenario === 'matching_ack') {
    const receipts = [];
    for (let i = 0; i < 2; i++) {
      receipts.push(context.inputHarness.send({type: 'key', key: 'ArrowRight'}));
      await flush();
    }
    for (const input of inputs(socket)) socket.receive({type: 'input_ack', id: input.id});
    const accepted = await Promise.all(receipts);
    advance(5001);
    await flush();
    return {accepted, ...context.inputHarness.snapshot(), notices: notices.length, closes: socket.closes};
  }

  if (scenario === 'missing_ack') {
    for (let i = 0; i < 20; i++) {
      context.inputHarness.send({type: 'mouse_move', x: i + 1, y: 1});
      await flush();
    }
    const before = inputs(socket).length;
    advance(5001);
    await flush();
    const timedOut = context.inputHarness.snapshot();
    context.inputHarness.send({type: 'mouse_move', x: 99, y: 1});
    await flush();
    return {before, timedOut, after: context.inputHarness.snapshot(), sentAfter: inputs(socket).length,
      notices: notices.length, closes: socket.closes};
  }
  if (scenario === 'late_ack' || scenario === 'old_state') {
    for (let i = 0; i < 2; i++) {
      context.inputHarness.send({type: 'key', key: 'ArrowRight'});
      await flush();
    }
    const sent = inputs(socket);
    advance(5001);
    await flush();
    if (scenario === 'late_ack') {
      for (const input of sent) socket.receive({type: 'input_ack', id: input.id});
    } else {
      // A late same-generation message from the old socket must not re-authorize
      // inputs. A later ordinary poll also must retain the safety block.
      socket.receive({type: 'state', task});
      context.inputHarness.render(task);
    }
    await flush();
    context.inputHarness.send({type: 'key', key: 'ArrowLeft'});
    await flush();
    advance(6000);
    await flush();
    return {...context.inputHarness.snapshot(), before: sent.length,
      sentAfter: inputs(socket).length, notices: notices.length, closes: socket.closes,
      socketCount: sockets.length,
      httpDiscreteInputs: httpInputs.filter(action => action.type !== 'reset_input').length};
  }
  if (scenario.startsWith('frame_')) {
    let prior = null, partial = null;
    if (scenario === 'frame_partial') {
      socket.receive({type: 'frame_start', id: 9, size: 6});
      socket.receiveBinary([1, 2, 3]);
      const receipt = context.inputHarness.send({type: 'key', key: 'ArrowRight'});
      await flush();
      for (const input of inputs(socket)) socket.receive({type: 'input_ack', id: input.id});
      if (!await receipt) throw new Error('Input ACK did not work during a partial image');
    } else if (scenario === 'frame_complete') {
      socket.receiveBinary([255, 216, 255, 217]);
      prior = element('screen').src;
      socket.receive({type: 'frame_start', id: 9, size: 6});
      socket.receiveBinary([1, 2, 3]);
      partial = element('screen').src;
      socket.receiveBinary([4, 5, 6]);
    } else if (scenario === 'frame_legacy') {
      socket.receiveBinary([255, 216, 255, 217]);
    } else if (scenario === 'frame_invalid_size') {
      socket.receive({type: 'frame_start', id: 9, size: 0});
    } else if (scenario === 'frame_oversize_header') {
      socket.receive({type: 'frame_start', id: 9, size: 2097153});
    } else if (scenario === 'frame_invalid_id') {
      socket.receive({type: 'frame_start', id: Number.MAX_SAFE_INTEGER + 1, size: 4});
    } else if (scenario === 'frame_overlap') {
      socket.receive({type: 'frame_start', id: 9, size: 6});
      socket.receiveBinary([1, 2, 3]);
      socket.receive({type: 'frame_start', id: 10, size: 4});
    } else if (scenario === 'frame_oversize_chunk') {
      socket.receive({type: 'frame_start', id: 9, size: 20000});
      socket.receiveBinary(new Array(20000).fill(1));
    } else if (scenario === 'frame_overflow') {
      socket.receive({type: 'frame_start', id: 9, size: 3});
      socket.receiveBinary([1, 2, 3, 4]);
    } else if (scenario === 'frame_empty_chunk') {
      socket.receive({type: 'frame_start', id: 9, size: 4});
      socket.receiveBinary([]);
    } else if (scenario === 'frame_unexpected_binary') {
      socket.receive({type: 'frame_start', id: 9, size: 4});
      socket.receiveBinary([255, 216, 255, 217]);
      socket.receiveBinary([1]);
    } else if (scenario === 'frame_disconnect') {
      socket.receive({type: 'frame_start', id: 9, size: 6});
      socket.receiveBinary([1, 2, 3]);
      socket.close();
      await flush();
      socket.receiveBinary([4, 5, 6]);
    } else throw new Error('Unknown frame scenario');
    await flush();
    const current = element('screen').src || null;
    return {...context.inputHarness.snapshot(), notices: notices.length, closes: socket.closes,
      current, prior, partial, rendered: blobs.size, revokedUrls,
      bytes: current && blobs.has(current) ? Array.from(new Uint8Array(await blobs.get(current).arrayBuffer())) : null,
      frameAcks: socket.sent.filter(value => value.type === 'frame_ack'),
      auth: socket.sent.find(value => value.csrf)};
  }
  throw new Error('Unknown scenario');
}

main().then(result => process.stdout.write(JSON.stringify(result) + '\n')).catch(error => {
  process.stderr.write(error.stack + '\n');
  process.exitCode = 1;
});
"""


class FrontendInputAckTests(unittest.TestCase):
    def run_scenario(self, scenario: str) -> dict:
        node = shutil.which("node")
        if not node:
            import playwright

            candidate = Path(playwright.__file__).parent / "driver" / "node"
            if candidate.is_file():
                node = str(candidate)
        if not node:
            self.skipTest("Node runtime unavailable; no browser or runtime is installed by this test")
        app_js = Path(__file__).resolve().parents[1] / "taskapp" / "static" / "app.js"
        result = subprocess.run([node, "-e", NODE_HARNESS, str(app_js), scenario],
                                capture_output=True, text=True, timeout=15)
        self.assertEqual(result.returncode, 0, result.stderr)
        return json.loads(result.stdout)

    def test_matching_ack_resolves_receipts_and_cancels_timeouts(self):
        result = self.run_scenario("matching_ack")
        self.assertEqual(result["accepted"], [True, True], result)
        self.assertEqual(result["pending"], 0, result)
        self.assertEqual(result["notices"], 0, result)
        self.assertEqual(result["closes"], 0, result)
        self.assertTrue(result["operable"], result)

    def test_missing_ack_stops_hover_error_storm_without_replaying_input(self):
        result = self.run_scenario("missing_ack")
        self.assertEqual(result["before"], 20, result)
        self.assertEqual(result["notices"], 1, result)
        self.assertFalse(result["timedOut"]["operable"], result)
        self.assertEqual(result["timedOut"]["pending"], 0, result)
        self.assertEqual(result["closes"], 1, result)
        self.assertEqual(result["sentAfter"], result["before"], result)

    def assert_stays_blocked(self, scenario: str):
        result = self.run_scenario(scenario)
        self.assertEqual(result["before"], 2, result)
        self.assertEqual(result["notices"], 1, result)
        self.assertFalse(result["operable"], result)
        self.assertTrue(result["inputBlocked"], result)
        self.assertEqual(result["pending"], 0, result)
        self.assertEqual(result["closes"], 1, result)
        self.assertEqual(result["socketCount"], 1, result)
        self.assertEqual(result["sentAfter"], result["before"], result)
        self.assertEqual(result["httpDiscreteInputs"], 0, result)

    def test_late_ack_after_timeout_cannot_restore_or_replay_input(self):
        self.assert_stays_blocked("late_ack")

    def test_old_socket_state_and_same_generation_poll_cannot_restore_input(self):
        self.assert_stays_blocked("old_state")

    def test_partial_chunked_frame_is_not_displayed_and_input_ack_still_works(self):
        result = self.run_scenario("frame_partial")
        self.assertEqual(result["rendered"], 0, result)
        self.assertIsNone(result["current"], result)
        self.assertEqual(result["frameAcks"], [{"type": "frame_ack", "id": 9, "offset": 3}], result)
        self.assertEqual(result["pending"], 0, result)
        self.assertTrue(result["operable"], result)
        self.assertTrue(result["auth"]["frame_chunks"], result)

    def test_complete_frame_is_assembled_before_replacing_previous_image(self):
        result = self.run_scenario("frame_complete")
        self.assertEqual(result["partial"], result["prior"], result)
        self.assertNotEqual(result["current"], result["prior"], result)
        self.assertEqual(result["bytes"], [1, 2, 3, 4, 5, 6], result)
        self.assertEqual(result["rendered"], 2, result)
        self.assertEqual(result["revokedUrls"], [result["prior"]], result)
        self.assertEqual(result["frameAcks"], [
            {"type": "frame_ack"}, {"type": "frame_ack", "id": 9, "offset": 3},
            {"type": "frame_ack", "id": 9, "offset": 6}], result)

    def test_legacy_raw_jpeg_remains_compatible_before_any_frame_header(self):
        result = self.run_scenario("frame_legacy")
        self.assertEqual(result["bytes"], [255, 216, 255, 217], result)
        self.assertEqual(result["rendered"], 1, result)
        self.assertEqual(result["frameAcks"], [{"type": "frame_ack"}], result)
        self.assertTrue(result["operable"], result)

    def test_bad_frame_header_or_chunk_blocks_input_without_displaying_partial_frame(self):
        for scenario in ("frame_invalid_size", "frame_oversize_header", "frame_invalid_id", "frame_overlap",
                         "frame_oversize_chunk", "frame_overflow", "frame_empty_chunk"):
            with self.subTest(scenario=scenario):
                result = self.run_scenario(scenario)
                self.assertFalse(result["operable"], result)
                self.assertTrue(result["inputBlocked"], result)
                self.assertEqual(result["rendered"], 0, result)
                self.assertEqual(result["closes"], 1, result)
                self.assertEqual(result["notices"], 1, result)

    def test_chunked_connection_does_not_treat_stray_chunk_as_legacy_image(self):
        result = self.run_scenario("frame_unexpected_binary")
        self.assertFalse(result["operable"], result)
        self.assertTrue(result["inputBlocked"], result)
        self.assertEqual(result["rendered"], 1, result)
        self.assertEqual(result["closes"], 1, result)

    def test_disconnected_half_frame_discards_late_old_socket_chunks(self):
        result = self.run_scenario("frame_disconnect")
        self.assertEqual(result["rendered"], 0, result)
        self.assertIsNone(result["current"], result)
        self.assertFalse(result["channelReady"], result)
        self.assertEqual(result["closes"], 1, result)


if __name__ == "__main__":
    unittest.main()
