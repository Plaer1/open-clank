"""Canonical cooperating-writer SQLite guard profile for inactive P08-B."""

FUNCTION = 'openclank_p08_writer_v1'
V1_MARKER = ('CREATE INDEX provider_cross_model_profile_v1 ON provider_operation_bindings '
          '(id) WHERE cross_model_coordinator IS NOT NULL')

_GROUP_NEW = "EXISTS (SELECT 1 FROM provider_operation_bindings p WHERE p.owner=NEW.owner AND p.root_operation_id=NEW.root_operation_id AND p.actor_scope_key=NEW.actor_scope_key AND p.cross_model_coordinator IS NOT NULL)"
_GROUP_OLD = "EXISTS (SELECT 1 FROM provider_operation_bindings p WHERE p.owner=OLD.owner AND p.root_operation_id=OLD.root_operation_id AND p.actor_scope_key=OLD.actor_scope_key AND p.cross_model_coordinator IS NOT NULL)"
_LEASE_NEW = "EXISTS (SELECT 1 FROM provider_operation_bindings b JOIN provider_operation_bindings p ON p.owner=b.owner AND p.root_operation_id=b.root_operation_id AND p.actor_scope_key=b.actor_scope_key WHERE b.id=NEW.binding_id AND p.cross_model_coordinator IS NOT NULL)"
_LEASE_OLD = "EXISTS (SELECT 1 FROM provider_operation_bindings b JOIN provider_operation_bindings p ON p.owner=b.owner AND p.root_operation_id=b.root_operation_id AND p.actor_scope_key=b.actor_scope_key WHERE b.id=OLD.binding_id AND p.cross_model_coordinator IS NOT NULL)"

V1_TRIGGERS = {
    'p08_binding_insert': f"CREATE TRIGGER p08_binding_insert BEFORE INSERT ON provider_operation_bindings WHEN NEW.cross_model_coordinator IS NOT NULL OR {_GROUP_NEW} BEGIN SELECT CASE WHEN {FUNCTION}('insert',NEW.id,NEW.owner,NEW.root_operation_id,NEW.actor_scope_key,NULL,NEW.cross_model_coordinator) != 1 THEN RAISE(ABORT,'P08 writer unavailable') END; END",
    'p08_binding_update': f"CREATE TRIGGER p08_binding_update BEFORE UPDATE ON provider_operation_bindings WHEN OLD.cross_model_coordinator IS NOT NULL OR NEW.cross_model_coordinator IS NOT NULL OR {_GROUP_OLD} OR {_GROUP_NEW} BEGIN SELECT CASE WHEN {FUNCTION}('update',OLD.id,OLD.owner,OLD.root_operation_id,OLD.actor_scope_key,OLD.cross_model_coordinator,NEW.cross_model_coordinator) != 1 THEN RAISE(ABORT,'P08 writer unavailable') END; END",
    'p08_binding_delete': f"CREATE TRIGGER p08_binding_delete BEFORE DELETE ON provider_operation_bindings WHEN OLD.cross_model_coordinator IS NOT NULL OR {_GROUP_OLD} BEGIN SELECT CASE WHEN {FUNCTION}('delete',OLD.id,OLD.owner,OLD.root_operation_id,OLD.actor_scope_key,OLD.cross_model_coordinator,NULL) != 1 THEN RAISE(ABORT,'P08 writer unavailable') END; END",
    'p08_lease_insert': f"CREATE TRIGGER p08_lease_insert BEFORE INSERT ON provider_credential_leases WHEN {_LEASE_NEW} BEGIN SELECT CASE WHEN {FUNCTION}('lease-insert',NEW.binding_id,NEW.owner,NEW.root_operation_id,'',NULL,NULL) != 1 THEN RAISE(ABORT,'P08 writer unavailable') END; END",
    'p08_lease_update': f"CREATE TRIGGER p08_lease_update BEFORE UPDATE ON provider_credential_leases WHEN {_LEASE_OLD} OR {_LEASE_NEW} BEGIN SELECT CASE WHEN {FUNCTION}('lease-update',OLD.binding_id,OLD.owner,OLD.root_operation_id,'',NULL,NULL) != 1 THEN RAISE(ABORT,'P08 writer unavailable') END; END",
    'p08_lease_delete': f"CREATE TRIGGER p08_lease_delete BEFORE DELETE ON provider_credential_leases WHEN {_LEASE_OLD} BEGIN SELECT CASE WHEN {FUNCTION}('lease-delete',OLD.binding_id,OLD.owner,OLD.root_operation_id,'',NULL,NULL) != 1 THEN RAISE(ABORT,'P08 writer unavailable') END; END",
}

# The unique marker both identifies the profile and prevents a second
# coordinator anywhere in the same owner/root, regardless of actor scope.
MARKER = ('CREATE UNIQUE INDEX provider_cross_model_profile_v2 ON provider_operation_bindings '
          '(owner, root_operation_id) WHERE cross_model_coordinator IS NOT NULL')
