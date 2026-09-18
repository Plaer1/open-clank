"""Shared containment, environment, redaction, and audit policy for shell tools."""

from __future__ import annotations

import asyncio
import functools
import hashlib
import json
import ntpath
import os
import re
import shlex
import shutil
import subprocess
import tempfile
import threading
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Mapping, Optional, Sequence


class ShellContainmentError(RuntimeError):
    pass


class ShellApprovalError(RuntimeError):
    pass


_ENV_ALLOW = {
    "PATH",
    "LANG",
    "LANGUAGE",
    "TERM",
    "COLORTERM",
    "COLUMNS",
    "LINES",
    "TMPDIR",
    "TMP",
    "TEMP",
    "SYSTEMROOT",
    "WINDIR",
    "COMSPEC",
    "PATHEXT",
    "PYTHONIOENCODING",
}
_SECRET_NAME = re.compile(
    r"(?:KEY|TOKEN|SECRET|PASSWORD|PASSWD|CREDENTIAL|AUTH|COOKIE|PRIVATE)",
    re.IGNORECASE,
)
_PEM = re.compile(
    r"-----BEGIN [A-Z0-9 ]+-----[\s\S]*?-----END [A-Z0-9 ]+-----",
)
_PEM_BEGIN = re.compile(r"-----BEGIN [A-Z0-9 ]+-----")
_PEM_END = re.compile(r"-----END [A-Z0-9 ]+-----")
_REDACTIONS = (
    (re.compile(r"\b(Bearer|Token)\s+[A-Za-z0-9._\-+/=]{12,}", re.I), r"\1 <redacted>"),
    (
        re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}"),
        "<redacted-jwt>",
    ),
    (re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b"), "<redacted-aws-key>"),
    (re.compile(r"\bgh[pousr]_[A-Za-z0-9]{20,}\b"), "<redacted-github-token>"),
    (re.compile(r"\bsk-ant-[A-Za-z0-9_-]{16,}\b"), "<redacted-api-key>"),
    (re.compile(r"\bsk-[A-Za-z0-9_-]{16,}\b"), "<redacted-api-key>"),
    (re.compile(r"\bxox[abprs]-[A-Za-z0-9-]{10,}\b"), "<redacted-slack-token>"),
    (
        re.compile(
            r"""(?ix)
            \b([A-Z0-9_.-]*(?:KEY|TOKEN|SECRET|PASSWORD|PASSWD|CREDENTIAL|AUTH|COOKIE)
            [A-Z0-9_.-]*)(\s*[:=]\s*)(["']?)[^\s"',;]{4,}\3
            """
        ),
        r"\1\2<redacted>",
    ),
)
_SHELL_PUNCTUATION = ";&|()<>\n"
_COMMAND_BOUNDARY_CHARS = ";&|()\n"
_COMMAND_PREFIXES = {"!", "{", "}", "if", "then", "elif", "else", "do", "while", "until"}
_INDIRECT_COMMANDS = {
    "alias",
    "builtin",
    "busybox",
    "busybox.exe",
    "chroot",
    "chrt",
    "command",
    "env",
    "exec",
    "fakeroot",
    "ionice",
    "nice",
    "nohup",
    "setsid",
    "stdbuf",
    "systemd-run",
    "taskset",
    "time",
    "timeout",
    "unshare",
    "watch",
    "xargs",
}
_SHELL_INTERPRETERS = {
    "bash",
    "cmd",
    "cmd.exe",
    "dash",
    "fish",
    "ksh",
    "powershell",
    "powershell.exe",
    "pwsh",
    "sh",
    "zsh",
}
_CODE_EVAL_FLAGS = {
    "node": {"-e", "--eval"},
    "node.exe": {"-e", "--eval"},
    "nodejs": {"-e", "--eval"},
    "bun": {"-e", "--eval"},
    "bun.exe": {"-e", "--eval"},
    "deno": {"eval"},
    "deno.exe": {"eval"},
    "perl": {"-e", "-E"},
    "php": {"-r"},
    "python": {"-c"},
    "python3": {"-c"},
    "ruby": {"-e"},
}
_INTERPRETER_INSPECTION_FLAGS = {
    "--help",
    "--version",
}
_SCRIPT_SUFFIXES = {
    ".bash",
    ".bat",
    ".cmd",
    ".cjs",
    ".js",
    ".mjs",
    ".php",
    ".pl",
    ".ps1",
    ".py",
    ".rb",
    ".sh",
    ".zsh",
}
_FILESYSTEM_MUTATORS = {
    "add-content",
    "chmod",
    "chown",
    "chgrp",
    "copy-item",
    "setfacl",
    "move-item",
    "new-item",
    "out-file",
    "rename-item",
    "set-content",
    "takeown",
    "icacls",
}
_REMOTE_CONTROL_COMMANDS = {
    "crictl",
    "ctr",
    "docker",
    "kubectl",
    "machinectl",
    "nerdctl",
    "podman",
    "virsh",
}
_PERSISTENCE_COMMANDS = {
    "at",
    "batch",
    "crontab",
    "launchctl",
    "schtasks",
}
_CREDENTIAL_COMMANDS = {
    "chpasswd",
    "gcloud",
    "gh",
    "git-credential",
    "htpasswd",
    "kinit",
    "npm",
    "passwd",
    "security",
}
_NETWORK_COMMANDS = {
    "curl",
    "ftp",
    "nc",
    "ncat",
    "netcat",
    "rsync",
    "scp",
    "sftp",
    "socat",
    "ssh",
    "telnet",
    "wget",
}
_PACKAGE_COMMANDS = {
    "apk",
    "apt",
    "apt-get",
    "brew",
    "cargo",
    "choco",
    "dnf",
    "dotnet",
    "gem",
    "go",
    "npm",
    "pacman",
    "pip",
    "pip3",
    "pipx",
    "pnpm",
    "poetry",
    "scoop",
    "uv",
    "winget",
    "yarn",
    "yum",
    "zypper",
}
_PACKAGE_MUTATIONS = {
    "add",
    "build",
    "develop",
    "global",
    "install",
    "link",
    "publish",
    "remove",
    "sync",
    "uninstall",
    "unlink",
    "update",
    "upgrade",
}
_SERVICE_COMMANDS = {
    "launchctl",
    "rc-service",
    "sc",
    "service",
    "systemctl",
}
_SERVICE_MUTATIONS = {
    "add-wants",
    "daemon-reexec",
    "daemon-reload",
    "delete",
    "disable",
    "edit",
    "enable",
    "kill",
    "link",
    "mask",
    "preset",
    "reload",
    "restart",
    "revert",
    "set-default",
    "start",
    "stop",
    "unmask",
}
_ASSIGNMENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*\+?=")
_AUDIT_MAX_BYTES = 2 * 1024 * 1024
_IS_WINDOWS = os.name == "nt"
SHELL_ENVIRONMENT_CLASS = "minimal-v1"
_SHELL_APPROVAL_PERMISSION = "native-shell-destructive"


@dataclass(frozen=True)
class _PendingShellApproval:
    request_id: str
    owner: str
    session_id: str
    workspace: str
    authority_workspace_id: str
    binding: str
    future: asyncio.Future[str]


_PENDING_SHELL_APPROVALS: dict[str, _PendingShellApproval] = {}
_PENDING_SHELL_APPROVALS_LOCK = threading.Lock()


@dataclass(frozen=True)
class _PendingSudoPassword:
    request_id: str
    owner: str
    session_id: str
    binding: str
    future: asyncio.Future[tuple[str, str]]


