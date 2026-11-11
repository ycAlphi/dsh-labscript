/**
 * dsh-labscript -- host half.
 *
 * Registers the labs_* tools, the runtime skill, and a small HTTP API used by
 * the browser configuration page. The browser half is optional: the tools work
 * without it, and the page only edits the four connection settings (backend,
 * python, runmanager host/port, protocolsDir).
 */
import fs from 'node:fs'
import { createRuntime, LabError } from './runtime.js'
import { registerTools } from './tools.js'
import { registerSkill } from './skill.js'
import { writeUiConfig } from './store.mjs'

export const name = 'dsh-labscript'
export const inject = ['tools', 'skills']

const PACKAGE = JSON.parse(fs.readFileSync(new URL('../package.json', import.meta.url), 'utf8'))
const API_PREFIX = '/dsh-labscript/api'
const MAX_BODY_BYTES = 64 * 1024

function sendJson(response, status, body) {
  response.writeHead(status, { 'Content-Type': 'application/json; charset=utf-8' })
  response.end(JSON.stringify(body))
}

function readJsonBody(request) {
  return new Promise((resolve, reject) => {
    const chunks = []
    let size = 0
    request.on('data', (chunk) => {
      size += chunk.length
      if (size > MAX_BODY_BYTES) {
        reject(new Error('request body too large'))
        request.destroy()
        return
      }
      chunks.push(chunk)
    })
    request.on('end', () => {
      try {
        resolve(chunks.length === 0 ? {} : JSON.parse(Buffer.concat(chunks).toString('utf8')))
      } catch (error) {
        reject(new Error('request body is not JSON'))
      }
    })
    request.on('error', reject)
  })
}

function isTrustedRequest(ctx, request) {
  let connection
  try {
    connection = typeof ctx.get === 'function' ? ctx.get('connection') : undefined
  } catch {
    connection = undefined
  }
  if (connection === undefined) return false
  try {
    if (typeof connection.isTrusted === 'function' && connection.isTrusted(request) === true) return true
    if (typeof connection.requestRejection === 'function') {
      const code = connection.requestRejection(request)
      return code === undefined || code === null || code === false
    }
  } catch {
    /* fall through to reject */
  }
  return false
}

function registerRoutes(ctx, runtime) {
  const webServer = typeof ctx.get === 'function' ? ctx.get('webServer') : ctx.webServer
  if (!webServer || typeof webServer.register !== 'function') return
  const handler = async (request, response) => {
    const url = new URL(request.url || '/', 'http://127.0.0.1')
    if (!url.pathname.startsWith(API_PREFIX)) {
      sendJson(response, 404, { ok: false, error: 'not found' })
      return
    }
    if (!isTrustedRequest(ctx, request)) {
      sendJson(response, 403, { ok: false, error: 'untrusted request origin' })
      return
    }
    const route = url.pathname.slice(API_PREFIX.length).replace(/\/+$/, '') || '/'
    try {
      if (route === '/config' && request.method === 'GET') {
        const config = runtime.config
        const doctorInfo = { configFile: runtime.uiConfigPath(), stateFile: runtime.statePath(), python: config.python }
        sendJson(response, 200, {
          ok: true,
          version: runtime.version,
          packageName: PACKAGE.name,
          ...doctorInfo,
          config: {
            backend: config.backend,
            python: config.python,
            runmanager: config.runmanager,
            protocolsDir: config.protocolsDir,
          },
        })
        return
      }
      if (route === '/config' && request.method === 'POST') {
        const body = await readJsonBody(request)
        const stored = writeUiConfig(body, runtime.uiHome)
        sendJson(response, 200, { ok: true, config: stored })
        return
      }
      if (route === '/doctor' && request.method === 'POST') {
        const result = await runtime.doctor()
        sendJson(response, 200, { ok: true, ...result })
        return
      }
      sendJson(response, 404, { ok: false, error: 'not found' })
    } catch (error) {
      sendJson(response, 400, { ok: false, error: error && error.message ? error.message : String(error) })
    }
  }
  ctx.effect(() => webServer.register({ kind: 'prefix', path: API_PREFIX, handler }))
}

export function apply(ctx, config) {
  const runtime = createRuntime(ctx, config)
  registerTools(ctx, runtime)
  registerSkill(ctx)
  registerRoutes(ctx, runtime)
  const logger = ctx.logger
  if (logger && typeof logger.info === 'function') {
    const current = runtime.config
    logger.info('[dsh-labscript] v' + runtime.version + ' backend=' + current.backend + ' python=' + (current.python || '<unset>'))
    if (current.backend === 'labscript' && !current.python) {
      logger.warn('[dsh-labscript] backend is labscript but config.python is empty; every hardware tool will fail')
    }
  }
  return runtime
}

export { LabError }
