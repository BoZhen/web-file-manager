import base64
import contextlib
import io
import json
import os
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


class ConfigurationTest(unittest.TestCase):
    def test_bind_addresses_support_multiple_interfaces(self):
        self.assertEqual(
            webfm._parse_bind_addresses("127.0.0.1, 192.168.1.10"),
            ("127.0.0.1", "192.168.1.10"),
        )
        self.assertEqual(webfm._parse_bind_addresses(""), ("0.0.0.0",))

    def test_latex_files_use_the_text_preview(self):
        self.assertEqual(webfm._classify(Path("notes.tex")), "text")


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

    def test_unified_sidebar_shell_is_present(self):
        required_ids = {
            "nav-rail",
            "favorites",
            "pane-left",
            "pane-right",
            "preview-title",
            "preview-copy",
            "preview-download",
        }
        self.assertTrue(required_ids.issubset(self.ids))
        self.assertLess(webfm.HTML.index('id="pane-left"'), webfm.HTML.index('id="nav-rail"'))
        self.assertLess(webfm.HTML.index('id="nav-rail"'), webfm.HTML.index('id="pane-right"'))
        self.assertIn("grid-template-columns: 0 minmax(0, 1fr);", webfm.HTML)
        self.assertIn('id="btn-sidebar-reopen"', webfm.HTML)
        self.assertNotIn('id="nav-scrim"', webfm.HTML)

    def test_mobile_uses_explicit_preview_state(self):
        self.assertIn('.app.preview-open #pane-left { display: none; }', webfm.HTML)
        self.assertIn('.app.preview-open #pane-right { display: flex; }', webfm.HTML)
        self.assertIn('id="btn-back-list"', webfm.HTML)

    def test_preview_subtitle_uses_absolute_path_and_clears_stale_tooltip(self):
        render = webfm.HTML.split("function renderPreview(entry) {", 1)[1].split("const copy =", 1)[0]
        self.assertIn("pathForEditing(fullPath(entry.name))", render)
        self.assertIn("metaEl.textContent = metaPath;", render)
        self.assertIn("metaEl.title = metaPath;", render)
        self.assertNotIn("fmtSize(", render)
        self.assertNotIn("fmtTime(", render)
        reset = webfm.HTML.split("function resetPreview() {", 1)[1].split("function clearPreviewBody", 1)[0]
        self.assertIn('$("preview-meta").title = "";', reset)

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
        # the shown file changed (same name and kind): reloaded in place
        self.assertIn("refreshPreview(refreshedEntry);", webfm.HTML)

    def test_the_shown_file_is_watched_and_reloaded_in_place(self):
        self.assertIn('fetch("/api/stat?path="', webfm.HTML)
        self.assertIn("window.setInterval(pollPreview, WATCH_MS);", webfm.HTML)
        self.assertIn('document.visibilityState !== "visible"', webfm.HTML)
        self.assertIn("watchPreview(entry);", webfm.HTML)
        self.assertIn("watchPreview(null);", webfm.HTML)
        # cache-busting by the exact revision once it is known
        self.assertIn("const v = entry.rev || entry.mtime || 0;", webfm.HTML)
        self.assertIn('id="preview-live"', webfm.HTML)

    def test_markdown_preview_typesets_latex_with_local_mathjax(self):
        self.assertIn("function protectMarkdownMath(source)", webfm.HTML)
        self.assertIn("function restoreMarkdownMath(container, tokens)", webfm.HTML)
        self.assertIn("function ensureMarkdownMathJax()", webfm.HTML)
        self.assertIn('script.src = "/mathjax/es5/tex-mml-chtml.js";', webfm.HTML)
        self.assertIn("startup: { typeset: false }", webfm.HTML)
        self.assertIn("function typesetMarkdownMath(container)", webfm.HTML)
        self.assertIn("typesetMarkdownMath(body);", webfm.HTML)

    def test_text_previews_offer_full_content_copy_before_download(self):
        self.assertLess(
            webfm.HTML.index('id="preview-copy"'),
            webfm.HTML.index('id="preview-download"'),
        )
        self.assertIn(
            'const COPYABLE_KINDS = new Set(["markdown", "html", "text"]);',
            webfm.HTML,
        )
        self.assertIn("async function copyFullText(entry, button)", webfm.HTML)
        self.assertIn('fileUrlFor(entry, "text", "&full=1")', webfm.HTML)
        self.assertIn("navigator.clipboard.writeText(text)", webfm.HTML)


