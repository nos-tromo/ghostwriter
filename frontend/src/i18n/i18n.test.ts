import { describe, expect, it } from 'vitest'
import { en } from './en'
import { de } from './de'
import { format } from './index'

describe('i18n', () => {
  it('keeps de in step with en', () => {
    expect(Object.keys(de).sort()).toEqual(Object.keys(en).sort())
  })
  it('interpolates named placeholders', () => {
    expect(format('{n} Treffer', { n: 3 })).toBe('3 Treffer')
  })
  it('leaves unknown placeholders visible', () => {
    expect(format('{missing}', {})).toBe('{missing}')
  })
})
