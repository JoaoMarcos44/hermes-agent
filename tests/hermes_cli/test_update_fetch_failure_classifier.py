"""Fetch-failure classification for `hermes update` / `hermes update --check`.

A GitHub-side HTTP 429 (rate limit / outage) used to be reported as the
generic "Failed to fetch updates from origin." — or worse, matched the
"unable to access" branch and got called a local network error. The
classifier must call out rate limiting / outages explicitly, and the raw
stderr line must always be printed alongside the diagnosis.
"""

from hermes_cli import update_cmd


RATE_LIMIT_STDERR = (
    "error: RPC failed; HTTP 429 curl 22 The requested URL returned error: 429\n"
    "fatal: expected flush after ref listing"
)
CURL_429_STDERR = (
    "fatal: unable to access 'https://github.com/NousResearch/hermes-agent.git/':"
    " The requested URL returned error: 429"
)


class TestClassifyFetchFailure:
    def test_http_429_rpc_failure_reports_rate_limit(self):
        msg = update_cmd._classify_fetch_failure(RATE_LIMIT_STDERR)
        assert "rate limiting" in msg
        assert "try again in 5 minutes" in msg

    def test_curl_unable_to_access_429_is_rate_limit_not_network(self):
        # "unable to access" also appears here — 429 must win.
        msg = update_cmd._classify_fetch_failure(CURL_429_STDERR)
        assert "rate limiting" in msg
        assert "Network error" not in msg

    def test_rate_limit_phrase_without_code(self):
        msg = update_cmd._classify_fetch_failure("fatal: GitHub rate limit exceeded")
        assert "rate limiting" in msg

    def test_5xx_reports_outage(self):
        msg = update_cmd._classify_fetch_failure(
            "fatal: unable to access 'https://github.com/x.git/':"
            " The requested URL returned error: 503"
        )
        assert "outage" in msg
        assert "githubstatus.com" in msg

    def test_dns_failure_reports_network_error(self):
        msg = update_cmd._classify_fetch_failure(
            "fatal: unable to access 'https://github.com/x.git/':"
            " Could not resolve host: github.com"
        )
        assert msg.startswith("✗ Network error")

    def test_username_prompt_401_reports_github_not_user_credentials(self):
        # What GitHub's HTTP 401 looks like once the terminal prompt is
        # disabled — must NOT be blamed on the user's credentials.
        msg = update_cmd._classify_fetch_failure(
            "fatal: could not read Username for 'https://github.com':"
            " terminal prompts disabled"
        )
        assert "GitHub" in msg and "outage" in msg
        assert "check your git credentials" not in msg

    def test_auth_failure(self):
        msg = update_cmd._classify_fetch_failure(
            "fatal: Authentication failed for 'https://github.com/x.git/'"
        )
        assert "Authentication failed" in msg

    def test_ssh_publickey_denial_reports_ssh_auth_not_generic(self):
        # git wraps OpenSSH's own rejection as "Could not read from remote
        # repository" — never "Authentication failed" — so this needs its
        # own rule ahead of the generic fallback (#82169).
        msg = update_cmd._classify_fetch_failure(
            "git@github.com: Permission denied (publickey).\n"
            "fatal: Could not read from remote repository."
        )
        assert "SSH authentication failed" in msg
        assert "https://github.com/NousResearch/hermes-agent.git" in msg

    def test_ssh_host_key_failure_reports_ssh_auth(self):
        msg = update_cmd._classify_fetch_failure(
            "@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@\n"
            "Host key verification failed.\n"
            "fatal: Could not read from remote repository."
        )
        assert "SSH authentication failed" in msg

    def test_unknown_falls_back_to_generic(self):
        msg = update_cmd._classify_fetch_failure("fatal: something novel")
        assert msg == "✗ Failed to fetch updates from origin."


class TestPrintFetchFailure:
    def test_prints_diagnosis_and_first_raw_line(self, capsys):
        update_cmd._print_fetch_failure(RATE_LIMIT_STDERR)
        out = capsys.readouterr().out
        assert "rate limiting" in out
        assert "HTTP 429" in out
        # raw first stderr line preserved for diagnosability
        assert "error: RPC failed" in out

    def test_empty_stderr_prints_only_diagnosis(self, capsys):
        update_cmd._print_fetch_failure("")
        out = capsys.readouterr().out.strip().splitlines()
        assert out == ["✗ Failed to fetch updates from origin."]


def test_update_network_git_calls_never_prompt_for_credentials():
    """Every `git fetch`/`pull`/`push` in the updater runs with prompts disabled.

    Live incident (Sep 2026): a GitHub-side 401 made `hermes update` sit on
    ``Username for 'https://github.com':`` instead of failing with a diagnosis.
    """
    import os
    import subprocess

    kw = update_cmd._no_prompt_git_kwargs()
    assert kw["stdin"] is subprocess.DEVNULL
    assert kw["env"]["GIT_TERMINAL_PROMPT"] == "0"
    # Only the prompt is disabled — credential helpers / askpass stay
    # configured so a private-fork origin still authenticates.
    assert "GIT_CONFIG_COUNT" not in kw["env"] or kw["env"]["GIT_CONFIG_COUNT"] == os.environ.get("GIT_CONFIG_COUNT")


