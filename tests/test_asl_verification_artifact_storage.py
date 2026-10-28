"""Verification artifact storage tests."""

from __future__ import annotations

import errno
import hashlib
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from agent_session_loop import (
    artifacts,
)


class ArtifactStorageTests(unittest.TestCase):
    def test_cross_device_spool_rejects_staging_link_without_touching_target(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            spool = directory / "spool"
            victim = directory / "unrelated"
            spool.write_bytes(b"spooled")
            victim.write_bytes(b"unrelated")
            staging = artifacts.durability.staging_path(directory / "output.gz")
            staging.symlink_to(victim)
            cross_device = OSError(errno.EXDEV, "cross-device link")

            with (
                mock.patch.object(artifacts.os, "replace", side_effect=cross_device),
                mock.patch.object(artifacts.os, "rename", side_effect=cross_device),
                self.assertRaises(artifacts.ArtifactError),
            ):
                artifacts._promote_spool(
                    spool, artifact_dir=directory, artifact_name="output"
                )

            self.assertEqual(b"unrelated", victim.read_bytes())
            self.assertEqual(b"spooled", spool.read_bytes())
            self.assertFalse(staging.exists())

    def test_sha256_and_counts_are_exact(self) -> None:
        data = b"hello world"
        meta = artifacts.store_stream_output(data, failed=False)

        self.assertEqual(artifacts.OUTPUT_METADATA_VERSION, meta.version)
        self.assertEqual(len(data), meta.byte_count)
        self.assertEqual(len("hello world"), meta.char_count)
        self.assertEqual(hashlib.sha256(data).hexdigest(), meta.sha256)
        self.assertFalse(meta.truncated)
        self.assertEqual("hello world", meta.preview)
        self.assertIsNone(meta.artifact_path)

    def test_empty_stream_produces_bounded_metadata(self) -> None:
        meta = artifacts.store_stream_output(b"", failed=False)

        self.assertEqual(0, meta.byte_count)
        self.assertEqual(0, meta.char_count)
        self.assertFalse(meta.truncated)
        self.assertEqual("", meta.preview)
        self.assertEqual(hashlib.sha256(b"").hexdigest(), meta.sha256)

    def test_synthetic_14mb_stream_produces_bounded_metadata(self) -> None:
        data = b"A" * (14 * 1024 * 1024)
        meta = artifacts.store_stream_output(data, failed=False)

        self.assertTrue(meta.truncated)
        self.assertEqual(len(data), meta.byte_count)
        self.assertEqual(len(data), meta.char_count)
        # Preview is bounded to head + tail (plus the truncation marker),
        # never the full 14 MB stream.
        self.assertLessEqual(
            len(meta.preview),
            artifacts.PREVIEW_HEAD_BYTES
            + artifacts.PREVIEW_TAIL_BYTES
            + len(artifacts.PREVIEW_TRUNCATION_MARKER),
        )
        self.assertIn(artifacts.PREVIEW_TRUNCATION_MARKER, meta.preview)
        self.assertTrue(meta.preview.startswith("A" * 32))
        self.assertTrue(meta.preview.endswith("A" * 32))

    def test_preview_uses_first_and_last_8kib(self) -> None:
        head = b"H" * artifacts.PREVIEW_HEAD_BYTES
        middle = b"M" * 4096
        tail = b"T" * artifacts.PREVIEW_TAIL_BYTES
        preview, truncated = artifacts.bounded_preview(head + middle + tail)

        self.assertTrue(truncated)
        self.assertNotIn("M", preview)
        self.assertEqual(
            "H" * artifacts.PREVIEW_HEAD_BYTES,
            preview[: artifacts.PREVIEW_HEAD_BYTES],
        )
        self.assertEqual(
            "T" * artifacts.PREVIEW_TAIL_BYTES,
            preview[-artifacts.PREVIEW_TAIL_BYTES :],
        )

    def test_preview_not_truncated_at_exact_boundary(self) -> None:
        data = b"X" * (artifacts.PREVIEW_HEAD_BYTES + artifacts.PREVIEW_TAIL_BYTES)
        preview, truncated = artifacts.bounded_preview(data)

        self.assertFalse(truncated)
        self.assertEqual(data.decode("utf-8"), preview)

    def test_invalid_utf8_is_handled_in_preview_and_counts(self) -> None:
        data = b"ok\xff\xfebytes"
        meta = artifacts.store_stream_output(data, failed=False)

        self.assertEqual(len(data), meta.byte_count)
        # Invalid bytes decode to U+FFFD replacement characters, so the
        # character count reflects the replaced string, not the raw bytes.
        self.assertEqual(len(data.decode("utf-8", errors="replace")), meta.char_count)
        self.assertIn("�", meta.preview)
        self.assertEqual(hashlib.sha256(data).hexdigest(), meta.sha256)

    def test_failed_output_is_recoverable_byte_for_byte(self) -> None:
        data = bytes(range(256)) * 2048  # 512 KiB of every byte value
        with tempfile.TemporaryDirectory() as tmp:
            meta = artifacts.store_stream_output(
                data,
                failed=True,
                artifact_dir=tmp,
                artifact_name="stdout",
            )

            self.assertIsNotNone(meta.artifact_path)
            artifact = Path(meta.artifact_path)
            self.assertTrue(artifact.exists())
            self.assertEqual("stdout.gz", artifact.name)
            recovered = artifacts.read_gzip(artifact)
            self.assertEqual(data, recovered)
            self.assertEqual(meta.sha256, artifacts.sha256_hex(recovered))

    def test_passing_output_creates_no_full_raw_artifact(self) -> None:
        data = b"C" * (1024 * 1024)
        with tempfile.TemporaryDirectory() as tmp:
            meta = artifacts.store_stream_output(
                data,
                failed=False,
                artifact_dir=tmp,
                artifact_name="stdout",
            )

            self.assertIsNone(meta.artifact_path)
            self.assertEqual([], list(Path(tmp).iterdir()))

    def test_empty_failed_output_still_recoverable(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            meta = artifacts.store_stream_output(
                b"", failed=True, artifact_dir=tmp, artifact_name="stderr"
            )

            self.assertIsNotNone(meta.artifact_path)
            self.assertEqual(b"", artifacts.read_gzip(meta.artifact_path))

    def test_artifact_name_is_sanitized(self) -> None:
        cases = {
            "../../etc/passwd": "etc-passwd",
            "weird name!@#.log": "weird-name-.log",
            "  ": "output",
            "": "output",
            "...": "output",
            "already_safe.txt": "already_safe.txt",
        }
        for raw, expected in cases.items():
            with self.subTest(raw=raw):
                self.assertEqual(expected, artifacts.sanitize_artifact_name(raw))

    def test_derive_artifact_stem_keeps_safe_names_unchanged(self) -> None:
        for name in ("stdout", "boom-stdout", "already_safe.txt"):
            with self.subTest(name=name):
                self.assertEqual(name, artifacts.derive_artifact_stem(name))
        self.assertEqual("output", artifacts.derive_artifact_stem(None))
        self.assertEqual("output", artifacts.derive_artifact_stem(""))

    def test_derive_artifact_stem_distinguishes_colliding_names(self) -> None:
        first = artifacts.derive_artifact_stem("a/b-stdout")
        second = artifacts.derive_artifact_stem("a?b-stdout")

        self.assertNotEqual(first, second)
        # Both keep the human-readable sanitized stem as a prefix and stay
        # single-path-segment safe.
        self.assertTrue(first.startswith("a-b-stdout-"))
        self.assertTrue(second.startswith("a-b-stdout-"))
        self.assertEqual(first, artifacts.sanitize_artifact_name(first))
        self.assertEqual(second, artifacts.sanitize_artifact_name(second))

    def test_derive_artifact_stem_distinguishes_digest_prefix_collisions(
        self,
    ) -> None:
        # These names sanitize to the same stem AND share the first 8 hex
        # chars of their SHA-256 digests ("cc47903e"), so an 8-char digest
        # suffix still collided; the full digest must keep them apart.
        first = artifacts.derive_artifact_stem("a$%#:??/b-stdout")
        second = artifacts.derive_artifact_stem("a%%#%$:/b-stdout")

        self.assertNotEqual(first, second)
        self.assertTrue(first.startswith("a-b-stdout-"))
        self.assertTrue(second.startswith("a-b-stdout-"))
        self.assertEqual(first, artifacts.sanitize_artifact_name(first))
        self.assertEqual(second, artifacts.sanitize_artifact_name(second))

    def test_derive_artifact_stem_is_idempotent_and_deterministic(self) -> None:
        derived = artifacts.derive_artifact_stem("../../etc/passwd")
        self.assertEqual(derived, artifacts.derive_artifact_stem(derived))
        self.assertEqual(derived, artifacts.derive_artifact_stem("../../etc/passwd"))

    def test_derive_artifact_stem_byte_caps_overlong_names(self) -> None:
        # A ~192-char sanitized name plus the full 64-hex digest used to
        # yield a 260-byte .gz filename (267 with the live -stdout suffix),
        # past the 255-byte per-component limit once the atomic-write
        # ``.{name}.tmp`` decoration is added.
        first = "a" * 190 + "/x-stdout"
        second = "b" + "a" * 189 + "/x-stdout"
        stems = [
            artifacts.derive_artifact_stem(first),
            artifacts.derive_artifact_stem(second),
        ]

        self.assertNotEqual(stems[0], stems[1])
        for stem in stems:
            with self.subTest(stem=stem):
                # The longest generated component is the temp form of the
                # gzip artifact; it must fit the 255-byte limit.
                tmp_component = f".{stem}.gz.tmp"
                self.assertLessEqual(len(tmp_component.encode("utf-8")), 255)
                self.assertEqual(stem, artifacts.sanitize_artifact_name(stem))
                self.assertEqual(stem, artifacts.derive_artifact_stem(stem))
        # The readable prefix survives truncation.
        self.assertTrue(stems[0].startswith("a" * 32))

    def test_derive_artifact_stem_caps_safe_but_overlong_names(self) -> None:
        # Names that sanitize losslessly can still overflow the component
        # limit on their own; they get the same truncate-plus-digest
        # treatment, and distinct names stay distinct.
        first = artifacts.derive_artifact_stem("a" * 250)
        second = artifacts.derive_artifact_stem("a" * 251)

        self.assertNotEqual(first, second)
        for stem in (first, second):
            with self.subTest(stem=stem):
                self.assertLessEqual(len(f".{stem}.gz.tmp".encode()), 255)
                self.assertEqual(stem, artifacts.derive_artifact_stem(stem))

    def test_truncate_utf8_never_splits_multibyte_characters(self) -> None:
        # Sanitized stems are ASCII today, but the byte cap must stay safe if
        # sanitization ever preserves multibyte characters: a cut that lands
        # mid-sequence drops the partial character instead of splitting it.
        text = "é" * 100  # two bytes per character
        for max_bytes in (0, 1, 2, 3, 199, 200, 201):
            with self.subTest(max_bytes=max_bytes):
                truncated = artifacts._truncate_utf8(text, max_bytes)
                self.assertLessEqual(len(truncated.encode("utf-8")), max_bytes)
                self.assertTrue(text.startswith(truncated))

    def test_overlong_check_name_yields_recoverable_live_artifact(self) -> None:
        # Live verification names failure artifacts ``{check.name}-stdout``;
        # a ~192-char check name must still produce a bounded, recoverable
        # artifact instead of losing the spool at promotion time.
        data = b"failure output for an overlong check name"
        names = ["a" * 190 + "/x-stdout", "b" + "a" * 189 + "/x-stdout"]
        with tempfile.TemporaryDirectory() as tmp:
            paths = []
            for name in names:
                capture = artifacts.StreamCapture(spool=True)
                capture.feed(data)
                meta = capture.finalize(
                    failed=True, artifact_dir=tmp, artifact_name=name
                )
                with self.subTest(name=name):
                    self.assertIsNotNone(meta.artifact_path)
                    artifact = Path(meta.artifact_path)
                    self.assertLessEqual(len(artifact.name.encode("utf-8")), 255)
                    self.assertLessEqual(len(f".{artifact.name}.tmp".encode()), 255)
                    self.assertEqual(data, artifacts.read_gzip(artifact))
                paths.append(meta.artifact_path)
            self.assertNotEqual(paths[0], paths[1])

    def test_colliding_artifact_names_do_not_overwrite(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            first = artifacts.store_stream_output(
                b"first", failed=True, artifact_dir=tmp, artifact_name="a/b"
            )
            second = artifacts.store_stream_output(
                b"second", failed=True, artifact_dir=tmp, artifact_name="a?b"
            )

            self.assertNotEqual(first.artifact_path, second.artifact_path)
            self.assertEqual(b"first", artifacts.read_gzip(first.artifact_path))
            self.assertEqual(b"second", artifacts.read_gzip(second.artifact_path))

    def test_sanitized_name_has_no_path_separators(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            meta = artifacts.store_stream_output(
                b"data",
                failed=True,
                artifact_dir=tmp,
                artifact_name="../escape/attempt",
            )

            artifact = Path(meta.artifact_path)
            self.assertEqual(Path(tmp), artifact.parent)
            self.assertNotIn("/", artifact.name.replace(".gz", ""))

    def test_interrupted_write_cleans_up_and_leaves_destination_untouched(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "output.gz"
            target.write_bytes(b"original")

            with (
                mock.patch(
                    "agent_session_loop.durability.os.replace",
                    side_effect=OSError("boom"),
                ),
                self.assertRaises(artifacts.ArtifactError),
            ):
                artifacts.atomic_write_bytes(target, b"new-data")

            # Destination is unchanged and no stray temp file remains.
            self.assertEqual(b"original", target.read_bytes())
            self.assertEqual(["output.gz"], [p.name for p in Path(tmp).iterdir()])

    def test_gzip_write_is_atomic_and_deterministic(self) -> None:
        data = b"deterministic payload" * 100
        with tempfile.TemporaryDirectory() as tmp:
            first = Path(tmp) / "a.gz"
            second = Path(tmp) / "b.gz"
            artifacts.atomic_write_gzip(first, data)
            artifacts.atomic_write_gzip(second, data)

            self.assertEqual(first.read_bytes(), second.read_bytes())
            self.assertEqual(data, artifacts.read_gzip(first))

    def test_read_gzip_rejects_non_gzip_data(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            bogus = Path(tmp) / "bogus.gz"
            bogus.write_bytes(b"not gzip at all")
            with self.assertRaises(artifacts.ArtifactError):
                artifacts.read_gzip(bogus)

    def test_metadata_roundtrips_through_dict(self) -> None:
        meta = artifacts.store_stream_output(b"payload", failed=False)
        restored = artifacts.OutputMetadata.from_dict(meta.to_dict())
        self.assertEqual(meta, restored)

    def test_metadata_from_dict_rejects_unknown_version(self) -> None:
        payload = artifacts.store_stream_output(b"x", failed=False).to_dict()
        payload["version"] = 999
        with self.assertRaises(artifacts.ArtifactError):
            artifacts.OutputMetadata.from_dict(payload)

    def test_stream_capture_matches_store_stream_output_across_chunkings(
        self,
    ) -> None:
        # Multi-byte characters ensure chunk boundaries split UTF-8 sequences;
        # the incremental capture must still produce metadata identical to the
        # whole-buffer reference for every chunking.
        data = ("héllo wörld ✓" * 3000).encode("utf-8")
        reference = artifacts.store_stream_output(data, failed=False)
        for chunk_size in (1, 7, 1024, len(data)):
            with self.subTest(chunk_size=chunk_size):
                capture = artifacts.StreamCapture()
                for offset in range(0, len(data), chunk_size):
                    capture.feed(data[offset : offset + chunk_size])
                meta = capture.finalize(failed=False)
                self.assertEqual(reference, meta)

    def test_stream_capture_matches_reference_below_preview_bound(self) -> None:
        data = b"short output\n"
        reference = artifacts.store_stream_output(data, failed=False)
        capture = artifacts.StreamCapture()
        capture.feed(data)
        self.assertEqual(reference, capture.finalize(failed=False))

    def test_stream_capture_empty_stream_matches_reference(self) -> None:
        reference = artifacts.store_stream_output(b"", failed=False)
        capture = artifacts.StreamCapture()
        self.assertEqual(reference, capture.finalize(failed=False))

    def test_stream_capture_spool_retains_failed_stream_byte_for_byte(
        self,
    ) -> None:
        data = bytes(range(256)) * 512  # 128 KiB of every byte value
        with tempfile.TemporaryDirectory() as tmp:
            capture = artifacts.StreamCapture(spool=True)
            for offset in range(0, len(data), 4096):
                capture.feed(data[offset : offset + 4096])
            meta = capture.finalize(
                failed=True, artifact_dir=tmp, artifact_name="stdout"
            )

            self.assertIsNotNone(meta.artifact_path)
            artifact = Path(meta.artifact_path)
            self.assertEqual("stdout.gz", artifact.name)
            self.assertEqual(data, artifacts.read_gzip(artifact))
            self.assertEqual(artifacts.sha256_hex(data), meta.sha256)
            # The spooled artifact is byte-identical to the buffered writer's
            # output, so compressed-artifact digests stay stable across runs.
            reference = Path(tmp) / "reference.gz"
            artifacts.atomic_write_gzip(reference, data)
            self.assertEqual(reference.read_bytes(), artifact.read_bytes())

    def test_stream_capture_spool_deleted_when_not_failed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            capture = artifacts.StreamCapture(spool=True)
            capture.feed(b"passing output")
            spool_path = capture._spool_path
            self.assertIsNotNone(spool_path)

            meta = capture.finalize(
                failed=False, artifact_dir=tmp, artifact_name="stdout"
            )

            self.assertIsNone(meta.artifact_path)
            self.assertEqual([], list(Path(tmp).iterdir()))
            self.assertFalse(spool_path.exists())

    def test_stream_capture_promotion_failure_fails_open(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            blocker = Path(tmp) / "blocker"
            blocker.write_text("not a dir\n", encoding="utf-8")
            capture = artifacts.StreamCapture(spool=True)
            capture.feed(b"failure output")
            spool_path = capture._spool_path

            meta = capture.finalize(
                failed=True,
                artifact_dir=blocker / "sub",
                artifact_name="stdout",
            )

            self.assertIsNone(meta.artifact_path)
            self.assertEqual("failure output", meta.preview)
            self.assertEqual(artifacts.sha256_hex(b"failure output"), meta.sha256)
            self.assertFalse(spool_path.exists())

    def test_stream_capture_discard_removes_spool(self) -> None:
        capture = artifacts.StreamCapture(spool=True)
        capture.feed(b"aborted")
        spool_path = capture._spool_path
        self.assertIsNotNone(spool_path)

        capture.discard()

        self.assertFalse(spool_path.exists())
