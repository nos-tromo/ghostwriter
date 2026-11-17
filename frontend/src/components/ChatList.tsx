import { useEffect, useId, useRef, useState } from 'react'
import {
  ActionBar,
  Button,
  ChevronDownIcon,
  DeleteButton,
  HoverIconAction,
  Input,
  Menu,
  MenuItem,
  NewButton,
  Spinner,
  cn,
} from '@infra/ui'
import { describeError } from '../api/errorMessage'
import type { ChatOut } from '../api/types'
import { useChats, useCreateChat, useDeleteChat, useRenameChat } from '../hooks/useChats'
import { useT } from '../i18n/LanguageContext'

/** The longest title the backend takes (`MAX_TITLE_CHARS`). */
const MAX_TITLE_CHARS = 200

function ErrorLine({ error }: { error: unknown }) {
  const t = useT()
  const { key, vars } = describeError(error)
  return (
    <p role="alert" className="px-3 text-xs text-danger">
      {t(key, vars)}
    </p>
  )
}

/** The user's chats, the latest activity first: start one, open one, rename
 *  or delete it. */
export function ChatList({ selected, onSelect }: { selected: string | null; onSelect: (id: string | null) => void }) {
  const t = useT()
  const headingId = useId()
  const chats = useChats()
  const create = useCreateChat()
  // The open chat as it is now, for a delete that settles after the user moved on.
  const selectedRef = useRef(selected)
  useEffect(() => {
    selectedRef.current = selected
  })

  const start = () => create.mutate(undefined, { onSuccess: (chat) => onSelect(chat.id) })
  const onDeleted = (id: string) => {
    if (selectedRef.current === id) onSelect(null)
  }

  return (
    <div className="flex min-h-0 flex-1 flex-col gap-2">
      <div className="flex items-center justify-between gap-2">
        <h2 id={headingId} className="px-3 text-xs font-medium uppercase tracking-wider text-muted-foreground">
          {t('chat.list')}
        </h2>
        <NewButton label={t('chat.new')} busy={create.isPending} onClick={start} className="h-7" />
      </div>
      {create.isError && <ErrorLine error={create.error} />}
      {chats.isPending ? (
        <Spinner className="mx-3" label={t('chat.loading')} />
      ) : chats.isError ? (
        <ErrorLine error={chats.error} />
      ) : chats.data.length === 0 ? (
        <p className="px-3 text-sm text-muted-foreground">{t('chat.list_empty')}</p>
      ) : (
        <ul aria-labelledby={headingId} className="flex flex-col gap-1">
          {chats.data.map((chat) => (
            <ChatRow
              key={chat.id}
              chat={chat}
              active={chat.id === selected}
              onSelect={onSelect}
              onDeleted={onDeleted}
            />
          ))}
        </ul>
      )}
    </div>
  )
}

function ChatRow({
  chat,
  active,
  onSelect,
  onDeleted,
}: {
  chat: ChatOut
  active: boolean
  onSelect: (id: string) => void
  onDeleted: (id: string) => void
}) {
  const t = useT()
  const del = useDeleteChat()
  const [editing, setEditing] = useState(false)
  const [confirming, setConfirming] = useState(false)
  const [failure, setFailure] = useState<unknown>(null)
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
      onDeleted(chat.id)
    } catch (error) {
      setFailure(error)
      close()
    }
  }

  return (
    <li>
      {editing ? (
        <RenameField chat={chat} onDone={finishRename} onFailed={setFailure} />
      ) : (
        <div className="group relative flex items-center">
          <button
            ref={(node) => {
              if (node && refocusRef.current) {
                refocusRef.current = false
                node.focus()
              }
            }}
            type="button"
            aria-current={active ? 'true' : undefined}
            onClick={() => onSelect(chat.id)}
            title={title}
            className={cn(
              'min-w-0 flex-1 truncate rounded-md px-3 py-2 pr-9 text-left text-sm hover:bg-accent',
              active && 'bg-primary/15 text-primary',
            )}
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
                icon={<ChevronDownIcon className="h-4 w-4" />}
                label={t('chat.options')}
                className="h-7"
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
                  <MenuItem onSelect={() => setEditing(true)}>{t('chat.rename')}</MenuItem>
                  <MenuItem
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
      {failure !== null && <ErrorLine error={failure} />}
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
