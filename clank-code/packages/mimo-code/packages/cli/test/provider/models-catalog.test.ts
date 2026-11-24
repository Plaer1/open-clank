import { describe, expect, test } from "bun:test"
import { mkdtemp, mkdir, readFile, rm, writeFile } from "node:fs/promises"
import { tmpdir } from "node:os"
import path from "node:path"
import { createCatalog } from "../../src/provider/models-catalog"
import { watchModelsCatalogReload } from "../../src/provider/models-catalog-reload"

function model(id: string, name = id) {
  return {
    id,
    name,
    release_date: "2026-01-01",
    attachment: false,
    reasoning: false,
    temperature: true,
    tool_call: true,
    voice_design: false,
    voice_clone: false,
    limit: { context: 128_000, output: 8_000 },
  }
}

function provider(id: string, models: Record<string, ReturnType<typeof model>>) {
  return {
    id,
    name: `Provider ${id}`,
    env: [`${id.toUpperCase()}_API_KEY`],
    models,
  }
}

function catalog(providerID: string, modelID: string, name = modelID) {
  return { [providerID]: provider(providerID, { [modelID]: model(modelID, name) }) }
}

async function fixture() {
  const directory = await mkdtemp(path.join(tmpdir(), "models-catalog-"))
  return {
    directory,
    cache: path.join(directory, "cache", "models.json"),
    explicit: path.join(directory, "operator-models.json"),
    async write(file: string, value: unknown) {
      await mkdir(path.dirname(file), { recursive: true })
      await writeFile(file, JSON.stringify(value))
    },
    async [Symbol.asyncDispose]() {
      await rm(directory, { recursive: true, force: true })
    },
  }
}

function deferred() {
  let resolve!: () => void
  const promise = new Promise<void>((done) => {
    resolve = done
  })
  return { promise, resolve }
}

