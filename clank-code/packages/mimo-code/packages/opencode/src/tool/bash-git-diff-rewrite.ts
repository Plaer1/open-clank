/** Conservative lookup only: execute through the original Bash and environment. */
import { access, realpath } from 'node:fs/promises'
import { constants } from 'node:fs'
import path from 'node:path'
import { gitExecutable } from '../git/index-physical'

export async function gitDiffRewriteEligible(command: string, shellName: string, interactive: boolean | undefined, env: NodeJS.ProcessEnv): Promise<boolean> {
  if (process.platform === 'win32' || shellName !== 'bash' || interactive || command !== 'git diff' || typeof env.PATH !== 'string') return false
  const dirs=env.PATH.split(':')
  if (!dirs.length || dirs.some(dir => !dir || !path.isAbsolute(dir))) return false
  try {
    const installed=await gitExecutable()
    for (const dir of dirs) {
      try {
        const candidate=await realpath(path.join(dir,'git'))
        await access(candidate,constants.X_OK)
        return candidate===installed.path
      } catch { /* match shell PATH lookup's next entry */ }
    }
  } catch { /* optional optimization unavailable */ }
  return false
}
