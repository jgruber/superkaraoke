/**
 * main.js — User interface logic
 *
 * Alpine.js app for search, queue management, and likes.
 * Connects to /ws as client_type="user" to receive real-time queue updates.
 */

const WS_RECONNECT_MS = 3000
const SEARCH_DEBOUNCE_MS = 250

function initApp() {
  return {
    // ── State ──────────────────────────────────────────────────────────────
    darkMode: false,
    connected: false,

    // Songs / search
    songs: [],
    total: 0,
    query: '',
    sort: 'title',
    page: 0,
    pageSize: 48,
    loading: false,
    searchTimer: null,

    // Queue
    queue: [],
    nowPlaying: null,

    // Enqueue modal
    modal: null,          // song being confirmed
    modalUser: '',

    // YouTube
    ytModal: false,
    ytQuery: '',
    ytResults: [],
    ytSearching: false,
    ytError: null,
    ytDownloads: {},   // job_id -> job object

    // Toast
    toast: null,
    toastTimer: null,

    // ── Init ───────────────────────────────────────────────────────────────
    init() {
      this.darkMode = localStorage.getItem('sk-theme') === 'dark' ||
        (!localStorage.getItem('sk-theme') && window.matchMedia('(prefers-color-scheme: dark)').matches)
      this._applyTheme()
      this._connectWS()
    },

    // ── Theme ──────────────────────────────────────────────────────────────
    toggleTheme() {
      this.darkMode = !this.darkMode
      localStorage.setItem('sk-theme', this.darkMode ? 'dark' : 'light')
      this._applyTheme()
    },

    _applyTheme() {
      document.documentElement.classList.toggle('dark', this.darkMode)
    },

    // ── WebSocket ──────────────────────────────────────────────────────────
    _connectWS() {
      const proto = location.protocol === 'https:' ? 'wss' : 'ws'
      this.ws = new WebSocket(`${proto}://${location.host}/ws`)

      this.ws.onopen = () => {
        this.connected = true
        this.ws.send(JSON.stringify({ client_type: 'user', name: '' }))
      }

      this.ws.onmessage = (ev) => {
        try { this._handleWS(JSON.parse(ev.data)) } catch {}
      }

      this.ws.onclose = () => {
        this.connected = false
        setTimeout(() => this._connectWS(), WS_RECONNECT_MS)
      }

      setInterval(() => {
        if (this.ws?.readyState === WebSocket.OPEN) {
          this.ws.send(JSON.stringify({ type: 'ping' }))
        }
      }, 30000)
    },

    _handleWS(msg) {
      if (msg.type === 'state' || msg.type === 'queue_update') {
        this.queue = msg.queue || []
        this.nowPlaying = msg.now_playing || null
      } else if (msg.type === 'play') {
        this.nowPlaying = msg
      } else if (msg.type === 'stop') {
        this.nowPlaying = null
      }
    },

    // ── Song search ────────────────────────────────────────────────────────
    onSearchInput() {
      clearTimeout(this.searchTimer)
      this.searchTimer = setTimeout(() => {
        this.page = 0
        this.fetchSongs()
      }, SEARCH_DEBOUNCE_MS)
    },

    onSortChange() {
      this.page = 0
      this.fetchSongs()
    },

    async fetchSongs() {
      this.loading = true
      const params = new URLSearchParams({
        q: this.query,
        sort: this.sort,
        limit: this.pageSize,
        offset: this.page * this.pageSize,
      })
      try {
        const res = await fetch(`/api/songs?${params}`)
        const data = await res.json()
        this.songs = data.songs
        this.total = data.total
      } catch {
        this.showToast('Failed to load songs', 'error')
      } finally {
        this.loading = false
      }
    },

    get totalPages() {
      return Math.ceil(this.total / this.pageSize)
    },

    prevPage() {
      if (this.page > 0) { this.page--; this.fetchSongs() }
    },

    nextPage() {
      if (this.page < this.totalPages - 1) { this.page++; this.fetchSongs() }
    },

    // ── Enqueue ────────────────────────────────────────────────────────────
    openEnqueue(song) {
      this.modal = song
      this.modalUser = localStorage.getItem('sk-username') || ''
      this.$nextTick(() => this.$refs.usernameInput?.focus())
    },

    closeModal() {
      this.modal = null
    },

    async confirmEnqueue(playNext = false) {
      if (!this.modal) return
      const user = this.modalUser.trim() || 'anonymous'
      localStorage.setItem('sk-username', user)

      try {
        const res = await fetch('/api/queue', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ song_id: this.modal.id, user, play_next: playNext }),
        })
        if (!res.ok) throw new Error()
        this.showToast(`"${this.modal.title}" added to queue`)
        this.closeModal()
        this.query = ''
        this.songs = []
        this.total = 0
        this.page = 0
      } catch {
        this.showToast('Failed to add song', 'error')
      }
    },

    isInQueue(songId) {
      return this.queue.some(e => e.song_id === songId) ||
        this.nowPlaying?.song?.id === songId
    },

    // ── Queue management ───────────────────────────────────────────────────
    async removeFromQueue(queueId) {
      await fetch(`/api/queue/${queueId}`, { method: 'DELETE' })
    },

    async moveUp(queueId) {
      await fetch(`/api/queue/${queueId}/move-up`, { method: 'POST' })
    },

    async moveDown(queueId) {
      await fetch(`/api/queue/${queueId}/move-down`, { method: 'POST' })
    },

    async skip() {
      await fetch('/api/queue/skip', { method: 'POST' })
    },

    // ── Likes ──────────────────────────────────────────────────────────────
    async toggleLike(song) {
      const liked = song.likes > 0  // simple heuristic; a real version tracks per-user
      const method = liked ? 'DELETE' : 'POST'
      try {
        const res = await fetch(`/api/songs/${song.id}/like`, { method })
        const data = await res.json()
        song.likes = data.likes
        // Sync into main songs array
        const s = this.songs.find(s => s.id === song.id)
        if (s) s.likes = data.likes
      } catch {
        this.showToast('Failed to update like', 'error')
      }
    },

    // ── YouTube ────────────────────────────────────────────────────────────
    openYouTube() {
      this.ytQuery = this.query
      this.ytModal = true
      this.ytResults = []
      this.ytError = null
      if (this.ytQuery) this.ytSearch()
    },

    closeYouTube() {
      this.ytModal = false
    },

    async ytSearch() {
      if (!this.ytQuery.trim()) return
      this.ytSearching = true
      this.ytError = null
      this.ytResults = []
      try {
        const res = await fetch(`/api/youtube/search?q=${encodeURIComponent(this.ytQuery)}`)
        const data = await res.json()
        this.ytResults = data.results
        if (!this.ytResults.length) this.ytError = 'No results found'
      } catch {
        this.ytError = 'Search failed — check connection'
      } finally {
        this.ytSearching = false
      }
    },

    async ytDownload(video) {
      try {
        const res = await fetch('/api/youtube/download', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ url: video.url, title: video.title }),
        })
        if (!res.ok) throw new Error()
        const { job_id } = await res.json()
        this.$set(this.ytDownloads, job_id, { status: 'pending', progress: 0, title: video.title })
        this._pollDownload(job_id)
      } catch {
        this.showToast('Failed to start download', 'error')
      }
    },

    _pollDownload(job_id) {
      const poll = async () => {
        try {
          const res = await fetch(`/api/youtube/download/${job_id}`)
          const job = await res.json()
          this.ytDownloads = { ...this.ytDownloads, [job_id]: job }
          if (job.status === 'done') {
            this.showToast(`"${job.title}" downloaded — library rescanning…`)
            return
          }
          if (job.status === 'error') {
            this.showToast(`Download failed: ${job.error || 'unknown error'}`, 'error')
            return
          }
          setTimeout(poll, 1500)
        } catch {
          setTimeout(poll, 3000)
        }
      }
      setTimeout(poll, 1500)
    },

    // ── Toast ──────────────────────────────────────────────────────────────
    showToast(msg, type = 'success') {
      clearTimeout(this.toastTimer)
      this.toast = { msg, type }
      this.toastTimer = setTimeout(() => { this.toast = null }, 3000)
    },
  }
}

export { initApp }
