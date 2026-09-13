"""#252: lineage.html navigates like a canvas -- wheel zooms, drag pans.

#250 gave the page zoom buttons. This is the interaction model on top of
them: the wheel zooms with no modifier and holds the point under the cursor,
a press-and-move grabs the canvas, and a Hand toggle makes that the standing
mode.

The property that is easiest to break here is #213's: a plain click on a node
must still open the cycle-details panel. Panning suppresses exactly one
click, and only when the pointer actually moved, so both behaviours are
asserted side by side in this file.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / 'tests'))

from scripts import techtree_viewer as tv  # noqa: E402
from test_issue250_lineage_viewport import _open  # noqa: E402

NOW = '2026-01-02 01:00:00'

# Reading the scroll box and the zoom in one round trip.
STATE = """() => {
    const g = document.querySelector('[data-lineage-graph-scroll]');
    const s = document.getElementById('lineage-svg');
    const panel = document.getElementById('cycle-details-panel');
    const rect = g.getBoundingClientRect();
    return {
        scrollLeft: g.scrollLeft, scrollTop: g.scrollTop,
        maxScrollLeft: g.scrollWidth - g.clientWidth,
        maxScrollTop: g.scrollHeight - g.clientHeight,
        clientW: g.clientWidth, clientH: g.clientHeight,
        rectLeft: rect.left, rectTop: rect.top,
        svgW: +s.getAttribute('width'),
        zoom: window.lineageRenderer.getZoom(),
        panMode: window.lineageRenderer.getPanMode(),
        panAttr: g.getAttribute('data-pan-mode'),
        cursor: getComputedStyle(g).cursor,
        panelHidden: panel.hidden,
        pageOverflowY: document.documentElement.scrollHeight - document.documentElement.clientHeight,
    };
}"""


# The first node in document order is usually scrolled off screen; a drag
# aimed at negative coordinates does nothing at all and proves nothing.
_VISIBLE_NODE = """() => {
    const g = document.querySelector('[data-lineage-graph-scroll]');
    const box = g.getBoundingClientRect();
    for (const n of document.querySelectorAll('.lineage-node')) {
        const r = n.getBoundingClientRect();
        if (r.left > box.left + 40 && r.right < box.right - 40 &&
            r.top > box.top + 40 && r.bottom < box.bottom - 40) {
            return {x: r.left + r.width / 2, y: r.top + r.height / 2, radius: r.width / 2};
        }
    }
    return null;
}"""


def _centre(state: dict) -> tuple[float, float]:
    return (state['rectLeft'] + state['clientW'] / 2,
            state['rectTop'] + state['clientH'] / 2)


def _wide_and_tall_pages(roots: int = 60, depth: int = 15) -> dict:
    """A forest that overflows on BOTH axes.

    #250's chain fixture is one column wide, so every horizontal assertion in
    this file would have compared 0 with 0 and passed for the wrong reason.
    """
    from test_techtree_viewer import _fixture

    rows = []
    for r in range(roots):
        for d in range(depth):
            sha = f'sha-{r}-{d}'
            rows.append({
                'phase': 'evolution_tree',
                'cycle_id': f'cycle-{r}-{d}',
                'sha': sha,
                'parent_sha': f'sha-{r}-{d - 1}' if d else '',
                'ts': f'2026-01-02T00:{(r + d) % 60:02d}:{(r * d) % 60:02d}Z',
            })
    data = _fixture()
    data['evolution_tree'] = {'nodes': {}, 'current_sha': f'sha-{roots - 1}-{depth - 1}'}
    data['ledger_tail'] = rows
    data['ledger_history'] = rows
    return tv.render_pages(data, host='eeepc', generated_at=NOW)


def _opened_on_full_history(width: int = 1280, height: int = 800):
    """The 'all' projection, scrolled to the middle so nothing clamps at 0."""
    page, close = _open(_wide_and_tall_pages(), width, height)
    page.click('[data-lineage-filter="all"]')
    page.wait_for_timeout(300)
    page.evaluate("""() => {
        const g = document.querySelector('[data-lineage-graph-scroll]');
        g.scrollLeft = Math.round((g.scrollWidth - g.clientWidth) / 2);
        g.scrollTop = Math.round((g.scrollHeight - g.clientHeight) / 2);
    }""")
    page.wait_for_timeout(100)
    room = page.evaluate(STATE)
    # Every assertion below compares scroll positions; with no room to scroll
    # they would all compare 0 with 0 and pass for the wrong reason.
    assert room['maxScrollLeft'] > 300 and room['maxScrollTop'] > 300, room
    return page, close


# --- generator half ---------------------------------------------------------


def test_hand_toggle_and_navigation_hint_are_published() -> None:
    html = tv.render_pages(_fixture_pages(), host='eeepc', generated_at=NOW)['lineage.html']
    assert 'data-lineage-pan-toggle' in html
    assert 'aria-pressed="false"' in html
    assert 'wheel zooms' in html and 'drag pans' in html
    # The cursor is the only affordance that says the graph is draggable.
    assert '.lineage-graph-scroll[data-pan-mode="hand"] { cursor: grab; }' in html
    assert '.lineage-graph-scroll[data-panning="true"] { cursor: grabbing;' in html


def _fixture_pages() -> dict:
    from test_techtree_viewer import _fixture
    return _fixture()


# --- wheel ------------------------------------------------------------------


def test_wheel_zooms_with_no_modifier() -> None:
    page, close = _opened_on_full_history()
    try:
        before = page.evaluate(STATE)
        x, y = _centre(before)
        page.mouse.move(x, y)
        page.mouse.wheel(0, -400)
        page.wait_for_timeout(150)
        zoomed_in = page.evaluate(STATE)
        page.mouse.wheel(0, 800)
        page.wait_for_timeout(150)
        zoomed_out = page.evaluate(STATE)
    finally:
        close()

    assert before['zoom'] == 1, before
    assert zoomed_in['zoom'] > before['zoom'], (before, zoomed_in)
    assert zoomed_out['zoom'] < zoomed_in['zoom'], (zoomed_in, zoomed_out)
    # Zooming must never hand the scroll back to the document.
    assert zoomed_in['pageOverflowY'] <= 1 and zoomed_out['pageOverflowY'] <= 1


def test_wheel_zoom_holds_the_point_under_the_cursor() -> None:
    page, close = _opened_on_full_history()
    try:
        before = page.evaluate(STATE)
        # A pointer deliberately away from the centre: a centre-anchored zoom
        # would pass an on-centre test by accident.
        x = before['rectLeft'] + before['clientW'] * 0.25
        y = before['rectTop'] + before['clientH'] * 0.3
        page.mouse.move(x, y)
        page.mouse.wheel(0, -300)
        page.wait_for_timeout(150)
        after = page.evaluate(STATE)
    finally:
        close()

    assert after['zoom'] > before['zoom'], (before, after)
    offset_x = x - before['rectLeft']
    offset_y = y - before['rectTop']
    # The content coordinate that was under the pointer...
    content_x = (before['scrollLeft'] + offset_x) / before['zoom']
    content_y = (before['scrollTop'] + offset_y) / before['zoom']
    # ...must still be under the pointer afterwards.
    landed_x = content_x * after['zoom'] - after['scrollLeft']
    landed_y = content_y * after['zoom'] - after['scrollTop']
    assert abs(landed_x - offset_x) <= 2, (offset_x, landed_x, before, after)
    assert abs(landed_y - offset_y) <= 2, (offset_y, landed_y, before, after)


def test_shift_wheel_still_scrolls_sideways() -> None:
    page, close = _opened_on_full_history()
    try:
        before = page.evaluate(STATE)
        x, y = _centre(before)
        page.mouse.move(x, y)
        page.keyboard.down('Shift')
        page.mouse.wheel(0, 300)
        page.wait_for_timeout(150)
        page.keyboard.up('Shift')
        after = page.evaluate(STATE)
    finally:
        close()

    assert after['scrollLeft'] > before['scrollLeft'], (before, after)
    assert after['zoom'] == before['zoom'], 'shift+wheel must scroll, not zoom'


# --- drag -------------------------------------------------------------------


def _drag(page, start, dx, dy, button='left'):
    page.mouse.move(*start)
    page.mouse.down(button=button)
    page.mouse.move(start[0] + dx, start[1] + dy, steps=10)
    page.mouse.up(button=button)
    page.wait_for_timeout(120)


def test_drag_pans_the_graph() -> None:
    page, close = _opened_on_full_history()
    try:
        before = page.evaluate(STATE)
        _drag(page, _centre(before), -140, -70)
        after = page.evaluate(STATE)
    finally:
        close()

    # Dragging left moves the content left, which means scrolling right.
    assert after['scrollLeft'] == pytest.approx(before['scrollLeft'] + 140, abs=3), (before, after)
    assert after['scrollTop'] == pytest.approx(before['scrollTop'] + 70, abs=3), (before, after)
    assert after['zoom'] == before['zoom'], 'a drag must not change the zoom'
    assert after['pageOverflowY'] <= 1, after


def test_middle_button_drag_pans() -> None:
    page, close = _opened_on_full_history()
    try:
        before = page.evaluate(STATE)
        _drag(page, _centre(before), -100, 0, button='middle')
        after = page.evaluate(STATE)
    finally:
        close()

    assert after['scrollLeft'] == pytest.approx(before['scrollLeft'] + 100, abs=3), (before, after)


def test_a_drag_that_ends_on_a_node_does_not_open_it() -> None:
    """The #213 panel opens on click, and a drag ends with a click event."""
    page, close = _opened_on_full_history()
    try:
        node = page.evaluate(_VISIBLE_NODE)
        assert node, 'no node is on screen to drag from'
        before = page.evaluate(STATE)
        _drag(page, (node['x'], node['y']), 130, 40)
        after = page.evaluate(STATE)
    finally:
        close()

    # The drag has to have done something, or the panel staying shut is
    # simply the absence of any event at all.
    assert after['scrollLeft'] != before['scrollLeft'], (before, after)
    assert after['panelHidden'], 'a drag must not open the cycle-details panel'


