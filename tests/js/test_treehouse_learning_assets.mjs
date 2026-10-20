import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import {evaluateLearningAsset, puzzleValid, puzzleMoves, puzzleSearch} from '../../static/js/copal/treehouseLearningAssets.js';

test('alias mutation and rebinding preserve the lesson states', () => {
  assert.equal(evaluateLearningAsset('cs-state-trace', {}, 0).rows.length, 1);
  assert.deepEqual(evaluateLearningAsset('cs-state-trace', {mode:'alias'}, 2).rows.map(row => row[2]), ['[1, 2]', '[1, 2]']);
  assert.deepEqual(evaluateLearningAsset('cs-state-trace', {mode:'copy'}, 2).rows.map(row => row[2]), ['[1]', '[1, 2]']);
  assert.deepEqual(evaluateLearningAsset('cs-state-trace', {}, 3).rows.map(row => row[2]), ['[1, 2]', '[9]']);
});

test('puzzle state space and both declared costs', () => {
  const valid = [];
  for(let ax=0;ax<2;ax++)for(let ay=0;ay<3;ay++)for(let bx=0;bx<3;bx++)for(let by=0;by<3;by++){
    const state=[ax,ay,bx,by];if(puzzleValid(state))valid.push(state);
  }
  assert.equal(valid.length,42);
  for(const state of valid)for(const move of puzzleMoves(state,true))assert.ok(puzzleValid(move.state));
  assert.equal(puzzleSearch(false).path.length,3);
  assert.equal(puzzleSearch(true).path.length,2);
  assert.equal(puzzleSearch(false,1).status,'bound reached');
  assert.throws(()=>puzzleSearch(false,Infinity));
  assert.equal(puzzleValid([0,0,1,0]),false);
  assert.equal(puzzleValid([2,0,1,1]),false);
});

test('context accounting distinguishes overflow and rejects invalid numbers', () => {
  assert.equal(evaluateLearningAsset('ai-context-budget').rows.at(-1)[1],2500);
  assert.match(evaluateLearningAsset('ai-context-budget',{evidence:9000}).summary,/Overflow/);
  for(const value of ['', 'bad', -1, Infinity, 100001])assert.throws(()=>evaluateLearningAsset('ai-context-budget',{evidence:value}));
  assert.throws(()=>evaluateLearningAsset('unknown'));
});

test('every reviewed asset has bounded finite default output', () => {
  const records=JSON.parse(fs.readFileSync('static/treehouse/assets/registry.json','utf8'));
  assert.equal(records.length,22);assert.equal(new Set(records.map(x=>x.id)).size,22);
  for(const record of records){const config=JSON.parse(fs.readFileSync(record.path,'utf8'));const input=Object.fromEntries(config.fields.map(f=>[f.key,f.default]));const result=evaluateLearningAsset(record.id,input,20);assert.ok(result.rows.length<=42);assert.ok(result.headers.length<=8);assert.ok(result.summary.length>10);assert.ok(!JSON.stringify(result).includes('NaN'));}
});

test('graph, lower bound and conditional probability handle structural boundaries', () => {
  assert.equal(evaluateLearningAsset('cs-graph-search',{start:'E'},20).rows.length,1);
  assert.equal(evaluateLearningAsset('cs-graph-search',{},20).rows.length,4);
  assert.match(evaluateLearningAsset('cs-boundary-search',{preset:'empty'},20).summary,/Lower bound 0/);
  assert.match(evaluateLearningAsset('cs-boundary-search',{preset:'duplicates',target:2},20).summary,/Lower bound 1/);
  assert.throws(()=>evaluateLearningAsset('cs-finite-probability',{w1:0,w2:0,condition:'positive'}),/zero probability/);
  assert.match(evaluateLearningAsset('cs-finite-probability',{w0:1,w1:2,w2:1}).summary,/Expectation = 1/);
});

test('frontier ties, payoff equilibrium and shader endpoints', () => {
  const frontier=evaluateLearningAsset('cs-finite-frontier').rows;
  assert.equal(frontier.find(x=>x[0]==='Wasteful').at(-1),'dominated');
  assert.equal(frontier.find(x=>x[0]==='Guard').at(-1),'frontier');
  assert.equal(frontier.find(x=>x[0]==='Twin').at(-1),'frontier');
  const payoff=evaluateLearningAsset('games-payoff-table');assert.equal(payoff.rows.filter(x=>x.at(-1)==='pure equilibrium').length,1);
  const matching={a0:1,b0:-1,a1:-1,b1:1,a2:-1,b2:1,a3:1,b3:-1};assert.equal(evaluateLearningAsset('games-payoff-table',matching).rows.filter(x=>x.at(-1)==='pure equilibrium').length,0);
  for(const [sample,expected]of [[0,.1],[.5,.25],[1,.4]]){const result=evaluateLearningAsset('games-shader-ramp',{sample,strength:.5});assert.ok(Math.abs(result.swatches[0].value-expected)<1e-9);assert.equal(result.swatches[0].value,result.swatches[1].value);}
});

