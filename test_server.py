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


DRAFT_UUID = "4192A339-DC34-4C34-A786-1485843076E8"


def mime(subject, files, body="Test body", recipients=None, sender=""):
    message = EmailMessage()
    message["Subject"] = subject
    message["X-Universally-Unique-Identifier"] = DRAFT_UUID
    for kind, addresses in (recipients or {}).items():
        if addresses: message[kind] = ', '.join(addresses)
    if sender: message['From'] = sender
    message.set_content(body)
    for name, data in files:
        message.add_attachment(data, maintype="application", subtype="pdf", filename=name)
    return message.as_string()


class MailHarness:
    """Model asynchronous saves at the osascript boundary, not the verifier."""

    def __init__(self, *, corrupt=False, lose_on_close=False, never_ready=False):
        self.subject = ""
        self.body = ""
        self.recipients = {}
        self.sender = ""
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
            self.body = arguments[-1]
            self.sender = arguments[2]
            position = 3
            for kind in ('to', 'cc', 'bcc'):
                count = int(arguments[position]); position += 1
                self.recipients[kind] = arguments[position:position+count]
                position += count
            return "87"  # Compose IDs do not equal saved IDs.
        if script == server.COMPOSE_SCRIPT:
            op = arguments[0]
            if op == "prepare":
                self.subject = arguments[2]
                self.body = arguments[3]
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
            if not arguments[1]:
                assert arguments[3] == DRAFT_UUID, "Follow saves by stable Mail UUID"
                assert not arguments[2], "RFC Message-ID changes on every save"
            assert all(p.exists() for p in self.source_paths)
            return json.dumps({
                "id": 111476 + len(self.calls), "message_id": f"save-{len(self.calls)}@example.invalid",
                "subject": self.subject, "source": mime(self.subject, self.visible_files, self.body, self.recipients, self.sender),
            })
        raise AssertionError("Unexpected script")


class AttachmentTests(unittest.TestCase):
    def test_stale_versions_are_selected_only_by_full_verified_state(self):
        import time
        expected=Counter({server._fingerprint("a.pdf",b"a"):1})
        stale={"id":1,"source":mime("test",[],"body")}
        current={"id":2,"source":mime("test",[("a.pdf",b"a")],"body")}
        with patch.object(server,"_run_script",return_value=json.dumps([stale,current])):
            result=server._wait_for_saved(expected,time.monotonic()+1,draft_uuid=DRAFT_UUID,subject="test",fields={"body":"body"})
            self.assertEqual(result["id"],2)
        with patch.object(server,"_run_script",return_value=json.dumps([current,{**current,"id":3}])), patch.object(server,"POLL_INTERVAL",0.001):
            with self.assertRaisesRegex(server.ToolError,"Multiple saved versions"):
                server._wait_for_saved(expected,time.monotonic()+0.01,draft_uuid=DRAFT_UUID,subject="test",fields={"body":"body"})

    def test_saved_fields_must_match_body_and_every_recipient_group(self):
        fields={'body':'Body', 'to':['to@example.invalid'], 'cc':['cc@example.invalid'], 'bcc':['bcc@example.invalid'], 'sender':'from@example.invalid'}
        source=mime('subject', [], 'Body', {k:fields[k] for k in ('to','cc','bcc')}, fields['sender'])
        self.assertTrue(server._fields_match(source, fields))
        for changed in ({'body':'Wrong'}, {'bcc':[]}, {'to':['other@example.invalid']}, {'sender':'other@example.invalid'}):
            self.assertFalse(server._fields_match(source, {**fields, **changed}))

    def test_unrequested_leading_blank_line_is_rejected(self):
        self.assertFalse(server._fields_match(mime('test',[],'\nBody'),{'body':'Body'}))
        self.assertTrue(server._fields_match(mime('test',[],'\nBody'),{'body':'\nBody'}))

    def test_generated_html_hides_only_mail_header_and_escapes_user_text(self):
        text='  <script>alert("x")</script> & text\nSecond'
        html=server._body_html(text)
        self.assertNotIn('<script>',html)
        html=html.replace('<body>','<body><div class="Apple-Mail-URLShareUserContentTopClass"><br></div>')
        m=EmailMessage();m.set_content('');m.add_alternative(html,subtype='html')
        self.assertTrue(server._fields_match(m.as_string(),{'body':text}))
        without_style=html.replace('div.Apple-Mail-URLShareUserContentTopClass { display: none !important; }','')
        m=EmailMessage();m.set_content('');m.add_alternative(without_style,subtype='html')
        self.assertFalse(server._fields_match(m.as_string(),{'body':text}))

    def test_mail_unicode_line_separator_matches_saved_newline(self):
        expected={'body':'\n\u2028\nBody\u2028Second line\n'}
        self.assertTrue(server._fields_match(mime('test',[],'\n\n\nBody\nSecond line\n'),expected))
        self.assertFalse(server._fields_match(mime('test',[],'Body Second line'),expected))

    def test_mail_html_alternative_and_conflicting_bodies(self):
        message=EmailMessage()
        message.set_content('')
        message.add_alternative('<html><head><style>ignored</style></head><body><div><br></div><blockquote><p><span>&nbsp; </span>Indented.</p>\n<p>åäö &amp; text</p><object>attachment preview</object></blockquote></body></html>',subtype='html')
        expected={'body':'\n  Indented.\nåäö & text\n','to':[],'cc':[],'bcc':[]}
        self.assertTrue(server._fields_match(message.as_string(),expected))
        self.assertFalse(server._fields_match(message.as_string(),{**expected,'body':'Wrong text'}))
        message.get_body(preferencelist=('plain',)).set_content('Conflicting visible text')
        self.assertFalse(server._fields_match(message.as_string(),expected))

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
        self.assertTrue(all(a[3] == DRAFT_UUID and not a[2] for a in snapshots[1:]))

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

    def test_draft_uuid_is_required_before_replacing_marker(self):
        self.assertEqual(server._draft_uuid(mime("test", [])), DRAFT_UUID)
        for source in ("Subject: test\n\nbody", "X-Universally-Unique-Identifier: invalid\n\nbody"):
            with self.assertRaisesRegex(server.ToolError, "stable draft UUID"):
                server._draft_uuid(source)

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
