#!/usr/bin/env node
/**
 * Rewrite the absolute `cwd` recorded in a DSH session log header so a session
 * copied from another machine can be resumed on this one.
 *
 * UNSUPPORTED: this edits the durable artifact directly. The shipped harness
 * offers no API to re-home a session. Do it with the DSH host on this machine
 * STOPPED, and keep the generated .bak file.
 *
 * Usage:
 *   node migrate-session-cwd.mjs <log-file> <new-absolute-cwd> [--move] [--dry-run]
 *
 *   <log-file>          .../sessions/--<project>--/<session-id>/session.jsonl.zstd
 *                       (or session.jsonl for a compression:'none' root)
 *   <new-absolute-cwd>  the directory the session must run in on THIS machine
 *   --move              also relocate the session directory into the project
 *                       directory matching the new cwd (recommended)
 *   --dry-run           report what would change without writing anything
 */

import { readFileSync, writeFileSync, renameSync, copyFileSync, existsSync, mkdirSync, statSync } from 'node:fs'
import { constants, zstdCompressSync, zstdDecompressSync } from 'node:zlib'
import { basename, dirname, isAbsolute, join, resolve } from 'node:path'

const ZSTD_MAGIC = 0xfd2fb528

/** Structurally complete frame ranges in a concatenated Zstandard stream. */
function frameRanges(buf) {
  const frames = []
  let offset = 0
  while (offset < buf.length) {
    const start = offset
    if (buf.length - offset < 4) throw new Error(`torn frame at byte ${offset}`)
    if (buf.readUInt32LE(offset) !== ZSTD_MAGIC) throw new Error(`invalid frame magic at byte ${offset}`)
    offset += 4
    if (offset === buf.length) throw new Error(`torn frame header at byte ${start}`)
    const descriptor = buf.readUInt8(offset)
    offset += 1
    if ((descriptor & 0x18) !== 0) throw new Error(`reserved frame-header bit at byte ${offset - 1}`)
    const contentSizeFlag = descriptor >>> 6
    const singleSegment = (descriptor & 0x20) !== 0
    const checksum = (descriptor & 0x04) !== 0
    const dictionaryFlag = descriptor & 0x03
    const dictionaryBytes = dictionaryFlag === 3 ? 4 : dictionaryFlag
    const contentSizeBytes = contentSizeFlag === 0 ? (singleSegment ? 1 : 0) : 1 << contentSizeFlag
    const remaining = (singleSegment ? 0 : 1) + dictionaryBytes + contentSizeBytes
    if (buf.length - offset < remaining) throw new Error(`torn frame header at byte ${start}`)
    offset += remaining
    for (;;) {
      if (buf.length - offset < 3) throw new Error(`torn block header at byte ${offset}`)
      const blockHeader = buf.readUIntLE(offset, 3)
      offset += 3
      const lastBlock = (blockHeader & 1) !== 0
      const blockType = (blockHeader >>> 1) & 0x03
      const blockSize = blockHeader >>> 3
      if (blockType === 0x03) throw new Error(`reserved block type at byte ${offset - 3}`)
      const payloadBytes = blockType === 0x01 ? 1 : blockSize
      if (buf.length - offset < payloadBytes) throw new Error(`torn block at byte ${offset}`)
      offset += payloadBytes
      if (lastBlock) break
    }
    if (checksum) {
      if (buf.length - offset < 4) throw new Error(`torn checksum at byte ${offset}`)
      offset += 4
    }
    frames.push({ start, end: offset })
  }
  return frames
}

/** One filesystem-safe path segment, identical to the backend's encodeSegment. */
function encodeSegment(raw) {
  if (raw.length === 0) throw new Error('cannot encode an empty path segment')
  if (raw === '.') return '~002E'
  if (raw === '..') return '~002E~002E'
  let out = ''
  for (let i = 0; i < raw.length; i++) {
    const code = raw.charCodeAt(i)
    const ch = String.fromCharCode(code)
    if (ch !== '~' && /^[A-Za-z0-9._-]$/.test(ch)) out += ch
    else out += '~' + code.toString(16).toUpperCase().padStart(4, '0')
  }
  return out
}

