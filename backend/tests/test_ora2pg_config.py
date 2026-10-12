import pytest

from app.config import get_settings
from app.models import DbKind
from app.services import ora2pg
from app.services.endpoints import Endpoint


def oracle(**overrides) -> Endpoint:
    fields = dict(
        kind=DbKind.ORACLE, host="ora.example", port=1521, database="ORCLPDB1",
        username="STUDIO_READER", password="pw#with;odd chars",
    )
    return Endpoint(**{**fields, **overrides})


def directives(text: str) -> dict[str, str]:
    return dict(line.split("\t", 1) for line in text.splitlines())


def test_config_uses_service_name_and_read_only_transaction():
    conf = directives(ora2pg.render_config(oracle(), user_grants=False))
    assert conf["ORACLE_DSN"] == "dbi:Oracle:host=ora.example;service_name=ORCLPDB1;port=1521"
    assert conf["ORACLE_USER"] == "STUDIO_READER"
    assert conf["TRANSACTION"] == "readonly"
    assert conf["USER_GRANTS"] == "0"
    assert conf["PG_VERSION"] == str(get_settings().target_pg_version)


def test_config_never_contains_the_password():
    text = ora2pg.render_config(oracle(), user_grants=True)
    assert "pw#with" not in text
    assert "ORACLE_PWD" not in text


def test_sid_option_switches_dsn_key():
    conf = directives(ora2pg.render_config(oracle(options={"use_sid": True}), user_grants=False))
    assert ";sid=ORCLPDB1;" in conf["ORACLE_DSN"]


def test_extra_directives_override_defaults():
    conf = directives(ora2pg.render_config(oracle(), user_grants=False, extra={"DEBUG": 1}))
    assert conf["DEBUG"] == "1"


@pytest.mark.parametrize("bad", ["two\nlines", "carriage\rreturn", " padded", ""])
def test_values_that_could_inject_directives_are_rejected(bad):
    with pytest.raises(ValueError):
        ora2pg.render_config(oracle(username=bad), user_grants=False)


def test_postgres_endpoint_is_rejected():
    with pytest.raises(ValueError):
        ora2pg.render_config(oracle(kind=DbKind.POSTGRES), user_grants=False)


def test_failure_summary_prefers_fatal_lines():
    result = ora2pg.Ora2PgResult(
        args=[], returncode=1, stdout="noise\nFATAL: ORA-01017: invalid username/password\nmore",
        stderr="", duration_seconds=0.1, output_dir=None,
    )
    assert not result.succeeded
    assert "ORA-01017" in result.failure_summary()


def test_ora2pg_is_installed_in_the_image():
    assert ora2pg.ora2pg_version().startswith("Ora2Pg v")
