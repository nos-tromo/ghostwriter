import { useCallback, useEffect, useRef, useState } from 'react'

/** The recording formats asked for, in order: Chrome's, Firefox's, Safari's. */
export const RECORDER_TYPES = ['audio/webm;codecs=opus', 'audio/ogg;codecs=opus', 'audio/mp4']

export type RecorderState = 'idle' | 'recording' | 'unsupported' | 'denied'

export interface Recorder {
  state: RecorderState
  /** Whole seconds recorded so far; 0 while not recording. */
  elapsedS: number
  start(): Promise<void>
  stop(): void
}

/** The first of {@link RECORDER_TYPES} this browser records, or null when it records none of them. */
function recorderType(): string | null {
  if (typeof MediaRecorder === 'undefined' || !navigator.mediaDevices?.getUserMedia) return null
  return RECORDER_TYPES.find((type) => MediaRecorder.isTypeSupported(type)) ?? null
}

/** Whether the browser refused the microphone (the user, or a policy), not merely failed to open one. */
function isRefusal(error: unknown): boolean {
  return error instanceof DOMException && (error.name === 'NotAllowedError' || error.name === 'SecurityError')
}

/**
 * Dictation's recorder: the microphone opened on `start`, recorded in the
 * first format the browser supports, and closed again on `stop`, at
 * `maxSeconds`, or when the component goes.
 *
 * The recording stays in memory: its chunks live in this closure until they
 * are handed to `onRecorded` as one blob, and nothing is written anywhere.
 * Every track of the microphone is stopped once the recorder has finished, so
 * the browser's recording indicator goes out. A recording cut short by an
 * unmount is dropped, never handed on.
 *
 * @param options.maxSeconds - The longest recording; it stops itself there.
 * @param options.onRecorded - Gets the recording and its type.
 * @returns The state, the seconds so far, and the two actions.
 */
export function useRecorder({
  maxSeconds,
  onRecorded,
}: {
  maxSeconds: number
  onRecorded: (blob: Blob, mime: string) => void
}): Recorder {
  const [type] = useState(recorderType)
  const [state, setState] = useState<RecorderState>(type === null ? 'unsupported' : 'idle')
  const [elapsedS, setElapsedS] = useState(0)
  const stopRef = useRef<() => void>(() => {})
  const busyRef = useRef(false)
  const aliveRef = useRef(true)
  const onRecordedRef = useRef(onRecorded)
  useEffect(() => {
    onRecordedRef.current = onRecorded
  }, [onRecorded])

  useEffect(() => {
    aliveRef.current = true
    return () => {
      aliveRef.current = false
      stopRef.current()
    }
  }, [])

  const start = useCallback(async () => {
    if (type === null || busyRef.current) return
    busyRef.current = true
    let media: MediaStream
    try {
      media = await navigator.mediaDevices.getUserMedia({ audio: true })
    } catch (error) {
      busyRef.current = false
      if (aliveRef.current) setState(isRefusal(error) ? 'denied' : 'unsupported')
      return
    }
    const release = () => media.getTracks().forEach((track) => track.stop())
    if (!aliveRef.current) {
      release()
      busyRef.current = false
      return
    }

    let recorder: MediaRecorder
    try {
      recorder = new MediaRecorder(media, { mimeType: type })
      recorder.start()
    } catch {
      // The format was offered but cannot be recorded after all.
      release()
      busyRef.current = false
      setState('unsupported')
      return
    }
    const chunks: Blob[] = []
    const startedAt = Date.now()
    const stop = () => {
      if (recorder.state !== 'inactive') recorder.stop()
    }
    const timer = setInterval(() => {
      const seconds = Math.min(maxSeconds, Math.floor((Date.now() - startedAt) / 1000))
      setElapsedS(seconds)
      if (seconds >= maxSeconds) stop()
    }, 1000)
    recorder.ondataavailable = (event) => {
      if (event.data.size > 0) chunks.push(event.data)
    }
    recorder.onstop = () => {
      clearInterval(timer)
      release()
      busyRef.current = false
      if (!aliveRef.current) return
      setState('idle')
      setElapsedS(0)
      onRecordedRef.current(new Blob(chunks, { type }), type)
    }
    stopRef.current = stop
    setElapsedS(0)
    setState('recording')
  }, [type, maxSeconds])

  const stop = useCallback(() => stopRef.current(), [])

  return { state, elapsedS, start, stop }
}
