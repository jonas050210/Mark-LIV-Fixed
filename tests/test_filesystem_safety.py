from __future__ import annotations

import errno
import os
import threading
import unittest
import zipfile
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from actions import file_controller, file_processor
from actions.file_handlers.data import _process_xml
from core import path_policy, undo
from core.path_policy import PathPolicyError, resolve_user_path


class PathPolicyTests(unittest.TestCase):
    def test_traversal_and_protected_credentials_are_denied(self) -> None:
        with self.assertRaises(PathPolicyError):
            resolve_user_path("../../etc/passwd", allow_missing=True)
        project = Path(__file__).resolve().parents[1]
        protected = project / "config" / "api_keys.json"
        with self.assertRaises(PathPolicyError):
            resolve_user_path(protected, allow_missing=True)
        with self.assertRaises(PathPolicyError):
            resolve_user_path(project / "memory" / "sessions.json", allow_missing=True)
        with self.assertRaises(PathPolicyError):
            resolve_user_path(
                Path.home(), allow_missing=False, protect_ancestors=True
            )

    def test_symlink_parent_is_rejected_for_mutation(self) -> None:
        with TemporaryDirectory(dir=Path.home()) as directory, TemporaryDirectory() as outside:
            root = Path(directory)
            link = root / "escape"
            try:
                link.symlink_to(Path(outside), target_is_directory=True)
            except (OSError, NotImplementedError):
                self.skipTest("symbolic links are unavailable")
            with self.assertRaises(PathPolicyError):
                resolve_user_path(link / "file.txt", reject_symlinks=True)

    def test_atomic_write_works_when_fchmod_is_unavailable(self) -> None:
        with TemporaryDirectory(dir=Path.home()) as directory:
            target = Path(directory) / "note.txt"
            target.write_text("original", encoding="utf-8")
            with patch.object(path_policy.os, "fchmod", None, create=True):
                path_policy.atomic_write_text(target, "replacement")
            self.assertEqual(target.read_text(encoding="utf-8"), "replacement")
            self.assertEqual(list(target.parent.glob(".*.tmp")), [])


class FileControllerSafetyTests(unittest.TestCase):
    def tearDown(self) -> None:
        undo.clear()

    def test_create_and_write_do_not_silently_overwrite(self) -> None:
        with TemporaryDirectory(dir=Path.home()) as directory:
            root = Path(directory)
            target = root / "note.txt"
            target.write_text("original", encoding="utf-8")

            result = file_controller.create_file(str(root), "note.txt", "replacement")
            self.assertIn("already exists", result)
            self.assertEqual(target.read_text(encoding="utf-8"), "original")

            result = file_controller.write_file(str(root), "note.txt", "replacement")
            self.assertIn("explicitly request", result)
            self.assertEqual(target.read_text(encoding="utf-8"), "original")

    def test_undo_refuses_to_delete_a_user_modified_file_and_is_retained(self) -> None:
        with TemporaryDirectory(dir=Path.home()) as directory:
            root = Path(directory)
            target = root / "created.txt"
            self.assertIn("File created", file_controller.create_file(str(root), target.name, "assistant"))
            target.write_text("user changed this", encoding="utf-8")

            result = undo.undo_last()
            self.assertIn("Undo refused", result)
            self.assertTrue(target.exists())
            self.assertEqual(target.read_text(encoding="utf-8"), "user changed this")
            self.assertTrue(undo.can_undo())

    def test_move_refuses_destination_conflict(self) -> None:
        with TemporaryDirectory(dir=Path.home()) as directory:
            root = Path(directory)
            source = root / "source.txt"
            destination = root / "destination.txt"
            source.write_text("source", encoding="utf-8")
            destination.write_text("destination", encoding="utf-8")

            result = file_controller.move_file(str(source), destination=str(destination))
            self.assertIn("Destination already exists", result)
            self.assertEqual(source.read_text(encoding="utf-8"), "source")
            self.assertEqual(destination.read_text(encoding="utf-8"), "destination")

    def test_recursive_mutation_refuses_a_protected_descendant(self) -> None:
        with TemporaryDirectory(dir=Path.home()) as directory:
            root = Path(directory) / "tree"
            protected = root / "nested" / "private"
            protected.mkdir(parents=True)
            (protected / "secret.txt").write_text("secret", encoding="utf-8")
            with patch(
                "core.path_policy.protected_roots",
                return_value=(protected.resolve(),),
            ), patch.object(file_controller, "_safe_trash") as trash:
                result = file_controller.delete_file(str(root))
            self.assertIn("Access denied", result)
            self.assertTrue(protected.exists())
            trash.assert_not_called()

    def test_failed_directory_copy_does_not_leave_a_partial_destination(self) -> None:
        with TemporaryDirectory(dir=Path.home()) as directory:
            root = Path(directory)
            source = root / "source"
            destination = root / "destination"
            source.mkdir()
            (source / "one.txt").write_text("one", encoding="utf-8")

            def fail_after_creating_staging(_source, staging, **_kwargs):
                staging_path = Path(staging)
                staging_path.mkdir()
                (staging_path / "partial.txt").write_text("partial", encoding="utf-8")
                raise OSError("simulated copy failure")

            with patch.object(file_controller.shutil, "copytree", side_effect=fail_after_creating_staging):
                result = file_controller.copy_file(
                    str(source), destination=str(destination)
                )

            self.assertIn("could not copy", result.casefold())
            self.assertIn("simulated copy failure", result)
            self.assertFalse(destination.exists())
            self.assertEqual(list(root.glob(".*.copying-*")), [])


