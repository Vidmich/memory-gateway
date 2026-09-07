import { describe, expect, it } from 'vitest'

import { MAX_DEPTH, filesFrom } from '@/pages/dropFiles'

/**
 * The entry API exists only in a browser, and jsdom has none of it — so these fakes are
 * the API, shaped exactly as the spec describes it.
 *
 * Two of its details are the whole reason this module exists, and both are asserted:
 * `DataTransfer.files` is empty for a dropped *folder*, and `readEntries` returns a
 * *batch* rather than a directory, signalling the end with an empty one.
 */

function fileEntry(name: string): FileSystemEntry {
  return {
    isFile: true,
    isDirectory: false,
    name,
    file: (resolve: (file: File) => void) => resolve(new File([name], name)),
  } as unknown as FileSystemEntry
}

function directoryEntry(name: string, children: FileSystemEntry[]): FileSystemEntry {
  return {
    isFile: false,
    isDirectory: true,
    name,
    createReader: () => {
      // One batch, then an empty one — which is how the real API says "that is all".
      let served = false
      return {
        readEntries: (resolve: (entries: FileSystemEntry[]) => void) => {
          resolve(served ? [] : children)
          served = true
        },
      }
    },
  } as unknown as FileSystemEntry
}

function transfer(options: {
  entries?: (FileSystemEntry | null)[]
  files?: File[]
}): DataTransfer {
  const entries = options.entries ?? []
  return {
    items: entries.map((entry) => ({ kind: 'file', webkitGetAsEntry: () => entry })),
    files: options.files ?? [],
  } as unknown as DataTransfer
}

describe('filesFrom', () => {
  it('reads a plain drop of files', async () => {
    const files = await filesFrom(
      transfer({ entries: [fileEntry('a.md'), fileEntry('b.md')] }),
    )

    expect(files.map((file) => file.name)).toEqual(['a.md', 'b.md'])
  })

  it('reads the contents of a dropped folder', async () => {
    // `DataTransfer.files` is *empty* for a folder, so a zone that only read it would
    // silently do nothing — the first thing anybody tries on this screen.
    const files = await filesFrom(
      transfer({
        entries: [directoryEntry('docs', [fileEntry('one.md'), fileEntry('two.md')])],
        files: [],
      }),
    )

    expect(files.map((file) => file.name)).toEqual(['one.md', 'two.md'])
  })

  it('descends through nested folders', async () => {
    const files = await filesFrom(
      transfer({
        entries: [
          directoryEntry('docs', [
            fileEntry('index.md'),
            directoryEntry('api', [fileEntry('auth.md')]),
          ]),
        ],
      }),
    )

    expect(files.map((file) => file.name)).toEqual(['index.md', 'auth.md'])
  })

  it('stops descending at a depth limit', async () => {
    // A symlink loop is a hang with no error, which is worse than a missed file.
    let deepest: FileSystemEntry = fileEntry('bottom.md')
    for (let level = 0; level <= MAX_DEPTH + 2; level += 1) {
      deepest = directoryEntry(`level${level}`, [deepest])
    }

    const files = await filesFrom(transfer({ entries: [deepest] }))

    expect(files).toEqual([])
  })

  it('falls back to the flat list when the browser offers no entries', async () => {
    // Firefox before the entry API, and anything driving the page programmatically.
    const files = await filesFrom(transfer({ entries: [], files: [new File(['x'], 'x.md')] }))

    expect(files.map((file) => file.name)).toEqual(['x.md'])
  })

  it('falls back when the entries turn out to hold nothing', async () => {
    const files = await filesFrom(
      transfer({ entries: [directoryEntry('empty', [])], files: [new File(['x'], 'x.md')] }),
    )

    expect(files.map((file) => file.name)).toEqual(['x.md'])
  })

  it('ignores a drop that is not a file at all', async () => {
    const dragged = {
      items: [{ kind: 'string', webkitGetAsEntry: () => null }],
      files: [],
    } as unknown as DataTransfer

    expect(await filesFrom(dragged)).toEqual([])
  })
})
