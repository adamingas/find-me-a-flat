"""Invalid schedules fail before opening services or creating databases."""

import pytest
from click.testing import CliRunner

from openrent import cli, daemon


@pytest.mark.parametrize(
    "extra, message",
    [
        (["--cron", "bad"], "five valid fields"),
        (["--cron", "0 0 0 * * *"], "five valid fields"),
        (["--cron", "0 25 * * *"], "five valid fields"),
        (["--timezone", "Unknown/Zone"], "Unknown/Zone"),
        (["--notify-cron-schedule", "invalid"], "five valid fields"),
        (["--notify-cron-schedule", "0 9 * * *"], "requires --email-to"),
        (
            ["--email-to", "self@example.com", "--email-preview", "preview.html"],
            "requires --notify-cron-schedule",
        ),
    ],
)
def test_invalid_daemon_settings_do_not_start_services(tmp_path, monkeypatch, extra, message):
    def forbidden(*args, **kwargs):
        pytest.fail("Invalid settings must fail before opening services")

    monkeypatch.setattr(daemon, "OpenRentClient", forbidden)
    monkeypatch.setattr(daemon.database, "connect", forbidden)
    monkeypatch.setattr(daemon, "DBOS", forbidden)
    path = tmp_path / "archive.sqlite"
    result = CliRunner().invoke(
        cli.app,
        [
            "daemon",
            "--location",
            "London",
            "--radius-distance",
            "2",
            "--cron",
            "0 * * * *",
            "--db",
            str(path),
            *extra,
        ],
    )
    assert result.exit_code != 0
    # ZoneInfo's lookup exception is not wrapped by Click.
    assert message in result.output or message in str(result.exception)
    assert not path.exists()