test('generator validity, network replay, cache and save never invent success', () => {
  assert.deepEqual(evaluateLearningAsset('games-generator-check',{seed:6}),evaluateLearningAsset('games-generator-check',{seed:6}));
  assert.match(evaluateLearningAsset('games-generator-check',{seed:1}).summary,/INVALID/);
  assert.match(evaluateLearningAsset('games-generator-check',{seed:0}).summary,/exit reachable/);
  const comparison=evaluateLearningAsset('games-generator-check',{seed:0,compare:'yes'});assert.equal(comparison.comparisons.length,4);assert.match(comparison.summary,/seed 1: invalid/);
  assert.throws(()=>evaluateLearningAsset('games-generator-check',{seed:16}));
  const network=evaluateLearningAsset('games-network-replay',{ack:2,authority:1},5);assert.equal(network.replay.predicted,2);
  assert.match(evaluateLearningAsset('ai-memory-cache',{stored:'yes',cached:'yes',supplied:'no'}).rows.at(-1)[1],/no preference/);
  assert.match(evaluateLearningAsset('ai-save-revisions',{remote:'yes',action:'save'}).summary,/Conflict/);
  assert.equal(evaluateLearningAsset('ai-save-revisions',{remote:'yes',action:'save'}).rows.at(-1)[1],'Remote C');
  assert.equal(evaluateLearningAsset('ai-save-revisions',{remote:'no',action:'view'}).rows.at(-1)[1],'Original A');
  assert.equal(evaluateLearningAsset('ai-save-revisions',{remote:'no',action:'save'}).rows.at(-1)[1],'Edited B');
});

test('packet identity, delay, loss and correction actually drive reconciliation',()=>{
  const baseline=evaluateLearningAsset('games-network-replay',{ack:4,authority:1},8).replay;
  assert.equal(baseline.confirmed,4);assert.equal(baseline.correctionDelta,-1);
  const duplicate=evaluateLearningAsset('games-network-replay',{ack:4,authority:1,packet:'duplicate'},8).replay;
  assert.equal(duplicate.predicted,baseline.predicted);assert.ok(duplicate.receipt.some(x=>x[4]==='duplicate ignored'));
  const old=evaluateLearningAsset('games-network-replay',{ack:4,authority:1,packet:'old'},8).replay;
  assert.equal(old.predicted,baseline.predicted);assert.ok(old.receipt.some(x=>x[4]==='old tick ignored'));
  const delayed=evaluateLearningAsset('games-network-replay',{ack:4,authority:1,delay:5},4).replay;
  assert.equal(delayed.confirmed,0);assert.equal(delayed.correctionDelta,0);assert.ok(delayed.receipt.some(x=>x[4]==='pending'));
  const lost=evaluateLearningAsset('games-network-replay',{ack:3,authority:1,drop:'third'},5);
  assert.equal(lost.replay.confirmed,2);assert.match(lost.summary,/known dropped packet/);
});

test('graph presets, learner model categories and every remaining control family',()=>{
  assert.equal(evaluateLearningAsset('cs-graph-search',{graph:'disconnected'},20).rows.length,2);
  assert.equal(evaluateLearningAsset('cs-graph-search',{graph:'diamond',method:'dfs'},20).rows.length,4);
  assert.equal(evaluateLearningAsset('ai-model-axes',{card:4,axis:'task',category:'classification'}).feedback.matches,true);
  assert.equal(evaluateLearningAsset('ai-model-axes',{card:4,axis:'task',category:'embedding'}).feedback.matches,true);
  assert.equal(evaluateLearningAsset('ai-model-axes',{card:4,axis:'modality',category:'image'}).feedback.matches,false);
  assert.throws(()=>evaluateLearningAsset('games-payoff-table',{a0:.5}),/integers/);
  assert.equal(evaluateLearningAsset('cs-box-layout',{sizing:'border'}).rows.at(-1)[1],200);
  assert.equal(evaluateLearningAsset('cs-box-layout',{sizing:'content'}).rows.at(-1)[1],236);
  assert.match(evaluateLearningAsset('cs-feedback-trace',{factor:2,initial:100},20).summary,/Stopped/);
  assert.equal(evaluateLearningAsset('cs-compile-map',{stage:'process'}).rows[0][0],'Process');
  assert.match(evaluateLearningAsset('cs-binary-evidence',{excerpt:'intent',category:'inference'}).summary,/agrees/);
  assert.equal(evaluateLearningAsset('games-asset-handoff',{contract:'animated'}).rows.at(-1)[1],'retained');
  assert.equal(evaluateLearningAsset('ai-delegation-cost',{workers:3,lead:1000,worker:1200}).rows[0][1],4600);
  assert.match(evaluateLearningAsset('ai-native-availability',{card:'host'}).rows[0][2],/unavailable/);
  for(const stored of ['yes','no'])for(const supplied of ['yes','no'])for(const cached of ['yes','no'])assert.equal(evaluateLearningAsset('ai-memory-cache',{stored,supplied,cached}).rows.at(-1)[1],supplied==='yes'?'preference supplied':'no preference supplied');
});


