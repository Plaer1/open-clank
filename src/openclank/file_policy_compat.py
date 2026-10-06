"""Canonical compatibility projections; no legacy permission authority lives here."""
from __future__ import annotations

import hashlib
import json
import os
import time
from datetime import datetime, timezone
from pathlib import Path

COMPAT_SCHEMA = """
CREATE TABLE IF NOT EXISTS file_policy_subject_aliases (
 username TEXT PRIMARY KEY, subject_id TEXT NOT NULL, is_admin INTEGER NOT NULL DEFAULT 0);
CREATE TABLE IF NOT EXISTS file_policy_legacy_aliases (
 kind TEXT NOT NULL, legacy_id TEXT NOT NULL, target_id TEXT NOT NULL,
 PRIMARY KEY(kind,legacy_id));
CREATE TABLE IF NOT EXISTS file_policy_import_sources (
 id TEXT PRIMARY KEY, source_hash TEXT NOT NULL, source_ciphertext TEXT NOT NULL,
 complete INTEGER NOT NULL DEFAULT 0, created_unix_ms INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS file_policy_import_items (
 id TEXT PRIMARY KEY, kind TEXT NOT NULL, legacy_id TEXT NOT NULL,
 subject_id TEXT, owner_username TEXT NOT NULL DEFAULT '', source_json TEXT NOT NULL,
 status TEXT NOT NULL DEFAULT 'unresolved', reason TEXT NOT NULL,
 canonical_id TEXT, UNIQUE(kind,legacy_id));
CREATE TABLE IF NOT EXISTS file_policy_approval_details (
 binding_id TEXT PRIMARY KEY REFERENCES file_policy_bindings(id) ON DELETE CASCADE,
 permission_type TEXT NOT NULL, pattern TEXT NOT NULL, resource TEXT NOT NULL DEFAULT '');
"""


def _json(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=False)


def expiry_ms(value):
    if value in (None, ''):
        return None
    if isinstance(value, (int, float)):
        return int(value)
    parsed = datetime.fromisoformat(str(value))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return int(parsed.timestamp() * 1000)


