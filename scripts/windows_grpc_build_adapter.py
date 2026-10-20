"""Explicit isolated grpc build adapter retaining O2 with supported per-module optimization."""
import os
from pathlib import Path
import setuptools
from distutils.ccompiler import new_compiler

# Select the exact class used by the pinned distutils compiler factory, not an import alias.
Compiler = type(new_compiler(compiler="msvc"))

if os.name != 'nt':
    raise RuntimeError('Native MSVC builder adapter requires Windows')
_original_call = Compiler.call
_reported = set()

def language_call(self, cmd, **kwargs):
    # Language correction uses explicit cl inputs; only the declared cl/link optimization flags change.
    c = any(str(arg).lower().startswith('/tc') for arg in cmd)
    cpp = any(str(arg).lower().startswith('/tp') for arg in cmd)
    filtered = list(cmd)
    if Path(str(cmd[0])).name.lower() == 'cl.exe' and c != cpp:
        removed = '/std:c++17' if c else '/std:c11'
        filtered = [arg for arg in cmd if str(arg).lower() != removed]
        if len(filtered) != len(cmd):
            language = 'C11' if c else 'C++17'
            if language not in _reported:
                print('[msvc-language] '+language+' command removed only '+removed, flush=True)
                _reported.add(language)
    executable = Path(str(cmd[0])).name.lower()
    if executable == 'cl.exe' and c != cpp:
        replacement = ('/gl', '/GL-')
    elif executable == 'link.exe':
        replacement = ('/ltcg', '/LTCG:OFF')
    else:
        replacement = None
    if replacement is not None:
        original, selected = replacement
        count = sum(str(arg).lower() == original for arg in filtered)
        filtered = [selected if str(arg).lower() == original else arg for arg in filtered]
        if count and executable not in _reported:
            print('[msvc-no-ltcg] '+executable+' replaced only '+original+' with '+selected+'; per-module O2 unchanged', flush=True)
            _reported.add(executable)
    return _original_call(self, filtered, **kwargs)

language_call._openclank_grpc_language_adapter = True
language_call._openclank_grpc_no_ltcg_adapter = True
Compiler.call = language_call
print('[msvc-no-ltcg] pinned build-only per-module adapter active', flush=True)
