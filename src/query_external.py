"""Glue that lets the Phase 1 compiler query external (synced) tables safely.

External tables are referenced in the AST by their CatalogTable.id. They are
exempt from per-row owner-scoping (they have no per-user owner) — that is sound
ONLY because the run/preview routes require the can_manage_data_sources
privilege whenever a query touches an external table (see query_routes). This
module provides the resolver, the owner-clause adapter, and the
external-touch detector the route needs.
"""
from typing import Callable

from sqlalchemy import MetaData, Table, true

from core.database import engine as app_engine_default
from core.querybuilder_models import CatalogTable
from src import query_catalog
from src.query_catalog import QueryValidationError


def build_resolver(db, app_engine=None) -> Callable[[str], Table]:
    app_engine = app_engine or app_engine_default

    def resolve(name: str) -> Table:
        if query_catalog.is_allowed_table(name):
            return query_catalog.resolve_table(name)
        ct = (db.query(CatalogTable)
              .filter(CatalogTable.id == name,
                      CatalogTable.is_internal == False)  # noqa: E712
              .first())
        if ct is None or not ct.physical_table_name:
            raise QueryValidationError(f"Unknown or forbidden table: {name!r}")
        return Table(ct.physical_table_name, MetaData(), autoload_with=app_engine)

    return resolve


def build_owner_clause(db) -> Callable:
    def owner_clause(name: str, aliased, user: str):
        if query_catalog.is_allowed_table(name):
            return query_catalog.owner_clause(name, aliased, user)
        # External table: exempt from per-row scoping (gated by privilege at the
        # route). true() is a no-op AND-term.
        return true()
    return owner_clause


def query_touches_external(db, ast: dict) -> bool:
    tables = ast.get("tables", []) if isinstance(ast, dict) else []
    for t in tables if isinstance(tables, list) else []:
        if not isinstance(t, dict):
            continue
        name = t.get("table")
        if name and not query_catalog.is_allowed_table(name):
            return True
    return False