class CopyProgressTests(unittest.TestCase):
    def test_copy_file_reports_progress_up_to_completion(self) -> None:
        with TemporaryDirectory(dir=Path.home()) as directory:
            root = Path(directory)
            source = root / "source.bin"
            source.write_bytes(b"x" * (2 * 1024 * 1024))
            destination = root / "copy.bin"
            progress_calls = []

            result = file_controller.copy_file(
                str(source), destination=str(destination),
                report_progress=lambda p, m="": progress_calls.append((p, m)),
            )

            self.assertIn("Copied", result)
            self.assertTrue(destination.exists())
            self.assertEqual(destination.read_bytes(), source.read_bytes())
            self.assertTrue(progress_calls)
            self.assertEqual(progress_calls[-1][0], 100)
            self.assertTrue(all(0 <= p <= 100 for p, _ in progress_calls))

    def test_copy_directory_reports_progress_up_to_completion(self) -> None:
        with TemporaryDirectory(dir=Path.home()) as directory:
            root = Path(directory)
            source = root / "tree"
            source.mkdir()
            (source / "a.bin").write_bytes(b"a" * (1024 * 1024))
            (source / "b.bin").write_bytes(b"b" * (1024 * 1024))
            destination = root / "tree_copy"
            progress_calls = []

            result = file_controller.copy_file(
                str(source), destination=str(destination),
                report_progress=lambda p, m="": progress_calls.append((p, m)),
            )

            self.assertIn("Copied", result)
            self.assertTrue((destination / "a.bin").exists())
            self.assertTrue((destination / "b.bin").exists())
            self.assertTrue(progress_calls)
            self.assertEqual(progress_calls[-1][0], 100)

    def test_copy_file_cancellation_leaves_no_partial_destination(self) -> None:
        with TemporaryDirectory(dir=Path.home()) as directory:
            root = Path(directory)
            source = root / "source.bin"
            source.write_bytes(b"x" * (2 * 1024 * 1024))
            destination = root / "copy.bin"
            cancel_event = threading.Event()
            cancel_event.set()

            result = file_controller.copy_file(
                str(source), destination=str(destination), cancel_event=cancel_event,
            )

            self.assertIn("could not copy", result.casefold())
            self.assertFalse(destination.exists())


