"""Keychain writes must not put the secret on the command line.

`security add-generic-password ... -w <secret>` shows the secret in the process
table to every process on the machine for as long as the command runs. Feed the
command to `security -i` over stdin instead, with the value hex-encoded so no
quoting rule can break it.
"""

from catapult import refresh


class Recorder:
    def __init__(self):
        self.calls: list[tuple[list[str], dict]] = []

    def __call__(self, argv, **kwargs):
        self.calls.append((list(argv), kwargs))

        class Result:
            returncode = 0
            stdout = ""

        return Result()


def test_secret_is_not_passed_as_an_argument(monkeypatch):
    run = Recorder()
    monkeypatch.setattr(refresh.subprocess, "run", run)
    secret = 'tok"en with spaces and /slashes+='

    assert refresh._keychain_set("acct", secret)

    assert run.calls, "expected security to be invoked"
    for argv, _ in run.calls:
        assert all(secret not in arg for arg in argv), argv


def test_secret_travels_to_security_over_stdin_hex_encoded(monkeypatch):
    run = Recorder()
    monkeypatch.setattr(refresh.subprocess, "run", run)
    secret = 'tok"en'

    refresh._keychain_set("acct", secret)

    interactive = [kw for argv, kw in run.calls if argv[:2] == ["security", "-i"]]
    assert interactive, "expected a `security -i` invocation"
    stdin = interactive[-1]["input"]
    assert secret.encode().hex() in stdin
    assert "add-generic-password" in stdin and " -U" in stdin
    assert " acct" in stdin and refresh._KEYCHAIN_SERVICE in stdin
