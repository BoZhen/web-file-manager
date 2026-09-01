#!/usr/bin/env python3
"""Web file manager — browse $HOME, preview images/video/PDF/text, upload."""

import base64
import json
import mimetypes
import os
import re
import secrets
import sys
from pathlib import Path
from urllib.parse import quote

import tornado.ioloop
import tornado.web

HOME = Path.home().resolve()
PROJECT_DIR = Path(__file__).resolve().parent
PDFJS_DIR = PROJECT_DIR / "pdfjs"
MATHJAX_DIR = PROJECT_DIR / "mathjax"
PORT = int(os.environ.get("WEBFM_PORT", "7701"))
AUTH = os.environ.get("WEBFM_AUTH", "")  # user:pass — REQUIRED
CONFIG_DIR = Path(os.environ.get("WEBFM_CONFIG_DIR", str(HOME / ".config" / "web-file-manager"))).expanduser()
FAVORITES_FILE = Path(os.environ.get("WEBFM_FAVORITES_FILE", str(CONFIG_DIR / "favorites.json"))).expanduser()
MAX_UPLOAD_BYTES = 10 * 1024 * 1024 * 1024  # 10 GB
TEXT_PREVIEW_CAP = 512 * 1024  # 512 KB
MATHJAX_DEFAULT_URL = "/mathjax/es5/tex-mml-chtml.js"

IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp", ".svg", ".avif", ".heic"}
VIDEO_EXTS = {".mp4", ".m4v", ".webm", ".ogv", ".mov", ".mkv"}
PDF_EXTS = {".pdf"}
MARKDOWN_EXTS = {".md", ".markdown"}
HTML_EXTS = {".html", ".htm"}
TEXT_EXTS = {
    ".txt", ".md", ".markdown", ".rst", ".log",
    ".py", ".pyi", ".ipynb", ".js", ".mjs", ".ts", ".tsx", ".jsx",
    ".css", ".scss", ".sass", ".less",
    ".json", ".yaml", ".yml", ".toml", ".ini", ".cfg", ".conf",
    ".xml", ".csv", ".tsv",
    ".sh", ".bash", ".zsh", ".fish",
    ".c", ".h", ".cpp", ".cc", ".hpp", ".rs", ".go", ".java", ".kt",
    ".rb", ".php", ".pl", ".lua", ".sql",
    ".tex", ".bib", ".m", ".jl", ".r", ".swift",
    ".gitignore", ".env", ".dockerfile", "dockerfile", "makefile",
}

LANG_MAP = {
    ".py": "python", ".pyi": "python",
    ".js": "javascript", ".mjs": "javascript", ".jsx": "javascript",
    ".ts": "typescript", ".tsx": "typescript",
    ".html": "html", ".htm": "html",
    ".css": "css", ".scss": "scss", ".less": "less",
    ".json": "json", ".yaml": "yaml", ".yml": "yaml",
    ".toml": "toml", ".ini": "ini",
    ".xml": "xml", ".md": "markdown", ".markdown": "markdown",
    ".sh": "bash", ".bash": "bash", ".zsh": "bash", ".fish": "bash",
    ".c": "c", ".h": "c", ".cpp": "cpp", ".cc": "cpp", ".hpp": "cpp",
    ".rs": "rust", ".go": "go", ".java": "java", ".kt": "kotlin",
    ".rb": "ruby", ".php": "php", ".pl": "perl", ".lua": "lua",
    ".sql": "sql", ".tex": "latex", ".jl": "julia", ".r": "r",
    ".swift": "swift",
}


def _classify(p: Path) -> str:
    if p.is_dir():
        return "dir"
    ext = p.suffix.lower()
    name = p.name.lower()
    if ext in IMAGE_EXTS:
        return "image"
    if ext in VIDEO_EXTS:
        return "video"
    if ext in PDF_EXTS:
        return "pdf"
    if ext in MARKDOWN_EXTS:
        return "markdown"
    if ext in HTML_EXTS:
        return "html"
    if ext in TEXT_EXTS or name in TEXT_EXTS:
        return "text"
    return "other"


def _safe_resolve(rel: str) -> Path:
    """Resolve a request path relative to HOME. Raises ValueError on escape."""
    rel = (rel or "").lstrip("/")
    target = (HOME / rel).resolve(strict=False)
    target.relative_to(HOME)  # raises ValueError if outside HOME
    return target


def _to_rel(p: Path) -> str:
    r = p.relative_to(HOME)
    s = str(r)
    return "/" if s == "." else "/" + s


