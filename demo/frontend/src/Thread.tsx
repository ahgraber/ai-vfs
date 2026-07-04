import {
  ComposerPrimitive,
  MessagePartPrimitive,
  MessagePrimitive,
  ThreadPrimitive,
  type ToolCallMessagePartProps,
} from "@assistant-ui/react"
import type { FC } from "react"

/** Compact card for any tool call — the demo's payload is watching these fire. */
const ToolFallback: FC<ToolCallMessagePartProps> = ({ toolName, argsText, result, status }) => {
  const running = status?.type === "running"
  return (
    <div className={`tool ${running ? "tool-running" : ""}`}>
      <div className="tool-head">
        <span className="tool-icon">{running ? "⏳" : "🔧"}</span>
        <code>{toolName}</code>
      </div>
      {argsText ? <pre className="tool-args">{argsText}</pre> : null}
      {result !== undefined ? (
        <pre className="tool-result">{typeof result === "string" ? result : JSON.stringify(result, null, 2)}</pre>
      ) : null}
    </div>
  )
}

const UserMessage: FC = () => (
  <MessagePrimitive.Root className="msg msg-user">
    <div className="bubble">
      <MessagePrimitive.Parts />
    </div>
  </MessagePrimitive.Root>
)

const AssistantMessage: FC = () => (
  <MessagePrimitive.Root className="msg msg-assistant">
    <div className="bubble">
      <MessagePrimitive.Parts
        components={{
          Text: () => <MessagePartPrimitive.Text />,
          tools: { Fallback: ToolFallback },
        }}
      />
    </div>
  </MessagePrimitive.Root>
)

const Composer: FC = () => (
  <ComposerPrimitive.Root className="composer">
    <ComposerPrimitive.Input
      className="composer-input"
      placeholder="Ask the agent to explore or edit the VFS…"
      autoFocus
    />
    <ComposerPrimitive.Send className="composer-send">Send</ComposerPrimitive.Send>
  </ComposerPrimitive.Root>
)

export const Thread: FC = () => (
  <ThreadPrimitive.Root className="thread">
    <ThreadPrimitive.Viewport className="thread-viewport">
      <ThreadPrimitive.Empty>
        <div className="thread-empty">
          <p>Try: "List everything under /specs, then summarize the session spec."</p>
          <p>Or: "Use run_python to read /NORTH-STAR.md and count its lines."</p>
        </div>
      </ThreadPrimitive.Empty>
      <ThreadPrimitive.Messages components={{ UserMessage, AssistantMessage }} />
    </ThreadPrimitive.Viewport>
    <Composer />
  </ThreadPrimitive.Root>
)
