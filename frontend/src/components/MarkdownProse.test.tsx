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
})
