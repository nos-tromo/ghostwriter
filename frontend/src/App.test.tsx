import { describe, expect, it } from 'vitest'
import { render, screen, within } from '@testing-library/react'
import App from './App'

describe('App', () => {
  it('shows the app title in its one header row', () => {
    render(<App />)
    const [header] = screen.getAllByRole('banner')
    expect(screen.getAllByRole('banner')).toHaveLength(1)
    expect(within(header).getByText('Ghostwriter')).toBeInTheDocument()
  })
})
