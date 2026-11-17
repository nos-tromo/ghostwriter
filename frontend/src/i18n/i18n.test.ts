import { describe, expect, it } from 'vitest'
import { en } from './en'
import { de } from './de'
import { format } from './index'

describe('i18n', () => {
  it('keeps de in step with en', () => {
    expect(Object.keys(de).sort()).toEqual(Object.keys(en).sort())
  })
  it('addresses the user as "Sie" in German, as every sibling app does', () => {
    const informal = Object.entries(de).filter(([, text]) => /\b(du|dich|dir|dein\w*)\b/i.test(text))
    expect(informal).toEqual([])
    expect(de['chat.delete_busy']).toBe('Beenden Sie die Antwort, bevor Sie den Chat löschen.')
  })
  it('interpolates named placeholders', () => {
    expect(format('{n} Treffer', { n: 3 })).toBe('3 Treffer')
  })
  it('leaves unknown placeholders visible', () => {
    expect(format('{missing}', {})).toBe('{missing}')
  })
})
