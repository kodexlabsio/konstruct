"""The MCP App views in a real browser (Chromium through Playwright), drawn as an MCP host draws
them: in a sandboxed frame, handed the tool's result over the Apps bridge.

Each view is given a result recorded from the tool (``viewer/tests/results/``) and checked for what
it draws, for WCAG 2.2 AA with axe-core, for making no request of any kind, and for drawing the
repository's text as text. Skipped when Playwright's Chromium is not installed, as
``tests/test_browser.py`` is.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from konstruct.outputs.html import view_html
from tests.test_browser import axe_violations, browser

__all__ = ['browser']

pytestmark = pytest.mark.browser

RESULTS = Path(__file__).resolve().parents[1] / 'viewer' / 'tests' / 'results'

# A host as small as the extension allows: it answers ui/initialize, hands over the tool's input and
# result once the view says it is ready, and sizes the frame to what the view asks for.
HOST = """<!doctype html>
<html lang="en"><head><meta charset="utf-8"><title>Host</title></head>
<body style="margin:0">
<iframe id="view" sandbox="allow-scripts" title="Konstruct view" style="border:0;width:100%;display:block"></iframe>
<script>
const frame = document.getElementById('view');
window.host = {messages: [], ready: false, context: {}, send: []};
window.addEventListener('message', event => {
    if (event.source !== frame.contentWindow) return;
    const message = event.data;
    host.messages.push(message);
    const reply = data => frame.contentWindow.postMessage({jsonrpc: '2.0', ...data}, '*');
    if (message.method === 'ui/initialize')
        reply({id: message.id, result: {protocolVersion: '2026-01-26', hostInfo: {name: 'test', version: '1'},
               hostCapabilities: {}, hostContext: host.context}});
    else if (message.method === 'ui/notifications/initialized') {
        host.ready = true;
        for (const data of host.send) reply(data);
    } else if (message.method === 'ui/notifications/size-changed')
        frame.style.height = message.params.height + 'px';
});
</script>
</body></html>"""


def recorded(name: str) -> dict[str, Any]:
    data: dict[str, Any] = json.loads((RESULTS / f'{name}.json').read_text(encoding='utf-8'))
    return data


@pytest.fixture
def host(browser: Any) -> Iterator[Any]:
    pages: list[Any] = []

    def open_view(
        view: str, call: dict[str, Any], *, theme: str = 'dark', locale: str = 'en-GB', **options: Any
    ) -> Any:
        options.setdefault('viewport', {'width': 900, 'height': 700})
        context = browser.new_context(**options)
        page = context.new_page()
        page.problems = []
        page.on('pageerror', lambda error: page.problems.append(str(error)))
        page.on('console', lambda m: page.problems.append(m.text) if m.type in ('error', 'warning') else None)
        # The view connects nowhere: any request at all, even for an image, is a problem.
        page.on(
            'request',
            lambda r: page.problems.append(f'request {r.url}') if not r.url.startswith('data:') else None,
        )
        page.set_content(HOST)
        notifications = [
            {'method': 'ui/notifications/tool-input', 'params': {'arguments': call.get('arguments', {})}},
            {'method': 'ui/notifications/tool-result', 'params': call['result']},
        ]
        page.evaluate(
            '([context, send, html]) => { host.context = context; host.send = send; '
            'document.getElementById("view").srcdoc = html; }',
            [{'theme': theme, 'locale': locale}, notifications, view_html(view)],
        )
        page.wait_for_function('host.ready')
        frame = page.frames[1]
        frame.wait_for_selector('main.view:not([data-state="waiting"])')
        page.view = frame
        pages.append(page)
        return page

    yield open_view
    for page in pages:
        assert page.problems == [], page.problems
        page.context.close()


def test_impact_draws_the_walk_step_by_step(host: Any) -> None:
    page = host('impact', recorded('impact'))
    frame = page.view
    assert frame.inner_text('#view-title') == 'stg_payments.amount'
    lanes = frame.locator('.lane h3').all_inner_texts()
    assert lanes == ['Changed', 'Step 1', 'Step 2']
    step2 = frame.locator('.lane').nth(2).inner_text()
    assert 'customer_ltv.lifetime_value' in step2 and 'passed through' in step2
    assert 'through jaffle_shop_daily.build_customer_ltv' in step2
    assert 'customer_dashboard' in frame.inner_text('#section-exposures + ul')
    assert 'Repository' not in frame.inner_text('.view-tool')
    assert 'text from the repository' in frame.inner_text('.untrusted')
    # The host was told the view's height, and drew the frame to fit it.
    sizes = page.evaluate('host.messages.filter(m => m.method === "ui/notifications/size-changed").length')
    assert sizes >= 1


def test_explain_draws_the_formula_and_its_wording(host: Any) -> None:
    page = host('explain', recorded('explain-composed'))
    frame = page.view
    assert frame.inner_text('#view-title').startswith('customers.customer_lifetime_value')
    assert 'is the sum of ([amount] divided by 100)' in frame.inner_text('.lead')
    assert 'SUM(raw_payments.amount / 100)' in frame.inner_text('.code pre')
    frame.click('.code [role="tab"]:nth-child(2)')
    assert 'SUM(stg_payments.amount)' in frame.inner_text('.code pre')
    steps = frame.locator('.step').all_inner_texts()
    assert len(steps) == 3 and 'LEFT JOIN ON: stg_payments.order_id' in steps[0]
    assert 'keeping every row of the left side' in steps[0]


def test_gate_draws_each_finding_with_its_file_and_fix(host: Any) -> None:
    page = host('gate', recorded('gate'))
    frame = page.view
    assert 'Gate failed' in frame.inner_text('#view-title')
    assert frame.inner_text('.lead') == '1 finding to fix before proposing the change'
    finding = frame.inner_text('.finding')
    assert 'stg_payments.amount is removed but still read by customers, orders' in finding
    assert 'models/staging/stg_payments.sql' in finding and 'Keep the column' in finding
    # The test host's frame grants no clipboard, so the button selects the path shown instead.
    frame.click('.finding .copy')
    assert frame.inner_text('.finding .copy') == 'Selected: copy it with your keyboard'
    assert frame.evaluate('String(getSelection())') == 'models/staging/stg_payments.sql'
    passed = host('gate', recorded('gate-passed'), theme='light')
    assert 'Gate passed' in passed.view.inner_text('#view-title')
    assert passed.view.evaluate('document.documentElement.dataset.theme') == 'light'


def test_conform_draws_each_verdict_with_both_formulas_and_the_witness(host: Any) -> None:
    page = host('conform', recorded('conform'))
    frame = page.view
    stats = frame.inner_text('.stats')
    assert '4\nMetrics' in stats and '2\nconforms' in stats
    differs = frame.locator('.finding').first.inner_text()
    assert 'net_revenue' in differs and 'differs' in differs
    assert 'SUM(payments.amount)' in differs and 'SUM(raw_payments.amount / 100)' in differs
    assert "status = 'completed'" in differs and "where amount = 100, status = 'returned'" in differs
    assert 'The same, with an error of at most 2.7e-19.' in frame.inner_text('.capped.findings')


def test_an_error_result_is_shown_as_the_error(host: Any) -> None:
    page = host('impact', recorded('impact-error'))
    page.view.wait_for_selector('.view-error')
    assert "nothing in the graph matches 'nope'" in page.view.inner_text('.view-error')


def test_the_views_follow_the_hosts_locale(host: Any) -> None:
    page = host('gate', recorded('gate'), locale='de-DE')
    assert 'Gate nicht bestanden' in page.view.inner_text('#view-title')
    assert page.view.evaluate('document.documentElement.lang') == 'de'
    assert (
        host('explain', recorded('explain-composed'), locale='ar').view.evaluate(
            'document.documentElement.dir'
        )
        == 'rtl'
    )


# Whether the last character of an element's text is drawn rightmost on its line, as it is when
# the text reads left to right; right to left, it is drawn leftmost.
ENDS_ON_THE_RIGHT = """selector => {
    const element = document.querySelector(selector);
    const walker = document.createTreeWalker(element, NodeFilter.SHOW_TEXT);
    const glyphs = [];
    for (let node = walker.nextNode(); node; node = walker.nextNode())
        for (let i = 0; i < node.data.length; i++) {
            if (!node.data[i].trim()) continue;
            const range = document.createRange();
            range.setStart(node, i);
            range.setEnd(node, i + 1);
            glyphs.push(range.getBoundingClientRect());
        }
    const last = glyphs[glyphs.length - 1];
    const line = glyphs.filter(g => Math.abs(g.top - last.top) < 2);
    return line.every(g => g.right <= last.right + 0.5);
}"""


@pytest.mark.parametrize('locale', ['en-GB', 'ar'])
def test_english_and_code_read_left_to_right_in_any_language(host: Any, locale: str) -> None:
    """Konstruct's English and the code read left to right on a left-to-right or right-to-left
    page, even when they start with a name written right to left."""
    call = recorded('explain-composed')
    english = call['result']['structuredContent']['english']
    english['sentence'] = 'הכנסות נטו (customers.net) is the sum of [amount].'
    english['names'][0]['wording'] = '[סכום (raw_payments)]'
    page = host('explain', call, locale=locale)
    for selector in (
        '.sentence',
        '[aria-labelledby="names-title"] .wording',
        '.formula-text',
        '.conditions-list code',
        '#view-title code',
    ):
        assert page.view.evaluate(ENDS_ON_THE_RIGHT, selector), (locale, selector)


def test_a_large_result_is_drawn_a_page_at_a_time(host: Any) -> None:
    call = recorded('impact')
    data = call['result']['structuredContent']
    column = data['columns'][0]
    data['columns'] = [{**column, 'id': f'c{i}', 'label': f'm{i}.c'} for i in range(2500)]
    data['tests'] = [{**data['tests'][0], 'id': f't{i}', 'label': f'test_{i}'} for i in range(450)]
    page = host('impact', call)
    frame = page.view
    assert frame.locator('.lane').nth(1).locator('li').count() == 100
    assert 'Showing 100 of 2500' in frame.inner_text('.lane >> nth=1')
    tests = frame.locator('#section-tests ~ ul li')
    assert tests.count() == 100
    frame.click('#section-tests ~ .more button')
    assert tests.count() == 200


def test_repository_text_is_drawn_as_text_and_fetches_nothing(host: Any) -> None:
    hostile = '<img src="https://example.com/x.png" onerror="parent.postMessage(1, \'*\')"><b>bold</b>'
    call = recorded('gate')
    finding = call['result']['structuredContent']['findings'][0]
    finding['message'] = hostile
    finding['path'] = '<script>alert(1)</script>.sql'
    page = host('gate', call)
    frame = page.view
    assert hostile in frame.inner_text('.finding .message')
    assert frame.locator('.finding img, .finding b, .finding script').count() == 0
    # The host heard nothing but the bridge's own messages.
    methods = page.evaluate('host.messages.map(m => m.method ?? null)')
    assert all(isinstance(m, str) and m.startswith('ui/') for m in methods)


@pytest.mark.parametrize('theme', ['dark', 'light'])
def test_each_view_meets_wcag(host: Any, theme: str) -> None:
    calls = {
        'impact': recorded('impact'),
        'explain': recorded('explain-composed'),
        'gate': recorded('gate'),
        'conform': recorded('conform'),
    }
    for view, call in calls.items():
        page = host(view, call, theme=theme)
        assert axe_violations(page.view) == [], (view, theme)
