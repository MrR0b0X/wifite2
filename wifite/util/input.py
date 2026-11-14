#!/usr/bin/env python
# -*- coding: utf-8 -*-

"""
Keyboard input handling for wifite2 TUI.
Provides non-blocking keyboard input for interactive views.
"""

import sys
import select
import os
import re
import shutil
import termios
import time
import tty
import atexit
from contextlib import contextmanager
from typing import Optional

from .color import Color


_echo_terminal_fd = None
_echo_terminal_close_fd = False
_echo_terminal_original = None
_echo_terminal_suppressed = False
_interactive_input_active = False


def _set_terminal_echo(enabled):
    """Toggle terminal echo while preserving the caller's original setting."""
    global _echo_terminal_fd, _echo_terminal_close_fd
    global _echo_terminal_original, _echo_terminal_suppressed

    if _echo_terminal_fd is None:
        fd, close_fd = _input_terminal_fd()
        if fd is None:
            return
        try:
            original = termios.tcgetattr(fd)
        except (termios.error, OSError):
            if close_fd:
                os.close(fd)
            return
        _echo_terminal_fd = fd
        _echo_terminal_close_fd = close_fd
        _echo_terminal_original = original
        atexit.register(_restore_terminal_echo)

    try:
        current = termios.tcgetattr(_echo_terminal_fd)
        updated = current[:]
        updated[6] = current[6][:]
        echo_flags = termios.ECHO | getattr(termios, 'ECHOCTL', 0)
        if enabled:
            updated[3] = (current[3] & ~echo_flags) | (
                _echo_terminal_original[3] & echo_flags)
        else:
            updated[3] &= ~echo_flags
        termios.tcsetattr(_echo_terminal_fd, termios.TCSANOW, updated)
        _echo_terminal_suppressed = not enabled
    except (termios.error, OSError):
        pass


def _restore_terminal_echo():
    """Restore the original terminal echo flags when Wifite exits."""
    global _echo_terminal_fd, _echo_terminal_close_fd
    global _echo_terminal_original, _echo_terminal_suppressed
    if _echo_terminal_fd is None:
        return
    try:
        current = termios.tcgetattr(_echo_terminal_fd)
        restored = current[:]
        restored[6] = current[6][:]
        echo_flags = termios.ECHO | getattr(termios, 'ECHOCTL', 0)
        restored[3] = (current[3] & ~echo_flags) | (
            _echo_terminal_original[3] & echo_flags)
        termios.tcsetattr(_echo_terminal_fd, termios.TCSANOW, restored)
    except (termios.error, OSError):
        pass
    if _echo_terminal_close_fd:
        try:
            os.close(_echo_terminal_fd)
        except OSError:
            pass
    _echo_terminal_fd = None
    _echo_terminal_close_fd = False
    _echo_terminal_original = None
    _echo_terminal_suppressed = False


def suppress_terminal_echo_for_messages():
    """Keep status output from echoing spare keystrokes as blank lines."""
    if not _interactive_input_active and not _echo_terminal_suppressed:
        _set_terminal_echo(False)


def interactive_input():
    """Read a line of interactive input, treating closed stdin like Ctrl+C.

    Non-interactive runs (piped stdin, ``< /dev/null``, CI) used to crash
    at prompts with "Unexpected Error: EOF when reading a line". Every
    prompt site already handles KeyboardInterrupt, so EOF is reported once
    here and raised as KeyboardInterrupt to reuse those graceful exits.
    """
    global _interactive_input_active
    if _echo_terminal_suppressed:
        _discard_queued_input()
    _set_terminal_echo(True)
    _interactive_input_active = True
    try:
        return input()
    except EOFError:
        _interactive_input_active = False
        suppress_terminal_echo_for_messages()
        Color.pl('\n{!} {O}No interactive input available (stdin closed); '
                 'aborting prompt{W}')
        raise KeyboardInterrupt from None
    finally:
        _interactive_input_active = False
        # input() echoes a newline, but Color.p() cannot see it. Forget the
        # prompt width so later status messages never clear/pad that old row.
        Color.last_sameline_length = 0


def _discard_queued_input():
    """Discard terminal keystrokes queued after a submitted line."""
    tty_fd = None
    close_fd = False
    try:
        fd = sys.stdin.fileno()
        if os.isatty(fd):
            tty_fd = fd
    except (AttributeError, OSError, ValueError):
        pass
    if tty_fd is None:
        try:
            tty_fd = os.open('/dev/tty', os.O_RDWR)
            close_fd = True
            if not os.isatty(tty_fd):
                os.close(tty_fd)
                return
        except OSError:
            return
    try:
        termios.tcflush(tty_fd, termios.TCIFLUSH)
    except (OSError, ValueError):
        pass
    finally:
        if close_fd:
            try:
                os.close(tty_fd)
            except OSError:
                pass


