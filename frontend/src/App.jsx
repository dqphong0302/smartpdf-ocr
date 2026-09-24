import { useState, useEffect, useRef, useCallback } from 'react'
import DOMPurify from 'dompurify'

const API = ''  // proxy handles it

const TRANSLATE_STEPS = [
  { num: 1, label: 'Tải sách PDF' },
  { num: 2, label: 'Cấu hình Dịch' },
  { num: 3, label: 'Tiến trình Xử lý' },
  { num: 4, label: 'Kết quả & Xem trước' },
]

const OCR_STEPS = [
  { num: 1, label: 'Tải lên' },
  { num: 2, label: 'Chọn trang' },
  { num: 3, label: 'Xử lý OCR' },
  { num: 4, label: 'Kết quả' },
]

export default function App() {
  // ── Auth state ──────
  const [isLoggedIn, setIsLoggedIn] = useState(false)
  const [authChecking, setAuthChecking] = useState(true)
  const [loginUser, setLoginUser] = useState('')
  const [loginPass, setLoginPass] = useState('')
  const [loginError, setLoginError] = useState('')
  const [currentUser, setCurrentUser] = useState('')
  const [loginLoading, setLoginLoading] = useState(false)

  // ── Mode Switcher ──
  const [appMode, setAppMode] = useState('translate') // 'translate' | 'ocr'
  const [showDashboard, setShowDashboard] = useState(false)
  const [dashboardJobs, setDashboardJobs] = useState([])
  const [dashboardLoading, setDashboardLoading] = useState(false)

  // ── Common Document state ──
  const [step, setStep] = useState(1)
  const [job, setJob] = useState(null)
  const [analysis, setAnalysis] = useState(null)
  const [selectedPages, setSelectedPages] = useState(new Set())
  const [uploading, setUploading] = useState(false)
  const [processing, setProcessing] = useState(false)
  const [elapsed, setElapsed] = useState(0)
  const [dragOver, setDragOver] = useState(false)
  const [copied, setCopied] = useState(false)
  const [lightboxUrl, setLightboxUrl] = useState(null)

  // ── Book Translation Engine state ──
  const [translateMode, setTranslateMode] = useState('inplace') // 'inplace' | 'bilingual_dual'
  const [glossaryProfile, setGlossaryProfile] = useState('general') // 'general' | 'medical' | 'dental' | 'tech'
  const [availableGlossaries, setAvailableGlossaries] = useState([])
  const [selectedModel, setSelectedModel] = useState('gpt-5.6-luna')
  const [availableModels, setAvailableModels] = useState(['gpt-5.6-luna', 'gh/gpt-5.4-mini', 'gh/gpt-5.4'])
  const [autoMineGlossary, setAutoMineGlossary] = useState(true)
  const [rangeMode, setRangeMode] = useState('all') // 'all' | 'custom'
  const [customRangeInput, setCustomRangeInput] = useState('')
  const [translationProgress, setTranslationProgress] = useState({ current: 0, total: 0, message: '' })
  const [translationResult, setTranslationResult] = useState(null)
  const [jobGlossary, setJobGlossary] = useState({})
  const [resultTab, setResultTab] = useState('preview') // 'preview' | 'glossary'

  // ── OCR Specific state ──
  const [forceMethod, setForceMethod] = useState('vision')
  const [extractImages, setExtractImages] = useState(false)
  const [pageResults, setPageResults] = useState({})
  const [summary, setSummary] = useState(null)
  const [previewPage, setPreviewPage] = useState(null)
  const [viewMode, setViewMode] = useState('text')
  const [isSharedView, setIsSharedView] = useState(false)
  const [shareUrl, setShareUrl] = useState(null)

  const fileInput = useRef(null)
  const wsRef = useRef(null)
  const timerRef = useRef(null)

  // ── Check auth on mount ──────
  useEffect(() => {
    fetch(`${API}/api/auth/check`, { credentials: 'include' })
      .then(res => {
        if (res.ok) return res.json()
        throw new Error('Not logged in')
      })
      .then(data => {
        setIsLoggedIn(true)
        setCurrentUser(data.username)
      })
      .catch(() => setIsLoggedIn(false))
      .finally(() => setAuthChecking(false))
  }, [])

  // ── Load available models & glossaries ──────
  useEffect(() => {
    if (!isLoggedIn) return

    // Fetch models
    fetch(`${API}/api/v1/models/gpt`, { credentials: 'include' })
      .then(r => r.ok ? r.json() : null)
      .then(d => {
        if (d && d.supported_models) {
          setAvailableModels(d.supported_models)
          if (d.active_model) setSelectedModel(d.active_model)
        }
      })
      .catch(() => {})

    // Fetch glossaries
    fetch(`${API}/api/translate/glossaries`, { credentials: 'include' })
      .then(r => r.ok ? r.json() : null)
      .then(d => {
        if (d && d.glossaries) {
          setAvailableGlossaries(d.glossaries)
        }
      })
      .catch(() => {})
  }, [isLoggedIn])

  // ── Global Lightbox hook ──────
  useEffect(() => {
    window.openLightbox = (url) => setLightboxUrl(url)
    return () => { delete window.openLightbox }
  }, [])

  const handleLogin = async (e) => {
    e.preventDefault()
    setLoginError('')
    setLoginLoading(true)
    try {
      const res = await fetch(`${API}/api/auth/login`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ username: loginUser, password: loginPass }),
        credentials: 'include',
      })
      if (!res.ok) {
        const d = await res.json()
        throw new Error(d.detail || 'Đăng nhập thất bại')
      }
      const data = await res.json()
      setIsLoggedIn(true)
      setCurrentUser(data.username)
    } catch (err) {
      setLoginError(err.message)
    }
    setLoginLoading(false)
  }

  const handleLogout = async () => {
    await fetch(`${API}/api/auth/logout`, { method: 'POST', credentials: 'include' })
    setIsLoggedIn(false)
    setCurrentUser('')
    setLoginUser('')
    setLoginPass('')
  }

  // ── Hash routing ──────
  useEffect(() => {
    const loadFromHash = async () => {
      const hash = window.location.hash
      if (hash === '#/dashboard') {
        setShowDashboard(true)
        setDashboardLoading(true)
        try {
          const res = await fetch(`${API}/api/jobs`, { credentials: 'include' })
          if (res.ok) setDashboardJobs(await res.json())
        } catch { }
        setDashboardLoading(false)
        return
      }

      setShowDashboard(false)

      const match = hash.match(/^#\/view\/(.+)$/)
      if (!match) return

      const jobId = match[1]
      try {
        const res = await fetch(`${API}/api/jobs/${jobId}?include_text=true`, { credentials: 'include' })
        if (!res.ok) throw new Error('Not found')
        const data = await res.json()

        setJob({ job_id: data.job_id, filename: data.filename })
        setAnalysis({ total_pages: data.total_pages, pages: [] })
        setElapsed(data.elapsed_time || 0)
        setSummary(data.summary)
        setIsSharedView(true)
        setAppMode('ocr')

        const pr = {}
        const sel = new Set()
        for (const [num, page] of Object.entries(data.pages)) {
          const n = parseInt(num)
          pr[n] = page
          sel.add(n)
        }
        setPageResults(pr)
        setSelectedPages(sel)
        setStep(4)
        setShareUrl(window.location.href)
      } catch {
        alert('Không tìm thấy kết quả OCR này.')
      }
    }
    loadFromHash()
    window.addEventListener('hashchange', loadFromHash)
    return () => window.removeEventListener('hashchange', loadFromHash)
  }, [])

  // ── Upload Handler ─────────────────────────
  const handleUpload = useCallback(async (file) => {
    if (!file || !file.name.toLowerCase().endsWith('.pdf')) {
      alert('Vui lòng chọn file có định dạng PDF.')
      return
    }
    setUploading(true)
    setJob(null)
    setAnalysis(null)
    setPageResults({})
    setSummary(null)
    setTranslationResult(null)
    setJobGlossary({})
    setElapsed(0)

    const form = new FormData()
    form.append('file', file)

    try {
      const res = await fetch(`${API}/api/upload`, { method: 'POST', body: form, credentials: 'include' })
      if (!res.ok) {
        let msg = `Upload failed (${res.status})`
        try { const d = await res.json(); msg = d.detail || msg } catch { }
        throw new Error(msg)
      }
      const data = await res.json()
      setJob(data)
      setAnalysis(data.analysis)
      const allPages = new Set(data.analysis.pages.map(p => p.page_num))
      setSelectedPages(allPages)
      setCustomRangeInput(`1-${data.analysis.total_pages}`)
      setStep(2)
    } catch (e) {
      alert('Lỗi tải lên: ' + (e.message || 'Không thể kết nối server'))
    }
    setUploading(false)
  }, [])

  const onDrop = useCallback((e) => {
    e.preventDefault()
    setDragOver(false)
    const file = e.dataTransfer.files[0]
    if (file) handleUpload(file)
  }, [handleUpload])

  // ── Range parsing helper ───────────────────
  const parseRangeString = (str, maxPages) => {
    const pages = new Set()
    const parts = str.split(',')
    for (const part of parts) {
      const trimmed = part.trim()
      if (trimmed.includes('-')) {
        const [sStr, eStr] = trimmed.split('-')
        const s = parseInt(sStr, 10)
        const e = parseInt(eStr, 10)
        if (!isNaN(s) && !isNaN(e)) {
          for (let i = Math.max(1, s); i <= Math.min(maxPages, e); i++) {
            pages.add(i)
          }
        }
      } else {
        const p = parseInt(trimmed, 10)
        if (!isNaN(p) && p >= 1 && p <= maxPages) {
          pages.add(p)
        }
      }
    }
    return pages
  }

  const handleCustomRangeChange = (val) => {
    setCustomRangeInput(val)
    if (analysis) {
      const parsed = parseRangeString(val, analysis.total_pages)
      setSelectedPages(parsed)
    }
  }

  const togglePage = (num) => {
    setSelectedPages(prev => {
      const next = new Set(prev)
      if (next.has(num)) next.delete(num)
      else next.add(num)
      return next
    })
  }

  const selectAll = () => {
    if (!analysis) return
    const all = new Set(analysis.pages.map(p => p.page_num))
    setSelectedPages(all)
    setCustomRangeInput(`1-${analysis.total_pages}`)
  }

  const selectNone = () => {
    setSelectedPages(new Set())
    setCustomRangeInput('')
  }

  // ── Start Book Translation ──────────────────
  const startTranslation = async () => {
    if (!job) return
    setProcessing(true)
    setElapsed(0)
    setStep(3)
    setTranslationProgress({
      current: 0,
      total: selectedPages.size,
      message: 'Khởi tạo quy trình AegisTrans và kết nối 9router...'
    })

    const start = Date.now()
    timerRef.current = setInterval(() => {
      setElapsed(((Date.now() - start) / 1000).toFixed(1))
    }, 100)

    const wsUrl = `${window.location.protocol === 'https:' ? 'wss' : 'ws'}://${window.location.host}/ws/${job.job_id}`
    const ws = new WebSocket(wsUrl)
    wsRef.current = ws

    ws.onmessage = (e) => {
      try {
        const msg = JSON.parse(e.data)
        if (msg.type === 'translation_progress') {
          setTranslationProgress({
            current: msg.current,
            total: msg.total,
            message: msg.message,
          })
        } else if (msg.type === 'translation_completed') {
          setProcessing(false)
          clearInterval(timerRef.current)
          setTranslationResult(msg.result)
          // Fetch auto-mined glossary
          fetch(`${API}/api/translate/${job.job_id}/glossary`, { credentials: 'include' })
            .then(r => r.ok ? r.json() : null)
            .then(d => { if (d && d.terms) setJobGlossary(d.terms) })
            .catch(() => {})
          setStep(4)
        } else if (msg.type === 'translation_failed') {
          setProcessing(false)
          clearInterval(timerRef.current)
          alert('Dịch sách thất bại: ' + (msg.error || 'Lỗi không xác định'))
          setStep(2)
        }
      } catch {}
    }

    ws.onclose = () => {
      // Handled in message
    }

    const pagesArray = [...selectedPages].sort((a, b) => a - b)
    try {
      const res = await fetch(`${API}/api/translate/${job.job_id}`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        credentials: 'include',
        body: JSON.stringify({
          mode: translateMode,
          glossary_profile: glossaryProfile,
          model: selectedModel,
          pages: pagesArray,
        }),
      })
      if (!res.ok) {
        const err = await res.json()
        throw new Error(err.detail || 'Không thể bắt đầu dịch sách')
      }
    } catch (e) {
      alert('Lỗi bắt đầu dịch: ' + e.message)
      setProcessing(false)
      clearInterval(timerRef.current)
      setStep(2)
    }
  }

  // ── Start OCR ──────────────────────────────
  const startOcr = async () => {
    if (!job) return
    setProcessing(true)
    setElapsed(0)
    setStep(3)

    const start = Date.now()
    timerRef.current = setInterval(() => {
      setElapsed(((Date.now() - start) / 1000).toFixed(1))
    }, 100)

    const wsUrl = `${window.location.protocol === 'https:' ? 'wss' : 'ws'}://${window.location.host}/ws/${job.job_id}`
    const ws = new WebSocket(wsUrl)
    wsRef.current = ws

    ws.onmessage = (e) => {
      const msg = JSON.parse(e.data)
      if (msg.type === 'page_update') {
        setPageResults(prev => ({ ...prev, [msg.page.page_num]: msg.page }))
        if (msg.summary) setSummary(msg.summary)
      } else if (msg.type === 'job_update') {
        if (msg.summary) setSummary(msg.summary)
        if (['completed', 'failed', 'interrupted'].includes(msg.status)) {
          setProcessing(false)
          clearInterval(timerRef.current)
          if (msg.elapsed_time) setElapsed(msg.elapsed_time)
          setStep(4)
          if (msg.status === 'completed') {
            const url = `${window.location.origin}${window.location.pathname}#/view/${job.job_id}`
            window.history.replaceState(null, '', `#/view/${job.job_id}`)
            setShareUrl(url)
          }
        }
      }
    }

    const params = new URLSearchParams()
    params.set('mode', 'custom')
    for (const p of selectedPages) params.append('pages', p)
    if (forceMethod !== 'auto') params.set('force_method', forceMethod)
    if (extractImages) params.set('extract_images', 'true')

    try {
      const res = await fetch(`${API}/api/ocr/${job.job_id}?${params}`, { method: 'POST', credentials: 'include' })
      if (!res.ok) {
        const err = await res.json()
        throw new Error(err.detail || 'OCR failed')
      }
    } catch (e) {
      alert('Lỗi OCR: ' + e.message)
      setProcessing(false)
      clearInterval(timerRef.current)
      setStep(2)
    }
  }

  // ── Download Helpers ───────────────────────
  const downloadOcr = (format) => {
    if (!job) return
    window.open(`${API}/api/download/${job.job_id}?format=${format}`, '_blank')
  }

  const copyText = (text) => {
    navigator.clipboard.writeText(text)
    setCopied(true)
    setTimeout(() => setCopied(false), 1800)
  }

  const resetAll = () => {
    setStep(1)
    setJob(null)
    setAnalysis(null)
    setSelectedPages(new Set())
    setPageResults({})
    setSummary(null)
    setTranslationResult(null)
    setJobGlossary({})
    setElapsed(0)
    setPreviewPage(null)
    setShareUrl(null)
    setIsSharedView(false)
    setShowDashboard(false)
    window.history.replaceState(null, '', window.location.pathname)
  }

  // ── Clean up on unmount ────────────────────
  useEffect(() => {
    return () => {
      if (wsRef.current) wsRef.current.close()
      if (timerRef.current) clearInterval(timerRef.current)
    }
  }, [])

  // ── Computed ───────────────────────────────
  const completedCount = appMode === 'translate'
    ? translationProgress.current
    : (summary?.completed || 0)
  const totalCount = appMode === 'translate'
    ? (translationProgress.total || selectedPages.size || 1)
    : (summary?.total || selectedPages.size || 1)
  const progressPercent = totalCount > 0 ? Math.min(100, (completedCount / totalCount) * 100) : 0
  const resultPages = [...selectedPages].sort((a, b) => a - b).filter(n => pageResults[n] && pageResults[n].method !== 'skipped')
  const currentResult = previewPage ? pageResults[previewPage] : null

  // ── Loading state ──────────────────────────
  if (authChecking) {
    return (
      <div className="app" style={{ display: 'flex', alignItems: 'center', justifyContent: 'center' }}>
        <span className="spinner spinner-lg" />
      </div>
    )
  }

  // ── Login Page ─────────────────────────────
  if (!isLoggedIn) {
    return (
      <div className="app login-page">
        <div className="login-card">
          <div className="login-logo">
            <div className="logo-icon" style={{ fontSize: '2.5rem' }}>📖</div>
            <h1>Smart <span className="highlight">PDF</span> Suite</h1>
            <p className="login-subtitle">Hệ thống Dịch Sách AI & OCR Cao Cấp</p>
          </div>
          <form onSubmit={handleLogin} className="login-form">
            <div className="login-field">
              <label>👤 Tên đăng nhập</label>
              <input
                type="text" autoFocus autoComplete="username"
                value={loginUser} onChange={e => setLoginUser(e.target.value)}
                placeholder="Nhập username"
              />
            </div>
            <div className="login-field">
              <label>🔒 Mật khẩu</label>
              <input
                type="password" autoComplete="current-password"
                value={loginPass} onChange={e => setLoginPass(e.target.value)}
                placeholder="Nhập mật khẩu"
              />
            </div>
            {loginError && <div className="login-error">⚠️ {loginError}</div>}
            <button type="submit" className="btn primary login-btn" disabled={loginLoading || !loginUser || !loginPass}>
              {loginLoading ? <><span className="spinner" /> Đang xác thực...</> : 'Đăng nhập'}
            </button>
          </form>
        </div>
      </div>
    )
  }

  const currentSteps = appMode === 'translate' ? TRANSLATE_STEPS : OCR_STEPS

  return (
    <div className="app">
      {/* ── App Header ────────────────────── */}
      <header className="app-header">
        <div className="logo" style={{ cursor: 'pointer' }} onClick={resetAll}>
          <div className="logo-icon">{appMode === 'translate' ? '📖' : '⚡'}</div>
          <span>Smart <span className="highlight">PDF</span> <span style={{ fontSize: 11, background: 'var(--accent-glow)', padding: '2px 6px', borderRadius: 4, marginLeft: 4 }}>v2.0</span></span>
        </div>

        {/* Navigation Mode Switcher */}
        <div className="nav-mode-tabs">
          <button
            className={`nav-tab ${appMode === 'translate' && !showDashboard ? 'active' : ''}`}
            onClick={() => { setAppMode('translate'); setShowDashboard(false) }}
          >
            <span>📖</span> Dịch Sách AI
          </button>
          <button
            className={`nav-tab ${appMode === 'ocr' && !showDashboard ? 'active' : ''}`}
            onClick={() => { setAppMode('ocr'); setShowDashboard(false) }}
          >
            <span>⚡</span> Smart OCR
          </button>
          <button
            className={`nav-tab ${showDashboard ? 'active' : ''}`}
            onClick={() => { setShowDashboard(true); window.location.hash = '#/dashboard' }}
          >
            <span>📊</span> Dự Án
          </button>
        </div>

        <div className="header-info">
          {job && (
            <>
              <div className="header-stat">
                <span className="icon">📁</span>
                <span style={{ maxWidth: 160, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }} title={job.filename}>
                  {job.filename}
                </span>
              </div>
              <div className="header-stat">
                <span className="icon">📃</span>
                <span>{analysis?.total_pages} trang</span>
              </div>
              {elapsed > 0 && (
                <div className="header-stat">
                  <span className="icon">⏱</span>
                  <span>{elapsed}s</span>
                </div>
              )}
              <button className="btn ghost" onClick={resetAll} title="Tải file mới">🔄</button>
            </>
          )}
          <span className="header-user" title={currentUser}>👤 {currentUser}</span>
          <button className="btn ghost" onClick={handleLogout} title="Đăng xuất">🚪</button>
        </div>
      </header>

      {/* ── Dashboard Route ───────────────── */}
      {showDashboard ? (
        <div className="main-content">
          <div className="dashboard">
            <div className="dashboard-header">
              <h2>📚 Lịch sử & Dự án PDF</h2>
              <button className="btn primary" onClick={resetAll}>➕ Bắt đầu Tài liệu mới</button>
            </div>
            {dashboardLoading ? (
              <div className="upload-step" style={{ textAlign: 'center', padding: '4rem' }}>
                <span className="spinner spinner-lg" />
                <p style={{ marginTop: '1rem', opacity: 0.7 }}>Đang nạp dữ liệu...</p>
              </div>
            ) : dashboardJobs.length === 0 ? (
              <div className="upload-step" style={{ textAlign: 'center', padding: '4rem' }}>
                <div style={{ fontSize: '3rem', marginBottom: '1rem' }}>📭</div>
                <p style={{ opacity: 0.7 }}>Chưa có dự án nào được xử lý</p>
              </div>
            ) : (
              <div className="dashboard-grid">
                {dashboardJobs.map(j => (
                  <div key={j.job_id} className="dashboard-card">
                    <div className="dashboard-card-header">
                      <span className="dashboard-filename">📄 {j.filename}</span>
                      <span className={`badge ${j.status === 'completed' ? 'success' : j.status === 'processing' ? 'warning' : 'error'}`}>
                        {j.status === 'completed' ? '✅ Hoàn thành' : j.status === 'processing' ? '⏳ Đang xử lý' : '❌ Lỗi'}
                      </span>
                    </div>
                    <div className="dashboard-card-meta">
                      <span>📃 {j.total_pages || 0} trang</span>
                      {j.elapsed_time > 0 && <span>⏱ {j.elapsed_time}s</span>}
                      <span>📅 {new Date(j.created_at * 1000).toLocaleDateString('vi-VN')}</span>
                    </div>
                    <div className="dashboard-card-actions">
                      <button className="btn primary small" onClick={() => { window.location.hash = `#/view/${j.job_id}` }}>👁 Xem OCR</button>
                      <button className="btn ghost small" onClick={() => window.open(`${API}/api/translate/${j.job_id}/download`, '_blank')}>📥 PDF Dịch</button>
                    </div>
                  </div>
                ))}
              </div>
            )}
          </div>
        </div>
      ) : (
        <>
          {/* ── Stepper ────────────────────── */}
          {job && (
            <div className="stepper">
              {currentSteps.map((s, i) => (
                <div key={s.num} style={{ display: 'flex', alignItems: 'center' }}>
                  <div className={`step-item ${step === s.num ? 'active' : ''} ${step > s.num ? 'completed' : ''}`}>
                    <div className="step-num">{step > s.num ? '✓' : s.num}</div>
                    <span>{s.label}</span>
                  </div>
                  {i < currentSteps.length - 1 && (
                    <div className={`step-connector ${step > s.num ? 'active' : ''}`} />
                  )}
                </div>
              ))}
            </div>
          )}

          {/* ── Main Content Area ──────────── */}
          <div className="main-content">

            {/* ── Step 1: Upload ──────────── */}
            {step === 1 && (
              <div className="upload-step">
                <div
                  className={`upload-card ${dragOver ? 'drag-over' : ''}`}
                  onClick={() => fileInput.current?.click()}
                  onDragOver={(e) => { e.preventDefault(); setDragOver(true) }}
                  onDragLeave={() => setDragOver(false)}
                  onDrop={onDrop}
                >
                  <div className="upload-icon">
                    {uploading ? <span className="spinner spinner-lg" /> : appMode === 'translate' ? '📖' : '📄'}
                  </div>
                  <h3>{uploading ? 'Đang tải lên & phân tích PDF...' : appMode === 'translate' ? 'Kéo thả Sách / Giáo trình PDF vào đây' : 'Kéo thả file PDF cần OCR vào đây'}</h3>
                  <p>hoặc nhấn để duyệt file từ máy tính</p>
                  
                  {appMode === 'translate' ? (
                    <div className="upload-hint" style={{ marginTop: 14 }}>
                      ✨ <strong>AegisTrans Engine:</strong> Giữ 100% hình ảnh & bảng biểu · TrueType Unicode chống tràn lề · Pass 1 tự khai phá thuật ngữ chuyên ngành
                    </div>
                  ) : (
                    <div className="upload-hint" style={{ marginTop: 14 }}>
                      Hỗ trợ PDF tối đa 500 trang · Phân loại thông minh Digital vs Scan vs Vision AI
                    </div>
                  )}

                  <input
                    ref={fileInput}
                    type="file"
                    accept=".pdf"
                    style={{ display: 'none' }}
                    onChange={(e) => handleUpload(e.target.files[0])}
                  />
                </div>
              </div>
            )}

            {/* ── Step 2: Configuration ───── */}
            {step === 2 && analysis && (
              <div className="select-step" style={{ padding: '24px 32px', overflowY: 'auto' }}>
                {appMode === 'translate' ? (
                  /* ── BOOK TRANSLATION CONFIGURATION ── */
                  <div style={{ maxWidth: 960, margin: '0 auto', display: 'flex', flexDirection: 'column', gap: 24 }}>
                    
                    {/* Mode Selection Cards */}
                    <div>
                      <h3 style={{ fontSize: 16, fontWeight: 700, marginBottom: 4 }}>1. Chọn Chế độ Dàn Trang (Layout Preservation)</h3>
                      <p style={{ fontSize: 13, color: 'var(--text-secondary)' }}>Quyết định cách hệ thống dàn văn bản tiếng Việt lên trang sách.</p>
                      
                      <div className="layout-cards-grid">
                        <div
                          className={`layout-card ${translateMode === 'inplace' ? 'selected' : ''}`}
                          onClick={() => setTranslateMode('inplace')}
                        >
                          <span className="layout-card-badge">Được khuyên dùng</span>
                          <div className="layout-card-header">
                            <span className="layout-card-icon">📄</span>
                            <span className="layout-card-title">Thay Thế Trực Tiếp (In-Place)</span>
                          </div>
                          <p className="layout-card-desc">
                            Giữ nguyên 100% hình ảnh, đồ họa vector và bảng biểu. Xóa văn bản gốc và chèn bản dịch tiếng Việt vào đúng tọa độ ban đầu với thuật ngữ song ngữ.
                          </p>
                        </div>

                        <div
                          className={`layout-card ${translateMode === 'bilingual_dual' ? 'selected' : ''}`}
                          onClick={() => setTranslateMode('bilingual_dual')}
                        >
                          <div className="layout-card-header">
                            <span className="layout-card-icon">📑</span>
                            <span className="layout-card-title">Trang Song Ngữ Liền Kề (Bilingual)</span>
                          </div>
                          <p className="layout-card-desc">
                            Nhân bản mỗi trang: trang bên trái giữ nguyên tiếng Anh gốc, trang bên phải là bản dịch tiếng Việt đã dàn trang hoàn chỉnh để dễ đối chiếu.
                          </p>
                        </div>
                      </div>
                    </div>

                    {/* Domain Glossary Profiles */}
                    <div>
                      <h3 style={{ fontSize: 16, fontWeight: 700, marginBottom: 4 }}>2. Chọn Hồ Sơ Thuật Ngữ Chuyên Ngành (Domain Glossary)</h3>
                      <p style={{ fontSize: 13, color: 'var(--text-secondary)' }}>Kích hoạt bộ từ điển chuẩn học thuật và quy tắc song ngữ `Thuật ngữ (English term)`.</p>
                      
                      <div className="profile-pills-wrap">
                        {[
                          { id: 'medical', name: 'Y khoa Lâm sàng', icon: '🩺', desc: 'MeSH, UMLS, ICD-10, Giải phẫu & Bệnh học' },
                          { id: 'dental', name: 'Nha khoa & Khớp TMD', icon: '🦷', desc: 'Khớp thái dương hàm, Cắn khớp, Implant, Nha chu' },
                          { id: 'tech', name: 'Kỹ thuật & Công nghệ', icon: '💻', desc: 'Kiến trúc hệ thống, Microservices, Cloud, AI' },
                          { id: 'general', name: 'Học thuật Tổng quát', icon: '🌐', desc: 'Giáo trình đại học, Kinh tế, Xã hội học' },
                        ].map(p => (
                          <div
                            key={p.id}
                            className={`profile-pill ${glossaryProfile === p.id ? 'active' : ''}`}
                            onClick={() => setGlossaryProfile(p.id)}
                          >
                            <span className="profile-pill-icon">{p.icon}</span>
                            <div className="profile-pill-info">
                              <span className="profile-pill-name">{p.name}</span>
                              <span className="profile-pill-desc">{p.desc}</span>
                            </div>
                          </div>
                        ))}
                      </div>
                    </div>

                    {/* Model & Advanced Settings */}
                    <div style={{ background: 'var(--bg-card)', padding: 20, borderRadius: 14, border: '1px solid var(--border)' }}>
                      <h3 style={{ fontSize: 15, fontWeight: 700, marginBottom: 12 }}>3. Cấu hình AI Gateway & Two-Pass Mining</h3>
                      
                      <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fit, minmax(260px, 1fr))', gap: 20 }}>
                        <div>
                          <label style={{ fontSize: 13, color: 'var(--text-secondary)', display: 'block', marginBottom: 6 }}>Model Dịch Thuật (9router AI Gateway):</label>
                          <select
                            value={selectedModel}
                            onChange={(e) => setSelectedModel(e.target.value)}
                            style={{ width: '100%', padding: '10px 12px', borderRadius: 8, background: 'var(--bg-secondary)', color: 'var(--text-primary)', border: '1px solid var(--border)' }}
                          >
                            {availableModels.map(m => (
                              <option key={m} value={m}>{m} {m === 'gpt-5.6-luna' ? '(Khuyên dùng)' : ''}</option>
                            ))}
                          </select>
                        </div>

                        <div style={{ display: 'flex', alignItems: 'center' }}>
                          <label className="toggle-switch" style={{ cursor: 'pointer' }}>
                            <input
                              type="checkbox"
                              checked={autoMineGlossary}
                              onChange={(e) => setAutoMineGlossary(e.target.checked)}
                            />
                            <span className="slider" />
                            <span className="toggle-label" style={{ fontSize: 13, marginLeft: 10, lineHeight: 1.4 }}>
                              <strong>Pass 1 Auto-Term Mining:</strong> Tự động quét Mục lục (TOC) & Index để học thuật ngữ riêng của cuốn sách trước khi dịch
                            </span>
                          </label>
                        </div>
                      </div>
                    </div>

                    {/* Page Range Selection */}
                    <div style={{ background: 'var(--bg-card)', padding: 20, borderRadius: 14, border: '1px solid var(--border)' }}>
                      <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', marginBottom: 12 }}>
                        <h3 style={{ fontSize: 15, fontWeight: 700 }}>4. Chọn Phạm Vi Trang Cần Dịch</h3>
                        <div style={{ display: 'flex', gap: 8 }}>
                          <button
                            className={`btn small ${rangeMode === 'all' ? 'primary' : ''}`}
                            onClick={() => { setRangeMode('all'); selectAll() }}
                          >
                            Toàn bộ sách ({analysis.total_pages} trang)
                          </button>
                          <button
                            className={`btn small ${rangeMode === 'custom' ? 'primary' : ''}`}
                            onClick={() => setRangeMode('custom')}
                          >
                            Tùy chọn trang
                          </button>
                        </div>
                      </div>

                      {rangeMode === 'custom' && (
                        <div style={{ marginBottom: 16 }}>
                          <input
                            type="text"
                            value={customRangeInput}
                            onChange={(e) => handleCustomRangeChange(e.target.value)}
                            placeholder="Nhập dải trang, ví dụ: 1-5, 10, 15-20"
                            style={{ width: '100%', padding: '10px 14px', borderRadius: 8, background: 'var(--bg-secondary)', color: 'var(--text-primary)', border: '1px solid var(--border)', fontSize: 13 }}
                          />
                          <p style={{ fontSize: 12, color: 'var(--text-muted)', marginTop: 6 }}>
                            Đã chọn <strong>{selectedPages.size}</strong> trên tổng số <strong>{analysis.total_pages}</strong> trang. Bạn cũng có thể click trực tiếp vào từng trang bên dưới:
                          </p>
                        </div>
                      )}

                      {/* Mini Thumbnail Grid */}
                      <div style={{ maxHeight: 220, overflowY: 'auto', display: 'grid', gridTemplateColumns: 'repeat(auto-fill, minmax(90px, 1fr))', gap: 10, padding: 4 }}>
                        {analysis.pages.map(page => {
                          const isSel = selectedPages.has(page.page_num)
                          return (
                            <div
                              key={page.page_num}
                              onClick={() => togglePage(page.page_num)}
                              style={{
                                cursor: 'pointer',
                                border: isSel ? '2px solid var(--accent)' : '1px solid var(--border)',
                                borderRadius: 8,
                                overflow: 'hidden',
                                background: isSel ? 'var(--accent-glow)' : 'var(--bg-secondary)',
                                textAlign: 'center',
                                padding: 4,
                                position: 'relative'
                              }}
                            >
                              <img
                                src={`${API}/api/thumbnail/${job.job_id}/${page.page_num}?width=120`}
                                alt={`P${page.page_num}`}
                                style={{ width: '100%', height: 90, objectFit: 'cover', borderRadius: 4 }}
                                loading="lazy"
                              />
                              <div style={{ fontSize: 11, fontWeight: 600, marginTop: 2, color: isSel ? '#fff' : 'var(--text-secondary)' }}>
                                {isSel ? '✓ ' : ''}Trang {page.page_num}
                              </div>
                            </div>
                          )
                        })}
                      </div>
                    </div>

                    {/* Launch Button */}
                    <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'flex-end', gap: 16, paddingTop: 10 }}>
                      <span style={{ fontSize: 14, color: 'var(--text-secondary)' }}>
                        Tổng số trang dịch: <strong>{selectedPages.size} trang</strong>
                      </span>
                      <button
                        className="btn primary"
                        style={{ padding: '12px 28px', fontSize: 15, fontWeight: 700 }}
                        onClick={startTranslation}
                        disabled={selectedPages.size === 0}
                      >
                        🚀 Bắt đầu Dịch Sách AI ({selectedPages.size} trang)
                      </button>
                    </div>

                  </div>
                ) : (
                  /* ── ORIGINAL OCR CONFIGURATION ── */
                  <>
                    <div className="select-toolbar">
                      <div className="toolbar-group">
                        <span className="toolbar-label">Chọn:</span>
                        <button className="btn" onClick={selectAll}>Tất cả</button>
                        <button className="btn" onClick={selectNone}>Bỏ chọn</button>
                      </div>
                      <div className="toolbar-separator" />
                      <div className="toolbar-group">
                        <span className="toolbar-label">Engine:</span>
                        <select value={forceMethod} onChange={(e) => setForceMethod(e.target.value)}>
                          <option value="auto">🤖 Auto</option>
                          <option value="tesseract">📝 Tesseract</option>
                          <option value="vision">👁 Vision AI</option>
                        </select>
                      </div>
                      <div className="toolbar-spacer" />
                      <span className="page-count-badge">{selectedPages.size}/{analysis.total_pages} trang</span>
                      <button className="btn primary" onClick={startOcr} disabled={selectedPages.size === 0}>
                        ▶ Bắt đầu OCR
                      </button>
                    </div>

                    <div className="pages-grid-wrapper">
                      <div className="pages-grid">
                        {analysis.pages.map(page => {
                          const isSelected = selectedPages.has(page.page_num)
                          return (
                            <div
                              key={page.page_num}
                              className={`page-card ${isSelected ? 'selected' : ''}`}
                              onClick={() => togglePage(page.page_num)}
                            >
                              <img
                                src={`${API}/api/thumbnail/${job.job_id}/${page.page_num}?width=200`}
                                alt={`Page ${page.page_num}`}
                                loading="lazy"
                              />
                              <div className="card-overlay" />
                              <div className="card-check">{isSelected ? '✓' : ''}</div>
                              <span className="card-num">Trang {page.page_num}</span>
                            </div>
                          )
                        })}
                      </div>
                    </div>
                  </>
                )}
              </div>
            )}

            {/* ── Step 3: Live Processing ─── */}
            {step === 3 && (
              <div className="processing-step" style={{ maxWidth: 840, margin: '2rem auto', width: '100%' }}>
                {appMode === 'translate' ? (
                  /* ── Book Translation Live Progress ── */
                  <div>
                    {/* Visual Phase Tracker */}
                    <div className="phase-tracker">
                      <div className={`phase-node ${progressPercent === 0 ? 'active' : 'done'}`}>
                        <span className="phase-node-icon">🔍</span>
                        <span className="phase-node-title">Pass 1: Term Mining</span>
                        <span className="phase-node-sub">Học biệt ngữ từ sách</span>
                      </div>
                      <div className={`phase-node ${progressPercent > 0 && progressPercent < 40 ? 'active' : progressPercent >= 40 ? 'done' : ''}`}>
                        <span className="phase-node-icon">📐</span>
                        <span className="phase-node-title">Phân tích Layout</span>
                        <span className="phase-node-sub">Cột & Font Weights</span>
                      </div>
                      <div className={`phase-node ${progressPercent >= 40 && progressPercent < 90 ? 'active' : progressPercent >= 90 ? 'done' : ''}`}>
                        <span className="phase-node-icon">✍️</span>
                        <span className="phase-node-title">Typesetting Chống Tràn</span>
                        <span className="phase-node-sub">Dàn trang Unicode UTF-8</span>
                      </div>
                      <div className={`phase-node ${progressPercent >= 90 ? 'active' : ''}`}>
                        <span className="phase-node-icon">🔖</span>
                        <span className="phase-node-title">Đóng gói PDF</span>
                        <span className="phase-node-sub">Tái lập Mục lục Bookmarks</span>
                      </div>
                    </div>

                    {/* Progress Header */}
                    <div className="progress-header" style={{ marginBottom: 16 }}>
                      <div className="progress-title">
                        <h3>
                          <span className="spinner" style={{ marginRight: 10 }} />
                          {translationProgress.message || 'Đang xử lý dịch sách...'}
                        </h3>
                        <span className="timer">⏱ {elapsed}s</span>
                      </div>
                      <div className="progress-track" style={{ height: 10 }}>
                        <div
                          className="progress-fill active"
                          style={{ width: `${Math.max(5, progressPercent)}%` }}
                        />
                      </div>
                      <div className="progress-stats">
                        <span>Đã xử lý: {completedCount}/{totalCount} trang ({Math.round(progressPercent)}%)</span>
                        <span>Model: {selectedModel} · Profile: {glossaryProfile}</span>
                      </div>
                    </div>

                    {/* Terminal Window */}
                    <div className="translate-terminal">
                      <div className="terminal-header">
                        <span>● ● ● TIẾN TRÌNH TRỰC TIẾP (AEGISTRANS WORKFLOW)</span>
                        <span>WEBSOCKET LIVE</span>
                      </div>
                      <div style={{ display: 'flex', flexDirection: 'column', gap: 6 }}>
                        <div>[Init] Kết nối 9router AI Gateway thành công. Model: {selectedModel}</div>
                        <div>[Layout] Khởi tạo các TrueType Fonts: Regular (DejaVuSans), Bold (DejaVuSans-Bold), Italic (LiberationSans-Italic).</div>
                        {glossaryProfile && <div>[Glossary] Nạp hồ sơ chuyên ngành: <strong>{glossaryProfile.toUpperCase()}</strong>. Tự động áp dụng quy tắc song ngữ `Thuật ngữ (English term)`.</div>}
                        <div>[Worker] {translationProgress.message}</div>
                        <div style={{ color: 'var(--accent-light)', marginTop: 8 }}>
                          ⏳ Đang dịch và dàn trang theo thời gian thực... Vui lòng không đóng cửa sổ này.
                        </div>
                      </div>
                    </div>
                  </div>
                ) : (
                  /* ── Standard OCR Live Progress ── */
                  <div className="progress-header">
                    <div className="progress-title">
                      <h3><span className="spinner" style={{ marginRight: 10 }} />Đang xử lý OCR...</h3>
                      <span className="timer">⏱ {elapsed}s</span>
                    </div>
                    <div className="progress-track">
                      <div className="progress-fill active" style={{ width: `${progressPercent}%` }} />
                    </div>
                    <div className="progress-stats">
                      <span>{completedCount}/{totalCount} trang</span>
                    </div>
                  </div>
                )}
              </div>
            )}

            {/* ── Step 4: Results & Preview ─ */}
            {step === 4 && (
              <div className="results-step" style={{ padding: '20px 32px', overflowY: 'auto' }}>
                {appMode === 'translate' && translationResult ? (
                  /* ── BOOK TRANSLATION RESULTS & PDF PREVIEW ── */
                  <div style={{ maxWidth: 1100, margin: '0 auto', display: 'flex', flexDirection: 'column', gap: 20 }}>
                    
                    {/* Celebration Header */}
                    <div style={{
                      background: 'linear-gradient(135deg, rgba(99, 102, 241, 0.15), rgba(52, 211, 153, 0.15))',
                      border: '1px solid var(--green-border)',
                      borderRadius: 16,
                      padding: '24px 32px',
                      display: 'flex',
                      alignItems: 'center',
                      justifyContent: 'space-between',
                      flexWrap: 'wrap',
                      gap: 20
                    }}>
                      <div>
                        <div style={{ display: 'flex', alignItems: 'center', gap: 10 }}>
                          <span style={{ fontSize: 28 }}>🎉</span>
                          <h2 style={{ fontSize: 20, fontWeight: 800 }}>Dịch Sách & Dàn Trang Hoàn Tất!</h2>
                        </div>
                        <p style={{ fontSize: 13, color: 'var(--text-secondary)', marginTop: 6 }}>
                          Đã giữ nguyên 100% hình ảnh, đồ họa vector và căn lề. Tái lập thành công bookmark mục lục PDF.
                        </p>
                        <div style={{ display: 'flex', gap: 18, marginTop: 12, fontSize: 13 }}>
                          <span>⏱ Thời gian: <strong>{translationResult.time_taken}s</strong></span>
                          <span>📃 Số trang: <strong>{translationResult.translated_pages} / {translationResult.total_pages}</strong></span>
                          <span>✍️ Khối văn bản dịch: <strong>{translationResult.translated_blocks}</strong></span>
                          <span>🏷️ Thuật ngữ chuyên sâu: <strong>{Object.keys(jobGlossary).length} từ</strong></span>
                        </div>
                      </div>

                      <div style={{ display: 'flex', gap: 12 }}>
                        <a
                          className="btn primary"
                          style={{ padding: '12px 24px', fontSize: 14, fontWeight: 700, textDecoration: 'none' }}
                          href={`${API}/api/translate/${job.job_id}/download`}
                          download
                        >
                          📥 Tải Xuống File PDF Đã Dịch
                        </a>
                        <button className="btn" onClick={resetAll}>🔄 Dịch File Khác</button>
                      </div>
                    </div>

                    {/* Sub-tabs: Inline PDF Preview vs Terminology Table */}
                    <div style={{ display: 'flex', alignItems: 'center', gap: 12, borderBottom: '1px solid var(--border)', paddingBottom: 10 }}>
                      <button
                        className={`btn ${resultTab === 'preview' ? 'primary' : 'ghost'}`}
                        onClick={() => setResultTab('preview')}
                      >
                        👁️ Xem Trước Trực Tiếp (In-Browser PDF Preview)
                      </button>
                      <button
                        className={`btn ${resultTab === 'glossary' ? 'primary' : 'ghost'}`}
                        onClick={() => setResultTab('glossary')}
                      >
                        📖 Bảng Thuật Ngữ Đã Áp Dụng ({Object.keys(jobGlossary).length})
                      </button>
                    </div>

                    {resultTab === 'preview' ? (
                      /* Embedded PDF Preview Box */
                      <div>
                        <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', marginBottom: 8 }}>
                          <span style={{ fontSize: 13, color: 'var(--text-secondary)' }}>
                            Trình xem PDF tích hợp (Bạn có thể cuộn, phóng to và kiểm tra bố cục chữ đè hình):
                          </span>
                          <a
                            href={`${API}/api/translate/${job.job_id}/download?inline=true`}
                            target="_blank"
                            rel="noreferrer"
                            style={{ fontSize: 12, color: 'var(--accent-light)', textDecoration: 'none' }}
                          >
                            ↗ Mở toàn màn hình trong tab mới
                          </a>
                        </div>
                        <div className="pdf-preview-box">
                          <iframe
                            className="pdf-embed-frame"
                            src={`${API}/api/translate/${job.job_id}/download?inline=true`}
                            title="Bản dịch PDF trực tiếp"
                          />
                        </div>
                      </div>
                    ) : (
                      /* Terminology Table */
                      <div className="applied-glossary-box">
                        <table className="glossary-table">
                          <thead>
                            <tr>
                              <th>Thuật ngữ Tiếng Anh (Source Term)</th>
                              <th>Bản dịch Chuẩn Tiếng Việt (Standard Translation)</th>
                              <th>Nguồn Phân Loại</th>
                            </tr>
                          </thead>
                          <tbody>
                            {Object.entries(jobGlossary).length > 0 ? (
                              Object.entries(jobGlossary).map(([en, vi]) => (
                                <tr key={en}>
                                  <td className="term-en">{en}</td>
                                  <td className="term-vi">{vi}</td>
                                  <td><span style={{ fontSize: 11, background: 'var(--bg-glass)', padding: '2px 8px', borderRadius: 6 }}>Pass 1 Term Mining</span></td>
                                </tr>
                              ))
                            ) : (
                              <tr>
                                <td colSpan={3} style={{ textAlign: 'center', padding: 24, color: 'var(--text-muted)' }}>
                                  Đã dịch theo cơ sở thuật ngữ nền của profile {glossaryProfile.toUpperCase()} và ngữ cảnh học thuật.
                                </td>
                              </tr>
                            )}
                          </tbody>
                        </table>
                      </div>
                    )}

                  </div>
                ) : (
                  /* ── ORIGINAL OCR RESULTS VIEW ── */
                  <div className="results-toolbar">
                    <button className="btn" onClick={() => downloadOcr('html')}>📥 HTML</button>
                    <button className="btn" onClick={() => downloadOcr('text')}>📥 TXT</button>
                    <button className="btn" onClick={() => downloadOcr('markdown')}>📥 MD</button>
                    <button className="btn" onClick={resetAll}>↩ File mới</button>
                  </div>
                )}
              </div>
            )}

          </div>

          {/* Toast */}
          {copied && <div className="copied-toast">✓ Đã copy vào clipboard!</div>}

          {/* Lightbox */}
          {lightboxUrl && (
            <div className="lightbox-overlay" onClick={() => setLightboxUrl(null)}>
              <div className="lightbox-close">&times;</div>
              <div className="lightbox-content" onClick={(e) => e.stopPropagation()}>
                <img src={lightboxUrl} alt="Phóng to ảnh" className="lightbox-image" />
              </div>
            </div>
          )}
        </>
      )}
    </div>
  )
}
