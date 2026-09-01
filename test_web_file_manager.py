import base64
import re
import tempfile
import unittest
from collections import Counter
from html.parser import HTMLParser
from pathlib import Path

import tornado.web
from tornado.testing import AsyncHTTPTestCase

import web_file_manager as webfm


class IdCollector(HTMLParser):
    def __init__(self):
        super().__init__()
        self.ids = []

    def handle_starttag(self, _tag, attrs):
        element_id = dict(attrs).get("id")
        if element_id:
            self.ids.append(element_id)


class FrontendContractTest(unittest.TestCase):
    def setUp(self):
        self.parser = IdCollector()
        self.parser.feed(webfm.HTML)
        self.ids = set(self.parser.ids)

    def test_dom_ids_are_unique(self):
        duplicates = [
            element_id
            for element_id, count in Counter(self.parser.ids).items()
            if count > 1
        ]
        self.assertEqual(duplicates, [])

    def test_static_javascript_id_references_exist(self):
        jquery_style = set(re.findall(r'\$\("([^"]+)"\)', webfm.HTML))
        native_style = set(
            re.findall(r'document\.getElementById\("([^"]+)"\)', webfm.HTML)
        )
        dynamic_ids = {"force-text-btn", "markdown-mathjax-script"}
        missing = (jquery_style | native_style) - self.ids - dynamic_ids
        self.assertEqual(missing, set())

    def test_cloud_light_unified_sidebar_shell_is_present(self):
        required_ids = {
            "nav-rail",
            "favorites",
            "pane-left",
            "pane-right",
            "preview-title",
            "preview-download",
        }
        self.assertTrue(required_ids.issubset(self.ids))
        self.assertIn('document.documentElement.dataset.theme = "cloud-light"', webfm.HTML)
        self.assertNotIn('<select id="theme-select"', webfm.HTML)
        self.assertLess(webfm.HTML.index('id="pane-left"'), webfm.HTML.index('id="nav-rail"'))
        self.assertLess(webfm.HTML.index('id="nav-rail"'), webfm.HTML.index('id="pane-right"'))
        self.assertIn("grid-template-columns: 0 minmax(0, 1fr);", webfm.HTML)
        self.assertIn('id="btn-sidebar-reopen"', webfm.HTML)
        self.assertNotIn('id="nav-scrim"', webfm.HTML)

    def test_mobile_uses_explicit_preview_state(self):
        self.assertIn('.app.preview-open #pane-left { display: none; }', webfm.HTML)
        self.assertIn('.app.preview-open #pane-right { display: flex; }', webfm.HTML)
        self.assertIn('id="btn-back-list"', webfm.HTML)

    def test_favorites_are_the_only_collapsible_shortcut_section(self):
        self.assertNotIn('id="locations-heading"', webfm.HTML)
        self.assertNotIn('id="nav-home"', webfm.HTML)
        self.assertIn('id="btn-favorites-toggle"', webfm.HTML)
        self.assertIn('id="favorites" hidden', webfm.HTML)
        self.assertIn("function setFavoritesExpanded(expanded)", webfm.HTML)
        self.assertIn("setFavoritesExpanded(false);", webfm.HTML)

    def test_hidden_files_toggle_uses_open_and_closed_eye_states(self):
        self.assertIn('id="icon-eye-closed"', webfm.HTML)
        self.assertIn('id="hidden-eye-icon" href="#icon-eye-closed"', webfm.HTML)
        self.assertIn(
            '$("hidden-eye-icon").setAttribute("href", visible ? "#icon-eye" : "#icon-eye-closed")',
            webfm.HTML,
        )
        self.assertIn('btn.setAttribute("aria-pressed", visible ? "true" : "false")', webfm.HTML)

    def test_video_preview_has_five_second_keyboard_seek(self):
        self.assertIn("const VIDEO_SEEK_STEP = 5;", webfm.HTML)
        self.assertIn("function seekVideo(delta)", webfm.HTML)
        self.assertIn('sel.kind === "video"', webfm.HTML)
        self.assertIn(
            'video.setAttribute("aria-keyshortcuts", "ArrowLeft ArrowRight")',
            webfm.HTML,
        )

    def test_video_preview_starts_muted(self):
        self.assertIn("video.defaultMuted = true;", webfm.HTML)
        self.assertIn("video.muted = true;", webfm.HTML)

    def test_background_refresh_preserves_unchanged_preview_dom(self):
        self.assertIn("function sameEntryRevision(before, after)", webfm.HTML)
        self.assertIn("const keepPreviewDom = sameEntryRevision", webfm.HTML)
        self.assertIn(
            "load(state.cwd, { preservePreview: true });",
            webfm.HTML,
        )
        self.assertIn(
            "else if (!keepPreviewDom) renderPreview(refreshedEntry);",
            webfm.HTML,
        )

    def test_markdown_preview_typesets_latex_with_local_mathjax(self):
        self.assertIn("function protectMarkdownMath(source)", webfm.HTML)
        self.assertIn("function restoreMarkdownMath(container, tokens)", webfm.HTML)
        self.assertIn("function ensureMarkdownMathJax()", webfm.HTML)
        self.assertIn('script.src = "/mathjax/es5/tex-mml-chtml.js";', webfm.HTML)
        self.assertIn("startup: { typeset: false }", webfm.HTML)
        self.assertIn("function typesetMarkdownMath(container)", webfm.HTML)
        self.assertIn("typesetMarkdownMath(body);", webfm.HTML)