def test_a_drag_is_suppressed_even_without_pointer_capture() -> None:
    """The previous test passes for a reason it does not name.

    Once the movement threshold is crossed the renderer takes pointer
    capture, and a captured pointer retargets its click to the capturing
    element -- so the node never sees the click regardless of the explicit
    suppression. Removing setPointerCapture proves the suppression itself
    works: without this test, deleting `pan.suppressClick = pan.moved`
    changes nothing that any test observes.
    """
    page, close = _opened_on_full_history()
    try:
        page.evaluate("() => { delete Element.prototype.setPointerCapture; }")
        node = page.evaluate(_VISIBLE_NODE)
        assert node, 'no node is on screen to drag from'
        # Past the 4px threshold, still inside the 9px node: the click really
        # does land on the node, so only the suppression can stop it.
        _drag(page, (node['x'], node['y']), 7, 0)
        lands_on_node = page.evaluate(
            "([x, y]) => { const el = document.elementFromPoint(x, y);"
            " return !!(el && el.closest && el.closest('.lineage-node')); }",
            [node['x'] + 7, node['y']],
        )
        after = page.evaluate(STATE)
    finally:
        close()

    assert lands_on_node, 'the drag ended off the node, so this proves nothing'
    assert after['panelHidden'], 'the drag-ending click must be suppressed on its own'


