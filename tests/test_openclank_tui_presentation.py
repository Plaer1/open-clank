"""Focused output capture; no server, credentials or terminal process."""
import io
import os
from types import SimpleNamespace
from unittest.mock import patch
from src.openclank.tui_app import OpenClankTui
from src.openclank.cli import _terminal_print
from src.openclank.tui_client import TuiClientError


class Output(io.StringIO):
    def __init__(self, tty):
        super().__init__(); self.tty = tty
    def isatty(self): return self.tty
    def fileno(self): return 42


class Client:
    profile = SimpleNamespace(name='profile\x1b[31m')
    def bootstrap(self): return {'principal': {'owner':'owner\u009b'}, 'sessions':[{'id':'s','name':'a'*50,'model':'model'}]}
    def messages(self, *_a, **_kw): return {'items':[{'role':'assistant','content':'hello\x1b[31m world'}]}
    def stream_turn(self, *_a):
        yield {'delta':'safe\x1b[31m'}
        yield {'type':'permission_request'}
    def diagnostics(self): raise TuiClientError('failure\x1b[2J')


def test_terminal_presentation():
    with patch.dict(os.environ, {}, clear=True), patch('os.get_terminal_size', return_value=os.terminal_size((32,24))):
        color = Output(True); tui = OpenClankTui(Client(), output=color); tui._refresh(); tui._show_home(); tui._show_messages('1'); tui._send_turn('1 hi')
        assert '\x1b[38;5;44mOPEN CLANK\x1b[0m' in color.getvalue()
        assert '\x1b[31m' not in color.getvalue() and '\u009b' not in color.getvalue()
        assert 'ASSISTANT' in color.getvalue() and 'Human action required' in color.getvalue()
        assert all(len(line) <= 32 for line in color.getvalue().replace('\x1b[38;5;44m','').replace('\x1b[0m','').splitlines())
        plain = Output(False); tui = OpenClankTui(Client(), output=plain); tui._refresh(); tui._show_home(); tui._show_messages('1'); tui._send_turn('1 hi'); tui._write('Request failed: failure\x1b[2J')
        assert '\x1b' not in plain.getvalue() and 'Request failed:' in plain.getvalue()
        no_color = Output(True)
        with patch.dict(os.environ, {'NO_COLOR':'1'}):
            tui = OpenClankTui(Client(), output=no_color); tui._refresh(); tui._show_home()
        assert '\x1b' not in no_color.getvalue()
        diagnostics = io.StringIO(); _terminal_print('failure\x1b[2J\u009b', file=diagnostics)
        assert diagnostics.getvalue() == 'failure\n'
