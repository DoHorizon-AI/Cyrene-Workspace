"""
┌──────────────────────────────────────────────────────────────────────────┐
│  📄 directory-role-bootstrap.py                                          │
│  Module: tooling.deploy.teams_cloud.directory_role_bootstrap             │
│  Role: Initialize isolated Directory operator and runtime logins.        │
│                                                                          │
│  模块职责：迁移后配置分离的operator和只读runtime登录角色。              │
└──────────────────────────────────────────────────────────────────────────┘
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import psycopg
from psycopg import sql
from psycopg.conninfo import conninfo_to_dict

ADMIN_URL_FILE_ENV = "CYRENE_DIRECTORY_ADMIN_DATABASE_URL_FILE"
APP_URL_FILE_ENV = "CYRENE_DIRECTORY_APP_DATABASE_URL_FILE"
OPERATOR_URL_FILE_ENV = "CYRENE_DIRECTORY_OPERATOR_DATABASE_URL_FILE"
ADMIN_USER = "cyrene_directory_admin"
APP_USER = "cyrene_authority_app"
OPERATOR_USER = "cyrene_directory_operator"
READ_ROLE = "cyrene_workspace_directory_reader"
OPERATOR_ROLE = "cyrene_workspace_directory_operator"


class BootstrapError(ValueError):
    """Database role bootstrap could not prove the restricted runtime login."""


def _secret_file(environment_name: str) -> str:
    """Read one protected URL file into process memory without logging it."""

    filename = os.environ.get(environment_name, "")
    path = Path(filename)
    if not path.is_absolute() or path.is_symlink() or not path.is_file():
        raise BootstrapError(f"required database URL file is unavailable: {environment_name}")
    metadata = path.stat()
    if metadata.st_mode & 0o077 or metadata.st_size > 8192:
        raise BootstrapError(
            f"database URL file permissions or size are invalid: {environment_name}"
        )
    try:
        value = path.read_text(encoding="utf-8").strip()
    except (OSError, UnicodeDecodeError) as error:
        raise BootstrapError(f"database URL file is unreadable: {environment_name}") from error
    if not value.startswith(("postgres://", "postgresql://")):
        raise BootstrapError(f"database URL file is not a PostgreSQL URL: {environment_name}")
    return value


def _validate_url(value: str, expected_user: str) -> dict[str, str]:
    """Require the private database target and hostname-verifying TLS."""

    try:
        parameters = conninfo_to_dict(value)
    except (TypeError, ValueError) as error:
        raise BootstrapError("Directory PostgreSQL URL is malformed") from error
    if parameters.get("user") != expected_user:
        raise BootstrapError("Directory PostgreSQL URL uses the wrong restricted role")
    if parameters.get("host") != "postgres" or parameters.get("dbname") != "cyrene_directory":
        raise BootstrapError("Directory PostgreSQL URL is outside the private database target")
    if parameters.get("sslmode") != "verify-full" or not parameters.get("sslrootcert"):
        raise BootstrapError("Directory PostgreSQL URL must verify the private server certificate")
    return parameters


def _ensure_login(connection: psycopg.Connection, user: str, password: str) -> None:
    """Create one least-privilege login without changing an existing password."""

    role = connection.execute(
        "SELECT rolsuper, rolcreatedb, rolcreaterole, rolreplication, rolbypassrls, rolcanlogin "
        "FROM pg_roles WHERE rolname = %s",
        (user,),
    ).fetchone()
    if role is None:
        setting_name = f"cyrene.bootstrap_password_{user}"
        connection.execute("SELECT set_config(%s, %s, false)", (setting_name, password))
        connection.execute(
            sql.SQL(
                "DO $bootstrap$ DECLARE role_name text := {}; setting_name text := {}; "
                "BEGIN EXECUTE format('CREATE ROLE %I LOGIN PASSWORD %L', role_name, current_setting(setting_name)); "
                "END $bootstrap$"
            ).format(sql.Literal(user), sql.Literal(setting_name))
        )
        connection.execute("SELECT set_config(%s, '', false)", (setting_name,))
        role = connection.execute(
            "SELECT rolsuper, rolcreatedb, rolcreaterole, rolreplication, rolbypassrls, rolcanlogin "
            "FROM pg_roles WHERE rolname = %s",
            (user,),
        ).fetchone()
    if role != (False, False, False, False, False, True):
        raise BootstrapError(f"existing Directory login {user} is not least-privileged")


def _verify_login(url: str, user: str, group_role: str) -> None:
    """Verify login, exclusive group membership, and schema reachability."""

    with psycopg.connect(url, connect_timeout=5) as connection:
        memberships = connection.execute(
            "SELECT parent.rolname FROM pg_auth_members membership "
            "JOIN pg_roles parent ON parent.oid = membership.roleid "
            "JOIN pg_roles member ON member.oid = membership.member "
            "WHERE member.rolname = current_user ORDER BY parent.rolname"
        ).fetchall()
        row = connection.execute(
            "SELECT current_user, pg_has_role(current_user, %s, 'MEMBER'), "
            "has_schema_privilege(current_user, 'cyrene_workspace_directory', 'USAGE'), "
            "(SELECT rolsuper OR rolcreatedb OR rolcreaterole OR rolreplication OR rolbypassrls "
            " FROM pg_roles WHERE rolname = current_user), "
            "has_table_privilege(current_user, 'cyrene_workspace_directory.memberships', 'SELECT'), "
            "has_table_privilege(current_user, 'cyrene_workspace_directory.memberships', 'INSERT'), "
            "has_table_privilege(current_user, 'cyrene_workspace_directory.memberships', 'DELETE')",
            (group_role,),
        ).fetchone()
        expected_prefix = (user, True, True, False, True)
        if row[:5] != expected_prefix or memberships != [(group_role,)]:
            raise BootstrapError(f"Directory login {user} did not pass its isolated role check")
        if group_role == READ_ROLE and row[5:] != (False, False):
            raise BootstrapError("Directory runtime reader has write access to memberships")
        if group_role == OPERATOR_ROLE and row[5:] != (True, True):
            raise BootstrapError(
                "Directory operator cannot perform its scoped membership operation"
            )
        if user == APP_USER:
            connection.execute(
                "SELECT organization_id FROM cyrene_workspace_directory.memberships LIMIT 0"
            )


def bootstrap() -> None:
    """Create and verify distinct operator and read-only runtime logins."""

    admin_url = _secret_file(ADMIN_URL_FILE_ENV)
    admin = _validate_url(admin_url, ADMIN_USER)
    app_url = _secret_file(APP_URL_FILE_ENV)
    app = _validate_url(app_url, APP_USER)
    operator_url = _secret_file(OPERATOR_URL_FILE_ENV)
    operator = _validate_url(operator_url, OPERATOR_USER)
    for name, parameters in (("app", app), ("operator", operator)):
        if admin.get("dbname") != parameters.get("dbname") or admin.get("host") != parameters.get(
            "host"
        ):
            raise BootstrapError(
                f"Directory {name} and admin URLs must address the same private database"
            )
        if not parameters.get("password"):
            raise BootstrapError(f"Directory {name} URL must contain its login password")

    with psycopg.connect(admin_url, autocommit=True, connect_timeout=5) as connection:
        groups = connection.execute(
            "SELECT rolname, rolcanlogin, rolsuper, rolcreatedb, rolcreaterole "
            "FROM pg_roles WHERE rolname = ANY(%s)",
            ([READ_ROLE, OPERATOR_ROLE],),
        ).fetchall()
        if sorted(groups) != sorted(
            [(READ_ROLE, False, False, False, False), (OPERATOR_ROLE, False, False, False, False)]
        ):
            raise BootstrapError("Directory schema migrations must complete before role bootstrap")
        _ensure_login(connection, APP_USER, app["password"])
        _ensure_login(connection, OPERATOR_USER, operator["password"])
        connection.execute(
            sql.SQL("GRANT {} TO {}").format(sql.Identifier(READ_ROLE), sql.Identifier(APP_USER))
        )
        connection.execute(
            sql.SQL("GRANT {} TO {}").format(
                sql.Identifier(OPERATOR_ROLE), sql.Identifier(OPERATOR_USER)
            )
        )

    _verify_login(app_url, APP_USER, READ_ROLE)
    _verify_login(operator_url, OPERATOR_USER, OPERATOR_ROLE)


def main() -> int:
    """Return a sanitized status without exposing database credentials."""

    try:
        bootstrap()
    except (OSError, BootstrapError, psycopg.Error):
        print("Directory runtime role bootstrap failed closed.", file=sys.stderr)
        return 2
    print("Directory runtime role is ready.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
