/**
 * screen.js — Display screen logic
 *
 * Connects to /ws as client_type="screen".
 * On "play" message: updates <video> src and plays.
 * On "stop" message: clears video, shows idle state.
 * On "queue_update": updates the "up next" overlay.
 */

const WS_RECONNECT_MS = 3000

function initScreen() {
  return {
    // State
    connected: false,
    nowPlaying: null,
    upNext: null,
    queue: [],
    error: null,

    // Pitch shifting
    semitones: 0,
    _baseStreamUrl: null,   // stream_url from server, without semitones param

    // DOM refs set in init()
    video: null,
    ws: null,

    init() {
      this.video = this.$refs.video
      this._applyTheme()
      this._connect()

      // Autoplay unlock: some browsers block autoplay until user gesture
      document.addEventListener('click', () => {
        if (this.video && this.video.paused && this.nowPlaying) {
          this.video.play().catch(() => {})
        }
      }, { once: true })
    },

    _applyTheme() {
      const prefersDark = window.matchMedia('(prefers-color-scheme: dark)').matches
      document.documentElement.classList.toggle('dark', prefersDark)
    },

    _connect() {
      const proto = location.protocol === 'https:' ? 'wss' : 'ws'
      this.ws = new WebSocket(`${proto}://${location.host}/ws`)

      this.ws.onopen = () => {
        this.connected = true
        this.error = null
        this.ws.send(JSON.stringify({ client_type: 'screen', name: 'display' }))
      }

      this.ws.onmessage = (ev) => {
        try {
          this._handle(JSON.parse(ev.data))
        } catch (e) {
          console.error('WS parse error', e)
        }
      }

      this.ws.onerror = () => {
        this.connected = false
      }

      this.ws.onclose = () => {
        this.connected = false
        setTimeout(() => this._connect(), WS_RECONNECT_MS)
      }

      // Keep-alive ping every 30s
      this._pingInterval = setInterval(() => {
        if (this.ws.readyState === WebSocket.OPEN) {
          this.ws.send(JSON.stringify({ type: 'ping' }))
        }
      }, 30000)
    },

    _handle(msg) {
      switch (msg.type) {
        case 'state':
          this.queue = msg.queue || []
          this.upNext = this.queue[0] || null
          if (msg.now_playing) {
            this._startPlayback(msg.now_playing)
          }
          break

        case 'play':
          this.nowPlaying = msg
          this._startPlayback(msg)
          break

        case 'queue_update':
          this.queue = msg.queue || []
          this.nowPlaying = msg.now_playing || this.nowPlaying
          // upNext is first item that isn't now-playing
          this.upNext = this.queue[0] || null
          break

        case 'stop':
          this.nowPlaying = null
          this.upNext = this.queue[0] || null
          this._stopPlayback()
          break
      }
    },

    _pitchedUrl() {
      if (!this._baseStreamUrl) return null
      return this.semitones === 0
        ? this._baseStreamUrl
        : `${this._baseStreamUrl}?semitones=${this.semitones}`
    },

    _startPlayback(msg) {
      this._baseStreamUrl = msg.stream_url
      if (!this._baseStreamUrl) return
      this._loadVideo(this._pitchedUrl())
    },

    _loadVideo(url) {
      this.video.pause()
      this.video.src = url
      this.video.load()
      this.video.play().catch(err => {
        console.warn('Autoplay blocked:', err)
        this.error = 'Tap anywhere to enable audio playback'
      })
    },

    adjustPitch(delta) {
      const next = Math.max(-12, Math.min(12, this.semitones + delta))
      if (next === this.semitones) return
      this.semitones = next
      // Reconnect to a new stream at the new pitch (restarts from beginning)
      if (this._baseStreamUrl) {
        this._loadVideo(this._pitchedUrl())
      }
    },

    resetPitch() {
      this.adjustPitch(-this.semitones)
    },

    _stopPlayback() {
      this._baseStreamUrl = null
      this.video.pause()
      this.video.removeAttribute('src')
      this.video.load()
    },

    clearError() {
      this.error = null
      if (this.video && this.nowPlaying) {
        this.video.play().catch(() => {})
      }
    },
  }
}

export { initScreen }
