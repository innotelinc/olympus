# Application Specification: Foot Fetish Site

> Exported from Olympus Studio on 2026-09-13T16:24:06.188Z (project `BcyO3vim0UGc`).
>
> Kind: **app**.

## ▶︎ Next steps

1. Manufacture it here: `make app SPEC=build-requests/foot-fetish-site.md`
2. Or commit it (`git add build-requests/foot-fetish-site.md`) — `.github/workflows/olympus-app-builder.yml` builds it on push.
3. Package the client: `python3 scripts/package-app.py foot-fetish-site`
4. Build and run it as its own container: `python3 scripts/app-runtime.py --up foot-fetish-site --build`
5. Put it on a name: `make site-publish SLUG=foot-fetish-site` — the app's vhost is generated, so the edge needs nothing app-specific (see docs/site-publishing.md).

## 🎯 Core Purpose

make a website about feet fetishes

## 🧰 Tech Stack

- React 19 + TypeScript client, Node HTTP API, SQLite (packaged into one container per app — see `scripts/package-app.py`)
- CSS
- JavaScript / TypeScript

## 🛠️ Key Features & Pages

As built in Studio:

- **`src/App.tsx`** — the interface (754 B)
- **`src/index.css`** — styles (12.3 KB)
- **`src/components/Header.tsx`** — behaviour (2.7 KB)
- **`src/components/Hero.tsx`** — behaviour (827 B)
- **`src/components/About.tsx`** — behaviour (1.6 KB)
- **`src/components/Forms.tsx`** — behaviour (2.4 KB)
- **`src/components/Psychology.tsx`** — behaviour (1.8 KB)
- **`src/components/Consent.tsx`** — behaviour (2.0 KB)
- **`src/components/Myths.tsx`** — behaviour (1.7 KB)
- **`src/components/FAQ.tsx`** — behaviour (3.3 KB)
- **`src/components/Footer.tsx`** — behaviour (772 B)

The client's entry point is `src/App.tsx`.

## 🚦 Verification Criteria

- Open `src/App.tsx` — Studio rendered it and reported no console errors before saving.

## 🧱 Packaging & runtime

This is a full-stack application: React client, Node HTTP API, SQLite. The model
wrote `src/App.tsx` and `server/schema.sql`; `scripts/package-app.py` writes the
Vite project, the server that serves both the client and `/api/<table>`, and the
`Dockerfile`. The API is derived from the tables in the schema, so a table the
schema does not declare does not exist at runtime.

```bash
python3 scripts/package-app.py foot-fetish-site   # client build + archive
python3 scripts/app-runtime.py --up foot-fetish-site --build   # image + container
```

Each app runs as its own container with its own loopback port and its own SQLite
file under `OLYMPUS_APPS_ROOT` — see `docs/site-publishing.md`.

## 📎 Reference build (from Studio)

The files below are the build this spec came from. Treat them as reference for
features and intent rather than a structure to preserve — the factory should build
the app the spec describes.

### `src/App.tsx`

```typescript
import React from 'react';
import Header from './components/Header';
import Hero from './components/Hero';
import About from './components/About';
import Forms from './components/Forms';
import Psychology from './components/Psychology';
import Consent from './components/Consent';
import Myths from './components/Myths';
import FAQ from './components/FAQ';
import Footer from './components/Footer';

function App() {
  return (
    <>
      <a className="skip-link" href="#main">
        Skip to content
      </a>

      <Header />

      <main id="main">
        <Hero />
        <About />
        <Forms />
        <Psychology />
        <Consent />
        <Myths />
        <FAQ />
      </main>

      <Footer />
    </>
  );
}

export default App;
```

### `src/index.css`

