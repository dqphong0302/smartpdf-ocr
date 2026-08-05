import { useState, useEffect, useRef, useCallback } from 'react'
import DOMPurify from 'dompurify'

const API = ''  // proxy handles it

const STEPS = [
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

  // ── App state ──────
  const [step, setStep] = useState(1)
  const [job, setJob] = useState(null)
  const [analysis, setAnalysis] = useState(null)
  const [selectedPages, setSelectedPages] = useState(new Set())
  const [forceMethod, setForceMethod] = useState('vision')
  const [extractImages, setExtractImages] = useState(false)
  const [lightboxUrl, setLightboxUrl] = useState(null)
  const [pageResults, setPageResults] = useState({})
  const [processing, setProcessing] = useState(false)
  const [uploading, setUploading] = useState(false)
  const [summary, setSummary] = useState(null)
  const [elapsed, setElapsed] = useState(0)
  const [dragOver, setDragOver] = useState(false)
  const [previewPage, setPreviewPage] = useState(null)
  const [copied, setCopied] = useState(false)
  const [viewMode, setViewMode] = useState('text') // 'text' or 'html'
  const [isSharedView, setIsSharedView] = useState(false) // read-only shared view
  const [shareUrl, setShareUrl] = useState(null)
  const [showDashboard, setShowDashboard] = useState(false)
  const [dashboardJobs, setDashboardJobs] = useState([])
  const [dashboardLoading, setDashboardLoading] = useState(false)
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

  // ── Global Lightbox hook for injected HTML click events ──────
  useEffect(() => {
    window.openLightbox = (url) => {
      setLightboxUrl(url)
    }
    return () => {
      delete window.openLightbox
    }
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

  // ── Load shared view or dashboard from URL hash ────────
  useEffect(() => {
    const loadFromHash = async () => {
      const hash = window.location.hash

      // Dashboard route
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

      // Shared view route
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

        // Load page results
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

  // ── Upload ─────────────────────────────────
  const handleUpload = useCallback(async (file) => {
    if (!file || !file.name.toLowerCase().endsWith('.pdf')) return
    setUploading(true)
    setJob(null)
    setAnalysis(null)
    setPageResults({})
    setSummary(null)
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
      setStep(2) // move to page selection
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

  // ── Page Selection ─────────────────────────
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
    setSelectedPages(new Set(analysis.pages.map(p => p.page_num)))
  }
  const selectNone = () => setSelectedPages(new Set())
  const selectInvert = () => {
    if (!analysis) return
    const inverted = new Set()
    analysis.pages.forEach(p => {
      if (!selectedPages.has(p.page_num)) inverted.add(p.page_num)
    })
    setSelectedPages(inverted)
  }
  const selectScanned = () => {
    if (!analysis) return
    setSelectedPages(new Set(
      analysis.pages.filter(p => p.classification !== 'digital').map(p => p.page_num)
    ))
  }

  // ── Start OCR ──────────────────────────────
  const startOcr = async () => {
    if (!job) return
    setProcessing(true)
    setElapsed(0)
    setStep(3) // move to processing step

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
        if (msg.status === 'completed' || msg.status === 'failed') {
          setProcessing(false)
          clearInterval(timerRef.current)
          if (msg.elapsed_time) setElapsed(msg.elapsed_time)
          setStep(4) // move to results
          // Update URL for sharing
          if (msg.status === 'completed') {
            const url = `${window.location.origin}${window.location.pathname}#/view/${job.job_id}`
            window.history.replaceState(null, '', `#/view/${job.job_id}`)
            setShareUrl(url)
          }
        }
      } else if (msg.type === 'init') {
        if (msg.job && msg.job.pages) {
          const pr = {}
          for (const [num, page] of Object.entries(msg.job.pages)) {
            pr[parseInt(num)] = page
          }
          setPageResults(pr)
          if (msg.job.summary) setSummary(msg.job.summary)
        }
      }
    }

    ws.onclose = () => {
      setProcessing(false)
      clearInterval(timerRef.current)
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

  // ── Download ───────────────────────────────
  const download = (format) => {
    if (!job) return
    window.open(`${API}/api/download/${job.job_id}?format=${format}`, '_blank')
  }

  // ── Copy to clipboard ─────────────────────
  const copyText = (text) => {
    navigator.clipboard.writeText(text)
    setCopied(true)
    setTimeout(() => setCopied(false), 1800)
  }

  // ── Set initial preview page when results arrive ───
  useEffect(() => {
    if (step === 4 && previewPage === null) {
      const pages = [...selectedPages].sort((a, b) => a - b)
      const first = pages.find(n => pageResults[n] && pageResults[n].text)
      if (first) setPreviewPage(first)
      else if (pages.length > 0) setPreviewPage(pages[0])
    }
  }, [step, selectedPages, pageResults, previewPage])

  // ── Cleanup ────────────────────────────────
  useEffect(() => {
    return () => {
      if (wsRef.current) wsRef.current.close()
      if (timerRef.current) clearInterval(timerRef.current)
    }
  }, [])

  // ── New file ───────────────────────────────
  const resetAll = () => {
    setStep(1)
    setJob(null)
    setAnalysis(null)
    setSelectedPages(new Set())
    setPageResults({})
    setSummary(null)
    setElapsed(0)
    setPreviewPage(null)
    setShareUrl(null)
    setIsSharedView(false)
    setShowDashboard(false)
    window.history.replaceState(null, '', window.location.pathname)
  }

  // ── Dashboard helpers ─────────────────────
  const loadDashboard = async () => {
    window.location.hash = '#/dashboard'
  }

  const deleteJob = async (jobId) => {
    if (!confirm('Xóa kết quả OCR này?')) return
    try {
      await fetch(`${API}/api/jobs/${jobId}`, { method: 'DELETE', credentials: 'include' })
      setDashboardJobs(prev => prev.filter(j => j.job_id !== jobId))
    } catch { }
  }

  const formatTime = (ts) => {
    if (!ts) return '—'
    return new Date(ts * 1000).toLocaleString('vi-VN', { day: '2-digit', month: '2-digit', year: 'numeric', hour: '2-digit', minute: '2-digit' })
  }

  // ── Computed ───────────────────────────────
  const completedCount = summary?.completed || 0
  const totalCount = summary?.total || selectedPages.size || 0
  const progress = totalCount > 0 ? (completedCount / totalCount) * 100 : 0
  const resultPages = [...selectedPages].sort((a, b) => a - b).filter(n => pageResults[n] && pageResults[n].method !== 'skipped')
  const currentResult = previewPage ? pageResults[previewPage] : null

  // ── Auth check loading ────────────────
  if (authChecking) {
    return (
      <div className="app" style={{ display: 'flex', alignItems: 'center', justifyContent: 'center' }}>
        <span className="spinner spinner-lg" />
      </div>
    )
  }

  // ── Login page ────────────────────
  if (!isLoggedIn) {
    return (
      <div className="app login-page">
        <div className="login-card">
          <div className="login-logo">
            <div className="logo-icon" style={{ fontSize: '2.5rem' }}>📄</div>
            <h1>Smart <span className="highlight">PDF</span> OCR</h1>
            <p className="login-subtitle">Đăng nhập để sử dụng dịch vụ</p>
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

  return (
    <div className="app">
      {/* ── Header ──────────────────────── */}
      <header className="app-header">
        <div className="logo">
          <div className="logo-icon">📄</div>
          <span>Smart <span className="highlight">PDF</span> OCR</span>
        </div>
        <div className="header-info">
          {job && (
            <>
              <div className="header-stat">
                <span className="icon">📁</span>
                <span>{job.filename}</span>
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
            </>
          )}
          {job && (
            <button className="btn ghost" onClick={resetAll} title="File mới">🔄</button>
          )}
          <button className="btn ghost" onClick={loadDashboard} title="Lịch sử OCR">📋</button>
          <span className="header-user" title={currentUser}>👤 {currentUser}</span>
          <button className="btn ghost" onClick={handleLogout} title="Đăng xuất">🚪</button>
        </div>
      </header>

      {/* ── Dashboard View ──────────────── */}
      {showDashboard ? (
        <div className="main-content">
          <div className="dashboard">
            <div className="dashboard-header">
              <h2>📚 Lịch sử OCR</h2>
              <button className="btn primary" onClick={resetAll}>➕ Tải file mới</button>
            </div>
            {dashboardLoading ? (
              <div className="upload-step" style={{ textAlign: 'center', padding: '4rem' }}>
                <span className="spinner spinner-lg" />
                <p style={{ marginTop: '1rem', opacity: 0.7 }}>Đang tải...</p>
              </div>
            ) : dashboardJobs.length === 0 ? (
              <div className="upload-step" style={{ textAlign: 'center', padding: '4rem' }}>
                <div style={{ fontSize: '3rem', marginBottom: '1rem' }}>📭</div>
                <p style={{ opacity: 0.7 }}>Chưa có file nào được xử lý</p>
              </div>
            ) : (
              <div className="dashboard-grid">
                {dashboardJobs.map(j => (
                  <div key={j.job_id} className="dashboard-card">
                    <div className="dashboard-card-header">
                      <span className="dashboard-filename">📄 {j.filename}</span>
                      <span className={`badge ${j.status === 'completed' ? 'success' : j.status === 'processing' ? 'warning' : j.status === 'failed' ? 'error' : ''}`}>
                        {j.status === 'completed' ? '✅ Hoàn thành' : j.status === 'processing' ? '⏳ Đang xử lý' : j.status === 'failed' ? '❌ Lỗi' : '⏸ Chờ'}
                      </span>
                    </div>
                    <div className="dashboard-card-meta">
                      <span>📃 {j.total_pages || 0} trang</span>
                      {j.elapsed_time > 0 && <span>⏱ {j.elapsed_time}s</span>}
                      <span>📅 {formatTime(j.created_at)}</span>
                    </div>
                    {j.summary && j.status === 'completed' && (
                      <div className="dashboard-card-summary">
                        {j.summary.methods?.digital > 0 && <span className="method-chip digital">💻 {j.summary.methods.digital} digital</span>}
                        {j.summary.methods?.tesseract > 0 && <span className="method-chip tess">🔤 {j.summary.methods.tesseract} tesseract</span>}
                        {j.summary.methods?.vision > 0 && <span className="method-chip vision">🤖 {j.summary.methods.vision} vision</span>}
                        {j.summary.avg_confidence > 0 && <span className="method-chip conf">🎯 {j.summary.avg_confidence}%</span>}
                      </div>
                    )}
                    <div className="dashboard-card-actions">
                      {j.status === 'completed' && (
                        <button className="btn primary small" onClick={() => { window.location.hash = `#/view/${j.job_id}` }}>👁 Xem</button>
                      )}
                      <button className="btn ghost small" onClick={() => deleteJob(j.job_id)}>🗑 Xóa</button>
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
              {STEPS.map((s, i) => (
                <div key={s.num} style={{ display: 'flex', alignItems: 'center' }}>
                  <div className={`step-item ${step === s.num ? 'active' : ''} ${step > s.num ? 'completed' : ''}`}>
                    <div className="step-num">
                      {step > s.num ? '✓' : s.num}
                    </div>
                    <span>{s.label}</span>
                  </div>
                  {i < STEPS.length - 1 && (
                    <div className={`step-connector ${step > s.num ? 'active' : ''}`} />
                  )}
                </div>
              ))}
            </div>
          )}

          {/* ── Main Content ───────────────── */}
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
                    {uploading ? <span className="spinner spinner-lg" /> : '📄'}
                  </div>
                  <h3>{uploading ? 'Đang tải lên...' : 'Kéo thả PDF vào đây'}</h3>
                  <p>hoặc nhấn để chọn file</p>
                  <div className="upload-hint">
                    Hỗ trợ PDF tối đa 50MB · Tự động phân tích trang scan và digital
                  </div>
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

            {/* ── Step 2: Select Pages ─────── */}
            {step === 2 && analysis && (
              <div className="select-step">
                {/* Analysis Summary */}
                <div className="analysis-bar">
                  <div className="analysis-card">
                    <div className="a-icon green">🟢</div>
                    <div>
                      <div className="a-label">Digital</div>
                      <div className="a-value">{analysis.summary.digital || 0}</div>
                      <div className="a-desc">Có sẵn text · miễn phí</div>
                    </div>
                  </div>
                  <div className="analysis-card">
                    <div className="a-icon yellow">🟡</div>
                    <div>
                      <div className="a-label">Scan đơn giản</div>
                      <div className="a-value">{analysis.summary.scan_simple || 0}</div>
                      <div className="a-desc">Tesseract · miễn phí</div>
                    </div>
                  </div>
                  <div className="analysis-card">
                    <div className="a-icon red">🔴</div>
                    <div>
                      <div className="a-label">Scan phức tạp</div>
                      <div className="a-value">{analysis.summary.scan_complex || 0}</div>
                      <div className="a-desc">Vision AI · tốn phí</div>
                    </div>
                  </div>
                </div>

                {/* Toolbar */}
                <div className="select-toolbar">
                  <div className="toolbar-group">
                    <span className="toolbar-label">Chọn:</span>
                    <button className="btn" onClick={selectAll}>Tất cả</button>
                    <button className="btn" onClick={selectNone}>Bỏ chọn</button>
                    <button className="btn" onClick={selectInvert}>Đảo</button>
                    <button className="btn" onClick={selectScanned}>Chỉ Scan</button>
                  </div>

                  <div className="toolbar-separator" />

                  <div className="toolbar-group">
                    <span className="toolbar-label">Engine:</span>
                    <select value={forceMethod} onChange={(e) => setForceMethod(e.target.value)}>
                      <option value="auto">🤖 Auto (Thông minh)</option>
                      <option value="tesseract">📝 Tesseract (Local)</option>
                      <option value="vision">👁 Vision AI (GPT-5.4)</option>
                    </select>
                  </div>

                  <div className="toolbar-separator" />

                  <div className="toolbar-group">
                    <label className="toggle-switch">
                      <input 
                        type="checkbox" 
                        checked={extractImages} 
                        onChange={(e) => setExtractImages(e.target.checked)} 
                      />
                      <span className="slider" />
                      <span className="toggle-label" style={{ fontSize: 13, marginLeft: 8, whiteSpace: 'nowrap' }}>🖼️ Trích xuất ảnh</span>
                    </label>
                  </div>

                  <div className="toolbar-spacer" />

                  <span className="page-count-badge">
                    {selectedPages.size}/{analysis.total_pages} trang đã chọn
                  </span>

                  <button
                    className="btn primary"
                    onClick={startOcr}
                    disabled={selectedPages.size === 0}
                  >
                    ▶ Bắt đầu OCR
                  </button>
                </div>

                {/* Page Grid */}
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
                          <div className="card-check">
                            {isSelected ? '✓' : ''}
                          </div>
                          <span className="card-num">Trang {page.page_num}</span>
                          <span className={`card-badge ${page.classification}`}>
                            {page.classification === 'digital' ? 'TXT' :
                              page.classification === 'scan_simple' ? 'SCAN' : 'AI'}
                          </span>
                        </div>
                      )
                    })}
                  </div>
                </div>
              </div>
            )}

            {/* ── Step 3: Processing ──────── */}
            {step === 3 && (
              <div className="processing-step">
                <div className="progress-header">
                  <div className="progress-title">
                    <h3>
                      {processing ? (
                        <><span className="spinner" style={{ marginRight: 10 }} />Đang xử lý OCR...</>
                      ) : (
                        'Hoàn thành!'
                      )}
                    </h3>
                    <span className="timer">⏱ {elapsed}s</span>
                  </div>
                  <div className="progress-track">
                    <div className={`progress-fill ${processing ? 'active' : ''}`} style={{ width: `${progress}%` }} />
                  </div>
                  <div className="progress-stats">
                    <span>{completedCount}/{totalCount} trang</span>
                    <div className="method-stats">
                      {summary && (
                        <>
                          <span className="method-stat"><span className="method-dot digital" /> {summary.methods?.digital || 0} digital</span>
                          <span className="method-stat"><span className="method-dot tesseract" /> {summary.methods?.tesseract || 0} tesseract</span>
                          <span className="method-stat"><span className="method-dot vision" /> {summary.methods?.vision || 0} vision</span>
                        </>
                      )}
                    </div>
                    {summary?.avg_confidence > 0 && <span>Confidence: {summary.avg_confidence}%</span>}
                  </div>
                </div>

                <div className="processing-grid">
                  {[...selectedPages].sort((a, b) => a - b).map(num => {
                    const pr = pageResults[num]
                    const status = pr?.status || 'pending'
                    return (
                      <div key={num} className={`proc-card ${status}`}>
                        <img
                          src={`${API}/api/thumbnail/${job.job_id}/${num}?width=200`}
                          alt={`Page ${num}`}
                          loading="lazy"
                        />
                        <div className="proc-overlay">
                          {status === 'processing' && <span className="spinner spinner-lg" />}
                          {status === 'completed' && '✅'}
                          {status === 'failed' && '❌'}
                        </div>
                        <span className="proc-num">Trang {num}</span>
                        {pr?.method && pr.method !== 'skipped' && (
                          <span className={`proc-method ${pr.method}`}>{pr.method}</span>
                        )}
                      </div>
                    )
                  })}
                </div>
              </div>
            )}

            {/* ── Step 4: Results ─────────── */}
            {step === 4 && (
              <div className="results-step">
                {/* Toolbar */}
                <div className="results-toolbar">
                  <div className="page-nav">
                    <button
                      className="btn-icon"
                      onClick={() => {
                        const idx = resultPages.indexOf(previewPage)
                        if (idx > 0) setPreviewPage(resultPages[idx - 1])
                      }}
                      disabled={resultPages.indexOf(previewPage) <= 0}
                    >◀</button>
                    <span className="page-indicator">
                      Trang {previewPage || '-'} / {resultPages.length}
                    </span>
                    <button
                      className="btn-icon"
                      onClick={() => {
                        const idx = resultPages.indexOf(previewPage)
                        if (idx < resultPages.length - 1) setPreviewPage(resultPages[idx + 1])
                      }}
                      disabled={resultPages.indexOf(previewPage) >= resultPages.length - 1}
                    >▶</button>
                  </div>

                  <div className="toolbar-separator" />

                  {currentResult && (
                    <>
                      {currentResult.method && (
                        <span className={`card-badge ${currentResult.method === 'digital' ? 'digital' : currentResult.method === 'tesseract' ? 'scan_simple' : 'scan_complex'}`}
                          style={{ position: 'static' }}
                        >
                          {currentResult.method}
                        </span>
                      )}
                      {currentResult.confidence > 0 && (
                        <span className={`confidence-badge ${currentResult.confidence >= 90 ? 'confidence-high' : currentResult.confidence >= 70 ? 'confidence-med' : 'confidence-low'}`}>
                          {currentResult.confidence}%
                        </span>
                      )}
                      {currentResult.time_taken > 0 && (
                        <span style={{ fontSize: 12, color: 'var(--text-muted)' }}>
                          ⏱ {currentResult.time_taken}s
                        </span>
                      )}
                    </>
                  )}

                  <div className="toolbar-spacer" />

                  {currentResult?.text && (
                    <button className="btn" onClick={() => copyText(currentResult.text)}>
                      📋 Copy trang này
                    </button>
                  )}
                  <button className="btn" onClick={() => {
                    const allText = resultPages.map(n => {
                      const p = pageResults[n]
                      return p?.text ? `--- Trang ${n} ---\n${p.text}` : ''
                    }).filter(Boolean).join('\n\n')
                    copyText(allText)
                  }}>
                    📋 Copy tất cả
                  </button>
                  <button className="btn" onClick={() => download('html')}>📥 HTML</button>
                  <button className="btn" onClick={() => download('text')}>📥 TXT</button>
                  <button className="btn" onClick={() => download('json')}>📥 JSON</button>
                  <button className="btn" onClick={() => download('markdown')}>📥 MD</button>
                  {shareUrl && (
                    <button className="btn" onClick={() => {
                      navigator.clipboard.writeText(shareUrl)
                      setCopied(true)
                      setTimeout(() => setCopied(false), 2000)
                    }} title={shareUrl}>
                      {copied ? '✅ Đã copy!' : '🔗 Chia sẻ'}
                    </button>
                  )}
                  {!isSharedView && (
                    <button className="btn" onClick={() => setStep(2)} title="Chọn lại trang">
                      ↩ Chọn lại
                    </button>
                  )}
                </div>

                {/* Main content: Sidebar + Preview */}
                <div className="results-main">
                  {/* Page sidebar */}
                  <div className="results-sidebar">
                    {resultPages.map(num => {
                      const pr = pageResults[num]
                      return (
                        <div
                          key={num}
                          className={`result-page-item ${previewPage === num ? 'active' : ''}`}
                          onClick={() => setPreviewPage(num)}
                        >
                          <span className="rp-num">{num}</span>
                          <span>Trang {num}</span>
                          {pr?.method && (
                            <span className={`rp-method ${pr.method}`}>
                              {pr.method === 'digital' ? 'TXT' : pr.method === 'tesseract' ? 'OCR' : 'AI'}
                            </span>
                          )}
                        </div>
                      )
                    })}
                  </div>

                  {/* Preview: Image + Text side by side */}
                  <div className="results-preview">
                    <div className="preview-image">
                      {previewPage && (
                        <img
                          src={`${API}/api/thumbnail/${job.job_id}/${previewPage}?width=800`}
                          alt={`Page ${previewPage}`}
                        />
                      )}
                    </div>
                    <div className="preview-text">
                      <div className="preview-text-header">
                        <h4>Nội dung OCR — Trang {previewPage || '-'}</h4>
                        <div style={{ display: 'flex', gap: 6, alignItems: 'center' }}>
                          <button
                            className={`btn ${viewMode === 'text' ? 'active' : ''}`}
                            onClick={() => setViewMode('text')}
                            style={{ padding: '4px 10px', fontSize: 11 }}
                          >Text</button>
                          <button
                            className={`btn ${viewMode === 'html' ? 'active' : ''}`}
                            onClick={() => setViewMode('html')}
                            style={{ padding: '4px 10px', fontSize: 11 }}
                          >HTML</button>
                          {currentResult?.text && (
                            <button className="btn ghost" onClick={() => copyText(currentResult.text)} title="Copy">
                              📋
                            </button>
                          )}
                        </div>
                      </div>
                      {viewMode === 'html' && currentResult?.html_text ? (
                        <div
                          className="preview-text-content"
                          style={{ fontFamily: 'Georgia, serif', whiteSpace: 'normal' }}
                          dangerouslySetInnerHTML={{ __html: DOMPurify.sanitize(currentResult.html_text) }}
                        />
                      ) : (
                        <div className="preview-text-content">
                          {currentResult?.text || ''}
                        </div>
                      )}
                    </div>
                  </div>
                </div>

                {/* Download bar */}
                <div className="download-bar">
                  <div className="download-summary">
                    ✅ {resultPages.length} trang đã xử lý
                    {summary && (
                      <> · 🟢 {summary.methods?.digital || 0} digital · 🟡 {summary.methods?.tesseract || 0} tesseract · 🔵 {summary.methods?.vision || 0} vision</>
                    )}
                    {elapsed > 0 && <> · ⏱ {elapsed}s</>}
                    {summary?.avg_confidence > 0 && <> · Confidence: {summary.avg_confidence}%</>}
                  </div>
                </div>
              </div>
            )}
          </div>

          {/* Toast */}
          {copied && <div className="copied-toast">✓ Đã copy vào clipboard!</div>}

          {/* Lightbox Overlay */}
          {lightboxUrl && (
            <div className="lightbox-overlay" onClick={() => setLightboxUrl(null)}>
              <div className="lightbox-close">&times;</div>
              <div className="lightbox-content" onClick={(e) => e.stopPropagation()}>
                <img src={lightboxUrl} alt="Phóng to ảnh gốc" className="lightbox-image" />
              </div>
            </div>
          )}
        </>
      )}
    </div>
  )
}
