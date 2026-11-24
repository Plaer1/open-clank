export const FILE_RESULT_CONTRACT = "open-clank.file-result/v1" as const

export type FileResultKind = "text" | "directory" | "image" | "pdf" | "binary" | "search"
export type FileResultUnit = "character" | "line" | "entry" | "result" | "byte"
export type FileTruncationReason = "character_limit" | "byte_limit" | "line_limit" | "result_limit"

export type FileResult = {
  contract: typeof FILE_RESULT_CONTRACT
  operation: "read" | "write" | "edit" | "list" | "glob" | "grep" | "view_image"
  path: string
  kind: FileResultKind
  range: { unit: FileResultUnit; start: number; end: number } | null
  page: {
    unit: FileResultUnit
    cursor: number
    next_cursor: number | null
    has_more: boolean
    returned: number
    total: number | null
  }
  bytes_considered: number | null
  lines_considered: number | null
  truncation_reason: FileTruncationReason | null
  encoding: string | null
  newline: "lf" | "crlf" | "cr" | null
  media_type: string | null
  fingerprint: string | null
  search_mode: "literal" | "regex" | null
  items: Array<Record<string, unknown>>
  diagnostics: Array<{ code: string; message: string }>
}

export function fileResult(
  input: Omit<
    FileResult,
    | "contract"
    | "range"
    | "bytes_considered"
    | "lines_considered"
    | "truncation_reason"
    | "encoding"
    | "newline"
    | "media_type"
    | "fingerprint"
    | "search_mode"
    | "items"
    | "diagnostics"
  > &
    Partial<
      Pick<
        FileResult,
        | "range"
        | "bytes_considered"
        | "lines_considered"
        | "truncation_reason"
        | "encoding"
        | "newline"
        | "media_type"
        | "fingerprint"
        | "search_mode"
        | "items"
        | "diagnostics"
      >
    >,
): FileResult {
  return {
    contract: FILE_RESULT_CONTRACT,
    range: null,
    bytes_considered: null,
    lines_considered: null,
    truncation_reason: null,
    encoding: null,
    newline: null,
    media_type: null,
    fingerprint: null,
    search_mode: null,
    items: [],
    diagnostics: [],
    ...input,
  }
}