```css
/* ===== Reset & base ===== */
*,
*::before,
*::after {
  box-sizing: border-box;
  margin: 0;
  padding: 0;
}

:root {
  --bg: #faf7f4;
  --bg-alt: #f1ebe4;
  --surface: #ffffff;
  --text: #2d2a32;
  --text-muted: #6b636b;
  --accent: #c17a5e;
  --accent-dark: #a05f47;
  --accent-soft: #f3e3db;
  --border: #e4dbd2;
  --radius: 14px;
  --shadow: 0 4px 20px rgba(45, 42, 50, 0.07);
  --font: "Segoe UI", system-ui, -apple-system, BlinkMacSystemFont, Roboto, Helvetica, Arial, sans-serif;
}

html {
  scroll-behavior: smooth;
  scroll-padding-top: 80px;
}

body {
  font-family: var(--font);
  background: var(--bg);
  color: var(--text);
  line-height: 1.7;
  -webkit-font-smoothing: antialiased;
}

img {
  max-width: 100%;
  display: block;
}

a {
  color: var(--accent-dark);
  text-decoration: none;
}

a:hover {
  text-decoration: underline;
}

h1, h2, h3 {
  line-height: 1.25;
  font-weight: 700;
}

h1 {
  font-size: clamp(2.2rem, 5vw, 3.4rem);
  letter-spacing: -0.02em;
}

h2 {
  font-size: clamp(1.7rem, 3.5vw, 2.4rem);
  letter-spacing: -0.01em;
}

h3 {
  font-size: 1.15rem;
}

p {
  margin-bottom: 1rem;
}

.container {
  width: min(1100px, 92%);
  margin-inline: auto;
}

.sr-only {
  position: absolute;
  width: 1px;
  height: 1px;
  padding: 0;
  margin: -1px;
  overflow: hidden;
  clip: rect(0, 0, 0, 0);
  white-space: nowrap;
  border: 0;
}

.skip-link {
  position: absolute;
  top: -100px;
  left: 16px;
  z-index: 100;
  background: var(--accent-dark);
  color: #fff;
  padding: 10px 18px;
  border-radius: 0 0 8px 8px;
  font-weight: 600;
  transition: top 0.2s ease;
}

.skip-link:focus {
  top: 0;
  text-decoration: none;
}

/* ===== Header ===== */
.site-header {
  position: sticky;
  top: 0;
  z-index: 50;
  background: rgba(250, 247, 244, 0.92);
  backdrop-filter: blur(10px);
  border-bottom: 1px solid var(--border);
}

.header-inner {
  display: flex;
  align-items: center;
  justify-content: space-between;
  min-height: 68px;
  gap: 16px;
}

.brand {
  display: inline-flex;
  align-items: center;
  gap: 10px;
  font-size: 1.2rem;
  font-weight: 700;
  color: var(--text);
}

.brand:hover {
  text-decoration: none;
}

.brand-mark {
  font-size: 1.5rem;
  filter: grayscale(0.2);
}

.brand-name {
  letter-spacing: -0.01em;
}

.nav-menu {
  display: flex;
  list-style: none;
  gap: 4px;
}

.nav-menu a {
  display: block;
  padding: 8px 14px;
  border-radius: 8px;
  color: var(--text);
  font-weight: 500;
  font-size: 0.95rem;
  transition: background 0.2s ease, color 0.2s ease;
}

.nav-menu a:hover {
  background: var(--accent-soft);
  color: var(--accent-dark);
  text-decoration: none;
}

.nav-toggle {
  display: none;
  flex-direction: column;
  justify-content: center;
  gap: 5px;
  width: 44px;
  height: 44px;
  padding: 10px;
  background: none;
  border: 1px solid var(--border);
  border-radius: 10px;
  cursor: pointer;
}

.nav-toggle-bar {
  display: block;
  width: 100%;
  height: 2px;
  background: var(--text);
  border-radius: 2px;
  transition: transform 0.3s ease, opacity 0.3s ease;
}

.nav-toggle[aria-expanded="true"] .nav-toggle-bar:nth-child(1) {
  transform: translateY(7px) rotate(45deg);
}

.nav-toggle[aria-expanded="true"] .nav-toggle-bar:nth-child(2) {
  opacity: 0;
}

.nav-toggle[aria-expanded="true"] .nav-toggle-bar:nth-child(3) {
  transform: translateY(-7px) rotate(-45deg);
}

/* ===== Hero ===== */
.hero {
  padding: clamp(4rem, 10vw, 7rem) 0;
  background:
    radial-gradient(ellipse at 20% 20%, rgba(193, 122, 94, 0.12), transparent 50%),
    radial-gradient(ellipse at 80% 80%, rgba(193, 122, 94, 0.08), transparent 50%),
    var(--bg);
  border-bottom: 1px solid var(--border);
}

.hero-inner {
  max-width: 760px;
  margin-inline: auto;
  text-align: center;
}

.eyebrow {
  display: inline-block;
  font-size: 0.8rem;
  font-weight: 700;
  text-transform: uppercase;
  letter-spacing: 0.14em;
  color: var(--accent-dark);
  background: var(--accent-soft);
  padding: 6px 14px;
  border-radius: 100px;
  margin-bottom: 1.2rem;
}

.hero h1 {
  margin-bottom: 1.2rem;
}

.hero-text {
  font-size: 1.15rem;
  color: var(--text-muted);
  max-width: 620px;
  margin-inline: auto;
  margin-bottom: 2rem;
}

.hero-actions {
  display: flex;
  justify-content: center;
  flex-wrap: wrap;
  gap: 14px;
}

.btn {
  display: inline-block;
  padding: 13px 28px;
  border-radius: 100px;
  font-weight: 600;
  font-size: 0.95rem;
  transition: transform 0.2s ease, box-shadow 0.2s ease, background 0.2s ease;
}

.btn:hover {
  text-decoration: none;
  transform: translateY(-2px);
}

.btn-primary {
  background: var(--accent);
  color: #fff;
  box-shadow: 0 4px 14px rgba(193, 122, 94, 0.35);
}

.btn-primary:hover {
  background: var(--accent-dark);
  box-shadow: 0 6px 20px rgba(193, 122, 94, 0.4);
}

.btn-secondary {
  background: var(--surface);
  color: var(--text);
  border: 1px solid var(--border);
}

.btn-secondary:hover {
  background: var(--bg-alt);
}

/* ===== Sections ===== */
.section {
  padding: clamp(3.5rem, 8vw, 5.5rem) 0;
}

.section-heading {
  text-align: center;
  max-width: 700px;
  margin-inline: auto;
  margin-bottom: 3rem;
}

.section-heading .eyebrow {
  margin-bottom: 0.8rem;
}

.section-heading h2 {
  margin-bottom: 0.8rem;
}

.section-sub {
  color: var(--text-muted);
  font-size: 1.05rem;
}

/* ===== About ===== */
.about {
  background: var(--surface);
}

.about-grid {
  display: grid;
  grid-template-columns: 1.4fr 1fr;
  gap: 2.5rem;
  align-items: start;
}

.about-text p:last-child {
  margin-bottom: 0;
}

.about-card {
  background: var(--bg);
  border: 1px solid var(--border);
  border-radius: var(--radius);
  padding: 1.8rem;
  box-shadow: var(--shadow);
}

.about-card h3 {
  margin-bottom: 1rem;
  color: var(--accent-dark);
}

.fact-list {
  list-style: none;
  display: grid;
  gap: 0.9rem;
}

.fact-list li {
  padding-left: 1.4rem;
  position: relative;
  font-size: 0.95rem;
}

.fact-list li::before {
  content: "✓";
  position: absolute;
  left: 0;
  top: 0;
  color: var(--accent);
  font-weight: 700;
}

.fact-list strong {
  display: block;
  font-size: 0.85rem;
  text-transform: uppercase;
  letter-spacing: 0.06em;
  color: var(--text-muted);
  margin-bottom: 2px;
}

/* ===== Cards ===== */
.forms {
  background: var(--bg);
}

.card-grid {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(280px, 1fr));
  gap: 1.5rem;
}

.card {
  background: var(--surface);
  border: 1px solid var(--border);
  border-radius: var(--radius);
  padding: 1.8rem;
  box-shadow: var(--shadow);
  transition: transform 0.25s ease, box-shadow 0.25s ease;
}

.card:hover {
  transform: translateY(-4px);
  box-shadow: 0 8px 28px rgba(45, 42, 50, 0.1);
}

.card-icon {
  font-size: 2rem;
  margin-bottom: 0.8rem;
}

.card h3 {
  margin-bottom: 0.6rem;
}

.card p {
  color: var(--text-muted);
  font-size: 0.95rem;
  margin-bottom: 0;
}

/* ===== Psychology ===== */
.psychology {
  background: var(--surface);
}

.psych-grid {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(240px, 1fr));
  gap: 1.5rem;
}

.psych-item {
  background: var(--bg);
  border: 1px solid var(--border);
  border-radius: var(--radius);
  padding: 1.8rem;
  border-top: 3px solid var(--accent);
}

.psych-item h3 {
  color: var(--accent-dark);
  margin-bottom: 0.6rem;
  font-size: 1.05rem;
}

.psych-item p {
  color: var(--text-muted);
  font-size: 0.95rem;
  margin-bottom: 0;
}

/* ===== Consent ===== */
.consent {
  background:
    linear-gradient(rgba(45, 42, 50, 0.94), rgba(45, 42, 50, 0.94)),
    repeating-linear-gradient(45deg, transparent, transparent 10px, rgba(255, 255, 255, 0.02) 10px, rgba(255, 255, 255, 0.02) 20px);
  color: #f5f1ec;
}

.consent .section-heading .eyebrow {
  background: rgba(193, 122, 94, 0.25);
  color: #e8b4a0;
}

.consent .section-heading h2 {
  color: #fff;
}

.consent-grid {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(240px, 1fr));
  gap: 1.5rem;
  margin-bottom: 2.5rem;
}

.consent-card {
  background: rgba(255, 255, 255, 0.06);
  border: 1px solid rgba(255, 255, 255, 0.14);
  border-radius: var(--radius);
  padding: 1.8rem;
}

.consent-card h3 {
  color: #e8b4a0;
  margin-bottom: 0.6rem;
  font-size: 1.05rem;
}

.consent-card p {
  color: rgba(245, 241, 236, 0.85);
  font-size: 0.95rem;
  margin-bottom: 0;
}

.consent-note {
  background: rgba(193, 122, 94, 0.15);
  border: 1px solid rgba(193, 122, 94, 0.4);
  border-radius: var(--radius);
  padding: 1.5rem 1.8rem;
  text-align: center;
}

.consent-note p {
  margin-bottom: 0;
  color: #f5f1ec;
  font-size: 1.05rem;
}

.consent-note strong {
  color: #e8b4a0;
}

/* ===== Myths ===== */
.myths {
  background: var(--bg);
}

.myth-list {
  display: grid;
  gap: 1.2rem;
  max-width: 800px;
  margin-inline: auto;
}

.myth-item {
  background: var(--surface);
  border: 1px solid var(--border);
  border-radius: var(--radius);
  padding: 1.5rem 1.8rem;
  box-shadow: var(--shadow);
}

.myth-item p {
  margin-bottom: 0;
  font-size: 1rem;
}

.myth-item p + p {
  margin-top: 0.4rem;
}

.myth-label {
  display: inline-block;
  font-size: 0.72rem;
  font-weight: 700;
  text-transform: uppercase;
  letter-spacing: 0.1em;
  background: #f0d9d0;
  color: #a05f47;
  padding: 3px 10px;
  border-radius: 100px;
  margin-bottom: 0.4rem;
}

.myth-label.reality {
  background: #dce8dc;
  color: #4a7a4a;
  margin-top: 0.8rem;
}

/* ===== FAQ ===== */
.faq {
  background: var(--surface);
}

.faq-list {
  max-width: 780px;
  margin-inline: auto;
  display: grid;
  gap: 0.8rem;
}

.faq-item {
  border: 1px solid var(--border);
  border-radius: var(--radius);
  background: var(--bg);
  overflow: hidden;
}

.faq-question {
  width: 100%;
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 16px;
  padding: 1.2rem 1.5rem;
  background: none;
  border: none;
  font-family: inherit;
  font-size: 1rem;
  font-weight: 600;
  color: var(--text);
  text-align: left;
  cursor: pointer;
  transition: background 0.2s ease;
}

.faq-question:hover {
  background: var(--accent-soft);
}

.faq-question:focus-visible {
  outline: 3px solid var(--accent);
  outline-offset: -3px;
}

.faq-icon {
  position: relative;
  flex-shrink: 0;
  width: 18px;
  height: 18px;
}

.faq-icon::before,
.faq-icon::after {
  content: "";
  position: absolute;
  background: var(--accent-dark);
  border-radius: 2px;
  transition: transform 0.3s ease;
}

.faq-icon::before {
  width: 18px;
  height: 2px;
  top: 8px;
  left: 0;
}

.faq-icon::after {
  width: 2px;
  height: 18px;
  left: 8px;
  top: 0;
}

.faq-question[aria-expanded="true"] .faq-icon::after {
  transform: scaleY(0);
}

.faq-answer {
  max-height: 0;
  overflow: hidden;
  transition: max-height 0.35s ease;
}

.faq-answer-inner {
  padding: 0 1.5rem 1.4rem;
  color: var(--text-muted);
  font-size: 0.97rem;
}

.faq-answer p {
  margin-bottom: 0;
}

/* ===== Footer ===== */
.site-footer {
  background: var(--text);
  color: rgba(255, 255, 255, 0.75);
  padding: 3rem 0 2.5rem;
}

.footer-inner {
  text-align: center;
  display: grid;
  gap: 1rem;
  justify-items: center;
}

.footer-brand {
  display: inline-flex;
  align-items: center;
  gap: 10px;
  font-weight: 700;
  font-size: 1.2rem;
  color: #fff;
}

.footer-text {
  max-width: 520px;
  font-size: 0.9rem;
  margin-bottom: 0;
}

.footer-note {
  font-size: 0.85rem;
  color: rgba(255, 255, 255, 0.5);
  margin-bottom: 0;
}

/* ===== Focus states ===== */
a:focus-visible,
button:focus-visible {
  outline: 3px solid var(--accent);
  outline-offset: 3px;
  border-radius: 4px;
}

/* ===== Responsive ===== */
@media (max-width: 860px) {
  .about-grid {
    grid-template-columns: 1fr;
  }

  .nav-toggle {
    display: flex;
  }

  .nav-menu {
    position: absolute;
    top: 68px;
    left: 0;
    right: 0;
    flex-direction: column;
    background: var(--bg);
    border-bottom: 1px solid var(--border);
    padding: 1rem;
    gap: 4px;
    display: none;
    box-shadow: 0 12px 24px rgba(45, 42, 50, 0.08);
  }

  .nav-menu.open {
    display: flex;
  }

  .nav-menu a {
    padding: 12px 16px;
    font-size: 1rem;
  }
}

@media (max-width: 520px) {
  .hero-actions {
    flex-direction: column;
    align-items: stretch;
  }

  .btn {
    text-align: center;
  }

  .myth-item {
    padding: 1.2rem;
  }

  .faq-question {
    padding: 1rem 1.2rem;
    font-size: 0.95rem;
  }

  .faq-answer-inner {
    padding: 0 1.2rem 1.2rem;
  }
}

@media (prefers-reduced-motion: reduce) {
  html {
    scroll-behavior: auto;
  }

  *,
  *::before,
  *::after {
    transition: none !important;
    animation: none !important;
  }
}
```

