import {test,expect} from 'bun:test'
import {Skill} from '../../src/skill'

test('domain annotations share a byte ceiling without removing skill names or descriptions',()=>{
 const skills=Array.from({length:100},(_,i)=>({name:`skill-${String(i).padStart(3,'0')}`,description:`description-${i}`,location:'/tmp/SKILL.md',content:'',domainRefs:Array.from({length:8},(_,j)=>({block_id:`${'界'.repeat(100)}-${j}`,revision:1}))}))
 for(const verbose of [true,false]) {
  const withDomains=Skill.fmt(skills,{verbose}),baseline=Skill.fmt(skills.map(({domainRefs,...skill})=>skill),{verbose})
  expect(Buffer.byteLength(withDomains)-Buffer.byteLength(baseline)).toBeLessThanOrEqual(8192)
  expect(withDomains).toContain('domain annotations omitted')
  for(const skill of skills){expect(withDomains).toContain(skill.name);expect(withDomains).toContain(skill.description)}
  expect(withDomains).toContain('revalidated on load')
 }
})
