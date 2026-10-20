import assert from 'node:assert/strict';
import test from 'node:test';
import fs from 'node:fs/promises';
import ts from '../../packages/mimo-code/packages/opencode/node_modules/typescript/lib/typescript.js';
import { NATIVE_SETTINGS_REFERENCE, NATIVE_SETTINGS_SOURCE_DECLARATIONS, NATIVE_SETTINGS_SOURCE_ROOTS, NATIVE_SETTINGS_SOURCE_EXCLUSIONS, getNativeSettingsReference, findNativeSettingReference, findNativeSettingReferences, searchNativeSettingsReference, unclassifiedNativeSettingDeclarations } from '../../static/js/nativeSettingsReference.js';

function schemaDeclarations(text, filename) {
  const source = ts.createSourceFile(filename, text, ts.ScriptTarget.Latest, true, ts.ScriptKind.TS);
  const isSchemaObject = object => {
    const call = object.parent;
    return ts.isCallExpression(call) && (/^(?:Schema\.Struct|z\.object)$/.test(call.expression.getText(source)) || /^Schema\.Class/.test(call.expression.getText(source)));
  };
  const keys = new Set();
  function visit(node) {
    if (ts.isPropertyAssignment(node) && ts.isObjectLiteralExpression(node.parent) && isSchemaObject(node.parent)) {
      const segments = [];
      let symbol = '';
      for (let current = node; current; current = current.parent) {
        if (ts.isPropertyAssignment(current) && ts.isObjectLiteralExpression(current.parent) && isSchemaObject(current.parent)) {
          if (ts.isIdentifier(current.name) || ts.isStringLiteral(current.name)) segments.unshift(current.name.text);
        }
        if (ts.isClassDeclaration(current)) { symbol = current.name?.text || 'AnonymousClass'; break; }
        if (ts.isVariableDeclaration(current)) { symbol = current.name.getText(source); break; }
      }
      if (symbol && segments.length) keys.add(`${symbol}.${segments.join('.')}`);
    }
    ts.forEachChild(node, visit);
  }
  visit(source);
  return [...keys].sort();
}

const normalized = path => path.replace(/\.\{[^}]+\}/g, '');

test('reference reconciles every current symbolic native schema declaration to a classified template', async () => {
  const templates = new Set(NATIVE_SETTINGS_REFERENCE.map(row => normalized(row.path)));
  for (const [module, expected] of Object.entries(NATIVE_SETTINGS_SOURCE_DECLARATIONS)) {
    const source = await fs.readFile(new URL(`../../packages/mimo-code/packages/opencode/src/config/${module}`, import.meta.url), 'utf8');
    const actual = schemaDeclarations(source, module);
    assert.deepEqual(actual, expected, `${module}: source additions/removals must receive explicit reference review`);
    for (const declaration of actual) {
      const [symbol, ...tail] = declaration.split('.');
      if (NATIVE_SETTINGS_SOURCE_EXCLUSIONS[`${module}:${symbol}`]) continue;
      const root = NATIVE_SETTINGS_SOURCE_ROOTS[module]?.[symbol];
      assert.notEqual(root, undefined, `${module}:${symbol} is unclassified`);
      assert.ok(templates.has((root ? root + '.' : '') + tail.join('.')), `${module}:${declaration} has no classified native path template`);
    }
  }
  assert.equal(new Set(NATIVE_SETTINGS_REFERENCE.map(row => row.path)).size, NATIVE_SETTINGS_REFERENCE.length);
});

test('structural reconciliation detects a new nested field even when its leaf name already exists', () => {
  const source = 'const InfoSchema = Schema.Struct({compaction: Schema.Struct({auto: Schema.Boolean}), memory: Schema.Struct({auto: Schema.Boolean})});';
  const declarations = schemaDeclarations(source, 'config.ts');
  assert.ok(unclassifiedNativeSettingDeclarations('config.ts', declarations).includes('InfoSchema.memory.auto'));
  assert.equal(unclassifiedNativeSettingDeclarations('config.ts', declarations).includes('InfoSchema.compaction.auto'), false);
});