class CanonicalCompatibility:
    def expire_bindings(self):
        now = int(time.time() * 1000)
        with self._connect() as conn:
            self._begin(conn)
            ids = [row[0] for row in conn.execute("SELECT id FROM file_policy_bindings WHERE status='active' AND expires_unix_ms IS NOT NULL AND expires_unix_ms<=?", (now,))]
            if not ids:
                conn.rollback()
                return
            generation = self._bump(conn)
            conn.execute("UPDATE file_policy_bindings SET status='expired',generation=?,revision=revision+1,updated_unix_ms=? WHERE status='active' AND expires_unix_ms IS NOT NULL AND expires_unix_ms<=?", (generation, now, now))
            self._audit(conn, generation=generation, event='expire', actor_subject_id='system', target_type='bindings', target_id='expiry', reason_code='approval_expired', details={'count': len(ids)})
            conn.commit()

    def sync_subjects(self, users, *, complete=True):
        # Auth remains the identity authority, SQLite owns all permission state.
        with self._connect() as conn:
            self._begin(conn)
            changed = False
            records = dict(users)
            valid_names = {str(name).strip().lower() for name, record in records.items() if isinstance(record, dict) and record.get('account_id')}
            for row in (conn.execute('SELECT username FROM file_policy_subject_aliases').fetchall() if complete else []):
                if row['username'].startswith('deleted:'):
                    if conn.execute('UPDATE file_policy_subject_aliases SET is_admin=0 WHERE username=? AND is_admin!=0', (row['username'],)).rowcount:
                        changed = True
                    continue
                if row['username'] not in valid_names:
                    conn.execute('DELETE FROM file_policy_subject_aliases WHERE username=?', (row['username'],))
                    changed = True
            for name, record in records.items():
                if not isinstance(record, dict) or not record.get('account_id'):
                    continue
                values = (str(name).strip().lower(), str(record['account_id']), int(record.get('is_admin') is True))
                old = conn.execute('SELECT subject_id,is_admin FROM file_policy_subject_aliases WHERE username=?', (values[0],)).fetchone()
                if old and tuple(old) == values[1:]:
                    continue
                conn.execute('INSERT INTO file_policy_subject_aliases VALUES (?,?,?) ON CONFLICT(username) DO UPDATE SET subject_id=excluded.subject_id,is_admin=excluded.is_admin', values)
                changed = True
            if changed:
                generation = self._bump(conn)
                self._audit(conn, generation=generation, event='identity_projection', actor_subject_id='system', target_type='subjects', target_id='auth', reason_code='identity_sync')
            conn.commit()

    def move_subject_alias(self, source_name, target_name, expected_subject):
        from src.openclank.file_policy import FilePolicyError
        source_name, target_name = str(source_name).strip().lower(), str(target_name).strip().lower()
        if not source_name or not target_name or source_name == target_name or not expected_subject:
            raise FilePolicyError('Invalid account alias move', code='owner_conflict')
        with self._connect() as conn:
            self._begin(conn)
            source = conn.execute('SELECT subject_id FROM file_policy_subject_aliases WHERE username=?', (source_name,)).fetchone()
            target = conn.execute('SELECT subject_id FROM file_policy_subject_aliases WHERE username=?', (target_name,)).fetchone()
            if source and source[0] == expected_subject and not target:
                conn.execute('UPDATE file_policy_subject_aliases SET username=?,is_admin=CASE WHEN ? THEN 0 ELSE is_admin END WHERE username=?', (target_name, int(target_name.startswith('deleted:')), source_name))
                generation = self._bump(conn)
                self._audit(conn, generation=generation, event='rename_alias', actor_subject_id=expected_subject,
                    target_type='subject', target_id=expected_subject, reason_code='account_renamed')
                conn.commit()
                return 'applied'
            if not source and target and target[0] == expected_subject:
                conn.rollback()
                return 'already_applied'
            conn.rollback()
            raise FilePolicyError('Account alias changed after preflight', code='owner_conflict')

    def subject_for_username(self, username):
        key = str(username or '').strip().lower()
        with self._connect() as conn:
            row = conn.execute('SELECT subject_id FROM file_policy_subject_aliases WHERE username=?', (key,)).fetchone()
        return str(row[0]) if row else None

    def username_for_subject(self, subject):
        with self._connect() as conn:
            row = conn.execute('SELECT username FROM file_policy_subject_aliases WHERE subject_id=? ORDER BY username LIMIT 1', (subject,)).fetchone()
        return str(row[0]) if row else None

    def subject_is_admin(self, subject):
        with self._connect() as conn:
            return bool(conn.execute('SELECT 1 FROM file_policy_subject_aliases WHERE subject_id=? AND is_admin=1', (subject,)).fetchone())

    def legacy_target(self, kind, identifier, *, fallback=None):
        """Resolve an alias key, retaining the caller's canonical ID on a miss."""
        with self._connect() as conn:
            row = conn.execute('SELECT target_id FROM file_policy_legacy_aliases WHERE kind=? AND legacy_id=?', (kind, identifier)).fetchone()
        return str(row[0]) if row else str(identifier if fallback is None else fallback)

    def approval_details(self, binding_id):
        with self._connect() as conn:
            row = conn.execute('SELECT permission_type,pattern,resource FROM file_policy_approval_details WHERE binding_id=?', (binding_id,)).fetchone()
        return dict(row) if row else None

    def store_approval_details(self, binding_id, permission_type, pattern, resource=''):
        with self._connect() as conn:
            self._begin(conn)
            changed = conn.execute('INSERT OR IGNORE INTO file_policy_approval_details VALUES (?,?,?,?)', (binding_id, permission_type, pattern, resource)).rowcount
            if changed:
                generation = self._bump(conn)
                conn.execute('UPDATE file_policy_bindings SET generation=?,revision=revision+1,updated_unix_ms=? WHERE id=?', (generation, int(time.time()*1000), binding_id))
                self._audit(conn, generation=generation, event='approval_details', actor_subject_id='system', target_type='binding', target_id=binding_id, reason_code='approval_recorded')
            conn.commit()

    def approval_match(self, *, subject_id, permission_type, filepath='', session_id='', workspace_id='', resource=''):
        # These bindings supply consent only; filesystem/process doors still run.
        self.expire_bindings()
        now = int(time.time() * 1000)
        with self._connect() as conn:
            self._begin(conn)
            rows = conn.execute('''SELECT b.*,d.pattern,d.resource FROM file_policy_bindings b
                JOIN file_policy_approval_details d ON d.binding_id=b.id
                WHERE b.subject_id=? AND b.subject_kind='user' AND b.binding_class='operation'
                  AND b.operation=? AND b.status='active'
                  AND (b.expires_unix_ms IS NULL OR b.expires_unix_ms>?) ORDER BY b.id''', (subject_id, permission_type, now)).fetchall()
            for row in rows:
                if row['chat_id'] and row['chat_id'] != session_id:
                    continue
                if row['workspace_id'] and row['workspace_id'] != workspace_id:
                    continue
                if row['resource'] and row['resource'] != resource:
                    continue
                if row['workspace_id']:
                    ws = conn.execute('SELECT * FROM file_policy_workspaces WHERE id=? AND archived=0 AND owner_subject_id=?', (row['workspace_id'], subject_id)).fetchone()
                    if not ws:
                        continue
                if row['location_id']:
                    loc = conn.execute('SELECT * FROM file_policy_locations WHERE id=? AND enabled=1 AND availability=\'available\'', (row['location_id'],)).fetchone()
                    if not loc or not set(json.loads(row['capabilities_json'])).issubset(json.loads(loc['capabilities_json'])):
                        continue
                    if filepath:
                        target = os.path.realpath(filepath)
                        root = loc['canonical_path']
                        try:
                            inside = target == root if loc['kind'] == 'exact_file' else os.path.commonpath([root, target]) == root
                        except ValueError:
                            inside = False
                        if not inside:
                            continue
                pattern = row['pattern']
                if pattern != '*' and not (filepath and (filepath == pattern or filepath.startswith(pattern.rstrip('/') + '/'))):
                    continue
                if row['lifetime'] == 'once':
                    if row['remaining_uses'] != 1:
                        continue
                    generation = self._bump(conn)
                    conn.execute("UPDATE file_policy_bindings SET status='consumed',remaining_uses=0,generation=?,revision=revision+1,updated_unix_ms=? WHERE id=?", (generation, now, row['id']))
                    self._audit(conn, generation=generation, event='consume', actor_subject_id=subject_id, target_type='binding', target_id=row['id'], reason_code='once_consumed')
                conn.commit()
                return True
            conn.rollback()
        return False

    def people_capabilities(self, subject_id, location_id, *, workspace_id=None, chat_id=None, connection=None, binding_class="people"):
        if binding_class not in {"people", "agent"}:
            raise ValueError("unsupported path binding class")
        def read(conn):
            locations = {row['id']: dict(row) for row in conn.execute('SELECT * FROM file_policy_locations')}
            target = locations.get(location_id)
            if not target or not target['enabled'] or target['availability'] != 'available':
                return set()
            ceiling = set()
            now = int(time.time() * 1000)
            for b in conn.execute("SELECT * FROM file_policy_bindings WHERE subject_kind='user' AND subject_id=? AND binding_class=? AND status='active' AND (expires_unix_ms IS NULL OR expires_unix_ms>?)", (subject_id, binding_class, now)):
                source = locations.get(b['location_id'])
                if not source or not source['enabled'] or source['availability'] != 'available':
                    continue
                if b['workspace_id'] and b['workspace_id'] != workspace_id:
                    continue
                if b['chat_id'] and b['chat_id'] != chat_id:
                    continue
                if source['id'] != target['id'] and b['lifetime'] != 'always':
                    continue
                try:
                    contains = source['canonical_path'] == target['canonical_path'] if source['kind'] == 'exact_file' else os.path.commonpath([source['canonical_path'], target['canonical_path']]) == source['canonical_path']
                except ValueError:
                    contains = False
                if contains:
                    ceiling |= set(json.loads(b['capabilities_json'])) & set(json.loads(source['capabilities_json']))
            return ceiling & set(json.loads(target['capabilities_json']))
        if connection is not None:
            return read(connection)
        with self._connect() as conn:
            conn.execute('BEGIN')
            result = read(conn)
            conn.commit()
            return result

    def registry_projection(self):
        self.expire_bindings()
        # One transaction gives roots, bindings and their generation one view.
        with self._connect() as conn:
            conn.execute('BEGIN')
            generation = self._generation(conn)
            locations = {row['id']: dict(row) for row in conn.execute('SELECT * FROM file_policy_locations')}
            bindings = [dict(row) for row in conn.execute("SELECT * FROM file_policy_bindings WHERE binding_class IN ('people','agent')")]
            names = {row['subject_id']: row['username'] for row in conn.execute('SELECT * FROM file_policy_subject_aliases ORDER BY username DESC')}
            admins = {row['subject_id'] for row in conn.execute('SELECT * FROM file_policy_subject_aliases WHERE is_admin=1')}
            aliases = {row['target_id']: row['legacy_id'] for row in conn.execute("SELECT * FROM file_policy_legacy_aliases WHERE kind='root' ORDER BY legacy_id DESC")}
            conn.commit()
        roots, assignments = {}, {}
        now = int(time.time() * 1000)
        def active(b):
            # Scoped bindings are selected separately by agent_scope, never
            # projected into an unbound always lane.
            return b['status'] == 'active' and (b['expires_unix_ms'] is None or b['expires_unix_ms'] > now)
        def physical(loc, identifier, owner, caps, enabled):
            return dict(id=identifier, location_id=loc['id'], owner_id=owner,
                kind='exact_file' if loc['kind'] == 'exact_file' else 'recursive_directory',
                canonical_path=loc['canonical_path'], display_path=loc['display_path'],
                enabled=bool(enabled and loc['enabled']), capabilities=sorted(caps),
                platform_identity=json.loads(loc['platform_identity_json']),
                availability=loc['availability'], last_validated_unix_ms=loc['updated_unix_ms'])
        for loc in locations.values():
            roots[loc['id']] = physical(loc, loc['id'], '__openclank_app_visibility__', json.loads(loc['capabilities_json']), loc['enabled'])
        for b in bindings:
            loc = locations.get(b['location_id'])
            if not loc or b['subject_kind'] != 'user':
                continue
            name = names.get(b['subject_id'], 'deleted:' + b['subject_id'])
            caps = set(json.loads(b['capabilities_json'])) & set(json.loads(loc['capabilities_json']))
            if b['binding_class'] == 'people':
                assignments[b['id']] = dict(id=b['id'], root_id=loc['id'], subject_kind='user', subject_id=name,
                    issuer_id=names.get(b['created_by_subject_id'], 'deleted:' + b['created_by_subject_id']),
                    capabilities=sorted(caps), enabled=active(b) and b['lifetime'] == 'always', generation=generation)
            else:
                identifier = aliases.get(b['id'], b['id'])
                root = physical(loc, identifier, name, caps, active(b) and bool(caps))
                root.update(physical_capabilities=json.loads(loc['capabilities_json']), binding_id=b['id'], lifetime=b['lifetime'], workspace_id=b['workspace_id'], chat_id=b['chat_id'])
                roots[identifier] = root
        return dict(version=1, generation=generation, roots=roots, visibility_assignments=assignments)

    def apply_registry_projection(self, before, after):
        """Translate one legacy mutation into canonical records; never write JSON."""
        from src.openclank.file_policy import FilePolicyError
        with self._connect() as conn:
            self._begin(conn)
            if self._generation(conn) != before['generation']:
                conn.rollback()
                raise FilePolicyError('File access changed; retry', code='policy_generation_changed')
            now = int(time.time() * 1000)
            generation = self._generation(conn)
            def bump():
                nonlocal generation
                if generation == before['generation']:
                    generation = self._bump(conn)
                return generation
            def subject(name):
                row = conn.execute('SELECT subject_id FROM file_policy_subject_aliases WHERE username=?', (str(name).strip().lower(),)).fetchone()
                if not row:
                    raise FilePolicyError('Immutable account identity is unavailable', code='subject_required')
                return row[0]
            def binding(identifier, row, klass, loc, sid):
                caps = set(row.get('capabilities') or [])
                location = conn.execute('SELECT * FROM file_policy_locations WHERE id=?', (loc,)).fetchone()
                if not location or not caps or not caps.issubset(json.loads(location['capabilities_json'])):
                    raise FilePolicyError('Binding exceeds Location capabilities', code='capability_escalation')
                exists = conn.execute('SELECT id FROM file_policy_bindings WHERE id=?', (identifier,)).fetchone()
                status = 'active' if row.get('enabled') else 'revoked'
                if exists:
                    conn.execute('UPDATE file_policy_bindings SET capabilities_json=?,status=?,generation=?,revision=revision+1,updated_unix_ms=? WHERE id=?', (_json(sorted(caps)), status, bump(), now, identifier))
                else:
                    conn.execute('''INSERT INTO file_policy_bindings
                        (id,binding_class,subject_kind,subject_id,location_id,capabilities_json,lifetime,status,created_by_subject_id,generation,revision,created_unix_ms,updated_unix_ms)
                        VALUES (?,?,'user',?,?,?,'always',?,?,?,1,?,?)''', (identifier, klass, sid, loc, _json(sorted(caps)), status, sid, bump(), now, now))
            for identifier, old in before['roots'].items():
                row = after['roots'].get(identifier)
                if row is None:
                    if old.get('binding_id'):
                        conn.execute("UPDATE file_policy_bindings SET status='revoked',generation=?,revision=revision+1,updated_unix_ms=? WHERE id=?", (bump(), now, old['binding_id']))
                    else:
                        conn.execute('UPDATE file_policy_locations SET enabled=0,generation=?,revision=revision+1,updated_unix_ms=? WHERE id=?', (bump(), now, old['location_id']))
                    continue
                if row == old:
                    continue
                if row.get('owner_id') != old.get('owner_id'):
                    # A lifecycle rename moves only the username alias; all
                    # authority remains on the same immutable subject.
                    conn.execute('UPDATE file_policy_subject_aliases SET username=? WHERE username=?', (row['owner_id'], old['owner_id']))
                    bump()
                if old.get('binding_id'):
                    if row.get('capabilities') != old.get('capabilities') or row.get('enabled') != old.get('enabled'):
                        binding(old['binding_id'], row, 'agent', old['location_id'], subject(row['owner_id']))
                else:
                    conn.execute('UPDATE file_policy_locations SET enabled=?,capabilities_json=?,generation=?,revision=revision+1,updated_unix_ms=? WHERE id=?', (int(bool(row['enabled'])), _json(row['capabilities']), bump(), now, old['location_id']))
            for identifier, row in after['roots'].items():
                if identifier in before['roots']:
                    continue
                kind = 'exact_file' if row['kind'] == 'exact_file' else ('whole_root' if Path(row['canonical_path']) == Path(Path(row['canonical_path']).anchor) else 'directory')
                loc = conn.execute('SELECT * FROM file_policy_locations WHERE canonical_key=? AND kind=?', (os.path.normcase(row['canonical_path']), kind)).fetchone()
                sid = subject(row['owner_id']) if row['owner_id'] != '__openclank_app_visibility__' else 'system'
                if not loc:
                    loc_id = 'location-' + hashlib.sha256((kind + ':' + row['canonical_path']).encode()).hexdigest()
                    conn.execute('''INSERT INTO file_policy_locations
                        (id,kind,canonical_path,canonical_key,display_path,capabilities_json,platform_identity_json,availability,enabled,generation,revision,created_by_subject_id,created_unix_ms,updated_unix_ms)
                        VALUES (?,?,?,?,?,?,?,?,?,?,1,?,?,?)''', (loc_id,kind,row['canonical_path'],os.path.normcase(row['canonical_path']),row['display_path'],_json(row['capabilities']),_json(row['platform_identity']),row['availability'],int(row['enabled']),bump(),sid,now,now))
                else:
                    loc_id = loc['id']
                    if not loc['enabled'] or not set(row['capabilities']).issubset(json.loads(loc['capabilities_json'])):
                        raise FilePolicyError('Existing Location restrictions win', code='capability_escalation')
                if sid != 'system':
                    # Non-admin legacy callers must have People access already.
                    if not conn.execute('SELECT 1 FROM file_policy_subject_aliases WHERE subject_id=? AND is_admin=1', (sid,)).fetchone():
                        ceiling = self.people_capabilities(sid, loc_id, connection=conn)
                        if not set(row['capabilities']).issubset(ceiling):
                            raise FilePolicyError('Agent access exceeds People access', code='capability_escalation')
                    binding(identifier, row, 'agent', loc_id, sid)
            for identifier, old in before['visibility_assignments'].items():
                row = after['visibility_assignments'].get(identifier)
                if row is None:
                    conn.execute("UPDATE file_policy_bindings SET status='revoked',generation=?,revision=revision+1,updated_unix_ms=? WHERE id=?", (bump(),now,identifier))
                elif row != old:
                    if row['subject_id'] != old['subject_id']:
                        conn.execute('UPDATE file_policy_subject_aliases SET username=? WHERE username=?', (row['subject_id'],old['subject_id']))
                        bump()
                    if row.get('capabilities') != old.get('capabilities') or row.get('enabled') != old.get('enabled'):
                        binding(identifier, row, 'people', before['roots'][old['root_id']]['location_id'], subject(row['subject_id']))
            for identifier, row in after['visibility_assignments'].items():
                if identifier in before['visibility_assignments']:
                    continue
                if row.get('subject_kind') != 'user':
                    raise FilePolicyError('Immutable group membership is unavailable', code='subject_required')
                root = after['roots'][row['root_id']]
                binding(identifier,row,'people',root.get('location_id') or row['root_id'],subject(row['subject_id']))
            if generation != before['generation']:
                self._audit(conn,generation=generation,event='compatibility_mutation',actor_subject_id='system',target_type='policy',target_id='registry-adapter',reason_code='canonical_adapter')
            conn.commit()

    def migration_state(self, *, subject_id=None, include_inactive=False):
        with self._connect() as conn:
            source = conn.execute("SELECT source_hash,complete FROM file_policy_import_sources WHERE id='legacy-v1'").fetchone()
            rows = [dict(row) for row in conn.execute('SELECT * FROM file_policy_import_items ORDER BY kind,legacy_id')]
        if subject_id is not None:
            rows = [row for row in rows if row['subject_id'] == subject_id]
        unresolved = sum(row['status'] == 'unresolved' for row in rows)
        imported = sum(row['status'] == 'imported' for row in rows)
        items = []
        for row in rows:
            if include_inactive or row['status'] == 'unresolved':
                row['source'] = json.loads(row.pop('source_json'))
                resolvable = row['reason'] in {'unknown_subject', 'ambiguous_workspace', 'ambiguous_location', 'outside_location'}
                row['actions'] = (["retry", "revoke"] + (["resolve"] if resolvable else [])) if row['status'] == 'unresolved' else ["revoke"] if row['status'] == 'imported' else []
                items.append(row)
        return dict(complete=bool(source and source['complete']), source_hash=source['source_hash'] if source else None,
                    imported_count=imported, unresolved_count=unresolved, items=items)
