interface EyeMarkProps {
  size?: 'small' | 'medium' | 'large'
  className?: string
}

export function EyeMark({ size = 'medium', className = '' }: EyeMarkProps) {
  return <span className={`eye-mark ${size} ${className}`} aria-hidden="true">
    <svg viewBox="0 0 64 64" focusable="false">
      <defs>
        <linearGradient id="eye-flame" x1="10" y1="12" x2="54" y2="52" gradientUnits="userSpaceOnUse">
          <stop stopColor="#ffd38a" />
          <stop offset="0.48" stopColor="#e57732" />
          <stop offset="1" stopColor="#8f2f1f" />
        </linearGradient>
      </defs>
      <path className="eye-orbit" d="M5 32C13 18 22 12 32 12s19 6 27 20c-8 14-17 20-27 20S13 46 5 32Z" />
      <path className="eye-lid" d="M8 32c7-9 15-14 24-14s17 5 24 14c-7 9-15 14-24 14S15 41 8 32Z" />
      <ellipse className="eye-iris" cx="32" cy="32" rx="10" ry="15" />
      <ellipse className="eye-pupil" cx="32" cy="32" rx="3.4" ry="11" />
      <circle className="argus-dot dot-one" cx="11" cy="18" r="2" />
      <circle className="argus-dot dot-two" cx="53" cy="18" r="2" />
      <circle className="argus-dot dot-three" cx="32" cy="6" r="1.7" />
    </svg>
  </span>
}

export function Brand({ compact = false }: { compact?: boolean }) {
  return <div className="brand">
    <EyeMark size={compact ? 'small' : 'medium'} />
    <div className="brand-copy">
      <strong>EyeOfSauron</strong>
      <span>powered by Argus</span>
    </div>
  </div>
}
