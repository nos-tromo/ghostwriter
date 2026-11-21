import { useEffect, useState } from 'react'

/** An object URL of this mount's own for a blob, revoked when the mount goes
 *  or the blob changes, so one card's unmount never breaks another's image. */
export function useObjectUrl(blob: Blob | undefined): string | null {
  const [url, setUrl] = useState<string | null>(null)
  useEffect(() => {
    if (!blob) return
    const created = URL.createObjectURL(blob)
    setUrl(created)
    return () => {
      URL.revokeObjectURL(created)
      setUrl(null)
    }
  }, [blob])
  return url
}

/** Save a blob under its name through a transient object URL, revoked once the click has been dispatched. */
export function saveBlob(blob: Blob, name: string) {
  const url = URL.createObjectURL(blob)
  const anchor = document.createElement('a')
  anchor.href = url
  anchor.download = name
  anchor.click()
  setTimeout(() => URL.revokeObjectURL(url), 0)
}