class FileHandlerRangeTest(AsyncHTTPTestCase):
    DATA = b"0123456789abcdef"

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.old_home = webfm.HOME
        self.old_auth = webfm.AUTH
        webfm.HOME = Path(self.temp_dir.name).resolve()
        webfm.AUTH = "tester:secret"
        (webfm.HOME / "sample.mp4").write_bytes(self.DATA)
        super().setUp()

    def tearDown(self):
        super().tearDown()
        webfm.HOME = self.old_home
        webfm.AUTH = self.old_auth
        self.temp_dir.cleanup()

    def get_app(self):
        return tornado.web.Application([(r"/api/file", webfm.FileHandler)])

    @property
    def auth_headers(self):
        token = base64.b64encode(webfm.AUTH.encode()).decode()
        return {"Authorization": "Basic " + token}

    def fetch_file(self, range_header=None):
        headers = self.auth_headers
        if range_header:
            headers["Range"] = range_header
        return self.fetch("/api/file?mode=raw&path=/sample.mp4", headers=headers)

    def test_full_response_advertises_range_support(self):
        response = self.fetch_file()

        self.assertEqual(response.code, 200)
        self.assertEqual(response.body, self.DATA)
        self.assertEqual(response.headers["Content-Type"], "video/mp4")
        self.assertEqual(response.headers["Content-Length"], str(len(self.DATA)))
        self.assertEqual(response.headers["Accept-Ranges"], "bytes")
        self.assertNotIn("Content-Range", response.headers)

    def test_explicit_range_returns_partial_content(self):
        response = self.fetch_file("bytes=2-5")

        self.assertEqual(response.code, 206)
        self.assertEqual(response.body, self.DATA[2:6])
        self.assertEqual(response.headers["Content-Length"], "4")
        self.assertEqual(
            response.headers["Content-Range"],
            f"bytes 2-5/{len(self.DATA)}",
        )

    def test_open_and_suffix_ranges(self):
        cases = [
            ("bytes=12-", self.DATA[12:], "bytes 12-15/16"),
            ("bytes=-4", self.DATA[-4:], "bytes 12-15/16"),
            ("bytes=14-99", self.DATA[14:], "bytes 14-15/16"),
        ]

        for header, expected_body, expected_content_range in cases:
            with self.subTest(header=header):
                response = self.fetch_file(header)
                self.assertEqual(response.code, 206)
                self.assertEqual(response.body, expected_body)
                self.assertEqual(
                    response.headers["Content-Range"], expected_content_range
                )

    def test_unsatisfiable_and_multiple_ranges_return_416(self):
        for header in ("bytes=99-", "bytes=5-2", "bytes=0-1,4-5", "items=0-1"):
            with self.subTest(header=header):
                response = self.fetch_file(header)
                self.assertEqual(response.code, 416)
                self.assertEqual(response.body, b"")
                self.assertEqual(
                    response.headers["Content-Range"],
                    f"bytes */{len(self.DATA)}",
                )


if __name__ == "__main__":
    unittest.main()
