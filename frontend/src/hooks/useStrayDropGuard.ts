import { useEffect } from 'react'

/** Whether a drop on this element is the browser's to handle: text into a text field. */
function isTextField(target: EventTarget | null): boolean {
  return (
    target instanceof HTMLElement &&
    (target.isContentEditable || target instanceof HTMLTextAreaElement || target instanceof HTMLInputElement)
  )
}

/**
 * Keep a drop beside a drop zone from navigating the tab: a browser opens a
 * file, and some a link, dropped where nothing takes it. A drag the page took
 * (its `dragover` or `drop` already prevented) is left as it is, and so is
 * text dragged into a text field.
 */
export function useStrayDropGuard() {
  useEffect(() => {
    const guard = (event: DragEvent) => {
      if (event.defaultPrevented) return
      const files = Array.from(event.dataTransfer?.types ?? []).includes('Files')
      if (!files && isTextField(event.target)) return
      event.preventDefault()
      if (event.type === 'dragover' && event.dataTransfer) event.dataTransfer.dropEffect = 'none'
    }
    window.addEventListener('dragover', guard)
    window.addEventListener('drop', guard)
    return () => {
      window.removeEventListener('dragover', guard)
      window.removeEventListener('drop', guard)
    }
  }, [])
}
