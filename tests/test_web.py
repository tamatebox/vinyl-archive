"""The browser half, checked from Python.

There is no JS test runner here on purpose — node is not a dependency of a
Raspberry Pi capture server. What these cover is the one failure this UI has
actually had: markup and script drifting apart. Renaming a container broke
both lists at once, and because the render error is swallowed by the polling
loop it showed up as a blank page rather than an error, which is the hardest
kind to notice.
"""

import re

from vinyl_archive.main import STATIC_DIR

# Which scripts each page loads, and therefore whose element lookups have to
# resolve against it.
PAGES = {
    "index.html": ("common.js", "app.js"),
    "history.html": ("common.js", "history.js"),
}

ID_ATTR = re.compile(r'id="([^"]+)"')
LOOKUP = re.compile(r'\$\("([^"]+)"\)')
SCRIPT_SRC = re.compile(r'<script src="/static/([^"]+)"')


def read(name: str) -> str:
    return (STATIC_DIR / name).read_text()


def test_every_element_the_scripts_ask_for_exists():
    for page, scripts in PAGES.items():
        present = set(ID_ATTR.findall(read(page)))
        for script in scripts:
            wanted = set(LOOKUP.findall(read(script)))
            missing = sorted(wanted - present)
            assert not missing, f"{script} looks up {missing}, absent from {page}"


def test_pages_load_exactly_the_scripts_they_are_checked_against():
    """Otherwise the check above passes while the browser loads something
    else — including a page that forgets common.js and fails on line one."""
    for page, scripts in PAGES.items():
        assert SCRIPT_SRC.findall(read(page)) == list(scripts)


def test_shared_script_comes_first():
    """common.js defines what the page scripts call at their top level, so
    loading it second is a ReferenceError before anything renders."""
    for page, scripts in PAGES.items():
        assert scripts[0] == "common.js"


def test_pages_reach_each_other():
    """Each page is the way to the other one; a page with no link out of it
    is only reachable by typing the URL."""
    assert 'href="/history"' in read("index.html")
    assert 'href="/"' in read("history.html")
