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

    // The words are the button: no chevron beside them.
    const toggle = screen.getByRole('button', { name: '2 steps' })
    expect(screen.getAllByRole('button')).toHaveLength(1)
    expect(toggle).toHaveAttribute('aria-expanded', 'false')
    expect(screen.queryByText('Ran Python')).not.toBeInTheDocument()

    await user.click(toggle)
    expect(toggle).toHaveAttribute('aria-expanded', 'true')
    const steps = screen.getAllByRole('listitem')
    expect(steps).toHaveLength(2)
    expect(within(steps[0]).getByRole('img', { name: 'Done' })).toBeInTheDocument()
    // The code stays behind the step's own text.
    expect(container.querySelector('pre')).toBeNull()
    const details = within(steps[0]).getByRole('button', { name: 'Ran Python' })
    expect(details).toHaveAttribute('aria-expanded', 'false')

    await user.click(details)
    expect(details).toHaveAttribute('aria-expanded', 'true')
    // The code, out of its JSON and highlighted as Python, then what it printed.
    const code = container.querySelector('code.language-python')
    expect(code).toHaveTextContent('import pandas as pd')
    expect(code?.querySelector('.hljs-keyword')).toHaveTextContent('import')
    expect(within(steps[0]).getByText('42')).toBeInTheDocument()

    await user.click(details)
    expect(details).toHaveAttribute('aria-expanded', 'false')
    expect(container.querySelector('pre')).toBeNull()

    // Once expired, the trail keeps what was done and says why the rest is gone;
    // the text opens nothing, so it is no button.
    expect(within(steps[1]).getByText('Read a document')).toBeInTheDocument()
    expect(within(steps[1]).getByText('Details deleted after 7 days')).toBeInTheDocument()
    expect(within(steps[1]).queryByRole('button')).not.toBeInTheDocument()

    await user.click(toggle)
    expect(toggle).toHaveAttribute('aria-expanded', 'false')
    expect(screen.queryByRole('listitem')).not.toBeInTheDocument()
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

  it('opens from the keyboard, as the buttons its texts are', async () => {
    const user = userEvent.setup()
    render(<Trail steps={[CODE]} fileDays={7} />)

    await user.tab()
    expect(screen.getByRole('button', { name: '1 step' })).toHaveFocus()
    await user.keyboard('{Enter}')
    await user.tab()
    const details = screen.getByRole('button', { name: 'Ran Python' })
    expect(details).toHaveFocus()
    await user.keyboard(' ')
    expect(details).toHaveAttribute('aria-expanded', 'true')
    expect(screen.getByText('42')).toBeInTheDocument()
  })

  it('stays open when the user clicks inside the code or the result', async () => {
    const user = userEvent.setup()
    const { container } = render(<Trail steps={[CODE]} fileDays={7} />)
    await user.click(screen.getByRole('button', { name: '1 step' }))
    const details = screen.getByRole('button', { name: 'Ran Python' })
    await user.click(details)

    const code = container.querySelector('code.language-python')
    expect(code).not.toBeNull()
    await user.click(code as Element)
    await user.click(screen.getByText('42'))
    expect(details).toHaveAttribute('aria-expanded', 'true')
    expect(screen.getByRole('button', { name: '1 step' })).toHaveAttribute('aria-expanded', 'true')
  })

  it('shows the steps as they run, without the disclosure', () => {
    render(<Trail steps={[step({ id: 's1', tool: 'list_files', status: 'running' })]} fileDays={7} live />)
    expect(screen.getByText('Listed the files')).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: '1 step' })).not.toBeInTheDocument()
  })
})
