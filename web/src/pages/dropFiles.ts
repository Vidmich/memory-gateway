/**
 * Reading a drag-and-drop, including the contents of a dropped *folder*.
 *
 * `DataTransfer.files` is flat, and for a folder it is empty — the entry API is the only
 * way to see inside one. Dragging a folder of documents onto a connector is the single
 * most likely thing anybody does on that screen, so a zone that silently ignored it would
 * be the first thing they tried and the first thing that appeared not to work.
 *
 * Its own module rather than a helper beside the component, because the entry API exists
 * only in a browser and is worth testing with a fake rather than a real drop.
 */

/** How deep a dropped folder is walked. Deep enough for any real documentation tree, and
 *  a bound rather than a promise, because a symlink loop is a hang with no error. */
export const MAX_DEPTH = 8

export async function filesFrom(transfer: DataTransfer): Promise<File[]> {
  const entries = entriesOf(transfer)
  if (entries.length === 0) return Array.from(transfer.files ?? [])

  const files: File[] = []
  for (const entry of entries) await walk(entry, files, 0)
  // A browser that gave us entries but no files — or a drop of something that is not a
  // file at all — still falls back to the flat list rather than to nothing.
  return files.length > 0 ? files : Array.from(transfer.files ?? [])
}

function entriesOf(transfer: DataTransfer): FileSystemEntry[] {
  const found: FileSystemEntry[] = []
  for (const item of Array.from(transfer.items ?? [])) {
    if (item.kind !== 'file') continue
    // Called through the item so `this` is the item, which is what the API requires.
    const entry = 'webkitGetAsEntry' in item ? item.webkitGetAsEntry() : null
    if (entry) found.push(entry)
  }
  return found
}

async function walk(entry: FileSystemEntry, into: File[], depth: number): Promise<void> {
  if (entry.isFile) {
    const file = await new Promise<File | null>((resolve) => {
      ;(entry as FileSystemFileEntry).file(resolve, () => resolve(null))
    })
    if (file) into.push(file)
    return
  }
  if (!entry.isDirectory || depth >= MAX_DEPTH) return

  const reader = (entry as FileSystemDirectoryEntry).createReader()
  // `readEntries` returns a *batch*, not the whole directory, and signals the end with an
  // empty one. Reading it once is the bug that silently drops everything past the first
  // hundred files in a large folder.
  for (;;) {
    const batch = await new Promise<FileSystemEntry[]>((resolve) => {
      reader.readEntries(resolve, () => resolve([]))
    })
    if (batch.length === 0) return
    for (const child of batch) await walk(child, into, depth + 1)
  }
}
