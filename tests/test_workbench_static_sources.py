"""Fragment composition and validators for the shared browser document."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from asl_workbench.http_auth import HttpRefusal
from asl_workbench.http_messages import Request
from asl_workbench.http_static import StaticBundle
from asl_workbench.static_sources import read_static_source

ASL_TEST_PORTABLE = True


class StaticSourceTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        (self.root / "parts").mkdir()
        self.bundle = StaticBundle(self.root)

    def request(self, etag: str = "") -> Request:
        return Request.build(
            "GET",
            "/static/styles.css",
            headers={"If-None-Match": etag},
        )

    def test_nested_fragments_preserve_order_and_bytes(self) -> None:
        (self.root / "index.html").write_bytes(
            b"<main>\n<!-- include: parts/panel.html -->\n</main>\n"
        )
        (self.root / "parts/panel.html").write_bytes(
            b"<section>\n<!-- include: body.html -->\n</section>\n"
        )
        (self.root / "parts/body.html").write_bytes(b"caf\xc3\xa9\n")
        self.assertEqual(
            read_static_source(self.root / "index.html"),
            b"<main>\n<section>\ncaf\xc3\xa9\n</section>\n</main>\n",
        )

    def test_fragment_change_invalidates_the_parent_validator(self) -> None:
        (self.root / "styles.css").write_text("/* include: parts/panel.css */\n")
        fragment = self.root / "parts/panel.css"
        fragment.write_text("main { color: red; }\n")
        first = self.bundle.asset(self.request(), "styles.css")
        etag = first.headers["ETag"]
        self.assertEqual(first.body, fragment.read_bytes())
        self.assertEqual(
            self.bundle.asset(self.request(etag), "styles.css").status, 304
        )
        fragment.write_text("main { color: tan; }\n")
        changed = self.bundle.asset(self.request(etag), "styles.css")
        self.assertEqual(changed.status, 200)
        self.assertNotEqual(changed.headers["ETag"], etag)
        self.assertEqual(changed.body, fragment.read_bytes())
        fragment.unlink()
        with self.assertRaises(HttpRefusal) as raised:
            self.bundle.asset(self.request(changed.headers["ETag"]), "styles.css")
        self.assertEqual(raised.exception.status, 404)

    def test_cycles_and_escaping_includes_are_refused(self) -> None:
        source = self.root / "styles.css"
        for include in ("styles.css", "../outside.css", "/outside.css"):
            with self.subTest(include=include):
                source.write_text(f"/* include: {include} */\n")
                with self.assertRaises(OSError):
                    read_static_source(source)

    def test_a_symlink_cannot_include_a_source_outside_the_bundle(self) -> None:
        with tempfile.TemporaryDirectory() as outside:
            target = Path(outside) / "private.css"
            target.write_text("private")
            (self.root / "parts/panel.css").symlink_to(target)
            source = self.root / "styles.css"
            source.write_text("/* include: parts/panel.css */\n")
            with self.assertRaises(OSError):
                read_static_source(source)

    def test_script_comments_are_not_includes(self) -> None:
        source = self.root / "app.js"
        source.write_bytes(b"/* include: missing.js */\n")
        self.assertEqual(read_static_source(source), source.read_bytes())
