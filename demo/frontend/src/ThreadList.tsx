import { ThreadListItemPrimitive, ThreadListPrimitive } from "@assistant-ui/react"
import type { FC } from "react"

/**
 * A conversation switcher. Threads are held in memory by the runtime — new
 * conversations start with fresh dialogue but share the one VFS the server owns,
 * and everything resets on reload. No persistence, by design.
 */
const ThreadListItem: FC = () => (
  <ThreadListItemPrimitive.Root className="tl-item">
    <ThreadListItemPrimitive.Trigger className="tl-trigger">
      <ThreadListItemPrimitive.Title fallback="New chat" />
    </ThreadListItemPrimitive.Trigger>
  </ThreadListItemPrimitive.Root>
)

export const ThreadList: FC = () => (
  <div className="threadlist">
    <ThreadListPrimitive.New className="tl-new">＋ New chat</ThreadListPrimitive.New>
    <ThreadListPrimitive.Root className="tl-root">
      <ThreadListPrimitive.Items>{() => <ThreadListItem />}</ThreadListPrimitive.Items>
    </ThreadListPrimitive.Root>
  </div>
)
