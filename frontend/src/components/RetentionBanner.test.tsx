import { describe, expect, it } from 'vitest'
import { render, screen } from '@testing-library/react'
import { RetentionBanner } from './RetentionBanner'
import { LanguageContext } from '../i18n/LanguageContext'
import type { AppConfig } from '../api/types'

const CONFIG: AppConfig = {
  ui_language: 'en',
  require_purpose: [],
  speech_input: true,
  attachments: true,
  attachment_types: ['.csv', '.pdf', '.xlsx'],
  max_attachment_bytes: 50 * 1024 * 1024,
  max_recording_s: 120,
  max_recording_bytes: 10 * 1024 * 1024,
  chat_retention_days: 90,
  file_retention_days: 7,
  version: '0.1.0',
}

describe('RetentionBanner', () => {
  it('names both retention periods', () => {
    const { unmount } = render(<RetentionBanner config={CONFIG} />)
    const banner = screen.getByRole('status')
    expect(banner).toHaveTextContent('Chats are deleted 90 days after they were started.')
    expect(banner).toHaveTextContent('Files and tool results are deleted after 7 days.')
    unmount()

    render(
      <LanguageContext value="de">
        <RetentionBanner config={CONFIG} />
      </LanguageContext>,
    )
    const german = screen.getByRole('status')
    expect(german).toHaveTextContent('Chats werden 90 Tage nach ihrem Beginn gelöscht.')
    expect(german).toHaveTextContent('Dateien und Tool-Ergebnisse werden nach 7 Tagen gelöscht.')
  })

  it('names only the file period while chats are kept', () => {
    render(<RetentionBanner config={{ ...CONFIG, chat_retention_days: null }} />)
    const banner = screen.getByRole('status')
    expect(banner).toHaveTextContent('Files and tool results are deleted after 7 days.')
    expect(banner).not.toHaveTextContent('Chats are deleted')
  })
})
