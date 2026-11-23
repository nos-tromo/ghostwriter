import { describe, expect, it } from 'vitest'
import { clipKind, screenshotName } from './format'

describe('screenshotName', () => {
  const at = new Date(2026, 10, 20, 9, 5, 7)

  it('names a screenshot by its local time', () => {
    expect(screenshotName(at, new Set())).toBe('screenshot-20261120-090507.png')
  })

  it('counts up from 2 while the name is taken, before the extension', () => {
    const first = 'screenshot-20261120-090507.png'
    expect(screenshotName(at, new Set([first]))).toBe('screenshot-20261120-090507-2.png')
    expect(screenshotName(at, new Set([first, 'screenshot-20261120-090507-2.png']))).toBe(
      'screenshot-20261120-090507-3.png',
    )
  })

  it('pads single digits of every part', () => {
    expect(screenshotName(new Date(2026, 0, 2, 3, 4, 5), new Set())).toBe('screenshot-20260102-030405.png')
  })
})

describe('clipKind', () => {
  it('calls an audio suffix audio, in any case, and every other clip video', () => {
    expect(clipKind('voice-memo.M4A')).toBe('audio')
    expect(clipKind('voice-memo.opus')).toBe('audio')
    expect(clipKind('harbour-tour.mkv')).toBe('video')
    expect(clipKind('harbour-tour.WEBM')).toBe('video')
  })
})
