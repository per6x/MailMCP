"""Rule/signature validation and update semantics without accessing Mail."""
import unittest
from unittest.mock import patch
from pydantic import ValidationError
from mcp.server.mcpserver.exceptions import ToolError

import mail_settings as settings


class SettingsTests(unittest.TestCase):
    def test_rule_is_disabled_by_default_and_conditions_are_validated(self):
        condition=settings.RuleCondition(field='subject header',expression='fixture')
        with patch.object(settings.api,'call',return_value={}) as call:
            settings.create_mail_rule('Test',[condition],settings.RuleActions(mark_read=True))
        self.assertIs(call.call_args.kwargs['enabled'],False)
        self.assertEqual(call.call_args.kwargs['conditions'][0]['field'],'subject header')
        self.assertEqual(call.call_args.kwargs['actions'],{'mark_read':True})
        with self.assertRaises(ValidationError): settings.RuleCondition(field='arbitrary code')

    def test_omitted_actions_and_explicit_destination_clear_are_distinct(self):
        with patch.object(settings.api,'call',return_value={}) as call:
            settings.update_mail_rule('Test',actions=settings.RuleActions(copy_to=None))
        self.assertEqual(call.call_args.kwargs['actions'],{'copy_to':None})
        self.assertIsNone(call.call_args.kwargs['conditions'])
        self.assertIsNone(call.call_args.kwargs['enabled'])

    def test_invalid_or_empty_configuration_never_calls_mail(self):
        with patch.object(settings.api,'call') as call:
            operations=[lambda:settings.create_signature('', 'text'),
                        lambda:settings.update_signature('Test'),
                        lambda:settings.update_mail_rule('Test'),
                        lambda:settings.create_mail_rule('Test',[],settings.RuleActions()),
                        lambda:settings.create_mail_rule('Test',[settings.RuleCondition(field='header key')],settings.RuleActions()),
                        lambda:settings.update_mail_rule('Test',actions=settings.RuleActions(mark_read=None))]
            for operation in operations:
                with self.assertRaises(ToolError):operation()
            call.assert_not_called()

    def test_action_schema_rejects_unknown_fields_and_invalid_flag_colors(self):
        for value in ({'execute':'arbitrary'}, {'flag_index':7}, {'mark_read':'true'}):
            with self.assertRaises(ValidationError):settings.RuleActions(**value)


if __name__=='__main__':unittest.main()
