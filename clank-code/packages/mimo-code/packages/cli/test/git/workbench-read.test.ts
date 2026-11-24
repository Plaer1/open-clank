import { test, expect } from "bun:test"
import { mkdtemp, readFile, rm, writeFile, access, stat } from "node:fs/promises"
import os from "node:os"
import path from "node:path"
import { ManagedRuntime } from "effect"
import { Git } from "../../src/git"
import { createPhysicalBranch, fetchPhysicalCommit, inspectPhysicalRemote, probePhysicalRemote, pushPhysicalCommit, previewPhysicalCheckout, previewPhysicalCommit } from "../../src/git/index-physical"

test("workbench reads ignore hostile Git environment and never invoke clean filters", async () => {
  const root = await mkdtemp(path.join(os.tmpdir(), "p06-git-"))
  const remote = await mkdtemp(path.join(os.tmpdir(), "d4-fetch-"))
  const git = (...args: string[]) => {
    const result = Bun.spawnSync(["git", ...args], { cwd: root, stdout: "pipe", stderr: "pipe", env: { PATH: process.env.PATH, HOME: root } })
    if (result.exitCode !== 0) throw new Error("fixture setup failed")
  }
  const runtime = ManagedRuntime.make(Git.defaultLayer)
  const prior = process.env.GIT_DIR
  try {
    git("init", "-q")
    git("config", "user.name", "Fixture")
    git("config", "user.email", "fixture@example.invalid")
    await writeFile(path.join(root, "file.txt"), "original\n")
    for (let i = 0; i < 80; i++) await writeFile(path.join(root, `item-${String(i).padStart(3, "0")}.txt`), `item ${i}\n`)
    git("add", ".")
    git("commit", "-qm", "fixture")
    const remoteGit = (...args: string[]) => { const value=Bun.spawnSync(['git',...args],{cwd:remote,stdout:'pipe',stderr:'pipe',env:{PATH:process.env.PATH,HOME:remote}}); if(value.exitCode!==0)throw Error('remote fixture failed');return value.stdout.toString().trim() }
    remoteGit('init','-q');remoteGit('-c','user.name=Fixture','-c','user.email=fixture@example.invalid','commit','--allow-empty','-qm','remote')
    const remoteOID=remoteGit('rev-parse','HEAD'), fetchPhases:string[]=[]
    const remoteStat=await stat(remote), remoteIdentity={device:remoteStat.dev,inode:remoteStat.ino}
    const remoteBranch=remoteGit('symbolic-ref','--short','HEAD')
    git('remote','add','fixture',remote)
    git('remote','set-url','--push','fixture','file://'+remote)
    const preview = await previewPhysicalCommit(root)
    const info=await inspectPhysicalRemote(root,preview,'fixture',remoteBranch)
    expect(info.remoteURL).toBe(remote);expect(info.oldOID).toBeNull()
    expect((await inspectPhysicalRemote(root,preview,'fixture',remoteBranch,true)).remoteURL).toBe('file://'+remote)
    expect(await probePhysicalRemote(root,preview,info.remoteURL,info.sourceRef,remoteIdentity)).toBe(remoteOID)
    expect(await probePhysicalRemote(root,preview,info.remoteURL,'refs/heads/missing',remoteIdentity,true)).toBeNull()
    const phases: string[] = []
    const created = await createPhysicalBranch(root, preview, 'refs/heads/reviewed', async phase => { phases.push(phase); return true })
    expect(created.state).toBe('applied')
    expect(phases).toEqual(['before_lock', 'before_publish'])
    const duplicate = await createPhysicalBranch(root, preview, 'refs/heads/reviewed', async () => true)
    expect(duplicate.state).toBe('refused')
    const revoked = await createPhysicalBranch(root, preview, 'refs/heads/revoked', async phase => phase !== 'before_publish')
    expect(revoked.state).toBe('refused')
    const verify = Bun.spawnSync(['git','show-ref','--verify','--quiet','refs/heads/revoked'], { cwd:root, env:{PATH:process.env.PATH,HOME:root} })
    expect(verify.exitCode).not.toBe(0)
    const wrongRemote=await fetchPhysicalCommit(root,preview,remote,'refs/remotes/fixture/wrong',null,remoteOID,{...remoteIdentity,inode:remoteIdentity.inode+1},async ()=>true)
    expect(wrongRemote).toEqual({state:'refused',transfer:'not_issued',ref:'not_issued',cleanup:'complete'})
    const fetched=await fetchPhysicalCommit(root,preview,remote,'refs/remotes/fixture/main',null,remoteOID,remoteIdentity,async phase=>{fetchPhases.push(phase);return true})
    expect(fetched).toEqual({state:'applied',transfer:'completed',ref:'applied',cleanup:'complete'})
    expect(fetchPhases).toEqual(['before_transfer','before_publish'])
    expect(Bun.spawnSync(['git','rev-parse','refs/remotes/fixture/main'],{cwd:root,env:{PATH:process.env.PATH,HOME:root}}).stdout.toString().trim()).toBe(remoteOID)
    const denied=await fetchPhysicalCommit(root,preview,remote,'refs/remotes/fixture/denied',null,remoteOID,remoteIdentity,async phase=>phase!=='before_publish')
    expect(denied).toEqual({state:'refused',transfer:'completed',ref:'not_issued',cleanup:'complete'})
    expect(Bun.spawnSync(['git','show-ref','--verify','--quiet','refs/remotes/fixture/denied'],{cwd:root,env:{PATH:process.env.PATH,HOME:root}}).exitCode).not.toBe(0)
    const pushed=await pushPhysicalCommit(root,preview,remote,'refs/heads/pushed',remoteIdentity,async()=>true)
    expect(pushed).toEqual({state:'applied',remote:'accepted',cleanup:'complete'})
    expect(remoteGit('rev-parse','refs/heads/pushed')).toBe(preview.headOID)
    const pushDenied=await pushPhysicalCommit(root,preview,remote,'refs/heads/denied',remoteIdentity,async()=>false)
    expect(pushDenied).toEqual({state:'refused',remote:'not_issued',cleanup:'complete'})
    expect(Bun.spawnSync(['git','show-ref','--verify','--quiet','refs/heads/denied'],{cwd:remote,env:{PATH:process.env.PATH,HOME:remote}}).exitCode).not.toBe(0)
    const nonFF=await pushPhysicalCommit(root,preview,remote,'refs/heads/'+remoteBranch,remoteIdentity,async()=>true)
    expect(nonFF.state).toBe('unknown')
    expect(remoteGit('rev-parse','refs/heads/'+remoteBranch)).toBe(remoteOID)
    await writeFile(path.join(root,'target-blob.tmp'),'target\n')
    const command=(args:string[],extra:Record<string,string>={})=>{const r=Bun.spawnSync(['git',...args],{cwd:root,stdout:'pipe',stderr:'pipe',env:{PATH:process.env.PATH,HOME:root,...extra}});if(r.exitCode!==0)throw Error('checkout fixture failed');return r.stdout.toString().trim()}
    const blobOID=command(['hash-object','-w','target-blob.tmp']),candidate=path.join(root,'.git','fixture-index'),indexEnv={GIT_INDEX_FILE:candidate}
    command(['read-tree','HEAD'],indexEnv);command(['update-index','--cacheinfo',`100644,${blobOID},file.txt`],indexEnv)
    const treeOID=command(['write-tree'],indexEnv),targetCommit=command(['commit-tree',treeOID,'-p',preview.headOID,'-m','target'])
    command(['update-ref','refs/heads/checkout-target',targetCommit])
    await Bun.write(path.join(root,'.clanker','private.txt'),'private').catch(async()=>{await import('node:fs/promises').then(fs=>fs.mkdir(path.join(root,'.clanker'),{recursive:true}));await Bun.write(path.join(root,'.clanker','private.txt'),'private')})
    await import('node:fs/promises').then(fs=>fs.mkdir(path.join(root,'node_modules'),{recursive:true}));await Bun.write(path.join(root,'node_modules','unrelated.txt'),'untracked')
    const checkout=await previewPhysicalCheckout(root,'refs/heads/checkout-target')
    expect(checkout.targetOID).toBe(targetCommit);expect(checkout.changed).toEqual(['file.txt'])
    command(['update-ref','refs/remotes/fixture/checkout',targetCommit])
    const pull=await previewPhysicalCheckout(root,'refs/remotes/fixture/checkout','pull')
    expect(pull.operation).toBe('pull');expect(pull.targetOID).toBe(targetCommit)
    await writeFile(path.join(root,'.git','info','attributes'),'file.txt text\n')
    await expect(previewPhysicalCheckout(root,'refs/heads/checkout-target')).rejects.toThrow()
    await rm(path.join(root,'.git','info','attributes'))
    const marker = path.join(root, "filter-ran")
    git("config", "filter.marker.clean", `touch ${marker}; cat`)
    await writeFile(path.join(root, ".gitattributes"), "file.txt filter=marker\n")
    await writeFile(path.join(root, "file.txt"), "changed\n")
    process.env.GIT_DIR = path.join(root, "hostile-nonexistent")
    const read = (operation: "root" | "index" | "tree" | "others" | "status") => runtime.runPromise(Git.Service.use((service) => service.readWorkbench({ cwd: root, operation })))
    const resolved = await read("root")
    expect(resolved.exitCode).toBe(0)
    expect(resolved.text().split("\n")[0]).toBe(root)
    const inventory = await read("index")
    expect(inventory.exitCode).toBe(0)
    const prefix = Git.inventoryPrefix(inventory.stdout)
    expect(prefix.truncated).toBe(true)
    expect(prefix.data.includes(Buffer.from("item-079.txt"))).toBe(false)
    const selected = await runtime.runPromise(Git.Service.use((service) => service.readWorkbench({ cwd: root, operation: "indexEntry", path: "item-079.txt" })))
    expect(selected.text()).toContain("item-079.txt")
    const tree = await read("tree")
    expect(tree.exitCode).toBe(0)
    const oid = tree.text().match(/^100644 blob ([0-9a-f]+)\t/)?.[1]
    expect(oid).toBeTruthy()
    const blob = await runtime.runPromise(Git.Service.use((service) => service.readWorkbench({ cwd: root, operation: "blob", path: oid! })))
    expect(blob.text()).toBe("original\n")
    expect((await read("others")).exitCode).toBe(0)
    for (const operation of ["branches", "log", "worktrees"] as const) {
      const result = await runtime.runPromise(Git.Service.use((service) => service.readWorkbench({ cwd: root, operation })))
      expect(result.exitCode).toBe(0)
      if (operation === "branches") expect(result.stdout.toString()).toContain("refs/heads/")
      if (operation === "log") expect(result.stdout.toString()).toContain("fixture")
      if (operation === "worktrees") expect(result.stdout.toString()).toContain(`worktree ${root}\0`)
    }
    expect((await read("status")).exitCode).not.toBe(0)
    expect(await access(marker).then(() => true, () => false)).toBe(false)
    expect(await readFile(path.join(root, "file.txt"), "utf8")).toBe("changed\n")
  } finally {
    if (prior === undefined) delete process.env.GIT_DIR; else process.env.GIT_DIR = prior
    await runtime.dispose()
    await rm(root, { recursive: true, force: true })
    await rm(remote, { recursive: true, force: true })
  }
}, 20000)
