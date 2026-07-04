import { useCallback, useEffect, useState } from "react"

type TreeResp = { prefix: string; paths: string[] }
type Version = { version_number: number; size: number }
type FileResp = { path: string; version_number: number | null; content: string; versions: Version[] }
type DiffResp = { path: string; older: number | null; newer: number | null; diff: string; note?: string }

async function getJSON<T>(url: string): Promise<T> {
  const resp = await fetch(url)
  if (!resp.ok) throw new Error(`${url} -> ${resp.status}`)
  return (await resp.json()) as T
}

function DiffView({ diff }: { diff: string }) {
  return (
    <pre className="diff">
      {diff.split("\n").map((line, i) => {
        const cls = line.startsWith("+") ? "add" : line.startsWith("-") ? "del" : line.startsWith("@@") ? "hunk" : ""
        return (
          // biome-ignore lint/suspicious/noArrayIndexKey: diff lines are positional and static
          <span key={i} className={`diff-line ${cls}`}>
            {line || " "}
          </span>
        )
      })}
    </pre>
  )
}

/**
 * A read-only view of the live VFS the agent is mutating.
 * Polls the tree so writes/deletes appear as you chat;
 * click a path to read its current version, or diff it against the prior one.
 */
export function VfsInspector() {
  const [paths, setPaths] = useState<string[]>([])
  const [selected, setSelected] = useState<string | null>(null)
  const [file, setFile] = useState<FileResp | null>(null)
  const [diff, setDiff] = useState<DiffResp | null>(null)
  const [error, setError] = useState<string | null>(null)

  const refreshTree = useCallback(async () => {
    try {
      setPaths((await getJSON<TreeResp>("/api/vfs/tree?prefix=/")).paths)
      setError(null)
    } catch (e) {
      setError(String(e))
    }
  }, [])

  useEffect(() => {
    refreshTree()
    const id = setInterval(refreshTree, 4000) // agent mutations show up without a manual reload
    return () => clearInterval(id)
  }, [refreshTree])

  const openFile = useCallback(async (path: string) => {
    setSelected(path)
    setDiff(null)
    setFile(await getJSON<FileResp>(`/api/vfs/file?path=${encodeURIComponent(path)}`))
  }, [])

  const showDiff = useCallback(async () => {
    if (!selected) return
    setDiff(await getJSON<DiffResp>(`/api/vfs/diff?path=${encodeURIComponent(selected)}`))
  }, [selected])

  return (
    <div className="inspector">
      <div className="inspector-head">
        <h2>VFS</h2>
        <button type="button" className="ghost" onClick={refreshTree}>
          refresh
        </button>
      </div>
      {error ? <p className="inspector-error">{error}</p> : null}
      <ul className="tree">
        {paths.map((p) => (
          <li key={p}>
            <button
              type="button"
              className={`tree-item ${p === selected ? "active" : ""}`}
              onClick={() => openFile(p)}
            >
              {p}
            </button>
          </li>
        ))}
      </ul>
      {file ? (
        <div className="file-view">
          <div className="file-head">
            <code>{file.path}</code>
            <span className="file-meta">
              v{file.version_number} · {file.versions.length} version{file.versions.length === 1 ? "" : "s"}
            </span>
            {file.versions.length > 1 ? (
              <button type="button" className="ghost" onClick={showDiff}>
                diff latest
              </button>
            ) : null}
          </div>
          {diff ? (
            diff.diff ? (
              <DiffView diff={diff.diff} />
            ) : (
              <p className="file-note">{diff.note ?? "no changes"}</p>
            )
          ) : (
            <pre className="file-body">{file.content}</pre>
          )}
        </div>
      ) : (
        <p className="file-note">Select a path to view its current version.</p>
      )}
    </div>
  )
}
