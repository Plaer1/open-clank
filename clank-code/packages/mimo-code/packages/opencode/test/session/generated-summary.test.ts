import { expect, test } from 'bun:test'
import { spawnSync } from 'node:child_process'
import { createHash } from 'node:crypto'
import path from 'node:path'
import { zTextResourceContents } from '../../node_modules/@agentclientprotocol/sdk/dist/schema/zod.gen.js'
import { generatedAttachmentCandidate, sourceEvidence, contextFrame, projectContextInspection } from '../../src/session/context-inspection'

test('reconciled generated attachment reaches only its matching unknown-mapping context frame', () => {
  const root=path.resolve(import.meta.dir,'../../../../../..')
  const env:NodeJS.ProcessEnv={...process.env,DATABASE_URL:'sqlite:///:memory:',OPEN_CLANK_DATA_DIR:'/tmp/openclank-p16-fixture',ODYSSEUS_DATA_DIR:'/tmp/openclank-p16-fixture',FM_DB_PATH:'/tmp/openclank-p16-fixture/fm.sqlite',PYTHONPATH:root}
  delete env.OPEN_CLANK_AGENT_HOME;delete env.ODYSSEUS_AGENT_HOME
  const child=spawnSync(process.env.OPEN_CLANK_PYTHON || 'python3',['tests/fixtures/generated_summary_host.py'],{cwd:root,env,encoding:'utf8'})
  expect(child.status).toBe(0)
  const {resource,proof}=JSON.parse(child.stdout)
  const acp=zTextResourceContents.parse(resource),candidate=generatedAttachmentCandidate(acp)
  expect(candidate?.uploadID).toBeString()
  const part={id:'part',type:'text',text:acp.text}, message={info:{id:'message'},parts:[part]}
  const partSHA256=createHash('sha256').update(part.text,'utf8').digest('hex')
  const verified=new Map([['part',{messageID:'message',partSHA256,source:{kind:'generated_artifact' as const,id:proof.artifactID,revision:proof.previewHash,digest:proof.outputSHA256,artifactSHA256:proof.artifactSHA256,sourceReceiptSHA256:proof.sourceReceiptSHA256}}]])
  const evidence=sourceEvidence([message],undefined,verified)
  expect(evidence.sources.find(item=>item.kind==='generated_artifact')?.id).toBe(proof.artifactID)
  expect(sourceEvidence([{...message,parts:[{...part,text:'changed'}]}],undefined,verified).sources.some(item=>item.kind==='generated_artifact')).toBe(false)
  expect(sourceEvidence([{...message,info:{id:'other'}}],undefined,verified).sources.some(item=>item.kind==='generated_artifact')).toBe(false)
  const frame=contextFrame({identity:{sessionID:'engine'},sequence:{attempt:1,transform:1},providerID:'p',modelID:'m',window:{hard:100,effective:90,usable:80,source:'config'},...evidence,prompt:[],instructions:undefined,tools:[]})
  const projected=projectContextInspection({version:1,frames:[frame],omittedFrameCount:0})
  expect(projected?.frames[0].sourceMapping).toBe('unknown')
  expect(projected?.frames[0].sources.some(item=>item.kind==='generated_artifact')).toBe(true)
},20000)
