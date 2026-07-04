import { AssistantRuntimeProvider } from "@assistant-ui/react"
import { useChatRuntime } from "@assistant-ui/react-ai-sdk"
import { DefaultChatTransport } from "ai"
import { useMemo } from "react"

import { Thread } from "./Thread"
import { ThreadList } from "./ThreadList"
import { VfsInspector } from "./VfsInspector"

export function App() {
  // DefaultChatTransport (a plain AI SDK transport) instead of the assistant-ui
  // default: this is plain server-side-tool chat, so we deliberately do NOT
  // forward frontend tool definitions or client system prompts to the backend.
  const runtime = useChatRuntime({
    transport: useMemo(() => new DefaultChatTransport({ api: "/api/chat" }), []),
  })

  return (
    <AssistantRuntimeProvider runtime={runtime}>
      <div className="layout">
        <header className="topbar">
          <span className="brand">ai-vfs</span>
          <span className="tagline">an agent whose only filesystem is the VFS</span>
        </header>
        <main className="panes">
          <nav className="pane threads-pane">
            <ThreadList />
          </nav>
          <section className="pane chat-pane">
            <Thread />
          </section>
          <aside className="pane inspect-pane">
            <VfsInspector />
          </aside>
        </main>
      </div>
    </AssistantRuntimeProvider>
  )
}
