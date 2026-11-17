// Copied from docint's frontend/src/components/common/MarkdownProse.tsx, with
// bundled code highlighting (rehype-highlight) and the code block the trail uses.
import Markdown, { type Components, type Options as MarkdownOptions } from 'react-markdown'
import rehypeHighlight, { type Options as HighlightOptions } from 'rehype-highlight'
import remarkGfm from 'remark-gfm'
import { cn, useTheme } from '@infra/ui'
import { languages } from '../lib/highlight'

const components: Components = {
  // A model-written table is often wider than its panel, so it scrolls in its own box.
  table: ({ node: _node, ...props }) => (
    <div className="overflow-x-auto" data-testid="markdown-table">
      <table {...props} />
    </div>
  ),
}

/** Only the bundled grammars, and only for a block that names its language:
 *  no guessing, and `txt` / `text` / `plain` stay plain. The output is
 *  classes, never inline styles or markup. */
const highlight: HighlightOptions = { languages, detect: false, plainText: ['txt', 'text', 'plain'] }
const rehypePlugins: MarkdownOptions['rehypePlugins'] = [[rehypeHighlight, highlight]]

/**
 * Model-written Markdown (chat answers) rendered as prose.
 *
 * Raw HTML in the source is shown as text, never parsed — there is no
 * `rehype-raw` — so an answer cannot inject markup into the page.
 */
export function MarkdownProse({ text, className }: { text: string; className?: string }) {
  // The typography plugin's `prose-invert` hardcodes light-on-dark prose
  // colors; only apply it when the resolved theme is actually dark, or
  // markdown renders illegibly on the light-theme `bg-muted` panel.
  const { resolved } = useTheme()
  return (
    <div
      className={cn(
        'prose prose-sm max-w-none break-words prose-p:my-2 prose-pre:bg-muted prose-pre:text-foreground prose-code:before:content-none prose-code:after:content-none',
        resolved === 'dark' && 'prose-invert',
        className,
      )}
    >
      <Markdown remarkPlugins={[remarkGfm]} rehypePlugins={rehypePlugins} components={components}>
        {text}
      </Markdown>
    </div>
  )
}

/** A fence the code cannot close: longer than any run of backticks in it. */
function fence(code: string, language: string): string {
  const longest = Math.max(0, ...Array.from(code.matchAll(/`+/g), (run) => run[0].length))
  const ticks = '`'.repeat(Math.max(3, longest + 1))
  return `${ticks}${language}\n${code}\n${ticks}`
}

/** Code shown as a highlighted block, through the same pipeline as an answer's. */
export function CodeBlock({ code, language }: { code: string; language: string }) {
  return <MarkdownProse text={fence(code, language)} className="prose-pre:my-0" />
}