describe("models catalog", () => {
  test("uses the cache as the entity set, fills snapshot fields, and gives callers isolated values", async () => {
    await using files = await fixture()
    const snapshot = {
      alpha: provider("alpha", {
        retained: model("retained", "Snapshot name"),
        removed: model("removed"),
      }),
      removedProvider: provider("removedProvider", { gone: model("gone") }),
    }
    await files.write(files.cache, {
      alpha: {
        id: "alpha",
        name: "Provider alpha",
        env: [],
        models: {
          retained: {
            id: "retained",
            name: "Cached name",
            limit: { context: 256_000, output: 16_000 },
          },
        },
      },
    })
    const subject = createCatalog({
      cache: files.cache,
      snapshot: async () => snapshot,
      fetch: async () => new Response(null, { status: 500 }),
    })

    const first = await subject.get()
    expect(Object.keys(first)).toEqual(["alpha"])
    expect(Object.keys(first.alpha.models)).toEqual(["retained"])
    expect(first.alpha.models.retained).toMatchObject({
      name: "Cached name",
      release_date: "2026-01-01",
      limit: { context: 256_000, output: 16_000 },
    })

    first.alpha.models.retained.name = "mutated by caller"
    expect((await subject.get()).alpha.models.retained.name).toBe("Cached name")
  })

  test("publishes a fresh shared-cache change once and isolates listener failures", async () => {
    await using files = await fixture()
    await files.write(files.cache, catalog("alpha", "one", "First"))
    const errors: unknown[] = []
    let fetches = 0
    let notifications = 0
    const subject = createCatalog({
      cache: files.cache,
      snapshot: async () => catalog("alpha", "one", "Snapshot"),
      fetch: async () => {
        fetches++
        return Response.json(catalog("alpha", "one", "Fetched"))
      },
      onError: (error) => errors.push(error),
    })
    await subject.get()
    const unsubscribe = subject.subscribe(() => {
      notifications++
    })
    subject.subscribe(() => {
      throw new Error("listener failed")
    })

    await files.write(files.cache, catalog("alpha", "one", "Second"))
    await subject.refresh()
    expect(fetches).toBe(0)
    expect(notifications).toBe(1)
    expect(errors).toHaveLength(1)
    expect((await subject.get()).alpha.models.one.name).toBe("Second")

    await subject.refresh()
    expect(notifications).toBe(1)
    expect(errors).toHaveLength(1)

    unsubscribe()
    await files.write(files.cache, catalog("alpha", "one", "Third"))
    await subject.refresh()
    expect(notifications).toBe(1)
    expect(errors).toHaveLength(2)
  })

  test("treats a valid explicit catalog as exclusive and never fetches or rewrites the shared cache", async () => {
    await using files = await fixture()
    const cached = catalog("cached", "cached-model")
    const override = catalog("operator", "operator-model")
    await files.write(files.cache, cached)
    await files.write(files.explicit, override)
    let fetches = 0
    const subject = createCatalog({
      cache: files.cache,
      explicit: files.explicit,
      snapshot: async () => catalog("snapshot", "snapshot-model"),
      fetch: async () => {
        fetches++
        return Response.json(catalog("fetched", "fetched-model"))
      },
    })

    expect(await subject.get()).toEqual(override)
    await subject.refresh(true)
    expect(fetches).toBe(0)
    expect(JSON.parse(await readFile(files.cache, "utf8"))).toEqual(cached)
  })

  test("fails closed for an invalid explicit catalog without falling back to cache or network", async () => {
    await using files = await fixture()
    await files.write(files.cache, catalog("cached", "cached-model"))
    await writeFile(files.explicit, "not json")
    const errors: unknown[] = []
    let fetches = 0
    const subject = createCatalog({
      cache: files.cache,
      explicit: files.explicit,
      snapshot: async () => catalog("snapshot", "snapshot-model"),
      fetch: async () => {
        fetches++
        return Response.json(catalog("fetched", "fetched-model"))
      },
      onError: (error) => errors.push(error),
    })

    expect(await subject.get()).toEqual({})
    await subject.refresh(true)
    expect(await subject.get()).toEqual({})
    expect(fetches).toBe(0)
    expect(errors).toHaveLength(2)
  })

  test("runs a late force request after the non-force refresh it joined", async () => {
    await using files = await fixture()
    const entered = deferred()
    const release = deferred()
    let fetches = 0
    const subject = createCatalog({
      cache: files.cache,
      snapshot: async () => catalog("snapshot", "snapshot-model"),
      fetch: async () => {
        fetches++
        if (fetches === 1) {
          entered.resolve()
          await release.promise
        }
        return Response.json(catalog("remote", `model-${fetches}`))
      },
    })

    const ordinary = subject.refresh()
    await entered.promise
    const forced = subject.refresh(true)
    release.resolve()
    await Promise.all([ordinary, forced])

    expect(fetches).toBe(2)
    expect(Object.keys((await subject.get()).remote.models)).toEqual(["model-2"])
    expect(JSON.parse(await readFile(files.cache, "utf8"))).toEqual(catalog("remote", "model-2"))
  })

  test("coalesces force requests already covered by an active forced refresh", async () => {
    await using files = await fixture()
    const entered = deferred()
    const release = deferred()
    let fetches = 0
    const subject = createCatalog({
      cache: files.cache,
      snapshot: async () => catalog("snapshot", "snapshot-model"),
      fetch: async () => {
        fetches++
        entered.resolve()
        await release.promise
        return Response.json(catalog("remote", "forced"))
      },
    })

    const first = subject.refresh(true)
    await entered.promise
    const second = subject.refresh(true)
    release.resolve()
    await Promise.all([first, second])

    expect(fetches).toBe(1)
  })
})

describe("models catalog reload watcher", () => {
  test("debounces catalog changes and cancels pending work when stopped", async () => {
    let listener: (() => void) | undefined
    let unsubscribed = false
    let reloads = 0
    const reloaded = deferred()
    const stop = watchModelsCatalogReload({
      subscribe(next) {
        listener = next
        return () => {
          unsubscribed = true
        }
      },
      async reload() {
        reloads++
        reloaded.resolve()
      },
      delayMs: 5,
    })

    listener?.()
    listener?.()
    listener?.()
    await Promise.race([
      reloaded.promise,
      Bun.sleep(500).then(() => {
        throw new Error("timed out waiting for reload")
      }),
    ])
    expect(reloads).toBe(1)

    listener?.()
    stop()
    await Bun.sleep(20)
    expect(unsubscribed).toBe(true)
    expect(reloads).toBe(1)
  })
})