_PENDING_SUDO_PASSWORDS: dict[str, _PendingSudoPassword] = {}
_PENDING_SUDO_PASSWORDS_LOCK = threading.Lock()
# binding -> (secret, expiry_epoch). Single-use: popped by the bash handler when
# it wires the askpass environment, and never written to logs, specs, or audit.
_SUDO_SECRETS: dict[str, tuple[str, float]] = {}
_SUDO_SECRETS_LOCK = threading.Lock()
_SUDO_SECRET_TTL_S = 300.0


def minimal_shell_env(
    source: Optional[Mapping[str, str]] = None,
    *,
    cwd: Optional[str] = None,
    extra: Optional[Mapping[str, str]] = None,
) -> dict[str, str]:
    """Build a small child environment without inheriting ambient credentials."""
    merged = dict(source or os.environ)
    if extra:
        merged.update({str(key): str(value) for key, value in extra.items()})
    env = {
        str(name): str(value)
        for name, value in merged.items()
        if value is not None and (name.upper() in _ENV_ALLOW or name.upper().startswith("LC_"))
    }
    env.setdefault("PATH", os.defpath)
    env.setdefault("TERM", "xterm-256color")
    env.setdefault("COLUMNS", "120")
    env.setdefault("LINES", "40")
    env.setdefault("LANG", "C.UTF-8")
    if os.name == "nt":
        env.setdefault("PYTHONIOENCODING", "utf-8")
    if cwd:
        env["HOME"] = os.path.realpath(cwd)
        if os.name == "nt":
            env["USERPROFILE"] = os.path.realpath(cwd)
    return env


def redact_text(value: object, *, source_env: Optional[Mapping[str, str]] = None) -> str:
    """Redact secret shapes and exact secret environment values before a sink."""
    text = str(value or "")
    text = _PEM.sub("<redacted-pem-block>", text)
    for pattern, replacement in _REDACTIONS:
        text = pattern.sub(replacement, text)
    for name, secret in (source_env or os.environ).items():
        if not _SECRET_NAME.search(str(name)) or not secret or len(str(secret)) < 4:
            continue
        text = text.replace(str(secret), "<redacted>")
    return text


class StreamingRedactor:
    """Redact chunked process output without leaking split tokens or PEM bodies."""

    def __init__(
        self,
        *,
        source_env: Optional[Mapping[str, str]] = None,
        holdback: int = 512,
    ):
        self._source_env = source_env
        self._holdback = max(0, holdback)
        self._max_buffer = max(64 * 1024, self._holdback * 2)
        self._pending = ""
        self._in_pem = False
        self._dropping_long_token = False

    def feed(self, value: object, *, final: bool = False) -> str:
        self._pending += str(value or "")
        output: list[str] = []
        while self._pending:
            if self._dropping_long_token:
                boundary = re.search(r"[ \t\r\n]", self._pending)
                if boundary is None:
                    self._pending = ""
                    if final:
                        self._dropping_long_token = False
                    break
                self._pending = self._pending[boundary.start():]
                self._dropping_long_token = False
                continue

            if self._in_pem:
                end = _PEM_END.search(self._pending)
                if end is None:
                    if final:
                        self._pending = ""
                        self._in_pem = False
                    elif len(self._pending) > 128:
                        self._pending = self._pending[-128:]
                    break
                self._pending = self._pending[end.end():]
                self._in_pem = False
                continue

            begin = _PEM_BEGIN.search(self._pending)
            if begin is not None:
                output.append(
                    redact_text(
                        self._pending[:begin.start()],
                        source_env=self._source_env,
                    )
                )
                output.append("<redacted-pem-block>")
                self._pending = self._pending[begin.end():]
                self._in_pem = True
                continue

            marker = "-----BEGIN "
            partial = self._pending.rfind("-----")
            if partial >= 0:
                fragment = self._pending[partial:]
                if marker.startswith(fragment) or fragment.startswith(marker):
                    output.append(
                        redact_text(
                            self._pending[:partial],
                            source_env=self._source_env,
                        )
                    )
                    self._pending = fragment
                    if final:
                        output.append("<redacted-pem-block>")
                        self._pending = ""
                    elif len(self._pending) > self._max_buffer:
                        output.append("<redacted-pem-block>")
                        self._pending = self._pending[-128:]
                        self._in_pem = True
                    break

            if final:
                output.append(redact_text(self._pending, source_env=self._source_env))
                self._pending = ""
                break
            target = len(self._pending) - self._holdback
            if target <= 0:
                break
            split = max(
                self._pending.rfind(char, 0, target + 1)
                for char in (" ", "\t", "\r", "\n")
            )
            if split < 0:
                if len(self._pending) > self._max_buffer:
                    output.append("<redacted-long-token>")
                    self._pending = ""
                    self._dropping_long_token = True
                break
            split += 1
            output.append(
                redact_text(self._pending[:split], source_env=self._source_env)
            )
            self._pending = self._pending[split:]
        return "".join(output)

    def finish(self) -> str:
        return self.feed("", final=True)


def _active_dynamic_shell(source: str) -> bool:
    """Detect execution-bearing expansions while respecting shell quotes."""
    quote = ""
    escaped = False
    index = 0
    while index < len(source):
        char = source[index]
        if escaped:
            escaped = False
            index += 1
            continue
        if quote == "'":
            if char == "'":
                quote = ""
            index += 1
            continue
        if char == "\\":
            escaped = True
            index += 1
            continue
        if char in {"'", '"'}:
            quote = char if not quote else ("" if quote == char else quote)
            index += 1
            continue
        if char == "`":
            return True
        if index + 1 < len(source) and (
            (char == "$" and source[index + 1] == "(")
            or (not quote and char in "<>" and source[index + 1] == "(")
        ):
            return True
        index += 1
    return False


def _shell_tokens(command: str) -> list[str]:
    lexer = shlex.shlex(
        command,
        posix=True,
        punctuation_chars=_SHELL_PUNCTUATION,
    )
    # Preserve newlines as command boundaries, including inside quoted words.
    lexer.whitespace = " \t\r"
    lexer.whitespace_split = True
    return list(lexer)


def _simple_commands(tokens: Sequence[str]) -> Iterable[list[str]]:
    words: list[str] = []
    for token in tokens:
        if (
            token
            and all(char in _SHELL_PUNCTUATION for char in token)
            and any(char in _COMMAND_BOUNDARY_CHARS for char in token)
        ):
            if words:
                yield words
                words = []
            continue
        if token in {"then", "elif", "else", "do"}:
            if words:
                yield words
                words = []
            continue
        words.append(token)
    if words:
        yield words


def _command_index(words: Sequence[str]) -> Optional[int]:
    index = 0
    while index < len(words):
        word = words[index]
        if word in _COMMAND_PREFIXES or _ASSIGNMENT.match(word):
            index += 1
            continue
        if word and all(char in "<>" for char in word):
            index += 2
            continue
        return index
    return None


def _program_name(word: str) -> str:
    normalized = word.replace("\\", "/").rstrip("/")
    return normalized.rsplit("/", 1)[-1].lower()


def _has_force_flag(arguments: Sequence[str]) -> bool:
    return any(
        argument in {"-f", "--force", "--force-with-lease"}
        or (
            argument.startswith("-")
            and not argument.startswith("--")
            and "f" in argument[1:]
        )
        for argument in arguments
    )


def _is_code_interpreter(program: str) -> bool:
    return (
        program in _CODE_EVAL_FLAGS
        or re.fullmatch(
            r"(?:python|pypy|node|nodejs|bun|deno|perl|php|ruby)\d*(?:\.\d+)*(?:\.exe)?",
            program,
        ) is not None
    )


def _is_shell_interpreter(program: str) -> bool:
    return (
        program in _SHELL_INTERPRETERS
        or re.fullmatch(
            r"(?:bash|dash|fish|ksh|zsh)\d*(?:\.\d+)*(?:\.exe)?",
            program,
        ) is not None
    )


