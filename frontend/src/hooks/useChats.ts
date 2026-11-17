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

export function useChat(id: string | null) {
  return useQuery({ queryKey: chatKey(id), queryFn: () => getChat(id!), enabled: id !== null })
}

export function useCreateChat() {
  const qc = useQueryClient()
  return useMutation({ mutationFn: createChat, onSuccess: (chat) => storeChat(qc, chat) })
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
