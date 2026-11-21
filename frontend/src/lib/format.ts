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

const pad = (n: number) => String(n).padStart(2, '0')

/** The name of a pasted screenshot: `screenshot-YYYYMMDD-HHMMSS.png` in the
 *  user's local time; while `taken` holds it, `-2`, `-3`, … before `.png`, so
 *  two pasted in one second are two files. */
export function screenshotName(at: Date, taken: ReadonlySet<string>): string {
  const stamp =
    `${at.getFullYear()}${pad(at.getMonth() + 1)}${pad(at.getDate())}` +
    `-${pad(at.getHours())}${pad(at.getMinutes())}${pad(at.getSeconds())}`
  const base = `screenshot-${stamp}`
  let name = `${base}.png`
  for (let n = 2; taken.has(name); n += 1) name = `${base}-${n}.png`
  return name
}
