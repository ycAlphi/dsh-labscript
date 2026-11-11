/**
 * dsh-labscript -- browser half.
 *
 * A pre-built client bundle: DSH serves it as-is and expects the
 * window.__ModuleLoader__.load({ id, factory }) wrapper. It registers one small
 * configuration page into the official Plugins page (plugins.bundle.config).
 * The page only edits backend, python, runmanager host/port and protocolsDir;
 * everything else stays in the profile config. It talks to this plugin's own
 * routes under /dsh-labscript/api (GET/POST config, POST doctor).
 */
window.__ModuleLoader__.load({
  id: 'dsh-labscript',
  factory: (require) => {
    const module = { exports: {} }
    const exports = module.exports
    Object.defineProperty(exports, Symbol.toStringTag, { value: 'Module' })

    const React = require('react')
    const h = React.createElement
    const { useEffect, useState } = React

    const API = '/dsh-labscript/api'
    const STYLE_ID = 'dsh-labscript/plugin-config.css'

    const CSS = [
      '.lsc{max-width:720px;display:flex;flex-direction:column;gap:12px;font-size:13px;line-height:20px}',
      '.lsc-section{border:1px solid #d9d9d9;border-radius:8px;padding:12px;display:flex;flex-direction:column;gap:8px}',
      '.lsc-section h3{margin:0;font-size:14px;font-weight:600}',
      '.lsc-row{display:flex;flex-direction:column;gap:4px}',
      '.lsc label{font-weight:500}',
      '.lsc input,.lsc select{width:100%;box-sizing:border-box;padding:6px 8px;border-radius:6px;border:1px solid #d9d9d9;background:#fff;color:inherit;font:inherit}',
      '.lsc .hint{color:#8a8a8a;font-size:12px;line-height:18px}',
      '.lsc-actions{display:flex;gap:8px;flex-wrap:wrap;align-items:center}',
      '.lsc button{padding:5px 12px;border-radius:6px;border:1px solid #d9d9d9;background:#f5f5f5;color:inherit;cursor:pointer;font:inherit}',
      '.lsc button.primary{background:#2b6cb0;border-color:transparent;color:#fff}',
      '.lsc button:disabled{opacity:.55;cursor:not-allowed}',
      '.lsc-msg{padding:8px 10px;border-radius:6px;white-space:pre-wrap;font-size:12px;line-height:18px}',
      '.lsc-msg.ok{background:rgba(22,163,74,.1);color:#16a34a}',
      '.lsc-msg.err{background:rgba(220,38,38,.1);color:#dc2626}',
      '.lsc-check{display:flex;gap:8px;font-size:12px;line-height:18px}',
      '.lsc-check b{min-width:88px;font-weight:600}',
      '.lsc-check.ok b{color:#16a34a}',
      '.lsc-check.warn b{color:#ea580c}',
      '.lsc-check.fail b{color:#dc2626}',
    ].join(String.fromCharCode(10))

    function ensureStyle() {
      if (document.getElementById(STYLE_ID) !== null) return
      const style = document.createElement('style')
      style.id = STYLE_ID
      style.textContent = CSS
      document.head.appendChild(style)
    }

    async function apiFetch(path, options) {
      const response = await fetch(API + path, Object.assign({ headers: { 'Content-Type': 'application/json' } }, options || {}))
      let payload
      try {
        payload = await response.json()
      } catch (error) {
        throw new Error('接口返回了非 JSON 内容（HTTP ' + response.status + '）')
      }
      if (!response.ok || payload.ok === false) throw new Error(payload.error || ('请求失败（HTTP ' + response.status + '）'))
      return payload
    }

    function Row(props) {
      return h('div', { className: 'lsc-row' },
        h('label', null, props.label),
        props.children,
        props.hint === undefined ? null : h('span', { className: 'hint' }, props.hint),
      )
    }

    function ConfigPage() {
      const [loading, setLoading] = useState(true)
      const [configFile, setConfigFile] = useState('')
      const [version, setVersion] = useState('')
      const [backend, setBackend] = useState('mock-labscript')
      const [python, setPython] = useState('')
      const [host, setHost] = useState('127.0.0.1')
      const [port, setPort] = useState(42523)
      const [protocolsDir, setProtocolsDir] = useState('')
      const [busy, setBusy] = useState('')
      const [message, setMessage] = useState(null)
      const [checks, setChecks] = useState(null)

      const load = async () => {
        setLoading(true)
        try {
          const payload = await apiFetch('/config')
          setVersion(payload.version || '')
          setConfigFile(payload.configFile || '')
          const current = payload.config || {}
          setBackend(current.backend || 'mock-labscript')
          setPython(current.python || '')
          setHost((current.runmanager && current.runmanager.host) || '127.0.0.1')
          setPort((current.runmanager && current.runmanager.port) || 42523)
          setProtocolsDir(current.protocolsDir || '')
        } catch (error) {
          setMessage({ kind: 'err', text: error.message })
        } finally {
          setLoading(false)
        }
      }

      useEffect(() => {
        ensureStyle()
        load()
      }, [])

      const save = async () => {
        setBusy('save')
        setMessage(null)
        try {
          await apiFetch('/config', {
            method: 'POST',
            body: JSON.stringify({
              backend,
              python,
              runmanager: { host, port: Number(port) },
              protocolsDir,
            }),
          })
          setMessage({ kind: 'ok', text: '已保存；下一次工具调用生效，不需要重启。' })
        } catch (error) {
          setMessage({ kind: 'err', text: error.message })
        } finally {
          setBusy('')
        }
      }

      const doctor = async () => {
        setBusy('doctor')
        setMessage(null)
        setChecks(null)
        try {
          const payload = await apiFetch('/doctor', { method: 'POST', body: '{}' })
          setChecks(payload.checks || [])
        } catch (error) {
          setMessage({ kind: 'err', text: error.message })
        } finally {
          setBusy('')
        }
      }

      const checkRows = (checks || []).map((check, index) => h('div', { key: index, className: 'lsc-check ' + check.status },
        h('b', null, check.status),
        h('span', null, check.name + ': ' + check.detail + (check.hint ? '  -- ' + check.hint : '')),
      ))

      return h('div', { className: 'lsc' },
        h('div', null, h('h3', null, 'dsh-labscript'), h('span', { className: 'hint' }, 'v' + version + (configFile ? '  ·  ' + configFile : ''))),
        h('section', { className: 'lsc-section' },
          h('h3', null, '连接'),
          h(Row, { label: 'backend' },
            h('select', { value: backend, onChange: (event) => setBackend(event.target.value) },
              h('option', { value: 'mock-labscript' }, 'mock-labscript（模拟器）'),
              h('option', { value: 'labscript' }, 'labscript（真实硬件）'),
            ),
          ),
          h(Row, { label: 'python', hint: '真机填 labscript-suite 环境的 python；模拟器只需 numpy+h5py。' }, h('input', { type: 'text', value: python, onChange: (event) => setPython(event.target.value) })),
          h(Row, { label: 'runmanager host' }, h('input', { type: 'text', value: host, onChange: (event) => setHost(event.target.value) })),
          h(Row, { label: 'runmanager port' }, h('input', { type: 'number', value: port, onChange: (event) => setPort(event.target.value) })),
        ),
        h('section', { className: 'lsc-section' },
          h('h3', null, '实验'),
          h(Row, { label: 'protocolsDir', hint: '协议 YAML 目录；留空则协议列表为空。' }, h('input', { type: 'text', value: protocolsDir, onChange: (event) => setProtocolsDir(event.target.value) })),
          h('span', { className: 'hint' }, 'scriptsFile、hard limits、postprocess、approval、pathMap 等高级配置仍写在 profile 的 cordis.patch.yml。'),
        ),
        h('div', { className: 'lsc-actions' },
          h('button', { className: 'primary', disabled: loading || busy !== '', onClick: save }, busy === 'save' ? '保存中…' : '保存'),
          h('button', { disabled: loading || busy !== '', onClick: doctor }, busy === 'doctor' ? '诊断中…' : '运行诊断'),
        ),
        message === null ? null : h('div', { className: 'lsc-msg ' + message.kind }, message.text),
        checks === null ? null : h('div', { className: 'lsc-section' }, h('h3', null, '诊断结果'), checkRows),
      )
    }

    function apply(ctx) {
      ctx.effect(
        () => ctx.slots.inject('plugins.bundle.config', () =>
          ctx.slots.register({ name: 'plugins.bundle.config', key: 'dsh-labscript' }, ConfigPage)),
        'dsh-labscript: plugin configuration page',
      )
    }

    exports.apply = apply
    exports.inject = ['slots']
    exports.ConfigPage = ConfigPage
    return module.exports
  },
})