class ThemeTest(unittest.TestCase):
    """The default theme comes from WEBFM_THEME, the menu offers every theme the CSS
    defines, and component rules take their colors from the theme."""

    BLOCK = re.compile(r':root\[data-theme="([a-z0-9-]+)"\][^{]*\{([^}]*)\}')

    def css_blocks(self):
        return {theme_id: body for theme_id, body in self.BLOCK.findall(webfm.HTML)}

    def test_menu_and_css_offer_the_same_themes(self):
        listed = [theme_id for theme_id, _, _ in webfm.THEMES]
        self.assertEqual(len(listed), len(set(listed)))
        self.assertEqual(set(listed), set(self.css_blocks()))

    def test_dark_flag_matches_the_css_color_scheme(self):
        blocks = self.css_blocks()
        for theme_id, _, dark in webfm.THEMES:
            self.assertEqual("color-scheme: dark" in blocks[theme_id], dark, theme_id)

    def test_default_is_dark_and_bad_values_fall_back(self):
        self.assertEqual(webfm.FALLBACK_THEME, "darcula")
        self.assertEqual(webfm._default_theme("nord-light"), "nord-light")
        with contextlib.redirect_stderr(io.StringIO()) as err:
            self.assertEqual(webfm._default_theme("no-such-theme"), "darcula")
        self.assertIn("WEBFM_THEME", err.getvalue())

    def test_page_applies_a_saved_choice_or_the_default_before_its_css(self):
        html = webfm.HTML
        self.assertIn('id="theme-select"', html)
        self.assertIn('id="hljs-theme"', html)
        self.assertIn("github-dark.min.css", html)
        self.assertLess(html.index("applyTheme(wfmSavedTheme);"), html.index("<style>"))

    def test_component_rules_take_colors_from_the_theme(self):
        # Cloud Light's former literals now live only in its own block.
        css = webfm.HTML[webfm.HTML.index("<style>"):webfm.HTML.index("</style>")]
        components = css[css.index("* { box-sizing: border-box; }"):]
        for literal in ("#344054", "#98a2b3", "#b2b8c2", "#1849a9", "#d1e0ff",
                        "#1570ef", "#eef2f6", "#fef3f2", "#f0f2f5", "rgba(255, 255, 255, 0.96)"):
            self.assertNotIn(literal, components, literal)


class ThemePageTest(AsyncHTTPTestCase):
    def setUp(self):
        self.old = (webfm.AUTH, webfm.DEFAULT_THEME)
        webfm.AUTH = "tester:secret"
        webfm.DEFAULT_THEME = "nord"
        super().setUp()

    def tearDown(self):
        super().tearDown()
        webfm.AUTH, webfm.DEFAULT_THEME = self.old

    def get_app(self):
        return tornado.web.Application([(r"/", webfm.IndexHandler)])

    def test_page_carries_the_themes_and_the_default(self):
        token = base64.b64encode(webfm.AUTH.encode()).decode()
        html = self.fetch("/", headers={"Authorization": "Basic " + token}).body.decode()
        self.assertNotIn("__WEBFM_THEME", html)
        self.assertIn('const WFM_DEFAULT_THEME = "nord";', html)
        self.assertIn('{"id": "darcula", "name": "Darcula", "dark": true}', html)


