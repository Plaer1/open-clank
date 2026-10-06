import { defineConfig } from "drizzle-kit"

export default defineConfig({
  dialect: "sqlite",
  schema: "./src/**/*.sql.ts",
  out: "../../../../.clanker/tools/native/mimo/journals",
  dbCredentials: {
    url: process.env.MIMOCODE_DB || ":memory:",
  },
})