def _inspection_only(program: str, arguments: Sequence[str]) -> bool:
    allowed = set(_INTERPRETER_INSPECTION_FLAGS)
    if re.fullmatch(r"(?:python|pypy)\d*(?:\.\d+)*(?:\.exe)?", program):
        allowed.add("-V")
    return bool(arguments) and all(argument in allowed for argument in arguments)


def _opaque_executable(command_word: str) -> bool:
    normalized = command_word.replace("\\", "/")
    lowered = normalized.lower()
    if any(lowered.endswith(suffix) for suffix in _SCRIPT_SUFFIXES):
        return True
    if "/" not in normalized:
        return False
    if normalized.startswith(("/bin/", "/sbin/", "/usr/bin/", "/usr/sbin/")):
        return False
    return True


def _classify_simple_command(words: Sequence[str]) -> set[str]:
    actions: set[str] = set()
    index = _command_index(words)
    if index is None:
        return actions
    command_word = words[index]
    program = _program_name(command_word)
    arguments = list(words[index + 1:])
    lowered_arguments = [argument.lower() for argument in arguments]

    # A computed/globbed command name is opaque to static classification.
    if any(char in command_word for char in "$`*?[{") or command_word.startswith("~"):
        actions.add("dynamic-shell")
        return actions

    if program in {
        "rm",
        "rmdir",
        "unlink",
        "shred",
        "del",
        "erase",
        "rd",
        "remove-item",
        "ri",
    }:
        actions.add("remove")
    if program in {"clear-content", "truncate"}:
        actions.add("truncate-file")
    if program == "dd" and any(argument.startswith("of=") for argument in arguments):
        actions.add("overwrite")
    if program.startswith("mkfs") or program in {
        "format",
        "format-volume",
        "wipefs",
        "clear-disk",
    }:
        actions.add("filesystem-format")

    if program in {"sudo", "doas", "pkexec", "su"}:
        actions.add("privilege")
    if program in {"eval", "source", ".", "trap"}:
        actions.add("dynamic-shell")
    if program in _INDIRECT_COMMANDS:
        actions.add("indirect-command")
    if _is_shell_interpreter(program) and not _inspection_only(program, arguments):
        actions.add("dynamic-shell")
    if _is_code_interpreter(program) and not _inspection_only(program, arguments):
        actions.add("dynamic-code")
    if _opaque_executable(command_word):
        actions.add("indirect-command")
    if program in _FILESYSTEM_MUTATORS:
        actions.add("filesystem-metadata")
    if program in {"cp", "mv", "install", "patch", "tee", "unzip"}:
        actions.add("overwrite")
    if program == "ln" and _has_force_flag(arguments):
        actions.add("overwrite")
    if program == "sed" and any(
        argument == "--in-place" or argument.startswith("-i")
        for argument in arguments
    ):
        actions.add("overwrite")
    if program in {"tar", "bsdtar", "gtar"} and any(
        argument in {"--extract", "--get"}
        or (
            argument.startswith("-")
            and not argument.startswith("--")
            and "x" in argument[1:]
        )
        for argument in arguments
    ):
        actions.add("archive-extract")
    if program == "git" and "apply" in lowered_arguments:
        actions.add("overwrite")
    if any(">" in argument for argument in arguments):
        actions.add("overwrite")
    if program in _REMOTE_CONTROL_COMMANDS:
        actions.add("remote-control")
    if program in _PERSISTENCE_COMMANDS:
        actions.add("persistence")
    if program in _NETWORK_COMMANDS:
        actions.add("network")
    if program in _PACKAGE_COMMANDS and any(
        argument in _PACKAGE_MUTATIONS for argument in lowered_arguments
    ):
        actions.add("package")
    if program in _SERVICE_COMMANDS and any(
        argument in _SERVICE_MUTATIONS for argument in lowered_arguments
    ):
        actions.add("service")
    if (
        program in {"passwd", "chpasswd", "htpasswd", "git-credential", "kinit"}
        or (program == "gh" and "auth" in lowered_arguments)
        or (program == "gcloud" and "auth" in lowered_arguments)
        or (program == "npm" and any(argument in {"login", "logout", "token"} for argument in lowered_arguments))
        or (program == "security" and any(argument in {"add-generic-password", "delete-generic-password"} for argument in lowered_arguments))
    ):
        actions.add("credential")
    if (
        (program in {"npm", "pnpm", "yarn"} and any(argument in {"exec", "dlx"} for argument in lowered_arguments))
        or (program in {"uv", "pipx"} and "run" in lowered_arguments)
    ):
        actions.add("indirect-command")

    if program == "find":
        if "-delete" in arguments:
            actions.add("remove")
        if any(argument in {"-exec", "-execdir", "-ok", "-okdir"} for argument in arguments):
            actions.add("indirect-command")

    if program == "git":
        # Dynamic git subcommands/flags may resolve to a destructive alias or
        # hide a canonical destructive option.
        if any("$" in argument or "`" in argument for argument in arguments):
            actions.add("dynamic-shell")
        lowered = lowered_arguments
        if "reset" in lowered and "--hard" in lowered:
            actions.add("git-reset-hard")
        if "clean" in lowered and _has_force_flag(lowered):
            actions.add("git-clean")
        if "push" in lowered and _has_force_flag(lowered):
            actions.add("git-force")
        if "branch" in lowered and any(
            argument in {"-D", "--delete"} for argument in arguments
        ):
            actions.add("git-branch")
        if "tag" in lowered and any(
            argument in {"-d", "--delete"} for argument in arguments
        ):
            actions.add("git-tag")
        if "worktree" in lowered and "remove" in lowered:
            actions.add("git-worktree")
        if "stash" in lowered and any(
            argument in {"drop", "clear"} for argument in arguments
        ):
            actions.add("git-stash")
        if "checkout" in lowered:
            actions.add("git-checkout")
        if "restore" in lowered:
            actions.add("git-restore")
        if "rebase" in lowered:
            actions.add("git-rebase")
        if "cherry-pick" in lowered:
            actions.add("git-cherry-pick")
        if "revert" in lowered:
            actions.add("git-revert")
        if "switch" in lowered and _has_force_flag(lowered):
            actions.add("git-switch-force")
        if "commit" in lowered and "--amend" in lowered:
            actions.add("git-commit-amend")

    return actions


def destructive_actions(command: str) -> list[str]:
    """Classify approval-worthy shell actions without executing expansions.

    Shell quoting and escaping are normalized by ``shlex``. Execution-bearing
    substitutions and indirect evaluators escalate conservatively instead of
    being guessed safe.
    """
    source = normalize_shell_command(command)
    actions: set[str] = set()
    if _active_dynamic_shell(source):
        actions.add("dynamic-shell")
    try:
        for words in _simple_commands(_shell_tokens(source)):
            actions.update(_classify_simple_command(words))
    except ValueError:
        actions.add("opaque-shell-syntax")
    return sorted(actions)


def command_needs_sudo_password(command: str) -> bool:
    """True when an approved command invokes ``sudo`` in command position.

    Uses the same shlex-based tokenization as the policy classifier so quoted
    text (``echo "sudo x"``) does not trigger a spurious password prompt.
    """
    if _IS_WINDOWS:
        return False
    try:
        for words in _simple_commands(_shell_tokens(normalize_shell_command(command))):
            index = _command_index(words)
            if index is not None and _program_name(words[index]) == "sudo":
                return True
    except ValueError:
        return False
    return False


def normalize_shell_command(command: str) -> str:
    """Canonicalize transport-only differences without changing shell meaning."""
    return str(command or "").replace("\r\n", "\n").replace("\r", "\n").strip()


