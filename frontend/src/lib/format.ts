import type { Lang } from '../i18n'

const UNITS = ['B', 'KB', 'MB', 'GB'] as const

/** A size in bytes as people read it, in the UI's language: `120 B`, `1.5 KB`, `12 MB`. */
export function formatSize(bytes: number, lang: Lang): string {
  let value = bytes
  let unit = 0
  while (value >= 1024 && unit < UNITS.length - 1) {
    value /= 1024
    unit += 1
  }
  const digits = unit === 0 || value >= 10 ? 0 : 1
  return `${new Intl.NumberFormat(lang, { maximumFractionDigits: digits }).format(value)} ${UNITS[unit]}`
}