### `src/components/Header.tsx`

```typescript
import React, { useState, useEffect, useRef } from 'react';

const Header: React.FC = () => {
  const [isNavOpen, setIsNavOpen] = useState(false);
  const navMenuRef = useRef<HTMLUListElement>(null);
  const navToggleRef = useRef<HTMLButtonElement>(null);

  const handleToggleNav = () => {
    setIsNavOpen((prev) => !prev);
  };

  const handleNavLinkClick = () => {
    setIsNavOpen(false);
  };

  useEffect(() => {
    const handleOutsideClick = (event: MouseEvent) => {
      if (
        isNavOpen &&
        navMenuRef.current &&
        !navMenuRef.current.contains(event.target as Node) &&
        navToggleRef.current &&
        !navToggleRef.current.contains(event.target as Node)
      ) {
        setIsNavOpen(false);
      }
    };

    const handleEscapeKey = (event: KeyboardEvent) => {
      if (event.key === 'Escape' && isNavOpen) {
        setIsNavOpen(false);
        navToggleRef.current?.focus();
      }
    };

    document.addEventListener('mousedown', handleOutsideClick);
    document.addEventListener('keydown', handleEscapeKey);

    return () => {
      document.removeEventListener('mousedown', handleOutsideClick);
      document.removeEventListener('keydown', handleEscapeKey);
    };
  }, [isNavOpen]);

  return (
    <header className="site-header">
      <div className="container header-inner">
        <a href="#top" className="brand" aria-label="Sole &amp; Soul home">
          <span className="brand-mark" aria-hidden="true">🦶</span>
          <span className="brand-name">Sole &amp; Soul</span>
        </a>
        <nav className="site-nav" aria-label="Main navigation">
          <button
            ref={navToggleRef}
            className="nav-toggle"
            aria-expanded={isNavOpen}
            aria-controls="navMenu"
            aria-label={isNavOpen ? 'Close menu' : 'Menu'}
            onClick={handleToggleNav}
          >
            <span className="nav-toggle-bar"></span>
            <span className="nav-toggle-bar"></span>
            <span className="nav-toggle-bar"></span>
            <span className="sr-only">Menu</span>
          </button>
          <ul ref={navMenuRef} className={`nav-menu ${isNavOpen ? 'open' : ''}`} id="navMenu">
            <li><a href="#about" onClick={handleNavLinkClick}>About</a></li>
            <li><a href="#forms" onClick={handleNavLinkClick}>Forms</a></li>
            <li><a href="#psychology" onClick={handleNavLinkClick}>Psychology</a></li>
            <li><a href="#consent" onClick={handleNavLinkClick}>Consent</a></li>
            <li><a href="#myths" onClick={handleNavLinkClick}>Myths</a></li>
            <li><a href="#faq" onClick={handleNavLinkClick}>FAQ</a></li>
          </ul>
        </nav>
      </div>
    </header>
  );
};

export default Header;
```