def _input_terminal_fd():
    """Return a terminal input descriptor and whether this function opened it."""
    try:
        fd = sys.stdin.fileno()
        if os.isatty(fd):
            return fd, False
    except (AttributeError, OSError, ValueError):
        pass
    try:
        fd = os.open('/dev/tty', os.O_RDWR)
        if os.isatty(fd):
            return fd, True
        os.close(fd)
    except OSError:
        pass
    return None, False


@contextmanager
def suppress_terminal_echo():
    """Hide and discard terminal input during a non-interactive operation."""
    fd, close_fd = _input_terminal_fd()
    original = None
    changed = False
    if fd is not None:
        try:
            original = termios.tcgetattr(fd)
            quiet = original[:]
            quiet[6] = original[6][:]
            quiet[3] &= ~termios.ECHO
            quiet[3] &= ~getattr(termios, 'ECHOCTL', 0)
            termios.tcsetattr(fd, termios.TCSANOW, quiet)
            changed = True
        except (termios.error, OSError):
            pass
    try:
        yield
    finally:
        if fd is not None:
            if changed:
                try:
                    termios.tcsetattr(fd, termios.TCSANOW, original)
                except (termios.error, OSError):
                    pass
            try:
                termios.tcflush(fd, termios.TCIFLUSH)
            except (termios.error, OSError):
                pass
            if close_fd:
                try:
                    os.close(fd)
                except OSError:
                    pass


def _collect_terminal_input(seconds, echo=True):
    """Read queued terminal keys, optionally suppressing their terminal echo."""
    fd, close_fd = _input_terminal_fd()
    if fd is None:
        time.sleep(seconds)
        _discard_queued_input()
        return b''

    try:
        original = termios.tcgetattr(fd)
    except (termios.error, OSError):
        if close_fd:
            os.close(fd)
        time.sleep(seconds)
        _discard_queued_input()
        return b''
    input_mode = original[:]
    input_mode[6] = original[6][:]
    input_mode[3] &= ~termios.ICANON
    if not echo:
        input_mode[3] &= ~termios.ECHO
    input_mode[3] &= ~getattr(termios, 'ECHOCTL', 0)
    input_mode[6][termios.VMIN] = 0
    input_mode[6][termios.VTIME] = 0
    pressed_keys = bytearray()
    try:
        termios.tcsetattr(fd, termios.TCSANOW, input_mode)
        deadline = time.monotonic() + seconds
        while True:
            remaining = max(0, deadline - time.monotonic())
            ready, _, _ = select.select([fd], [], [], remaining)
            if not ready:
                break
            keys = os.read(fd, 4096)
            if not keys:
                break
            pressed_keys.extend(keys)
    finally:
        try:
            termios.tcsetattr(fd, termios.TCSANOW, original)
            termios.tcflush(fd, termios.TCIFLUSH)
        except (termios.error, OSError):
            pass
        if close_fd:
            try:
                os.close(fd)
            except OSError:
                pass
    return bytes(pressed_keys)


def _echoed_input_rows(pressed_keys, columns):
    """Count terminal row advances from echoed text and Enter keypresses."""
    row_advances = 0
    column = 0
    pending_wrap = False
    for character in bytes(pressed_keys).decode('utf-8', errors='replace'):
        if character in ('\r', '\n'):
            row_advances += 1
            column = 0
            pending_wrap = False
            continue
        if character in ('\b', '\x7f'):
            column = max(0, column - 1)
            pending_wrap = False
            continue
        if character == '\t':
            width = 8 - (column % 8)
        elif character.isprintable():
            import unicodedata
            if unicodedata.combining(character):
                width = 0
            else:
                width = 2 if unicodedata.east_asian_width(character) in ('W', 'F') else 1
        else:
            width = 0
        if width == 0:
            continue
        if pending_wrap:
            row_advances += 1
            column = 0
            pending_wrap = False
        column += width
        while column > columns:
            row_advances += 1
            column -= columns
        if column == columns:
            pending_wrap = True
    return row_advances


def _clear_echoed_input(pressed_keys, row_advances):
    """Clear echoed type-ahead while preserving the previously submitted line."""
    if not pressed_keys:
        return
    if row_advances:
        _clear_terminal_rows(row_advances)
    else:
        sys.stdout.write('\r\033[2K')
        sys.stdout.flush()