class CrossDeviceMoveTests(unittest.TestCase):
    def test_move_falls_back_to_a_verified_copy_across_devices(self) -> None:
        with TemporaryDirectory(dir=Path.home()) as directory:
            root = Path(directory)
            source = root / "source.txt"
            destination = root / "dest" / "source.txt"
            destination.parent.mkdir()
            source.write_text("payload", encoding="utf-8")
            progress_calls = []

            def cross_device(_src, _dst):
                raise OSError(errno.EXDEV, "Invalid cross-device link")

            with patch.object(file_controller, "move_no_replace", side_effect=cross_device), \
                 patch.object(
                     file_controller, "_safe_trash",
                     return_value=f"Moved to Trash: {source.name}",
                 ) as trash:
                result = file_controller.move_file(
                    str(source), destination=str(destination),
                    report_progress=lambda p, m="": progress_calls.append((p, m)),
                )

            self.assertIn("different drive", result)
            self.assertTrue(destination.exists())
            self.assertEqual(destination.read_text(encoding="utf-8"), "payload")
            trash.assert_called_once()
            self.assertTrue(progress_calls)
            self.assertEqual(progress_calls[-1][0], 100)

    def test_move_across_devices_rolls_back_if_the_original_cannot_be_trashed(self) -> None:
        with TemporaryDirectory(dir=Path.home()) as directory:
            root = Path(directory)
            source = root / "source.txt"
            destination = root / "dest" / "source.txt"
            destination.parent.mkdir()
            source.write_text("payload", encoding="utf-8")

            def cross_device(_src, _dst):
                raise OSError(errno.EXDEV, "Invalid cross-device link")

            # Whether send2trash happens to be installed on the machine
            # running this suite is not something a test should depend on --
            # a passing test must not start failing just because someone
            # `pip install`s an unrelated package. `_safe_trash` is forced to
            # report unavailability directly, which exercises the same
            # rollback branch in `_move_across_devices` deterministically.
            unavailable = (
                "send2trash is not installed. "
                "Run: pip install send2trash — "
                "Permanent deletion is disabled for safety."
            )
            with patch.object(file_controller, "move_no_replace", side_effect=cross_device), \
                 patch.object(file_controller, "_safe_trash", return_value=unavailable):
                result = file_controller.move_file(str(source), destination=str(destination))

            self.assertIn("could not move it across drives", result)
            self.assertTrue(source.exists())
            self.assertEqual(source.read_text(encoding="utf-8"), "payload")
            self.assertFalse(destination.exists())

    def test_move_across_devices_refuses_an_unsafe_directory(self) -> None:
        with TemporaryDirectory(dir=Path.home()) as directory:
            root = Path(directory)
            source = root / "tree"
            protected = source / "private"
            protected.mkdir(parents=True)
            (protected / "secret.txt").write_text("secret", encoding="utf-8")
            destination = root / "tree_copy"

            def cross_device(_src, _dst):
                raise OSError(errno.EXDEV, "Invalid cross-device link")

            with patch.object(file_controller, "move_no_replace", side_effect=cross_device), \
                 patch(
                     "core.path_policy.protected_roots",
                     return_value=(protected.resolve(),),
                 ):
                result = file_controller.move_file(str(source), destination=str(destination))

            self.assertIn("Access denied", result)
            self.assertTrue(source.exists())
            self.assertFalse(destination.exists())


class GeneratedOutputSafetyTests(unittest.TestCase):
    def test_parser_uses_a_stable_private_snapshot_and_cleans_it_up(self) -> None:
        with TemporaryDirectory(dir=Path.home()) as directory:
            source = Path(directory) / "report.txt"
            source.write_text("original", encoding="utf-8")
            with file_processor._stable_input_snapshot(source) as snapshot:
                self.assertTrue(snapshot.exists())
                source.write_text("raced replacement", encoding="utf-8")
                self.assertEqual(snapshot.read_text(encoding="utf-8"), "original")
                self.assertEqual(
                    file_processor._output_path(snapshot, "result").name,
                    "report_result.txt",
                )
                if os.name != "nt":
                    self.assertEqual(snapshot.stat().st_mode & 0o077, 0)
            self.assertFalse(snapshot.exists())

    def test_generated_output_race_preserves_the_external_file(self) -> None:
        with TemporaryDirectory(dir=Path.home()) as directory:
            destination = Path(directory) / "result.txt"

            def racing_writer(staging: Path) -> None:
                staging.write_text("generated", encoding="utf-8")
                destination.write_text("external", encoding="utf-8")

            with self.assertRaises(FileExistsError):
                file_processor._write_generated(destination, racing_writer)

            self.assertEqual(destination.read_text(encoding="utf-8"), "external")
            self.assertEqual(list(destination.parent.glob(".*.processing-*")), [])


