/** Consume one supervisor-private Windows pipe before any child tools start. */
export function consumeWindowsServerPassword(): string | undefined {
  const endpoint = process.env.OPEN_CLANK_WORKER_AUTH_PIPE
  const parent = process.env.OPEN_CLANK_WORKER_AUTH_PARENT_PID
  const configured =
    Object.prototype.hasOwnProperty.call(process.env, "OPEN_CLANK_WORKER_AUTH_PIPE") ||
    Object.prototype.hasOwnProperty.call(process.env, "OPEN_CLANK_WORKER_AUTH_PARENT_PID")
  delete process.env.OPEN_CLANK_WORKER_AUTH_PIPE
  delete process.env.OPEN_CLANK_WORKER_AUTH_PARENT_PID
  if (!configured) return undefined
  if (
    process.platform !== "win32" ||
    !endpoint || !/^\\\\\.\\pipe\\open-clank-worker-auth-[a-f0-9]{32}$/.test(endpoint) ||
    !parent || !/^\d+$/.test(parent) || !Number.isSafeInteger(Number(parent)) || Number(parent) <= 0 ||
    Object.prototype.hasOwnProperty.call(process.env, "OPEN_CLANK_WORKER_AUTH_FD")
  ) {
    throw new Error("Invalid Open Clank worker-auth pipe metadata")
  }
  // Load native code only for an explicitly configured Windows handoff.
  const { dlopen, ptr } = require("bun:ffi") as typeof import("bun:ffi")
  const library = dlopen("kernel32.dll", {
    CreateFileW: { args: ["ptr", "u32", "u32", "ptr", "u32", "u32", "u64"], returns: "u64" },
    GetNamedPipeServerProcessId: { args: ["u64", "ptr"], returns: "i32" },
    ReadFile: { args: ["u64", "ptr", "u32", "ptr", "ptr"], returns: "i32" },
    WriteFile: { args: ["u64", "ptr", "u32", "ptr", "ptr"], returns: "i32" },
    CloseHandle: { args: ["u64"], returns: "i32" },
  })
  let handle: bigint | undefined
  try {
    const name = new Uint16Array(endpoint.length + 1)
    for (let i = 0; i < endpoint.length; i++) name[i] = endpoint.charCodeAt(i)
    // OPEN_EXISTING; noninheritable; SECURITY_SQOS_PRESENT with anonymous
    // impersonation level so a substituted server cannot borrow child authority.
    const opened = library.symbols.CreateFileW(ptr(name), 0xc0000000, 0, null, 3, 0x100000, 0n)
    if (BigInt(opened) === 0n || BigInt(opened) === 0xffffffffffffffffn) {
      throw new Error("Unable to open Open Clank worker-auth pipe")
    }
    handle = BigInt(opened)
    const actual = new Uint32Array(1)
    if (!library.symbols.GetNamedPipeServerProcessId(handle, ptr(actual)) || actual[0] !== Number(parent)) {
      throw new Error("Open Clank worker-auth server identity mismatch")
    }
    function readExact(size: number): Uint8Array {
      const buffer = new Uint8Array(size)
      const count = new Uint32Array(1)
      let offset = 0
      while (offset < size) {
        if (!library.symbols.ReadFile(handle!, ptr(buffer.subarray(offset)), size - offset, ptr(count), null) ||
            count[0] === 0 || count[0]! > size - offset) {
          throw new Error("Unable to read Open Clank worker-auth pipe")
        }
        offset += count[0]!
      }
      return buffer
    }
    const header = readExact(4)
    const size = new DataView(header.buffer, header.byteOffset, 4).getUint32(0, true)
    if (size < 1 || size > 1024) throw new Error("Invalid Open Clank worker-auth secret")
    const bytes = readExact(size)
    let password: string
    try {
      password = new TextDecoder("utf-8", { fatal: true }).decode(bytes)
    } catch {
      throw new Error("Invalid Open Clank worker-auth secret")
    } finally {
      bytes.fill(0)
    }
    const ack = new Uint8Array([1])
    const written = new Uint32Array(1)
    if (!library.symbols.WriteFile(handle, ptr(ack), 1, ptr(written), null) || written[0] !== 1) {
      throw new Error("Unable to acknowledge Open Clank worker-auth pipe")
    }
    return password
  } finally {
    if (handle !== undefined) library.symbols.CloseHandle(handle)
    library.close()
  }
}
