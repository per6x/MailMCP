"""Deterministic attachment regressions; no running Mail application required."""

from collections import Counter
from email.message import EmailMessage
from pathlib import Path
import json
import subprocess
import tempfile
import unittest
from unittest.mock import patch

import server


def mime(subject, files):
    message = EmailMessage()
    message["Subject"] = subject
    message.set_content("Test body")
    for name, data in files:
        message.add_attachment(data, maintype="application", subtype="pdf", filename=name)
    return message.as_string()


class MailHarness:
    """Model asynchronous saves at the osascript boundary, not the verifier."""

    def __init__(self, *, corrupt=False, lose_on_close=False, never_ready=False):
        self.subject = ""
        self.files = []
        self.visible_files = []
        self.calls = []
        self.corrupt = corrupt
        self.lose_on_close = lose_on_close
        self.never_ready = never_ready
        self.closed = False
        self.source_paths = []

    def run(self, script, arguments, deadline, **kwargs):
        self.calls.append((script, arguments))
        if script == server.MAIL_SCRIPT:
            self.subject = arguments[-1]
            return "87"  # Compose IDs do not equal saved IDs.
        if script == server.COMPOSE_SCRIPT:
            op = arguments[0]
            if op == "prepare":
                self.subject = arguments[2]
            elif op == "attach":
                path = Path(arguments[2])
                self.source_paths.append(path)
                self.files.append((path.name, path.read_bytes()))
            elif op == "save" and not self.never_ready:
                self.visible_files = list(self.files)
                if self.corrupt:
                    self.visible_files = [(n, b"x" * len(data)) for n, data in self.files]
            elif op == "close":
                self.closed = True
                if self.lose_on_close:
                    self.visible_files = []
            return ""
        if script == server.DRAFT_SCRIPT:
            assert arguments[0] == "snapshot"
            assert all(p.exists() for p in self.source_paths)
            return json.dumps({
                "id": 111476 + len(self.calls), "message_id": "test@example.invalid",
                "subject": self.subject, "source": mime(self.subject, self.visible_files),
            })
        raise AssertionError("Unexpected script")


class AttachmentTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.files = []
        for folder, name, data in (("a", "CV å.pdf", b"first pdf"),
                                   ("b", "letter.pdf", b"second pdf")):
            path = Path(self.temp.name) / folder / name
            path.parent.mkdir()
            path.write_bytes(data)
            self.files.append(str(path))

    def save(self, harness, files=None):
        with patch.object(server, "_run_script", side_effect=harness.run), \
             patch.object(server, "SAVE_TIMEOUT", 0.05), \
             patch.object(server, "POLL_INTERVAL", 0.001), \
             patch.object(server.sys, "platform", "darwin"):
            return server.save_email_draft(
                to=[], subject="Verification", body="Test body",
                attachments=self.files if files is None else files,
            )

    def test_delayed_loading_is_saved_before_next_attachment_and_close(self):
        h = MailHarness()
        self.assertEqual(self.save(h), "Draft saved in Apple Mail.")
        operations = [a[0] for s, a in h.calls if s == server.COMPOSE_SCRIPT]
        self.assertEqual(operations, ["prepare", "attach", "save", "attach", "save", "close"])
        self.assertTrue(h.closed)
        snapshots = [a for s, a in h.calls if s == server.DRAFT_SCRIPT]
        self.assertTrue(all(a[2] == "test@example.invalid" for a in snapshots[1:]))

    def test_duplicate_filenames_keep_multiplicity_and_contents(self):
        other = Path(self.temp.name) / "b" / "CV å.pdf"
        other.write_bytes(b"different contents")
        h = MailHarness()
        self.save(h, [self.files[0], str(other), self.files[0]])
        self.assertEqual(len(h.visible_files), 3)

    def test_missing_or_corrupt_attachments_fail_without_closing(self):
        for mode in ({"corrupt": True}, {"never_ready": True}):
            with self.subTest(mode=mode):
                h = MailHarness(**mode)
                with self.assertRaisesRegex(server.ToolError, "missing or mismatched.*CV å.pdf"):
                    self.save(h)
                self.assertFalse(h.closed)
                self.assertEqual(len(h.files), 1)

    def test_close_must_preserve_persisted_bytes(self):
        with self.assertRaisesRegex(server.ToolError, "Could not verify"):
            self.save(MailHarness(lose_on_close=True))

    def test_empty_attachments_still_verify_after_close(self):
        h = MailHarness()
        self.save(h, [])
        self.assertTrue(h.closed)
        self.assertEqual(h.calls[-1][0], server.DRAFT_SCRIPT)

    def test_invalid_file_does_not_create_draft(self):
        h = MailHarness()
        with self.assertRaisesRegex(server.ToolError, "not a file"):
            self.save(h, [str(Path(self.temp.name) / "missing")])
        self.assertEqual(h.calls, [])

    def test_mime_decodes_inline_unicode_and_preserves_duplicate_counts(self):
        m = EmailMessage()
        m.set_content("body")
        for _ in range(2):
            m.add_attachment(b"pdf", maintype="application", subtype="pdf",
                             filename="CV å.pdf", disposition="inline")
        self.assertEqual(server._mime_attachments(m.as_string()),
                         Counter({server._fingerprint("CV å.pdf", b"pdf"): 2}))

    def test_subprocess_timeout_is_actionable(self):
        with patch.object(server.subprocess, "run", side_effect=subprocess.TimeoutExpired("osascript", 1)):
            with self.assertRaisesRegex(server.ToolError, "save deadline"):
                server._run_script("test", [], server.time.monotonic() + 1)

    def test_edit_keeps_exports_until_verification_and_original_on_failure(self):
        for fail in (False, True):
            with self.subTest(fail=fail):
                calls = []
                exports = []
                message = {"body": "body", "subject": "old", "to": [], "cc": [],
                           "bcc": [], "sender": "", "attachments": [{"name": "CV.pdf"}]}

                def operation(op, value, *extra):
                    calls.append(op)
                    if op == "export":
                        path = Path(extra[0]) / "0" / "CV.pdf"
                        path.write_bytes(b"exported pdf")
                        exports.append(path)
                        return [str(path)]
                    return {}

                h = MailHarness(never_ready=fail)
                with patch.object(server, "read_email_draft", return_value=message), \
                     patch.object(server, "_draft_operation", side_effect=operation), \
                     patch.object(server, "_run_script", side_effect=h.run), \
                     patch.object(server, "SAVE_TIMEOUT", 0.05), \
                     patch.object(server, "POLL_INTERVAL", 0.001), \
                     patch.object(server.sys, "platform", "darwin"):
                    if fail:
                        with self.assertRaises(server.ToolError):
                            server.edit_email_draft(123, subject="edited")
                    else:
                        server.edit_email_draft(123, subject="edited")
                self.assertEqual("delete" in calls, not fail)
                self.assertTrue(all(not p.exists() for p in exports))

    def test_edit_replaces_and_clears_without_export(self):
        message = {"body": "body", "subject": "old", "to": [], "cc": [],
                   "bcc": [], "sender": "", "attachments": [{"name": "old.pdf"}]}
        for files in ([], self.files):
            with patch.object(server, "read_email_draft", return_value=message), \
                 patch.object(server, "_draft_operation") as operation, \
                 patch.object(server, "save_email_draft") as save:
                server.edit_email_draft(123, attachments=files)
                self.assertEqual(save.call_args.kwargs["attachments"], files)
                operation.assert_called_once_with("delete", 123)


if __name__ == "__main__":
    unittest.main()
