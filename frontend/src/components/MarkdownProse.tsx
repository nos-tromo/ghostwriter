// Copied from docint's frontend/src/components/common/MarkdownProse.tsx, with
// bundled code highlighting (rehype-highlight), a copy button per code block,
// and the code block the trail uses.
import type { ComponentProps } from 'react'
import Markdown, { type Components, type ExtraProps, type Options as MarkdownOptions } from 'react-markdown'
import rehypeHighlight, { type Options as HighlightOptions } from 'rehype-highlight'
import remarkGfm from 'remark-gfm'
import { CopyButton, cn, useTheme } from '@infra/ui'
import { useT } from '../i18n/LanguageContext'
import { languages } from '../lib/highlight'

/** Where a link may go: the web and mail. Anything else (`javascript:` and
 *  `data:`, which react-markdown already empties, or a path into this app) is
 *  shown as its text, never as an empty href that would reload the app. */
const LINK_PROTOCOLS: ReadonlySet<string> = new Set(['http:', 'https:', 'mailto:'])

function linkTarget(href: string | undefined): string | null {
  if (!href) return null
  try {
    return LINK_PROTOCOLS.has(new URL(href).protocol) ? href : null
  } catch {
    return null
  }
}

/** The text of a hast node and everything in it. */
function textOf(node: unknown): string {
  if (!node || typeof node !== 'object') return ''
  const { type, value, children } = node as { type?: unknown; value?: unknown; children?: unknown }
  if (type === 'text') return typeof value === 'string' ? value : ''
  return Array.isArray(children) ? children.map(textOf).join('') : ''
}

/** Whether a link's text already says where it goes: a bare URL, or GFM's
 *  autolinked `www.` and e-mail forms. */
function saysWhere(text: string, href: string): boolean {
  return text === href || `http://${text}` === href || `mailto:${text}` === href
}

/** A code block with a button that copies its code, as written: no fence,
 *  no highlighting. The wrapper takes the block's margins, so the button sits
 *  on the block's corner. */
function CopyablePre({ node, ...props }: ComponentProps<'pre'> & ExtraProps) {
  const t = useT()
  return (
    <div className="relative my-3 first:mt-0 last:mb-0 [&>pre]:my-0">
      <pre {...props} />
      <CopyButton
        text={textOf(node).replace(/\n$/, '')}
        label={t('chat.copy_code')}
        copiedLabel={t('common.copied')}
        className="absolute right-1 top-1"
      />
    </div>
  )
}

const components: Components = {
  pre: CopyablePre,
  // A model-written table is often wider than its panel, so it scrolls in its own box.
  table: ({ node: _node, ...props }) => (
    <div className="overflow-x-auto" data-testid="markdown-table">
      <table {...props} />
    </div>
  ),
  // The model's text is no place to load from: an image would be fetched
  // without the stated purpose, or carry data off in its URL. Image results
  // come as cards; here only the alt text is shown.
  img: ({ alt }) => (alt ? <span>{alt}</span> : null),
  // A link opens in a new tab, which keeps a streaming answer running, and
  // sends no referrer. When its text says something else, the full URL is
  // shown beside it, so a hidden target cannot pass for another.
  a: ({ node, href, children }) => {
    const target = linkTarget(href)
    if (target === null) return <span>{children}</span>
    return (
      <>
        <a href={target} target="_blank" rel="noopener noreferrer">
          {children}
        </a>
        {!saysWhere(textOf(node).trim(), target) && (
          <span className="break-all text-muted-foreground"> ({target})</span>
        )}
      </>
    )
  },
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