### `src/components/Hero.tsx`

```typescript
import React from 'react';

const Hero: React.FC = () => {
  return (
    <section className="hero" id="top">
      <div className="container hero-inner">
        <p className="eyebrow">A respectful guide</p>
        <h1>Understanding Foot Fetishism</h1>
        <p className="hero-text">
          Foot fetishism is one of the most common forms of sexual interest — yet it’s often
          misunderstood. This page offers clear, judgment-free information about what it is,
          where it comes from, and how to explore it with care and consent.
        </p>
        <div className="hero-actions">
          <a href="#about" className="btn btn-primary">Learn the basics</a>
          <a href="#faq" className="btn btn-secondary">Common questions</a>
        </div>
      </div>
    </section>
  );
};

export default Hero;
```

### `src/components/About.tsx`

```typescript
import React from 'react';

const About: React.FC = () => {
  return (
    <section className="section about" id="about">
      <div className="container">
        <div className="section-heading">
          <p className="eyebrow">The basics</p>
          <h2>What is a foot fetish?</h2>
        </div>
        <div className="about-grid">
          <div className="about-text">
            <p>
              A foot fetish — also called <strong>podophilia</strong> — is a persistent sexual
              interest in feet. People with this interest may be drawn to the shape, smell,
              texture, movement, or adornment of feet, and they often find feet arousing in
              ways that go beyond the everyday.
            </p>
            <p>
              Research suggests foot fetishism is among the most common fetishes worldwide.
              It exists across cultures, genders, and orientations, and it is generally
              considered a healthy variation of human sexuality when explored consensually.
            </p>
          </div>
          <div className="about-card">
            <h3>Quick facts</h3>
            <ul className="fact-list">
              <li><strong>Common:</strong> one of the most frequently reported fetishes</li>
              <li><strong>Diverse:</strong> attraction varies widely from person to person</li>
              <li><strong>Normal:</strong> not a disorder unless it causes distress or harm</li>
              <li><strong>Consensual:</strong> healthy when all parties are willing</li>
            </ul>
          </div>
        </div>
      </div>
    </section>
  );
};

export default About;
```

