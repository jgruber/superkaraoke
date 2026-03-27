/**
 * screen.js — Display screen logic
 *
 * Connects to /ws as client_type="screen".
 * On "play" message: updates <video> src and plays.
 * On "stop" message: clears video, shows idle state.
 * On "queue_update": updates the "up next" overlay.
 */
import QRCode from 'qrcode'

const WS_RECONNECT_MS = 3000

function initScreen() {
  return {
    // State
    connected: false,
    nowPlaying: null,
    upNext: null,
    queue: [],
    error: null,

    // Set true when this screen connects while another song is already playing.
    // The screen shows a "waiting" overlay and only starts video on the next 'play'.
    waitingForNext: false,
    currentlyPlaying: null,  // display-only: what other screens are playing right now

    // Pitch shifting
    semitones: 0,
    _baseStreamUrl: null,   // stream_url from server, without semitones param

    // Sync / buffering
    _serverTs: 0,           // server Unix timestamp when song started
    _playAt: 0,             // Unix timestamp (ms) when all screens should start playing
    _playTimer: null,       // setTimeout handle for deferred play

    // Playback progress (0–1) for the progress bar
    playProgress: 0,
    _duration: 0,       // known song duration in seconds (from server play message)

    // Autoplay unlock state — true until the user taps the screen
    needsGesture: true,
    _pendingPlay: false,    // true if play was attempted while gesture not yet given

    // DOM refs set in init()
    video: null,
    ws: null,

    async init() {
      this.video = this.$refs.video
      this._applyTheme()
      this._connect()
      await this._renderQR()
    },

    _applyTheme() {
      const prefersDark = window.matchMedia('(prefers-color-scheme: dark)').matches
      document.documentElement.classList.toggle('dark', prefersDark)
    },

    async _renderQR() {
      const canvas = this.$refs.qrCanvas
      if (!canvas) return
      // Use origin so the QR always points to the root of the app (reverse-proxy friendly)
      const url = location.origin
      try {
        await QRCode.toCanvas(canvas, url, {
          width: 180,
          margin: 2,
          color: { dark: '#ffffff', light: '#00000000' },
        })
      } catch (e) {
        console.warn('QR render failed:', e)
      }
    },

    _connect() {
      const proto = location.protocol === 'https:' ? 'wss' : 'ws'
      this.ws = new WebSocket(`${proto}://${location.host}/ws`)

      this.ws.onopen = () => {
        this.connected = true
        this.error = null
        try {
          this.ws.send(JSON.stringify({ client_type: 'screen', name: 'display' }))
        } catch (e) {
          console.warn('WS send failed on open:', e)
        }
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
          // Initial state on connect — update queue display only.
          // If a song is currently playing, show the "waiting for next" overlay
          // instead of starting playback mid-song.
          this.queue = msg.queue || []
          this.upNext = this.queue[0] || null
          if (msg.now_playing) {
            this.waitingForNext = true
            this.currentlyPlaying = msg.now_playing
          }
          break

        case 'play':
          // A new song is starting — clear waiting state and begin playback
          this.waitingForNext = false
          this.currentlyPlaying = null
          this.nowPlaying = msg
          this._startPlayback(msg)
          break

        case 'queue_update':
          this.queue = msg.queue || []
          // Update currentlyPlaying info for the waiting overlay, but do NOT
          // set nowPlaying (which would hide the idle/waiting screen and show blank video)
          if (this.waitingForNext) {
            this.currentlyPlaying = msg.now_playing || this.currentlyPlaying
          } else if (this.nowPlaying) {
            this.nowPlaying = msg.now_playing || this.nowPlaying
          }
          this.upNext = this.queue[0] || null
          break

        case 'pause':
          if (this.video) this.video.pause()
          break

        case 'resume':
          if (this.video && this.nowPlaying) this.video.play().catch(() => {})
          break

        case 'stop':
          // Playback ended on all screens — go back to idle
          this.waitingForNext = false
          this.currentlyPlaying = null
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
      // play_at is a server Unix timestamp (seconds); convert to ms for Date.now() comparison
      this._playAt = (msg.play_at || 0) * 1000
      this._serverTs = (msg.server_ts || 0) * 1000
      this._duration = msg.duration_secs || 0
      if (!this._baseStreamUrl) return
      this._loadVideo(this._pitchedUrl())
    },

    _loadVideo(url) {
      this.video.pause()
      clearTimeout(this._playTimer)

      this.video.onerror = () => {
        const e = this.video.error
        console.error('Video error:', e ? `code=${e.code} message=${e.message}` : 'unknown')
      }
      this.video.ontimeupdate = () => {
        // Prefer server-supplied duration (accurate for CDG streams where
        // video.duration is NaN/Infinity due to fragmented MP4 with empty_moov).
        const dur = this._duration || this.video.duration
        if (dur && isFinite(dur)) {
          this.playProgress = Math.min(1, this.video.currentTime / dur)
        }
      }
      this.video.onended = () => {
        console.info('Video ended — signalling server')
        if (this.ws?.readyState === WebSocket.OPEN) {
          this.ws.send(JSON.stringify({ type: 'song_ended' }))
        }
      }

      this.video.src = url
      this.video.load()

      if (this.needsGesture) {
        this._pendingPlay = true
        return
      }
      this._schedulePlay()
    },

    _schedulePlay() {
      clearTimeout(this._playTimer)
      const msUntilPlay = Math.max(0, this._playAt - Date.now())
      this._playTimer = setTimeout(() => this._doPlay(), msUntilPlay)
    },

    _doPlay() {
      // If the song started a while ago (late-joining screen), seek to the
      // current position so this screen stays in sync with others.
      if (this._serverTs > 0) {
        const elapsed = (Date.now() - this._serverTs) / 1000
        const dur = this.video.duration
        if (elapsed > 1 && dur && isFinite(dur) && elapsed < dur - 1) {
          try { this.video.currentTime = elapsed } catch (_) {}
        }
      }

      const p = this.video.play()
      if (p !== undefined) {
        p.catch(err => {
          if (err.name === 'NotAllowedError') {
            this.needsGesture = true
            this._pendingPlay = true
          } else {
            console.warn('Play failed:', err)
          }
        })
      }
    },

    // Called when user taps the gesture overlay
    unlockAndPlay() {
      this.needsGesture = false
      if (this._pendingPlay && this.video) {
        this._pendingPlay = false
        this._doPlay()
      }
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
      this._pendingPlay = false
      this.playProgress = 0
      this._duration = 0
      clearTimeout(this._playTimer)
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
