// The highlight.js grammars bundled for code blocks. They are imported here,
// so the build carries them and nothing is loaded at runtime; a fenced block
// in any other language stays plain text.
import type { LanguageFn } from 'highlight.js'
import bash from 'highlight.js/lib/languages/bash'
import css from 'highlight.js/lib/languages/css'
import javascript from 'highlight.js/lib/languages/javascript'
import json from 'highlight.js/lib/languages/json'
import markdown from 'highlight.js/lib/languages/markdown'
import python from 'highlight.js/lib/languages/python'
import sql from 'highlight.js/lib/languages/sql'
import typescript from 'highlight.js/lib/languages/typescript'
import xml from 'highlight.js/lib/languages/xml'
import yaml from 'highlight.js/lib/languages/yaml'

/** By name; each grammar's own aliases (`py`, `js`, `ts`, `sh`, `html`, `yml`, …) work too. */
export const languages: Record<string, LanguageFn> = {
  python,
  javascript,
  typescript,
  json,
  bash,
  sql,
  yaml,
  xml,
  css,
  markdown,
}