class ArchiveSafetyTests(unittest.TestCase):
    def test_zip_traversal_is_rejected_without_writing(self) -> None:
        with TemporaryDirectory(dir=Path.home()) as directory:
            root = Path(directory)
            archive = root / "bad.zip"
            with zipfile.ZipFile(archive, "w") as handle:
                handle.writestr("../escaped.txt", "bad")
            destination = root / "output"

            result = file_processor._process_archive(
                archive, "extract", {"destination": str(destination)}
            )
            self.assertIn("Archive rejected", result)
            self.assertFalse(destination.exists())
            self.assertFalse((root / "escaped.txt").exists())

    def test_zip_symlink_is_rejected(self) -> None:
        with TemporaryDirectory(dir=Path.home()) as directory:
            archive = Path(directory) / "link.zip"
            info = zipfile.ZipInfo("link")
            info.create_system = 3
            info.external_attr = (0o120777 << 16)
            with zipfile.ZipFile(archive, "w") as handle:
                handle.writestr(info, "/etc/passwd")

            result = file_processor._process_archive(archive, "list", {})
            self.assertIn("Archive rejected", result)
            self.assertIn("links are not allowed", result)

    def test_extract_allocates_a_new_folder_instead_of_merging(self) -> None:
        with TemporaryDirectory(dir=Path.home()) as directory:
            root = Path(directory)
            archive = root / "bundle.zip"
            with zipfile.ZipFile(archive, "w") as handle:
                handle.writestr("file.txt", "content")
            occupied = root / "bundle"
            occupied.mkdir()
            (occupied / "existing.txt").write_text("keep", encoding="utf-8")

            result = file_processor._process_archive(archive, "extract", {})
            self.assertIn("bundle_1", result)
            self.assertEqual((occupied / "existing.txt").read_text(encoding="utf-8"), "keep")
            self.assertEqual((root / "bundle_1" / "file.txt").read_text(encoding="utf-8"), "content")

    def test_suspicious_compression_ratio_is_rejected(self) -> None:
        with TemporaryDirectory(dir=Path.home()) as directory:
            archive = Path(directory) / "bomb.zip"
            with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_DEFLATED) as handle:
                handle.writestr("huge.txt", b"A" * (11 * 1024 * 1024))
            result = file_processor._process_archive(archive, "list", {})
            self.assertIn("Archive rejected", result)
            self.assertIn("compression ratio", result)


if __name__ == "__main__":
    unittest.main()


class FailureReasonTests(unittest.TestCase):
    """A refused file operation has to say why it was refused.

    "Could not copy: ValueError" is indistinguishable from a bug. The user
    cannot tell a rejected name from a missing folder from a full disk, and
    neither can the model deciding what to do next.
    """

    def test_common_failures_are_translated_into_reasons(self) -> None:
        from actions.file_controller import _why

        self.assertEqual(_why(PermissionError()), "permission was denied")
        self.assertEqual(_why(FileNotFoundError()), "the path no longer exists")
        self.assertEqual(_why(FileExistsError()), "something with that name already exists")
        self.assertEqual(_why(IsADirectoryError()), "the target is a folder")

    def test_a_full_disk_is_named(self) -> None:
        from actions.file_controller import _why

        self.assertEqual(_why(OSError(28, "No space left on device")), "the disk is full")

    def test_a_validation_message_is_passed_through(self) -> None:
        from actions.file_controller import _why

        self.assertEqual(
            _why(ValueError("the name must not contain a folder path")),
            "the name must not contain a folder path",
        )

    def test_an_unhelpful_exception_still_names_its_type(self) -> None:
        from actions.file_controller import _why

        self.assertIn("RuntimeError", _why(RuntimeError()))
        self.assertIn("RuntimeError", _why(RuntimeError("x" * 500)))

    def test_xml_entities_are_rejected_by_the_parser(self) -> None:
        with TemporaryDirectory(dir=Path.home()) as directory:
            source = Path(directory) / "hostile.xml"
            source.write_text(
                '<!DOCTYPE x [<!ENTITY payload "expanded">]><x>&payload;</x>',
                encoding="utf-8",
            )
            result = _process_xml(source, "validate", {})
        self.assertIn("Invalid XML", result)

    def test_normal_xml_remains_supported(self) -> None:
        with TemporaryDirectory(dir=Path.home()) as directory:
            source = Path(directory) / "safe.xml"
            source.write_text("<root><item>value</item></root>", encoding="utf-8")
            result = _process_xml(source, "validate", {})
        self.assertIn("Valid XML", result)
        self.assertIn("root", result)

    def test_a_rejected_rename_explains_itself(self) -> None:
        import tempfile
        from pathlib import Path

        from actions.file_controller import file_controller

        with tempfile.TemporaryDirectory(dir=Path.home()) as directory:
            source = Path(directory) / "note.txt"
            source.write_text("x", encoding="utf-8")
            result = file_controller(
                {"action": "rename", "path": str(source), "name": "../escaped.txt"}
            )
        self.assertIn("folder path", result)
        self.assertNotIn("ValueError", result)