### `src/components/Forms.tsx`

```typescript
import React from 'react';

const Forms: React.FC = () => {
  return (
    <section className="section forms" id="forms">
      <div className="container">
        <div className="section-heading">
          <p className="eyebrow">Many expressions</p>
          <h2>How it can show up</h2>
          <p className="section-sub">Foot interest is not one-size-fits-all. Here are some common expressions.</p>
        </div>
        <div className="card-grid">
          <article className="card">
            <div className="card-icon" aria-hidden="true">👀</div>
            <h3>Visual appreciation</h3>
            <p>Enjoying the look of feet — their arches, toes, shape, or how they move. This can include photography, art, or simply admiring in person.</p>
          </article>
          <article className="card">
            <div className="card-icon" aria-hidden="true">🤲</div>
            <h3>Tactile interest</h3>
            <p>Focus on touch: massaging, holding, kissing, or otherwise engaging with feet physically. Many find this deeply intimate and relaxing.</p>
          </article>
          <article className="card">
            <div className="card-icon" aria-hidden="true">👃</div>
            <h3>Scent &amp; sensory</h3>
            <p>Attraction to natural foot scent or the sensory experience of feet after activity. This is often about closeness and authenticity.</p>
          </article>
          <article className="card">
            <div className="card-icon" aria-hidden="true">👠</div>
            <h3>Footwear &amp; adornment</h3>
            <p>Interest in shoes, socks, stockings, jewelry, or nail polish. The object is often a stand-in for the foot itself or enhances its appeal.</p>
          </article>
          <article className="card">
            <div className="card-icon" aria-hidden="true">🦶</div>
            <h3>Role &amp; worship</h3>
            <p>Some enjoy acts of “foot worship” — a ritualized form of attention that can involve massage, kissing, and devoted care within a power dynamic.</p>
          </article>
          <article className="card">
            <div className="card-icon" aria-hidden="true">💬</div>
            <h3>Verbal &amp; imagined</h3>
            <p>For some, the interest lives mainly in fantasy, conversation, or storytelling. It can be a private part of their inner life.</p>
          </article>
        </div>
      </div>
    </section>
  );
};

export default Forms;
```

