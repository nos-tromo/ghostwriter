import { useMutation, useQuery, useQueryClient, type QueryClient } from '@tanstack/react-query'
import { createChat, deleteChat, getChat, listChats, renameChat } from '../api/chats'
import type { ChatDetail, ChatOut } from '../api/types'

export const chatsKey = ['chats'] as const
export const chatKey = (id: string | null) => ['chat', id] as const

/** The latest activity first, as the server orders them. A new answer, a
 *  regenerate and a rename all move a chat (its `updated_at`), so a cached
 *  list is ordered again on every read rather than kept in the order it
 *  arrived. Never by `created_at`. */
function byActivity(chats: ChatOut[]): ChatOut[] {
  return [...chats].sort((a, b) => Date.parse(b.updated_at) - Date.parse(a.updated_at))
}

/** Put a chat the server returned into the cached list, in its place or as a
 *  new entry, and into the chat's own entry; the list's order follows from
 *  its `updated_at`. */
export function storeChat(qc: QueryClient, chat: ChatOut) {
  qc.setQueryData<ChatOut[]>(chatsKey, (chats = []) =>
    chats.some((c) => c.id === chat.id) ? chats.map((c) => (c.id === chat.id ? chat : c)) : [...chats, chat],
  )
  qc.setQueryData<ChatDetail>(chatKey(chat.id), (detail) => (detail ? { ...detail, chat } : detail))
}

export function useChats() {
  return useQuery({ queryKey: chatsKey, queryFn: listChats, select: byActivity })
}

let order = 0
/** The next mark on the page's own order of events: when a chat's read
 *  began, when a file was attached. */
export const nextMark = (): number => ++order

/** When the read that brought a chat's detail began, by the detail; the
 *  chat's query keeps the object its read returned (no structural sharing). */
const readMarks = new WeakMap<ChatDetail, number>()

/** The mark of the read that brought `detail` (see {@link nextMark});
 *  `undefined` for none, or for a detail put in the cache here. */
export function readMarkOf(detail: ChatDetail | undefined): number | undefined {
  return detail === undefined ? undefined : readMarks.get(detail)
}

export function useChat(id: string | null) {
  return useQuery({
    queryKey: chatKey(id),
    queryFn: async () => {
      const mark = nextMark()
      const detail = await getChat(id!)
      readMarks.set(detail, mark)
      return detail
    },
    structuralSharing: false,
    enabled: id !== null,
  })
}

export function useCreateChat() {
  const qc = useQueryClient()
  return useMutation({ mutationFn: createChat, onSuccess: (chat) => storeChat(qc, chat) })
}

/**
 * Starting a chat and opening it: the one place both the chat list's New chat and the composer's note
 * start one from, so they create and select alike.
 *
 * @param onSelect - Called with the new chat's id once it is created.
 * @returns `start`, which ignores a press while one is being created; whether one is; and why the last one
 *   failed (null for none).
 */
export function useStartChat(onSelect: (id: string | null) => void): {
  start: () => void
  pending: boolean
  error: unknown
} {
  const create = useCreateChat()
  const { mutate, isPending, error } = create
  const start = () => {
    if (isPending) return
    mutate(undefined, { onSuccess: (chat) => onSelect(chat.id) })
  }
  return { start, pending: isPending, error }
}

export function useRenameChat() {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: ({ id, title }: { id: string; title: string }) => renameChat(id, title),
    onSuccess: (chat) => storeChat(qc, chat),
  })
}

export function useDeleteChat() {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: deleteChat,
    onSuccess: (_, id) => {
      qc.setQueryData<ChatOut[]>(chatsKey, (chats) => chats?.filter((c) => c.id !== id))
      qc.removeQueries({ queryKey: chatKey(id) })
    },
  })
}
