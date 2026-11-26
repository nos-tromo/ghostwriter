import { useEffect, useId, useRef, useState } from 'react'
import {
  ActionBar,
  Button,
  DeleteButton,
  HoverIconAction,
  Input,
  Menu,
  MenuItem,
  MoreIcon,
  NewButton,
  PencilIcon,
  Spinner,
  TrashIcon,
  cn,
} from '@infra/ui'
import { ApiError } from '../api/client'
import { describeError } from '../api/errorMessage'
import type { ChatOut } from '../api/types'
import { useChats, useDeleteChat, useRenameChat, useStartChat } from '../hooks/useChats'
import { useT } from '../i18n/LanguageContext'

/** The longest title the backend takes (`MAX_TITLE_CHARS`). */
const MAX_TITLE_CHARS = 200

/** What a delete's refusal tells the user to do: stop the answer still
 *  running (409), or try again once workbench can delete the files (502). */
const DELETE_REFUSALS: Partial<Record<number, 'chat.delete_busy' | 'chat.delete_files'>> = {
  409: 'chat.delete_busy',
  502: 'chat.delete_files',
}

/** Why a request failed; a delete's refusal says what the user can do about it. */
function ErrorLine({ error, deleting = false }: { error: unknown; deleting?: boolean }) {
  const t = useT()
  const { key, vars } = describeError(error)
  const refusal = deleting && error instanceof ApiError ? DELETE_REFUSALS[error.status] : undefined
  return (
    <p role="alert" className="px-3 text-xs text-danger">
      {refusal ? t(refusal) : t(key, vars)}
    </p>
  )
}

/** A request that failed in a row, and whether it was the delete. */
type Failure = { error: unknown; deleting: boolean }

/** The user's chats, the latest activity first: start one, open one, rename
 *  or delete it. */
export function ChatList({ selected, onSelect }: { selected: string | null; onSelect: (id: string | null) => void }) {
  const t = useT()
  const headingId = useId()
  const chats = useChats()
  const { start, pending: starting, error: startError } = useStartChat(onSelect)
  // The open chat as it is now, for a delete that settles after the user moved on.
  const selectedRef = useRef(selected)
  useEffect(() => {
    selectedRef.current = selected
  })
  // Each row's button and the new-chat button, where focus goes after a delete.
  const rowsRef = useRef(new Map<string, HTMLButtonElement>())
  const newRef = useRef<HTMLButtonElement>(null)
  const registerRow = (id: string, node: HTMLButtonElement | null) => {
    if (node) rowsRef.current.set(id, node)
    else rowsRef.current.delete(id)
  }

  // The deleted row takes its menu with it, so focus moves to the row that
  // followed it, or to the new-chat button after the last one, never to <body>.
  const onDeleted = (id: string, nextId: string | null) => {
    const next = nextId === null ? undefined : rowsRef.current.get(nextId)
    ;(next?.isConnected ? next : newRef.current)?.focus()
    if (selectedRef.current === id) onSelect(null)
  }

  return (
    <div className="flex min-h-0 flex-1 flex-col gap-2">
      <div className="flex items-center justify-between gap-2">
        <h2 id={headingId} className="px-3 text-xs font-medium uppercase tracking-wider text-muted-foreground">
          {t('chat.list')}
        </h2>
        <NewButton ref={newRef} label={t('chat.new')} busy={starting} onClick={start} className="h-7" />
      </div>
      {startError !== null && <ErrorLine error={startError} />}
      {chats.isPending ? (
        <Spinner className="mx-3" label={t('chat.loading')} />
      ) : chats.isError ? (
        <ErrorLine error={chats.error} />
      ) : chats.data.length === 0 ? (
        <p className="px-3 text-sm text-muted-foreground">{t('chat.list_empty')}</p>
      ) : (
        <ul aria-labelledby={headingId} className="flex flex-col gap-1">
          {chats.data.map((chat, i) => (
            <ChatRow
              key={chat.id}
              chat={chat}
              nextId={chats.data[i + 1]?.id ?? null}
              active={chat.id === selected}
              onSelect={onSelect}
              onDeleted={onDeleted}
              registerRow={registerRow}
            />
          ))}
        </ul>
      )}
    </div>
  )
}

