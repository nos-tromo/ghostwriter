import { Button, WarningIcon } from '@infra/ui'
import type { MeasureUnit, NoticeOut } from '../api/types'
import type { Strings } from '../i18n'
import { useT } from '../i18n/LanguageContext'

type T = ReturnType<typeof useT>

/** What a document is counted in, as the words for many and for one. */
const UNIT_KEYS: Record<MeasureUnit, { many: keyof Strings; one: keyof Strings }> = {
  page: { many: 'unit.page', one: 'unit.page_one' },
  slide: { many: 'unit.slide', one: 'unit.slide_one' },
  paragraph: { many: 'unit.paragraph', one: 'unit.paragraph_one' },
  row: { many: 'unit.row', one: 'unit.row_one' },
  line: { many: 'unit.line', one: 'unit.line_one' },
}

/** A document notice's file name, while it is kept: the details expire with the file's other content. */
function nameOf(notice: NoticeOut): string | null {
  return typeof notice.name === 'string' && notice.name !== '' ? notice.name : null
}

/** What a document was read of, when the notice still has all of it: how much, of how much, in what. */
function partialText(notice: NoticeOut, t: T): string {
  const { read, total, unit } = notice
  const name = nameOf(notice)
  if (name === null || typeof read !== 'number' || typeof total !== 'number' || !unit || !Object.hasOwn(UNIT_KEYS, unit)) {
    return t('notice.document_partial_generic')
  }
  const units = t(total === 1 ? UNIT_KEYS[unit].one : UNIT_KEYS[unit].many)
  return t('notice.document_partial', { name, read, total, units })
}

/** The line a notice says, or null for one not said under an answer: the trimmed context is the context
 *  meter's, and a code this app does not know is not this app's to say. */
function lineText(notice: NoticeOut, t: T): string | null {
  switch (notice.code) {
    case 'answer_cut':
      return t('notice.answer_cut')
    case 'document_partial':
      return partialText(notice, t)
    case 'document_unread': {
      const name = nameOf(notice)
      return name === null ? t('notice.document_unread_generic') : t('notice.document_unread', { name })
    }
    case 'images_dropped':
      return t('chat.images_dropped')
    default:
      return null
  }
}

/**
 * What the server said about an answer besides its text, quietly, a line each: it was cut at the
 * length limit, a document was read in part or not at all, the model took no pictures. Not errors, so
 * the text stays muted and only the icon takes the warning colour.
 *
 * @param notices - The answer's notices, in order; one this app does not know is left out. Two may be equal
 *   (two files unread, their details expired), and each has its line.
 * @param live - Whether the answer is being written: its lines are then announced politely, and a
 *   stored answer's are not.
 * @param onContinue - When given, the `answer_cut` line offers to go on with the answer.
 */
export function NoticeLines({
  notices,
  live,
  onContinue,
}: {
  notices: readonly NoticeOut[]
  live: boolean
  onContinue?: () => void
}) {
  const t = useT()
  // Keyed by position: once its details expire a notice is its code alone, so two files unread are two
  // equal notices. The list is only ever appended to while the answer is written, and static after.
  const lines = notices.flatMap((notice, index) => {
    const text = lineText(notice, t)
    return text === null ? [] : [{ notice, text, index }]
  })
  if (lines.length === 0) return null
  return (
    <div className="space-y-1">
      {lines.map(({ notice, text, index }) => (
        <div
          key={`${index}:${notice.code}`}
          data-notice={notice.code}
          className="flex flex-wrap items-center gap-x-2 gap-y-1"
        >
          <WarningIcon aria-hidden="true" className="size-4 shrink-0 text-warning" />
          <p role={live ? 'status' : undefined} className="text-sm text-muted-foreground">
            {text}
          </p>
          {notice.code === 'answer_cut' && onContinue && (
            <Button variant="ghost" size="sm" onClick={onContinue}>
              {t('notice.continue')}
            </Button>
          )}
        </div>
      ))}
    </div>
  )
}