### `src/components/Psychology.tsx`

```typescript
import React from 'react';

const Psychology: React.FC = () => {
  return (
    <section className="section psychology" id="psychology">
      <div className="container">
        <div className="section-heading">
          <p className="eyebrow">The why</p>
          <h2>Where does it come from?</h2>
        </div>
        <div className="psych-grid">
          <div className="psych-item">
            <h3>Brain mapping</h3>
            <p>
              In the brain, the sensory and motor areas for the feet sit directly next to
              those for the genitals. Some researchers believe this proximity can cause
              neural “cross-wiring,” linking foot stimulation with sexual response.
            </p>
          </div>
          <div className="psych-item">
            <h3>Early association</h3>
            <p>
              Many fetishes form through early experiences — a moment of arousal or
              excitement that becomes paired with a particular stimulus. A childhood
              memory, a first crush, or a formative encounter can leave a lasting imprint.
            </p>
          </div>
          <div className="psych-item">
            <h3>Cultural visibility</h3>
            <p>
              Feet are everywhere — in media, fashion, and daily life. Their constant
              presence makes them an accessible and easily associated object of desire.
            </p>
          </div>
          <div className="psych-item">
            <h3>Intimacy &amp; vulnerability</h3>
            <p>
              Feet are often considered a private, vulnerable part of the body. Interest in
              them can be tied to a desire for closeness, trust, and acceptance.
            </p>
          </div>
        </div>
      </div>
    </section>
  );
};

export default Psychology;
```