def _terminal_geometry():
    """Return whether a controlling terminal exists and its column count."""
    for stream in (sys.stdout, sys.stdin):
        try:
            fd = stream.fileno()
            if os.isatty(fd):
                columns = os.get_terminal_size(fd).columns
                if columns <= 0:
                    columns = shutil.get_terminal_size(fallback=(80, 24)).columns
                return True, max(1, columns)
        except (AttributeError, OSError, ValueError):
            pass

    try:
        fd = os.open('/dev/tty', os.O_RDWR)
    except OSError:
        return False, max(1, shutil.get_terminal_size(fallback=(80, 24)).columns)
    try:
        if os.isatty(fd):
            columns = os.get_terminal_size(fd).columns
            if columns <= 0:
                columns = shutil.get_terminal_size(fallback=(80, 24)).columns
            return True, max(1, columns)
    except OSError:
        pass
    finally:
        os.close(fd)
    return False, max(1, shutil.get_terminal_size(fallback=(80, 24)).columns)


def _terminal_rows(text, columns):
    """Count terminal rows occupied by rendered text starting in column zero."""
    rendered = Color.s(text)
    rendered = re.sub(r'\033\[[0-?]*[ -/]*[@-~]', '', rendered)
    return sum(max(1, (len(line) + columns - 1) // columns)
               for line in rendered.split('\n'))


def _clear_terminal_rows(rows):
    """Clear *rows* from the current cursor position and return to their top."""
    sys.stdout.write(f'\033[{rows}A')
    for row in range(rows):
        sys.stdout.write('\r\033[2K')
        if row < rows - 1:
            sys.stdout.write('\033[1B')
    if rows > 1:
        sys.stdout.write(f'\033[{rows - 1}A')
    sys.stdout.write('\r')
    sys.stdout.flush()


def prompt_until_valid(prompt, parser, invalid_message='Invalid input.'):
    """Prompt until *parser* accepts the response, without repeating the prompt.

    ``parser`` should return the parsed value or raise ``ValueError`` when the
    response is invalid. Empty input is treated like any other value, so the
    caller can choose whether it is a valid default or should be retried.
    """
    Color.p(prompt)
    if Color.kalidroid:
        redraw, columns = False, 80
    else:
        redraw, columns = _terminal_geometry()
    while True:
        response = interactive_input()
        if redraw:
            queued_keys = _collect_terminal_input(0)
            queued_rows = _echoed_input_rows(queued_keys, columns)
            _clear_echoed_input(queued_keys, queued_rows)
        else:
            _discard_queued_input()
        suppress_terminal_echo_for_messages()
        try:
            return parser(response)
        except ValueError as error:
            retry_message = str(error) or invalid_message
            if redraw:
                _clear_terminal_rows(_terminal_rows(prompt + response, columns))
            Color.pl('{!} {O}%s{W}' % retry_message)
            if redraw:
                _collect_terminal_input(1, echo=False)
                error_rows = _terminal_rows(
                    '{!} {O}%s{W}' % retry_message, columns)
                _clear_terminal_rows(error_rows)
                Color.last_sameline_length = 0
                Color.p(prompt)
            else:
                time.sleep(1)
                _discard_queued_input()

def parse_index_selection(response, maximum, allow_all=True):
    """Parse comma-separated 1-based indices and inclusive ranges.

    Returns zero-based indices. Raises ``ValueError`` for blanks, malformed
    values, reversed ranges, or indices outside ``1..maximum``.
    """
    value = response.strip().lower()
    if allow_all and value == 'all':
        return list(range(maximum))
    if not value:
        raise ValueError(f'Enter a selection from 1 to {maximum}.')

    selected = []
    for part in value.split(','):
        part = part.strip()
        if not part:
            raise ValueError('Use comma-separated numbers or ranges.')
        if '-' in part:
            bounds = part.split('-')
            if not all(bound.strip().isdigit() for bound in bounds):
                raise ValueError('Use ranges like 1-3 or dash-separated numbers.')
            numbers = [int(bound.strip()) for bound in bounds]
            if len(numbers) == 2:
                first, last = numbers
                if first > last:
                    raise ValueError('Range start must not exceed its end.')
                numbers = range(first, last + 1)
        elif part.isdigit():
            numbers = [int(part)]
        else:
            raise ValueError('Use comma-separated numbers or ranges.')

        if any(number < 1 or number > maximum for number in numbers):
            raise ValueError(f'Choose numbers from 1 to {maximum}.')
        selected.extend(number - 1 for number in numbers)

    return list(dict.fromkeys(selected))


def parse_yes_no(response, default=None):
    """Parse a yes/no response, optionally using a default for blank input."""
    value = response.strip().lower()
    if not value and default is not None:
        return bool(default)
    if value in ('y', 'yes'):
        return True
    if value in ('n', 'no'):
        return False
    raise ValueError('Enter y or n.')


class KeyboardInput:
    """Non-blocking keyboard input handler."""

    def __init__(self):
        """Initialize keyboard input handler."""
        self.fd = sys.stdin.fileno()
        self.old_settings = None

    def __enter__(self):
        """Context manager entry - set up raw mode."""
        self.old_settings = termios.tcgetattr(self.fd)
        tty.setraw(self.fd)
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        """Context manager exit - restore terminal settings."""
        if self.old_settings:
            termios.tcsetattr(self.fd, termios.TCSADRAIN, self.old_settings)
        return False

    def get_key(self, timeout: float = 0.0) -> Optional[str]:
        """
        Get a single keypress with optional timeout.

        Args:
            timeout: Timeout in seconds (0 = non-blocking)

        Returns:
            Key string or None if no key pressed
        """
        # Check if input is available
        if timeout > 0:
            ready, _, _ = select.select([sys.stdin], [], [], timeout)
            if not ready:
                return None
        else:
            # Non-blocking check
            ready, _, _ = select.select([sys.stdin], [], [], 0)
            if not ready:
                return None

        # Read the key
        ch = sys.stdin.read(1)

        # Handle escape sequences (arrow keys, function keys, etc.)
        if ch == '\x1b':
            # Check if more characters are available (escape sequence)
            ready, _, _ = select.select([sys.stdin], [], [], 0.1)
            if ready:
                ch2 = sys.stdin.read(1)
                if ch2 == '[':
                    # CSI sequence
                    ch3 = sys.stdin.read(1)
                    
                    # Handle sequences that end with ~
                    if ch3 in '0123456789':
                        ch4 = sys.stdin.read(1)
                        if ch4 == '~':
                            return '\x1b[' + ch3 + ch4
                        else:
                            return '\x1b[' + ch3 + ch4
                    else:
                        return '\x1b[' + ch3
                else:
                    return '\x1b' + ch2
            else:
                # Just ESC key
                return '\x1b'

        return ch

    @staticmethod
    def is_ctrl_c(key: str) -> bool:
        """
        Check if key is Ctrl+C.

        Args:
            key: Key string

        Returns:
            True if Ctrl+C, False otherwise
        """
        return key == '\x03'

    @staticmethod
    def key_name(key: str) -> str:
        """
        Get human-readable name for key.

        Args:
            key: Key string

        Returns:
            Human-readable key name
        """
        key_names = {
            '\x1b[A': 'Up',
            '\x1b[B': 'Down',
            '\x1b[C': 'Right',
            '\x1b[D': 'Left',
            '\x1b[H': 'Home',
            '\x1b[F': 'End',
            '\x1b[5~': 'Page Up',
            '\x1b[6~': 'Page Down',
            '\x1b': 'Escape',
            '\r': 'Enter',
            '\n': 'Enter',
            ' ': 'Space',
            '\x03': 'Ctrl+C',
            '\x7f': 'Backspace',
            '\t': 'Tab',
        }
        return key_names.get(key, key if len(key) == 1 else 'Unknown')


class NonBlockingInput:
    """
    Simple non-blocking input reader for use during scanning/attacks.
    Does not require raw mode.
    """

    @staticmethod
    def has_input(timeout: float = 0.0) -> bool:
        """
        Check if input is available.

        Args:
            timeout: Timeout in seconds (0 = non-blocking)

        Returns:
            True if input is available, False otherwise
        """
        ready, _, _ = select.select([sys.stdin], [], [], timeout)
        return bool(ready)

    @staticmethod
    def read_line(timeout: float = 0.0) -> Optional[str]:
        """
        Read a line of input with optional timeout.

        Args:
            timeout: Timeout in seconds (0 = non-blocking)

        Returns:
            Input line or None if no input available
        """
        if NonBlockingInput.has_input(timeout):
            try:
                return sys.stdin.readline().strip()
            except (OSError, ValueError):
                return None
        return None


def handle_ctrl_c(func):
    """
    Decorator to handle Ctrl+C gracefully in TUI views.

    Args:
        func: Function to wrap

    Returns:
        Wrapped function
    """
    def wrapper(*args, **kwargs):
        try:
            return func(*args, **kwargs)
        except KeyboardInterrupt:
            # Let the caller handle the interrupt
            raise
    return wrapper


# Convenience functions for common key checks
def is_arrow_key(key: str) -> bool:
    """Check if key is an arrow key."""
    return key in ['\x1b[A', '\x1b[B', '\x1b[C', '\x1b[D']


def is_navigation_key(key: str) -> bool:
    """Check if key is a navigation key (arrows, page up/down, home/end)."""
    return key in ['\x1b[A', '\x1b[B', '\x1b[C', '\x1b[D', 
                   '\x1b[H', '\x1b[F', '\x1b[5~', '\x1b[6~']


def is_enter_key(key: str) -> bool:
    """Check if key is Enter."""
    return key in ['\r', '\n']


def is_escape_key(key: str) -> bool:
    """Check if key is Escape."""
    return key == '\x1b'
