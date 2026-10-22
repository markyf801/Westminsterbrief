"""Values inside onclick="..." attributes must go through tojson|forceescape.

HTML-escaping alone is not enough there: the browser turns &#39; back into a
quote before running the JavaScript, so a search term like  x');alert(1)//
would run as code."""
import html
import re
from pathlib import Path

from flask import Flask, render_template_string

TEMPLATES = Path(__file__).parents[1] / 'templates'
_QUOTED_IN_HANDLER = re.compile(r"""\son[a-z]+="[^"]*'[^'"]*\{\{""")


def test_no_quoted_template_values_in_event_handlers():
    bad = []
    for f in TEMPLATES.rglob('*.html'):
        for n, line in enumerate(f.read_text(encoding='utf-8').splitlines(), 1):
            if _QUOTED_IN_HANDLER.search(line):
                bad.append(f'{f.relative_to(TEMPLATES)}:{n}')
    assert not bad, bad


def test_tojson_forceescape_keeps_value_a_string():
    hostile = "x');alert(1);//\"<b>"
    with Flask(__name__).test_request_context('/'):
        out = render_template_string('<button onclick="f({{ t | tojson | forceescape }})">', t=hostile)
    attr = html.unescape(re.search(r'onclick="([^"]*)"', out).group(1))
    assert attr.startswith('f("') and attr.endswith('")')
    assert "'" not in attr and '<' not in attr