test('only explicit record reference paths configure children; unsupported siblings and protected families stay locked', () => {
  const controls = [
    { path: 'checkpoint', referencePaths: ['checkpoint.thresholds'], writable: true, destination: {panel:'ai',field:'checkpoint'} },
    { path: 'memory', referencePaths: ['memory.graph.enabled'], writable: true },
    { path: 'provider', referencePaths: ['provider.{provider}.options.apiKey'], writable: true },
    { path: 'agent', coversDescendants: true, referencePaths: ['agent.*.model','agent.*.description','agent.*.options','agent.*.permission','agent.*.tools','agent.*.{unknown}'], writable: true },
  ];
  const rows = getNativeSettingsReference({controls});
  assert.equal(rows.find(row => row.path === 'checkpoint.thresholds').disposition,'configured-control');
  assert.equal(rows.find(row => row.path === 'checkpoint.thresholds').destination.field,'checkpoint');
  assert.equal(rows.find(row => row.path === 'memory.graph.enabled').disposition,'declared-only');
  assert.equal(rows.find(row => row.path.endsWith('.options.apiKey')).writable,false);
  assert.equal(rows.find(row => row.path.includes('.retry.maxJudge')).disposition,'declared-only');
  assert.equal(rows.find(row => row.path === 'checkpoint.task_cleanup_days').disposition,'compatibility');
  assert.equal(rows.find(row => row.path === 'model').disposition,'host-equivalent');
  assert.equal(rows.find(row => row.path === 'agent.{name}.model').disposition,'configured-control');
  for (const path of ['agent.{name}.options','agent.{name}.permission','agent.{name}.tools']) {
    assert.equal(rows.find(row => row.path === path).disposition,'owner-restricted', path);
    assert.equal(rows.find(row => row.path === path).writable,false,path);
  }
});

test('dynamic placeholders resolve concrete named models, MCP branches and plugin option indices', () => {
  assert.equal(findNativeSettingReference('provider.mine.models.claude-4.8.limit.context').path,'provider.{provider}.models.{model}.limit.context');
  const remote = findNativeSettingReferences('mcp.server.sampling',{transport:'remote'});
  assert.ok(remote.some(row => row.path === 'mcp.{name}[remote].sampling'));
  assert.equal(remote.some(row => row.path === 'mcp.{name}[local].sampling'),false);
  assert.equal(findNativeSettingReference('plugin[0][1].example').path,'plugin[][1].{option}');
});

test('explicit unavailable control preserves owner restriction and host reason/destination without claiming a writer', () => {
  const rows = getNativeSettingsReference({controls:[{path:'instructions',supported:false,writable:true,overrideDisposition:true,availability:'available',reason:'INSTRUCTION_SOURCE_OWNER_REQUIRED',destination:{panel:'ai',field:'instructions'}}]});
  const instructions=rows.find(row=>row.path==='instructions');
  assert.equal(instructions.availability,'unavailable');assert.equal(instructions.writable,false);
  assert.equal(instructions.disposition,'owner-restricted');assert.equal(instructions.reason,'INSTRUCTION_SOURCE_OWNER_REQUIRED');
  assert.deepEqual(instructions.destination,{panel:'ai',field:'instructions'});
  const baseline=getNativeSettingsReference().find(row=>row.path==='instructions');
  assert.match(baseline.reason,/Files authority/);assert.match(baseline.reason,/Hexes owner/);
});

test('authored reference search supports translated descriptions while paths and read-only disposition stay stable', () => {
  const translate = text => text === 'Checkpoint section token budgets' ? '检查点区块预算' : text;
  const rows = searchNativeSettingsReference('检查点',{translate});
  assert.equal(rows[0].path,'checkpoint.push_caps');
  assert.equal(rows[0].writable,false);
  assert.equal(findNativeSettingReference('memory.graph.enabled').disposition,'declared-only');
  assert.equal(findNativeSettingReference('server.port').disposition,'operator');
});