def test_ssl_cert_file_becomes_git_ca_for_explicit_and_lazy_fetches(monkeypatch, tmp_path):
    """The updater's CA fallback is process-scoped so partial-clone lazy fetches inherit it."""
    import os
    import subprocess

    corporate_ca = str(tmp_path / "corporate-ca.pem")
    monkeypatch.setenv("SSL_CERT_FILE", corporate_ca)
    monkeypatch.delenv("GIT_SSL_CAINFO", raising=False)
    monkeypatch.delenv("GIT_SSL_CAPATH", raising=False)
    calls = []

    def run(cmd, **kwargs):
        if cmd[-4:] == ["config", "--name-only", "--get-regexp", r"^http\..*sslca(info|path)$"]:
            return subprocess.CompletedProcess(cmd, 1, "", "")
        calls.append((cmd, os.environ.get("GIT_SSL_CAINFO"), kwargs))
        return subprocess.CompletedProcess(cmd, 0, "", "")

    monkeypatch.setattr(update_cmd.subprocess, "run", run)

    kw = update_cmd._no_prompt_git_kwargs(["git"], tmp_path)
    assert kw["env"]["GIT_SSL_CAINFO"] == corporate_ca

    # A local-looking checkout may make an implicit promisor fetch in a treeless clone.
    update_cmd._git_run(["git"], ["checkout", "main"], cwd=tmp_path)
    assert calls[-1][1] == corporate_ca


def test_ssl_cert_file_does_not_override_git_ca_env(monkeypatch, tmp_path):
    """An explicit Git CA environment setting has higher precedence than the fallback."""
    git_ca = str(tmp_path / "git-ca.pem")
    monkeypatch.setenv("SSL_CERT_FILE", str(tmp_path / "python-ca.pem"))
    monkeypatch.setenv("GIT_SSL_CAINFO", git_ca)

    def unexpected_run(*_args, **_kwargs):
        raise AssertionError("explicit GIT_SSL_CAINFO must skip the Git config probe")

    monkeypatch.setattr(update_cmd.subprocess, "run", unexpected_run)

    assert update_cmd._no_prompt_git_kwargs(["git"], tmp_path)["env"]["GIT_SSL_CAINFO"] == git_ca


def test_ssl_cert_file_does_not_override_git_ca_config(monkeypatch, tmp_path):
    """A persisted Git CA setting remains authoritative."""
    import subprocess

    monkeypatch.setenv("SSL_CERT_FILE", str(tmp_path / "python-ca.pem"))
    monkeypatch.delenv("GIT_SSL_CAINFO", raising=False)
    monkeypatch.delenv("GIT_SSL_CAPATH", raising=False)

    def run(cmd, **_kwargs):
        assert cmd[-4:] == ["config", "--name-only", "--get-regexp", r"^http\..*sslca(info|path)$"]
        return subprocess.CompletedProcess(cmd, 0, "http.sslcainfo\n", "")

    monkeypatch.setattr(update_cmd.subprocess, "run", run)

    assert "GIT_SSL_CAINFO" not in update_cmd._no_prompt_git_kwargs(["git"], tmp_path)["env"]


def test_update_and_upstream_network_calls_disable_terminal_prompts(monkeypatch, tmp_path):
    """Exercise origin fetch and fork fetch/pull/push, not their source spelling."""
    import subprocess
    from hermes_cli import update_cmd_git

    monkeypatch.setenv("GIT_TERMINAL_PROMPT", "1")
    monkeypatch.setenv("GCM_INTERACTIVE", "Always")
    monkeypatch.setenv("GIT_ASKPASS", "fixture-askpass")
    monkeypatch.setenv("GIT_CONFIG_COUNT", "1")
    monkeypatch.setenv("GIT_CONFIG_KEY_0", "credential.helper")
    monkeypatch.setenv("GIT_CONFIG_VALUE_0", "fixture-helper")
    monkeypatch.setattr(update_cmd, "_has_upstream_remote", lambda *a: True)
    monkeypatch.setattr(update_cmd, "_count_commits_between",
                        lambda git, cwd, base, head: 2 if head == "upstream/main" else 0)
    calls = []

    def run(cmd, **kwargs):
        calls.append((cmd[1:], kwargs))
        return subprocess.CompletedProcess(cmd, 0, "", "")

    monkeypatch.setattr(subprocess, "run", run)
    update_cmd._git_run(["git"], ["fetch", "origin", "main"], cwd=tmp_path, network=True, check=True)
    assert update_cmd_git._sync_with_upstream_if_needed(["git"], tmp_path, assume_yes=True)
    assert [args[0] for args, _ in calls] == ["fetch", "fetch", "pull", "push"]
    for args, kwargs in calls:
        assert kwargs["stdin"] is subprocess.DEVNULL, args
        env = kwargs["env"]
        assert env["GIT_TERMINAL_PROMPT"] == "0", args
        assert env["GCM_INTERACTIVE"] == "Never", args
        assert env["GIT_ASKPASS"] == "fixture-askpass", args
        assert env["GIT_CONFIG_COUNT"] == "1", args
        assert env["GIT_CONFIG_VALUE_0"] == "fixture-helper", args
