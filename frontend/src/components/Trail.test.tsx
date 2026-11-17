import { describe, expect, it } from 'vitest'
import { render, screen, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { Trail } from './Trail'
import { LanguageContext } from '../i18n/LanguageContext'
import { step } from '../test/fixtures'

const CODE = step({
  id: 's1',
  tool: 'run_python',
  arguments: JSON.stringify({ code: 'import pandas as pd\nprint(pd.read_excel("sales.xlsx")["B"].sum())' }),
  result: '42',
})
const EXPIRED = step({ id: 's2', tool: 'read_document', expired: true })

describe('Trail', () => {
  it('keeps a step’s code behind a disclosure and says when it expired', async () => {
    const user = userEvent.setup()
    const { container } = render(<Trail steps={[CODE, EXPIRED]} fileDays={7} />)

    const toggle = screen.getByRole('button', { name: '2 steps' })
    expect(toggle).toHaveAttribute('aria-expanded', 'false')
    expect(screen.queryByText('Ran Python')).not.toBeInTheDocument()

    await user.click(toggle)
    expect(toggle).toHaveAttribute('aria-expanded', 'true')
    const steps = screen.getAllByRole('listitem')
    expect(steps).toHaveLength(2)
    expect(within(steps[0]).getByText('Ran Python')).toBeInTheDocument()
    expect(within(steps[0]).getByRole('img', { name: 'Done' })).toBeInTheDocument()
    // The code stays behind its own disclosure.
    expect(container.querySelector('pre')).toBeNull()
    const details = within(steps[0]).getByRole('button', { name: 'Details' })
    expect(details).toHaveAttribute('aria-expanded', 'false')

    await user.click(details)
    expect(details).toHaveAttribute('aria-expanded', 'true')
    // The code, out of its JSON and highlighted as Python, then what it printed.
    const code = container.querySelector('code.language-python')
    expect(code).toHaveTextContent('import pandas as pd')
    expect(code?.querySelector('.hljs-keyword')).toHaveTextContent('import')
    expect(within(steps[0]).getByText('42')).toBeInTheDocument()

    // Once expired, the trail keeps what was done and says why the rest is gone.
    expect(within(steps[1]).getByText('Read a document')).toBeInTheDocument()
    expect(within(steps[1]).getByText('Details deleted after 7 days')).toBeInTheDocument()
    expect(within(steps[1]).queryByRole('button', { name: 'Details' })).not.toBeInTheDocument()
  })

  it('labels every workbench tool, and an unknown one by name', async () => {
    const user = userEvent.setup()
    const tools = [
      step({ id: 't1', tool: 'list_files' }),
      step({ id: 't2', tool: 'read_document', detail: 'sales.xlsx' }),
      step({ id: 't3', tool: 'search_files', detail: 'Umsatz' }),
      step({ id: 't4', tool: 'run_python' }),
      step({ id: 't5', tool: 'inspect_template', detail: 'brief.docx' }),
      step({ id: 't6', tool: 'fill_template', detail: 'brief.docx' }),
      step({ id: 't7', tool: 'write_table', detail: 'summe.xlsx' }),
      step({ id: 't8', tool: 'write_document', detail: 'bericht.docx' }),
      step({ id: 't9', tool: 'make_chart', detail: 'chart.png' }),
      step({ id: 't10', tool: 'search_files', status: 'error' }),
      step({ id: 't11', tool: 'write_table', status: 'running' }),
    ]
    const { unmount } = render(<Trail steps={tools} fileDays={7} />)
    await user.click(screen.getByRole('button', { name: '11 steps' }))

    expect(screen.getAllByRole('listitem').map((item) => item.textContent)).toEqual([
      'Listed the files',
      'Read sales.xlsx',
      'Searched for Umsatz',
      'Ran Python',
      'Inspected the template brief.docx',
      'Filled the template brief.docx',
      'Wrote the table summe.xlsx',
      'Wrote the document bericht.docx',
      'Used make_chart',
      'Searched the files',
      'Wrote a table',
    ])
    expect(screen.getByRole('img', { name: 'Failed' })).toBeInTheDocument()
    expect(screen.getByRole('status', { name: 'Running' })).toBeInTheDocument()
    unmount()

    render(
      <LanguageContext value="de">
        <Trail steps={[tools[1], tools[8]]} fileDays={7} />
      </LanguageContext>,
    )
    await user.click(screen.getByRole('button', { name: '2 Schritte' }))
    expect(screen.getAllByRole('listitem').map((item) => item.textContent)).toEqual([
      'sales.xlsx gelesen',
      'make_chart verwendet',
    ])
  })

  it('shows the steps as they run, without the disclosure', () => {
    render(<Trail steps={[step({ id: 's1', tool: 'list_files', status: 'running' })]} fileDays={7} live />)
    expect(screen.getByText('Listed the files')).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: '1 step' })).not.toBeInTheDocument()
  })
})
