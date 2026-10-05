"""Pointer sampling regressions against the complete app.js, without a browser."""
from __future__ import annotations

import json
import shutil
import subprocess
import unittest
from pathlib import Path

from tests.test_input_ack_frontend import NODE_HARNESS


MOUSE_HARNESS = NODE_HARNESS.split("async function main() {", 1)[0].replace(
    "let clock = 0, timerId = 0;", "let clock = 0, timerId = 0, timerExecutions = 0;"
).replace("timer.callback();", "timerExecutions++; timer.callback();").replace(
    "this.sent.push(JSON.parse(value));",
    "const data = JSON.parse(value); data.at = clock; this.sent.push(data);"
).replace(
    "append() {}", "append(...children) { this.children = (this.children || []).concat(children); }"
).replace(
    "window: {addEventListener() {}}",
    "window: {handlers: new Map(), addEventListener(name, handler) { this.handlers.set(name, handler); }}"
) + r"""
function pointer(x, id = 7) {
  return {clientX: x, clientY: 10, pointerId: id, button: 0, preventDefault() {},
    shiftKey: false, altKey: false, ctrlKey: false, metaKey: false};
}
function actions(socket) {
  return inputs(socket).map(value => ({type: value.action.type, x: value.action.x,
    at: value.at, id: value.id, sequence: value.action.sequence}));
}
function moves(socket) { return inputs(socket).filter(value => value.action.type === 'mouse_move'); }
function ack(socket, input) { socket.receive({type: 'input_ack', id: input.id}); }

async function main() {
  const task = {state: 'active', mine: true, claimed: true, message: 'owned fixture',
    stream_generation: 17, expires_at: Date.now() / 1000 + 600, tabs: [], network_warning: null};
  context.inputHarness.render(task);
  const socket = sockets[0], screen = element('screen');
  socket.onopen(); socket.receive({type: 'state', task}); await flush();
  if (!context.inputHarness.snapshot().operable) throw new Error('Owned fake channel is not ready');

  if (scenario === 'sample20') {
    screen.onpointermove(pointer(10));
    advance(19); await flush(); const before = moves(socket).length;
    advance(1); await flush();
    for (const input of moves(socket)) ack(socket, input);
    await flush();
    screen.onpointermove(pointer(20));
    advance(20); await flush();
    return {before, moves: actions(socket).filter(value => value.type === 'mouse_move')};
  }
  if (scenario === 'hover_blur') {
    screen.onpointermove(pointer(10)); advance(10); screen.onblur();
    advance(100); await flush();
    return {moves: moves(socket).length};
  }
  if (scenario === 'no_repeat') {
    screen.onpointermove(pointer(10)); advance(20); await flush();
    ack(socket, moves(socket)[0]); await flush(); advance(200); await flush();
    return {moves: moves(socket).length};
  }
  if (scenario.startsWith('http_')) {
    socket.close(); await flush(); context.inputHarness.render(task); await flush();
    const posted = [], releases = [];
    context.fetch = (path, options) => {
      if (!path.endsWith('/input')) return new Promise(() => {});
      const action = JSON.parse(options.body); posted.push({type: action.type, x: action.x, sequence: action.sequence});
      return new Promise(resolve => releases.push(() => resolve({ok: true, json: async () => ({ok: true})})));
    };
    screen.onpointerdown(pointer(1)); await flush();
    if (scenario === 'http_order') { releases.shift()(); await flush(); }
    screen.onpointermove(pointer(2)); advance(20); await flush();
    if (scenario === 'http_blur') screen.onblur(); else screen.onpointerup(pointer(4));
    await flush(); const before = posted.slice();
    for (let index = 0; releases.length && index < 8; index++) { releases.shift()(); await flush(); }
    advance(100); await flush();
    return {before, after: posted};
  }
  screen.onpointerdown(pointer(1)); await flush();
  ack(socket, inputs(socket)[0]); await flush();

  if (scenario === 'normal_segments') {
    for (let index = 1; index <= 110; index++) {
      screen.onpointermove(pointer(index)); advance(20); await flush();
      const move = moves(socket).at(-1); if (move) ack(socket, move); await flush();
    }
    return {moves: actions(socket).filter(value => value.type === 'mouse_move')};
  }

  if (scenario === 'budget100') {
    for (let index = 1; index <= 200; index++) {
      screen.onpointermove(pointer(index)); advance(20); await flush();
    }
    const before = moves(socket).length, beforeTimers = timerExecutions;
    ack(socket, moves(socket)[0]); await flush();
    advance(0); await flush();
    return {before, beforeTimers, moves: actions(socket).filter(value => value.type === 'mouse_move'),
      notices: notices.length};
  }
  screen.onpointermove(pointer(2)); advance(40); await flush();
  const firstMove = moves(socket)[0];
  if (!firstMove) throw new Error('Initial move was not sampled');

  if (scenario === 'up_before_ack') {
    screen.onpointermove(pointer(3)); advance(1);
    screen.onpointerup(pointer(4)); await flush();
    const before = actions(socket);
    ack(socket, firstMove); await flush(); advance(100); await flush();
    return {before, after: actions(socket), notices: notices.length};
  }
  if (scenario === 'old_gesture_ack') {
    screen.onpointerup(pointer(4)); await flush();
    screen.onpointerdown(pointer(100, 8)); await flush();
    screen.onpointermove(pointer(110, 8)); advance(20); await flush();
    const before = moves(socket).length;
    ack(socket, firstMove); await flush(); advance(20); await flush();
    return {before, moves: actions(socket).filter(value => value.type === 'mouse_move')};
  }
  if (scenario === 'blur_discards_pending') {
    screen.onpointermove(pointer(30)); advance(20); await flush();
    screen.onblur(); await flush();
    const before = moves(socket).length;
    ack(socket, firstMove); await flush(); advance(100); await flush();
    return {before, after: moves(socket).length, actions: actions(socket)};
  }
  if (scenario === 'navigate_barrier') {
    screen.onpointermove(pointer(30)); advance(20); await flush();
    element('address').value = 'https://example.com/owned';
    element('navigate').onsubmit({preventDefault() {}}); await flush();
    const before = actions(socket);
    ack(socket, firstMove); await flush(); advance(100); await flush();
    return {before, after: actions(socket)};
  }
  if (scenario === 'tab_barrier' || scenario === 'close_tab_barrier' || scenario === 'text_barrier') {
    screen.onpointermove(pointer(30)); advance(20); await flush();
    if (scenario === 'text_barrier') {
      element('remote-text').value = 'owned fixture';
      element('text-input').onsubmit({preventDefault() {}});
    } else {
      context.inputHarness.render({...task, tabs: [{id: 1, index: 0, url: 'https://example.com', active: true}]});
      const tab = element('tabs').children.at(-1), button = tab.children[scenario === 'tab_barrier' ? 0 : 1];
      button.onclick({stopPropagation() {}});
    }
    await flush(); const before = actions(socket);
    ack(socket, firstMove); await flush(); advance(100); await flush();
    return {before, after: actions(socket)};
  }
  if (scenario === 'generation_switch') {
    screen.onpointermove(pointer(30)); advance(20); await flush();
    const nextTask = {...task, stream_generation: 18}; context.inputHarness.render(nextTask);
    const next = sockets.at(-1); next.onopen(); next.receive({type: 'state', task: nextTask}); await flush();
    screen.onpointermove(pointer(50)); advance(20); await flush();
    socket.receive({type: 'input_ack', id: firstMove.id}); await flush();
    screen.onpointermove(pointer(60)); advance(20); await flush();
    const before = moves(next).length;
    ack(next, moves(next)[0]); await flush(); advance(0); await flush();
    return {before, oldMoves: moves(socket).length, nextMoves: actions(next).filter(value => value.type === 'mouse_move')};
  }
  if (scenario === 'timeout_stops_sampling') {
    screen.onpointermove(pointer(30)); advance(20); await flush();
    advance(5001); await flush();
    socket.receive({type: 'input_ack', id: firstMove.id}); await flush();
    screen.onpointermove(pointer(50)); advance(100); await flush();
    return {...context.inputHarness.snapshot(), moves: moves(socket).length,
      notices: notices.length, closes: socket.closes};
  }
  if (scenario === 'pagehide') {
    screen.onpointermove(pointer(30)); advance(20); await flush();
    context.window.handlers.get('pagehide')(); await flush();
    const before = actions(socket);
    socket.receive({type: 'input_ack', id: firstMove.id}); advance(100); await flush();
    return {...context.inputHarness.snapshot(), before, after: actions(socket), closes: socket.closes};
  }
  throw new Error('Unknown pointer scenario');
}

main().then(result => process.stdout.write(JSON.stringify(result) + '\n')).catch(error => {
  process.stderr.write(error.stack + '\n'); process.exitCode = 1;
});
"""


