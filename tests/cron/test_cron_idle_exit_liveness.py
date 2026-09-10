"""Cron activity must keep an SSH-isolated dashboard backend alive (#107485)."""

import types

from hermes_cli import web_server_idle_exit as idle_exit


def test_turn_probe_counts_in_process_cron_execution(monkeypatch):
    gateway = types.SimpleNamespace(_sessions_lock=__import__("threading").Lock(), _sessions={})
    monkeypatch.setitem(__import__("sys").modules, "tui_gateway.server", gateway)
    monkeypatch.setattr("cron.scheduler.get_running_job_ids", lambda: frozenset({"daily"}))

    assert idle_exit.turn_in_flight() is True


def test_turn_probe_allows_exit_only_when_cron_and_chat_are_idle(monkeypatch):
    gateway = types.SimpleNamespace(_sessions_lock=__import__("threading").Lock(), _sessions={})
    monkeypatch.setitem(__import__("sys").modules, "tui_gateway.server", gateway)
    monkeypatch.setattr("cron.scheduler.get_running_job_ids", lambda: frozenset())

    assert idle_exit.turn_in_flight() is False
