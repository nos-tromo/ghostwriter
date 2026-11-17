import { afterEach, describe, expect, it, vi } from 'vitest'
import { act, renderHook } from '@testing-library/react'
import { RECORDER_TYPES, useRecorder } from './useRecorder'
import { installMedia, uninstallMedia } from '../test/media'

afterEach(() => {
  vi.useRealTimers()
  uninstallMedia()
})

function recorder(maxSeconds = 120) {
  const onRecorded = vi.fn<(blob: Blob, mime: string) => void>()
  const hook = renderHook(() => useRecorder({ maxSeconds, onRecorded }))
  return { onRecorded, ...hook }
}

describe('useRecorder', () => {
  it('picks the first recording format the browser supports', async () => {
    expect(RECORDER_TYPES).toEqual(['audio/webm;codecs=opus', 'audio/ogg;codecs=opus', 'audio/mp4'])
    // Safari: only mp4.
    const safari = installMedia({ supported: ['audio/mp4'] })
    const first = recorder()
    await act(() => first.result.current.start())
    expect(safari.recorders[0].options).toEqual({ mimeType: 'audio/mp4' })
    act(() => first.result.current.stop())
    expect(first.onRecorded).toHaveBeenCalledWith(expect.any(Blob), 'audio/mp4')
    first.unmount()

    // Firefox: ogg and mp4, so ogg.
    const firefox = installMedia({ supported: ['audio/ogg;codecs=opus', 'audio/mp4'] })
    const second = recorder()
    await act(() => second.result.current.start())
    expect(firefox.recorders[0].options).toEqual({ mimeType: 'audio/ogg;codecs=opus' })
    second.unmount()

    // None of them, or no MediaRecorder at all: the browser cannot record.
    const none = installMedia({ supported: ['audio/wav'] })
    const third = recorder()
    expect(third.result.current.state).toBe('unsupported')
    await act(() => third.result.current.start())
    expect(none.getUserMedia).not.toHaveBeenCalled()
    third.unmount()
    uninstallMedia()
    expect(recorder().result.current.state).toBe('unsupported')
  })

  it('releases the microphone after recording and says when access was denied', async () => {
    const media = installMedia()
    const { result, onRecorded, unmount } = recorder()
    expect(result.current.state).toBe('idle')

    await act(() => result.current.start())
    expect(media.getUserMedia).toHaveBeenCalledWith({ audio: true })
    expect(result.current.state).toBe('recording')
    expect(media.track.stop).not.toHaveBeenCalled()
    act(() => result.current.stop())
    expect(result.current.state).toBe('idle')
    expect(media.track.stop).toHaveBeenCalledTimes(1)
    const [blob, mime] = onRecorded.mock.calls[0]
    expect(mime).toBe('audio/webm;codecs=opus')
    expect(blob.type).toBe('audio/webm;codecs=opus')
    expect(await blob.text()).toBe('synthetic voice')

    // Leaving mid-recording releases it too, and nothing is handed on.
    await act(() => result.current.start())
    unmount()
    expect(media.track.stop).toHaveBeenCalledTimes(2)
    expect(onRecorded).toHaveBeenCalledTimes(1)

    const refused = installMedia({ denied: true })
    const denied = recorder()
    await act(() => denied.result.current.start())
    expect(refused.getUserMedia).toHaveBeenCalled()
    expect(denied.result.current.state).toBe('denied')
    expect(refused.recorders).toHaveLength(0)
  })

  it('counts the seconds and stops itself at the limit', async () => {
    vi.useFakeTimers()
    installMedia()
    const { result, onRecorded } = recorder(3)
    await act(() => result.current.start())
    expect(result.current.elapsedS).toBe(0)
    act(() => vi.advanceTimersByTime(2_000))
    expect(result.current.elapsedS).toBe(2)
    expect(onRecorded).not.toHaveBeenCalled()
    act(() => vi.advanceTimersByTime(1_000))
    expect(result.current.state).toBe('idle')
    expect(onRecorded).toHaveBeenCalledTimes(1)
  })
})
