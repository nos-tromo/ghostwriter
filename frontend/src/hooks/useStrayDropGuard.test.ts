import { describe, expect, it } from 'vitest'
import { createEvent, fireEvent, renderHook } from '@testing-library/react'
import { useStrayDropGuard } from './useStrayDropGuard'

/** Fire a drag event of `types` on `target`, as a browser does; the event, to read what became of it. */
function drag(target: Element, type: 'dragOver' | 'drop', types: string[]) {
  const event = createEvent[type](target, { dataTransfer: { files: [], types, dropEffect: 'copy' } })
  fireEvent(target, event)
  return event as DragEvent
}

describe('useStrayDropGuard', () => {
  it('keeps a file dropped outside a drop zone from opening in the tab', () => {
    const { unmount } = renderHook(() => useStrayDropGuard())
    const over = drag(document.body, 'dragOver', ['Files'])
    expect(over.defaultPrevented).toBe(true)
    expect(over.dataTransfer?.dropEffect).toBe('none')
    expect(drag(document.body, 'drop', ['Files']).defaultPrevented).toBe(true)
    // A link dropped on the page does not navigate either.
    expect(drag(document.body, 'drop', ['text/uri-list']).defaultPrevented).toBe(true)
    unmount()
    expect(drag(document.body, 'drop', ['Files']).defaultPrevented).toBe(false)
  })

  it('leaves text dropped into a text field, and a drop the page took, as they are', () => {
    renderHook(() => useStrayDropGuard())
    const field = document.body.appendChild(document.createElement('textarea'))
    expect(drag(field, 'dragOver', ['text/plain']).defaultPrevented).toBe(false)
    expect(drag(field, 'drop', ['text/plain']).defaultPrevented).toBe(false)

    const zone = document.body.appendChild(document.createElement('div'))
    zone.addEventListener('dragover', (event) => event.preventDefault())
    const over = drag(zone, 'dragOver', ['Files'])
    expect(over.defaultPrevented).toBe(true)
    expect(over.dataTransfer?.dropEffect).toBe('copy')
    field.remove()
    zone.remove()
  })
})