def test_a_plain_click_on_a_node_still_opens_the_panel() -> None:
    """The other half of the same rule -- without this the suppression could
    simply be swallowing every click and the drag test would still pass."""
    page, close = _opened_on_full_history()
    try:
        page.evaluate(
            "() => document.querySelector('.lineage-node')"
            ".dispatchEvent(new MouseEvent('click', {bubbles: true, cancelable: true}))"
        )
        page.wait_for_selector('.cycle-details-body h3', timeout=4000)
        dispatched = page.evaluate("() => document.getElementById('cycle-details-panel').hidden")

        # And again through a real pointer, which is what a user does.
        page.click('#cycle-details-close')
        page.wait_for_timeout(120)
        box = page.evaluate(_VISIBLE_NODE)
        assert box, 'no node is on screen to click'
        page.mouse.move(box['x'], box['y'])
        page.mouse.down()
        page.mouse.up()
        page.wait_for_selector('.cycle-details-body h3', timeout=4000)
        real = page.evaluate("() => document.getElementById('cycle-details-panel').hidden")
    finally:
        close()

    assert dispatched is False
    assert real is False, 'a press with no movement is a click, not a pan'


# --- hand tool --------------------------------------------------------------


def test_hand_toggle_switches_mode_and_cursor() -> None:
    page, close = _opened_on_full_history()
    try:
        start = page.evaluate(STATE)
        page.click('[data-lineage-pan-toggle]')
        page.wait_for_timeout(100)
        pressed = page.evaluate(STATE)
        aria = page.get_attribute('[data-lineage-pan-toggle]', 'aria-pressed')
        page.click('[data-lineage-pan-toggle]')
        page.wait_for_timeout(100)
        released = page.evaluate(STATE)
    finally:
        close()

    assert start['panMode'] == 'select' and start['panAttr'] == 'select', start
    assert pressed['panMode'] == 'hand' and pressed['panAttr'] == 'hand', pressed
    assert pressed['cursor'] == 'grab', pressed
    assert aria == 'true'
    assert released['panMode'] == 'select' and released['cursor'] != 'grab', released


def test_holding_space_gives_the_hand_without_leaving_select_mode() -> None:
    page, close = _opened_on_full_history()
    try:
        page.keyboard.down('Space')
        page.wait_for_timeout(100)
        held = page.evaluate(STATE)
        page.keyboard.up('Space')
        page.wait_for_timeout(100)
        let_go = page.evaluate(STATE)
    finally:
        close()

    assert held['panAttr'] == 'hand' and held['cursor'] == 'grab', held
    # Space is a temporary hand: the standing mode must be untouched.
    assert held['panMode'] == 'select', held
    assert let_go['panAttr'] == 'select' and let_go['cursor'] != 'grab', let_go


def test_h_shortcut_toggles_the_hand_tool() -> None:
    page, close = _opened_on_full_history()
    try:
        page.keyboard.press('h')
        page.wait_for_timeout(100)
        after = page.evaluate(STATE)
    finally:
        close()

    assert after['panMode'] == 'hand', after
