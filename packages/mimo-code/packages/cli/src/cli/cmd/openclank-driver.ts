import { closeSync, readSync, writeSync } from "node:fs"
import { cmd } from "./cmd"
import {
  decodeDriverFrame,
  encodeDriverResponse,
  OpenClankDriverSession,
  type DriverResponse,
} from "../../openclank-runtime/protocol"

const MAX_FRAME_BYTES = 256 * 1024

function readExact(fd: number, size: number): Uint8Array | null {
  const buffer = Buffer.allocUnsafe(size)
  let offset = 0
  while (offset < size) {
    const count = readSync(fd, buffer, offset, size - offset, null)
    if (count === 0) {
      if (offset === 0) return null
      throw new Error("driver control frame truncated")
    }
    offset += count
  }
  return new Uint8Array(buffer)
}

function writeFrame(fd: number, response: DriverResponse) {
  const frame = encodeDriverResponse(response)
  let offset = 0
  while (offset < frame.byteLength) {
    const count = writeSync(fd, frame, offset, frame.byteLength - offset)
    if (count <= 0) throw new Error("driver control frame write failed")
    offset += count
  }
}

export function runOpenClankDriver(controlFd: number): void {
  if (!Number.isSafeInteger(controlFd) || controlFd < 3 || controlFd > 1024) {
    throw new Error("invalid --control-fd")
  }
  const session = new OpenClankDriverSession()
  try {
    while (true) {
      const header = readExact(controlFd, 4)
      if (!header) return
      const length = new DataView(header.buffer, header.byteOffset, header.byteLength).getUint32(0, false)
      if (length === 0 || length > MAX_FRAME_BYTES) throw new Error("driver control frame length invalid")
      const body = readExact(controlFd, length)
      if (!body) throw new Error("driver control frame truncated")
      let response: DriverResponse
      try {
        response = session.handle(decodeDriverFrame(new Uint8Array([...header, ...body])))
      } catch (error) {
        const request_id = "0".repeat(32)
        response = {
          schema_version: 1,
          request_id,
          ok: false,
          error: { code: "protocol_error", safe_message: "driver frame rejected", retryable: false },
        }
        console.error(error instanceof Error ? error.message : "driver frame rejected")
      }
      writeFrame(controlFd, response)
      if (response.event === "shutdown_ack") return
    }
  } finally {
    closeSync(controlFd)
  }
}

export const OpenClankDriverCommand = cmd({
  command: "openclank-driver",
  describe: "run the private Open Clank direct-driver control loop",
  builder: (yargs) => yargs
    .option("protocol-major", { type: "number", demandOption: true })
    .option("protocol-minor", { type: "number", demandOption: true })
    .option("control-fd", { type: "number", demandOption: true })
    .strict(),
  handler: (args) => {
    if (args.protocolMajor !== 1 || args.protocolMinor !== 0) throw new Error("unsupported direct-driver protocol")
    runOpenClankDriver(args.controlFd)
  },
})
