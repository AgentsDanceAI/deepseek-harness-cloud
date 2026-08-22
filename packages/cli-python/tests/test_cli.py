from dsh_cloud_cli.cli import CliError, parse_args


def test_rejects_secret_values_on_command_line():
    try:
        parse_args(["init", "--upstream-key", "secret"])
    except CliError as error:
        assert "secret values are not accepted" in str(error)
    else:
        raise AssertionError("secret value was accepted")


def test_parses_safe_trial_dry_run_options():
    assert parse_args(["start", "--mode", "trial", "--dry-run", "--json"]) == {
        "command": "start",
        "positionals": [],
        "options": {"mode": "trial", "dryRun": True, "json": True},
    }
