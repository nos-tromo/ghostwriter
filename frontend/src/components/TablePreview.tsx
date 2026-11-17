// TablePreview from docint's frontend/src/components/chat/AnalysisCard.tsx,
// over workbench's preview of a table a tool wrote.
import { useId } from 'react'
import type { TablePreview as Preview } from '../api/types'
import { useT } from '../i18n/LanguageContext'

/** Rows of a table the card shows; the rest are counted. */
const PREVIEW_ROWS = 5

/** Stored JSON, so a field that should be a list is trusted only as one. */
function list<T>(value: unknown): T[] {
  return Array.isArray(value) ? (value as T[]) : []
}

function text(value: unknown): string {
  return typeof value === 'string' ? value : ''
}

/**
 * The header and first rows of a table, and how many more it has.
 *
 * `row_count` counts the data rows below the header, so the rest is that
 * minus the rows shown; a table whose rows were not counted but cut short
 * says that more follow in the file.
 *
 * Every cell was read from a file, so it goes through React as text and nothing else.
 * The scroll area is reachable by keyboard and named by the table's caption, the file's name.
 */
export function TablePreview({ preview, caption }: { preview: Preview; caption: string }) {
  const t = useT()
  const captionId = useId()
  const columns = list<string>(preview.columns)
  const shown = list<string[]>(preview.rows).slice(0, PREVIEW_ROWS)
  const more = typeof preview.row_count === 'number' ? Math.max(0, preview.row_count - shown.length) : 0
  const uncounted = preview.row_count === null && preview.truncated === true
  return (
    <div className="space-y-1">
      <div
        role="region"
        aria-labelledby={captionId}
        tabIndex={0}
        className="overflow-x-auto rounded-md border border-border outline-none focus-visible:ring-1 focus-visible:ring-primary"
      >
        <table className="w-full text-xs">
          <caption id={captionId} className="break-words px-2 py-1 text-left text-xs text-muted-foreground">
            {caption}
          </caption>
          <thead className="text-muted-foreground">
            <tr>
              {columns.map((column, c) => (
                // A header may repeat, or be empty, and the table is never reordered.
                // eslint-disable-next-line @eslint-react/no-array-index-key
                <th key={c} scope="col" className="px-2 py-1 text-left font-medium">
                  {text(column)}
                </th>
              ))}
            </tr>
          </thead>
          <tbody>
            {shown.map((row, r) => (
              // eslint-disable-next-line @eslint-react/no-array-index-key
              <tr key={r} className="border-t border-border">
                {list<string>(row).map((cell, c) => (
                  // eslint-disable-next-line @eslint-react/no-array-index-key
                  <td key={c} className="break-words px-2 py-1 align-top">
                    {text(cell)}
                  </td>
                ))}
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      {more > 0 && (
        <div className="text-xs text-muted-foreground">
          {more === 1 ? t('result.more_rows_one') : t('result.more_rows', { count: more })}
        </div>
      )}
      {uncounted && <div className="text-xs text-muted-foreground">{t('result.more_rows_unknown')}</div>}
    </div>
  )
}
