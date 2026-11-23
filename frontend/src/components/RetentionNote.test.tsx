import { describe, expect, it } from 'vitest'
import { render } from '@testing-library/react'
import { RetentionNote } from './RetentionNote'
import { LanguageContext } from '../i18n/LanguageContext'
import type { AppConfig } from '../api/types'

const CONFIG: AppConfig = {
  ui_language: 'en',
  require_purpose: [],
  speech_input: true,
  attachments: true,
  attachment_types: ['.csv', '.pdf', '.xlsx'],
  image_types: [],
  media_types: [],
  media: false,
  max_images: 8,
  vision: false,
  max_attachment_bytes: 50 * 1024 * 1024,
  max_recording_s: 120,
  max_recording_bytes: 10 * 1024 * 1024,
  chat_retention_days: 90,
  file_retention_days: 7,
  version: '0.1.0',
}

describe('RetentionNote', () => {
  it('names both retention periods', () => {
    const { container, unmount } = render(<RetentionNote config={CONFIG} />)
    expect(container).toHaveTextContent(
      'Chats are deleted 90 days after they were started. Files and tool results are deleted after 7 days.',
    )
    unmount()

    const german = render(
      <LanguageContext value="de">
        <RetentionNote config={CONFIG} />
      </LanguageContext>,
    ).container
    expect(german).toHaveTextContent(
      'Chats werden 90 Tage nach ihrem Beginn gelöscht. Dateien und Tool-Ergebnisse werden nach 7 Tagen gelöscht.',
    )
  })

  it('names only the chat period while attachments are off', () => {
    const { container, unmount } = render(<RetentionNote config={{ ...CONFIG, attachments: false }} />)
    expect(container).toHaveTextContent('Chats are deleted 90 days after they were started.')
    expect(container).not.toHaveTextContent('Files and tool results')
    unmount()

    // Neither period applies: no line at all.
    const none = render(<RetentionNote config={{ ...CONFIG, attachments: false, chat_retention_days: null }} />)
    expect(none.container).toBeEmptyDOMElement()
  })

  it('names only the file period while chats are kept', () => {
    const { container } = render(<RetentionNote config={{ ...CONFIG, chat_retention_days: null }} />)
    expect(container).toHaveTextContent('Files and tool results are deleted after 7 days.')
    expect(container).not.toHaveTextContent('Chats are deleted')
  })
})