class MouseSamplingTests(unittest.TestCase):
    def run_scenario(self, scenario: str) -> dict:
        node = shutil.which("node")
        if not node:
            import playwright

            candidate = Path(playwright.__file__).parent / "driver" / "node"
            if candidate.is_file():
                node = str(candidate)
        if not node:
            self.skipTest("Node runtime unavailable; this test installs nothing")
        app_js = Path(__file__).resolve().parents[1] / "taskapp" / "static" / "app.js"
        result = subprocess.run([node, "-e", MOUSE_HARNESS, str(app_js), scenario],
                                capture_output=True, text=True, timeout=15)
        self.assertEqual(result.returncode, 0, result.stderr)
        return json.loads(result.stdout)

    def test_samples_latest_pointer_every_20ms_without_waiting_for_100_points(self):
        result = self.run_scenario("sample20")
        self.assertEqual(result["before"], 0, result)
        self.assertEqual([(value["x"], value["at"]) for value in result["moves"]], [(10, 20), (20, 40)], result)

    def test_slow_ack_keeps_one_move_and_caps_sampling_work_at_100(self):
        result = self.run_scenario("budget100")
        self.assertEqual(result["before"], 1, result)
        # One handshake timer may also expire; no busy sampling beyond 100 callbacks.
        self.assertLessEqual(result["beforeTimers"], 101, result)
        self.assertEqual([value["x"] for value in result["moves"]], [1, 200], result)
        self.assertEqual(result["notices"], 0, result)

    def test_up_is_immediate_and_ordered_with_final_coordinates_before_move_ack(self):
        result = self.run_scenario("up_before_ack")
        self.assertEqual([value["type"] for value in result["before"]], ["mouse_down", "mouse_move", "mouse_up"], result)
        self.assertEqual(result["before"][-1]["x"], 4, result)
        self.assertEqual(result["before"][-1]["at"], 41, result)
        sequences = [value["sequence"] for value in result["before"]]
        self.assertEqual(sequences, sorted(set(sequences)), result)
        self.assertEqual(result["after"], result["before"], result)

    def test_old_gesture_ack_releases_its_slot_without_stalling_new_gesture(self):
        result = self.run_scenario("old_gesture_ack")
        self.assertEqual(result["before"], 1, result)
        self.assertEqual([value["x"] for value in result["moves"]], [2, 110], result)

    def test_blur_discards_pending_moves_even_when_old_move_is_unconfirmed(self):
        result = self.run_scenario("blur_discards_pending")
        self.assertEqual(result["before"], 1, result)
        self.assertEqual(result["after"], 1, result)
        self.assertEqual(result["actions"][-1]["type"], "reset_input", result)

    def test_navigation_is_a_barrier_and_late_ack_cannot_send_old_move(self):
        result = self.run_scenario("navigate_barrier")
        self.assertEqual([value["type"] for value in result["before"]], ["mouse_down", "mouse_move", "navigate"], result)
        self.assertEqual(result["after"], result["before"], result)

    def test_hover_blur_discards_unsent_sample_without_a_held_pointer(self):
        self.assertEqual(self.run_scenario("hover_blur")["moves"], 0)

    def test_normal_sampling_rolls_after_100_points_without_stalling_or_replaying(self):
        result = self.run_scenario("normal_segments")
        self.assertEqual([value["x"] for value in result["moves"]], list(range(1, 111)), result)
        self.assertEqual([value["at"] for value in result["moves"]], list(range(20, 2201, 20)), result)

    def test_http_keeps_down_move_up_serial_when_the_move_was_already_posted(self):
        result = self.run_scenario("http_order")
        self.assertEqual([value["type"] for value in result["before"]], ["mouse_down", "mouse_move"], result)
        self.assertEqual([value["type"] for value in result["after"]], ["mouse_down", "mouse_move", "mouse_up"], result)
        self.assertEqual(result["after"][-1]["x"], 4, result)
        sequences = [value["sequence"] for value in result["after"]]
        self.assertEqual(sequences, sorted(set(sequences)), result)

    def test_http_up_discards_old_move_queued_behind_slow_down_request(self):
        result = self.run_scenario("http_up")
        self.assertEqual([value["type"] for value in result["before"]], ["mouse_down"], result)
        self.assertEqual([value["type"] for value in result["after"]], ["mouse_down", "mouse_up"], result)
        self.assertEqual(result["after"][-1]["x"], 4, result)

    def test_http_blur_discards_old_move_queued_behind_slow_down_request(self):
        result = self.run_scenario("http_blur")
        self.assertEqual([value["type"] for value in result["after"]], ["mouse_down", "reset_input"], result)

    def test_sampling_does_not_invent_events_when_pointer_is_stationary(self):
        self.assertEqual(self.run_scenario("no_repeat")["moves"], 1)

    def test_tabs_and_text_form_are_barriers_for_unconfirmed_pointer_moves(self):
        for scenario, kind in (("tab_barrier", "tab"), ("close_tab_barrier", "close_tab"), ("text_barrier", "text")):
            with self.subTest(scenario=scenario):
                result = self.run_scenario(scenario)
                self.assertEqual([value["type"] for value in result["before"]], ["mouse_down", "mouse_move", kind], result)
                self.assertEqual(result["after"], result["before"], result)

    def test_old_generation_ack_cannot_clear_new_socket_move_slot(self):
        result = self.run_scenario("generation_switch")
        self.assertEqual(result["oldMoves"], 1, result)
        self.assertEqual(result["before"], 1, result)
        self.assertEqual([value["x"] for value in result["nextMoves"]], [50, 60], result)

    def test_timeout_stops_sampling_and_late_ack_cannot_replay_pending_point(self):
        result = self.run_scenario("timeout_stops_sampling")
        self.assertFalse(result["operable"], result)
        self.assertTrue(result["inputBlocked"], result)
        self.assertEqual(result["moves"], 1, result)
        self.assertEqual(result["notices"], 1, result)
        self.assertEqual(result["closes"], 1, result)

    def test_pagehide_resets_then_closes_without_replaying_latest_sample(self):
        result = self.run_scenario("pagehide")
        self.assertEqual([value["type"] for value in result["before"]], ["mouse_down", "mouse_move", "reset_input"], result)
        self.assertEqual(result["after"], result["before"], result)
        self.assertEqual(result["pending"], 0, result)
        self.assertFalse(result["operable"], result)
        self.assertEqual(result["closes"], 1, result)


if __name__ == "__main__":
    unittest.main()
