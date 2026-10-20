import { Log } from "@/util"

const log = Log.create({ service: "upgrade" })

export async function upgrade() {
  log.debug("engine self-update is disabled; Open Clank owns engine activation")
}