class FileHandlerRangeTest(AsyncHTTPTestCase):
    DATA = b"0123456789abcdef"

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.old_home = webfm.HOME
        self.old_auth = webfm.AUTH
        self.old_text_preview_cap = webfm.TEXT_PREVIEW_CAP
        webfm.HOME = Path(self.temp_dir.name).resolve()
        webfm.AUTH = "tester:secret"
        webfm.TEXT_PREVIEW_CAP = 8
        (webfm.HOME / "sample.mp4").write_bytes(self.DATA)
        (webfm.HOME / "sample.py").write_text("abcdefghijklmno", encoding="utf-8")
        super().setUp()

    def tearDown(self):
        super().tearDown()
        webfm.HOME = self.old_home
        webfm.AUTH = self.old_auth
        webfm.TEXT_PREVIEW_CAP = self.old_text_preview_cap
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

    def test_full_text_mode_bypasses_preview_truncation(self):
        preview = self.fetch(
            "/api/file?mode=text&path=/sample.py",
            headers=self.auth_headers,
        )
        full = self.fetch(
            "/api/file?mode=text&full=1&path=/sample.py",
            headers=self.auth_headers,
        )

        preview_data = json.loads(preview.body)
        full_data = json.loads(full.body)
        self.assertTrue(preview_data["truncated"])
        self.assertEqual(preview_data["content"], "abcdefgh")
        self.assertFalse(full_data["truncated"])
        self.assertEqual(full_data["content"], "abcdefghijklmno")


class StatHandlerTest(AsyncHTTPTestCase):
    """GET /api/stat: what the page polls to reload a preview when its file changes."""

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.old_home, self.old_auth = webfm.HOME, webfm.AUTH
        webfm.HOME = Path(self.temp_dir.name).resolve()
        webfm.AUTH = "tester:secret"
        (webfm.HOME / "plot.png").write_bytes(b"one")
        (webfm.HOME / "sub").mkdir()
        super().setUp()

    def tearDown(self):
        super().tearDown()
        webfm.HOME, webfm.AUTH = self.old_home, self.old_auth
        self.temp_dir.cleanup()

    def get_app(self):
        return tornado.web.Application([(r"/api/stat", webfm.StatHandler)])

    def stat(self, path, auth=True):
        headers = {"Authorization": "Basic " + base64.b64encode(webfm.AUTH.encode()).decode()} if auth else {}
        return self.fetch("/api/stat?path=" + path, headers=headers)

    def test_a_file_reports_its_revision_uncached(self):
        r = self.stat("/plot.png")
        self.assertEqual(r.code, 200)
        self.assertEqual(r.headers.get("Cache-Control"), "no-store")
        d = json.loads(r.body)
        st = (webfm.HOME / "plot.png").stat()
        self.assertEqual(d, {"mtime": int(st.st_mtime), "size": 3, "rev": f"{st.st_mtime_ns}-3"})

    def test_a_rewrite_within_the_same_second_changes_the_revision(self):
        f = webfm.HOME / "plot.png"
        os.utime(f, ns=(1_700_000_000_100_000_000, 1_700_000_000_100_000_000))
        before = json.loads(self.stat("/plot.png").body)
        f.write_bytes(b"two")
        os.utime(f, ns=(1_700_000_000_900_000_000, 1_700_000_000_900_000_000))
        after = json.loads(self.stat("/plot.png").body)
        self.assertEqual(before["mtime"], after["mtime"])
        self.assertNotEqual(before["rev"], after["rev"])

    def test_missing_files_folders_and_escapes(self):
        self.assertEqual(self.stat("/gone.png").code, 404)
        self.assertEqual(self.stat("/sub").code, 404)
        self.assertEqual(self.stat("/../../../../../../etc/passwd").code, 403)   # /tmp itself is an extra root

    def test_login_required(self):
        self.assertEqual(self.stat("/plot.png", auth=False).code, 401)