function ChatRow({
  chat,
  nextId,
  active,
  onSelect,
  onDeleted,
  registerRow,
}: {
  chat: ChatOut
  /** The chat below this one, if any. */
  nextId: string | null
  active: boolean
  onSelect: (id: string) => void
  onDeleted: (id: string, nextId: string | null) => void
  registerRow: (id: string, node: HTMLButtonElement | null) => void
}) {
  const t = useT()
  const del = useDeleteChat()
  const [editing, setEditing] = useState(false)
  const [confirming, setConfirming] = useState(false)
  const [failure, setFailure] = useState<Failure | null>(null)
  // Set when a rename ends by key, so focus goes back to the row it left.
  const refocusRef = useRef(false)
  const title = chat.title || t('chat.untitled')

  const finishRename = (byKey: boolean) => {
    refocusRef.current = byKey
    setEditing(false)
  }

  const remove = async (close: () => void) => {
    setFailure(null)
    try {
      await del.mutateAsync(chat.id)
      // Closed first: an open menu pulls focus back into its panel on its
      // next render, and the trigger it hands focus to goes with the row.
      close()
      onDeleted(chat.id, nextId)
    } catch (error) {
      setFailure({ error, deleting: true })
      close()
    }
  }

  return (
    <li>
      {editing ? (
        <RenameField
          chat={chat}
          onDone={finishRename}
          onFailed={(error) => setFailure(error === null ? null : { error, deleting: false })}
        />
      ) : (
        // The row is one surface: it highlights on hover with the dots inside
        // it, and stays highlighted while its menu is open.
        <div
          className={cn(
            'group relative flex items-center rounded-lg',
            active ? 'bg-primary/15 text-primary' : 'hover:bg-accent has-[[aria-expanded=true]]:bg-accent',
          )}
        >
          <button
            ref={(node) => {
              registerRow(chat.id, node)
              if (node && refocusRef.current) {
                refocusRef.current = false
                node.focus()
              }
            }}
            type="button"
            aria-current={active ? 'true' : undefined}
            onClick={() => onSelect(chat.id)}
            title={title}
            className="min-w-0 flex-1 truncate rounded-lg px-3 py-2 pr-9 text-left text-sm"
          >
            {title}
          </button>
          <Menu
            align="end"
            className="absolute right-1"
            onOpenChange={(open) => {
              if (!open) setConfirming(false)
            }}
            trigger={(props) => (
              <HoverIconAction
                icon={<MoreIcon className="h-4 w-4" />}
                label={t('chat.options', { title })}
                className="h-7 hover:bg-transparent"
                {...props}
              />
            )}
          >
            {({ close }) =>
              confirming ? (
                <div className="w-60 max-w-full px-3 py-2">
                  <p className="whitespace-normal text-sm text-foreground">{t('chat.delete_confirm')}</p>
                  <ActionBar className="mt-2">
                    <Button size="sm" variant="secondary" disabled={del.isPending} onClick={() => setConfirming(false)}>
                      {t('common.cancel')}
                    </Button>
                    <DeleteButton
                      variant="danger"
                      label={t('chat.delete')}
                      busy={del.isPending}
                      onClick={() => void remove(close)}
                    >
                      {t('chat.delete')}
                    </DeleteButton>
                  </ActionBar>
                </div>
              ) : (
                <>
                  <MenuItem icon={<PencilIcon />} onSelect={() => setEditing(true)}>
                    {t('chat.rename')}
                  </MenuItem>
                  <MenuItem
                    icon={<TrashIcon />}
                    tone="danger"
                    closeOnSelect={false}
                    disabled={del.isPending}
                    onSelect={() => setConfirming(true)}
                  >
                    {t('chat.delete')}
                  </MenuItem>
                </>
              )
            }
          </Menu>
        </div>
      )}
      {failure !== null && <ErrorLine error={failure.error} deleting={failure.deleting} />}
    </li>
  )
}

/** The title as a field: Enter saves, Escape cancels, and leaving it cancels too. */
function RenameField({
  chat,
  onDone,
  onFailed,
}: {
  chat: ChatOut
  onDone: (byKey: boolean) => void
  onFailed: (error: unknown) => void
}) {
  const t = useT()
  const rename = useRenameChat()
  const [value, setValue] = useState(chat.title ?? '')

  const save = async () => {
    const title = value.trim()
    if (!title || title === chat.title) return onDone(true)
    onFailed(null)
    try {
      await rename.mutateAsync({ id: chat.id, title })
    } catch (error) {
      onFailed(error)
    }
    onDone(true)
  }

  return (
    <Input
      autoFocus
      aria-label={t('chat.title')}
      value={value}
      maxLength={MAX_TITLE_CHARS}
      readOnly={rename.isPending}
      onChange={(event) => setValue(event.target.value)}
      onFocus={(event) => event.currentTarget.select()}
      onKeyDown={(event) => {
        if (event.key === 'Enter') {
          event.preventDefault()
          if (!rename.isPending) void save()
        } else if (event.key === 'Escape') {
          event.preventDefault()
          onDone(true)
        }
      }}
      onBlur={() => {
        if (!rename.isPending) onDone(false)
      }}
      className="h-9"
    />
  )
}
