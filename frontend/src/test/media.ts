// A fake microphone and MediaRecorder for the recorder's tests. The recording
// is a few synthetic bytes; nothing is captured.
import { vi } from 'vitest'

export class FakeRecorder {
  static instances: FakeRecorder[] = []
  static supported: string[] = []
  static isTypeSupported(type: string): boolean {
    return FakeRecorder.supported.includes(type)
  }

  state: RecordingState = 'inactive'
  readonly mimeType: string
  ondataavailable: ((event: { data: Blob }) => void) | null = null
  onstop: (() => void) | null = null

  constructor(
    readonly stream: MediaStream,
    readonly options?: MediaRecorderOptions,
  ) {
    this.mimeType = options?.mimeType ?? ''
    FakeRecorder.instances.push(this)
  }

  start() {
    this.state = 'recording'
  }

  /** As a browser does: the last data, then `stop`. */
  stop() {
    if (this.state === 'inactive') return
    this.state = 'inactive'
    this.ondataavailable?.({ data: new Blob(['synthetic voice'], { type: this.mimeType }) })
    this.onstop?.()
  }
}

/**
 * Give the page a microphone and a MediaRecorder that takes `supported`.
 * `denied` refuses the microphone as a browser does when the user says no;
 * `failure` names the error the next request for it throws instead
 * (`NotFoundError`, `NotReadableError`), once.
 */
export function installMedia({
  supported = ['audio/webm;codecs=opus', 'audio/ogg;codecs=opus', 'audio/mp4'],
  denied = false,
  failure,
}: { supported?: string[]; denied?: boolean; failure?: string } = {}) {
  FakeRecorder.instances = []
  FakeRecorder.supported = supported
  const track = { stop: vi.fn() }
  let failing = failure
  const getUserMedia = vi.fn(async (_constraints: MediaStreamConstraints) => {
    if (denied) throw new DOMException('Permission denied', 'NotAllowedError')
    if (failing) {
      const name = failing
      failing = undefined
      throw new DOMException('No microphone', name)
    }
    return { getTracks: () => [track] } as unknown as MediaStream
  })
  vi.stubGlobal('MediaRecorder', FakeRecorder)
  Object.defineProperty(navigator, 'mediaDevices', { value: { getUserMedia }, configurable: true })
  return { track, getUserMedia, recorders: FakeRecorder.instances }
}

/** Take the microphone away again. */
export function uninstallMedia() {
  delete (navigator as { mediaDevices?: unknown }).mediaDevices
  vi.unstubAllGlobals()
}
