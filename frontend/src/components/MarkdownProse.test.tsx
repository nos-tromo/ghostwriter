import { afterEach, describe, expect, it, vi } from 'vitest'
import { render, screen } from '@testing-library/react'
import { MarkdownProse } from './MarkdownProse'

afterEach(() => vi.unstubAllGlobals())

describe('MarkdownProse', () => {
  it('highlights code without any network request', () => {
    const fetch = vi.fn()
    vi.stubGlobal('fetch', fetch)
    const { container } = render(<MarkdownProse text={'```python\nprint(1)\n```'} />)

    expect(container.querySelector('code.hljs.language-python')).not.toBeNull()
    expect(container.querySelector('.hljs-built_in')).toHaveTextContent('print')
    expect(fetch).not.toHaveBeenCalled()
  })

  it('leaves a language it does not bundle, and plain text, unhighlighted', () => {
    const { container } = render(<MarkdownProse text={'```cobol\nDISPLAY 1\n```\n\n```text\nprint(1)\n```\n\n```\nprint(1)\n```'} />)
    expect(container.querySelectorAll('pre')).toHaveLength(3)
    expect(container.querySelector('[class^="hljs-"]')).toBeNull()
  })

  it('shows raw HTML in an answer as text', () => {
    const { container } = render(<MarkdownProse text={'Siehe <img src=x onerror=alert(1)> hier'} />)
    expect(container.querySelector('img')).toBeNull()
    expect(screen.getByText(/<img src=x onerror=alert\(1\)>/)).toBeInTheDocument()
  })

  it('shows a model-written image as its alt text, and fetches nothing', () => {
    const fetch = vi.fn()
    vi.stubGlobal('fetch', fetch)
    const { container } = render(
      <MarkdownProse text={'Siehe ![chart](/ghostwriter/api/chats/c1/files/f1) und ![](https://example.invalid/?q=x)'} />,
    )
    expect(container.querySelector('img')).toBeNull()
    expect(container).toHaveTextContent('Siehe chart und')
    expect(container.innerHTML).not.toContain('/ghostwriter/api/chats/c1/files/f1')
    expect(container.innerHTML).not.toContain('example.invalid')
    expect(fetch).not.toHaveBeenCalled()
  })

  it('links only to the web and to mail, in a new tab', () => {
    const { container } = render(
      <MarkdownProse
        text={
          '[a](javascript:alert(1)) [b](data:text/html;base64,PHNjcmlwdD4=) [c](/ghostwriter/api/chats/c1) ' +
          '[d](vbscript:msgbox) [e](https://example.invalid/report) [f](mailto:team@example.invalid)'
        }
      />,
    )
    const links = Array.from(container.querySelectorAll('a'))
    expect(links.map((a) => a.getAttribute('href'))).toEqual([
      'https://example.invalid/report',
      'mailto:team@example.invalid',
    ])
    for (const link of links) {
      expect(link).toHaveAttribute('target', '_blank')
      expect(link).toHaveAttribute('rel', 'noopener noreferrer')
    }
    // Everything else is its text, never an empty href that would reload the app.
    expect(container.querySelector('a[href=""]')).toBeNull()
    expect(container.querySelector('a:not([href])')).toBeNull()
    expect(container).toHaveTextContent(/^a b c d e \(https:\/\/example\.invalid\/report\) f \(mailto:team@example\.invalid\)$/)
  })

  it('shows where a link goes when its text says something else', () => {
    const { container } = render(<MarkdownProse text={'[click here](https://example.invalid/?q=x)'} />)
    const link = screen.getByRole('link', { name: 'click here' })
    expect(link).toHaveAttribute('href', 'https://example.invalid/?q=x')
    expect(container).toHaveTextContent('click here (https://example.invalid/?q=x)')
  })

  it('shows a bare URL once', () => {
    const { container } = render(
      <MarkdownProse text={'Siehe https://example.invalid/report und <https://example.invalid/b> oder team@example.invalid'} />,
    )
    expect(container.querySelectorAll('a')).toHaveLength(3)
    expect(container).toHaveTextContent(
      /^Siehe https:\/\/example\.invalid\/report und https:\/\/example\.invalid\/b oder team@example\.invalid$/,
    )
  })
})