class ExtraRootTest(AsyncHTTPTestCase):
    """A directory outside HOME (WEBFM_EXTRA_ROOTS) is browsable read-only under its
    own namespace segment, and never confused with a HOME directory of the same name."""

    PNG = b"\x89PNG\r\n\x1a\n" + b"0" * 32

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        base = Path(self.temp_dir.name).resolve()
        self.old = (webfm.HOME, webfm.AUTH, webfm.EXTRA_ROOTS)
        webfm.HOME = base / "home"
        self.extra = base / "scratch"
        self.outside = base / "elsewhere"
        for d in (webfm.HOME / "tmp", self.extra / "sub", self.outside):
            d.mkdir(parents=True)
        webfm.AUTH = "tester:secret"
        webfm.EXTRA_ROOTS = [str(self.extra)]
        self.seg = ":" + str(self.extra).strip("/").replace("/", ":")
        (self.extra / "plot.png").write_bytes(self.PNG)
        (webfm.HOME / "tmp" / "mine.txt").write_text("home tmp", encoding="utf-8")
        (self.outside / "secret.txt").write_text("no", encoding="utf-8")
        (self.extra / "to-outside").symlink_to(self.outside / "secret.txt")
        (self.extra / "to-home").symlink_to(webfm.HOME / "tmp")
        super().setUp()

    def tearDown(self):
        super().tearDown()
        webfm.HOME, webfm.AUTH, webfm.EXTRA_ROOTS = self.old
        self.temp_dir.cleanup()

    def get_app(self):
        return tornado.web.Application([
            (r"/", webfm.IndexHandler),
            (r"/api/list", webfm.ListHandler),
            (r"/api/file", webfm.FileHandler),
            (r"/api/upload", webfm.UploadHandler),
        ])

    @property
    def auth(self):
        return {"Authorization": "Basic " + base64.b64encode(webfm.AUTH.encode()).decode()}

    def get_json(self, url):
        r = self.fetch(url, headers=self.auth)
        return r.code, json.loads(r.body or b"{}")

    def test_listing_the_extra_root(self):
        code, data = self.get_json("/api/list?path=/" + self.seg)
        self.assertEqual(code, 200)
        self.assertEqual(data["path"], "/" + self.seg)
        self.assertEqual(data["parent"], "/")
        names = [e["name"] for e in data["entries"]]
        self.assertIn("plot.png", names)
        self.assertIn("sub", names)
        self.assertIn("to-home", names)          # resolves into HOME: allowed
        self.assertNotIn("to-outside", names)    # resolves elsewhere: hidden

    def test_file_in_the_extra_root_is_served(self):
        r = self.fetch("/api/file?mode=raw&path=/%s/plot.png" % self.seg, headers=self.auth)
        self.assertEqual(r.code, 200)
        self.assertEqual(r.body, self.PNG)

    def test_escapes_are_refused(self):
        for path in ("/%s/../elsewhere/secret.txt" % self.seg, "/%s/to-outside" % self.seg, "/../elsewhere/secret.txt"):
            r = self.fetch("/api/file?mode=raw&path=" + path, headers=self.auth)
            self.assertEqual(r.code, 403, path)

    def test_home_tmp_keeps_its_meaning(self):
        code, data = self.get_json("/api/list?path=/tmp")
        self.assertEqual(code, 200)
        self.assertEqual(data["path"], "/tmp")
        self.assertEqual([e["name"] for e in data["entries"]], ["mine.txt"])

    def test_symlink_into_home_maps_back_to_home_paths(self):
        code, data = self.get_json("/api/list?path=/%s/to-home" % self.seg)
        self.assertEqual(code, 200)
        self.assertEqual(data["path"], "/tmp")

    def test_extra_root_is_read_only(self):
        body = (b"--B\r\nContent-Disposition: form-data; name=\"file\"; filename=\"x.txt\"\r\n\r\nhi\r\n--B--\r\n")
        headers = dict(self.auth, **{"Content-Type": "multipart/form-data; boundary=B"})
        r = self.fetch("/api/upload?path=/" + self.seg, method="POST", body=body, headers=headers)
        self.assertEqual(r.code, 403)
        self.assertFalse((self.extra / "x.txt").exists())
        r = self.fetch("/api/upload?path=/tmp", method="POST", body=body, headers=headers)
        self.assertEqual(r.code, 200)
        self.assertTrue((webfm.HOME / "tmp" / "x.txt").exists())

    def test_roots_inside_home_and_the_filesystem_root_are_ignored(self):
        webfm.EXTRA_ROOTS = [str(webfm.HOME / "tmp"), "/", str(self.extra)]
        self.assertEqual(list(webfm._mounts()), [self.seg])

    def test_page_carries_the_roots(self):
        r = self.fetch("/", headers=self.auth)
        html = r.body.decode()
        self.assertNotIn("__WEBFM_ROOTS_JSON__", html)
        self.assertIn(json.dumps([{"seg": self.seg, "path": str(self.extra)}]), html)


if __name__ == "__main__":
    unittest.main()