/** Readable project directory name, identical to the backend's projectKey. */
function projectKey(cwd) {
  if (cwd.length === 0) throw new Error('cannot encode an empty project path')
  let readable = ''
  let separatorRun = false
  for (let i = 0; i < cwd.length; i++) {
    const code = cwd.charCodeAt(i)
    const ch = String.fromCharCode(code)
    if (ch === '/' || ch === '\\' || ch === ':') {
      if (!separatorRun) readable += '-'
      separatorRun = true
    } else if (ch !== '~' && /^[A-Za-z0-9._-]$/.test(ch)) {
      readable += ch
      separatorRun = false
    } else {
      readable += '~' + code.toString(16).toUpperCase().padStart(4, '0')
      separatorRun = false
    }
  }
  const slug = readable.replace(/^-+/, '') || 'root'
  return `--${slug.slice(0, 251)}--`
}

const [logArg, cwdArg, ...flags] = process.argv.slice(2)
if (!logArg || !cwdArg) {
  console.error('usage: node migrate-session-cwd.mjs <log-file> <new-absolute-cwd> [--move] [--dry-run]')
  process.exit(2)
}
const move = flags.includes('--move')
const dryRun = flags.includes('--dry-run')

const logFile = resolve(logArg)
if (!existsSync(logFile)) throw new Error(`no such log file: ${logFile}`)
if (!isAbsolute(cwdArg)) throw new Error(`new cwd must be absolute: ${cwdArg}`)
if (!existsSync(cwdArg) || !statSync(cwdArg).isDirectory()) {
  throw new Error(`new cwd must be an existing directory on this machine: ${cwdArg}`)
}

const isZstd = logFile.endsWith('.jsonl.zstd')
const original = readFileSync(logFile)
let headerText
let rebuild

if (isZstd) {
  const frames = frameRanges(original)
  if (frames.length === 0) throw new Error('no complete Zstandard frame found')
  const first = frames[0]
  headerText = zstdDecompressSync(original.subarray(first.start, first.end)).toString('utf8')
  rebuild = (newHeaderText) => {
    const frame = zstdCompressSync(Buffer.from(newHeaderText, 'utf8'), {
      params: { [constants.ZSTD_c_checksumFlag]: 1 },
    })
    return Buffer.concat([frame, original.subarray(first.end)])
  }
} else {
  const newline = original.indexOf(0x0a)
  if (newline < 0) throw new Error('log has no complete header line')
  headerText = original.subarray(0, newline + 1).toString('utf8')
  rebuild = (newHeaderText) => Buffer.concat([Buffer.from(newHeaderText, 'utf8'), original.subarray(newline + 1)])
}

const header = JSON.parse(headerText)
if (header?.type !== 'session' || typeof header.id !== 'string' || header.id === '') {
  throw new Error('first record is not a session header line')
}
const sessionDir = dirname(logFile)
if (encodeSegment(header.id) !== basename(sessionDir)) {
  throw new Error(`header id "${header.id}" does not match its directory "${basename(sessionDir)}"`)
}
const oldCwd = header.cwd
header.cwd = cwdArg
const newHeaderText = JSON.stringify(header) + '\n'

const projectDirName = projectKey(cwdArg)
const rootDir = dirname(dirname(sessionDir))
const targetDir = join(rootDir, projectDirName, encodeSegment(header.id))
const needsMove = resolve(sessionDir) !== resolve(targetDir)

console.log(`log:        ${logFile}`)
console.log(`encoding:   ${isZstd ? 'zstd frames' : 'plain jsonl'}`)
console.log(`session id: ${header.id}`)
console.log(`cwd:        ${oldCwd ?? '(none)'}  ->  ${cwdArg}`)
console.log(`project:    ${basename(dirname(sessionDir))}  ->  ${projectDirName}`)
console.log(`target dir: ${targetDir}${needsMove ? '  (move needed)' : '  (already in place)'}`)

if (dryRun) {
  console.log('dry run: nothing written')
  process.exit(0)
}

copyFileSync(logFile, `${logFile}.bak`)
writeFileSync(`${logFile}.tmp`, rebuild(newHeaderText))
renameSync(`${logFile}.tmp`, logFile)
console.log(`rewrote header; backup at ${logFile}.bak`)

if (needsMove && move) {
  mkdirSync(dirname(targetDir), { recursive: true })
  if (existsSync(targetDir)) throw new Error(`target session directory already exists: ${targetDir}`)
  renameSync(sessionDir, targetDir)
  console.log(`moved session dir to ${targetDir}`)
} else if (needsMove) {
  console.log('NOTE: the session directory still sits under the OLD project directory.')
  console.log('      Appends are written to the NEW one, so move it (re-run with --move)')
  console.log('      or the same id will exist in two project directories.')
}
