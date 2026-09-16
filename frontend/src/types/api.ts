/**
 * Wire types shared by every API module.
 *
 * The backend serialises camelCase (see `app/schemas/base.py`), so these mirror
 * the JSON exactly — no mapping layer in between.
 */

/** The single error envelope every endpoint returns. */
export interface ApiErrorEnvelope {
  error: {
    code: string
    message: string
    detail?: Record<string, unknown>
  }
}

export interface PageMeta {
  page: number
  pageSize: number
  total: number
  pages: number
}

export interface Page<T> {
  items: T[]
  meta: PageMeta
}

export interface ApiMessage {
  message: string
  code?: string | null
}

/** Pagination/sorting options accepted by list endpoints. */
export interface ListQuery {
  page?: number
  pageSize?: number
  search?: string
  [key: string]: unknown
}