def _load_favorites():
    try:
        data = json.loads(FAVORITES_FILE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []

    raw = data.get("favorites", []) if isinstance(data, dict) else data
    if not isinstance(raw, list):
        return []

    favorites = []
    seen = set()
    for item in raw:
        if not isinstance(item, str):
            continue
        try:
            rel = _to_rel(_safe_resolve(item))
        except ValueError:
            continue
        if rel not in seen:
            favorites.append(rel)
            seen.add(rel)
    return favorites


def _save_favorites(favorites):
    FAVORITES_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = FAVORITES_FILE.with_suffix(FAVORITES_FILE.suffix + ".tmp")
    payload = json.dumps({"favorites": favorites}, ensure_ascii=False, indent=2)
    tmp.write_text(payload + "\n", encoding="utf-8")
    tmp.replace(FAVORITES_FILE)


def _favorite_entries():
    entries = []
    for rel in _load_favorites():
        try:
            target = _safe_resolve(rel)
        except ValueError:
            continue
        if not target.exists() or not target.is_dir():
            continue
        try:
            st = target.stat()
        except OSError:
            continue
        entries.append({
            "path": _to_rel(target),
            "name": "~" if target == HOME else target.name,
            "mtime": int(st.st_mtime),
        })
    return entries


def _looks_binary(data: bytes) -> bool:
    if not data:
        return False
    if b"\x00" in data:
        return True
    # Try UTF-8; tolerate failures within the last 3 bytes (truncation may have
    # cut a multi-byte char mid-sequence).
    try:
        data.decode("utf-8")
        return False
    except UnicodeDecodeError as e:
        if e.start >= len(data) - 3:
            return False
    # Fall back to GBK for Chinese content (Windows users, legacy files).
    try:
        data.decode("gbk")
        return False
    except UnicodeDecodeError:
        pass
    return True


def _parse_byte_range(header: str, size: int):
    """Parse a single RFC 7233 byte range and return its inclusive bounds."""
    match = re.fullmatch(r"bytes=(\d*)-(\d*)", header.strip())
    if not match or size <= 0:
        raise ValueError("invalid or unsatisfiable range")

    start_text, end_text = match.groups()
    if not start_text and not end_text:
        raise ValueError("empty range")

    if not start_text:
        suffix_length = int(end_text)
        if suffix_length <= 0:
            raise ValueError("invalid suffix range")
        start = max(size - suffix_length, 0)
        return start, size - 1

    start = int(start_text)
    if start >= size:
        raise ValueError("range starts beyond file")

    end = size - 1 if not end_text else min(int(end_text), size - 1)
    if end < start:
        raise ValueError("range end precedes start")
    return start, end


class BasicAuthMixin:
    """HTTP Basic Auth check — mandatory for this app."""

    def _check_auth(self):
        header = self.request.headers.get("Authorization", "")
        if header.startswith("Basic "):
            try:
                decoded = base64.b64decode(header[6:]).decode()
                if secrets.compare_digest(decoded, AUTH):
                    return True
            except Exception:
                pass
        self.set_status(401)
        self.set_header("WWW-Authenticate", 'Basic realm="webfm"')
        self.finish()
        return False


class AuthStaticHandler(BasicAuthMixin, tornado.web.StaticFileHandler):
    async def get(self, path, include_body=True):
        if not self._check_auth():
            return
        await super().get(path, include_body)


class IndexHandler(BasicAuthMixin, tornado.web.RequestHandler):
    def get(self, *_args):
        if not self._check_auth():
            return
        self.set_header("Content-Type", "text/html; charset=utf-8")
        self.set_header("Cache-Control", "no-store")
        self.write(HTML.replace("__WEBFM_HOME_JSON__", json.dumps(str(HOME))))


class ListHandler(BasicAuthMixin, tornado.web.RequestHandler):
    def get(self):
        if not self._check_auth():
            return
        rel = self.get_argument("path", "")
        try:
            target = _safe_resolve(rel)
        except ValueError:
            self.set_status(403)
            self.write({"error": "path escapes root"})
            return
        if not target.exists() or not target.is_dir():
            self.set_status(404)
            self.write({"error": "not a directory"})
            return

        entries = []
        favorites = set(_load_favorites())
        try:
            for child in sorted(target.iterdir(), key=lambda p: (not p.is_dir(), p.name.lower())):
                try:
                    real = child.resolve(strict=False)
                    real.relative_to(HOME)  # skip entries that escape
                except ValueError:
                    continue
                except OSError:
                    continue
                try:
                    st = child.stat()
                except OSError:
                    continue
                kind = _classify(child)
                entry = {
                    "name": child.name,
                    "is_dir": kind == "dir",
                    "size": st.st_size,
                    "mtime": int(st.st_mtime),
                    "kind": kind,
                }
                if kind == "dir":
                    entry["favorite"] = _to_rel(real) in favorites
                entries.append(entry)
        except PermissionError:
            self.set_status(403)
            self.write({"error": "permission denied"})
            return

        rel_from_home = _to_rel(target)
        parent = _to_rel(target.parent) if target != HOME else None

        self.set_header("Content-Type", "application/json")
        self.write({"path": rel_from_home, "parent": parent, "entries": entries})


class FavoritesHandler(BasicAuthMixin, tornado.web.RequestHandler):
    def get(self):
        if not self._check_auth():
            return
        self.set_header("Content-Type", "application/json")
        self.write({"favorites": _favorite_entries()})

    def post(self):
        if not self._check_auth():
            return

        try:
            data = json.loads(self.request.body.decode("utf-8") or "{}")
        except (UnicodeDecodeError, json.JSONDecodeError):
            self.set_status(400)
            self.write({"error": "invalid json"})
            return

        path = data.get("path", "")
        try:
            target = _safe_resolve(path)
        except ValueError:
            self.set_status(403)
            self.write({"error": "path escapes root"})
            return
        if not target.exists() or not target.is_dir():
            self.set_status(404)
            self.write({"error": "not a directory"})
            return

        rel = _to_rel(target)
        favorites = _load_favorites()
        if data.get("favorite", True):
            if rel not in favorites:
                favorites.append(rel)
        else:
            favorites = [item for item in favorites if item != rel]

        _save_favorites(favorites)
        self.set_header("Content-Type", "application/json")
        self.write({"ok": True, "favorites": _favorite_entries()})


class FileHandler(BasicAuthMixin, tornado.web.RequestHandler):
    CHUNK = 64 * 1024

    async def get(self):
        if not self._check_auth():
            return
        rel = self.get_argument("path", "")
        mode = self.get_argument("mode", "raw")
        force = self.get_argument("force", "0") == "1"
        try:
            target = _safe_resolve(rel)
        except ValueError:
            self.set_status(403)
            self.write({"error": "path escapes root"})
            return
        if not target.exists() or not target.is_file():
            self.set_status(404)
            self.write({"error": "not a file"})
            return

        if mode == "text":
            self._serve_text(target, force=force)
            return

        ctype, _ = mimetypes.guess_type(target.name)
        if not ctype:
            ctype = "application/octet-stream"
        self.set_header("Content-Type", ctype)
        size = target.stat().st_size
        self.set_header("Accept-Ranges", "bytes")

        byte_range = None
        range_header = self.request.headers.get("Range")
        if range_header:
            try:
                byte_range = _parse_byte_range(range_header, size)
            except ValueError:
                self.set_status(416)
                self.set_header("Content-Range", f"bytes */{size}")
                self.set_header("Content-Length", "0")
                return

        start = 0
        content_length = size
        if byte_range:
            start, end = byte_range
            content_length = end - start + 1
            self.set_status(206)
            self.set_header("Content-Range", f"bytes {start}-{end}/{size}")
        self.set_header("Content-Length", str(content_length))

        if mode == "download":
            # RFC 5987 filename* for unicode
            name = target.name.replace('"', "")
            ascii_name = name.encode("ascii", "replace").decode("ascii")
            self.set_header(
                "Content-Disposition",
                f'attachment; filename="{ascii_name}"; filename*=UTF-8\'\'{quote(name)}'
            )
        else:
            self.set_header("Cache-Control", "private, max-age=3600")

        with open(target, "rb") as f:
            f.seek(start)
            remaining = content_length
            while remaining:
                chunk = f.read(min(self.CHUNK, remaining))
                if not chunk:
                    break
                self.write(chunk)
                await self.flush()
                remaining -= len(chunk)

    def _serve_text(self, target: Path, force: bool):
        size = target.stat().st_size
        read_n = min(size, TEXT_PREVIEW_CAP)
        with open(target, "rb") as f:
            raw = f.read(read_n)
        truncated = size > read_n

        if not force and _looks_binary(raw[:8192]):
            self.set_header("Content-Type", "application/json")
            self.write({"binary": True, "size": size})
            return

        content = None
        encoding = None
        for enc in ("utf-8", "gbk"):
            try:
                content = raw.decode(enc)
                encoding = enc
                break
            except UnicodeDecodeError:
                continue
        if content is None:
            content = raw.decode("latin-1", errors="replace")
            encoding = "latin-1"

        ext = target.suffix.lower()
        lang = LANG_MAP.get(ext, "")

        self.set_header("Content-Type", "application/json")
        self.write({
            "binary": False,
            "content": content,
            "truncated": truncated,
            "size": size,
            "encoding": encoding,
            "lang": lang,
        })


class PreviewHandler(BasicAuthMixin, tornado.web.RequestHandler):
    """Serve HOME files at path-shaped URLs so HTML notes can load relative assets."""

    CHUNK = 64 * 1024

    async def get(self, rel):
        if not self._check_auth():
            return
        try:
            target = _safe_resolve(rel)
        except ValueError:
            self.set_status(403)
            self.write({"error": "path escapes root"})
            return
        if not target.exists() or not target.is_file():
            self.set_status(404)
            self.write({"error": "not a file"})
            return

        ctype, _ = mimetypes.guess_type(target.name)
        if not ctype:
            ctype = "application/octet-stream"
        self.set_header("Cache-Control", "private, max-age=3600")
        self.set_header("X-Content-Type-Options", "nosniff")

        if target.suffix.lower() in HTML_EXTS:
            self._serve_html_note(target)
            return

        self.set_header("Content-Type", ctype)
        self.set_header("Content-Length", str(target.stat().st_size))
        with open(target, "rb") as f:
            while True:
                chunk = f.read(self.CHUNK)
                if not chunk:
                    break
                self.write(chunk)
                await self.flush()

    def _serve_html_note(self, target: Path):
        nonce = secrets.token_urlsafe(16)
        raw = target.read_bytes()
        body = self._prepare_html_note(raw, nonce)

        self.set_header("Content-Type", "text/html; charset=utf-8")
        self.set_header("Content-Length", str(len(body)))
        self.set_header("Cache-Control", "no-store")
        self.write(body)

    @staticmethod
    def _prepare_html_note(raw: bytes, nonce: str) -> bytes:
        html = None
        for enc in ("utf-8", "gbk"):
            try:
                html = raw.decode(enc)
                break
            except UnicodeDecodeError:
                continue
        if html is None:
            html = raw.decode("latin-1", errors="replace")

        html = PreviewHandler._rewrite_mathjax_to_local(html)

        # Many exported notes already include their own MathJax config/script.
        # Do not inject a second loader because that can overwrite package
        # settings such as `cancel` or race with the author's MathJax script.
        # Their CDN URL has already been rewritten to local /mathjax above.
        if "mathjax" in html.lower():
            return html.encode("utf-8")

        injection = f"""
<script nonce="{nonce}">
window.MathJax = {{
  tex: {{
    inlineMath: [['$', '$'], ['\\\\(', '\\\\)']],
    displayMath: [['$$', '$$'], ['\\\\[', '\\\\]']],
    processEscapes: true,
    processEnvironments: true
  }},
  options: {{
    skipHtmlTags: ['script', 'noscript', 'style', 'textarea', 'pre', 'code']
  }}
}};
</script>
<script nonce="{nonce}" id="MathJax-script" async src="{MATHJAX_DEFAULT_URL}"></script>
"""
        lower = html.lower()
        idx = lower.rfind("</body>")
        if idx >= 0:
            html = html[:idx] + injection + html[idx:]
        else:
            html = html + injection
        return html.encode("utf-8")

    @staticmethod
    def _rewrite_mathjax_to_local(html: str) -> str:
        """Make HTML notes independent of external MathJax/polyfill CDNs."""

        # polyfill.io is unnecessary for current browsers and can block mobile
        # rendering when the phone cannot reach it.
        html = re.sub(
            r'<script\b[^>]*\bsrc=["\']https?://polyfill\.io/[^"\']*["\'][^>]*>\s*</script>',
            "",
            html,
            flags=re.IGNORECASE,
        )

        def repl(match):
            filename = match.group("file")
            return f"/mathjax/es5/{filename}"

        patterns = [
            r"https://cdn\.jsdelivr\.net/npm/mathjax@[^/]+/es5/(?P<file>[^\"'<> ]+)",
            r"https://cdn\.jsdelivr\.net/npm/mathjax/es5/(?P<file>[^\"'<> ]+)",
            r"https://cdnjs\.cloudflare\.com/ajax/libs/mathjax/[^/]+/es5/(?P<file>[^\"'<> ]+)",
            r"https://unpkg\.com/mathjax@[^/]+/es5/(?P<file>[^\"'<> ]+)",
        ]
        for pattern in patterns:
            html = re.sub(pattern, repl, html, flags=re.IGNORECASE)
        return html


class UploadHandler(BasicAuthMixin, tornado.web.RequestHandler):
    def post(self):
        if not self._check_auth():
            return
        rel = self.get_argument("path", "")
        try:
            target_dir = _safe_resolve(rel)
        except ValueError:
            self.set_status(403)
            self.write({"error": "path escapes root"})
            return
        if not target_dir.exists() or not target_dir.is_dir():
            self.set_status(404)
            self.write({"error": "not a directory"})
            return

        files = self.request.files.get("file", [])
        if not files:
            self.set_status(400)
            self.write({"error": "no file"})
            return

        saved = []
        for finfo in files:
            # Sanitize filename: strip path components
            name = os.path.basename(finfo["filename"]) or "unnamed"
            if name in (".", ".."):
                continue
            dest = target_dir / name
            try:
                dest_resolved = dest.resolve(strict=False)
                dest_resolved.relative_to(HOME)
            except ValueError:
                continue
            with open(dest, "wb") as f:
                f.write(finfo["body"])
            saved.append(name)

        self.set_header("Content-Type", "application/json")
        self.write({"ok": True, "saved": saved})


HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<meta name="theme-color" id="theme-color-meta" content="#f5f7fa">
<title>File Manager</title>
<link rel="stylesheet"
      href="https://cdn.jsdelivr.net/gh/highlightjs/cdn-release@11.9.0/build/styles/github.min.css">
<script src="https://cdn.jsdelivr.net/gh/highlightjs/cdn-release@11.9.0/build/highlight.min.js"></script>
<script src="https://cdn.jsdelivr.net/npm/marked@11.1.1/marked.min.js"></script>
<script>
document.documentElement.dataset.theme = "cloud-light";
</script>
<style>
  :root, :root[data-theme="dracula"] {
    color-scheme: dark;
    --theme-color: #282a36;
    --bg: #282a36;
    --panel: #21222c;
    --panel-2: #343746;
    --border: #44475a;
    --text: #f8f8f2;
    --muted: #6272a4;
    --accent: #bd93f9;
    --accent-2: #8be9fd;
    --danger: #ff5555;
    --hover: #424450;
    --selected: #44475a;
    --subtle: rgba(68, 71, 90, 0.32);
    --drop-bg: rgba(139, 233, 253, 0.10);
    --sidebar-glass: rgba(40, 42, 54, 0.72);
    --star: #f1fa8c;
    --star-bg: rgba(241, 250, 140, 0.12);
    --star-border: rgba(241, 250, 140, 0.30);
    --success: #50fa7b;
    --preview-bg: #191a21;
    --pdf-bg: #21222c;
    --code-bg: #282a36;
    --code-text: #f8f8f2;
    --doc-bg: #282a36;
    --doc-text: #f8f8f2;
    --doc-border: #44475a;
    --doc-muted: #6272a4;
    --doc-link: #8be9fd;
    --doc-code-bg: #21222c;
    --doc-row: #2f3140;
    --floating-bg: rgba(0, 0, 0, 0.5);
    --floating-border: rgba(255, 255, 255, 0.2);
    --floating-text: white;
    --nav-bg: rgba(0, 0, 0, 0.4);
    --nav-hover-bg: rgba(0, 0, 0, 0.7);
    --preview-info-bg: linear-gradient(to bottom, rgba(0,0,0,0.7), transparent);
    --preview-info-button-bg: rgba(255,255,255,0.15);
    --preview-info-button-border: rgba(255,255,255,0.2);
    --warning: #b45309;
  }
  :root[data-theme="nord"] {
    color-scheme: dark;
    --theme-color: #2e3440;
    --bg: #2e3440;
    --panel: #3b4252;
    --panel-2: #434c5e;
    --border: #4c566a;
    --text: #e5e9f0;
    --muted: #d8dee9;
    --accent: #88c0d0;
    --accent-2: #5e81ac;
    --danger: #bf616a;
    --hover: #4c566a;
    --selected: #3b5368;
    --subtle: rgba(76, 86, 106, 0.28);
    --drop-bg: rgba(136, 192, 208, 0.10);
    --sidebar-glass: rgba(46, 52, 64, 0.72);
    --star: #ebcb8b;
    --star-bg: rgba(235, 203, 139, 0.12);
    --star-border: rgba(235, 203, 139, 0.30);
    --success: #a3be8c;
    --preview-bg: #242933;
    --pdf-bg: #3b4252;
    --code-bg: #2e3440;
    --code-text: #e5e9f0;
    --doc-bg: #2e3440;
    --doc-text: #e5e9f0;
    --doc-border: #4c566a;
    --doc-muted: #d8dee9;
    --doc-link: #88c0d0;
    --doc-code-bg: #3b4252;
    --doc-row: #343c4a;
  }
  :root[data-theme="darcula"] {
    color-scheme: dark;
    --theme-color: #2b2b2b;
    --bg: #2b2b2b;
    --panel: #313335;
    --panel-2: #3c3f41;
    --border: #555555;
    --text: #a9b7c6;
    --muted: #808080;
    --accent: #6897bb;
    --accent-2: #cc7832;
    --danger: #bc3f3c;
    --hover: #4b4d4f;
    --selected: #214283;
    --subtle: rgba(60, 63, 65, 0.36);
    --drop-bg: rgba(104, 151, 187, 0.12);
    --sidebar-glass: rgba(43, 43, 43, 0.74);
    --star: #ffc66d;
    --star-bg: rgba(255, 198, 109, 0.12);
    --star-border: rgba(255, 198, 109, 0.30);
    --success: #6a8759;
    --preview-bg: #252525;
    --pdf-bg: #313335;
    --code-bg: #2b2b2b;
    --code-text: #a9b7c6;
    --doc-bg: #2b2b2b;
    --doc-text: #a9b7c6;
    --doc-border: #555555;
    --doc-muted: #808080;
    --doc-link: #6897bb;
    --doc-code-bg: #313335;
    --doc-row: #303030;
  }
  :root[data-theme="one-dark"] {
    color-scheme: dark;
    --theme-color: #282c34;
    --bg: #282c34;
    --panel: #21252b;
    --panel-2: #2c313a;
    --border: #3e4451;
    --text: #abb2bf;
    --muted: #5c6370;
    --accent: #61afef;
    --accent-2: #c678dd;
    --danger: #e06c75;
    --hover: #353b45;
    --selected: #3a3f4b;
    --subtle: rgba(62, 68, 81, 0.30);
    --drop-bg: rgba(97, 175, 239, 0.10);
    --sidebar-glass: rgba(40, 44, 52, 0.72);
    --star: #e5c07b;
    --star-bg: rgba(229, 192, 123, 0.12);
    --star-border: rgba(229, 192, 123, 0.30);
    --success: #98c379;
    --preview-bg: #1f2329;
    --pdf-bg: #21252b;
    --code-bg: #282c34;
    --code-text: #abb2bf;
    --doc-bg: #282c34;
    --doc-text: #abb2bf;
    --doc-border: #3e4451;
    --doc-muted: #5c6370;
    --doc-link: #61afef;
    --doc-code-bg: #21252b;
    --doc-row: #2d323c;
  }
  :root[data-theme="gruvbox-dark"] {
    color-scheme: dark;
    --theme-color: #282828;
    --bg: #282828;
    --panel: #3c3836;
    --panel-2: #504945;
    --border: #665c54;
    --text: #ebdbb2;
    --muted: #928374;
    --accent: #83a598;
    --accent-2: #458588;
    --danger: #fb4934;
    --hover: #665c54;
    --selected: #504945;
    --subtle: rgba(80, 73, 69, 0.32);
    --drop-bg: rgba(131, 165, 152, 0.12);
    --sidebar-glass: rgba(40, 40, 40, 0.74);
    --star: #fabd2f;
    --star-bg: rgba(250, 189, 47, 0.12);
    --star-border: rgba(250, 189, 47, 0.30);
    --success: #b8bb26;
    --preview-bg: #1d2021;
    --pdf-bg: #3c3836;
    --code-bg: #282828;
    --code-text: #ebdbb2;
    --doc-bg: #282828;
    --doc-text: #ebdbb2;
    --doc-border: #665c54;
    --doc-muted: #928374;
    --doc-link: #83a598;
    --doc-code-bg: #3c3836;
    --doc-row: #32302f;
  }
  :root[data-theme="catppuccin-mocha"] {
    color-scheme: dark;
    --theme-color: #1e1e2e;
    --bg: #1e1e2e;
    --panel: #181825;
    --panel-2: #313244;
    --border: #45475a;
    --text: #cdd6f4;
    --muted: #a6adc8;
    --accent: #89b4fa;
    --accent-2: #cba6f7;
    --danger: #f38ba8;
    --hover: #45475a;
    --selected: #45475a;
    --subtle: rgba(69, 71, 90, 0.30);
    --drop-bg: rgba(137, 180, 250, 0.10);
    --sidebar-glass: rgba(30, 30, 46, 0.72);
    --star: #f9e2af;
    --star-bg: rgba(249, 226, 175, 0.12);
    --star-border: rgba(249, 226, 175, 0.30);
    --success: #a6e3a1;
    --preview-bg: #11111b;
    --pdf-bg: #181825;
    --code-bg: #1e1e2e;
    --code-text: #cdd6f4;
    --doc-bg: #1e1e2e;
    --doc-text: #cdd6f4;
    --doc-border: #45475a;
    --doc-muted: #a6adc8;
    --doc-link: #89b4fa;
    --doc-code-bg: #181825;
    --doc-row: #252538;
  }
  :root[data-theme$="-light"], :root[data-theme="catppuccin-latte"] {
    color-scheme: light;
    --floating-bg: rgba(17, 24, 39, 0.72);
    --floating-border: rgba(255, 255, 255, 0.32);
    --floating-text: white;
    --nav-bg: rgba(17, 24, 39, 0.38);
    --nav-hover-bg: rgba(17, 24, 39, 0.64);
    --preview-info-bg: linear-gradient(to bottom, rgba(17,24,39,0.62), transparent);
    --preview-info-button-bg: rgba(255,255,255,0.22);
    --preview-info-button-border: rgba(255,255,255,0.34);
    --warning: #b45309;
  }
  :root[data-theme="one-light"] {
    --theme-color: #fafafa;
    --bg: #fafafa;
    --panel: #ffffff;
    --panel-2: #f0f0f0;
    --border: #d0d0d0;
    --text: #383a42;
    --muted: #696c77;
    --accent: #4078f2;
    --accent-2: #0184bc;
    --danger: #e45649;
    --hover: #e5e5e6;
    --selected: #e5e5e6;
    --subtle: rgba(160, 161, 167, 0.18);
    --drop-bg: rgba(64, 120, 242, 0.08);
    --sidebar-glass: rgba(250, 250, 250, 0.80);
    --star: #986801;
    --star-bg: rgba(152, 104, 1, 0.10);
    --star-border: rgba(152, 104, 1, 0.24);
    --success: #50a14f;
    --preview-bg: #f5f5f5;
    --pdf-bg: #f0f0f0;
    --code-bg: #fafafa;
    --code-text: #383a42;
    --doc-bg: #ffffff;
    --doc-text: #383a42;
    --doc-border: #d0d0d0;
    --doc-muted: #696c77;
    --doc-link: #4078f2;
    --doc-code-bg: #f0f0f0;
    --doc-row: #f7f7f7;
  }
  :root[data-theme="github-light"] {
    --theme-color: #ffffff;
    --bg: #ffffff;
    --panel: #ffffff;
    --panel-2: #f6f8fa;
    --border: #d0d7de;
    --text: #24292f;
    --muted: #57606a;
    --accent: #0969da;
    --accent-2: #8250df;
    --danger: #cf222e;
    --hover: #eaeef2;
    --selected: #ddf4ff;
    --subtle: rgba(208, 215, 222, 0.28);
    --drop-bg: rgba(9, 105, 218, 0.08);
    --sidebar-glass: rgba(255, 255, 255, 0.80);
    --star: #9a6700;
    --star-bg: rgba(154, 103, 0, 0.10);
    --star-border: rgba(154, 103, 0, 0.24);
    --success: #1a7f37;
    --preview-bg: #f6f8fa;
    --pdf-bg: #eaeef2;
    --doc-bg: #ffffff;
    --doc-text: #24292f;
    --doc-border: #d0d7de;
    --doc-muted: #57606a;
    --doc-link: #0969da;
    --doc-code-bg: #f6f8fa;
    --doc-row: #f6f8fa;
  }
  :root[data-theme="solarized-light"] {
    --theme-color: #fdf6e3;
    --bg: #fdf6e3;
    --panel: #eee8d5;
    --panel-2: #e8dfc4;
    --border: #d6cbb0;
    --text: #657b83;
    --muted: #839496;
    --accent: #268bd2;
    --accent-2: #2aa198;
    --danger: #dc322f;
    --hover: #e2d8bd;
    --selected: #eee8d5;
    --subtle: rgba(147, 161, 161, 0.20);
    --drop-bg: rgba(38, 139, 210, 0.08);
    --sidebar-glass: rgba(253, 246, 227, 0.80);
    --star: #b58900;
    --star-bg: rgba(181, 137, 0, 0.10);
    --star-border: rgba(181, 137, 0, 0.24);
    --success: #859900;
    --preview-bg: #eee8d5;
    --pdf-bg: #e8dfc4;
    --code-bg: #fdf6e3;
    --code-text: #657b83;
    --doc-bg: #fdf6e3;
    --doc-text: #657b83;
    --doc-border: #d6cbb0;
    --doc-muted: #839496;
    --doc-link: #268bd2;
    --doc-code-bg: #eee8d5;
    --doc-row: #f7efd9;
  }
  :root[data-theme="gruvbox-light"] {
    --theme-color: #fbf1c7;
    --bg: #fbf1c7;
    --panel: #f9f5d7;
    --panel-2: #ebdbb2;
    --border: #d5c4a1;
    --text: #3c3836;
    --muted: #7c6f64;
    --accent: #458588;
    --accent-2: #b16286;
    --danger: #cc241d;
    --hover: #d5c4a1;
    --selected: #ebdbb2;
    --subtle: rgba(213, 196, 161, 0.28);
    --drop-bg: rgba(69, 133, 136, 0.09);
    --sidebar-glass: rgba(251, 241, 199, 0.80);
    --star: #d79921;
    --star-bg: rgba(215, 153, 33, 0.10);
    --star-border: rgba(215, 153, 33, 0.25);
    --success: #98971a;
    --preview-bg: #f2e5bc;
    --pdf-bg: #ebdbb2;
    --code-bg: #fbf1c7;
    --code-text: #3c3836;
    --doc-bg: #fbf1c7;
    --doc-text: #3c3836;
    --doc-border: #d5c4a1;
    --doc-muted: #7c6f64;
    --doc-link: #458588;
    --doc-code-bg: #ebdbb2;
    --doc-row: #f9edc0;
  }
  :root[data-theme="catppuccin-latte"] {
    --theme-color: #eff1f5;
    --bg: #eff1f5;
    --panel: #e6e9ef;
    --panel-2: #dce0e8;
    --border: #bcc0cc;
    --text: #4c4f69;
    --muted: #6c6f85;
    --accent: #1e66f5;
    --accent-2: #8839ef;
    --danger: #d20f39;
    --hover: #ccd0da;
    --selected: #ccd0da;
    --subtle: rgba(188, 192, 204, 0.28);
    --drop-bg: rgba(30, 102, 245, 0.08);
    --sidebar-glass: rgba(239, 241, 245, 0.80);
    --star: #df8e1d;
    --star-bg: rgba(223, 142, 29, 0.10);
    --star-border: rgba(223, 142, 29, 0.25);
    --success: #40a02b;
    --preview-bg: #e6e9ef;
    --pdf-bg: #dce0e8;
    --code-bg: #eff1f5;
    --code-text: #4c4f69;
    --doc-bg: #eff1f5;
    --doc-text: #4c4f69;
    --doc-border: #bcc0cc;
    --doc-muted: #6c6f85;
    --doc-link: #1e66f5;
    --doc-code-bg: #e6e9ef;
    --doc-row: #f4f6fb;
  }
  :root[data-theme="nord-light"] {
    --theme-color: #eceff4;
    --bg: #eceff4;
    --panel: #e5e9f0;
    --panel-2: #d8dee9;
    --border: #c8d0dd;
    --text: #2e3440;
    --muted: #4c566a;
    --accent: #5e81ac;
    --accent-2: #88c0d0;
    --danger: #bf616a;
    --hover: #cfd7e3;
    --selected: #d8dee9;
    --subtle: rgba(143, 188, 187, 0.16);
    --drop-bg: rgba(94, 129, 172, 0.08);
    --sidebar-glass: rgba(236, 239, 244, 0.80);
    --star: #d08770;
    --star-bg: rgba(208, 135, 112, 0.10);
    --star-border: rgba(208, 135, 112, 0.25);
    --success: #a3be8c;
    --preview-bg: #e5e9f0;
    --pdf-bg: #d8dee9;
    --code-bg: #eceff4;
    --code-text: #2e3440;
    --doc-bg: #eceff4;
    --doc-text: #2e3440;
    --doc-border: #c8d0dd;
    --doc-muted: #4c566a;
    --doc-link: #5e81ac;
    --doc-code-bg: #e5e9f0;
    --doc-row: #f2f4f8;
  }
  :root[data-theme="cloud-light"] {
    color-scheme: light;
    --theme-color: #f5f7fa;
    --bg: #f5f7fa;
    --panel: #ffffff;
    --panel-2: #f8fafc;
    --border: #e4e7ec;
    --text: #101828;
    --muted: #667085;
    --accent: #2563eb;
    --accent-2: #1d4ed8;
    --danger: #d92d20;
    --hover: #f2f4f7;
    --selected: #eaf2ff;
    --subtle: #f8fafc;
    --drop-bg: rgba(37, 99, 235, 0.08);
    --sidebar-glass: rgba(255, 255, 255, 0.92);
    --star: #2563eb;
    --star-bg: #eaf2ff;
    --star-border: #b2ccff;
    --success: #039855;
    --preview-bg: #f5f7fa;
    --pdf-bg: #eef1f5;
    --code-bg: #ffffff;
    --code-text: #101828;
    --doc-bg: #ffffff;
    --doc-text: #101828;
    --doc-border: #e4e7ec;
    --doc-muted: #667085;
    --doc-link: #2563eb;
    --doc-code-bg: #f2f4f7;
    --doc-row: #f8fafc;
    --floating-bg: rgba(16, 24, 40, 0.78);
    --floating-border: rgba(255, 255, 255, 0.36);
    --floating-text: #ffffff;
    --nav-bg: rgba(16, 24, 40, 0.42);
    --nav-hover-bg: rgba(16, 24, 40, 0.70);
    --preview-info-bg: transparent;
    --preview-info-button-bg: #ffffff;
    --preview-info-button-border: #d0d5dd;
    --warning: #dc6803;
  }
  * { box-sizing: border-box; }
  html, body {
    margin: 0; padding: 0; height: 100%; width: 100%;
    background: var(--bg); color: var(--text);
    font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", "PingFang SC",
                 "Hiragino Sans GB", "Microsoft YaHei", sans-serif;
    font-size: 14px; overflow: hidden;
  }
  a { color: var(--accent); text-decoration: none; }
  a:hover { text-decoration: underline; }
  button {
    background: var(--panel-2); color: var(--text);
    border: 1px solid var(--border); border-radius: 6px;
    padding: 6px 10px; cursor: pointer; font-size: 13px;
  }
  button:hover { background: var(--hover); }
  button:active { transform: translateY(1px); }
  input[type=file] { display: none; }

  .app {
    --sidebar-width: 380px;
    display: grid;
    grid-template-columns: minmax(280px, min(var(--sidebar-width), 72vw)) 1fr;
    height: 100vh; height: 100dvh; width: 100%;
    transition: grid-template-columns 0.18s ease;
  }
  .app.sidebar-resizing {
    cursor: col-resize;
    user-select: none;
    transition: none;
  }
  .app.sidebar-resizing * { cursor: col-resize !important; }
  .app.sidebar-collapsed { grid-template-columns: 0 1fr; }
  .app.sidebar-collapsed #pane-left {
    visibility: hidden; pointer-events: none;
  }
  .pane {
    display: flex; flex-direction: column;
    overflow: hidden; min-width: 0;
    background: var(--panel);
    position: relative;
  }
  .pane + .pane { border-left: 1px solid var(--border); }
  .app.sidebar-collapsed .pane + .pane { border-left: none; }

  .sidebar-toggle {
    background: var(--sidebar-glass);
    color: var(--text);
    border: 1px solid var(--border);
    border-radius: 4px;
    padding: 2px 9px;
    font-size: 13px; line-height: 1.4;
    cursor: pointer;
    z-index: 4;
    opacity: 0.45;
    transition: opacity 0.15s;
  }
  .sidebar-toggle:hover { opacity: 1; }
  .sidebar-reopen {
    position: absolute;
    top: 50%; left: 8px;
    transform: translateY(-50%);
    background: var(--sidebar-glass);
    color: var(--text);
    border: 1px solid var(--border);
    border-radius: 4px;
    padding: 6px 9px;
    font-size: 13px; line-height: 1.4;
    cursor: pointer;
    z-index: 4;
    opacity: 0.55;
    transition: opacity 0.15s;
  }
  .sidebar-reopen:hover { opacity: 1; }
  .sidebar-resizer {
    position: absolute;
    top: 0; right: 0;
    width: 10px; height: 100%;
    cursor: col-resize;
    z-index: 5;
    touch-action: none;
  }
  .sidebar-resizer::after {
    content: "";
    position: absolute;
    top: 0; right: 0;
    width: 2px; height: 100%;
    background: transparent;
    transition: background 0.12s;
  }
  .sidebar-resizer:hover::after,
  .app.sidebar-resizing .sidebar-resizer::after {
    background: var(--accent);
  }

  .pane-header {
    flex-shrink: 0;
    padding: 8px 10px;
    background: var(--panel-2);
    border-bottom: 1px solid var(--border);
    display: flex; flex-direction: column; gap: 6px;
  }
  .crumbs {
    display: flex; flex-wrap: wrap; gap: 2px;
    font-size: 13px; align-items: center;
  }
  .crumbs span.sep { color: var(--muted); padding: 0 2px; }
  .crumbs a { padding: 2px 6px; border-radius: 4px; }
  .crumbs a:hover { background: var(--hover); text-decoration: none; }
  .crumbs .current {
    padding: 2px 6px; color: var(--muted); cursor: text;
    border-radius: 4px;
  }
  .crumbs .current:hover { background: var(--hover); color: var(--text); }
  .crumbs .edit-path {
    background: transparent; border: 1px solid transparent;
    color: var(--muted); padding: 2px 6px; font-size: 12px;
    cursor: pointer; border-radius: 4px; line-height: 1;
  }
  .crumbs .edit-path:hover { border-color: var(--border); color: var(--text); }
  .crumbs .path-input {
    flex: 1; min-width: 200px;
    background: var(--bg); color: var(--text);
    border: 1px solid var(--accent); border-radius: 4px;
    padding: 4px 8px; font-size: 13px;
    font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
  }
  .crumbs .path-input:focus { outline: none; border-color: var(--accent-2); }

  .toolbar {
    display: flex; gap: 6px; flex-wrap: wrap; align-items: center;
  }
  .toolbar .spacer { flex: 1; }
  .toolbar label.btn {
    background: var(--panel); color: var(--text);
    border: 1px solid var(--border); border-radius: 6px;
    padding: 6px 10px; cursor: pointer; font-size: 13px;
  }
  .toolbar label.btn:hover { background: var(--hover); }
  .theme-select {
    flex: 1;
    min-width: 0;
    width: auto;
    background: var(--panel);
    color: var(--text);
    border: 1px solid var(--border);
    border-radius: 6px;
    padding: 6px 8px;
    font-size: 13px;
    cursor: pointer;
  }
  .theme-select:focus {
    outline: none;
    border-color: var(--accent);
  }
  .theme-footer {
    display: flex;
    align-items: center;
    gap: 8px;
    flex-shrink: 0;
    border-top: 1px solid var(--border);
    padding: 8px 10px;
    background: var(--panel-2);
  }
  .theme-footer-label {
    flex-shrink: 0;
    color: var(--muted);
    font-size: 11px;
    font-weight: 700;
    text-transform: uppercase;
    letter-spacing: 0;
  }

  .listing {
    flex: 1; overflow-y: auto; overflow-x: hidden;
    padding: 4px 0;
    position: relative;
  }
  .listing.dragover::before {
    content: "Drop files to upload";
    position: absolute; inset: 8px;
    border: 2px dashed var(--accent);
    border-radius: 8px;
    display: flex; align-items: center; justify-content: center;
    background: var(--drop-bg);
    color: var(--accent); font-weight: 600; font-size: 16px;
    pointer-events: none; z-index: 5;
  }
  .favorites {
    flex: 0 1 auto;
    border-bottom: 1px solid var(--border);
    background: var(--subtle);
    max-height: 28vh;
    overflow-y: auto;
  }
  .favorites:empty { display: none; }
  .favorites.is-collapsed {
    overflow: hidden;
  }
  .favorites.is-collapsed .favorite-row {
    display: none;
  }
  .favorites-title {
    width: 100%;
    display: flex; align-items: center; gap: 6px;
    padding: 7px 12px;
    background: transparent;
    border: 0;
    border-radius: 0;
    color: var(--muted);
    font-size: 11px; font-weight: 700; text-transform: uppercase;
    letter-spacing: 0;
    text-align: left;
  }
  .favorites-title:hover { background: var(--hover); }
  .favorites-title:active { transform: none; }
  .favorites-title .label {
    overflow: hidden; text-overflow: ellipsis; white-space: nowrap;
  }
  .favorites-title .count {
    margin-left: auto;
    color: var(--muted);
    font-size: 11px;
    font-weight: 700;
  }
  .favorites-title .disclosure {
    width: 12px;
    color: var(--muted);
    text-align: center;
  }
  .favorite-row {
    display: grid;
    grid-template-columns: 24px minmax(0, 1fr);
    align-items: center; gap: 8px;
    padding: 5px 12px;
    cursor: pointer;
    border-left: 3px solid transparent;
  }
  .favorite-row:hover { background: var(--panel-2); }
  .favorite-row.current {
    background: var(--selected);
    border-left-color: var(--accent);
  }
  .favorite-row .name {
    overflow: hidden; text-overflow: ellipsis; white-space: nowrap;
    color: var(--text);
  }
  .row {
    display: grid;
    grid-template-columns: 24px minmax(0, 1fr) auto;
    align-items: center; gap: 8px;
    padding: 6px 12px;
    cursor: pointer;
    border-left: 3px solid transparent;
  }
  .row.dir {
    grid-template-columns: 24px 24px minmax(0, 1fr) auto;
  }
  .row:hover { background: var(--panel-2); }
  .row.selected {
    background: var(--selected);
    border-left-color: var(--accent);
  }
  .row .icon {
    font-size: 16px; line-height: 1; text-align: center;
  }
  .row .name {
    overflow: hidden; text-overflow: ellipsis; white-space: nowrap;
  }
  .row .meta {
    font-size: 11px; color: var(--muted); white-space: nowrap;
  }
  .row.dir .name { color: var(--accent); font-weight: 500; }
  .favorite-toggle {
    width: 24px; height: 24px;
    padding: 0;
    display: inline-flex; align-items: center; justify-content: center;
    background: transparent;
    border: 1px solid transparent;
    color: var(--muted);
    border-radius: 4px;
    font-size: 16px; line-height: 1;
  }
  .favorite-toggle:hover {
    background: var(--star-bg);
    border-color: var(--star-border);
    color: var(--star);
  }
  .favorite-toggle.active { color: var(--star); }

  .uploads {
    flex-shrink: 0;
    border-top: 1px solid var(--border);
    padding: 4px 10px;
    max-height: 30vh; overflow-y: auto;
    background: var(--panel-2);
  }
  .uploads:empty { display: none; }
  .upload-item {
    font-size: 12px; padding: 4px 0;
  }
  .upload-item .bar {
    height: 4px; background: var(--border); border-radius: 2px; overflow: hidden;
    margin-top: 3px;
  }
  .upload-item .bar > div {
    height: 100%; background: var(--accent); width: 0%;
    transition: width 0.1s linear;
  }
  .upload-item.done .bar > div { background: var(--success); }
  .upload-item.err .bar > div { background: var(--danger); }

  .preview {
    flex: 1; overflow: auto;
    display: flex; align-items: center; justify-content: center;
    padding: 0;
    background: var(--preview-bg);
    position: relative;
  }
  .preview.empty {
    color: var(--muted); font-size: 14px;
  }
  .preview img {
    max-width: 100%; max-height: 100%;
    object-fit: contain; display: block;
  }
  .preview video {
    width: 100%; height: 100%;
    object-fit: contain; display: block;
    background: #000;
  }
  .preview iframe {
    width: 100%; height: 100%; border: 0; background: white;
  }

  /* PDF: embedded Mozilla PDF.js viewer (full UI inside iframe) */
  .preview.pdf-mode {
    align-items: stretch; justify-content: stretch;
    padding: 0; overflow: hidden; background: var(--pdf-bg);
  }
  .pdf-viewer {
    width: 100%; height: 100%; border: 0; background: var(--pdf-bg);
  }

  /* HTML notes: render as a web page while keeping scripts disabled */
  .preview.html-mode {
    align-items: stretch; justify-content: stretch;
    padding: 0; overflow: hidden; background: white;
  }
  .html-viewer {
    width: 100%; height: 100%; border: 0; background: white;
  }
  .preview pre {
    margin: 0; width: 100%; height: 100%;
    overflow: auto; padding: 12px;
    font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
    font-size: 13px; line-height: 1.5;
    background: var(--code-bg); color: var(--code-text);
    text-align: left;
  }
  .preview pre code { background: transparent !important; padding: 0; display: block; }

  /* Markdown rendered view */
  .preview.md-mode {
    align-items: stretch; justify-content: stretch;
    background: var(--doc-bg);
  }
  .md-render {
    width: 100%; height: 100%;
    overflow-y: auto; overflow-x: hidden;
    padding: 24px clamp(16px, 5vw, 48px) 80px;
    color: var(--doc-text);
    font-size: 15px; line-height: 1.7;
    font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", "PingFang SC",
                 "Hiragino Sans GB", "Microsoft YaHei", sans-serif;
  }
  .md-body { max-width: 820px; margin: 0 auto; }
  .md-body h1, .md-body h2, .md-body h3, .md-body h4, .md-body h5, .md-body h6 {
    margin: 1.6em 0 0.6em; font-weight: 600; line-height: 1.25;
  }
  .md-body h1 { font-size: 1.9em; border-bottom: 1px solid var(--doc-border); padding-bottom: 8px; }
  .md-body h2 { font-size: 1.5em; border-bottom: 1px solid var(--doc-border); padding-bottom: 6px; }
  .md-body h3 { font-size: 1.25em; }
  .md-body h4 { font-size: 1.05em; }
  .md-body p { margin: 0.8em 0; }
  .md-body a { color: var(--doc-link); }
  .md-body img { max-width: 100%; height: auto; border-radius: 4px; margin: 0.4em 0; }
  .md-body mjx-container[display="true"] {
    max-width: 100%; overflow-x: auto; overflow-y: hidden;
    padding: 0.2em 0;
  }
  .md-body blockquote {
    margin: 0.8em 0; padding: 0 1em;
    border-left: 4px solid var(--doc-border); color: var(--doc-muted);
  }
  .md-body hr { border: 0; border-top: 1px solid var(--doc-border); margin: 1.6em 0; }
  .md-body ul, .md-body ol { padding-left: 2em; margin: 0.8em 0; }
  .md-body li { margin: 0.2em 0; }
  .md-body code:not(pre code) {
    background: var(--doc-code-bg); padding: 1px 6px; border-radius: 4px;
    font-size: 0.88em; font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
  }
  .md-body pre {
    background: var(--doc-code-bg); padding: 14px 16px; border-radius: 6px;
    overflow-x: auto; margin: 1em 0;
    font-size: 13px; line-height: 1.5;
  }
  .md-body pre code {
    background: transparent; padding: 0; border-radius: 0; font-size: inherit;
  }
  .md-body table {
    border-collapse: collapse; margin: 1em 0;
    display: block; overflow-x: auto; max-width: 100%;
  }
  .md-body th, .md-body td {
    border: 1px solid var(--doc-border); padding: 6px 12px;
  }
  .md-body th { background: var(--doc-code-bg); }
  .md-body tr:nth-child(even) { background: var(--doc-row); }
  .md-body kbd {
    background: var(--doc-code-bg); border: 1px solid var(--doc-border); border-bottom-width: 2px;
    border-radius: 4px; padding: 1px 6px; font-size: 0.85em;
  }
  .md-toggle {
    position: absolute; top: 8px; right: 12px;
    background: var(--floating-bg);
    color: var(--floating-text); border: 1px solid var(--floating-border);
    border-radius: 4px; padding: 3px 10px; font-size: 12px;
    cursor: pointer; z-index: 3;
    opacity: 0; transition: opacity 0.2s;
  }
  .preview:hover .md-toggle, .md-toggle:focus { opacity: 1; }
  @media (max-width: 820px) { .md-toggle { opacity: 0.85; } }
  .preview .nav-arrow {
    position: absolute; top: 50%; transform: translateY(-50%);
    background: var(--nav-bg);
    color: white; border: none; border-radius: 50%;
    width: 36px; height: 36px; font-size: 18px;
    cursor: pointer; opacity: 0;
    transition: opacity 0.2s;
  }
  .preview:hover .nav-arrow { opacity: 0.8; }
  .preview .nav-arrow.left { left: 10px; }
  .preview .nav-arrow.right { right: 10px; }
  .preview .nav-arrow:hover { background: var(--nav-hover-bg); opacity: 1; }
  .preview .nav-arrow:disabled { display: none; }

  .preview-info {
    position: absolute; top: 0; left: 0; right: 0;
    background: var(--preview-info-bg);
    padding: 8px 12px;
    display: flex; align-items: center; gap: 8px;
    color: white; font-size: 12px;
    pointer-events: none;
    opacity: 0; transition: opacity 0.2s;
  }
  .preview:hover .preview-info { opacity: 1; }
  .preview-info > * { pointer-events: auto; }
  .preview-info .fname {
    flex: 1; overflow: hidden; text-overflow: ellipsis; white-space: nowrap;
  }
  .preview-info button {
    background: var(--preview-info-button-bg); border: 1px solid var(--preview-info-button-border);
    color: white; font-size: 12px; padding: 3px 8px;
  }
  .preview.pdf-mode .preview-info {
    top: auto; left: auto; right: 12px; bottom: 12px;
    max-width: min(520px, calc(100% - 24px));
    border: 1px solid var(--preview-info-button-border);
    border-radius: 6px;
    background: var(--floating-bg);
    z-index: 2;
  }
  .preview .text-banner {
    position: sticky; top: 0; left: 0; right: 0;
    background: var(--warning); color: white;
    padding: 4px 8px; font-size: 12px;
    z-index: 2;
  }
  .preview .force-text {
    margin-top: 12px;
  }

  .mobile-only { display: none; }

  /* Narrow viewport: swipe between panes */
  @media (max-width: 820px) {
    .app, .app.sidebar-collapsed {
      display: flex;
      grid-template-columns: none;
      overflow-x: auto; overflow-y: hidden;
      scroll-snap-type: x mandatory;
      scrollbar-width: none;
      -webkit-overflow-scrolling: touch;
    }
    .app::-webkit-scrollbar { display: none; }
    .app.sidebar-collapsed #pane-left {
      visibility: visible; pointer-events: auto;
    }
    .pane {
      flex: 0 0 100%;
      width: 100%;
      scroll-snap-align: start;
      scroll-snap-stop: always;
    }
    .pane + .pane { border-left: none; border-top: 1px solid var(--border); }
    .mobile-only { display: inline-flex; }
    .favorites {
      max-height: none;
      overflow: hidden;
    }
    .favorites.is-expanded {
      max-height: min(34vh, 240px);
      overflow-y: auto;
    }
    .preview .nav-arrow { opacity: 0.6; }
    .preview-info { opacity: 1; }
    .sidebar-toggle { display: none; }
    .sidebar-reopen { display: none; }
    .sidebar-resizer { display: none; }
  }

  /* Cloud Light application shell — canonical layout from DESIGN.md. */
  html, body {
    background: var(--bg);
    color: var(--text);
    font-size: 14px;
  }
  button, input, select { font: inherit; }
  button:focus-visible, a:focus-visible, [tabindex]:focus-visible, input:focus-visible {
    outline: 3px solid #84adff;
    outline-offset: 2px;
  }
  .ui-icon {
    width: 18px;
    height: 18px;
    display: block;
    flex: 0 0 auto;
    fill: none;
    stroke: currentColor;
    stroke-width: 1.8;
    stroke-linecap: round;
    stroke-linejoin: round;
  }
  .icon-button {
    width: 36px;
    height: 36px;
    padding: 0;
    display: inline-flex;
    align-items: center;
    justify-content: center;
    border: 1px solid transparent;
    border-radius: 8px;
    background: transparent;
    color: var(--muted);
  }
  .icon-button:hover {
    background: var(--hover);
    border-color: var(--border);
    color: var(--text);
  }
  .text-button {
    height: 36px;
    display: inline-flex;
    align-items: center;
    gap: 7px;
    padding: 0 11px;
    border: 1px solid var(--border);
    border-radius: 8px;
    background: var(--panel);
    color: #344054;
    font-weight: 600;
  }
  .text-button:hover { background: var(--hover); }
  .primary-button {
    height: 36px;
    display: inline-flex;
    align-items: center;
    gap: 7px;
    padding: 0 13px;
    border: 1px solid var(--accent);
    border-radius: 8px;
    background: var(--accent);
    color: #ffffff;
    font-weight: 650;
    box-shadow: 0 1px 2px rgba(16, 24, 40, 0.08);
  }
  .primary-button:hover { background: var(--accent-2); border-color: var(--accent-2); }

  .app, .app.sidebar-collapsed {
    --sidebar-width: 440px;
    display: grid;
    grid-template-columns: minmax(340px, min(var(--sidebar-width), 52vw)) minmax(0, 1fr);
    width: 100%;
    height: 100vh;
    height: 100dvh;
    overflow: hidden;
    background: var(--bg);
    transition: grid-template-columns 0.18s ease;
  }
  .app.sidebar-collapsed {
    grid-template-columns: 0 minmax(0, 1fr);
  }
  .app.sidebar-collapsed #pane-left {
    visibility: hidden;
    pointer-events: none;
  }
  .nav-rail {
    min-width: 0;
    max-height: min(38vh, 320px);
    flex: 0 0 auto;
    display: flex;
    flex-direction: column;
    overflow: hidden;
    background: var(--panel-2);
    border-bottom: 1px solid var(--border);
    z-index: 2;
  }
  .app.sidebar-collapsed .nav-rail {
    visibility: visible;
    opacity: 1;
  }
  .brand {
    height: 60px;
    flex: 0 0 auto;
    display: flex;
    align-items: center;
    gap: 10px;
    padding: 0 12px 0 16px;
    border-bottom: 1px solid var(--border);
  }
  .brand-mark {
    width: 34px;
    height: 34px;
    display: grid;
    place-items: center;
    border-radius: 10px;
    background: var(--accent);
    color: #ffffff;
    box-shadow: 0 1px 2px rgba(16, 24, 40, 0.12);
  }
  .brand-mark .ui-icon { width: 19px; height: 19px; }
  .brand-copy { min-width: 0; display: flex; flex: 1; flex-direction: column; gap: 1px; }
  .brand-copy strong { font-size: 15px; line-height: 1.2; letter-spacing: -0.01em; }
  .brand-copy span { color: var(--muted); font-size: 11px; line-height: 1.2; }
  .brand .sidebar-toggle {
    width: 32px;
    height: 32px;
    display: inline-flex;
    align-items: center;
    justify-content: center;
    padding: 0;
    border: 0;
    border-radius: 8px;
    background: transparent;
    color: var(--muted);
    opacity: 1;
  }
  .brand .sidebar-toggle:hover { background: var(--hover); color: var(--text); }
  .nav-content {
    flex: 1;
    min-height: 0;
    overflow-y: auto;
    padding: 9px 10px;
  }
  .nav-section + .nav-section { margin-top: 9px; }
  .nav-section-heading {
    min-height: 26px;
    display: flex;
    align-items: center;
    gap: 8px;
    padding: 0 8px;
    color: #98a2b3;
    font-size: 11px;
    font-weight: 700;
    letter-spacing: 0.06em;
    text-transform: uppercase;
  }
  .nav-section-heading .count {
    margin-left: auto;
    min-width: 20px;
    padding: 1px 6px;
    border-radius: 999px;
    background: #eef2f6;
    color: var(--muted);
    text-align: center;
    font-size: 10px;
  }
  .favorites-toggle {
    width: 100%;
    border: 0;
    border-radius: 7px;
    background: transparent;
    cursor: pointer;
    text-align: left;
  }
  .favorites-toggle:hover { background: var(--hover); }
  .favorites-toggle .favorites-chevron {
    width: 15px;
    height: 15px;
    margin-left: 2px;
    transform: rotate(180deg);
    transition: transform 0.16s ease;
  }
  .favorites-toggle[aria-expanded="true"] .favorites-chevron {
    transform: rotate(-90deg);
  }
  .nav-item, .favorite-row {
    width: 100%;
    min-height: 36px;
    display: grid;
    grid-template-columns: 20px minmax(0, 1fr) auto;
    align-items: center;
    gap: 10px;
    padding: 6px 9px;
    border: 0;
    border-radius: 8px;
    background: transparent;
    color: #344054;
    cursor: pointer;
    text-align: left;
  }
  .nav-item:hover, .favorite-row:hover { background: var(--hover); text-decoration: none; }
  .nav-item.current, .favorite-row.current {
    background: var(--selected);
    color: #1849a9;
  }
  .nav-item .label, .favorite-row .name {
    min-width: 0;
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
    color: inherit;
    font-weight: 560;
  }
  .nav-item .meta, .nav-status { color: var(--muted); font-size: 11px; }
  .favorites {
    max-height: none;
    overflow: visible;
    border: 0;
    background: transparent;
  }
  .favorites:empty { display: block; }
  .favorite-row {
    grid-template-columns: 20px minmax(0, 1fr) 28px;
    border-left: 0;
  }
  .favorite-row > .ui-icon, .favorite-row > span > .ui-icon { color: #f79009; }
  .favorite-row .favorite-toggle {
    width: 28px;
    height: 28px;
    padding: 0;
    display: inline-flex;
    align-items: center;
    justify-content: center;
    border: 0;
    border-radius: 7px;
    background: transparent;
    color: #98a2b3;
    opacity: 0;
  }
  .favorite-row:hover .favorite-toggle,
  .favorite-row:focus-within .favorite-toggle { opacity: 1; }
  .favorite-row .favorite-toggle:hover { color: var(--danger); background: #fef3f2; }
  .favorite-empty {
    padding: 8px 9px;
    color: #98a2b3;
    font-size: 12px;
    line-height: 1.45;
  }
  .nav-footer {
    flex: 0 0 auto;
    padding: 6px 10px;
    border-top: 1px solid var(--border);
  }

  .pane {
    min-width: 0;
    display: flex;
    flex: initial;
    flex-direction: column;
    overflow: hidden;
    background: var(--panel);
  }
  .pane + .pane { border-left: 1px solid var(--border); border-top: 0; }
  .directory-header, .preview-header {
    min-height: 68px;
    flex: 0 0 auto;
    border-bottom: 1px solid var(--border);
    background: rgba(255, 255, 255, 0.96);
  }
  .directory-header {
    display: flex;
    flex-direction: column;
    justify-content: center;
    gap: 7px;
    padding: 9px 12px;
  }
  .directory-topline {
    min-width: 0;
    display: flex;
    align-items: center;
    gap: 7px;
  }
  .crumbs {
    min-width: 0;
    flex: 1;
    display: flex;
    flex-wrap: nowrap;
    align-items: center;
    gap: 1px;
    overflow: hidden;
    font-size: 13px;
  }
  .crumbs a, .crumbs .current {
    max-width: 180px;
    padding: 3px 6px;
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
    border-radius: 6px;
    color: #344054;
  }
  .crumbs .current { color: var(--text); font-weight: 650; }
  .crumbs span.sep { padding: 0; color: #b2b8c2; }
  .crumbs .edit-path {
    width: 28px;
    height: 28px;
    padding: 0;
    display: inline-flex;
    align-items: center;
    justify-content: center;
    border: 0;
    border-radius: 6px;
    color: var(--muted);
  }
  .crumbs .path-input {
    height: 34px;
    min-width: 0;
    padding: 0 10px;
    border: 1px solid var(--accent);
    border-radius: 8px;
    background: var(--panel);
    color: var(--text);
    font-size: 13px;
    font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
  }
  .toolbar {
    display: flex;
    align-items: center;
    gap: 6px;
    flex-wrap: nowrap;
  }
  .toolbar .spacer { flex: 1; }
  .toolbar label.btn { padding: 0; border: 0; background: transparent; }
  .toolbar label.btn:hover { background: transparent; }
  .sidebar-reopen {
    position: static;
    transform: none;
    width: 34px;
    height: 34px;
    display: inline-flex;
    align-items: center;
    justify-content: center;
    padding: 0;
    border: 1px solid transparent;
    border-radius: 8px;
    background: transparent;
    color: var(--muted);
    opacity: 1;
  }
  .sidebar-reopen:hover { border-color: var(--border); background: var(--hover); color: var(--text); }
  .app:not(.sidebar-collapsed) #btn-sidebar-reopen { display: none; }
  .sidebar-resizer { right: -5px; width: 10px; }
  .sidebar-resizer::after { right: 4px; width: 1px; background: transparent; }
  .list-summary {
    min-height: 34px;
    flex: 0 0 auto;
    display: flex;
    align-items: center;
    gap: 8px;
    padding: 0 14px;
    border-bottom: 1px solid #f0f2f5;
    background: var(--panel);
    color: var(--muted);
    font-size: 12px;
  }
  .list-summary .spacer { flex: 1; }
  .listing {
    flex: 1;
    min-height: 0;
    padding: 6px 8px 14px;
    overflow-y: auto;
    overflow-x: hidden;
    background: var(--panel);
  }
  .listing.dragover::before { content: "松开以上传文件"; }
  .row, .row.dir {
    min-height: 42px;
    display: grid;
    grid-template-columns: 22px minmax(0, 1fr) auto 30px;
    align-items: center;
    gap: 9px;
    padding: 5px 7px;
    border: 1px solid transparent;
    border-left: 1px solid transparent;
    border-radius: 8px;
    cursor: pointer;
  }
  .row:hover { background: var(--hover); }
  .row.selected {
    border-color: #d1e0ff;
    border-left-color: #d1e0ff;
    background: var(--selected);
  }
  .row .icon {
    width: 22px;
    height: 22px;
    display: grid;
    place-items: center;
    color: #667085;
    font-size: 0;
  }
  .row.dir .icon { color: #f79009; }
  .row.image .icon { color: #7f56d9; }
  .row.video .icon { color: #d92d20; }
  .row.pdf .icon { color: #d92d20; }
  .row.markdown .icon, .row.text .icon { color: #1570ef; }
  .row .icon .ui-icon { width: 19px; height: 19px; }
  .row .name {
    min-width: 0;
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
    color: #344054;
    font-weight: 520;
  }
  .row.selected .name { color: #1849a9; font-weight: 650; }
  .row .meta { color: #98a2b3; font-size: 11px; font-variant-numeric: tabular-nums; }
  .row .favorite-toggle {
    width: 28px;
    height: 28px;
    padding: 0;
    display: inline-flex;
    align-items: center;
    justify-content: center;
    border: 0;
    border-radius: 7px;
    background: transparent;
    color: #98a2b3;
    opacity: 0;
  }
  .row:hover .favorite-toggle, .row:focus-within .favorite-toggle,
  .row .favorite-toggle.active { opacity: 1; }
  .row .favorite-toggle:hover, .row .favorite-toggle.active {
    background: var(--star-bg);
    color: var(--star);
  }
  .uploads {
    flex: 0 0 auto;
    max-height: 112px;
    overflow-y: auto;
    padding: 0 12px;
    border-top: 1px solid var(--border);
    background: var(--panel-2);
  }
  .uploads:empty { display: none; }
  .upload-item { padding: 9px 0; color: #344054; }
  .upload-item .bar { height: 3px; margin-top: 6px; }

  .preview-header {
    min-width: 0;
    display: flex;
    align-items: center;
    gap: 10px;
    padding: 9px 14px;
  }
  .preview-heading { flex: 1; min-width: 0; }
  .preview-title {
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
    color: var(--text);
    font-size: 14px;
    font-weight: 650;
  }
  .preview-meta {
    margin-top: 2px;
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
    color: var(--muted);
    font-size: 11px;
  }
  .preview-header [hidden] { display: none !important; }
  .preview {
    flex: 1;
    min-height: 0;
    padding: 0;
    overflow: auto;
    background: var(--preview-bg);
  }
  .preview.empty {
    display: grid;
    place-items: center;
    color: var(--muted);
  }
  .empty-state {
    max-width: 320px;
    padding: 28px;
    text-align: center;
  }
  .empty-state-icon {
    width: 52px;
    height: 52px;
    margin: 0 auto 14px;
    display: grid;
    place-items: center;
    border: 1px solid var(--border);
    border-radius: 14px;
    background: var(--panel);
    color: #98a2b3;
    box-shadow: 0 1px 2px rgba(16, 24, 40, 0.04);
  }
  .empty-state h2 { margin: 0; color: #344054; font-size: 15px; font-weight: 650; }
  .empty-state p { margin: 7px 0 0; color: var(--muted); font-size: 12px; line-height: 1.55; }
  .preview-info { display: none; }
  .preview pre { padding: 18px 20px; background: var(--code-bg); color: var(--code-text); }
  .preview.md-mode { background: var(--doc-bg); }
  .md-render { padding-top: 8px; }
  .md-toggle { display: none; }
  .preview.pdf-mode, .preview.html-mode { background: var(--panel); }
  .preview video { background: #000000; }
  .preview .nav-arrow { box-shadow: 0 4px 12px rgba(16, 24, 40, 0.18); }
  #btn-back-list { display: none; }

  @media (max-width: 1179px) and (min-width: 821px) {
    .nav-rail { max-height: min(34vh, 260px); }
    .brand-copy span { display: none; }
  }

  @media (max-width: 820px) {
    .app, .app.sidebar-collapsed {
      display: block;
      overflow: hidden;
    }
    .app.sidebar-collapsed #pane-left {
      visibility: visible;
      pointer-events: auto;
    }
    .pane {
      width: 100%;
      height: 100%;
      display: flex;
      flex: initial;
      scroll-snap-align: none;
    }
    #pane-right { display: none; border: 0; }
    .app.preview-open #pane-left { display: none; }
    .app.preview-open #pane-right { display: flex; }
    .directory-header, .preview-header { min-height: 62px; }
    .nav-rail {
      position: static;
      width: auto;
      max-height: min(30vh, 220px);
      visibility: visible;
      opacity: 1;
      transform: none;
      box-shadow: none;
    }
    .brand { height: 54px; }
    .brand .sidebar-toggle, #btn-sidebar-reopen { display: none !important; }
    .directory-header { padding: 8px 10px; }
    .toolbar { overflow-x: auto; scrollbar-width: none; }
    .toolbar::-webkit-scrollbar { display: none; }
    .toolbar .spacer { display: none; }
    .text-button, .primary-button { flex: 0 0 auto; }
    .list-summary { padding: 0 12px; }
    .listing { padding: 5px 6px 12px; }
    .row, .row.dir {
      grid-template-columns: 22px minmax(0, 1fr) 28px;
      min-height: 46px;
      padding: 6px 8px;
    }
    .row .meta { display: none; }
    .row .favorite-toggle { opacity: 1; }
    .preview-header { padding: 8px 10px; }
    #btn-back-list { display: inline-flex; }
    .preview-header .text-button .button-label { display: none; }
    .preview-header .text-button { width: 36px; padding: 0; justify-content: center; }
    .sidebar-resizer { display: none; }
    .preview .nav-arrow { opacity: 0.76; }
    .md-render { padding: 18px 14px 64px; }
  }

  @media (prefers-reduced-motion: reduce) {
    *, *::before, *::after {
      scroll-behavior: auto !important;
      transition-duration: 0.01ms !important;
      animation-duration: 0.01ms !important;
      animation-iteration-count: 1 !important;
    }
  }
</style>
</head>
<body>
<svg width="0" height="0" aria-hidden="true" focusable="false"
     style="position:absolute;overflow:hidden">
  <symbol id="icon-files" viewBox="0 0 24 24">
    <path d="M3.5 6.5h6l2 2h9v9.5a2 2 0 0 1-2 2h-15a2 2 0 0 1-2-2V8.5a2 2 0 0 1 2-2Z"/>
    <path d="M2 11h18.5"/>
  </symbol>
  <symbol id="icon-home" viewBox="0 0 24 24">
    <path d="m3 11 9-8 9 8"/><path d="M5.5 9.5V21h13V9.5"/><path d="M9.5 21v-7h5v7"/>
  </symbol>
  <symbol id="icon-folder" viewBox="0 0 24 24">
    <path d="M3 5.5h6l2 2h10v10.5a2 2 0 0 1-2 2H3a2 2 0 0 1-2-2V7.5a2 2 0 0 1 2-2Z"/>
  </symbol>
  <symbol id="icon-image" viewBox="0 0 24 24">
    <rect x="2.5" y="3.5" width="19" height="17" rx="2"/><circle cx="8" cy="9" r="1.5"/>
    <path d="m4.5 18 5-5 3.5 3 2.5-2.5 4 4.5"/>
  </symbol>
  <symbol id="icon-video" viewBox="0 0 24 24">
    <rect x="2.5" y="4.5" width="14" height="15" rx="2"/><path d="m16.5 10 5-3v10l-5-3"/>
  </symbol>
  <symbol id="icon-file-text" viewBox="0 0 24 24">
    <path d="M6 2.5h8l4 4V21H6a2 2 0 0 1-2-2V4.5a2 2 0 0 1 2-2Z"/><path d="M14 2.5v5h4M8 12h6M8 16h7"/>
  </symbol>
  <symbol id="icon-file" viewBox="0 0 24 24">
    <path d="M6 2.5h8l4 4V21H6a2 2 0 0 1-2-2V4.5a2 2 0 0 1 2-2Z"/><path d="M14 2.5v5h4"/>
  </symbol>
  <symbol id="icon-menu" viewBox="0 0 24 24"><path d="M4 7h16M4 12h16M4 17h16"/></symbol>
  <symbol id="icon-panel-left" viewBox="0 0 24 24">
    <rect x="3" y="4" width="18" height="16" rx="2"/><path d="M9 4v16M15 9l-3 3 3 3"/>
  </symbol>
  <symbol id="icon-chevron-left" viewBox="0 0 24 24"><path d="m15 18-6-6 6-6"/></symbol>
  <symbol id="icon-arrow-up" viewBox="0 0 24 24"><path d="m6 10 6-6 6 6M12 4v16"/></symbol>
  <symbol id="icon-refresh" viewBox="0 0 24 24">
    <path d="M20 7v5h-5M4 17v-5h5"/><path d="M6.1 8A7 7 0 0 1 18.8 6L20 8M4 16l1.2 2A7 7 0 0 0 18 16"/>
  </symbol>
  <symbol id="icon-sort" viewBox="0 0 24 24"><path d="M8 6h12M8 12h8M8 18h4M4 4v16M2 18l2 2 2-2"/></symbol>
  <symbol id="icon-upload" viewBox="0 0 24 24"><path d="m7 9 5-5 5 5M12 4v12"/><path d="M5 15v4h14v-4"/></symbol>
  <symbol id="icon-eye" viewBox="0 0 24 24"><path d="M2 12s3.5-6 10-6 10 6 10 6-3.5 6-10 6-10-6-10-6Z"/><circle cx="12" cy="12" r="2.5"/></symbol>
  <symbol id="icon-eye-closed" viewBox="0 0 24 24">
    <path d="M3 9.5c2 3.3 5 5 9 5s7-1.7 9-5"/>
    <path d="m5.5 13.2-2 2M9 15l-1 3M15 15l1 3M18.5 13.2l2 2"/>
  </symbol>
  <symbol id="icon-star" viewBox="0 0 24 24"><path d="m12 3 2.8 5.7 6.2.9-4.5 4.4 1.1 6.2-5.6-3-5.6 3 1.1-6.2L3 9.6l6.2-.9L12 3Z"/></symbol>
  <symbol id="icon-download" viewBox="0 0 24 24"><path d="M12 3v13M7 11l5 5 5-5M5 21h14"/></symbol>
  <symbol id="icon-code" viewBox="0 0 24 24"><path d="m8 9-4 3 4 3M16 9l4 3-4 3M14 5l-4 14"/></symbol>
  <symbol id="icon-edit" viewBox="0 0 24 24"><path d="M4 20h4l11-11-4-4L4 16v4Z"/><path d="m13.5 6.5 4 4"/></symbol>
</svg>

<div class="app" id="app">
  <section class="pane" id="pane-left" aria-label="文件浏览侧栏">
  <aside class="nav-rail" id="nav-rail" aria-label="主导航">
    <div class="brand">
      <div class="brand-mark" aria-hidden="true">
        <svg class="ui-icon"><use href="#icon-files"></use></svg>
      </div>
      <div class="brand-copy">
        <strong>文件</strong>
        <span>Web File Manager</span>
      </div>
      <button class="sidebar-toggle" id="btn-sidebar" type="button"
              title="收起文件侧栏（快捷键 B）" aria-label="收起文件侧栏" aria-expanded="true">
        <svg class="ui-icon"><use href="#icon-panel-left"></use></svg>
      </button>
    </div>

    <nav class="nav-content">
      <section class="nav-section" aria-labelledby="favorites-heading">
        <button class="nav-section-heading favorites-toggle" id="btn-favorites-toggle"
                type="button" aria-expanded="false" aria-controls="favorites">
          <svg class="ui-icon favorites-chevron" aria-hidden="true"><use href="#icon-chevron-left"></use></svg>
          <span id="favorites-heading">收藏夹</span>
          <span class="count" id="favorites-count">0</span>
        </button>
        <div class="favorites" id="favorites" hidden></div>
      </section>
    </nav>

    <div class="nav-footer">
      <button class="nav-item" id="btn-hidden" type="button" title="显示隐藏文件"
              aria-label="隐藏文件当前不可见，点击显示" aria-pressed="false">
        <svg class="ui-icon" aria-hidden="true"><use id="hidden-eye-icon" href="#icon-eye-closed"></use></svg>
        <span class="label">隐藏文件</span>
        <span class="nav-status" id="hidden-status">隐藏</span>
      </button>
    </div>
  </aside>
    <header class="directory-header">
      <div class="directory-topline">
        <div class="crumbs" id="crumbs"></div>
      </div>
      <div class="toolbar" aria-label="目录操作">
        <button class="icon-button" id="btn-up" type="button" title="返回上一级" aria-label="返回上一级">
          <svg class="ui-icon"><use href="#icon-arrow-up"></use></svg>
        </button>
        <button class="icon-button" id="btn-refresh" type="button" title="刷新" aria-label="刷新">
          <svg class="ui-icon"><use href="#icon-refresh"></use></svg>
        </button>
        <button class="text-button" id="btn-sort" type="button" title="切换排序方式">
          <svg class="ui-icon"><use href="#icon-sort"></use></svg>
          <span id="sort-label">名称 ↑</span>
        </button>
        <span class="spacer"></span>
        <label class="btn" title="上传文件">
          <span class="primary-button">
            <svg class="ui-icon"><use href="#icon-upload"></use></svg>
            上传
          </span>
          <input type="file" id="upload-input" multiple>
        </label>
      </div>
    </header>
    <div class="list-summary">
      <span id="item-count">0 项</span>
      <span class="spacer"></span>
      <span id="directory-status">主目录</span>
    </div>
    <div class="listing" id="listing" role="list"></div>
    <div class="uploads" id="uploads" aria-live="polite"></div>
    <div class="sidebar-resizer" id="sidebar-resizer" title="拖动调整文件列表宽度" aria-hidden="true"></div>
  </section>

  <section class="pane" id="pane-right" aria-label="文件预览">
    <header class="preview-header">
      <button class="sidebar-reopen" id="btn-sidebar-reopen" type="button"
              title="展开文件侧栏（快捷键 B）" aria-label="展开文件侧栏" aria-expanded="false">
        <svg class="ui-icon"><use href="#icon-panel-left"></use></svg>
      </button>
      <button class="icon-button" id="btn-back-list" type="button" title="返回文件列表" aria-label="返回文件列表">
        <svg class="ui-icon"><use href="#icon-chevron-left"></use></svg>
      </button>
      <div class="preview-heading">
        <div class="preview-title" id="preview-title">预览</div>
        <div class="preview-meta" id="preview-meta">选择一个文件，在这里查看内容</div>
      </div>
      <button class="text-button" id="preview-toggle" type="button" hidden>
        <svg class="ui-icon"><use href="#icon-code"></use></svg>
        <span class="button-label">源码</span>
      </button>
      <button class="text-button" id="preview-download" type="button" hidden>
        <svg class="ui-icon"><use href="#icon-download"></use></svg>
        <span class="button-label">下载</span>
      </button>
    </header>
    <div class="preview empty" id="preview">
      <div class="empty-state">
        <div class="empty-state-icon" aria-hidden="true">
          <svg class="ui-icon"><use href="#icon-file"></use></svg>
        </div>
        <h2>选择文件进行预览</h2>
        <p>支持图片、视频、PDF、Markdown、HTML 和代码文本。</p>
      </div>
    </div>
  </section>
</div>

<script>
const MOBILE_QUERY = window.matchMedia("(max-width: 820px)");
const HOME_PATH = __WEBFM_HOME_JSON__;
const SIDEBAR_WIDTH_KEY = "wfm-sidebar-width";
const NAV_COLLAPSED_KEY = "wfm-browser-sidebar-collapsed-v3";
const SIDEBAR_DEFAULT_WIDTH = 440;
const SIDEBAR_MIN_WIDTH = 340;
const SIDEBAR_MAX_WIDTH = 900;
let sidebarWidth = SIDEBAR_DEFAULT_WIDTH;

const state = {
  cwd: "/",                  // path relative to HOME, "/" = HOME root
  entries: [],
  selected: null,            // currently previewed entry name
  imageList: [],             // filenames of images in cwd, for ←/→
  imageIdx: -1,
  favorites: [],
  showHidden: localStorage.getItem("wfm-show-hidden") === "1",
  sortBy: localStorage.getItem("wfm-sort") || "name",
};

const SORT_MODES = ["name", "mtime", "size"];
const SORT_LABEL = { name: "名称 ↑", mtime: "修改时间 ↓", size: "大小 ↓" };
const SORT_CMP = {
  name:  (a, b) => a.name.localeCompare(b.name, "zh"),
  mtime: (a, b) => (b.mtime - a.mtime) || a.name.localeCompare(b.name, "zh"),
  size:  (a, b) => (b.size  - a.size)  || a.name.localeCompare(b.name, "zh"),
};

function sortEntries(entries) {
  const dirs  = entries.filter(e => e.is_dir);
  const files = entries.filter(e => !e.is_dir);
  // Dir size from stat() is meaningless — fall back to name for size mode.
  dirs.sort(state.sortBy === "size" ? SORT_CMP.name : SORT_CMP[state.sortBy]);
  files.sort(SORT_CMP[state.sortBy]);
  return [...dirs, ...files];
}

const $ = (id) => document.getElementById(id);

function iconMarkup(name) {
  return `<svg class="ui-icon" aria-hidden="true"><use href="#icon-${name}"></use></svg>`;
}

const ICON = {
  dir: iconMarkup("folder"),
  image: iconMarkup("image"),
  video: iconMarkup("video"),
  pdf: iconMarkup("file-text"),
  markdown: iconMarkup("file-text"),
  html: iconMarkup("code"),
  text: iconMarkup("file-text"),
  other: iconMarkup("file"),
};

function fmtSize(n) {
  if (n < 1024) return n + " B";
  if (n < 1024 * 1024) return (n / 1024).toFixed(1) + " KB";
  if (n < 1024 * 1024 * 1024) return (n / 1024 / 1024).toFixed(1) + " MB";
  return (n / 1024 / 1024 / 1024).toFixed(2) + " GB";
}

function fmtTime(ts) {
  const d = new Date(ts * 1000);
  const now = new Date();
  const sameYear = d.getFullYear() === now.getFullYear();
  const m = String(d.getMonth() + 1).padStart(2, "0");
  const day = String(d.getDate()).padStart(2, "0");
  const hh = String(d.getHours()).padStart(2, "0");
  const mm = String(d.getMinutes()).padStart(2, "0");
  return sameYear ? `${m}-${day} ${hh}:${mm}` : `${d.getFullYear()}-${m}-${day}`;
}

function api(url) {
  return fetch(url, { credentials: "same-origin" }).then(r => {
    if (!r.ok) throw new Error("HTTP " + r.status);
    return r.json();
  });
}

function normalizePath(input) {
  let p = (input || "").trim();
  if (!p || p === "~" || p === "/") return "/";
  if (p.startsWith("~/")) p = p.slice(1);   // ~/foo → /foo
  else if (p.startsWith("~")) p = p.slice(1);
  if (!p.startsWith("/")) {
    // Treat as relative to current cwd
    const base = state.cwd === "/" ? "" : state.cwd;
    p = base + "/" + p;
  }
  return p;
}

function decodePathText(input) {
  try { return decodeURIComponent(input); }
  catch (e) { return input; }
}

function normalizeExternalPath(input) {
  let p = (input || "").trim();
  if (!p) return "/";
  if (p.startsWith("file://")) {
    try { p = decodeURIComponent(new URL(p).pathname); } catch (e) {}
  } else if (/^https?:\/\//i.test(p)) {
    try {
      const u = new URL(p, window.location.href);
      if (u.origin === window.location.origin) p = u.pathname;
    } catch (e) {}
  }
  p = decodePathText(p.split("#", 1)[0].split("?", 1)[0]);
  if (HOME_PATH && (p === HOME_PATH || p === HOME_PATH + "/")) return "/";
  if (HOME_PATH && p.startsWith(HOME_PATH + "/")) return p.slice(HOME_PATH.length) || "/";
  return normalizePath(p);
}

function pathForEditing(path) {
  if (!HOME_PATH) return path === "/" ? "/" : path;
  return path === "/" ? HOME_PATH + "/" : HOME_PATH + path;
}

async function openPath(input, opts) {
  const target = opts && opts.normalized ? input : normalizeExternalPath(input);
  if (await load(target, { silent: true })) return;

  const slash = target.lastIndexOf("/");
  const dir = slash <= 0 ? "/" : target.slice(0, slash);
  const name = target.slice(slash + 1);
  if (name && await load(dir, { silent: true })) {
    if (entryByName(name)) {
      selectFile(name);
      scrollToPreview();
      return;
    }
  }

  alert("Failed to open: " + target);
  if (!opts || opts.fallbackHome !== false) load("/");
}

function initialOpenTarget() {
  const params = new URLSearchParams(window.location.search);
  const raw = params.get("open") || params.get("path") || params.get("file");
  if (raw) return raw;
  const path = window.location.pathname;
  if (!path || path === "/") return "";
  if (/^\/(?:api|preview|pdfjs|mathjax)\//.test(path)) return "";
  return path;
}

async function openInitialPath() {
  const raw = initialOpenTarget();
  if (!raw) {
    load("/");
    return;
  }
  await openPath(raw);
}

function managerPathFromHref(href) {
  let raw = (href || "").trim();
  if (!raw || raw.startsWith("#")) return "";
  if (/^(?:mailto|tel|javascript):/i.test(raw)) return "";
  try {
    const u = new URL(raw, window.location.href);
    const param = u.searchParams.get("open") || u.searchParams.get("path") || u.searchParams.get("file");
    if (u.origin === window.location.origin && u.pathname === "/" && param) {
      return normalizeExternalPath(param);
    }
  } catch (e) {}
  if (/^https?:\/\//i.test(raw)) {
    try {
      const u = new URL(raw, window.location.href);
      if (u.origin !== window.location.origin) return "";
      raw = u.pathname;
    } catch (e) {
      return "";
    }
  }
  const decoded = decodePathText(raw);
  if (/^\/(?:api|preview|pdfjs|mathjax)\//.test(decoded)) return "";
  if (decoded.startsWith("file://")) return normalizeExternalPath(decoded);
  if (HOME_PATH && (decoded === HOME_PATH || decoded.startsWith(HOME_PATH + "/"))) {
    return normalizeExternalPath(decoded);
  }
  if (decoded.startsWith("~/") || decoded === "~") return normalizeExternalPath(decoded);
  if (decoded.startsWith("/")) return normalizeExternalPath(decoded);
  return "";
}

function enterPathEditMode() {
  const c = $("crumbs");
  c.innerHTML = "";
  const inp = document.createElement("input");
  inp.type = "text";
  inp.className = "path-input";
  inp.value = pathForEditing(state.cwd);
  inp.spellcheck = false;
  inp.autocapitalize = "off";
  inp.autocomplete = "off";

  let cancelled = false;
  let submitting = false;
  let skipBlurCommit = false;
  const finish = async (commit) => {
    if (submitting) return;
    if (!c.contains(inp)) return;  // already torn down by another nav
    if (commit && !cancelled) {
      const target = normalizeExternalPath(inp.value);
      submitting = true;
      inp.disabled = true;
      const ok = await load(target);
      submitting = false;
      if (!ok && c.contains(inp)) {
        inp.disabled = false;
        inp.focus();
        inp.select();
      }
    } else {
      buildCrumbs(state.cwd);
    }
  };
  inp.oninput = () => { skipBlurCommit = false; };
  inp.onkeydown = (e) => {
    if (e.key === "Enter") {
      e.preventDefault();
      skipBlurCommit = true;
      finish(true);
    }
    else if (e.key === "Escape") { e.preventDefault(); cancelled = true; finish(false); }
  };
  inp.onblur = () => {
    setTimeout(() => {
      if (skipBlurCommit) return;
      finish(true);
    }, 100);
  };

  c.appendChild(inp);
  inp.focus();
  // Select the trailing segment so users can quickly replace just that part
  const lastSlash = inp.value.lastIndexOf("/");
  if (lastSlash >= 0 && lastSlash < inp.value.length - 1) {
    inp.setSelectionRange(lastSlash + 1, inp.value.length);
  } else {
    inp.select();
  }
}

function buildCrumbs(path) {
  const c = $("crumbs");
  c.innerHTML = "";
  const root = document.createElement("a");
  root.href = "#"; root.textContent = "主目录";
  root.onclick = (e) => { e.preventDefault(); load("/"); };
  c.appendChild(root);

  if (path !== "/" && path !== "") {
    const parts = path.split("/").filter(Boolean);
    let acc = "";
    parts.forEach((p, i) => {
      const sep = document.createElement("span");
      sep.className = "sep"; sep.textContent = "/";
      c.appendChild(sep);
      acc += "/" + p;
      if (i === parts.length - 1) {
        const cur = document.createElement("span");
        cur.className = "current"; cur.textContent = p;
        cur.title = "点击输入路径";
        cur.onclick = enterPathEditMode;
        c.appendChild(cur);
      } else {
        const a = document.createElement("a");
        a.href = "#"; a.textContent = p;
        const target = acc;
        a.onclick = (e) => { e.preventDefault(); load(target); };
        c.appendChild(a);
      }
    });
  }

  const edit = document.createElement("button");
  edit.className = "edit-path";
  edit.innerHTML = iconMarkup("edit");
  edit.title = "输入路径";
  edit.setAttribute("aria-label", "输入路径");
  edit.onclick = enterPathEditMode;
  c.appendChild(edit);
}

function visibleEntries() {
  const filtered = state.showHidden
    ? state.entries
    : state.entries.filter(e => !e.name.startsWith("."));
  return sortEntries(filtered);
}

function labelForPath(path) {
  if (!path || path === "/") return "主目录";
  const parts = path.split("/").filter(Boolean);
  return parts[parts.length - 1] || path;
}

function setCurrentListingFavorite(path, favorite) {
  state.entries.forEach((entry) => {
    if (entry.is_dir && fullPath(entry.name) === path) {
      entry.favorite = favorite;
    }
  });
}

function renderFavorites() {
  const box = $("favorites");
  box.innerHTML = "";
  $("favorites-count").textContent = String(state.favorites.length);

  if (!state.favorites.length) {
    const empty = document.createElement("div");
    empty.className = "favorite-empty";
    empty.textContent = "在文件夹右侧点星标，即可固定到这里。";
    box.appendChild(empty);
    return;
  }

  state.favorites.forEach((fav) => {
    const row = document.createElement("div");
    row.className = "favorite-row";
    if (fav.path === state.cwd) row.classList.add("current");
    row.title = fav.path;
    row.tabIndex = 0;
    row.setAttribute("role", "button");

    const icon = document.createElement("span");
    icon.innerHTML = iconMarkup("folder");

    const star = document.createElement("button");
    star.className = "favorite-toggle active";
    star.type = "button";
    star.innerHTML = iconMarkup("star");
    star.title = "从收藏夹移除";
    const displayName = fav.path === "/" ? "主目录" : (fav.name || labelForPath(fav.path));
    star.setAttribute("aria-label", `从收藏夹移除 ${displayName}`);
    star.onclick = async (e) => {
      e.preventDefault();
      e.stopPropagation();
      star.disabled = true;
      try {
        await setFavorite(fav.path, false);
        setCurrentListingFavorite(fav.path, false);
        renderListing();
      } catch (err) {
        alert("更新收藏夹失败：" + err.message);
      } finally {
        star.disabled = false;
      }
    };

    const name = document.createElement("span");
    name.className = "name";
    name.textContent = displayName;
    name.title = fav.path;

    row.appendChild(icon);
    row.appendChild(name);
    row.appendChild(star);
    const openFavorite = () => {
      load(fav.path);
    };
    row.onclick = openFavorite;
    row.onkeydown = (e) => {
      if (e.key === "Enter" || e.key === " ") {
        e.preventDefault();
        openFavorite();
      }
    };
    box.appendChild(row);
  });
}

async function refreshFavorites() {
  try {
    const data = await api("/api/favorites");
    state.favorites = data.favorites || [];
    renderFavorites();
  } catch (e) {
    console.warn("加载收藏夹失败", e);
  }
}

async function setFavorite(path, favorite) {
  const res = await fetch("/api/favorites", {
    method: "POST",
    credentials: "same-origin",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ path, favorite }),
  });
  if (!res.ok) throw new Error("HTTP " + res.status);
  const data = await res.json();
  state.favorites = data.favorites || [];
  renderFavorites();
}

function renderListing() {
  const lst = $("listing");
  lst.innerHTML = "";
  const visible = visibleEntries();
  $("item-count").textContent = `${visible.length} 项`;
  state.imageList = visible.filter(e => e.kind === "image").map(e => e.name);
  if (state.selected) {
    state.imageIdx = state.imageList.indexOf(state.selected);
  }
  if (!visible.length) {
    const empty = document.createElement("div");
    empty.className = "favorite-empty";
    empty.style.padding = "20px 10px";
    empty.textContent = state.entries.length ? "隐藏文件未显示。" : "这个文件夹是空的。";
    lst.appendChild(empty);
    return;
  }
  visible.forEach((entry) => {
    const row = document.createElement("div");
    row.className = "row " + (entry.is_dir ? "dir" : entry.kind);
    row.title = fullPath(entry.name);
    row.tabIndex = 0;
    row.setAttribute("role", "listitem");
    if (state.selected === entry.name) row.classList.add("selected");

    const icon = document.createElement("span");
    icon.className = "icon";
    icon.innerHTML = ICON[entry.kind] || ICON.other;

    let favoriteBtn = null;
    if (entry.is_dir) {
      favoriteBtn = document.createElement("button");
      favoriteBtn.className = "favorite-toggle";
      favoriteBtn.type = "button";
      if (entry.favorite) favoriteBtn.classList.add("active");
      favoriteBtn.innerHTML = iconMarkup("star");
      favoriteBtn.title = entry.favorite ? "从收藏夹移除" : "添加到收藏夹";
      favoriteBtn.setAttribute("aria-label", `${entry.favorite ? "从收藏夹移除" : "添加到收藏夹"} ${entry.name}`);
      favoriteBtn.onclick = async (e) => {
        e.preventDefault();
        e.stopPropagation();
        const next = !entry.favorite;
        const path = fullPath(entry.name);
        favoriteBtn.disabled = true;
        try {
          await setFavorite(path, next);
          entry.favorite = next;
          favoriteBtn.classList.toggle("active", next);
          favoriteBtn.title = next ? "从收藏夹移除" : "添加到收藏夹";
          favoriteBtn.setAttribute("aria-label", `${next ? "从收藏夹移除" : "添加到收藏夹"} ${entry.name}`);
        } catch (err) {
          alert("更新收藏夹失败：" + err.message);
        } finally {
          favoriteBtn.disabled = false;
        }
      };
    }

    const name = document.createElement("span");
    name.className = "name";
    name.textContent = entry.name;
    name.title = entry.name;

    const meta = document.createElement("span");
    meta.className = "meta";
    meta.textContent = entry.is_dir ? fmtTime(entry.mtime)
                                    : `${fmtSize(entry.size)} · ${fmtTime(entry.mtime)}`;

    row.appendChild(icon);
    row.appendChild(name);
    row.appendChild(meta);
    if (favoriteBtn) row.appendChild(favoriteBtn);
    else {
      const spacer = document.createElement("span");
      spacer.setAttribute("aria-hidden", "true");
      row.appendChild(spacer);
    }

    const activateRow = () => {
      if (entry.is_dir) {
        const next = (state.cwd === "/" ? "" : state.cwd) + "/" + entry.name;
        load(next);
      } else {
        selectFile(entry.name);
        scrollToPreview();
      }
    };
    row.onclick = activateRow;
    row.onkeydown = (e) => {
      if (e.key === "Enter" || e.key === " ") {
        e.preventDefault();
        activateRow();
      }
    };
    lst.appendChild(row);
  });
}

function sameEntryRevision(before, after) {
  return Boolean(
    before && after && !after.is_dir
    && before.name === after.name
    && before.kind === after.kind
    && before.size === after.size
    && before.mtime === after.mtime
  );
}

async function load(path, opts) {
  try {
    const data = await api("/api/list?path=" + encodeURIComponent(path));
    const preservePreview = Boolean(opts && opts.preservePreview && path === state.cwd);
    const previousSelected = preservePreview ? state.selected : null;
    const previousEntry = previousSelected ? entryByName(previousSelected) : null;
    state.cwd = data.path || "/";
    state.entries = data.entries || [];
    const refreshedEntry = previousSelected
      ? state.entries.find(entry => entry.name === previousSelected && !entry.is_dir)
      : null;
    const keepPreviewDom = sameEntryRevision(previousEntry, refreshedEntry);
    state.selected = refreshedEntry ? previousSelected : null;
    state.imageIdx = -1;
    buildCrumbs(state.cwd);
    $("directory-status").textContent = labelForPath(state.cwd) || "主目录";
    renderFavorites();
    renderListing();
    if (!refreshedEntry) resetPreview();
    else if (!keepPreviewDom) renderPreview(refreshedEntry);
    return true;
  } catch (e) {
    if (!opts || !opts.silent) alert("加载失败：" + e.message);
    return false;
  }
}

function entryByName(name) {
  return state.entries.find(e => e.name === name);
}

function fullPath(name) {
  const base = state.cwd === "/" ? "" : state.cwd;
  return base + "/" + name;
}

// Cache-bust by mtime so editing a file with the same name re-fetches it
// instead of serving from the 1-hour browser cache. The backend ignores `v`.
function fileUrlFor(entry, mode, extra) {
  const v = entry.mtime || 0;
  let u = "/api/file?mode=" + mode
        + "&path=" + encodeURIComponent(fullPath(entry.name))
        + "&v=" + v;
  if (extra) u += extra;
  return u;
}

function previewUrlFor(entry) {
  const encodedPath = fullPath(entry.name)
    .split("/")
    .map(seg => encodeURIComponent(seg))
    .join("/");
  return "/preview" + encodedPath + "?v=" + (entry.mtime || 0) + "&viewer=html-note-v2";
}

function selectFile(name) {
  const entry = entryByName(name);
  if (!entry || entry.is_dir) return;
  state.selected = name;
  if (entry.kind === "image") {
    state.imageIdx = state.imageList.indexOf(name);
  } else {
    state.imageIdx = -1;
  }
  // Update row highlight
  document.querySelectorAll(".row").forEach(r => r.classList.remove("selected"));
  const rows = document.querySelectorAll(".row .name");
  rows.forEach(n => {
    if (n.textContent === name) n.parentElement.classList.add("selected");
  });
  renderPreview(entry);
}

const KIND_LABEL = {
  image: "图片", video: "视频", pdf: "PDF", markdown: "Markdown",
  html: "HTML", text: "文本", other: "文件",
};

function emptyPreviewMarkup() {
  return `<div class="empty-state">
    <div class="empty-state-icon" aria-hidden="true">${iconMarkup("file")}</div>
    <h2>选择文件进行预览</h2>
    <p>支持图片、视频、PDF、Markdown、HTML 和代码文本。</p>
  </div>`;
}

function resetPreview() {
  const pv = $("preview");
  pv.className = "preview empty";
  pv.innerHTML = emptyPreviewMarkup();
  $("preview-title").textContent = "预览";
  $("preview-meta").textContent = "选择一个文件，在这里查看内容";
  $("preview-toggle").hidden = true;
  $("preview-toggle").onclick = null;
  $("preview-download").hidden = true;
  $("preview-download").onclick = null;
  $("app").classList.remove("preview-open");
}

function clearPreviewBody(pv) {
  clearMarkdownMath(pv);
  pv.replaceChildren();
}

function setPreviewToggleLabel(button, label) {
  const text = button.querySelector(".button-label");
  if (text) text.textContent = label;
}

function renderPreview(entry) {
  const pv = $("preview");
  pv.className = "preview";
  clearPreviewBody(pv);

  $("preview-title").textContent = entry.name;
  $("preview-meta").textContent = `${KIND_LABEL[entry.kind] || "文件"} · ${fmtSize(entry.size)} · ${fmtTime(entry.mtime)}`;
  const download = $("preview-download");
  download.hidden = false;
  download.onclick = () => { window.location.href = fileUrlFor(entry, "download"); };
  const previewToggle = $("preview-toggle");
  previewToggle.hidden = !(entry.kind === "html" || entry.kind === "markdown");
  previewToggle.onclick = null;
  setPreviewToggleLabel(previewToggle, "源码");

  const fileUrl = fileUrlFor(entry, "raw");

  if (entry.kind === "image") {
    const img = document.createElement("img");
    img.src = fileUrl;
    img.alt = entry.name;
    pv.appendChild(img);
    addImageNav(pv);
  } else if (entry.kind === "video") {
    const video = document.createElement("video");
    video.src = fileUrl;
    video.controls = true;
    video.preload = "metadata";
    video.playsInline = true;
    video.defaultMuted = true;
    video.muted = true;
    video.title = "←/→ 后退/前进 5 秒";
    video.setAttribute("aria-keyshortcuts", "ArrowLeft ArrowRight");
    pv.appendChild(video);
  } else if (entry.kind === "pdf") {
    loadPdf(entry);
  } else if (entry.kind === "markdown") {
    loadMarkdown(entry, previewToggle);
  } else if (entry.kind === "html") {
    loadHtml(entry, previewToggle);
  } else if (entry.kind === "text") {
    loadText(entry, false);
  } else {
    const msg = document.createElement("div");
    msg.className = "empty-state";
    msg.innerHTML = `<div class="empty-state-icon" aria-hidden="true">${iconMarkup("file")}</div>
                     <h2>暂不支持预览</h2>
                     <p>可以下载后使用本地应用打开。</p>`;
    pv.appendChild(msg);
  }
}

// --- PDF: embed Mozilla's full PDF.js viewer (self-hosted under /pdfjs) ---
function loadPdf(entry) {
  const pv = $("preview");
  pv.classList.add("pdf-mode");
  const fileUrl = fileUrlFor(entry, "raw");
  const viewerUrl = "/pdfjs/web/viewer.html?file=" + encodeURIComponent(fileUrl) + "#pagemode=none";
  const iframe = document.createElement("iframe");
  iframe.className = "pdf-viewer";
  iframe.src = viewerUrl;
  iframe.allow = "fullscreen";
  pv.appendChild(iframe);
}

function loadHtml(entry, toggleBtn) {
  const pv = $("preview");
  pv.classList.remove("md-mode");
  pv.classList.remove("pdf-mode");
  pv.classList.add("html-mode");
  clearPreviewBody(pv);

  const iframe = document.createElement("iframe");
  iframe.className = "html-viewer";
  iframe.src = previewUrlFor(entry);
  // Trusted personal notes: render like a normal webpage so MathJax and other
  // note-owned assets/scripts work as they would in a browser tab.
  iframe.referrerPolicy = "no-referrer";
  pv.appendChild(iframe);

  if (toggleBtn) {
    setPreviewToggleLabel(toggleBtn, "源码");
    toggleBtn.onclick = () => loadHtmlSource(entry, toggleBtn);
  }
}

function loadHtmlSource(entry, toggleBtn) {
  const pv = $("preview");
  pv.classList.remove("html-mode");
  clearPreviewBody(pv);
  if (toggleBtn) {
    setPreviewToggleLabel(toggleBtn, "预览");
    toggleBtn.onclick = () => loadHtml(entry, toggleBtn);
  }
  loadText(entry, false);
}

function resolveRelative(baseDir, ref) {
  // Resolve ref relative to baseDir (a path like "/a/b"), returning a path
  // rooted at HOME ("/..."). Leaves absolute or scheme-prefixed refs untouched.
  if (/^([a-z]+:|\/\/|#|mailto:|tel:)/i.test(ref)) return null;  // external/anchor
  if (ref.startsWith("/")) return ref;                            // already root-relative
  const parts = (baseDir === "/" ? [] : baseDir.split("/").filter(Boolean));
  ref.split("/").forEach(seg => {
    if (seg === "" || seg === ".") return;
    if (seg === "..") parts.pop();
    else parts.push(seg);
  });
  return "/" + parts.join("/");
}

let markdownMathJaxPromise = null;

function protectMarkdownMath(source) {
  const tokens = [];
  const stash = math => {
    const marker = `\uE000MATH${tokens.length}\uE001`;
    tokens.push(math);
    return marker;
  };
  let markdown = source
    .replace(/\$\$[\s\S]+?\$\$/g, stash)
    .replace(/\\\[[\s\S]+?\\\]/g, stash)
    .replace(/\\\([\s\S]+?\\\)/g, stash);
  markdown = markdown.replace(
    /(^|[^\\$])(\$(?!\$)(?:\\.|[^$\n])+\$)/gm,
    (match, prefix, math) => prefix + stash(math),
  );
  return { markdown, tokens };
}

function restoreMarkdownMath(container, tokens) {
  if (!tokens.length) return;
  const marker = /\uE000MATH(\d+)\uE001/g;
  const walker = document.createTreeWalker(container, NodeFilter.SHOW_TEXT);
  while (walker.nextNode()) {
    walker.currentNode.nodeValue = walker.currentNode.nodeValue.replace(
      marker,
      (match, index) => tokens[Number(index)] ?? match,
    );
  }
}

function ensureMarkdownMathJax() {
  if (window.MathJax && typeof window.MathJax.typesetPromise === "function") {
    return window.MathJax.startup && window.MathJax.startup.promise
      ? window.MathJax.startup.promise
      : Promise.resolve();
  }
  if (markdownMathJaxPromise) return markdownMathJaxPromise;

  window.MathJax = {
    tex: {
      inlineMath: [['$', '$'], ['\\(', '\\)']],
      displayMath: [['$$', '$$'], ['\\[', '\\]']],
      processEscapes: true,
      processEnvironments: true,
    },
    options: {
      skipHtmlTags: ['script', 'noscript', 'style', 'textarea', 'pre', 'code'],
    },
    startup: { typeset: false },
  };

  markdownMathJaxPromise = new Promise((resolve, reject) => {
    const script = document.createElement("script");
    script.id = "markdown-mathjax-script";
    script.src = "/mathjax/es5/tex-mml-chtml.js";
    script.async = true;
    script.onload = () => {
      const startup = window.MathJax && window.MathJax.startup;
      if (!startup || !startup.promise) {
        reject(new Error("MathJax startup failed"));
        return;
      }
      startup.promise.then(resolve, reject);
    };
    script.onerror = () => reject(new Error("MathJax load failed"));
    document.head.appendChild(script);
  }).catch(error => {
    markdownMathJaxPromise = null;
    const script = document.getElementById("markdown-mathjax-script");
    if (script) script.remove();
    throw error;
  });
  return markdownMathJaxPromise;
}

function clearMarkdownMath(container) {
  if (window.MathJax && typeof window.MathJax.typesetClear === "function") {
    window.MathJax.typesetClear([container]);
  }
}

function typesetMarkdownMath(container) {
  ensureMarkdownMathJax()
    .then(() => {
      if (!container.isConnected) return;
      return window.MathJax.typesetPromise([container]);
    })
    .catch(error => console.error("Markdown MathJax:", error));
}

function loadMarkdown(entry, toggle) {
  const pv = $("preview");
  clearPreviewBody(pv);
  pv.classList.add("md-mode");

  const dirOfFile = (() => {
    const full = fullPath(entry.name);
    const idx = full.lastIndexOf("/");
    return idx <= 0 ? "/" : full.slice(0, idx);
  })();

  const url = fileUrlFor(entry, "text");
  fetch(url, { credentials: "same-origin" })
    .then(r => {
      if (!r.ok) throw new Error("HTTP " + r.status);
      return r.json();
    })
    .then(data => {
      if (state.selected !== entry.name) return;
      if (data.binary) {
        renderPreview(entry);  // shouldn't happen for .md but fall through
        return;
      }
      const source = data.content || "";

      // Configure marked with a custom image/link renderer
      const renderer = new marked.Renderer();
      const baseImg = renderer.image.bind(renderer);
      const baseLink = renderer.link.bind(renderer);
      renderer.image = (href, title, text) => {
        const managerTarget = managerPathFromHref(href);
        const resolved = managerTarget || resolveRelative(dirOfFile, href);
        if (resolved) href = "/api/file?mode=raw&path=" + encodeURIComponent(resolved);
        return baseImg(href, title, text);
      };
      renderer.link = (href, title, text) => {
        const managerTarget = managerPathFromHref(href);
        if (managerTarget) {
          href = "/?open=" + encodeURIComponent(managerTarget);
        } else {
          // Rewrite relative links to other files in the manager so they at
          // least open as a download / raw view; external links stay as-is.
          const resolved = resolveRelative(dirOfFile, href);
          if (resolved) href = "/api/file?mode=raw&path=" + encodeURIComponent(resolved);
        }
        return baseLink(href, title, text);
      };

      marked.setOptions({
        gfm: true, breaks: false, mangle: false, headerIds: false,
        renderer: renderer,
        highlight: function(code, lang) {
          if (lang && hljs.getLanguage(lang)) {
            try { return hljs.highlight(code, { language: lang }).value; } catch(e) {}
          }
          try { return hljs.highlightAuto(code).value; } catch(e) {}
          return code;
        },
      });

      const wrap = document.createElement("div");
      wrap.className = "md-render";
      const body = document.createElement("article");
      body.className = "md-body";
      wrap.appendChild(body);

      let mode = "rendered";
      const renderBody = () => {
        clearMarkdownMath(body);
        if (mode === "rendered") {
          const protectedMath = protectMarkdownMath(source);
          try {
            body.innerHTML = marked.parse(protectedMath.markdown);
            restoreMarkdownMath(body, protectedMath.tokens);
          }
          catch (e) { body.textContent = "渲染失败：" + e.message; }
          // Apply hljs to any code blocks marked.parse missed (highlight option
          // covers most paths but be defensive).
          body.querySelectorAll("pre code").forEach(el => {
            if (!el.classList.contains("hljs")) {
              try { hljs.highlightElement(el); } catch(e) {}
            }
          });
          typesetMarkdownMath(body);
          setPreviewToggleLabel(toggle, "源码");
        } else {
          body.innerHTML = "";
          const pre = document.createElement("pre");
          pre.style.cssText = "white-space: pre-wrap; word-break: break-word; font-family: ui-monospace, monospace; font-size: 13px; line-height: 1.55; background: var(--doc-code-bg); padding: 14px; border-radius: 6px;";
          pre.textContent = source;
          body.appendChild(pre);
          setPreviewToggleLabel(toggle, "预览");
        }
      };
      toggle.onclick = () => {
        mode = (mode === "rendered") ? "source" : "rendered";
        renderBody();
      };

      if (data.truncated) {
        const banner = document.createElement("div");
        banner.className = "text-banner";
        banner.textContent = `仅显示前 ${fmtSize(TEXT_CAP)}，文件总大小 ${fmtSize(data.size)}`;
        wrap.insertBefore(banner, body);
      }

      pv.appendChild(wrap);
      renderBody();
    })
    .catch(e => {
      if (state.selected !== entry.name) return;
      const err = document.createElement("div");
      err.style.cssText = "color: var(--danger); padding: 20px;";
      err.textContent = "加载失败：" + e.message;
      pv.appendChild(err);
    });
}

function loadText(entry, force) {
  const pv = $("preview");
  clearPreviewBody(pv);

  const url = fileUrlFor(entry, "text", force ? "&force=1" : "");
  fetch(url, { credentials: "same-origin" })
    .then(r => {
      if (!r.ok) throw new Error("HTTP " + r.status);
      return r.json();
    })
    .then(data => {
      if (state.selected !== entry.name) return;
      if (data.binary) {
        const box = document.createElement("div");
        box.className = "empty-state";
        box.innerHTML = `<div class="empty-state-icon" aria-hidden="true">${iconMarkup("file")}</div>
                        <h2>这是二进制文件</h2>
                        <p>文件大小 ${fmtSize(data.size)}，可以强制作为文本打开。</p>
                        <button class="force-text text-button" id="force-text-btn">强制打开</button>`;
        pv.appendChild(box);
        document.getElementById("force-text-btn").onclick = () => loadText(entry, true);
        return;
      }
      const pre = document.createElement("pre");
      const code = document.createElement("code");
      if (data.lang) code.className = "language-" + data.lang;
      code.textContent = data.content;
      pre.appendChild(code);
      if (data.truncated) {
        const banner = document.createElement("div");
        banner.className = "text-banner";
        banner.textContent = `仅显示前 ${fmtSize(TEXT_CAP)}，文件总大小 ${fmtSize(data.size)}`;
        pv.appendChild(banner);
      }
      pv.appendChild(pre);
      try { hljs.highlightElement(code); } catch(e) {}
    })
    .catch(e => {
      if (state.selected !== entry.name) return;
      const err = document.createElement("div");
      err.style.cssText = "color: var(--danger); padding: 20px;";
      err.textContent = "加载失败：" + e.message;
      pv.appendChild(err);
    });
}

function addImageNav(pv) {
  const left = document.createElement("button");
  left.className = "nav-arrow left"; left.innerHTML = "‹";
  left.onclick = () => navImage(-1);
  if (state.imageIdx <= 0) left.disabled = true;

  const right = document.createElement("button");
  right.className = "nav-arrow right"; right.innerHTML = "›";
  right.onclick = () => navImage(1);
  if (state.imageIdx >= state.imageList.length - 1) right.disabled = true;

  pv.appendChild(left);
  pv.appendChild(right);
}

function navImage(delta) {
  const next = state.imageIdx + delta;
  if (next < 0 || next >= state.imageList.length) return;
  selectFile(state.imageList[next]);
}

function seekVideo(delta) {
  const video = $("preview").querySelector("video");
  if (!video || !Number.isFinite(video.currentTime) || !Number.isFinite(video.duration)) {
    return false;
  }
  video.currentTime = Math.min(
    Math.max(video.currentTime + delta, 0),
    video.duration,
  );
  return true;
}

function scrollToPreview() {
  if (!MOBILE_QUERY.matches) return;
  $("app").classList.add("preview-open");
}

function scrollToList() {
  $("app").classList.remove("preview-open");
  const selected = document.querySelector(".row.selected");
  if (selected) selected.focus({ preventScroll: true });
}

// Constant exposed for the truncation banner above
const TEXT_CAP = 512 * 1024;
const VIDEO_SEEK_STEP = 5;

// --- Toolbar ---
$("btn-up").onclick = () => {
  if (state.cwd === "/" || state.cwd === "") return;
  const parts = state.cwd.split("/").filter(Boolean);
  parts.pop();
  load("/" + parts.join("/"));
};
$("btn-refresh").onclick = () => load(state.cwd);
$("btn-back-list").onclick = scrollToList;
function setFavoritesExpanded(expanded) {
  const button = $("btn-favorites-toggle");
  const favorites = $("favorites");
  button.setAttribute("aria-expanded", expanded ? "true" : "false");
  favorites.hidden = !expanded;
}
$("btn-favorites-toggle").onclick = () => {
  setFavoritesExpanded($("btn-favorites-toggle").getAttribute("aria-expanded") !== "true");
};
setFavoritesExpanded(false);
document.addEventListener("click", (e) => {
  const link = e.target.closest("a[href]");
  if (!link) return;
  const target = managerPathFromHref(link.getAttribute("href"));
  if (!target) return;
  e.preventDefault();
  openPath(target, { normalized: true, fallbackHome: false });
});

function sidebarMaxWidth() {
  const reserved = 360;
  return Math.max(SIDEBAR_MIN_WIDTH, Math.min(SIDEBAR_MAX_WIDTH, window.innerWidth - reserved));
}

function clampSidebarWidth(width) {
  const value = Number(width);
  if (!Number.isFinite(value)) return SIDEBAR_DEFAULT_WIDTH;
  return Math.round(Math.min(Math.max(value, SIDEBAR_MIN_WIDTH), sidebarMaxWidth()));
}

function applySidebarWidth(width, persist) {
  sidebarWidth = clampSidebarWidth(width);
  $("app").style.setProperty("--sidebar-width", sidebarWidth + "px");
  if (persist) {
    try { localStorage.setItem(SIDEBAR_WIDTH_KEY, String(sidebarWidth)); } catch (e) {}
  }
}

function restoreSidebarWidth() {
  let saved = SIDEBAR_DEFAULT_WIDTH;
  try {
    const raw = Number(localStorage.getItem(SIDEBAR_WIDTH_KEY));
    if (Number.isFinite(raw) && raw > 0) saved = raw;
  } catch (e) {}
  applySidebarWidth(saved, false);
}

function initSidebarResize() {
  const app = $("app");
  const handle = $("sidebar-resizer");
  if (!handle) return;

  let startX = 0;
  let startWidth = sidebarWidth;

  handle.addEventListener("pointerdown", (e) => {
    if (MOBILE_QUERY.matches) return;
    e.preventDefault();
    startX = e.clientX;
    startWidth = sidebarWidth;
    app.classList.add("sidebar-resizing");
    if (handle.setPointerCapture) handle.setPointerCapture(e.pointerId);
  });

  handle.addEventListener("pointermove", (e) => {
    if (!app.classList.contains("sidebar-resizing")) return;
    e.preventDefault();
    applySidebarWidth(startWidth + e.clientX - startX, false);
  });

  const finish = (e) => {
    if (!app.classList.contains("sidebar-resizing")) return;
    app.classList.remove("sidebar-resizing");
    applySidebarWidth(sidebarWidth, true);
    if (handle.releasePointerCapture) {
      try { handle.releasePointerCapture(e.pointerId); } catch (err) {}
    }
  };
  handle.addEventListener("pointerup", finish);
  handle.addEventListener("pointercancel", finish);
}

function setSidebarCollapsed(collapsed) {
  const app = $("app");
  app.classList.toggle("sidebar-collapsed", Boolean(collapsed) && !MOBILE_QUERY.matches);
  const expanded = !app.classList.contains("sidebar-collapsed");
  $("btn-sidebar").setAttribute("aria-expanded", expanded ? "true" : "false");
  $("btn-sidebar-reopen").setAttribute("aria-expanded", expanded ? "true" : "false");
  try {
    localStorage.setItem(NAV_COLLAPSED_KEY, app.classList.contains("sidebar-collapsed") ? "1" : "0");
  } catch (e) {}
}
$("btn-sidebar").onclick = () => setSidebarCollapsed(true);
$("btn-sidebar-reopen").onclick = () => setSidebarCollapsed(false);
restoreSidebarWidth();
initSidebarResize();
setSidebarCollapsed(!MOBILE_QUERY.matches && localStorage.getItem(NAV_COLLAPSED_KEY) === "1");
window.addEventListener("resize", () => {
  if (!MOBILE_QUERY.matches) applySidebarWidth(sidebarWidth, false);
  if (!MOBILE_QUERY.matches) {
    setSidebarCollapsed(localStorage.getItem(NAV_COLLAPSED_KEY) === "1");
  } else {
    $("app").classList.remove("sidebar-collapsed");
  }
});

function syncHiddenBtn() {
  const btn = $("btn-hidden");
  const visible = state.showHidden;
  $("hidden-eye-icon").setAttribute("href", visible ? "#icon-eye" : "#icon-eye-closed");
  $("hidden-status").textContent = visible ? "可见" : "隐藏";
  btn.classList.toggle("current", state.showHidden);
  btn.setAttribute("aria-pressed", visible ? "true" : "false");
  btn.setAttribute("aria-label", visible
    ? "隐藏文件当前可见，点击停止显示"
    : "隐藏文件当前不可见，点击显示");
  btn.title = visible ? "隐藏以点号开头的文件" : "显示以点号开头的文件";
}
$("btn-hidden").onclick = () => {
  state.showHidden = !state.showHidden;
  localStorage.setItem("wfm-show-hidden", state.showHidden ? "1" : "0");
  syncHiddenBtn();
  renderListing();
};
syncHiddenBtn();

function syncSortBtn() {
  $("sort-label").textContent = SORT_LABEL[state.sortBy] || SORT_LABEL.name;
}
$("btn-sort").onclick = () => {
  const idx = SORT_MODES.indexOf(state.sortBy);
  state.sortBy = SORT_MODES[(idx + 1) % SORT_MODES.length];
  localStorage.setItem("wfm-sort", state.sortBy);
  syncSortBtn();
  renderListing();
};
syncSortBtn();

// --- Keyboard nav ---
window.addEventListener("keydown", (e) => {
  if (e.target.tagName === "INPUT" || e.target.tagName === "TEXTAREA") return;
  if (e.key === "b" && !e.ctrlKey && !e.metaKey && !e.altKey && !MOBILE_QUERY.matches) {
    setSidebarCollapsed(!$("app").classList.contains("sidebar-collapsed"));
    e.preventDefault();
    return;
  }
  if (e.key === "Escape") {
    if ($("app").classList.contains("preview-open")) scrollToList();
  }
  const sel = state.selected ? entryByName(state.selected) : null;
  if (sel && sel.kind === "image") {
    if (e.key === "ArrowLeft") { navImage(-1); e.preventDefault(); }
    else if (e.key === "ArrowRight") { navImage(1); e.preventDefault(); }
  } else if (sel && sel.kind === "video") {
    if (e.key === "ArrowLeft") { seekVideo(-VIDEO_SEEK_STEP); e.preventDefault(); }
    else if (e.key === "ArrowRight") { seekVideo(VIDEO_SEEK_STEP); e.preventDefault(); }
  }
});

// --- Upload ---
function uploadFiles(files) {
  if (!files || !files.length) return;
  const uploadsDiv = $("uploads");
  Array.from(files).forEach(file => {
    const item = document.createElement("div");
    item.className = "upload-item";
    item.innerHTML = `<div>${file.name} · ${fmtSize(file.size)}</div>
                      <div class="bar"><div></div></div>`;
    uploadsDiv.appendChild(item);
    const bar = item.querySelector(".bar > div");

    const xhr = new XMLHttpRequest();
    xhr.open("POST", "/api/upload?path=" + encodeURIComponent(state.cwd));
    xhr.upload.onprogress = (ev) => {
      if (ev.lengthComputable) {
        bar.style.width = (ev.loaded / ev.total * 100) + "%";
      }
    };
    xhr.onload = () => {
      if (xhr.status >= 200 && xhr.status < 300) {
        bar.style.width = "100%";
        item.classList.add("done");
        setTimeout(() => item.remove(), 2500);
        load(state.cwd);
      } else {
        item.classList.add("err");
        item.querySelector("div").textContent += "  ⚠ " + xhr.status;
      }
    };
    xhr.onerror = () => {
      item.classList.add("err");
      item.querySelector("div").textContent += "  ⚠ network";
    };
    const form = new FormData();
    form.append("file", file);
    xhr.send(form);
  });
}

$("upload-input").addEventListener("change", (e) => {
  uploadFiles(e.target.files);
  e.target.value = "";
});

const listing = $("listing");
listing.addEventListener("dragover", (e) => {
  e.preventDefault();
  listing.classList.add("dragover");
});
listing.addEventListener("dragleave", (e) => {
  if (e.target === listing) listing.classList.remove("dragover");
});
listing.addEventListener("drop", (e) => {
  e.preventDefault();
  listing.classList.remove("dragover");
  if (e.dataTransfer.files) uploadFiles(e.dataTransfer.files);
});
// Suppress browser default drag-drop on the whole window (would otherwise navigate away)
window.addEventListener("dragover", (e) => e.preventDefault());
window.addEventListener("drop", (e) => e.preventDefault());

// --- Auto-refresh listing when returning to the tab ---
let _hiddenAt = 0;
document.addEventListener("visibilitychange", () => {
  if (document.visibilityState === "hidden") {
    _hiddenAt = Date.now();
  } else if (document.visibilityState === "visible") {
    if (_hiddenAt && Date.now() - _hiddenAt > 3000) {
      load(state.cwd, { preservePreview: true });
    }
    _hiddenAt = 0;
  }
});

// --- Boot ---
refreshFavorites();
openInitialPath();
</script>
</body>
</html>"""


def main():
    if not AUTH or ":" not in AUTH:
        print("ERROR: WEBFM_AUTH must be set as 'user:pass'", file=sys.stderr)
        print("Example: WEBFM_AUTH='user:strong-password' python web_file_manager.py", file=sys.stderr)
        sys.exit(2)

    app = tornado.web.Application([
        (r"/", IndexHandler),
        (r"/api/list", ListHandler),
        (r"/api/favorites", FavoritesHandler),
        (r"/api/file", FileHandler),
        (r"/api/upload", UploadHandler),
        (r"/preview/(.*)", PreviewHandler),
        (r"/pdfjs/(.*)", AuthStaticHandler, {"path": str(PDFJS_DIR)}),
        (r"/mathjax/(.*)", AuthStaticHandler, {"path": str(MATHJAX_DIR)}),
        (r"/(.*)", IndexHandler),
    ])
    app.listen(PORT, address="0.0.0.0", max_body_size=MAX_UPLOAD_BYTES)
    print(f"Web file manager running at http://0.0.0.0:{PORT}")
    print(f"Root: {HOME}")
    print(f"Auth: enabled (user={AUTH.split(':', 1)[0]})")
    tornado.ioloop.IOLoop.current().start()


if __name__ == "__main__":
    main()
