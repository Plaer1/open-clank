import type { Hooks, PluginInput } from "@mimo-ai/plugin"
import crypto from "crypto"
import { Global } from "../global"
import path from "path"
import fs from "fs"

const PLATFORM_URL = process.env.MIMO_PLATFORM_URL || "https://platform.xiaomimimo.com"

function getKeyName(): string {
  const filePath = path.join(Global.Path.data, "mimo-key-name")
  try {
    const existing = fs.readFileSync(filePath, "utf-8").trim()
    if (existing) return existing
  } catch {}
  const name = `mimo-code-cli-key-${crypto.randomBytes(4).toString("hex")}`
  fs.writeFileSync(filePath, name)
  return name
}

function generateKeyPair() {
  const keyPair = crypto.generateKeyPairSync("x25519", {
    publicKeyEncoding: { type: "spki", format: "der" },
    privateKeyEncoding: { type: "pkcs8", format: "der" },
  })
  const publicKeyBase64 = Buffer.from(keyPair.publicKey).toString("base64url")
  return { publicKey: publicKeyBase64, privateKeyDer: keyPair.privateKey }
}

function decrypt(privateKeyDer: Buffer, encryptedBase64: string): { sk?: string; uid: string; url?: string } {
  const encrypted = Buffer.from(encryptedBase64, "base64url")
  // Format: ephemeralPublicKey(32 bytes) + nonce(12 bytes) + ciphertext + tag(16 bytes)
  const ephemeralPub = encrypted.subarray(0, 32)
  const nonce = encrypted.subarray(32, 44)
  const ciphertextAndTag = encrypted.subarray(44)
  const tag = ciphertextAndTag.subarray(ciphertextAndTag.length - 16)
  const ciphertext = ciphertextAndTag.subarray(0, ciphertextAndTag.length - 16)

  const privateKey = crypto.createPrivateKey({ key: privateKeyDer, format: "der", type: "pkcs8" })
  const ephemeralPublicKey = crypto.createPublicKey({
    key: Buffer.concat([Buffer.from("302a300506032b656e032100", "hex"), ephemeralPub]),
    format: "der",
    type: "spki",
  })

  const sharedSecret = crypto.diffieHellman({ privateKey, publicKey: ephemeralPublicKey })
  const derivedKey = crypto.createHash("sha256").update(sharedSecret).digest()

  const decipher = crypto.createDecipheriv("aes-256-gcm", derivedKey, nonce)
  decipher.setAuthTag(tag)
  const decrypted = Buffer.concat([decipher.update(ciphertext), decipher.final()])

  return JSON.parse(decrypted.toString("utf-8"))
}

function buildAuthorizeUrl(publicKey: string, redirectUri: string): string {
  const params = new URLSearchParams({
    pk: publicKey,
    redirect_uri: redirectUri,
    kn: "mimocode",
    key_name: getKeyName(),
  })
  return `${PLATFORM_URL}/authorize?${params.toString()}`
}

export async function MimoAuthPlugin(_input: PluginInput): Promise<Hooks> {
  return {
    config: async (input) => {
      input.provider ??= {}
      // Register xiaomi as a config provider so it shows up even before login.
      // name/api are intentionally left to the models.dev database (name: "Xiaomi",
      // api: https://api.xiaomimimo.com/v1) — hardcoding "MiMo" here collided with
      // the free "mimo" provider's display name and confused users.
      input.provider.xiaomi ??= {}
      // Both "opencode" and "opencode-go" stay enabled. The opencode custom
      // loader strips the free/public tier (and hides paid models until the
      // user authenticates). "opencode-go" has no free models and no custom
      // loader, so it only loads once a subscription key/auth is present.
    },
    auth: {
      provider: "xiaomi",
      async loader(getAuth) {
        const auth = (await getAuth()) as { type: string; metadata?: Record<string, string> }
        if (auth?.type !== "api" || !auth.metadata?.base_url) return {}
        return { baseURL: auth.metadata.base_url }
      },
      methods: [
        {
          label: "Browser login (paste code)",
          type: "oauth" as const,
          authorize: async () => {
            const { publicKey, privateKeyDer } = generateKeyPair()
            const manualUrl = buildAuthorizeUrl(publicKey, `${PLATFORM_URL}/authorize/code/callback`)

            return {
              url: manualUrl,
              method: "code" as const,
              instructions: "Finish sign-in on this device, copy the code it gives you, then paste it into Open Clank.",
              callback: async (code: string) => {
                try {
                  const result = decrypt(privateKeyDer, code.trim())
                  const metadata: Record<string, string> = { uid: result.uid }
                  if (result.url) metadata.base_url = result.url
                  return { type: "success" as const, key: result.sk ?? "", metadata }
                } catch {
                  return { type: "failed" as const }
                }
              },
            }
          },
        },
      ],
    },
    "chat.headers": async (input, output) => {
      if (input.model.providerID !== "xiaomi") return
      output.headers["X-Mimo-Source"] = "mimocode-cli"
    },
  }
}

export async function AnthropicProxyPlugin(_input: PluginInput): Promise<Hooks> {
  return {
    auth: {
      provider: "anthropic",
      async loader(_getAuth, provider) {
        if (!provider?.options?.baseURL) return {}
        return {
          async fetch(url: any, init: any) {
            if (init?.headers && typeof init.headers === "object" && !Array.isArray(init.headers)) {
              delete init.headers["anthropic-beta"]
            }
            const res = await fetch(url, init)
            if (!res.body || !res.headers.get("content-type")?.includes("text/event-stream")) return res
            const reader = res.body.getReader()
            const decoder = new TextDecoder()
            let done = false
            let buffer = ""
            const body = new ReadableStream<Uint8Array>({
              async pull(ctrl) {
                if (done) { ctrl.close(); return }
                const chunk = await reader.read()
                if (chunk.done) { ctrl.close(); return }
                ctrl.enqueue(chunk.value)
                buffer += decoder.decode(chunk.value, { stream: true })
                if (buffer.includes("\nevent: message_stop\n") || buffer.includes("\ndata: {\"type\":\"message_stop\"}")) {
                  done = true
                  void reader.cancel()
                  ctrl.close()
                }
                if (buffer.length > 512) buffer = buffer.slice(-256)
              },
              cancel() { reader.cancel() },
            })
            return new Response(body, { headers: res.headers, status: res.status })
          },
        }
      },
      methods: [],
    },
  }
}
