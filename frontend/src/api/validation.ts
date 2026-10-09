/**
 * Pydantic validation failures, made readable.
 *
 * A rejected request body comes back as
 *
 *     {"error": {"code": "validation_failed", "message": "Request validation failed",
 *                "detail": {"errors": [{"loc": ["body", "name"], "msg": "...", ...}]}}}
 *
 * The `message` is byte-for-byte identical for every rejection, so on its own it
 * tells a user nothing at all — which is how "新建规格" came to fail with a
 * message that named neither the field nor the rule. The actionable part is the
 * per-field list, and this module turns that into sentences.
 *
 * It deliberately imports nothing from `client.ts`: `ApiError` uses these
 * functions, so a dependency back the other way would be a cycle.
 */

/** One rejected field. */
interface FieldValidationError {
  /** Dotted path with the `body`/`query`/`path` source token stripped. */
  field: string
  /** Field label if one was supplied, otherwise the raw field name. */
  label: string
  /** The reason, translated where the rule is a recognised one. */
  message: string
}

/** Field name → the label the form actually shows, e.g. `cpuMilli` → `CPU（milli）`. */
export type FieldLabels = Record<string, string>

interface RawValidationError {
  loc?: unknown
  msg?: unknown
  type?: unknown
  ctx?: unknown
}

/** Sources Pydantic prepends to `loc`; they say nothing a user needs. */
const LOCATION_SOURCES = new Set(['body', 'query', 'path', 'header', 'cookie'])

function asRecord(value: unknown): Record<string, unknown> {
  return typeof value === 'object' && value !== null ? (value as Record<string, unknown>) : {}
}

function asNumber(value: unknown): number | null {
  return typeof value === 'number' ? value : null
}

function fieldPath(loc: unknown): string {
  if (!Array.isArray(loc)) return ''
  const parts = loc
    .filter((part) => !(typeof part === 'string' && LOCATION_SOURCES.has(part)))
    .map((part) => String(part))
  return parts.join('.')
}

/**
 * Render one Pydantic error as a Chinese sentence.
 *
 * Pydantic reports machine-oriented English (`String should match pattern '...'`).
 * Showing that verbatim is barely better than showing nothing, so the handful of
 * rules an admin form can actually trip are stated in the form's own language.
 * Anything unrecognised falls back to Pydantic's `msg`, which is at least
 * specific — better an English reason than a generic one.
 */
function describe(type: string, ctx: Record<string, unknown>, fallback: string): string {
  const ge = asNumber(ctx.ge)
  const le = asNumber(ctx.le)
  const minLength = asNumber(ctx.min_length)
  const maxLength = asNumber(ctx.max_length)

  switch (type) {
    case 'missing':
      return '必填'
    case 'extra_forbidden':
      // The backend forbids unknown fields on purpose, so this means the client
      // sent something the endpoint does not accept — typically an immutable
      // field such as `name` on an update.
      return '该字段不接受修改，请勿提交'
    case 'string_pattern_mismatch':
      return typeof ctx.pattern === 'string'
        ? `格式不正确（须匹配 ${ctx.pattern}）`
        : '格式不正确'
    case 'string_too_short':
      return minLength === null ? '太短' : `至少 ${minLength} 个字符`
    case 'string_too_long':
      return maxLength === null ? '太长' : `最多 ${maxLength} 个字符`
    case 'greater_than_equal':
      return ge === null ? '数值太小' : `不能小于 ${ge}`
    case 'greater_than':
      return ge === null ? '数值太小' : `必须大于 ${ge}`
    case 'less_than_equal':
      return le === null ? '数值太大' : `不能大于 ${le}`
    case 'less_than':
      return le === null ? '数值太大' : `必须小于 ${le}`
    case 'int_parsing':
    case 'int_type':
    case 'int_from_float':
      return '必须是整数'
    case 'float_parsing':
    case 'float_type':
      return '必须是数字'
    case 'bool_parsing':
    case 'bool_type':
      return '必须是 true 或 false'
    case 'list_type':
    case 'tuple_type':
    case 'set_type':
      return '必须是列表'
    case 'dict_type':
      return '必须是对象'
    case 'datetime_parsing':
    case 'datetime_type':
      return '不是有效的时间'
    case 'enum':
      return typeof ctx.expected === 'string' ? `只能是 ${ctx.expected}` : '取值不在允许范围内'
    case 'value_error':
      // Raised by the backend's own `field_validator`s, which already phrase the
      // reason in Chinese — the useful text is in `ctx.error`.
      return typeof ctx.error === 'string' ? ctx.error : fallback
    default:
      return fallback
  }
}

/**
 * Parse `detail.errors` into readable field errors.
 *
 * Returns `[]` for anything that is not a validation envelope, so callers can
 * treat "no field errors" and "not that kind of error" identically.
 */
function parseValidationErrors(detail: unknown, labels: FieldLabels = {}): FieldValidationError[] {
  const errors = asRecord(detail).errors
  if (!Array.isArray(errors)) return []

  return errors.map((raw) => {
    const entry = asRecord(raw) as RawValidationError
    const field = fieldPath(entry.loc)
    const message = describe(
      typeof entry.type === 'string' ? entry.type : '',
      asRecord(entry.ctx),
      typeof entry.msg === 'string' ? entry.msg : '校验未通过',
    )
    const label = (field && labels[field]) || field || '请求体'
    return { field, label, message }
  })
}

/** Format parsed errors as `标识：格式不正确（…）`, one per line. */
function formatValidationErrors(errors: FieldValidationError[]): string {
  return errors.map((error) => `${error.label}：${error.message}`).join('\n')
}

/**
 * The message a human should see, with field details appended.
 *
 * Newline-separated rather than comma-joined so a multi-field rejection reads as
 * a list wherever the container honours `white-space: pre-line`.
 */
export function withFieldDetails(message: string, detail: unknown, labels: FieldLabels = {}): string {
  const rendered = formatValidationErrors(parseValidationErrors(detail, labels))
  return rendered ? `${message}\n${rendered}` : message
}
