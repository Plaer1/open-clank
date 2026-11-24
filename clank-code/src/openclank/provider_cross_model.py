"""Inactive, optional P08-B policy profile checks; never issues authority."""
from __future__ import annotations

import copy
import json
from .provider_cross_model_schema import FUNCTION, MARKER as _MARKER, TRIGGERS, V1_MARKER, V1_TRIGGERS



def _profile_matches(connection, *, version: int) -> bool:
    """Recognize only the explicit copied profile; absent or partial stays Off."""
    try:
        if connection.dialect.name != 'sqlite':
            return False
        marker = connection.exec_driver_sql(
            "SELECT name,sql FROM sqlite_master WHERE name IN ('provider_cross_model_profile_v1','provider_cross_model_profile_v2')"
        ).fetchall()
        route = {row[1]: (str(row[2]).upper(), row[3], row[4]) for row in connection.exec_driver_sql('PRAGMA table_info("provider_model_routes")')}
        binding = {row[1]: (str(row[2]).upper(), row[3], row[4]) for row in connection.exec_driver_sql('PRAGMA table_info("provider_operation_bindings")')}
        indexes = connection.exec_driver_sql('PRAGMA index_list("provider_operation_bindings")').fetchall()
        triggers = dict(connection.exec_driver_sql("SELECT name,sql FROM sqlite_master WHERE type='trigger' AND name LIKE 'p08_%'").fetchall())
        affinity = False
        for index in indexes:
            if index[2] and not index[4]:
                keys = [row for row in connection.exec_driver_sql('SELECT * FROM pragma_index_xinfo(?)', (index[1],)) if row[5]]
                if ([row[2] for row in keys] == ['owner','root_operation_id','actor_scope_key','connection_id','billing_lane','model_id']
                        and all(row[3] == 0 and row[4] == 'BINARY' for row in keys)):
                    affinity = True
                    break
        expected_marker = [('provider_cross_model_profile_v2', _MARKER)] if version == 2 else [('provider_cross_model_profile_v1', V1_MARKER)]
        expected_triggers = TRIGGERS if version == 2 else V1_TRIGGERS
        return bool([tuple(row) for row in marker] == expected_marker and triggers == expected_triggers and route.get('cross_model_policy') == ('JSON', 0, None)
                    and binding.get('cross_model_coordinator') == ('JSON', 0, None) and affinity
                    and {'actor_scope_key','engine_session_id','actor_id','execution_revision','model_route_id'}.issubset(binding))
    except Exception:
        return False


def profile_ready(connection) -> bool:
    return _profile_matches(connection, version=2)


def profile_v1_ready(connection) -> bool:
    """Explicit upgrade input only, never runtime root-fence authority."""
    return _profile_matches(connection, version=1)


def profile_absent(connection) -> bool:
    """Recognize only a wholly absent optional profile, never a partial one."""
    try:
        if connection.dialect.name != 'sqlite': return False
        marker = connection.exec_driver_sql("SELECT 1 FROM sqlite_master WHERE name IN ('provider_cross_model_profile_v1','provider_cross_model_profile_v2')").first()
        triggers = connection.exec_driver_sql("SELECT 1 FROM sqlite_master WHERE type='trigger' AND name LIKE 'p08_%' LIMIT 1").first()
        route = {row[1] for row in connection.exec_driver_sql('PRAGMA table_info("provider_model_routes")')}
        binding = {row[1] for row in connection.exec_driver_sql('PRAGMA table_info("provider_operation_bindings")')}
        return bool(route and binding and marker is None and triggers is None and 'cross_model_policy' not in route and 'cross_model_coordinator' not in binding)
    except Exception:
        return False


def set_writer(connection, callback=None):
    """Install a connection-local, deny-default cooperating-writer function."""
    connection.connection.driver_connection.create_function(FUNCTION, 7, callback or (lambda *args: 0))


def validate_policy(value):
    """Return a detached closed policy; route/options authority is checked separately."""
    if type(value) is not dict or set(value) != {'version','enabled','revision','alternatives'}:
        raise ValueError('cross-model policy unavailable')
    if type(value['version']) is not int or value['version'] != 1 or type(value['enabled']) is not bool:
        raise ValueError('cross-model policy unavailable')
    if type(value['revision']) is not int or not 1 <= value['revision'] <= 2**31 - 1:
        raise ValueError('cross-model policy unavailable')
    items = value['alternatives']
    if type(items) is not list or len(items) > 8 or (value['enabled'] and not items):
        raise ValueError('cross-model policy unavailable')
    seen = set()
    for item in items:
        if type(item) is not dict or set(item) != {'route_id','options'}:
            raise ValueError('cross-model policy unavailable')
        route = item['route_id']
        if type(route) is not str or not 1 <= len(route) <= 256 or route.strip() != route or '\x00' in route or route in seen:
            raise ValueError('cross-model policy unavailable')
        options = item['options']
        if (type(options) is not dict or set(options) != {'route_id','catalog_revision','options'}
                or type(options['route_id']) is not str or not 1 <= len(options['route_id']) <= 256
                or options['route_id'].strip() != options['route_id'] or '\x00' in options['route_id']
                or type(options['catalog_revision']) is not int or not 1 <= options['catalog_revision'] <= 2**31 - 1
                or type(options['options']) is not dict or set(options['options']) - {'reasoning_effort'}):
            raise ValueError('cross-model policy unavailable')
        if 'reasoning_effort' in options['options']:
            effort = options['options']['reasoning_effort']
            if type(effort) is not str or not 1 <= len(effort) <= 128 or effort.strip() != effort or '\x00' in effort:
                raise ValueError('cross-model policy unavailable')
        seen.add(route)
    try:
        if len(json.dumps(value, ensure_ascii=False, separators=(',', ':'), allow_nan=False).encode('utf-8')) > 16384:
            raise ValueError('cross-model policy unavailable')
    except (TypeError, UnicodeError, RecursionError):
        raise ValueError('cross-model policy unavailable') from None
    return copy.deepcopy(value)
