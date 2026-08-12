import pytest
from pydantic import ValidationError

from atlas_backend.config import Settings

VALID_DSN = "postgresql+asyncpg://user:pass@localhost:5432/atlas"
LONG_SECRET = "x" * 48


def build(**overrides: object) -> Settings:
    base: dict[str, object] = {
        "database_url": VALID_DSN,
        "jwt_secret": LONG_SECRET,
        "environment": "dev",
    }
    return Settings(**(base | overrides))  # type: ignore[arg-type]


def test_defaults_are_applied() -> None:
    settings = build()
    assert settings.access_token_ttl_s == 900
    assert settings.is_production is False


@pytest.mark.parametrize(
    "dsn",
    [
        "postgresql://user:pass@localhost/atlas",  # sync driver
        "postgres://user:pass@localhost/atlas",
        "sqlite+aiosqlite:///atlas.db",
        "not a url",
    ],
)
def test_non_async_postgres_dsn_is_rejected(dsn: str) -> None:
    with pytest.raises(ValidationError, match="asyncpg"):
        build(database_url=dsn)


def test_log_level_is_normalised() -> None:
    assert build(log_level="debug").log_level == "DEBUG"


def test_unknown_log_level_is_rejected() -> None:
    with pytest.raises(ValidationError, match="invalid log level"):
        build(log_level="chatty")


class TestProductionHygiene:
    def test_short_secret_is_rejected(self) -> None:
        with pytest.raises(ValidationError, match="at least 32 characters"):
            build(environment="prod", jwt_secret="short")

    def test_statement_logging_is_rejected(self) -> None:
        # Echoed SQL would put device identifiers into the log stream.
        with pytest.raises(ValidationError, match="database_echo must be off"):
            build(environment="prod", database_echo=True)

    def test_valid_production_config_is_accepted(self) -> None:
        settings = build(environment="prod")
        assert settings.is_production is True

    def test_dev_tolerates_a_short_secret(self) -> None:
        assert build(jwt_secret="short").environment == "dev"


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("access_token_ttl_s", 30),
        ("access_token_ttl_s", 7200),
        ("clock_skew_tolerance_s", 1),
        ("heartbeat_interval_s", 0.5),
        ("pairing_code_ttl_s", 10),
    ],
)
def test_out_of_range_values_are_rejected(field: str, value: object) -> None:
    with pytest.raises(ValidationError):
        build(**{field: value})


def test_secrets_are_not_printed() -> None:
    settings = build(bootstrap_token="super-secret-value")
    assert "super-secret-value" not in repr(settings)
    assert "super-secret-value" not in str(settings.bootstrap_token)