_ROOT_NEW = "EXISTS (SELECT 1 FROM provider_operation_bindings p WHERE p.owner=NEW.owner AND p.root_operation_id=NEW.root_operation_id AND p.cross_model_coordinator IS NOT NULL)"
_ROOT_OLD = "EXISTS (SELECT 1 FROM provider_operation_bindings p WHERE p.owner=OLD.owner AND p.root_operation_id=OLD.root_operation_id AND p.cross_model_coordinator IS NOT NULL)"
_LEASE_ROOT_NEW = "EXISTS (SELECT 1 FROM provider_operation_bindings b JOIN provider_operation_bindings p ON p.owner=b.owner AND p.root_operation_id=b.root_operation_id WHERE b.id=NEW.binding_id AND p.cross_model_coordinator IS NOT NULL)"
_LEASE_ROOT_OLD = "EXISTS (SELECT 1 FROM provider_operation_bindings b JOIN provider_operation_bindings p ON p.owner=b.owner AND p.root_operation_id=b.root_operation_id WHERE b.id=OLD.binding_id AND p.cross_model_coordinator IS NOT NULL)"
TRIGGERS = {
    'p08_binding_insert': f"CREATE TRIGGER p08_binding_insert BEFORE INSERT ON provider_operation_bindings WHEN NEW.cross_model_coordinator IS NOT NULL OR {_ROOT_NEW} BEGIN SELECT CASE WHEN {FUNCTION}('insert',NEW.id,NEW.owner,NEW.root_operation_id,NEW.actor_scope_key,NULL,NEW.cross_model_coordinator) != 1 THEN RAISE(ABORT,'P08 writer unavailable') END; END",
    'p08_binding_update': f"CREATE TRIGGER p08_binding_update BEFORE UPDATE ON provider_operation_bindings WHEN OLD.cross_model_coordinator IS NOT NULL OR NEW.cross_model_coordinator IS NOT NULL OR {_ROOT_OLD} OR {_ROOT_NEW} BEGIN SELECT CASE WHEN OLD.id IS NOT NEW.id OR OLD.owner IS NOT NEW.owner OR OLD.root_operation_id IS NOT NEW.root_operation_id OR OLD.actor_scope_key IS NOT NEW.actor_scope_key OR {FUNCTION}('update',OLD.id,OLD.owner,OLD.root_operation_id,OLD.actor_scope_key,OLD.cross_model_coordinator,NEW.cross_model_coordinator) != 1 THEN RAISE(ABORT,'P08 writer unavailable') END; END",
    'p08_binding_delete': f"CREATE TRIGGER p08_binding_delete BEFORE DELETE ON provider_operation_bindings WHEN OLD.cross_model_coordinator IS NOT NULL OR {_ROOT_OLD} BEGIN SELECT CASE WHEN {FUNCTION}('delete',OLD.id,OLD.owner,OLD.root_operation_id,OLD.actor_scope_key,OLD.cross_model_coordinator,NULL) != 1 THEN RAISE(ABORT,'P08 writer unavailable') END; END",
    'p08_lease_insert': f"CREATE TRIGGER p08_lease_insert BEFORE INSERT ON provider_credential_leases WHEN {_LEASE_ROOT_NEW} OR {_ROOT_NEW} BEGIN SELECT CASE WHEN {FUNCTION}('lease-insert',NEW.binding_id,NEW.owner,NEW.root_operation_id,NEW.holder_id,NULL,json_array(NEW.lease_id_digest,NEW.credential_owner,NEW.holder_id,NEW.connection_id,NEW.account_id,NEW.model_id,NEW.grant_id,NEW.credential_revision)) != 1 THEN RAISE(ABORT,'P08 writer unavailable') END; END",
    'p08_lease_update': f"CREATE TRIGGER p08_lease_update BEFORE UPDATE ON provider_credential_leases WHEN {_LEASE_ROOT_OLD} OR {_LEASE_ROOT_NEW} OR {_ROOT_OLD} OR {_ROOT_NEW} BEGIN SELECT CASE WHEN OLD.binding_id IS NOT NEW.binding_id OR OLD.owner IS NOT NEW.owner OR OLD.root_operation_id IS NOT NEW.root_operation_id OR {FUNCTION}('lease-update',OLD.binding_id,OLD.owner,OLD.root_operation_id,'',NULL,NULL) != 1 THEN RAISE(ABORT,'P08 writer unavailable') END; END",
    'p08_lease_delete': f"CREATE TRIGGER p08_lease_delete BEFORE DELETE ON provider_credential_leases WHEN {_LEASE_ROOT_OLD} OR {_ROOT_OLD} BEGIN SELECT CASE WHEN {FUNCTION}('lease-delete',OLD.binding_id,OLD.owner,OLD.root_operation_id,'',NULL,NULL) != 1 THEN RAISE(ABORT,'P08 writer unavailable') END; END",
}