test('one received history drives exact, pending, drop and bracket displays', () => {
  const exact=evaluateLearningAsset('games-network-replay',{ack:2,authority:1,drop:'third'},0);
  assert.deepEqual(exact.display.accepted,{0:0,1:0,2:1});
  assert.equal(exact.display.mode,'exact'); assert.equal(exact.display.value,0);
  assert.equal(exact.replay.correctionDelta,-1);
  assert.ok(!exact.replay.events.some(p=>p.status==='dropped'));
  const pending=evaluateLearningAsset('games-network-replay',{ack:2,authority:1,delay:2,drop:'third'},0);
  assert.deepEqual(pending.display.accepted,{0:0});assert.equal(pending.replay.predicted,2);
  assert.equal(pending.display.mode,'hold');assert.equal(pending.display.reason,'later packets pending');
  const lost=evaluateLearningAsset('games-network-replay',{ack:3,authority:1,drop:'third'},4);
  assert.deepEqual(lost.display.accepted,{0:0,1:1,2:2});assert.equal(lost.replay.predicted,2);
  assert.equal(lost.display.value,2);assert.equal(lost.display.reason,'known dropped packet');
  const bracket=evaluateLearningAsset('games-network-replay',{ack:4,authority:2,drop:'third'},4);
  assert.equal(bracket.display.mode,'interpolate');assert.equal(bracket.display.time,3);assert.equal(bracket.display.value,2);
  for(let ack=0;ack<=20;ack++)for(let delay=0;delay<=5;delay++) {
    const value=evaluateLearningAsset('games-network-replay',{ack,delay,drop:'third',packet:'old'},20);
    assert.ok(value.rows.length<=22);assert.ok(Number.isFinite(value.display.value));
    assert.ok(value.replay.events.filter(x=>x.status==='received').every(x=>x.arrival<=20));
  }
});

test('complete generator order matches all eight declared row/count cases', () => {
  const expected=[
    [0,'no',['S....','#....','.#...','##...','.#..E'],18,true],
    [0,'yes',['S....','#....','.#...','##...','.#..E'],18,true],
    [1,'no',['S...#','.....','#..##','#...#','#..#E'],16,false],
    [1,'yes',['S...#','.....','#..##','#....','#..#E'],18,true],
    [2,'no',['S....','#..#.','#....','#....','.#..E'],19,true],
    [2,'yes',['S....','#..#.','#....','#....','.#..E'],19,true],
    [3,'no',['S#...','.#...','..#..','###.#','..##E'],4,false],
    [3,'yes',['S#...','.#...','..#..','###..','..##E'],4,false],
  ];
  for(const [seed,repair,rows,reachable,valid]of expected){
    const value=evaluateLearningAsset('games-generator-check',{seed,repair});
    assert.deepEqual(value.rows.map(r=>r[1].replaceAll(' ','')),rows);
    assert.deepEqual(value.validation,{reachable,valid});
    assert.match(value.summary,/Even seeds carve top row/);assert.match(value.summary,/repair opens \(4,3\) last/);
  }
});

test('toy tokenizer preserves whitespace, vocabulary choice and unknown stop', () => {
  const a=evaluateLearningAsset('ai-tokenization-trace',{text:'a',vocabulary:'v1'},20);
  assert.deepEqual(a.tokenization.ids,[7,3,4,5,6]);assert.equal(a.tokenization.count,5);
  assert.match(a.rows[1][1],/U\+0020/);
  const b=evaluateLearningAsset('ai-tokenization-trace',{text:'b'},20);assert.deepEqual(b.tokenization.ids,[1,3,2,4,5,6]);
  assert.deepEqual(evaluateLearningAsset('ai-tokenization-trace',{text:'a',vocabulary:'v2'},20).tokenization.ids,[1,2,3,4,5,6]);
  const unknown=evaluateLearningAsset('ai-tokenization-trace',{text:'unknown'},20);
  assert.equal(unknown.tokenization.count,null);assert.equal(unknown.tokenization.unknown,'2');assert.match(unknown.summary,/full count unknown/);
  assert.equal(evaluateLearningAsset('ai-tokenization-trace',{},0).rows.length,0);
  assert.equal(evaluateLearningAsset('ai-tokenization-trace',{},1).rows.length,1);
  assert.throws(()=>evaluateLearningAsset('ai-tokenization-trace',{text:'arbitrary'}));
  const binary=evaluateLearningAsset('cs-binary-evidence',{excerpt:'bytes',category:'observation'});
  assert.match(binary.rows[0][1],/Given SP\+12 contains 2/);assert.match(binary.rows[0][1],/No disassembler/);
});
