/**
 * screen.js — Display screen logic
 *
 * Connects to /ws as client_type="screen". Plays the server's live HLS
 * stream via hls.js (or native HLS on Safari).
 *
 * Multi-screen sync strategy:
 *   1. On connect, the screen runs a short NTP-lite handshake to measure
 *      its clock offset against the server (serverNow() below).
 *   2. On 'play', the server supplies play_at (server Unix seconds). The
 *      screen waits until serverNow() === play_at before calling video.play().
 *   3. Pitch changes are sent back to the server; the server restarts the
 *      HLS session and broadcasts a fresh 'play' message to every screen.
 */
import Hls from 'hls.js'

const WS_RECONNECT_MS = 3000
const TIMESYNC_ROUNDS  = 5
const TIMESYNC_INTERVAL_MS = 30000

function initScreen() {
  return {
    // Connection / UI state
    connected: false,
    nowPlaying: null,
    upNext: null,
    queue: [],
    error: null,
    waitingForNext: false,
    currentlyPlaying: null,

    // Pitch
    semitones: 0,

    // Sync
    _serverOffsetMs: 0,     // serverNow() = Date.now() + _serverOffsetMs
    _serverTs: 0,           // ms since epoch when the song started (server frame)
    _playAt: 0,             // ms since epoch when all screens should begin
    _playTimer: null,

    // Progress
    playProgress: 0,
    _duration: 0,

    // Autoplay gating
    needsGesture: true,
    _pendingPlay: false,

    // DOM / media
    video: null,
    ws: null,
    _hls: null,
    _currentStreamUrl: null,  // dedupe repeated 'play' broadcasts with same URL
    _pingInterval: null,
    _timesyncInterval: null,

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
      try {
        const QRCode = (await import('qrcode')).default
        await QRCode.toCanvas(canvas, location.origin, {
          width: 180,
          margin: 2,
          color: { dark: '#ffffff', light: '#00000000' },
        })
      } catch (e) {
        console.warn('QR render failed:', e)
      }
    },

    serverNow() { return Date.now() + this._serverOffsetMs },

    _connect() {
      const proto = location.protocol === 'https:' ? 'wss' : 'ws'
      this.ws = new WebSocket(`${proto}://${location.host}/ws?client_type=screen&name=display`)

      this.ws.onopen = () => {
        this.connected = true
        this.error = null
        this._startTimeSync()
      }

      this.ws.onmessage = (ev) => {
        try { this._handle(JSON.parse(ev.data)) }
        catch (e) { console.error('WS parse error', e) }
      }

      this.ws.onerror = () => { this.connected = false }

      this.ws.onclose = () => {
        this.connected = false
        clearInterval(this._pingInterval)
        clearInterval(this._timesyncInterval)
        setTimeout(() => this._connect(), WS_RECONNECT_MS)
      }

      this._pingInterval = setInterval(() => {
        if (this.ws.readyState === WebSocket.OPEN) {
          this.ws.send(JSON.stringify({ type: 'ping' }))
        }
      }, 30000)
    },

    // ── Clock sync ─────────────────────────────────────────────────────────
    _startTimeSync() {
      this._timesyncSamples = []
      let rounds = TIMESYNC_ROUNDS
      const send = () => {
        if (this.ws?.readyState !== WebSocket.OPEN) return
        this.ws.send(JSON.stringify({ type: 'time_sync', t1: Date.now() }))
      }
      // Fire a short burst now to get an initial offset…
      const burst = setInterval(() => {
        if (--rounds <= 0) clearInterval(burst)
        send()
      }, 120)
      send()
      // …then one round every 30 s to compensate for drift.
      clearInterval(this._timesyncInterval)
      this._timesyncInterval = setInterval(send, TIMESYNC_INTERVAL_MS)
    },

    _recordTimeSync(msg) {
      const t4 = Date.now()
      const { t1, t2, t3 } = msg
      if (typeof t1 !== 'number') return
      const rtt    = (t4 - t1) - (t3 - t2)
      const offset = ((t2 - t1) + (t3 - t4)) / 2
      this._timesyncSamples.push({ rtt, offset })
      // Keep the 8 best (lowest-rtt) samples and use their median offset.
      this._timesyncSamples.sort((a, b) => a.rtt - b.rtt)
      this._timesyncSamples = this._timesyncSamples.slice(0, 8)
      const offsets = this._timesyncSamples.map(s => s.offset).sort((a, b) => a - b)
      this._serverOffsetMs = offsets[Math.floor(offsets.length / 2)]
    },

    // ── WS message dispatch ────────────────────────────────────────────────
    _handle(msg) {
      if (msg.type !== 'time_sync' && msg.type !== 'pong') {
        console.info('[ws]', msg.type, msg)
      }
      switch (msg.type) {
        case 'time_sync':
          this._recordTimeSync(msg)
          break

        case 'state':
          this.queue = msg.queue || []
          this.upNext = this.queue[0] || null
          if (msg.now_playing && msg.now_playing.stream_url) {
            // Joining mid-song: show the "waiting" screen; the next 'play'
            // broadcast (which also fires on pitch change) will start us.
            this.waitingForNext = true
            this.currentlyPlaying = msg.now_playing
          }
          break

        case 'play':
          this.waitingForNext = false
          this.currentlyPlaying = null
          this.nowPlaying = msg
          this.semitones = typeof msg.semitones === 'number' ? msg.semitones : 0
          this._startPlayback(msg)
          break

        case 'queue_update':
          this.queue = msg.queue || []
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
          this.waitingForNext = false
          this.currentlyPlaying = null
          this.nowPlaying = null
          this.upNext = this.queue[0] || null
          this._stopPlayback()
          break
      }
    },

    // ── Playback ──────────────────────────────────────────────────────────
    _startPlayback(msg) {
      this._playAt   = (msg.play_at   || 0) * 1000
      this._serverTs = (msg.server_ts || 0) * 1000
      this._duration = msg.duration_secs || 0
      if (!msg.stream_url) return
      // Re-broadcasting the same stream_url (e.g. a redundant queue_update
      // translated into play) must NOT tear down and re-attach hls.js —
      // that would restart playback from segment 0 and look like a loop.
      if (msg.stream_url === this._currentStreamUrl) {
        console.info('[play] same stream_url, only rescheduling')
        this._schedulePlay()
        return
      }
      this._currentStreamUrl = msg.stream_url
      this._loadVideo(msg.stream_url)
    },

    _destroyHls() {
      if (this._hls) {
        try { this._hls.destroy() } catch (_) {}
        this._hls = null
      }
    },

    _loadVideo(url) {
      console.info('[load] new stream:', url)
      this.video.pause()
      clearTimeout(this._playTimer)
      this._destroyHls()
      this.playProgress = 0

      this.video.onerror = () => {
        const e = this.video.error
        console.error('Video error:', e ? `code=${e.code} message=${e.message}` : 'unknown')
      }
      this.video.ontimeupdate = () => {
        const dur = this._duration || this.video.duration
        if (dur && isFinite(dur)) {
          this.playProgress = Math.min(1, this.video.currentTime / dur)
        }
      }
      this.video.onended = () => {
        console.info('[video] onended @ currentTime=' + this.video.currentTime)
        if (this.ws?.readyState === WebSocket.OPEN) {
          this.ws.send(JSON.stringify({ type: 'song_ended' }))
        }
      }
      this.video.onseeked = () => {
        console.info('[video] seeked → ' + this.video.currentTime.toFixed(2))
      }
      this.video.onwaiting = () => {
        console.info('[video] waiting (buffer underrun) @ ' + this.video.currentTime.toFixed(2))
      }

      const isHls = /\.m3u8(\?|$)/.test(url)
      if (isHls && Hls.isSupported()) {
        const hls = new Hls({
          // ffmpeg writes segments as fast as it can and appends
          // #EXT-X-ENDLIST on exit. The playlist is always further
          // ahead than playback, so even during the brief "still live"
          // window the live-edge heuristics don't fight us.
          startPosition: 0,
          maxLiveSyncPlaybackRate: 1,
          enableWorker: true,
          lowLatencyMode: false,
          backBufferLength: 90,
          maxBufferLength: 60,
          debug: false,
        })
        this._hls = hls
        hls.loadSource(url)
        hls.attachMedia(this.video)
        // BUFFER_EOS fires when hls.js finishes LOADING the last
        // fragment into the source buffer, which — now that ffmpeg
        // runs at full speed — happens many seconds before the <video>
        // element actually plays through the song. Rely on the native
        // 'ended' event instead, which fires once currentTime reaches
        // the duration set by #EXT-X-ENDLIST.
        hls.on(Hls.Events.ERROR, (_e, data) => {
          console.warn('[hls] ERROR', data.type, data.details, data.fatal ? '(fatal)' : '')
          if (data.fatal) {
            if (data.type === Hls.ErrorTypes.NETWORK_ERROR) hls.startLoad()
            else if (data.type === Hls.ErrorTypes.MEDIA_ERROR) hls.recoverMediaError()
          }
        })
      } else if (isHls && this.video.canPlayType('application/vnd.apple.mpegurl')) {
        this.video.src = url
        this.video.load()
      } else {
        this.video.src = url
        this.video.load()
      }

      if (this.needsGesture) {
        this._pendingPlay = true
        return
      }
      this._schedulePlay()
    },

    _schedulePlay() {
      clearTimeout(this._playTimer)
      const msUntilPlay = Math.max(0, this._playAt - this.serverNow())
      this._playTimer = setTimeout(() => this._doPlay(), msUntilPlay)
    },

    _doPlay() {
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

    unlockAndPlay() {
      this.needsGesture = false
      if (this._pendingPlay && this.video) {
        this._pendingPlay = false
        this._schedulePlay()
      }
    },

    _stopPlayback() {
      console.info('[stop] playback cleared')
      this._pendingPlay = false
      this._currentStreamUrl = null
      this.playProgress = 0
      this._duration = 0
      clearTimeout(this._playTimer)
      this._destroyHls()
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
