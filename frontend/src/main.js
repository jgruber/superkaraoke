/**
 * main.js — User interface logic
 *
 * Alpine.js app for search, queue management, likes, auth, and user management.
 * Connects to /ws as client_type="user" to receive real-time queue updates.
 */

const WS_RECONNECT_MS    = 3000
const SEARCH_DEBOUNCE_MS = 250

function initApp() {
  return {
    // ── State ──────────────────────────────────────────────────────────────
    darkMode:  false,
    connected: false,

    // Songs / search
    songs:       [],
    total:       0,
    query:       '',
    sort:        'title',
    page:        0,
    pageSize:    48,
    loading:     false,
    searchTimer: null,

    // Queue
    queue:      [],
    nowPlaying: null,
    paused:     false,

    // Enqueue modal
    modal:     null,   // song being confirmed
    modalUser: '',

    // YouTube
    ytModal:     false,
    ytQuery:     '',
    ytResults:   [],
    ytSearching: false,
    ytError:     null,
    ytDownloads: {},   // job_id → job object

    // ── Auth ───────────────────────────────────────────────────────────────
    authUser:    null,   // username from session (null = local / not logged in)
    isLocal:     false,  // true = client IP is in allowed_networks
    isBootstrap: false,  // true = no users exist yet
    authReady:   false,  // true once /api/auth/me has responded

    // Login modal
    showLogin:      false,
    loginUser:      '',
    loginPass:      '',
    loginError:     null,
    loginLoading:   false,

    // Profile dropdown
    profileOpen: false,

    // User management modal
    usersModal:      false,
    usersList:       [],
    usersLoading:    false,
    newUsername:     '',
    newPassword:     '',
    newPasswordConfirm: '',
    newUserError:    null,
    newUserSaving:   false,
    // per-user change-password form (keyed by username)
    changePwForm:    {},   // { username: { open, current, next, confirm, error, saving } }
    deleteConfirm:   null, // username pending delete confirmation

    // Toast
    toast:      null,
    toastTimer: null,

    // ── Computed ───────────────────────────────────────────────────────────
    /** Username shown in the profile chip */
    get displayUser() {
      if (this.authUser) return this.authUser
      return localStorage.getItem('sk-username') || 'Guest'
    },

    /** True if the current visitor can reach user management */
    get canManageUsers() {
      return this.authUser || this.isLocal || this.isBootstrap
    },

    // ── Init ───────────────────────────────────────────────────────────────
    async init() {
      this.darkMode = localStorage.getItem('sk-theme') === 'dark' ||
        (!localStorage.getItem('sk-theme') && window.matchMedia('(prefers-color-scheme: dark)').matches)
      this._applyTheme()
      await this._checkAuth()
      this._connectWS()
    },

    // ── Auth ───────────────────────────────────────────────────────────────
    async _checkAuth() {
      try {
        const res  = await fetch('/api/auth/me')
        if (res.ok) {
          const d          = await res.json()
          this.authUser    = d.username || null
          this.isLocal     = d.local    || false
          this.isBootstrap = d.bootstrap || false
        } else if (res.status === 401) {
          this.showLogin = true
        }
      } catch {
        // network error — show login as a fallback
        this.showLogin = true
      } finally {
        this.authReady = true
      }
    },

    async submitLogin() {
      this.loginError   = null
      this.loginLoading = true
      try {
        const res = await fetch('/api/auth/login', {
          method:  'POST',
          headers: { 'Content-Type': 'application/json' },
          body:    JSON.stringify({ username: this.loginUser.trim(), password: this.loginPass }),
        })
        if (!res.ok) {
          const d = await res.json().catch(() => ({}))
          this.loginError = d.detail || 'Login failed'
          return
        }
        const d = await res.json()
        this.authUser  = d.username
        this.isLocal   = false
        this.showLogin = false
        this.loginUser = ''
        this.loginPass = ''
      } catch {
        this.loginError = 'Network error — please try again'
      } finally {
        this.loginLoading = false
      }
    },

    async logout() {
      await fetch('/api/auth/logout', { method: 'POST' })
      this.authUser    = null
      this.isLocal     = false
      this.profileOpen = false
      this.showLogin   = true
    },

    // ── Profile dropdown ───────────────────────────────────────────────────
    toggleProfile() {
      this.profileOpen = !this.profileOpen
    },

    setLocalUsername() {
      const name = prompt('Enter your name:', localStorage.getItem('sk-username') || '')
      if (name !== null) {
        const trimmed = name.trim()
        if (trimmed) localStorage.setItem('sk-username', trimmed)
        else         localStorage.removeItem('sk-username')
      }
      this.profileOpen = false
    },

    // ── User management modal ──────────────────────────────────────────────
    async openUsersModal() {
      this.profileOpen     = false
      this.usersModal      = true
      this.newUsername     = ''
      this.newPassword     = ''
      this.newPasswordConfirm = ''
      this.newUserError    = null
      this.changePwForm    = {}
      this.deleteConfirm   = null
      await this._loadUsers()
    },

    async _loadUsers() {
      this.usersLoading = true
      try {
        const res = await fetch('/api/users')
        if (res.ok) {
          const d        = await res.json()
          this.usersList = d.users
        }
      } catch {}
      finally { this.usersLoading = false }
    },

    async createUser() {
      this.newUserError = null
      if (!this.newUsername.trim()) { this.newUserError = 'Username required'; return }
      if (!this.newPassword)        { this.newUserError = 'Password required';  return }
      if (this.newPassword !== this.newPasswordConfirm) {
        this.newUserError = 'Passwords do not match'; return
      }
      this.newUserSaving = true
      try {
        const res = await fetch('/api/users', {
          method:  'POST',
          headers: { 'Content-Type': 'application/json' },
          body:    JSON.stringify({ username: this.newUsername.trim(), password: this.newPassword }),
        })
        const d = await res.json().catch(() => ({}))
        if (!res.ok) { this.newUserError = d.detail || 'Failed to create user'; return }
        this.newUsername  = ''
        this.newPassword  = ''
        this.newPasswordConfirm = ''
        this.showToast(`User "${d.username}" created`)
        // Once first user is created, bootstrap mode ends
        if (this.isBootstrap) { this.isBootstrap = false }
        await this._loadUsers()
      } catch {
        this.newUserError = 'Network error'
      } finally {
        this.newUserSaving = false
      }
    },

    openChangePw(username) {
      this.changePwForm = {
        ...this.changePwForm,
        [username]: { open: true, current: '', next: '', confirm: '', error: null, saving: false },
      }
    },

    async submitChangePw(username) {
      const f = this.changePwForm[username]
      if (!f) return
      f.error = null
      if (!f.next)              { f.error = 'New password required'; return }
      if (f.next !== f.confirm) { f.error = 'Passwords do not match'; return }
      f.saving = true
      try {
        const body = { new_password: f.next }
        // Remote auth'd users must supply current password for their own account
        if (this.authUser && !this.isLocal) body.current_password = f.current
        const res = await fetch(`/api/users/${encodeURIComponent(username)}/password`, {
          method:  'POST',
          headers: { 'Content-Type': 'application/json' },
          body:    JSON.stringify(body),
        })
        const d = await res.json().catch(() => ({}))
        if (!res.ok) { f.error = d.detail || 'Failed to change password'; return }
        this.changePwForm = { ...this.changePwForm, [username]: { ...f, open: false } }
        this.showToast('Password updated')
      } catch {
        f.error = 'Network error'
      } finally {
        f.saving = false
      }
    },

    async deleteUser(username) {
      try {
        const res = await fetch(`/api/users/${encodeURIComponent(username)}`, { method: 'DELETE' })
        if (!res.ok) { this.showToast('Delete failed', 'error'); return }
        this.deleteConfirm = null
        this.showToast(`User "${username}" deleted`)
        // If we deleted ourselves, log out
        if (username === this.authUser) { await this.logout(); return }
        await this._loadUsers()
      } catch {
        this.showToast('Network error', 'error')
      }
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
        this.queue      = msg.queue       || []
        this.nowPlaying = msg.now_playing || null
        this.paused     = msg.now_playing?.paused || false
      } else if (msg.type === 'play') {
        this.nowPlaying = msg
        this.paused     = false
      } else if (msg.type === 'pause') {
        this.paused = true
      } else if (msg.type === 'resume') {
        this.paused = false
      } else if (msg.type === 'stop') {
        this.nowPlaying = null
        this.paused     = false
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
        q:      this.query,
        sort:   this.sort,
        limit:  this.pageSize,
        offset: this.page * this.pageSize,
      })
      try {
        const res  = await fetch(`/api/songs?${params}`)
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
      // Authenticated users use their session name — no prompt needed
      if (this.authUser) {
        this._enqueue(song, this.authUser)
        return
      }
      const saved = localStorage.getItem('sk-username')
      if (saved) {
        this._enqueue(song, saved)
        return
      }
      this.modal     = song
      this.modalUser = ''
      this.$nextTick(() => this.$refs.usernameInput?.focus())
    },

    async _enqueue(song, user, playNext = false) {
      try {
        const res = await fetch('/api/queue', {
          method:  'POST',
          headers: { 'Content-Type': 'application/json' },
          body:    JSON.stringify({ song_id: song.id, user, play_next: playNext }),
        })
        if (!res.ok) throw new Error()
        this.showToast(`"${song.title}" added to queue`)
        this.query = ''
        this.songs = []
        this.total = 0
        this.page  = 0
      } catch {
        this.showToast('Failed to add song', 'error')
      }
    },

    closeModal() { this.modal = null },

    async confirmEnqueue(playNext = false) {
      if (!this.modal) return
      const user = this.modalUser.trim() || 'anonymous'
      localStorage.setItem('sk-username', user)
      const song = this.modal
      this.closeModal()
      await this._enqueue(song, user, playNext)
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

    async togglePause() {
      const endpoint = this.paused ? '/api/queue/resume' : '/api/queue/pause'
      await fetch(endpoint, { method: 'POST' })
    },

    // ── Likes ──────────────────────────────────────────────────────────────
    async toggleLike(song) {
      const liked  = song.likes > 0
      const method = liked ? 'DELETE' : 'POST'
      try {
        const res  = await fetch(`/api/songs/${song.id}/like`, { method })
        const data = await res.json()
        song.likes = data.likes
        const s    = this.songs.find(s => s.id === song.id)
        if (s) s.likes = data.likes
      } catch {
        this.showToast('Failed to update like', 'error')
      }
    },

    // ── YouTube ────────────────────────────────────────────────────────────
    openYouTube() {
      this.ytQuery   = this.query
      this.ytModal   = true
      this.ytResults = []
      this.ytError   = null
      if (this.ytQuery) this.ytSearch()
    },

    closeYouTube() { this.ytModal = false },

    async ytSearch() {
      if (!this.ytQuery.trim()) return
      this.ytSearching = true
      this.ytError     = null
      this.ytResults   = []
      try {
        const res  = await fetch(`/api/youtube/search?q=${encodeURIComponent(this.ytQuery)}`)
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
          method:  'POST',
          headers: { 'Content-Type': 'application/json' },
          body:    JSON.stringify({ url: video.url, title: video.title, channel: video.channel }),
        })
        if (!res.ok) throw new Error()
        const { job_id } = await res.json()
        this.ytDownloads = { ...this.ytDownloads, [job_id]: { status: 'pending', progress: 0, title: video.title } }
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
            if (job.song_id) {
              // Use auth username if available, otherwise localStorage
              const user = this.authUser || localStorage.getItem('sk-username') || 'anonymous'
              await fetch('/api/queue', {
                method:  'POST',
                headers: { 'Content-Type': 'application/json' },
                body:    JSON.stringify({ song_id: job.song_id, user }),
              })
              this.showToast(`"${job.title}" downloaded and added to queue`)
              this.closeYouTube()
            } else {
              this.showToast(`"${job.title}" downloaded — search to add to queue`)
            }
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
      this.toast      = { msg, type }
      this.toastTimer = setTimeout(() => { this.toast = null }, 3000)
    },
  }
}

export { initApp }
