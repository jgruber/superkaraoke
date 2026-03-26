/**
 * library.js — Library management UI
 *
 * Full song list with search/filter/sort, inline metadata editing,
 * re-detect from file, and MusicBrainz lookup.
 */

const PAGE_SIZE = 50
const DEBOUNCE_MS = 300

function initLibrary() {
  return {
    // ── State ─────────────────────────────────────────────────────────────
    darkMode: false,
    songs: [],
    total: 0,
    stats: { total: 0, cdg_count: 0, video_count: 0, no_artist: 0, liked: 0 },

    query: '',
    sort: 'title',
    kindFilter: '',
    page: 0,
    loading: false,
    scanning: false,
    _searchTimer: null,

    // Edit modal
    modal: false,
    song: null,       // working copy being edited
    saving: false,
    redetecting: false,
    deleteConfirm: false,
    deleting: false,

    // MusicBrainz panel (inside modal)
    mbOpen: false,
    mbTitle: '',
    mbArtist: '',
    mbLoading: false,
    mbResults: [],
    mbError: null,

    // Import confirmation
    importConfirm: false,
    importing: false,
    _importFile: null,

    // Toast
    toast: null,
    _toastTimer: null,

    // ── Boot ──────────────────────────────────────────────────────────────
    init() {
      this.darkMode = localStorage.getItem('sk-theme') === 'dark' ||
        (!localStorage.getItem('sk-theme') && window.matchMedia('(prefers-color-scheme: dark)').matches)
      this._applyTheme()
      this.fetchStats()
      this.fetchSongs()
    },

    toggleTheme() {
      this.darkMode = !this.darkMode
      localStorage.setItem('sk-theme', this.darkMode ? 'dark' : 'light')
      this._applyTheme()
    },

    _applyTheme() {
      document.documentElement.classList.toggle('dark', this.darkMode)
    },

    // ── Data fetching ─────────────────────────────────────────────────────
    async fetchStats() {
      try {
        const r = await fetch('/api/library/stats')
        this.stats = await r.json()
      } catch {}
    },

    async fetchSongs() {
      this.loading = true
      const p = new URLSearchParams({
        q: this.query,
        sort: this.sort,
        kind: this.kindFilter,
        limit: PAGE_SIZE,
        offset: this.page * PAGE_SIZE,
      })
      try {
        const r = await fetch(`/api/library?${p}`)
        const d = await r.json()
        this.songs = d.songs
        this.total = d.total
      } catch {
        this.showToast('Failed to load songs', 'error')
      } finally {
        this.loading = false
      }
    },

    onSearchInput() {
      clearTimeout(this._searchTimer)
      this._searchTimer = setTimeout(() => { this.page = 0; this.fetchSongs() }, DEBOUNCE_MS)
    },

    onFilterChange() { this.page = 0; this.fetchSongs() },

    get totalPages() { return Math.ceil(this.total / PAGE_SIZE) },
    prevPage() { if (this.page > 0) { this.page--; this.fetchSongs() } },
    nextPage() { if (this.page < this.totalPages - 1) { this.page++; this.fetchSongs() } },

    // ── Scan ──────────────────────────────────────────────────────────────
    async triggerScan() {
      this.scanning = true
      try {
        await fetch('/api/library/scan', { method: 'POST' })
        this.showToast('Scan started — library will update shortly')
        // Poll for completion by refreshing after a few seconds
        setTimeout(() => { this.fetchStats(); this.fetchSongs() }, 4000)
      } catch {
        this.showToast('Scan failed', 'error')
      } finally {
        this.scanning = false
      }
    },

    // ── Edit modal ────────────────────────────────────────────────────────
    openEdit(s) {
      // Deep copy so edits don't mutate the table row before saving
      this.song = { ...s }
      this.mbOpen = false
      this.mbResults = []
      this.mbError = null
      this.mbTitle  = s.title
      this.mbArtist = s.artist
      this.deleteConfirm = false
      this.modal = true
      this.$nextTick(() => this.$refs.titleInput?.focus())
    },

    closeModal() {
      this.modal = false
      this.song = null
      this.deleteConfirm = false
    },

    async deleteSong() {
      if (!this.song) return
      this.deleting = true
      try {
        const r = await fetch(`/api/library/${this.song.id}`, { method: 'DELETE' })
        if (!r.ok) throw new Error()
        this.songs = this.songs.filter(s => s.id !== this.song.id)
        this.total = Math.max(0, this.total - 1)
        this.showToast(`"${this.song.title}" removed from library`)
        this.closeModal()
        this.fetchStats()
      } catch {
        this.showToast('Delete failed', 'error')
      } finally {
        this.deleting = false
      }
    },

    async save() {
      if (!this.song) return
      this.saving = true
      try {
        const r = await fetch(`/api/library/${this.song.id}`, {
          method: 'PATCH',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({
            title:        this.song.title,
            artist:       this.song.artist,
            year:         this.song.year   ? Number(this.song.year)  : null,
            genre:        this.song.genre,
            likes:        this.song.likes  ? Number(this.song.likes) : 0,
            is_duplicate: this.song.is_duplicate ? 1 : 0,
          }),
        })
        if (!r.ok) throw new Error()
        const updated = await r.json()
        // Reflect in table
        const idx = this.songs.findIndex(s => s.id === updated.id)
        if (idx !== -1) this.songs[idx] = updated
        this.showToast('Saved')
        this.closeModal()
      } catch {
        this.showToast('Save failed', 'error')
      } finally {
        this.saving = false
      }
    },

    // ── Re-detect ─────────────────────────────────────────────────────────
    async redetect() {
      if (!this.song) return
      this.redetecting = true
      try {
        const r = await fetch(`/api/library/${this.song.id}/redetect`, { method: 'POST' })
        if (!r.ok) throw new Error()
        const updated = await r.json()
        this.song = { ...updated }
        this.mbTitle  = updated.title
        this.mbArtist = updated.artist
        const idx = this.songs.findIndex(s => s.id === updated.id)
        if (idx !== -1) this.songs[idx] = updated
        this.showToast('Metadata re-detected from file')
      } catch {
        this.showToast('Re-detect failed', 'error')
      } finally {
        this.redetecting = false
      }
    },

    // ── MusicBrainz ───────────────────────────────────────────────────────
    toggleMb() {
      this.mbOpen = !this.mbOpen
      if (this.mbOpen && this.mbResults.length === 0) this.mbSearch()
    },

    async mbSearch() {
      if (!this.mbTitle && !this.mbArtist) return
      this.mbLoading = true
      this.mbError   = null
      this.mbResults = []
      try {
        const p = new URLSearchParams({ title: this.mbTitle, artist: this.mbArtist })
        const r = await fetch(`/api/library/${this.song.id}/lookup?${p}`, { method: 'POST' })
        if (!r.ok) throw new Error()
        const d = await r.json()
        this.mbResults = d.results
        if (this.mbResults.length === 0) this.mbError = 'No results found'
      } catch {
        this.mbError = 'Lookup failed — check connection'
      } finally {
        this.mbLoading = false
      }
    },

    applyMbResult(result) {
      if (result.title)  this.song.title  = result.title
      if (result.artist) this.song.artist = result.artist
      if (result.year)   this.song.year   = result.year
      if (result.genre)  this.song.genre  = result.genre
      this.mbOpen = false
      this.showToast('Applied — review and save')
    },

    // ── Export / Import ───────────────────────────────────────────────────
    exportDb() {
      window.location.href = '/api/library/export'
    },

    pickImportFile() {
      this.$refs.importFileInput.value = ''
      this.$refs.importFileInput.click()
    },

    onImportFilePicked(event) {
      const file = event.target.files[0]
      if (!file) return
      this._importFile = file
      this.importConfirm = true
    },

    async confirmImport() {
      if (!this._importFile) return
      this.importing = true
      try {
        const body = new FormData()
        body.append('file', this._importFile)
        const r = await fetch('/api/library/import', { method: 'POST', body })
        if (!r.ok) {
          const err = await r.json().catch(() => ({}))
          throw new Error(err.detail || 'Import failed')
        }
        const d = await r.json()
        this.importConfirm = false
        this._importFile = null
        this.showToast(`Imported — ${d.songs.toLocaleString()} songs. Rescanning…`)
        setTimeout(() => { this.fetchStats(); this.fetchSongs() }, 3000)
      } catch (e) {
        this.showToast(e.message || 'Import failed', 'error')
      } finally {
        this.importing = false
      }
    },

    // ── Toast ─────────────────────────────────────────────────────────────
    showToast(msg, type = 'success') {
      clearTimeout(this._toastTimer)
      this.toast = { msg, type }
      this._toastTimer = setTimeout(() => { this.toast = null }, 3000)
    },
  }
}

export { initLibrary }
