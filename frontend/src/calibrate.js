/**
 * calibrate.js — A/V sync remote.
 *
 * Streams a flash + beep test pattern to every connected screen and
 * lets the operator nudge the global av_offset_ms while watching a real
 * screen. Browser-side auto-measurement of A/V latency is unreliable
 * with MSE audio tracks (createMediaElementSource / captureStream both
 * silently fail to deliver samples on Chrome), so this page is a plain
 * remote control — the operator's eyes and ears are the measurement.
 */

const POLL_MS = 1000

function initCalibrate() {
  return {
    darkMode: false,
    settingsOpen: false,

    testing: false,
    avOffsetMs: 0,
    busy: false,
    error: null,

    _pollTimer: null,

    async init() {
      this.darkMode = localStorage.getItem('sk-theme') === 'dark' ||
        (!localStorage.getItem('sk-theme') && window.matchMedia('(prefers-color-scheme: dark)').matches)
      this._applyTheme()
      await this._refresh()
      this._pollTimer = setInterval(() => this._refresh(), POLL_MS)
      window.addEventListener('beforeunload', () => {
        clearInterval(this._pollTimer)
      })
    },

    toggleTheme() {
      this.darkMode = !this.darkMode
      localStorage.setItem('sk-theme', this.darkMode ? 'dark' : 'light')
      this._applyTheme()
    },

    _applyTheme() {
      document.documentElement.classList.toggle('dark', this.darkMode)
    },

    async _refresh() {
      try {
        const res = await fetch('/api/calibrate/state')
        if (!res.ok) return
        const d = await res.json()
        this.avOffsetMs = d.av_offset_ms ?? 0
        this.testing   = !!d.testing
      } catch (_) { /* ignore transient */ }
    },

    async start() {
      this.busy = true
      this.error = null
      try {
        const res = await fetch('/api/calibrate/start', { method: 'POST' })
        if (!res.ok) throw new Error(`HTTP ${res.status}`)
      } catch (e) {
        this.error = 'Failed to start test: ' + (e.message || e)
      } finally {
        this.busy = false
        await this._refresh()
      }
    },

    async stop() {
      this.busy = true
      this.error = null
      try {
        const res = await fetch('/api/calibrate/stop', { method: 'POST' })
        if (!res.ok) throw new Error(`HTTP ${res.status}`)
      } catch (e) {
        this.error = 'Failed to stop test: ' + (e.message || e)
      } finally {
        this.busy = false
        await this._refresh()
      }
    },

    async nudge(deltaMs) {
      const next = Math.max(-2000, Math.min(2000, (this.avOffsetMs || 0) + deltaMs))
      if (next === this.avOffsetMs) return
      this.avOffsetMs = next
      await this._apply(next)
    },

    async reset() {
      if (this.avOffsetMs === 0) return
      this.avOffsetMs = 0
      await this._apply(0)
    },

    async _apply(offsetMs) {
      this.error = null
      try {
        const res = await fetch('/api/calibrate/apply', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ offset_ms: offsetMs }),
        })
        if (!res.ok) throw new Error(`HTTP ${res.status}`)
      } catch (e) {
        this.error = 'Failed to apply offset: ' + (e.message || e)
      }
    },
  }
}

export { initCalibrate }
