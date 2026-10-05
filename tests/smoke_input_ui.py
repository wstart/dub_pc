"""Owned input fixture checks; no external CAPTCHA, OS clipboard, or OS IME use."""
from __future__ import annotations

import time

from tests.smoke_admin_browser import wait


def run_input_cases(ui, target, mode, reset, snapshot, record, received) -> None:
    """Exercise task-page input against a real isolated AdsPower fixture."""
    screen = ui.locator('#screen')

    def point(selector, ratio=0.5):
        box, bounds = target.locator(selector).bounding_box(), screen.bounding_box()
        return (bounds['x'] + (box['x'] + box['width'] * ratio) * bounds['width'] / 1280,
                bounds['y'] + (box['y'] + box['height'] / 2) * bounds['height'] / 800)

    def click(selector, ratio=0.5, **options):
        ui.mouse.click(*point(selector, ratio), **options)

    def field_value(selector):
        return target.locator(selector).input_value()

    def value_is(selector, value):
        target.wait_for_function(
            '([selector, value]) => document.querySelector(selector).value === value',
            arg=[selector, value], timeout=10000)

    def event_seen(type, **fields):
        return any(event['type'] == type and all(event.get(key) == value
                                                for key, value in fields.items())
                   for event in snapshot()['mouseEvents'])

    def clipboard(text):
        # This verifies the browser paste event path, not a shared system clipboard.
        ui.evaluate('''text => {
            const data = new DataTransfer(); data.setData('text/plain', text);
            document.getElementById('screen').dispatchEvent(new ClipboardEvent(
                'paste', {clipboardData: data, bubbles: true, cancelable: true}));
        }''', text)

    reset()
    ui.mouse.move(*point('#pad'))
    wait(lambda: event_seen('pointermove', target='pad', buttons=0), mode + ' hover')
    ui.mouse.click(*point('#pad'), click_count=2, delay=90)
    wait(lambda: event_seen('dblclick', target='pad', detail=2), mode + ' double click')
    click('#pad', button='right')
    wait(lambda: event_seen('contextmenu', target='pad', button=2), mode + ' context menu')
    click('#pad', button='middle')
    wait(lambda: event_seen('auxclick', target='pad', button=1), mode + ' middle click')
    ui.keyboard.down('Shift')
    ui.keyboard.down('Control')
    try:
        click('#pad')
    finally:
        ui.keyboard.up('Control')
        ui.keyboard.up('Shift')
    wait(lambda: event_seen('pointerdown', target='pad', shift=True, control=True),
         mode + ' modifier click')
    assert any(event['type'] == 'pointerup' and event['buttons'] == 0
               for event in snapshot()['mouseEvents'])
    record(mode + '_mouse_inputs', {'hover': True, 'double_click': True,
                                   'right_click': True, 'middle_click': True,
                                   'modifier_click': True})

    reset()
    wheel_point = point('#pad')
    for delta_mode, amount in ((0, 20), (1, 2), (2, 1)):
        before = len(snapshot()['wheelEvents'])
        ui.evaluate('''({x, y, deltaMode, amount}) => {
            document.getElementById('screen').dispatchEvent(new WheelEvent('wheel', {
                clientX: x, clientY: y, deltaX: 0, deltaY: amount, deltaMode,
                bubbles: true, cancelable: true}));
        }''', {'x': wheel_point[0], 'y': wheel_point[1],
               'deltaMode': delta_mode, 'amount': amount})
        wait(lambda: len(snapshot()['wheelEvents']) > before, mode + ' wheel mode')
        actual = snapshot()['wheelEvents'][-1]
        assert actual['mode'] == 0, actual
        assert actual['dy'] == amount if delta_mode == 0 else actual['dy'] > amount, actual
        record(f'{mode}_wheel_mode_{delta_mode}', {'original': amount, 'target': actual})

    reset()
    click('#field', ratio=0.1)
    ui.keyboard.type('abc')
    ui.keyboard.press('Shift+Digit1')
    value_is('#field', 'abc!')
    # A composition event is a deterministic transport check, not actual OS IME proof.
    ui.evaluate('''() => document.getElementById('screen').dispatchEvent(
        new CompositionEvent('compositionend', {data: '🙂中文', bubbles: true}))''')
    value_is('#field', 'abc!🙂中文')
    clipboard(' 符号+$[]{}')
    value_is('#field', 'abc!🙂中文 符号+$[]{}')
    record(mode + '_text_inputs', {'ordinary': True, 'shift_symbol': True,
                                  'emoji_composition_event': True,
                                  'chinese_composition_event': True, 'paste_event': True,
                                  'value': field_value('#field')})

    reset()
    click('#field', ratio=0.1)
    draft = '中文🙂 符号+$[]{}'
    ui.locator('#remote-text').fill(draft)
    ui.locator('#text-input button').first.click()
    value_is('#field', draft)
    ui.wait_for_function('() => document.getElementById("remote-text").value === ""')
    record(mode + '_text_bar_submission', {'value': draft, 'local_cleared_after_acceptance': True})

    click('#multiline', ratio=0.1)
    text = '第一行🙂\nsecond line +$[]{}\n第三行'
    clipboard(text)
    value_is('#multiline', text)
    click('#editable', ratio=0.1)
    clipboard('可编辑中文🙂')
    target.wait_for_function(
        '() => document.getElementById("editable").textContent === "可编辑中文🙂"')
    click('#password', ratio=0.1)
    ui.keyboard.type('owned-only')
    value_is('#password', 'owned-only')
    record(mode + '_multiline_editable_password', {'multiline_paste': True,
                                                  'contenteditable': True,
                                                  'password': True})

    reset()
    click('#field', ratio=0.1)
    ui.keyboard.type('abc')
    value_is('#field', 'abc')
    ui.keyboard.press('Home')
    ui.keyboard.press('Delete')
    value_is('#field', 'bc')
    ui.keyboard.press('End')
    ui.keyboard.press('Backspace')
    value_is('#field', 'b')
    ui.keyboard.press('ArrowLeft')
    ui.keyboard.type('X')
    value_is('#field', 'Xb')
    ui.keyboard.press('Tab')
    target.wait_for_function('() => document.activeElement.id === "second"')
    ui.keyboard.type('second')
    value_is('#second', 'second')
    ui.keyboard.press('Shift+Tab')
    target.wait_for_function('() => document.activeElement.id === "field"')
    record(mode + '_editing_keys', {'home_delete': True, 'end_backspace': True,
                                    'arrow_insert': True, 'tab': True, 'shift_tab': True})

    ui.keyboard.press('Control+a')
    ui.keyboard.type('shortcut')
    value_is('#field', 'shortcut')
    ui.keyboard.press('Control+z')
    wait(lambda: field_value('#field') != 'shortcut', mode + ' undo')
    undone = field_value('#field')
    ui.keyboard.press('Control+Shift+z')
    value_is('#field', 'shortcut')
    ui.keyboard.press('Control+BracketLeft')
    wait(lambda: any(event['key'] == '[' and event['control'] and not event['meta']
                     for event in snapshot()['keyEvents']), mode + ' control punctuation')
    keys = snapshot()['keyEvents']
    assert any(event['key'].lower() == 'a' and event['meta'] and not event['control']
               for event in keys), keys
    assert any(event['key'].lower() == 'z' and event['meta'] and not event['control']
               for event in keys), keys
    record(mode + '_mac_shortcuts', {'select_all': True, 'undo': True, 'redo': True,
                                    'undo_value': undone, 'control_punctuation': True})

    for scenario in ('outside', 'cancel', 'blur'):
        reset()
        start = point('#slider', ratio=0.02)
        ui.mouse.move(*start)
        ui.mouse.down()
        try:
            wait(lambda: snapshot()['active'], mode + ' pointer held')
            if scenario == 'outside':
                bounds = screen.bounding_box()
                ui.mouse.move(bounds['x'] + bounds['width'] + 30, bounds['y'] - 30, steps=8)
                ui.mouse.up()
            elif scenario == 'cancel':
                ui.evaluate('''() => document.getElementById('screen').dispatchEvent(
                    new PointerEvent('pointercancel', {pointerId: 1, pointerType: 'mouse',
                        bubbles: true, cancelable: true}))''')
            else:
                # The window event emulates loss of browser focus, not OS window automation.
                ui.evaluate('() => window.dispatchEvent(new Event("blur"))')
            wait(lambda: not snapshot()['active'], mode + ' release on ' + scenario)
        finally:
            ui.mouse.up()
        ui.mouse.move(*point('#pad'))
        wait(lambda: event_seen('pointermove', target='pad', buttons=0), mode + ' no stuck press')
        record(mode + '_release_' + scenario, {'released': True, 'later_move_buttons': 0})

    for scenario in ('cancel', 'blur'):
        reset()
        ui.mouse.move(*point('#action'))
        ui.mouse.down()
        try:
            wait(lambda: event_seen('pointerdown', target='action', buttons=1),
                 mode + ' button pressed')
            if scenario == 'cancel':
                ui.evaluate('''() => document.getElementById('screen').dispatchEvent(
                    new PointerEvent('pointercancel', {pointerId: 1, pointerType: 'mouse',
                        bubbles: true, cancelable: true}))''')
            else:
                ui.evaluate('() => window.dispatchEvent(new Event("blur"))')
            wait(lambda: event_seen('pointerup', target='action', buttons=0),
                 mode + ' button cancelled')
            assert snapshot()['actionCount'] == 0, 'Cancel generated a remote button click'
            record(mode + '_cancel_button_' + scenario, {'released': True, 'accidental_clicks': 0})
        finally:
            ui.mouse.up()

    reset()
    box = target.frame_locator('#child').locator('#frame-action').bounding_box()
    bounds = screen.bounding_box()
    ui.mouse.move(bounds['x'] + (box['x'] + 10) * bounds['width'] / 1280,
                  bounds['y'] + (box['y'] + 10) * bounds['height'] / 800)
    ui.mouse.down()
    try:
        wait(lambda: any(event['type'] == 'pointerdown' and event['buttons'] == 1
                         for event in snapshot()['frameMouseEvents']), mode + ' frame button pressed')
        ui.evaluate('() => window.dispatchEvent(new Event("blur"))')
        wait(lambda: any(event['type'] == 'pointerup' and event['buttons'] == 0
                         for event in snapshot()['frameMouseEvents']), mode + ' frame button cancelled')
        assert snapshot()['frameActionCount'] == 0, 'Cancel generated an iframe button click'
        record(mode + '_cancel_iframe_button', {'released': True, 'accidental_clicks': 0})
    finally:
        ui.mouse.up()

    if mode == 'http':
        reset()
        fault = {'rejected': False, 'reset_seen': False}

        def reject_release(route):
            action = route.request.post_data_json
            if action['type'] == 'mouse_up' and not fault['rejected']:
                fault['rejected'] = True
                route.fulfill(status=500, json={'error': 'owned lost mouse release'})
            else:
                if action['type'] == 'reset_input':
                    fault['reset_seen'] = True
                route.continue_()

        ui.route('**/input', reject_release)
        try:
            ui.mouse.move(*point('#action'))
            ui.mouse.down()
            wait(lambda: event_seen('pointerdown', target='action', buttons=1), 'HTTP faulty release held')
            started = time.monotonic()
            ui.mouse.up()
            wait(lambda: event_seen('pointerup', target='action', buttons=0),
                 'HTTP rejected mouse_up safety reset', timeout=4)
            assert fault['rejected'] and fault['reset_seen'], fault
            assert snapshot()['actionCount'] == 0, 'Failed release generated button click'
            record('http_rejected_release_cleanup', {'release_rejected': True, 'reset_reached_server': True,
                                                     'accidental_clicks': 0,
                                                     'release_seconds': round(time.monotonic() - started, 3)})
        finally:
            ui.mouse.up()
            ui.unroute('**/input', reject_release)

        click('#field', ratio=0.1)
        wait(lambda: event_seen('pointerup', target='field', buttons=0), 'HTTP focus click completed')
        draft = 'HTTP拒绝后不得丢失的本地草稿🙂'
        ui.locator('#remote-text').fill(draft)

        def reject_text(route):
            if route.request.post_data_json['type'] == 'text':
                route.fulfill(status=409, json={'error': 'owned fixture rejection'})
            else:
                route.continue_()

        ui.route('**/input', reject_text)
        try:
            ui.locator('#text-input button').first.click()
            ui.wait_for_function('''() => document.getElementById("notice").textContent.includes(
                "owned fixture rejection")''')
            assert ui.locator('#remote-text').input_value() == draft
            record('http_rejection_preserves_draft', {'draft_preserved': True})
        finally:
            ui.unroute('**/input', reject_text)
    assert not any(action['type'] == 'click' for action in received), 'New UI emitted legacy click'