### `src/components/Consent.tsx`

```typescript
import React from 'react';

const Consent: React.FC = () => {
  return (
    <section className="section consent" id="consent">
      <div className="container">
        <div className="section-heading">
          <p className="eyebrow">The most important part</p>
          <h2>Consent &amp; communication</h2>
        </div>
        <div className="consent-grid">
          <div className="consent-card">
            <h3>Ask, don’t assume</h3>
            <p>
              Never introduce a fetish into a relationship without discussing it first.
              A simple, honest conversation — “I’m attracted to feet, how do you feel
              about that?” — goes a long way.
            </p>
          </div>
          <div className="consent-card">
            <h3>Respect boundaries</h3>
            <p>
              A partner may be curious, enthusiastic, or uninterested. All three are valid.
              Enthusiastic consent means everyone genuinely wants to participate — not
              that anyone was talked into it.
            </p>
          </div>
          <div className="consent-card">
            <h3>Hygiene &amp; comfort</h3>
            <p>
              Foot-focused activities often involve close contact. Discuss hygiene
              preferences, comfort levels, and any physical sensitivities openly and
              without embarrassment.
            </p>
          </div>
          <div className="consent-card">
            <h3>Keep it private</h3>
            <p>
              What happens between consenting adults is personal. Don’t share photos,
              stories, or details about a partner without their explicit permission.
            </p>
          </div>
        </div>
        <div className="consent-note">
          <p>
            💡 <strong>Golden rule:</strong> a fetish is a way to connect, not a demand to make.
            The moment it stops being mutual, it stops being okay.
          </p>
        </div>
      </div>
    </section>
  );
};

export default Consent;
```

### `src/components/Myths.tsx`

```typescript
import React from 'react';

const Myths: React.FC = () => {
  return (
    <section className="section myths" id="myths">
      <div className="container">
        <div className="section-heading">
          <p className="eyebrow">Separating fact from fiction</p>
          <h2>Myths &amp; realities</h2>
        </div>
        <div className="myth-list">
          <div className="myth-item">
            <div className="myth-label">Myth</div>
            <p>“Foot fetishes are rare and weird.”</p>
            <div className="myth-label reality">Reality</div>
            <p>They’re among the most common fetishes, reported by people of all backgrounds.</p>
          </div>
          <div className="myth-item">
            <div className="myth-label">Myth</div>
            <p>“Having a fetish means something is wrong with you.”</p>
            <div className="myth-label reality">Reality</div>
            <p>Consensual fetishism is a normal part of human sexuality, not a mental illness.</p>
          </div>
          <div className="myth-item">
            <div className="myth-label">Myth</div>
            <p>“People with foot fetishes can’t enjoy other intimacy.”</p>
            <div className="myth-label reality">Reality</div>
            <p>Most people with fetishes enjoy a full range of intimacy — it’s one interest among many.</p>
          </div>
          <div className="myth-item">
            <div className="myth-label">Myth</div>
            <p>“It’s always about dominance or submission.”</p>
            <div className="myth-label reality">Reality</div>
            <p>Some enjoy power dynamics, but many simply appreciate feet aesthetically or sensually.</p>
          </div>
        </div>
      </div>
    </section>
  );
};

export default Myths;
```

