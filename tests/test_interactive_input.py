import builtins
from contextlib import redirect_stdout
import io
import termios
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from wifite.attack.all import Answer, AttackAll
from wifite.config import Configuration
from wifite.util.color import Color
from wifite.util.input import (
    _discard_queued_input,
    _collect_terminal_input,
    _echoed_input_rows,
    _terminal_geometry,
    interactive_input,
    parse_index_selection,
    parse_yes_no,
    prompt_until_valid,
)


def stdin_eof():
    raise EOFError('eof')


class TestInteractiveInput(unittest.TestCase):
    def test_passthrough_when_stdin_available(self):
        Color.last_sameline_length = 12
        with patch.object(builtins, 'input', return_value='  yes  '):
            self.assertEqual(interactive_input(), '  yes  ')
        self.assertEqual(Color.last_sameline_length, 0)

    def test_eof_raises_keyboard_interrupt_with_notice(self):
        Color.last_sameline_length = 12
        with patch.object(builtins, 'input', side_effect=stdin_eof):
            with self.assertRaises(KeyboardInterrupt):
                interactive_input()
        self.assertEqual(Color.last_sameline_length, 0)

    def test_prompt_site_exits_gracefully_on_eof(self):
        # Regression test: closed stdin at an interactive prompt used to
        # crash with "Unexpected Error: EOF when reading a line".
        with patch.object(builtins, 'input', side_effect=stdin_eof):
            answer = AttackAll.user_wants_to_continue(targets_remaining=1,
                                                      attacks_remaining=1)
        self.assertEqual(answer, Answer.ExitOrReturn)

    def test_yes_no_parser_requires_explicit_choice_or_uses_default(self):
        self.assertTrue(parse_yes_no(' YES '))
        self.assertFalse(parse_yes_no('no'))
        self.assertTrue(parse_yes_no('', default=True))
        self.assertFalse(parse_yes_no('', default=False))
        with self.assertRaises(ValueError):
            parse_yes_no('maybe')

    def test_index_selection_parser_checks_all_indices_and_ranges(self):
        self.assertEqual(parse_index_selection('1, 3-4', 4), [0, 2, 3])
        self.assertEqual(parse_index_selection('6-10-11-12', 12), [5, 9, 10, 11])
        self.assertEqual(parse_index_selection('all', 3), [0, 1, 2])
        for answer in ('', '0', '4', '2-1', '1,,2', 'one'):
            with self.subTest(answer=answer), self.assertRaises(ValueError):
                parse_index_selection(answer, 3)

    def test_invalid_input_reuses_prompt_row_and_keeps_error(self):
        class TTYOutput(io.StringIO):
            def isatty(self):
                return True

            def fileno(self):
                return 1

        def parse_yes_no_or_retry(value):
            value = value.strip().lower()
            if value in ('y', 'yes'):
                return True
            if value in ('n', 'no'):
                return False
            raise ValueError()

        output = TTYOutput()
        with patch('wifite.util.input.sys.stdout', output), \
                patch('wifite.util.input.os.isatty', return_value=True), \
                patch('wifite.util.input.os.get_terminal_size',
                      return_value=SimpleNamespace(columns=40)), \
                patch('wifite.util.input.interactive_input', side_effect=['', 'Y']), \
                patch('wifite.util.input._collect_terminal_input',
                      side_effect=[b'', b'\n\n', b'']) as collect_input, \
                patch.object(Color, 'kalidroid', False):
            result = prompt_until_valid(
                'Restore interface to managed mode?' + (' details' * 8)
                + '\n    Confirm (y/n): ',
                parse_yes_no_or_retry,
                invalid_message='Please enter y or n.')

        self.assertTrue(result)
        self.assertEqual(collect_input.call_args_list[0].args, (0,))
        self.assertEqual(collect_input.call_args_list[1].args, (1,))
        self.assertEqual(collect_input.call_args_list[1].kwargs, {'echo': False})
        self.assertEqual(collect_input.call_args_list[2].args, (0,))
        rendered = output.getvalue()
        self.assertIn('Please enter y or n.', rendered)
        self.assertEqual(rendered.count('Please enter y or n.'), 1)
        self.assertEqual(rendered.count('Restore interface to managed mode?'), 2)
        self.assertIn('\033[1A', rendered)
        self.assertGreaterEqual(rendered.count('\r\033[2K'), 3)

    def test_discards_queued_keys_from_terminal_input(self):
        class TTYInput:
            def fileno(self):
                return 17

        with patch('wifite.util.input.sys.stdin', TTYInput()), \
                patch('wifite.util.input.os.isatty', return_value=True), \
                patch('wifite.util.input.termios.tcflush') as tcflush:
            _discard_queued_input()

        tcflush.assert_called_once_with(17, termios.TCIFLUSH)

    def test_detects_controlling_terminal_when_standard_streams_are_pipes(self):
        class PipedInput:
            def fileno(self):
                return 19

        with patch('wifite.util.input.sys.stdout', io.StringIO()), \
                patch('wifite.util.input.sys.stdin', PipedInput()), \
                patch('wifite.util.input.os.isatty', side_effect=[False, True]), \
                patch('wifite.util.input.os.open', return_value=23), \
                patch('wifite.util.input.os.get_terminal_size',
                      return_value=SimpleNamespace(columns=54)), \
                patch('wifite.util.input.os.close') as close_tty:
            self.assertEqual(_terminal_geometry(), (True, 54))

        close_tty.assert_called_once_with(23)

    def test_counts_enter_and_typed_rows_in_terminal_echo(self):
        self.assertEqual(_echoed_input_rows(b'\n\n', 80), 2)
        self.assertEqual(_echoed_input_rows(b'abc\n\n', 80), 2)
        self.assertEqual(_echoed_input_rows(b'abcde\n', 4), 2)
        self.assertEqual(_echoed_input_rows(b'12345', 4), 1)

    def test_extra_enters_are_cleared_before_and_during_error(self):
        def parse_choice(value):
            if value == '1':
                return 1
            raise ValueError('Choose an interface from 1 to 4.')

        cases = (
            (b'\n\n', b''),       # extra enters immediately after submission
            (b'', b'\n\n\n'),   # enters while the error is displayed
            (b'', b'hhh\n\n\n'),  # typed text and enters during the error
        )
        for before_error, during_error in cases:
            with self.subTest(before_error=before_error, during_error=during_error):
                output = io.StringIO()
                with patch('wifite.util.input.sys.stdout', output), \
                        patch('wifite.util.input.os.isatty', return_value=True), \
                        patch('wifite.util.input.os.get_terminal_size',
                              return_value=SimpleNamespace(columns=40)), \
                        patch('wifite.util.input.interactive_input',
                              side_effect=['bad', '1']), \
                        patch('wifite.util.input._collect_terminal_input',
                              side_effect=[before_error, during_error, b'']) as collect_input, \
                        patch.object(Color, 'kalidroid', False):
                    result = prompt_until_valid(
                        'Select wireless interface (1-4): ', parse_choice)

                self.assertEqual(result, 1)
                rendered = output.getvalue()
                self.assertEqual(
                    rendered.count('Choose an interface from 1 to 4.'), 1)
                self.assertEqual(
                    rendered.count('Select wireless interface'), 2)
                self.assertGreaterEqual(rendered.count('\r\033[2K'), 2)
                self.assertEqual(collect_input.call_args_list[1].kwargs,
                                 {'echo': False})

    def test_attack_continue_prompt_is_emitted_once(self):
        output = io.StringIO()
        with patch('wifite.util.input.interactive_input', return_value='c'), \
                patch.object(Configuration, 'infinite_mode', False), \
                redirect_stdout(output):
            answer = AttackAll.user_wants_to_continue(
                targets_remaining=0, attacks_remaining=1)

        self.assertEqual(answer, Answer.Continue)
        rendered = output.getvalue()
        self.assertEqual(rendered.count('Do you want to'), 1)
        self.assertIn('current target,\n    or ', rendered)


if __name__ == '__main__':
    unittest.main()
