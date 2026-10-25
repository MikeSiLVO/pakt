from click.testing import CliRunner

from pakt import cli
from pakt.config import Config
from pakt.web.auth import check_password


def test_password_command_saves_a_hash_and_secret():
    """`pakt password` stores a scrypt hash and a session secret, never the password."""
    result = CliRunner().invoke(cli.main, ["password"], input="correct horse\ncorrect horse\n")
    assert result.exit_code == 0
    web = Config.load().web
    assert check_password("correct horse", web.password_hash) and web.session_secret
    assert "correct horse" not in web.password_hash


def test_short_passwords_are_refused():
    """A password under eight characters is asked for again."""
    result = CliRunner().invoke(cli.main, ["password"], input="short\nshort\nlong enough\nlong enough\n")
    assert result.exit_code == 0 and "at least 8" in result.output
    assert check_password("long enough", Config.load().web.password_hash)


def test_clear_removes_the_password():
    """`pakt password --clear` removes the hash and secret."""
    CliRunner().invoke(cli.main, ["password"], input="correct horse\ncorrect horse\n")
    CliRunner().invoke(cli.main, ["password", "--clear"])
    web = Config.load().web
    assert web.password_hash == "" and web.session_secret == ""


def test_sync_without_trakt_login_exits_with_an_error():
    """`pakt sync` before logging in to Trakt fails with status 1."""
    result = CliRunner().invoke(cli.main, ["sync"])
    assert result.exit_code == 1 and "pakt login" in result.output
