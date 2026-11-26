import { describe, expect, it } from 'vitest'
import { CONFIG } from '../test/fixtures'
import { NEAR_FULL_RATIO, contextRatio, nearlyFull } from './context'

describe('contextRatio', () => {
  it('divides the estimate by the budget of the switch', () => {
    expect(contextRatio(20_400, CONFIG, false)).toBe(0.6)
    expect(contextRatio(20_400, CONFIG, true)).toBeCloseTo(0.785, 3)
  })

  it('caps at 1 and never rounds', () => {
    expect(contextRatio(40_000, CONFIG, false)).toBe(1)
    expect(contextRatio(27_166, CONFIG, false)).toBeCloseTo(0.799, 6)
  })

  it('is null without an estimate', () => {
    expect(contextRatio(null, CONFIG, false)).toBeNull()
    expect(contextRatio(null, CONFIG, true)).toBeNull()
  })

  it('is null for a budget that cannot be divided by', () => {
    expect(contextRatio(1_000, { ...CONFIG, prompt_tokens: 0 }, false)).toBeNull()
  })
})

describe('nearlyFull', () => {
  it('is decided on the unrounded ratio, from NEAR_FULL_RATIO', () => {
    expect(NEAR_FULL_RATIO).toBe(0.8)
    expect(nearlyFull(0.799)).toBe(false)
    expect(nearlyFull(0.8)).toBe(true)
    expect(nearlyFull(1)).toBe(true)
  })

  it('is false without a ratio', () => {
    expect(nearlyFull(null)).toBe(false)
  })
})