def _static_positionals(
    arguments: Sequence[str],
    *,
    value_options: Sequence[str] = (),
) -> list[str]:
    values: list[str] = []
    options_done = False
    skip_next = False
    value_options = tuple(value_options)
    for argument in arguments:
        if skip_next:
            skip_next = False
            continue
        if not options_done and argument == "--":
            options_done = True
            continue
        if not options_done and argument in value_options:
            skip_next = True
            continue
        if not options_done and any(
            argument.startswith(f"{option}=") for option in value_options
        ):
            continue
        if not options_done and argument.startswith("-"):
            continue
        values.append(argument)
    return values


def _named_option_values(
    arguments: Sequence[str],
    names: Sequence[str],
) -> list[str]:
    values: list[str] = []
    lowered_names = {name.lower() for name in names}
    for index, argument in enumerate(arguments):
        lowered = argument.lower()
        if lowered in lowered_names and index + 1 < len(arguments):
            values.append(arguments[index + 1])
            continue
        for name in lowered_names:
            prefix = f"{name}="
            if lowered.startswith(prefix):
                values.append(argument[len(prefix):])
    return values


def _simple_target_specs(words: Sequence[str]) -> list[tuple[str, str]]:
    index = _command_index(words)
    if index is None:
        return []
    program = _program_name(words[index])
    arguments = list(words[index + 1:])
    lowered = [argument.lower() for argument in arguments]
    specs: list[tuple[str, str]] = []

    def add(role: str, values: Iterable[str]) -> None:
        specs.extend(
            (role, value)
            for value in values
            if value and value != "-"
        )

    removers = {
        "rm", "rmdir", "unlink", "shred", "del", "erase", "rd",
        "remove-item", "ri",
    }
    if program in removers:
        add("remove_target", _static_positionals(arguments))
        add(
            "remove_target",
            _named_option_values(arguments, ("-path", "-literalpath")),
        )
    elif program in {"clear-content", "truncate"}:
        add(
            "truncate_target",
            _static_positionals(
                arguments,
                value_options=("-s", "--size", "-o", "--io-blocks"),
            ),
        )
        add(
            "truncate_target",
            _named_option_values(arguments, ("-path", "-literalpath")),
        )

    if program == "dd":
        add(
            "overwrite_destination",
            (
                argument[3:]
                for argument in arguments
                if argument.startswith("of=")
            ),
        )

    if program.startswith("mkfs") or program in {
        "format", "format-volume", "wipefs", "clear-disk",
    }:
        add("format_target", _static_positionals(arguments))
        add(
            "format_target",
            _named_option_values(arguments, ("-driveletter", "-number")),
        )

    if program in {"cp", "mv", "install"}:
        positionals = _static_positionals(
            arguments,
            value_options=("-t", "--target-directory"),
        )
        explicit_destinations = _named_option_values(
            arguments,
            ("-t", "--target-directory"),
        )
        if explicit_destinations:
            add("overwrite_destination", explicit_destinations)
            add("move_source" if program == "mv" else "copy_source", positionals)
        elif positionals:
            add(
                "move_source" if program == "mv" else "copy_source",
                positionals[:-1],
            )
            add("overwrite_destination", positionals[-1:])

    if program in {"copy-item", "move-item", "rename-item"}:
        add(
            "overwrite_destination",
            _named_option_values(arguments, ("-destination", "-newname")),
        )
        add(
            "move_source" if program != "copy-item" else "copy_source",
            _named_option_values(arguments, ("-path", "-literalpath")),
        )

    if program == "ln" and _has_force_flag(arguments):
        positionals = _static_positionals(
            arguments,
            value_options=("-t", "--target-directory"),
        )
        destinations = _named_option_values(
            arguments,
            ("-t", "--target-directory"),
        )
        add("overwrite_destination", destinations or positionals[-1:])

    if program == "sed" and any(
        argument == "--in-place" or argument.startswith("-i")
        for argument in arguments
    ):
        positionals = _static_positionals(
            arguments,
            value_options=("-e", "--expression", "-f", "--file"),
        )
        add("overwrite_destination", positionals[-1:])

    if program in {"tar", "bsdtar", "gtar", "unzip"} and (
        program == "unzip"
        or "--extract" in arguments
        or "--get" in arguments
        or any(
            argument.startswith("-")
            and not argument.startswith("--")
            and "x" in argument[1:]
            for argument in arguments
        )
    ):
        destination = _named_option_values(
            arguments,
            ("-C", "--directory", "-d"),
        )
        add("archive_destination", destination or ["."])

    if program in _FILESYSTEM_MUTATORS:
        paths = _named_option_values(
            arguments,
            ("-path", "-literalpath", "-destination"),
        )
        if not paths:
            positionals = _static_positionals(arguments)
            paths = positionals[1:] if program in {
                "chmod", "chown", "chgrp", "setfacl", "takeown", "icacls",
            } else positionals
        add("metadata_target", paths)

    if program in {"patch", "tee"}:
        add("overwrite_destination", _static_positionals(arguments))

    if program == "find" and "-delete" in arguments:
        roots = []
        for argument in arguments:
            if argument.startswith("-") or argument in {"(", ")", "!"}:
                break
            roots.append(argument)
        add("remove_search_root", roots or ["."])

    if program == "git":
        add("repository", _named_option_values(arguments, ("-C",)))
        if "worktree" in lowered and "remove" in lowered:
            remove_index = lowered.index("remove")
            add(
                "remove_target",
                _static_positionals(arguments[remove_index + 1:]),
            )
        if "--" in arguments and (
            "checkout" in lowered or "restore" in lowered
        ):
            add(
                "overwrite_destination",
                arguments[arguments.index("--") + 1:],
            )
        if any(
            action in lowered
            for action in ("clean", "reset", "rebase", "cherry-pick", "revert")
        ):
            add("repository", ["."])

    for offset, argument in enumerate(words):
        if argument and all(char == ">" for char in argument):
            if offset + 1 < len(words):
                add("overwrite_destination", [words[offset + 1]])

    return specs


def _target_path_fact(
    argument: str,
    *,
    role: str,
    cwd: str,
    workspace: Optional[str],
) -> dict[str, object]:
    dynamic = any(char in argument for char in "$`*?[{") or argument.startswith("~")
    if dynamic:
        return {
            "argument": argument,
            "role": role,
            "resolved": False,
            "reason": "dynamic_or_glob_path",
        }

    path = argument
    if not os.path.isabs(path):
        path = os.path.join(cwd, path)
    path = os.path.abspath(path)
    real_path = os.path.realpath(path)
    parent = os.path.dirname(path) or path
    parent_real = os.path.realpath(parent)
    exists = os.path.lexists(path)
    is_symlink = os.path.islink(path)
    stat_result = None
    if exists:
        try:
            stat_result = os.lstat(path)
        except OSError:
            stat_result = None
    within_workspace = None
    if workspace:
        try:
            within_workspace = (
                os.path.commonpath([os.path.realpath(workspace), real_path])
                == os.path.realpath(workspace)
            )
        except ValueError:
            within_workspace = False
    destination = role in {
        "archive_destination",
        "overwrite_destination",
    }
    fact: dict[str, object] = {
        "argument": argument,
        "role": role,
        "resolved": True,
        "path": path,
        "real_path": real_path,
        "exists": exists,
        "is_symlink": is_symlink,
        "traverses_symlink": real_path != path,
        "is_file": os.path.isfile(path),
        "is_directory": os.path.isdir(path),
        "parent_path": parent,
        "parent_real_path": parent_real,
        "parent_exists": os.path.isdir(parent),
        "parent_writable": os.access(parent, os.W_OK),
        "within_workspace": within_workspace,
        "would_clobber": bool(destination and exists),
        "no_clobber": bool(destination and not exists),
    }
    if is_symlink:
        try:
            fact["symlink_target"] = os.readlink(path)
        except OSError:
            fact["symlink_target"] = None
    if stat_result is not None:
        fact.update({
            "owner_uid": int(getattr(stat_result, "st_uid", 0)),
            "owner_gid": int(getattr(stat_result, "st_gid", 0)),
            "mode": format(stat_result.st_mode & 0o7777, "04o"),
            "size": int(stat_result.st_size),
            "device": int(stat_result.st_dev),
            "inode": int(stat_result.st_ino),
        })
    return fact