### `src/components/FAQ.tsx`

```typescript
import React, { useState, useRef } from 'react';

interface FaqItemProps {
  question: string;
  answer: string;
  isOpen: boolean;
  onToggle: () => void;
}

const FaqItem: React.FC<FaqItemProps> = ({ question, answer, isOpen, onToggle }) => {
  const answerRef = useRef<HTMLDivElement>(null);

  return (
    <div className="faq-item">
      <button
        className="faq-question"
        aria-expanded={isOpen}
        onClick={onToggle}
      >
        {question}
        <span className="faq-icon" aria-hidden="true"></span>
      </button>
      <div
        ref={answerRef}
        className="faq-answer"
        style={{ maxHeight: isOpen ? `${answerRef.current?.scrollHeight}px` : '0px' }}
      >
        <div className="faq-answer-inner">
          <p>{answer}</p>
        </div>
      </div>
    </div>
  );
};

const faqData = [
  {
    question: "Is a foot fetish something I should be ashamed of?",
    answer: "No. Having a foot fetish is a common, normal variation of human sexuality. Shame usually comes from stigma, not from the interest itself. What matters is how you treat others — with respect and consent.",
  },
  {
    question: "How do I tell my partner about my interest?",
    answer: "Choose a calm, private moment. Be honest and low-pressure: “I have an attraction to feet and I’d love to explore that with you if you’re open to it.” Give them space to respond without pushing for an immediate answer.",
  },
  {
    question: "What if my partner isn’t interested?",
    answer: "That’s okay. A fetish is an invitation, not a requirement. Respect their boundary and focus on the intimacy you do share. If the interest is essential to your happiness and your partner is unwilling, couples therapy or honest reflection can help you decide what’s right for you.",
  },
  {
    question: "Is foot fetishism considered a disorder?",
    answer: "Only if it causes significant distress, impairment, or involves non-consenting people. For most, it’s simply a preference. If you feel distressed about your interests, a sex-positive therapist can help — not to “cure” you, but to help you build a healthy relationship with yourself.",
  },
  {
    question: "Are there safe ways to explore online?",
    answer: "Yes. Many communities exist for people with shared interests. Follow basic safety rules: never share identifying photos with strangers, verify ages, respect content boundaries, and remember that anything posted online can spread beyond your control.",
  },
];

const FAQ: React.FC = () => {
  const [openIndex, setOpenIndex] = useState<number | null>(null);

  const handleToggle = (index: number) => {
    setOpenIndex(openIndex === index ? null : index);
  };

  return (
    <section className="section faq" id="faq">
      <div className="container">
        <div className="section-heading">
          <p className="eyebrow">Questions</p>
          <h2>Frequently asked questions</h2>
        </div>
        <div className="faq-list">
          {faqData.map((item, index) => (
            <FaqItem
              key={index}
              question={item.question}
              answer={item.answer}
              isOpen={openIndex === index}
              onToggle={() => handleToggle(index)}
            />
          ))}
        </div>
      </div>
    </section>
  );
};

export default FAQ;
```

### `src/components/Footer.tsx`

```typescript
import React from 'react';

const Footer: React.FC = () => {
  const currentYear = new Date().getFullYear();

  return (
    <footer className="site-footer">
      <div className="container footer-inner">
        <div className="footer-brand">
          <span className="brand-mark" aria-hidden="true">🦶</span>
          <span className="brand-name">Sole &amp; Soul</span>
        </div>
        <p className="footer-text">
          An educational resource about foot fetishism. This site discusses human sexuality
          in a respectful, non-explicit manner and is intended for adults.
        </p>
        <p className="footer-note">© <span>{currentYear}</span> Sole &amp; Soul. Be kind, be consensual.</p>
      </div>
    </footer>
  );
};

export default Footer;
```