def destructive_target_facts(
    command: str,
    *,
    cwd: str,
    workspace: Optional[str] = None,
) -> list[dict[str, object]]:
    """Resolve static filesystem targets without executing shell expansion."""
    source = normalize_shell_command(command)
    if _active_dynamic_shell(source):
        return []
    specs: list[tuple[str, str]] = []
    try:
        for words in _simple_commands(_shell_tokens(source)):
            specs.extend(_simple_target_specs(words))
    except ValueError:
        return []
    facts: list[dict[str, object]] = []
    seen: set[tuple[str, str]] = set()
    for role, argument in specs:
        key = (role, argument)
        if key in seen:
            continue
        seen.add(key)
        facts.append(
            _target_path_fact(
                argument,
                role=role,
                cwd=os.path.realpath(cwd),
                workspace=workspace,
            )
        )
    return facts


def shell_approval_binding(
    command: str,
    *,
    cwd: str,
    containment: str,
    network: str = "enabled",
    environment_class: str = SHELL_ENVIRONMENT_CLASS,
    workspace: Optional[str] = None,
    target_facts: Optional[Sequence[Mapping[str, object]]] = None,
) -> str:
    """Bind an approval to the exact normalized execution tuple."""
    facts = list(target_facts) if target_facts is not None else destructive_target_facts(
        command,
        cwd=cwd,
        workspace=workspace,
    )
    descriptor = {
        "command": normalize_shell_command(command),
        "cwd": os.path.realpath(cwd),
        "environment_class": str(environment_class),
        "containment": str(containment),
        "network": network_policy(network),
        "target_facts": facts,
    }
    payload = json.dumps(
        descriptor,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


@functools.lru_cache(maxsize=1)
def _approval_grant_store():
    from src.constants import DATA_DIR
    from src.openclank.permission_grants import GrantStore

    data_dir = Path(DATA_DIR)
    data_dir.mkdir(parents=True, exist_ok=True)
    return GrantStore(str(data_dir / "app.db"))


async def _require_shell_approval_grant(
    command: str,
    *,
    ctx: Mapping[str, object],
    cwd: str,
    containment: str,
    network: str = "enabled",
) -> str:
    """Ask before a destructive native command and return its bound grant hash.

    The trusted request context supplies owner/session/workspace. The model only
    supplies the command, so it cannot mint or transplant an approval token.
    """
    owner = str(ctx.get("owner") or "")
    session_id = str(ctx.get("session_id") or "")
    authority_workspace_id = str(
        ctx.get("authority_workspace_id") or ""
    ).strip()
    workspace = os.path.realpath(str(ctx.get("workspace") or cwd))
    actions = destructive_actions(command)
    target_facts = destructive_target_facts(
        command,
        cwd=cwd,
        workspace=workspace,
    )
    binding = shell_approval_binding(
        command,
        cwd=cwd,
        containment=containment,
        network=network,
        workspace=workspace,
        target_facts=target_facts,
    )
    if not actions:
        return binding

    if not session_id or not workspace:
        raise ShellApprovalError(
            "destructive shell commands require a trusted session and workspace"
        )
    active_cwd = os.path.realpath(cwd)
    try:
        cwd_is_scoped = os.path.commonpath([workspace, active_cwd]) == workspace
    except ValueError:
        cwd_is_scoped = False
    if not cwd_is_scoped:
        raise ShellApprovalError(
            "destructive shell approval cwd is outside the trusted workspace"
        )

    def audit(event: str) -> None:
        append_shell_audit(
            command=command,
            owner=owner,
            session_id=session_id,
            workspace=workspace,
            containment=containment,
            network=network,
            actions=actions,
            event=event,
        )

    try:
        from src.openclank.operation_approvals import match_operation_approval

        if match_operation_approval(
            owner=owner,
            permission_type=_SHELL_APPROVAL_PERMISSION,
            resource=binding,
            session_id=session_id,
            workspace_id=authority_workspace_id,
            workspace_path=workspace,
        ):
            audit("approval_reused")
            return binding
    except Exception:
        pass
    try:
        if _approval_grant_store().match(
            _SHELL_APPROVAL_PERMISSION,
            owner=owner,
            session_id=session_id,
            workspace=workspace,
            workspace_id=authority_workspace_id,
            resource=binding,
        ):
            audit("approval_reused")
            return binding
    except Exception:
        # A broken durable store must never become an implicit approval.
        pass

    # Owner permission mode (yolo/auto): approve with once semantics — no
    # durable grant is written, but the audit record stands.
    try:
        from src.permission_mode import auto_approves

        if auto_approves(owner):
            audit("approval_auto_permission_mode")
            return binding
    except Exception:
        pass

    progress_cb = ctx.get("progress_cb")
    if not callable(progress_cb):
        audit("approval_unavailable")
        raise ShellApprovalError(
            "destructive shell command needs interactive approval"
        )

    loop = asyncio.get_running_loop()
    request_id = "shell_perm_" + uuid.uuid4().hex[:20]
    future: asyncio.Future[str] = loop.create_future()
    pending = _PendingShellApproval(
        request_id=request_id,
        owner=owner,
        session_id=session_id,
        workspace=workspace,
        authority_workspace_id=authority_workspace_id,
        binding=binding,
        future=future,
    )
    with _PENDING_SHELL_APPROVALS_LOCK:
        _PENDING_SHELL_APPROVALS[request_id] = pending
    payload = {
        "type": "permission_request",
        "data": {
            "request_id": request_id,
            "session_id": session_id,
            "permission_type": f"bash destructive command · exact {binding[:12]}",
            "detail": {
                "command": redact_text(normalize_shell_command(command)),
                "workdir": active_cwd,
                "environment_class": SHELL_ENVIRONMENT_CLASS,
                "containment": containment,
                "network": network_policy(network),
                "destructive_actions": actions,
                "target_facts": target_facts,
            },
            "options": ["once", "chat", "workspace", "always", "reject"],
            "always_pattern": "*",
        },
    }
    try:
        audit("approval_requested")
        await progress_cb(payload)
        option_id = await future
        if option_id in {"chat", "workspace", "always"}:
            try:
                from src.openclank.permission_grants import grant_scope_for_lifetime
                (
                    grant_session,
                    grant_workspace,
                    grant_workspace_id,
                ) = grant_scope_for_lifetime(
                    option_id,
                    session_id=session_id,
                    workspace=workspace,
                    workspace_id=authority_workspace_id,
                )
                if (
                    option_id == "always"
                    or grant_session
                    or grant_workspace
                    or grant_workspace_id
                ):
                    from src.openclank.operation_approvals import (
                        record_operation_approval,
                    )

                    persisted = record_operation_approval(
                        owner=owner,
                        permission_type=_SHELL_APPROVAL_PERMISSION,
                        pattern="*",
                        resource=binding,
                        lifetime=option_id,
                        session_id=grant_session,
                        workspace_id=grant_workspace_id,
                        target_path=workspace,
                    )
                    if not persisted:
                        _approval_grant_store().add(
                            _SHELL_APPROVAL_PERMISSION,
                            "*",
                            owner=owner,
                            session_id=grant_session,
                            workspace=grant_workspace,
                            workspace_id=grant_workspace_id,
                            resource=binding,
                        )
                else:
                    option_id = "once"
            except Exception as exc:
                raise ShellApprovalError(
                    "could not persist the bound shell approval"
                ) from exc
        if option_id not in {"once", "chat", "workspace", "always"}:
            audit("approval_rejected")
            raise ShellApprovalError("destructive shell command was rejected")
        audit("approval_granted")
        return binding
    finally:
        with _PENDING_SHELL_APPROVALS_LOCK:
            _PENDING_SHELL_APPROVALS.pop(request_id, None)


async def require_shell_approval(
    command: str,
    *,
    ctx: Mapping[str, object],
    cwd: str,
    containment: str,
    network: str = "enabled",
) -> str:
    """Approve a destructive command, then collect a sudo password if needed.

    The grant and the credential are separate prompts: the owner first approves
    the exact execution tuple, and only then is asked for the sudo password the
    approved command will need. Declining the password prompt is not a veto —
    the command still runs and sudo fails non-interactively, exactly as before.
    """
    binding = await _require_shell_approval_grant(
        command,
        ctx=ctx,
        cwd=cwd,
        containment=containment,
        network=network,
    )
    if command_needs_sudo_password(command):
        await _request_sudo_password(command, binding, ctx=ctx)
    return binding


async def _request_sudo_password(
    command: str,
    binding: str,
    *,
    ctx: Mapping[str, object],
) -> None:
    """Ask the owner for the sudo password and stash it single-use per binding.

    The secret lives only in ``_SUDO_SECRETS`` (TTL ``_SUDO_SECRET_TTL_S``) and
    is popped by the bash handler when it builds the askpass environment. It is
    never logged, audited, or written to the worker spec.
    """
    owner = str(ctx.get("owner") or "")
    session_id = str(ctx.get("session_id") or "")
    progress_cb = ctx.get("progress_cb")
    if not callable(progress_cb):
        return
    loop = asyncio.get_running_loop()
    request_id = "sudo_perm_" + uuid.uuid4().hex[:20]
    future: asyncio.Future[tuple[str, str]] = loop.create_future()
    pending = _PendingSudoPassword(
        request_id=request_id,
        owner=owner,
        session_id=session_id,
        binding=binding,
        future=future,
    )
    with _PENDING_SUDO_PASSWORDS_LOCK:
        _PENDING_SUDO_PASSWORDS[request_id] = pending
    payload = {
        "type": "sudo_password_request",
        "data": {
            "request_id": request_id,
            "session_id": session_id,
            "sudo_password": True,
            "permission_type": f"sudo password · exact {binding[:12]}",
            "detail": {
                "command": redact_text(normalize_shell_command(command)),
            },
            "options": ["once", "reject"],
        },
    }
    try:
        await progress_cb(payload)
        option_id, secret = await future
        if option_id == "once" and secret:
            with _SUDO_SECRETS_LOCK:
                _SUDO_SECRETS[binding] = (secret, time.time() + _SUDO_SECRET_TTL_S)
    finally:
        with _PENDING_SUDO_PASSWORDS_LOCK:
            _PENDING_SUDO_PASSWORDS.pop(request_id, None)


def resolve_sudo_password(
    request_id: str,
    option_id: str,
    *,
    secret: str = "",
    owner: str,
    session_id: str,
) -> bool:
    """Resolve only an exact pending owner/session sudo-password request."""
    if option_id not in {"once", "reject"}:
        return False
    with _PENDING_SUDO_PASSWORDS_LOCK:
        pending = _PENDING_SUDO_PASSWORDS.get(str(request_id))
        if (
            pending is None
            or pending.owner != str(owner or "")
            or pending.session_id != str(session_id or "")
            or pending.future.done()
        ):
            return False
        future = pending.future

    def finish() -> None:
        if not future.done():
            future.set_result((option_id, str(secret or "")))

    future.get_loop().call_soon_threadsafe(finish)
    return True


def pop_sudo_secret(binding: str) -> Optional[str]:
    """Consume the single-use sudo password stashed for an approval binding."""
    with _SUDO_SECRETS_LOCK:
        entry = _SUDO_SECRETS.pop(str(binding or ""), None)
    if entry is None:
        return None
    secret, expiry = entry
    if time.time() >= expiry:
        return None
    return secret


_SUDO_IN_POSITION = re.compile(r"(^|&&|\|\||[|;])(\s*)sudo(?=\s)")


def inject_sudo_askpass(command: str) -> str:
    """Rewrite command-position ``sudo`` to ``sudo -A`` so it reads SUDO_ASKPASS.

    Text-level rewrite (quoted ``sudo`` occurrences are rewritten too — harmless
    in practice because this only runs on commands that actually invoke sudo).
    Only applies when the worker environment carries SUDO_ASKPASS; the approval
    binding is always computed on the original command.
    """
    return _SUDO_IN_POSITION.sub(lambda m: f"{m.group(1)}{m.group(2)}sudo -A", command)


def resolve_shell_approval(
    request_id: str,
    option_id: str,
    *,
    owner: str,
    session_id: str,
) -> bool:
    """Resolve only an exact pending owner/session request."""
    if option_id not in {"once", "chat", "workspace", "always", "reject"}:
        return False
    with _PENDING_SHELL_APPROVALS_LOCK:
        pending = _PENDING_SHELL_APPROVALS.get(str(request_id))
        if (
            pending is None
            or pending.owner != str(owner or "")
            or pending.session_id != str(session_id or "")
            or pending.future.done()
        ):
            return False
        future = pending.future

    def finish() -> None:
        if not future.done():
            future.set_result(option_id)

    future.get_loop().call_soon_threadsafe(finish)
    return True


def reject_shell_approval_scope(
    *,
    owner: str,
    session_id: str = "",
    workspace: str = "",
    authority_workspace_id: str = "",
    all_pending: bool = False,
) -> int:
    """Reject pending destructive-shell approvals in one reset domain."""
    if (
        not session_id
        and not workspace
        and not authority_workspace_id
        and not all_pending
    ):
        return 0
    matches: list[asyncio.Future[str]] = []
    with _PENDING_SHELL_APPROVALS_LOCK:
        for pending in _PENDING_SHELL_APPROVALS.values():
            if pending.owner != str(owner or "") or pending.future.done():
                continue
            if all_pending:
                matches.append(pending.future)
                continue
            if session_id and pending.session_id != str(session_id):
                continue
            if workspace and authority_workspace_id:
                if (
                    pending.authority_workspace_id != authority_workspace_id
                    and pending.workspace != workspace
                ):
                    continue
            elif workspace and pending.workspace != workspace:
                continue
            elif (
                authority_workspace_id
                and pending.authority_workspace_id != authority_workspace_id
            ):
                continue
            matches.append(pending.future)
    for future in matches:
        future.get_loop().call_soon_threadsafe(
            lambda target=future: (
                None if target.done() else target.set_result("reject")
            )
        )
    return len(matches)


def sandbox_policy() -> str:
    value = str(os.getenv("OPEN_CLANK_SHELL_SANDBOX", "auto")).strip().lower()
    if value not in {"required", "auto", "off"}:
        raise ShellContainmentError(
            "OPEN_CLANK_SHELL_SANDBOX must be required, auto, or off"
        )
    return value


def network_policy(value: Optional[str] = None) -> str:
    policy = str(
        value
        if value is not None
        else os.getenv("OPEN_CLANK_SHELL_NETWORK", "enabled")
    ).strip().lower()
    if policy not in {"enabled", "disabled"}:
        raise ShellContainmentError(
            "shell network policy must be enabled or disabled"
        )
    return policy


def shell_command_argv(shell: str, command: str) -> list[str]:
    """Build argv for Bash, PowerShell, or cmd without conflating their flags."""
    name = ntpath.basename(shell).lower()
    if name in {"powershell", "powershell.exe", "pwsh", "pwsh.exe"}:
        return [
            shell,
            "-NoLogo",
            "-NoProfile",
            "-NonInteractive",
            "-Command",
            command,
        ]
    if name in {"cmd", "cmd.exe"}:
        return [shell, "/d", "/s", "/c", command]
    return [shell, "--noprofile", "--norc", "-c", command]


@functools.lru_cache(maxsize=1)
def _working_bwrap() -> Optional[str]:
    executable = shutil.which("bwrap")
    if not executable or os.name == "nt":
        return None
    try:
        result = subprocess.run(
            [
                executable,
                "--die-with-parent",
                "--ro-bind",
                "/",
                "/",
                "--proc",
                "/proc",
                "--dev",
                "/dev",
                "--",
                "/bin/true",
            ],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=5,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return executable if result.returncode == 0 else None


@functools.lru_cache(maxsize=1)
def _working_network_bwrap() -> Optional[str]:
    executable = _working_bwrap()
    if executable is None:
        return None
    try:
        result = subprocess.run(
            [
                executable,
                "--die-with-parent",
                "--unshare-net",
                "--ro-bind",
                "/",
                "/",
                "--proc",
                "/proc",
                "--dev",
                "/dev",
                "--",
                "/bin/true",
            ],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=5,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return executable if result.returncode == 0 else None


def _dir_mounts(parent: str, child: str) -> list[str]:
    """Create bwrap --dir arguments for missing parents below a tmpfs mount."""
    relative = os.path.relpath(child, parent)
    if relative in {".", ""} or relative.startswith(".."):
        return []
    current = parent
    result: list[str] = []
    for part in Path(relative).parts:
        current = os.path.join(current, part)
        result.extend(["--dir", current])
    return result


def _contains_path(parent: str, child: str) -> bool:
    try:
        return os.path.commonpath([parent, child]) == parent
    except ValueError:
        return False


def contained_parser_argv(
    argv: Sequence[str],
    *,
    input_descriptor: int,
    runtime_roots: Sequence[str] = (),
    input_destination: str = "/input/source",
) -> tuple[list[str], str]:
    """Wrap one parser process in the strict no-network import profile.

    Unlike the interactive shell sandbox, parser workers do not see the host
    root or workspace.  They receive only the system runtime, explicitly named
    dependency roots, and one immutable input descriptor.
    """
    if os.name == "nt" or input_descriptor < 0:
        raise ShellContainmentError("parser containment is unavailable")
    executable = _working_network_bwrap()
    if executable is None:
        raise ShellContainmentError("parser containment is unavailable")
    destination = os.path.abspath(input_destination)
    if not destination.startswith("/input/"):
        raise ShellContainmentError("parser input must be mounted below /input")

    roots: list[tuple[str, str]] = []
    # The contained worker executes the contract's pinned command hooks with
    # `shell=True`; expose the standard interpreter mount points explicitly.
    # `/bin` is often a symlink to `/usr/bin`, but bubblewrap does not recreate
    # that compatibility path unless it is mounted by name.
    for candidate in ("/usr", "/bin", "/sbin", "/lib", "/lib64", *runtime_roots):
        target = os.path.abspath(str(candidate or ""))
        if not target or not os.path.exists(target):
            continue
        source = os.path.realpath(target)
        if any(target == mounted_target for _, mounted_target in roots):
            continue
        roots.append((source, target))

    parent_dirs: set[str] = {"/input"}
    for _, target in roots:
        current = os.path.dirname(target)
        while current not in {"", "/"}:
            parent_dirs.add(current)
            current = os.path.dirname(current)
    current = os.path.dirname(destination)
    while current not in {"", "/"}:
        parent_dirs.add(current)
        current = os.path.dirname(current)

    args = [
        executable,
        "--die-with-parent",
        "--new-session",
        "--unshare-pid",
        "--unshare-ipc",
        "--unshare-uts",
        "--unshare-net",
        "--clearenv",
        "--proc",
        "/proc",
        "--dev",
        "/dev",
        "--tmpfs",
        "/tmp",
        "--tmpfs",
        "/run",
    ]
    for path in sorted(parent_dirs, key=lambda value: (value.count(os.sep), value)):
        args.extend(("--dir", path))
    for source, target in roots:
        args.extend(("--ro-bind", source, target))
    args.extend(
        (
            "--ro-bind-data",
            str(input_descriptor),
            destination,
            "--setenv",
            "HOME",
            "/tmp",
            "--setenv",
            "TMPDIR",
            "/tmp",
            "--setenv",
            "PATH",
            "/usr/bin",
            "--setenv",
            "LANG",
            "C.UTF-8",
            "--chdir",
            "/tmp",
            "--",
        )
    )
    args.extend(str(item) for item in argv)
    return args, "bwrap"


def contained_argv(
    argv: Sequence[str],
    *,
    workspace: str,
    cwd: Optional[str] = None,
    network: Optional[str] = None,
    owner: Optional[str] = None,
    project_id: Optional[str] = None,
    hex_target: Optional[str] = None,
    hex_db_path: Optional[str] = None,
    workspace_readonly: bool = False,
    workspace_overlay: Optional[tuple[str, str]] = None,
    readonly_data_mounts: Sequence[tuple[int, str]] = (),
) -> tuple[list[str], str]:
    """Wrap one invocation in a workspace-write-only bubblewrap sandbox."""
    if owner and project_id and hex_target:
        from src.constants import FM_DB_PATH
        from src.project_hex import require_hex_activation
        require_hex_activation(
            hex_target,
            owner=owner,
            project_id=project_id,
            db_path=hex_db_path or FM_DB_PATH,
            workspace_root=workspace,
        )
    policy = sandbox_policy()
    network_mode = network_policy(network)
    root = os.path.realpath(workspace)
    active_cwd = os.path.realpath(cwd or root)
    try:
        if os.path.commonpath([root, active_cwd]) != root:
            raise ShellContainmentError("shell cwd must stay inside the active workspace")
    except ValueError as exc:
        raise ShellContainmentError("shell cwd must stay inside the active workspace") from exc

    if workspace_overlay and workspace_readonly:
        raise ShellContainmentError("workspace overlay cannot also be read-only")
    overlay: Optional[tuple[str, str]] = None
    if workspace_overlay:
        if len(workspace_overlay) != 2:
            raise ShellContainmentError("workspace overlay requires upper and work directories")
        upper, work = (os.path.realpath(str(item)) for item in workspace_overlay)
        if not os.path.isdir(upper) or not os.path.isdir(work) or upper == work:
            raise ShellContainmentError("workspace overlay directories are unavailable")
        if _contains_path(root, upper) or _contains_path(root, work):
            raise ShellContainmentError("workspace overlay state must stay outside the workspace")
        overlay = (upper, work)

    if _IS_WINDOWS:
        if overlay:
            raise ShellContainmentError("workspace overlay requires bubblewrap containment")
        if readonly_data_mounts:
            raise ShellContainmentError(
                "verified file snapshots require bubblewrap containment"
            )
        if network_mode == "disabled":
            raise ShellContainmentError(
                "network-disabled shell execution requires bubblewrap"
            )
        if policy == "required":
            raise ShellContainmentError(
                "shell containment is unavailable on this platform and "
                "OPEN_CLANK_SHELL_SANDBOX=required forbids unsandboxed execution"
            )
        # auto/off: the 2026-08-14 owner ruling makes the OS account the
        # confinement boundary; containment is best-effort, never a gate.
        return list(argv), "off"
    if policy == "off":
        if overlay:
            raise ShellContainmentError("workspace overlay requires bubblewrap containment")
        if readonly_data_mounts:
            raise ShellContainmentError(
                "verified file snapshots require bubblewrap containment"
            )
        if network_mode == "disabled":
            raise ShellContainmentError(
                "network-disabled shell execution requires bubblewrap"
            )
        return list(argv), "off"
    executable = (
        _working_network_bwrap()
        if network_mode == "disabled"
        else _working_bwrap()
    )
    if executable is None:
        if network_mode == "disabled":
            raise ShellContainmentError(
                "network-disabled shell containment is unavailable"
            )
        if policy == "required":
            raise ShellContainmentError(
                "shell containment is unavailable and "
                "OPEN_CLANK_SHELL_SANDBOX=required forbids unsandboxed execution"
            )
        # auto: no bubblewrap on this host — run under the OS account boundary.
        return list(argv), "off"

    args = [
        executable,
        "--die-with-parent",
        "--new-session",
    ]
    if overlay:
        args.extend(["--unshare-user", "--uid", "0", "--gid", "0"])
    args.extend([
        "--unshare-pid",
        "--unshare-ipc",
        "--unshare-uts",
        "--ro-bind",
        "/",
        "/",
        "--proc",
        "/proc",
        "--dev",
        "/dev",
        "--tmpfs",
        "/tmp",
        "--tmpfs",
        "/run",
    ])
    if network_mode == "disabled":
        args.append("--unshare-net")
    from src.constants import AUTH_FILE, DATA_DIR, FM_DB_PATH

    protected: list[str] = []
    candidates = {
        DATA_DIR,
        os.path.dirname(AUTH_FILE),
        AUTH_FILE,
        FM_DB_PATH,
        f"{FM_DB_PATH}-wal",
        f"{FM_DB_PATH}-shm",
        f"{FM_DB_PATH}-journal",
        os.environ.get("MIMOCODE_HOME"),
        os.environ.get("OPEN_CLANK_CONTROL_DATA_DIR"),
        os.environ.get("OPEN_CLANK_SKILLS_DIR"),
    }
    for candidate in sorted({item for item in candidates if item}, key=len):
        lexical = os.path.abspath(candidate)
        resolved = os.path.realpath(candidate)
        if not os.path.exists(resolved):
            continue
        if not any(
            _contains_path(existing, resolved)
            for existing in protected
        ):
            protected.append(resolved)
    before_workspace = [
        candidate for candidate in protected
        if _contains_path(candidate, root)
    ]
    after_workspace = [
        candidate for candidate in protected
        if candidate not in before_workspace
    ]

    temp_root = os.path.realpath(tempfile.gettempdir())
    for candidate in before_workspace:
        if _contains_path(temp_root, candidate):
            args.extend(_dir_mounts(temp_root, candidate))
        if _contains_path("/run", candidate) and candidate != "/run":
            args.extend(_dir_mounts("/run", candidate))
        if os.path.isdir(candidate):
            args.extend(["--tmpfs", candidate])
        else:
            args.extend(["--ro-bind", os.devnull, candidate])

    masked_parent = max(
        (
            candidate for candidate in before_workspace
            if _contains_path(candidate, root)
        ),
        key=len,
        default=None,
    )
    if masked_parent:
        args.extend(_dir_mounts(masked_parent, root))
    else:
        if root.startswith(temp_root + os.sep):
            args.extend(_dir_mounts(temp_root, root))
        if root.startswith("/run/"):
            args.extend(_dir_mounts("/run", root))
    if overlay:
        upper, work = overlay
        args.extend(["--overlay-src", root, "--overlay", upper, work, root])
    else:
        args.extend(["--ro-bind" if workspace_readonly else "--bind", root, root])

    # App databases, credentials, and cross-tenant catalogues are control-plane
    # state, not workspace inputs. Hide directories entirely and replace lone
    # files with an empty read-only file: read-only exposure still leaks them.
    for candidate in after_workspace:
        if os.path.isdir(candidate):
            args.extend(["--tmpfs", candidate])
        else:
            args.extend(["--ro-bind", os.devnull, candidate])

    runtime_cache_target = (
        os.path.abspath(os.path.join(os.environ["MIMOCODE_HOME"], "cache"))
        if os.environ.get("MIMOCODE_HOME")
        else ""
    )
    runtime_cache = (
        os.path.realpath(runtime_cache_target)
        if runtime_cache_target
        else ""
    )
    if (
        runtime_cache
        and os.path.exists(runtime_cache)
        and not (
            _contains_path(runtime_cache, root)
            or _contains_path(runtime_cache_target, root)
        )
    ):
        cache_parent = max(
            (
                candidate for candidate in protected
                if (
                    _contains_path(candidate, runtime_cache)
                    or _contains_path(candidate, runtime_cache_target)
                )
            ),
            key=len,
            default=None,
        )
        if cache_parent:
            args.extend(_dir_mounts(cache_parent, runtime_cache_target))
        args.extend([
            "--bind" if _contains_path(root, runtime_cache) else "--ro-bind",
            runtime_cache,
            runtime_cache_target,
        ])
    for descriptor, destination in readonly_data_mounts:
        mount_path = os.path.abspath(destination)
        if descriptor < 0 or not _contains_path(root, mount_path):
            raise ShellContainmentError(
                "verified file snapshots must mount inside the active workspace"
            )
        args.extend(["--ro-bind-data", str(descriptor), mount_path])
    args.extend(["--chdir", active_cwd, "--"])
    args.extend(str(item) for item in argv)
    return args, "bwrap-overlay" if overlay else "bwrap"


def append_shell_audit(
    *,
    command: str,
    owner: Optional[str],
    session_id: Optional[str],
    workspace: str,
    containment: str,
    network: str = "enabled",
    actions: Optional[Iterable[str]] = None,
    event: str = "start",
) -> None:
    """Append one bounded, redacted JSONL audit event."""
    from src.constants import DATA_DIR
    from services.memory.skill_lifecycle import locked

    path = Path(DATA_DIR) / "shell-audit.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    record = {
        "ts": time.time(),
        "event": event,
        "owner": str(owner or ""),
        "session_id": str(session_id or ""),
        "workspace": os.path.realpath(workspace),
        "containment": containment,
        "network": network_policy(network),
        "destructive_actions": sorted(set(actions or ())),
        "command": redact_text(command),
    }
    payload = (json.dumps(record, ensure_ascii=False) + "\n").encode("utf-8")
    if len(payload) > _AUDIT_MAX_BYTES // 2:
        command_bytes = str(record["command"]).encode("utf-8")
        record["command"] = command_bytes[: _AUDIT_MAX_BYTES // 4].decode(
            "utf-8", errors="ignore"
        )
        payload = (json.dumps(record, ensure_ascii=False) + "\n").encode("utf-8")
    # Share the lifecycle marker with owner rename/purge so another owner's
    # concurrent append cannot race the JSONL compare-and-swap replacement.
    with locked(str(path)):
        with open(path, "a+b") as handle:
            if os.name == "nt":
                import msvcrt

                if handle.tell() == 0:
                    handle.write(b"\0")
                    handle.flush()
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_LOCK, 1)
            else:
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            try:
                handle.seek(0, os.SEEK_END)
                if handle.tell() + len(payload) > _AUDIT_MAX_BYTES:
                    keep_bytes = max(
                        0,
                        min(
                            _AUDIT_MAX_BYTES // 2,
                            _AUDIT_MAX_BYTES - len(payload),
                        ),
                    )
                    offset = max(0, handle.tell() - keep_bytes)
                    handle.seek(offset)
                    tail = handle.read()
                    if offset:
                        # The bounded seek can land inside a JSON record.  Drop
                        # that incomplete prefix so the retained file remains
                        # valid JSONL for lifecycle inventory and replay.
                        newline = tail.find(b"\n")
                        tail = tail[newline + 1 :] if newline >= 0 else b""
                    handle.seek(0)
                    handle.truncate()
                    handle.write(tail)
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            finally:
                if os.name == "nt":
                    handle.seek(0)
                    msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
